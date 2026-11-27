"""A targeted retry must check the copper that remains after its blockers are lifted."""

import pytest

from kicad_tools.router.access_ripup import RipupCycleGuard
from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _via(net, x, y):
    return Via(x, y, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=net)


def _segment(net, x1, y1, x2, y2, layer):
    return Segment(x1, y1, x2, y2, width=0.2, layer=layer, net=net)


@pytest.mark.parametrize("sibling_can_reland", [True, False])
def test_ripup_uses_live_copper_at_each_search_and_after_rollback(monkeypatch, sibling_can_reland):
    owner = Autorouter(12, 12, rules=DesignRules(grid_resolution=0.2), force_python=True)
    for net, y, layer in [(1, 3.0, Layer.F_CU), (2, 8.0, Layer.B_CU)]:
        for index, x in enumerate((2.0, 10.0)):
            owner.add_component(
                f"J{net}{index}",
                [
                    {
                        "number": "1",
                        "x": x,
                        "y": y,
                        "width": 0.6,
                        "height": 0.6,
                        "net": net,
                        "net_name": f"N{net}",
                        "layer": layer,
                    }
                ],
            )
    lifted = _via(2, 6.0, 3.0)
    stationary = _via(3, 6.0, 6.0)
    original = Route(
        net=2,
        net_name="N2",
        vias=[lifted],
        segments=[
            _segment(2, 2, 8, 6, 3, Layer.B_CU),
            _segment(2, 6, 3, 10, 8, Layer.B_CU),
        ],
    )
    other = Route(net=3, net_name="N3", vias=[stationary])
    net_routes = {2: [original], 3: [other]}
    for route in (original, other):
        owner._mark_route(route)
        owner.grid.mark_route_usage(route)
        owner.routes.append(route)

    owner._update_router_via_foreign_context(1)
    owner._update_router_segment_foreign_context(1)
    straight = Route(net=1, net_name="N1", segments=[_segment(1, 2, 3, 10, 3, Layer.F_CU)])
    crosses_stationary = Route(net=1, net_name="N1", segments=[_segment(1, 3, 6, 9, 6, Layer.F_CU)])
    assert not owner.router._validate_route_clearance(straight, exclude_net=1)

    worker = NegotiatedRouter(owner.grid, owner.router, owner.rules, owner.net_class_map)
    real_search = worker.route_net_negotiated
    searched = []

    def search(pads, *args, **kwargs):
        net = pads[0].net
        searched.append(net)
        if net == 1:
            # This failed on Board 06: a separate validator retained the removed via.
            assert owner.router._validate_route_clearance(straight, exclude_net=1)
            assert not owner.router._validate_route_clearance(crosses_stationary, exclude_net=1)
        else:
            # The new victim copper is now a foreign obstacle for its sibling.
            assert {pad[-1] for pad in owner.router._foreign_pad_tuples} == {1}
            gx, gy = owner.grid.world_to_grid(6.0, 3.0)
            assert not owner.router._check_via_placement_cached(gx, gy, net, allow_sharing=True)
            if not sibling_can_reland:
                return []
        return real_search(pads, *args, **kwargs)

    monkeypatch.setattr(worker, "route_net_negotiated", search)
    success = owner._guarded_targeted_ripup(
        worker,
        RipupCycleGuard(),
        failed_net=1,
        blocking_nets={2},
        net_routes=net_routes,
        pads_by_net={net: [owner.pads[key] for key in keys] for net, keys in owner.nets.items()},
        present_cost_factor=1.0,
        mark_route_callback=owner._mark_route,
        ripup_history={},
        max_ripups_per_net=3,
        per_net_timeout=None,
    )
    assert searched == [1, 2]
    assert success is sibling_can_reland
    assert stationary in owner.router._foreign_vias
    if success:
        assert net_routes[1] and net_routes[2]
        assert lifted not in owner.router._foreign_vias
    else:
        assert net_routes[2] == [original]
        assert lifted in owner.router._foreign_vias
        assert not owner.router._validate_route_clearance(straight, exclude_net=1)
