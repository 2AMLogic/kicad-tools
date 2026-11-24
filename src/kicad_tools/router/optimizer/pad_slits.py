"""Check optimizer rewrites against the fabrication gap around same-net pads and vias."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from itertools import pairwise
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
    """Guard shortcuts past vias on the current route and other routes of the same net."""
    edge = getattr(getattr(grid, "rules", None), "same_net_trace_gap_band_edge_mm", None)
    if grid is None or not isinstance(edge, (int, float)) or edge <= 0:
        return None

    from shapely.geometry import Point  # type: ignore[import-untyped]

    from kicad_tools.core.types import copper_span_contains

    siblings = [other for other in grid.routes if other is not route and other.net == route.net]
    layers = {s.layer for s in route.segments}
    barrels = defaultdict(list)
    for branch in [route, *siblings]:
        for via in branch.vias:
            copper = Point(via.x, via.y).buffer(via.diameter / 2, quad_segs=64)
            for layer in layers:
                if copper_span_contains(*via.drilled_layers, layer):
                    barrels[via.net, layer].append(copper)
    if not barrels:
        return None
    # The live grid still contains the original route. Its unchanged traces
    # come from the candidate chain and its kept segments during each check.
    background = defaultdict(list)
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


def via_exit_doglegs(
    segments: list[Segment], grid: RoutingGrid | None
) -> Iterator[tuple[list[Segment], list[Segment]]]:
    """Extend a short launch from a via before a right-angle turn and reconnect its far end."""
    from kicad_tools.core.types import copper_span_contains

    from ..quantize import dogleg_points, is_45_aligned
    from .algorithms import _reverse_path

    if grid is None or len(segments) < 2:
        return
    edge = grid.rules.same_net_trace_gap_band_edge_mm
    if edge is None or edge <= 0:
        return
    vias = [v for route in grid.routes if route.net == segments[0].net for v in route.vias]
    for reverse in (False, True):
        first, second = _reverse_path(segments[-2:]) if reverse else segments[:2]
        if first.end != second.start or first.width != second.width:
            continue
        dx, dy = first.x2 - first.x1, first.y2 - first.y1
        tx, ty = second.x2 - second.x1, second.y2 - second.y1
        length = math.hypot(dx, dy)
        if (
            not length
            or (tx == 0 and ty == 0)
            or not is_45_aligned(dx, dy)
            or not math.isclose(dx * tx + dy * ty, 0, abs_tol=1e-9)
        ):
            continue
        via = next(
            (
                v
                for v in vias
                if v.net == first.net
                and (v.x, v.y) == first.start
                and copper_span_contains(*v.drilled_layers, first.layer)
            ),
            None,
        )
        if via is None:
            continue
        # At a perpendicular turn the original launch length is also the distance
        # from the barrel center to the extended line of the returning trace.
        missing = (via.diameter + first.width) / 2 + edge - length
        if missing <= 0:
            continue
        pitch = grid.resolution
        step_length = pitch * length / max(abs(dx), abs(dy))
        scale = math.ceil(missing / step_length) * step_length / length
        bend = (first.x2 + dx * scale, first.y2 + dy * scale)
        points = [first.start, *dogleg_points(*bend, *second.end)]
        legs = [
            Segment(*a, *b, first.width, first.layer, first.net, first.net_name)
            for a, b in pairwise(points)
            if a != b
        ]
        if reverse:
            legs = _reverse_path(legs)
            yield legs, [*segments[:-2], *legs]
        else:
            yield legs, [*legs, *segments[2:]]


def covered_via_elbow(
    segments: list[Segment], route: Route, grid: RoutingGrid | None
) -> list[Segment] | None:
    """Retract a slitted diagonal onto a trunk and via that already carry the elbow."""
    if grid is None or len(segments) != 2 or not route.vias:
        return None
    edge = grid.rules.same_net_trace_gap_band_edge_mm
    first, second = segments
    if (
        edge is None
        or edge <= 0
        or first.end != second.start
        or (first.width, first.layer, first.net) != (second.width, second.layer, second.net)
    ):
        return None

    from shapely.geometry import LineString, Point

    from kicad_tools.core.types import copper_span_contains
    from kicad_tools.validate.rules.physical_gap import CopperSource, iter_source_gaps

    from .algorithms import _reverse_path

    reverse = first.x1 != first.x2 and first.y1 != first.y2
    run, arm = _reverse_path(segments) if reverse else segments
    dx, dy = abs(arm.x2 - arm.x1), abs(arm.y2 - arm.y1)
    if dx == 0 or not math.isclose(dx, dy, abs_tol=1e-8):
        return None
    via = next(
        (
            v
            for v in route.vias
            if v.net == run.net
            and (v.x, v.y) == arm.end
            and copper_span_contains(*v.drilled_layers, run.layer)
        ),
        None,
    )
    if via is None:
        return None
    if run.x1 == run.x2:
        bend = (run.x1, via.y)
    elif run.y1 == run.y2:
        bend = (via.x, run.y1)
    else:
        return None
    if bend == run.start or not 0 < math.dist(bend, arm.end) < (via.diameter + run.width) / 2:
        return None

    siblings = [other for other in grid.routes if other is not route and other.net == route.net]
    tracks = [s for other in siblings for s in other.segments if s.layer == run.layer]
    # The long leg must follow a stationary trace across its full length. The
    # short transverse leg must fit entirely inside that same trace and barrel.
    line = LineString([run.start, bend])
    if not line.covers(LineString([run.start, run.end])):
        return None
    spine = next(
        (s for s in tracks if s.width >= run.width and LineString([s.start, s.end]).covers(line)),
        None,
    )
    if spine is None:
        return None

    def copper(s: Segment):
        return LineString([s.start, s.end]).buffer(s.width / 2, quad_segs=64)

    candidate = [
        Segment(*a, *b, run.width, run.layer, run.net, run.net_name)
        for a, b in pairwise([run.start, bend, arm.end])
    ]
    barrel = Point(arm.end).buffer(via.diameter / 2, quad_segs=64)
    stationary = copper(spine).union(barrel)
    if not stationary.covers(copper(candidate[1])):
        return None

    diagonal = copper(arm)
    window = diagonal.envelope.buffer(edge)
    layer, net = run.layer.name, str(run.net)
    local: list[CopperSource] = []

    def add(shape, kind: str) -> None:
        if shape is not None and shape.intersects(window):
            local.append(CopperSource(shape, layer, net, kind, kind))

    for s in tracks:
        add(copper(s), "track")
    for other in [route, *siblings]:
        for v in other.vias:
            if copper_span_contains(*v.drilled_layers, run.layer):
                add(Point(v.x, v.y).buffer(v.diameter / 2, quad_segs=64), "via")
    for pad in grid.pads:
        if (
            pad.net == run.net
            and not pad.steiner_point
            and (pad.through_hole or pad.layer == run.layer)
        ):
            add(_pad_copper(pad), "pad")

    # A third branch can join the protruding arm. Removing its only contact
    # would disconnect it even when both endpoints of this route survive.
    if any(
        s.geometry.intersects(diagonal) and not s.geometry.intersects(stationary) for s in local
    ):
        return None
    previous = [*local, CopperSource(diagonal, layer, net, "diagonal", "track")]
    if not any(
        "diagonal" in gap.ids and "track" in gap.ids for gap in iter_source_gaps(previous, edge)
    ):
        return None
    # The replacement is wholly covered. Its copper union equals the stationary
    # copper alone; check any newly exposed facing edges around the removed arm.
    if any(
        window.intersects(Point(gap.a)) or window.intersects(Point(gap.b))
        for gap in iter_source_gaps(local, edge)
    ):
        return None
    return _reverse_path(candidate) if reverse else candidate


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
