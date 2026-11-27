"""Board 05's seed-42 optimizer shortcuts that park a bend beside their own pad's mask opening."""

import argparse
import math

import pytest

from kicad_tools.cli.route_cmd import (
    _optimize_traces,
    _resolve_route_mask_guard,
    _uniform_board_mask_margin,
)
from kicad_tools.geometry.copper import standard_pad_polygon
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pad_slits import mask_opening_check
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules
from kicad_tools.validate.mask_copper import clean_own_pad_entry

JLCPCB_MASK_CLEARANCE = 0.09
CHECKER_UNCERTAINTY_MM = 0.010004
GUARD = (JLCPCB_MASK_CLEARANCE, 0.0)
RULES = {"same_net_pad_gap_band_edge_mm": 0.15, "same_net_trace_gap_band_edge_mm": 0.1016}

#: U1/14 on board 05: a 0.55 x 1.475 roundrect centred (161.7, 104.1625).
U1_14 = ((161.7, 104.1625), (0.55, 1.475), "roundrect")
#: J3/3 on board 05: a 1.7 mm circle centred (178.5, 95.08).
J3_3 = ((178.5, 95.08), (1.7, 1.7), "circle")
#: RV1/2 on board 05: a 1.44 mm circle centred (169.96, 131.0), the SPEED chain's end.
RV1_2 = ((169.96, 131.0), (1.44, 1.44), "circle")

#: The nFAULT F.Cu chain the router left before the optimizer (commit journal record 314).
NFAULT = [
    (161.7, 104.1625),
    (161.585, 104.0475),
    (161.585, 103.58),
    (161.585, 103.19),
    (161.065, 102.67),
    (157.815, 102.67),
]
#: The HALL_A F.Cu chain the router left before the optimizer (record 317).
HALL_A = [
    (173.4125, 91.0),
    (173.87, 90.5425),
    (173.87, 90.32),
    (174.455, 89.735),
    (175.17, 89.735),
    (177.055, 91.62),
    (177.055, 93.7),
    (177.705, 94.35),
    (178.435, 95.08),
    (178.5, 95.08),
]
#: The SPEED F.Cu chain the router left before the optimizer (record 50).
SPEED = [
    (170.55, 126.0),
    (170.1, 126.45),
    (170.1, 126.72),
    (170.1, 130.295),
    (169.96, 130.435),
    (169.96, 131.0),
]

_ROUNDRECT = {"shape": "roundrect", "roundrect_rratio": 0.25}
_PIN = {"width": 0.55, "height": 1.475, **_ROUNDRECT}
NFAULT_PADS = [
    ("U1", {"x": 161.7, "y": 104.1625, **_PIN}),
    ("U1L", {"x": 160.9, "y": 104.1625, "net": 11, "net_name": "OTHER_A", **_PIN}),
    ("U1R", {"x": 162.5, "y": 104.1625, "net": 12, "net_name": "OTHER_B", **_PIN}),
    ("V1", {"x": 157.815, "y": 102.67, "width": 0.6, "height": 0.6, "shape": "circle"}),
]
HALL_A_PADS = [
    ("R10", {"x": 173.4125, "y": 91.0, "width": 0.6, "height": 0.6, "shape": "circle"}),
    (
        "J3",
        {
            "x": 178.5,
            "y": 95.08,
            "width": 1.7,
            "height": 1.7,
            "shape": "circle",
            "through_hole": True,
            "drill": 1.0,
        },
    ),
]
SPEED_PADS = [
    (
        "C14",
        {"x": 170.55, "y": 126.0, "width": 1.0, "height": 1.45, **_ROUNDRECT},
    ),
    (
        "RV1",
        {
            "x": 169.96,
            "y": 131.0,
            "width": 1.44,
            "height": 1.44,
            "shape": "circle",
            "through_hole": True,
            "drill": 1.0,
        },
    ),
]


def _router(origin, pads, points, net, name):
    router = Autorouter(
        16,
        16,
        rules=DesignRules(**RULES),
        origin_x=origin[0],
        origin_y=origin[1],
        force_python=True,
        physics_enabled=False,
    )
    for ref, kwargs in pads:
        router.add_component(ref, [{"number": "1", "net": net, "net_name": name, **kwargs}])
    route = Route(
        net=net,
        net_name=name,
        segments=[
            Segment(*a, *b, 0.2, Layer.F_CU, net, name)
            for a, b in zip(points, points[1:], strict=False)
        ],
    )
    router.routes = [route]
    router.grid.mark_route(route)
    return router


def _nfault(points=NFAULT):
    return _router((155, 99), NFAULT_PADS, points, 10, "nFAULT")


def _hall_a():
    return _router((170, 86), HALL_A_PADS, HALL_A, 15, "HALL_A")


def _speed():
    return _router((164, 121), SPEED_PADS, SPEED, 18, "SPEED")


def _run(router, *, guard):
    args = argparse.Namespace(_mfr_mask_guard=GUARD) if guard else argparse.Namespace()
    _optimize_traces(router, args, quiet=True)
    return router.routes[0]


def _points(route):
    segments = route.segments
    return [(round(s.x1, 6), round(s.y1, 6)) for s in segments] + [
        (round(segments[-1].x2, 6), round(segments[-1].y2, 6))
    ]


def _length(route):
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)


def _mask_defects(route, pad):
    """Legs ``kct check --mfr jlcpcb`` reports against *pad*'s opening, by the rule's own measure."""
    from shapely.geometry import LineString

    (cx, cy), (w, h), shape = pad
    opening = standard_pad_polygon(cx, cy, w, h, shape, 0.0, 0.25)
    defects = []
    for seg in route.segments:
        copper = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)]).buffer(
            seg.width / 2, quad_segs=64
        )
        gap = copper.distance(opening)
        if 0 < gap < JLCPCB_MASK_CLEARANCE:
            defects.append(round(gap, 4))
        elif gap == 0 and not clean_own_pad_entry(
            opening,
            copper,
            opening,
            f"pad:{shape}",
            seg.width,
            ((seg.x1, seg.y1), (seg.x2, seg.y2)),
            clearance_mm=JLCPCB_MASK_CLEARANCE,
            uncertainty_mm=CHECKER_UNCERTAINTY_MM,
        ):
            defects.append(0.0)
    return defects


def test_staircase_shortcut_parks_its_bend_beside_the_pad_without_the_guard():
    """The defect: the compressed exit turns 0.12 mm above U1/14, 0.02 mm from the opening."""
    route = _run(_nfault(), guard=False)
    assert _points(route) == [
        (161.7, 104.1625),
        (161.7, 103.305),
        (161.065, 102.67),
        (157.815, 102.67),
    ]
    assert _mask_defects(route, U1_14) == [0.02]


def test_staircase_shortcut_beside_own_pad_is_declined():
    route = _run(_nfault(), guard=True)
    assert _points(route) == [
        (161.7, 104.1625),
        (161.585, 104.0475),
        (161.585, 103.19),
        (161.065, 102.67),
        (157.815, 102.67),
    ]
    assert _mask_defects(route, U1_14) == []


def test_detour_shortcut_beside_own_pad_is_declined():
    unguarded = _run(_hall_a(), guard=False)
    assert _points(unguarded) == [(173.4125, 91.0), (177.4925, 95.08), (178.5, 95.08)]
    assert _mask_defects(unguarded, J3_3) == [0.0575]

    incoming = _hall_a().routes[0]
    guarded = _run(_hall_a(), guard=True)
    assert _mask_defects(guarded, J3_3) == []
    assert _length(guarded) < _length(incoming)


def test_rewrite_that_leaves_the_chain_no_worse_is_kept():
    """The router's own RV1/2 exit already overlaps the wiper opening: the shortcut is kept."""
    assert (
        _points(_run(_speed(), guard=False))
        == _points(_run(_speed(), guard=True))
        == [
            (170.55, 126.0),
            (170.55, 130.41),
            (169.96, 131.0),
        ]
    )


@pytest.mark.parametrize(("grid", "clearance"), [(None, 0.09), ("grid", 0.0), ("grid", -0.1)])
def test_guard_needs_a_grid_and_a_clearance(grid, clearance):
    assert mask_opening_check(grid, clearance) is None


def test_mask_margin_grows_the_opening():
    grid = _nfault().grid
    above = [Segment(161.2, 103.2, 162.2, 103.2, 0.2, Layer.F_CU, 10, "nFAULT")]
    assert mask_opening_check(grid, JLCPCB_MASK_CLEARANCE, 0.0)([], above, [])
    assert not mask_opening_check(grid, JLCPCB_MASK_CLEARANCE, 0.05)([], above, [])


def test_board_mask_margin_resolution(tmp_path):
    plain = tmp_path / "plain.kicad_pcb"
    plain.write_text("(setup (pad_to_mask_clearance 0.05))")
    assert _uniform_board_mask_margin(plain) == 0.05
    undeclared = tmp_path / "undeclared.kicad_pcb"
    undeclared.write_text("(setup)")
    assert _uniform_board_mask_margin(undeclared) == 0.0
    overridden = tmp_path / "overridden.kicad_pcb"
    overridden.write_text("(setup (pad_to_mask_clearance 0)) (pad (solder_mask_margin 0.2))")
    assert _uniform_board_mask_margin(overridden) is None
    ruled = tmp_path / "ruled.kicad_pcb"
    ruled.write_text("(setup)")
    (tmp_path / "ruled.kicad_dru").write_text("(constraint solder_mask_margin 0.1)")
    assert _uniform_board_mask_margin(ruled) is None
    assert _uniform_board_mask_margin(tmp_path / "missing.kicad_pcb") is None


def test_route_mask_guard_resolution(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(setup (pad_to_mask_clearance 0))")
    assert _resolve_route_mask_guard(None, board) is None
    assert _resolve_route_mask_guard("seeed", board) is None
    assert _resolve_route_mask_guard("jlcpcb", board) == GUARD
    board.write_text("(pad (solder_mask_margin 0.1))")
    assert _resolve_route_mask_guard("jlcpcb", board) is None
