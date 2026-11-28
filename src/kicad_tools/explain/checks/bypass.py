"""Bypass capacitor placement checks.

Bypass capacitors (decoupling capacitors) should be placed as close as
possible to the IC power pins they are decoupling, with short, wide traces.

Which pin a cap decouples comes from the schematic's cap-to-pin association
(:func:`kicad_tools.placement.decoupling.schematic_cap_associations`), shared
with the ``bypass_distance`` rule of ``kct check``
(:mod:`kicad_tools.validate.rules.bypass_distance`).  This check does no
pairing of its own: pairing a cap with the nearest power pad on its rail
mis-pairs on shared rails (issue #10141).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..mistakes import CheckIncomplete, Mistake, MistakeCategory

if TYPE_CHECKING:
    from ...schema.pcb import PCB


# Maximum recommended distance from bypass cap to power pin (mm)
MAX_BYPASS_DISTANCE_MM = 3.0

# Warning threshold (mm) - still functional but not optimal
BYPASS_WARNING_DISTANCE_MM = 5.0


class BypassCapDistanceCheck:
    """Check that bypass capacitors are close to the pin they decouple.

    Bypass capacitors should be within 3mm of the power pin they are
    decoupling. At greater distances, trace inductance reduces filtering
    effectiveness, especially at high frequencies.

    The target pin is the one the schematic designates for each cap.  Without
    a schematic the check cannot run and raises
    :class:`~kicad_tools.explain.mistakes.CheckIncomplete`; caps the schematic
    gives no defensible target are left to the ``bypass_distance_unassociated``
    finding of ``kct check``.

    Args:
        schematic_path: Explicit schematic.  By default the schematic is
            resolved next to the PCB like ``kct check`` does.
    """

    category = MistakeCategory.BYPASS_CAP

    def __init__(self, schematic_path: str | Path | None = None) -> None:
        self.schematic_path = Path(schematic_path) if schematic_path is not None else None

    def _schematic_for(self, pcb: PCB) -> Path:
        if self.schematic_path is not None:
            if not self.schematic_path.exists():
                raise CheckIncomplete(f"schematic not found: {self.schematic_path}")
            return self.schematic_path
        from kicad_tools.schema.pcb import source_path_of
        from kicad_tools.sync.discover import resolve_schematic_for_pcb

        source = source_path_of(pcb)
        if source is None:
            raise CheckIncomplete(
                "needs a schematic to know which pin each bypass cap decouples; "
                "the PCB was not loaded from a file, so none can be resolved"
            )
        resolved = resolve_schematic_for_pcb(source)
        if resolved is None:
            raise CheckIncomplete(
                "no schematic found next to the PCB; bypass-cap distances need the "
                "schematic's cap-to-pin association"
            )
        return resolved

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check bypass capacitor placement.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for poorly placed bypass caps

        Raises:
            CheckIncomplete: If no schematic is available to associate caps
                with their supply pins.
        """
        from kicad_tools.validate.rules.bypass_distance import measure_bypass_pairs

        mistakes: list[Mistake] = []
        for row in measure_bypass_pairs(pcb, self._schematic_for(pcb)):
            if row.status != "measured" or row.distance_mm is None:
                continue
            if row.pin_reference is None or row.pin_pad is None:
                continue  # a measured row always names its pin
            dist = row.distance_mm
            ic = row.pin_reference
            pad = row.pin_pad
            cap = row.cap
            net = row.association.net
            # Report where the cap is (its footprint origin), as always.
            cap_fp = pcb.get_footprint(cap)
            assert cap_fp is not None  # a measured row has both footprints
            if dist <= MAX_BYPASS_DISTANCE_MM:
                continue
            if dist > BYPASS_WARNING_DISTANCE_MM:
                mistakes.append(
                    Mistake(
                        category=MistakeCategory.BYPASS_CAP,
                        severity="warning",
                        title="Bypass capacitor too far from power pin",
                        components=[cap, ic],
                        location=cap_fp.position,
                        explanation=(
                            f"{cap} is {dist:.1f}mm from {ic} "
                            f"pin {pad} ({net}). Bypass capacitors "
                            f"should be within {MAX_BYPASS_DISTANCE_MM}mm of the power "
                            f"pin they're decoupling. At {dist:.1f}mm, the inductance "
                            f"of the trace reduces filtering effectiveness."
                        ),
                        fix_suggestion=(
                            f"Move {cap} to within {MAX_BYPASS_DISTANCE_MM}mm "
                            f"of {ic} pin {pad}, with a short, wide "
                            f"trace to both {net} and GND."
                        ),
                        learn_more_url="docs/mistakes/bypass-cap-placement.md",
                        measurements={"distance_mm": round(dist, 1)},
                    )
                )
            else:
                mistakes.append(
                    Mistake(
                        category=MistakeCategory.BYPASS_CAP,
                        severity="info",
                        title="Bypass capacitor placement could be improved",
                        components=[cap, ic],
                        location=cap_fp.position,
                        explanation=(
                            f"{cap} is {dist:.1f}mm from {ic} "
                            f"pin {pad}. Ideally, bypass capacitors should be "
                            f"within {MAX_BYPASS_DISTANCE_MM}mm for optimal decoupling."
                        ),
                        fix_suggestion=(
                            f"Consider moving {cap} closer to {ic} pin {pad} if space permits."
                        ),
                        learn_more_url="docs/mistakes/bypass-cap-placement.md",
                        measurements={"distance_mm": round(dist, 1)},
                    )
                )

        return mistakes
