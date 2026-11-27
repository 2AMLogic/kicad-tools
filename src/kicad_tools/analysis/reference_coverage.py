"""Measure track centerlines against the saved fills on adjacent copper layers."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from functools import cache
from itertools import chain, pairwise
from typing import Any

from kicad_tools.core.layers import COPPER_LAYER_ORDER, is_declared_copper_layer
from kicad_tools.physics.stackup import Stackup
from kicad_tools.physics.transmission_line import TransmissionLine
from kicad_tools.schema.pcb import PCB, Arc, Segment

XY = tuple[float, float]
Report = dict[str, Any]
LayerFills = dict[str, dict[str, Any] | None]
Estimate = Callable[[str, float], Report | None]


def _reference_fills(pcb: PCB, order: list[str]) -> LayerFills:
    """Union saved copper once per layer/net. Missing fill makes its side unknown."""
    from shapely import union_all  # type: ignore[import-untyped]

    from kicad_tools.validate.rules.clearance import _collect_zone_fills

    polygons: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    filled_layers: dict[int, set[str]] = defaultdict(set)
    for layer, fills in _collect_zone_fills(pcb, include_unassigned=True).items():
        for fill in fills:
            zone = pcb.zones[fill.source_zone_index]
            if zone.keepout is not None:
                continue
            polygon: Any = fill.polygon
            if inflation := zone.fill_inflation():
                polygon = polygon.buffer(inflation)
            polygons[layer][fill.net_name].append(polygon)
            filled_layers[fill.source_zone_index].add(layer)

    unknown = set()
    for i, zone in enumerate(pcb.zones):
        if zone.keepout is None:
            declared = set(zone.layers or [zone.layer])
            if "*.Cu" in declared:
                declared.update(order)
            if "F&B.Cu" in declared:
                declared.update(("F.Cu", "B.Cu"))
            unknown.update(declared - filled_layers[i])
    return {
        layer: None
        if layer in unknown
        else {net: union_all(parts) for net, parts in polygons[layer].items()}
        for layer in order
    }


def _paths(tracks: list[Segment]) -> Iterator[list[tuple[Segment, XY]]]:
    """Follow end-to-end tracks on one layer, splitting branches and disjoint runs."""
    endpoints: dict[XY, list[int]] = defaultdict(list)
    # Coordinate ordering keeps distances independent of file order or direction.
    tracks = sorted(
        tracks,
        key=lambda t: (
            min(t.start, t.end),
            max(t.start, t.end),
            getattr(t, "mid", ()),
            t.width,
            t.uuid,
        ),
    )
    for i, track in enumerate(tracks):
        endpoints[track.start].append(i)
        endpoints[track.end].append(i)
    visited: set[int] = set()
    # Start open paths at a terminal/junction. Remaining degree-two runs are loops.
    for origin in sorted(endpoints, key=lambda p: (len(endpoints[p]) == 2, p)):
        for i in endpoints[origin]:
            path = []
            point = origin
            while i not in visited:
                track = tracks[i]
                path.append((track, point))
                visited.add(i)
                point = track.end if point == track.start else track.start
                incident = endpoints[point]
                if len(incident) != 2:
                    break
                i = incident[1] if incident[0] == i else incident[0]
            if path:
                yield path


def _spans(
    track: Segment, origin: XY, offset: float, references: LayerFills, estimate: Estimate, net: str
) -> tuple[list[Report], float]:
    """Split at exact straight-line intersections; arcs use bounded-error chords."""
    from shapely import get_parts
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

    points = track.centerline_points() if isinstance(track, Arc) else [track.start, track.end]
    line = LineString(points if track.start == origin else points[::-1])
    length = track.length if isinstance(track, Arc) else line.length
    if not length:
        return [], 0.0
    events: dict[float, list[tuple[str, str, int]]] = {0.0: [], length: []}
    for side, fills in references.items():
        for name, polygon in (fills or {}).items():
            for part in get_parts(line.intersection(polygon)):
                if part.geom_type != "LineString" or part.is_empty:
                    continue  # Isolated tangent points have zero covered length.
                start, end = sorted(
                    line.project(Point(p), normalized=True) * length
                    for p in (part.coords[0], part.coords[-1])
                )
                events.setdefault(start, []).append((side, name, 1))
                events.setdefault(end, []).append((side, name, -1))
    active: dict[str, Counter[str]] = defaultdict(Counter)
    spans = []
    for left, right in pairwise(sorted(events)):
        for side, name, change in events[left]:
            active[side][name] += change
        names = {
            side: sorted(+active[side]) if fills is not None else None
            for side, fills in references.items()
        }
        # A stripline needs copper on BOTH sides at this same location.
        # Same-net, floating and overlapping different-net fills do not establish
        # a unique independent reference conductor.
        supported = names and all(
            ns and len(ns) == 1 and ns[0] not in ("", net) for ns in names.values()
        )
        spans.append(
            {
                "start_mm": round(offset + left, 9),
                "end_mm": round(offset + right, 9),
                "width_mm": track.width,
                "reference_nets": names,
                "estimate": estimate(track.layer, track.width) if supported else None,
            }
        )
    return spans, length


def _estimator(stack: Stackup) -> Estimate:
    # Plane *locations* here come from the clipped adjacent fills. The usual
    # stripline calculator can scan past a signal layer to a declared power layer.
    local = replace(stack, layers=[replace(layer, copper_role="") for layer in stack.layers])
    model = TransmissionLine(local)

    # Require one measured dielectric between each pair of adjacent copper
    # layers. Otherwise the calculator could substitute a default substrate.
    usable = {l.name for l in stack.copper_layers if l.thickness_mm > 0}
    for top, bottom in pairwise(stack.copper_layers):
        gap = stack.layers[stack.get_layer_index(top.name) + 1 : stack.get_layer_index(bottom.name)]
        if (
            len(gap) != 1
            or not gap[0].is_dielectric
            or gap[0].epsilon_r < 1
            or gap[0].thickness_mm <= 0
        ):
            usable.difference_update((top.name, bottom.name))

    @cache
    def estimate(layer: str, width: float) -> Report | None:
        if not stack.has_explicit_data or layer not in usable or width <= 0:
            return None
        outer = stack.is_outer_layer(layer)
        result = model.microstrip(width, layer) if outer else model.stripline(width, layer)
        return {
            "model": "microstrip" if outer else "stripline",
            "z0_ohm": round(result.z0, 6),
            "delay_ps_per_mm": round(result.propagation_delay_ps_per_mm, 6),
        }

    return estimate


def _reference_summary(side: str, paths: list[Report], known: bool) -> Report:
    """Summarize one adjacent reference side of a layer.

    ``coverage`` takes one of five states:

    - ``"not_applicable"``: the fill is known, but no centerline length was
      measured on this layer (the net only passes through on a via), so both
      distances are ``0.0``.
    - ``"none"``: routed copper of positive length, none of it over this reference.
    - ``"partial"``: some routed length over this reference and some over void.
    - ``"complete"``: every routed length is over this reference.
    - ``"unknown"``: a zone declared on this side has no saved fill, so every
      measurement is ``None``.

    Unreferenced copper is ``"none"`` alone; ``"not_applicable"`` rows carry
    no routed length to judge.
    """
    if not known:
        return {
            "coverage": "unknown",
            "covered_mm": None,
            "uncovered_mm": None,
            "by_net_mm": None,
            "void_spans": None,
        }
    covered = uncovered = 0.0
    by_net: Counter[str] = Counter()
    voids: list[Report] = []
    for path_index, path in enumerate(paths):
        for span in path["spans"]:
            names = span["reference_nets"][side]
            length = span["end_mm"] - span["start_mm"]
            if names:
                covered += length
                for name in names:
                    by_net[name] += length
            else:
                uncovered += length
                if (
                    voids
                    and voids[-1]["path"] == path_index
                    and voids[-1]["end_mm"] == span["start_mm"]
                ):
                    voids[-1]["end_mm"] = span["end_mm"]
                else:
                    voids.append(
                        {"path": path_index, "start_mm": span["start_mm"], "end_mm": span["end_mm"]}
                    )
    if not covered and not uncovered:
        coverage = "not_applicable"
    elif not covered:
        coverage = "none"
    elif uncovered:
        coverage = "partial"
    else:
        coverage = "complete"
    return {
        "coverage": coverage,
        "void_spans": voids,
        "covered_mm": round(covered, 9),
        "uncovered_mm": round(uncovered, 9),
        "by_net_mm": {name: round(value, 9) for name, value in by_net.items()},
    }


def _net_report(
    pcb: PCB, name: str, order: list[str], fills: LayerFills, estimate: Estimate
) -> Report:
    net = pcb.get_net_by_name(name)
    if net is None:
        raise ValueError(f"Unknown net {name!r}")
    tracks: dict[str, list[Segment]] = defaultdict(list)
    for t in chain(pcb.segments_in_net(net.number), pcb.arcs_in_net(net.number)):
        tracks[t.layer].append(t)
    vias = list(pcb.vias_in_net(net.number))
    via_counts: Counter[str] = Counter()
    for via in vias:
        ends = [order.index(layer) for layer in via.layers]
        if ends:
            via_counts.update(order[min(ends) : max(ends) + 1])

    layers: dict[str, Report] = {}
    for layer in sorted(tracks.keys() | via_counts.keys()):
        i = order.index(layer)
        refs = {order[j]: fills[order[j]] for j in (i - 1, i + 1) if 0 <= j < len(order)}
        paths: list[Report] = []
        for path in _paths(tracks[layer]):
            spans = []
            offset = 0.0
            for track, origin in path:
                pieces, length = _spans(track, origin, offset, refs, estimate, name)
                spans.extend(pieces)
                offset += length
            last, origin = path[-1]
            paths.append(
                {
                    "start_xy_mm": path[0][1],
                    "end_xy_mm": last.end if last.start == origin else last.start,
                    "length_mm": round(offset, 9),
                    "spans": spans,
                }
            )
        layers[layer] = {
            "length_mm": round(sum(path["length_mm"] for path in paths), 9),
            "widths_mm": sorted({t.width for t in tracks[layer]}),
            "via_count": via_counts[layer],
            "references": {
                side: _reference_summary(side, paths, region is not None)
                for side, region in refs.items()
            },
            "paths": paths,
        }
    return {
        "length_mm": round(sum(layer["length_mm"] for layer in layers.values()), 9),
        "via_count": len(vias),
        "layers": layers,
    }


def _delta(first: Report, second: Report) -> Report:
    """Subtract comparable numeric measurements; preserve unavailable measurements."""

    def diff(a: Report, b: Report, key: str) -> float | None:
        left, right = a.get(key, 0), b.get(key, 0)
        return None if left is None or right is None else round(right - left, 9)

    result: Report = {k: diff(first, second, k) for k in ("length_mm", "via_count")}
    result["layers"] = {}
    for name in first["layers"].keys() | second["layers"].keys():
        a, b = (n["layers"].get(name, {}) for n in (first, second))
        row: Report = {k: diff(a, b, k) for k in ("length_mm", "via_count")}
        old_widths, new_widths = (set(layer.get("widths_mm", [])) for layer in (a, b))
        row["widths_added_mm"] = sorted(new_widths - old_widths)
        row["widths_removed_mm"] = sorted(old_widths - new_widths)
        refs = {}
        for side in a.get("references", {}).keys() | b.get("references", {}).keys():
            x, y = (layer.get("references", {}).get(side, {}) for layer in (a, b))
            changes: Report = {k: diff(x, y, k) for k in ("covered_mm", "uncovered_mm")}
            xn, yn = x.get("by_net_mm", {}), y.get("by_net_mm", {})
            changes["by_net_mm"] = (
                None
                if xn is None or yn is None
                else {net: diff(xn, yn, net) for net in xn.keys() | yn.keys()}
            )
            refs[side] = changes
        result["layers"][name] = {**row, "references": refs}
    return result


def reference_coverage(pcb: PCB, names: Sequence[str], *, compare: bool = False) -> Report:
    """Inspect saved fill without refilling. Pair deltas are second minus first."""
    if not names or (compare and len(names) != 2):
        raise ValueError("Select one or more nets, or exactly two nets for a comparison")
    stack = Stackup.from_pcb(pcb)
    if stack.has_explicit_data:
        order = [layer.name for layer in stack.copper_layers]
    else:
        enabled = {
            layer.name
            for layer in pcb.layers.values()
            if is_declared_copper_layer(layer.name, layer.type)
        }
        order = [layer for layer in COPPER_LAYER_ORDER if layer in enabled]
    fills = _reference_fills(pcb, order)
    estimate = _estimator(stack)
    nets = {name: _net_report(pcb, name, order, fills, estimate) for name in sorted(set(names))}
    report: Report = {
        "nets": nets,
        "layer_order": order,
        "distance_basis": "mm from start_xy_mm along each endpoint-connected path on its layer; XY is board-relative",
        "estimate_basis": "Ideal continuous adjacent reference on supported spans with explicit single-dielectric gaps",
        "unmodeled": [
            "incomplete coverage",
            "vias",
            "pads",
            "fringing",
            "coupling to switching copper",
        ],
    }
    if compare:
        first, second = names
        report["pair"] = {
            "delta_basis": "second minus first",
            "first": first,
            "second": second,
            "delta": _delta(nets[first], nets[second]),
        }
    return report
