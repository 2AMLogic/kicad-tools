"""The trace retry must agree with native KiCad on the #9924 pad/via pairs.

Issue #9924: ``_TraceResolverTransaction.validate_committed_geometry`` checked
newly committed vias against segments, other vias and mechanical holes, but
never against foreign pads, so a trace-resolver retry could commit a via inside
a pad's clearance halo.  The two pairs pinned here are the corpus cases the
#9922 classification run measured against pinned KiCad 10.0.6 (gap verified
before and after zone refill, PCB/project hashes checked):

=========== ============= =========== ===== ==================
seed        pair          KiCad gap   rule  KiCad verdict
=========== ============= =========== ===== ==================
0           N5 - N6       0.1782 mm   0.20  Reject
1           N6 - N7       0.1933 mm   0.20  Reject
=========== ============= =========== ===== ==================

Each parametrized case drives, over the regenerated corpus geometry, the
production transaction (with rollback on rejection), the ordinary pathfinder
commit gate and the native C++ validator -- which must reject with its via-pad
violation type 8 -- in **both registration orders** (pad first, via first).
Controls: at a 0.15 mm floor all three accept both pairs (the recorded KiCad
behaviour), and moving seed 0's via radially 0.08 mm away clears the original
0.20 mm floor.  The kicad-cli verdicts themselves are the recorded native
evidence from the issue's replay, not re-run here.
"""

from dataclasses import replace
from math import hypot

import pytest

from kicad_tools.router.core import Autorouter, _TraceResolverTransaction
from kicad_tools.router.pathfinder import Router
from tests.conformance.adapters._support import (
    layer_stack_for,
    net_ids,
    pair_contexts,
    router_pad,
    router_rules,
    single_object_route,
)
from tests.conformance.adapters.grid_cpp import GridCppAdapter
from tests.conformance.adapters.grid_py import GridPyAdapter
from tests.conformance.generator import generate_case


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("pad_first", [True, False])
@pytest.mark.parametrize(
    ("seed", "floor", "shift", "reject"),
    [
        (0, 0.20, 0, True),
        (1, 0.20, 0, True),
        (0, 0.15, 0, False),
        (1, 0.15, 0, False),
        (0, 0.20, 0.08, False),
    ],
)
def test_seeded_via_pad_retry_matches_pinned_kicad(seed, floor, shift, reject, pad_first):
    from kicad_tools.router import router_cpp

    case = generate_case(seed)
    case = replace(case, rules=replace(case.rules, trace_clearance=floor, via_clearance=floor))
    pair = next(p for p in pair_contexts(case) if p.kind == "pad-via")
    if shift:
        pad, via = pair.existing, pair.candidate
        dx, dy = via.x - pad.x, via.y - pad.y
        distance = hypot(dx, dy)
        pair = replace(
            pair,
            candidate=replace(
                via, x=via.x + shift * dx / distance, y=via.y + shift * dy / distance
            ),
        )

    nets = net_ids(case)
    rules = router_rules(case)
    router = Autorouter(
        case.width,
        case.height,
        rules=rules,
        layer_stack=layer_stack_for(case),
        force_python=True,
        adaptive_grid=False,
    )
    pad = router_pad(pair.existing, nets)
    route = single_object_route(pair.candidate, nets)

    def add_pad():
        router.pads[pad.key] = pad
        router.nets.setdefault(pad.net, []).append(pad.key)
        router.grid.add_pad(pad)

    if pad_first:
        add_pad()
    transaction = _TraceResolverTransaction(router)
    transaction.begin()
    router.routes.append(route)
    router._mark_route(route)
    if not pad_first:
        add_pad()

    ordinary = Router(router.grid, rules)._validate_route_clearance(
        route, route.net, component_pitches=router.component_pitches, exclude_refs=set()
    )
    native = GridCppAdapter()._validate(router_cpp, case, pair, nets, rules)
    assert ordinary is (not reject)
    assert native.valid is (not reject)
    if reject:
        assert native.violation_type == 8
    assert transaction.validate_committed_geometry() is (not reject)
    if reject:
        transaction.rollback()
        assert router.routes == [] and router.grid.routes == []
        assert pad.key in router.pads
    else:
        assert router.routes == [route] and router.grid.routes == [route]
    assert GridPyAdapter()._judge(case, pair, nets)[0] is reject
