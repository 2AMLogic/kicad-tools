"""The Board 06 TX1 escape uses two measured barrels on one continuous negative rail."""

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.router.rules import net_class_map_to_dict
from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"
FIXTURE = Path(__file__).parent / "fixtures/board06_tx1_barrels.kicad_pcb"


@pytest.fixture
def tx1(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(BOARD))
    from generate_design import _clear_pad_departures, usb3_net_class

    board = tmp_path / "tx1.kicad_pcb"
    shutil.copyfile(FIXTURE, board)
    shutil.copyfile(
        BOARD / "regression-fixture/diffpair_test_routed.kicad_pro", board.with_suffix(".kicad_pro")
    )
    nets = dict.fromkeys(("USB3_TX1+", "USB3_TX1-"), usb3_net_class())
    (tmp_path / "net_class_map.json").write_text(json.dumps(net_class_map_to_dict(nets)))
    return board, _clear_pad_departures


def test_tx1_reconnects_both_exits_on_the_measured_paths(tx1):
    board, repair = tx1
    assert repair(board) == 2
    saved = board.read_bytes()
    pcb = PCB.load(board)

    def point(at):
        return tuple(round(x, 6) for x in at)

    assert {(point(v.position), v.size, v.drill) for v in pcb.vias} == {
        ((12.03, 8.73), 0.6, 0.25),
        ((53.10, 17.905), 0.6, 0.25),
        ((25.2, 17.8), 0.6, 0.25),
    }
    assert all(s.width == 0.275 for s in pcb.segments)
    front = {
        frozenset((point(s.start), point(s.end)))
        for s in pcb.segments
        if s.net_name == "USB3_TX1-" and s.layer == "F.Cu"
    }
    assert front == {
        frozenset(((11.5, 8), (11.5, 8.2))),
        frozenset(((11.5, 8.2), (12.03, 8.73))),
        frozenset(((53.73, 17.46), (53.285, 17.905))),
        frozenset(((53.285, 17.905), (53.10, 17.905))),
    }
    assert repair(board) == 0
    assert board.read_bytes() == saved


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ('"A3" smd rect', '"A3" smd oval'),
        ("(via (at 53.25 17.8)", "(via (locked yes) (at 53.25 17.8)"),
        ("(via (at 11.8 8.5)", "(via (tenting (front yes) (back no)) (at 11.8 8.5)"),
        ("(start 25.2 17.8) (end 53.25 17.8)", "(start 25.2 17.81) (end 53.25 17.8)"),
        ("(end 54.5 24)", "(end 48.0 24)"),
    ),
)
def test_tx1_declines_both_exits_if_either_is_unsafe(tx1, original, replacement):
    board, repair = tx1
    source = board.read_text()
    assert source.count(original) == 1
    board.write_text(source.replace(original, replacement))
    before = board.read_bytes()
    assert repair(board) == 0
    assert board.read_bytes() == before
