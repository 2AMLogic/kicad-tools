"""Board 04's public ``mask_to_copper`` verdict after the repair (Issue #10664).

The committed routed PCB is ``kct fix-drc --only mask-copper``'s output on the
revision the issue measured (sha256 ``ab4c0289...``, ten errors).  The receipt
is published by the board's registered producer, whose ``tier_check`` gate
judges the generic ``kct check --mfr jlcpcb-tier1`` report exactly as ``kct
readiness --verify`` does; these tests read that evidence and re-judge it with
the verifier's own function, so the controls below show the exact exception
match was not loosened to let the board through.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD = REPO_ROOT / "boards" / "04-stm32-devboard"
OUTPUT = BOARD / "output"
ROUTED = OUTPUT / "stm32_devboard_routed.kicad_pcb"
RECEIPT = OUTPUT / "readiness.json"
TIER_CHECK = OUTPUT / "readiness" / "tier-check.json"
KNOWN_ISSUES = OUTPUT / "readiness" / "known-issues.json"
RECIPE = BOARD / "generate_design.py"

#: The revision the issue measured (ten errors) and the repaired board.
UNREPAIRED_REVISION = "b0a248c9e"
UNREPAIRED_SHA256 = "ab4c02899b71e0a248d8133692f06c2e630135870b08364d6813fbe83fd592c6"
REPAIRED_SHA256 = "bd9d473e0fc34ef7ece57a6456aa22d3d92d5a3866c8f320b3c8b28f0aba7281"
PAID_DRILL_RULES = frozenset(
    {
        "dimension_via_drill",
        "dimension_via_diameter",
        "dimension_annular_ring",
        "netclass_via_drill",
        "netclass_via_diameter",
        "netclass_annular_ring",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tier_report() -> dict:
    return json.loads(TIER_CHECK.read_text())


def _known_issues() -> list[dict]:
    return json.loads(KNOWN_ISSUES.read_text())["known_issues"]


def _judge(report: dict, issues: list[dict]):
    from kicad_tools.cli.readiness_cmd import judge_kct_check_report

    return judge_kct_check_report(report, issues)


def test_the_receipt_is_ready_and_binds_the_repaired_routed_pcb():
    receipt = json.loads(RECEIPT.read_text())
    assert _sha256(ROUTED) == REPAIRED_SHA256
    assert receipt["status"] == "ready" and receipt["blockers"] == []
    assert receipt["inputs"]["output/stm32_devboard_routed.kicad_pcb"] == REPAIRED_SHA256
    assert receipt["inputs"]["output/readiness/known-issues.json"] == _sha256(KNOWN_ISSUES)
    tier = next(check for check in receipt["checks"] if check["name"] == "tier_check")
    assert tier["status"] == "passed"


def test_the_public_check_ran_the_mask_rule_and_reports_no_mask_error():
    report = _tier_report()
    assert report["pcb_sha256"] == REPAIRED_SHA256
    assert report["summary"]["rules_checked_by_rule"]["mask_to_copper"] == 1
    errors = [v for v in report["violations"] if v["severity"] == "error"]
    assert errors and not any(v["rule_id"] == "mask_to_copper" for v in errors)
    assert {v["rule_id"] for v in errors} <= PAID_DRILL_RULES


def test_the_mask_findings_are_gone_not_excused():
    recorded = {entry["issue"] for entry in _known_issues()}
    assert 10664 not in recorded
    for entry in _known_issues():
        for item in entry.get("errors", []) + entry.get("warnings", []):
            assert not item["rule"].startswith("mask_to_copper"), item
        if entry["kind"] == "exception":
            assert {item["rule"] for item in entry["errors"]} <= PAID_DRILL_RULES


def test_control_a_mask_error_on_the_published_board_blocks_the_verdict():
    report, issues = _tier_report(), _known_issues()
    assert _judge(report, issues).status == "passed"
    injected = copy.deepcopy(report)
    injected["violations"].append(
        {
            "rule_id": "mask_to_copper",
            "type": "mask_to_copper",
            "severity": "error",
            "message": "Mask-to-copper overlap: violation; opening pad:U2/7",
            "location": [144.6, 89.8],
            "layer": "F.Cu",
            "items": [],
            "nets": ["NRST"],
            "status": "error",
            "waived": False,
        }
    )
    injected["summary"]["errors"] += 1
    outcome = _judge(injected, issues)
    assert outcome.status == "failed"
    assert "do not match the recorded exception identities" in outcome.detail
    assert "mask_to_copper at 144.60/89.80 (not recorded)" in outcome.detail


def _at(violation: dict) -> list[float]:
    """The identity's rounded location, as ``error_identity`` records it."""
    return [round(c, 2) + 0.0 for c in violation["location"]]


def test_control_a_reviewed_via_identity_that_moves_no_longer_matches():
    report, issues = _tier_report(), _known_issues()
    moved = copy.deepcopy(report)
    # The repair carried the SWCLK via from (172.6, 87.4) to (172.42, 87.58);
    # the exception was re-pinned for it, so the old position is no longer listed.
    located = [v for v in moved["violations"] if isinstance(v.get("location"), list)]
    swclk = [v for v in located if _at(v) == [172.42, 87.58] and v["severity"] == "error"]
    assert {v["rule_id"] for v in swclk} == {"dimension_via_drill"}
    assert not any(_at(v) == [172.6, 87.4] for v in located)
    for violation in swclk:
        violation["location"] = [172.6, 87.4]
    outcome = _judge(moved, issues)
    assert outcome.status == "failed"
    assert "dimension_via_drill at 172.60/87.40 (not recorded)" in outcome.detail
    assert (
        "dimension_via_drill at 172.42/87.58 (recorded for #6269, no longer fires)"
        in outcome.detail
    )


def test_the_recipe_repairs_the_mask_findings_before_its_gates():
    source = RECIPE.read_text()
    steps = [
        source.index('process["repair_bypass_mask_escapes"](routed_path)'),
        source.index("repair_mask_copper(routed_path, log=print)"),
        source.index("if not finalize_silkscreen(routed_path):"),
        source.index("run_drc(routed_path)"),
    ]
    assert steps == sorted(steps)
    assert "from kicad_tools.recipes.mask_repair import repair_mask_copper" in source


def test_the_unrepaired_revision_still_reports_the_ten_mask_errors(tmp_path):
    """The control: the copper the issue measured fails the public mask check."""
    from kicad_tools.recipes.mask_repair import mask_error_count

    shown = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "show",
            f"{UNREPAIRED_REVISION}:boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
        ],
        capture_output=True,
        check=False,
    )
    if shown.returncode:
        pytest.skip(f"revision {UNREPAIRED_REVISION} is not in this checkout's history")
    staged = tmp_path / "stm32_devboard_routed.kicad_pcb"
    staged.write_bytes(shown.stdout)
    assert hashlib.sha256(shown.stdout).hexdigest() == UNREPAIRED_SHA256
    for suffix in (".kicad_pro", ".kicad_dru"):
        staged.with_suffix(suffix).write_bytes(ROUTED.with_suffix(suffix).read_bytes())
    errors = mask_error_count(staged, "jlcpcb-tier1")
    if errors is None:
        pytest.skip("the mask assessment needs the qualified KiCad plot profile")
    assert errors == 10
    committed = mask_error_count(ROUTED, "jlcpcb-tier1")
    assert committed == 0
