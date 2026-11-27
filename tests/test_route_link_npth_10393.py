"""``kct route-link`` keeps tracks out of non-plated mounting holes (Issue #10393)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli import main  # noqa: E402
from kicad_tools.router.link_router import (  # noqa: E402
    LinkRouteRules,
    _build_model,
    route_clears_floors,
    route_link,
    terminal_for_pad,
)
from kicad_tools.schema.pcb import PCB  # noqa: E402
from tests.test_route_link_hv_8546 import _pad, _write  # noqa: E402

SIG = _pad(1, 105, 110, 1, "SIG") + _pad(2, 125, 110, 1, "SIG")


def _npth(x: float, y: float, size: float, drill: float = 0.6) -> str:
    """Unnumbered ``np_thru_hole`` pad on ``J9`` (the issue's mounting hole)."""
    return (
        f'  (footprint "MH" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000099") '
        f"(at {x} {y})\n"
        '    (fp_text reference "J9" (at 0 -1.5) (layer "F.SilkS"))\n'
        f'    (pad "" np_thru_hole circle (at 0 0) (size {size} {size}) (drill {drill}) '
        '(layers "*.Cu" "*.Mask")))\n'
    )


def _probe(board, capsys):
    rc = main(
        [
            "route-link",
            "--probe",
            "--format",
            "json",
            str(board),
            "--link",
            "SIG R1.1 R2.1",
            "--link-layers",
            "F.Cu",
        ]
    )
    return rc, json.loads(capsys.readouterr().out)["links"][0]


def _route(board, **rule_kwargs):
    pcb = PCB.load(str(board))
    nets = {n.name: num for num, n in pcb.nets.items() if n.name}
    rules = LinkRouteRules(**rule_kwargs)
    model = _build_model(pcb)
    route = route_link(
        pcb,
        nets["SIG"],
        "SIG",
        terminal_for_pad(pcb, "R1", "1"),
        terminal_for_pad(pcb, "R2", "1"),
        rules,
        model=model,
        allowed_layers={"F.Cu"},
    )
    return route, model, rules


# Model coordinates are relative to the board origin (100, 100), so the hole
# placed at (113, 110) in the file sits at (13, 10) here.
def _min_gap_to_hole(route, hx, hy, radius, half_w):
    from shapely.geometry import LineString, Point

    hole = Point(hx, hy)
    return min(
        LineString([(x1, y1), (x2, y2)]).distance(hole) - radius - half_w
        for x1, y1, x2, y2, _layer in route.segments
    )


def test_adjacent_hole_keeps_straight_route(tmp_path, capsys):
    board = _write(tmp_path, SIG + _npth(106, 111, 0.6))
    rc, link = _probe(board, capsys)
    assert rc == 0
    assert link["routable"]
    assert link["length_mm"] == pytest.approx(19.3, abs=0.01)


@pytest.mark.parametrize("size", [0.6, 0.8], ids=["drill-only", "under-0.8mm-pad"])
def test_centered_hole_is_detoured(tmp_path, capsys, size):
    board = _write(tmp_path, SIG + _npth(113, 110, size))
    rc, link = _probe(board, capsys)
    assert rc == 0
    assert link["routable"]
    # The straight 19.30 mm line crosses the drill; any legal path is longer.
    assert link["length_mm"] > 19.3 + 0.1


@pytest.mark.parametrize("size", [0.6, 0.8], ids=["drill-only", "under-0.8mm-pad"])
def test_routed_geometry_clears_hole_and_pad_copper(tmp_path, size):
    board = _write(tmp_path, SIG + _npth(113, 110, size))
    route, model, rules = _route(board)
    assert route is not None
    half_w = route.width_for("F.Cu") / 2
    # Track edge to hole edge keeps KiCad's default 0.25 mm hole clearance.
    assert _min_gap_to_hole(route, 13, 10, 0.3, half_w) >= rules.hole_clearance - 1e-3
    # The written route re-checks clean against every obstacle (incl. the hole).
    assert route_clears_floors(route, model, rules)


def test_hole_clearance_is_a_rule_knob(tmp_path):
    """A wider ``hole_clearance`` pushes the same route further from the drill."""
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    route, _model, rules = _route(board, hole_clearance=1.5)
    assert route is not None
    half_w = route.width_for("F.Cu") / 2
    assert _min_gap_to_hole(route, 13, 10, 0.3, half_w) >= 1.5 - 1e-3


def test_hole_also_blocks_via_site(tmp_path):
    """The drill stays a via blocker (hole-to-hole) next to the new track blocker."""
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    model = _build_model(PCB.load(str(board)))
    assert any(abs(x - 13) < 1e-6 and abs(y - 10) < 1e-6 for x, y, _d in model.drills)
