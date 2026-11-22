"""A route's returning leg must leave real air beside its own via (board 05)."""

from dataclasses import replace

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.route_via_gap import route_own_via_slit
from kicad_tools.router.rules import DesignRules
from tests.test_physical_copper_gap import _fab, _one_oz, _via, board, track

VIA = (56.665, 15.630)
TURN = (56.990, 15.305)
DEST = (60.760, 19.075)
EDGE = 0.1016


def candidate(points, layer=Layer.B_CU):
    return Route(
        net=1,
        net_name="RESET",
        segments=[
            Segment(*a, *b, 0.2, layer, net=1, net_name="RESET")
            for a, b in zip(points, points[1:], strict=False)
        ],
        vias=[Via(*VIA, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1, net_name="RESET")],
    )


def test_reset_return_and_controls_match_fabrication_checker():
    def gaps(points):
        tracks = [
            track(a, b, f"s{i}").replace("F.Cu", "B.Cu")
            for i, (a, b) in enumerate(zip(points, points[1:], strict=False))
        ]
        return _fab(board(_via(*VIA, "v"), *tracks), _one_oz())

    assert [v.actual_value for v in gaps([VIA, TURN, DEST])] == pytest.approx([0.077156], abs=1e-6)
    assert gaps([VIA, (60.110, 19.075), DEST]) == []
    assert gaps([VIA, TURN, (57.315, 14.980)]) == []


@pytest.mark.parametrize(
    "backend", ["python", pytest.param("cpp", marks=pytest.mark.requires_router_cpp)]
)
def test_returning_leg_is_rejected_and_connected_exits_survive(backend):
    rules = DesignRules(grid_resolution=0.1, trace_width=0.2, same_net_trace_gap_band_edge_mm=EDGE)
    grid = RoutingGrid(width=9, height=8, origin_x=54, origin_y=14, rules=rules)
    start = Pad(*VIA, 0.4, 0.4, 1, "RESET")
    end = replace(start, x=DEST[0], y=DEST[1], layer=Layer.B_CU)
    if backend == "cpp":
        native = CppPathfinder(CppGrid.from_routing_grid(grid), rules)

        def valid(route):
            return native._validate_route_clearance(route, start, end, 1) is None
    else:
        python = Router(grid, rules)

        def valid(route):
            return python._validate_route_clearance(route, exclude_net=1)

    assert valid(candidate([VIA, (60.110, 19.075), DEST]))
    assert valid(candidate([VIA, TURN, (57.315, 14.980)]))
    assert not valid(candidate([VIA, TURN, DEST]))


@pytest.mark.parametrize(
    ("gap", "expected_slit"),
    [(-0.2, False), (0.0, True), (0.06, True), (EDGE, False), (0.12, False)],
)
def test_emitted_width_and_barrel_define_an_open_band(gap, expected_slit):
    y = VIA[1] + 0.4 + 0.15 + gap
    route = candidate([(VIA[0] - 1, y), (VIA[0] + 1, y)])
    route.vias[0].diameter = 0.8
    route.segments[0].width = 0.3
    # A tangent parallel leg still leaves facing air beyond the point of
    # contact. The minimum open gap on its printed copper is 0.030448 mm.
    assert bool(_fab(board(route.to_sexp()), _one_oz())) is expected_slit
    assert (route_own_via_slit(route, EDGE) is not None) is expected_slit


def test_unresolved_edge_and_layer_outside_a_blind_via_span():
    route = candidate([VIA, TURN, DEST])
    assert route_own_via_slit(route, None) is None
    assert route_own_via_slit(route, 0) is None
    route.vias[0] = replace(route.vias[0], layers=(Layer.F_CU, Layer.IN2_CU), span_type="blind")
    assert route_own_via_slit(route, EDGE) is None
    for segment in route.segments:
        segment.layer = Layer.IN1_CU
    assert route_own_via_slit(route, EDGE) is not None


@pytest.mark.parametrize(
    "backend", ["python", pytest.param("cpp", marks=pytest.mark.requires_router_cpp)]
)
def test_search_retries_the_reported_turn_and_connects_the_pad(backend, monkeypatch):
    rules = DesignRules(grid_resolution=0.1, trace_width=0.2, same_net_trace_gap_band_edge_mm=EDGE)
    grid = RoutingGrid(width=9, height=8, origin_x=54, origin_y=14, rules=rules)
    start = Pad(*VIA, 0.6, 0.6, 1, "RESET", layer=Layer.F_CU, ref="U1", pin="1")
    end = replace(start, x=DEST[0], y=DEST[1], layer=Layer.B_CU, ref="U2")
    for pad in (start, end):
        grid.add_pad(pad)

    # Recreate the first known bad result at the boundary at which it was
    # observed on board 05. All following attempts use the real grid search.
    attempts = 0
    if backend == "cpp":
        router = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        convert_cpp = router._convert_result_to_route

        def reconstruct_cpp(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return candidate([VIA, TURN, DEST])
            return convert_cpp(*args, **kwargs)

        monkeypatch.setattr(router, "_convert_result_to_route", reconstruct_cpp)
    else:
        router = Router(grid, rules)
        convert_python = router._convert_path_to_route

        def reconstruct_python(path, route, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                bad = candidate([VIA, TURN, DEST])
                route.segments, route.vias = bad.segments, bad.vias
            else:
                convert_python(path, route, *args, **kwargs)

        monkeypatch.setattr(router, "_convert_path_to_route", reconstruct_python)

    routed = router.route(start, end)
    assert attempts >= 2
    assert routed is not None and routed.vias
    assert any(VIA in (s.start, s.end) for s in routed.segments)
    assert any(DEST in (s.start, s.end) for s in routed.segments)
    assert _fab(board(routed.to_sexp()), _one_oz()) == []
