"""Known-failure memo: rescue skip and per-net search back-off (issue #8486).

On the reported 97-net board the same 15 nets failed from iteration 2 on.
#8248 already skipped the repeat *relief* for the nine sealed ones, but every
iteration still (1) searched all 15 at the full 30 s per-net cap and (2) re-ran
the overflow-silent rescue's targeted rip-up, which was most of its cost.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.router.access_ripup import (
    KNOWN_FAILURE_BACKOFF_AFTER,
    KNOWN_FAILURE_BACKOFF_FLOOR_S,
    KNOWN_FAILURE_MIN_SEARCH_SLICE_S,
    KNOWN_FAILURE_REPROBE_EVERY,
    RipupCycleGuard,
)

from .test_relief_cycle_6363 import VICTIM, _fixture

SEAL = ("seal", ((("Q1", "2"),), ("/GATE", "Q1.1", "Q1.3")))
OTHER = ("seal", ((("Q1", "2"),), ("Q1.1", "Q1.3")))


def _fail_capped(g: RipupCycleGuard, it: int, cap: float, sig=SEAL, net: int = 7) -> float | None:
    """One iteration in which ``net`` enters under ``sig`` and spends its cap."""
    g.begin_iteration(it, {net: sig})
    used = g.search_cap(net, it, cap)
    g.note_search(net, it, used, used if used is not None else 0.0, routed=False)
    return used


# --- back-off rule ----------------------------------------------------------


def test_budget_halves_after_consecutive_capped_failures_under_one_seal():
    g = RipupCycleGuard()
    caps = [_fail_capped(g, it, 30.0) for it in range(1, 7)]
    assert KNOWN_FAILURE_BACKOFF_AFTER == 2
    assert caps == [30.0, 30.0, 15.0, 7.5, KNOWN_FAILURE_BACKOFF_FLOOR_S, 5.0]
    assert [it for it, _net, _cap in g.backoffs] == [3, 4, 5, 6]


def test_backoff_resets_when_the_seal_changes():
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0)
    assert _fail_capped(g, 3, 30.0) == 15.0
    assert _fail_capped(g, 4, 30.0, sig=OTHER) == 30.0  # changed: full budget
    assert _fail_capped(g, 5, 30.0, sig=OTHER) == 30.0  # streak restarts at 1
    assert _fail_capped(g, 6, 30.0, sig=OTHER) == 15.0


def test_backoff_resets_on_route_fast_failure_gap_or_missing_signature():
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0)
    # Routed: streak gone, and begin_iteration forgets a net no longer failed.
    g.begin_iteration(3, {7: SEAL})
    g.note_search(7, 3, g.search_cap(7, 3, 30.0), 1.0, routed=True)
    assert 7 not in g.capped_streak
    for it in (4, 5):
        _fail_capped(g, it, 30.0)
    # A fast failure keeps the streak (#8486 sub-cap back-off) ...
    g.begin_iteration(6, {7: SEAL})
    assert g.search_cap(7, 6, 30.0) == 15.0
    g.note_search(7, 6, 15.0, 0.4, routed=False)
    assert g.capped_streak[7][2] == 3
    # ... but a missing signature, or a gap, restarts it.
    g.begin_iteration(7, {7: None})
    g.note_search(7, 7, 30.0, 0.4, routed=False)
    assert 7 not in g.capped_streak
    # A gap (not searched in iteration N-1) restarts the count.
    for it in (8, 9):
        _fail_capped(g, it, 30.0)
    assert _fail_capped(g, 11, 30.0) == 30.0
    # No signature (no terminals, error) never backs off.
    for it in (12, 13, 14):
        assert _fail_capped(g, it, 30.0, sig=None) == 30.0


def test_routed_net_drops_every_memo_at_the_next_iteration():
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0)
    g.note_futile_rescue(7, SEAL)
    g.begin_iteration(3, {})  # net 7 routed during iteration 2
    assert 7 not in g.capped_streak and 7 not in g.futile_rescue
    assert 7 not in g.full_search_iter


def test_deterministic_arm_cap_none_is_untouched():
    g = RipupCycleGuard()
    for it in range(1, 6):
        assert _fail_capped(g, it, None) is None  # type: ignore[arg-type]
    assert g.backoffs == []


def test_known_fail_seconds_are_reported_per_iteration():
    g = RipupCycleGuard()
    for it in (1, 2, 3):
        _fail_capped(g, it, 30.0)
    # Iteration 1 was the first failure; 2 and 3 searched a known failure.
    assert g.known_fail_seconds == {2: 30.0, 3: 15.0}
    line = g.known_failure_line()
    assert "iter 2: 30.0s" in line and "iter 3: 15.0s" in line
    assert "1 search(es) backed off" in line


# --- the search wrapper -------------------------------------------------------


def test_reroute_wrapper_passes_the_backed_off_cap_and_feeds_the_outcome():
    router, _neg, _net_routes, _pads = _fixture()
    g = RipupCycleGuard()
    clock = [0.0]
    seen: list[float | None] = []

    def fake_route(net, pf, per_net_timeout=None):
        seen.append(per_net_timeout)
        clock[0] += per_net_timeout or 0.0  # runs to its cap
        return []

    fake_time = SimpleNamespace(time=lambda: clock[0])
    with (
        patch.object(router, "_route_net_negotiated", side_effect=fake_route),
        patch("kicad_tools.router.core.time", fake_time),
    ):
        for it in (1, 2, 3, 4):
            g.begin_iteration(it, {VICTIM: None})
            assert router._route_net_known_failure_aware(VICTIM, 1.0, 30.0, g, it) == []
    assert seen == [30.0, 30.0, 15.0, 7.5]


# --- overflow-silent rescue memo ------------------------------------------------


def _memoized(router, neg, net_routes, pads_by_net, guard, log, iteration):
    return router._silent_rescue_memoized(
        VICTIM,
        iteration,
        neg,
        net_routes,
        pads_by_net,
        1.0,
        30.0,
        log.append,
        lambda: "0s",
        ripup_guard=guard,
        ripup_history={},
        max_ripups_per_net=3,
        relief_probe_attempts=guard.relief_attempts,
        max_relief_probes_per_net=3,
        relief_deadline=None,
        deterministic_rescue=False,
    )


def test_futile_rescue_is_skipped_until_the_seal_changes():
    router, neg, net_routes, pads_by_net = _fixture()
    g = RipupCycleGuard()
    sigs = iter([SEAL, SEAL, SEAL, SEAL, OTHER, OTHER])
    calls: list[int] = []
    log: list[str] = []

    def rescue(net, *a, **kw):
        calls.append(net)
        return False

    with (
        patch.object(router, "_silent_rescue_one", side_effect=rescue),
        patch.object(router, "_relief_witness_signature", side_effect=lambda *a: next(sigs)),
    ):
        assert _memoized(router, neg, net_routes, pads_by_net, g, log, 2) is False  # runs
        assert _memoized(router, neg, net_routes, pads_by_net, g, log, 3) is False  # skip
        assert _memoized(router, neg, net_routes, pads_by_net, g, log, 4) is False  # skip
        assert _memoized(router, neg, net_routes, pads_by_net, g, log, 5) is False  # changed
    assert calls == [VICTIM, VICTIM]
    assert g.futile_rescue_skips == {VICTIM: 2}
    assert sum("skipped" in m and "#8486" in m for m in log) == 1  # logged once
    assert g.futile_rescue[VICTIM] == OTHER


def test_resolved_rescue_clears_the_memo_and_none_never_skips():
    router, neg, net_routes, pads_by_net = _fixture()
    g = RipupCycleGuard()
    outcomes = iter([False, True])
    with (
        patch.object(router, "_silent_rescue_one", side_effect=lambda *a, **k: next(outcomes)),
        patch.object(router, "_relief_witness_signature", return_value=SEAL),
    ):
        _memoized(router, neg, net_routes, pads_by_net, g, [], 2)
        g.futile_rescue.clear()  # a neighbour changed meanwhile: re-run
        assert _memoized(router, neg, net_routes, pads_by_net, g, [], 3) is True
    assert VICTIM not in g.futile_rescue

    calls: list[int] = []
    with (
        patch.object(router, "_silent_rescue_one", side_effect=lambda n, *a, **k: calls.append(n)),
        patch.object(router, "_relief_witness_signature", return_value=None),
    ):
        for it in (4, 5, 6):
            _memoized(router, neg, net_routes, pads_by_net, g, [], it)
    assert calls == [VICTIM] * 3


def test_skipped_rescue_does_not_spend_relief_attempts():
    """The real step chain: the first rescue charges one relief attempt, the
    skipped repeats charge none, so a later changed seal still has budget."""
    router, neg, net_routes, pads_by_net = _fixture()
    g = RipupCycleGuard()
    with (
        patch.object(router, "_relief_probe", return_value=([], set())),
        patch.object(router, "_relief_witness_signature", return_value=SEAL),
    ):
        for it in (2, 3, 4):
            router._commit_journal.iteration = it
            assert _memoized(router, neg, net_routes, pads_by_net, g, [], it) is False
    assert g.relief_attempts.get(VICTIM, 0) == 1
    assert g.futile_rescue_skips == {VICTIM: 2}


# --- sub-cap back-off and coarse copper key (issue #8486, re-scoped) -----------


def _fail_for(g: RipupCycleGuard, it: int, cap: float, took: float, sig=SEAL) -> float | None:
    g.begin_iteration(it, {7: sig})
    used = g.search_cap(7, it, cap)
    g.note_search(7, it, used, took, routed=False)
    return used


def test_sub_cap_failure_backs_off_bounded_by_its_own_time():
    g = RipupCycleGuard()
    caps = [_fail_for(g, it, 30.0, 12.0) for it in (1, 2, 3, 4)]
    # Never raised above the cap; bounded by the last failing time (12 s) once
    # the streak is established, then halving, floored at 5 s.
    assert caps == [30.0, 30.0, 12.0, 7.5]
    # Floor: a 1 s failure never cuts a search below the floor.
    g2 = RipupCycleGuard()
    caps = [_fail_for(g2, it, 30.0, 1.0) for it in (1, 2, 3)]
    assert caps == [30.0, 30.0, KNOWN_FAILURE_BACKOFF_FLOOR_S]
    # A smaller cap is never raised.
    g3 = RipupCycleGuard()
    assert [_fail_for(g3, it, 3.0, 1.0) for it in (1, 2, 3)] == [3.0, 3.0, 3.0]


def test_sub_cap_streak_resets_on_route_or_signature_change_and_report_counts_it():
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_for(g, it, 30.0, 12.0)
    assert _fail_for(g, 3, 30.0, 12.0, sig=OTHER) == 30.0
    for it in (4, 5):
        _fail_for(g, it, 30.0, 12.0, sig=OTHER)
    g.begin_iteration(6, {7: OTHER})
    g.note_search(7, 6, g.search_cap(7, 6, 30.0), 2.0, routed=True)
    assert 7 not in g.capped_streak and 7 not in g.last_fail_s
    assert "search(es) backed off" in g.known_failure_line()
    assert f"{len(g.backoffs)} search(es) backed off" in g.known_failure_line() and g.backoffs


def test_coarse_copper_rescue_memo_is_reprobed_every_k_iterations():
    from kicad_tools.router.access_ripup import KNOWN_FAILURE_REPROBE_EVERY

    COPPER = ("copper", ((2, "F.Cu"),))
    g = RipupCycleGuard()
    g.note_futile_rescue(7, COPPER, 2)
    hits = [
        g.repeats_futile_rescue(7, COPPER, it) for it in range(3, 3 + KNOWN_FAILURE_REPROBE_EVERY)
    ]
    assert hits == [True, True, False]  # iteration 2 + K re-probes
    # A seal signature is exact and never expires.
    g.note_futile_rescue(7, SEAL, 2)
    assert g.repeats_futile_rescue(7, SEAL, 50)


def test_copper_key_survives_nudges_but_not_a_new_foreign_net():
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router, _neg, net_routes, _pads = _fixture()

    def seg(net, y, x2):
        return Route(
            net=net,
            net_name=f"N{net}",
            segments=[Segment(x1=8.0, y1=y, x2=x2, y2=y, width=0.2, layer=Layer.F_CU, net=net)],
        )

    # Victim terminals sit at y=7; the 3 mm window spans y~3.75..10.25.
    base = dict(net_routes)
    base[2], base[3] = [seg(2, 8.0, 30.0)], [seg(3, 11.0, 30.0)]  # 3 is outside
    key = router._failure_neighbourhood_print(VICTIM, base)
    assert key is not None
    nudged = dict(base)
    nudged[2] = [seg(2, 8.4, 28.0)]  # same net, same layer, moved
    assert router._failure_neighbourhood_print(VICTIM, nudged) == key
    entered = dict(base)
    entered[3] = [seg(3, 9.0, 30.0)]  # net 3 enters the window
    assert router._failure_neighbourhood_print(VICTIM, entered) != key


# --- setup-bound back-off floor (issue #8644) ---------------------------------


def test_floor_rises_above_setup_cost_so_a_backed_off_net_reaches_the_search():
    """A net whose search keeps stopping in mask setup at the floor is given
    setup time + a minimum search slice next time, until setup completes."""
    setup_s = 6.0  # HV mask setup cost, a little above the 5 s floor
    g = RipupCycleGuard()
    caps: list[float | None] = []
    reached_search = False
    for it in range(1, 9):
        g.begin_iteration(it, {7: SEAL})
        used = g.search_cap(7, it, 30.0)
        caps.append(used)
        assert used is not None
        stopped = used < setup_s  # setup eats the whole budget
        if not stopped:
            assert used - setup_s >= KNOWN_FAILURE_MIN_SEARCH_SLICE_S or used == 30.0
            reached_search = True
            break
        g.note_search(7, it, used, used, routed=False, setup_stopped=True)
    assert reached_search, f"never reached A*: caps={caps}"
    assert caps[-1] > KNOWN_FAILURE_BACKOFF_FLOOR_S


def test_floor_is_unchanged_without_a_setup_stop_and_never_exceeds_cap():
    g = RipupCycleGuard()
    for it in (1, 2, 3, 4, 5):
        _fail_capped(g, it, 30.0)
    assert g.search_cap(7, 6, 30.0) == KNOWN_FAILURE_BACKOFF_FLOOR_S
    g.begin_iteration(6, {7: SEAL})
    g.note_search(7, 6, 5.0, 5.0, routed=False, setup_stopped=True)
    g.begin_iteration(7, {7: SEAL})
    assert g.search_cap(7, 7, 6.0) <= 6.0
    assert g.search_cap(7, 7, None) is None


def test_reroute_wrapper_reports_a_setup_stop_to_the_guard():
    router, _neg, _net_routes, _pads = _fixture()
    g = RipupCycleGuard()
    router.router = SimpleNamespace(last_route_deadline_hit=True, last_route_deadline_iterations=0)
    clock = [0.0]

    def fake_route(net, pf, per_net_timeout=None):
        clock[0] += per_net_timeout or 0.0
        return []

    with (
        patch.object(router, "_route_net_negotiated", side_effect=fake_route),
        patch("kicad_tools.router.core.time", SimpleNamespace(time=lambda: clock[0])),
    ):
        for it in (1, 2, 3, 4):
            g.begin_iteration(it, {VICTIM: None})
            router._route_net_known_failure_aware(VICTIM, 1.0, 30.0, g, it)
    assert g.setup_floor_s[VICTIM] > KNOWN_FAILURE_BACKOFF_FLOOR_S


def test_reroute_wrapper_reads_setup_stop_from_cpp_backend_py_router():
    """CppPathfinder-shaped stub: the flags exist only on ``_py_router``."""
    router, _neg, _net_routes, _pads = _fixture()
    g = RipupCycleGuard()
    py_router = SimpleNamespace(last_route_deadline_hit=True, last_route_deadline_iterations=0)
    router.router = SimpleNamespace(_py_router=py_router)
    clock = [0.0]

    def fake_route(net, pf, per_net_timeout=None):
        clock[0] += per_net_timeout or 0.0
        return []

    with (
        patch.object(router, "_route_net_negotiated", side_effect=fake_route),
        patch("kicad_tools.router.core.time", SimpleNamespace(time=lambda: clock[0])),
    ):
        for it in (1, 2, 3, 4):
            g.begin_iteration(it, {VICTIM: None})
            router._route_net_known_failure_aware(VICTIM, 1.0, 30.0, g, it)
    assert g.setup_floor_s[VICTIM] > KNOWN_FAILURE_BACKOFF_FLOOR_S


# --- Nov 2026 re-scope: why the narrow nets' streak never survived -------------
#
# Root cause, from the synthetic-board reproduction below and the reported
# run's shape: on a zero-overflow-stalled board the negotiated loop searches a
# still-unrouted net TWICE per iteration -- the zero-overflow recovery
# (``route_all_negotiated``) and the targeted-ripup fallback re-route -- and
# neither site went through ``_route_net_known_failure_aware``, so
# ``note_search`` never ran: the streak was not reset, it never STARTED (the
# reporter's "five to eight retained full-budget searches per iteration and
# only one narrow net ever backed off").  Where a search did reach the
# wrapper, the exact-match + consecutive-iteration rule in ``_known_capped``
# restarted the streak on every coarse-copper-key wobble (the reported six
# narrow signal nets) or on any iteration the loop searched the net elsewhere.


COPPER_A = ("copper", ((2, "F.Cu"),))
COPPER_B = ("copper", ((2, "F.Cu"), (3, "F.Cu")))  # a foreign net entered the window
COPPER_C = ("copper", ((2, "F.Cu"), (4, "via")))  # and churned again


def test_copper_streak_survives_key_wobble_and_iteration_gaps():
    """The reported narrow-net shape: the copper key wobbles every re-route
    pass and an iteration passes without a wrapper search; the streak (and
    therefore the back-off) survives, bounded by the K-iteration re-probe."""
    g = RipupCycleGuard()
    assert _fail_capped(g, 1, 30.0, sig=COPPER_A) == 30.0
    assert _fail_capped(g, 2, 30.0, sig=COPPER_B) == 30.0  # wobble: still extends
    # Iteration 3 searched the net outside the wrapper (a gap in the record).
    assert _fail_capped(g, 4, 30.0, sig=COPPER_C) == 15.0  # gap + wobble: backs off
    assert _fail_capped(g, 5, 30.0, sig=COPPER_A) == 30.0  # bounded full re-probe
    assert _fail_capped(g, 6, 30.0, sig=COPPER_B) == KNOWN_FAILURE_BACKOFF_FLOOR_S
    assert g.capped_streak[7][2] == 5
    # The wobble iterations still count their seconds as known-fail time.
    assert g.known_fail_seconds[2] == 30.0 and g.known_fail_seconds[4] == 15.0


def test_copper_backoff_reprobes_at_full_cap_every_k_iterations():
    """A coarse-key back-off never suppresses a full-budget retry for good:
    every ``KNOWN_FAILURE_REPROBE_EVERY`` iterations the net is searched at
    the full cap again, and a failing re-probe keeps the streak."""
    g = RipupCycleGuard()
    caps = [_fail_capped(g, it, 30.0, sig=COPPER_A) for it in range(1, 8)]
    assert KNOWN_FAILURE_REPROBE_EVERY == 3
    assert caps == [30.0, 30.0, 15.0, 7.5, 30.0, 5.0, 5.0]
    # A seal key is exact, so its back-off never expires (see the seal test
    # above: [30, 30, 15, 7.5, 5, 5] with no re-probe).


def test_removing_copper_present_at_the_first_failure_restarts_the_streak():
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0, sig=COPPER_A)
    assert _fail_capped(g, 3, 30.0, sig=COPPER_B) == 15.0
    # Net 3 is still present, but net 2 no longer occupies its original layer.
    changed = ("copper", ((2, "B.Cu"), (3, "F.Cu")))
    assert _fail_capped(g, 3, 30.0, sig=changed) == 30.0
    assert g.capped_streak[7] == (3, changed, 1)
    assert g.known_fail_seconds[3] == 15.0
    assert _fail_capped(g, 4, 30.0, sig=changed) == 30.0
    assert _fail_capped(g, 5, 30.0, sig=changed) == 15.0


def test_both_per_iteration_searches_of_a_copper_net_are_capped():
    """The loop searches a failed net twice in one iteration (zero-overflow
    recovery, then the reroute pass).  Both get backed-off caps -- the second
    one halving deeper, on the streak the first search just extended -- and
    both count as known-fail seconds; the streak itself extends once."""
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0, sig=COPPER_A)
    g.begin_iteration(3, {7: COPPER_B})
    first = g.search_cap(7, 3, 30.0)
    g.note_search(7, 3, first, first, routed=False)
    second = g.search_cap(7, 3, 30.0)
    g.note_search(7, 3, second, second, routed=False)
    assert first == 15.0 and second == 7.5
    assert g.capped_streak[7][2] == 3  # extended once, not twice
    assert g.known_fail_seconds[3] == 22.5  # both searches' seconds counted


def test_seal_copper_regime_change_resets_the_streak():
    """A seal witness appearing or disappearing changes the failure mode
    structurally: full retry, streak from scratch, in both directions."""
    g = RipupCycleGuard()
    for it in (1, 2):
        _fail_capped(g, it, 30.0, sig=COPPER_A)
    # The witness appears (copper -> seal).
    assert _fail_capped(g, 3, 30.0, sig=SEAL) == 30.0
    assert _fail_capped(g, 4, 30.0, sig=SEAL) == 30.0
    assert _fail_capped(g, 5, 30.0, sig=SEAL) == 15.0  # seal streak, exact key
    # The witness disappears again (seal -> copper).
    assert _fail_capped(g, 6, 30.0, sig=COPPER_A) == 30.0
    assert _fail_capped(g, 7, 30.0, sig=COPPER_B) == 30.0
    assert _fail_capped(g, 8, 30.0, sig=COPPER_A) == 15.0  # original copper still present


# --- negotiated-loop reproduction (Nov 2026 maintainer decision) ---------------
#
# The reporter's board is not in-repo; this synthetic board reproduces the
# reported shape: one unsealed 2-pad net whose only corridor is closed by a
# full-height wall of fixed through-hole pads, with ordinary healthy traffic
# around it.  Every rip-up iteration fails it (unsealed, so its memo is the
# coarse copper key), and before this fix each iteration searched it twice at
# the full per-net cap through sites the known-failure wrapper never saw.
# On a board this small each failing search returns in well under a second,
# so the assertions target the ASSIGNED caps and the guard's ledger, not
# wall-clock -- the multi-hour dense-board saving is the operator's
# re-benchmark on the reporter's board.


def _wall_board(
    gap: bool = False,
    *,
    terminal_x=(6.0, 24.0),
    terminal_y=(6.0, 14.0),
    gap_y=(9.4, 10.6),
    rules=None,
    width=30.0,
):
    from kicad_tools.router.core import Autorouter

    def pad(num, x, y, net, w=0.6, h=0.6, th=False):
        return {
            "number": str(num),
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "net": net,
            "net_name": f"N{net}",
            "through_hole": th,
            "drill": 0.4 if th else 0.0,
        }

    router = Autorouter(width=width, height=20.0, rules=rules)
    # The victim: net 1, one pad each side of the wall.
    router.add_component(
        "U1",
        [
            pad(1, terminal_x[0], terminal_y[0], 1),
            pad(2, terminal_x[1], terminal_y[1], 1),
        ],
    )
    # A full-height wall of overlapping through-hole pads at x=15 (each its
    # own single-pad net: fixed obstacles no rip-up can move).  ``gap`` opens
    # a corridor -- the negative control's real reachability change.
    wall = []
    i = 0
    y = 0.35
    while y < 19.8:
        i += 1
        wall.append(pad(i, 15.0, y, 100 + i, w=1.0, h=1.0, th=True))
        y += 0.85
    if gap:
        wall = [p for p in wall if not gap_y[0] < p["y"] < gap_y[1]]
    router.add_component("W1", wall)
    # Healthy traffic: eight 2-pad nets, four per half.
    net = 10
    for k in range(4):
        yl = 3.0 + k * 2.0
        router.add_component(f"HL{k}", [pad(1, 3.0, yl, net), pad(2, 11.0, yl + 0.4, net)])
        net += 1
    for k in range(4):
        yr = 3.0 + k * 2.0
        router.add_component(f"HR{k}", [pad(1, 19.0, yr, net), pad(2, 27.0, yr + 0.4, net)])
        net += 1
    return router


def _run_wall_board(gap: bool = False):
    router = _wall_board(gap=gap)
    routes = router.route_all_negotiated(
        max_iterations=4,
        timeout=120,
        per_net_timeout=20.0,
        use_targeted_ripup=True,
        best_stall_patience=None,
    )
    return router, {r.net for r in routes}


def _corridor_wall(net, extent=(0.35, 19.75), via_y=(9.70, 10.55)):
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment, Via

    layers = (Layer.F_CU, Layer.B_CU)
    return Route(
        net,
        "wall",
        segments=[Segment(15, extent[0], 15, extent[1], 0.90, layer, net) for layer in layers],
        vias=[Via(15, y, 0.5, 1.2, layers, net) for y in via_y],
    )


@pytest.mark.requires_router_cpp
def test_reopening_a_live_corridor_restores_the_first_search_budget():
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router = _wall_board(gap=True)
    blocker = _corridor_wall(99)
    remote = Route(101, "remote", segments=[Segment(8, 12, 8.5, 12, 0.2, Layer.F_CU, 101)])
    router.routes.append(blocker)
    router.grid.mark_route(blocker)
    guard = RipupCycleGuard()
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        for it in range(1, 6):
            if it in (3, 5):
                router.routes.append(remote)
                router.grid.mark_route(remote)
            elif it == 4:
                router.grid.unmark_route(remote)
                router.routes.remove(remote)
            guard.begin_iteration(it, {1: None})
            assert not router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
        caps = [call.kwargs["per_net_timeout"] for call in run.call_args_list]
        assert caps[:2] == [30.0, 30.0]
        assert all(5.0 <= cap <= 15.0 for cap in caps[2:4])
        assert caps[4] == 30.0  # Periodic full re-probe, with the wall still in place.

        # Full reorder can clear a corridor between two searches in one iteration.
        # The remote segment remains on the live board when the wall is ripped.
        router.grid.unmark_route(blocker)
        router.routes.remove(blocker)
        routed = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
        assert run.call_args.kwargs["per_net_timeout"] == 30.0
        assert routed and any(route.segments for route in routed)
        assert 1 not in guard.capped_streak

    open_board = _wall_board(gap=True)
    open_board.routes.append(remote)
    open_board.grid.mark_route(remote)
    expected = open_board._route_net_negotiated(1, 1.0, per_net_timeout=30.0)
    assert [(r.segments, r.vias) for r in routed] == [(r.segments, r.vias) for r in expected]


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("same_iteration", [False, True])
@pytest.mark.parametrize("terminal_x", [(6.0, 24.0), (2.0, 28.0)], ids=["interior", "edge"])
@pytest.mark.parametrize(
    "terminal_y, gap_y, wall_extent, via_y",
    [
        pytest.param((6.0, 14.0), (9.4, 10.6), (0.35, 19.75), (9.70, 10.55), id="between-pads"),
        pytest.param((9.0, 11.0), (15.15, 17.5), (15.45, 17.5), (15.65, 16.55, 17.3), id="detour"),
    ],
)
def test_removing_a_replacement_wall_restores_the_first_open_search(
    same_iteration, terminal_x, terminal_y, gap_y, wall_extent, via_y
):
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment, Via

    router = _wall_board(gap=True, terminal_x=terminal_x, terminal_y=terminal_y, gap_y=gap_y)
    guard = RipupCycleGuard()
    layers = (Layer.F_CU, Layer.B_CU)
    remote = Route(
        99,
        "remote",
        segments=[Segment(8, 12, 8.5, 12, 0.20, layer, 99) for layer in layers],
        vias=[Via(8, 12, 0.30, 0.60, layers, 99)],
    )
    original = _corridor_wall(99, wall_extent, via_y)
    replacement = _corridor_wall(201, wall_extent, via_y)
    for route in (remote, original):
        router.routes.append(route)
        router.grid.mark_route(route)
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        for it in (1, 2):
            guard.begin_iteration(it, {1: None})
            assert not router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
            assert run.call_args.kwargs["per_net_timeout"] == 30.0
        router.grid.unmark_route(original)
        router.routes.remove(original)
        router.routes.append(replacement)
        router.grid.mark_route(replacement)
        guard.begin_iteration(3, {1: None})
        assert not router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 3)
        assert 5.0 <= run.call_args.kwargs["per_net_timeout"] < 30.0

        # Net 99 still occupies both layers and has a via after the wall is gone.
        router.grid.unmark_route(replacement)
        router.routes.remove(replacement)
        it = 3 if same_iteration else 4
        if not same_iteration:
            guard.begin_iteration(it, {1: None})
        routes = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
        assert run.call_args.kwargs["per_net_timeout"] == 30.0
        assert _net_pad_pairs(router, 1, routes) == {frozenset({0, 1})}
        assert 1 not in guard.capped_streak

    fresh = _wall_board(gap=True, terminal_x=terminal_x, terminal_y=terminal_y, gap_y=gap_y)
    fresh.routes.append(remote)
    fresh.grid.mark_route(remote)
    expected = fresh._route_net_negotiated(1, 1.0, per_net_timeout=30.0)
    assert [(r.segments, r.vias) for r in routes] == [(r.segments, r.vias) for r in expected]


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize(("width", "height", "angle"), [(5, 0.5, 75), (0.5, 5, -15)])
def test_rotated_pads_leave_the_first_open_detour_at_full_budget(tmp_path, width, height, angle):
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    def fixed_pad(pin, x, y, w, h, rotation=0):
        return (
            f'(pad "{pin}" thru_hole rect (at {x} {y} {rotation}) (size {w} {h}) '
            '(drill 0.4) (layers "*.Cu" "*.Mask") (net 2 "FIXED"))'
        )

    # The vertical pad wall has a gap above the terminals. Six angled pads
    # below the gap leave the same passage under either KiCad encoding.
    fixed = []
    for index in range(24):
        y = 0.35 + 0.85 * index
        if y < 19.8 and not 15.15 < y < 17.5:
            fixed.append(fixed_pad(f"W{index}", 15, y, 1, 1))
    for index in range(6):
        fixed.append(fixed_pad(f"F{index}", 2.5 + 5 * index, 14.1, width, height, angle))
    board = tmp_path / "detour.kicad_pcb"
    board.write_text(
        "(kicad_pcb (version 20240108) (generator test)\n"
        '(net 0 "") (net 1 "SIG") (net 2 "FIXED")\n'
        '(gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))\n'
        '(footprint "Test" (layer "F.Cu") (at 0 0)\n'
        '(property "Reference" "J1" (at 0 0) (layer "F.SilkS"))\n'
        '(pad "1" smd rect (at 2 9) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG"))\n'
        '(pad "2" smd rect (at 28 11) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG"))\n'
        + "\n".join(fixed)
        + "))\n"
    )
    router, names = load_pcb_for_routing(str(board), use_pcb_rules=False, validate_drc=False)
    net = names["SIG"]
    gate = _corridor_wall(99, (15.45, 17.5), (15.65, 16.55, 17.3))
    anchor = Route(98, "anchor", segments=[Segment(8, 12, 8.5, 12, 0.2, Layer.F_CU, 98)])
    for route in (gate, anchor):
        router.routes.append(route)
        router.grid.mark_route(route)
    guard = RipupCycleGuard()
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        for iteration in (1, 2, 3):
            guard.begin_iteration(iteration, {net: None})
            assert not router._route_net_known_failure_aware(net, 1.0, 30.0, guard, iteration)
        assert run.call_args.kwargs["per_net_timeout"] == 5.0

        router.grid.unmark_route(gate)
        router.routes.remove(gate)
        guard.begin_iteration(4, {net: None})
        routes = router._route_net_known_failure_aware(net, 1.0, 30.0, guard, 4)
        assert _net_pad_pairs(router, net, routes) == {frozenset({0, 1})}
        assert run.call_args.kwargs["per_net_timeout"] == 30.0


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize(
    "handover_iteration, replacement_floor, cap, expected_budget, reachable",
    [
        (3, 0.0, 30.0, 30.0, True),
        (4, 0.0, 30.0, 30.0, True),
        (4, 2.0, 30.0, 5.0, False),
        (4, 0.0, 3.0, 3.0, True),
    ],
)
def test_detour_budget_follows_the_replacement_owners_clearance(
    handover_iteration, replacement_floor, cap, expected_budget, reachable
):
    from kicad_tools.geometry.copper import segment_centerline_distance
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules(net_clearance_floors={99: 2.0, 201: replacement_floor})
    router = _wall_board(gap=True, terminal_y=(9.0, 11.0), gap_y=(15.15, 17.5), rules=rules)
    original = _corridor_wall(99, (15.45, 16.0), (15.45,))
    replacement = _corridor_wall(201, (15.45, 16.0), (15.45,))
    router.routes.append(original)
    router.grid.mark_route(original)
    guard = RipupCycleGuard()

    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        for iteration in (1, 2, 3):
            guard.begin_iteration(iteration, {1: None})
            assert not router._route_net_known_failure_aware(1, 1.0, cap, guard, iteration)
        assert run.call_args.kwargs["per_net_timeout"] == min(cap, 5.0)

        # The short gate leaves a 0.45 mm gap to the returned track. Its owner
        # alone determines whether the authored 2 mm minimum closes that path.
        router.grid.unmark_route(original)
        router.routes.remove(original)
        router.routes.append(replacement)
        router.grid.mark_route(replacement)
        if handover_iteration != 3:
            guard.begin_iteration(handover_iteration, {1: None})
        routed = router._route_net_known_failure_aware(1, 1.0, cap, guard, handover_iteration)
        assert run.call_args.kwargs["per_net_timeout"] == expected_budget
        assert bool(routed) == reachable

    if reachable:
        # The returned copper actually uses the gap opened by the rule change.
        wall = replacement.segments[0]
        gap = min(
            segment_centerline_distance(
                (s.x1, s.y1), (s.x2, s.y2), (wall.x1, wall.y1), (wall.x2, wall.y2)
            )
            - (s.width + wall.width) / 2
            for route in routed
            for s in route.segments
        )
        assert 0 < gap < 2.0


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("gate_x", [15.0, 15.3], ids=["between-centers", "near-center"])
def test_thin_gate_between_coarse_cell_centers_stays_in_the_witness(gate_x):
    """A thin blocker whose clearance envelope falls between coarse sample
    centers (wide board default trace, narrow failed net) must stay in the
    witness, so opening it restores the full budget on the first retry."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment, Via
    from kicad_tools.router.rules import DesignRules, NetClassRouting

    rules = DesignRules(trace_width=1.3, trace_clearance=0.1)
    router = _wall_board(
        gap=True,
        terminal_x=(2.0, 28.0),
        terminal_y=(9.0, 11.0),
        gap_y=(15.15, 17.5),
        rules=rules,
    )
    router.net_class_map["N1"] = NetClassRouting("narrow", trace_width=0.25, clearance=0.1)
    layers = (Layer.F_CU, Layer.B_CU)
    gate = Route(
        99,
        "gate",
        segments=[Segment(gate_x, 15.45, gate_x, 17.5, 0.20, layer, 99) for layer in layers],
        vias=[Via(gate_x, y, 0.20, 0.45, layers, 99) for y in (15.65, 16.55, 17.30)],
    )
    anchor = Route(101, "anchor", segments=[Segment(8, 12, 8.5, 12, 0.20, Layer.F_CU, 101)])
    for route in (anchor, gate):
        router.routes.append(route)
        router.grid.mark_route(route)
    guard = RipupCycleGuard()
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        for it in (1, 2, 3):
            guard.begin_iteration(it, {1: None})
            assert not router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
        assert [c.kwargs["per_net_timeout"] for c in run.call_args_list][:3] == [30.0, 30.0, 5.0]

        router.grid.unmark_route(gate)
        router.routes.remove(gate)
        guard.begin_iteration(4, {1: None})
        routed = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 4)
        assert run.call_args.kwargs["per_net_timeout"] == 30.0
        assert _net_pad_pairs(router, 1, routed) == {frozenset({0, 1})}
        assert 1 not in guard.capped_streak


@pytest.mark.parametrize("copper", ["segments", "vias"])
def test_search_obstacles_compare_the_pair_rules_for_the_same_copper(copper):
    from kicad_tools.router.pairwise_clearance import AttachZone, PairwiseClearanceTable
    from kicad_tools.router.rules import NetClassRouting

    router = _wall_board(gap=True)
    router.net_names.update({99: "A", 201: "B", 202: "C"})
    router.rules.pairwise_clearance = PairwiseClearanceTable(
        0.15, {}, {("A", "N1"): 2.0, ("B", "N1"): 2.0}
    )

    def snapshot(owner, item_floor=0.0):
        wall = _corridor_wall(owner)
        if copper == "segments":
            wall.vias.clear()
        else:
            wall.segments.clear()
        wall.authored_clearance = item_floor
        return router._failure_neighbourhood_print(1, {owner: [wall]}, search_geometry=True)

    # An owner change inside the terminal window preserves an equivalent rule.
    assert snapshot(99) == snapshot(201)
    assert snapshot(99) != snapshot(202)
    assert snapshot(202, item_floor=2.0) != snapshot(202)

    # Equal numerical pair minima can still bind differently in a rated zone,
    # or when only one of the two owners is the failed net's coupled partner.
    router._pairwise_attach_zones_cache = (AttachZone(0, 0, 30, 20, frozenset({"N1", "A"})),)
    assert snapshot(99) != snapshot(201)
    router._pairwise_attach_zones_cache = ()
    router.net_class_map["N1"] = NetClassRouting("pair", diffpair_partner="B")
    assert snapshot(99) != snapshot(201)


@pytest.mark.requires_router_cpp
def test_stalled_corridor_nets_full_budget_searches_now_back_off():
    """The reproduction: a permanently corridor-failed unsealed net keeps a
    copper-keyed streak, its twice-per-iteration searches are assigned backed
    off caps (previously they stayed at the full per-net cap, invisible to
    the guard), the seconds land in the per-iteration known-fail report, and
    the net is still reported unrouted -- never silently dropped."""
    witnesses = []
    note_search = RipupCycleGuard.note_search

    def record_witness(guard, net, *args, **kwargs):
        if net == 1:
            witnesses.append(guard.entry_signature[net])
        note_search(guard, net, *args, **kwargs)

    with patch.object(RipupCycleGuard, "note_search", record_witness):
        router, routed_nets = _run_wall_board(gap=False)
    # The full reorder removes the eight healthy nets between the recovery
    # attempt and the second search of iteration 1.
    assert witnesses[0][0] == "copper" and witnesses[0][1]
    assert witnesses[1] == ("copper", frozenset())
    g = router._ripup_cycle_guard
    rec = g.capped_streak.get(1)
    assert rec is not None and rec[1][0] == "copper"  # Unsealed copper witness.
    assert [b for b in g.backoffs if b[1] == 1], "no search was ever backed off"
    assert all(cap < 20.0 for _it, net, cap in g.backoffs if net == 1)
    assert g.known_fail_seconds, "known-fail seconds never reported"
    line = g.known_failure_line()
    assert "search(es) backed off" in line and "iter" in line
    # Quality intact: every healthy net routed, the victim honestly failed.
    assert 1 not in routed_nets and len(routed_nets) == 8


@pytest.mark.requires_router_cpp
def test_open_corridor_control_routes_the_victim_and_engages_no_backoff():
    """Negative control with a real change to reachable copper: the same board
    with the wall's corridor opened routes the victim outright; no streak, no
    back-off, and the run still reaches full connectivity."""
    router, routed_nets = _run_wall_board(gap=True)
    g = router._ripup_cycle_guard
    assert 1 in routed_nets and len(routed_nets) == 9
    assert not [b for b in g.backoffs if b[1] == 1]
    assert 1 not in g.capped_streak


# --- partial nets keep their failure record (November 2026 scope) -------------


def _three_pad_board(gap: bool = False, *, width=30.0):
    """``_wall_board`` plus a third pad on net 1 left of the wall.

    The reported residual: a partially connected narrow net.  With the wall
    closed the search connects one of the three pad pairs; with the corridor
    open it connects all three.
    """
    router = _wall_board(gap=gap, width=width)
    router.add_component(
        "AUX",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 13.5,
                "width": 0.6,
                "height": 0.6,
                "net": 1,
                "net_name": "N1",
                "through_hole": False,
                "drill": 0.0,
            }
        ],
    )
    return router


def _net_pad_pairs(router, net: int, routes) -> set[frozenset[int]]:
    """Connected physical pad pairs of ``net``'s copper (contact graph)."""
    from kicad_tools.router.terminal_connectivity import physical_pad_components

    pads = [router.pads[k] for k in router.nets.get(int(net), []) if k in router.pads]
    comps = physical_pad_components(pads, list(routes))
    return {
        frozenset((i, j))
        for i in range(len(pads))
        for j in range(i + 1, len(pads))
        if comps[i] == comps[j]
    }


def _commit_copper(router, routes) -> None:
    for route in routes:
        router._mark_route(route)
        router.grid.mark_route_usage(route)
        router.routes.append(route)


def _rip_copper(router, routes) -> None:
    for route in routes:
        router.grid.unmark_route_usage(route)
        router.grid.unmark_route(route)
        if route in router.routes:
            router.routes.remove(route)


def _seed_partial_entry(router, guard: RipupCycleGuard):
    """Route net 1 once against the closed wall and snapshot it as the
    negotiated loop's iteration-entry state."""
    entry = router._route_net_negotiated(1, 1.0, per_net_timeout=30.0)
    assert entry, "wall board: net 1 must land partial copper"
    pairs = _net_pad_pairs(router, 1, entry)
    assert pairs, "wall board: net 1 must connect at least one pad pair"
    _commit_copper(router, entry)
    router._known_failure_entry = {1: (pairs, list(entry))}
    return entry, pairs


@pytest.mark.requires_router_cpp
def test_partial_net_keeps_its_failure_record_and_backs_off():
    """A partial reroute that connects no new pad is a failure, not a route.

    Pre-fix the nonempty partial return cleared the streak every iteration, so
    the net was re-searched at the full cap forever (the reported 120.5 s per
    iteration for four nets).
    """
    router = _three_pad_board(gap=False)
    guard = RipupCycleGuard()
    entry, pairs = _seed_partial_entry(router, guard)
    caps = []
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        live = list(entry)
        for it in range(1, 5):
            guard.begin_iteration(it, {1: None})
            _rip_copper(router, live)
            out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
            caps.append(run.call_args.kwargs["per_net_timeout"])
            # No progress: the kept copper still connects the entry pads.
            assert _net_pad_pairs(router, 1, out) >= pairs
            _commit_copper(router, out)
            live = list(out)
    assert caps[:2] == [30.0, 30.0]
    assert caps[2:] == [KNOWN_FAILURE_BACKOFF_FLOOR_S, KNOWN_FAILURE_BACKOFF_FLOOR_S]
    assert guard.capped_streak[1][2] == 4
    assert [it for it, net, _cap in guard.backoffs if net == 1] == [3, 4]


@pytest.mark.requires_router_cpp
def test_opening_the_wall_gives_the_partial_net_a_full_budget_and_progress_resets():
    """Meaningful change control: removing the copper that blocks the missing
    connection restores the full budget immediately, the progress is kept, and
    the failure record is cleared."""

    router = _three_pad_board(gap=True)
    wall = _corridor_wall(99)
    router.routes.append(wall)
    router.grid.mark_route(wall)
    guard = RipupCycleGuard()
    entry, pairs = _seed_partial_entry(router, guard)
    assert len(pairs) == 1
    caps = []
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        live = list(entry)
        for it in (1, 2):
            guard.begin_iteration(it, {1: None})
            _rip_copper(router, live)
            out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
            caps.append(run.call_args.kwargs["per_net_timeout"])
            _commit_copper(router, out)
            live = list(out)
        assert caps == [30.0, 30.0]
        assert guard.capped_streak[1][2] == 2
        # The corridor opens: the wall's copper disappears from the board.
        router.grid.unmark_route(wall)
        router.routes.remove(wall)
        guard.begin_iteration(3, {1: None})
        _rip_copper(router, live)
        out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 3)
        assert run.call_args.kwargs["per_net_timeout"] == 30.0
        assert len(_net_pad_pairs(router, 1, out)) == 3
        assert 1 not in guard.capped_streak
        assert router._known_failure_entry[1][0] > pairs


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("worse", ["loses-pads", "finds-nothing"])
def test_a_worse_or_failed_partial_reroute_keeps_the_entry_copper(worse):
    """A no-progress re-search must not strand already-connected pads.

    Pre-fix a failed (or pad-losing) reroute of a ripped partial net left it
    empty-handed for the rest of the run; the reporter measured one net losing
    two pad pairs this way.
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router = _three_pad_board(gap=False)
    guard = RipupCycleGuard()
    entry, pairs = _seed_partial_entry(router, guard)
    stub = Route(1, "stub", segments=[Segment(6.0, 6.0, 6.0, 8.0, 0.2, Layer.F_CU, 1)], vias=[])

    def fake_search(net, pf, per_net_timeout=None):
        if worse == "loses-pads":
            router._mark_route(stub)  # the search commits what it returns
            return [stub]
        return []

    with patch.object(router, "_route_net_negotiated", side_effect=fake_search):
        guard.begin_iteration(1, {1: None})
        _rip_copper(router, entry)
        out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 1)
    assert [id(r) for r in out] == [id(r) for r in entry]
    assert _net_pad_pairs(router, 1, out) == pairs
    assert guard.capped_streak[1][2] == 1


@pytest.mark.requires_router_cpp
def test_entry_copper_is_not_relanded_over_a_net_that_took_the_space():
    """Another net legally occupying the ripped space blocks the re-land.

    Re-committing the entry copper unconditionally would overlap that net and
    publish a short into the checkpoints (review of PR #9937).
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router = _three_pad_board(gap=False)
    guard = RipupCycleGuard()
    entry, pairs = _seed_partial_entry(router, guard)
    seg = entry[0].segments[0]
    mx, my = (seg.x1 + seg.x2) / 2, (seg.y1 + seg.y2) / 2
    blocker = Route(
        50, "n50", segments=[Segment(mx, my - 3.0, mx, my + 3.0, 0.25, seg.layer, 50)], vias=[]
    )
    assert isinstance(seg.layer, Layer)

    guard.begin_iteration(1, {1: None})
    _rip_copper(router, entry)
    _commit_copper(router, [blocker])  # net 50 takes the vacated space
    assert not router._entry_copper_still_legal(entry)
    with patch.object(router, "_route_net_negotiated", return_value=[]):
        out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 1)
    assert out == []  # nothing re-landed over the foreign copper
    assert not any(r in router.routes for r in entry)
    assert guard.capped_streak[1][2] == 1  # still a no-progress failure


@pytest.mark.requires_router_cpp
def test_equal_connectivity_result_ratchets_the_entry_copper():
    """A same-connectivity re-search is kept, and a second search in the same
    iteration must fall back to it, not to the superseded entry copper."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router = _three_pad_board(gap=False)
    guard = RipupCycleGuard()
    entry, pairs = _seed_partial_entry(router, guard)
    new = [
        Route(
            r.net,
            r.net_name,
            segments=[Segment(s.x1, s.y1, s.x2, s.y2, s.width, s.layer, s.net) for s in r.segments],
            vias=list(r.vias),
        )
        for r in entry
    ]
    assert Layer is not None

    def fake_search(net, pf, per_net_timeout=None):
        for r in new:
            router._mark_route(r)
        return list(new)

    guard.begin_iteration(1, {1: None})
    _rip_copper(router, entry)
    with patch.object(router, "_route_net_negotiated", side_effect=fake_search):
        out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 1)
    assert [id(r) for r in out] == [id(r) for r in new]
    assert [id(r) for r in router._known_failure_entry[1][1]] == [id(r) for r in new]
    assert router._known_failure_entry[1][0] == pairs


@pytest.mark.requires_router_cpp
def test_an_irrelevant_copper_accretion_keeps_the_partial_backoff():
    """Extra foreign copper appearing in the window does not restart the
    streak: additions cannot open a corridor."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router = _three_pad_board(gap=False)

    def foreign(y: float) -> Route:
        return Route(50, "n50", segments=[Segment(6.0, y, 9.0, y, 0.25, Layer.F_CU, 50)], vias=[])

    f1 = foreign(8.0)
    _commit_copper(router, [f1])
    guard = RipupCycleGuard()
    entry, pairs = _seed_partial_entry(router, guard)
    caps = []
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        live = list(entry)
        for it in (1, 2):
            guard.begin_iteration(it, {1: None})
            _rip_copper(router, live)
            out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
            caps.append(run.call_args.kwargs["per_net_timeout"])
            _commit_copper(router, out)
            live = list(out)
        assert caps == [30.0, 30.0]
        _commit_copper(router, [foreign(9.0)])  # unrelated extra copper
        guard.begin_iteration(3, {1: None})
        _rip_copper(router, live)
        out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 3)
        assert run.call_args.kwargs["per_net_timeout"] == KNOWN_FAILURE_BACKOFF_FLOOR_S
        assert _net_pad_pairs(router, 1, out) >= pairs


@pytest.mark.requires_router_cpp
def test_negotiated_entry_point_backs_off_the_partial_net_and_keeps_its_pads():
    """The issue's public reproduction, end to end: the normal negotiated entry
    point on the three-pad board assigns the partial net's ordinary reroutes a
    backing-off budget and never drops its connected pads."""
    router = _three_pad_board(gap=False)
    caps = []
    orig = router._route_net_negotiated

    def record(net, pf, per_net_timeout=None, **kwargs):
        if net == 1:
            caps.append(per_net_timeout)
        return orig(net, pf, per_net_timeout=per_net_timeout, **kwargs)

    with patch.object(router, "_route_net_negotiated", side_effect=record):
        routes = router.route_all_negotiated(
            max_iterations=5,
            timeout=900,
            per_net_timeout=30.0,
            use_targeted_ripup=False,
            best_stall_patience=None,
        )
    reroute_caps = caps[1:]  # the initial pass is not under the loop's guard
    assert reroute_caps and reroute_caps[0] == 30.0
    assert any(cap < 30.0 for cap in reroute_caps[1:]), reroute_caps
    net1 = [r for r in routes if r.net == 1]
    assert net1, "the partial net must keep its copper"
    assert _net_pad_pairs(router, 1, net1), "connected pads were lost"


# --- remote copper outside the reachable search space (November 2026) --------


def _side_wall(net, x, extent=(0.35, 19.75), via_y=(3.0, 7.0, 11.0, 15.0)):
    """A second full-height wall at ``x``, sealing the board's far side."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment, Via

    layers = (Layer.F_CU, Layer.B_CU)
    return Route(
        net,
        "wall",
        segments=[Segment(x, extent[0], x, extent[1], 0.90, layer, net) for layer in layers],
        vias=[Via(x, y, 0.5, 1.2, layers, net) for y in via_y],
    )


def _compartment_track(y):
    """A foreign track inside the compartment sealed by ``_side_wall``.

    At ``width=36`` with the wall at x=30 it lies beyond every terminal's
    3 mm window yet inside the board, so only the reachability fill can
    exclude it.
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    return Route(
        121,
        "n121",
        segments=[
            Segment(32.0, y, 34.0, y, 0.20, layer, 121) for layer in (Layer.F_CU, Layer.B_CU)
        ],
        vias=[],
    )


def test_search_witness_ignores_copper_sealed_behind_a_full_wall():
    """The search witness is scoped to the net's reachable search space.

    Pre-fix it listed every foreign shape on the board, so a track shifting
    inside a compartment the search can never enter restarted the back-off
    every iteration (the reporter's public control).
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router = _three_pad_board(gap=False, width=36.0)
    wall2 = _side_wall(120, 30.0)
    near = Route(50, "n50", segments=[Segment(6.0, 8.0, 9.0, 8.0, 0.25, Layer.F_CU, 50)], vias=[])

    def snapshot(track_y, with_wall=True):
        routes = {50: [near], 121: [_compartment_track(track_y)]}
        if with_wall:
            routes[120] = [wall2]
        return router._failure_neighbourhood_print(1, routes, search_geometry=True)

    base = snapshot(10.0)
    without_track = router._failure_neighbourhood_print(
        1, {50: [near], 120: [wall2]}, search_geometry=True
    )
    assert base and base == without_track  # the sealed track is out of scope
    assert snapshot(10.4) == base  # its moves are invisible to the witness
    # Removing the bounding wall opens the compartment: the track enters the
    # reachable space and the wall's shapes leave the board.
    assert snapshot(10.0, with_wall=False) != base


@pytest.mark.parametrize(("attribute", "value"), [("height", 0), ("origin_x", 10)])
def test_unknown_or_partial_board_extent_keeps_the_board_wide_witness(
    monkeypatch, attribute, value
):
    router = _three_pad_board(gap=False, width=36.0)
    wall = _side_wall(120, 30.0)

    def snapshot(y):
        return router._failure_neighbourhood_print(
            1, {120: [wall], 121: [_compartment_track(y)]}, search_geometry=True
        )

    assert snapshot(10.0) == snapshot(10.4)
    monkeypatch.setattr(router.grid, attribute, value)
    assert snapshot(10.0) != snapshot(10.4)


@pytest.mark.requires_router_cpp
def test_remote_copper_churn_in_a_sealed_compartment_leaves_the_empty_net_backed_off():
    """The repeatedly-empty group of the reported six nets: a net with no
    copper keeps its back-off while only unreachable copper moves."""
    router = _wall_board(gap=False, width=36.0)
    wall2 = _side_wall(120, 30.0)
    _commit_copper(router, [wall2])
    track = _compartment_track(10.0)
    _commit_copper(router, [track])
    guard = RipupCycleGuard()
    caps = []
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        for it in range(1, 6):
            _rip_copper(router, [track])
            track = _compartment_track(10.0 + 0.2 * it)
            _commit_copper(router, [track])
            guard.begin_iteration(it, {1: None})
            assert not router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
            caps.append(run.call_args.kwargs["per_net_timeout"])
    assert caps[:2] == [30.0, 30.0]
    assert caps[2:4] == [KNOWN_FAILURE_BACKOFF_FLOOR_S, KNOWN_FAILURE_BACKOFF_FLOOR_S]
    assert caps[4] == 30.0  # periodic full re-probe, wall still in place
    assert [it for it, net, _cap in guard.backoffs if net == 1] == [3, 4]


@pytest.mark.requires_router_cpp
def test_remote_copper_churn_keeps_the_partial_net_backoff_and_its_pads():
    """The partially connected group: the same churn must neither restart the
    streak nor drop the pads the net already connects."""
    router = _three_pad_board(gap=False, width=36.0)
    wall2 = _side_wall(120, 30.0)
    _commit_copper(router, [wall2])
    track = _compartment_track(10.0)
    _commit_copper(router, [track])
    guard = RipupCycleGuard()
    entry, pairs = _seed_partial_entry(router, guard)
    caps = []
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        live = list(entry)
        for it in range(1, 6):
            guard.begin_iteration(it, {1: None})
            _rip_copper(router, live)
            _rip_copper(router, [track])
            track = _compartment_track(10.0 + 0.2 * it)
            _commit_copper(router, [track])
            out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
            caps.append(run.call_args.kwargs["per_net_timeout"])
            assert _net_pad_pairs(router, 1, out) >= pairs
            _commit_copper(router, out)
            live = list(out)
    assert caps[:2] == [30.0, 30.0]
    assert caps[2:4] == [KNOWN_FAILURE_BACKOFF_FLOOR_S, KNOWN_FAILURE_BACKOFF_FLOOR_S]
    assert caps[4] == 30.0
    assert [it for it, net, _cap in guard.backoffs if net == 1] == [3, 4]


@pytest.mark.requires_router_cpp
def test_opening_the_compartment_restores_the_full_budget_immediately():
    """Removing the bounding wall is a real reachability change: the very next
    search runs at the full cap, before the periodic re-probe is due."""
    router = _wall_board(gap=False, width=36.0)
    wall2 = _side_wall(120, 30.0)
    _commit_copper(router, [wall2])
    track = _compartment_track(10.0)
    _commit_copper(router, [track])
    guard = RipupCycleGuard()
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        for it in (1, 2, 3):
            _rip_copper(router, [track])
            track = _compartment_track(10.0 + 0.2 * it)
            _commit_copper(router, [track])
            guard.begin_iteration(it, {1: None})
            assert not router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
        assert run.call_args.kwargs["per_net_timeout"] == KNOWN_FAILURE_BACKOFF_FLOOR_S
        _rip_copper(router, [wall2])
        guard.begin_iteration(4, {1: None})
        assert not router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 4)
        assert run.call_args.kwargs["per_net_timeout"] == 30.0


@pytest.mark.requires_router_cpp
def test_a_near_foreign_shape_shifting_still_resets_the_streak():
    """Negative control against over-scoping: copper near the terminals stays
    in the witness, so its movement keeps restoring the full budget."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router = _wall_board(gap=False)
    near = Route(50, "n50", segments=[Segment(8.0, 9.0, 11.0, 9.0, 0.25, Layer.F_CU, 50)], vias=[])
    _commit_copper(router, [near])
    guard = RipupCycleGuard()
    with patch.object(router, "_route_net_negotiated", wraps=router._route_net_negotiated) as run:
        for it in (1, 2, 3):
            _rip_copper(router, [near])
            near = Route(
                50,
                "n50",
                segments=[Segment(8.0, 9.0 + 0.2 * it, 11.0, 9.0 + 0.2 * it, 0.25, Layer.F_CU, 50)],
                vias=[],
            )
            _commit_copper(router, [near])
            guard.begin_iteration(it, {1: None})
            assert not router._route_net_known_failure_aware(1, 1.0, 30.0, guard, it)
            assert run.call_args.kwargs["per_net_timeout"] == 30.0
    assert not [entry for entry in guard.backoffs if entry[1] == 1]


# --- issue #9963: progress over the routing-terminal pads, and the
# legality-rejected ratchet -------------------------------------------------


def _escape_endpoint_override(router, x=8.0, y=6.0):
    """Install a routing-terminal override for net 1's ``U1.1`` pad.

    Mirrors what ``generate_escape_routes`` leaves in
    ``_escape_pad_overrides``: the pad's terminal moves to its escape
    endpoint while ``self.pads`` keeps the original geometry.
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad

    original = router.pads[("U1", "1")]
    router._escape_pad_overrides[("U1", "1")] = Pad(
        x=x,
        y=y,
        width=0.2,
        height=0.2,
        net=original.net,
        net_name=original.net_name,
        layer=Layer.F_CU,
        ref=original.ref,
        pin=original.pin,
    )


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("stub_in_routes", [False, True], ids=["stub-absent", "stub-present"])
def test_connected_pad_pairs_measures_the_routing_terminal_view(stub_in_routes):
    """Issue #9963 measurement: ``_connected_pad_pairs`` must see the pads the
    negotiated loop routes against -- the ``pads_by_net`` substitution -- not
    the raw ``self.pads`` geometry.

    A net's ``net_routes`` entry holds only search results; the escape stub
    copper that ties the original pad to its escape endpoint never enters it.
    So with the stub absent (the loop's actual shape) the raw view cannot see
    a terminal the router connected at the endpoint, while the routing-
    terminal view can.  With the stub present the two views agree."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment
    from kicad_tools.router.terminal_connectivity import physical_pad_components

    router = _three_pad_board(gap=False)
    _escape_endpoint_override(router)
    # Search-shaped copper: endpoint -> AUX.1, clearing U1.1's original
    # outline (which pad view sees the terminal is the variable measured).
    routes = [Route(1, "N1", segments=[Segment(8.0, 6.0, 5.2, 13.4, 0.2, Layer.F_CU, 1)], vias=[])]
    if stub_in_routes:
        routes.append(
            Route(1, "N1", segments=[Segment(6.0, 6.0, 8.0, 6.0, 0.2, Layer.F_CU, 1)], vias=[])
        )

    keys = [k for k in router.nets.get(1, []) if k in router.pads]
    routing_pads = [router._escape_pad_overrides.get(k, router.pads[k]) for k in keys]
    comps = physical_pad_components(routing_pads, routes)
    expected = {
        frozenset((i, j))
        for i in range(len(routing_pads))
        for j in range(i + 1, len(routing_pads))
        if comps[i] == comps[j]
    }
    # The routing-terminal view connects U1.1 (at its endpoint) to AUX.1 in
    # both cases; U1.2 stays behind the wall.
    assert expected == {frozenset({0, 2})}
    assert router._connected_pad_pairs(1, list(routes)) == frozenset(expected)


@pytest.mark.requires_router_cpp
def test_progress_through_an_escape_endpoint_counts_as_progress():
    """Issue #9963: a partial net whose search connects a terminal at its
    escape endpoint has gained a pad pair -- progress -- even though the
    landed copper never touches the original pad outline (the escape stub
    stays out of ``net_routes``)."""

    router = _three_pad_board(gap=False)
    _escape_endpoint_override(router)
    guard = RipupCycleGuard()
    guard.begin_iteration(1, {1: None})
    router._known_failure_entry = {1: (frozenset(), [])}
    out = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 1)
    assert out, "the search must land copper at the escape endpoint"
    # Measurement: the landed copper reaches the endpoint terminal and AUX.1
    # without touching U1.1's original outline -- the raw pad view that
    # ``_net_pad_pairs`` still uses reads no pair at all.
    assert _net_pad_pairs(router, 1, out) == set()
    # The routing-terminal view sees the gained pair: progress, so the entry
    # ratchets and no failure streak starts.
    assert router._known_failure_entry[1][0] == {frozenset({0, 2})}
    assert 1 not in guard.capped_streak


@pytest.mark.requires_router_cpp
def test_legality_rejected_worse_result_ratchets_the_entry():
    """Issue #9963 second note: when the re-land is refused, the kept
    re-search result becomes the entry state.

    A second search of the same net later in the iteration (a site that did
    not rip the first result, and after legality changed back) must compare
    against -- and fall back to -- the copper the net actually kept, never
    re-landing the superseded entry copper beside it."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    router = _three_pad_board(gap=False)
    guard = RipupCycleGuard()
    entry, _pairs = _seed_partial_entry(router, guard)
    stub1 = Route(
        1, "stub1", segments=[Segment(24.0, 14.0, 24.0, 16.0, 0.2, Layer.F_CU, 1)], vias=[]
    )
    stub2 = Route(1, "stub2", segments=[Segment(5.0, 13.5, 5.0, 15.5, 0.2, Layer.F_CU, 1)], vias=[])
    searches = iter([(stub1,), (stub2,)])

    def fake_search(net, pf, per_net_timeout=None):
        found = list(next(searches))
        for route in found:
            router._mark_route(route)  # the search commits what it returns
        return found

    guard.begin_iteration(1, {1: None})
    _rip_copper(router, entry)
    with (
        patch.object(router, "_route_net_negotiated", side_effect=fake_search),
        patch.object(router, "_entry_copper_still_legal", return_value=False),
    ):
        out1 = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 1)
    assert [id(r) for r in out1] == [id(stub1)]  # worse result kept
    _commit_copper(router, out1)
    # The entry now describes the copper the net actually kept.
    assert router._known_failure_entry[1][0] == frozenset()
    assert [id(r) for r in router._known_failure_entry[1][1]] == [id(stub1)]

    # Second search of the same net in the same iteration; its site did not
    # rip the first result, and the foreign blocker has since been removed.
    with (
        patch.object(router, "_route_net_negotiated", side_effect=fake_search),
        patch.object(router, "_entry_copper_still_legal", return_value=True),
    ):
        out2 = router._route_net_known_failure_aware(1, 1.0, 30.0, guard, 1)
    assert [id(r) for r in out2] == [id(stub2)]  # fresh result kept
    _commit_copper(router, out2)
    assert not any(r in router.routes for r in entry), "superseded entry copper re-landed"
    net1 = [r for r in router.routes if r.net == 1]
    assert all(r is stub1 or r is stub2 for r in net1)
    assert 1 in guard.capped_streak  # both no-progress searches still count
