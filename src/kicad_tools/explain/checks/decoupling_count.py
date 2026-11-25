"""Per-pin decoupling capacitor count (issue #10134).

:class:`~kicad_tools.explain.checks.bypass.BypassCapDistanceCheck` judges how
far an existing bypass cap sits from its pin, and
:class:`~kicad_tools.explain.checks.decoupling.MissingDecouplingCapCheck`
fires only when a power *net* has no capacitor anywhere on the board.
Neither counts capacitors **per IC power pin**, so a second or third 100 nF
beside a pin that already has one, or a pin with no capacitor nearby on a rail
that has capacitors elsewhere, is invisible.

:class:`DecouplingCountCheck` closes that gap.  The cap-to-pin association
prefers the **schematic designation**
(:func:`kicad_tools.placement.decoupling.schematic_cap_associations`, issue
#10140) when a schematic sits next to the PCB (``<board>.kicad_sch``) or is
passed as ``schematic_path``: a cap is inventoried against the pin its drawing
(or ``KCT_Decouples`` override) names, at its **PCB distance**, however far that
is.  A cap the schematic does not designate -- or no schematic at all -- falls
back to the **PCB-geometric rule**: the nearest power pad of an IC on the same
net, if that pad is within :data:`DEFAULT_RADIUS_MM`.  When a pin is left with no assigned
capacitor on an otherwise-decoupled rail, the finding distinguishes a nearby
same-rail capacitor that was assigned to another closer (or tie-broken) pin
from a pin whose nearest same-rail capacitor really sits outside
:data:`DEFAULT_RADIUS_MM`.  The gateable ``kct check`` rule is issue #10131.

The full per-pin tally -- including pins that are clean, pins with no capacitor,
and pins on a rail with no decoupling at all -- is available from
:meth:`DecouplingCountCheck.pin_inventory`; :meth:`DecouplingCountCheck.check`
turns only the problem pins into advisory findings.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from ..mistakes import (
    Mistake,
    MistakeCategory,
    distance,
    is_ground_net,
    is_ic_footprint,
    is_power_net,
    power_pin_nets,
)
from .bypass import BYPASS_WARNING_DISTANCE_MM
from .decoupling import _capacitance_farads, _is_decoupling_cap

if TYPE_CHECKING:
    from ...placement.decoupling import SchematicCapAssociation
    from ...schema.pcb import PCB, Footprint, Pad

logger = logging.getLogger(__name__)

#: A capacitor is only associated with a pin within this distance (mm).
DEFAULT_RADIUS_MM = BYPASS_WARNING_DISTANCE_MM

#: More than this many caps of one value on a single pin is reported.
DEFAULT_MAX_SAME_VALUE = 1


@dataclass(frozen=True)
class AssignedCap:
    """A decoupling capacitor associated with an IC power pin."""

    reference: str
    value: str
    distance_mm: float
    #: ``"schematic"`` when the schematic designates this pin, else ``"geometric"``.
    source: str = "geometric"


@dataclass
class PinDecoupling:
    """The decoupling inventory of one (IC, power pad) pair."""

    ic_reference: str
    pad_number: str
    net_name: str
    position: tuple[float, float]
    #: Associated caps, nearest first.
    caps: list[AssignedCap] = field(default_factory=list)
    #: True when any decoupling cap sits on this net anywhere on the board.
    rail_has_caps: bool = False
    #: Distance (mm) to the nearest decoupling cap on the net, whether or not
    #: it is associated with this pin; None when the rail has none.
    nearest_rail_cap_mm: float | None = None
    #: Reference designator and value of the nearest decoupling cap on the net,
    #: and the ``(ic_reference, pad_number)`` it was assigned to (None if
    #: unassigned because no power pad was within radius).
    nearest_rail_cap_ref: str | None = None
    nearest_rail_cap_value: str | None = None
    nearest_rail_cap_assigned_to: tuple[str, str] | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable form of the inventory row."""
        nearest: dict[str, object] | None = None
        if self.nearest_rail_cap_ref is not None and self.nearest_rail_cap_mm is not None:
            nearest = {
                "ref": self.nearest_rail_cap_ref,
                "value": self.nearest_rail_cap_value,
                "distance_mm": round(self.nearest_rail_cap_mm, 2),
                "assigned_to": (
                    _format_owner(self.nearest_rail_cap_assigned_to)
                    if self.nearest_rail_cap_assigned_to is not None
                    else None
                ),
            }
        return {
            "ic": self.ic_reference,
            "pin": self.pad_number,
            "net": self.net_name,
            "location": list(self.position),
            "rail_has_caps": self.rail_has_caps,
            "caps": [
                {
                    "ref": c.reference,
                    "value": c.value,
                    "distance_mm": c.distance_mm,
                    "source": c.source,
                }
                for c in self.caps
            ],
            "nearest_rail_cap": nearest,
        }

    def same_value_groups(self) -> dict[str, list[AssignedCap]]:
        """Caps grouped by value (parsed capacitance, else the raw string)."""
        groups: dict[str, list[AssignedCap]] = {}
        for cap in self.caps:
            farads = _capacitance_farads(cap.value)
            key = f"{farads:.4g}F" if farads is not None else cap.value.strip().lower()
            groups.setdefault(key, []).append(cap)
        return groups


def _format_owner(owner: tuple[str, str] | None) -> str:
    """Format an ``(ic_reference, pad_number)`` owner tuple as ``IC.PAD``."""
    if owner is None:
        return "another pin"
    return f"{owner[0]}.{owner[1]}"


def _format_distance(distance_mm: float, radius_mm: float) -> tuple[str, float]:
    """Format a measured distance so a value just above ``radius_mm`` never rounds onto it."""
    rounded_1 = round(distance_mm, 1)
    if distance_mm > radius_mm and rounded_1 <= radius_mm:
        rounded_2 = round(distance_mm, 2)
        if rounded_2 > radius_mm:
            return f"{rounded_2:.2f}", rounded_2
        rounded_3 = round(distance_mm, 3)
        return f"{rounded_3:.3f}", rounded_3
    return f"{rounded_1:.1f}", rounded_1


def _pad_position(fp: Footprint, pad: Pad) -> tuple[float, float]:
    """Absolute pad position, applying the footprint rotation (KiCad Y-down)."""
    px, py = pad.position
    if fp.rotation:
        theta = math.radians(-fp.rotation)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        px, py = px * cos_t - py * sin_t, px * sin_t + py * cos_t
    return (fp.position[0] + px, fp.position[1] + py)


class DecouplingCountCheck:
    """Flag IC power pins with redundant or no associated decoupling caps.

    Args:
        max_same_value: Report a pin with more than this many capacitors of
            the same value (default 1).
        radius_mm: Association radius from a capacitor to a power pad (the
            geometric fallback only; a schematic-designated cap is inventoried
            at any distance).
        schematic_path: Schematic whose cap designations to use.  Defaults to
            schematic the project resolver finds for the PCB (``project.kct``,
            stage-suffix-stripped sibling), when one exists.
    """

    category = MistakeCategory.DECOUPLING

    def __init__(
        self,
        max_same_value: int = DEFAULT_MAX_SAME_VALUE,
        radius_mm: float = DEFAULT_RADIUS_MM,
        schematic_path: str | Path | None = None,
    ) -> None:
        self.max_same_value = max_same_value
        self.radius_mm = radius_mm
        self.schematic_path = Path(schematic_path) if schematic_path else None
        self._association_cache: tuple[PCB, dict[str, SchematicCapAssociation] | None] | None = None

    # ------------------------------------------------------------------
    # Schematic association
    # ------------------------------------------------------------------

    def _resolve_schematic(self, pcb: PCB) -> Path | None:
        if self.schematic_path is not None:
            return self.schematic_path
        board_path = getattr(pcb, "_path", None)
        if board_path is None:
            return None
        try:
            from ...sync.discover import resolve_schematic_for_pcb

            return resolve_schematic_for_pcb(board_path)
        except Exception as exc:  # discovery must not break detect-mistakes
            logger.debug(
                "decoupling count: schematic discovery failed for %s (%s)", board_path, exc
            )
            return None

    def schematic_associations(self, pcb: PCB) -> dict[str, SchematicCapAssociation] | None:
        """Schematic designations by cap reference, or None when unavailable.

        None (no schematic, or one that cannot be read) means every cap uses
        the geometric rule.
        """
        if self._association_cache is not None and self._association_cache[0] is pcb:
            return self._association_cache[1]
        rows: dict[str, SchematicCapAssociation] | None = None
        schematic = self._resolve_schematic(pcb)
        if schematic is not None:
            try:
                from ...placement.decoupling import schematic_cap_associations

                rows = {r.cap.reference: r for r in schematic_cap_associations(schematic)}
            except Exception as exc:  # unreadable schematic must not break detect-mistakes
                logger.warning(
                    "decoupling count: cannot read schematic %s (%s); using PCB geometry",
                    schematic,
                    exc,
                )
        self._association_cache = (pcb, rows)
        return rows

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    def pin_inventory(
        self,
        pcb: PCB,
        associations: Mapping[str, SchematicCapAssociation] | None = None,
    ) -> list[PinDecoupling]:
        """Return one row per IC power pad, including clean and empty pins.

        Args:
            pcb: The board.
            associations: Schematic designations by cap reference.  Defaults
                to :meth:`schematic_associations`.
        """
        if associations is None:
            associations = self.schematic_associations(pcb)
        evidence = power_pin_nets(pcb)

        pins: list[PinDecoupling] = []
        by_net: dict[str, list[tuple[PinDecoupling, Footprint]]] = {}
        for fp in pcb.footprints:
            if not is_ic_footprint(fp):
                continue
            for pad in fp.pads:
                if not pad.net_name or not is_power_net(pad.net_name, evidence):
                    continue
                pin = PinDecoupling(
                    ic_reference=fp.reference,
                    pad_number=str(pad.number),
                    net_name=pad.net_name,
                    position=_pad_position(fp, pad),
                )
                pins.append(pin)
                by_net.setdefault(pad.net_name, []).append((pin, fp))

        # (cap footprint, power net) for every two-terminal decoupling cap
        caps: list[tuple[Footprint, str]] = []
        for fp in pcb.footprints:
            if not _is_decoupling_cap(fp.reference, fp.value) or len(fp.pads) != 2:
                continue
            pad_nets = {p.net_name for p in fp.pads if p.net_name}
            if len(pad_nets) != 2 or not any(is_ground_net(n) for n in pad_nets):
                continue
            for net in sorted(pad_nets):
                if is_power_net(net, evidence):
                    caps.append((fp, net))
                    break

        rail_caps: dict[str, list[Footprint]] = {}
        for cap, net in caps:
            rail_caps.setdefault(net, []).append(cap)

        # A cap goes to the pin its schematic designates (at its PCB distance),
        # else to its nearest power pad on the same net within radius; ties
        # break on (IC reference, pad number) so the result is deterministic.
        cap_owner: dict[int, tuple[str, str]] = {}
        for cap, net in caps:
            row = associations.get(cap.reference) if associations else None
            if row is not None and row.pin is not None:
                designated = [
                    (distance(cap.position, pin.position), pin)
                    for pin, _ in by_net.get(net, [])
                    if (pin.ic_reference, pin.pad_number) == (row.pin.reference, row.pin.pad)
                ]
                if designated:
                    d, owner = min(designated, key=lambda e: (e[0], e[1].pad_number))
                    owner.caps.append(
                        AssignedCap(cap.reference, cap.value, round(d, 2), "schematic")
                    )
                    cap_owner[id(cap)] = (owner.ic_reference, owner.pad_number)
                    continue
            best: tuple[float, str, str, PinDecoupling] | None = None
            for pin, _ in by_net.get(net, []):
                d = distance(cap.position, pin.position)
                cand = (d, pin.ic_reference, pin.pad_number, pin)
                if best is None or cand[:3] < best[:3]:
                    best = cand
            if best is not None and best[0] <= self.radius_mm:
                owner = best[3]
                owner.caps.append(AssignedCap(cap.reference, cap.value, round(best[0], 2)))
                cap_owner[id(cap)] = (owner.ic_reference, owner.pad_number)

        for net, entries in by_net.items():
            if net not in rail_caps:
                continue
            for pin, _ in entries:
                nearest_d, _, nearest_cap = min(
                    (distance(cap.position, pin.position), cap.reference, cap)
                    for cap in rail_caps[net]
                )
                pin.rail_has_caps = True
                pin.nearest_rail_cap_mm = nearest_d
                pin.nearest_rail_cap_ref = nearest_cap.reference
                pin.nearest_rail_cap_value = nearest_cap.value
                pin.nearest_rail_cap_assigned_to = cap_owner.get(id(nearest_cap))

        for pin in pins:
            pin.caps.sort(key=lambda c: (c.distance_mm, c.reference))
        pins.sort(key=lambda p: (p.ic_reference, p.net_name, p.pad_number))
        return pins

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------

    def check(self, pcb: PCB) -> list[Mistake]:
        """Advisory findings for redundant or missing per-pin decoupling."""
        mistakes: list[Mistake] = []
        inventory = self.pin_inventory(pcb)

        # Uncovered pins on an otherwise-decoupled rail fall into two distinct
        # situations:
        # 1. A same-rail capacitor sits within ``radius_mm`` of the pin, but
        #    the geometric rule assigned it to another closer (or tie-broken)
        #    power pin.  Saying "no decoupling capacitor within 5mm" here
        #    contradicts the measured distance.
        # 2. Every decoupling capacitor on the rail really is outside
        #    ``radius_mm``.
        # Each situation is grouped once per (IC, net) so per-pin findings do
        # not collide on the waiver key; the nearby-cap case includes the
        # nearby capacitor reference(s) in ``components`` so the two shapes
        # stay distinct even when one IC has both on the same rail.  A rail
        # with no decoupling at all is MissingDecouplingCapCheck's.
        unassigned_nearby: dict[tuple[str, str], list[PinDecoupling]] = {}
        none_within_radius: dict[tuple[str, str], list[PinDecoupling]] = {}
        for pin in inventory:
            if not pin.rail_has_caps or any(c.distance_mm <= self.radius_mm for c in pin.caps):
                continue
            if pin.caps:
                # Schematic-designated caps, all farther than the radius.
                none_within_radius.setdefault((pin.ic_reference, pin.net_name), []).append(pin)
            elif pin.nearest_rail_cap_mm is not None and pin.nearest_rail_cap_mm <= self.radius_mm:
                unassigned_nearby.setdefault((pin.ic_reference, pin.net_name), []).append(pin)
            else:
                none_within_radius.setdefault((pin.ic_reference, pin.net_name), []).append(pin)

        for (ic_ref, net), pins in unassigned_nearby.items():
            pin_list = ", ".join(p.pad_number for p in pins)
            noun = "pin" if len(pins) == 1 else "pins"
            verb = "has" if len(pins) == 1 else "have"
            nearest = min(p.nearest_rail_cap_mm for p in pins if p.nearest_rail_cap_mm is not None)
            nearest_str, nearest_val = _format_distance(nearest, self.radius_mm)
            nearby_cap_refs = list(
                dict.fromkeys(p.nearest_rail_cap_ref for p in pins if p.nearest_rail_cap_ref)
            )
            distinct_pairs = list(
                dict.fromkeys(
                    (
                        p.nearest_rail_cap_ref,
                        p.nearest_rail_cap_value,
                        p.nearest_rail_cap_assigned_to,
                    )
                    for p in pins
                )
            )
            if len(distinct_pairs) == 1:
                cap_ref, cap_val, owner = distinct_pairs[0]
                owner_str = _format_owner(owner)
                explanation = (
                    f"{ic_ref} {noun} {pin_list} ({net}) {verb} no separately "
                    f"assigned decoupling capacitor: {cap_ref} ({cap_val}) is "
                    f"{nearest_str}mm away on {net} (within {self.radius_mm:g}mm), "
                    f"but was assigned to {owner_str}. Each supply pin should "
                    "have its own dedicated bypass capacitor unless nearby pins "
                    "intentionally share one."
                )
                cap_article = (
                    "a dedicated decoupling capacitor"
                    if len(pins) == 1
                    else "dedicated decoupling capacitors"
                )
                fix_suggestion = (
                    f"Add {cap_article} (typically 100nF, or per the datasheet) "
                    f"for {ic_ref} {noun} {pin_list} rather than relying solely "
                    f"on {cap_ref} (assigned to {owner_str}), or waive this "
                    f"advisory if sharing {cap_ref} is intentional."
                )
            else:
                per_pin = "; ".join(
                    f"pin {p.pad_number}: {p.nearest_rail_cap_ref} "
                    f"({p.nearest_rail_cap_value}, "
                    f"{_format_distance(p.nearest_rail_cap_mm or 0.0, self.radius_mm)[0]}mm "
                    f"away, assigned to {_format_owner(p.nearest_rail_cap_assigned_to)})"
                    for p in pins
                )
                owners_summary = ", ".join(
                    dict.fromkeys(
                        f"{p.nearest_rail_cap_ref} assigned to "
                        f"{_format_owner(p.nearest_rail_cap_assigned_to)}"
                        for p in pins
                        if p.nearest_rail_cap_ref
                    )
                )
                explanation = (
                    f"{ic_ref} {noun} {pin_list} ({net}) {verb} no separately "
                    f"assigned decoupling capacitor: nearby {net} capacitors "
                    f"within {self.radius_mm:g}mm were assigned to other pins "
                    f"({per_pin}). Each supply pin should have its own dedicated "
                    "bypass capacitor unless nearby pins intentionally share one."
                )
                fix_suggestion = (
                    "Add dedicated decoupling capacitors (typically 100nF, or "
                    f"per the datasheet) for {ic_ref} {noun} {pin_list} rather "
                    f"than relying solely on capacitors assigned to other pins "
                    f"({owners_summary}), or waive this advisory if sharing "
                    "those capacitors is intentional."
                )
            mistakes.append(
                Mistake(
                    category=MistakeCategory.DECOUPLING,
                    severity="warning",
                    title="Power pin has no separately assigned decoupling capacitor",
                    components=[ic_ref, net, *nearby_cap_refs],
                    location=pins[0].position,
                    explanation=explanation,
                    fix_suggestion=fix_suggestion,
                    learn_more_url="docs/mistakes/bypass-cap-placement.md",
                    measurements={
                        "pin_count": float(len(pins)),
                        "nearest_rail_cap_mm": nearest_val,
                    },
                )
            )

        for (ic_ref, net), pins in none_within_radius.items():
            pin_list = ", ".join(p.pad_number for p in pins)
            nearest_pin = min(
                pins,
                key=lambda p: (
                    p.nearest_rail_cap_mm if p.nearest_rail_cap_mm is not None else float("inf"),
                    p.pad_number,
                ),
            )
            nearest = nearest_pin.nearest_rail_cap_mm or 0.0
            nearest_str, nearest_val = _format_distance(nearest, self.radius_mm)
            nearest_cap_note = (
                f"nearest is {nearest_pin.nearest_rail_cap_ref} at {nearest_str}mm away"
                if nearest_pin.nearest_rail_cap_ref
                else f"nearest is {nearest_str}mm away"
            )
            if (
                nearest_pin.nearest_rail_cap_ref
                and nearest_pin.nearest_rail_cap_assigned_to is not None
            ):
                nearest_cap_note += (
                    f", assigned to {_format_owner(nearest_pin.nearest_rail_cap_assigned_to)}"
                )
            noun = "pin" if len(pins) == 1 else "pins"
            mistakes.append(
                Mistake(
                    category=MistakeCategory.DECOUPLING,
                    severity="warning",
                    title="Power pin has no decoupling capacitor nearby",
                    components=[ic_ref, net],
                    location=pins[0].position,
                    explanation=(
                        f"{ic_ref} {noun} {pin_list} ({net}) "
                        f"{'has' if len(pins) == 1 else 'have'} no decoupling "
                        f"capacitor within {self.radius_mm:g}mm, although other "
                        f"capacitors decouple {net} ({nearest_cap_note}). Each "
                        "supply pin should have its own local bypass capacitor."
                    ),
                    fix_suggestion=(
                        f"Place a decoupling capacitor (typically 100nF, or per "
                        f"the datasheet) within {self.radius_mm:g}mm of "
                        f"{ic_ref} {noun} {pin_list}."
                    ),
                    learn_more_url="docs/mistakes/bypass-cap-placement.md",
                    measurements={
                        "pin_count": float(len(pins)),
                        "nearest_rail_cap_mm": nearest_val,
                    },
                )
            )

        for pin in inventory:
            where = f"{pin.ic_reference} pin {pin.pad_number} ({pin.net_name})"
            for caps in pin.same_value_groups().values():
                if len(caps) <= self.max_same_value:
                    continue
                listing = ", ".join(
                    f"{c.reference} ({c.value}, {c.distance_mm:.1f}mm)" for c in caps
                )
                mistakes.append(
                    Mistake(
                        category=MistakeCategory.DECOUPLING,
                        severity="info",
                        title="Redundant decoupling capacitors on one power pin",
                        components=[pin.ic_reference, pin.net_name, *(c.reference for c in caps)],
                        location=pin.position,
                        explanation=(
                            f"{where} has {len(caps)} capacitors of the same value "
                            f"nearby: {listing}. More than {self.max_same_value} "
                            "of one value on a single pin adds cost and board "
                            "area without improving decoupling."
                        ),
                        fix_suggestion=(
                            f"Keep one {caps[0].value} capacitor on this pin; "
                            "remove the extras or change one to a different value "
                            "(for example a 10uF bulk part) if the datasheet "
                            "asks for it."
                        ),
                        learn_more_url="docs/mistakes/bypass-cap-placement.md",
                        measurements={
                            "cap_count": float(len(caps)),
                            "nearest_mm": round(min(c.distance_mm for c in caps), 1),
                        },
                    )
                )
        return mistakes
