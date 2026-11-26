"""Bypass-capacitor distance rule driven by the schematic's cap-to-pin association.

A decoupling capacitor only helps the supply pin it was drawn next to.  The
old ``detect-mistakes`` pairing took *the nearest power pad of any IC on the
rail*, so on a shared rail it paired a cap with a neighbouring IC and
reported a swapped placement as fine -- or a correct one as too far (20
findings on one reporter's board).  This rule measures the PCB distance from
each cap's supply pad to the pad of **the supply pin its schematic
designates**, taken from
:func:`kicad_tools.placement.decoupling.schematic_cap_associations`.  No
pairing happens here: the association is the only source of truth, and
:class:`~kicad_tools.explain.checks.bypass.BypassCapDistanceCheck` consumes
the same :func:`measure_bypass_pairs` helper.

Rule IDs generated:

* ``bypass_distance`` -- a cap's supply pad is farther than the configured
  maximum from its associated pin's pad.  Warning severity.
* ``bypass_distance_unassociated`` -- the schematic gives no defensible
  target for a cap (``unassociated``), or the cap/pin is not on the board so
  the distance cannot be measured.  Info severity: it says the distance was
  *not* checked, which is not a distance failure and not a pass.

Both ids are advisory (see ``DRCChecker.RULE_CATEGORY``); ``--strict`` blocks
on the warning.  Without a schematic the rule does not run and ``kct check``
records ``bypass_distance`` as ``unknown (needs_input:schematic)`` rather than
reporting a clean pass.  Individual findings are waivable via
``.kct_waivers.json`` with ``items=[<cap ref>, <ic ref>]``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kicad_tools.schema.physical_identity import physical_pad_position

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.placement.decoupling import SchematicCapAssociation
    from kicad_tools.schema.pcb import PCB, Footprint

BYPASS_DISTANCE_RULE_ID = "bypass_distance"
BYPASS_UNASSOCIATED_RULE_ID = "bypass_distance_unassociated"

#: Default maximum cap-pad-to-pin-pad distance (mm).  Matches the 3 mm
#: ``detect-mistakes`` has always recommended for bypass placement.
DEFAULT_MAX_DISTANCE_MM = 3.0

PairStatus = Literal["measured", "unassociated", "unplaced"]


@dataclass(frozen=True)
class BypassPairing:
    """One schematic cap association resolved against the board.

    Attributes:
        association: The schematic association this row was built from.
        status: ``measured`` (both pads found), ``unassociated`` (the schematic
            names no target pin) or ``unplaced`` (the cap, the pin's part, or
            the pad is absent from the PCB).
        distance_mm: Cap supply pad to pin pad distance; ``None`` unless
            ``status == "measured"``.
        cap_position: Board-frame position of the cap's supply pad, if found.
        opposite_side: ``True`` when the cap and the pin's part sit on
            different board sides (the XY distance is still what is measured).
        detail: Human-readable reason for a non-measured row.
    """

    association: SchematicCapAssociation
    status: PairStatus
    distance_mm: float | None = None
    cap_position: tuple[float, float] | None = None
    opposite_side: bool = False
    detail: str = ""

    @property
    def cap(self) -> str:
        return self.association.cap.reference

    @property
    def pin_reference(self) -> str | None:
        pin = self.association.pin
        return pin.reference if pin else None

    @property
    def pin_pad(self) -> str | None:
        pin = self.association.pin
        return pin.pad if pin else None


def _copper_land_centers(footprint: Footprint, number: str) -> list[tuple[float, float]]:
    return [
        physical_pad_position(footprint, pad)
        for pad in footprint.pads
        if pad.number == number and any(layer.endswith(".Cu") for layer in pad.layers)
    ]


def resolve_pairings(
    pcb: PCB, associations: Sequence[SchematicCapAssociation]
) -> list[BypassPairing]:
    """Measure each association on ``pcb``.  This is the only pairing consumer.

    Distances are between pad centres, in the board frame, from the cap's
    supply pad (``association.cap.supply_pad``) to the associated pin's pad.
    For a repeated pad number, use the nearest pair of physical copper lands.
    """
    rows: list[BypassPairing] = []
    for assoc in associations:
        cap_ref = assoc.cap.reference
        if assoc.pin is None:
            rows.append(BypassPairing(assoc, "unassociated", detail=assoc.reason))
            continue
        cap_fp = pcb.get_footprint(cap_ref)
        pin_fp = pcb.get_footprint(assoc.pin.reference)
        if cap_fp is None or pin_fp is None:
            missing = cap_ref if cap_fp is None else assoc.pin.reference
            rows.append(BypassPairing(assoc, "unplaced", detail=f"{missing} is not on the PCB"))
            continue
        cap_lands = _copper_land_centers(cap_fp, assoc.cap.supply_pad)
        pin_lands = _copper_land_centers(pin_fp, assoc.pin.pad)
        if not cap_lands or not pin_lands:
            missing_pad = (
                f"{cap_ref} pad {assoc.cap.supply_pad}"
                if not cap_lands
                else f"{assoc.pin.reference} pad {assoc.pin.pad}"
            )
            rows.append(BypassPairing(assoc, "unplaced", detail=f"{missing_pad} is not on the PCB"))
            continue
        distance, cap_pos = min(
            (math.dist(cap, pin), cap) for cap in cap_lands for pin in pin_lands
        )
        rows.append(
            BypassPairing(
                assoc,
                "measured",
                distance_mm=distance,
                cap_position=cap_pos,
                opposite_side=cap_fp.layer != pin_fp.layer,
            )
        )
    return rows


def measure_bypass_pairs(pcb: PCB, schematic_path: str | Path) -> list[BypassPairing]:
    """Associate caps from ``schematic_path`` and measure them on ``pcb``."""
    from kicad_tools.placement.decoupling import schematic_cap_associations

    return resolve_pairings(pcb, schematic_cap_associations(schematic_path))


class BypassDistanceRule(DRCRule):
    """Flag bypass caps farther than ``max_distance_mm`` from their designated pin."""

    rule_id = BYPASS_DISTANCE_RULE_ID
    name = "Bypass Capacitor Distance"
    description = (
        "Measures each decoupling capacitor's distance to the supply pin its "
        "schematic designates (not the nearest pin on the rail)"
    )

    def __init__(
        self,
        schematic_path: str | Path | None,
        *,
        max_distance_mm: float = DEFAULT_MAX_DISTANCE_MM,
        severity: str = "warning",
    ) -> None:
        """Configure the rule.

        Args:
            schematic_path: The board's schematic.  ``None`` means the rule
                cannot run: :meth:`check` returns no findings and the caller
                (``kct check`` coverage) must report it as not run.
            max_distance_mm: Largest allowed cap-pad-to-pin-pad distance.
            severity: Severity of over-distance findings (``error``,
                ``warning`` or ``info``).  Unassociated findings are always
                ``info``.
        """
        if severity not in ("error", "warning", "info"):
            raise ValueError(f"invalid severity {severity!r}")
        if max_distance_mm <= 0:
            raise ValueError(f"max_distance_mm must be positive, got {max_distance_mm!r}")
        self.schematic_path = Path(schematic_path) if schematic_path is not None else None
        self.max_distance_mm = max_distance_mm
        self.severity = severity

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Return one finding per over-distance or unmeasurable cap."""
        del design_rules
        results = DRCResults()
        if self.schematic_path is None:
            return results
        results.rules_checked = 1
        try:
            rows = measure_bypass_pairs(pcb, self.schematic_path)
        except Exception as exc:  # noqa: BLE001 - an unreadable schematic must be visible
            # A schematic that cannot be associated is "not checked", never a
            # pass and never a crash of the whole ``kct check`` run.
            results.add(
                DRCViolation(
                    rule_id=BYPASS_UNASSOCIATED_RULE_ID,
                    severity="info",
                    message=(
                        f"bypass-cap distances were NOT checked: the schematic "
                        f"cap association for {self.schematic_path.name} failed "
                        f"({type(exc).__name__}: {exc})"
                    ),
                )
            )
            return results
        for row in rows:
            if row.status != "measured":
                results.add(self._unmeasured_violation(pcb, row))
            elif row.distance_mm is not None and row.distance_mm > self.max_distance_mm:
                results.add(self._distance_violation(pcb, row))
        return results

    def _net(self, pcb: PCB, row: BypassPairing) -> tuple[str, ...]:
        fp = pcb.get_footprint(row.cap)
        if fp is not None:
            for pad in fp.pads:
                if pad.number == row.association.cap.supply_pad and pad.net_name:
                    return (pad.net_name,)
        return ()

    def _distance_violation(self, pcb: PCB, row: BypassPairing) -> DRCViolation:
        assert row.distance_mm is not None and row.pin_reference is not None
        side = " (on the opposite board side)" if row.opposite_side else ""
        pos = row.cap_position
        return DRCViolation(
            rule_id=BYPASS_DISTANCE_RULE_ID,
            severity=self.severity,
            message=(
                f"{row.cap} is {row.distance_mm:.2f}mm from {row.pin_reference} pin "
                f"{row.pin_pad}{side}, the supply pin its schematic designates "
                f"({row.association.source}: {row.association.reason}); maximum "
                f"{self.max_distance_mm:g}mm -- move {row.cap} next to that pin, fix the "
                f"drawing if the association is wrong, or waive via .kct_waivers.json"
            ),
            location=(round(pos[0], 3), round(pos[1], 3)) if pos else None,
            actual_value=round(row.distance_mm, 4),
            required_value=self.max_distance_mm,
            items=(row.cap, row.pin_reference),
            nets=self._net(pcb, row),
        )

    def _unmeasured_violation(self, pcb: PCB, row: BypassPairing) -> DRCViolation:
        if row.status == "unassociated":
            what = "has no supply pin its schematic designates"
        else:
            what = "could not be measured"
        pos = pcb.get_pad_position(row.cap, row.association.cap.supply_pad)
        return DRCViolation(
            rule_id=BYPASS_UNASSOCIATED_RULE_ID,
            severity="info",
            message=(
                f"{row.cap} {what} ({row.detail}); its distance was NOT checked -- "
                f"draw it beside its supply pin, or name one with the KCT_Decouples "
                f"symbol field"
            ),
            location=(round(pos[0], 3), round(pos[1], 3)) if pos else None,
            items=(row.cap,),
            nets=self._net(pcb, row),
        )
