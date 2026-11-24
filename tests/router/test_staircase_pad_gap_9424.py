"""Board 05's seed-42 SPEED pad exit through the ordinary route optimizer."""

import argparse

import pytest

from kicad_tools.cli.route_cmd import _optimize_traces
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

PAD = (170.55, 126.0)
END = (169.96, 131.0)
ORIGINAL = [PAD, (170.1, 126.45), (170.1, 126.72), (170.1, 130.295), (169.96, 130.435), END]
DIAGONAL_FIRST = (169.96, 126.59)
VERTICAL_FIRST = (170.55, 130.41)


def optimize_exit(edge, *, block_vertical=False, pad_layer=Layer.F_CU):
    router = Autorouter(
        14,
        14,
        rules=DesignRules(same_net_pad_gap_band_edge_mm=edge),
        origin_x=164,
        origin_y=121,
        force_python=True,
        physics_enabled=False,
    )

    def add_pad(ref, xy, size, net, name, **kwargs):
        router.add_component(
            ref,
            [
                dict(
                    number="1",
                    x=xy[0],
                    y=xy[1],
                    width=size[0],
                    height=size[1],
                    net=net,
                    net_name=name,
                    **kwargs,
                )
            ],
        )

    add_pad(
        "C14",
        PAD,
        (1, 1.45),
        18,
        "SPEED",
        shape="roundrect",
        roundrect_rratio=0.25,
        layer=pad_layer,
    )
    add_pad("U1", END, (0.6, 0.6), 18, "SPEED")
    if block_vertical:
        add_pad("C1", (170.55, 129), (0.2, 0.2), 19, "OTHER", shape="circle")
    route = Route(
        net=18,
        net_name="SPEED",
        segments=[
            Segment(*a, *b, 0.2, Layer.F_CU, 18, "SPEED")
            for a, b in zip(ORIGINAL, ORIGINAL[1:], strict=False)
        ],
    )
    router.routes = [route]
    router.grid.mark_route(route)
    _optimize_traces(router, argparse.Namespace(), quiet=True)
    segments = router.routes[0].segments
    assert all(s.net == 18 and s.layer == Layer.F_CU and s.width == 0.2 for s in segments)
    return [(s.x1, s.y1) for s in segments] + [(segments[-1].x2, segments[-1].y2)]


def test_fab_pad_gap_selects_clear_shortcut():
    assert optimize_exit(0.15) == [PAD, VERTICAL_FIRST, END]


def test_foreign_pad_blocks_alternative_and_retains_legal_exit():
    # The adjacent collinear middle legs can still merge.
    assert optimize_exit(0.15, block_vertical=True) == [
        PAD,
        ORIGINAL[1],
        ORIGINAL[3],
        ORIGINAL[4],
        END,
    ]


@pytest.mark.parametrize(
    ("edge", "layer"), [(None, Layer.F_CU), (0.02, Layer.F_CU), (0.15, Layer.B_CU)]
)
def test_pad_layer_and_resolved_fabrication_edge_scope_the_shortcut(edge, layer):
    assert optimize_exit(edge, pad_layer=layer) == [PAD, DIAGONAL_FIRST, END]
