"""Drained Python fallbacks after a native via-verdict are not re-run (#8486).

The November 25 capped comparison put 99.95% of the non-native time of the
slow ``via_via_blocked`` calls inside the Python fallback (21.55 s of 60.00 s
sampled), and the first-stage trace showed one pad pair paying it five times
(~65 s).  The #6098 exact-grid proof only covers the identical request on an
unchanged grid, and the #8370 repeat-only skip excluded
``FAILURE_VIA_VIA_BLOCKED``, so a repeat of the same request after any grid
change re-ran -- and re-drained -- the 10-100x slower A*.

These tests pin the #8486 extension of that skip to the via verdict, on the
public via-verdict grid (the maintainer's control): the obstruction is
visible to both routers (the C++ stored-via registry and the Python route
halo), the direct-pad search drains, a tree goal instead of the direct pad
still gets its full fallback and routes clearance-valid copper with zero new
vias, and a rip-up that opens a via slot recovers in the native search.  The
drained/deadline distinction, the relief-probe exemption, the
``reset_fallback_for_net`` valve and the env opt-out are all retained.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
from kicad_tools.router.grid import RoutingGrid, routed_halo_radius_cells
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Via
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.requires_router_cpp

PITCH = 0.5
FOREIGN = 2
#: The trace-only corridor: rows of the carpet within this distance of
#: ``y = 6.25`` are omitted, so a 0.2 mm trace fits (0.75 mm to the nearest
#: stored via, >= 0.6 mm needed) while a via does not (< 0.8 mm needed).
CORRIDOR_Y = 6.25
CORRIDOR_HALF_WIDTH = 0.7
CORRIDOR_X0, CORRIDOR_X1 = 0.5, 8.0


@pytest.fixture(autouse=True)
def _repeat_skip_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", raising=False)
    monkeypatch.delenv("KICAD_ROUTER_SKIP_PROVEN_FALLBACK", raising=False)


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )


def _via(x: float, y: float) -> Via:
    return Via(
        x=x,
        y=y,
        drill=0.3,
        diameter=0.6,
        layers=(Layer.IN1_CU, Layer.B_CU),
        net=FOREIGN,
        net_name="FOREIGN",
    )


def _carpet_vias() -> list[Via]:
    """The stored-via carpet: 0.5 mm pitch minus the trace-only corridor."""
    vias = []
    for i in range(25):
        for j in range(25):
            x, y = i * PITCH, j * PITCH
            if abs(y - CORRIDOR_Y) < CORRIDOR_HALF_WIDTH and CORRIDOR_X0 <= x <= CORRIDOR_X1:
                continue
            vias.append(_via(x, y))
    return vias


def _commit_stored_vias(grid: RoutingGrid, route: Route) -> None:
    """Register foreign stored vias so BOTH routers see them, without cell marks.

    The C++ stored-via registry is synced from ``grid.routes`` on every
    ``route()`` call (``sync_stored_routes``), which is what produces the
    ``via_via_blocked`` verdict; the Python route halo records are what the
    fallback A*'s ``_is_via_blocked`` consults.  No cells are blocked, so
    F.Cu motion stays legal and the native failure is the via verdict, not
    ``no_path`` -- the same shape as the #2476 fixture the maintainer used.
    """
    grid.routes.append(route)
    for v in route.vias:
        grid._route_halo.record(
            grid._route_halo.via_key(v),
            routed_halo_radius_cells(v, grid.rules, grid.resolution),
            True,
        )


def _pathfinder(*, traced: bool = True) -> CppPathfinder:
    grid = RoutingGrid(12.0, 12.0, _rules(), layer_stack=LayerStack.four_layer_all_signal())
    _commit_stored_vias(
        grid, Route(net=FOREIGN, net_name="FOREIGN", segments=[], vias=_carpet_vias())
    )
    native = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(native, _rules(), diagonal_routing=True)
    pathfinder.set_routable_layers(native.get_routable_indices())
    if traced:
        pathfinder.enable_per_call_timing(True)
    return pathfinder


def _pads() -> tuple[Pad, Pad]:
    common = {"width": 0.2, "height": 0.2, "net": 1, "net_name": "N1"}
    return (
        Pad(x=1.0, y=CORRIDOR_Y, layer=Layer.F_CU, **common),
        Pad(x=9.0, y=CORRIDOR_Y, layer=Layer.B_CU, **common),
    )


def _tree_cells() -> set[tuple[int, int, int]]:
    """Goal cells of the net's own tree, inside the trace-only corridor."""
    return {(gx, 62, 0) for gx in range(12, 71)}


def _route(pathfinder: CppPathfinder, calls: int, **kwargs) -> list[dict]:
    for _ in range(calls):
        assert pathfinder.route(*_pads(), **kwargs) is None
    return [r["python_fallback"] for r in pathfinder.get_and_clear_per_call_timings()]


def _flip_an_irrelevant_cell(pathfinder: CppPathfinder) -> None:
    """A grid change far from either pad: churns the #6098 stamp only."""
    grid = pathfinder._grid._py_grid
    grid._pad_blocked[0, 0, 0] = not grid._pad_blocked[0, 0, 0]


def test_first_via_blocked_call_drains_and_a_changed_grid_repeat_is_skipped() -> None:
    """The core policy: a drained same-request repeat is not re-drained.

    Measured avoided work on this public grid: one real Python A* drain
    (~1.9 s here; 21.55 s per two calls on the reported board) instead of
    one per repeat, for a repeat whose only change is grid churn.
    """
    pathfinder = _pathfinder()
    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        (first,) = _route(pathfinder, 1, per_net_timeout=30.0)
        _flip_an_irrelevant_cell(pathfinder)
        (second,) = _route(pathfinder, 1, per_net_timeout=30.0)
    assert spy.call_count == 1  # the avoided work: the repeat never ran
    assert first["request"] == "first"
    assert first["action"] == "ran"
    assert first["outcome"] == "drained"
    assert second["request"] == "repeat"
    assert second["grid"] == "changed"
    assert second["prior_outcome"] == "drained"
    assert second["action"] == "skipped_drained_repeat"
    assert "outcome" not in second  # nothing ran, so nothing ended
    stats = pathfinder.fallback_stats
    assert stats["python_fallback_repeat_skips"] == 1
    assert stats["python_fallback_seconds_saved"] >= first["seconds"]


def test_unchanged_grid_repeat_still_uses_the_exact_proof() -> None:
    """The #6098 exact-grid proof stays ahead of the repeat skip."""
    pathfinder = _pathfinder()
    first, second = _route(pathfinder, 2, per_net_timeout=30.0)
    assert first["action"] == "ran"
    assert second["request"] == "repeat"
    assert second["grid"] == "unchanged"
    assert second["action"] == "skipped_proven_unreachable"


def test_a_tree_goal_keeps_its_full_fallback_and_routes() -> None:
    """The retained success: a different goal on the same pads still routes.

    The maintainer's control -- after the native via verdict and a drained
    direct-pad search, a goal on the net's own tree returns clearance-valid
    copper with zero new vias, at the full (not reduced) budget.
    """
    pathfinder = _pathfinder()
    (direct,) = _route(pathfinder, 1, per_net_timeout=30.0)
    assert direct["outcome"] == "drained"
    route = pathfinder.route(*_pads(), per_net_timeout=30.0, extra_goal_cells=_tree_cells())
    assert route is not None
    assert route.vias == []
    (tree,) = [r["python_fallback"] for r in pathfinder.get_and_clear_per_call_timings()]
    assert tree["request"] == "changed_request"
    assert tree["action"] == "ran"
    assert tree["outcome"] == "found"
    assert tree["reduced_budget"] is False
    assert tree["budget_s"] == pytest.approx(30.0, abs=0.5)


def test_reset_fallback_for_net_restores_a_full_budget_attempt() -> None:
    """The negotiated valve: a witness change / re-probe buys a fresh attempt."""
    pathfinder = _pathfinder()
    _route(pathfinder, 1, per_net_timeout=30.0)
    assert pathfinder._python_drained_nets
    _flip_an_irrelevant_cell(pathfinder)  # the exact #6098 proof no longer matches
    pathfinder.reset_fallback_for_net(1)
    assert pathfinder._python_drained_nets == {}
    assert pathfinder._python_failed_attempts == {}
    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        assert pathfinder.route(*_pads(), per_net_timeout=30.0) is None
    assert spy.call_count == 1
    assert spy.call_args.kwargs["per_net_timeout"] == pytest.approx(30.0, abs=0.5)
    assert pathfinder.fallback_stats["python_fallback_reduced_repeats"] == 0


def test_ripping_up_the_blocker_opens_the_connection_without_a_fallback() -> None:
    """A copper change that opens the connection recovers in the native search.

    The maintained control: ripping the foreign carpet up through the
    production path (``unmark_route`` + the stored-route invalidation) opens
    via slots everywhere, and the next call connects with a via -- the skip
    never strands a net whose blocker actually changed.
    """
    grid = RoutingGrid(12.0, 12.0, _rules(), layer_stack=LayerStack.four_layer_all_signal())
    carpet = Route(net=FOREIGN, net_name="FOREIGN", segments=[], vias=_carpet_vias())
    _commit_stored_vias(grid, carpet)
    native = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(native, _rules(), diagonal_routing=True)
    pathfinder.set_routable_layers(native.get_routable_indices())
    pathfinder.enable_per_call_timing(True)

    assert pathfinder.route(*_pads(), per_net_timeout=30.0) is None
    blocked = pathfinder.get_and_clear_per_call_timings()[0]
    assert [s["failure_reason"] for s in blocked["searches"]] == ["via_via_blocked"]

    grid.unmark_route(carpet)
    route = pathfinder.route(*_pads(), per_net_timeout=30.0)
    assert route is not None and route.vias, "the connection needs, and now gets, a via"
    (opened,) = pathfinder.get_and_clear_per_call_timings()
    assert opened["succeeded"] is True
    assert [s["succeeded"] for s in opened["searches"]] == [True]
    assert "python_fallback" not in opened


def _deadline_stub(self, *args, **kwargs):
    self.last_route_exhausted = False
    self.last_route_deadline_hit = True
    return None


def test_a_deadline_stopped_via_blocked_search_is_not_skipped() -> None:
    """Drained vs deadline: a wall-clock stop is not a proof, so it retries."""
    pathfinder = _pathfinder()
    with mock.patch.object(Router, "route", autospec=True, side_effect=_deadline_stub):
        first, second = _route(pathfinder, 2, per_net_timeout=30.0)
    assert first["outcome"] == "deadline"
    assert second["request"] == "repeat"
    assert second["prior_outcome"] == "deadline"
    assert second["action"] == "ran"
    assert second["outcome"] == "deadline"
    assert second["reduced_budget"] is True  # bounded by #8485, not skipped
    assert pathfinder.fallback_stats["python_fallback_repeat_skips"] == 0


def test_opt_out_env_restores_the_repeat_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", "0")
    pathfinder = _pathfinder()
    (first,) = _route(pathfinder, 1, per_net_timeout=30.0)
    _flip_an_irrelevant_cell(pathfinder)
    (repeat,) = _route(pathfinder, 1, per_net_timeout=30.0)
    assert first["action"] == "ran"
    assert repeat["request"] == "repeat"
    assert repeat["grid"] == "changed"
    assert repeat["action"] == "ran"
    assert repeat["outcome"] == "drained"
    assert pathfinder.fallback_stats["python_fallback_repeat_skips"] == 0


def test_relief_mode_never_skips_a_via_blocked_repeat() -> None:
    """Relief probes stress the search; they always run and record nothing."""
    pathfinder = _pathfinder()
    pathfinder.set_relief_mode(True)
    (first,) = _route(pathfinder, 1, per_net_timeout=30.0)
    # Grid churn keeps the #6098 exact proof out of the way; the point is
    # that no repeat guard fires in relief-probe mode.
    _flip_an_irrelevant_cell(pathfinder)
    (second,) = _route(pathfinder, 1, per_net_timeout=30.0)
    assert first["action"] == "ran"
    assert first["outcome"] == "drained"
    assert second["action"] == "ran"
    assert pathfinder.fallback_stats["python_fallback_repeat_skips"] == 0
    assert pathfinder._python_drained_nets == {}


def test_tracing_does_not_change_the_skip_or_its_counters() -> None:
    """The trace observes the skip; it never steers it."""
    untraced = _pathfinder(traced=False)
    traced = _pathfinder(traced=True)
    results = []
    for pathfinder in (untraced, traced):
        with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
            assert pathfinder.route(*_pads(), per_net_timeout=30.0) is None
            _flip_an_irrelevant_cell(pathfinder)
            assert pathfinder.route(*_pads(), per_net_timeout=30.0) is None
        assert spy.call_count == 1
        results.append(pathfinder.fallback_stats["python_fallback_repeat_skips"])
    assert results == [1, 1]
    assert untraced.get_and_clear_per_call_timings() == []
