"""Check optimizer rewrites against the fabrication gap around same-net pads and vias."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from ..primitives import Pad, Route, Segment

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]

    from ..grid import RoutingGrid
    from ..layers import Layer


ReplacementCheck = Callable[[list[Segment], list[Segment], list[Segment]], bool]


def _pad_copper(pad: Pad):
    from kicad_tools.geometry.copper import standard_pad_polygon

    from ..clearance_shapes import pad_kernel_params

    shape, ratio = pad_kernel_params(pad)
    return standard_pad_polygon(pad.x, pad.y, pad.width, pad.height, shape, pad.rotation, ratio)


def _leg(seg: Segment) -> tuple:
    """Geometric identity in a uniform-width, single-net/layer chain."""
    a, b = (seg.x1, seg.y1), (seg.x2, seg.y2)
    return (a, b, seg.width) if a <= b else (b, a, seg.width)


def pad_gap_check(grid: RoutingGrid | None) -> ReplacementCheck | None:
    """Bind the resolved pad-spacing limit and the router's stationary pad copper."""
    edge = getattr(getattr(grid, "rules", None), "same_net_pad_gap_band_edge_mm", None)
    if grid is None or not isinstance(edge, (int, float)) or edge <= 0:
        return None

    pads = defaultdict(list)
    for pad in grid.pads:
        if pad.through_hole or pad.steiner_point or not pad.net:
            continue
        copper = _pad_copper(pad)
        if copper is not None:
            pads[pad.net, pad.layer].append(copper)
    return _copper_gap_check(pads, edge, "pad")


def via_gap_check(grid: RoutingGrid | None, route: Route) -> ReplacementCheck | None:
    """Guard shortcuts past a stationary via on another route of the same net."""
    edge = getattr(getattr(grid, "rules", None), "same_net_trace_gap_band_edge_mm", None)
    if grid is None or not isinstance(edge, (int, float)) or edge <= 0:
        return None

    from shapely.geometry import Point  # type: ignore[import-untyped]

    from kicad_tools.core.types import copper_span_contains

    siblings = [other for other in grid.routes if other is not route and other.net == route.net]
    if not any(other.vias for other in siblings):
        return None
    layers = {s.layer for s in route.segments}
    barrels = defaultdict(list)
    background = defaultdict(list)
    for branch in [route, *siblings]:
        for via in branch.vias:
            copper = Point(via.x, via.y).buffer(via.diameter / 2, quad_segs=64)
            for layer in layers:
                if copper_span_contains(*via.drilled_layers, layer):
                    if branch is route:
                        background[via.net, layer].append(("via", copper))
                    else:
                        barrels[via.net, layer].append(copper)
    if not barrels:
        return None
    # The live grid still contains the original route. Only its siblings
    # and stationary pads stay in the union unchanged during the rewrite.
    for pad in grid.pads:
        if pad.steiner_point or pad.net != route.net:
            continue
        copper = _pad_copper(pad)
        if copper is not None:
            for layer in layers:
                if pad.through_hole or pad.layer == layer:
                    background[pad.net, layer].append(("pad", copper))
    return _copper_gap_check(
        barrels, edge, "via", [s for other in siblings for s in other.segments], background
    )


def _copper_gap_check(
    shapes: dict[tuple[int, Layer], list[BaseGeometry]],
    edge: float,
    kind: str,
    sibling_copper: Sequence[Segment] = (),
    background: dict[tuple[int, Layer], list[tuple[str, BaseGeometry]]] | None = None,
) -> ReplacementCheck:
    from shapely.geometry import LineString
    from shapely.strtree import STRtree  # type: ignore[import-untyped]

    from kicad_tools.validate.rules.physical_gap import CopperSource, iter_source_gaps

    trees = {key: STRtree(polygons) for key, polygons in shapes.items()}
    background = background or {}
    background_trees = {
        key: STRtree([shape for _, shape in items]) for key, items in background.items()
    }

    def replacement_is_clear(
        original: list[Segment], candidate: list[Segment], kept: list[Segment]
    ) -> bool:
        if not candidate or candidate == original:
            return True
        first = candidate[0]
        key = first.net, first.layer
        tree = trees.get(key)
        if tree is None:
            return True
        previous = {_leg(seg) for seg in original}
        changed = [
            LineString([(s.x1, s.y1), (s.x2, s.y2)]) for s in candidate if _leg(s) not in previous
        ]
        if not changed:
            return True
        # Only features within the fab band of a changed leg can gain a slit.
        _, hits = tree.query(changed, predicate="dwithin", distance=edge + first.width / 2)
        layer, net = first.layer.name, str(first.net)
        for idx in set(hits):
            target = shapes[key][idx]
            left, bottom, right, top = target.bounds
            sources = [CopperSource(target, layer, net, kind, kind)]
            nearby = background_trees.get(key)
            if nearby is not None:
                for item in nearby.query(target, predicate="dwithin", distance=edge):
                    feature, copper = background[key][item]
                    sources.append(CopperSource(copper, layer, net, "kept", feature))
            for neighbour in tree.query(target, predicate="dwithin", distance=edge):
                if neighbour != idx:
                    sources.append(
                        CopperSource(shapes[key][neighbour], layer, net, "neighbour", kind)
                    )
            for segments, is_candidate in (
                (candidate, True),
                (kept, False),
                (sibling_copper, False),
            ):
                for seg in segments:
                    reach = edge + seg.width / 2
                    if (
                        seg.layer != first.layer
                        or seg.net != first.net
                        or min(seg.x1, seg.x2) > right + reach
                        or max(seg.x1, seg.x2) < left - reach
                        or min(seg.y1, seg.y2) > top + reach
                        or max(seg.y1, seg.y2) < bottom - reach
                    ):
                        continue
                    copper = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)]).buffer(
                        seg.width / 2, quad_segs=64
                    )
                    owner = "replacement" if is_candidate and _leg(seg) not in previous else "kept"
                    sources.append(CopperSource(copper, layer, net, owner))
            # The closest point alone misses a slit beside an actual copper join.
            # Grade facing edges of the copper union the way fabrication DRC does.
            if any(
                "replacement" in gap.ids and kind in gap.ids
                for gap in iter_source_gaps(sources, edge)
            ):
                return False
        return True

    return replacement_is_clear
