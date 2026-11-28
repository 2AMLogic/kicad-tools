"""Same-net routes share copper once (Issue #9424).

Several connections of one net can leave the same pad.  The pathfinder
routes each on its own, so the shared escape leg is drawn once per
connection, and the trace optimizer's pad-entry straightening and via
minimisation then align those copies on one line -- the copper ``kct
check`` reports as ``redundant_copper_covered`` (8 warnings on board 05's
unattended seed-42 route), with a 45-degree branch off the shared trunk
that ``sharp_trace_corner`` reports as an acute 3-way junction.

The geometry below is that route's RESET pad U1.29 at (160.1, 95.838)
translated to (10, 10): three connections leave the pad north along its
axis, one turns north-west, one continues to R5.2 and the third turns
45 degrees to its via.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.sibling import (
    plan_same_net_reconcile,
    reconcile_same_net_routes,
)
from kicad_tools.router.primitives import Pad, Route, Segment, Via

NET = 7
NAME = "RESET"
PAD = (10.0, 10.0)
SPLIT = (10.77, 8.447)  # the via the 45-degree branch ends in
FOOT = (10.0, 8.447)  # its perpendicular foot on the trunk


def _seg(a, b, width=0.2, net=NET, name=NAME, layer=Layer.F_CU) -> Segment:
    return Segment(
        x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=width, layer=layer, net=net, net_name=name
    )


def _via(p, net=NET, name=NAME, diameter=0.6) -> Via:
    return Via(
        x=p[0],
        y=p[1],
        drill=0.3,
        diameter=diameter,
        layers=(Layer.F_CU, Layer.B_CU),
        net=net,
        net_name=name,
    )


def _route(segments, vias=(), net=NET, name=NAME) -> Route:
    return Route(net=net, net_name=name, segments=list(segments), vias=list(vias))


def _coords(segments) -> list[tuple[float, float, float, float]]:
    return [(round(s.x1, 3), round(s.y1, 3), round(s.x2, 3), round(s.y2, 3)) for s in segments]


def _pad_route() -> Route:
    """U1.29 -> R5.2: the trunk the other two connections run along."""
    return _route([_seg(PAD, (10.0, 4.474)), _seg((10.0, 4.474), (11.312, 3.162))])


def _west_route() -> Route:
    """U1.29 -> U3.3: leaves the trunk 0.698 mm from the pad centre."""
    return _route([_seg(PAD, (10.0, 9.302)), _seg((10.0, 9.302), (9.0, 8.302))])


def _via_route() -> Route:
    """U1.29 -> its via: the 45-degree branch that makes the acute junction."""
    return _route([_seg(PAD, (10.0, 9.217)), _seg((10.0, 9.217), SPLIT)], vias=[_via(SPLIT)])


def _segments(plan, index: int, route: Route) -> list[Segment]:
    return plan.rewrites.get(index, route.segments)


#: U1.29 itself: 0.55 x 1.475 mm, so its inscribed circle stops 0.275 mm from
#: the centre and the junction 0.783 mm up the trunk is not under it.
_PAD = Pad(x=PAD[0], y=PAD[1], width=0.55, height=1.475, net=NET, net_name=NAME, layer=Layer.F_CU)


def _plan(routes, pads=None, **kwargs):
    return plan_same_net_reconcile(routes, pads=[_PAD] if pads is None else pads, **kwargs)


class TestCoveredLegIsKeptOnce:
    def test_trunk_takes_the_shared_pad_escape(self):
        trunk, west, via = _pad_route(), _west_route(), _via_route()
        plan = _plan([trunk, west, via])

        # The longest leg keeps the whole escape; the copies inside it go.
        assert _segments(plan, 0, trunk) == trunk.segments
        assert _coords(_segments(plan, 1, west)) == [(10.0, 9.302, 9.0, 8.302)]
        assert _coords(_segments(plan, 2, via)) == [(10.0, 9.217, *SPLIT)]
        assert (plan.dropped, plan.trimmed, plan.squared) == (2, 0, 0)

    def test_order_of_the_routes_does_not_decide_the_keeper(self):
        trunk, west, via = _pad_route(), _west_route(), _via_route()
        plan = _plan([via, west, trunk])

        assert _segments(plan, 2, trunk) == trunk.segments
        assert len(_segments(plan, 0, via)) == 1
        assert len(_segments(plan, 1, west)) == 1

    def test_a_narrower_leg_is_not_covered(self):
        trunk = _pad_route()
        thick = _route(
            [_seg(PAD, (10.0, 9.302), width=0.5), _seg((10.0, 9.302), (9.0, 8.302), width=0.5)]
        )
        plan = _plan([trunk, thick])

        # Dropping the wider copy would remove copper.
        assert plan.rewrites == {}

    def test_another_net_or_layer_is_not_a_keeper(self):
        trunk = _pad_route()
        other_net = _west_route()
        for seg in other_net.segments:
            seg.net, seg.net_name = 8, "RUN"
        other_layer = _route([_seg(PAD, (10.0, 9.302), layer=Layer.B_CU)])
        plan = _plan([trunk, other_net, other_layer])

        assert plan.rewrites == {}

    def test_equal_length_copies_are_the_duplicate_dedupe_s_business(self):
        first = _route([_seg(PAD, (10.0, 9.302))])
        second = _route([_seg(PAD, (10.0, 9.302))])
        plan = _plan([first, second])

        assert plan.rewrites == {}

    def test_fixed_copper_shelters_a_route_leg_and_is_never_rewritten(self):
        fixed = _route([_seg(PAD, (10.0, 4.474))])
        west = _west_route()
        plan = _plan([west], fixed=[fixed])

        assert _coords(plan.rewrites[0]) == [(10.0, 9.302, 9.0, 8.302)]
        assert fixed.segments[0].x2 == PAD[0] and fixed.segments[0].y2 == 4.474

    def test_a_wider_leg_over_narrower_fixed_copper_stays(self):
        fixed = _route([_seg(PAD, (10.0, 9.302))])
        thick = _route(
            [_seg(PAD, (10.0, 9.302), width=0.5), _seg((10.0, 9.302), (9.0, 8.302), width=0.5)]
        )
        plan = _plan([thick], fixed=[fixed])

        # Dropping the wider copy would remove copper.
        assert plan.rewrites == {}

    def test_a_skipped_net_passes_through_untouched(self):
        trunk, west = _pad_route(), _west_route()
        plan = _plan([trunk, west], skip_nets={NET})

        assert plan.rewrites == {}


class TestDeadTipBeyondAVia:
    """The board's RESET B.Cu pair at (170.49, 75.305): a 1.365 mm leg under a
    9.36 mm leg that runs on through a via to the short leg's free end."""

    def _routes(self):
        short = _route(
            [
                _seg((10.0, 5.0), (10.0, 6.365), layer=Layer.B_CU),
                _seg((10.0, 6.365), (10.0, 15.805)),
            ],
            vias=[_via((10.0, 6.365))],
        )
        long_ = _route([_seg((10.0, 14.36), (10.0, 5.0), layer=Layer.B_CU)])
        return short, long_

    def test_the_long_leg_stops_at_the_via(self):
        short, long_ = self._routes()
        plan = _plan([short, long_])

        assert _coords(_segments(plan, 0, short)) == [(10.0, 6.365, 10.0, 15.805)]
        assert _coords(_segments(plan, 1, long_)) == [(10.0, 14.36, 10.0, 6.365)]
        assert (plan.dropped, plan.trimmed) == (1, 1)

    def test_without_a_via_to_stop_at_the_pair_stays(self):
        short, long_ = self._routes()
        short.vias.clear()
        plan = _plan([short, long_])

        assert plan.rewrites == {}

    def test_a_shared_end_on_a_pad_needs_no_trim(self):
        short, long_ = self._routes()
        short.vias.clear()
        pad = Pad(x=10.0, y=5.0, width=1.0, height=1.0, net=NET, net_name=NAME, layer=Layer.B_CU)
        plan = _plan([short, long_], pads=[pad])

        assert _segments(plan, 0, short) == short.segments[1:]
        assert _segments(plan, 1, long_) == long_.segments

    def test_a_branch_joined_to_the_tail_keeps_it(self):
        short, long_ = self._routes()
        join = _route([_seg((10.0, 5.6), (12.0, 5.6), layer=Layer.B_CU)])
        plan = _plan([short, long_, join])

        assert plan.rewrites == {}

    def test_a_fixed_keeper_cannot_be_trimmed_so_nothing_is_dropped(self):
        short, _ = self._routes()
        fixed = _route([_seg((10.0, 14.36), (10.0, 5.0), layer=Layer.B_CU)])
        plan = _plan([short], fixed=[fixed])

        assert plan.rewrites == {}


class TestAcuteBranchEntersItsViaSquarely:
    def test_the_diagonal_becomes_a_perpendicular_entry(self):
        trunk, via = _pad_route(), _via_route()
        plan = _plan([trunk, via], validate=lambda old, new, kept: True)

        assert _coords(_segments(plan, 1, via)) == [(FOOT[0], FOOT[1], SPLIT[0], SPLIT[1])]
        assert plan.squared == 1
        assert via.vias == [_via(SPLIT)]

    def test_the_trunk_is_untouched(self):
        trunk, via = _pad_route(), _via_route()
        plan = _plan([trunk, via], validate=lambda old, new, kept: True)

        assert _segments(plan, 0, trunk) == trunk.segments

    def test_without_the_clearance_gates_the_diagonal_stays(self):
        trunk, via = _pad_route(), _via_route()
        plan = _plan([trunk, via])

        assert _coords(_segments(plan, 1, via)) == [(10.0, 9.217, *SPLIT)]
        assert plan.squared == 0

    def test_a_vetoed_entry_stays_a_diagonal(self):
        trunk, via = _pad_route(), _via_route()
        seen: list[tuple[Segment, Segment]] = []

        def veto(old, new, kept):
            seen.append((old, new))
            return False

        plan = _plan([trunk, via], validate=veto)

        assert _coords(_segments(plan, 1, via)) == [(10.0, 9.217, *SPLIT)]
        assert seen[0][0] is via.segments[1]
        assert (seen[0][1].start, seen[0][1].end) == (FOOT, SPLIT)

    def test_the_validator_sees_the_trunk_as_kept_copper(self):
        trunk, via = _pad_route(), _via_route()
        kept: list[list[Segment]] = []
        plan = _plan([trunk, via], validate=lambda old, new, keep: kept.append(list(keep)) or True)

        assert plan.squared == 1
        assert [s.start for s in kept[0]] == [PAD, (10.0, 4.474)]

    def test_a_branch_that_already_leaves_squarely_is_left_alone(self):
        trunk = _pad_route()
        square = _route(
            [_seg((10.0, 6.0), (12.0, 6.0)), _seg((12.0, 6.0), (14.0, 6.0))],
            vias=[_via((14.0, 6.0))],
        )
        # The branch starts on the trunk and runs east from there.
        square.segments[0] = _seg(FOOT, (12.0, FOOT[1]))
        square.segments[1] = _seg((12.0, FOOT[1]), (14.0, FOOT[1]))
        plan = _plan([trunk, square], validate=lambda old, new, kept: True)

        assert _segments(plan, 1, square) == square.segments

    def test_a_junction_under_pad_copper_is_not_reported_so_not_rewritten(self):
        trunk, via = _pad_route(), _via_route()
        pad = Pad(x=10.0, y=9.0, width=1.0, height=1.0, net=NET, net_name=NAME, layer=Layer.F_CU)
        plan = _plan([trunk, via], pads=[_PAD, pad], validate=lambda old, new, kept: True)

        assert _coords(_segments(plan, 1, via)) == [(10.0, 9.217, *SPLIT)]
        assert plan.squared == 0

    def test_a_third_branch_on_the_diagonal_body_refuses_the_rewrite(self):
        trunk, via = _pad_route(), _via_route()
        # A stub that meets the diagonal half-way and nothing else.
        mid = (10.25, 8.967)
        stub = _route([_seg(mid, (12.25, 8.967))])
        plan = _plan([trunk, via, stub], validate=lambda old, new, kept: True)

        assert _coords(_segments(plan, 1, via)) == [(10.0, 9.217, *SPLIT)]
        assert plan.squared == 0

    def test_a_foot_off_the_trunk_refuses_the_rewrite(self):
        trunk = _route([_seg(PAD, (10.0, 8.6))])  # ends above the via's level
        via = _via_route()
        plan = _plan([trunk, via], validate=lambda old, new, kept: True)

        assert _coords(_segments(plan, 1, via)) == [(10.0, 9.217, *SPLIT)]


class TestReconcileRoutesOnTheRouter:
    def _router(self) -> Autorouter:
        router = Autorouter(width=40.0, height=40.0)
        router.add_component(
            "U1",
            [
                {
                    "number": "29",
                    "x": PAD[0],
                    "y": PAD[1],
                    "width": _PAD.width,
                    "height": _PAD.height,
                    "net": NET,
                    "net_name": NAME,
                    "layer": Layer.F_CU,
                }
            ],
        )
        for route in (_pad_route(), _west_route(), _via_route()):
            router.grid.mark_route(route)
            router.routes.append(route)
        return router

    @staticmethod
    def _geometries(routes) -> list[tuple[int, tuple]]:
        return sorted((r.net, tuple((s.x1, s.y1, s.x2, s.y2) for s in r.segments)) for r in routes)

    def test_routes_and_grid_agree_after_the_pass(self):
        router = self._router()
        trunk = router.routes[0]

        plan = reconcile_same_net_routes(router)

        assert plan.dropped == 2
        assert len(router.routes[1].segments) == 1
        assert len(router.routes[2].segments) == 1
        assert router.routes[0] is trunk
        assert self._geometries(router.routes) == self._geometries(router.grid.routes)
        for route in router.routes:
            for seg in route.segments:
                cells = router.grid._get_segment_cells(seg)
                assert cells, "a kept segment has no marked cell"
                for gx, gy, layer_idx in cells:
                    assert router.grid.grid[layer_idx][gy][gx].blocked

    def test_nothing_to_do_leaves_every_route_object_alone(self):
        router = self._router()
        before = list(router.routes)

        plan = reconcile_same_net_routes(router, skip_nets={NET})

        assert plan.rewrites == {}
        assert router.routes == before
