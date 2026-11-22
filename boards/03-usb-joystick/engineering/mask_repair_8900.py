"""Scratch-only staging of Board 03's verified mask-copper repair (Issue #8900).

``kct fix-drc --only mask-copper`` (#9334 / #9905) re-centres tracks and slides
vias out of foreign mask openings on the routed board (72 -> 41 same-net
``mask_to_copper`` errors on the pinned toolchain, every move re-checked
against native DRC and pad connectivity).

This module is deliberately NOT called from the live
``boards/03-usb-joystick/generate_design.py``.  The operator's Board 03
ruling (#8900) forbids an expected-stale entry and requires the published
``output/manufacturing/design-source/generate_design.py`` to match the live
recipe byte for byte, so the live recipe cannot gain Step 5.55 until the
consistent producer-generated release (copper, Gerbers, manifests and the
archived recipe re-issued together) lands -- after the residual findings
(#9939) and the #9083 silkscreen wiring.  Same staging as Board 07
(``real_design/engineering``, PR #9925).  The release PR moves
``repair_mask_copper`` into the recipe as Step 5.55 (after the Step 5.5 fill,
before Step 6 ``run_drc``) and deletes this file.

Usage on a scratch copy of the routed, filled board::

    uv run python boards/03-usb-joystick/engineering/mask_repair_8900.py PCB
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# ``kct fix-drc --only mask-copper`` summary line the repair step parses to
# tell "made no progress on real findings" (a hard error) from "nothing left
# to repair" (the converged end state this issue is driving toward).
_FIX_DRC_SUMMARY_RE = re.compile(r"Repaired (\d+)/(\d+) violations")


def repair_mask_copper(routed_path: Path, *, max_invocations: int = 3) -> int:
    """Slide/re-centre copper out of foreign mask openings (#8900, #9334).

    Board 03's routed copper carries same-net ``mask_to_copper`` errors --
    tracks and vias that pass beside or enter a pad's mask opening
    off-centre (72 on ``origin/main`` @ ``d0eeef579``: 45 beside-pad
    pass-throughs, 27 overlaps, 0 own-pour -- resolved in the checker by
    #9436/#9441).  The verified mask repair merged in #9905 (``kct
    fix-drc --only mask-copper``) re-centres own-pad entries and slides
    pass-throughs clear, keeping only moves whose saved board re-checks
    clean under native DRC with the pad connectivity intact.

    Measured on the committed board (pinned toolchain, 2026-11-22): the
    filled board keeps 27 repairs in one invocation (72 -> 41 errors; the
    command's internal passes converge) and a second invocation confirms
    the residual.  The invocation loop below stops as soon as one
    invocation repairs nothing new or leaves the file bytes unchanged.

    Run it on the routed board AFTER the fiducials (whose pads are new
    mask openings the repair must account for) and AFTER the zone fill:
    the repairer's own native gate compares the saved board before/after
    each move, and on the unfilled board that comparison is
    pour-served-opens noise (62 -> 0 opens on a fresh regen), while the
    filled board is the state the mask findings are measured on.  The
    tool refills the zones itself after saving, so the copper the Step 6
    DRC / LVS gates re-derive stays fill-consistent.  The mask assessment
    needs the qualified KiCad plot profile (the pinned container in CI,
    or ``KCT_KICAD_DOCKER=1``); an unqualified host sees no mask findings
    here and is failed by Step 6 ``run_drc``'s own incomplete verdict, so
    this step never silently substitutes for that gate.  It prints the
    kept/residual counts and returns the number of repairs kept.
    """

    print("\n" + "=" * 60)
    print("Repairing mask-to-copper findings (fix-drc --only mask-copper)...")
    print("=" * 60)

    total_kept = 0
    previous_bytes = routed_path.read_bytes()
    for invocation in range(1, max_invocations + 1):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "fix-drc",
                str(routed_path),
                "--only",
                "mask-copper",
                "--mfr",
                "jlcpcb-tier1",
                "--max-displacement",
                "2.0",
                "--max-passes",
                "3",
            ],
            capture_output=True,
            text=True,
        )
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if "3D coverage" not in line:
                    print(f"   {line}")
        if result.returncode == 3:
            raise RuntimeError(
                "fix-drc mask-copper pass rolled back for connectivity loss "
                f"(exit 3, invocation {invocation})"
            )
        if result.returncode not in (0, 1, 2):
            raise RuntimeError(
                f"fix-drc mask-copper pass failed (exit {result.returncode}, "
                f"invocation {invocation}): {result.stderr[-500:]}"
            )
        match = _FIX_DRC_SUMMARY_RE.search(result.stdout or "")
        if match is None:
            raise RuntimeError(
                "fix-drc mask-copper pass printed no 'Repaired N/M "
                f"violations' summary (invocation {invocation}); cannot tell "
                "progress from convergence"
            )
        repaired, detected = (int(g) for g in match.groups())
        if detected == 0:
            print(f"   invocation {invocation}: no mask findings left; done.")
            break
        current_bytes = routed_path.read_bytes()
        total_kept += repaired
        if repaired == 0:
            # Converged residual: every remaining finding was declined for
            # a structural reason the tool will not revisit (junction at
            # the moved end, would violate clearance, ...).  This is the
            # tracked tool gap, not a transport failure -- Step 6 run_drc
            # stays the gate that fails the recipe while any remain, and
            # an unqualified KiCad already fails there via its incomplete
            # verdict, so this step does not double-gate it.
            print(
                f"   invocation {invocation}: converged -- {detected} "
                "finding(s) remain that the tool declines to move (see the "
                "decline reasons above); stopping."
            )
            break
        print(f"   invocation {invocation}: kept {repaired} of {detected} finding(s)")
        if current_bytes == previous_bytes:
            break
        previous_bytes = current_bytes
    print(f"   mask-copper repairs kept: {total_kept}")
    return total_kept


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: mask_repair_8900.py ROUTED_PCB", file=sys.stderr)
        return 2
    repair_mask_copper(Path(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
