"""Physical controls for the nine-pair fixture's pad-departure repair (issue #9989)."""

import json
import sys
from itertools import pairwise
from math import dist, sqrt
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from shapely.geometry import LineString, box

from kicad_tools.router.rules import net_class_map_from_path, net_class_map_to_dict
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.diffpair_skew import derive_pair_skews

BOARD06 = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"


@pytest.fixture
def departures(monkeypatch):
    monkeypatch.syspath_prepend(str(BOARD06))
    import pad_departures

    return pad_departures


def _rules():
    from kicad_tools.manufacturers import get_profile

    return get_profile("jlcpcb").get_design_rules(layers=4)


def _uid(kind: str, i: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"dep/{kind}/{i}"))


def _write_board(
    path,
    *,
    pad_ref="U1",
    pad_number="13",
    pad_at=(10.0, 10.0),
    pad_size=(0.3, 0.7),
    segments=(),
    vias=(),
    far_ref="R1",
    far_at=(10.0, 6.5),
    locked_index=None,
    neighbor=True,
    extra="",
):
    """One sideband pad, its F.Cu departure, a far pad and an edge outline.

    The ``neighbor`` net's vertical track stands where the neighbouring CC
    column sits on the real board, so a sideways via slide is clearance-boxed
    and the barrel moves out along the pad axis as it does on seed 42.
    """
    items = [
        "(kicad_pcb (version 20240108) (generator test)",
        '(general (thickness 1.6)) (layers (0 "F.Cu" signal) (2 "In1.Cu" power)'
        ' (4 "In2.Cu" power) (31 "B.Cu" signal))',
        '(setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "USB_CC2")',
        "(gr_rect (start 5 4) (end 16 14) (stroke (width 0.05) (type default))"
        ' (fill none) (layer "Edge.Cuts"))',
        f'(footprint "QFP" (layer "F.Cu") (at {pad_at[0]} {pad_at[1]})'
        f' (property "Reference" "{pad_ref}")'
        f' (pad "{pad_number}" smd rect (at 0 0) (size {pad_size[0]} {pad_size[1]})'
        ' (layers "F.Cu" "F.Mask") (net 1 "USB_CC2")))',
        f'(footprint "Hdr" (layer "F.Cu") (at {far_at[0]} {far_at[1]})'
        f' (property "Reference" "{far_ref}")'
        ' (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.4)'
        ' (layers "*.Cu" "*.Mask") (net 1 "USB_CC2")))',
    ]
    if neighbor:
        low = min(pad_at[1], far_at[1]) - 1.5
        high = max(pad_at[1], far_at[1]) + 1.5
        items.extend(
            [
                '(net 2 "USB_CC3")',
                f"(segment (start {pad_at[0] + 0.6} {low}) (end {pad_at[0] + 0.6} {high})"
                ' (width 0.2) (layer "F.Cu") (net 2)'
                f' (uuid "{_uid("nbr", 0)}"))',
                f"(segment (start {pad_at[0] + 0.6} {low}) (end {pad_at[0] + 0.6} {high})"
                ' (width 0.2) (layer "In2.Cu") (net 2)'
                f' (uuid "{_uid("nbr", 1)}"))',
            ]
        )
    for i, (layer, start, end, width) in enumerate(segments):
        lock = "(locked yes)" if locked_index == i else ""
        items.append(
            f"(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]})"
            f' (width {width}) (layer "{layer}") (net 1) {lock} (uuid "{_uid("seg", i)}"))'
        )
    for i, (x, y) in enumerate(vias):
        items.append(
            f'(via (at {x} {y}) (size 0.6) (drill 0.25) (layers "F.Cu" "B.Cu")'
            f' (net 1) (uuid "{_uid("via", i)}"))'
        )
    path.write_text("\n".join([*items, extra, ")"]))


def _taper(depth=1.0, step=0.05, top=0.1016):
    """A U1.13-style widening staircase running ``depth`` mm up from the pad."""
    segments = []
    y = 0.0
    i = 0
    while depth - y > step - 1e-9:
        width = round(top + (0.4 - top) * (y / depth), 4)
        segments.append(("F.Cu", (10, 10 - y - step), (10, 10 - y), width))
        y += step
        i += 1
    return segments


def _fc(pcb, net="USB_CC2"):
    return [s for s in pcb.segments if s.layer == "F.Cu" and s.net_name == net]


def _neck_and_piece(path, net="USB_CC2"):
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(path)
    front = _fc(pcb, net)
    neck = next(s for s in front if abs(s.width - 0.18) < 1e-9)
    piece = next(
        s
        for s in front
        if s is not neck
        and s.width > 0.2
        and {_round(s.start), _round(s.end)} & {_round(neck.start), _round(neck.end)}
    )
    return pcb, neck, piece


def _round(point):
    return round(point[0], 3), round(point[1], 3)


def test_tapered_departure_becomes_one_straight_neck_and_keeps_the_trunk(tmp_path, departures):
    from kicad_tools.validate.connectivity import ConnectivityValidator

    board = tmp_path / "taper.kicad_pcb"
    segments = [*_taper(), ("F.Cu", (10, 9.0), (10, 6.5), 0.4)]
    _write_board(board, segments=segments, vias=[])
    trunk_uuid = _uid("seg", len(segments) - 1)
    assert departures.clear_pad_departures(board, _rules()) == 1
    pcb, neck, piece = _neck_and_piece(board)
    center = pcb.get_pad_position("U1", "13")
    # The neck leaves the pad center along the escape line and stays under the
    # impedance rule's neck-down exemption (the 1.00/1.10 mm negative control).
    assert _round(center) in (_round(neck.start), _round(neck.end))
    assert 0.4 < dist(neck.start, neck.end) < departures.NECK_MAX_MM
    # The wide copper clears the pad opening by the mask warning band.
    cx, cy = center
    opening = box(cx - 0.15, cy - 0.35, cx + 0.15, cy + 0.35)
    piece_copper = LineString([piece.start, piece.end]).buffer(piece.width / 2)
    assert piece_copper.distance(opening) >= departures.BAND_MM - 1e-9
    # The original trunk is preserved, connectivity holds, and the pass is done.
    assert any(s.uuid == trunk_uuid for s in _fc(pcb))
    assert not ConnectivityValidator(board).validate().issues
    saved = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == saved


def test_over_wide_departure_to_a_via_gets_a_neck(tmp_path, departures):
    board = tmp_path / "wide.kicad_pcb"
    _write_board(
        board,
        pad_size=(0.3, 0.6),
        segments=[
            ("F.Cu", (10, 10), (10, 11.1), 0.4),
            ("In2.Cu", (10, 11.1), (10, 12.8), 0.4),
            ("In2.Cu", (10, 12.8), (10, 13.8), 0.4),
        ],
        vias=[(10, 11.1)],
        far_at=(10, 13.8),
    )
    assert departures.clear_pad_departures(board, _rules()) == 1
    pcb, neck, piece = _neck_and_piece(board)
    via = next(v for v in pcb.vias if v.net_name == "USB_CC2")
    assert piece.width == pytest.approx(0.4)
    assert _round(via.position) in (_round(piece.start), _round(piece.end))
    assert len([v for v in pcb.vias if v.net_name == "USB_CC2"]) == 1
    saved = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == saved


def test_diagonal_departure_rejoins_the_kept_trunk(tmp_path, departures):

    board = tmp_path / "diag.kicad_pcb"
    segments = [
        ("F.Cu", (10, 10), (8.9, 11.1), 0.4),
        ("F.Cu", (8.9, 11.1), (8.9, 14.5), 0.4),
        ("F.Cu", (8.9, 14.5), (10, 14.5), 0.4),
    ]
    _write_board(board, pad_size=(0.3, 0.6), segments=segments, far_at=(10, 14.5))
    trunk_uuid = _uid("seg", 1)
    assert departures.clear_pad_departures(board, _rules()) == 1
    pcb, neck, piece = _neck_and_piece(board)
    center = pcb.get_pad_position("U1", "13")
    trunk = next(s for s in _fc(pcb) if s.uuid == trunk_uuid)
    # One straight 45-degree entry out of the pad; the wide piece lands on the
    # kept trunk's near endpoint, so the original destination is preserved.
    dx = abs(neck.start[0] - neck.end[0])
    dy = abs(neck.start[1] - neck.end[1])
    assert dx == pytest.approx(dy, abs=1e-6)
    assert _round(center) in (_round(neck.start), _round(neck.end))
    junction = (
        _round(trunk.start)
        if dist(trunk.start, center) < dist(trunk.end, center)
        else _round(trunk.end)
    )
    assert junction in (_round(piece.start), _round(piece.end))
    saved = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == saved


def _write_via_departure(board, *, extra_segments=(), inner_segments=None):
    if inner_segments is None:
        inner_segments = [
            ("In2.Cu", (10.2, 12.6), (10.2, 10.4), 0.4),
            ("In2.Cu", (10.2, 10.4), (10, 10.6), 0.4),
            ("In2.Cu", (10.2, 12.6), (10, 12.6), 0.4),
        ]
    _write_board(
        board,
        pad_number="12",
        pad_size=(0.3, 0.7),
        segments=[
            *(
                ("F.Cu", (10, 10 + y + 0.05), (10, 10 + y), round(0.1016 + y * 0.5, 4))
                for y in [i * 0.05 for i in range(12)]
            ),
            *inner_segments,
            *extra_segments,
        ],
        vias=[(10, 10.6)],
        far_at=(10, 12.6),
    )


@pytest.mark.parametrize("return_x", [9.0, 10.5], ids=["away", "crossing"])
@pytest.mark.parametrize(("layer", "x"), [("F.Cu", 9.76), ("In2.Cu", 9.72)])
def test_via_slide_preserves_the_contact_at_a_returning_branch(
    tmp_path, monkeypatch, layer, x, return_x
):
    from scripts.ci.net_class_map_resolver import load_board_recipe_module

    board = tmp_path / "returning-branch.kicad_pcb"
    _write_via_departure(
        board,
        extra_segments=[
            (layer, (x, 10.45), (return_x, 10.45), 0.20),
            (layer, (return_x, 10.45), (return_x, 12.6), 0.20),
            (layer, (return_x, 12.6), (10, 12.6), 0.20),
        ],
    )
    original = board.read_bytes()
    monkeypatch.syspath_prepend(str(BOARD06))
    recipe = load_board_recipe_module(BOARD06)
    # The fixture has no zones to refill or audit.
    recipe._finalize_signal_copper(board, [sys.executable, "-c", "pass"], lambda tag: True)
    assert board.read_bytes() == original


def test_via_slide_keeps_the_inner_taper_connected_to_its_new_stub(tmp_path, departures):
    from kicad_tools.schema.pcb import PCB

    board = tmp_path / "inner-taper.kicad_pcb"
    # The old annulus also touched two earlier pieces of this continuous taper.
    points = [
        (10, 12.6),
        (11.3, 12.6),
        (11.3, 10.1),
        (10.2, 10.1),
        (10.2, 10.25),
        (10.2, 10.30),
        (10.2, 10.35),
        (10.2, 10.40),
        (10.15, 10.45),
        (10.10, 10.50),
        (10.05, 10.55),
        (10, 10.60),
    ]
    widths = [0.35, 0.35, 0.35, 0.18, 0.19, 0.2, 0.21, 0.23, 0.25, 0.27, 0.29]
    inner = [
        ("In2.Cu", start, end, width)
        for start, end, width in zip(points[:-1], points[1:], widths, strict=True)
    ]
    _write_via_departure(board, inner_segments=inner)
    old_seat = _round(PCB.load(board).vias[0].position)
    assert departures.clear_pad_departures(board, _rules()) == 1
    pcb = PCB.load(board)
    via = next(v for v in pcb.vias if v.net_name == "USB_CC2")
    assert _round(via.position) != old_seat
    assert any(
        {_round(s.start), _round(s.end)} == {old_seat, _round(via.position)}
        for s in pcb.segments
        if s.layer == "In2.Cu"
    )


def test_relocated_departure_keeps_the_inner_extension(tmp_path, departures):
    from kicad_tools.validate.connectivity import ConnectivityValidator

    board = tmp_path / "slid.kicad_pcb"
    _write_via_departure(board)
    assert departures.clear_pad_departures(board, _rules()) == 1
    pcb, neck, piece = _neck_and_piece(board)
    center = pcb.get_pad_position("U1", "12")
    via = next(v for v in pcb.vias if v.net_name == "USB_CC2")
    # The barrel slid out of the ring dam, size and drill unchanged, and the
    # rebuilt escape reaches the new seat.
    assert via.position[1] > center[1] + 0.7
    assert (via.size, via.drill) == (pytest.approx(0.6), pytest.approx(0.25))
    assert _round(via.position) in (_round(piece.start), _round(piece.end))
    assert not ConnectivityValidator(board).validate().issues
    # The In2 approach keeps its landing at the old seat through the stub.
    old_seat = _round((center[0], center[1] + 0.6))
    in2 = [s for s in pcb.segments if s.layer == "In2.Cu" and s.net_name == "USB_CC2"]
    assert any({_round(s.start), _round(s.end)} & {old_seat} for s in in2)
    saved = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == saved


def test_a_via_already_clear_of_the_ring_dam_moves_nothing(tmp_path, departures):
    board = tmp_path / "clear.kicad_pcb"
    _write_board(
        board,
        pad_number="12",
        pad_size=(0.3, 0.7),
        segments=[
            ("F.Cu", (10, 10), (10, 11.1), 0.4),
            ("In2.Cu", (10, 11.1), (10, 12.8), 0.4),
            ("In2.Cu", (10, 12.8), (10, 13.8), 0.4),
        ],
        vias=[(10, 11.1)],
        far_at=(10, 13.8),
    )
    original = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == original


def test_locked_staircase_is_left_alone(tmp_path, departures):
    board = tmp_path / "locked.kicad_pcb"
    _write_board(board, segments=[*_taper(), ("F.Cu", (10, 9.0), (10, 6.5), 0.4)], locked_index=3)
    original = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == original


def test_group_locked_neck_is_left_alone(tmp_path, departures):
    board = tmp_path / "group.kicad_pcb"
    segments = [*_taper(), ("F.Cu", (10, 9.0), (10, 6.5), 0.4)]
    _write_board(board, segments=segments)
    text = board.read_text().rstrip()[:-1]
    board.write_text(text + f' (group "neck" (locked yes) (members "{_uid("seg", 2)}")))\n')
    original = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == original


def test_branched_pad_escape_is_left_alone(tmp_path, departures):
    board = tmp_path / "branched.kicad_pcb"
    _write_board(
        board,
        segments=[
            *_taper(),
            ("F.Cu", (10, 9.0), (10, 6.5), 0.4),
            ("F.Cu", (10, 9.7), (11.4, 9.7), 0.4),
            ("F.Cu", (11.4, 9.7), (11.4, 6.5), 0.4),
            ("F.Cu", (11.4, 6.5), (10, 6.5), 0.4),
        ],
    )
    original = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == original


def test_neck_that_would_reach_the_impedance_exemption_is_refused(tmp_path, departures):
    """The 1.00/1.10 mm negative control: a long pad cannot be escaped in one neck."""
    board = tmp_path / "toolong.kicad_pcb"
    _write_board(
        board,
        pad_size=(0.3, 1.9),
        segments=[("F.Cu", (10, 10), (10, 11.9), 0.4)],
        vias=[(10, 11.9)],
        far_at=(10, 13.8),
    )
    original = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == original


# --- MIPI clock pair exits (issue #10083) -------------------------------------------------

_CLOCK_P = [(6.8, 70.0), (6.85, 69.95), (6.85, 69.7), (7.1, 69.45), (7.1, 69.0461)]
_CLOCK_P_TRUNK = [(7.1, 68.35), (7.85, 67.6), (26.0835, 67.6)]


def _clock_pair(path, control="", p_jog=1.0, gap=0.1474):
    """J4 and U4's clock pads as the seed-42 router leaves them.

    Every exit is a stair of narrow pieces, each pad's trunk is 0.225 mm, and the
    negative rail reaches B.Cu through a barrel at each end. ``p_jog`` adds
    positive-rail length through a bump in its trunk: at 1.0 mm the pair models
    15.3 ps before the repair and 18.5 ps after, against the 20.135 ps budget.
    """
    from generate_design import mipi_net_class

    pair = {net: mipi_net_class() for net in ("MIPI_CLK+", "MIPI_CLK-")}
    path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(pair)))
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {
                        "rules": {
                            "min_clearance": 0.1016,
                            "min_track_width": 0.1016,
                            "min_through_hole_diameter": 0.20,
                            "min_via_hole": 0.20,
                        },
                    },
                },
                "net_settings": {"classes": [{"name": "Default", "clearance": 0.15}]},
            }
        )
    )
    parts = [
        "(kicad_pcb (version 20240108) (generator test)",
        '(layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal)'
        ' (36 "B.SilkS" user) (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user)'
        ' (44 "Edge.Cuts" user))',
        "(gr_rect (start 0 0) (end 35 80) (stroke (width 0.05) (type default))"
        ' (fill none) (layer "Edge.Cuts"))',
        '(net 1 "MIPI_CLK+") (net 2 "MIPI_CLK-") (net 3 "MIPI_D0+")',
        '(footprint "Connector_FFC:FFC_4P_0.5mm" (layer "F.Cu") (at 0 0)',
        '(property "Reference" "J4" (at 0 0) (layer "F.SilkS"))',
        '(pad "1" smd rect (at 6.8 70) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 1 "MIPI_CLK+"))',
        '(pad "2" smd rect (at 7.6 70) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 2 "MIPI_CLK-"))',
        '(pad "3" smd rect (at 8.4 70) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 3 "MIPI_D0+")))',
        '(footprint "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm" (layer "F.Cu") (at 0 0)',
        '(property "Reference" "U4" (at 0 0) (layer "F.SilkS"))',
        '(pad "1" smd rect (at 27 68) (size 0.7 0.3) (layers "F.Cu" "F.Mask") (net 1 "MIPI_CLK+"))',
        '(pad "2" smd rect (at 27 68.8) (size 0.7 0.3) (layers "F.Cu" "F.Mask") (net 2 "MIPI_CLK-")))',
    ]
    count = [0]

    def run(net, layer, points, widths, locked=False):
        # Split each leg in two so every stair has interior joints.
        for (a, b), width in zip(pairwise(points), widths, strict=True):
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            for start, end in ((a, mid), (mid, b)):
                count[0] += 1
                parts.append(
                    f"(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) "
                    f'(width {width}) (layer "{layer}") (net {net})'
                    + (" (locked yes)" if locked and count[0] == 3 else "")
                    + f' (uuid "{_uid("clk", count[0])}"))'
                )

    def via(net, at):
        parts.append(
            f'(via (at {at[0]} {at[1]}) (size 0.6) (drill 0.25) (layers "F.Cu" "B.Cu")'
            f' (net {net}) (uuid "{_uid("via", count[0])}"))'
        )

    run(1, "F.Cu", _CLOCK_P, [0.11, 0.14, 0.17, 0.2], locked=control == "locked")
    trunk = [_CLOCK_P[-1], *_CLOCK_P_TRUNK]
    if p_jog:
        trunk.insert(3, (14.0, 67.6))
        trunk.insert(4, (14.0, 67.6 + p_jog / 2))
        trunk.insert(5, (14.6, 67.6 + p_jog / 2))
        trunk.insert(6, (14.6, 67.6))
    run(1, "F.Cu", trunk, [0.225] * (len(trunk) - 1))
    run(1, "F.Cu", [(26.0835, 67.6), (26.6, 67.6), (27.0, 68.0)], [0.15, 0.12])
    if control == "branch":
        parts.append(
            '(segment (start 6.85 69.7) (end 6.4 69.7) (width 0.2) (layer "F.Cu") (net 1)'
            f' (uuid "{_uid("clk", 900)}"))'
        )
    run(2, "F.Cu", [(7.6, 70.0), (7.65, 70.05), (7.65, 70.6), (7.7, 70.65)], [0.11, 0.15, 0.18])
    via(2, (7.7, 70.65))
    run(2, "B.Cu", [(7.7, 70.65), (8.3599, 70.65)], [0.19])
    run(
        2,
        "B.Cu",
        [(8.3599, 70.65), (8.5, 70.65), (10.35, 68.8), (26.0, 68.8)],
        [0.225] * 3,
    )
    run(2, "B.Cu", [(26.0, 68.8), (26.8, 68.8), (27.6, 68.8)], [0.19, 0.15])
    via(2, (27.6, 68.8))
    run(2, "F.Cu", [(27.6, 68.8), (27.0, 68.8)], [0.14])
    if control in ("foreign-gap", "foreign-short"):
        # A data track beside the moved U4 barrel (0.795 mm out from the pad),
        # ``gap`` mm from its edge, or overlapping it for the short.
        edge = 27.795 + 0.3
        x = edge + 0.05 + (gap if control == "foreign-gap" else -0.025)
        parts.append(
            f"(segment (start {x} 68.3) (end {x} 69.3) (width 0.1) "
            f'(layer "F.Cu") (net 3) (uuid "{_uid("clk", 901)}"))'
        )
    path.write_text("\n".join([*parts, ")"]))


def _clock_skew(path):
    pcb = PCB.load(path)
    classes = net_class_map_from_path(path.with_name("net_class_map.json"), pcb_path=path)
    (pair,) = (
        p
        for p in derive_pair_skews(pcb, classes, num_copper_layers=len(pcb.copper_layers))
        if p.p_name == "MIPI_CLK+"
    )
    return pair.skew_ps, pair.limits.budget.budget_ps


def _clock_snapshot(pcb):
    return (
        sorted(
            (s.net_name, s.layer, _round(s.start), _round(s.end), s.width) for s in pcb.segments
        ),
        sorted((v.net_name, _round(v.position)) for v in pcb.vias),
    )


def _rail_length(pcb, net):
    return sum(dist(s.start, s.end) for s in pcb.segments if s.net_name == net)


def test_clock_exits_widen_and_move_both_negative_barrels(tmp_path, departures):
    board = tmp_path / "clock.kicad_pcb"
    _clock_pair(board)
    before = PCB.load(board)
    skew_before, budget = _clock_skew(board)
    assert departures.clear_pad_departures(board, _rules()) == 4
    saved = PCB.load(board)
    # The positive rail keeps its length exactly; the negative rail grows.
    assert _rail_length(saved, "MIPI_CLK+") == pytest.approx(
        _rail_length(before, "MIPI_CLK+"), abs=1e-6
    )
    assert _rail_length(saved, "MIPI_CLK-") > _rail_length(before, "MIPI_CLK-")
    assert sorted((v.net_name, _round(v.position), v.size, v.drill) for v in saved.vias) == [
        ("MIPI_CLK-", (8.1, 70.85), 0.6, 0.25),
        ("MIPI_CLK-", (27.795, 68.8), 0.6, 0.25),
    ]
    # Every pad exit is trunk-width copper: no narrow piece is left beside a pad.
    for ref, number in (("J4", "1"), ("J4", "2"), ("U4", "1"), ("U4", "2")):
        center = saved.get_pad_position(ref, number)
        near = [
            s
            for s in saved.segments
            if s.layer == "F.Cu"
            and s.net_name.startswith("MIPI_CLK")
            and min(dist(s.start, center), dist(s.end, center)) < 1.0
        ]
        assert near
        assert all(s.width == pytest.approx(0.225) for s in near)
    # J4's exits leave the pad straight for 0.625 mm.
    for net, sign in (("MIPI_CLK+", -1), ("MIPI_CLK-", 1)):
        center = saved.get_pad_position("J4", "1" if net == "MIPI_CLK+" else "2")
        assert any(
            _round(s.start) == _round(center)
            and _round(s.end) == _round((center[0], center[1] + sign * 0.625))
            for s in saved.segments
            if s.net_name == net
        )
    j4_negative = {
        (_round(s.start), _round(s.end))
        for s in saved.segments
        if s.layer == "F.Cu" and s.net_name == "MIPI_CLK-"
    }
    assert ((7.6, 70.625), (7.825, 70.85)) in j4_negative
    assert ((7.825, 70.85), (8.1, 70.85)) in j4_negative
    skew_after, _ = _clock_skew(board)
    assert skew_before < skew_after < budget
    from kicad_tools.validate.connectivity import ConnectivityValidator

    assert not ConnectivityValidator(board).validate().issues
    once = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == once


@pytest.mark.parametrize(
    "control",
    [
        "foreign-gap",  # the U4 barrel would leave 0.1474 mm against the 0.15 mm minimum
        "foreign-short",  # an adjacent data net the moved barrel would touch
        "locked",
        "branch",
    ],
)
def test_clock_rejections_keep_the_original_geometry(tmp_path, departures, control):
    board = tmp_path / "clock.kicad_pcb"
    _clock_pair(board, control)
    before = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == before


def test_clock_gap_control_passes_at_the_project_minimum(tmp_path, departures):
    """The 0.1474 mm rejection above is the gap, not the foreign track's presence."""
    board = tmp_path / "clock.kicad_pcb"
    _clock_pair(board, "foreign-gap", gap=0.151)
    assert departures.clear_pad_departures(board, _rules()) == 4


def test_clock_skew_breach_keeps_the_original_geometry(tmp_path, departures):
    board = tmp_path / "clock.kicad_pcb"
    # At 0.6 mm of positive-rail bump the pair models 17.5 ps now; the repair adds
    # 0.600 mm to the slower negative rail (20.8 ps) and would cross the budget.
    _clock_pair(board, p_jog=0.6)
    skew, budget = _clock_skew(board)
    assert skew < budget
    before = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == before


def test_clock_without_a_timing_policy_is_left_alone(tmp_path, departures):
    board = tmp_path / "clock.kicad_pcb"
    _clock_pair(board)
    board.with_name("net_class_map.json").unlink()
    before = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == before


def test_clock_with_unverifiable_rules_keeps_the_original_file(tmp_path, departures, monkeypatch):
    board = tmp_path / "clock.kicad_pcb"
    _clock_pair(board)
    board.with_suffix(".kicad_dru").write_text("(version 1)\n")
    before = board.read_bytes()

    def missing_toolchain(*args, **kwargs):
        raise departures.KiCadEnvironmentError("kicad-cli unavailable")

    monkeypatch.setattr(departures, "active_evidence_toolchain", lambda: None)
    monkeypatch.setattr(departures, "evidence_toolchain", missing_toolchain)
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == before


# The four cases below drive the generator's real refill transaction, whose
# `kct zones fill` child launches its own gated native checks; a slot wait
# inside that child is wall time the parent pytest alarm pays with no credit.
# CI therefore runs this module in its own serial lane with a 120 s budget
# (scripts/ci/jobs/test.sh board06, Issue #10125) instead of the bulk pool's
# 60 s cap, which a hosted run sat at 55.55 s already.
@pytest.mark.parametrize("constraint", ["keepout", "width"])
@pytest.mark.parametrize("permitted", [False, True], ids=["constrained", "permitted"])
def test_final_clock_copper_obeys_j4_rule_area(tmp_path, departures, capsys, constraint, permitted):
    from generate_design import _finalize_signal_copper

    from kicad_tools.cli.kicad_toolchain import docker_fallback_enabled
    from kicad_tools.cli.runner import find_kicad_cli

    if not docker_fallback_enabled() and find_kicad_cli() is None:
        pytest.skip("kicad-cli is needed to evaluate the authored constraints")
    board = tmp_path / "clock.kicad_pcb"
    _clock_pair(board)
    # Only the widened J4/1 exit reaches the sliver left of its original taper.
    if constraint == "keepout":
        x, y, w, h = (6.51 if permitted else 6.71), 69.53, 0.01, 0.02
        tracks = "not_allowed"
    else:
        x, y, w, h = 6.77, 69.72, 0.10, 0.08
        tracks = "allowed"
        board.with_suffix(".kicad_dru").write_text(
            '(version 1)\n(rule "Clock exit width" (layer "F.Cu") '
            "(condition \"A.NetName == 'MIPI_CLK+' && A.intersectsArea('Clock launch')\") "
            f"(constraint track_width (max {0.23 if permitted else 0.20}mm)))\n"
        )
    area = (
        '(zone (net 0) (net_name "") (layer "F.Cu") (name "Clock launch") (hatch edge 0.5)'
        f" (connect_pads (clearance 0)) (min_thickness 0.25) (keepout (tracks {tracks})"
        " (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))"
        " (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))"
        f" (polygon (pts (xy {x} {y}) (xy {x + w} {y}) (xy {x + w} {y + h}) (xy {x} {y + h}))))"
    )
    board.write_text(board.read_text().rstrip()[:-1] + area + ")\n")
    before = board.read_bytes()
    # There are no copper pours on this fixture. Run the generator's same
    # legalization and refill transaction; KiCad evaluates the actual rule area.
    fill = [sys.executable, "-m", "kicad_tools.cli", "zones", "fill", str(board)]
    _finalize_signal_copper(board, fill, lambda tag: True)
    output = capsys.readouterr().out
    if permitted:
        assert "MIPI clock pair: 4 exits widened" in output
        assert sorted(_round(v.position) for v in PCB.load(board).vias) == [
            (8.1, 70.85),
            (27.795, 68.8),
        ]
    else:
        assert board.read_bytes() == before
        assert ("Items not allowed" if constraint == "keepout" else "Clock exit width") in output


# --- MIPI data pair exits (issue #10113) --------------------------------------------------

_DATA_RUNS = {
    "MIPI_D0+": [
        [(8.4, 70.0), (8.4, 69.8672)],  # J4.3 straight exit (clean, kept)
        [(8.4, 69.8672), (9.4172, 68.85)],  # J4.3 diagonal (kept)
        [(9.4172, 68.85), (25.8828, 68.85)],  # trunk
        [(25.8828, 68.85), (26.8328, 69.8)],  # U4.3 approach diagonal
        [(26.8328, 69.8), (26.8, 69.8)],  # U4.3 jog
        [(26.8, 69.8), (27.0, 69.6)],  # U4.3 entry
    ],
    "MIPI_D0-": [
        [(9.2, 70.0), (9.0, 69.8)],  # J4.4 entry diagonal
        [(9.0, 69.8), (9.0, 69.8328)],  # J4.4 jog
        [(9.0, 69.8328), (9.5828, 69.25)],  # J4.4 approach diagonal
        [(9.5828, 69.25), (25.7172, 69.25)],  # trunk
        [(25.7172, 69.25), (26.6672, 70.2)],  # U4.4 approach diagonal
        [(26.6672, 70.2), (26.8, 70.2)],  # U4.4 jog
        [(26.8, 70.2), (27.0, 70.4)],  # U4.4 entry
    ],
}


def _data_pair(path, control="", gap=0.1474, tolerance_mm=None):
    """J4 and U4's data pads exactly as the seed-42 router leaves them.

    Every piece is trunk-width 0.225 mm on F.Cu and each leg carries an
    interior joint, so corner recognition sees the run the regen built.
    ``tolerance_mm`` tightens the class's length cap for the timing control.
    """
    import dataclasses

    from generate_design import mipi_net_class

    pair = {}
    for net in ("MIPI_D0+", "MIPI_D0-"):
        klass = mipi_net_class()
        if tolerance_mm is not None:
            klass = dataclasses.replace(klass, skew_tolerance_mm=tolerance_mm)
        pair[net] = klass
    path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(pair)))
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {
                        "rules": {
                            "min_clearance": 0.1016,
                            "min_track_width": 0.1016,
                            "min_through_hole_diameter": 0.20,
                            "min_via_hole": 0.20,
                        },
                    },
                },
                "net_settings": {"classes": [{"name": "Default", "clearance": 0.15}]},
            }
        )
    )
    parts = [
        "(kicad_pcb (version 20240108) (generator test)",
        '(layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal)'
        ' (36 "B.SilkS" user) (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user)'
        ' (44 "Edge.Cuts" user))',
        "(gr_rect (start 0 0) (end 35 80) (stroke (width 0.05) (type default))"
        ' (fill none) (layer "Edge.Cuts"))',
        '(net 1 "MIPI_D0+") (net 2 "MIPI_D0-") (net 3 "USB_CC1")',
        '(footprint "Connector_FFC:FFC_4P_0.5mm" (layer "F.Cu") (at 0 0)',
        '(property "Reference" "J4" (at 0 0) (layer "F.SilkS"))',
        '(pad "3" smd rect (at 8.4 70) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 1 "MIPI_D0+"))',
        '(pad "4" smd rect (at 9.2 70) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 2 "MIPI_D0-")))',
        '(footprint "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm" (layer "F.Cu") (at 0 0)',
        '(property "Reference" "U4" (at 0 0) (layer "F.SilkS"))',
        '(pad "3" smd rect (at 27 69.6) (size 0.7 0.3) (layers "F.Cu" "F.Mask") (net 1 "MIPI_D0+"))',
        '(pad "4" smd rect (at 27 70.4) (size 0.7 0.3) (layers "F.Cu" "F.Mask") (net 2 "MIPI_D0-")))',
    ]
    count = [0]

    def run(net_number, points):
        # Split each leg in two so every run has interior joints.
        for a, b in pairwise(points):
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            for start, end in ((a, mid), (mid, b)):
                count[0] += 1
                lock = (
                    " (locked yes)"
                    if control == "locked" and net_number == 2 and count[0] == 17
                    else ""
                )
                parts.append(
                    f"(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) "
                    f'(width 0.225) (layer "F.Cu") (net {net_number}){lock}'
                    f' (uuid "{_uid("dat", count[0])}"))'
                )

    run(1, _DATA_RUNS["MIPI_D0+"][0])
    run(1, _DATA_RUNS["MIPI_D0+"][1])
    run(1, _DATA_RUNS["MIPI_D0+"][2])
    run(1, _DATA_RUNS["MIPI_D0+"][3])
    run(1, _DATA_RUNS["MIPI_D0+"][4])
    run(1, _DATA_RUNS["MIPI_D0+"][5])
    run(2, _DATA_RUNS["MIPI_D0-"][0])
    run(2, _DATA_RUNS["MIPI_D0-"][1])
    run(2, _DATA_RUNS["MIPI_D0-"][2])
    run(2, _DATA_RUNS["MIPI_D0-"][3])
    run(2, _DATA_RUNS["MIPI_D0-"][4])
    run(2, _DATA_RUNS["MIPI_D0-"][5])
    run(2, _DATA_RUNS["MIPI_D0-"][6])
    if control == "branch":
        parts.append(
            '(segment (start 26.8328 69.8) (end 26.5 70.1) (width 0.225) (layer "F.Cu")'
            f' (net 1) (uuid "{_uid("dat", 900)}"))'
        )
    if control in ("foreign-gap", "foreign-short"):
        # A foreign track parallel to the lower half of the rebuilt U4.3
        # rejoin, ``gap`` mm of edge clearance from it (a negative offset
        # overlaps for the short).
        (x0, y0), (x1, y1) = (25.96, 68.85), (26.71, 69.6)
        offset = 0.1125 + 0.05 + (gap if control == "foreign-gap" else -0.02)
        ax, ay = x0 + (x1 - x0) * 0.15, y0 + (y1 - y0) * 0.15
        bx, by = x0 + (x1 - x0) * 0.55, y0 + (y1 - y0) * 0.55
        nx, ny = (y1 - y0), -(x1 - x0)
        norm = (nx * nx + ny * ny) ** 0.5
        parts.append(
            f"(segment (start {ax + nx / norm * offset} {ay + ny / norm * offset})"
            f" (end {bx + nx / norm * offset} {by + ny / norm * offset}) (width 0.1) "
            f'(layer "F.Cu") (net 3) (uuid "{_uid("dat", 901)}"))'
        )
    path.write_text("\n".join([*parts, ")"]))


def _data_skew(path):
    pcb = PCB.load(path)
    classes = net_class_map_from_path(path.with_name("net_class_map.json"), pcb_path=path)
    (pair,) = (
        p
        for p in derive_pair_skews(pcb, classes, num_copper_layers=len(pcb.copper_layers))
        if p.p_name == "MIPI_D0+"
    )
    budget = pair.limits.budget.budget_ps if pair.limits.budget else None
    return pair.skew_ps, budget


def test_data_exits_rebuild_the_measured_corridors(tmp_path, departures):
    board = tmp_path / "data.kicad_pcb"
    _data_pair(board)
    before = PCB.load(board)
    skew_before, budget = _data_skew(board)
    assert skew_before is not None and budget is not None and skew_before < budget
    assert departures.clear_pad_departures(board, _rules()) == 3
    saved = PCB.load(board)
    # Both rails shorten by the same measured amount: the pair keeps its skew.
    deltas = {
        net: _rail_length(saved, net) - _rail_length(before, net)
        for net in ("MIPI_D0+", "MIPI_D0-")
    }
    assert deltas["MIPI_D0+"] == pytest.approx(-0.2313, abs=2e-3)
    assert deltas["MIPI_D0-"] == pytest.approx(deltas["MIPI_D0+"], abs=1e-3)
    skew_after, _ = _data_skew(board)
    # The raw rail difference is unchanged; the small drift is the #9767
    # classifier shedding the old jog pieces from the before-measurement.
    assert skew_after == pytest.approx(skew_before, abs=0.1)
    assert skew_after < budget
    from kicad_tools.validate.connectivity import ConnectivityValidator

    assert not ConnectivityValidator(board).validate().issues
    # The measured exits: one straight axis piece with its corner inside the
    # land, one 45-degree rejoin, and the trunk extended along its own line.
    copper = {
        (net, frozenset((_round(s.start), _round(s.end))))
        for net in ("MIPI_D0+", "MIPI_D0-")
        for s in saved.segments
        if s.net_name == net
    }
    for net, a, b in (
        ("MIPI_D0-", (9.2, 70.0), (9.2, 69.633)),
        ("MIPI_D0-", (9.2, 69.633), (9.583, 69.25)),
        ("MIPI_D0+", (27.0, 69.6), (26.71, 69.6)),
        ("MIPI_D0+", (26.71, 69.6), (25.96, 68.85)),
        ("MIPI_D0+", (9.417, 68.85), (17.65, 68.85)),
        ("MIPI_D0+", (17.65, 68.85), (25.96, 68.85)),
        ("MIPI_D0-", (27.0, 70.4), (26.905, 70.4)),
        ("MIPI_D0-", (26.905, 70.4), (25.755, 69.25)),
        ("MIPI_D0-", (9.583, 69.25), (17.65, 69.25)),
        ("MIPI_D0-", (17.65, 69.25), (25.755, 69.25)),
    ):
        assert (net, frozenset((a, b))) in copper, (net, a, b)
    for s in saved.segments:
        if s.net_name in ("MIPI_D0+", "MIPI_D0-"):
            assert s.width == pytest.approx(0.225)
    once = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == once


@pytest.mark.parametrize(
    "control",
    [
        "foreign-gap",  # the rebuilt U4.3 rejoin would leave 0.1474 mm against the minimum
        "foreign-short",  # an adjacent foreign net the rebuilt rejoin would touch
        "locked",
        "branch",
    ],
)
def test_data_rejections_keep_the_original_geometry(tmp_path, departures, control):
    board = tmp_path / "data.kicad_pcb"
    _data_pair(board, control)
    before = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == before


def test_data_gap_control_passes_at_the_project_minimum(tmp_path, departures):
    """The 0.1474 mm rejection above is the gap, not the foreign track's presence."""
    board = tmp_path / "data.kicad_pcb"
    _data_pair(board, "foreign-gap", gap=0.151)
    assert departures.clear_pad_departures(board, _rules()) == 3


@pytest.mark.parametrize(
    ("reference", "number", "layer", "gap", "edits"),
    [
        ("FID8", "1", "F.Cu", 0.1474, 0),
        ("FID8", "", "F.Cu", 0.1474, 0),
        ("#FID8", "", "F.Cu", 0.1474, 0),
        ("FID8", "", "F.Cu", 0.1510, 3),
        ("FID8", "", "B.Cu", 0.1474, 3),
        ("FID8", "", "F.Mask", 0.1474, 3),
    ],
)
def test_data_rejoin_clearance_to_unassigned_pad(
    tmp_path, departures, reference, number, layer, gap, edits
):
    board = tmp_path / "data.kicad_pcb"
    _data_pair(board)
    # Place the round pad off the proposed U4.3 diagonal by the copper radii
    # plus the specified edge clearance. The project requires 0.1500 mm.
    offset = (0.225 / 2 + 0.15 / 2 + gap) / sqrt(2)
    x, y = 25.96 + 0.75 * 0.35 + offset, 68.85 + 0.75 * 0.35 - offset
    fiducial = (
        f'(footprint "Test:Optical_Target" (layer "F.Cu") (at {x:.8f} {y:.8f})'
        f' (property "Reference" "{reference}" (at 0 0) (layer "F.SilkS"))'
        f' (pad "{number}" smd circle (at 0 0) (size 0.15 0.15) (layers "{layer}")))'
    )
    board.write_text(board.read_text().rstrip()[:-1] + fiducial + "\n)\n")
    before = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == edits
    if edits == 0:
        assert board.read_bytes() == before


def test_data_pair_beyond_its_timing_budget_is_left_alone(tmp_path, departures):
    """The repair keeps the pair inside its differential budget: a class cap
    the measured 0.65 mm rail difference already breaks refuses it intact."""
    board = tmp_path / "data.kicad_pcb"
    _data_pair(board, tolerance_mm=0.2)
    skew, budget = _data_skew(board)
    assert skew is not None
    before = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == before


def test_data_without_a_timing_policy_is_left_alone(tmp_path, departures):
    board = tmp_path / "data.kicad_pcb"
    _data_pair(board)
    board.with_name("net_class_map.json").unlink()
    before = board.read_bytes()
    assert departures.clear_pad_departures(board, _rules()) == 0
    assert board.read_bytes() == before


@pytest.mark.parametrize("constraint", ["keepout", "width"])
@pytest.mark.parametrize("permitted", [False, True], ids=["constrained", "permitted"])
def test_final_data_copper_obeys_j4_rule_area(tmp_path, departures, capsys, constraint, permitted):
    from generate_design import _finalize_signal_copper

    from kicad_tools.cli.kicad_toolchain import docker_fallback_enabled
    from kicad_tools.cli.runner import find_kicad_cli

    if not docker_fallback_enabled() and find_kicad_cli() is None:
        pytest.skip("kicad-cli is needed to evaluate the authored constraints")
    board = tmp_path / "data.kicad_pcb"
    _data_pair(board)
    # Only the rebuilt J4.4 exit reaches the sliver under the pad's long axis;
    # the original diagonal left of it never did.
    if constraint == "keepout":
        x, y, w, h = (8.95, 69.75, 0.1, 0.1) if permitted else (9.15, 69.6, 0.1, 0.1)
        tracks = "not_allowed"
    else:
        x, y, w, h = 9.1, 69.65, 0.05, 0.1
        tracks = "allowed"
        board.with_suffix(".kicad_dru").write_text(
            '(version 1)\n(rule "Data exit width" (layer "F.Cu") '
            "(condition \"A.NetName == 'MIPI_D0-'\") "
            f"(constraint track_width (max {0.23 if permitted else 0.20}mm)))\n"
        )
    area = (
        '(zone (net 0) (net_name "") (layer "F.Cu") (name "Data launch") (hatch edge 0.5)'
        f" (connect_pads (clearance 0)) (min_thickness 0.25) (keepout (tracks {tracks})"
        " (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))"
        " (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))"
        f" (polygon (pts (xy {x} {y}) (xy {x + w} {y}) (xy {x + w} {y + h}) (xy {x} {y + h}))))"
    )
    board.write_text(board.read_text().rstrip()[:-1] + area + ")\n")
    before = board.read_bytes()
    # There are no copper pours on this fixture. Run the generator's same
    # legalization and refill transaction; KiCad evaluates the actual rule area.
    fill = [sys.executable, "-m", "kicad_tools.cli", "zones", "fill", str(board)]
    _finalize_signal_copper(board, fill, lambda tag: True)
    output = capsys.readouterr().out
    if permitted:
        assert "MIPI data pair: 3 exits rebuilt" in output
        saved = PCB.load(board)
        assert any(
            _round(s.start) == (9.2, 70.0) and _round(s.end) == (9.2, 69.633)
            for s in saved.segments
            if s.net_name == "MIPI_D0-"
        )
    else:
        assert board.read_bytes() == before
        assert ("Items not allowed" if constraint == "keepout" else "Data exit width") in output
