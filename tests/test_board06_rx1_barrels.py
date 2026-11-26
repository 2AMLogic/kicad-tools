"""The two Board 06 USB3 RX1 exits must pass the manufacturing checks together."""

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.router.rules import net_class_map_to_dict
from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"
FIXTURE = Path(__file__).parent / "fixtures/board06_rx1_barrels.kicad_pcb"
NETS = ("USB3_RX1+", "USB3_RX1-")


@pytest.fixture
def rx1(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(BOARD))
    from generate_design import _clear_pad_departures, usb3_net_class

    board = tmp_path / "rx1.kicad_pcb"
    shutil.copyfile(FIXTURE, board)
    shutil.copyfile(
        BOARD / "regression-fixture/diffpair_test_routed.kicad_pro", board.with_suffix(".kicad_pro")
    )
    classes = dict.fromkeys(NETS, usb3_net_class())
    (tmp_path / "net_class_map.json").write_text(json.dumps(net_class_map_to_dict(classes)))
    return board, _clear_pad_departures


def _ends(segment):
    return frozenset(tuple(round(v, 6) for v in p) for p in (segment.start, segment.end))


@pytest.mark.parametrize("reverse", [False, True])
def test_rx1_keeps_the_bga_landing_and_joins_each_barrel_once(rx1, reverse):
    board, repair = rx1
    bga_landing = "(start 29.132 19.292) (end 29.53 19.69)"
    if reverse:
        source = board.read_text()
        source = source.replace(bga_landing, "(start 29.53 19.69) (end 29.132 19.292)")
        source = source.replace(
            "(start 10.3 9.5) (end 11.2 9.5)", "(start 11.2 9.5) (end 10.3 9.5)"
        )
        board.write_text(source)
    original = PCB.load(board)
    original_tail = next(
        s for s in original.segments if s.layer == "B.Cu" and (29.53, 19.69) in _ends(s)
    )
    assert repair(board) == 2
    after = board.read_bytes()
    pcb = PCB.load(board)
    assert {_ends(t) for t in pcb.segments if t.uuid == original_tail.uuid} == {
        _ends(original_tail)
    }
    assert {
        (v.net_name, tuple(round(n, 6) for n in v.position), v.size, v.drill) for v in pcb.vias
    } == {
        ("USB3_RX1-", (9.975, 9.2375), 0.6, 0.25),
        ("USB3_RX1+", (30.575, 20.525), 0.6, 0.25),
    }
    assert all(t.width == 0.275 for t in pcb.segments)
    bottom = {_ends(t) for t in pcb.segments if t.layer == "B.Cu"}
    assert frozenset(((9.975, 9.2375), (11.2, 9.5))) in bottom
    assert frozenset(((29.53, 19.69), (30.575, 20.525))) in bottom
    assert repair(board) == 0
    assert board.read_bytes() == after


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ('"B10" smd rect', '"B10" smd oval'),
        ("(via (at 29.53 19.69)", "(via (locked yes) (at 29.53 19.69)"),
        ("(via (at 10.3 9.5)", "(via (tenting (front yes) (back no)) (at 10.3 9.5)"),
        ("(start 29.132 19.292) (end 29.53 19.69)", "(start 29.122 19.292) (end 29.53 19.69)"),
        ("(start 22.222 19.292)", "(start 18.222 19.292)"),
    ],
    ids=[
        "changed pad",
        "locked via",
        "back mask opening",
        "changed BGA landing",
        "skew exceeds budget",
    ],
)
def test_rx1_declines_both_ends_when_one_is_unsafe(rx1, original, replacement):
    board, repair = rx1
    source = board.read_text()
    assert source.count(original) == 1
    board.write_text(source.replace(original, replacement))
    before = board.read_bytes()
    assert repair(board) == 0
    assert board.read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [
        '(segment (start 29.84 20) (end 29.84 19.5) (width 0.275) (layer "F.Cu") (net 2)'
        ' (uuid "32f157e4-b467-4d49-9764-15112a212e16"))',
        '(segment (start 9.7 9.2375) (end 10.2 9.2375) (width 0.275) (layer "B.Cu") (net 3)'
        ' (uuid "f0ed9206-9193-4ab3-926f-38cbcca2e04d"))',
        '(group "kept BGA landing" (uuid "af2a7520-0d06-4b35-a52c-59531d4fc117")'
        ' (members "93e66900-36c5-55b0-b1c9-a69403a8a555"))',
    ],
    ids=[
        "tap at original BGA corner",
        "foreign copper at new connector via",
        "grouped BGA landing",
    ],
)
def test_rx1_preserves_both_exits_with_local_constraints(rx1, extra):
    board, repair = rx1
    board.write_text(board.read_text().rstrip().removesuffix(")") + extra + "\n)\n")
    before = board.read_bytes()
    assert repair(board) == 0
    assert board.read_bytes() == before


@pytest.mark.parametrize("neighbor", [(9.975, 8.5375), (31.275, 20.525)], ids=["J1", "U2"])
def test_rx1_respects_project_only_drill_clearance(rx1, neighbor):
    board, repair = rx1
    project = board.with_suffix(".kicad_pro")
    settings = json.loads(project.read_text())
    design = settings["board"]["design_settings"]
    design["rules"]["min_hole_to_hole"] = 0.5
    design["rule_severities"]["hole_to_hole"] = "error"
    project.write_text(json.dumps(settings))
    # The new barrel would have 0.175 mm of copper clearance but only 0.450 mm
    # of drill clearance. The project supplies the limit without a .kicad_dru.
    x, y = neighbor
    extra = (
        f'(via (at {x} {y}) (size 0.45) (drill 0.25) (layers "F.Cu" "B.Cu")'
        ' (tenting (front yes) (back yes)) (net 3) (uuid "89800718-7e0a-4939-a833-1ea6aad94a42"))'
    )
    board.write_text(board.read_text().rstrip().removesuffix(")") + extra + "\n)\n")
    before = board.read_bytes()
    assert repair(board) == 0
    assert board.read_bytes() == before
