"""A two-row SMD breakout with preserved copper on either side of its pad."""

from types import SimpleNamespace

import pytest

from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.preserved_pad_escape import find_departure, retry_preserved_departure
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


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
