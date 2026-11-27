"""Pour-slot detection ignores strips over milled board openings (Issue #10121)."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Point

from kicad_tools.optim.pour_slot_cleanup import cleanup_pour_slots
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.pour_slot import iter_pour_slot_pairs
from tests.test_pour_slot_cleanup import _board

_CUTOUT = (
    "  (gr_rect (start {x0} {y0}) (end {x1} {y1}) "
    '(stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") '
    '(uuid "00000000-0000-0000-0000-0000000000c1"))\n'
)


def _with_cutout(pcb: PCB, rect: tuple[float, float, float, float], tmp_path) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    pcb.save(path)
    text = path.read_text()
    x0, y0, x1, y1 = rect
    idx = text.rstrip().rfind(")")
    path.write_text(text[:idx] + _CUTOUT.format(x0=x0, y0=y0, x1=x1, y1=y1) + text[idx:])
    reloaded = PCB.load(path)
    # Zones are not serialized by the fixture; reuse the test zone.
    reloaded.zones.extend(pcb.zones)
    return reloaded


def test_solid_board_still_warns():
    assert len(list(iter_pour_slot_pairs(_board(0.7)))) == 1


def test_strip_over_internal_cutout_is_not_a_slot(tmp_path):
    # Tracks at y=10 and y=10.9; the opening spans the whole gap and run.
    pcb = _with_cutout(_board(0.7), (5, 10.0, 25, 10.9), tmp_path)
    assert list(iter_pour_slot_pairs(pcb)) == []


def test_optimizer_does_not_move_tracks_over_internal_cutout(tmp_path):
    pcb = _with_cutout(_board(0.7), (5, 10.0, 25, 10.9), tmp_path)
    before = sorted((s.start, s.end) for s in pcb.segments)
    res = cleanup_pour_slots(pcb, referee=lambda _p: {"clearance": 0, "unconnected": 0})
    assert not res.changed
    assert sorted((s.start, s.end) for s in pcb.segments) == before


def test_material_keeps_nested_curved_island_and_disconnected_exterior():
    from kicad_tools.validate.rules.pour_slot import _board_material

    pcb = PCB.create(width=50, height=40, center=False)
    for contour in (
        "(gr_rect (start 22 1) (end 49 39)",
        "(gr_circle (center 35 10) (end 37 10)",
        "(gr_rect (start 60 0) (end 70 10)",
    ):
        pcb._sexp.append(parse_string(f'{contour} (fill none) (layer "Edge.Cuts"))'))
    material = _board_material(pcb)
    assert material is not None and material.is_valid
    assert material.area == pytest.approx(974 + 4 * math.pi + 100, abs=0.001)
    assert all(material.contains(Point(p)) for p in ((15, 16.275), (35, 10), (65, 5)))
    assert not material.contains(Point(35, 20))


@pytest.mark.parametrize("remove_all", [False, True], ids=["open", "missing"])
def test_uncertifiable_outline_keeps_the_advisory_and_leaves_copper_alone(remove_all):
    pcb = _board(0.7)
    edges = pcb._sexp.find_children("gr_line")
    assert len(edges) == 4
    for edge in edges if remove_all else edges[:1]:
        pcb._sexp.remove(edge)
    assert len(list(iter_pour_slot_pairs(pcb))) == 1
    before = [(s.start, s.end) for s in pcb.segments]
    result = cleanup_pour_slots(pcb, referee=lambda _pcb: pytest.fail("uncertified outline"))
    assert result.left_alone == 1 and "Edge.Cuts" in result.left_alone_reasons[0]
    assert [(s.start, s.end) for s in pcb.segments] == before
