"""The native search must grade a new trace against an already stored via (#9273)."""

import pytest

from kicad_tools.core.geometry import point_to_segment_distance

pytestmark = pytest.mark.requires_router_cpp

# Dyadic values make both the touching copper and the exact upper edge
# representable in the native float grid.
VIA_X, VIA_Y = 5.0, 5.0
DIAMETER = 0.5
WIDTH = 0.25
EDGE = 0.25
NET = 7


def _route(method, start, end, *, band=EDGE, layer=0, via_span=(0, 3), via_net=NET):
    from kicad_tools.router import router_cpp

    grid = router_cpp.Grid3D(80, 80, 4, 0.125, 0.0, 0.0)
    grid.add_stored_via(
        VIA_X, VIA_Y, 0.25, DIAMETER, via_net, layer_from=via_span[0], layer_to=via_span[1]
    )
    rules = router_cpp.DesignRules()
    rules.trace_width = 0.125  # the emitted per-net override below owns the gap
    rules.trace_clearance = rules.via_clearance = 0.0625
    rules.same_net_trace_gap_band_edge_mm = band
    finder = router_cpp.Pathfinder(grid, rules, True)
    finder.set_routable_layers([layer])
    result = getattr(finder, method)(
        *start, layer, *end, layer, NET, negotiated_mode=True, emit_trace_width=WIDTH
    )
    assert result.success
    assert result.segments and not result.vias
    return result.segments


def _minimum_gap(segments):
    return min(
        point_to_segment_distance(VIA_X, VIA_Y, s.x1, s.y1, s.x2, s.y2) - (DIAMETER + s.width) / 2
        for s in segments
    )


@pytest.mark.parametrize("method", ["route", "route_resumable"])
@pytest.mark.parametrize("layer", [0, 3])
def test_stored_via_then_trace_avoids_open_slit(method, layer):
    # Board 03 VBUS exhibited this order on both F.Cu and B.Cu. A straight
    # sweep here leaves 0.125 mm between the via and the trace.
    start, end = (2, 5.5), (8, 5.5)
    assert _minimum_gap(_route(method, start, end, layer=layer, band=0)) == 0.125
    gap = _minimum_gap(_route(method, start, end, layer=layer))
    assert gap <= 1e-9 or gap >= EDGE - 1e-9


@pytest.mark.parametrize("offset", [0, 0.25, 0.375, 0.625, 0.75])
def test_overlap_contact_and_band_edge_keep_the_straight_route(offset):
    y = VIA_Y + offset
    segments = _route("route_resumable", (2, y), (8, y))
    assert all(s.y1 == y and s.y2 == y for s in segments)


@pytest.mark.parametrize("start,end", [((2, 5), (5, 5)), ((5, 5), (8, 5))])
def test_a_radial_trace_can_enter_or_leave_the_annulus(start, end):
    segments = _route("route_resumable", start, end)
    assert all(s.y1 == VIA_Y and s.y2 == VIA_Y for s in segments)


@pytest.mark.parametrize("via_span,via_net", [((1, 2), NET), ((0, 3), NET + 1)])
def test_other_layers_and_foreign_vias_use_only_their_own_clearance(via_span, via_net):
    # 0.125 exceeds the foreign-net clearance of 0.0625.
    segments = _route("route_resumable", (2, 5.5), (8, 5.5), via_span=via_span, via_net=via_net)
    assert _minimum_gap(segments) == 0.125


@pytest.mark.parametrize("method", ["route", "route_resumable"])
@pytest.mark.parametrize("start,end", [((2, 5), (4.5, 5)), ((4.5, 5), (2, 5))])
def test_an_endpoint_inside_the_band_can_connect_through_the_annulus(method, start, end):
    # The direct segment ends 0.125 mm short. A legal route visits the
    # annulus and approaches this endpoint from the other side.
    assert _minimum_gap(_route(method, start, end)) <= 1e-9


@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_diagonal_closest_approach_falls_between_grid_vertices(method):
    # The normal from (5,5) meets this diagonal halfway through a grid edge.
    start, end = (2, 2.625), (8, 8.625)
    original_gap = _minimum_gap(_route(method, start, end, band=0))
    assert 0 < original_gap < EDGE
    gap = _minimum_gap(_route(method, start, end))
    assert gap <= 1e-9 or gap >= EDGE - 1e-9
