"""Per-call route traces split wall time into native / fallback / other (#8486).

The November 25 private-board trace could not say where 64.90 s of five
unfenced ``via_via_blocked`` calls went: the recorded native searches covered
83.79 s of the 148.69 s the calls took, and nothing attributed the rest.  A
trace record now carries ``native_search_seconds`` (the fresh native searches),
``python_fallback_seconds`` (the Python fallback A*) and ``other_seconds``
(the remainder), which sum to ``elapsed``.

These controls reuse the public via-verdict fixture of
``tests/test_router_cpp_via_blocked_failure.py`` (a carpet of foreign stored
vias that leaves F.Cu motion legal but blocks every via drop) and cover the
three shapes the maintainer asked to compare: repeat calls on an unchanged
obstruction, a changed obstruction that opens the same connection, and an
ordinary unfenced timeout that a later full-budget call completes.  The
breakdown is purely observational, so each shape also pins that the produced
copper is identical with tracing off and on.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.requires_router_cpp

FOREIGN = 2
BREAKDOWN_KEYS = ("native_search_seconds", "python_fallback_seconds", "other_seconds")


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )


def _carpet_pathfinder(*, traced: bool) -> tuple[CppPathfinder, CppGrid]:
    """The public via-verdict fixture: In1..B.Cu foreign vias on a 0.4 mm pitch."""
    rules = _rules()
    grid = RoutingGrid(
        width=2.0,
        height=2.0,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    native = CppGrid.from_routing_grid(grid)
    for i in range(6):
        for j in range(6):
            native._impl.add_stored_via(
                i * 0.4,
                j * 0.4,
                0.3,
                rules.via_diameter,
                FOREIGN,
                layer_from=1,
                layer_to=3,
            )
    pathfinder = CppPathfinder(native, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(native.get_routable_indices())
    if traced:
        pathfinder.enable_per_call_timing(True)
    return pathfinder, native


def _via_pads() -> tuple[Pad, Pad]:
    start = Pad(x=0.2, y=0.2, width=0.2, height=0.2, layer=Layer.F_CU, net=1, net_name="N1")
    end = Pad(x=1.8, y=1.8, width=0.2, height=0.2, layer=Layer.B_CU, net=1, net_name="N1")
    return start, end


def _copper(route) -> tuple | None:
    if route is None:
        return None
    return (tuple(route.segments), tuple(route.vias))


def _assert_breakdown_adds_up(record: dict) -> None:
    for key in BREAKDOWN_KEYS:
        assert record[key] >= 0.0
    total = sum(record[key] for key in BREAKDOWN_KEYS)
    assert total == pytest.approx(record["elapsed"], abs=1e-6)
    assert record["native_search_seconds"] == pytest.approx(
        sum(s["elapsed"] for s in record["searches"]), abs=1e-9
    )


def test_unchanged_via_obstruction_repeats_pay_native_then_fallback_each_call() -> None:
    """Repeat calls on the same stored-via obstruction: where does each call go?"""
    pathfinder, _native = _carpet_pathfinder(traced=True)
    start, end = _via_pads()
    results = [pathfinder.route(start, end, negotiated_mode=False, per_net_timeout=30.0)]
    results.append(pathfinder.route(start, end, negotiated_mode=False, per_net_timeout=30.0))
    records = pathfinder.get_and_clear_per_call_timings()
    assert len(records) == 2
    for record in records:
        # The native search ends on the via verdict, not on its budget ...
        assert [s["failure_reason"] for s in record["searches"]] == ["via_via_blocked"]
        assert record["searches"][0]["fenced"] is False
        # ... and the geometric failure is handed to the Python fallback.
        assert record["python_fallback"]["cpp_failure_reason"] == "via_via_blocked"
        assert record["native_search_seconds"] > 0.0
        _assert_breakdown_adds_up(record)
    # An unchanged obstruction yields the same outcome and copper every call.
    assert _copper(results[0]) == _copper(results[1])


def test_removing_the_via_obstruction_opens_the_connection_without_a_fallback() -> None:
    """A changed obstruction that opens the same connection: native success, no fallback."""
    pathfinder, native = _carpet_pathfinder(traced=True)
    start, end = _via_pads()
    pathfinder.route(start, end, negotiated_mode=False, per_net_timeout=30.0)
    blocked = pathfinder.get_and_clear_per_call_timings()[0]
    assert blocked["searches"][0]["failure_reason"] == "via_via_blocked"

    native._impl.clear_stored_routes()  # the foreign vias are removed
    route = pathfinder.route(start, end, negotiated_mode=False, per_net_timeout=30.0)
    assert route is not None and route.vias, "the connection needs, and now gets, a via"
    (opened,) = pathfinder.get_and_clear_per_call_timings()
    assert opened["succeeded"] is True
    assert [s["succeeded"] for s in opened["searches"]] == [True]
    assert "python_fallback" not in opened
    assert opened["python_fallback_seconds"] == 0.0
    assert opened["native_search_seconds"] > 0.0
    _assert_breakdown_adds_up(opened)


def test_unfenced_timeout_is_all_native_and_a_full_budget_call_succeeds() -> None:
    """An ordinary timeout pays native time only; the same pair connects with budget."""
    rules = _rules()
    grid = RoutingGrid(
        width=40.0,
        height=40.0,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    # A foreign wall on every layer with a gap at the far edge: the only legal
    # path detours around it, so the search has real work to do.
    wall = Route(
        net=FOREIGN,
        net_name="WALL",
        segments=[
            Segment(
                x1=20.0,
                y1=0.0,
                x2=20.0,
                y2=36.0,
                width=0.25,
                layer=layer,
                net=FOREIGN,
                net_name="WALL",
            )
            for layer in (Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU)
        ],
    )
    grid.mark_route(wall)
    native = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(native, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(native.get_routable_indices())
    pathfinder.enable_per_call_timing(True)
    start = Pad(x=1.0, y=1.0, width=0.4, height=0.4, layer=Layer.F_CU, net=1, net_name="N1")
    end = Pad(x=38.0, y=2.0, width=0.4, height=0.4, layer=Layer.F_CU, net=1, net_name="N1")

    assert pathfinder.route(start, end, negotiated_mode=False, per_net_timeout=1e-4) is None
    (timed_out,) = pathfinder.get_and_clear_per_call_timings()
    assert [s["failure_reason"] for s in timed_out["searches"]] == ["timeout"]
    # A wall-clock timeout is never re-run in the Python A* (#3876).
    assert timed_out["python_fallback_seconds"] == 0.0
    _assert_breakdown_adds_up(timed_out)

    assert pathfinder.route(start, end, negotiated_mode=False, per_net_timeout=60.0) is not None
    (connected,) = pathfinder.get_and_clear_per_call_timings()
    assert connected["succeeded"] is True
    assert connected["native_search_seconds"] > timed_out["native_search_seconds"]
    _assert_breakdown_adds_up(connected)


def test_breakdown_is_absent_without_tracing_and_leaves_copper_unchanged() -> None:
    """Tracing off keeps the historical shape; tracing on does not steer the search."""
    start, end = _via_pads()
    off, _ = _carpet_pathfinder(traced=False)
    copper_off = _copper(off.route(start, end, negotiated_mode=False, per_net_timeout=30.0))
    assert off.get_and_clear_per_call_timings() == []

    on, _ = _carpet_pathfinder(traced=True)
    copper_on = _copper(on.route(start, end, negotiated_mode=False, per_net_timeout=30.0))
    (record,) = on.get_and_clear_per_call_timings()
    assert all(key in record for key in BREAKDOWN_KEYS)
    assert copper_on == copper_off


def test_early_return_attempt_carries_no_breakdown() -> None:
    """An attempt that returns before any search records no stale split."""
    pathfinder, _ = _carpet_pathfinder(traced=True)
    start, end = _via_pads()
    pathfinder.route(start, end, negotiated_mode=False, per_net_timeout=30.0)
    pathfinder.get_and_clear_per_call_timings()

    pathfinder._rules.allowed_layers = {"In1.Cu"}  # F.Cu/B.Cu pads: no legal start layer
    pathfinder.route(start, end, negotiated_mode=False, per_net_timeout=30.0)
    (record,) = pathfinder.get_and_clear_per_call_timings()
    assert not any(key in record for key in BREAKDOWN_KEYS)
    assert "searches" not in record
