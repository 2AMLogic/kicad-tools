"""Board 06 USB3 RX1/TX1 victim failures: budget stop vs geometric seal (issue #9900).

The seed-42 pure-Python leg (archived run 44961232189, source ``97886da3``)
reached targeted rip-up for all four USB3_RX1/TX1 conductors and recorded::

    = targeted rip-up for USB3_RX1+ (pair attempt 1/2): victim_geometric;
      ripped USB2_D+, USB2_D-, USB_CC2
    = targeted rip-up for USB3_RX1- (pair attempt 1/2): sibling_failed;
      ripped USB2_D+, USB2_D-, USB_CC2; USB2_D- could not re-land
    = targeted rip-up for USB3_TX1+ ...: victim_geometric; ripped
      USB2_D+, USB2_D-, USB_CC1
    = targeted rip-up for USB3_TX1- ...: victim_geometric; ripped USB_CC1

The focused boards reproduce a legal route that exceeds the Python
search budget, a displaced sibling that cannot re-land, and a sealed
clearance corridor that drains the search. The saved Board 06 regression
fixture exercises the eight signal identities on committed copper. The
two outputs of archive 44961232189 are separate physical boards.

The reduced fixture preserves the traced classes' width/clearance (USB3
0.275/0.15, USB2 0.250/0.15), the 0.05 mm grid pitch, the per-net
node-expansion budget mechanism (a scaled cap stands in for the 1M value so
the tests stay seconds-fast) and the ``max_ripups_per_net=3`` retry budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.algorithms.negotiated import (
    RIPUP_SIBLING_FAILED,
    RIPUP_VICTIM_BUDGET,
    RIPUP_VICTIM_GEOMETRIC,
    RIPUP_VICTIM_REFUSED,
    NegotiatedRouter,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Obstacle, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

VICTIM = 1
BLOCKER = 2

# The traced net classes (boards/06-diffpair-test/generate_design.py holds
# the USB3 width at 0.275 and USB2 at 0.250; both clear at 0.150).
USB3_CLASS = NetClassRouting(name="USB3", trace_width=0.275, clearance=0.15)
USB2_CLASS = NetClassRouting(name="USB2", trace_width=0.250, clearance=0.15)

# Deterministic-budget stand-ins (the archived run used 1,000,000): CUT is
# below the fixture basin's sweep cost, FULL is above it.  Calibrated on the
# 20x6 mm fixture: the gap sweep needs ~60k expansions, 5k stops it with the
# frontier open, 240k completes it in ~2s.
CUT_CAP = 5_000
FULL_CAP = 240_000

BOARD = Path(__file__).resolve().parents[2] / "boards" / "06-diffpair-test"
SAVED_RESULT = BOARD / "regression-fixture" / "diffpair_test_routed.kicad_pcb"

SAVED_SIGNALS = (
    "USB3_RX1+",
    "USB3_RX1-",
    "USB3_TX1+",
    "USB3_TX1-",
    "USB2_D+",
    "USB2_D-",
    "USB_CC1",
    "USB_CC2",
)


def _rules() -> DesignRules:
    return DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.25,
        via_diameter=0.45,
        min_trace_width=0.1016,
    )


def _add_pad(router, ref, pin, x, y, net, net_name, *, layer=Layer.F_CU):
    router.add_component(
        ref,
        [
            {
                "number": pin,
                "x": x,
                "y": y,
                "width": 0.4,
                "height": 0.4,
                "net": net,
                "net_name": net_name,
                "layer": layer,
            }
        ],
    )


def _wall(router, *, gap: bool) -> None:
    """A no-net keepout wall across the board at x=10; ``gap`` opens a corridor.

    Without the gap the only crossing would violate the 0.150 mm class
    clearance floor: adjacent keepouts (0.2 mm squares at 0.3 mm pitch with
    0.15 mm clearance) leave no legal cell column between them on either
    routable layer -- the negative control's "only available route violates
    clearance" shape.  With the gap the corridor is legal but a search that
    does not know where it is must sweep the whole west-side basin to find
    it, which is what the Board 06 J1 fanout corridors demand of the
    pure-Python A* under the shared node-expansion budget.
    """
    for i in range(18):
        y = 0.5 + i * 0.3
        if gap and 3.8 <= y <= 5.3:
            continue
        for lay in (Layer.F_CU, Layer.B_CU):
            router.grid.add_obstacle(
                Obstacle(x=10.0, y=y, width=0.2, height=0.2, layer=lay, clearance=0.15)
            )


def _classes(router: Autorouter) -> None:
    router.router.net_class_map = {
        router.net_names[VICTIM]: USB3_CLASS,
        router.net_names[BLOCKER]: USB2_CLASS,
    }


def _crossing_board() -> Autorouter:
    """VICTIM straddles the wall: its route must find the gap (basin sweep).

    The RX1+/TX1+/TX1- reduction -- the victim's own search, not a
    sibling's, is what the budget cuts.
    """
    router = Autorouter(20.0, 6.0, rules=_rules(), force_python=True, physics_enabled=False)
    _add_pad(router, "JX", "1", 1.5, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "UX", "1", 16.0, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "BX", "1", 6.0, 3.0, BLOCKER, "BLOCKER")
    _add_pad(router, "BX", "2", 8.0, 3.0, BLOCKER, "BLOCKER")
    _wall(router, gap=True)
    _classes(router)
    return router


def _local_board() -> Autorouter:
    """Scale the RX1− transaction to a local victim and a three-pad USB2_D−."""
    router = Autorouter(20.0, 6.0, rules=_rules(), force_python=True, physics_enabled=False)
    _add_pad(router, "J1", "B10", 1.5, 3.0, VICTIM, "USB3_RX1-")
    _add_pad(router, "U2", "B6", 4.0, 3.0, VICTIM, "USB3_RX1-")
    _add_pad(router, "J1", "A7", 16.0, 3.0, BLOCKER, "USB2_D-")
    _add_pad(router, "J1", "B7", 17.0, 3.0, BLOCKER, "USB2_D-")
    _add_pad(router, "U1", "11", 6.0, 3.0, BLOCKER, "USB2_D-")
    _wall(router, gap=True)
    _classes(router)
    return router


def _sealed_board(*, force_python: bool = True) -> Autorouter:
    """The negative control: no gap, so every crossing violates clearance."""
    router = Autorouter(20.0, 6.0, rules=_rules(), force_python=force_python, physics_enabled=False)
    _add_pad(router, "JX", "1", 1.5, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "UX", "1", 16.0, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "BX", "1", 6.0, 3.0, BLOCKER, "BLOCKER")
    _add_pad(router, "BX", "2", 8.0, 3.0, BLOCKER, "BLOCKER")
    _wall(router, gap=False)
    _classes(router)
    return router


def _blocker_route(*, crossing: bool, net_name: str = "BLOCKER") -> Route:
    """Authored BLOCKER copper, pre-committed like the #6342 seal ring.

    ``crossing`` routes it through the gap (the re-land must re-find that
    corridor); otherwise it is a local stub west of the wall.
    """
    if crossing:
        segs = [
            Segment(x1=6.0, y1=4.45, x2=16.0, y2=4.45, width=0.25, layer=Layer.F_CU, net=BLOCKER),
            Segment(x1=6.0, y1=4.45, x2=6.0, y2=3.0, width=0.25, layer=Layer.F_CU, net=BLOCKER),
            Segment(x1=16.0, y1=4.45, x2=16.0, y2=3.0, width=0.25, layer=Layer.F_CU, net=BLOCKER),
        ]
    else:
        segs = [Segment(x1=6.0, y1=3.0, x2=8.0, y2=3.0, width=0.25, layer=Layer.F_CU, net=BLOCKER)]
    return Route(net=BLOCKER, net_name=net_name, segments=segs)


def _pads_by_net(router: Autorouter) -> dict[int, list]:
    return {net: [router.pads[k] for k in keys] for net, keys in router.nets.items() if net}


def _neg(router: Autorouter) -> NegotiatedRouter:
    return NegotiatedRouter(
        grid=router.grid, router=router.router, rules=router.rules, net_class_map={}
    )


def _run_ripup(
    router: Autorouter,
    net_routes: dict[int, list[Route]],
    *,
    victim=VICTIM,
    blocker=BLOCKER,
    timeout=None,
):
    neg = _neg(router)
    ok = neg.targeted_ripup(
        failed_net=victim,
        blocking_nets={blocker},
        net_routes=net_routes,
        routes_list=router.routes,
        pads_by_net=_pads_by_net(router),
        present_cost_factor=0.5,
        mark_route_callback=router._mark_route,
        ripup_history={},
        max_ripups_per_net=3,
        per_net_timeout=timeout,
    )
    return ok, neg.last_ripup_outcome


# ---------------------------------------------------------------------------
# The victim budget stop (the RX1+/TX1+/TX1- reduction)
# ---------------------------------------------------------------------------


def test_cut_cap_stops_the_victim_search_without_a_no_path_proof():
    """The reduced failure mode: legal corridor present, search cut at budget."""
    router = _crossing_board()
    pads = _pads_by_net(router)[VICTIM]
    router.router._max_iterations_override = CUT_CAP
    route = router.router.route(pads[0], pads[1])
    assert route is None
    # The open frontier was cut by the iteration cap, not drained: no
    # unreachability proof exists (the #6098 flag).
    assert router.router.last_route_exhausted is False


def test_a_legal_route_exists_on_the_same_state_at_full_budget():
    """The moves the cut search rejected were electrically legal.

    Raising ONLY the node-expansion cap (the deterministic budget) on the
    same grid state lands the victim -- the fixture-scale witness of the
    archived C++-vs-Python divergence (C++ connects USB3_RX1+ on the same
    board the Python cap cut).
    """
    router = _crossing_board()
    pads = _pads_by_net(router)[VICTIM]
    router.router._max_iterations_override = FULL_CAP
    route = router.router.route(pads[0], pads[1])
    assert route is not None


def test_targeted_ripup_records_victim_budget_and_rolls_back():
    """The transaction tells the truth and preserves the board (#9900).

    With ``BLOCKER`` ripped and the victim's search stopped at its budget,
    the outcome must be ``victim_budget`` -- the pre-#9900 code called this
    ``victim_geometric`` ("no path with blockers ripped"), a seal that was
    never demonstrated.  The #3470 verbatim rollback is unchanged.
    """
    router = _crossing_board()
    blocker = _blocker_route(crossing=False)
    assert router._mark_route(blocker)
    net_routes: dict[int, list[Route]] = {BLOCKER: [blocker]}
    router.router._max_iterations_override = CUT_CAP
    ok, outcome = _run_ripup(router, net_routes)
    assert ok is False
    assert outcome.reason == RIPUP_VICTIM_BUDGET
    assert outcome.ripped == (BLOCKER,)
    # Rollback: the blocker's exact original copper is back, the victim
    # holds nothing, and the blocker is still marked on the grid.
    assert net_routes[BLOCKER] == [blocker]
    assert net_routes.get(VICTIM, []) == []
    probe = router.grid.world_to_grid(7.0, 3.0)
    assert router.grid.is_blocked_for_net(
        probe[0], probe[1], router.grid.layer_to_index(Layer.F_CU.value), VICTIM
    )


def test_expired_multi_pad_victim_cannot_inherit_a_previous_net_proof(monkeypatch):
    board = _sealed_board()
    local = 20
    for index, x in enumerate((1.5, 4.0, 7.0)):
        _add_pad(board, f"U{index}", "1", x, 1.0, local, "LOCAL")
    board.router._max_iterations_override = FULL_CAP
    assert board.router.route(*_pads_by_net(board)[VICTIM]) is None
    assert board.router.last_route_exhausted
    blocker = _blocker_route(crossing=False)
    assert board._mark_route(blocker)
    board.routes.append(blocker)
    routes = {BLOCKER: [blocker]}
    with monkeypatch.context() as scoped:
        scoped.setattr(
            board.router, "route", lambda *args, **kwargs: pytest.fail("expired edge searched")
        )
        ok, outcome = _run_ripup(board, routes, victim=local, timeout=1e-9)
    assert not ok and outcome.reason == RIPUP_VICTIM_BUDGET
    assert routes[BLOCKER][0] is blocker
    assert _run_ripup(board, routes, victim=local, timeout=30)[0]
    assert len(routes[local]) == 2


def test_later_drained_edge_preserves_an_earlier_budget_stop(monkeypatch):
    board = _sealed_board()
    local = 20
    for index, x in enumerate((1.5, 4.0, 16.0)):
        _add_pad(board, f"U{index}", "1", x, 1.0, local, "LOCAL")
    original = board.router.route
    drained = []

    def route(start, end, **options):
        crosses_wall = (start.x < 10) != (end.x < 10)
        board.router._max_iterations_override = FULL_CAP if crosses_wall else 1
        candidate = original(start, end, **options)
        drained.append(board.router.last_route_exhausted)
        return candidate

    monkeypatch.setattr(board.router, "route", route)
    blocker = _blocker_route(crossing=False)
    assert board._mark_route(blocker)
    routes = {BLOCKER: [blocker]}
    ok, outcome = _run_ripup(board, routes, victim=local)
    assert drained == [False, True]
    assert not ok and outcome.reason == RIPUP_VICTIM_BUDGET
    assert routes[BLOCKER][0] is blocker


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize(
    "fallback_cap,reason", [(64, RIPUP_VICTIM_BUDGET), (FULL_CAP, RIPUP_VICTIM_GEOMETRIC)]
)
def test_native_drain_and_python_fallback_must_both_prove_no_path(
    monkeypatch, fallback_cap, reason
):
    from kicad_tools.router.pathfinder import Router

    board = _sealed_board(force_python=False)
    original = Router._route_impl

    def capped(self, *args, **options):
        self._max_iterations_override = fallback_cap
        return original(self, *args, **options)

    monkeypatch.setattr(Router, "_route_impl", capped)
    blocker = _blocker_route(crossing=False)
    assert board._mark_route(blocker)
    routes = {BLOCKER: [blocker]}
    ok, outcome = _run_ripup(board, routes)
    assert board.router.get_last_failure_info()["failure_reason"] == 1
    assert board.router._py_router.last_route_exhausted == (reason == RIPUP_VICTIM_GEOMETRIC)
    assert not ok and outcome.reason == reason
    assert routes[BLOCKER][0] is blocker


@pytest.mark.parametrize("silent_rescue", [False, True])
@pytest.mark.parametrize("protect", [True, False])
def test_plane_commit_veto_has_its_own_outcome(tmp_path, protect, silent_rescue):
    from kicad_tools.router.access_ripup import RipupCycleGuard
    from tests.test_route_plane_zone_9597 import _board_text, _load, _sig_id

    source = tmp_path / "plane.kicad_pcb"
    source.write_text(_board_text())
    board = _load(source, force_python=silent_rescue)
    board.protect_plane_zones = protect
    board.router.net_class_map = board.net_class_map
    spare = 77
    for ref, y in [("B1", 8.0), ("B2", 12.0)]:
        _add_pad(board, ref, "1", 12.0, y, spare, "SPARE", layer=Layer.IN2_CU)
    blocker = Route(spare, "SPARE", segments=[Segment(12, 8, 12, 12, 0.20, Layer.IN2_CU, spare)])
    assert board._mark_route(blocker)
    board.grid.mark_route_usage(blocker)
    board.routes.append(blocker)
    routes = {spare: [blocker]}
    sig = _sig_id(board)
    if silent_rescue:
        worker = _neg(board)
        pads = _pads_by_net(board)
        assert worker.find_blocking_nets_for_connection(*pads[sig]) == {spare}
        ok = board._silent_rescue_one(
            sig,
            worker,
            routes,
            pads,
            0.5,
            30,
            lambda _line: None,
            lambda: "test",
            ripup_guard=RipupCycleGuard(),
            ripup_history={},
            max_ripups_per_net=3,
            relief_probe_attempts={},
            max_relief_probes_per_net=0,
            relief_deadline=None,
            deterministic_rescue=True,
        )
        outcome = worker.last_ripup_outcome
    else:
        ok, outcome = _run_ripup(board, routes, victim=sig, blocker=spare, timeout=30)
    if protect:
        assert not ok and outcome.reason == RIPUP_VICTIM_REFUSED
        assert "split zone [+3V3]" in board.plane_zone_refusals[0]
        assert routes[spare][0] is blocker
    else:
        assert ok and routes[_sig_id(board)] and routes[spare]


# ---------------------------------------------------------------------------
# The sibling re-land (the RX1- reduction)
# ---------------------------------------------------------------------------


def test_sibling_reland_failure_restores_both_three_pad_usb2_routes(monkeypatch):
    """A short re-land succeeds before the crossing exhausts its budget."""
    board = _local_board()
    local = Route(BLOCKER, "USB2_D-", segments=[Segment(16, 3, 17, 3, 0.25, Layer.F_CU, BLOCKER)])
    crossing = _blocker_route(crossing=True, net_name="USB2_D-")
    original = [local, crossing]
    for route in original:
        assert board._mark_route(route)
        board.grid.mark_route_usage(route)
        board.routes.append(route)
    routes = {BLOCKER: original.copy()}
    usage = board.grid._usage_count.copy()
    search = board.router.route
    attempts = []

    def observe(start, end, **options):
        candidate = search(start, end, **options)
        attempts.append(
            (start.net_name, candidate is not None, board.router.last_route_iteration_limited)
        )
        return candidate

    monkeypatch.setattr(board.router, "route", observe)
    board.router._max_iterations_override = CUT_CAP
    ok, outcome = _run_ripup(board, routes)
    assert not ok and outcome.reason == RIPUP_SIBLING_FAILED
    assert board.net_names[outcome.failed_sibling] == "USB2_D-"
    assert attempts == [
        ("USB3_RX1-", True, False),
        ("USB2_D-", True, False),
        ("USB2_D-", False, True),
    ]
    assert routes[BLOCKER][0] is local and routes[BLOCKER][1] is crossing
    assert board.routes[0] is local and board.routes[1] is crossing
    assert len(board.routes) == 2 and not routes.get(VICTIM)
    assert (board.grid._usage_count == usage).all()
    probe = board.grid.world_to_grid(11.0, 4.45)
    assert board.grid.is_blocked_for_net(
        *probe, board.grid.layer_to_index(Layer.F_CU.value), VICTIM
    )

    board.router._max_iterations_override = FULL_CAP
    assert _run_ripup(board, routes)[0]
    assert len(routes[VICTIM]) == 1 and len(routes[BLOCKER]) == 2


# ---------------------------------------------------------------------------
# The negative control: the only available route violates clearance
# ---------------------------------------------------------------------------


def test_sealed_corridor_drains_and_records_victim_geometric():
    """No legal corridor -> the search DRAINS -> the geometric reason stays.

    With the gap closed, every crossing violates the 0.150 mm class
    clearance floor on both routable layers.  At the full budget the search
    drains its open set (a true no-path proof), so the transaction keeps
    ``victim_geometric`` -- the label #9900 narrowed, not removed.  Ripping
    the local blocker cannot change that.
    """
    router = _sealed_board()
    pads = _pads_by_net(router)[VICTIM]
    router.router._max_iterations_override = FULL_CAP
    route = router.router.route(pads[0], pads[1])
    assert route is None
    assert router.router.last_route_exhausted is True, "a sealed corridor must drain"

    blocker = _blocker_route(crossing=False)
    assert router._mark_route(blocker)
    net_routes: dict[int, list[Route]] = {BLOCKER: [blocker]}
    ok, outcome = _run_ripup(router, net_routes)
    assert ok is False
    assert outcome.reason == RIPUP_VICTIM_GEOMETRIC
    assert net_routes[BLOCKER] == [blocker]


# ---------------------------------------------------------------------------
# The saved result: both polarities of RX1 and TX1 plus the displaced nets
# ---------------------------------------------------------------------------


def test_committed_board_connects_all_four_usb3_polarities_and_displaced_nets():
    """The repository regression fixture has physical copper for all eight nets."""
    from kicad_tools.analysis.net_status import NetStatusAnalyzer
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(SAVED_RESULT))
    result = NetStatusAnalyzer(pcb).analyze()
    complete = {status.net_name for status in result.complete}
    for name in SAVED_SIGNALS:
        status = result.get_net(name)
        assert status is not None, f"{name} missing from the saved result"
        assert name in complete, (
            f"{name}: {status.connected_count}/{status.total_connections} connected"
        )


@pytest.mark.requires_router_cpp
def test_cpp_backend_routes_the_corridor_the_python_cap_cut():
    """Backend cross-check: C++ lands the corridor the cut search rejected.

    The two backends use the same reduced board and net classes.
    """
    router = Autorouter(20.0, 6.0, rules=_rules(), physics_enabled=False)
    _add_pad(router, "JX", "1", 1.5, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "UX", "1", 16.0, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "BX", "1", 6.0, 3.0, BLOCKER, "BLOCKER")
    _add_pad(router, "BX", "2", 8.0, 3.0, BLOCKER, "BLOCKER")
    _wall(router, gap=True)
    _classes(router)
    pads = _pads_by_net(router)[VICTIM]
    route = router.router.route(pads[0], pads[1])
    assert route is not None
