"""Pour-slot advisory: parallel traces too close for the zone fill (Issue #8435).

Two different-net tracks that run side by side on a layer carrying a copper
zone leave a strip of pour between them.  Subtract each track's required
clearance to the zone from the edge-to-edge gap to get the strip width.
A strip narrower than the zone's ``min_thickness``
cannot be filled, so KiCad drops it and the plane gets a *slot* -- a long
copper-free channel that passes every DRC gate.

Rule id ``pour_slot`` (always ``severity="warning"``).  A pair is reported
when the tracks are parallel, on the same layer as a pour zone, on different
nets, overlap along their length, and::

    first_clearance + second_clearance < gap < first_clearance + second_clearance + min_thickness

Each side uses the maximum of the board minimum, the zone clearance, and the
applicable track and zone netclass clearances. The last matching custom copper
clearance replaces the project requirement for that track-to-zone pair.
Touching/overlapping tracks (``gap <= 0``) are a DRC matter and not reported,
and so is a *packed* pair whose gap is at or below the sum (Issue #10121): its
clearance keep-outs already overlap, so no fill strip ever existed between
the tracks -- the copper-free channel is the unavoidable clearance void any
two different-net conductors at that spacing produce, and ``min_thickness``
has no island to drop.  Native-refill controls on pinned KiCad 10.0.6
(Issue #10121) confirm the split: a 0.55 mm gap over a 0.2/0.3 mm zone leaves
a dropped-island slot on the film, while a 0.395 mm gap (5 um below packed)
leaves only the clearance void, inside a pad row whose own 0.250 mm pad-edge
gap produces the same channel with no tracks at all.

Which fill-gap case the threshold matches (see :meth:`Zone.fill_inflation`):
that docstring's ``gap <= min_thickness`` row is about when two *fill
fragments* are electrically bonded, which is a different question.  Here the
question is whether a strip *between tracks* survives the fill, and KiCad
discards fill islands narrower than ``min_thickness`` after clearing each
track by its applicable clearance.  This is a
geometric advisory, not a native-DRC oracle, and it is independent of the
board's file version.

The repair pass (moving/packing traces) is
:mod:`kicad_tools.optim.pour_slot_cleanup` (Issue #8440); it consumes
:func:`iter_pour_slot_pairs` so detection is implemented once and the cleanup
cannot drift from this advisory.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from itertools import product
from typing import TYPE_CHECKING, Any

from shapely import get_parts  # type: ignore[import-untyped]
from shapely.geometry import LineString, Polygon  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]

from kicad_tools.core.project_clearance import DEFAULT_NETCLASS_CLEARANCE_MM
from kicad_tools.router.board_clearance_rules import (
    CLEARANCE,
    BoardClearanceRules,
    ItemProps,
)
from kicad_tools.schema.pcb import _segment_dedup_key

from ..copper_refs import Origin, board_origin, segment_witness
from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules, ItemProps
from kicad_tools.schema.pcb import PCB

RULE_ID = "pour_slot"

#: Sine of the largest angle (between track directions) treated as parallel.
PARALLEL_SIN_TOL = 0.02
#: Minimum side-by-side overlap (mm) worth reporting.
MIN_OVERLAP_MM = 0.01
#: Packed-pair tolerance (mm): ``gap <= sum of clearances + PACKED_GAP_TOL_MM``
#: leaves no fillable strip between the clearance keep-outs (Issue #10121).
PACKED_GAP_TOL_MM = 1e-3


def _unit(seg: Any) -> tuple[float, float, float] | None:
    dx, dy = seg.end[0] - seg.start[0], seg.end[1] - seg.start[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    return dx / length, dy / length, length


@dataclass(frozen=True)
class _Pour:
    outline: BaseGeometry
    net_name: str
    clearance: float
    min_thickness: float
    priority: int = 0
    single_layer: bool = False  #: the zone's ``Layer`` property is unambiguous


def _effective_pours(pours: list[_Pour]) -> list[_Pour]:
    """Clip each pour to the area it governs on its layer (Issue #10121).

    KiCad fills higher-priority zones first and removes their outlines from
    lower-priority zones, so only the highest-priority zone covering a point
    decides that point's clearance and ``min_thickness``.  Zones of equal
    priority keep their full outlines.
    """
    result: list[_Pour] = []
    for pour in pours:
        higher = [p.outline for p in pours if p.priority > pour.priority]
        outline = pour.outline
        if higher:
            outline = outline.difference(unary_union(higher))
        if not outline.is_empty:
            result.append(replace(pour, outline=outline))
    return result


@dataclass(frozen=True)
class PourSlotPair:
    """One flagged track pair (Issue #8440): the segments behind a violation."""

    layer: str
    first: Any
    second: Any
    gap: float  #: edge-to-edge distance (mm)
    threshold: float  #: sum of the two track-to-zone clearances and the minimum thickness
    first_clearance: float
    second_clearance: float
    thickness: float
    overlap: float
    location: tuple[float, float]
    unresolved_rules: tuple[str, ...] = ()  #: alternative clearance readings, advisory only

    @property
    def clearance(self) -> float:
        """Conservative spacing for moved copper in the repair pass."""
        return max(self.first_clearance, self.second_clearance)


PourSlotClearances = BoardClearanceRules | Mapping[str, float]


def iter_pour_slot_pairs(
    pcb: PCB, netclass_clearances: PourSlotClearances | None = None
) -> Iterator[PourSlotPair]:
    """Yield every pair :class:`PourSlotRule` would report, with its segments."""
    return PourSlotRule(netclass_clearances).iter_pairs(pcb)


class PourSlotRule(DRCRule):
    """Report parallel different-net track pairs that leave a slot in a pour."""

    rule_id = RULE_ID
    name = "Pour Slot"
    description = (
        "Reports parallel different-net tracks whose gap leaves an unfillable "
        "strip in a poured layer after allowing for each track's clearance "
        "to the zone (advisory)."
    )

    def __init__(self, netclass_clearances: PourSlotClearances | None = None) -> None:
        self.clearances = netclass_clearances or {}
        self._rule_areas: list[tuple[str, frozenset[str], BaseGeometry]] = []
        self._areas_cache: dict[int, tuple[tuple[str, bool | None], ...]] = {}
        self._enclosed_cache: dict[int, tuple[tuple[str, bool | None], ...]] = {}

    def _track_clearance(self, track: str, zone_net: str, layer: str) -> float:
        if isinstance(self.clearances, BoardClearanceRules):
            track_props = ItemProps(
                "Track", track, layer=layer, footprint_ref="", footprint_lib_id=""
            )
            zone_props = ItemProps("Zone", zone_net, footprint_ref="", footprint_lib_id="")
            return self.clearances.clearance(track_props, zone_props, layer)
        default = self.clearances.get("", DEFAULT_NETCLASS_CLEARANCE_MM)
        return max(self.clearances.get(track, default), self.clearances.get(zone_net, default))

    def _segment_clearance(
        self, seg: Any, zone: _Pour, layer: str, areas: tuple[tuple[str, bool | None], ...]
    ) -> tuple[tuple[float, ...], tuple[str, ...]]:
        """Possible track-to-zone clearances and the rules that leave them undecided."""
        if not isinstance(self.clearances, BoardClearanceRules):
            value = self._track_clearance(seg.net_name, zone.net_name, layer)
            return (max(zone.clearance, value),), ()
        track = ItemProps(
            "Track",
            seg.net_name,
            layer=layer,
            footprint_ref="",
            footprint_lib_id="",
            areas=areas,
            enclosed=self._segment_enclosed(seg, layer),
            width_mm=seg.width,
        )
        pour = ItemProps(
            "Zone",
            zone.net_name,
            layer=layer if zone.single_layer else None,
            footprint_ref="",
            footprint_lib_id="",
        )
        res = self.clearances.explain_required(CLEARANCE, track, pour, layer)
        if not res.possible_requirements:
            return (max(zone.clearance, res.requirement),), ()
        values = tuple(sorted({max(zone.clearance, c) for c in res.possible_requirements}))
        return values, res.unknown_rules if len(values) > 1 else ()

    def _segment_areas(self, seg: Any, layer: str) -> tuple[tuple[str, bool | None], ...]:
        """Per named rule area: does ``seg``'s copper touch it (``None``: other layer)."""
        key = id(seg)
        cached = self._areas_cache.get(key)
        if cached is not None:
            return cached
        shape = LineString([seg.start, seg.end]).buffer(seg.width / 2)
        result = tuple(
            (name, None if layers and layer not in layers else bool(shape.intersects(outline)))
            for name, layers, outline in self._rule_areas
        )
        self._areas_cache[key] = result
        return result

    def _segment_enclosed(self, seg: Any, layer: str) -> tuple[tuple[str, bool | None], ...]:
        """Per named rule area: is ``seg``'s copper fully inside it (``None``: other layer)."""
        key = id(seg)
        cached = self._enclosed_cache.get(key)
        if cached is not None:
            return cached
        shape = LineString([seg.start, seg.end]).buffer(seg.width / 2)
        result = tuple(
            (name, None if layers and layer not in layers else bool(outline.covers(shape)))
            for name, layers, outline in self._rule_areas
        )
        self._enclosed_cache[key] = result
        return result

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        results = DRCResults()
        results.rules_checked = 1
        results.rules_checked_by_rule[RULE_ID] = 1
        origin = board_origin(pcb)
        for pair in self.iter_pairs(pcb):
            results.add(self._violation(pair, origin))
        return results

    def iter_pairs(self, pcb: PCB) -> Iterator[PourSlotPair]:
        """Yield each flagged pair (the single detection path, Issue #8440)."""
        zones: dict[str, list[_Pour]] = defaultdict(list)
        self._rule_areas = []
        self._areas_cache = {}
        self._enclosed_cache = {}
        pour_keepouts: dict[str, list[BaseGeometry]] = defaultdict(list)
        for zone in pcb.zones:
            if zone.keepout is not None:
                contours = getattr(zone, "rule_area_contours", None) or [zone.polygon]
                if getattr(zone, "name", "") and all(len(ring) >= 3 for ring in contours):
                    area = Polygon(contours[0], holes=contours[1:])
                    self._rule_areas.append(
                        (
                            zone.name,
                            frozenset({zone.layer, *getattr(zone, "layers", [])} - {""}),
                            area if area.is_valid else area.buffer(0),
                        )
                    )
                if not zone.keepout.copperpour_allowed:
                    ko_contours = getattr(zone, "rule_area_contours", None) or [zone.polygon]
                    if all(len(ring) >= 3 for ring in ko_contours):
                        ko = Polygon(ko_contours[0], holes=ko_contours[1:])
                        ko = ko if ko.is_valid else ko.buffer(0)
                        for ko_layer in {zone.layer, *getattr(zone, "layers", [])} - {""}:
                            pour_keepouts[ko_layer].append(ko)
                continue
            contours = zone.rule_area_contours or [zone.polygon]
            if any(len(ring) < 3 for ring in contours):
                continue
            outline = Polygon(contours[0], holes=contours[1:])
            pour = _Pour(
                outline if outline.is_valid else outline.buffer(0),
                getattr(zone, "net_name", ""),
                zone.clearance,
                zone.min_thickness,
                getattr(zone, "priority", 0) or 0,
                len({zone.layer, *getattr(zone, "layers", [])} - {""}) == 1,
            )
            for layer in {zone.layer, *getattr(zone, "layers", [])}:
                if layer:
                    zones[layer].append(pour)
        # Only the highest-priority zone governs each point (Issue #10121).
        zones = {layer: _effective_pours(pours) for layer, pours in zones.items()}
        # A copper-pour keepout leaves no fill on its layers, so no slot can
        # form inside it (Issue #10121 review).
        for layer, kos in pour_keepouts.items():
            if layer in zones:
                cut = unary_union(kos)
                zones[layer] = [replace(p, outline=p.outline.difference(cut)) for p in zones[layer]]
                zones[layer] = [p for p in zones[layer] if not p.outline.is_empty]
        zones = {layer: pours for layer, pours in zones.items() if pours}
        if not zones:
            return

        # Exact duplicate copies of a segment (same net, layer, width and
        # unordered endpoints) are already reported by
        # ``redundant_copper_duplicate``; collect each geometry once so a
        # duplicated track does not report the same slot once per copy
        # (Issue #8503).
        by_layer: dict[str, list[tuple[Any, tuple[float, float, float]]]] = defaultdict(list)
        collected: set[Any] = set()
        for seg in pcb.segments:
            if seg.layer in zones and seg.width > 0 and seg.net_number:
                key = _segment_dedup_key(seg.net_number, seg.layer, seg.start, seg.end, seg.width)
                if key in collected:
                    continue
                u = _unit(seg)
                if u is not None:
                    collected.add(key)
                    by_layer[seg.layer].append((seg, u))

        for layer, items in by_layer.items():
            yield from self._check_layer(layer, items, zones[layer])

    @staticmethod
    def _violation(pair: PourSlotPair, origin: Origin) -> DRCViolation:
        a, b = pair.first, pair.second
        na = a.net_name or f"net {a.net_number}"
        nb = b.net_name or f"net {b.net_number}"
        if pair.unresolved_rules:
            message = (
                f"Possible pour slot between [{na}] and [{nb}] on {pair.layer} "
                f"at a {pair.gap:.3f} mm gap. The track-to-zone clearance depends on "
                f"unresolved rules ({', '.join(pair.unresolved_rules)})."
            )
        else:
            message = (
                f"Pour slot: [{na}] and [{nb}] run parallel on {pair.layer} for "
                f"{pair.overlap:.2f} mm with {pair.gap:.3f} mm gap; the pour between them "
                f"(< {pair.threshold:.3f} mm = {pair.first_clearance:.3f} + "
                f"{pair.second_clearance:.3f} clearances + "
                f"{pair.thickness:.3f} min_thickness) cannot fill, leaving a slot"
            )
        return DRCViolation(
            rule_id=RULE_ID,
            severity="warning",
            message=message,
            location=pair.location,
            layer=pair.layer,
            actual_value=pair.gap,
            required_value=None if pair.unresolved_rules else pair.threshold,
            witnesses=(segment_witness(a, origin), segment_witness(b, origin)),
            nets=(na, nb),
        )

    # ------------------------------------------------------------------

    def _check_layer(
        self,
        layer: str,
        items: list[tuple[Any, tuple[float, float, float]]],
        zlist: list[_Pour],
    ) -> Iterator[PourSlotPair]:
        # Use the same electrical requirements for the search radius and the
        # candidate strip width. Zones on one net can have distinct local clearances.
        nets = {seg.net_name for seg, _ in items}
        clearances = {
            (net, zone_net): self._track_clearance(net, zone_net, layer)
            for zone_net in {z.net_name for z in zlist}
            for net in nets
        }
        max_clear = max(max(clearances.values()), max(z.clearance for z in zlist))
        max_thick = max(z.min_thickness for z in zlist)
        max_w = max(s.width for s, _u in items)
        reach = 2 * max_clear + max_thick + max_w  # centre-to-centre search radius
        cell = max(reach, 0.1)

        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (seg, _u) in enumerate(items):
            xs = (seg.start[0], seg.end[0])
            ys = (seg.start[1], seg.end[1])
            for cx in range(int((min(xs) - reach) // cell), int((max(xs) + reach) // cell) + 1):
                for cy in range(int((min(ys) - reach) // cell), int((max(ys) + reach) // cell) + 1):
                    grid[(cx, cy)].append(i)

        seen: set[tuple[int, int]] = set()
        for members in grid.values():
            for ai, i in enumerate(members):
                for j in members[ai + 1 :]:
                    key = (i, j) if i < j else (j, i)
                    if key in seen:
                        continue
                    seen.add(key)
                    pair = self._check_pair(layer, items[key[0]], items[key[1]], zlist)
                    if pair is not None:
                        yield pair

    def _check_pair(
        self,
        layer: str,
        first: tuple[Any, tuple[float, float, float]],
        second: tuple[Any, tuple[float, float, float]],
        zlist: list[_Pour],
    ) -> PourSlotPair | None:
        # Use the same projection axis for either serialization of the pair.
        if first[0].net_number > second[0].net_number:
            first, second = second, first
        a, (ux, uy, la) = first
        b, (vx, vy, _lb) = second
        if a.net_number == b.net_number:
            return None
        if abs(ux * vy - uy * vx) > PARALLEL_SIN_TOL:
            return None

        # Project b's ends onto a's frame: t along a, d perpendicular to a.
        def frame(p: tuple[float, float]) -> tuple[float, float]:
            rx, ry = p[0] - a.start[0], p[1] - a.start[1]
            return rx * ux + ry * uy, -rx * uy + ry * ux

        tb0, db0 = frame(b.start)
        tb1, db1 = frame(b.end)
        start_t = max(0.0, min(tb0, tb1))
        end_t = min(la, max(tb0, tb1))
        overlap = end_t - start_t
        if overlap < MIN_OVERLAP_MM:
            return None
        half_widths = (a.width + b.width) / 2
        slope = (db1 - db0) / (tb1 - tb0)

        def separation(t: float) -> float:
            return db0 + slope * (t - tb0)

        areas_a = self._segment_areas(a, layer)
        areas_b = self._segment_areas(b, layer)

        def slot(zone: _Pour, clearance_a: float, clearance_b: float) -> PourSlotPair | None:
            packed_gap = clearance_a + clearance_b
            threshold = packed_gap + zone.min_thickness
            # Split the shared projection where either facing gap crosses a
            # slot boundary. Within each interval the signed distance is linear.
            low = half_widths + packed_gap + PACKED_GAP_TOL_MM
            high = half_widths + threshold - 1e-6
            cuts = [start_t, end_t]
            if slope:
                for boundary in (-high, -low, low, high):
                    t = tb0 + (boundary - db0) / slope
                    if start_t < t < end_t:
                        cuts.append(t)
                cuts.sort()

            midpoint_shift = (a.width - b.width) / 4 + (clearance_a - clearance_b) / 2
            runs = []
            for left, right in zip(cuts, cuts[1:], strict=False):
                distance = separation((left + right) / 2)
                if not low < abs(distance) < high:
                    continue
                shift = math.copysign(1, distance) * midpoint_shift
                ends = []
                for t in (left, right):
                    offset = separation(t) / 2 + shift
                    ends.append(
                        (a.start[0] + ux * t - uy * offset, a.start[1] + uy * t + ux * offset)
                    )
                covered = zone.outline.intersection(LineString(ends))
                runs.extend(get_parts(covered))
            run = max(runs, key=lambda part: part.length, default=None)
            if run is None or run.length < MIN_OVERLAP_MM:
                return None
            point = run.interpolate(0.5, normalized=True)
            gap = abs(separation(frame((point.x, point.y))[0])) - half_widths
            return PourSlotPair(
                layer=layer,
                first=a,
                second=b,
                gap=gap,
                threshold=threshold,
                first_clearance=clearance_a,
                second_clearance=clearance_b,
                thickness=zone.min_thickness,
                overlap=run.length,
                location=(point.x, point.y),
            )

        possible = None
        for zone in zlist:
            # A track on the pour net joins the fill along its edge. Only a
            # third-net pour has a clearance boundary on both sides of the strip.
            if zone.net_name and zone.net_name in (a.net_name, b.net_name):
                continue
            values_a, unknown_a = self._segment_clearance(a, zone, layer, areas_a)
            values_b, unknown_b = self._segment_clearance(b, zone, layer, areas_b)
            for ca, cb in product(values_a, values_b):
                pair = slot(zone, ca, cb)
                if pair is None:
                    continue
                if not unknown_a and not unknown_b:
                    return pair
                # This clearance could leave a slot. Keep looking for a
                # definite finding in another governing zone before reporting it.
                if possible is None:
                    possible = replace(
                        pair, unresolved_rules=tuple(dict.fromkeys(unknown_a + unknown_b))
                    )
                break
        return possible
