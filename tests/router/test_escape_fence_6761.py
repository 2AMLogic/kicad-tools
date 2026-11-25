"""Hard escape fences for fine-pitch columns (Issue #6761).

On board 05 the first wide net out of U2's left column (PHASE_C, 0.4 mm
trace, 0.8 mm via) left U2.9 with a 45-degree F.Cu diagonal across U2.10's and
U2.11's lanes and dropped its via at their far end.  The #6382 soft lane cost
and two via-cost experiments (#6422/#6426, #6415) only moved which neighbour
stranded.  An escape fence is the hard form of the lane, scoped to a
connection that starts or ends on the column: it may not put its centreline
in another net's lane of that column (dilated so a minimum-width neighbour
still fits) nor drop a via that reaches one, and it falls back to the
unfenced search rather than lose the connection.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import CppPathfinder
from kicad_tools.router.pad_lanes import (
    ESCAPE_FENCE_DISABLE_ENV,
    LANE_LENGTH_MM,
    PAD_VIA_FENCE_LAYER,
    FenceLane,
    collect_fence_lanes,
    column_pitch,
    escape_fence_rects,
    escape_fences_disabled,
    lane_column,
    tight_fence_lanes,
)
from kicad_tools.router.primitives import Pad

from .test_pad_lane_budget_6382 import (
    LEFT_TIP,
    PITCH,
    Y0,
    _build_router,
    _column_pads,
    _Grid,
)

requires_cpp = pytest.mark.requires_router_cpp

RES = 0.05
TRACE_BAND = 0.2 + 0.15 + 0.1  # 0.4 mm trace + clearance + min foreign half width
VIA_BAND = 0.4 + 0.15 + 0.1  # 0.8 mm via + via clearance + min foreign half width


class _Region:
    def __init__(self, pads: list[Pad]) -> None:
        self.pad_refs = {p.key for p in pads}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ESCAPE_FENCE_DISABLE_ENV, "KCT_DISABLE_PAD_LANES"):
        monkeypatch.delenv(name, raising=False)


def _fence_lanes() -> list[FenceLane]:
    pads = _column_pads()
    return collect_fence_lanes(pads, [_Region(pads)], lambda pad: 0)


def _rects(terminals, net: int):
    return escape_fence_rects(
        _fence_lanes(),
        terminals,
        net,
        _Grid(RES),
        trace_band_mm=TRACE_BAND,
        via_band_mm=VIA_BAND,
    )


def _centre(index: int, origin: float = 0.0) -> float:
    return origin + index * RES


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_columns_split_left_and_right() -> None:
    lanes = _fence_lanes()
    assert len(lanes) == 8
    left = {fl.column for fl in lanes if fl.lane.direction < 0}
    right = {fl.column for fl in lanes if fl.lane.direction > 0}
    assert len(left) == 1 and len(right) == 1
    pad = _column_pads()[0]
    assert lane_column(lanes[0].lane, pad) in left


def test_terminal_on_column_fences_every_other_net_lane_of_that_column() -> None:
    # Start on U1.2 (net 2, row 1) -- left column rows 0, 2, 3 are fenced.
    rects = _rects([("U1", "2"), ("D1", "1")], net=2)
    trace = [r for r in rects if r[1] == 0]
    via = [r for r in rects if r[1] == PAD_VIA_FENCE_LAYER]
    assert sorted(src for _, _, src in trace) == [1, 3, 4]
    assert sorted(src for _, _, src in via) == [1, 3, 4]
    # The right column (nets 10..13) is never fenced for a left-column start.
    assert all(src < 10 for _, _, src in rects)


def test_no_terminal_on_a_column_means_no_fence() -> None:
    assert _rects([("D1", "1"), ("D2", "1")], net=2) == []


def test_same_net_lanes_are_never_fenced() -> None:
    pads = _column_pads()
    pads[2] = Pad(**{**pads[2].__dict__, "net": 2, "net_name": "N2"})
    lanes = collect_fence_lanes(pads, [_Region(pads)], lambda pad: 0)
    rects = escape_fence_rects(
        lanes, [("U1", "2")], 2, _Grid(RES), trace_band_mm=TRACE_BAND, via_band_mm=VIA_BAND
    )
    assert 2 not in {src for _, _, src in rects}


def test_trace_band_keeps_the_own_centreline_and_a_neighbour_exit_open() -> None:
    rects = _rects([("U1", "2")], net=2)
    own_y = Y0 + PITCH
    for (gx1, gy1, gx2, gy2), layer, src in rects:
        if layer != 0:
            continue
        lane_y = Y0 + (src - 1) * PITCH
        # Every fenced cell centre is strictly within the band of the lane.
        assert _centre(gy1) > lane_y - TRACE_BAND - 1e-9
        assert _centre(gy2) < lane_y + TRACE_BAND + 1e-9
        # ... and the routing net's own centreline row is never fenced.
        assert not (_centre(gy1) - 1e-9 <= own_y <= _centre(gy2) + 1e-9)
        # The trace fence spans exactly the lane band along the axis.
        assert _centre(gx2) < LEFT_TIP
        assert _centre(gx1) >= LEFT_TIP - LANE_LENGTH_MM - 1e-9


def test_via_fence_runs_one_via_footprint_past_the_lane_end() -> None:
    rects = _rects([("U1", "2")], net=2)
    for (gx1, _gy1, gx2, _gy2), layer, _src in rects:
        if layer != PAD_VIA_FENCE_LAYER:
            continue
        far_end = LEFT_TIP - LANE_LENGTH_MM
        assert _centre(gx1) < far_end - VIA_BAND + RES + 1e-9
        assert _centre(gx1) > far_end - VIA_BAND - 1e-9
        assert _centre(gx2) < LEFT_TIP


def test_only_columns_narrower_than_a_via_footprint_are_fenced() -> None:
    lanes = _fence_lanes()
    left = [fl for fl in lanes if fl.lane.direction < 0]
    assert column_pitch(left) == pytest.approx(PITCH)
    # 0.6 mm via + 0.15 mm clearance = 0.75 mm > 0.65 mm pitch: fenced.
    assert len(tight_fence_lanes(lanes, lambda net: 0.6, 0.15)) == len(lanes)
    # 0.4 mm via + 0.15 mm = 0.55 mm < 0.65 mm pitch: the soft lanes suffice.
    assert tight_fence_lanes(lanes, lambda net: 0.4, 0.15) == []
    # One wide-via net is enough to fence its whole column.
    wide = tight_fence_lanes(lanes, lambda net: 0.8 if net == 1 else 0.4, 0.15)
    assert {fl.column for fl in wide} == {fl.column for fl in left}


def test_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not escape_fences_disabled()
    monkeypatch.setenv(ESCAPE_FENCE_DISABLE_ENV, "1")
    assert escape_fences_disabled()
    monkeypatch.setenv(ESCAPE_FENCE_DISABLE_ENV, "0")
    assert not escape_fences_disabled()


# ---------------------------------------------------------------------------
# Native A*: the wide net leaves its column before it turns
# ---------------------------------------------------------------------------


def _fcu_crossings(routes, y: float) -> list[float]:
    """Distances from the tip at which F.Cu copper of ``routes`` crosses row ``y``."""
    out: list[float] = []
    for route in routes:
        for s in route.segments:
            if getattr(s.layer, "name", str(s.layer)) not in ("F_CU", "CopperLayer.F_CU"):
                continue
            if s.y1 != s.y2 and (s.y1 - y) * (s.y2 - y) <= 0:
                x = s.x1 + (y - s.y1) / (s.y2 - s.y1) * (s.x2 - s.x1)
                if x < LEFT_TIP:
                    out.append(LEFT_TIP - x)
        for v in route.vias:
            if abs(v.y - y) <= 0.2 + v.diameter / 2 and v.x < LEFT_TIP:
                out.append(LEFT_TIP - v.x)
    return out


def _route_wide_then_nb(monkeypatch: pytest.MonkeyPatch, *, fences: bool):
    if not fences:
        monkeypatch.setenv(ESCAPE_FENCE_DISABLE_ENV, "1")
    # WIDE's target is close and below the column: the cheapest escape cuts a
    # diagonal across NB's lane 0.8 mm from its tip, even with the soft lanes.
    router, nb_y = _build_router((6.5, 12.6))
    router._install_pad_lane_budgets()
    installed = len(router.router._pad_lane_fences)
    wide = router.route_net(1)
    nb = router.route_net(2)
    return installed, wide, nb, nb_y


@requires_cpp
def test_fence_keeps_wide_escape_out_of_neighbour_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails without the fix: WIDE crosses NB's row 0.81 mm out and NB needs vias."""
    installed, wide, nb, nb_y = _route_wide_then_nb(monkeypatch, fences=True)
    # Four left-column lanes; the right column's pads are NC (no lane).
    assert installed == 4
    assert wide, "WIDE must route"
    crossings = _fcu_crossings(wide, nb_y)
    assert not crossings or min(crossings) >= LANE_LENGTH_MM, crossings
    assert nb, "NB must route"
    assert sum(len(r.vias) for r in nb) == 0
    for route in nb:
        for s in route.segments:
            assert s.y1 == pytest.approx(nb_y) and s.y2 == pytest.approx(nb_y)


@requires_cpp
def test_escape_hatch_reproduces_the_lane_crossing(monkeypatch: pytest.MonkeyPatch) -> None:
    installed, wide, nb, nb_y = _route_wide_then_nb(monkeypatch, fences=False)
    assert installed == 0
    assert wide and nb
    assert min(_fcu_crossings(wide, nb_y)) < LANE_LENGTH_MM
    assert sum(len(r.vias) for r in nb) > 0


@requires_cpp
def test_fence_falls_back_when_every_path_crosses_the_lane() -> None:
    """A fence reshapes an escape; it never loses a connection."""
    # WIDE's target pad sits on NB's centreline inside NB's lane: every
    # fenced search fails, so the connection is only reachable unfenced.
    router, nb_y = _build_router((8.6, Y0 + PITCH))
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    before = getattr(pathfinder, "_escape_fence_fallbacks", 0)
    wide = router.route_net(1)
    assert wide, "WIDE must still route into the lane"
    assert pathfinder._escape_fence_fallbacks > before


@requires_cpp
def test_python_backend_installs_no_fences() -> None:
    from kicad_tools.router.core import Autorouter

    router = Autorouter(width=20, height=20, force_python=True)
    assert router._install_escape_fences([object()]) == 0


@requires_cpp
def test_via_fence_layer_is_not_a_trace_cost() -> None:
    """A ``layer == -2`` budget never prices a trace step (it is via-only)."""
    rects = _rects([("U1", "2")], net=2)
    assert any(layer == PAD_VIA_FENCE_LAYER for _, layer, _ in rects)
    assert all(math.isfinite(v) for v in (TRACE_BAND, VIA_BAND))


# ---------------------------------------------------------------------------
# Issue #8486: a provably fence-covered goal skips the hopeless fenced search
# ---------------------------------------------------------------------------


def test_trace_fences_cover_goal_table() -> None:
    from kicad_tools.router.pad_lanes import trace_fences_cover_goal

    full = [(0, 0, 9, 9, 0)]
    goal = (2, 2, 5, 5)
    away = (20, 20, 25, 25)
    # Every goal cell on the only legal layer is covered.
    assert trace_fences_cover_goal(
        full, end_metal=goal, end_layers=[0], start_metal=away, start_layers=[0]
    )
    # One uncovered goal cell keeps the fenced search.
    assert not trace_fences_cover_goal(
        [(0, 0, 9, 4, 0)], end_metal=goal, end_layers=[0], start_metal=away, start_layers=[0]
    )
    # A via fence refuses via drops only -- it never covers a goal.
    assert not trace_fences_cover_goal(
        [(0, 0, 9, 9, PAD_VIA_FENCE_LAYER)],
        end_metal=goal,
        end_layers=[0],
        start_metal=away,
        start_layers=[0],
    )
    # A start seed inside the goal box on a shared legal layer is accepted at
    # pop without any step, fences notwithstanding.
    assert not trace_fences_cover_goal(
        full, end_metal=goal, end_layers=[0], start_metal=(4, 4, 8, 8), start_layers=[0]
    )
    # ... but a seed on a layer the goal does not accept changes nothing.
    assert trace_fences_cover_goal(
        full, end_metal=goal, end_layers=[0], start_metal=(4, 4, 8, 8), start_layers=[1]
    )
    # Every LEGAL goal layer must be covered (a through-hole goal).
    assert not trace_fences_cover_goal(
        full, end_metal=goal, end_layers=[0, 1], start_metal=away, start_layers=[0]
    )
    # An empty goal box is never provably covered.
    assert not trace_fences_cover_goal(
        full, end_metal=(5, 5, 4, 4), end_layers=[0], start_metal=away, start_layers=[0]
    )


class _CountingImpl:
    """Delegate proxy counting native ``route_resumable`` launches."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[tuple, dict]] = []

    def route_resumable(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._inner.route_resumable(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def timeouts(self) -> list[float]:
        # ``timeout_s`` is the 19th positional argument of the binding (the
        # wall-clock budget ``_run_fresh_search`` passes).
        return [float(a[18]) for a, _ in self.calls]


@requires_cpp
def test_covered_goal_runs_one_unfenced_search_with_the_full_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forced-recovery target is fence-covered: skip the doomed search.

    Old behaviour: the fenced search drained its whole budget failing, then
    the unfenced retry connected.  On the reported 97-net board the drained
    first budgets of those pairs cost 942.72 s of duplicate timeouts.  The
    pre-emption must keep the connection AND the full budget for the one
    search that runs.
    """
    router, _nb_y = _build_router((8.6, Y0 + PITCH))
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    proxy = _CountingImpl(pathfinder._impl)
    pathfinder._impl = proxy
    preempted_before = getattr(pathfinder, "_escape_fence_preempted", 0)
    routes = router.route_net(1, per_net_timeout=2.0)
    assert routes, "WIDE must still route into the lane"
    assert len(proxy.calls) == 1, "the hopeless fenced search must be skipped"
    assert proxy.timeouts()[0] == pytest.approx(2.0, abs=0.1)
    assert pathfinder._escape_fence_preempted == preempted_before + 1


@requires_cpp
def test_covered_goal_with_no_deadline_still_runs_one_search() -> None:
    """``per_net_timeout=None`` (no deadline) keeps the single-search shape."""
    router, _nb_y = _build_router((8.6, Y0 + PITCH))
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    proxy = _CountingImpl(pathfinder._impl)
    pathfinder._impl = proxy
    routes = router.route_net(1)
    assert routes, "WIDE must still route into the lane"
    assert len(proxy.calls) == 1
    assert proxy.timeouts()[0] == 0.0  # 0.0 is the binding's "no deadline"
    assert pathfinder._escape_fence_preempted == 1


@requires_cpp
def test_repeated_timeout_pays_one_budget_not_two() -> None:
    """A covered goal whose unfenced search also fails times out once.

    This is the duplicate-timeout shape measured on the reported board:
    fenced search times out, unfenced retry times out again.  With the
    pre-emption only the unfenced search runs, so one budget is spent.
    The D1 target is fenced-covered (pre-empted) and enclosed by a keepout
    ring, so the single unfenced search drains its whole budget.
    """
    router, _nb_y = _build_router((8.6, Y0 + PITCH))
    tx, ty = 8.6, Y0 + PITCH
    router.add_component(
        "RING",
        [
            {
                "number": str(i + 1),
                "x": x,
                "y": y,
                "width": w,
                "height": h,
                "net": 0,
                "net_name": "",
                "through_hole": True,
            }
            for i, (x, y, w, h) in enumerate(
                [
                    (tx - 0.4, ty, 0.2, 1.6),
                    (tx + 0.4, ty, 0.2, 1.6),
                    (tx, ty + 0.4, 1.6, 0.2),
                    (tx, ty - 0.4, 1.6, 0.2),
                ]
            )
        ],
    )
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    proxy = _CountingImpl(pathfinder._impl)
    pathfinder._impl = proxy
    routes = router.route_net(1, per_net_timeout=0.02)
    assert not routes  # the ring makes the connection unreachable
    assert len(proxy.calls) == 1, "no second, duplicate timeout search"
    assert proxy.timeouts()[0] == pytest.approx(0.02, abs=0.005)
    assert pathfinder._escape_fence_preempted == 1


@requires_cpp
def test_normal_escape_is_never_preempted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A goal that is not fence-covered keeps the fenced search (and its shape)."""
    router, _nb_y = _build_router((6.5, 12.6))
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    preempted_before = getattr(pathfinder, "_escape_fence_preempted", 0)
    fallbacks_before = getattr(pathfinder, "_escape_fence_fallbacks", 0)
    routes = router.route_net(1)
    assert routes, "WIDE must route"
    assert pathfinder._escape_fence_preempted == preempted_before
    assert pathfinder._escape_fence_fallbacks == fallbacks_before


# ---------------------------------------------------------------------------
# Issue #8486: search-budget controls for the escape-fence pair
#
# The November 24 trace measured 43 unfenced retries after escape-fence
# timeouts draining 942.72 s again (one retry connected in 23.29 s), and the
# deadline-policy matrix on these fixtures showed a fixed fraction either
# starves that recovery (4 ms cap, 3/3 -> 2/3) or, small enough, changes the
# produced copper (the neighbour lane is entered).  A cap sweep on the
# clear-lane fixture pins the reason: the fenced success itself needs
# ~35 ms of native search, so at a 30 ms cap even the CURRENT policy cuts it
# and the retry enters the lane -- every fraction only moves that break to a
# larger cap.  Both budgets of the pair are load-bearing; no reserve policy
# passes its own controls.  These tests pin the two controls any future
# change to this budget must confront: the unfenced retry keeps a fresh FULL
# budget, and a fenced success under a deadline keeps the full budget too.
# ---------------------------------------------------------------------------


def _ring_around(router, tx: float, ty: float) -> None:
    """A through-hole keepout ring enclosing (tx, ty): the connection dies."""
    router.add_component(
        "RING",
        [
            {
                "number": str(i + 1),
                "x": x,
                "y": y,
                "width": w,
                "height": h,
                "net": 0,
                "net_name": "",
                "through_hole": True,
            }
            for i, (x, y, w, h) in enumerate(
                [
                    (tx - 0.4, ty, 0.2, 1.6),
                    (tx + 0.4, ty, 0.2, 1.6),
                    (tx, ty + 0.4, 1.6, 0.2),
                    (tx, ty - 0.4, 1.6, 0.2),
                ]
            )
        ],
    )


@requires_cpp
def test_uncovered_pair_pays_two_budgets_and_the_retry_keeps_the_full_cap() -> None:
    """The recovery control: the unfenced retry must not be starved.

    This is the two-search shape the covered-goal pre-emption cannot reach:
    the goal is NOT fence-covered (the ring sits clear of every lane), so the
    fenced search runs, fails, and the unfenced retry runs.  The retry is the
    search that connected in 23.29 s on the reported board and in the
    matrix's 4 ms recovery cell -- it keeps a fresh FULL budget, never the
    remainder of a shared allowance.
    """
    router, _nb_y = _build_router((6.5, 12.6))
    _ring_around(router, 6.5, 12.6)
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    proxy = _CountingImpl(pathfinder._impl)
    pathfinder._impl = proxy
    preempted_before = getattr(pathfinder, "_escape_fence_preempted", 0)
    routes = router.route_net(1, per_net_timeout=0.05)
    assert not routes  # the ring makes the connection unreachable
    assert len(proxy.calls) == 2
    assert proxy.timeouts() == pytest.approx([0.05, 0.05], abs=0.001)
    assert pathfinder._escape_fence_fallbacks == 1
    assert pathfinder._escape_fence_preempted == preempted_before


@requires_cpp
def test_fenced_success_under_a_deadline_keeps_the_full_budget_and_the_lane() -> None:
    """The copper control: a fenced success must not be silently cut.

    The clear-lane escape succeeds fenced in ~35 ms of native search; with a
    2 s cap the whole budget is available to it.  Any reserve carved out of
    the fenced budget risks cutting a slower fenced success and replacing
    its copper with the unfenced retry's (which enters the neighbour lane).
    """
    router, nb_y = _build_router((6.5, 12.6))
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    proxy = _CountingImpl(pathfinder._impl)
    pathfinder._impl = proxy
    routes = router.route_net(1, per_net_timeout=2.0)
    assert routes, "WIDE must route"
    assert len(proxy.calls) == 1
    assert proxy.timeouts()[0] == pytest.approx(2.0, abs=0.01)
    crossings = _fcu_crossings(routes, nb_y)
    assert not crossings or min(crossings) >= LANE_LENGTH_MM, crossings


@requires_cpp
def test_no_fences_keeps_the_single_full_budget_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without fences there is no pair, so the one search keeps the full cap."""
    monkeypatch.setenv(ESCAPE_FENCE_DISABLE_ENV, "1")
    router, _nb_y = _build_router((6.5, 12.6))
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    proxy = _CountingImpl(pathfinder._impl)
    pathfinder._impl = proxy
    routes = router.route_net(1, per_net_timeout=2.0)
    assert routes, "WIDE must route"
    assert len(proxy.calls) == 1
    assert proxy.timeouts()[0] == pytest.approx(2.0, abs=0.01)
    assert getattr(pathfinder, "_escape_fence_fallbacks", 0) == 0


@requires_cpp
def test_no_deadline_keeps_the_two_search_recovery_shape() -> None:
    """``per_net_timeout=None`` (deterministic arm) is untouched: 0.0 budgets."""
    router, _nb_y = _build_router((6.5, 12.6))
    _ring_around(router, 6.5, 12.6)
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    # The native two-search shape is the subject; the Python fallback is
    # out of scope for this control.
    pathfinder._try_python_fallback = lambda *a, **k: None
    proxy = _CountingImpl(pathfinder._impl)
    pathfinder._impl = proxy
    routes = router.route_net(1)
    assert not routes
    assert len(proxy.calls) == 2
    assert proxy.timeouts() == [0.0, 0.0]  # 0.0 is the binding's "no deadline"


# ---------------------------------------------------------------------------
# Issue #8486: the per-attempt diagnostic in the native-call timing record
#
# The November 24 private-board trace could not attribute its three
# unexplained 30 s attempts: the per-call timing record carried only net /
# elapsed / budget / success, which cannot distinguish an uncovered
# unreachable target (fenced timeout, then unfenced timeout) from a covered
# one (pre-empted, single unfenced timeout).  Where tracing is enabled the
# record now also carries the chosen pad pair, the legal goal bounds and
# layers, the fence decision, and each native search's duration and outcome.
# These four public fixtures are the acceptance test the reporter's table
# defined: ordinary exit (fenced success), exit inside the neighbouring lane
# (covered goal, pre-empted -> unfenced success), and both ring-enclosed
# unreachable variants.  The diagnostic is purely observational -- the copper
# must be identical with tracing off and on.
# ---------------------------------------------------------------------------

_DIAG_SHAPES = [
    # (name, wide_target, ring)
    ("ordinary-exit", (6.5, 12.6), False),
    ("covered-exit", (8.6, Y0 + PITCH), False),
    ("ordinary-ring", (6.5, 12.6), True),
    ("covered-ring", (8.6, Y0 + PITCH), True),
]


def _build_diag_shape(wide_target: tuple[float, float], ring: bool):
    router, _nb_y = _build_router(wide_target)
    if ring:
        _ring_around(router, wide_target[0], wide_target[1])
    router._install_pad_lane_budgets()
    pathfinder = router.router
    assert isinstance(pathfinder, CppPathfinder)
    return router, pathfinder


def _copper_signature(routes) -> tuple:
    """Comparable signature of a route list: every segment and via."""
    return tuple(
        (
            route.net,
            tuple(route.segments),
            tuple(route.vias),
        )
        for route in routes
    )


@requires_cpp
@pytest.mark.parametrize("name,wide_target,ring", _DIAG_SHAPES)
def test_per_attempt_diagnostic_distinguishes_the_four_public_targets(
    name: str, wide_target: tuple[float, float], ring: bool
) -> None:
    """One drained record per attempt names the pair, target and fence path.

    Expected per shape (the reporter's table, now asserted):

    ============== ============== ============= ==========================
    shape           fence_decision goal_covered searches
    ============== ============== ============= ==========================
    ordinary-exit   applied        False         1 fenced success
    covered-exit    skipped-       True          1 unfenced success
                    covered
    ordinary-ring   applied        False         fenced timeout, then
                                                 unfenced timeout
    covered-ring    skipped-       True          1 unfenced timeout
                    covered
    ============== ============== ============= ==========================
    """
    router, pathfinder = _build_diag_shape(wide_target, ring)
    pathfinder.enable_per_call_timing(True)
    routes = router.route_net(1, per_net_timeout=0.08 if ring else 2.0)
    records = pathfinder.get_and_clear_per_call_timings()
    assert len(records) == 1, "one route() attempt for the two-pad net"
    record = records[0]
    # Historical fields stay intact (Issue #2929 shape).
    assert record["net"] == 1
    assert record["net_name"] == "WIDE"
    for key in ("elapsed", "per_net_timeout", "deadline_violated", "succeeded"):
        assert key in record
    # The chosen pad pair -- the actual connection considered.
    assert record["start_pad"] == "U1.1"
    assert record["end_pad"] == "D1.1"
    # The legal target bounds (grid cells) and layers, so an observer can
    # independently recompute fence coverage from a saved trace.
    gx1, gy1, gx2, gy2 = record["end_bounds"]
    assert all(isinstance(v, int) for v in (gx1, gy1, gx2, gy2))
    assert gx1 <= gx2 and gy1 <= gy2
    assert record["end_layers"], "the SMD goal must name its legal layer"
    assert all(isinstance(l, int) and l >= 0 for l in record["end_layers"])
    assert isinstance(record["start_bounds"], tuple) and record["start_layers"]

    searches = record["searches"]
    if name == "ordinary-exit":
        assert routes, "WIDE must route"
        assert record["succeeded"] is True
        assert record["fence_decision"] == "applied"
        assert record["goal_covered"] is False
        assert len(searches) == 1
        assert searches[0]["fenced"] is True
        assert searches[0]["succeeded"] is True
        assert searches[0]["failure_reason"] == "none"
        assert searches[0]["elapsed"] > 0.0
    elif name == "covered-exit":
        assert routes, "WIDE must route"
        assert record["succeeded"] is True
        assert record["fence_decision"] == "skipped-covered"
        assert record["goal_covered"] is True
        assert len(searches) == 1, "the hopeless fenced search was skipped"
        assert searches[0]["fenced"] is False
        assert searches[0]["succeeded"] is True
    elif name == "ordinary-ring":
        assert not routes, "the ring makes the connection unreachable"
        assert record["succeeded"] is False
        assert record["fence_decision"] == "applied"
        assert record["goal_covered"] is False
        assert len(searches) == 2, "fenced timeout, then the unfenced retry"
        assert searches[0]["fenced"] is True
        assert searches[1]["fenced"] is False
        assert all(s["succeeded"] is False for s in searches)
        assert all(s["failure_reason"] == "timeout" for s in searches)
    else:  # covered-ring
        assert not routes, "the ring makes the connection unreachable"
        assert record["succeeded"] is False
        assert record["fence_decision"] == "skipped-covered"
        assert record["goal_covered"] is True
        assert len(searches) == 1, "no duplicate second timeout search"
        assert searches[0]["fenced"] is False
        assert searches[0]["succeeded"] is False
        assert searches[0]["failure_reason"] == "timeout"


@requires_cpp
@pytest.mark.parametrize("name,wide_target,ring", _DIAG_SHAPES)
def test_per_attempt_diagnostic_leaves_the_copper_unchanged(
    name: str, wide_target: tuple[float, float], ring: bool
) -> None:
    """Tracing is pure observation: identical copper off and on."""
    router_off, pathfinder_off = _build_diag_shape(wide_target, ring)
    assert not pathfinder_off._per_call_timing_enabled
    copper_off = _copper_signature(router_off.route_net(1, per_net_timeout=2.0))
    assert pathfinder_off.get_and_clear_per_call_timings() == []

    router_on, pathfinder_on = _build_diag_shape(wide_target, ring)
    pathfinder_on.enable_per_call_timing(True)
    copper_on = _copper_signature(router_on.route_net(1, per_net_timeout=2.0))
    assert pathfinder_on.get_and_clear_per_call_timings()

    assert copper_on == copper_off, "the diagnostic must not steer the search"


@requires_cpp
def test_early_return_attempt_carries_no_stale_diagnostic() -> None:
    """An attempt that returns before a native search records no diagnostic.

    ``_route_impl`` returns early when ``allowed_layers`` leaves no legal
    start layer; the record for that attempt must not inherit the previous
    traced attempt's pad pair / searches (Issue #8486 review).
    """
    router, pathfinder = _build_diag_shape((6.5, 12.6), False)
    pathfinder.enable_per_call_timing(True)
    assert router.route_net(1, per_net_timeout=2.0), "WIDE must route"
    first = pathfinder.get_and_clear_per_call_timings()
    assert len(first) == 1 and first[0]["searches"]

    router.rules.allowed_layers = {"In1.Cu"}  # F.Cu pads: no legal start layer
    router.route_net(2, per_net_timeout=2.0)
    second = pathfinder.get_and_clear_per_call_timings()
    assert second, "the attempt still produces a timing record"
    for record in second:
        assert record["net"] == 2
        assert "start_pad" not in record
        assert "searches" not in record
