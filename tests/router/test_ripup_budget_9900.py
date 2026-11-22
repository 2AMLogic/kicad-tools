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

Replaying those transactions on the reconstructed pre-retry state (the
iteration-0 copper of the 12 nets the archived run committed) shows every
failed victim search stopped at the deterministic 1,000,000-node-expansion
cap with the frontier still open -- never a drained no-path proof -- and the
C++ backend routes the same four nets on the same grid state in seconds
(USB3_RX1+ needs ~96k expansions under C++ vs >3M under pure Python; every
cell of the C++ route's 2,127-cell corridor is passable under the Python
grid's own ``is_blocked_for_net`` predicate).  ``victim_geometric`` therefore
claimed a geometric seal that was never demonstrated: the cause is the pure
Python A*'s search-capacity under the shared node-expansion budget, an
intentional backend limitation (the 10-100x C++ speedup is why
``--deterministic-budget`` exists), not board geometry.

This module pins the repair and the reduction at fixture scale:

* a victim whose search stops at its (here scaled-down) deterministic cap
  while a legal route exists records ``victim_budget``, not
  ``victim_geometric`` (``NegotiatedRouter._victim_failure_is_proven``);
* the transaction still rolls back verbatim -- existing copper, pair
  constraints and the manufacturing clearance floor are preserved;
* a sibling re-land that cannot return under the same budget keeps the
  ``sibling_failed`` outcome and its rollback (the archived USB3_RX1-
  shape: victim lands, USB2_D- cannot re-land);
* the negative control -- the only available route violates the class
  clearance -- drains the search and still records ``victim_geometric``;
* the saved C++ seed-42 result keeps all four USB3_RX1/TX1 polarities AND
  every displaced USB2/CC signal connected.

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


def _add_pad(router, ref, pin, x, y, net, net_name):
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
                "layer": Layer.F_CU,
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
    router.router.net_class_map = {"VICTIM": USB3_CLASS, "BLOCKER": USB2_CLASS}


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
    """VICTIM stays west of the wall; BLOCKER straddles it.

    The RX1- reduction -- once BLOCKER is ripped the victim lands cheaply,
    and the re-land that fails is BLOCKER's own basin sweep.
    """
    router = Autorouter(20.0, 6.0, rules=_rules(), force_python=True, physics_enabled=False)
    _add_pad(router, "JX", "1", 1.5, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "JX", "2", 4.0, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "BX", "1", 6.0, 3.0, BLOCKER, "BLOCKER")
    _add_pad(router, "BX", "2", 16.0, 3.0, BLOCKER, "BLOCKER")
    _wall(router, gap=True)
    _classes(router)
    return router


def _sealed_board() -> Autorouter:
    """The negative control: no gap, so every crossing violates clearance."""
    router = Autorouter(20.0, 6.0, rules=_rules(), force_python=True, physics_enabled=False)
    _add_pad(router, "JX", "1", 1.5, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "UX", "1", 16.0, 3.0, VICTIM, "VICTIM")
    _add_pad(router, "BX", "1", 6.0, 3.0, BLOCKER, "BLOCKER")
    _add_pad(router, "BX", "2", 8.0, 3.0, BLOCKER, "BLOCKER")
    _wall(router, gap=False)
    _classes(router)
    return router


def _blocker_route(*, crossing: bool) -> Route:
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
    return Route(net=BLOCKER, net_name="BLOCKER", segments=segs)


def _pads_by_net(router: Autorouter) -> dict[int, list]:
    return {net: [router.pads[k] for k in keys] for net, keys in router.nets.items() if net}


def _neg(router: Autorouter) -> NegotiatedRouter:
    return NegotiatedRouter(
        grid=router.grid, router=router.router, rules=router.rules, net_class_map={}
    )


def _run_ripup(router: Autorouter, net_routes: dict[int, list[Route]]):
    neg = _neg(router)
    ok = neg.targeted_ripup(
        failed_net=VICTIM,
        blocking_nets={BLOCKER},
        net_routes=net_routes,
        routes_list=router.routes,
        pads_by_net=_pads_by_net(router),
        present_cost_factor=0.5,
        mark_route_callback=router._mark_route,
        ripup_history={},
        max_ripups_per_net=3,
        per_net_timeout=None,
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


# ---------------------------------------------------------------------------
# The sibling re-land (the RX1- reduction)
# ---------------------------------------------------------------------------


def test_sibling_reland_failure_rolls_back_with_reason_sibling_failed():
    """Victim lands, blocker cannot re-land under the same budget.

    The archived USB3_RX1- transaction: the victim routed with the blockers
    ripped, then USB2_D- could not re-land and the whole transaction rolled
    back.  Here the victim's two pads are local (cheap route once the
    blocker is ripped) while the blocker's re-land must sweep the gap basin
    the cap cuts -- with the rollback preserving the blocker verbatim.
    """
    router = _local_board()
    blocker = _blocker_route(crossing=True)
    assert router._mark_route(blocker)
    net_routes: dict[int, list[Route]] = {BLOCKER: [blocker]}
    router.router._max_iterations_override = CUT_CAP
    ok, outcome = _run_ripup(router, net_routes)
    assert ok is False
    assert outcome.reason == RIPUP_SIBLING_FAILED
    assert outcome.failed_sibling == BLOCKER
    # The #3470 rollback: the victim's fresh copper is gone, the blocker's
    # exact original object is restored.
    assert net_routes[BLOCKER] == [blocker]
    assert net_routes.get(VICTIM, []) == []
    probe = router.grid.world_to_grid(11.0, 4.45)
    assert router.grid.is_blocked_for_net(
        probe[0], probe[1], router.grid.layer_to_index(Layer.F_CU.value), VICTIM
    )


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


def test_saved_cpp_result_connects_all_four_usb3_polarities_and_displaced_nets():
    """Per-signal connectivity on the committed seed-42 routed artifact.

    The archived C++ leg's 21/21 result is what the pure-Python leg failed
    to reach; the committed regression artifact carries it.  Every USB3
    RX1/TX1 polarity -- and every USB2/CC signal the traced transactions
    displaced -- must be connected by real geometric copper contact.
    """
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

    The full-scale divergence (C++ ~96k expansions vs Python >3M on
    USB3_RX1+ on the identical grid state) at fixture scale: same board,
    same classes, default C++ backend -- it connects immediately.
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
