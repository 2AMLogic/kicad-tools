"""Board 06's mirrored USB-C data pads must be repaired as a pair."""

from itertools import pairwise
from pathlib import Path

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
