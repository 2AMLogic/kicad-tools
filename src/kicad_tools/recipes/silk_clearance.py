"""Clear silk graphics and reference designators from component drill holes.

The JLCPCB profiles grade ink over plated and non-plated component drills
as errors. Via-only ink may remain. Unclippable graphics, references with
no legal position and free legends that still cross a component hole are
reported to the recipe caller.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class SilkClearanceError(RuntimeError):
    """A silk-over-hole finding survived the clearance pass."""


def clear_silk_over_holes(
    pcb_path: str | Path,
    *,
    mask_clearance_mm: float = 0.05,
    log: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Clear ``silk_hole_overlap`` findings on component drills.

    Clips silk graphics off drilled holes, then moves the reference
    designators the rule still flags.  ``pcb_path`` is re-saved in place
    only when a half actually changed something.

    Args:
        pcb_path: Routed ``.kicad_pcb`` to clear; mutated and re-saved
            in place.
        mask_clearance_mm: Solder-mask margin used to size pad/via
            apertures while planning reference moves (matches
            ``DesignRules.min_solder_mask_clearance_mm``; the placer's
            own default).
        log: Optional sink for per-half progress lines (recipes pass
            ``print``).

    Returns:
        ``(graphics_clipped, references_moved)``.

    Raises:
        SilkClearanceError: the rule still reports a ``silk_hole_overlap``
            finding after both halves ran -- an unclippable graphic, an
            unplaceable reference, or silk neither half moves (free text).
    """
    from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer
    from kicad_tools.silkscreen.place_refs import SilkRefPlacer, references_over_holes
    from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap

    def _say(message: str) -> None:
        if log is not None:
            log(message)

    path = Path(pcb_path)

    repairer = SilkscreenRepairer(path)
    hole_result = repairer.repair_hole_clearance(include_vias=False)
    if hole_result.total_fixed:
        repairer.save(path)
    _say(
        f"   silk hole clip: {hole_result.total_fixed} graphic(s) clipped, "
        f"{len(hole_result.skipped)} skipped (arc/circle)"
    )

    placer = SilkRefPlacer(path)
    over = references_over_holes(placer.pcb, include_vias=False)
    moved = 0
    if over:
        plan = placer.plan(
            only=over, mask_clearance_mm=mask_clearance_mm, include_via_obstacles=False
        )
        moved = placer.apply(plan)
        if moved:
            placer.save(path)
    _say(f"   silk ref placement: {moved} reference(s) moved off holes")

    # The placer edits the S-expression tree, so check the saved drill geometry.
    findings = check_silk_hole_overlap(SilkRefPlacer(path).pcb, include_vias=False).violations
    survivors = sorted(" / ".join(v.items) for v in findings)
    if survivors:
        raise SilkClearanceError(
            f"silk still overlaps drilled hole(s) after the clearance pass: {', '.join(survivors)}"
        )
    return hole_result.total_fixed, moved
