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

    from kicad_tools.schema.pcb import PCB

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


def authored_arc_copper(
    pcb: PCB, net_map: dict[str, int], excluded_nets: frozenset[str] = frozenset()
) -> dict[str, list[tuple[int, BaseGeometry | None]]]:
    """Collect retained track arcs in the sheet coordinates used by the routing grid."""
    from shapely.affinity import translate  # type: ignore[import-untyped]

    from kicad_tools.validate.rules.physical_gap import track_arc_copper_geometry

    by_layer: dict[str, list[tuple[int, BaseGeometry | None]]] = {}
    for node in pcb._sexp.find_children("arc"):
        layer_node = node.find_child("layer")
        layer = layer_node.get_string(0) if layer_node else None
        if not layer:
            continue
        net_node = node.find_child("net")
        value = net_node.get_first_atom() if net_node else 0
        net = value if isinstance(value, int) else net_map.get(str(value), 0)
        if excluded_nets:
            declared = pcb.get_net(value) if isinstance(value, int) else None
            if (declared.name if declared else str(value)) in excluded_nets:
                continue
        try:
            copper = track_arc_copper_geometry(node, pcb.board_origin)
            copper = translate(copper, *pcb.board_origin)
        except (TypeError, ValueError):
            # A curve the fabrication check cannot measure cannot prove this edit safe.
            copper = None
        by_layer.setdefault(layer, []).append((net, copper))
    return by_layer


def authored_footprint_poly_copper(pcb: PCB) -> dict[str, list[BaseGeometry | None]]:
    """Collect footprint copper polygons in the sheet coordinates used by the routing grid.

    The fabrication gap check grades every ``fp_poly`` on a copper layer (a net tie joins
    its pads with exactly this primitive, issue #5817), but the router grid models none of
    them. A polygon the check cannot measure maps to ``None`` so callers fail closed.
    """
    from shapely.affinity import translate

    from kicad_tools.validate.rules.physical_gap import (
        _footprint_transform,
        _fp_poly_geometry,
    )

    copper_layers = {layer.name for layer in pcb.copper_layers}
    by_layer: dict[str, list[BaseGeometry | None]] = {}
    for footprint in pcb.footprints:
        transform = _footprint_transform(footprint)
        for graphic in footprint.graphics:
            if graphic.graphic_type != "poly" or graphic.layer not in copper_layers:
                continue
            try:
                copper = _fp_poly_geometry(graphic, transform)
                if copper is not None and not copper.is_empty:
                    copper = translate(copper, *pcb.board_origin)
                else:
                    copper = None
            except (TypeError, ValueError):
                copper = None
            by_layer.setdefault(graphic.layer, []).append(copper)
    return by_layer


def _band_edge(grid: RoutingGrid | None, attr: str) -> float | None:
    edge = getattr(getattr(grid, "rules", None), attr, None)
    if (
        isinstance(edge, bool)
        or not isinstance(edge, (int, float))
        or not math.isfinite(edge)
        or edge <= 0
    ):
        return None
    return float(edge)


def covered_via_elbow(
    segments: list[Segment], route: Route, grid: RoutingGrid | None
) -> list[Segment] | None:
    """Retract a slitted diagonal onto a trunk and via that already carry the elbow."""
    if grid is None or len(segments) != 2 or not route.vias:
        return None
    trace_edge = _band_edge(grid, "same_net_trace_gap_band_edge_mm")
    pad_edge = _band_edge(grid, "same_net_pad_gap_band_edge_mm")
    active_edges = [edge for edge in (trace_edge, pad_edge) if edge is not None]
    first, second = segments
    if (
        not active_edges
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

    other_routes = [other for other in grid.routes if other is not route]
    siblings = [other for other in other_routes if other.net == route.net]
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

    scan_edge = max(active_edges)
    diagonal = copper(arm)
    window = diagonal.envelope.buffer(scan_edge)
    search_window = window.buffer(scan_edge)
    layer, net = run.layer.name, str(run.net)
    local: list[CopperSource] = []

    def add(shape, source_net: int | str, kind: str) -> None:
        if shape is not None and shape.intersects(search_window):
            local.append(CopperSource(shape, layer, str(source_net), f"{kind}:{len(local)}", kind))

    for other in other_routes:
        for s in other.segments:
            if s.layer == run.layer:
                add(copper(s), s.net, "track")
    for other in [route, *other_routes]:
        for v in other.vias:
            if copper_span_contains(*v.drilled_layers, run.layer):
                add(Point(v.x, v.y).buffer(v.diameter / 2, quad_segs=64), v.net, "via")
    for pad in grid.pads:
        if (
            not pad.steiner_point
            and not (not pad.through_hole and getattr(pad, "drill", 0.0) > 0)
            and (pad.through_hole or pad.layer == run.layer)
        ):
            add(_pad_copper(pad), pad.net, "pad")

    for source_net, shape in getattr(grid, "authored_arc_copper", {}).get(run.layer.kicad_name, ()):
        if shape is None:
            return None
        add(shape, source_net, "arc")

    # Footprint copper polygons are graded under the trace band with no net (as the
    # fabrication check records them), so they are modeled the same way here.
    for shape in getattr(grid, "authored_footprint_poly_copper", {}).get(run.layer.kicad_name, ()):
        if shape is None:
            return None
        add(shape, "", "poly")

    # A third branch can join the protruding arm. Removing its only contact
    # would disconnect it even when both endpoints of this route survive.
    if any(
        (s.net == net or s.kind == "poly")
        and s.geometry.intersects(diagonal)
        and not s.geometry.intersects(stationary)
        for s in local
    ):
        return None

    def violates_policy(gap) -> bool:
        limit = pad_edge if "pad" in gap.kinds else trace_edge
        return limit is not None and gap.distance < limit

    previous = [*local, CopperSource(diagonal, layer, net, "diagonal", "track")]
    previous_gaps = list(iter_source_gaps(previous, scan_edge))
    if not any(
        "diagonal" in gap.ids and gap.nets == (net,) and violates_policy(gap)
        for gap in previous_gaps
    ):
        return None
    prior_worst: dict[tuple[str, ...], float] = {}
    for gap in previous_gaps:
        if "diagonal" not in gap.ids and violates_policy(gap):
            prior_worst[gap.ids] = min(prior_worst.get(gap.ids, math.inf), gap.distance)
    # The replacement is wholly covered. Its copper union equals the stationary
    # copper alone; check any newly exposed facing edges around the removed arm.
    if any(
        violates_policy(gap)
        and (window.intersects(Point(gap.a)) or window.intersects(Point(gap.b)))
        and gap.distance < prior_worst.get(gap.ids, math.inf) - 1e-6
        for gap in iter_source_gaps(local, scan_edge)
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


def adds_trace_gap(
    original: list[Segment], candidate: list[Segment], route: Route, grid: RoutingGrid | None
) -> bool:
    """True if *candidate* opens a trace-band slit to other copper that *original* did not.

    Grades the facing edges of the candidate chain against every other route's
    copper on its layer (the retained sibling branches), the way the fabrication
    gap check does. A slit the incoming chain already had does not count, so the
    caller only falls back to *original* when that is strictly cleaner (issue #10164).
    """
    edge = _band_edge(grid, "same_net_trace_gap_band_edge_mm") if grid is not None else None
    if grid is None or edge is None or not candidate or candidate == original:
        return False

    from shapely.geometry import LineString

    from kicad_tools.validate.rules.physical_gap import CopperSource, iter_source_gaps

    first = candidate[0]
    layer, net = first.layer.name, str(first.net)

    def copper(s: Segment):
        return LineString([s.start, s.end]).buffer(s.width / 2, quad_segs=64)

    others = [
        CopperSource(copper(s), layer, str(s.net), f"other:{i}", "track")
        for i, s in enumerate(
            s
            for r in grid.routes
            if r is not route and r.net == route.net
            for s in r.segments
            if s.layer == first.layer
        )
    ]
    if not others:
        return False

    kept = {_leg(s) for s in original}

    def worst(chain: list[Segment]) -> float:
        # Only legs the rewrite changed are graded: a join the incoming chain
        # already makes (a pad or via shoulder) is not modeled here.
        own = [
            CopperSource(copper(s), layer, net, f"own:{i}", "track") for i, s in enumerate(chain)
        ]
        changed = {f"own:{i}" for i, s in enumerate(chain) if _leg(s) not in kept}
        gaps = [
            gap.distance
            for gap in iter_source_gaps([*others, *own], edge)
            if gap.distance < edge
            and changed.intersection(gap.ids)
            and any(i.startswith("other:") for i in gap.ids)
        ]
        return min(gaps, default=math.inf)

    return worst(candidate) < edge


_MASK_UNCERTAINTY_MM = 0.0101

_MASK_ENTRY_PAD_KINDS = frozenset({"pad:rect", "pad:roundrect", "pad:circle", "pad:oval"})


def mask_opening_check(
    grid: RoutingGrid | None, clearance: float, mask_margin: float = 0.0
) -> ReplacementCheck | None:
    """Decline a rewrite that adds same-net copper beside a pad's mask opening.

    ``kct check --mfr`` judges every track segment against each mask opening of
    its own net (Issue #9424): copper clear of the opening but nearer than the
    fab's mask-to-copper *clearance* is a disjoint pass-through, and copper
    over the opening is clean only as one entry -- an authored end inside the
    pad and no more copper beside the opening than one steep crossing leaves
    (:func:`kicad_tools.validate.mask_copper.clean_own_pad_entry`).  A shortcut
    that parks its bend just outside the pad it enters fails both ways: the
    bending leg ends outside the pad, so its rounded corner is copper beside
    the opening (0.02 mm from U1/14 on board 05).

    The check counts such ``(segment, opening)`` defects on the chain before
    and after the rewrite and declines it only when the count grows.  Defects
    the incoming chain already carries are not charged to the rewrite, so a
    shortcut that trades one for a cleaner shape is kept.  Pads without a net
    or a standard shape and vias are not modelled.  *mask_margin* is the
    board's solder-mask expansion (0 when the board declares none); openings
    and copper are compared with the plot's construction budget
    (``_MASK_UNCERTAINTY_MM``, the one ``drc.repair_mask_copper`` judges
    undrawn copper with).
    """
    if (
        grid is None
        or isinstance(clearance, bool)
        or not isinstance(clearance, (int, float))
        or not math.isfinite(clearance)
        or clearance <= 0
    ):
        return None

    from shapely.geometry import LineString
    from shapely.strtree import STRtree

    from kicad_tools.validate.mask_copper import clean_own_pad_entry

    from ..layers import Layer
    from ..terminal_connectivity import _contact_outline

    reach = clearance + _MASK_UNCERTAINTY_MM
    openings: dict[tuple[int, Layer], list[tuple[BaseGeometry, BaseGeometry, str]]] = defaultdict(
        list
    )
    for pad in grid.pads:
        kind = f"pad:{pad.shape}"
        if pad.steiner_point or not pad.net or kind not in _MASK_ENTRY_PAD_KINDS:
            continue
        layers = [
            layer
            for name, layer in (("F.Mask", Layer.F_CU), ("B.Mask", Layer.B_CU))
            if name in pad.authored_layers or "*.Mask" in pad.authored_layers
        ]
        if not layers:
            continue
        # KiCad plots an omitted roundrect ratio as 0.25.
        copper = _contact_outline(pad, default_roundrect_ratio=0.25)
        if copper is None or copper.is_empty:
            continue
        opening = copper.buffer(mask_margin, quad_segs=64) if mask_margin else copper
        if opening.is_empty:
            continue
        for layer in layers:
            openings[pad.net, layer].append((opening, copper, kind))
    if not openings:
        return None
    trees = {key: STRtree([item[0] for item in items]) for key, items in openings.items()}

    def defects(segments: list[Segment]) -> int:
        count = 0
        for seg in segments:
            items = openings.get((seg.net, seg.layer))
            if not items:
                continue
            copper = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)]).buffer(
                seg.width / 2, quad_segs=64
            )
            near = trees[seg.net, seg.layer].query(copper, predicate="dwithin", distance=reach)
            for index in near:
                opening, pad_copper, kind = items[int(index)]
                gap = copper.distance(opening)
                if gap > 0:
                    count += gap < reach
                elif not clean_own_pad_entry(
                    opening,
                    copper,
                    pad_copper,
                    kind,
                    seg.width,
                    ((seg.x1, seg.y1), (seg.x2, seg.y2)),
                    clearance_mm=clearance,
                    uncertainty_mm=_MASK_UNCERTAINTY_MM,
                    margin_mm=mask_margin,
                ):
                    count += 1
        return count

    def replacement_is_mask_clean(
        original: list[Segment], candidate: list[Segment], kept: list[Segment]
    ) -> bool:
        if not candidate or candidate == original:
            return True
        return defects(candidate) <= defects(original)

    return replacement_is_mask_clean
