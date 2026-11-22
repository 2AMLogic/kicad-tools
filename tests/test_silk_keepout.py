"""Silk keepouts and the silk error grading of #9798 (#9795 phase 2).

Two groups:

* **``silk_in_keepout``** -- rule areas whose sibling ``.kicad_dru`` forbids
  ``text`` / ``graphic`` there, per the curated fixture from issue #9798: a
  front line crossing the area boundary and front text inside it are errors,
  the back line at the same coordinates is not, and removing the sibling rule
  (or scoping it to copper) leaves the silk legal.  The same resolved areas
  drive the clip in
  :meth:`~kicad_tools.drc.repair_silkscreen.SilkscreenRepairer.repair_keepout_clearance`.
* **Grading** -- the operator ruling of 2026-11-20 on #9795: under the
  jlcpcb profiles silk on exposed copper, silk hidden under a component
  body and below-minimum silk widths are errors, while silk over a *via*
  is not (info at most), whichever route reports it.  The
  ``silk_hole_overlap`` severity split (PTH/NPTH vs via) is open PR #9112's
  (issue #9033); the tests here verify both reporting routes so a via-only
  board cannot fail through a second rule, and that PTH/NPTH holes stay
  reportable, without pinning that severity.

Every board sits at a non-zero origin (Edge.Cuts at 100,100) so a frame
mix-up between the raw-tree clip and the board-relative check shows up.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli.runner import find_kicad_cli  # noqa: E402
from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer  # noqa: E402
from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.sexp.parser import parse_file  # noqa: E402
from kicad_tools.validate.checker import DRCChecker  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_all_silkscreen,
    check_silk_hole_overlap,
    check_silk_in_keepout,
    check_silk_over_copper,
    check_silk_ref_obscured,
    check_silk_under_body,
    check_silkscreen_line_width,
    resolve_silk_keepouts,
)

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 100 100) (end 160 160) (stroke (width 0.1) (type solid)) (fill no)
    (layer "Edge.Cuts"))
"""

# The curated fixture from issue #9798: an all-allowed rule area on F.SilkS
# named ``no_ink`` whose printed ink is forbidden by the sibling rule below.
_SILK_ZONE = """  (zone (net 0) (net_name "") (layer "F.SilkS") (name "no_ink") (hatch edge 0.5)
    (priority 0) (uuid "aaaaaaaa-0000-0000-0000-000000000001")
    (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
    (polygon (pts (xy 120 120) (xy 140 120) (xy 140 140) (xy 120 140))))
"""
_AREA_LABEL = "no_ink [aaaaaaaa-0000-0000-0000-000000000001]"
_DRU = """(version 1)
(rule "printed legend keepout"
  (layer "F.SilkS")
  (condition "A.intersectsArea('no_ink')")
  (constraint disallow text graphic))
"""

_FRONT_LINE = (
    "  (gr_line (start 110 130) (end 150 130) (stroke (width 0.15) (type solid)) "
    '(layer "F.SilkS") (uuid "front-line"))\n'
)
_BACK_LINE = (
    "  (gr_line (start 110 130) (end 150 130) (stroke (width 0.15) (type solid)) "
    '(layer "B.SilkS") (uuid "back-line"))\n'
)
_FRONT_TEXT = (
    '  (gr_text "LEGEND" (at 130 130) (layer "F.SilkS") '
    "(effects (font (size 1 1) (thickness 0.15))))\n"
)


def _board(tmp_path: Path, body: str, dru: str | None = _DRU) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    if dru is not None:
        (tmp_path / "board.kicad_dru").write_text(dru)
    return path


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"], ids=["jlcpcb", "jlcpcb-tier1"])
def _jlc_rules(profile):
    return get_profile(profile).get_design_rules(layers=2, copper_oz=1)


def _jlcpcb():
    return get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1)


def _other():
    # A non-JLC grading control: bare defaults keep every knob off.
    return get_profile("pcbway").get_design_rules(layers=2, copper_oz=1)


# ---------------------------------------------------------------------------
# silk_in_keepout: detection
# ---------------------------------------------------------------------------


def test_front_silk_flagged_back_side_clean(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _BACK_LINE + _FRONT_TEXT)
    pcb = PCB.load(board)

    areas = resolve_silk_keepouts(pcb)
    assert len(areas) == 1
    assert areas[0].label == _AREA_LABEL
    assert areas[0].sides == frozenset({"F"})
    assert areas[0].disallow_text and areas[0].disallow_graphic
    # Zone polygons are board-relative: sheet 120..140 minus origin (100,100).
    assert areas[0].polygon.bounds == (20.0, 20.0, 40.0, 40.0)

    violations = check_silk_in_keepout(pcb).violations
    assert [(v.rule_id, v.severity) for v in violations] == [
        ("silk_in_keepout", "error"),
        ("silk_in_keepout", "error"),
    ]
    items = {v.items for v in violations}
    assert ("gr_line {front-line}", _AREA_LABEL) in items
    assert ("LEGEND", _AREA_LABEL) in items
    # The back line at the same coordinates prints on the other side.
    assert all("back-line" not in i for pair in items for i in pair)


def test_no_sibling_rule_leaves_silk_legal(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _FRONT_TEXT, dru=None)
    pcb = PCB.load(board)
    assert resolve_silk_keepouts(pcb) == []
    assert check_silk_in_keepout(pcb).violations == []


def test_copper_scoped_rule_leaves_silk_legal(tmp_path: Path) -> None:
    # The same area addressed by a rule scoped to copper: the five built-in
    # flags leave the silk legal and so does this custom rule.
    dru = _DRU.replace('(layer "F.SilkS")', '(layer "F.Cu")')
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _FRONT_TEXT, dru=dru)
    pcb = PCB.load(board)
    assert resolve_silk_keepouts(pcb) == []
    assert check_silk_in_keepout(pcb).violations == []


def test_ordinary_copper_keepout_is_not_a_silk_keepout(tmp_path: Path) -> None:
    copper_zone = _SILK_ZONE.replace('(layer "F.SilkS")', '(layer "F.Cu")').replace(
        "(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) "
        "(footprints allowed))",
        "(keepout (tracks not_allowed) (vias not_allowed))",
    )
    board = _board(tmp_path, copper_zone + _FRONT_LINE + _FRONT_TEXT, dru=None)
    pcb = PCB.load(board)
    assert check_silk_in_keepout(pcb).violations == []


def test_text_only_restriction_spares_graphics(tmp_path: Path) -> None:
    dru = _DRU.replace("disallow text graphic", "disallow text")
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _FRONT_TEXT, dru=dru)
    pcb = PCB.load(board)

    areas = resolve_silk_keepouts(pcb)
    assert len(areas) == 1
    assert areas[0].disallow_text and not areas[0].disallow_graphic

    violations = check_silk_in_keepout(pcb).violations
    assert len(violations) == 1
    assert violations[0].items[0] == "LEGEND"


def test_footprint_owned_rotated_area(tmp_path: Path) -> None:
    # KiCad stores footprint-embedded zones in board coordinates with the
    # placement already applied (schema/pcb.py, Issue #6119), so a rotated
    # RF module's antenna area is where the polygon says it is.  The
    # module's own fp_line silk, rotated with it, crosses the area.
    body = """
  (footprint "X:Y" (layer "F.Cu") (at 230 230 90)
    (property "Reference" "AE1" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (zone (net 0) (net_name "") (layer "F.SilkS") (name "antenna") (hatch edge 0.5)
      (uuid "bbbbbbbb-0000-0000-0000-000000000002")
      (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
      (polygon (pts (xy 225 225) (xy 235 225) (xy 235 235) (xy 225 235))))
    (fp_line (start -4 -4) (end 4 4) (stroke (width 0.15) (type solid))
      (layer "F.SilkS") (uuid "fp-own-line"))
  )
  (gr_line (start 145 145) (end 148 148) (stroke (width 0.15) (type solid))
    (layer "F.SilkS") (uuid "outside-line"))
"""
    dru = (
        "(version 1)\n"
        '(rule "no print"\n'
        '  (layer "F.SilkS")\n'
        "  (condition \"A.intersectsArea('antenna')\")\n"
        "  (constraint disallow graphic))\n"
    )
    board = _board(tmp_path, body, dru=dru)
    pcb = PCB.load(board)

    areas = resolve_silk_keepouts(pcb)
    assert len(areas) == 1
    assert areas[0].polygon.bounds == (125.0, 125.0, 135.0, 135.0)

    violations = check_silk_in_keepout(pcb).violations
    # The footprint's own rotated line (uuid fp-own-line) crosses the
    # area; the far-away board-level line does not.
    hit = {v.items[0] for v in violations}
    assert hit == {"AE1 (fp_line) {fp-own-line}"}


def test_registered_in_check_all_and_categorised(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE)
    pcb = PCB.load(board)

    results = check_all_silkscreen(pcb, _jlcpcb())
    assert any(v.rule_id == "silk_in_keepout" and v.severity == "error" for v in results.violations)
    # The error severity is what fails the check: DRCResults.error_count is
    # the failing-verdict input the CLI reports.
    assert results.error_count >= 1
    # The ``silk`` prefix fallback keeps the rule out of the fab-blocking
    # Manufacturing bucket; its error severity still fails the check.
    assert DRCChecker.category_for_rule("silk_in_keepout") == "advisory-quality"
    assert DRCChecker.category_for_rule("silk_under_body") == "advisory-quality"


# ---------------------------------------------------------------------------
# repair_keepout_clearance: the clip #9797 consumes
# ---------------------------------------------------------------------------


def _gr_lines(board: Path) -> list[tuple[str, tuple[float, float], tuple[float, float], str]]:
    out = []
    for node in parse_file(board).find_all("gr_line"):
        layer = str(node.find("layer").get_first_atom())  # type: ignore[union-attr]
        start = node.find("start")
        end = node.find("end")
        assert start is not None and end is not None
        s = (float(start.get_float(0)), float(start.get_float(1)))  # type: ignore[union-attr]
        e = (float(end.get_float(0)), float(end.get_float(1)))  # type: ignore[union-attr]
        uid = str(node.find("uuid").get_first_atom()) if node.find("uuid") else ""  # type: ignore[union-attr]
        out.append((layer, s, e, uid))
    return out


_AREA_RECT = "(polygon (pts (xy 120 120) (xy 140 120) (xy 140 140) (xy 120 140)))"
_AREA_HOLE = "(polygon (pts (xy 126 126) (xy 134 126) (xy 134 134) (xy 126 134)))"
_AREA_ARC = (
    "(polygon (pts (xy 120 120) (arc (start 130 120) (mid 140 130) (end 130 140)) (xy 120 140)))"
)
_AREA_CIRCULAR_HOLE = (
    "(polygon (pts (arc (start 124 122) (mid 122 124) (end 124 126)) "
    "(arc (start 124 126) (mid 126 124) (end 124 122))))"
)


@pytest.mark.parametrize("footprint_owned", [False, True], ids=["board", "rotated-footprint"])
@pytest.mark.parametrize(
    ("contours", "ignored", "forbidden"),
    [
        (_AREA_RECT + _AREA_HOLE, False, {"ring", "lobe"}),
        (_AREA_ARC, False, {"ring", "center", "lobe"}),
        (_AREA_RECT + _AREA_HOLE, True, {"center"}),
        (_AREA_RECT + _AREA_HOLE + _AREA_CIRCULAR_HOLE, False, {"lobe"}),
        (_AREA_RECT + _AREA_HOLE + _AREA_CIRCULAR_HOLE, True, {"ring", "center"}),
    ],
    ids=["cutout", "arc", "ignored-cutout", "two-cutouts", "two-ignored-cutouts"],
)
def test_rule_area_contours_match_native_silk(
    tmp_path: Path, footprint_owned: bool, contours: str, ignored: bool, forbidden: set[str]
) -> None:
    # KiCad saves cutouts as sibling polygons and can omit both arc endpoints
    # from the literal xy vertices. Embedded rule areas already use sheet coordinates.
    zone = _SILK_ZONE.replace(_AREA_RECT, contours)
    rules = _DRU
    if ignored:
        zone = zone.replace("no_ink", "exception").replace("000000000001", "000000000002")
        rules += (
            '(rule "allow ink" (layer "F.SilkS") (condition "A.intersectsArea(\'exception\')") '
            "(severity ignore) (constraint disallow graphic))"
        )
    if footprint_owned:
        zone = f'(footprint "X:Y" (layer "F.Cu") (at 130 130 90) {zone})'
    probes = {
        "ring": (123, 125, 124),
        "center": (128, 132, 130),
        "lobe": (135, 136, 129),
        "outside": (143, 145, 130),
    }
    ink = "".join(
        f"(gr_line (start {x1} {y}) (end {x2} {y}) (stroke (width 0.2) (type solid)) "
        f'(layer "F.SilkS") (uuid "{name}"))'
        for name, (x1, x2, y) in probes.items()
    )
    board = _board(tmp_path, (_SILK_ZONE if ignored else "") + zone + ink, rules)
    pcb = PCB.load(board)
    assert {v.items[0] for v in check_silk_in_keepout(pcb).violations} == {
        f"gr_line {{{name}}}" for name in forbidden
    }
    if contours == _AREA_ARC:
        (area,) = pcb.footprint_rule_areas if footprint_owned else pcb.rule_areas
        assert area.polygon == [(20, 20), (20, 40)]
    repairer = SilkscreenRepairer(board)
    assert repairer.repair_keepout_clearance().total_fixed == len(forbidden)
    repairer.save()
    assert {uid for _, _, _, uid in _gr_lines(board)} == probes.keys() - forbidden
    assert not check_silk_in_keepout(PCB.load(board)).violations


def test_clipping_across_a_rule_area_keeps_the_ink_in_its_cutout(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE.replace(_AREA_RECT, _AREA_RECT + _AREA_HOLE) + _FRONT_LINE)
    repairer = SilkscreenRepairer(board)
    (fix,) = repairer.repair_keepout_clearance().fixes
    assert fix.pieces == 3
    repairer.save()
    spans = sorted((min(a[0], b[0]), max(a[0], b[0])) for _, a, b, _ in _gr_lines(board))
    left, middle, right = spans
    assert left[0] == 110 and left[1] < 120
    assert 126 < middle[0] < 130 < middle[1] < 134
    assert right[0] > 140 and right[1] == 150
    assert not check_silk_in_keepout(PCB.load(board)).violations


def test_curved_cutouts_move_with_a_repositioned_footprint(tmp_path: Path) -> None:
    from shapely.geometry import Point

    zone = _SILK_ZONE.replace(_AREA_RECT, _AREA_RECT + _AREA_HOLE + _AREA_CIRCULAR_HOLE)
    board = _board(tmp_path, f'(footprint "X:Y" (layer "F.Cu") (at 130 130 90) {zone})')
    pcb = PCB.load(board)
    fp = pcb.footprints[0]
    fp.position = (45, 35)
    fp.rotation = 180
    pcb.save()
    for placed in (pcb, PCB.load(board)):
        (area,) = resolve_silk_keepouts(placed)
        assert area.polygon.bounds == pytest.approx((35, 25, 55, 45))
        assert len(area.polygon.interiors) == 2
        assert not area.polygon.covers(Point(45, 35))  # square cutout
        assert not area.polygon.covers(Point(39, 41))  # circular cutout
        assert area.polygon.covers(Point(44, 29))  # solid band beyond both


def test_clip_keeps_both_printable_portions_and_is_idempotent(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _BACK_LINE)

    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    repairer.save()

    assert len(result.fixes) == 1
    fix = result.fixes[0]
    assert fix.owner == "board"
    assert fix.element_type == "gr_line"
    assert fix.action == "trimmed"
    assert fix.pieces == 2
    assert fix.areas == [_AREA_LABEL]
    assert fix.new_area < fix.old_area
    assert result.text_residual == []
    assert result.residual == []

    front = [l for l in _gr_lines(board) if l[0] == "F.SilkS"]
    assert len(front) == 2
    # The area (120..140) grew by the stroke half-width plus the safety
    # margin; both survivors end strictly outside it.
    for _layer, (sx, _sy), (ex, _ey), _uid in front:
        assert max(sx, ex) < 120.0 or min(sx, ex) > 140.0
        assert max(sx, ex) <= 120.0 + 1e-6 or min(sx, ex) >= 140.0 - 1e-6 or True
    assert PCB.load(board)  # still parses

    # The clipped board passes the check...
    assert check_silk_in_keepout(PCB.load(board)).violations == []
    # ...and a second pass changes nothing, byte for byte.
    once = board.read_bytes()
    repairer = SilkscreenRepairer(board)
    assert repairer.repair_keepout_clearance().total_fixed == 0
    repairer.save()
    assert board.read_bytes() == once


def test_clip_removes_graphic_inside_area_and_reports_text(tmp_path: Path) -> None:
    inside_rect = (
        "  (gr_rect (start 126 126) (end 134 134) (stroke (width 0.15) (type solid)) "
        '(fill none) (layer "F.SilkS") (uuid "inside-rect"))\n'
    )
    board = _board(tmp_path, _SILK_ZONE + inside_rect + _FRONT_TEXT)

    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    repairer.save()

    actions = {(f.element_type, f.action) for f in result.fixes}
    assert ("gr_rect", "removed") in actions
    # Text is never clipped: the surviving item and its area are reported,
    # and the re-measured rule keeps it an error.
    assert result.text_residual == [f"LEGEND in {_AREA_LABEL}"]
    assert result.residual == [
        f"Silkscreen LEGEND lies in silk keepout {_AREA_LABEL}",
    ]
    residual = check_silk_in_keepout(PCB.load(board)).violations
    assert [(v.rule_id, v.severity) for v in residual] == [("silk_in_keepout", "error")]


def test_clip_footprint_silk_through_rotation(tmp_path: Path) -> None:
    # The rotated module's own fp_line crossing its antenna area is clipped
    # through the footprint transform, in footprint-local coordinates.
    # fp-local (-8,-8)..(8,8) rotated 90 at sheet (230,230) spans
    # (222,238)-(238,222): both ends outside the 225..235 area, the middle
    # inside it, so two printable portions survive.
    body = """
  (footprint "X:Y" (layer "F.Cu") (at 230 230 90)
    (property "Reference" "AE1" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (zone (net 0) (net_name "") (layer "F.SilkS") (name "antenna") (hatch edge 0.5)
      (uuid "bbbbbbbb-0000-0000-0000-000000000002")
      (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
      (polygon (pts (xy 225 225) (xy 235 225) (xy 235 235) (xy 225 235))))
    (fp_line (start -8 -8) (end 8 8) (stroke (width 0.15) (type solid))
      (layer "F.SilkS") (uuid "fp-own-line"))
  )
"""
    dru = (
        "(version 1)\n"
        '(rule "no print"\n'
        '  (layer "F.SilkS")\n'
        "  (condition \"A.intersectsArea('antenna')\")\n"
        "  (constraint disallow graphic))\n"
    )
    board = _board(tmp_path, body, dru=dru)

    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    repairer.save()

    assert len(result.fixes) == 1
    fix = result.fixes[0]
    assert fix.owner == "AE1"
    assert fix.element_type == "fp_line"
    assert fix.pieces == 2
    assert result.residual == []
    assert check_silk_in_keepout(PCB.load(board)).violations == []


# ---------------------------------------------------------------------------
# Grading per the operator ruling on #9795
# ---------------------------------------------------------------------------


def _pad_footprint(body: str = "", *, ref: str = "U1", at: str = "130 130") -> str:
    return f"""  (footprint "X:Y" (layer "F.Cu") (at {at})
    (property "Reference" "{ref}" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    {body}
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste"))
  )
"""


_PAD_SILK = (
    "(fp_line (start -0.9 0) (end 0.9 0) (stroke (width 0.15) (type solid)) "
    '(layer "F.SilkS") (uuid "over-pad"))'
)


def test_silk_over_pad_error_under_jlcpcb_warning_elsewhere(tmp_path: Path) -> None:
    board = _board(tmp_path, _pad_footprint(_PAD_SILK))
    pcb = PCB.load(board)

    def findings(rules):
        return [
            (v.severity, v.items)
            for v in check_silk_over_copper(pcb, rules).violations
            if v.items[1].startswith("U1 pad")
        ]

    assert findings(_jlcpcb()) == [("error", ("U1 (fp_line)", "U1 pad 1"))]
    assert findings(_other()) == [("warning", ("U1 (fp_line)", "U1 pad 1"))]


def test_untented_via_not_an_error_under_jlcpcb(tmp_path: Path) -> None:
    # An untented via (setup default untented) with silk straight over it:
    # reported, but never at error severity under the jlcpcb profiles --
    # and the pad hit next to it still is.
    body = (
        "  (setup (pad_to_mask_clearance 0) (tenting (front no) (back no)))\n"
        '  (via (at 130 130) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0)'
        ' (uuid "v1"))\n'
        + _pad_footprint("", ref="U1", at="135 130").replace(
            '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")',
            '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")',
        )
        + "  (gr_line (start 129 130) (end 136.5 130) (stroke (width 0.3) (type solid)) "
        '(layer "F.SilkS") (uuid "span"))\n'
    )
    board = _board(tmp_path, body)
    pcb = PCB.load(board)

    over_copper = [
        (v.severity, v.items[1]) for v in check_silk_over_copper(pcb, _jlcpcb()).violations
    ]
    vias = [entry for entry in over_copper if entry[1].startswith("via")]
    pads = [entry for entry in over_copper if entry[1].startswith("U1 pad")]
    assert vias and all(sev == "info" for sev, _ in vias)
    assert pads and all(sev == "error" for sev, _ in pads)
    # Control: with the knob off (non-JLC profile) both are warnings again.
    plain = [(v.severity, v.items[1]) for v in check_silk_over_copper(pcb, _other()).violations]
    assert plain and all(sev == "warning" for sev, _ in plain)


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"])
def test_via_only_fixture_has_no_silk_errors(tmp_path: Path, profile: str) -> None:
    # A tented and an untented via, silk over both, nothing else on the
    # board: no silk rule may grade it an error under the jlcpcb profiles.
    # Both reporting routes are exercised -- the drill set
    # (``silk_hole_overlap``, whose via split is #9112's) and the untented
    # aperture set (``silk_over_copper``) -- so a via-only case cannot
    # reappear as an error under a second rule.
    body = (
        '  (via (at 130 130) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0)'
        ' (uuid "tented"))\n'
        '  (via (at 140 130) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0)'
        ' (uuid "bare") (tenting (front no) (back no)))\n'
        "  (gr_line (start 129 130) (end 141 130) (stroke (width 0.3) (type solid)) "
        '(layer "F.SilkS") (uuid "span"))\n'
    )
    board = _board(tmp_path, body)
    pcb = PCB.load(board)
    rules = _jlc_rules(profile)

    assert check_silk_hole_overlap(pcb, rules).violations  # reported, but...
    for violation in check_silk_hole_overlap(pcb, rules).violations:
        assert violation.severity != "error"
    for violation in check_silk_over_copper(pcb, rules).violations:
        assert violation.severity != "error"


def test_pth_and_npth_holes_stay_reportable(tmp_path: Path) -> None:
    # PTH / NPTH drills under silk remain reported under the jlcpcb
    # profiles.  The severity split (error for real holes, via waived) is
    # open PR #9112 / issue #9033's regrade; these assertions hold before
    # and after it lands.
    pads = """  (footprint "X:Y" (layer "F.Cu") (at 130 130)
    (property "Reference" "J1" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5)
      (layers "*.Cu" "*.Mask") (uuid "pth"))
    (pad "2" np_thru_hole circle (at 3 0) (size 1 1) (drill 0.5)
      (layers "*.Cu" "*.Mask") (uuid "npth"))
  )
  (gr_line (start 129.5 130) (end 133.5 130) (stroke (width 0.3) (type solid))
    (layer "F.SilkS") (uuid "span"))
"""
    board = _board(tmp_path, pads)
    pcb = PCB.load(board)

    holes = [v.items[1] for v in check_silk_hole_overlap(pcb, _jlcpcb()).violations]
    assert any("J1 pad 1 hole" in h for h in holes)
    assert any("J1 pad 2 hole" in h for h in holes)


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"])
def test_silk_minimums_error_under_jlcpcb(tmp_path: Path, profile: str) -> None:
    thin = (
        "  (gr_line (start 110 110) (end 120 110) (stroke (width 0.1) (type solid)) "
        '(layer "F.SilkS") (uuid "thin"))\n'
        '  (gr_text "tiny" (at 115 115) (layer "F.SilkS") '
        "(effects (font (size 0.6 0.6) (thickness 0.1))))\n"
    )
    board = _board(tmp_path, thin, dru=None)
    pcb = PCB.load(board)

    under_jlc = check_silkscreen_line_width(pcb, _jlc_rules(profile)).violations
    assert under_jlc and all(v.severity == "error" for v in under_jlc)
    under_other = check_silkscreen_line_width(pcb, _other()).violations
    assert under_other and all(v.severity == "warning" for v in under_other)

    from kicad_tools.validate.rules.silkscreen import (
        check_silkscreen_text_height,
        check_silkscreen_text_thickness,
    )

    assert all(
        v.severity == "error" for v in check_silkscreen_text_height(pcb, _jlcpcb()).violations
    )
    assert check_silkscreen_text_height(pcb, _jlcpcb()).violations
    assert all(
        v.severity == "error" for v in check_silkscreen_text_thickness(pcb, _jlcpcb()).violations
    )
    assert check_silkscreen_text_thickness(pcb, _jlcpcb()).violations
    assert all(
        v.severity == "warning" for v in check_silkscreen_text_thickness(pcb, _other()).violations
    )


_COURTYARD_FP = """  (footprint "X:Y" (layer "F.Cu") (at 130 130)
    (property "Reference" "U1" (at 0 -6) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -3 -3) (end 3 3) (stroke (width 0.05) (type solid))
      (fill none) (layer "F.CrtYd") (uuid "crtyd"))
    (fp_line (start -2 -2) (end 2 -2) (stroke (width 0.15) (type solid))
      (layer "F.SilkS") (uuid "own-silk"))
  )
"""


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"])
def test_silk_under_body_error_under_jlcpcb(tmp_path: Path, profile: str) -> None:
    # Graphics and text of *another* part (here: board-level silk) hidden
    # entirely under U1's body on the same side -- per the operator: an
    # error under jlcpcb.  U1's own silk inside its own courtyard is not
    # this rule's finding, and ordinary silk-to-silk intersections alone do
    # not demonstrate body obstruction.
    body = (
        _COURTYARD_FP
        + "  (gr_line (start 128.5 129) (end 131.5 129) (stroke (width 0.15) (type solid)) "
        '(layer "F.SilkS") (uuid "hidden"))\n' + '  (gr_text "SW" (at 130 131) (layer "F.SilkS") '
        "(effects (font (size 1 1) (thickness 0.15))))\n"
        + "  (gr_line (start 150 150) (end 152 152) (stroke (width 0.15) (type solid)) "
        '(layer "F.SilkS") (uuid "free-1"))\n'
        + "  (gr_line (start 150 150) (end 152 149) (stroke (width 0.15) (type solid)) "
        '(layer "F.SilkS") (uuid "free-2"))\n'
    )
    board = _board(tmp_path, body, dru=None)
    pcb = PCB.load(board)

    def hit_items(rules):
        return {v.items for v in check_silk_under_body(pcb, rules).violations}

    under_jlc = hit_items(_jlcpcb())
    assert ("gr_line {hidden}", "U1 (courtyard)") in under_jlc
    assert ("SW", "U1 (courtyard)") in under_jlc
    assert all(v.severity == "error" for v in check_silk_under_body(pcb, _jlcpcb()).violations)
    # Grading control: warning under a non-JLC profile.
    under_other = check_silk_under_body(pcb, _other()).violations
    assert under_other and all(v.severity == "warning" for v in under_other)
    # The module's own silk and the crossing free lines are never flagged.
    for pair in under_jlc:
        assert "own-silk" not in pair[0]
        assert "free-" not in pair[0]


# ---------------------------------------------------------------------------
# Native parity on the pinned kicad-cli image (AC: findings agree where
# kicad-cli checks the same thing)
# ---------------------------------------------------------------------------


def _native_items_not_allowed(board: Path) -> list[str]:
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    report = board.with_suffix(".drc.json")
    subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            str(board),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    return [
        item.get("description", "")
        for v in data["violations"]
        if v["type"] == "items_not_allowed"
        for item in v["items"]
    ]


def test_native_parity_on_pinned_image(tmp_path: Path) -> None:
    # The curated fixture from the issue: the sibling rule makes the pinned
    # image report the same two items kct does (front line + front text),
    # and removing it clears both engines.  kct keeps its own rule id and
    # severity -- the native ``items_not_allowed`` finding and the kct
    # verdict stay separately identifiable.
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE + _BACK_LINE + _FRONT_TEXT)
    pcb = PCB.load(board)
    kct_items = sorted(v.items[0] for v in check_silk_in_keepout(pcb).violations)
    assert kct_items == ["LEGEND", "gr_line {front-line}"]

    native = _native_items_not_allowed(board)
    assert len(native) == 2
    assert any("Segment on F.Silkscreen" in d for d in native)
    assert any("'LEGEND' on F.Silkscreen" in d for d in native)
    assert not any("B.Silkscreen" in d for d in native)  # back line is legal

    # Same board without the sibling rule: both engines are silent.
    board.with_suffix(".kicad_dru").unlink()
    assert _native_items_not_allowed(board) == []
    assert check_silk_in_keepout(PCB.load(board)).violations == []


def test_obscured_reference_error_under_jlcpcb(tmp_path: Path) -> None:
    # A hidden reference with no replacement text: the ref half of the
    # under-body ruling, regraded with the same knob.
    body = _COURTYARD_FP.replace(
        '(property "Reference" "U1" (at 0 -6) (layer "F.Fab")',
        '(property "Reference" "U1" (at 0 -6) (layer "F.Fab") (hide yes)',
    )
    board = _board(tmp_path, body, dru=None)
    pcb = PCB.load(board)

    under_jlc = check_silk_ref_obscured(pcb, _jlcpcb()).violations
    assert under_jlc and all(v.severity == "error" for v in under_jlc)
    under_other = check_silk_ref_obscured(pcb, _other()).violations
    assert under_other and all(v.severity == "warning" for v in under_other)


# ---------------------------------------------------------------------------
# Review regressions (PR #9823): public legalizer, negated conditions, and
# per-side / per-kind pairing across rules on one area.
# ---------------------------------------------------------------------------


def _back_text(x: float = 130.0, y: float = 130.0) -> str:
    return (
        f'  (gr_text "BACKTXT" (at {x} {y}) (layer "B.SilkS") '
        "(effects (font (size 1 1) (thickness 0.15)) (justify mirror)))\n"
    )


def test_fix_silkscreen_all_clips_keepout_through_public_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main

    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE)
    assert check_silk_in_keepout(PCB.load(board)).violations  # the defect is real

    code = fix_silkscreen_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"])
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["total_fixed"] >= 1
    assert "silk_in_keepout" in report["rules_covered"]
    assert report["total_keepout_clip_fixed"] == 1
    fix = report["keepout_clip_fixes"][0]
    assert fix["action"] == "trimmed" and fix["pieces"] == 2
    assert fix["layer"] == "F.SilkS"  # not the literal string "None"
    assert report["keepout_residual"] == []
    assert check_silk_in_keepout(PCB.load(board)).violations == []
    assert len([l for l in _gr_lines(board) if l[0] == "F.SilkS"]) == 2


@pytest.mark.parametrize("owner", ["board", "rotated-footprint"])
@pytest.mark.parametrize("legacy_width", [False, True], ids=["stroke", "legacy-width"])
@pytest.mark.parametrize(
    ("corners", "fill", "width", "exempt", "action"),
    [
        ((115, 145), "yes", 0.2, False, "trimmed"),
        ((115, 145), "no", 0.2, False, None),
        ((126, 132), "yes", 0.0, False, "removed"),
        ((126, 132), "no", 0.0, False, "removed"),
        ((115, 145), "no", 0.0, False, None),
        ((115, 145), "", 0.0, False, "trimmed"),
        ((115, 145), "", 0.2, False, None),
        ((115, 135), "", 0.0, True, None),
        ((115, 135), "yes", 0.2, True, None),
        ((115, 135), "no", 0.2, True, "trimmed"),
        ((115, 135), "no", 0.0, True, "trimmed"),
    ],
    ids=(
        "enclosing-solid",
        "enclosing-outline",
        "zero-width-solid",
        "zero-width-outline",
        "zero-width-outline-surrounds-area",
        "implicit-solid",
        "implicit-outline",
        "implicit-solid-exemption",
        "interior-exemption",
        "outline-misses-exemption",
        "zero-width-outline-misses-exemption",
    ),
)
def test_rectangle_ink_in_keepout_and_saved_repair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    owner: str,
    legacy_width: bool,
    corners: tuple[int, int],
    fill: str,
    width: float,
    exempt: bool,
    action: str | None,
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main

    lo, hi = corners
    fill_node = f"(fill {fill})" if fill else ""
    stroke = f"(width {width})" if legacy_width else f"(stroke (width {width}) (type solid))"
    style = f'{stroke} {fill_node} (layer "F.SilkS")'
    if owner == "board":
        graphic = f"(gr_rect (start {lo} {lo}) (end {hi} {hi}) {style})"
    else:
        # Invert the 90-degree placement so the footprint prints the same rectangle.
        graphic = (
            '(footprint "Test:Rect" (layer "F.Cu") (at 110 150 90) '
            f"(fp_rect (start {150 - lo} {lo - 110}) (end {150 - hi} {hi - 110}) {style}))"
        )
    zones, rules = _SILK_ZONE, _DRU
    if exempt:
        zones += (
            _SILK_ZONE.replace("no_ink", "preserve_only")
            .replace("000000000001", "000000000002")
            .replace("120", "124")
            .replace("140", "130")
        )
        rules += """(rule "preserve printed ink"
          (layer "F.SilkS") (condition "A.intersectsArea('preserve_only')")
          (severity ignore) (constraint disallow graphic))"""
    board = _board(tmp_path, zones + graphic + _BACK_LINE, rules)
    original = board.read_bytes()
    assert len(check_silk_in_keepout(PCB.load(board)).violations) == int(action is not None)

    args = [str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]
    assert fix_silkscreen_main(args) == 0
    fixes = json.loads(capsys.readouterr().out)["keepout_clip_fixes"]
    assert [fix["action"] for fix in fixes] == ([] if action is None else [action])
    saved = board.read_bytes()
    if action is None:
        assert saved == original
    elif action == "trimmed" and fill == "yes":
        # The legal band outside the 20 mm square keepout remains printed.
        assert fixes[0]["new_area_mm2"] == pytest.approx(511.95, abs=0.02)
    assert check_silk_in_keepout(PCB.load(board)).violations == []
    assert fix_silkscreen_main(args) == 0
    assert json.loads(capsys.readouterr().out)["keepout_clip_fixes"] == []
    assert board.read_bytes() == saved


@pytest.mark.parametrize("footprint", [False, True], ids=["board", "rotated-footprint"])
@pytest.mark.parametrize("width", [0, 0.2])
@pytest.mark.parametrize("control", ["active", "rule-off", "other-side", "displaced", "hull-only"])
def test_public_keepout_reports_the_authored_bezier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], footprint: bool, width: float, control: str
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_main

    identity = "fb5658fe-0010-4050-9000-000000000001"
    # This cubic's printed width-0.20 area is 1.753699 mm² on KiCad 10.0.6.
    points = [(125, 129), (126, 125), (130, 130), (132, 126)]
    if control == "displaced":
        points = [(x + 20, y) for x, y in points]
    if footprint:
        # Inverse of the 90 degree footprint placement at (110, 110).
        points = [(110 - y, x - 110) for x, y in points]
    vertices = " ".join(f"(xy {x} {y})" for x, y in points)
    layer = "B.SilkS" if control == "other-side" else "F.SilkS"
    owner = "fp" if footprint else "gr"
    curve = (
        f"({owner}_curve (pts {vertices}) (stroke (width {width}) (type solid)) "
        f'(layer "{layer}") (uuid "{identity}"))'
    )
    if footprint:
        curve = f'(footprint "test:curve" (layer "F.Cu") (at 110 110 90) {curve})'
    zone = _SILK_ZONE
    if control == "hull-only":
        # A control point falls inside this window. The ink stays outside.
        zone = zone.replace(
            "(xy 120 120) (xy 140 120) (xy 140 140) (xy 120 140)",
            "(xy 125.9 124.9) (xy 126.1 124.9) (xy 126.1 125.1) (xy 125.9 125.1)",
        )
    board = _board(tmp_path, zone + curve, dru=None if control == "rule-off" else _DRU)
    original = board.read_bytes()
    violations = check_silk_in_keepout(PCB.load(board)).violations
    assert len(violations) == (control == "active")
    if violations:
        assert identity in violations[0].items[0] and violations[0].items[1] == _AREA_LABEL
        if width:
            assert violations[0].actual_value == pytest.approx(1.753699, abs=0.001)
    for _ in range(2):
        assert fix_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == bool(
            violations
        )
        report = json.loads(capsys.readouterr().out)
        assert report["text_in_keepout"] == [] and not report["total_fixed"]
        assert len(report["keepout_residual"]) == len(violations)
        if violations:
            assert (
                identity in report["keepout_residual"][0]
                and _AREA_LABEL in report["keepout_residual"][0]
            )
        assert board.read_bytes() == original


_TEXT_SHAPE_ID = "fb5658fe-0010-4050-9000-000000000001"


def _boxed_silk(kind: str, layer: str = "F.SilkS", *, uuid: bool = True) -> str:
    ident = f'(uuid "{_TEXT_SHAPE_ID}")' if uuid else ""
    effects = "(effects (font (size 1 1) (thickness 0.2)))"
    stroke = "(stroke (width 0.2) (type solid))"
    if kind == "table":
        return (
            f'(table (column_count 1) {ident} (layer "{layer}") '
            f"(border (external yes) (header yes) {stroke}) "
            f"(separators (rows yes) (cols yes) {stroke}) (column_widths 8) (row_heights 4) "
            f'(cells (table_cell "MARK" (start 124 125) (end 132 129) (margins 0.3 0.3 0.3 0.3) '
            f'(span 1 1) (layer "{layer}") (effects (font (size 1 1) (thickness 0.2)) '
            "(justify left top)))))"
        )
    if kind == "gr_text_box":
        return f'({kind} "MARK" (start 123 124) (end 133 129) (layer "{layer}") {ident} {effects} {stroke})'
    if kind == "fp_text_box":
        mark = f'({kind} "MARK" (start 13 14) (end 23 19) (layer "{layer}") {ident} {effects} {stroke})'
    else:
        mark = f'(property "Manufacturer" "MARK" (at 18 18) (layer "{layer}") {ident} {effects})'
    return f'(footprint "test:ink" (layer "F.Cu") (at 110 110) {mark})'


@pytest.mark.parametrize("kind", ["gr_text_box", "fp_text_box", "table", "property"])
@pytest.mark.parametrize(
    "selection", ["whole", "type", "wrong-type", "graphic-only", "back", "rule-off"]
)
def test_native_text_items_retain_a_named_keepout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, selection: str
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_main
    from kicad_tools.validate.mask_copper_geometry import _resolve_native_python_command

    if _resolve_native_python_command(None, tmp_path)[0] is None:
        pytest.skip("Per-object native KiCad geometry unavailable")
    item_type = {
        "gr_text_box": "Text Box",
        "fp_text_box": "Text Box",
        "table": "Table",
        "property": "Text",
    }[kind]
    dru = _DRU
    if selection in ("type", "wrong-type"):
        expected = item_type if selection == "type" else "Table Cell"
        dru = dru.replace(
            "A.intersectsArea('no_ink')", f"A.intersectsArea('no_ink') && A.Type == '{expected}'"
        )
    if selection == "graphic-only":
        dru = dru.replace("disallow text graphic", "disallow graphic")
    board = _board(
        tmp_path,
        _SILK_ZONE + _boxed_silk(kind, "B.SilkS" if selection == "back" else "F.SilkS"),
        None if selection == "rule-off" else dru,
    )
    original = board.read_bytes()
    positive = selection in ("whole", "type")
    violations = check_silk_in_keepout(PCB.load(board)).violations
    assert [v.rule_id for v in violations] == (["silk_in_keepout"] if positive else [])
    if positive:
        assert violations[0].severity == "error" and violations[0].actual_value > 0
        assert _TEXT_SHAPE_ID in violations[0].items[0] and violations[0].items[1] == _AREA_LABEL
    for _ in range(2):
        assert fix_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert not result["total_fixed"] and not result["keepout_residual"]
        assert result["text_in_keepout"] == ([" / ".join(violations[0].items)] if positive else [])
        assert board.read_bytes() == original


@pytest.mark.parametrize(
    "kind,x,y,hit,native",
    [
        ("gr_text_box", 128.47345, 126.47531, True, True),  # Plotted glyph.
        ("fp_text_box", 131.133152, 126.577926, False, False),  # Blank box interior.
        ("gr_text_box", 133.0, 126.5, True, True),
        ("borderless", 133.0, 126.5, False, True),  # Frame.
        ("table", 132.0, 127.0, True, True),  # Table's native effective border.
        (
            "table",
            126.003006,
            126.051504,
            True,
            False,
        ),  # Native table DRC leaves the cell glyph clear.
    ],
)
def test_native_keepout_uses_the_effective_text_shape(
    tmp_path: Path, kind: str, x: float, y: float, hit: bool, native: bool
) -> None:
    from kicad_tools.validate.mask_copper_geometry import _resolve_native_python_command

    if _resolve_native_python_command(None, tmp_path)[0] is None:
        pytest.skip("Per-object native KiCad geometry unavailable")
    points = " ".join(
        f"(xy {x + dx:.6f} {y + dy:.6f})"
        for dx, dy in ((-0.008, -0.008), (0.008, -0.008), (0.008, 0.008), (-0.008, 0.008))
    )
    zone = _SILK_ZONE.replace("(xy 120 120) (xy 140 120) (xy 140 140) (xy 120 140)", points)
    mark = _boxed_silk("gr_text_box" if kind == "borderless" else kind, uuid=False)
    if kind == "borderless":
        mark = mark.replace('(layer "F.SilkS")', '(layer "F.SilkS") (border no)')
    board = _board(tmp_path, zone + mark)
    source = board.read_bytes()
    assert bool(_native_items_not_allowed(board)) == native
    results = check_silk_in_keepout(PCB.load(board)).violations
    assert [v.rule_id for v in results] == (["silk_in_keepout"] if hit else [])
    if results:
        assert "{" not in results[0].items[0] and results[0].items[1] == _AREA_LABEL
    assert source == board.read_bytes()


def test_unavailable_native_shape_names_the_unmeasured_item_and_fails_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_main
    from kicad_tools.validate import native_silk_text

    def unavailable(*args):
        raise RuntimeError("KiCad is unavailable")

    monkeypatch.setattr(native_silk_text, "_native_shapes", unavailable)
    board = _board(tmp_path, _SILK_ZONE + _boxed_silk("gr_text_box"))
    original = board.read_bytes()
    (finding,) = check_silk_in_keepout(PCB.load(board)).violations
    assert finding.rule_id == "silk_keepout_geometry_unverified"
    assert finding.severity == "error" and finding.actual_value is None
    assert _TEXT_SHAPE_ID in finding.items[0] and finding.items[1] == _AREA_LABEL
    assert fix_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["keepout_measurement_errors"] == [finding.message]
    assert not result["text_in_keepout"] and not result["total_fixed"]
    assert original == board.read_bytes()


@pytest.mark.parametrize("owner", ["gr", "fp"])
@pytest.mark.parametrize("shape", ["arc-poly", "rounded-arc", "empty-corner"])
def test_clip_uses_the_curved_boundary_of_closed_silk(
    tmp_path: Path, owner: str, shape: str
) -> None:
    offset = 110 if owner == "fp" else 0

    def at(x: float, y: float) -> str:
        return f"{x - offset} {y - offset}"

    zone = _SILK_ZONE
    if shape == "arc-poly":
        geometry = (
            f"({owner}_poly (pts (xy {at(116, 123)}) (arc (start {at(116, 123)}) "
            f"(mid {at(122, 129)}) (end {at(116, 135)})))"
        )
        fill = "yes"
    else:
        geometry = f"({owner}_rect (start {at(122, 122)}) (end {at(135, 135)}) (radius 3)"
        fill = "no"
        low, high = (122.82, 122.93) if shape == "rounded-arc" else (121.96, 122.04)
        zone = zone.replace(
            "(xy 120 120) (xy 140 120) (xy 140 140) (xy 120 140)",
            f"(xy {low} {low}) (xy {high} {low}) (xy {high} {high}) (xy {low} {high})",
        )
    graphic = f'{geometry} (stroke (width 0.2) (type solid)) (fill {fill}) (layer "F.SilkS"))'
    if owner == "fp":
        graphic = f'(footprint "test:ink" (layer "F.Cu") (at 110 110) {graphic})'
    board = _board(tmp_path, zone + graphic)
    original = board.read_bytes()
    assert bool(check_silk_in_keepout(PCB.load(board)).violations) == (shape != "empty-corner")
    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    if shape == "empty-corner":
        assert not result.fixes and board.read_bytes() == original
    else:
        assert len(result.fixes) == 1 and result.fixes[0].action == "trimmed"
        repairer.save()
        assert not check_silk_in_keepout(PCB.load(board)).violations


@pytest.mark.parametrize("footprint", [False, True], ids=["board", "footprint"])
@pytest.mark.parametrize("has_uuid", [False, True], ids=["no-uuid", "authored-uuid"])
@pytest.mark.parametrize("width", [0, 0.2])
def test_public_keepout_preserves_graphic_and_text_kinds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    footprint: bool,
    has_uuid: bool,
    width: float,
) -> None:
    from kicad_tools.cli.check_cmd import main as check_main
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_main

    prefix = "fp" if footprint else "gr"
    offset = 0 if footprint else 110

    def xy(x: int, y: int) -> str:
        return f"{offset + x} {offset + y}"

    def uuid(n: int) -> str:
        return f' (uuid "fb5658fe-0010-4050-9000-{n:012d}")' if has_uuid else ""

    # Six printed shapes inside no_ink. Real text named like a board graphic
    # must still be reported as text even when it has no UUID.
    shapes = [
        ("line", f"(start {xy(11, 12)}) (end {xy(14, 12)})", ""),
        ("arc", f"(start {xy(15, 14)}) (mid {xy(17, 12)}) (end {xy(19, 14)})", ""),
        ("circle", f"(center {xy(23, 13)}) (end {xy(25, 13)})", ""),
        ("poly", f"(pts (xy {xy(11, 20)}) (xy {xy(13, 20)}) (xy {xy(13, 22)}))", ""),
        ("circle", f"(center {xy(17, 21)}) (end {xy(19, 21)})", "(fill no)"),
        ("poly", f"(pts (xy {xy(22, 20)}) (xy {xy(24, 20)}) (xy {xy(24, 22)}))", "(fill no)"),
    ]
    art = "".join(
        f"({prefix}_{kind} {coords} (stroke (width {width}) (type solid)) {fill} "
        f'(layer "F.SilkS"){uuid(n)})'
        for n, (kind, coords, fill) in enumerate(shapes, 31)
    )
    text = "fp_text user" if footprint else "gr_text"
    art += (
        f'({text} "gr_arc" (at {xy(26, 27)}) (layer "F.SilkS"){uuid(37)} '
        "(effects (font (size 1.5 1.5) (thickness 0.2))))"
    )
    if footprint:
        art = f'(footprint "Test:Legend" (layer "F.Cu") (at 110 110) {art})'
    board = _board(tmp_path, _SILK_ZONE + art)
    args = [str(board), "--mfr", "jlcpcb", "--format", "json"]

    def findings() -> list[dict]:
        assert check_main([*args, "--only", "silkscreen"]) == 2
        result = json.loads(capsys.readouterr().out)
        return [v for v in result["violations"] if v["rule_id"] == "silk_in_keepout"]

    assert len(findings()) == 7
    assert fix_main([*args, "--all"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert [fix["action"] for fix in report["keepout_clip_fixes"]] == ["removed"] * 3
    graphics, texts = report["keepout_residual"], report["text_in_keepout"]
    assert len(graphics) == 3 and len(texts) == 1
    assert all(_AREA_LABEL in item and "mm² overlap" in item for item in graphics)
    if has_uuid:
        assert all(any(f"{n:012d}" in item for item in graphics) for n in (32, 33, 35))
        assert f"{37:012d}" in texts[0]
    assert len(report["unresolved"]) == 4
    assert len(findings()) == 4
    saved = board.read_bytes()
    assert fix_main([*args, "--all"]) == 1
    assert json.loads(capsys.readouterr().out)["total_fixed"] == 0
    assert board.read_bytes() == saved


@pytest.mark.parametrize("shape", ["poly", "circle"])
@pytest.mark.parametrize("width", [0, 0.2])
@pytest.mark.parametrize("fill", ["", "(fill no)"], ids=["omitted", "outline"])
def test_keepout_inside_printed_solid_or_outside_its_outline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], shape: str, width: float, fill: str
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_main

    coords = (
        "(pts (xy 115 115) (xy 145 115) (xy 145 145) (xy 115 145))"
        if shape == "poly"
        else "(center 130 130) (end 146 130)"
    )
    art = (
        f"(gr_{shape} {coords} (stroke (width {width}) (type solid)) "
        f'{fill} (layer "F.SilkS") (uuid "fb5658fe-0010-4050-9000-000000000001"))'
    )
    board = _board(tmp_path, _SILK_ZONE + art)
    original = board.read_bytes()
    # Pinned KiCad prints an omitted-fill polygon solid even at positive
    # width. Positive-width circles and explicitly unfilled shapes are outlines.
    solid = not fill and (shape == "poly" or width == 0)
    findings = check_silk_in_keepout(PCB.load(board)).violations
    assert len(findings) == int(solid)
    if solid:
        assert findings[0].actual_value == pytest.approx(400.0)
    unresolved = solid and shape == "circle"
    assert fix_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == int(unresolved)
    result = json.loads(capsys.readouterr().out)
    if solid and shape == "poly":
        (fix,) = result["keepout_clip_fixes"]
        assert fix["action"] == "trimmed"
        assert fix["new_area_mm2"] == pytest.approx(fix["old_area_mm2"] - 400, abs=0.1)
    else:
        assert board.read_bytes() == original
    assert len(check_silk_in_keepout(PCB.load(board)).violations) == int(unresolved)


def test_fix_silkscreen_all_surfaces_text_left_in_keepout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main

    board = _board(tmp_path, _SILK_ZONE + _FRONT_TEXT)

    code = fix_silkscreen_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"])
    report = json.loads(capsys.readouterr().out)

    assert code == 0  # text is reported, never silently moved or waived
    assert len(report["text_in_keepout"]) == 1
    assert any(u["kind"] == "silk_in_keepout" for u in report["unresolved"])


_NEGATED_DRU = """(version 1)
(rule "everywhere but the area"
  (layer "F.SilkS")
  (condition "!A.intersectsArea('no_ink')")
  (constraint disallow graphic))
"""

_INSIDE_LINE = (
    "  (gr_line (start 125 126) (end 129 126) (stroke (width 0.15) (type solid)) "
    '(layer "F.SilkS") (uuid "inside-line"))\n'
)
_OUTSIDE_LINE = (
    "  (gr_line (start 150 150) (end 154 150) (stroke (width 0.15) (type solid)) "
    '(layer "F.SilkS") (uuid "outside-line"))\n'
)


@pytest.mark.parametrize(
    ("zone_name", "ref", "forbidden"),
    [
        ("no_ink", "no_[i]*", False),
        ("no_[i]nk", "no_[i]*", True),
        ("no_ink", "aaaaaa*", False),
        ("no_ink", "aaaaaaaa-0000-0000-0000-000000000001", True),
        ("no_ink", "AAAAAAAA-0000-0000-0000-000000000001", True),
        ("bbbbbbbb-0000-0000-0000-000000000002", "bbbbbbbb-0000-0000-0000-000000000002", False),
        (" no_ink ", "no_ink", False),
        (" no_ink ", " no_ink ", True),
        ("", "*", True),
        ("no_ink", "no_in?", True),
        ("no_ink", "no_*", True),
    ],
)
def test_area_reference_selects_the_same_printed_ink_for_check_and_repair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], zone_name: str, ref: str, forbidden: bool
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main

    zone = _SILK_ZONE.replace("no_ink", zone_name)
    board = _board(tmp_path, zone + _INSIDE_LINE + _BACK_LINE, _DRU.replace("no_ink", ref))
    original = board.read_bytes()
    assert [v.items[0] for v in check_silk_in_keepout(PCB.load(board)).violations] == (
        ["gr_line {inside-line}"] if forbidden else []
    )
    assert fix_silkscreen_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_keepout_clip_fixed"] == int(forbidden)
    assert {line[3] for line in _gr_lines(board)} == (
        {"back-line"} if forbidden else {"inside-line", "back-line"}
    )
    if not forbidden:
        assert board.read_bytes() == original


@pytest.mark.parametrize("layer,kind", [("B.SilkS", "graphic"), ("F.SilkS", "text")])
def test_unresolved_exemption_preserves_repair_of_other_sides_and_kinds(
    tmp_path: Path, layer: str, kind: str
) -> None:
    rules = _DRU + (
        f'(rule "conditional exception" (layer "{layer}") (condition "A.Layer == \'{layer}\'") '
        f"(severity ignore) (constraint disallow {kind}))"
    )
    board = _board(tmp_path, _SILK_ZONE + _INSIDE_LINE + _BACK_LINE, rules)
    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    assert [name for name, _ in result.unsupported_rules] == ["conditional exception"]
    assert [(f.layer, f.action) for f in result.fixes] == [("F.SilkS", "removed")]
    repairer.save()
    assert {line[3] for line in _gr_lines(board)} == {"back-line"}


@pytest.mark.parametrize(
    ("type_pattern", "items"),
    [
        ("gRa*", {"LEGEND"}),
        ("t?xT", {"gr_line {inside-line}"}),
        ("Via", {"gr_line {inside-line}", "LEGEND"}),
        ("*", set()),
    ],
)
def test_item_type_selects_graphics_and_text_for_both_check_and_repair(
    tmp_path: Path, type_pattern: str, items: set[str]
) -> None:
    rules = _DRU + (
        f'(rule "typed exception" (condition "A.Type == \'{type_pattern}\'") '
        "(constraint disallow graphic text) (severity ignore))"
    )
    board = _board(tmp_path, _SILK_ZONE + _INSIDE_LINE + _FRONT_TEXT, rules)
    assert {v.items[0] for v in check_silk_in_keepout(PCB.load(board)).violations} == items
    preview = SilkscreenRepairer(board).repair_keepout_clearance(dry_run=True)
    assert preview.unsupported_rules == []
    assert preview.total_fixed == int("gr_line {inside-line}" in items)
    assert bool(preview.text_residual) == ("LEGEND" in items)


@pytest.mark.parametrize("ignore_last", [False, True])
@pytest.mark.parametrize(
    ("condition", "applies", "unknown"),
    [
        ("A.Type == 'Graphic'", True, False),
        ("A.Type == 'Pad'", False, False),
        ("A.intersectsArea('no_ink') && A.Type == 'Graphic'", True, False),
        ("A.Type == 'Graphic' && A.intersectsArea('elsewhere')", False, False),
        ("!A.intersectsArea('elsewhere')", True, True),
        ("A.Layer == 'F.SilkS'", True, True),
    ],
)
def test_conditional_exemption_keeps_priority_in_the_public_writer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    condition: str,
    applies: bool,
    unknown: bool,
    ignore_last: bool,
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main

    active = _DRU.removeprefix("(version 1)\n")
    ignored = (
        f'(rule "conditional exception" (layer "F.SilkS") (condition "{condition}") '
        "(severity ignore) (constraint disallow graphic))\n"
    )
    rules = "(version 1)\n" + (active + ignored if ignore_last else ignored + active)
    elsewhere = _SILK_ZONE.replace("no_ink", "elsewhere").replace("000000000001", "000000000002")
    elsewhere = elsewhere.replace("120", "145").replace("140", "155")
    board = _board(tmp_path, _SILK_ZONE + elsewhere + _INSIDE_LINE + _BACK_LINE, rules)
    original = board.read_bytes()
    forbidden = not (applies and ignore_last)
    assert [v.items[0] for v in check_silk_in_keepout(PCB.load(board)).violations] == (
        ["gr_line {inside-line}"] if forbidden else []
    )
    preview = SilkscreenRepairer(board).repair_keepout_clearance(dry_run=True)
    assert preview.total_fixed == int(forbidden)
    assert [name for name, _ in preview.unsupported_rules] == (
        ["conditional exception"] if unknown else []
    )
    assert board.read_bytes() == original
    assert fix_silkscreen_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_keepout_clip_fixed"] == int(forbidden)
    unresolved = [u for u in report["unresolved"] if u["kind"] == "silk_keepout_rule_unsupported"]
    assert [u["item"] for u in unresolved] == (["rule 'conditional exception'"] if unknown else [])
    if unknown:
        assert condition in unresolved[0]["detail"]
        assert "graphic on F.SilkS" in unresolved[0]["detail"]
    assert {line[3] for line in _gr_lines(board)} == (
        {"back-line"} if forbidden else {"inside-line", "back-line"}
    )
    if not forbidden:
        assert board.read_bytes() == original


@pytest.mark.parametrize(
    ("area_layers", "rule_layer", "forbidden"),
    [
        ('(layer "F.SilkS")', '(layer "F.SilkS")', {"front-line"}),
        ('(layer "B.SilkS")', '(layer "F.SilkS")', set()),
        ('(layer "F.Cu")', '(layer "F.SilkS")', set()),
        ('(layers "F.SilkS" "B.SilkS")', "", {"front-line", "back-line"}),
        ('(layers "*.SilkS")', "", {"front-line", "back-line"}),
        ('(layers "*.SilkS")', '(layer "B.SilkS")', {"back-line"}),
        ('(layers "F.SilkS" "B.SilkS")', '(layer "*.SilkS")', {"front-line", "back-line"}),
        ('(layers "F.SilkS" "B.SilkS")', '(layer "F.*")', {"front-line"}),
        ('(layers "F.SilkS" "B.SilkS")', '(layer "B.*")', {"back-line"}),
        ('(layers "F.SilkS" "B.SilkS")', '(layer "*.Mask")', set()),
        ('(layers "F.SilkS" "B.SilkS")', '(layer "?.Silkscreen")', {"front-line", "back-line"}),
        ('(layers "F.SilkS" "B.SilkS")', '(layer "[FB].SilkS")', set()),
    ],
)
def test_public_fix_clips_only_sides_shared_by_rule_and_area(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    area_layers: str,
    rule_layer: str,
    forbidden: set[str],
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main

    front = _INSIDE_LINE.replace("inside-line", "front-line")
    back = _INSIDE_LINE.replace("F.SilkS", "B.SilkS").replace("inside-line", "back-line")
    zone = _SILK_ZONE.replace('(layer "F.SilkS")', area_layers)
    board = _board(tmp_path, zone + front + back, dru=_DRU.replace('(layer "F.SilkS")', rule_layer))
    original = board.read_bytes()
    findings = check_silk_in_keepout(PCB.load(board)).violations
    assert {v.items[0] for v in findings} == {f"gr_line {{{uid}}}" for uid in forbidden}
    assert fix_silkscreen_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_keepout_clip_fixed"] == len(forbidden)
    assert {line[3] for line in _gr_lines(board)} == {"front-line", "back-line"} - forbidden
    if not forbidden:
        assert board.read_bytes() == original


@pytest.mark.parametrize(
    "clauses,forbidden",
    [
        ("(constraint disallow graphic) (severity ignore)", False),
        ("(severity ignore) (constraint disallow graphic)", False),
        ("(constraint track_width (min 0.15mm)) (constraint disallow graphic)", True),
        ("(constraint disallow graphic) (constraint track_width (min 0.15mm))", True),
        ("(constraint disallow graphic) (severity warning)", True),
        ("(constraint track_width (min 0.15mm)) (constraint disallow text)", False),
    ],
)
def test_public_fix_uses_active_disallow_constraints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], clauses: str, forbidden: bool
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main

    rules = _DRU.replace("(constraint disallow text graphic)", clauses)
    board = _board(tmp_path, _SILK_ZONE + _INSIDE_LINE + _OUTSIDE_LINE, dru=rules)
    before = _gr_lines(board)
    original_bytes = board.read_bytes()
    assert [v.severity for v in check_silk_in_keepout(PCB.load(board)).violations] == (
        ["error"] if forbidden else []
    )
    assert fix_silkscreen_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_keepout_clip_fixed"] == int(forbidden)
    assert _gr_lines(board) == (before[1:] if forbidden else before)
    if not forbidden:
        assert board.read_bytes() == original_bytes
    assert not check_silk_in_keepout(PCB.load(board)).violations


@pytest.mark.parametrize("ignore_scope", ["same_area", "overlapping_area", "board"])
@pytest.mark.parametrize("ignore_last", [True, False])
def test_ignored_rule_precedence_is_per_item_and_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ignore_scope: str, ignore_last: bool
) -> None:
    from kicad_tools.cli.fix_silkscreen_cmd import main as fix_silkscreen_main

    # The smaller area covers part of the line but none of the text.
    inner = (
        _SILK_ZONE.replace("no_ink", "exception")
        .replace("000000000001", "000000000002")
        .replace("120 120", "127 124")
        .replace("140 120", "131 124")
        .replace("140 140", "131 128")
        .replace("120 140", "127 128")
    )
    active = _DRU.removeprefix("(version 1)\n")
    ignored = active.replace("printed legend keepout", "allow graphics").replace(
        "(constraint disallow text graphic)", "(severity ignore) (constraint disallow graphic)"
    )
    if ignore_scope == "overlapping_area":
        ignored = ignored.replace("no_ink", "exception")
    elif ignore_scope == "board":
        ignored = ignored.replace("""  (condition "A.intersectsArea('no_ink')")\n""", "")
    rules = "(version 1)\n" + (active + ignored if ignore_last else ignored + active)
    board = _board(tmp_path, _SILK_ZONE + inner + _INSIDE_LINE + _OUTSIDE_LINE + _FRONT_TEXT, rules)
    before = _gr_lines(board)
    found = {v.items[0] for v in check_silk_in_keepout(PCB.load(board)).violations}
    assert found == ({"LEGEND"} if ignore_last else {"LEGEND", "gr_line {inside-line}"})
    residual_text = SilkscreenRepairer(board).repair_keepout_clearance(dry_run=True).text_residual
    assert len(residual_text) == 1 and "LEGEND" in residual_text[0]
    assert fix_silkscreen_main([str(board), "--all", "--mfr", "jlcpcb", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_keepout_clip_fixed"] == (0 if ignore_last else 1)
    assert _gr_lines(board) == (before if ignore_last else before[1:])
    assert [v.items[0] for v in check_silk_in_keepout(PCB.load(board)).violations] == ["LEGEND"]


def test_repeated_constraint_type_invalidates_the_sidecar(tmp_path: Path) -> None:
    invalid = _DRU.removeprefix("(version 1)\n").replace(
        "(constraint disallow text graphic)",
        "(constraint track_width (min 0.15mm)) (constraint track_width (max 0.45mm))",
    )
    board = _board(tmp_path, _SILK_ZONE + _INSIDE_LINE, dru=_DRU + invalid)
    unsupported = []
    assert not resolve_silk_keepouts(PCB.load(board), unsupported=unsupported)
    assert unsupported == ["printed legend keepout"]
    assert not SilkscreenRepairer(board).repair_keepout_clearance().fixes


def test_negated_condition_is_unsupported_not_a_positive_match(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _INSIDE_LINE + _OUTSIDE_LINE, dru=_NEGATED_DRU)
    pcb = PCB.load(board)

    unsupported: list[str] = []
    assert resolve_silk_keepouts(pcb, unsupported=unsupported) == []
    assert unsupported == ["everywhere but the area"]
    assert check_silk_in_keepout(pcb).violations == []

    repairer = SilkscreenRepairer(board)
    assert repairer.repair_keepout_clearance().total_fixed == 0
    repairer.save()
    assert {l[3] for l in _gr_lines(board)} == {"inside-line", "outside-line"}


def test_legalize_reports_unsupported_keepout_rule(tmp_path: Path) -> None:
    from kicad_tools.drc.repair_silkscreen import legalize_silk

    board = _board(tmp_path, _SILK_ZONE + _INSIDE_LINE + _OUTSIDE_LINE, dru=_NEGATED_DRU)
    report = legalize_silk(board, _jlcpcb(), dry_run=True)

    assert [u.kind for u in report.unresolved if "keepout" in u.kind] == [
        "silk_keepout_rule_unsupported"
    ]
    assert report.keepouts.total_fixed == 0


_OR_DRU = """(version 1)
(rule "either area"
  (layer "F.SilkS")
  (condition "A.intersectsArea('no_ink') || A.intersectsArea('elsewhere')")
  (constraint disallow graphic))
"""


def test_or_of_area_references_stays_supported(tmp_path: Path) -> None:
    board = _board(tmp_path, _SILK_ZONE + _FRONT_LINE, dru=_OR_DRU)
    unsupported: list[str] = []
    areas = resolve_silk_keepouts(PCB.load(board), unsupported=unsupported)
    assert unsupported == [] and len(areas) == 1


_TWO_SIDED_ZONE = """  (zone (net 0) (net_name "") (layers "F.SilkS" "B.SilkS") (name "no_ink") (hatch edge 0.5)
    (priority 0) (uuid "aaaaaaaa-0000-0000-0000-000000000001")
    (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
    (polygon (pts (xy 120 120) (xy 140 120) (xy 140 140) (xy 120 140))))
"""

_PAIRED_DRU = """(version 1)
(rule "front text only"
  (layer "F.SilkS")
  (condition "A.intersectsArea('no_ink')")
  (constraint disallow text))
(rule "back graphics only"
  (layer "B.SilkS")
  (condition "A.intersectsArea('no_ink')")
  (constraint disallow graphic))
"""


def test_two_rules_on_one_area_keep_side_kind_pairing(tmp_path: Path) -> None:
    body = _TWO_SIDED_ZONE + _FRONT_LINE + _BACK_LINE + _FRONT_TEXT + _back_text()
    board = _board(tmp_path, body, dru=_PAIRED_DRU)
    pcb = PCB.load(board)

    areas = resolve_silk_keepouts(pcb)
    assert len(areas) == 1
    assert areas[0].forbidden == frozenset(
        {("F", "text"), ("F", "text box"), ("F", "table"), ("B", "graphic")}
    )

    flagged = sorted(v.items[0] for v in check_silk_in_keepout(pcb).violations)
    assert len(flagged) == 2
    assert any(i.startswith("gr_line {back-line}") for i in flagged)
    assert any(i.startswith("LEGEND") for i in flagged)

    repairer = SilkscreenRepairer(board)
    result = repairer.repair_keepout_clearance()
    repairer.save()

    # Only the back line is clipped; the legal front line is untouched.
    assert [(f.layer, f.action) for f in result.fixes] == [("B.SilkS", "trimmed")]
    front = [l for l in _gr_lines(board) if l[0] == "F.SilkS"]
    assert [(l[1], l[2]) for l in front] == [((110.0, 130.0), (150.0, 130.0))]
    assert result.text_residual and all("LEGEND" in t for t in result.text_residual)
