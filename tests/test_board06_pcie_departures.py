"""Seed-42 PCIe pad repairs preserve the paired exits and their timing."""

import json
from itertools import pairwise
from math import dist
from pathlib import Path

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.router.rules import net_class_map_from_path, net_class_map_to_dict
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.diffpair_skew import derive_pair_skews


@pytest.fixture
def repair(monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "boards/06-diffpair-test")
    )
    from pad_departures import clear_pad_departures

    return clear_pad_departures


def _receiver(path, control=None):
    from generate_design import pcie_net_class

    pair = {net: pcie_net_class() for net in ("PCIE_RX+", "PCIE_RX-")}
    path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(pair)))
    parts = [
        "(kicad_pcb (version 20240108) (generator test)",
        '(layers (0 "F.Cu" signal) (2 "In1.Cu" power) (4 "In2.Cu" power) (31 "B.Cu" signal))',
        '(net 1 "PCIE_RX+") (net 2 "PCIE_RX-")',
        '(footprint "Package_QFP:LQFP-48_7x7mm_P0.5mm" (layer "F.Cu") (at 0 0)',
        '(property "Reference" "U3" (at 0 0) (layer "F.SilkS"))',
        '(pad "31" smd rect (at 10 10) (size .7 .3) (layers "F.Cu" "F.Mask") (net 1))',
        '(pad "32" smd rect (at 10 9.2) (size .7 .3) (layers "F.Cu" "F.Mask") (net 2)))',
    ]

    def track(a, b, width, layer, net, locked=False):
        parts.append(
            f"(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
            f'(layer "{layer}") (net {net})' + (" (locked yes)" if locked else "") + ")"
        )

    for net, layer, front, inner in (
        (
            1,
            "In1.Cu",
            [(10, 10), (10.25, 10), (10.5, 10), (10.6, 10.1)],
            [(10.6, 10.1), (10.9, 10.4), (11.65, 11.15)],
        ),
        (2, "In2.Cu", [(10, 9.2), (10.4, 9.2), (10.8, 9.2), (11, 9.4)], [(11, 9.4), (13.7, 12.1)]),
    ):
        for (a, b), width in zip(pairwise(front), (0.12, 0.17, 0.225), strict=True):
            if control == "unequal" and net == 2 and width == 0.17:
                bend = (10.6, 9.0)
                track(a, bend, width, "F.Cu", net)
                track(bend, b, width, "F.Cu", net)
            else:
                track(a, b, width, "F.Cu", net)
        x, y = front[-1]
        parts.append(f'(via (at {x} {y}) (size .6) (drill .25) (layers "F.Cu" "B.Cu") (net {net}))')
        for a, b in pairwise(inner):
            track(
                a,
                b,
                0.225 if b == inner[-1] else 0.18,
                layer,
                net,
                locked=net == 2 and control == "locked",
            )
    # Retained remote copper sets the actual delay margin independently of
    # the local escapes. The shorter trunk is just inside the PCIe Gen1 limit.
    length = 2.065 if control == "delay-margin" else 2.5
    track((11.65, 11.15), (11.65 + length, 11.15), 0.225, "In1.Cu", 1)
    if control == "branch":
        track((11, 9.4), (11, 9.9), 0.225, "In2.Cu", 2)
    path.write_text("\n".join([*parts, ")"]))


def _skew(pcb):
    return sum(dist(s.start, s.end) * (1 if s.net_name == "PCIE_RX+" else -1) for s in pcb.segments)


def test_receiver_exits_keep_skew_and_barrel_size(tmp_path, repair):
    board = tmp_path / "receiver.kicad_pcb"
    _receiver(board)
    skew = _skew(PCB.load(board))
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    assert repair(board, rules) == 2
    saved = PCB.load(board)
    assert sorted((v.net_name, v.position, v.size, v.drill) for v in saved.vias) == [
        ("PCIE_RX+", (10.9, 10.0), 0.6, 0.25),
        ("PCIE_RX-", (11.2, 9.2), 0.6, 0.25),
    ]
    assert [s.width for s in saved.segments if s.layer == "F.Cu"] == [0.225, 0.225]
    assert _skew(saved) == pytest.approx(skew, abs=1e-6)
    once = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == once


@pytest.mark.parametrize("control", ["unequal", "delay-margin"])
def test_receiver_timing_declines_the_whole_pair(tmp_path, repair, control):
    board = tmp_path / "receiver.kicad_pcb"
    _receiver(board, control)
    before = board.read_bytes()
    pcb = PCB.load(board)
    classes = net_class_map_from_path(board.with_name("net_class_map.json"), pcb_path=board)
    (measured,) = derive_pair_skews(pcb, classes, num_copper_layers=len(pcb.copper_layers))
    assert measured.skew_ps < measured.limits.budget.budget_ps
    if control == "delay-margin":
        assert measured.limits.budget.budget_ps - measured.skew_ps < 0.05
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert board.read_bytes() == before


def test_receiver_delay_margin_follows_the_project_rate(tmp_path, repair):
    board = tmp_path / "receiver.kicad_pcb"
    _receiver(board, "delay-margin")
    sidecar = board.with_name("net_class_map.json")
    classes = json.loads(sidecar.read_text())
    for net_class in classes.values():
        net_class["data_rate_gbps"] = 2.0
    sidecar.write_text(json.dumps(classes))
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 2


def test_receiver_without_timing_policy_leaves_both_rails_untouched(tmp_path, repair):
    board = tmp_path / "receiver.kicad_pcb"
    _receiver(board)
    board.with_name("net_class_map.json").unlink()
    before = board.read_bytes()
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert board.read_bytes() == before


@pytest.mark.parametrize("control", ["locked", "branch"])
def test_ineligible_receiver_partner_preserves_the_entire_board(tmp_path, repair, control):
    board = tmp_path / "receiver.kicad_pcb"
    _receiver(board, control)
    before = board.read_bytes()
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert board.read_bytes() == before


_taper_cases = pytest.mark.parametrize(
    ("ref", "pair"), [("U3", "TX"), ("J3", "TX"), ("J3", "RX")], ids=["U3", "J3", "J3-RX"]
)


def _taper_board(path, ref, control="", *, pair="TX"):
    from uuid import UUID

    from generate_design import pcie_net_class

    nets = (f"PCIE_{pair}+", f"PCIE_{pair}-")
    policy = {net: pcie_net_class() for net in nets}
    # Match the fixture's authored PCIe clearance and board floor.
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "board": {"design_settings": {"rules": {"min_clearance": 0.1016}}},
                "net_settings": {"classes": [{"name": "Default", "clearance": 0.15}]},
            }
        )
    )
    path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(policy)))
    if ref == "U3":
        footprint, size, pins = "Package_QFP:LQFP-48_7x7mm_P0.5mm", ".7 .3", ("28", "29")
        offsets = (((0.25, 0), (0.821, 0.571)), ((0.25, 0), (0.821, -0.571)))
        centers = ((10.0, 10.8), (10.0, 10.0))
        direction = 1
    else:
        footprint, size = "Connector_PCIE:PCIE_Mini_Edge", ".5 .6"
        if pair == "RX":
            pins = ("8", "9")
            offsets = (
                ((-0.15, -0.15), (-0.9887, -0.15)),
                ((-0.15, -0.15), (-0.15, -0.2), (-0.9798, -0.2)),
            )
        else:
            pins = ("5", "6")
            offsets = (
                ((-0.15, 0.15), (-0.15, 0.2), (-0.9798, 0.2)),
                ((-0.15, 0.15), (-0.9887, 0.15)),
            )
        centers = ((10.0, 10.0), (10.0, 10.8))
        direction = -1
    parts = [
        "(kicad_pcb (version 20240108) (generator test)",
        '(layers (0 "F.Cu" signal) (2 "In1.Cu" power) (4 "In2.Cu" power) (31 "B.Cu" signal))',
        f'(net 1 "{nets[0]}") (net 2 "{nets[1]}") (net 3 "OTHER")',
        f'(footprint "{footprint}" (layer "F.Cu") (at 0 0)',
        f'(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))',
    ]
    for idx, ((x, y), pin) in enumerate(zip(centers, pins, strict=True), 1):
        parts.append(
            f'(pad "{pin}" smd rect (at {x} {y}) (size {size}) (layers "F.Cu" "F.Mask") (net {idx}))'
        )
    parts.append(")")
    original = {}
    for idx, ((cx, cy), turns) in enumerate(zip(centers, offsets, strict=True), 1):
        vertices = [(cx, cy), *((cx + dx, cy + dy) for dx, dy in turns)]
        original[nets[idx - 1]] = vertices
        points = [vertices[0]]
        for a, b in pairwise(vertices):
            points.extend([((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), b])
        if idx == 2 and control == "jog":
            x, y = points[1]
            points[1] = x, y + 0.04
        end = points[-1]
        points.append((end[0] + direction * (5 if idx == 2 and control == "timing" else 3), end[1]))
        for n, (a, b) in enumerate(pairwise(points)):
            width = 0.225 if b == points[-1] else 0.12 + n * 0.012
            uid = UUID(int=idx * 100 + n)
            parts.append(
                f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) (layer "F.Cu") (net {idx}) (uuid "{uid}"))'
            )
        if idx == 2 and control in ("group", "member"):
            locked = "(locked yes)" if control == "group" else ""
            parts.append(
                f'(group "" (uuid "{UUID(int=500)}") {locked} (members "{UUID(int=201)}"))'
            )
        if idx == 2 and control == "branch":
            x, y = points[1]
            parts.append(
                f'(segment (start {x} {y}) (end {x} {y - 0.5}) (width .15) (layer "F.Cu") (net 2))'
            )
    if control in ("foreign", "nearby"):
        # Place foreign copper along the negative taper, clear of its pad.
        if ref == "U3":
            delta = 0.185 if control == "foreign" else 0.3
            a, b = (10.43 + delta, 9.82 + delta), (10.65 + delta, 9.60 + delta)
        else:
            y = centers[1][1] + offsets[1][-1][1] + (0.26 if control == "foreign" else 0.42)
            a, b = (9.55, y), (9.3, y)
        parts.append(
            f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width .1) (layer "F.Cu") (net 3))'
        )
    parts.append(")")
    path.write_text("\n".join(parts))
    return original


@_taper_cases
def test_pcie_tapers_follow_their_original_centerlines(tmp_path, repair, ref, pair):
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    board = tmp_path / "pcie.kicad_pcb"
    original = _taper_board(board, ref, pair=pair)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    assert repair(board, rules) == 2
    saved = PCB.load(board)
    for net, vertices in original.items():
        tracks = [s for s in saved.segments if s.net_name == net and dist(s.start, s.end) < 2]
        assert len(tracks) == len(vertices) - 1
        assert all(s.layer == "F.Cu" and s.width == 0.225 for s in tracks)
        assert (
            unary_union([LineString([s.start, s.end]) for s in tracks]).hausdorff_distance(
                LineString(vertices)
            )
            < 1e-6
        )
        assert sum(dist(s.start, s.end) for s in tracks) == pytest.approx(
            LineString(vertices).length, abs=1e-6
        )
    once = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == once


@_taper_cases
@pytest.mark.parametrize("control", ["group", "member", "branch", "jog", "timing", "foreign"])
def test_ineligible_pcie_rail_preserves_its_partner(tmp_path, repair, ref, pair, control):
    board = tmp_path / "pcie.kicad_pcb"
    _taper_board(board, ref, control, pair=pair)
    before = board.read_bytes()
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert board.read_bytes() == before


@_taper_cases
@pytest.mark.parametrize("constraint", ["clearance", "physical_clearance"])
def test_pcie_tapers_use_authored_spacing(tmp_path, repair, ref, pair, constraint):
    board = tmp_path / "pcie.kicad_pcb"
    _taper_board(board, ref, "nearby", pair=pair)
    before = board.read_bytes()
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    assert repair(board, rules) == 2
    board.write_bytes(before)
    board.with_suffix(".kicad_dru").write_text(
        f"""(version 1)
(rule "Foreign clearance" (condition "A.NetName == 'OTHER' || B.NetName == 'OTHER'") (constraint {constraint} (min 0.45mm)))"""
    )
    assert repair(board, rules) == 0
    assert board.read_bytes() == before


def test_transmitter_uses_the_declared_pair_rate(tmp_path, repair):
    board = tmp_path / "transmitter.kicad_pcb"
    _taper_board(board, "U3", "timing")
    sidecar = board.with_name("net_class_map.json")
    classes = json.loads(sidecar.read_text())
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    before = board.read_bytes()
    sidecar.unlink()
    assert repair(board, rules) == 0
    assert board.read_bytes() == before
    for entry in classes.values():
        entry["data_rate_gbps"] = 0.5
    sidecar.write_text(json.dumps(classes))
    assert repair(board, rules) == 2


def _rule_area_poly(name, pts, *, tracks):
    return (
        f'(zone (net 0) (net_name "") (layers "F.Cu") (uuid "{UUID_AREA}") (name "{name}") '
        "(hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.2) "
        f"(keepout (tracks {tracks}) (vias allowed) (pads allowed) (copperpour allowed) "
        "(footprints allowed)) (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
        f"(polygon (pts {' '.join(f'(xy {x} {y})' for x, y in pts)})))"
    )


def _rule_area(name, x0, y0, x1, y1, *, tracks):
    return _rule_area_poly(name, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], tracks=tracks)


UUID_AREA = "00000000-0000-0000-0000-0000000009a1"


def _add_area(board, area):
    text = board.read_text().rstrip()
    assert text.endswith(")")
    board.write_text(text[:-1] + "\n" + area + ")")


def test_receiver_declines_both_rails_inside_a_track_keepout(tmp_path, repair):
    board = tmp_path / "pcie.kicad_pcb"
    _taper_board(board, "J3", pair="RX")
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    _add_area(board, _rule_area("keep", 9.3, 9.8, 9.4, 9.9, tracks="not_allowed"))
    before = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == before
    # Near miss: a band the original 0.144/0.156 mm taper clears (its copper
    # tops out 0.012 mm below the band) but the widened 0.225 mm rail enters.
    _taper_board(board, "J3", pair="RX")
    _add_area(board, _rule_area("keep", 9.3, 9.94, 9.4, 9.95, tracks="not_allowed"))
    before = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == before
    # Control: the same area moved clear of the widened rail is allowed.
    _taper_board(board, "J3", pair="RX")
    _add_area(board, _rule_area("keep", 9.3, 9.3, 9.4, 9.4, tracks="not_allowed"))
    assert repair(board, rules) == 2


def test_receiver_declines_a_keepout_whose_ring_is_degenerate(tmp_path, repair):
    board = tmp_path / "pcie.kicad_pcb"
    _taper_board(board, "J3", pair="RX")
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    _add_area(
        board,
        _rule_area_poly(
            "keep", [(9.3, 9.85), (9.4, 9.85), (9.5, 9.85), (9.6, 9.85)], tracks="not_allowed"
        ),
    )
    before = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == before
    # Control: both lobes of this self-intersecting keepout clear the rails.
    _taper_board(board, "J3", pair="RX")
    _add_area(
        board,
        _rule_area_poly(
            "keep", [(9.3, 10.2), (9.5, 10.4), (9.5, 10.2), (9.3, 10.4)], tracks="not_allowed"
        ),
    )
    assert repair(board, rules) == 2


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_receiver_respects_both_lobes_of_a_self_crossing_keepout(tmp_path, repair, reverse):
    board = tmp_path / "pcie.kicad_pcb"
    _taper_board(board, "J3", pair="RX")
    # Only the lower lobe reaches the proposed 0.225 mm rail. The original
    # copper clears it in either winding.
    ring = [(9.30, 9.94), (9.40, 10.01), (9.30, 10.01), (9.40, 9.94)]
    if reverse:
        ring.reverse()
    _add_area(board, _rule_area_poly("keep", ring, tracks="not_allowed"))
    before = board.read_bytes()
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert board.read_bytes() == before


def test_receiver_declines_both_rails_over_an_authored_maximum_width(tmp_path, repair):
    board = tmp_path / "pcie.kicad_pcb"
    _taper_board(board, "J3", pair="RX")
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    _add_area(board, _rule_area("J3 connector exit", 9.3, 9.8, 9.4, 9.9, tracks="allowed"))
    dru = board.with_suffix(".kicad_dru")
    template = (
        '(version 1)\n(rule "exit width" (layer "F.Cu") '
        "(condition \"A.NetName == 'PCIE_RX+' && A.intersectsArea('J3 connector exit')\") "
        "(constraint track_width (max {limit}mm)))"
    )
    dru.write_text(template.format(limit="0.2"))
    before = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == before
    # Control: a maximum that admits the 0.225 mm rail is allowed.
    dru.write_text(template.format(limit="0.23"))
    assert repair(board, rules) == 2


def test_receiver_skips_an_ignored_authored_maximum_width(tmp_path, repair):
    board = tmp_path / "pcie.kicad_pcb"
    _taper_board(board, "J3", pair="RX")
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    _add_area(board, _rule_area("J3 connector exit", 9.3, 9.8, 9.4, 9.9, tracks="allowed"))
    dru = board.with_suffix(".kicad_dru")
    template = (
        '(version 1)\n(rule "exit width" {severity}(layer "F.Cu") '
        "(condition \"A.NetName == 'PCIE_RX+' && A.intersectsArea('J3 connector exit')\") "
        "(constraint track_width (max 0.2mm)))"
    )
    dru.write_text(template.format(severity="(severity ignore) "))
    assert repair(board, rules) == 2
    # Control: without the ignored severity the same rule still declines.
    _taper_board(board, "J3", pair="RX")
    _add_area(board, _rule_area("J3 connector exit", 9.3, 9.8, 9.4, 9.9, tracks="allowed"))
    dru.write_text(template.format(severity=""))
    before = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == before
