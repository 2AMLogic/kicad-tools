"""The reusable write pass on Board 03's stock connector artwork (Issue #9800)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.core.sexp_file import save_pcb
from kicad_tools.drc.repair_silkscreen import legalize_silk
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_file, parse_string, serialize_sexp
from kicad_tools.validate.rules.silkscreen import check_all_silkscreen

BOARD = Path(__file__).resolve().parents[1] / "boards/03-usb-joystick"
LAYERS = {"F.SilkS", "F.Silkscreen"}


def _silk_lines(footprint):
    return [
        n for n in footprint.find_children("fp_line") if n.find("layer").get_string(0) in LAYERS
    ]


def _board(tmp_path):
    # Exercise the published route without depending on system footprint libraries.
    # Put the vendored stock J3 ink and the two stock 6.0 mm J1 line ends back.
    doc = parse_file(BOARD / "output/usb_joystick_routed.kicad_pcb")
    footprints = {
        next(
            p.get_string(1) for p in fp.find_children("property") if p.get_string(0) == "Reference"
        ): fp
        for fp in doc.find_children("footprint")
    }
    j3 = footprints["J3"]
    for node in _silk_lines(j3):
        j3.children.remove(node)
    library = parse_file(BOARD / "footprints/Joystick.pretty/Samtec_TSM-103-01-T-DV.kicad_mod")
    for node in _silk_lines(library):
        j3.add(node)
    j1_ends = [
        n.find("start")
        for n in _silk_lines(footprints["J1"])
        if n.find("end").get_float(1) == 8.73 and 6.0 < n.find("start").get_float(1) < 8.73
    ]
    assert len(j1_ends) == 2
    for start in j1_ends:
        start.set_atom(1, 6.0)
    path = tmp_path / "board.kicad_pcb"
    save_pcb(doc, path)
    return path


def _electrical_geometry(path):
    doc = parse_file(path)
    copper = [n for n in doc.children if n.name in {"segment", "arc", "via", "zone", "net"}]
    for fp in doc.find_children("footprint"):
        copper.extend(
            n for n in fp.children if n.name in {"at", "layer", "pad", "net_tie_pad_groups"}
        )
    return [serialize_sexp(n) for n in copper]


def _line_ids(fp):
    return sorted(g.uuid for g in fp.graphics if g.layer in LAYERS and g.graphic_type == "line")


def test_shared_pass_preserves_printed_mark_and_route_in_one_save(tmp_path):
    path = _board(tmp_path)
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=4)
    pcb = PCB.load(path)
    before_rules = {v.rule_id for v in check_all_silkscreen(pcb, rules).violations}
    assert {"silk_pad_clearance", "silk_edge_clearance"} <= before_rules
    original = {
        fp.reference: _line_ids(fp) for fp in pcb.footprints if fp.reference in {"J1", "J3"}
    }
    copper = _electrical_geometry(path)

    report = legalize_silk(path, rules)
    after = PCB.load(path)
    assert Counter((e.footprint_ref, e.action) for e in report.footprint_line_edits) == {
        ("J1", "trimmed_edge"): 2,
        ("J3", "moved_marker"): 4,
    }
    marker = [e for e in report.footprint_line_edits if e.action == "moved_marker"]
    deltas = {
        (round(x - a, 6), round(y - b, 6))
        for e in marker
        for (a, b), (x, y) in [(e.old_start, e.new_start), (e.old_end, e.new_end)]
    }
    assert len(deltas) == 1  # Rigid movement preserves every length, bend and joint.
    assert deltas != {(0.0, 0.0)}
    for fp in after.footprints:
        if fp.reference in original:
            assert _line_ids(fp) == original[fp.reference]
    assert len(original["J1"]) == 6
    assert _electrical_geometry(path) == copper
    assert not report.pads.skipped
    assert not {"silk_pad_clearance", "silk_edge_clearance"} & {
        v.rule_id for v in check_all_silkscreen(after, rules).violations
    }
    saved = path.read_bytes()
    assert legalize_silk(path, rules).total_fixed == 0
    assert path.read_bytes() == saved


@pytest.mark.parametrize(("layer", "can_move"), [("F.SilkS", False), ("B.SilkS", True)])
def test_joined_marker_respects_other_ink_on_its_own_side(tmp_path, layer, can_move):
    path = _board(tmp_path)
    doc = parse_file(path)
    # The marker's top bar can move away from the pads only toward this line.
    fp = next(f for f in PCB.load(path).footprints if f.reference == "J3")
    ox, oy = PCB.load(path).board_origin
    x, y = fp.position[0] + ox, fp.position[1] + oy - 4.10
    doc.add(
        parse_string(
            f"(gr_line (start {x - 1} {y}) (end {x + 1} {y}) "
            f'(stroke (width 0.15) (type solid)) (layer "{layer}") (uuid "other-ink"))'
        )
    )
    save_pcb(doc, path)
    report = legalize_silk(path, get_profile("jlcpcb-tier1").get_design_rules(layers=4))
    assert sum(e.action == "moved_marker" for e in report.footprint_line_edits) == (
        4 if can_move else 0
    )
    assert bool([s for s in report.pads.skipped if s.footprint_ref == "J3"]) == (not can_move)
