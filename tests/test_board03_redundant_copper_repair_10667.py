"""Board 03's redundant-copper repair (Issue #10667).

The committed ``usb_joystick_routed.kicad_pcb`` carries the findings the
reissue cannot clear: covered tracks and staircases that ``kct readiness``
refuses and that ``kct pcb prune`` / ``kct optimize-traces`` trade for new
``sharp_trace_corner`` findings.  ``kct fix-drc --only redundant-copper``
(``kicad_tools.drc.repair_redundant_copper``) is the verified cleanup path.

These tests run the repairer's own gates on the committed copper -- the
rule's chain walk, shortcut gates and acute-corner test -- with no KiCad, so
they cover the legal cleanup and the veto in seconds.  The saved-board half
of the transaction (native DRC, the mask assessment, the refill) is exercised
by ``tests/test_fix_drc_redundant_copper.py`` with stand-ins and was measured
on the pinned toolchain for the PR; see the PR's verification table.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.drc.repair_redundant_copper import (
    REPAIRED_RULES,
    RedundantCopperRepairer,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker

BOARD_DIR = Path(__file__).resolve().parent.parent / "boards" / "03-usb-joystick"
BOARD = BOARD_DIR / "output" / "usb_joystick_routed.kicad_pcb"
ROUTING_PLAN = BOARD_DIR / "routing-plan.json"
#: The recipe's USB bounds (``routing_plan.usb_geometry``): the cleanup must
#: not move a pair member's copper length, so it may not widen the skew.
USB_NETS = ("USB_D+", "USB_D-", "USB_MCU_D+", "USB_MCU_D-")


def _checker(pcb: PCB) -> DRCChecker:
    return DRCChecker(pcb, manufacturer="jlcpcb-tier1", layers=4, warn_on_inactive_skew_rules=False)


def _redundant(pcb: PCB) -> list:
    """The committed rule's repairable findings on ``pcb``."""
    checker = _checker(pcb)
    return [
        v
        for v in checker.redundant_copper_rule().check(pcb, checker.design_rules).violations
        if v.rule_id in REPAIRED_RULES
    ]


def _findings(pcb: PCB) -> Counter:
    checker = _checker(pcb)
    return Counter(
        v.rule_id
        for v in checker.redundant_copper_rule().check(pcb, checker.design_rules).violations
    )


def _repairer(board: Path = BOARD) -> tuple[RedundantCopperRepairer, PCB]:
    pcb = PCB.load(board)
    checker = _checker(pcb)
    return (
        RedundantCopperRepairer(
            board, rule=checker.redundant_copper_rule(), design_rules=checker.design_rules
        ),
        pcb,
    )


def _witness_of(unrepaired, pcb: PCB) -> tuple[str, ...]:
    """The finding an unrepaired entry names (its uuid is the first witness)."""
    checker = _checker(pcb)
    return next(
        tuple(v.witnesses)
        for v in checker.redundant_copper_rule().check(pcb, checker.design_rules).violations
        if v.rule_id == unrepaired.rule_id and v.witnesses and v.witnesses[0] == unrepaired.uuid
    )


def _copper_length(pcb: PCB, net: str) -> float:
    return sum(math.dist(s.start, s.end) for s in pcb.segments if s.net_name == net)


def test_committed_copper_carries_only_the_repairable_kinds_and_duplicates():
    """The census the issue measured: covered tracks and staircases, plus the
    exact duplicates ``kct pcb dedupe`` takes off before this repair runs."""
    findings = _findings(PCB.load(BOARD))
    assert findings["redundant_copper_covered"] >= 40
    # 37 once the corner gate sees a branch landing inside a continuous track
    # (#10675): a staircase whose only shorter path would tap a straight run at
    # 45 deg is no longer offered (it was 45 before; PR #10698 review).
    assert findings["redundant_copper_staircase"] >= 30
    assert set(findings) <= REPAIRED_RULES | {"redundant_copper_duplicate"}


def test_legal_cleanup_clears_every_covered_track_and_staircase(tmp_path: Path):
    """Every finding has a path through the rule's own gates, and the saved
    board no longer reports one (Issue #10667)."""
    repairer, pcb = _repairer()
    before = _findings(pcb)
    started = time.monotonic()
    plan = repairer.plan(_redundant(pcb))
    assert time.monotonic() - started < 120, "the committed board's plan should not take minutes"
    assert plan.unrepaired == []
    assert repairer.live_findings() == set()
    by_rule = Counter(e.rule_id for e in plan.edits)
    # Rewriting a chain can lay a leg on kept copper; the rounds that follow
    # remove that double too, so covered edits are at least the findings.
    assert by_rule["redundant_copper_covered"] >= before["redundant_copper_covered"]
    assert by_rule["redundant_copper_staircase"] >= before["redundant_copper_staircase"]

    saved = tmp_path / "usb_joystick_routed.kicad_pcb"
    repairer.save(saved)
    after = _findings(PCB.load(saved))
    assert after["redundant_copper_covered"] == 0
    assert after["redundant_copper_staircase"] == 0
    assert after["redundant_copper_detour"] == 0
    # The duplicates are dedupe's: this repair takes off no copper that is
    # not a covered track or a staircase's (a hidden track that is also a
    # duplicate goes with its finding, so the count may only fall).
    assert after["redundant_copper_duplicate"] <= before["redundant_copper_duplicate"]


def test_cleanup_is_length_neutral_on_the_usb_pairs(tmp_path: Path):
    """The recipe bounds USB_D+ at 16 mm and the pair skew at 2.5 mm; the
    cleanup rewrites both members' staircases without moving a length."""
    repairer, pcb = _repairer()
    repairer.plan(_redundant(pcb))
    saved = tmp_path / "usb_joystick_routed.kicad_pcb"
    repairer.save(saved)
    cleaned = PCB.load(saved)
    for net in USB_NETS:
        assert _copper_length(cleaned, net) == pytest.approx(_copper_length(pcb, net), abs=1e-3)


def test_veto_restores_the_touched_copper_byte_for_byte(tmp_path: Path):
    """A refused edit is put back as it was written: the board with every edit
    refused is the committed board, byte for byte (the #9939 pattern)."""
    original = BOARD.read_bytes()
    repairer, pcb = _repairer()
    plan = repairer.plan(_redundant(pcb))
    assert plan.edits, "nothing to veto: the committed board has no redundant copper"
    staged = tmp_path / "staged.kicad_pcb"
    repairer.save(staged)
    assert staged.read_bytes() != original

    # Vetoing one edit drops the rest with it: they were gated against a
    # board that carried it.  The board is then the input, exactly.
    first = plan.edits[0]
    gone = repairer.veto(first, "new_sharp_trace_corner")
    assert gone[0].uuid == first.uuid and gone[0].reason == "new_sharp_trace_corner"
    assert len(gone) == len(plan.edits)
    assert repairer.edits == ()
    restored = tmp_path / "restored.kicad_pcb"
    repairer.save(restored)
    assert restored.read_bytes() == original

    # The refused path stays refused: the finding takes another path or is
    # unrepaired with the veto's reason, and the rest are planned again
    # against the board as it now stands.
    again = repairer.plan(_redundant(pcb))
    assert {e.finding for e in again.edits} | {
        (u.rule_id, _witness_of(u, pcb)) for u in again.unrepaired
    } == {e.finding for e in plan.edits}
    other = next((e for e in again.edits if e.finding == first.finding), None)
    if other is not None:
        assert other.path_key != first.path_key
    else:
        assert [u.reason for u in again.unrepaired] == ["new_sharp_trace_corner"]


def test_veto_of_one_finding_leaves_the_others_untouched(tmp_path: Path):
    """The veto is per finding: refusing every path of one staircase puts its
    copper back and re-plans the rest, and nothing else changes."""
    repairer, pcb = _repairer()
    plan = repairer.plan(_redundant(pcb))
    victim = next(e for e in plan.edits if e.kind == "rewrite_chain")
    removed = {s.uuid for s in victim.removed}
    added = {s.uuid for s in victim.added}
    finding = victim.finding
    # Each veto also drops the edits planned after it; they come back with
    # the next plan, until the victim's finding has no path left.
    refused = 0
    while True:
        gone = repairer.veto(victim, "new_track_dangling")
        assert gone[0].uuid == victim.uuid
        refused += 1
        again = repairer.plan(_redundant(pcb))
        victim = next((e for e in again.edits if e.finding == finding), None)
        if victim is None:
            break
    assert refused >= 2, "the staircase has more than one path"
    saved = tmp_path / "usb_joystick_routed.kicad_pcb"
    repairer.save(saved)
    cleaned = PCB.load(saved)
    uuids = {s.uuid for s in cleaned.segments}
    assert removed <= uuids, "the refused edit's copper is back"
    assert not added & uuids, "the refused edit's legs are gone"
    findings = _findings(cleaned)
    assert findings["redundant_copper_staircase"] == 1
    assert findings["redundant_copper_covered"] == 0
    reasons = [
        u.reason
        for u in repairer.plan(_redundant(pcb)).unrepaired
        if u.rule_id.endswith("staircase")
    ]
    assert reasons == ["new_track_dangling"]


def test_cleanup_touches_only_the_board(tmp_path: Path):
    """No hand-placed coordinates: the routing plan is not an input or an
    output of the repair, only the board is."""
    plan_before = ROUTING_PLAN.read_bytes()
    repairer, pcb = _repairer()
    repairer.plan(_redundant(pcb))
    saved = tmp_path / "usb_joystick_routed.kicad_pcb"
    repairer.save(saved)
    assert ROUTING_PLAN.read_bytes() == plan_before
    assert BOARD.read_bytes() != saved.read_bytes()
