"""``kct fix-drc --only acute-branch``: the verified transaction (Issue #10732).

The repairer's own gates are covered by ``tests/test_repair_acute_branch.py``;
these drive the ``fix-drc`` pass that re-checks the *saved* board and puts an
edit back.  ``kct check``'s rule set is replaced by the ``sharp_trace_corner``
rule alone (plus whatever warning a test injects next to the edit), and the
native DRC by a constant state, so each case runs in milliseconds.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.cli import fix_drc_cmd
from kicad_tools.drc.compat import drc_results_to_report
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.mask_copper import MaskCopperAssessment
from kicad_tools.validate.rules.sharp_trace_corner import SharpTraceCornerRule

_PLANNER_TESTS = Path(__file__).resolve().parent / "test_repair_acute_branch.py"


def _planner_tests():
    spec = importlib.util.spec_from_file_location("kct_planner_tests_10732_cli", _PLANNER_TESTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


Fixtures = _planner_tests()
_Seg, _Via = Fixtures._Seg, Fixtures._Via

#: Board 03's BTN3 shape: a 45-degree tap from a via into one straight carrier.
CARRIER = _Seg((33.2, 32.8), (33.2, 35.2))
TAP = _Seg((31.2, 31.6), (33.2, 33.6))
ANCHOR = (31.2, 31.6)


def _write(tmp_path: Path, items: list) -> Path:
    path = Fixtures._write(tmp_path, items, name="usb.kicad_pcb")
    return path


def _board(tmp_path: Path) -> Path:
    return _write(tmp_path, [CARRIER, TAP, _Via(ANCHOR)])


class _Checks:
    """``_run_python_drc`` with only the sharp-corner rule, plus injections.

    ``extra`` maps a call ordinal (1-based, after the detection run) to the
    warnings that run reports in addition, so a test can fail the first saved
    board and pass the next one.
    """

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.extra: dict[int, list[DRCViolation]] = {}
        self.last: DRCReport | None = None

    def __call__(self, pcb_path, **_kwargs):
        pcb = PCB.load(pcb_path)
        results = SharpTraceCornerRule().check(pcb, None)
        report = drc_results_to_report(results, pcb_path)
        # The real run's mask assessment is complete on these boards; the pass
        # fails closed on anything less.
        report.mask_copper_assessments = [MaskCopperAssessment(coverage="complete")]
        self.calls.append(Path(pcb_path))
        ordinal = len(self.calls) - 1  # the detection run is not a re-check
        for v in self.extra.get(ordinal, []):
            report.violations.append(v)
        self.last = report
        return report


def _warning(rule: str, nets: tuple[str, ...], x: float, y: float) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.from_string(rule),
        type_str=rule,
        severity=Severity.WARNING,
        message=f"{rule} injected at ({x}, {y})",
        locations=[Location(x_mm=x, y_mm=y, layer="F.Cu")],
        nets=list(nets),
    )


def _state(*_args, **_kwargs):
    return fix_drc_cmd._MaskNativeState(errors=Counter(), opens=0)


def _refuse_every_candidate(checks: _Checks, rule: str = "track_dangling") -> None:
    """Inject the warning at the tap's anchor on each re-check: every candidate
    starts there, so none survives."""
    for ordinal in (1, 2, 3):
        checks.extra[ordinal] = [_warning(rule, ("SIG",), *ANCHOR)]


@pytest.fixture()
def quiet(monkeypatch):
    monkeypatch.setattr(fix_drc_cmd, "_kicad_cli_path", lambda: None)
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", _state)
    monkeypatch.setattr(fix_drc_cmd, "_mask_pad_islands", lambda board, nets: {})
    checks = _Checks()
    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", checks)
    return checks


def _argv(board: Path, *extra: str) -> list[str]:
    """``--only acute-branch`` on the tier-1 profile, as Board 03 runs it."""
    return [str(board), "--only", "acute-branch", "--mfr", "jlcpcb-tier1", "--quiet", *extra]


def _tap(pcb: PCB) -> list:
    """The SIG tracks other than the carrier, from the anchor on."""
    return [s for s in pcb.segments if (s.start, s.end) != (CARRIER.start, CARRIER.end)]


def test_clean_repair_writes_the_board_and_exits_zero(tmp_path, quiet):
    board = _board(tmp_path)
    carrier_uuid = PCB.load(board).segments[0].uuid
    assert fix_drc_cmd.main(_argv(board)) == 0
    saved = PCB.load(board)
    assert SharpTraceCornerRule().check(saved, None).violations == []
    kept = next(s for s in saved.segments if s.uuid == carrier_uuid)
    assert (kept.start, kept.end) == (CARRIER.start, CARRIER.end)
    assert len(_tap(saved)) == 2
    # One save for the verified candidate, then the final re-check.
    assert [p.name for p in quiet.calls[1:]] == ["usb.kicad_pcb", "usb.kicad_pcb"]


def test_a_new_dangling_warning_puts_the_edit_back_and_leaves_the_input(tmp_path, quiet):
    """The saved board reports a dangling end next to every candidate: the tap
    stays as it was and the board is the input byte for byte."""
    board = _board(tmp_path)
    before = board.read_bytes()
    _refuse_every_candidate(quiet)
    assert fix_drc_cmd.main(_argv(board)) == 2
    assert board.read_bytes() == before
    assert quiet.last is not None
    remaining = [v for v in quiet.last.violations if v.type_str == "sharp_trace_corner"]
    assert len(remaining) == 1 and remaining[0].nets == ["SIG"]


def test_a_new_redundant_copper_warning_vetoes_an_acute_edit(tmp_path, quiet):
    """The strict factory check counts a ``redundant_copper`` warning next, so a
    right-angle entry that leaves one behind is put back (unlike the
    redundant-copper pass, for which those findings are its own account)."""
    board = _board(tmp_path)
    before = board.read_bytes()
    _refuse_every_candidate(quiet, "redundant_copper_covered")
    assert fix_drc_cmd.main(_argv(board)) == 2
    assert board.read_bytes() == before


def test_the_next_landing_is_taken_after_a_veto(tmp_path, quiet):
    """Only the first saved candidate is refused: the tap is replanned onto its
    next landing, and that board is kept."""
    board = _board(tmp_path)
    quiet.extra[1] = [_warning("track_dangling", ("SIG",), *ANCHOR)]
    assert fix_drc_cmd.main(_argv(board)) == 0
    saved = PCB.load(board)
    assert SharpTraceCornerRule().check(saved, None).violations == []
    landing = max(_tap(saved), key=lambda s: s.start[0]).end
    assert landing[0] == 33.2 and landing[1] != 32.9  # not the first candidate's landing


def test_an_unrelated_new_warning_does_not_veto_by_default(tmp_path, quiet):
    """Only errors and the guarded warnings refuse an edit unless asked."""
    board = _board(tmp_path)
    quiet.extra[1] = [_warning("pour_slot", ("SIG",), *ANCHOR)]
    assert fix_drc_cmd.main(_argv(board)) == 0
    assert SharpTraceCornerRule().check(PCB.load(board), None).violations == []


def test_any_new_warning_vetoes_with_no_new_warnings(tmp_path, quiet):
    board = _board(tmp_path)
    before = board.read_bytes()
    _refuse_every_candidate(quiet, "pour_slot")
    assert fix_drc_cmd.main(_argv(board, "--no-new-warnings")) == 2
    assert board.read_bytes() == before


def test_a_new_native_open_reverts_the_edit(tmp_path, quiet, monkeypatch, capsys):
    board = _board(tmp_path)
    before = board.read_bytes()
    # The input reads 0 opens; every edited board shows one.
    reads = iter(range(100))

    def state(_board):
        return (
            _state()
            if next(reads) == 0
            else fix_drc_cmd._MaskNativeState(errors=Counter(), opens=1)
        )

    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", state)
    argv = _argv(board, "--format", "json")
    argv.remove("--quiet")
    assert fix_drc_cmd.main(argv) == 2
    assert board.read_bytes() == before
    block = json.loads(capsys.readouterr().out)["acute_branch"]
    assert block["repaired"] == 0
    assert [u["reason"] for u in block["unrepaired"]] == ["native_open"]
    assert block["unrepaired"][0]["net"] == "SIG"


def test_a_lost_pad_connection_reverts_the_edit(tmp_path, quiet, monkeypatch, capsys):
    """A candidate that would split the net's pads is refused and the previous
    copper restored."""
    board = _board(tmp_path)
    before = board.read_bytes()
    monkeypatch.setattr(fix_drc_cmd, "_mask_pad_islands", lambda b, nets: {"SIG": [frozenset()]})
    monkeypatch.setattr(fix_drc_cmd, "_split_mask_nets", lambda before, b: {"SIG"})
    argv = _argv(board, "--format", "json")
    argv.remove("--quiet")
    assert fix_drc_cmd.main(argv) == 2
    assert board.read_bytes() == before
    block = json.loads(capsys.readouterr().out)["acute_branch"]
    assert [u["reason"] for u in block["unrepaired"]] == ["pad_connection_lost"]


def test_report_names_the_refused_tap_and_its_reason(tmp_path, quiet, capsys):
    board = _board(tmp_path)
    _refuse_every_candidate(quiet)
    argv = _argv(board, "--format", "json")
    argv.remove("--quiet")
    assert fix_drc_cmd.main(argv) == 2
    doc = json.loads(capsys.readouterr().out)
    block = doc["acute_branch"]
    assert block["violations"] == 1 and block["repaired"] == 0
    assert [u["reason"] for u in block["unrepaired"]] == ["new_track_dangling"]
    assert block["unrepaired"][0]["rule"] == "sharp_trace_corner"
    assert doc["recheck"]["engines"][0]["targeted_warnings"] == 1
    assert doc["recheck"]["remaining_warnings"][0]["type"] == "sharp_trace_corner"
    assert doc["redundant_copper"] == {
        "violations": 0,
        "repaired": 0,
        "by_rule": {},
        "edits": [],
        "unrepaired": [],
    }


def test_dry_run_plans_without_writing(tmp_path, quiet):
    board = _board(tmp_path)
    before = board.read_bytes()
    assert fix_drc_cmd.main(_argv(board, "--dry-run")) == 0
    assert board.read_bytes() == before
    assert len(quiet.calls) == 1  # the detection run only: nothing saved, nothing re-checked


def test_other_categories_do_not_target_sharp_corners(tmp_path, quiet):
    """The findings are warnings: only ``--only acute-branch`` repairs them."""
    board = _board(tmp_path)
    before = board.read_bytes()
    argv = [str(board), "--only", "redundant-copper", "--mfr", "jlcpcb-tier1", "--quiet"]
    assert fix_drc_cmd.main(argv) == 2  # nothing targeted; the corner is "non-repairable" there
    assert board.read_bytes() == before


def test_post_pass_connectivity_drop_restores_input_board(tmp_path, quiet, monkeypatch):
    """A post-pass connectivity decrease must bulk-restore the pre-pass board
    instead of leaving the rewritten tap on disk."""
    board = _board(tmp_path)
    before = board.read_bytes()
    counts = iter([2, 1])
    monkeypatch.setattr(fix_drc_cmd, "_count_connected_nets", lambda _p: next(counts))
    monkeypatch.setattr(fix_drc_cmd, "_connectivity_report", lambda _p: None)
    monkeypatch.setattr(fix_drc_cmd, "_regressed_nets", lambda _b, _a: {"SIG"})
    assert fix_drc_cmd.main(_argv(board)) == 3
    assert board.read_bytes() == before
