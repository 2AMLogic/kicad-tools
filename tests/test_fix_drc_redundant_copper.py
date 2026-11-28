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
import math
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


def test_exhausted_round_limit_does_not_publish_vetoed_candidate(
    tmp_path, quiet, monkeypatch, capsys
):
    """PR #10698 regression: with one transaction round and a protected corner
    only on the first saved candidate, the command vetoes it, plans an
    unmeasured alternative, and must return without writing the vetoed bytes."""
    monkeypatch.setattr(fix_drc_cmd, "_REDUNDANT_TXN_ROUNDS", 1)
    board = _write(tmp_path, _staircase())
    before = board.read_bytes()
    quiet.extra[1] = [_warning("sharp_trace_corner", ("PWR",), 0.0, 0.0)]
    argv = _argv(board, "--format", "json")
    argv.remove("--quiet")
    assert fix_drc_cmd.main(argv) == 2
    assert board.read_bytes() == before
    doc = json.loads(capsys.readouterr().out)
    assert doc["redundant_copper"]["repaired"] == 0
    assert doc["redundant_copper"]["unrepaired"]


def test_moved_sharp_corner_within_proximity_radius_is_still_vetoed(tmp_path, quiet):
    """PR #10698 regression: a sharp corner at (0, 0) on the input replaced by
    one at (0.4, 0) on each candidate must not be masked by the 0.75 mm
    proximity tolerance."""
    board = _write(tmp_path, _staircase())
    before = board.read_bytes()
    quiet.extra[0] = [_warning("sharp_trace_corner", ("PWR",), 0.0, 0.0)]
    quiet.extra[1] = [_warning("sharp_trace_corner", ("PWR",), 0.4, 0.0)]
    quiet.extra[2] = [_warning("sharp_trace_corner", ("PWR",), 0.4, 0.0)]
    assert fix_drc_cmd.main(_argv(board)) == 2
    assert board.read_bytes() == before


def test_unchanged_sharp_corner_at_same_coordinate_is_not_treated_as_new(tmp_path, quiet):
    """Control for coordinate matching: an untouched pre-existing sharp corner
    at the same net, layer, and board coordinate does not block the repair."""
    board = _write(tmp_path, _staircase())
    quiet.extra[0] = [_warning("sharp_trace_corner", ("PWR",), 0.0, 0.0)]
    quiet.extra[1] = [_warning("sharp_trace_corner", ("PWR",), 0.0, 0.0)]
    # Exit 2 reflects the non-targeted pre-existing warning; the staircase itself
    # must be repaired rather than vetoed.
    assert fix_drc_cmd.main(_argv(board)) == 2
    saved = PCB.load(board)
    assert RedundantCopperRule().check(saved, None).violations == []
    assert len(saved.segments) == 2


def test_balanced_pair_stays_balanced_when_a_staircase_reuses_a_leg(tmp_path, quiet):
    chain = [(0, 0), (3, 0), (4, 1), (6, 1), (7, 2), (8, 2), (9, 3), (10, 3)]
    retained = [((0, 0), (2, 2)), ((2, 2), (5, 2)), ((0, 0), (-2, -2))]
    edges = [*zip(chain, chain[1:], strict=False), *retained]
    length = sum(math.dist(a, b) for a, b in edges)
    segments = [Board._Seg(a, b, width=0.2) for a, b in edges]
    segments.append(Board._Seg((0, 50), (length, 50), width=0.2, net_number=2))
    board = _write(tmp_path, segments)
    board.write_text(board.read_text().replace('"PWR"', '"USB_D+"').replace('"OTHER"', '"USB_D-"'))

    fix_drc_cmd.main(_argv(board))
    saved = PCB.load(board)
    lengths = {
        net: sum(math.dist(s.start, s.end) for s in saved.segments if s.net_name == net)
        for net in ("USB_D+", "USB_D-")
    }
    assert lengths["USB_D-"] == pytest.approx(length)
    assert lengths["USB_D+"] == pytest.approx(lengths["USB_D-"], abs=0.001)


def test_refused_covered_removal_preserves_an_independent_repair(tmp_path, quiet, monkeypatch):
    segments = [
        Board._Seg((a, y), (b, y), width=0.2, net_number=net)
        for net, y in ((1, 0), (2, 50))
        for a, b in ((0, 10), (2, 5))
    ]
    board = _write(tmp_path, segments)
    candidates = []

    def check(path, **kwargs):
        report = quiet(path, **kwargs)
        counts = Counter(seg.net_name for seg in PCB.load(path).segments)
        candidates.append((counts["PWR"], counts["OTHER"]))
        if counts["OTHER"] == 1:
            report.violations.append(_warning("sharp_trace_corner", ("OTHER",), 2, 50))
        return report

    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", check)
    assert fix_drc_cmd.main(_argv(board)) == 2
    assert (1, 1) in candidates and (1, 2) in candidates
    assert Counter(s.net_name for s in PCB.load(board).segments) == {"PWR": 1, "OTHER": 2}


def test_partial_veto_rechecks_surviving_edits_before_publishing(tmp_path, quiet):
    """PR #10698 regression: when one net's edit is vetoed with no alternate
    path while another net's edit survives, the transaction must save and
    verify the surviving-only candidate before writing it."""
    pwr = _staircase()
    other = [
        Board._Seg(
            (a[0], a[1] + 50.0), (b[0], b[1] + 50.0), width=2.6, net_number=2, net_name="OTHER"
        )
        for a, b in zip(Board._STAIR_PTS, Board._STAIR_PTS[1:], strict=False)
    ]
    board = _write(tmp_path, pwr + other)
    # Refuse both candidate paths of OTHER's staircase, leaving PWR clean.
    quiet.extra[1] = [_warning("sharp_trace_corner", ("OTHER",), 0.0, 50.0)]
    quiet.extra[2] = [_warning("sharp_trace_corner", ("OTHER",), 0.0, 50.0)]
    assert fix_drc_cmd.main(_argv(board)) == 2
    # Round 1: both -> veto OTHER path 1; Round 2: both -> veto OTHER path 2
    # (replan produces 0 new edits, PWR survives); Round 3: PWR-only verified
    # and published; Round 4: post-pass recheck.
    saved = PCB.load(board)
    remaining = RedundantCopperRule().check(saved, None).violations
    assert [v.nets for v in remaining] == [("OTHER",)]
    assert len([s for s in saved.segments if s.net_name == "PWR"]) == 2
    assert len([s for s in saved.segments if s.net_name == "OTHER"]) == len(other)


def test_unowned_blocking_warning_on_other_net_reverts_run(tmp_path, quiet):
    """A new blocking warning on a net no edit touched must still revert the
    run rather than passing uncharged."""
    board = _write(tmp_path, _staircase())
    before = board.read_bytes()
    quiet.extra[1] = [_warning("pour_slot", ("OTHER",), 80.0, 80.0)]
    quiet.extra[2] = [_warning("pour_slot", ("OTHER",), 80.0, 80.0)]
    assert fix_drc_cmd.main(_argv(board, "--no-new-warnings")) == 2
    assert board.read_bytes() == before


def test_post_pass_connectivity_drop_restores_input_board(tmp_path, quiet, monkeypatch):
    """PR #10698 regression: a post-pass connectivity decrease after a
    redundant-copper pass must bulk-restore the pre-pass snapshot instead of
    leaving the modified board on disk with zero edits restored."""
    board = _write(tmp_path, _staircase())
    before = board.read_bytes()
    counts = iter([2, 1])
    monkeypatch.setattr(fix_drc_cmd, "_count_connected_nets", lambda _p: next(counts))
    monkeypatch.setattr(fix_drc_cmd, "_connectivity_report", lambda _p: None)
    monkeypatch.setattr(fix_drc_cmd, "_regressed_nets", lambda _b, _a: {"PWR"})
    assert fix_drc_cmd.main(_argv(board)) == 3
    assert board.read_bytes() == before
