"""A board the public verifier blocks must not keep a ``ready`` receipt (Issue #10662).

Board 04's producer ran only its paid-drill checker, which never evaluates the
rules ``kct readiness --verify`` runs by default, so it re-pinned ``ready`` on a
board the public verifier blocked and nothing in the publish path noticed.  A
board registered with ``publish_blocked`` now also runs the public verifier's
generic-tier check, judged by the verifier's own function, and publishes the
verdict it measures: ``blocked`` naming the unaccepted findings, or ``ready``
only when the recorded exception matches exactly (#7355).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

import kicad_tools.cli.board_readiness as board_readiness
from tests.test_refresh_readiness_script import _CHECK_SCRIPT, _fake_board

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "boards" / "refresh_readiness.py"
CHECKED_AT = "2026-11-27T12:00:00+00:00"
REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_04 = REPO_ROOT / "boards" / "04-stm32-devboard"

VIA_A = ("dimension_via_drill", 10.0, 20.5)
VIA_B = ("dimension_via_drill", 11.25, -3.0)
VIA_C = ("dimension_via_diameter", 30.0, 5.0)
RECORDED = [VIA_A, VIA_B, VIA_C]


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("refresh_publishes_blocked_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _violation(finding: tuple[str, float, float]) -> dict:
    rule, x, y = finding
    return {"rule_id": rule, "severity": "error", "location": [x, y], "items": ["u", "v"]}


def _tier_report(findings: list[tuple[str, float, float]]) -> dict:
    """A generic-tier ``kct check`` report, as the public verifier's engine writes it."""
    return {
        "file": "output/board_routed.kicad_pcb",
        "manufacturer": "jlcpcb-tier1",
        "pcb_sha256": "0" * 64,
        "layers": 2,
        "summary": {"errors": len(findings), "warnings": 0, "passed": not findings},
        "violations": [_violation(f) for f in findings],
        # Re-hashed on every plot (kicad-cli stamps the film), so never evidence.
        "mask_copper_assessments": [{"gerber_sha256": {"F.Mask": "1" * 64}}],
        "meta_checks": {
            "drc": {"status": "FAILED" if findings else "PASSED"},
            "erc": {"status": "PASSED"},
            "lvs": {"status": "PASSED", "mismatches": [], "copper_mismatches": []},
            "manifest": {"status": "PASSED"},
        },
        "coverage": {},
    }


def _publishing_board(mod, tmp_path, monkeypatch, *, warnings: int = 0, **config) -> Path:
    """A board whose #6269-style exception records exactly ``RECORDED``."""
    board = _fake_board(tmp_path)
    (board / "check_board.py").write_text(_CHECK_SCRIPT)
    (board / "warnings.txt").write_text(str(warnings))
    entry = {
        "issue": 6269,
        "kind": "exception",
        "summary": "paid drill",
        "recorded": "2026-11-27",
        "errors": sorted(
            ({"rule": rule, "at": [x, y]} for rule, x, y in RECORDED),
            key=lambda item: (item["rule"], item["at"]),
        ),
    }
    evidence = board / "output" / "readiness" / "known-issues.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({"known_issues": [entry]}) + "\n")
    receipt = json.loads((board / "output" / "readiness.json").read_text())
    receipt["known_issues"] = [entry]
    receipt["inputs"]["output/readiness/known-issues.json"] = "0" * 64
    receipt["checks"] = [
        {"name": "kct_check", "status": "passed", "detail": "Reviewed paid-drill process."},
        {"name": "native_drc", "status": "passed", "detail": "0 violations"},
    ]
    (board / "output" / "readiness.json").write_text(json.dumps(receipt, indent=2) + "\n")
    monkeypatch.setitem(
        mod.BOARDS,
        board.name,
        mod.BoardConfig(
            routed_pcb="output/board_routed.kicad_pcb",
            schematic="output/board.kicad_sch",
            tier="jlcpcb-tier1",
            erc=(),
            check_script="check_board.py",
            tier_check="output/readiness/tier-check.json",
            archives=(),
            **{"publish_blocked": True, **config},
        ),
    )
    monkeypatch.setattr(mod, "gate_native_drc", lambda *a: (0, b"{}"))
    monkeypatch.setattr(mod, "gate_net_status", lambda *a: 100.0)
    monkeypatch.setattr(mod, "gate_sync", lambda *a: None)
    # read_readiness() itself is covered elsewhere; echo the staged receipt.
    monkeypatch.setattr(
        board_readiness,
        "read_readiness",
        lambda b: {"status": json.loads((b / "output" / "readiness.json").read_text())["status"]},
    )
    return board


def _public_check_writes(mod, monkeypatch, report: dict) -> list[list[str]]:
    """Make the generic-tier ``kct check`` subprocess answer with *report*."""
    real_run, calls = mod.run, []

    def run(cmd, **kwargs):
        if "kicad_tools.cli" in cmd:
            calls.append(cmd)
            Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(report))
            return subprocess.CompletedProcess(cmd, 2)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(mod, "run", run)
    return calls


def _receipt(board: Path, name: str = "readiness.json") -> dict:
    return json.loads((board / "output" / name).read_text())


def _check(receipt: dict, name: str) -> dict:
    return next(c for c in receipt["checks"] if c["name"] == name)


# ---------------------------------------------------------------------------
# The verdict the publisher publishes
# ---------------------------------------------------------------------------


def test_exact_exception_match_publishes_ready_and_binds_the_evidence(
    mod, tmp_path, monkeypatch
) -> None:
    board = _publishing_board(mod, tmp_path, monkeypatch)
    calls = _public_check_writes(mod, monkeypatch, _tier_report(RECORDED))

    published = mod.refresh(board, CHECKED_AT)

    assert "output/readiness.json" in published
    assert "output/readiness/tier-check.json" in published
    receipt = _receipt(board)
    assert receipt["status"] == "ready" and receipt["blockers"] == []
    tier = _check(receipt, "tier_check")
    assert tier["status"] == "passed"
    assert "3 error(s) accepted exactly by the recorded exception allowance" in tier["detail"]
    # The board-local check still passes: its recorded wording is kept.
    assert _check(receipt, "kct_check") == {
        "name": "kct_check",
        "status": "passed",
        "detail": "Reviewed paid-drill process.",
    }
    assert receipt["evidence"]["tier_check"] == "output/readiness/tier-check.json"
    evidence = board / "output" / "readiness" / "tier-check.json"
    assert receipt["inputs"]["output/readiness/tier-check.json"] == mod.sha256(evidence)
    assert "mask_copper_assessments" not in json.loads(evidence.read_text())
    # The invocation is the public verifier's, recorded board-relative.
    assert calls[0][3:-1] == [
        "check",
        "output/board_routed.kicad_pcb",
        "--mfr",
        "jlcpcb-tier1",
        "--schematic",
        "output/board.kicad_sch",
        "--format",
        "json",
        "--output",
    ]
    assert calls[0][-1].endswith("/tier-check.json")
    assert not calls[0][-1].endswith("/99-fake/output/readiness/tier-check.json")  # fresh path


def test_changed_identity_control_still_publishes_blocked(mod, tmp_path, monkeypatch) -> None:
    """One recorded via moves: the unchanged count must not pass (#7355)."""
    moved = (VIA_B[0], VIA_B[1] + 1.0, VIA_B[2])
    board = _publishing_board(mod, tmp_path, monkeypatch)
    _public_check_writes(mod, monkeypatch, _tier_report([VIA_A, moved, VIA_C]))

    published = mod.refresh(board, CHECKED_AT)

    receipt = _receipt(board)
    assert "output/readiness.json" in published
    assert receipt["status"] == "blocked"
    assert _check(receipt, "tier_check")["status"] == "failed"
    assert len(receipt["blockers"]) == 1
    blocker = receipt["blockers"][0]
    assert blocker.startswith("kct check reports 3 error(s); unaccepted error(s): ")
    assert "dimension_via_drill at 12.25/-3.00 (not recorded)" in blocker
    assert "dimension_via_drill at 11.25/-3.00 (recorded for #6269, no longer fires)" in blocker
    assert blocker.endswith(
        "[invocation: kct check <pcb> --mfr jlcpcb-tier1 --schematic output/board.kicad_sch]"
    )
    # Both decisions are retained: the process check passed, the tier check did not.
    assert _check(receipt, "kct_check")["status"] == "passed"
    assert _check(receipt, "native_drc")["status"] == "passed"


def test_unrecorded_finding_publishes_blocked(mod, tmp_path, monkeypatch) -> None:
    """An exact exception never covers a finding of another rule (#7355)."""
    mask = ("mask_to_copper", 144.6, 89.8)
    board = _publishing_board(mod, tmp_path, monkeypatch)
    _public_check_writes(mod, monkeypatch, _tier_report([*RECORDED, mask]))

    mod.refresh(board, CHECKED_AT)

    receipt = _receipt(board)
    assert receipt["status"] == "blocked"
    assert "mask_to_copper at 144.60/89.80 (not recorded)" in receipt["blockers"][0]


def test_failing_board_check_publishes_blocked_and_a_repair_flips_back(
    mod, tmp_path, monkeypatch
) -> None:
    board = _publishing_board(mod, tmp_path, monkeypatch, warnings=1)
    _public_check_writes(mod, monkeypatch, _tier_report(RECORDED))

    mod.refresh(board, CHECKED_AT)

    receipt = _receipt(board)
    assert receipt["status"] == "blocked"
    failed = _check(receipt, "kct_check")
    assert failed["status"] == "failed"
    assert failed["detail"].startswith(
        "check_board.py: 0 error(s); warning(s) outside the recorded allowance: "
    )
    assert receipt["blockers"] == [failed["detail"]]
    assert _check(receipt, "tier_check")["status"] == "passed"

    # The repair: the committed receipt is blocked, the gates now pass.
    (board / "warnings.txt").write_text("0")
    mod.refresh(board, CHECKED_AT)

    receipt = _receipt(board)
    assert receipt["status"] == "ready" and receipt["blockers"] == []
    assert all(c["status"] == "passed" for c in receipt["checks"])


def test_known_defect_blocks_whatever_the_checks_measure(mod, tmp_path, monkeypatch) -> None:
    """``kct readiness`` blocks on an open defect whatever the checks say (#7261)."""
    board = _publishing_board(mod, tmp_path, monkeypatch)
    defect = {
        "issue": 10664,
        "kind": "defect",
        "summary": "same-net mask findings",
        "recorded": "2026-11-27",
    }
    evidence = board / "output" / "readiness" / "known-issues.json"
    recorded = json.loads(evidence.read_text())
    recorded["known_issues"].append(defect)
    evidence.write_text(json.dumps(recorded))
    receipt = _receipt(board)
    receipt["known_issues"] = recorded["known_issues"]
    (board / "output" / "readiness.json").write_text(json.dumps(receipt, indent=2) + "\n")
    _public_check_writes(mod, monkeypatch, _tier_report(RECORDED))

    mod.refresh(board, CHECKED_AT)

    receipt = _receipt(board)
    assert receipt["status"] == "blocked"
    assert all(c["status"] == "passed" for c in receipt["checks"])
    assert receipt["blockers"] == ["known defect #10664: same-net mask findings"]


def test_failed_check_no_gate_re_measured_keeps_the_receipt_blocked(
    mod, tmp_path, monkeypatch
) -> None:
    """A person's failed entry is not cleared by gates that never measured it."""
    board = _publishing_board(mod, tmp_path, monkeypatch)
    receipt = _receipt(board)
    receipt["checks"].append({"name": "bom", "status": "failed", "detail": "part EOL"})
    (board / "output" / "readiness.json").write_text(json.dumps(receipt, indent=2) + "\n")
    _public_check_writes(mod, monkeypatch, _tier_report(RECORDED))

    mod.refresh(board, CHECKED_AT)

    receipt = _receipt(board)
    assert receipt["status"] == "blocked"
    assert receipt["blockers"] == ["part EOL"]
    assert _check(receipt, "tier_check")["status"] == "passed"


def test_without_publish_blocked_a_failed_tier_check_publishes_nothing(
    mod, tmp_path, monkeypatch
) -> None:
    board = _publishing_board(mod, tmp_path, monkeypatch, publish_blocked=False)
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}
    _public_check_writes(mod, monkeypatch, _tier_report([VIA_A, VIA_C]))

    with pytest.raises(mod.GateFailure, match="no longer fires"):
        mod.refresh(board, CHECKED_AT)
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


def test_checker_that_writes_no_report_cannot_reissue_a_passing_receipt(
    mod, tmp_path, monkeypatch
) -> None:
    """A second refresh whose tier check dies must not re-judge the last report."""
    board = _publishing_board(mod, tmp_path, monkeypatch)
    _public_check_writes(mod, monkeypatch, _tier_report(RECORDED))
    mod.refresh(board, CHECKED_AT)
    assert _check(_receipt(board), "tier_check")["status"] == "passed"
    published = board / "output" / "readiness" / "tier-check.json"
    assert published.is_file()
    before = {p: p.read_bytes() for p in board.rglob("*") if p.is_file()}

    real_run = mod.run

    def dying(cmd, **kwargs):
        if "kicad_tools.cli" in cmd:
            # exits 1 ("Path not found") and writes nothing, as the real check does
            return subprocess.CompletedProcess(cmd, 1, "", "Path not found")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(mod, "run", dying)
    with pytest.raises(mod.GateFailure, match="without producing a report"):
        mod.refresh(board, "2026-11-29T00:00:00Z")
    assert {p: p.read_bytes() for p in board.rglob("*") if p.is_file()} == before


def test_blocked_publish_is_a_non_zero_exit(mod, tmp_path, monkeypatch, capsys) -> None:
    board = _publishing_board(mod, tmp_path, monkeypatch)
    _public_check_writes(mod, monkeypatch, _tier_report([VIA_A]))
    monkeypatch.setattr(mod, "evidence_toolchain", lambda mounts: contextlib.nullcontext())

    assert mod.main([str(board), "--checked-at", CHECKED_AT]) == 1
    assert "BLOCKED: 99-fake does not verify ready (exit 1)" in capsys.readouterr().out

    _public_check_writes(mod, monkeypatch, _tier_report(RECORDED))
    assert mod.main([str(board), "--checked-at", CHECKED_AT]) == 0
    assert "BLOCKED" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Registration and the committed board
# ---------------------------------------------------------------------------


def test_only_board_04_publishes_its_verdict(mod) -> None:
    publishing = {name for name, cfg in mod.BOARDS.items() if cfg.publish_blocked}
    assert publishing == {"04-stm32-devboard"}
    assert mod.BOARDS["04-stm32-devboard"].tier_check == "output/readiness/tier-check.json"


def test_real_board_04_receipt_carries_the_public_verdict() -> None:
    """The committed receipt carries the public verdict, and validates as ready.

    #10662 found this receipt ``blocked`` by ten ``mask_to_copper`` errors the
    paid-drill checker never saw; #10664 repaired that copper and re-issued the
    board through this producer, so the tier gate now passes on an exact
    exception match and no defect is recorded.
    """
    from kicad_tools.cli.board_readiness import read_readiness

    receipt = json.loads((BOARD_04 / "output" / "readiness.json").read_text())
    assert receipt["status"] == "ready" and receipt["blockers"] == []
    kinds = {entry["issue"]: entry["kind"] for entry in receipt["known_issues"]}
    assert kinds == {6269: "exception"}
    tier = _check(receipt, "tier_check")
    assert (
        tier["status"] == "passed"
        and "accepted exactly by the recorded exception" in tier["detail"]
    )
    verdict = read_readiness(BOARD_04)
    assert verdict["status"] == "ready", verdict["blockers"]
