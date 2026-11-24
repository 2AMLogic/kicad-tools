"""The normal optimizer must grade new traces against vias on the same net."""

from argparse import Namespace
from itertools import pairwise
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString, Point

from kicad_tools.cli.route_cmd import _optimize_traces
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.optimizer.pad_slits import via_gap_check
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules
from kicad_tools.validate.rules.physical_gap import CopperSource, iter_source_gaps

VM_PATH = [
    (134.500, 68.000),
    (134.805, 67.695),
    (134.805, 67.115),
    (134.805, 66.270),
    (135.390, 65.685),
    (156.645, 65.685),
]
SHORTCUT = [VM_PATH[0], (136.815, 65.685), VM_PATH[-1]]


def _segments(points, layer):
    return [Segment(*a, *b, 0.2, layer, 26, "VM") for a, b in pairwise(points)]


def _gap(segments, via):
    sources = [
        CopperSource(
            LineString([seg.start, seg.end]).buffer(seg.width / 2, quad_segs=64),
            "F.Cu",
            seg.net_name,
            f"track-{i}",
            "track",
        )
        for i, seg in enumerate(segments)
        if seg.layer == Layer.F_CU
    ]
    sources.append(
        CopperSource(
            Point(via.x, via.y).buffer(via.diameter / 2, quad_segs=64),
            "F.Cu",
            via.net_name,
            "via",
            "via",
        )
    )
    return min(
        (g.distance for g in iter_source_gaps(sources, 0.1016) if "via" in g.ids), default=None
    )


@pytest.mark.parametrize(
    ("via_x", "edge", "expected_shortcut"),
    [
        (136.365, 0.1016, False),
        (136.765, 0.1016, True),
        (136.365, 0.0100, True),
        (136.365, None, True),
    ],
)
def test_standard_optimizer_honors_resolved_via_gap(via_x, edge, expected_shortcut):
    router = Autorouter(
        30,
        15,
        rules=DesignRules(same_net_trace_gap_band_edge_mm=edge),
        origin_x=131,
        origin_y=60,
        force_python=True,
        physics_enabled=False,
    )
    for name, (x, y) in zip(("A", "D"), (VM_PATH[0], VM_PATH[-1]), strict=True):
        router.add_component(
            name,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 0.6,
                    "height": 0.6,
                    "shape": "rect",
                    "net": 26,
                    "net_name": "VM",
                }
            ],
        )
    via = Via(via_x, 66.725, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 26, "VM")
    lead = _segments([(via.x - 0.59, via.y), (via.x, via.y)], Layer.F_CU)
    exit_legs = _segments([(via.x, via.y), (via.x + 8.58, via.y)], Layer.B_CU)
    candidate = Route(net=26, net_name="VM", segments=_segments(VM_PATH, Layer.F_CU))
    sibling = Route(net=26, net_name="VM", segments=[*lead, *exit_legs], vias=[via])
    assert _gap([*candidate.segments, *lead], via) is None
    router.routes = [candidate, sibling]
    for route in router.routes:
        router.grid.mark_route(route)
    _optimize_traces(router, Namespace(), quiet=True)
    saved, adjacent = router.routes
    assert adjacent.vias == [via]
    points = [seg.start for seg in saved.segments] + [saved.segments[-1].end]
    assert points == (SHORTCUT if expected_shortcut else VM_PATH)
    gap = _gap([*saved.segments, *adjacent.segments], via)
    if via_x == 136.365 and expected_shortcut:
        assert gap == pytest.approx(0.017193, abs=1e-6)
    else:
        assert gap is None


@pytest.mark.parametrize("shield", ["bare", "sibling-trace", "pad", "buried-via"])
def test_only_exposed_via_copper_can_block_a_shortcut(shield):
    via = Via(5, 5, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 26, "VM")
    if shield == "buried-via":
        via.layers = (Layer.IN1_CU, Layer.IN2_CU)
        via.span_type = "buried"
    track = Route(net=26, net_name="VM", segments=_segments([(2, 6), (8, 6)], Layer.F_CU))
    sibling = Route(net=26, net_name="VM", vias=[via])
    pads = []
    if shield == "sibling-trace":
        sibling.segments.append(Segment(4, 5.2, 6, 5.2, 0.6, Layer.F_CU, 26, "VM"))
    elif shield == "pad":
        pads.append(
            Pad(
                x=5,
                y=5,
                width=1.4,
                height=1.4,
                layer=Layer.F_CU,
                net=26,
                net_name="VM",
                ref="U1",
                pin="1",
                shape="rect",
            )
        )
    grid = SimpleNamespace(
        routes=[track, sibling],
        pads=pads,
        rules=DesignRules(same_net_trace_gap_band_edge_mm=0.1016),
    )
    check = via_gap_check(grid, track)
    replacement = _segments([(2, 5.45), (8, 5.45)], Layer.F_CU)
    assert (check is None or check(track.segments, replacement, [])) == (shield != "bare")


@pytest.mark.parametrize(
    ("bend", "next_bend", "blocked", "trunk_start"),
    [
        pytest.param((163.990, 105.465), (163.990, 108.715), False, 106.375, id="extend-launch"),
        pytest.param((164.055, 105.400), (164.055, 108.780), False, 106.245, id="clear-shortcut"),
        pytest.param((163.990, 105.465), (163.990, 108.715), True, 109.625, id="obstructed-exit"),
    ],
)
def test_run_staircase_beside_its_own_via(bend, next_bend, blocked, trunk_start):
    """Board 05's RUN exit and the two MISO objects that constrain its shortcut."""
    router = Autorouter(
        24,
        40,
        origin_x=150,
        origin_y=92,
        rules=DesignRules(
            grid_resolution=0.1, trace_width=0.15, same_net_trace_gap_band_edge_mm=0.1016
        ),
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
        force_python=True,
        physics_enabled=False,
    )
    start, barrel, end = (164.25, 101.0), (164.25, 105.205), (158.335, 128.54)
    for ref, (x, y), layer in (("A", start, Layer.B_CU), ("B", end, Layer.F_CU)):
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 0.35,
                    "height": 0.35,
                    "shape": "rect",
                    "net": 19,
                    "net_name": "RUN",
                    "layer": layer,
                }
            ],
        )
    points = [barrel, bend, next_bend, (164.9, 109.625), (164.9, 121.975), end]
    via = Via(*barrel, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 19, "RUN")
    back = Segment(*start, *barrel, 0.15, Layer.B_CU, 19, "RUN")
    front = [Segment(*a, *b, 0.15, Layer.F_CU, 19, "RUN") for a, b in pairwise(points)]
    route = Route(net=19, net_name="RUN", segments=[back, *front], vias=[via])
    miso = Route(
        net=12,
        net_name="MISO",
        segments=[Segment(163.405, 104.2675, 163.405, 108.91, 0.2, Layer.F_CU, 12, "MISO")],
        vias=[Via(164.315, 109.82, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 12, "MISO")],
    )
    router.routes = [route]
    router.grid.mark_route(route)
    router.grid.mark_route(miso)
    if blocked:
        obstacle = Segment(163.49, 105.49, 163.55, 105.49, 0.15, Layer.F_CU, 50, "BLOCK")
        router.grid.mark_route(Route(net=50, net_name="BLOCK", segments=[obstacle]))
    assert _gap(front, via) is None

    _optimize_traces(router, Namespace(), quiet=True)

    (result,) = router.routes
    assert result.vias == [via]
    assert back in result.segments
    assert _gap(result.segments, via) is None
    assert ((164.9, trunk_start), (164.9, 121.975)) in [
        (seg.start, seg.end) for seg in result.segments
    ]
    if blocked:
        assert result.segments == route.segments


def test_via_exit_at_either_end_uses_only_its_drilled_layers():
    from kicad_tools.router.optimizer.pad_slits import via_exit_doglegs

    points = [(0.0, 0.0), (-0.26, 0.26), (0.65, 1.17), (0.65, 5.0)]
    forward = _segments(points, Layer.F_CU)
    reverse = _segments(reversed(points), Layer.F_CU)
    via = Via(0, 0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 26, "VM")
    grid = SimpleNamespace(
        routes=[Route(net=26, net_name="VM", segments=forward, vias=[via])],
        rules=DesignRules(same_net_trace_gap_band_edge_mm=0.1016),
        resolution=0.1,
    )
    ((_, repaired),) = via_exit_doglegs(forward, grid)
    ((_, backwards),) = via_exit_doglegs(reverse, grid)
    assert [(s.start, s.end) for s in repaired] == [(s.end, s.start) for s in reversed(backwards)]
    assert repaired[-1] is forward[-1]
    assert backwards[0] is reverse[0]
    assert _gap(repaired, via) is None

    via.layers = (Layer.IN1_CU, Layer.IN2_CU)
    via.span_type = "buried"
    assert list(via_exit_doglegs(forward, grid)) == []
    assert list(via_exit_doglegs(reverse, grid)) == []
