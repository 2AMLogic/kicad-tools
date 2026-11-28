"""The recipe-level mask repair driver (issue #10664)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli import fix_drc_cmd
from kicad_tools.recipes import mask_repair
from kicad_tools.recipes.mask_repair import MaskRepairError, repair_mask_copper


@pytest.fixture
def board(tmp_path: Path) -> Path:
    path = tmp_path / "routed.kicad_pcb"
    path.write_bytes(b"(kicad_pcb v0)")
    return path


def _drive(monkeypatch, counts, *, codes=(0,), mutate=True):
    """Feed ``mask_error_count`` from ``counts`` and record each fix-drc call."""
    seen = []
    calls = []

    def count(pcb_path, mfr):
        seen.append(mfr)
        return counts[min(len(seen) - 1, len(counts) - 1)]

    def main(argv):
        calls.append(argv)
        if mutate:
            Path(argv[0]).write_bytes(f"(kicad_pcb v{len(calls)})".encode())
        return codes[min(len(calls) - 1, len(codes) - 1)]

    monkeypatch.setattr(mask_repair, "mask_error_count", count)
    monkeypatch.setattr(fix_drc_cmd, "main", main)
    return calls


def test_repair_runs_fix_drc_until_the_board_carries_no_error(tmp_path, monkeypatch, board):
    calls = _drive(monkeypatch, [10, 2, 0, 0])
    lines = []
    outcome = repair_mask_copper(board, log=lines.append)
    assert outcome.repaired == 10 and outcome.remaining == 0 and outcome.invocations == 2
    assert len(calls) == 2
    for argv in calls:
        assert argv[argv.index("--only") + 1] == "mask-copper"
        assert argv[argv.index("--mfr") + 1] == "jlcpcb-tier1"
        assert argv[argv.index("--max-passes") + 1] == "3"
    assert "10 -> 2 error(s)" in lines[0] and "2 -> 0 error(s)" in lines[-1]


def test_a_clean_board_is_left_alone(tmp_path, monkeypatch, board):
    calls = _drive(monkeypatch, [0])
    before = board.read_bytes()
    assert repair_mask_copper(board) == mask_repair.MaskRepairOutcome(0, 0, 0)
    assert calls == [] and board.read_bytes() == before


def test_repair_stops_when_an_invocation_leaves_the_bytes_unchanged(tmp_path, monkeypatch, board):
    counts = [4, 1, 1, 1]

    def count(pcb_path, mfr):
        return counts[min(len(calls), 3)]

    calls = []

    def main(argv):
        calls.append(argv)
        if len(calls) == 1:
            Path(argv[0]).write_bytes(b"(kicad_pcb v1)")
        return 1

    monkeypatch.setattr(mask_repair, "mask_error_count", count)
    monkeypatch.setattr(fix_drc_cmd, "main", main)
    lines = []
    outcome = repair_mask_copper(board, log=lines.append)
    assert outcome == mask_repair.MaskRepairOutcome(3, 1, 2)
    assert len(calls) == 2 and "declines to move" in lines[-1]


@pytest.mark.parametrize("code", [3, 4, 5])
def test_a_failed_pass_is_a_hard_error(tmp_path, monkeypatch, board, code):
    _drive(monkeypatch, [2, 2], codes=(code,), mutate=False)
    with pytest.raises(MaskRepairError, match=f"exit {code}"):
        repair_mask_copper(board)


def test_an_incomplete_assessment_is_a_hard_error(tmp_path, monkeypatch, board):
    _drive(monkeypatch, [None])
    with pytest.raises(MaskRepairError, match="did not complete"):
        repair_mask_copper(board)


def test_an_assessment_that_stops_completing_after_a_run_is_a_hard_error(
    tmp_path, monkeypatch, board
):
    _drive(monkeypatch, [2, None])
    with pytest.raises(MaskRepairError, match="after fix-drc"):
        repair_mask_copper(board)


def test_a_missing_board_is_a_hard_error(tmp_path, monkeypatch):
    _drive(monkeypatch, [0])
    with pytest.raises(FileNotFoundError):
        repair_mask_copper(tmp_path / "absent.kicad_pcb")


def test_fix_drc_output_is_echoed_through_the_log(tmp_path, monkeypatch, board):
    import sys

    def main(argv):
        print("Repaired 1/1 violations")
        print("NOTE: 3D coverage not evaluated for 17 footprint(s)", file=sys.stderr)
        Path(argv[0]).write_bytes(b"(kicad_pcb v1)")
        return 0

    monkeypatch.setattr(fix_drc_cmd, "main", main)
    counts = iter([1, 0])
    monkeypatch.setattr(mask_repair, "mask_error_count", lambda pcb_path, mfr: next(counts))
    lines = []
    outcome = repair_mask_copper(board, log=lines.append)
    assert outcome == mask_repair.MaskRepairOutcome(1, 0, 1)
    assert lines[0] == "   Repaired 1/1 violations"
    assert lines[1] == "   mask repair, invocation 1: 1 -> 0 error(s)"
