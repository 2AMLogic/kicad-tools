"""A bounded off-pad breakout when preserved copper traps a two-terminal search."""

from __future__ import annotations

import math
import time
from dataclasses import replace
from itertools import pairwise
from typing import TYPE_CHECKING

from . import wall_deadline
from .escape import EscapeDirection, EscapeRoute, EscapeRouter, escape_endpoint_pad
from .kelvin_escape import kelvin_access_candidate_clear
from .layers import Layer
from .pad_geometry import pad_point_distance
from .primitives import Pad, Route, Segment
from .quantize import dogleg_points
from .rule_area_grid import escape_copper_rule_area_hit

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .core import Autorouter
    from .kelvin_escape import EdgeSegments
    from .pathfinder import Router


def find_departure(
    escape: EscapeRouter,
    router: Router,
    source: Pad,
    pads: list[Pad],
    landing: Layer,
    edges: EdgeSegments,
    edge_clearance: float,
    deadline: float | None = None,
) -> EscapeRoute | None:
    """Find the nearest physically clear, on-grid barrel with a 45-degree stub."""
    grid, rules = escape.grid, escape.rules
    net_class = escape.net_class_map.get(source.net_name) if escape.net_class_map else None
    diameter = net_class.via_size if net_class else rules.via_diameter
    width = escape._get_trace_width_for_net(source.net_name)
    clearance = max(
        rules.via_clearance, rules.trace_clearance, net_class.clearance if net_class else 0
    )
    # This is a pad-local breakout, bounded even on very fine grids. Leave
    # long detours and the connection across the board to the regular A*.
    reach = max(source.width, source.height) / 2 + 2 * diameter + clearance
    radius = min(32, math.ceil(reach / grid.resolution))
    gx, gy = grid.world_to_grid(source.x, source.y)
    sites = []
    for row in range(max(0, gy - radius), min(grid.rows, gy + radius + 1)):
        for col in range(max(0, gx - radius), min(grid.cols, gx + radius + 1)):
            x, y = grid.grid_to_world(col, row)
            distance = math.hypot(x - source.x, y - source.y)
            if distance <= reach and pad_point_distance(source, x, y) >= diameter / 2 + clearance:
                sites.append((distance, x, y))

    for _, x, y in sorted(sites):
        if wall_deadline.expired() or (deadline is not None and time.monotonic() >= deadline):
            return None
        col, row = grid.world_to_grid(x, y)
        if (
            not router._component_hole_clear_cached(col, row)
            or not router._can_place_via_in_zones(col, row, source.net)
            or router._is_via_in_exclusion_zone(col, row)
        ):
            continue
        via = escape._make_escape_via(
            x=x,
            y=y,
            drill=rules.via_drill,
            diameter=diameter,
            layers=(source.layer, landing),
            net=source.net,
            net_name=source.net_name,
        )
        for axis_first in (False, True):
            points = dogleg_points(source.x, source.y, x, y, axis_first=axis_first)
            if axis_first and len(points) == 2:
                continue
            segments = [
                Segment(a[0], a[1], b[0], b[1], width, source.layer, source.net, source.net_name)
                for a, b in pairwise(points)
            ]
            candidate = EscapeRoute(
                source,
                EscapeDirection.VIA_DOWN,
                (x, y),
                landing,
                (x, y),
                segments,
                via,
                requires_main_route=True,
            )
            # The existing escape validator checks the entire barrel, every
            # physical pad and drill, fixed fills, authored floors and edges.
            # Its own-net isolation is conservative for this new terminal stub.
            if (
                kelvin_access_candidate_clear(
                    escape, candidate, (), pads, clearance, edges, edge_clearance
                )
                and not escape._via_in_pad_edge_zone(x, y, via.drill, pads)
                and not escape_copper_rule_area_hit(grid, segments, (via,), source.net, rules)
                and _raster_obstacles_clear(escape, candidate, clearance)
            ):
                return candidate
    return None


def _raster_obstacles_clear(escape: EscapeRouter, candidate: EscapeRoute, clearance: float) -> bool:
    """Refuse keepouts and other hard raster obstacles without physical geometry."""
    grid = escape.grid
    via = candidate.via
    assert via is not None

    def blocked(
        x1: float, y1: float, x2: float, y2: float, radius: float, layers: Iterable[int]
    ) -> bool:
        margin = radius + clearance
        left, top = grid.world_to_grid(min(x1, x2) - margin, min(y1, y2) - margin)
        right, bottom = grid.world_to_grid(max(x1, x2) + margin, max(y1, y2) + margin)
        if left < 0 or top < 0 or right >= grid.cols or bottom >= grid.rows:
            return True
        return any(
            grid.raster_only_blocked_cell(x, y, layer)
            for layer in layers
            for y in range(top, bottom + 1)
            for x in range(left, right + 1)
        )

    if blocked(via.x, via.y, via.x, via.y, via.diameter / 2, range(grid.num_layers)):
        return False
    return not any(
        blocked(s.x1, s.y1, s.x2, s.y2, s.width / 2, (grid.layer_to_index(s.layer.value),))
        for s in candidate.segments
    )


def retry_preserved_departure(
    owner: Autorouter,
    source: Pad,
    target: Pad,
    deadline: float | None,
) -> Route | None:
    """One retry between a new source breakout and an existing target barrel."""
    from .pathfinder import Router

    router = owner.router
    if source.escape_terminal:
        source, target = target, source
    if (
        not isinstance(router, Router)
        or not router.last_route_all_vias_blocked
        or router.relief_partner_drills
        or not router.diagonal_routing
        or source.steiner_point
        or source.layer_agnostic
        or source.through_hole
        or source.escape_terminal
        or not target.escape_terminal
        or source.layer not in (Layer.F_CU, Layer.B_CU)
    ):
        return None
    landing = Layer.B_CU if source.layer == Layer.F_CU else Layer.F_CU
    # The virtual target for a pad-to-barrel escape normally names only its
    # landing layer. Reuse its far surface only when a real through via spans it.
    target_barrel = next(
        (
            via
            for route in owner.grid.routes
            if route.net == source.net
            for via in route.vias
            if not via.is_micro
            and via.span_type is None
            and via.x == target.x
            and via.y == target.y
        ),
        None,
    )
    if (
        target_barrel is None
        or owner.grid.layer_to_index(landing.value) not in owner.grid.get_routable_indices()
    ):
        return None
    candidate = find_departure(
        owner._escape,
        router,
        source,
        owner.all_pads or list(owner.pads.values()),
        landing,
        owner._edge_segments or (),
        owner._edge_clearance or 0.0,
        deadline,
    )
    if candidate is None or candidate.via is None:
        return None
    remaining = None if deadline is None else deadline - time.monotonic()
    if wall_deadline.expired() or (remaining is not None and remaining <= 0):
        return None
    endpoint = escape_endpoint_pad(source, candidate, fallback_width=candidate.segments[0].width)
    diameter = target_barrel.diameter / math.sqrt(2)
    goal = replace(
        target,
        layer=landing,
        width=min(target.width, diameter),
        height=min(target.height, diameter),
        rotation=0.0,
        shape="rect",
    )
    path = router.route(endpoint, goal, per_net_timeout=remaining)
    if path is not None:
        # Return the breakout with the complete route. The caller owns the
        # commit after the search has succeeded.
        path.segments[:0] = candidate.segments
        path.vias.insert(0, candidate.via)
    return path
