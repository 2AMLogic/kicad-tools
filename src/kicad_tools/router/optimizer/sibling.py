"""Same-net routes share copper once (Issue #9424).

Several connections of one net can leave the same pad.  The pathfinder
routes every connection on its own, so each one draws the pad's escape leg,
and the trace optimizer's pad-entry straightening and via minimisation then
lay those copies on one line.  ``kct check`` reports every shorter copy as
``redundant_copper_covered``: on board 05's unattended route that was eight
of the nine warnings the nightly rule set had left, and the 45-degree branch
off the shared trunk was the ninth (``sharp_trace_corner``, a 3-way junction).

:func:`plan_same_net_reconcile` shapes a net's optimized copper into a tree
without moving any copper that carries current:

* a leg that lies wholly on a longer, no-narrower same-net leg of the same
  layer is dropped -- the copper union is unchanged, and whatever touched
  the dropped leg touches the longer one there;
* a branch that leaves a trunk toward the trunk's far side at an acute angle
  and ends in its own via enters that via perpendicular to the trunk instead,
  when the optimizer's clearance gates accept the new leg;
* a dead tip the first rule exposes -- the longer leg ran on through a via
  to the dropped leg's free end -- is cut back to that via.

:func:`reconcile_same_net_routes` applies a plan to a router through the same
grid transaction the trace optimizer uses.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Sequence

from kicad_tools.explain.checks.acid_trap import is_acute_corner_angle
from kicad_tools.pcb_prune import COVERED_TOL_MM

from ..acute_foldback import wedge_is_open
from ..primitives import Route, Segment, Via
from .algorithms import _overlaps_own_copper
from .trace import apply_route_transform_grid_synced

if TYPE_CHECKING:
    from ..layers import Layer

#: How far (mm) a point may sit from a line and still count as on it: a branch
#: end on a trunk, a via centre on a leg.  The optimizer writes grid-exact
#: coordinates, so this only absorbs float noise.
ON_LINE_TOL_MM = 1e-3

Validate = Callable[[Segment, Segment, list[Segment]], bool]
"""``(old, new, kept) -> bool``: the optimizer's clearance gates on a new leg.

*kept* is the same-net copper of the leg's layer that stays, the trunk
included, so the gates judge the new leg against everything it joins.
"""

#: Area (mm^2) below which a copper overlap is touching, not a join.
_TOUCH_AREA_MM2 = 1e-9


@dataclass
class SameNetPlan:
    """What :func:`plan_same_net_reconcile` would change.

    ``rewrites`` maps a route's position in the planned list to the segments
    it keeps; a route absent from it is unchanged.  ``dropped`` counts legs
    removed, ``squared`` acute entries rewritten and ``trimmed`` dead tips
    cut back.
    """

    rewrites: dict[int, list[Segment]] = field(default_factory=dict)
    dropped: int = 0
    trimmed: int = 0
    squared: int = 0


@dataclass
class _Leg:
    """One planned segment and what happens to it."""

    seg: Segment
    route: int  # -1: fixed copper, never rewritten
    keeper: _Leg | None = None  # the longer leg this one lies on
    drop: bool = False
    new: Segment | None = None  # the leg as it will be written, when moved

    @property
    def current(self) -> Segment:
        return self.new if self.new is not None else self.seg


def _length(seg: Segment) -> float:
    return math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)


def _point_line_dist(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Distance from *p* to the segment ``a``--``b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    len2 = dx * dx + dy * dy
    if len2 == 0.0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _covers(keeper: Segment, seg: Segment) -> bool:
    """``redundant_copper_covered``'s predicate: *seg* lies on *keeper*.

    The keeper is strictly longer, no narrower, and both ends of *seg* sit
    on its centreline within the rule's tolerance.
    """
    if keeper.width < seg.width - 1e-9:
        return False
    if _length(keeper) <= _length(seg) + COVERED_TOL_MM:
        return False
    return all(
        _point_line_dist(p, keeper.start, keeper.end) <= COVERED_TOL_MM
        for p in (seg.start, seg.end)
    )


def _spans(via: Via, layer: Layer) -> bool:
    from kicad_tools.core.types import copper_span_contains

    return copper_span_contains(*via.drilled_layers, layer)


def _buried(
    point: tuple[float, float], pads: Sequence[Any], vias: Sequence[Via], net: int, layer: Layer
) -> bool:
    """Whether same-net pad or via copper on *layer* contains *point*.

    Pads count by their inscribed circle, so a point counts as buried only
    when it is certainly under copper -- the choice ``sharp_trace_corner``
    and the fold-back repair make.
    """
    for pad in pads:
        if pad.net != net or not (pad.through_hole or pad.layer == layer):
            continue
        if math.dist(point, (pad.x, pad.y)) <= min(pad.width, pad.height) / 2:
            return True
    return any(
        via.net == net
        and _spans(via, layer)
        and math.dist(point, (via.x, via.y)) <= via.diameter / 2
        for via in vias
    )


class _Copper:
    """The same-net copper of one layer, for the contact tests.

    Every shape is the copper outline of one leg, via or pad; the contact
    tests ask whether two outlines overlap away from a shelter (a trunk, a
    via barrel) where the overlap is meant to be.
    """

    def __init__(self, legs: Sequence[_Leg], vias: Sequence[Via], pads: Sequence[Any]) -> None:
        from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

        self._Point = Point
        self._shapes: list[tuple[Any, float, str]] = []
        for leg in legs:
            half = leg.current.width / 2
            outline = LineString([leg.current.start, leg.current.end]).buffer(half, quad_segs=16)
            self._shapes.append((outline, half, "leg"))
        for via in vias:
            half = via.diameter / 2
            self._shapes.append((Point(via.x, via.y).buffer(half, quad_segs=16), half, "via"))
        for pad in pads:
            half = max(pad.width, pad.height) / 2
            self._shapes.append((Point(pad.x, pad.y).buffer(half, quad_segs=16), half, "pad"))

    def touches(self, shape: Any, shelters: Sequence[tuple[Any, float]] = ()) -> bool:
        """Whether any copper overlaps *shape* outside every shelter.

        A shelter is ``(outline, reach)``: copper that overlaps *shape* only
        within *reach* of the outline is joined there, not to *shape*.
        """
        for outline, half, _kind in self._shapes:
            if not outline.intersects(shape):
                continue
            overlap = outline.intersection(shape)
            if overlap.is_empty or overlap.area <= _TOUCH_AREA_MM2:
                continue
            covered = False
            for shelter, reach in shelters:
                if shelter.buffer(reach + half, quad_segs=16).covers(overlap):
                    covered = True
                    break
            if not covered:
                return True
        return False

    def reaches(self, point: tuple[float, float], margin: float) -> bool:
        """Whether any copper comes within *margin* of *point*."""
        probe = self._Point(*point)
        return any(outline.distance(probe) <= margin for outline, _half, _kind in self._shapes)


def _drop_covered(group: Sequence[_Leg]) -> None:
    """Mark every leg that lies on a longer kept leg of its group."""
    kept: list[_Leg] = []
    for leg in sorted(group, key=lambda item: -_length(item.seg)):
        keeper: _Leg | None = None
        if leg.route >= 0 and leg.seg.tuning_pattern is None:
            # ``kept`` runs longest first, so the first cover is the longest.
            keeper = next((item for item in kept if _covers(item.current, leg.seg)), None)
        if keeper is None:
            kept.append(leg)
        else:
            leg.drop, leg.keeper = True, keeper


def _squared_entry(
    branch: Segment,
    on: tuple[float, float],
    via_at: tuple[float, float],
    trunk: Segment,
    vias: Sequence[Via],
    pads: Sequence[Any],
) -> Segment | None:
    """The perpendicular entry replacing an acute branch into its via, or ``None``.

    *on* is the branch end that sits on *trunk*'s body and *via_at* its other
    end, the centre of a same-net via.  The replacement leaves the trunk at
    the via's perpendicular foot, so the junction there is a right angle.
    """
    t_len = _length(trunk)
    if t_len <= 0.0 or _point_line_dist(on, trunk.start, trunk.end) > ON_LINE_TOL_MM:
        return None
    ux, uy = (trunk.x2 - trunk.x1) / t_len, (trunk.y2 - trunk.y1) / t_len
    along = (on[0] - trunk.x1) * ux + (on[1] - trunk.y1) * uy
    if along <= ON_LINE_TOL_MM or t_len - along <= ON_LINE_TOL_MM:
        return None
    bx, by = via_at[0] - on[0], via_at[1] - on[1]
    b_len = math.hypot(bx, by)
    if b_len <= 0.0:
        return None
    # Signed travel from ``on`` toward the via along the trunk.  Zero is a
    # branch that already leaves squarely.
    forward = bx * ux + by * uy
    if abs(forward) <= ON_LINE_TOL_MM:
        return None
    fx, fy = (ux, uy) if forward > 0 else (-ux, -uy)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, (bx * fx + by * fy) / b_len))))
    if not is_acute_corner_angle(angle):
        return None
    foot_along = along + forward
    if foot_along <= trunk.width / 2 or t_len - foot_along <= trunk.width / 2:
        return None
    far = (trunk.x2, trunk.y2) if forward > 0 else (trunk.x1, trunk.y1)
    run = Segment(*on, *far, trunk.width, trunk.layer, trunk.net, trunk.net_name)
    if not wedge_is_open(branch, run, on, angle):
        return None
    if _buried(on, pads, vias, branch.net, branch.layer):
        return None
    foot = (trunk.x1 + foot_along * ux, trunk.y1 + foot_along * uy)
    if math.dist(foot, via_at) <= ON_LINE_TOL_MM:
        return None
    return Segment(*foot, *via_at, branch.width, branch.layer, branch.net, branch.net_name)


def _square_acute_entries(
    group: Sequence[_Leg],
    vias: Sequence[Via],
    pads: Sequence[Any],
    validate: Validate,
    plan: SameNetPlan,
) -> None:
    """Enter each acute branch's via squarely where the clearance gates allow."""
    from shapely.geometry import LineString, Point

    for leg in group:
        branch = leg.current
        if leg.drop or leg.route < 0 or branch.tuning_pattern is not None:
            continue
        for index, on in enumerate((branch.start, branch.end)):
            via_at = branch.end if index == 0 else branch.start
            via = next((v for v in vias if math.dist((v.x, v.y), via_at) <= ON_LINE_TOL_MM), None)
            if via is None:
                continue
            others = [item for item in group if item is not leg and not item.drop]
            for trunk in others:
                new = _squared_entry(branch, on, via_at, trunk.current, vias, pads)
                if new is None:
                    continue
                kept = [item.current for item in others]
                if _overlaps_own_copper([new], kept):
                    continue
                # A third branch or another via joined to the diagonal's body
                # would lose its only contact; one that only meets it on the
                # trunk or in the barrel meets the replacement's ends instead.
                body = LineString([branch.start, branch.end]).buffer(branch.width / 2, quad_segs=16)
                shelters = [
                    (LineString([trunk.current.start, trunk.current.end]), trunk.current.width / 2),
                    (Point(via.x, via.y), via.diameter / 2),
                ]
                joined = _Copper(
                    [item for item in others if item is not trunk],
                    [v for v in vias if v is not via],
                    pads,
                ).touches(body, shelters)
                if joined:
                    continue
                if validate(branch, new, kept):
                    leg.new = new
                    plan.squared += 1
                break


def _cut_dead_tips(
    group: Sequence[_Leg], vias: Sequence[Via], pads: Sequence[Any], plan: SameNetPlan
) -> None:
    """Cut a keeper back to its via where a drop would expose a dead tip."""
    from shapely.geometry import LineString, Point

    for leg in group:
        keeper = leg.keeper
        if not leg.drop or keeper is None:
            continue
        for tip in (leg.seg.start, leg.seg.end):
            current = keeper.current
            near = min(current.start, current.end, key=lambda p: math.dist(p, tip))
            if math.dist(near, tip) > ON_LINE_TOL_MM:
                continue
            far = current.end if near == current.start else current.start
            others = [
                item for item in group if item is not leg and item is not keeper and not item.drop
            ]
            net, layer = current.net, current.layer
            if _Copper(others, vias, pads).reaches(tip, ON_LINE_TOL_MM):
                continue  # the shared end touches something that stays
            if keeper.route < 0:
                leg.drop, leg.keeper = False, None  # fixed copper cannot be cut
                continue
            stop = None
            for via in vias:
                if via.net != net or not _spans(via, layer):
                    continue
                centre = (via.x, via.y)
                if _point_line_dist(centre, current.start, current.end) > ON_LINE_TOL_MM:
                    continue
                if (
                    math.dist(centre, tip) > ON_LINE_TOL_MM
                    and math.dist(centre, far) > ON_LINE_TOL_MM
                ):
                    stop = (
                        via
                        if stop is None
                        else min(stop, via, key=lambda v: math.dist((v.x, v.y), tip))
                    )
            if stop is None:
                leg.drop, leg.keeper = False, None
                continue
            tail = LineString([tip, (stop.x, stop.y)]).buffer(current.width / 2, quad_segs=16)
            joined = _Copper(others, [v for v in vias if v is not stop], pads).touches(
                tail, [(Point(stop.x, stop.y), stop.diameter / 2)]
            )
            if joined:
                leg.drop, leg.keeper = False, None
                continue
            end = (stop.x, stop.y)
            keeper.new = (
                Segment(*end, *far, current.width, layer, net, current.net_name)
                if near == current.start
                else Segment(*far, *end, current.width, layer, net, current.net_name)
            )
            plan.trimmed += 1


def plan_same_net_reconcile(
    routes: Sequence[Route],
    *,
    fixed: Sequence[Route] = (),
    pads: Sequence[Any] = (),
    skip_nets: set[int] | None = None,
    validate: Validate | None = None,
) -> SameNetPlan:
    """How the copper of *routes* becomes a tree where a net drew a leg twice.

    Args:
        routes: The routes to plan, in order; ``SameNetPlan.rewrites`` is keyed
            by position here.
        fixed: Copper that is never rewritten (``router.existing_routes``).
            It shelters route legs lying on it.
        pads: The board's pads, for the contact and burial tests.
        skip_nets: Nets whose routes pass through untouched.
        validate: The optimizer's clearance gates for a squared entry.  Without
            it no acute branch is rewritten.

    Drops and cuts only remove copper that another kept leg already covers,
    so a plan never opens a net or moves copper that carries current.
    """
    skip = frozenset(skip_nets or ())
    groups: dict[tuple[int, Layer], list[_Leg]] = defaultdict(list)
    vias: dict[int, list[Via]] = defaultdict(list)
    for route in (*fixed, *routes):
        for via in route.vias:
            if via.net:
                vias[via.net].append(via)
    for index, route in enumerate(routes):
        for seg in route.segments:
            if seg.net and seg.net not in skip and seg.width > 0:
                groups[seg.net, seg.layer].append(_Leg(seg, index))
    for route in fixed:
        for seg in route.segments:
            if seg.net and seg.width > 0:
                groups[seg.net, seg.layer].append(_Leg(seg, -1))
    pads_by_net: dict[int, list[Any]] = defaultdict(list)
    for pad in pads:
        if pad.net and not getattr(pad, "steiner_point", False):
            pads_by_net[pad.net].append(pad)

    plan = SameNetPlan()
    legs_of: dict[int, list[_Leg]] = defaultdict(list)  # route position -> its legs
    for (net, _layer), group in sorted(
        groups.items(), key=lambda kv: (kv[0][0], kv[0][1].kicad_name)
    ):
        _drop_covered(group)
        if validate is not None:
            _square_acute_entries(group, vias[net], pads_by_net[net], validate, plan)
        _cut_dead_tips(group, vias[net], pads_by_net[net], plan)
        for leg in group:
            if leg.drop:
                plan.dropped += 1
            if leg.route >= 0:
                legs_of[leg.route].append(leg)
    for index, legs in legs_of.items():
        planned = {id(leg.seg): leg for leg in legs}
        kept: list[Segment] = []
        changed = False
        for seg in routes[index].segments:
            planned_leg = planned.get(id(seg))  # a leg outside every group stays as is
            if planned_leg is not None and planned_leg.drop:
                changed = True
            elif planned_leg is not None and planned_leg.new is not None:
                changed = True
                kept.append(planned_leg.new)
            else:
                kept.append(seg)
        if changed:
            plan.rewrites[index] = kept
    return plan


def _clearance_gate(optimizer: Any, routes: Sequence[Route]) -> Validate | None:
    """The optimizer's gates on a squared entry, or ``None`` without an optimizer."""
    from .pad_slits import via_gap_check

    def validate(old: Segment, new: Segment, kept: list[Segment]) -> bool:
        if not optimizer.leg_rewrite_is_clear([old], [new], kept):
            return False
        route = next((r for r in routes if any(s is old for s in r.segments)), None)
        if route is None:
            return False
        check = via_gap_check(getattr(optimizer.collision_checker, "grid", None), route)
        return check is None or check([old], [new], kept)

    return validate


def reconcile_same_net_routes(
    router: Any, optimizer: Any = None, *, skip_nets: set[int] | None = None
) -> SameNetPlan:
    """Keep each same-net leg once on *router*, keeping its grid in sync.

    Plans with :func:`plan_same_net_reconcile` over ``router.routes`` and the
    router's fixed copper, then applies the plan through the grid-transactional
    pass the trace optimizer uses, so the grid, ``grid.routes`` and the commit
    journal see the reconciled copper.  *optimizer* supplies the clearance
    gates a squared entry must pass; without one only covered legs go.
    """
    routes = list(router.routes)
    grid = router.grid
    pads = list(getattr(grid, "pads", None) or ())
    if not pads:
        pads = list(getattr(router, "pads", {}).values())
    plan = plan_same_net_reconcile(
        routes,
        fixed=list(getattr(router, "existing_routes", None) or ()),
        pads=pads,
        skip_nets=skip_nets,
        validate=_clearance_gate(optimizer, routes) if optimizer is not None else None,
    )
    if not plan.rewrites:
        return plan
    positions = {id(route): index for index, route in enumerate(routes)}

    def transform(route: Route) -> Route:
        index = positions.get(id(route))
        segments = plan.rewrites.get(index) if index is not None else None
        return route if segments is None else replace(route, segments=list(segments))

    apply_route_transform_grid_synced(router, transform, skip_nets=skip_nets)
    return plan
