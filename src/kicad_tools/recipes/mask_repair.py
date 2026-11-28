"""Clear same-net ``mask_to_copper`` errors with the verified repair (issue #10664).

``kct fix-drc --only mask-copper`` (#9334, #9896, #9939) is the repo's
remediation path for a track or via that sits inside the error band beside its
own pad's mask opening: it re-centres own-pad entries, slides pass-throughs and
vias clear, takes away a redundant edge fragment (#10664) and widens a pad's
own-pour relief past the band the rule examines (#10664).  Every move is
re-checked on the saved, refilled board against native DRC and pad
connectivity, and a move that fails there is reverted.

A recipe calls :func:`repair_mask_copper` on the routed board once its copper
is final and its zones are filled: the repairer's own native gate compares the
saved board before and after each move, and on an unfilled board that
comparison is pour-served-opens noise.  The tool refills the zones itself after
saving, so the copper the recipe's DRC and LVS gates re-derive stays
fill-consistent.
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MaskRepairError", "MaskRepairOutcome", "mask_error_count", "repair_mask_copper"]

#: ``fix-drc`` exit codes that mean the pass ran: every move kept (0), none
#: kept or the pass rolled back for a failed re-check (1), some kept (2).
_RAN_EXIT_CODES = frozenset({0, 1, 2})


class MaskRepairError(RuntimeError):
    """The repair could not run or its result could not be assessed."""


@dataclass(frozen=True)
class MaskRepairOutcome:
    """What one repair run left on the board."""

    repaired: int  # mask_to_copper errors removed
    remaining: int  # mask_to_copper errors still on the board
    invocations: int  # ``fix-drc`` runs made


def mask_error_count(pcb_path: str | Path, mfr: str) -> int | None:
    """``mask_to_copper`` errors on ``pcb_path`` under ``mfr``'s policy; ``None``
    when the assessment cannot complete (an unqualified KiCad plot profile)."""
    from kicad_tools.validate.mask_copper import (
        assessment_results,
        check_mask_to_copper,
        default_request_for_manufacturer,
    )

    request = default_request_for_manufacturer(mfr)
    if request is None or request.policy is None:
        raise MaskRepairError(f"no mask_to_copper policy is recorded for {mfr}")
    assessment = check_mask_to_copper(pcb_path, policy=request.policy)
    if assessment.coverage != "complete":
        return None
    return sum(1 for v in assessment_results(assessment).violations if v.is_error)


def repair_mask_copper(
    routed_path: str | Path,
    *,
    mfr: str = "jlcpcb-tier1",
    max_displacement: float = 0.5,
    max_passes: int = 3,
    max_invocations: int = 3,
    log: Callable[[str], None] | None = None,
) -> MaskRepairOutcome:
    """Run the verified mask repair on ``routed_path`` until it stops making progress.

    Each invocation is one ``kct fix-drc --only mask-copper`` run of up to
    ``max_passes`` detect-repair cycles; a cycle re-runs detection, so a move
    an earlier cycle had to defer (a jog slide behind an exit extension) is
    picked up by the next.  The loop stops when the board carries no
    ``mask_to_copper`` error, or when an invocation leaves the file bytes
    unchanged: every remaining finding was declined for a structural reason
    the tool will not revisit.  Those findings are the caller's to gate on
    (``MaskRepairOutcome.remaining``); the function does not fail the recipe
    for them, matching Board 03's staged driver (#8900).

    Args:
        routed_path: Routed, zone-filled ``.kicad_pcb``; re-saved in place.
        mfr: Manufacturer whose ``mask_to_copper`` policy applies.
        max_displacement: Longest move ``fix-drc`` may make, in mm.
        max_passes: Detect-repair cycles per invocation.
        max_invocations: Invocations before the loop gives up on converging.
        log: Optional sink for progress lines (recipes pass ``print``).

    Returns:
        The errors removed and the errors left.

    Raises:
        MaskRepairError: ``fix-drc`` failed (a connectivity rollback, a failed
            mask assessment or any exit outside its run codes), or the mask
            assessment could not complete before or after a run.
    """
    path = Path(routed_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    say = log or (lambda _message: None)
    before = mask_error_count(path, mfr)
    if before is None:
        raise MaskRepairError(
            f"the mask_to_copper assessment of {path.name} did not complete "
            "(an unqualified KiCad plot profile?)"
        )
    repaired = 0
    for invocation in range(1, max_invocations + 1):
        if before == 0:
            say(f"   mask repair: no mask_to_copper error on {path.name}")
            return MaskRepairOutcome(0, 0, invocation - 1)
        previous = path.read_bytes()
        code = _run_fix_drc(path, mfr, max_displacement, max_passes, say)
        if code not in _RAN_EXIT_CODES:
            raise MaskRepairError(f"fix-drc mask-copper pass failed (exit {code})")
        after = mask_error_count(path, mfr)
        if after is None:
            raise MaskRepairError(
                f"the mask_to_copper assessment of {path.name} did not complete after "
                f"fix-drc (invocation {invocation})"
            )
        repaired += before - after
        say(f"   mask repair, invocation {invocation}: {before} -> {after} error(s)")
        if after == 0:
            return MaskRepairOutcome(repaired, 0, invocation)
        if path.read_bytes() == previous:
            say(f"   mask repair: converged with {after} finding(s) the tool declines to move")
            return MaskRepairOutcome(repaired, after, invocation)
        before = after
    say(f"   mask repair: stopped after {max_invocations} invocations with {before} error(s)")
    return MaskRepairOutcome(repaired, before, max_invocations)


def _run_fix_drc(
    path: Path, mfr: str, max_displacement: float, max_passes: int, say: Callable[[str], None]
) -> int:
    """One in-process ``fix-drc --only mask-copper`` run, its output echoed through ``say``."""
    from kicad_tools.cli import fix_drc_cmd

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fix_drc_cmd.main(
            [
                str(path),
                "--only",
                "mask-copper",
                "--mfr",
                mfr,
                "--max-displacement",
                str(max_displacement),
                "--max-passes",
                str(max_passes),
                "--quiet",
            ]
        )
    for line in (*out.getvalue().splitlines(), *err.getvalue().splitlines()):
        if line.strip() and "3D coverage" not in line:
            say(f"   {line}")
    return code
