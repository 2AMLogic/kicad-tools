"""The seed-42 PCIe receiver exits move as a pair and preserve its length difference."""

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
