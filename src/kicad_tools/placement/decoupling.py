"""Decoupling-capacitor affinity for the placement optimizer (issue #6020).

A decoupling capacitor only works when it sits next to the supply pin it
serves: the loop from the pin, through the cap, to ground must be short. The
placement objective in :mod:`kicad_tools.placement.cost` had no term for that.
Wirelength (half-perimeter of each net's bounding box) is blind to it, because
one cap anywhere inside the supply net's bounding box adds nothing. Board 04
showed the result: STM32 VDD pins 6-14 mm from their nearest cap.

This module supplies the term in three steps:

1. :func:`identify_decoupling_groups` finds the decoupling structure in the
   netlist. A *decoupling cap* is a capacitor (``C<n>``) with one pad on a
   supply net and the other on ground. A *supply pin* is an IC (``U<n>``) pad
   on the same supply net. Net names are classified by the shared
   :func:`~kicad_tools.router.net_class.is_power_rail_name` /
   :func:`~kicad_tools.router.net_class.is_ground_rail_name`, the same
   classifiers the ``decoupling_proximity`` FOM term uses after #5939. The
   reference predicates are that term's own helpers. When the PCB carries
   schematic pin types (``(pintype "power_in")``, #5985), they are used as
   extra evidence: a net with a ``power_in``/``power_out`` IC pin counts as a
   supply, and an IC pin on a supply net that is typed as something else (an
   ``EN`` tied high) is not treated as a supply pin.
2. :func:`assign_caps_to_pins` pairs each cap with a supply pin on its net. The
   pairing is greedy by distance and spreads the caps: every pin gets at most
   one cap before any pin gets a second. Four caps around a four-VDD MCU land
   one per VDD pin instead of crowding the nearest one.
3. :func:`compute_decoupling_distance` sums the cap-pad-to-pin distance over
   the pairs. That sum is the optimizer cost term. It is soft: it shapes a
   placement but never makes one infeasible.

The pairing is recomputed for every candidate placement, so it follows the IC
when the optimizer moves or rotates it.
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter, defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Mapping, Sequence

if TYPE_CHECKING:
    from kicad_tools.schema import Schematic

    from .cost import ComponentPlacement, Net, PlacementCostConfig, PlacementScore
    from .vector import PlacementVector

__all__ = [
    "DecouplingCap",
    "DecouplingGroup",
    "SchematicCapAssociation",
    "SnapMove",
    "SupplyPin",
    "apply_cap_associations",
    "assign_caps_to_pins",
    "compute_decoupling_distance",
    "compute_pin_coverage",
    "decoupling_pairs",
    "identify_decoupling_groups",
    "parse_capacitance_farads",
    "schematic_cap_associations",
    "snap_decoupling_caps",
]

PadKey = tuple[str, str]
"""``(reference, pad_number)``."""


@dataclass(frozen=True)
class DecouplingCap:
    """A capacitor between a supply net and ground.

    Attributes:
        reference: Capacitor reference designator (``C12``).
        supply_pad: The cap's pad on the supply net. Distances are measured
            from this pad.
        ground_pad: The cap's pad on the ground net.
    """

    reference: str
    supply_pad: str
    ground_pad: str


@dataclass(frozen=True)
class SupplyPin:
    """An IC pad on a supply net."""

    reference: str
    pad: str


@dataclass(frozen=True)
class SchematicCapAssociation:
    """One drawn decoupling cap, including caps without a defensible target."""

    cap: DecouplingCap
    net: str
    sheet: str
    pin: SupplyPin | None
    source: Literal["derived", "overridden", "unassociated"]
    reason: str


# A labelled cap elsewhere on the page is not a local bypass designation.
_SCHEMATIC_PROXIMITY_MM = 25.4
_TARGET_FIELD = "KCT_Decouples"


@dataclass(frozen=True)
class _DrawnPin:
    sheet: str
    position: tuple[float, float]
    wire: tuple[int, int] | None


def _drawn_wire_groups(schematic: Schematic) -> dict[tuple[int, int], tuple[int, int]]:
    """Physical wire groups on one drawing, before joining equal label names."""
    from kicad_tools.cli.sch_connectivity import build_wire_graph

    edges, _ = build_wire_graph(schematic)
    groups = {}
    for start, adjacent in edges.items():
        if not adjacent or start in groups:
            continue
        groups[start] = start
        pending = [start]
        while pending:
            for node in edges[pending.pop()]:
                if node not in groups:
                    groups[node] = start
                    pending.append(node)
    return groups


def _associate_drawn_cap(
    cap: DecouplingCap,
    net: str,
    site: _DrawnPin,
    targets: dict[SupplyPin, _DrawnPin],
    override: str,
) -> SchematicCapAssociation:
    if override:
        ref, _, pad = override.rpartition(".")
        target = SupplyPin(ref, pad)
        if target in targets:
            return SchematicCapAssociation(
                cap, net, site.sheet, target, "overridden", _TARGET_FIELD
            )
        return SchematicCapAssociation(
            cap,
            net,
            site.sheet,
            None,
            "unassociated",
            f"{_TARGET_FIELD}={override} does not name a supply pin on {net}",
        )
    ranked = []
    for pin, other in targets.items():
        if other.sheet != site.sheet:
            continue
        distance = math.dist(site.position, other.position)
        if distance <= _SCHEMATIC_PROXIMITY_MM:
            wired = site.wire is not None and site.wire == other.wire
            ranked.append((not wired, distance, pin))
    ranked.sort(key=lambda entry: entry[:2])
    if not ranked:
        reason = "No nearby same-sheet supply pin on this net"
    elif (
        len(ranked) > 1
        and ranked[0][0] == ranked[1][0]
        and math.isclose(ranked[0][1], ranked[1][1], abs_tol=0.001)
    ):
        reason = "Equally close supply pins on this net"
    else:
        label_only, distance, selected = ranked[0]
        evidence = "matching net" if label_only else "shared drawn wire"
        return SchematicCapAssociation(
            cap, net, site.sheet, selected, "derived", f"{evidence}, {distance:.2f} mm on sheet"
        )
    return SchematicCapAssociation(cap, net, site.sheet, None, "unassociated", reason)


def _drawn_decoupling_cap(
    ref: str, pins: dict[str, tuple[str, _DrawnPin]]
) -> tuple[DecouplingCap, str, _DrawnPin] | None:
    from kicad_tools.router.net_class import is_ground_rail_name

    if len(pins) != 2:
        return None
    first, second = pins
    first_ground = is_ground_rail_name(pins[first][0])
    if first_ground == is_ground_rail_name(pins[second][0]):
        return None
    supply, ground = (second, first) if first_ground else (first, second)
    net, site = pins[supply]
    return DecouplingCap(ref, supply, ground), net, site


def schematic_cap_associations(path: str | Path) -> list[SchematicCapAssociation]:
    """Associate each supply-to-ground cap with the power pin its schematic designates.

    Pin and net identities are hierarchy-aware. The drawn inference stays on
    the same sheet, while an explicit cap symbol property can name any actual
    same-net supply pin in the design. This function never reads PCB positions.
    """
    from kicad_tools.cli.sch_connectivity import snap_coord, to_coord
    from kicad_tools.lvs.board_lvs import _schematic_pin_to_net
    from kicad_tools.router.net_class import (
        is_ground_rail_name,
        is_power_rail_name,
        is_power_rail_pin,
    )
    from kicad_tools.schema import Schematic
    from kicad_tools.schema.hierarchy import build_hierarchy

    nets = _schematic_pin_to_net(Path(path))
    sheets = build_hierarchy(str(path)).all_nodes()
    multiplicity = Counter(node.path for node in sheets)
    targets: dict[str, dict[SupplyPin, _DrawnPin]] = defaultdict(dict)
    caps = []
    for node in sheets:
        schematic = Schematic.load(node.path)
        sheet = node.get_path_string()
        wire_groups = _drawn_wire_groups(schematic)
        wire_nodes = set(wire_groups)
        for symbol in schematic.symbols:
            ref = symbol.reference
            if multiplicity[node.path] > 1:
                ref = symbol.instance_reference(node.instance_path) or ref
            lib = schematic.get_lib_symbol_resolved(symbol.lib_id)
            if lib is None:
                continue
            prefix = lib.properties.get("Reference", "")
            is_cap = _is_capacitor(ref) or prefix == "C"
            if not (
                is_cap
                or _is_ic(ref)
                or prefix in ("U", "IC")
                or symbol.lib_id.startswith("Oscillator:")
            ):
                continue
            unit_pins = lib.get_pins_for_unit(symbol.unit)
            if is_cap and len({pin.number for pin in unit_pins}) != 2:
                continue
            positions = lib.get_all_pin_positions(
                instance_pos=symbol.position,
                instance_rot=symbol.rotation,
                mirror=symbol.mirror,
                unit=symbol.unit,
            )
            cap_pins = {}
            for pin in unit_pins:
                net = nets.get((ref, pin.number))
                if not net or pin.number not in positions:
                    continue
                pos = positions[pin.number]
                wire = wire_groups.get(snap_coord(to_coord(*pos), wire_nodes))
                site = _DrawnPin(sheet, pos, wire)
                if is_cap:
                    cap_pins[pin.number] = (net, site)
                elif is_power_rail_pin(pin.type, pin.name) and not is_ground_rail_name(net):
                    targets[net][SupplyPin(ref, pin.number)] = site
            if is_cap and (cap := _drawn_decoupling_cap(ref, cap_pins)):
                caps.append((*cap, (symbol.get_property(_TARGET_FIELD) or "").strip()))
    return [
        _associate_drawn_cap(cap, net, site, targets.get(net, {}), override)
        for cap, net, site, override in sorted(
            caps, key=lambda entry: (entry[2].sheet, entry[0].reference)
        )
        if net in targets or is_power_rail_name(net) or override
    ]


@dataclass(frozen=True)
class DecouplingGroup:
    """The decoupling caps and IC supply pins that share one supply net."""

    net: str
    caps: tuple[DecouplingCap, ...]
    pins: tuple[SupplyPin, ...]
    associated: tuple[tuple[str, SupplyPin], ...] = ()
    """``(cap reference, pin)`` pairs the schematic designates (issue #10142).

    :func:`assign_caps_to_pins` gives these caps their designated pin directly
    and runs its distance-greedy pairing only on the rest. Empty: the pairing
    is purely by distance, as before.
    """


def _is_capacitor(reference: str) -> bool:
    from kicad_tools.optim.fom_electrical import _looks_like_capacitor

    return _looks_like_capacitor(reference)


def _is_ic(reference: str) -> bool:
    from kicad_tools.optim.fom_electrical import _looks_like_ic

    return _looks_like_ic(reference)


def identify_decoupling_groups(
    nets: Sequence[Net],
    pin_types: Mapping[PadKey, tuple[str, str]] | None = None,
) -> list[DecouplingGroup]:
    """Find the decoupling caps and the IC supply pins they serve.

    Args:
        nets: Netlist, as the optimizer reads it (each net lists its
            ``(reference, pad)`` pins).
        pin_types: Optional ``(reference, pad) -> (pintype, pinfunction)`` map
            from the PCB's schematic pin annotations. Missing or empty entries
            fall back to the name heuristics.

    Returns:
        One :class:`DecouplingGroup` per supply net that has at least one
        decoupling cap and one IC supply pin, in net order. Groups are sorted
        internally by reference so the result is deterministic.
    """
    from kicad_tools.router.net_class import (
        POWER_PIN_TYPES,
        is_ground_rail_name,
        is_power_rail_name,
        is_power_rail_pin,
    )

    types = pin_types or {}

    # reference -> {pad: net name}
    pad_nets: dict[str, dict[str, str]] = {}
    for net in nets:
        for ref, pad in net.pins:
            pad_nets.setdefault(ref, {})[pad] = net.name

    def is_supply(net: Net) -> bool:
        if is_ground_rail_name(net.name):
            return False
        if is_power_rail_name(net.name):
            return True
        return any(
            _is_ic(ref) and is_power_rail_pin(*types.get((ref, pad), ("", "")))
            for ref, pad in net.pins
        )

    groups: list[DecouplingGroup] = []
    for net in nets:
        if not is_supply(net):
            continue

        caps: list[DecouplingCap] = []
        for ref, pad in net.pins:
            if not _is_capacitor(ref):
                continue
            cap_pads = pad_nets.get(ref, {})
            if len(cap_pads) != 2:
                continue
            (other_pad, other_net) = next((p, n) for p, n in cap_pads.items() if p != pad)
            if is_ground_rail_name(other_net):
                caps.append(DecouplingCap(reference=ref, supply_pad=pad, ground_pad=other_pad))

        ic_pins = [(ref, pad) for ref, pad in net.pins if _is_ic(ref)]
        # Pin-type filter: once any IC pin on the net is typed as a supply,
        # pins typed as something else (an EN or sense input tied to the rail)
        # are not decoupling targets. Untyped pins stay in.
        typed_power = [key for key in ic_pins if types.get(key, ("", ""))[0] in POWER_PIN_TYPES]
        if typed_power:
            ic_pins = [
                key
                for key in ic_pins
                if not types.get(key, ("", ""))[0] or types.get(key, ("", ""))[0] in POWER_PIN_TYPES
            ]

        if caps and ic_pins:
            groups.append(
                DecouplingGroup(
                    net=net.name,
                    caps=tuple(sorted(caps, key=lambda c: (c.reference, c.supply_pad))),
                    pins=tuple(SupplyPin(ref, pad) for ref, pad in sorted(set(ic_pins))),
                )
            )
    return groups


def apply_cap_associations(
    groups: Sequence[DecouplingGroup],
    associations: Sequence[SchematicCapAssociation],
) -> list[DecouplingGroup]:
    """Attach the schematic's cap -> pin designations to *groups* (issue #10142).

    Only associations that name a pin (``derived`` or ``overridden``) are used,
    and only when that pin is one of the group's own supply pins and the cap is
    one of its caps; anything else stays unassociated and keeps the
    distance-greedy pairing. Caps are matched by reference, so net names that
    differ between schematic and board (hierarchical labels) do not matter.
    """
    named = {a.cap.reference: a.pin for a in associations if a.pin is not None}
    if not named:
        return list(groups)
    out: list[DecouplingGroup] = []
    for group in groups:
        pins = set(group.pins)
        pairs = tuple(
            (cap.reference, named[cap.reference])
            for cap in group.caps
            if named.get(cap.reference) in pins
        )
        out.append(dataclasses.replace(group, associated=pairs) if pairs else group)
    return out


def parse_capacitance_farads(value: str) -> float | None:
    """Capacitance of a footprint value string (``100nF``, ``4u7``) in farads.

    ``None`` when the value is not a capacitance. The snap pass uses it to put
    the smallest cap on a pin closest to that pin (issue #10142).
    """
    from kicad_tools.explain.checks.decoupling import _capacitance_farads

    return _capacitance_farads(value)


def _position(
    reference: str,
    pad: str,
    pad_positions: Mapping[PadKey, tuple[float, float]] | None,
    centres: Mapping[str, tuple[float, float]],
) -> tuple[float, float] | None:
    if pad_positions is not None:
        pos = pad_positions.get((reference, pad))
        if pos is not None:
            return pos
    return centres.get(reference)


def assign_caps_to_pins(
    group: DecouplingGroup,
    positions: Mapping[PadKey, tuple[float, float]],
    max_per_pin: int | None = None,
) -> list[tuple[DecouplingCap, SupplyPin, float]]:
    """Pair caps in *group* with supply pins, spreading caps across pins.

    Greedy by distance in rounds: in round *k* only pins already holding *k*
    caps may take another, so every pin gets one cap before any pin gets two.
    With fewer caps than pins, the closest cap-pin pairs win and the
    remaining pins go without. Rounds stop at *max_per_pin* (``None``: until
    every cap is placed); caps left over are not assigned.

    Caps listed in ``group.associated`` (the schematic's own cap -> pin
    designation, issue #10142) are paired first, directly, and count toward
    their pin's load; only the other caps go through the greedy rounds. A group
    with no associations behaves exactly as the greedy pairing alone.

    Args:
        group: The supply net's caps and pins.
        positions: ``(reference, pad) -> (x, y)`` for every cap supply pad and
            supply pin. Entries missing from the map are skipped.
        max_per_pin: Most caps one pin may take.

    Returns:
        ``(cap, pin, distance_mm)`` for every assigned cap.
    """
    caps = [c for c in group.caps if (c.reference, c.supply_pad) in positions]
    pins = [p for p in group.pins if (p.reference, p.pad) in positions]
    if not caps or not pins:
        return []

    def distance(ci: int, pi: int) -> float:
        cx, cy = positions[(caps[ci].reference, caps[ci].supply_pad)]
        px, py = positions[(pins[pi].reference, pins[pi].pad)]
        return math.hypot(cx - px, cy - py)

    assigned: dict[int, tuple[int, float]] = {}
    load = [0] * len(pins)

    # Caps the schematic designates take their pin directly (issue #10142);
    # a designation whose pin has no position falls through to the greedy pairing.
    designated = dict(group.associated)
    pin_index = {(p.reference, p.pad): pi for pi, p in enumerate(pins)}
    for ci, cap in enumerate(caps):
        target = designated.get(cap.reference)
        pi = pin_index.get((target.reference, target.pad)) if target else None
        if pi is not None:
            assigned[ci] = (pi, distance(ci, pi))
            load[pi] += 1
    free = [ci for ci in range(len(caps)) if ci not in assigned]

    pairs: list[tuple[float, int, int]] = [
        (distance(ci, pi), ci, pi) for ci in free for pi in range(len(pins))
    ]
    pairs.sort()

    level = 0
    while len(assigned) < len(caps) and (max_per_pin is None or level < max_per_pin):
        for dist, ci, pi in pairs:
            if ci in assigned or load[pi] != level:
                continue
            assigned[ci] = (pi, dist)
            load[pi] += 1
        level += 1

    return [(caps[ci], pins[pi], dist) for ci, (pi, dist) in sorted(assigned.items())]


def decoupling_pairs(
    groups: Sequence[DecouplingGroup],
    placements: Sequence[ComponentPlacement],
    pad_positions: Mapping[PadKey, tuple[float, float]] | None = None,
) -> list[tuple[str, DecouplingCap, SupplyPin, float]]:
    """Assign caps to pins for every group: ``(net, cap, pin, distance)``.

    Pads missing from *pad_positions* (or all pads, when it is ``None``) are
    measured at their component centre.
    """
    centres = {p.reference: (p.x, p.y) for p in placements}
    out: list[tuple[str, DecouplingCap, SupplyPin, float]] = []
    for group in groups:
        positions: dict[PadKey, tuple[float, float]] = {}
        for cap in group.caps:
            pos = _position(cap.reference, cap.supply_pad, pad_positions, centres)
            if pos is not None:
                positions[(cap.reference, cap.supply_pad)] = pos
        for pin in group.pins:
            pos = _position(pin.reference, pin.pad, pad_positions, centres)
            if pos is not None:
                positions[(pin.reference, pin.pad)] = pos
        out.extend((group.net, c, p, d) for c, p, d in assign_caps_to_pins(group, positions))
    return out


def compute_pin_coverage(
    groups: Sequence[DecouplingGroup] | None,
    placements: Sequence[ComponentPlacement],
    pad_positions: Mapping[PadKey, tuple[float, float]] | None = None,
) -> float:
    """Sum over supply pins of the distance to the *nearest* cap on the rail (mm).

    The ``decoupling_proximity`` FOM term restricted to the identified
    caps and supply pins. Unlike :func:`compute_decoupling_distance` it does
    not care which cap serves which pin, so one cap between two adjacent
    supply pins covers both. The snap pass uses it as a guard: a move that
    helps the cap's assigned pin but strands a neighbouring pin is refused.
    """
    if not groups:
        return 0.0
    centres = {p.reference: (p.x, p.y) for p in placements}
    total = 0.0
    for group in groups:
        caps = [
            pos
            for c in group.caps
            if (pos := _position(c.reference, c.supply_pad, pad_positions, centres)) is not None
        ]
        if not caps:
            continue
        for pin in group.pins:
            pos = _position(pin.reference, pin.pad, pad_positions, centres)
            if pos is not None:
                total += min(math.hypot(pos[0] - x, pos[1] - y) for x, y in caps)
    return total


def compute_decoupling_distance(
    groups: Sequence[DecouplingGroup] | None,
    placements: Sequence[ComponentPlacement],
    pad_positions: Mapping[PadKey, tuple[float, float]] | None = None,
) -> float:
    """Decoupling-affinity cost: summed cap-to-assigned-pin distance (mm).

    Each decoupling cap's supply pad is measured to the IC supply pin
    :func:`assign_caps_to_pins` gives it. Zero means every cap sits on its
    pin; the term grows linearly with distance, like the
    ``decoupling_proximity`` FOM term it is meant to improve. ``None`` or
    empty *groups* yield ``0.0`` (the term is dormant).
    """
    if not groups:
        return 0.0
    return sum(d for _, _, _, d in decoupling_pairs(groups, placements, pad_positions))


@dataclass(frozen=True)
class SnapMove:
    """One cap moved by :func:`snap_decoupling_caps`."""

    cap: str
    pin: str
    before_mm: float
    after_mm: float


def _pad_bbox(pads) -> tuple[float, float, float, float] | None:
    if not pads:
        return None
    return (
        min(p.x - p.size_x / 2 for p in pads),
        min(p.y - p.size_y / 2 for p in pads),
        max(p.x + p.size_x / 2 for p in pads),
        max(p.y + p.size_y / 2 for p in pads),
    )


def _violation(score: PlacementScore, config: PlacementCostConfig) -> float:
    """Weighted hard-constraint part of a score (overlap, DRC, boundary, ...)."""
    b = score.breakdown
    return (
        config.overlap_weight * b.overlap
        + config.drc_weight * b.drc
        + config.boundary_weight * b.boundary
        + config.block_boundary_weight * (b.block_boundary + b.inter_block)
        + config.creepage_weight * b.creepage
    )


def _extent_bbox(
    extents: Mapping[str, tuple[float, float, float, float]] | None,
    reference: str,
    x: float,
    y: float,
    rotation: float,
    side: int,
) -> tuple[float, float, float, float] | None:
    """Board-space box of *reference*'s local extent placed at ``(x, y)``."""
    if not extents or reference not in extents:
        return None
    from .vector import PadDef, _transform_pad

    x0, y0, x1, y1 = extents[reference]
    corners = [
        _transform_pad(PadDef("", cx, cy, 0.0, 0.0), x, y, rotation, side)
        for cx, cy in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    ]
    return _pad_bbox(corners)


def _escape_lanes(
    placed,
    skip_index: int,
    pad_nets: Mapping[PadKey, str],
    supply_net: str,
    escape_mm: float,
    halo_mm: float,
) -> list[tuple[float, float, float, float]]:
    """Boxes a cap must keep clear of so IC signal pins can escape.

    For every IC pad (other than component *skip_index*) on a net that is
    neither *supply_net* nor ground, the lane is the pad's own extent
    stretched *escape_mm* outward along the axis that points away from the
    package centre, and widened by *halo_mm* on both sides of that axis so
    the trace (and a fan-out via) leaving the pad has room beside the cap.
    """
    from kicad_tools.router.net_class import is_ground_rail_name

    lanes: list[tuple[float, float, float, float]] = []
    for j, pc in enumerate(placed):
        if j == skip_index or not _is_ic(pc.reference):
            continue
        for pad in pc.pads:
            net = pad_nets.get((pc.reference, pad.name))
            if not net or net == supply_net or is_ground_rail_name(net):
                continue
            dx, dy = pad.x - pc.x, pad.y - pc.y
            x0, y0 = pad.x - pad.size_x / 2, pad.y - pad.size_y / 2
            x1, y1 = pad.x + pad.size_x / 2, pad.y + pad.size_y / 2
            if abs(dx) >= abs(dy):
                y0, y1 = y0 - halo_mm, y1 + halo_mm
                if dx >= 0:
                    x1 += escape_mm
                else:
                    x0 -= escape_mm
            else:
                x0, x1 = x0 - halo_mm, x1 + halo_mm
                if dy >= 0:
                    y1 += escape_mm
                else:
                    y0 -= escape_mm
            lanes.append((x0, y0, x1, y1))
    return lanes


def snap_decoupling_caps(
    vector,
    components,
    groups: Sequence[DecouplingGroup],
    board,
    score_fn: Callable[[PlacementVector], PlacementScore],
    config: PlacementCostConfig,
    *,
    margin_mm: float = 0.5,
    max_radius_mm: float = 6.0,
    step_mm: float = 0.25,
    directions: int = 16,
    pad_nets: Mapping[PadKey, str] | None = None,
    grid_mm: float = 0.05,
    extents: Mapping[str, tuple[float, float, float, float]] | None = None,
    yard_gap_mm: float = 0.15,
    escape_mm: float = 3.0,
    escape_halo_mm: float = 0.35,
    cap_values: Mapping[str, float] | None = None,
    allow_flip: bool | Collection[str] = False,
    fixed_refs: Collection[str] = (),
) -> tuple[PlacementVector, list[SnapMove]]:
    """Move each decoupling cap to the nearest free spot beside its pin.

    A deterministic local search run after the global optimizer (issue
    #6020). CMA-ES moves every footprint at once and rarely lands a 2 mm part
    within a millimetre of one particular pin, so the cost term alone leaves
    caps a few millimetres out. This pass finishes the job one cap at a time.

    For each ``(cap, pin)`` pair from :func:`assign_caps_to_pins`, candidate
    cap positions are generated so that the cap's supply pad sits at
    distance ``d`` from the pin (``d`` = 0, *step_mm*, ... *max_radius_mm*,
    in *directions* directions, all four rotations, on the cap's current
    side). Candidates are tried nearest first. The first one is taken that

    * keeps the cap's pad-extent bounding box at least *margin_mm* away from
      every other footprint's, and inside the board outline by the same
      margin (real pad geometry, stricter than the optimizer's centred-box
      model); and
    * stays out of the *escape lane* of every IC pad on another net: the
      strip running *escape_mm* straight out from the pad, away from its
      package, widened by *escape_halo_mm* each side (needs *pad_nets*). A cap parked across a signal pin's lane
      blocks the trace that has to leave that pin; pads on the cap's own
      supply net, on ground, or on no net have nothing to escape; and
    * adds no hard-constraint violation under *score_fn* (overlap, DRC,
      boundary, block, creepage). Ordinary moves lower the decoupling term
      and preserve :func:`compute_pin_coverage`.

    With *allow_flip*, a cap can move onto its IC's side. A collection limits
    flips to the named caps. With *cap_values*, caps sharing a pin are snapped
    smallest first. Fixing a required side or value order can increase distance.

    Wirelength is deliberately not a gate: a cap that moves onto its pin
    stretches the ground net's bounding box a little, and that trade is the
    point of the rule. A cap stays in place when no legal candidate is found.

    Args:
        vector: :class:`~kicad_tools.placement.vector.PlacementVector`.
        components: :class:`~kicad_tools.placement.vector.ComponentDef` list
            in vector order.
        groups: Decoupling groups for the board.
        board: :class:`~kicad_tools.placement.cost.BoardOutline`.
        score_fn: Scores a vector; must include the decoupling term.
        config: Cost weights, to separate the hard-constraint part of a
            score from the soft part.
        margin_mm: Required gap between pad-extent boxes.
        max_radius_mm: Furthest supply-pad-to-pin distance tried.
        step_mm: Radius step.
        directions: Number of directions around the pin.
        pad_nets: ``(reference, pad) -> net name`` for the board. Enables
            the escape-lane rule; ``None`` skips it.
        escape_mm: Length of an escape lane.
        escape_halo_mm: Extra lane width on each side of the pad.
        extents: ``reference -> (x0, y0, x1, y1)`` body box (courtyard, or
            silkscreen/fab outline plus pads) in the footprint's own
            (unrotated) frame. When given, a candidate's box must stay
            *yard_gap_mm* clear of every other footprint's, on top of the
            pad-margin rule -- KiCad's ``courtyards_overlap`` and
            ``silk_pad_clearance`` checks see bodies, not just pads.
        yard_gap_mm: Gap required between two *extents* boxes.
        grid_mm: Placement grid the cap centre is rounded to (``0`` keeps
            the exact position). An off-grid cap next to a fine-pitch
            package is harder for the router to reach.
        cap_values: ``cap reference -> farads``. Enables the smallest-closest
            ordering; caps missing from the map are not ordered.
        allow_flip: Allow all caps, or only the named caps, to flip to their IC side.
        fixed_refs: Leave these caps in place and include them in value ordering.

    Returns:
        ``(new_vector, moves)``.
    """
    from .cost import ComponentPlacement
    from .vector import (
        FIELDS_PER_COMPONENT,
        ROTATION_STEPS,
        PlacementVector,
        _transform_pad,
        decode,
    )
    from .wirelength import build_pad_position_map

    index = {c.reference: i for i, c in enumerate(components)}
    designated = {ref for group in groups for ref, _ in group.associated}
    if isinstance(allow_flip, bool):
        allow_flip = set(index) if allow_flip else set()
    data = vector.data.copy()
    current = PlacementVector(data=data.copy())
    best_score = score_fn(current)
    best_violation = _violation(best_score, config)

    def coverage(vec_data) -> float:
        placed_ = decode(PlacementVector(data=vec_data), components)
        return compute_pin_coverage(
            groups,
            [ComponentPlacement(p.reference, p.x, p.y, p.rotation) for p in placed_],
            build_pad_position_map(placed_),
        )

    best_coverage = coverage(data)

    placed = decode(current, components)
    placements = [ComponentPlacement(p.reference, p.x, p.y, p.rotation) for p in placed]
    pairs = sorted(
        decoupling_pairs(groups, placements, build_pad_position_map(placed)),
        key=lambda t: (t[3], t[1].reference),
    )
    if cap_values:
        # Smallest value first among the caps sharing a pin: it gets the
        # nearest free spot. Each pin's caps keep the slots they already had.
        slots: dict[SupplyPin, list[int]] = defaultdict(list)
        for i, t in enumerate(pairs):
            slots[t[2]].append(i)
        reordered = list(pairs)
        for idxs in slots.values():
            ordered = sorted(
                (pairs[i] for i in idxs),
                key=lambda t: cap_values.get(t[1].reference, math.inf),
            )
            for i, t in zip(idxs, ordered, strict=True):
                reordered[i] = t
        pairs = reordered
    # Seed value ordering with the caps that cannot move.
    handled: dict[SupplyPin, list[tuple[float, float]]] = defaultdict(list)

    for _, cap, pin, distance in pairs:
        if cap.reference in fixed_refs and cap_values and cap.reference in cap_values:
            handled[pin].append((cap_values[cap.reference], distance))

    angles = [2 * math.pi * k / directions for k in range(directions)]
    moves: list[SnapMove] = []

    for net, cap, pin, before in pairs:
        if cap.reference in fixed_refs:
            continue
        ci = index.get(cap.reference)
        pin_i = index.get(pin.reference)
        if ci is None or pin_i is None:
            continue
        cap_def = components[ci]
        supply = next((p for p in cap_def.pads if p.name == cap.supply_pad), None)
        if supply is None:
            continue

        placed = decode(PlacementVector(data=data), components)
        pin_pad = next((p for p in placed[pin_i].pads if p.name == pin.pad), None)
        if pin_pad is None:
            continue
        others = [
            bb for j, pc in enumerate(placed) if j != ci and (bb := _pad_bbox(pc.pads)) is not None
        ]
        yards = [
            yb
            for j, pc in enumerate(placed)
            if j != ci
            and (
                yb := _extent_bbox(
                    extents, pc.reference, pc.x, pc.y, pc.rotation, pc.side ^ components[j].side
                )
            )
        ]
        lanes = (
            _escape_lanes(placed, ci, pad_nets, net, escape_mm, escape_halo_mm) if pad_nets else []
        )

        def legal(
            bb: tuple[float, float, float, float],
            yard: tuple[float, float, float, float] | None = None,
        ) -> bool:
            if yard is not None and any(
                yard[0] < o[2] + yard_gap_mm
                and o[0] < yard[2] + yard_gap_mm
                and yard[1] < o[3] + yard_gap_mm
                and o[1] < yard[3] + yard_gap_mm
                for o in yards
            ):
                return False
            if (
                bb[0] < board.min_x + margin_mm
                or bb[1] < board.min_y + margin_mm
                or bb[2] > board.max_x - margin_mm
                or bb[3] > board.max_y - margin_mm
            ):
                return False
            if any(
                bb[0] < o[2] + margin_mm
                and o[0] < bb[2] + margin_mm
                and bb[1] < o[3] + margin_mm
                and o[1] < bb[3] + margin_mm
                for o in others
            ):
                return False
            return not any(
                bb[0] < o[2] and o[0] < bb[2] and bb[1] < o[3] and o[1] < bb[3] for o in lanes
            )

        base = ci * FIELDS_PER_COMPONENT
        side = int(round(float(data[base + 3])))
        ic_side = int(round(float(data[pin_i * FIELDS_PER_COMPONENT + 3])))
        flip = side != ic_side and cap.reference in allow_flip
        if flip:
            side = ic_side
        value = cap_values.get(cap.reference) if cap_values else None
        # A larger cap never ends nearer to the pin than a smaller one on it.
        floor = max(
            (dist for v, dist in handled[pin] if value is not None and v < value),
            default=None,
        )
        ceiling = min(
            (dist for v, dist in handled[pin] if value is not None and v > value),
            default=None,
        )
        named = cap.reference in designated
        # Meeting a required side or value order can mean moving farther away.
        move_outward = flip or (named and floor is not None and before <= floor + 1e-6)
        accepted = False
        final = before
        # No legal supply pad lies beyond the farthest corner of the board.
        # Bound the search even when the CLI radius is a very large finite value.
        farthest = math.hypot(
            max(abs(pin_pad.x - board.min_x), abs(pin_pad.x - board.max_x)),
            max(abs(pin_pad.y - board.min_y), abs(pin_pad.y - board.max_y)),
        )
        search_radius = min(max_radius_mm, farthest + grid_mm)
        for step in range(round(search_radius / step_mm) + 1):
            d = step * step_mm
            if d >= before and not move_outward:
                break
            for rot_idx, rot in enumerate(ROTATION_STEPS):
                off = _transform_pad(supply, 0.0, 0.0, rot, side ^ cap_def.side)
                for a in angles if d > 0 else angles[:1]:
                    cx = pin_pad.x + d * math.cos(a) - off.x
                    cy = pin_pad.y + d * math.sin(a) - off.y
                    if grid_mm > 0:
                        cx = round(cx / grid_mm) * grid_mm
                        cy = round(cy / grid_mm) * grid_mm
                    after = math.hypot(cx + off.x - pin_pad.x, cy + off.y - pin_pad.y)
                    if named and after > max_radius_mm + 1e-9:
                        continue
                    if after >= before and not move_outward:
                        continue
                    if floor is not None and after <= floor + 1e-6:
                        continue
                    if ceiling is not None and after >= ceiling - 1e-6:
                        continue
                    bb = _pad_bbox(
                        [_transform_pad(p, cx, cy, rot, side ^ cap_def.side) for p in cap_def.pads]
                    )
                    yard = _extent_bbox(extents, cap.reference, cx, cy, rot, side ^ cap_def.side)
                    if bb is None or not legal(bb, yard):
                        continue
                    trial = data.copy()
                    trial[base], trial[base + 1], trial[base + 2] = cx, cy, float(rot_idx)
                    trial[base + 3] = float(side)
                    score = score_fn(PlacementVector(data=trial))
                    violation = _violation(score, config)
                    improved = (
                        score.breakdown.decoupling <= best_score.breakdown.decoupling
                        if flip
                        else score.breakdown.decoupling < best_score.breakdown.decoupling
                    )
                    if violation > best_violation or not (improved or (named and move_outward)):
                        continue
                    cov = coverage(trial)
                    if (named and move_outward) or cov <= best_coverage + 1e-9:
                        data, best_score, best_violation = trial, score, violation
                        best_coverage = cov
                        accepted = True
                        moves.append(
                            SnapMove(cap.reference, f"{pin.reference}.{pin.pad}", before, after)
                        )
                        final = after
                        break
                if accepted:
                    break
            if accepted:
                break
        if value is not None:
            handled[pin].append((value, final if accepted else before))

    return PlacementVector(data=data), moves
