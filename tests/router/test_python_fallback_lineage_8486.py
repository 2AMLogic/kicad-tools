"""Per-call traces say how each Python fallback relates to earlier ones (#8486).

The November 25 comparison put 99.95% of the non-native time of the slow
``via_via_blocked`` calls inside the Python fallback.  Before choosing a
policy for repeats, the trace has to tell apart the cases the next policy
must treat differently:

* the same request on an unchanged grid vs. on a changed grid vs. a changed
  goal (``request`` / ``grid``);
* an earlier search that drained its open set (a proof for that grid) vs. one
  ended by its wall-clock deadline (no proof) -- ``prior_outcome`` / ``outcome``;
* whether the fallback ran or which existing guard skipped it (``action``).

The fixture is the public walled grid of ``test_cpp_proven_unreachable_skip_6098``.
The trace is purely observational: every shape also pins that tracing leaves
the routing decisions and skip counters exactly as they are untraced.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, router_cpp
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.requires_router_cpp


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )


def _walled_grid() -> RoutingGrid:
    """A 10x6 mm two-layer grid with a full-height wall at x = 5 mm."""
    grid = RoutingGrid(width=10.0, height=6.0, rules=_rules(), layer_stack=LayerStack.two_layer())
    col = grid.world_to_grid(5.0, 3.0)[0]
    grid._blocked[:, :, col - 2 : col + 3] = True
    grid._is_obstacle[:, :, col - 2 : col + 3] = True
    grid.bump_occupancy_generation()
    return grid


def _pads() -> tuple[Pad, Pad]:
    common = {"width": 0.6, "height": 0.6, "net": 1, "net_name": "SIG", "layer": Layer.F_CU}
    return Pad(x=2.0, y=3.0, **common), Pad(x=8.0, y=3.0, **common)


def _pathfinder(grid: RoutingGrid, *, traced: bool) -> CppPathfinder:
    cpp_grid = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(cpp_grid, _rules(), diagonal_routing=True)
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    if traced:
        pathfinder.enable_per_call_timing(True)
    return pathfinder


def _route_traces(pathfinder: CppPathfinder, calls: int, **kwargs) -> list[dict]:
    for _ in range(calls):
        assert pathfinder.route(*_pads(), **kwargs) is None
    return [r["python_fallback"] for r in pathfinder.get_and_clear_per_call_timings()]


@pytest.fixture(autouse=True)
def _default_fallback_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", raising=False)
    monkeypatch.delenv("KICAD_ROUTER_SKIP_PROVEN_FALLBACK", raising=False)


def test_first_search_is_traced_as_a_fresh_drained_request() -> None:
    (first,) = _route_traces(_pathfinder(_walled_grid(), traced=True), 1)
    assert first["request"] == "first"
    assert first["action"] == "ran"
    assert first["outcome"] == "drained"
    assert first["reduced_budget"] is False
    assert first["seconds"] > 0.0
    assert "grid" not in first and "prior_outcome" not in first


def test_identical_request_on_an_unchanged_grid_is_a_skipped_repeat() -> None:
    first, second, third = _route_traces(_pathfinder(_walled_grid(), traced=True), 3)
    assert first["action"] == "ran"
    for repeat in (second, third):
        assert repeat["request"] == "repeat"
        assert repeat["grid"] == "unchanged"
        assert repeat["prior_outcome"] == "drained"
        assert repeat["action"] == "skipped_proven_unreachable"
        assert "outcome" not in repeat  # nothing ran, so nothing ended


def test_a_rip_up_generation_bump_alone_is_still_an_unchanged_grid() -> None:
    grid = _walled_grid()
    pathfinder = _pathfinder(grid, traced=True)
    _route_traces(pathfinder, 1)
    grid.bump_occupancy_generation()
    (repeat,) = _route_traces(pathfinder, 1)
    assert repeat["grid"] == "unchanged"
    assert repeat["action"] == "skipped_proven_unreachable"


def test_a_changed_grid_is_traced_and_the_existing_repeat_guard_names_itself() -> None:
    grid = _walled_grid()
    pathfinder = _pathfinder(grid, traced=True)
    _route_traces(pathfinder, 1)
    grid._pad_blocked[0, 0, 0] = not grid._pad_blocked[0, 0, 0]
    (repeat,) = _route_traces(pathfinder, 1)
    assert repeat["request"] == "repeat"
    assert repeat["grid"] == "changed"
    assert repeat["prior_outcome"] == "drained"
    # The #8370 repeat-only skip, not the exact-grid proof, spares this one.
    assert repeat["action"] == "skipped_drained_repeat"


def test_a_changed_grid_reruns_python_when_the_repeat_guard_is_off(monkeypatch) -> None:
    monkeypatch.setenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", "0")
    grid = _walled_grid()
    pathfinder = _pathfinder(grid, traced=True)
    _route_traces(pathfinder, 1)
    grid._pad_blocked[0, 0, 0] = not grid._pad_blocked[0, 0, 0]
    (repeat,) = _route_traces(pathfinder, 1)
    assert (repeat["request"], repeat["grid"]) == ("repeat", "changed")
    assert repeat["action"] == "ran"
    assert repeat["outcome"] == "drained"


def test_a_changed_goal_or_mode_is_a_changed_request_not_a_repeat() -> None:
    pathfinder = _pathfinder(_walled_grid(), traced=True)
    _route_traces(pathfinder, 1)
    (other,) = _route_traces(pathfinder, 1, negotiated_mode=True)
    assert other["request"] == "changed_request"
    assert "grid" not in other
    assert other["action"] == "ran"


def _deadline_stub(self, *args, **kwargs):
    self.last_route_exhausted = False
    self.last_route_deadline_hit = True
    return None


def _capped_stub(self, *args, **kwargs):
    self.last_route_exhausted = False
    self.last_route_deadline_hit = False
    return None


@pytest.mark.parametrize(
    ("stub", "outcome"),
    [(_deadline_stub, "deadline"), (_capped_stub, "stopped")],
)
def test_a_search_that_did_not_drain_is_not_traced_as_one(stub, outcome) -> None:
    """A deadline (or cap) leaves no proof, so the repeat runs again and says why."""
    pathfinder = _pathfinder(_walled_grid(), traced=True)
    with mock.patch.object(Router, "route", autospec=True, side_effect=stub):
        first, second = _route_traces(pathfinder, 2, per_net_timeout=30.0)
    assert first["outcome"] == outcome
    assert second["request"] == "repeat"
    assert second["grid"] == "unchanged"
    assert second["prior_outcome"] == outcome
    assert second["action"] == "ran"
    assert second["outcome"] == outcome
    # The second attempt is already bounded by the #8485 repeat budget.
    assert second["reduced_budget"] is True


def test_reenabled_trace_starts_fresh_but_reuses_an_untraced_proof(monkeypatch) -> None:
    monkeypatch.setenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", "0")
    pathfinder = _pathfinder(_walled_grid(), traced=True)
    with mock.patch.object(Router, "route", autospec=True, side_effect=_deadline_stub):
        (first,) = _route_traces(pathfinder, 1)
    assert first["outcome"] == "deadline"

    pathfinder.enable_per_call_timing(False)
    assert pathfinder.route(*_pads()) is None
    pathfinder.enable_per_call_timing(True)
    (fresh,) = _route_traces(pathfinder, 1)
    assert fresh["action"] == "skipped_proven_unreachable"
    assert fresh["request"] == "first"
    assert "prior_outcome" not in fresh and "grid" not in fresh


@pytest.mark.parametrize(
    ("code", "action"),
    [
        ("FAILURE_TIMEOUT", "skipped_cpp_timeout"),
        ("FAILURE_ITERATION_LIMIT", "skipped_iteration_cap"),
        ("FAILURE_NO_PATH", "skipped_resume_repeat"),
    ],
)
def test_skips_before_classification_report_unclassified(code, action, monkeypatch) -> None:
    pathfinder = _pathfinder(_walled_grid(), traced=True)
    (first,) = _route_traces(pathfinder, 1)
    assert first["outcome"] == "drained"
    start, end = _pads()
    pathfinder._per_net_iteration_cap_active = True
    monkeypatch.setenv("KICAD_ROUTER_SKIP_RESUME_FALLBACK", "1")
    pathfinder._resume_clearance_exhaustions[(start.net, start.net_name)] = 1
    for goal_y in (3.0, 4.0):
        end.y = goal_y
        pathfinder._attempt_diagnostic = {}
        assert (
            pathfinder._try_python_fallback(
                start,
                end,
                cpp_failure_reason=getattr(router_cpp, code),
                reason="exhausted resume attempts",
            )
            is None
        )
        trace = pathfinder._attempt_diagnostic["python_fallback"]
        assert (trace["action"], trace["request"]) == (action, "unclassified")
        assert "grid" not in trace and "prior_outcome" not in trace and "outcome" not in trace


def test_missing_python_grid_returns_an_unclassified_skip() -> None:
    pathfinder = _pathfinder(_walled_grid(), traced=True)
    pathfinder._grid._py_grid = None
    pathfinder._attempt_diagnostic = {}
    assert pathfinder._try_python_fallback(*_pads()) is None
    trace = pathfinder._attempt_diagnostic["python_fallback"]
    assert (trace["action"], trace["request"]) == ("skipped_no_python_grid", "unclassified")
    assert "grid" not in trace and "prior_outcome" not in trace


def test_tracing_leaves_routing_decisions_and_counters_unchanged() -> None:
    results = {}
    for traced in (False, True):
        pathfinder = _pathfinder(_walled_grid(), traced=traced)
        with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
            routes = [pathfinder.route(*_pads()) for _ in range(3)]
        stats = pathfinder.fallback_stats
        results[traced] = (
            routes,
            spy.call_count,
            stats["python_fallback_proven_skips"],
            stats["python_fallback_repeat_skips"],
        )
    assert results[False] == results[True]
    untraced = _pathfinder(_walled_grid(), traced=False)
    untraced.route(*_pads())
    assert untraced._python_attempt_history == {}, "the untraced fast path keeps no history"
