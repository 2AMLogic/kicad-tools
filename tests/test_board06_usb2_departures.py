"""Board 06 repairs paired USB2 exits at the connector and transceiver."""

import json
import shutil
from collections import Counter
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB

BOARD06 = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"


@pytest.fixture
def repair(monkeypatch):
    monkeypatch.syspath_prepend(str(BOARD06))
    from pad_departures import clear_pad_departures

    return clear_pad_departures


def _board(path, *, locked=False, branch=False):
    layers = '(layers (0 "F.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal))'
    parts = [
        '(kicad_pcb (version 20260101) (generator "test")',
        layers,
        '(net 1 "USB2_D+") (net 2 "USB2_D-") (net 3 "other")',
        '(footprint "Connector_USB:USB_C_Receptacle_USB2.0" (layer "F.Cu") (at 0 0)',
        '(property "Reference" "J1" (at 0 0) (layer "F.SilkS"))',
        '(pad "B6" smd rect (at 15.5 10) (size .3 .6) (layers "F.Cu" "F.Mask") (net 1))',
        '(pad "B7" smd rect (at 14.5 10) (size .3 .6) (layers "F.Cu" "F.Mask") (net 2)))',
    ]

    def trace(net, layer, a, b):
        parts.append(
            f"(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) "
            f'(width 0.25) (layer "{layer}") (net {net}))'
        )

    for n, pts, layer, approach in [
        (1, [(15.5, 10), (15.65, 9.85), (15.65, 9.45)], "In1.Cu", (14.7, 8.5)),
        (2, [(14.5, 10), (14.5, 10.05), (14.8, 10.35), (14.8, 10.5)], "In2.Cu", (14.75, 10.45)),
    ]:
        for a, b in pairwise(pts):
            trace(n, "F.Cu", a, b)
        trace(n, layer, approach, pts[-1])
        lock = "(locked yes)" if n == 2 and locked else ""
        x, y = pts[-1]
        parts.append(
            f'(via (at {x} {y}) (size 0.6) (drill 0.25) (layers "F.Cu" "B.Cu") (net {n}) {lock})'
        )
    if branch:
        trace(2, "In2.Cu", (14.8, 10.5), (14.8, 11.5))
    path.write_text("\n".join([*parts, ")"]))


def test_both_barrels_clear_the_openings_and_keep_the_drills(tmp_path, repair):
    board = tmp_path / "usb.kicad_pcb"
    _board(board)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    assert repair(board, rules) == 2
    pcb = PCB.load(board)
    assert sorted((v.net_name, v.position, v.size, v.drill) for v in pcb.vias) == [
        ("USB2_D+", (16.05, 9.45), 0.45, 0.25),
        ("USB2_D-", (15.05, 10.55), 0.45, 0.25),
    ]
    assert sum(segment.layer == "F.Cu" for segment in pcb.segments) == 2
    once = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == once


@pytest.mark.parametrize("locked,branch", [(True, False), (False, True)])
def test_ineligible_partner_preserves_both_branches(tmp_path, repair, locked, branch):
    board = tmp_path / "usb.kicad_pcb"
    _board(board, locked=locked, branch=branch)
    before = board.read_bytes()
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert board.read_bytes() == before


# Absolute F.Cu corners of the two transceiver escapes on the seed-42 board.
_U1_RUNS = {
    "USB2_D+": [
        (13, 18.5),
        (13, 18.4975),
        (13.1975, 18.3),
        (13.1975, 18.1661),
        (13.9975, 17.3661),
        (13.9975, 9.6161),
    ],
    "USB2_D-": [
        (13.8, 18.5),
        (13.8, 18.4975),
        (13.6025, 18.3),
        (13.6025, 18.3339),
        (14.4025, 17.5339),
        (14.4025, 9.7839),
    ],
}


def _transceiver_board(path, control=""):
    from generate_design import usb2_net_class

    from kicad_tools.router.rules import net_class_map_to_dict

    shutil.copyfile(
        BOARD06 / "regression-fixture/diffpair_test_routed.kicad_pro",
        path.with_suffix(".kicad_pro"),
    )
    usb2 = usb2_net_class()
    if control == "coupling":
        usb2 = replace(usb2, coupled_continuity_threshold=0.99)
    classes = dict.fromkeys(_U1_RUNS, usb2)
    path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(classes)))
    margin = 0.2 if control == "mask" else 0
    parts = [
        "(kicad_pcb (version 20240108) (generator test) (general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (2 "In1.Cu" power) (4 "In2.Cu" power) (31 "B.Cu" signal)'
        ' (35 "F.Paste" user) (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user)'
        ' (44 "Edge.Cuts" user))',
        f"(setup (pad_to_mask_clearance {margin}))",
        "(gr_rect (start 0 0) (end 30 30) (stroke (width 0.05) (type default))"
        ' (fill none) (layer "Edge.Cuts"))',
        '(net 1 "USB2_D+") (net 2 "USB2_D-") (net 3 "other")',
        '(footprint "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm" (layer "F.Cu") (at 0 0)',
        '(uuid "da728f1a-4a15-42d5-acb4-986500d30d0f")',
        '(property "Reference" "U1" (at 0 0) (layer "F.SilkS"))',
    ]
    for idx, (net, run) in enumerate(_U1_RUNS.items(), 1):
        x, y = run[0]
        parts.append(
            f'(pad "{9 + idx}" smd rect (at {x} {y}) (size 0.3 0.7) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") (net {idx} "{net}") '
            f'(uuid "{uuid5(NAMESPACE_URL, net + "/pad")}"))'
        )
    parts.append(")")
    for idx, (net, run) in enumerate(_U1_RUNS.items(), 1):
        for piece, (a, b) in enumerate(pairwise(run)):
            lock = "(locked yes)" if control == "lock" and idx == 2 and piece == 2 else ""
            end = (b[0], b[1] - 0.1) if control == "trunk" and idx == 2 and piece == 4 else b
            parts.append(
                f"(segment (start {a[0]} {a[1]}) (end {end[0]} {end[1]}) (width 0.25)"
                f' (layer "F.Cu") (net {idx}) {lock} (uuid "{uuid5(NAMESPACE_URL, net + str(piece))}"))'
            )
    if control == "tap":
        parts.append('(via (at 13.47 18.31) (size .1) (drill .04) (layers "F.Cu" "B.Cu") (net 2))')
    if control == "group":
        parts.append(f'(group "Tune" (members "{uuid5(NAMESPACE_URL, "USB2_D-2")}"))')
    if control == "branch":
        parts.append(
            '(segment (start 13.6025 18.3339) (end 13.3 18.65) (width .1) (layer "F.Cu") (net 2))'
        )
    if control == "foreign":
        parts.append(
            '(segment (start 13.8 17.5) (end 13.8 17.7) (width .1) (layer "F.Cu") (net 3))'
        )
    path.write_text("\n".join([*parts, ")"]))


def _checker(path):
    from kicad_tools.router.rules import net_class_map_from_path
    from kicad_tools.validate import DRCChecker

    return DRCChecker(
        PCB.load(path),
        net_class_map=net_class_map_from_path(path.with_name("net_class_map.json")),
        emit_measurements=True,
    )


def test_u1_usb2_exits_keep_the_trunks_and_pair_limits(tmp_path, repair):
    board = tmp_path / "usb2.kicad_pcb"
    _transceiver_board(board)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    assert repair(board, rules) == 2
    pcb = PCB.load(board)
    expected = {
        "USB2_D+": [(13, 18.5), (13, 17.85), (13.9975, 16.8525), (13.9975, 9.6161)],
        "USB2_D-": [(13.8, 18.5), (13.8, 17.625), (14.4025, 17.0225), (14.4025, 9.7839)],
    }
    for net, points in expected.items():
        tracks = [s for s in pcb.segments if s.net_name == net]
        assert all(s.layer == "F.Cu" and s.width == 0.25 for s in tracks)
        assert {frozenset((s.start, s.end)) for s in tracks} == {
            frozenset(edge) for edge in pairwise(points)
        }
    checker = _checker(board)
    assert not checker.check_impedance().violations
    assert not checker.impedance_unmeasured_pairs
    (coupling,) = checker.check_diffpair_routing_continuity().violations
    assert coupling.actual_value >= coupling.required_value == 0.7
    once = board.read_bytes()
    assert repair(board, rules) == 0
    assert board.read_bytes() == once


@pytest.mark.parametrize(
    "control", ["lock", "group", "trunk", "branch", "tap", "foreign", "coupling", "mask"]
)
def test_u1_usb2_declines_both_rails(tmp_path, repair, control):
    board = tmp_path / "usb2.kicad_pcb"
    _transceiver_board(board, control)
    before = board.read_bytes()
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert board.read_bytes() == before


def test_symmetric_u1_exits_fail_impedance_and_preserve_the_board(tmp_path, repair, monkeypatch):
    import pad_departures

    board = tmp_path / "usb2.kicad_pcb"
    _transceiver_board(board)
    before = board.read_bytes()
    equal_exits = tuple(
        replace(ex, new=((0, 0), (0, -0.600), (ex.old[-1][0], -0.600 - ex.old[-1][0])))
        for ex in pad_departures._USB2_EXITS
    )
    # Independently grade the 0.600 mm control, then submit it to the normal repair.
    control = PCB.load(board)
    for ex in equal_exits:
        assert pad_departures._stage_data_exit(control, ex, set(), 0.25)
    staged = tmp_path / "symmetric.kicad_pcb"
    control.save(staged)
    (error,) = _checker(staged).check_impedance().errors
    assert error.items == ("USB2_D+",)
    assert error.actual_value == pytest.approx(113.0, abs=0.1)
    assert error.required_value == 90
    monkeypatch.setattr(pad_departures, "_USB2_EXITS", equal_exits)
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert board.read_bytes() == before


def _native_mask_measurements(board):
    from kicad_tools.cli.kicad_toolchain import PINNED_KICAD_VERSION, version_number
    from kicad_tools.cli.runner import find_kicad_cli, get_kicad_version
    from kicad_tools.validate.mask_copper import (
        check_mask_to_copper,
        default_request_for_manufacturer,
    )

    cli = find_kicad_cli()
    if cli is None or version_number(get_kicad_version(cli)) != PINNED_KICAD_VERSION:
        pytest.skip("The mask regression uses the pinned KiCad toolchain")
    policy = default_request_for_manufacturer("jlcpcb").policy
    report = check_mask_to_copper(board, policy)
    assert report.coverage == "complete", report.reasons
    return report.measurements


def test_u1_mask_errors_clear_on_both_transceiver_openings(tmp_path, repair):
    board = tmp_path / "usb2.kicad_pcb"
    _transceiver_board(board)

    def opening_errors():
        return Counter(
            m.owner_ref for m in _native_mask_measurements(board) if m.disposition == "violation"
        )

    assert opening_errors() == {"U1/10": 2, "U1/11": 2}
    assert repair(board, get_profile("jlcpcb").get_design_rules(layers=4)) == 2
    assert not opening_errors()


@pytest.fixture
def mirrored_connector(tmp_path, repair):
    """Both J1 orientations and the two routed rails from the measured seed-42 fixture."""
    from generate_design import usb2_net_class

    from kicad_tools.router.rules import net_class_map_to_dict

    path = tmp_path / "connector.kicad_pcb"
    shutil.copyfile(Path(__file__).parent / "fixtures/board06_j1_usb2.kicad_pcb", path)
    shutil.copyfile(
        BOARD06 / "regression-fixture/diffpair_test_routed.kicad_pro",
        path.with_suffix(".kicad_pro"),
    )
    classes = dict.fromkeys(_U1_RUNS, usb2_net_class())
    path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(classes)))
    return path


def test_j1_usb2_reroutes_both_orientations(mirrored_connector, repair):
    path = mirrored_connector
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    assert repair(path, rules) == 2
    pcb = PCB.load(path)
    barrels = {(v.net_name, v.position, v.size, v.drill) for v in pcb.vias}
    assert barrels == {
        ("USB2_D+", (14.5, 8.7), 0.45, 0.25),
        ("USB2_D-", (15.5, 8.7), 0.45, 0.25),
        ("USB2_D+", (16.05, 9.45), 0.45, 0.25),
        ("USB2_D-", (15.05, 10.55), 0.45, 0.25),
    }
    for net, x in (("USB2_D+", 14.5), ("USB2_D-", 15.5)):
        launches = [
            (s.start, s.end) for s in pcb.segments if s.net_name == net and s.layer == "F.Cu"
        ]
        assert ((x, 8.0), (x, 8.7)) in launches
    # The incoming diagonal, the U1 trunk and the mirrored barrel branch all
    # terminate at the B7 pad centre, the actual three-way copper junction.
    b7 = pcb.get_pad_position("J1", "B7")
    assert (
        sum(
            s.net_name == "USB2_D-" and s.layer == "F.Cu" and b7 in (s.start, s.end)
            for s in pcb.segments
        )
        == 3
    )
    checker = _checker(path)
    assert not checker.check_diffpair_routing_continuity().errors
    assert not checker.check_impedance().errors
    assert not checker.check_diffpair_length_skew().errors
    once = path.read_bytes()
    assert repair(path, rules) == 0
    assert path.read_bytes() == once


@pytest.mark.parametrize(
    "control",
    [
        "locked via",
        "foreign copper",
        "coupling",
        "mask margin",
        "inner tap",
        "moved B7",
        "exposed back barrel",
    ],
)
def test_j1_usb2_rejects_a_changed_partner(mirrored_connector, repair, control):
    path = mirrored_connector
    board = path.read_text()
    if control == "locked via":
        board = board.replace("(via (at 15.75 8.55)", "(via (locked yes) (at 15.75 8.55)")
    elif control == "exposed back barrel":
        board = board.replace(
            "(via (at 15.75 8.55)", "(via (tenting (front yes) (back no)) (at 15.75 8.55)"
        )
    elif control == "foreign copper":
        board = (
            board.rstrip()[:-1]
            + '(segment (start 15.5 8.7) (end 16.5 8.7) (width 0.2) (layer "In1.Cu") (net 3)))'
        )
    elif control == "inner tap":
        board = (
            board.rstrip()[:-1]
            + '(segment (start 14.75 9.2) (end 14.4 9.2) (width 0.15) (layer "In2.Cu") (net 2)))'
        )
    elif control == "moved B7":
        board = board.replace('(pad "B7" smd rect (at 14.5 10)', '(pad "B7" smd rect (at 14.55 10)')
        board = board.replace(
            "(start 14.5 10) (end 15.05 10.55)", "(start 14.55 10) (end 15.05 10.55)"
        )
    elif control == "mask margin":
        board = board.replace("pad_to_mask_clearance 0", "pad_to_mask_clearance 0.2")
    else:
        from generate_design import usb2_net_class

        from kicad_tools.router.rules import net_class_map_to_dict

        stricter = replace(usb2_net_class(), coupled_continuity_threshold=0.99)
        classes = dict.fromkeys(_U1_RUNS, stricter)
        path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(classes)))
    path.write_text(board)
    before = path.read_bytes()
    assert repair(path, get_profile("jlcpcb").get_design_rules(layers=4)) == 0
    assert path.read_bytes() == before


def test_j1_usb2_clears_all_eight_measured_mask_findings(mirrored_connector, repair):
    def failing_openings():
        return Counter(
            m.owner_ref
            for m in _native_mask_measurements(mirrored_connector)
            if m.owner_ref in {"J1/A6", "J1/A7", "J1/B6", "J1/B7"}
            and m.disposition in {"uncertain", "violation"}
        )

    assert failing_openings() == {"J1/A6": 3, "J1/A7": 3, "J1/B7": 2}
    assert repair(mirrored_connector, get_profile("jlcpcb").get_design_rules(layers=4)) == 2
    assert not failing_openings()
