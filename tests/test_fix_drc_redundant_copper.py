"""``kct fix-drc --only redundant-copper``: the verified transaction (Issue #10667).

The repairer's own gates are covered by ``tests/test_repair_redundant_copper.py``;
these drive the ``fix-drc`` pass that re-checks the *saved* board and puts an
edit back.  ``kct check``'s rule set is replaced by the ``redundant_copper``
rule alone (plus whatever warning a test injects next to the edit), and the
native DRC by a constant state, so each case runs in milliseconds; the boards
are the rule test module's staircases.
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
from kicad_tools.validate.rules.redundant_copper import RedundantCopperRule

_RULE_TESTS = Path(__file__).resolve().parent / "test_validate_redundant_copper.py"


def _rule_tests():
    spec = importlib.util.spec_from_file_location("kct_rule_tests_10667_cli", _RULE_TESTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


Board = _rule_tests()

_project = Path(__file__).resolve().parent.parent


def _write(tmp_path: Path, segments) -> Path:
    body = "".join(
        f"  (segment (start {s.start[0]} {s.start[1]}) (end {s.end[0]} {s.end[1]}) "
        f'(width {s.width}) (layer "{s.layer}") (net {s.net_number}) '
        f'(uuid "s{index:04d}-0000-4000-8000-000000000000"))\n'
        for index, s in enumerate(segments)
    )
    path = tmp_path / "usb.kicad_pcb"
    path.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (net 0 "") (net 1 "PWR") (net 2 "OTHER")\n' + body + ")\n"
    )
    PCB.load(path).save(path)
    return path


def _staircase() -> list:
    return [
        Board._Seg(a, b, width=2.6, net_name="PWR")
        for a, b in zip(Board._STAIR_PTS, Board._STAIR_PTS[1:], strict=False)
    ]


class _Checks:
    """``_run_python_drc`` with only the redundant-copper rule, plus injections.

    ``extra`` maps a call ordinal (1-based, after the detection run) to the
    warnings that run reports in addition, so a test can fail the first
    saved board and pass the next one.
    """

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.extra: dict[int, list[DRCViolation]] = {}
        self.last: DRCReport | None = None

    def __call__(self, pcb_path, **_kwargs):
        pcb = PCB.load(pcb_path)
        results = RedundantCopperRule().check(pcb, None)
        report = drc_results_to_report(results, pcb_path)
        # The real run's mask assessment is complete on these boards; the
        # pass fails closed on anything less.
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


def _refuse_every_saved_board(checks: _Checks, rule: str = "sharp_trace_corner") -> None:
    """Inject the warning next to the chain's start on each re-check: both of
    the chain's paths start there, so neither survives."""
    for ordinal in (1, 2):
        checks.extra[ordinal] = [_warning(rule, ("PWR",), 0.0, 0.0)]


@pytest.fixture()
def quiet(monkeypatch):
    monkeypatch.setattr(fix_drc_cmd, "_kicad_cli_path", lambda: None)
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", _state)
    monkeypatch.setattr(fix_drc_cmd, "_mask_pad_islands", lambda board, nets: {})
    checks = _Checks()
    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", checks)
    return checks


def _argv(board: Path, *extra: str) -> list[str]:
    """``--only redundant-copper`` on the tier-1 profile, as Board 03 runs it
    (the plain ``jlcpcb`` profile adds the fab-view slit oracle, which fails
    closed on a board with no declared stackup)."""
    return [str(board), "--only", "redundant-copper", "--mfr", "jlcpcb-tier1", "--quiet", *extra]


def test_clean_repair_writes_the_board_and_exits_zero(tmp_path, quiet):
    board = _write(tmp_path, _staircase())
    before = len(PCB.load(board).segments)
    assert fix_drc_cmd.main(_argv(board)) == 0
    saved = PCB.load(board)
    assert len(saved.segments) == before - 9 + 2
    assert RedundantCopperRule().check(saved, None).violations == []


def test_a_new_sharp_corner_puts_the_edit_back_and_leaves_the_input(tmp_path, quiet):
    """The saved board reports a corner the rule's gates did not see: the edit
    goes, the chain has no other path, and the board is the input byte for byte."""
    board = _write(tmp_path, _staircase())
    before = board.read_bytes()
    _refuse_every_saved_board(quiet)
    assert fix_drc_cmd.main(_argv(board)) == 2
    assert board.read_bytes() == before
    assert quiet.last is not None
    remaining = [v for v in quiet.last.violations if v.type_str.startswith("redundant_copper")]
    assert [v.type_str for v in remaining] == ["redundant_copper_staircase"]


def test_the_next_path_is_taken_after_a_veto(tmp_path, quiet):
    """The chain's second path is planned once the first is refused, and kept
    when the saved board is clean around it."""
    board = _write(tmp_path, _staircase())
    quiet.extra[1] = [_warning("sharp_trace_corner", ("PWR",), 0.0, 0.0)]
    # The second path starts along the same leg, so refuse that corner only once.
    assert fix_drc_cmd.main(_argv(board)) == 0
    saved = PCB.load(board)
    assert RedundantCopperRule().check(saved, None).violations == []
    # Diagonal-first was refused; orthogonal-first (0,0) -> (14,0) -> (24,10) is kept.
    legs = sorted((s.start, s.end) for s in saved.segments)
    assert any(start == (0.0, 0.0) and end == (14.0, 0.0) for start, end in legs) or any(
        start == (14.0, 0.0) and end == (0.0, 0.0) for start, end in legs
    )


def test_an_unrelated_new_warning_does_not_veto_by_default(tmp_path, quiet):
    """Only errors and the guarded warnings refuse an edit unless asked."""
    board = _write(tmp_path, _staircase())
    quiet.extra[1] = [_warning("pour_slot", ("PWR",), 0.0, 0.0)]
    assert fix_drc_cmd.main(_argv(board)) == 0
    assert RedundantCopperRule().check(PCB.load(board), None).violations == []


def test_any_new_warning_vetoes_with_no_new_warnings(tmp_path, quiet):
    board = _write(tmp_path, _staircase())
    before = board.read_bytes()
    _refuse_every_saved_board(quiet, "pour_slot")
    assert fix_drc_cmd.main(_argv(board, "--no-new-warnings")) == 2
    assert board.read_bytes() == before


def test_a_new_native_open_reverts_every_edit(tmp_path, quiet, monkeypatch):
    board = _write(tmp_path, _staircase())
    before = board.read_bytes()
    # The input reads 0 opens; only the edited board shows one.
    states = iter([_state(), fix_drc_cmd._MaskNativeState(errors=Counter(), opens=1)])
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", lambda b: next(states))
    assert fix_drc_cmd.main(_argv(board)) == 2
    assert board.read_bytes() == before


def test_report_names_the_refused_edit_and_its_reason(tmp_path, quiet, capsys):
    board = _write(tmp_path, _staircase())
    _refuse_every_saved_board(quiet)
    argv = _argv(board, "--format", "json")
    argv.remove("--quiet")  # --quiet suppresses the JSON document too
    assert fix_drc_cmd.main(argv) == 2

    doc = json.loads(capsys.readouterr().out)
    block = doc["redundant_copper"]
    assert block["violations"] == 1 and block["repaired"] == 0
    assert [u["reason"] for u in block["unrepaired"]] == ["new_sharp_trace_corner"]
    assert doc["recheck"]["engines"][0]["targeted_warnings"] == 1
    assert doc["recheck"]["remaining_warnings"][0]["type"] == "redundant_copper_staircase"


def test_dry_run_plans_without_writing(tmp_path, quiet):
    board = _write(tmp_path, _staircase())
    before = board.read_bytes()
    assert fix_drc_cmd.main(_argv(board, "--dry-run")) == 0
    assert board.read_bytes() == before
    assert len(quiet.calls) == 1  # the detection run only: nothing saved, nothing re-checked
