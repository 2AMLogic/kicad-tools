"""A two-row SMD breakout with preserved copper on either side of its pad."""

import time
from types import SimpleNamespace

import pytest

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRoute,
    EscapeRouter,
    escape_endpoint_pad,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.preserved_pad_escape import find_departure, retry_preserved_departure
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

# Issue #10179: wall-clock budget for the connected (positive-control) endpoint
# cases; ~15x the ~2 s they take on an idle host, so a loaded hosted runner
# cannot turn a routable case into a deadline exit.
_POSITIVE_CONTROL_BUDGET_S = 30.0


def test_positional_pad_identity_still_routes():
    # Existing positional callers may supply every field through the footprint side.
    source = Pad(
        2,
        2,
        0.6,
        0.6,
        1,
        "SIG",
        Layer.F_CU,
        "U1",
        "1",
        False,
        0,
        False,
        False,
        "",
        0,
        "rect",
        None,
        None,
        0,
        "U1",
        False,
        "west-land",
        0.2,
        (1, 1),
        Layer.F_CU,
    )
    assert source.key == ("U1", "west-land")
    assert (source.authored_clearance, source.footprint_origin, source.side) == (
        0.2,
        (1, 1),
        Layer.F_CU,
    )
    assert source.terminal_layers is None
    rules = DesignRules(grid_resolution=0.1)
    router = Router(RoutingGrid(8, 6, rules, layer_stack=LayerStack.two_layer()), rules)
    target = Pad(3, 2, 0.6, 0.6, 1, "SIG", ref="J1", pin="1")
    assert router.route(source, target, per_net_timeout=2) is not None


def departure_fixture():
    rules = DesignRules(
        trace_width=0.275,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        manufacturer="jlcpcb",
        grid_resolution=0.075,
    )
    grid = RoutingGrid(width=8, height=8, rules=rules, layer_stack=LayerStack.two_layer())
    escape = EscapeRouter(grid, rules)
    # Relative to a local board origin, with the source between its own row
    # and another row. Copper from the two neighbouring signals approaches it.
    source = Pad(3.5, 3.0, 0.3, 0.6, 1, "RX-", ref="J1", pin="B10")
    pads = [source]
    for row in (1.0, 3.0):
        for i, x in enumerate((1.5, 2.5, 3.5, 4.5, 5.5)):
            if (x, row) != (source.x, source.y):
                pads.append(Pad(x, row, 0.3, 0.6, 2 + i, f"N{i}", ref="J1", pin=f"{row}-{i}"))
    copper = [
        Route(
            9,
            "TX+",
            [
                Segment(2.5, 1.0, 2.5, 1.15, 0.275, Layer.F_CU, 9),
                Segment(2.5, 1.15, 3.75, 2.4, 0.275, Layer.F_CU, 9),
                Segment(3.75, 2.4, 4.35, 1.8, 0.275, Layer.F_CU, 9),
            ],
        ),
        Route(
            10,
            "RX+",
            [
                Segment(2.5, 3, 2.5, 3.15, 0.275, Layer.F_CU, 10),
                Segment(2.5, 3.15, 5.3, 5.95, 0.275, Layer.F_CU, 10),
            ],
        ),
        Route(
            11,
            "POWER",
            [
                Segment(4.5, 3, 4.5, 3.8, 0.2, Layer.F_CU, 11),
                Segment(4.5, 3.8, 4, 3.8, 0.2, Layer.F_CU, 11),
            ],
            [Via(4, 3.8, 0.2, 0.45, (Layer.F_CU, Layer.B_CU), 11)],
        ),
    ]
    for route in copper:
        grid.mark_route(route)
    return grid, escape, source, pads


def test_two_row_departure_routes_around_preserved_copper():
    grid, escape, source, pads = departure_fixture()
    originals = list(grid.routes)
    candidate = find_departure(escape, Router(grid, grid.rules), source, pads, Layer.B_CU, (), 0.0)
    assert candidate is not None
    assert candidate.via.x < source.x and candidate.via.y < source.y
    assert (candidate.segments[0].x1, candidate.segments[0].y1) == (source.x, source.y)
    assert (candidate.segments[-1].x2, candidate.segments[-1].y2) == candidate.via_pos
    assert all(seg.width == pytest.approx(0.275) for seg in candidate.segments)
    assert grid.routes == originals


def test_obstructed_back_layer_keeps_the_departure_uncommitted():
    grid, escape, source, pads = departure_fixture()
    barrier = Route(20, "fixed", [Segment(0, 3, 7, 3, 4, Layer.B_CU, 20)])
    grid.mark_route(barrier)
    originals = list(grid.routes)
    assert (
        find_departure(escape, Router(grid, grid.rules), source, pads, Layer.B_CU, (), 0.0) is None
    )
    assert grid.routes == originals


def test_raw_keepout_blocks_a_departure_on_either_layer():
    grid, escape, source, pads = departure_fixture()
    grid.add_keepout(0, 0, 7, 7, [Layer.B_CU])
    assert find_departure(escape, Router(grid, grid.rules), source, pads, Layer.B_CU, (), 0) is None


@pytest.mark.parametrize(
    "span,exhausted,reverse,expect_retry",
    [
        (None, True, False, True),
        (None, True, True, True),
        ("blind", True, False, False),
        (None, False, False, False),
    ],
)
def test_retry_needs_an_exhausted_frontier_and_physical_target_barrel(
    span, exhausted, reverse, expect_retry
):
    grid, escape, source, pads = departure_fixture()
    router = Router(grid, grid.rules)
    router.last_route_all_vias_blocked = exhausted
    target = Pad(
        6.45, 5.1, 0.4, 0.4, source.net, source.net_name, ref="U2", pin="B6", escape_terminal=True
    )
    target_via = Via(
        target.x, target.y, 0.3, 0.6, (Layer.F_CU, Layer.IN1_CU), source.net, span_type=span
    )
    grid.mark_route(Route(source.net, source.net_name, vias=[target_via]))
    owner = SimpleNamespace(
        router=router,
        grid=grid,
        _escape=escape,
        all_pads=pads,
        _edge_segments=(),
        _edge_clearance=0.0,
    )
    before = list(grid.routes)
    ends = (target, source) if reverse else (source, target)
    candidate = retry_preserved_departure(owner, *ends, None)
    assert (candidate is not None) == expect_retry
    if candidate is not None:
        assert candidate.segments[0].layer == Layer.F_CU
        assert candidate.segments[-1].layer == Layer.B_CU
        assert len(candidate.vias) == 1
    assert grid.routes == before


@pytest.mark.parametrize("fill_ok,pour_ok", [(True, True), (True, False), (False, True)])
def test_candidate_and_power_pour_publish_together(tmp_path, monkeypatch, fill_ok, pour_ok):
    from kicad_tools.cli import route_cmd

    source = tmp_path / "original.kicad_pcb"
    source.write_text('(kicad_pcb (version 20240108) (zone (net 1) (layer "B.Cu")))\n')
    source.with_suffix(".kicad_pro").write_text('{"authored": true}')
    source.with_suffix(".kicad_dru").write_text("(version 1)")
    output = tmp_path / "routed.kicad_pcb"
    output.write_bytes(source.read_bytes())
    added = '(segment (start 1 1) (end 2 2) (width 0.275) (layer "F.Cu") (net 2))'
    args = SimpleNamespace(pcb=source, dry_run=False)

    def fill(candidate, *, args, **kwargs):
        assert candidate != output
        assert added in candidate.read_text()
        assert output.read_bytes() == source.read_bytes()
        assert (
            candidate.with_suffix(".kicad_dru").read_bytes()
            == source.with_suffix(".kicad_dru").read_bytes()
        )
        args._zone_fill_verified = fill_ok

    def oracle(candidate, *, args, **kwargs):
        assert output.read_bytes() == source.read_bytes()
        args._pour_connectivity_verified = pour_ok

    monkeypatch.setattr(route_cmd, "_fill_zones_after_route", fill)
    monkeypatch.setattr(route_cmd, "_complete_pour_nets_with_oracle", oracle)
    accepted = route_cmd._publish_preserved_departure(source, output, added, args, None, True)
    assert accepted == (fill_ok and pour_ok)
    assert (added in output.read_text()) == accepted
    assert (
        output.with_suffix(".kicad_pro").read_bytes()
        == source.with_suffix(".kicad_pro").read_bytes()
    )
    if not accepted:
        assert output.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("saved", [False, True])
def test_speculative_refill_requires_a_verified_save(tmp_path, monkeypatch, saved):
    from kicad_tools.cli import route_cmd, runner

    board = tmp_path / "staged.kicad_pcb"
    board.write_text('(kicad_pcb (version 20240108) (zone (net 1) (layer "B.Cu")))\n')
    args = SimpleNamespace(pcb=board, _preserved_departure_trial=True)
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: tmp_path / "kicad-cli")
    monkeypatch.setattr(route_cmd, "_finishing_time_left", lambda _args: 25)
    monkeypatch.setattr(runner, "validate_net_format", lambda _path: SimpleNamespace(valid=True))

    def native(path, output, cli, timeout):
        assert path == board and output is None and timeout == 25
        return runner.KiCadCLIResult(success=True, refill_saved=saved)

    monkeypatch.setattr(runner, "_run_fill_zones_via_drc", native)
    route_cmd._fill_zones_after_route(board, quiet=True, args=args)
    assert args._zone_fill_verified is saved


def _surround_box_on_layer(
    grid: RoutingGrid,
    layer: Layer,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    net: int = 30,
) -> None:
    """Enclose ``(x1, y1)..(x2, y2)`` with foreign copper on ``layer``."""
    ring = Route(
        net,
        f"BARRIER_{layer.name}",
        [
            Segment(x1, y1, x2, y1, 0.2, layer, net),
            Segment(x2, y1, x2, y2, 0.2, layer, net),
            Segment(x2, y2, x1, y2, 0.2, layer, net),
            Segment(x1, y2, x1, y1, 0.2, layer, net),
        ],
    )
    grid.mark_route(ring)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("bidirectional", [False, True])
def test_even_ring_escape_barrel_accepts_far_layer_arrival(reverse, bidirectional):
    """Issue #10088: an off-grid even-ring BGA through barrel accepts B.Cu arrival."""
    rules = DesignRules(
        trace_width=0.275,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        manufacturer="jlcpcb",
        grid_resolution=0.075,
        allowed_layers=["F.Cu", "B.Cu"],
    )
    grid = RoutingGrid(
        width=10.0,
        height=8.0,
        rules=rules,
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    source = Pad(1.5, 4.05, 0.4, 0.6, 15, "USB3_RX2-", ref="J1", pin="A10", layer=Layer.F_CU)
    bga_ball = Pad(6.15, 4.20, 0.4, 0.4, 15, "USB3_RX2-", ref="U2", pin="F6", layer=Layer.F_CU)
    approach = Segment(6.15, 4.20, 6.6056, 4.6556, 0.275, Layer.F_CU, 15, "USB3_RX2-")
    barrel = Via(6.6056, 4.6556, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 15, "USB3_RX2-")
    grid.mark_route(Route(15, "USB3_RX2-", [approach], [barrel], is_escape=True))
    # Tightly enclose the ball+barrel on F.Cu so no second via fits inside;
    # B.Cu remains open directly to the barrel.
    _surround_box_on_layer(grid, Layer.F_CU, 5.60, 3.65, 7.26, 5.31)

    escape = EscapeRoute(
        pad=bga_ball,
        direction=EscapeDirection.SOUTH,
        escape_point=(barrel.x, barrel.y),
        escape_layer=Layer.F_CU,
        via_pos=(barrel.x, barrel.y),
        segments=[approach],
        via=barrel,
        ring_index=2,
        requires_main_route=True,
    )
    target = escape_endpoint_pad(bga_ball, escape, fallback_width=0.275)
    router = Router(grid, rules)
    ends = (target, source) if reverse else (source, target)
    route = (
        router.route_bidirectional(*ends, per_net_timeout=5.0)
        if bidirectional
        else router.route(*ends, per_net_timeout=5.0)
    )
    assert route is not None
    barrel_seg = route.segments[0] if reverse else route.segments[-1]
    assert barrel_seg.layer == Layer.B_CU
    barrel_xy = (barrel_seg.x1, barrel_seg.y1) if reverse else (barrel_seg.x2, barrel_seg.y2)
    assert barrel_xy == pytest.approx((barrel.x, barrel.y), abs=1e-4)


@pytest.mark.parametrize(
    "case,via_layers,span_type,is_micro,open_layer,expect_connected",
    [
        ("blind_in_span", (Layer.F_CU, Layer.IN1_CU), "blind", False, Layer.IN1_CU, True),
        ("blind_outside_span", (Layer.F_CU, Layer.IN1_CU), "blind", False, Layer.B_CU, False),
        ("micro_in_span", (Layer.F_CU, Layer.IN1_CU), None, True, Layer.IN1_CU, True),
        ("micro_outside_span", (Layer.F_CU, Layer.IN1_CU), None, True, Layer.B_CU, False),
        ("buried_unconnected", (Layer.IN1_CU, Layer.IN2_CU), "buried", False, Layer.IN1_CU, False),
        ("surface_only", None, None, False, Layer.B_CU, False),
        ("surface_tail_beyond_via", (Layer.F_CU, Layer.B_CU), None, False, Layer.F_CU, False),
    ],
)
def test_escape_endpoint_preserves_restricted_spans_and_rejects_unconnected_layers(
    case, via_layers, span_type, is_micro, open_layer, expect_connected
):
    """Issue #10088: only layers connected through escape copper to the pad may arrive."""
    rules = DesignRules(
        trace_width=0.275,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        manufacturer="jlcpcb",
        grid_resolution=0.075,
    )
    stack = LayerStack.four_layer_all_signal()
    grid = RoutingGrid(width=10.0, height=8.0, rules=rules, layer_stack=stack)
    source = Pad(1.5, 4.65, 0.4, 0.6, 15, "SIG", ref="J1", pin="A10", layer=Layer.F_CU)
    bga_ball = Pad(6.15, 4.65, 0.4, 0.4, 15, "SIG", ref="U2", pin="F6", layer=Layer.F_CU)
    approach = Segment(6.15, 4.65, 6.75, 4.65, 0.275, Layer.F_CU, 15, "SIG")
    segments = [approach]
    barrel = (
        None
        if via_layers is None
        else Via(
            6.75,
            4.65,
            0.3,
            0.6,
            via_layers,
            15,
            "SIG",
            is_micro=is_micro,
            span_type=span_type,
        )
    )
    escape_pt = (6.75, 4.65)
    escape_layer = Layer.F_CU
    box_x2 = 7.35
    if case == "surface_tail_beyond_via":
        tail = Segment(6.75, 4.65, 7.35, 4.65, 0.275, Layer.B_CU, 15, "SIG")
        segments.append(tail)
        escape_pt = (7.35, 4.65)
        escape_layer = Layer.B_CU
        box_x2 = 7.80

    grid.mark_route(
        Route(15, "SIG", segments, [barrel] if barrel is not None else [], is_escape=True)
    )
    if case != "surface_tail_beyond_via":
        under_layer = Layer.B_CU if case == "buried_unconnected" else Layer.IN2_CU
        if under_layer != open_layer:
            grid.mark_route(
                Route(50, "UNDER_BARREL", [Segment(6.75, 3.0, 6.75, 6.0, 0.25, under_layer, 50)])
            )
    for idx, layer in enumerate((Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU)):
        if layer != open_layer:
            _surround_box_on_layer(grid, layer, 5.65, 4.04, box_x2, 5.26, net=40 + idx)

    escape = EscapeRoute(
        pad=bga_ball,
        direction=EscapeDirection.SOUTH,
        escape_point=escape_pt,
        escape_layer=escape_layer,
        via_pos=(barrel.x, barrel.y) if barrel is not None else None,
        segments=segments,
        via=barrel,
        ring_index=2,
        requires_main_route=case != "surface_tail_beyond_via",
    )
    target = escape_endpoint_pad(bga_ball, escape, fallback_width=0.275)
    # Issue #10179: the connected controls took ~1.9 s locally against a 5 s
    # budget, and hosted runners returned ``None`` for them, indistinguishable
    # from a geometric disconnection.  Give only the positive controls a
    # budget a loaded runner cannot exhaust; the negative cases keep 5 s.
    budget = _POSITIVE_CONTROL_BUDGET_S if expect_connected else 5.0
    router = Router(grid, rules)
    started = time.monotonic()
    route = router.route(source, target, per_net_timeout=budget)
    elapsed = time.monotonic() - started
    if route is None and expect_connected:
        # ``last_route_deadline_hit`` is set by the search when the wall-clock
        # deadline (not an exhausted open set) ended it.
        reason = (
            "deadline expired"
            if router.last_route_deadline_hit
            else "no geometric path (search exhausted)"
        )
        pytest.fail(
            f"{case}: route returned None -- {reason}; "
            f"elapsed {elapsed:.2f}s of {budget:.1f}s budget"
        )
    assert (route is not None) == expect_connected
    if route is not None:
        assert route.segments[-1].layer == open_layer


def test_disconnected_escape_barrel_is_rejected():
    """An off-pad barrel with no conductor tying it to its pad grants no endpoint."""
    bga_ball = Pad(6.0, 4.05, 0.4, 0.4, 15, "SIG", ref="U2", pin="F6", layer=Layer.F_CU)
    floating_barrel = Via(7.2, 5.25, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 15, "SIG")
    escape = EscapeRoute(
        pad=bga_ball,
        direction=EscapeDirection.SOUTH,
        escape_point=(floating_barrel.x, floating_barrel.y),
        escape_layer=Layer.B_CU,
        via_pos=(floating_barrel.x, floating_barrel.y),
        segments=[],
        via=floating_barrel,
        ring_index=2,
        requires_main_route=True,
    )
    with pytest.raises(ValueError, match="no committed copper"):
        escape_endpoint_pad(bga_ball, escape, fallback_width=0.275)


@pytest.mark.parametrize(
    ("shape", "ratio", "connected"),
    [
        ("rect", None, True),
        ("roundrect", 0.05, True),
        ("roundrect", None, False),
        ("circle", None, False),
    ],
)
def test_escape_barrel_contact_at_a_pad_corner(shape, ratio, connected):
    pad = Pad(6, 4, 2, 2, 1, "SIG", shape=shape, roundrect_rratio=ratio)
    via = Via(6.96, 4.96, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 1, "SIG")
    escape = EscapeRoute(pad, EscapeDirection.VIA_DOWN, (via.x, via.y), Layer.B_CU, via=via)
    if connected:
        terminal = escape_endpoint_pad(pad, escape, fallback_width=0.18)
        assert terminal.layer == Layer.B_CU
        assert Layer.F_CU in terminal.terminal_layers
    else:
        with pytest.raises(ValueError, match="no committed copper"):
            escape_endpoint_pad(pad, escape, fallback_width=0.18)


@pytest.mark.parametrize("connected", [True, False], ids=["pad-connected", "floating-stub"])
def test_surface_escape_requires_a_connection_to_the_physical_pad(connected):
    pad = Pad(8, 2, 1, 1, 1, "SIG")
    start = (pad.x, pad.y) if connected else (7.5, 5)
    segment = Segment(*start, 8, 5, 0.25, Layer.F_CU, 1, "SIG")
    escape = EscapeRoute(pad, EscapeDirection.NORTH, (8, 5), Layer.F_CU, segments=[segment])
    rules = DesignRules(grid_resolution=0.1)
    grid = RoutingGrid(12, 9, rules, layer_stack=LayerStack.two_layer())
    candidates = [escape]
    committed = EscapeRouter(grid, rules).apply_escape_routes(candidates)
    assert candidates == ([escape] if connected else [])
    assert [seg for route in committed for seg in route.segments] == (
        [segment] if connected else []
    )
    if connected:
        terminal = escape_endpoint_pad(pad, escape, fallback_width=0.25)
        assert terminal.terminal_layers == (Layer.F_CU,)
    else:
        with pytest.raises(ValueError, match="no committed copper"):
            escape_endpoint_pad(pad, escape, fallback_width=0.25)


@pytest.mark.parametrize("reverse", [False, True])
def test_retry_preserved_departure_accepts_far_layer_of_through_barrel_and_edges(reverse):
    """retry_preserved_departure also admits arrival on the far face of a through barrel."""
    import time

    grid, escape, source, pads = departure_fixture()
    router = Router(grid, grid.rules)
    router.last_route_all_vias_blocked = True
    target = Pad(
        6.45,
        5.1,
        0.4,
        0.4,
        source.net,
        source.net_name,
        layer=Layer.B_CU,
        ref="U2",
        pin="B6",
        escape_terminal=True,
    )
    target_via = Via(target.x, target.y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), source.net)
    grid.mark_route(Route(source.net, source.net_name, vias=[target_via]))
    # Enclose the target on B.Cu so the retry route from B.Cu must transition
    # back to F.Cu before landing on the through barrel.
    _surround_box_on_layer(grid, Layer.B_CU, 5.4, 4.2, 7.5, 6.2, net=35)

    owner = SimpleNamespace(
        router=router,
        grid=grid,
        _escape=escape,
        all_pads=pads,
        _edge_segments=(),
        _edge_clearance=0.0,
    )
    ends = (target, source) if reverse else (source, target)

    # Expired deadline returns None without mutating routes.
    assert retry_preserved_departure(owner, *ends, time.monotonic() - 1.0) is None

    # Disallowed landing layer returns None cleanly.
    grid.rules.allowed_layers = ["F.Cu"]
    assert retry_preserved_departure(owner, *ends, None) is None
    grid.rules.allowed_layers = None

    candidate = retry_preserved_departure(owner, *ends, None)
    assert candidate is not None
    assert candidate.segments[0].layer == Layer.F_CU
    assert candidate.segments[-1].layer == Layer.F_CU
    assert len(candidate.vias) >= 2
