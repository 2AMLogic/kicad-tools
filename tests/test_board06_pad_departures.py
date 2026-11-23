"""Physical controls for the nine-pair fixture's pad-departure repair (issue #9989)."""

from math import dist
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from shapely.geometry import LineString, box

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


def test_relocated_departure_keeps_the_inner_extension(tmp_path, departures):
    from kicad_tools.validate.connectivity import ConnectivityValidator

    board = tmp_path / "slid.kicad_pcb"
    _write_board(
        board,
        pad_number="12",
        pad_size=(0.3, 0.7),
        segments=[
            *(
                ("F.Cu", (10, 10 + y + 0.05), (10, 10 + y), round(0.1016 + y * 0.5, 4))
                for y in [i * 0.05 for i in range(12)]
            ),
            ("In2.Cu", (10.2, 12.6), (10.2, 10.4), 0.4),
            ("In2.Cu", (10.2, 10.4), (10, 10.6), 0.4),
            ("In2.Cu", (10.2, 12.6), (10, 12.6), 0.4),
        ],
        vias=[(10, 10.6)],
        far_at=(10, 12.6),
    )
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
