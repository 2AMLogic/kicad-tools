"""A request that already hit the wall clock is probed from its goal side (#8486).

Measured on the reporter's 97-net board after #10335: of the six nets' 981.78 s,
930.68 s is native search, and 21 single unfenced timeouts alone take 538.26 s,
while three of the six pad pairs with such a timeout also connect on another
call.  A wall-clock stop is not a proof, so every repeat pays the whole budget
again, and no shorter deadline is justified because some of those repeats
connect.

A native A* drains only once it has expanded everything reachable from its
start.  With the goal in a small enclosed region, the start-side search floods
the whole open board before it can prove anything and the budget cuts it off
first; the same request searched from its goal drains the small region and
ends with ``no_path``.  On the public 40 x 40 mm wall grid (0.1 mm cells, four
layers) with a 6 x 6 mm sealed pocket at the goal: start side, 602,924
expansions (3.0 s) before it can say anything, so a 1 s budget ends in
``timeout``; goal side, ``no_path`` after 10,404 expansions in 0.03 s.  On the
detour wall, where a path exists, the two directions cost the same (303,554 and
307,187 expansions).

So the next call of such a request first runs a short goal-side probe, on the
most permissive model the forward flow ever retries with.  A drained probe
proves the request unreachable on this grid: the call returns exactly what the
forward timeout returns, having spent the probe's budget instead of the whole
one.  Any other probe outcome -- a route, its own deadline, the iteration cap
-- leaves the call as it was: the forward search runs with its whole budget and
does the same work, to the expansion, as without the probe.  First attempts,
deterministic (no wall clock) calls, relief probes, a call whose budget exceeds
every budget the start-side search has run out of, and
``KICAD_ROUTER_SKIP_REPEAT_FALLBACK=0`` never probe.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from kicad_tools.router import cpp_backend, wall_deadline
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, router_cpp
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.requires_router_cpp

FOREIGN = 2
ALL_LAYERS = (Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU)
#: A closed 10 x 10 mm box on the 40 mm grid; the left wall is the blocker.
BOX = ((30.0, 30.0, 40.0, 30.0), (30.0, 40.0, 40.0, 40.0), (40.0, 30.0, 40.0, 40.0))
LEFT_WALL = (30.0, 30.0, 30.0, 40.0)
#: A closed box around a start pad at (5, 5); the board edge closes its left.
START_BOX = ((0.0, 0.0, 12.0, 0.0), (0.0, 10.0, 12.0, 10.0), (12.0, 0.0, 12.0, 10.0))
#: A wall with a gap at the far edge: the only legal path detours around it.
GAP_WALL = (20.0, 0.0, 20.0, 36.0)


def _pocket(size: float) -> tuple[tuple, tuple]:
    """A closed 6 x 6 mm pocket in the far corner, and its removable left wall."""
    lo = size - 6.0
    box = ((lo, lo, size, lo), (lo, size, size, size), (size, lo, size, size))
    return box, (lo, lo, lo, size)


@pytest.fixture(autouse=True)
def _probe_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", raising=False)


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )


def _wall(*segments: tuple[float, float, float, float]) -> Route:
    return Route(
        net=FOREIGN,
        net_name="WALL",
        segments=[
            Segment(
                x1=x1, y1=y1, x2=x2, y2=y2, width=0.25, layer=layer, net=FOREIGN, net_name="WALL"
            )
            for x1, y1, x2, y2 in segments
            for layer in ALL_LAYERS
        ],
    )


def _pad(x: float, y: float, layer: Layer = Layer.F_CU, **extra) -> Pad:
    return Pad(x=x, y=y, width=0.4, height=0.4, layer=layer, net=1, net_name="N1", **extra)


def _board(walls, *, size: float = 40.0, traced: bool = True) -> CppPathfinder:
    rules = _rules()
    grid = RoutingGrid(size, size, rules, layer_stack=LayerStack.four_layer_all_signal())
    native = CppGrid.from_routing_grid(grid)
    for wall in walls:
        # Committed after the C++ grid is attached, through the production cell
        # mirror, so a later ``unmark_route`` frees both grids as in the router.
        grid.mark_route(wall)
        grid._mark_route_on_cpp_cells(wall)
    pathfinder = CppPathfinder(native, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(native.get_routable_indices())
    if traced:
        pathfinder.enable_per_call_timing(True)
    return pathfinder


def _pocket_board(*, size: float = 40.0, traced: bool = True) -> CppPathfinder:
    box, left = _pocket(size)
    return _board([_wall(*box), _wall(left)], size=size, traced=traced)


def _pocket_pads(size: float = 40.0) -> tuple[Pad, Pad]:
    return _pad(1, 1), _pad(size - 3, size - 3)


def _copper(route) -> tuple | None:
    if route is None:
        return None
    return (tuple(route.segments), tuple(route.vias))


def _probes(record: dict) -> list[dict]:
    return [s for s in record["searches"] if s.get("reverse_probe")]


def _ordinary(record: dict) -> list[dict]:
    return [s for s in record["searches"] if not s.get("reverse_probe")]


def _armed(pathfinder: CppPathfinder, start: Pad, end: Pad, *, budget: float = 1e9) -> None:
    """Record this request as one whose start-side search ran out of ``budget``.

    The record a wall-clock stop leaves, under the same key, without paying the
    stop itself; by default a budget above any cap a test uses.
    ``test_a_wall_clock_stop_arms_the_request`` takes the real path.
    """
    pathfinder._native_timed_out[
        pathfinder._native_request_key(start, end, net_class=None, negotiated_mode=False)
    ] = budget


@pytest.mark.parametrize("negotiated", [False, True])
def test_start_side_and_goal_side_agree_on_every_public_shape(negotiated: bool) -> None:
    """The proof rests on this: a request reachable one way is reachable the other."""
    through_hole = {"through_hole": True, "drill": 0.4}
    shapes = {
        "open": ([], _pad(1, 1), _pad(38, 38)),
        "goal sealed": (BOX + (LEFT_WALL,), _pad(1, 1), _pad(35, 35)),
        "start sealed": (START_BOX, _pad(5, 5), _pad(35, 35)),
        "detour": ((GAP_WALL,), _pad(1, 1), _pad(38, 2)),
        "through-hole, goal sealed": (
            BOX + (LEFT_WALL,),
            _pad(1, 1, **through_hole),
            _pad(35, 35, **through_hole),
        ),
        "through-hole, needs a via": ([], _pad(1, 1), _pad(38, 38, Layer.B_CU)),
    }
    with mock.patch.object(Router, "route", return_value=None):  # native verdicts only
        for name, (segments, start, end) in shapes.items():
            forward = _board([_wall(*segments)]).route(
                start, end, negotiated_mode=negotiated, per_net_timeout=60.0
            )
            reverse = _board([_wall(*segments)]).route(
                end, start, negotiated_mode=negotiated, per_net_timeout=60.0
            )
            assert (forward is not None) == (reverse is not None), name


def test_a_wall_clock_stop_arms_the_request() -> None:
    """The real path: only a stop on the clock arms, and only for budgets it covers."""
    start, end = _pocket_pads()
    pathfinder = _pocket_board()
    with mock.patch.object(Router, "route", return_value=None):
        assert pathfinder.route(start, end, per_net_timeout=None) is None
    (drained,) = pathfinder.get_and_clear_per_call_timings()
    # Unbounded, the start-side search answers for itself: nothing is recorded.
    assert [s["failure_reason"] for s in drained["searches"]] == ["no_path"]
    assert pathfinder._native_timed_out == {}

    stop = 0.6 * drained["native_search_seconds"]  # too short for that drain
    assert pathfinder.route(start, end, per_net_timeout=stop) is None
    (first,) = pathfinder.get_and_clear_per_call_timings()
    assert _probes(first) == []  # nothing was known about this request yet
    assert list(pathfinder._native_timed_out.values()) == [pytest.approx(stop)]

    # A budget that search never had may be one it drains within, so this call
    # keeps its ordinary search ...
    larger = 0.8 * drained["native_search_seconds"]
    assert pathfinder.route(start, end, per_net_timeout=larger) is None
    (unprobed,) = pathfinder.get_and_clear_per_call_timings()
    assert _probes(unprobed) == []
    assert [s["failure_reason"] for s in unprobed["searches"]] == ["timeout"]
    # ... and, having run out of it too, the next call at that budget is proven.
    assert pathfinder.route(start, end, per_net_timeout=larger) is None
    (proven,) = pathfinder.get_and_clear_per_call_timings()
    assert proven["reverse_probe"] == {"budget_s": pytest.approx(0.1 * larger), "outcome": "proved"}
    assert proven["native_search_seconds"] < larger / 5


def test_a_drain_within_a_larger_budget_keeps_the_python_handoff() -> None:
    """Why the budget guard exists: a drained search hands off, a timeout does not."""
    start, end = _pocket_pads(size=20.0)
    pathfinder = _pocket_board(size=20.0)
    _armed(pathfinder, start, end, budget=0.1)

    with mock.patch.object(Router, "route", return_value=None) as handoff:
        assert pathfinder.route(start, end, per_net_timeout=5.0) is None
    assert handoff.call_count == 1  # as after any drained native verdict
    (record,) = pathfinder.get_and_clear_per_call_timings()
    assert _probes(record) == []
    assert [s["failure_reason"] for s in record["searches"]] == ["no_path"]
    assert record["python_fallback"]["action"] == "ran"
    assert pathfinder._native_timed_out == {}  # a verdict, no longer a timeout


def test_a_repeat_on_unchanged_copper_is_proven_from_the_goal_side() -> None:
    """The saving: the same sealed request costs the probe's budget, not the cap."""
    start, end = _pocket_pads()
    pathfinder = _pocket_board()
    _armed(pathfinder, start, end)

    assert pathfinder.route(start, end, per_net_timeout=20.0) is None
    (repeat,) = pathfinder.get_and_clear_per_call_timings()
    assert repeat["per_net_timeout"] == 20.0
    assert repeat["reverse_probe"] == {"budget_s": pytest.approx(2.0), "outcome": "proved"}
    (probe,) = repeat["searches"]
    assert probe["reverse_probe"] is True
    assert probe["fenced"] is False
    assert probe["failure_reason"] == "no_path"
    # The probe is exactly this request searched the other way: the same work,
    # to the expansion, as a direct goal-to-start search on an identical board.
    direct_board = _pocket_board()
    with mock.patch.object(Router, "route", return_value=None):
        assert direct_board.route(end, start, per_net_timeout=60.0) is None
    (direct,) = direct_board.get_and_clear_per_call_timings()
    assert probe["expansions"] == direct["searches"][0]["expansions"] > 0
    assert repeat["native_search_seconds"] == pytest.approx(probe["elapsed"])
    assert repeat["native_search_seconds"] < 0.5  # the probe's ceiling is 2 s; the cap, 20 s

    stats = pathfinder.fallback_stats
    assert stats["native_reverse_probes"] == 1
    assert stats["native_reverse_proofs"] == 1
    assert stats["native_reverse_seconds_saved"] > 15.0


def test_a_proof_returns_what_the_forward_timeout_returns() -> None:
    """Same outcome as the stop it replaces: no route, no fallback, timeout evidence."""
    start, end = _pocket_pads()
    pathfinder = _pocket_board()
    _armed(pathfinder, start, end)

    with mock.patch.object(Router, "route", side_effect=Router.route) as spy:
        assert pathfinder.route(start, end, per_net_timeout=20.0) is None
    assert spy.call_count == 0
    (proof,) = pathfinder.get_and_clear_per_call_timings()
    assert proof["succeeded"] is False
    assert proof["deadline_violated"] is False
    assert proof["python_fallback_seconds"] == 0.0
    assert proof["python_fallback"]["action"] == "skipped_cpp_timeout"
    assert proof["python_fallback"]["cpp_failure_reason"] == "timeout"
    assert pathfinder.get_last_failure_info()["failure_reason"] == int(router_cpp.FAILURE_TIMEOUT)


def test_the_probe_budget_is_a_tenth_of_the_cap_and_never_more_than_five_seconds() -> None:
    start, end = _pocket_pads()
    pathfinder = _pocket_board()
    _armed(pathfinder, start, end)
    for cap, expected in ((20.0, 2.0), (100.0, 5.0)):
        assert pathfinder.route(start, end, per_net_timeout=cap) is None
        (record,) = pathfinder.get_and_clear_per_call_timings()
        assert record["reverse_probe"] == {"budget_s": pytest.approx(expected), "outcome": "proved"}


def test_a_moved_wall_still_connects_with_the_same_copper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: rip the blocking wall up, and the same connection routes."""
    start, end = _pocket_pads()
    _, left = _pocket(40.0)

    def opened(*, probe: bool) -> tuple[CppPathfinder, Route | None, dict]:
        # Same request, same record and same board each time; only the probe differs.
        if probe:
            monkeypatch.delenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", raising=False)
        else:
            monkeypatch.setenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", "0")
        pathfinder = _pocket_board()
        _armed(pathfinder, start, end)
        pathfinder._grid._py_grid.unmark_route(_wall(left))
        route = pathfinder.route(start, end, per_net_timeout=20.0)
        return pathfinder, route, pathfinder.get_and_clear_per_call_timings()[0]

    pathfinder, route, repeat = opened(probe=True)
    assert repeat["reverse_probe"]["outcome"] == "connected"  # the probe saw the opening
    assert _probes(repeat)[0]["succeeded"] is True
    _, expected, control = opened(probe=False)
    assert route is not None and expected is not None
    # The same start-side work, to the expansion, and so the same copper.
    assert [s["expansions"] for s in _ordinary(repeat)] == [
        s["expansions"] for s in control["searches"]
    ]
    assert _copper(route) == _copper(expected)

    # A route disarms the request: the next call is an ordinary one.
    assert pathfinder.route(start, end, per_net_timeout=20.0) is not None
    (plain,) = pathfinder.get_and_clear_per_call_timings()
    assert _probes(plain) == []


@pytest.mark.parametrize(
    ("excursion", "probe_budget", "forward_budget"),
    [(None, 0.3, 3.0), (0.4, 0.04, 0.36)],
    ids=["whole-forward-budget", "absolute-excursion-deadline"],
)
def test_forward_budget_after_a_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
    excursion: float | None,
    probe_budget: float,
    forward_budget: float,
) -> None:
    """Spend the probe budget on a controlled clock, then run a real forward search."""
    start, end = _pad(1, 1), _pad(3, 2)
    pathfinder = _board([], size=5.0)
    _armed(pathfinder, start, end)

    clock = mock.Mock(wraps=time)
    clock.monotonic.return_value = 100.0
    monkeypatch.setattr(wall_deadline, "time", clock)
    monkeypatch.setattr(cpp_backend, "time", clock)

    native = type(pathfinder._impl)
    real_search = native.route_resumable
    launches = []

    def search(impl, *args):
        # route_resumable takes start x/y, end x/y, and timeout at these positions.
        source, target, timeout = args[:2], args[3:5], args[18]
        launches.append((source, target, timeout))
        if source == (end.x, end.y):
            clock.monotonic.return_value += timeout
            return mock.Mock(success=False, failure_reason=router_cpp.FAILURE_TIMEOUT)
        return real_search(impl, *args)

    monkeypatch.setattr(native, "route_resumable", search)
    with wall_deadline.deadline(excursion):
        route = pathfinder.route(start, end, per_net_timeout=3.0)
    (record,) = pathfinder.get_and_clear_per_call_timings()
    assert record["reverse_probe"]["outcome"] == "timeout"
    assert launches == [
        ((end.x, end.y), (start.x, start.y), pytest.approx(probe_budget)),
        ((start.x, start.y), (end.x, end.y), pytest.approx(forward_budget)),
    ]

    control_board = _board([], size=5.0)
    expected = control_board.route(start, end, per_net_timeout=forward_budget)
    (control,) = control_board.get_and_clear_per_call_timings()
    (forward,) = _ordinary(record)
    assert forward["expansions"] == control["searches"][0]["expansions"] > 0
    assert route is not None and _copper(route) == _copper(expected)


def test_relief_mode_never_probes_or_records() -> None:
    """Relief probes stress the search; they keep every full attempt."""
    start, end = _pocket_pads()
    pathfinder = _pocket_board()
    _armed(pathfinder, start, end)
    pathfinder.set_relief_mode(True)
    pathfinder.route(start, end, per_net_timeout=0.3)
    (record,) = pathfinder.get_and_clear_per_call_timings()
    assert _probes(record) == []

    fresh = _pocket_board()
    fresh.set_relief_mode(True)
    fresh.route(start, end, per_net_timeout=0.001)
    assert fresh._native_timed_out == {}


def test_opt_out_env_restores_the_plain_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the probe the same repeat on unchanged copper runs to its budget."""
    start, end = _pocket_pads()
    monkeypatch.setenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", "0")
    pathfinder = _pocket_board()
    _armed(pathfinder, start, end)
    assert pathfinder.route(start, end, per_net_timeout=0.3) is None
    (repeat,) = pathfinder.get_and_clear_per_call_timings()
    assert _probes(repeat) == []
    assert repeat["native_search_seconds"] >= 0.29
    assert pathfinder.fallback_stats["native_reverse_probes"] == 0


def test_tracing_does_not_change_the_probe() -> None:
    """The trace observes the probe; it never steers it."""
    start, end = _pocket_pads()
    untraced = _pocket_board(traced=False)
    _armed(untraced, start, end)
    t0 = time.monotonic()
    assert untraced.route(start, end, per_net_timeout=20.0) is None
    assert time.monotonic() - t0 < 5.0
    assert untraced.get_and_clear_per_call_timings() == []

    traced = _pocket_board()
    _armed(traced, start, end)
    assert traced.route(start, end, per_net_timeout=20.0) is None
    assert untraced.fallback_stats["native_reverse_proofs"] == 1
    assert traced.fallback_stats["native_reverse_proofs"] == 1
