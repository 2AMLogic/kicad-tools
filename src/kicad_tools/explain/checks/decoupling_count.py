"""Per-pin decoupling capacitor count (issue #10134).

:class:`~kicad_tools.explain.checks.bypass.BypassCapDistanceCheck` judges how
far an existing bypass cap sits from its pin, and
:class:`~kicad_tools.explain.checks.decoupling.MissingDecouplingCapCheck`
fires only when a power *net* has no capacitor anywhere on the board.
Neither counts capacitors **per IC power pin**, so a second or third 100 nF
beside a pin that already has one, or a pin with no capacitor nearby on a rail
that has capacitors elsewhere, is invisible.

:class:`DecouplingCountCheck` closes that gap.  The cap-to-pin association is
**PCB-geometric and local to this check**: each decoupling capacitor on a power
net is assigned to the nearest power pad of an IC on the same net, if that pad
is within :data:`DEFAULT_RADIUS_MM`.  Schematic-based association (issue
#10131 / #10140) is deliberately not built here; when it lands it replaces
:meth:`DecouplingCountCheck.pin_inventory`'s assignment step.

The full per-pin tally -- including pins that are clean, pins with no capacitor,
and pins on a rail with no decoupling at all -- is available from
:meth:`DecouplingCountCheck.pin_inventory`; :meth:`DecouplingCountCheck.check`
turns only the problem pins into advisory findings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
    from ...schema.pcb import PCB, Footprint, Pad

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

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable form of the inventory row."""
        return {
            "ic": self.ic_reference,
            "pin": self.pad_number,
            "net": self.net_name,
            "location": list(self.position),
            "rail_has_caps": self.rail_has_caps,
            "caps": [
                {"ref": c.reference, "value": c.value, "distance_mm": c.distance_mm}
                for c in self.caps
            ],
        }

    def same_value_groups(self) -> dict[str, list[AssignedCap]]:
        """Caps grouped by value (parsed capacitance, else the raw string)."""
        groups: dict[str, list[AssignedCap]] = {}
        for cap in self.caps:
            farads = _capacitance_farads(cap.value)
            key = f"{farads:.4g}F" if farads is not None else cap.value.strip().lower()
            groups.setdefault(key, []).append(cap)
        return groups


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
        radius_mm: Association radius from a capacitor to a power pad.
    """

    category = MistakeCategory.DECOUPLING

    def __init__(
        self,
        max_same_value: int = DEFAULT_MAX_SAME_VALUE,
        radius_mm: float = DEFAULT_RADIUS_MM,
    ) -> None:
        self.max_same_value = max_same_value
        self.radius_mm = radius_mm

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    def pin_inventory(self, pcb: PCB) -> list[PinDecoupling]:
        """Return one row per IC power pad, including clean and empty pins."""
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

        for net, entries in by_net.items():
            for pin, _ in entries:
                if net in rail_caps:
                    pin.rail_has_caps = True
                    pin.nearest_rail_cap_mm = min(
                        distance(cap.position, pin.position) for cap in rail_caps[net]
                    )

        # Assign each cap to its nearest power pad on the same net; ties break
        # on (IC reference, pad number) so the result is deterministic.
        for cap, net in caps:
            best: tuple[float, str, str, PinDecoupling] | None = None
            for pin, _ in by_net.get(net, []):
                d = distance(cap.position, pin.position)
                cand = (d, pin.ic_reference, pin.pad_number, pin)
                if best is None or cand[:3] < best[:3]:
                    best = cand
            if best is not None and best[0] <= self.radius_mm:
                best[3].caps.append(AssignedCap(cap.reference, cap.value, round(best[0], 2)))

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

        # One "none nearby" finding per (IC, net) listing every uncovered pin:
        # the waiver key is built from the components and nets, so per-pin
        # findings on one IC and rail would share a key.  A rail with no
        # decoupling at all is MissingDecouplingCapCheck's, not reported here.
        uncovered: dict[tuple[str, str], list[PinDecoupling]] = {}
        for pin in inventory:
            if not pin.caps and pin.rail_has_caps:
                uncovered.setdefault((pin.ic_reference, pin.net_name), []).append(pin)
        for (ic_ref, net), pins in uncovered.items():
            pin_list = ", ".join(p.pad_number for p in pins)
            nearest = min(p.nearest_rail_cap_mm or 0.0 for p in pins)
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
                        f"capacitors decouple {net} (nearest is {nearest:.1f}mm "
                        "away). Each supply pin should have its own local bypass "
                        "capacitor."
                    ),
                    fix_suggestion=(
                        f"Place a decoupling capacitor (typically 100nF, or per "
                        f"the datasheet) within {self.radius_mm:g}mm of "
                        f"{ic_ref} {noun} {pin_list}, or confirm the {noun} "
                        "intentionally share a nearby pin's capacitor."
                    ),
                    learn_more_url="docs/mistakes/bypass-cap-placement.md",
                    measurements={
                        "pin_count": float(len(pins)),
                        "nearest_rail_cap_mm": round(nearest, 1),
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
                            "nearest_mm": min(c.distance_mm for c in caps),
                        },
                    )
                )
        return mistakes
