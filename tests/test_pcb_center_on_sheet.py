"""Tests for centering a PCB on its drawing sheet (kct pcb center-on-sheet).

Root cause covered: generated boards either sat at a fixed (100, 100) origin
on A4 (large boards overlap the title block) or on a tight (paper "User")
page (board hugs the frame corner).  The centering transform is a rigid,
grid-snapped translation of ALL geometry done with pure text editing and
exact decimal arithmetic, so routing / 45-degree copper / DRC are preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.pcb.center_sheet import (
    DEFAULT_FRAME_MARGIN_MM,
    DEFAULT_TITLE_BLOCK_MM,
    CenterReport,
    center_on_sheet,
    center_pcb_text,
    centered_origin,
    edge_cuts_bbox,
    select_paper,
    translate_pcb_text,
    usable_area,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def make_pcb(paper: str = '(paper "A4")', extra: str = "") -> str:
    """A minimal .kicad_pcb text with a 30x20 board at (100, 100)."""
    return f"""(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t{paper}
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(gr_rect
\t\t(start 100 100)
\t\t(end 130 120)
\t\t(layer "Edge.Cuts")
\t\t(width 0.1)
\t)
\t(footprint "Lib:FP"
\t\t(layer "F.Cu")
\t\t(at 105 110 90)
\t\t(pad "1" smd rect
\t\t\t(at -0.9125 0 90)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t)
\t)
\t(segment
\t\t(start 105 110)
\t\t(end 110.05 115.05)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net 1)
\t)
\t(via
\t\t(at 110.05 115.05)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net 1)
\t)
\t(zone
\t\t(net 1)
\t\t(net_name "GND")
\t\t(layer "F.Cu")
\t\t(polygon
\t\t\t(pts
\t\t\t\t(xy 100 100)
\t\t\t\t(xy 130 100)
\t\t\t\t(xy 130 120)
\t\t\t\t(xy 100 120)
\t\t\t)
\t\t)
\t\t(filled_polygon
\t\t\t(layer "F.Cu")
\t\t\t(pts
\t\t\t\t(xy 100.25 100.25)
\t\t\t\t(xy 129.75 100.25)
\t\t\t\t(xy 129.75 119.75)
\t\t\t)
\t\t)
\t)
{extra})
"""


A4_W, A4_H = 297.0, 210.0
USABLE_CX = (DEFAULT_FRAME_MARGIN_MM + (A4_W - DEFAULT_FRAME_MARGIN_MM)) / 2  # 148.5
USABLE_CY = (
    DEFAULT_FRAME_MARGIN_MM + (A4_H - DEFAULT_FRAME_MARGIN_MM - DEFAULT_TITLE_BLOCK_MM)
) / 2  # 87.5


# --------------------------------------------------------------------------
# Sheet geometry helpers
# --------------------------------------------------------------------------


class TestSheetGeometry:
    def test_usable_area_a4(self):
        assert usable_area(297, 210) == (10.0, 10.0, 287.0, 165.0)

    def test_select_paper_small_board(self):
        assert select_paper(30, 25) == "A4"

    def test_select_paper_respects_title_block(self):
        # 130 mm tall board: A4 usable height is 155, slack (155-130)/2 < 15
        assert select_paper(100, 130) == "A3"

    def test_select_paper_huge_board_returns_none(self):
        assert select_paper(2000, 2000) is None

    def test_centered_origin_a4(self):
        # 30x20 board on A4: usable center (148.5, 87.5)
        assert centered_origin(30, 20) == (133.5, 77.5)

    def test_centered_origin_snaps_to_grid(self):
        ox, oy = centered_origin(33.333, 20.777)
        assert round(ox / 0.05, 6) == round(ox / 0.05)
        assert round(oy / 0.05, 6) == round(oy / 0.05)

    def test_centered_origin_unknown_paper(self):
        with pytest.raises(ValueError, match="Unknown paper"):
            centered_origin(30, 20, "B5")


# --------------------------------------------------------------------------
# Bounding box discovery
# --------------------------------------------------------------------------


class TestEdgeCutsBbox:
    def test_rect_outline(self):
        assert edge_cuts_bbox(make_pcb()) == (100.0, 100.0, 130.0, 120.0)

    def test_line_outline(self):
        text = """(kicad_pcb
\t(paper "A4")
\t(gr_line (start 10 20) (end 50 20) (layer "Edge.Cuts"))
\t(gr_line (start 50 20) (end 50 60) (layer "Edge.Cuts"))
\t(gr_line (start 10 21) (end 50 21) (layer "F.SilkS"))
)
"""
        assert edge_cuts_bbox(text) == (10.0, 20.0, 50.0, 60.0)

    def test_no_outline(self):
        text = '(kicad_pcb\n\t(paper "A4")\n)\n'
        assert edge_cuts_bbox(text) is None


# --------------------------------------------------------------------------
# Rigid translation exactness
# --------------------------------------------------------------------------


class TestTranslationExactness:
    def test_centers_bbox_in_usable_area(self):
        new_text, report = center_pcb_text(make_pcb())
        bbox = edge_cuts_bbox(new_text)
        assert bbox is not None
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        assert cx == pytest.approx(USABLE_CX, abs=0.025)
        assert cy == pytest.approx(USABLE_CY, abs=0.025)

    def test_delta_is_grid_snapped(self):
        _, report = center_pcb_text(make_pcb())
        assert round(report.dx_mm / 0.05, 6) == round(report.dx_mm / 0.05)
        assert round(report.dy_mm / 0.05, 6) == round(report.dy_mm / 0.05)

    def test_coordinates_are_exact_decimal_strings(self):
        # 30x20 board at (100,100) on A4: dx=33.5, dy=-22.5 exactly.
        new_text, report = center_pcb_text(make_pcb())
        assert (report.dx_mm, report.dy_mm) == (33.5, -22.5)
        # segment endpoints shifted exactly, no float noise
        assert "(start 138.5 87.5)" in new_text  # 105+33.5, 110-22.5
        assert "(end 143.55 92.55)" in new_text  # 110.05+33.5, 115.05-22.5

    def test_45_degree_segment_stays_45(self):
        new_text, _ = center_pcb_text(make_pcb())
        # the fixture segment runs (105,110)->(110.05,115.05): exact 45 deg
        assert "(start 138.5 87.5)" in new_text
        assert "(end 143.55 92.55)" in new_text
        # dx == dy still holds exactly in decimal (no float round-trip)
        from decimal import Decimal

        run = Decimal("143.55") - Decimal("138.5")
        rise = Decimal("92.55") - Decimal("87.5")
        assert run == rise == Decimal("5.05")

    def test_footprint_moves_but_pad_relative_coords_do_not(self):
        new_text, _ = center_pcb_text(make_pcb())
        assert "(at 138.5 87.5 90)" in new_text  # footprint at, angle kept
        assert "(at -0.9125 0 90)" in new_text  # pad-relative at untouched

    def test_rotation_angle_preserved(self):
        new_text, _ = center_pcb_text(make_pcb())
        assert new_text.count(" 90)") >= 2  # both (at ... 90) survive

    def test_zone_polygon_and_filled_polygon_translate(self):
        new_text, _ = center_pcb_text(make_pcb())
        assert "(xy 133.5 77.5)" in new_text  # polygon vertex 100,100
        assert "(xy 133.75 77.75)" in new_text  # filled_polygon 100.25,100.25
        assert "(xy 163.25 97.25)" in new_text  # filled_polygon 129.75,119.75

    def test_only_coordinate_atoms_change(self):
        original = make_pcb()
        new_text, _ = center_pcb_text(original)
        # strip all digits: the non-numeric skeleton must be identical
        import re

        skel = re.compile(r"-?\d+(?:\.\d+)?")
        assert skel.sub("#", original) == skel.sub("#", new_text)

    def test_via_translates(self):
        new_text, _ = center_pcb_text(make_pcb())
        assert "(at 143.55 92.55)" in new_text

    def test_gr_text_translates_but_string_content_untouched(self):
        extra = '\t(gr_text "at 100 100"\n\t\t(at 102 118)\n\t\t(layer "F.SilkS")\n\t)\n'
        new_text, _ = center_pcb_text(make_pcb(extra=extra))
        assert '"at 100 100"' in new_text  # quoted string untouched
        assert "(at 135.5 95.5)" in new_text  # gr_text position moved


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


class TestIdempotency:
    def test_second_run_is_noop(self):
        first, r1 = center_pcb_text(make_pcb())
        second, r2 = center_pcb_text(first)
        assert r1.changed
        assert not r2.changed
        assert second == first
        assert (r2.dx_mm, r2.dy_mm) == (0.0, 0.0)

    def test_file_roundtrip_idempotent(self, tmp_path: Path):
        p = tmp_path / "board.kicad_pcb"
        p.write_text(make_pcb())
        r1 = center_on_sheet(p)
        assert r1.changed
        blob = p.read_bytes()
        r2 = center_on_sheet(p)
        assert not r2.changed
        assert p.read_bytes() == blob


# --------------------------------------------------------------------------
# Paper handling
# --------------------------------------------------------------------------


class TestPaperHandling:
    def test_user_paper_switches_to_a4(self):
        text = make_pcb(paper='(paper "User" 40 30)')
        new_text, report = center_pcb_text(text)
        assert report.paper_before == "User"
        assert report.paper_after == "A4"
        assert '(paper "A4")' in new_text
        assert "User" not in new_text.split("\n")[3]

    def test_user_paper_large_board_picks_a3(self):
        # 130 mm tall board on a User sheet -> needs A3 for 15 mm slack
        text = """(kicad_pcb
\t(paper "User" 110 140)
\t(gr_rect (start 5 5) (end 105 135) (layer "Edge.Cuts"))
)
"""
        new_text, report = center_pcb_text(text)
        assert report.paper_after == "A3"
        assert '(paper "A3")' in new_text

    def test_standard_paper_kept_when_board_fits(self):
        _, report = center_pcb_text(make_pcb())
        assert report.paper_before == "A4"
        assert report.paper_after == "A4"
        assert not report.paper_changed

    def test_keep_refuses_oversized_board(self):
        text = """(kicad_pcb
\t(paper "User" 40 30)
\t(gr_rect (start 5 5) (end 35 25) (layer "Edge.Cuts"))
)
"""
        with pytest.raises(ValueError, match="does not fit"):
            center_pcb_text(text, paper="keep")

    def test_explicit_paper_forced(self):
        new_text, report = center_pcb_text(make_pcb(), paper="A3")
        assert report.paper_after == "A3"
        assert '(paper "A3")' in new_text

    def test_unknown_paper_rejected(self):
        with pytest.raises(ValueError, match="Unknown paper"):
            center_pcb_text(make_pcb(), paper="B5")

    def test_missing_outline_raises(self):
        with pytest.raises(ValueError, match="Edge.Cuts"):
            center_pcb_text('(kicad_pcb\n\t(paper "A4")\n)\n')


# --------------------------------------------------------------------------
# Title-block avoidance
# --------------------------------------------------------------------------


class TestTitleBlockAvoidance:
    def test_board_lands_above_title_block(self):
        # tall board: 100x130 on A3 (usable y: 10..252-35=252? no: 297h)
        text = """(kicad_pcb
\t(paper "A4")
\t(gr_rect (start 100 100) (end 200 195) (layer "Edge.Cuts"))
)
"""
        new_text, report = center_pcb_text(text)
        # A4 usable bottom edge: 210 - 10 - 35 = 165
        assert report.bbox_after[3] <= 165.0
        assert report.bbox_after[1] >= 10.0

    def test_report_usable_area_matches_constants(self):
        _, report = center_pcb_text(make_pcb())
        assert report.usable_area == (10.0, 10.0, 287.0, 165.0)

    def test_custom_title_block_height(self):
        _, report = center_pcb_text(make_pcb(), title_block=0.0)
        # without a title block the usable center is the frame center
        cy = (report.bbox_after[1] + report.bbox_after[3]) / 2
        assert cy == pytest.approx(105.0, abs=0.025)


# --------------------------------------------------------------------------
# Report / dataclass behavior
# --------------------------------------------------------------------------


class TestReport:
    def test_changed_flags(self):
        _, report = center_pcb_text(make_pcb())
        assert isinstance(report, CenterReport)
        assert report.changed
        assert not report.paper_changed

    def test_dry_run_does_not_write(self, tmp_path: Path):
        p = tmp_path / "board.kicad_pcb"
        p.write_text(make_pcb())
        blob = p.read_bytes()
        report = center_on_sheet(p, dry_run=True)
        assert report.changed
        assert p.read_bytes() == blob

    def test_output_path_leaves_input_untouched(self, tmp_path: Path):
        src = tmp_path / "in.kicad_pcb"
        dst = tmp_path / "out.kicad_pcb"
        src.write_text(make_pcb())
        blob = src.read_bytes()
        center_on_sheet(src, output_path=dst)
        assert src.read_bytes() == blob
        assert dst.exists()
        assert edge_cuts_bbox(dst.read_text()) == (133.5, 77.5, 163.5, 97.5)


# --------------------------------------------------------------------------
# translate_pcb_text (explicit-delta rigid translation; board-07 routing
# frame sandwich, PR #4015)
# --------------------------------------------------------------------------


class TestTranslatePcbText:
    def test_translates_all_geometry_by_explicit_delta(self):
        moved = translate_pcb_text(make_pcb(), 6.5, -60.0)
        assert edge_cuts_bbox(moved) == (106.5, 40.0, 136.5, 60.0)
        assert "(at 111.5 50 90)" in moved  # footprint (at ...) moved
        assert "(at -0.9125 0 90)" in moved  # pad-relative coords untouched
        assert "(start 111.5 50)" in moved  # segment
        assert "(xy 106.75 40.25)" in moved  # filled_polygon vertex

    def test_zero_delta_is_identity(self):
        text = make_pcb()
        assert translate_pcb_text(text, 0.0, 0.0) == text

    def test_round_trip_is_byte_exact(self):
        # The board-07 recipe relies on this: centering then un-centering
        # (and vice versa) must restore every coordinate exactly.
        text = make_pcb()
        there = translate_pcb_text(text, -6.5, -60.0)
        back = translate_pcb_text(there, 6.5, 60.0)
        assert back == text

    def test_delta_snaps_to_grid(self):
        # 6.51 snaps to 6.5 on the default 0.05 mm grid.
        assert translate_pcb_text(make_pcb(), 6.51, 0.0) == translate_pcb_text(make_pcb(), 6.5, 0.0)

    def test_paper_node_is_never_touched(self):
        moved = translate_pcb_text(make_pcb(), 10.0, 10.0)
        assert '(paper "A4")' in moved

    def test_45_degree_copper_stays_exact(self):
        moved = translate_pcb_text(make_pcb(), 3.35, -7.15)
        # fixture segment is exactly 45 deg: (105,110)->(110.05,115.05)
        assert "(start 108.35 102.85)" in moved
        assert "(end 113.4 107.9)" in moved


# --------------------------------------------------------------------------
# Reinforcement sidecar follows the board (Issue #10250)
# --------------------------------------------------------------------------

import json  # noqa: E402
import re  # noqa: E402

from kicad_tools.pcb.reinforcement_sidecar import (  # noqa: E402
    load_wires,
    render_sidecar,
    sidecar_path,
    translate_sidecar_wires,
)


def _pt(x: float, y: float) -> dict[str, float]:
    return {"x": x, "y": y}


def _wires() -> list[dict]:
    """One plain wire anchored on the fixture via, one parallel-copy record."""
    return [
        {
            "net": "GND",
            "layer": "F.Cu",
            "kind": "run",
            "paths": [],
            "leg_start": _pt(105.0, 110.0),
            "leg_end": _pt(110.05, 115.05),
            "anchors": [_pt(105.0, 110.0), _pt(110.05, 115.05)],
            "length_mm": 7.1418,
            "straight_line_mm": 7.1418,
            "wire_gauge_awg": 30,
            "bare_diameter_mm": 0.255,
            "mask_opening": None,
            "status": "ok",
            "reason": "",
        },
        {
            "net": "GND",
            "layer": "F.Cu",
            "kind": "run",
            "strategy": "parallel",
            "copy_layer": "B.Cu",
            "leg_start": _pt(101.0, 101.0),
            "leg_end": _pt(102.0, 101.0),
            "anchors": [],
            "polyline": [_pt(101.0, 101.0), _pt(102.0, 101.0)],
            "copy_segments": [
                {"start": _pt(101.0, 102.0), "end": _pt(102.0, 102.0), "width_mm": 0.3}
            ],
            "stitch_vias": [_pt(101.5, 102.0)],
            "stitch_via_size_mm": 0.6,
            "length_mm": 1.0,
        },
    ]


def _write_reinforced(tmp_path: Path, name: str = "board.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text(make_pcb())
    sidecar_path(pcb).write_text(render_sidecar(_wires()))
    return pcb


def _via_xy(pcb_text: str) -> tuple[float, float]:
    m = re.search(r"\(via\s*\(at ([-\d.]+) ([-\d.]+)\)", pcb_text)
    assert m is not None
    return float(m.group(1)), float(m.group(2))


class TestReinforcementSidecar:
    def test_anchor_matches_moved_via(self, tmp_path: Path):
        pcb = _write_reinforced(tmp_path)
        report = center_on_sheet(pcb)
        assert (report.dx_mm, report.dy_mm) == (33.5, -22.5)
        assert report.sidecar == sidecar_path(pcb)
        via = _via_xy(pcb.read_text())
        assert via == (143.55, 92.55)
        wires = load_wires(sidecar_path(pcb))
        plain = next(w for w in wires if "strategy" not in w)
        assert (plain["anchors"][1]["x"], plain["anchors"][1]["y"]) == via

    def test_non_coordinate_fields_unchanged_and_format_kept(self, tmp_path: Path):
        pcb = _write_reinforced(tmp_path)
        before = {w["leg_start"]["x"]: w for w in load_wires(sidecar_path(pcb))}
        center_on_sheet(pcb)
        raw = sidecar_path(pcb).read_text()
        assert raw == json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"
        after = load_wires(sidecar_path(pcb))
        assert len(after) == len(before)
        coord_keys = {
            "leg_start", "leg_end", "anchors", "polyline", "copy_segments", "stitch_vias",
        }  # fmt: skip
        for w in after:
            orig = before[round(w["leg_start"]["x"] - 33.5, 4)]
            for key, value in orig.items():
                if key not in coord_keys:
                    assert w[key] == value, key

    def test_parallel_record_coordinates_translate(self, tmp_path: Path):
        moved = translate_sidecar_wires(_wires(), 33.5, -22.5)
        par = next(w for w in moved if w.get("strategy") == "parallel")
        assert par["polyline"][0] == _pt(134.5, 78.5)
        assert par["copy_segments"][0]["start"] == _pt(134.5, 79.5)
        assert par["copy_segments"][0]["end"] == _pt(135.5, 79.5)
        assert par["copy_segments"][0]["width_mm"] == 0.3
        assert par["stitch_vias"] == [_pt(135.0, 79.5)]

    def test_translate_is_pure_and_handles_empty(self):
        wires = _wires()
        snapshot = json.dumps(wires, sort_keys=True)
        translate_sidecar_wires(wires, 1.0, 2.0)
        assert json.dumps(wires, sort_keys=True) == snapshot
        assert translate_sidecar_wires([], 1.0, 2.0) == []

    def test_output_path_pair(self, tmp_path: Path):
        pcb = _write_reinforced(tmp_path)
        out = tmp_path / "out.kicad_pcb"
        pcb_blob, side_blob = pcb.read_bytes(), sidecar_path(pcb).read_bytes()
        report = center_on_sheet(pcb, output_path=out)
        assert pcb.read_bytes() == pcb_blob
        assert sidecar_path(pcb).read_bytes() == side_blob
        assert report.sidecar == sidecar_path(out)
        plain = next(w for w in load_wires(sidecar_path(out)) if "strategy" not in w)
        assert (plain["anchors"][1]["x"], plain["anchors"][1]["y"]) == _via_xy(out.read_text())

    def test_export_between_the_two_source_replacements(self, tmp_path: Path, monkeypatch):
        import os

        pcb = _write_reinforced(tmp_path)
        record = sidecar_path(pcb)
        out = _write_reinforced(tmp_path, "out.kicad_pcb")
        original = (out.read_bytes(), sidecar_path(out).read_bytes())
        replace = os.replace
        attempts = []

        def export_after_record(src, dst):
            replace(src, dst)
            if Path(dst) == record:
                # The live record has moved, while the live PCB still has the old vias.
                with pytest.raises(ValueError, match="publishing the input"):
                    center_on_sheet(pcb, output_path=out)
                attempts.append((out.read_bytes(), sidecar_path(out).read_bytes()))

        monkeypatch.setattr(os, "replace", export_after_record)
        center_on_sheet(pcb)
        assert attempts == [original]
        center_on_sheet(pcb, output_path=out)
        assert out.read_bytes() == pcb.read_bytes()
        assert sidecar_path(out).read_bytes() == record.read_bytes()
        plain = next(w for w in load_wires(record) if "strategy" not in w)
        assert plain["anchors"][1] == _pt(*_via_xy(out.read_text()))

    def test_record_replaced_during_copy_preserves_destination(self, tmp_path, monkeypatch):
        from kicad_tools.pcb import reinforcement_sidecar

        pcb = _write_reinforced(tmp_path)
        out = _write_reinforced(tmp_path, "out.kicad_pcb")
        original = (out.read_bytes(), sidecar_path(out).read_bytes())
        record = sidecar_path(pcb)
        replacement = tmp_path / "replacement.json"
        replacement.write_text(render_sidecar([]))
        translate = reinforcement_sidecar.translate_sidecar_wires

        def edit_after_read(wires, dx, dy):
            replacement.replace(record)
            return translate(wires, dx, dy)

        monkeypatch.setattr(reinforcement_sidecar, "translate_sidecar_wires", edit_after_read)
        with pytest.raises(ValueError, match="record changed"):
            center_on_sheet(pcb, output_path=out)
        assert load_wires(record) == []
        assert (out.read_bytes(), sidecar_path(out).read_bytes()) == original

    def test_export_from_read_only_input_directory(self, tmp_path):
        source_dir = tmp_path / "inputs"
        source_dir.mkdir()
        pcb = _write_reinforced(source_dir)
        original = (pcb.read_bytes(), sidecar_path(pcb).read_bytes())
        source_dir.chmod(0o555)
        try:
            out = tmp_path / "out.kicad_pcb"
            center_on_sheet(pcb, output_path=out)
        finally:
            source_dir.chmod(0o755)
        assert (pcb.read_bytes(), sidecar_path(pcb).read_bytes()) == original
        plain = next(w for w in load_wires(sidecar_path(out)) if "strategy" not in w)
        assert plain["anchors"][1] == _pt(*_via_xy(out.read_text()))

    def test_dry_run_reports_but_writes_nothing(self, tmp_path: Path):
        pcb = _write_reinforced(tmp_path)
        pcb_blob, side_blob = pcb.read_bytes(), sidecar_path(pcb).read_bytes()
        report = center_on_sheet(pcb, dry_run=True)
        assert report.sidecar == sidecar_path(pcb)
        assert pcb.read_bytes() == pcb_blob
        assert sidecar_path(pcb).read_bytes() == side_blob
        assert not list(tmp_path.glob("*.tmp-*"))

    @pytest.mark.parametrize("bad", ["{not json", '{"version": 99, "wires": []}'])
    def test_bad_sidecar_fails_before_writing(self, tmp_path: Path, bad: str):
        pcb = _write_reinforced(tmp_path)
        sidecar_path(pcb).write_text(bad)
        pcb_blob = pcb.read_bytes()
        for dry in (False, True):
            with pytest.raises(ValueError):
                center_on_sheet(pcb, dry_run=dry)
        assert pcb.read_bytes() == pcb_blob
        assert sidecar_path(pcb).read_text() == bad

    def test_pcb_write_failure_restores_sidecar(self, tmp_path: Path, monkeypatch):
        import os

        pcb = _write_reinforced(tmp_path)
        pcb_blob, side_blob = pcb.read_bytes(), sidecar_path(pcb).read_bytes()
        real_replace = os.replace

        def flaky(src, dst):
            if Path(dst) == pcb:
                raise OSError("disk full")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", flaky)
        with pytest.raises(OSError):
            center_on_sheet(pcb)
        assert pcb.read_bytes() == pcb_blob
        assert sidecar_path(pcb).read_bytes() == side_blob
        assert not list(tmp_path.glob("*.tmp-*"))

    def test_already_centered_leaves_sidecar_alone(self, tmp_path: Path):
        pcb = _write_reinforced(tmp_path)
        center_on_sheet(pcb)
        side_blob = sidecar_path(pcb).read_bytes()
        mtime = sidecar_path(pcb).stat().st_mtime_ns
        report = center_on_sheet(pcb)
        assert not report.changed
        assert report.sidecar is None
        assert sidecar_path(pcb).read_bytes() == side_blob
        assert sidecar_path(pcb).stat().st_mtime_ns == mtime

    def test_no_sidecar_creates_none(self, tmp_path: Path):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(make_pcb())
        report = center_on_sheet(pcb)
        assert report.changed
        assert report.sidecar is None
        assert not sidecar_path(pcb).exists()

    def test_plain_input_cannot_inherit_existing_output_wires(self, tmp_path: Path):
        pcb = tmp_path / "plain.kicad_pcb"
        pcb.write_text(make_pcb())
        out = _write_reinforced(tmp_path, "out.kicad_pcb")
        before = (out.read_bytes(), sidecar_path(out).read_bytes())
        with pytest.raises(ValueError, match="reinforcement"):
            center_on_sheet(pcb, output_path=out)
        assert (out.read_bytes(), sidecar_path(out).read_bytes()) == before

    @pytest.mark.parametrize("alias", ["input-record", "output-record", "hardlink"])
    def test_output_alias_keeps_input_pair_intact(self, tmp_path: Path, alias: str):
        pcb = _write_reinforced(tmp_path)
        out = tmp_path / "out.kicad_pcb"
        if alias == "input-record":
            out = sidecar_path(pcb)
        elif alias == "output-record":
            sidecar_path(out).symlink_to(pcb)
        else:
            out.hardlink_to(sidecar_path(pcb))
        before = (pcb.read_bytes(), sidecar_path(pcb).read_bytes())
        with pytest.raises(ValueError, match="overlap"):
            center_on_sheet(pcb, output_path=out)
        assert (pcb.read_bytes(), sidecar_path(pcb).read_bytes()) == before

    @pytest.mark.parametrize("fail_second_write", [False, True])
    def test_in_place_links_and_private_modes_survive(
        self, tmp_path: Path, monkeypatch, fail_second_write: bool
    ):
        import os
        from stat import S_IMODE

        pcb = _write_reinforced(tmp_path)
        side = sidecar_path(pcb)
        alias = tmp_path / "alias.kicad_pcb"
        alias.symlink_to(pcb)
        alias_record = tmp_path / "alias.kicad_pcb.reinforcement.json"
        alias_record.symlink_to(side)
        pcb.chmod(0o600)
        side.chmod(0o640)
        before = (pcb.read_bytes(), side.read_bytes())
        if fail_second_write:
            replace = os.replace

            def fail_board(src, dst):
                if Path(dst).resolve() == pcb:
                    raise OSError("forced PCB write failure")
                return replace(src, dst)

            monkeypatch.setattr(os, "replace", fail_board)
            with pytest.raises(OSError, match="forced PCB"):
                center_on_sheet(alias)
            assert (pcb.read_bytes(), side.read_bytes()) == before
        else:
            center_on_sheet(alias)
            assert pcb.read_bytes() != before[0]
            assert side.read_bytes() != before[1]
        assert alias.is_symlink() and alias_record.is_symlink()
        assert (S_IMODE(pcb.stat().st_mode), S_IMODE(side.stat().st_mode)) == (0o600, 0o640)

    def test_paper_only_change_validates_existing_sidecar(self, tmp_path: Path):
        pcb = _write_reinforced(tmp_path)
        center_on_sheet(pcb, paper="A3")
        pcb.write_text(pcb.read_text().replace('(paper "A3")', '(paper "A4")'))
        sidecar_path(pcb).write_text('{"version": 99, "wires": []}')
        before = pcb.read_bytes()
        with pytest.raises(ValueError):
            center_on_sheet(pcb, paper="A3")
        assert pcb.read_bytes() == before

    def test_centering_board_link_uses_its_canonical_record(self, tmp_path: Path):
        pcb = _write_reinforced(tmp_path)
        alias = tmp_path / "alias.kicad_pcb"
        alias.symlink_to(pcb.name)
        before = sidecar_path(pcb).read_bytes()
        report = center_on_sheet(alias)
        assert alias.is_symlink() and report.sidecar == sidecar_path(pcb)
        assert sidecar_path(pcb).read_bytes() != before
        assert not (tmp_path / "alias.kicad_pcb.reinforcement.json").exists()

    def test_distinct_hard_link_record_names_cannot_diverge(self, tmp_path):
        pcb = _write_reinforced(tmp_path)
        alias = tmp_path / "alias.kicad_pcb"
        alias.symlink_to(pcb.name)
        record = sidecar_path(pcb)
        alias_record = tmp_path / "alias.kicad_pcb.reinforcement.json"
        alias_record.hardlink_to(record)
        original = (pcb.read_bytes(), record.read_bytes())
        with pytest.raises(ValueError, match="two different reinforcement records"):
            center_on_sheet(alias)
        assert (alias.read_bytes(), alias_record.read_bytes()) == original
        assert alias_record.samefile(record)

    @pytest.mark.parametrize("fail_at", ["canonical record", "alias record", "board"])
    def test_legacy_output_publish_failure_restores_files(self, tmp_path, monkeypatch, fail_at):
        import os
        from stat import S_IMODE

        source = _write_reinforced(tmp_path)
        board = _write_reinforced(tmp_path, "working.kicad_pcb")
        alias = tmp_path / "review.kicad_pcb"
        alias.symlink_to(board.name)
        canonical = sidecar_path(board)
        legacy = tmp_path / "review.kicad_pcb.reinforcement.json"
        canonical.rename(legacy)
        legacy.write_text(render_sidecar([]))
        legacy.chmod(0o600)
        before = (source.read_bytes(), sidecar_path(source).read_bytes(), board.read_bytes())
        old_record = legacy.read_bytes()
        target = {"canonical record": canonical, "alias record": legacy, "board": board}[fail_at]
        replace = os.replace

        def fail_write(src, dst):
            if dst == target:
                raise OSError("forced publication failure")
            return replace(src, dst)

        monkeypatch.setattr(os, "replace", fail_write)
        with pytest.raises(OSError, match="forced publication failure"):
            center_on_sheet(source, output_path=alias)
        assert (
            source.read_bytes(),
            sidecar_path(source).read_bytes(),
            board.read_bytes(),
        ) == before
        assert alias.is_symlink() and not legacy.is_symlink()
        assert not os.path.lexists(canonical)
        assert legacy.read_bytes() == old_record and S_IMODE(legacy.stat().st_mode) == 0o600
        assert not list(tmp_path.glob(".center-*"))

    @pytest.mark.parametrize("linked_record", [False, True])
    def test_legacy_input_is_bound_only_when_published_in_place(self, tmp_path, linked_record):
        pcb = _write_reinforced(tmp_path)
        center_on_sheet(pcb)
        original = pcb.stat().st_mtime_ns
        canonical = sidecar_path(pcb)
        record_bytes = canonical.read_bytes()
        alias = tmp_path / "review.kicad_pcb"
        alias.symlink_to(pcb.name)
        legacy = tmp_path / "review.kicad_pcb.reinforcement.json"
        storage = tmp_path / "shared-wires.json" if linked_record else legacy
        canonical.rename(storage)
        if linked_record:
            legacy.symlink_to(storage.name)

        copy = tmp_path / "copy.kicad_pcb"
        center_on_sheet(alias, output_path=copy)
        assert sidecar_path(copy).read_bytes() == record_bytes
        assert center_on_sheet(alias, dry_run=True).sidecar == canonical
        assert not canonical.exists() and legacy.is_symlink() == linked_record
        assert storage.read_bytes() == record_bytes and pcb.stat().st_mtime_ns == original

        report = center_on_sheet(alias)
        assert not report.changed and report.output is None and report.sidecar == canonical
        assert pcb.stat().st_mtime_ns == original
        assert canonical.samefile(legacy) and canonical.read_bytes() == record_bytes
        assert canonical.is_symlink() == linked_record
        assert legacy.is_symlink()
        center_on_sheet(pcb, paper="A3")
        assert legacy.read_bytes() == canonical.read_bytes() != record_bytes
        if linked_record:
            assert canonical.samefile(storage)

    @pytest.mark.parametrize("existing_record", [False, True])
    def test_list_wires_through_requested_output_alias(self, tmp_path, capsys, existing_record):
        from kicad_tools.cli import main

        pcb = _write_reinforced(tmp_path)
        target = tmp_path / "canonical.kicad_pcb"
        target.write_text(make_pcb())
        if existing_record:
            sidecar_path(target).write_text(render_sidecar([]))
        alias = tmp_path / "review.kicad_pcb"
        alias.symlink_to(target.name)
        assert main(["pcb", "center-on-sheet", str(pcb), "-o", str(alias), "--format", "json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["output"] == str(alias)
        assert report["sidecar"] == str(sidecar_path(target))
        for path in (report["output"], str(target)):
            assert main(["pcb", "reinforce", path, "--list-wires", "--format", "json"]) == 0
            inventory = json.loads(capsys.readouterr().out)
            assert inventory["wires"] == load_wires(sidecar_path(target))
            plain = next(w for w in inventory["wires"] if "strategy" not in w)
            assert plain["anchors"][1] == _pt(*_via_xy(target.read_text()))

    def test_already_centered_copy_writes_both_files(self, tmp_path: Path):
        pcb = _write_reinforced(tmp_path)
        center_on_sheet(pcb)
        out = tmp_path / "copy.kicad_pcb"
        report = center_on_sheet(pcb, output_path=out)
        assert not report.changed and report.output == out and report.sidecar == sidecar_path(out)
        assert pcb.read_bytes() == out.read_bytes()
        assert sidecar_path(pcb).read_bytes() == sidecar_path(out).read_bytes()

    def test_cli_json_lists_sidecar(self, tmp_path: Path, capsys):
        from kicad_tools.cli.pcb_center_on_sheet import run_center_on_sheet

        pcb = _write_reinforced(tmp_path)
        assert run_center_on_sheet(pcb, dry_run=True, output_format="json") == 0
        assert json.loads(capsys.readouterr().out)["sidecar"] == str(sidecar_path(pcb))
        plain = tmp_path / "plain.kicad_pcb"
        plain.write_text(make_pcb())
        assert run_center_on_sheet(plain, dry_run=True, output_format="json") == 0
        assert json.loads(capsys.readouterr().out)["sidecar"] is None


def test_new_export_honors_private_umask(tmp_path):
    import os

    from kicad_tools.cli import main

    src = _write_reinforced(tmp_path, "source.kicad_pcb")
    src.chmod(0o644)
    sidecar_path(src).chmod(0o644)
    out = tmp_path / "out.kicad_pcb"
    old = os.umask(0o077)
    try:
        assert main(["pcb", "center-on-sheet", str(src), "--paper", "keep", "-o", str(out)]) == 0
    finally:
        os.umask(old)
    assert out.stat().st_mode & 0o777 == 0o600
    assert sidecar_path(out).stat().st_mode & 0o777 == 0o600


def test_in_place_alias_migration_keeps_private_record_mode(tmp_path):
    import os

    from kicad_tools.cli import main

    board = tmp_path / "board.kicad_pcb"
    board.write_text(make_pcb())
    alias = tmp_path / "review.kicad_pcb"
    alias.symlink_to(board)
    legacy = alias.with_name(alias.name + ".reinforcement.json")
    legacy.write_text(render_sidecar(_wires()))
    legacy.chmod(0o600)
    old = os.umask(0o022)
    try:
        assert main(["pcb", "center-on-sheet", str(alias), "--paper", "keep"]) == 0
    finally:
        os.umask(old)
    assert sidecar_path(board).stat().st_mode & 0o777 == 0o600
    assert alias.with_name(alias.name + ".reinforcement.json").stat().st_mode & 0o777 == 0o600


def test_distinct_output_alias_migration_keeps_private_destination_record_mode(tmp_path):
    import os

    from kicad_tools.cli import main
    from kicad_tools.cli.commands.pcb import run_pcb_command
    from kicad_tools.pcb.reinforcement_sidecar import sidecar_path
    from tests.test_pcb_reinforce_sidecar import _args, _two_net_pcb

    src = tmp_path / "src.kicad_pcb"
    other = tmp_path / "other.kicad_pcb"
    for path in (src, other):
        _two_net_pcb().save(path)
        assert run_pcb_command(_args(path, "NET_A")) == 0
    out = tmp_path / "review.kicad_pcb"
    out.symlink_to(other)
    legacy = out.with_name(out.name + ".reinforcement.json")
    canonical = sidecar_path(other)
    legacy.write_bytes(canonical.read_bytes())
    canonical.unlink()  # only the alias-side legacy record remains
    legacy.chmod(0o600)
    old = os.umask(0o022)
    try:
        assert main(["pcb", "center-on-sheet", str(src), "--paper", "A3", "-o", str(out)]) == 0
    finally:
        os.umask(old)
    assert sidecar_path(out).stat().st_mode & 0o777 == 0o600
