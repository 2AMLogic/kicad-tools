"""The native search must grade a new trace against an already stored via (#9273)."""

from itertools import pairwise

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


def _saved_via_findings(tmp_path, runs, layer):
    from kicad_tools.schema.pcb import PCB
    from tests.test_physical_copper_gap import _fab, _one_oz, track

    tracks = "\n".join(
        track(start, end, f"trace-{i}").replace("F.Cu", layer)
        for i, (start, end) in enumerate(runs)
    )
    file = tmp_path / "via-approach.kicad_pcb"
    file.write_text(
        f"""(kicad_pcb (version 20240108) (generator pcbnew) (general (thickness 1.6))
        (layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal))
        (net 0 "") (net 1 "GND")
        (via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "stored"))
        {tracks})"""
    )
    return [v for v in _fab(PCB.load(file), _one_oz()) if v.severity == "error"]


@pytest.mark.parametrize("method", ["route", "route_resumable"])
@pytest.mark.parametrize("layer", [0, 3])
@pytest.mark.parametrize(
    "dx,dy,offset",
    [(1, 0, 0.35), (1, 0, 0.40), (1, 0, 0.45), (1, 0, 0.50), (1, 0, 0.55), (1, 1, 0.30)],
)
def test_saved_route_has_no_open_air_beside_a_stored_via(tmp_path, method, layer, dx, dy, offset):
    from kicad_tools.router import router_cpp
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.pairwise_clearance import _fuse_collinear_runs
    from kicad_tools.router.primitives import Segment

    grid = router_cpp.Grid3D(150, 150, 4, 0.05, 1.25, 1.25)
    grid.add_stored_via(5, 5, 0.30, 0.60, 1, layer_from=0, layer_to=3)
    rules = router_cpp.DesignRules()
    rules.trace_width = 0.125
    rules.trace_clearance = rules.via_clearance = 0.10
    rules.same_net_trace_gap_band_edge_mm = 0.1016
    finder = router_cpp.Pathfinder(grid, rules, True)
    finder.set_routable_layers([layer])
    start = (5 - 2 * dx - dy * offset, 5 - 2 * dy + dx * offset)
    end = (5 + 2 * dx - dy * offset, 5 + 2 * dy + dx * offset)
    result = getattr(finder, method)(
        *start, layer, *end, layer, 1, negotiated_mode=True, emit_trace_width=0.20
    )
    assert result.success and not result.vias
    face = Layer.F_CU if layer == 0 else Layer.B_CU
    segments = _fuse_collinear_runs(
        [Segment(s.x1, s.y1, s.x2, s.y2, s.width, face, net=1) for s in result.segments]
    )
    assert segments[0].start == pytest.approx(start)
    assert segments[-1].end == pytest.approx(end)
    if dy == 0 and offset in (0.35, 0.55):  # deep contact and a clear pass
        assert len(segments) == 1
    assert not _saved_via_findings(tmp_path, [(s.start, s.end) for s in segments], face.kicad_name)


def test_one_via_contact_does_not_exempt_the_incoming_leg(tmp_path):
    # Both paths have the same endpoints. The diagonal on the first route
    # touches the stored barrel while its incoming horizontal leg leaves air.
    bad = [(3, 5.45), (5, 5.45), (5.10, 5.35), (6.90, 5.35), (7, 5.45)]
    clean = [(3, 5.45), (3.15, 5.60), (6.85, 5.60), (7, 5.45)]
    findings = _saved_via_findings(tmp_path, pairwise(bad), "F.Cu")
    assert [v.actual_value for v in findings] == pytest.approx([0.053247], abs=1e-6)
    assert not _saved_via_findings(tmp_path, pairwise(clean), "F.Cu")
