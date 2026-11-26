"""Stroke-painted polarity sectors and independent ink that intersects them."""

import math
import uuid

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.silkscreen import (
    check_silk_over_copper,
    check_silk_over_mask_opening,
    check_silk_overlap,
)

STROKE = '(stroke (width 0.15) (type solid)) (layer "F.SilkS")'
OFFSETS = (0.30, 0.34, 0.38, 0.42, 0.46, 0.50)


def line(start, end, item_id=None):
    item_id = item_id or uuid.uuid4()
    return (
        f"(fp_line (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) {STROKE} (uuid {item_id}))"
    )


def chord(offset, horizontal=False, item_id=None, angle=0):
    reach = math.sqrt(2**2 - offset**2)
    if horizontal:
        return line((-reach, offset), (reach, offset), item_id)
    if angle:
        cos, sin = math.cos(math.radians(angle)), math.sin(math.radians(angle))

        def rotate(y):
            return (round(offset * cos - y * sin, 6), round(offset * sin + y * cos, 6))

        return line(rotate(-reach), rotate(reach), item_id)
    return line((offset, -reach), (offset, reach), item_id)


def board(
    tmp_path,
    *,
    graphics="",
    offsets=OFFSETS,
    arc=False,
    horizontal=False,
    rotation=0,
    chord_angle=0,
    ref_layer="F.Fab",
    other="",
):
    perimeter = (
        f"(fp_arc (start 0 -2) (mid 2 0) (end 0 2) {STROKE})"
        if arc
        else f"(fp_circle (center 0 0) (end 2 0) {STROKE} (fill no))"
    )
    path = tmp_path / "sector.kicad_pcb"
    path.write_text(f"""(kicad_pcb (version 20240108) (generator "test")
        (footprint "ProjectMark" (layer "F.Cu") (at 10 10 {rotation})
            (property "Reference" "C1" (at 0.4 0) (layer "{ref_layer}")
              (effects (font (size 1 1) (thickness 0.15))))
            {perimeter} {" ".join(chord(x, horizontal, angle=chord_angle) for x in offsets)} {graphics}) {other})""")
    return PCB.load(path)


@pytest.fixture
def rules():
    return get_profile("jlcpcb").get_design_rules(2)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"rotation": 45},
        {"chord_angle": 30},
        {"chord_angle": 45},
        {"arc": True},
        {"horizontal": True, "offsets": tuple(-x for x in OFFSETS)},
    ],
)
def test_sector_has_no_internal_pairs(tmp_path, rules, options):
    assert not check_silk_overlap(board(tmp_path, **options), rules).violations


@pytest.mark.parametrize(
    "extra", ["crossing", "duplicate", "off_pitch", "overdraw", "tick", "on_pitch_tick", "foreign"]
)
def test_independent_stroke_is_identified(tmp_path, rules, extra):
    item_id = str(uuid.uuid4())
    graphics = {
        "crossing": line((-1, 0), (1, 0), item_id),
        "duplicate": chord(OFFSETS[0], item_id=item_id),
        "off_pitch": chord(0.37, item_id=item_id),
        "overdraw": line((0.38, 0.5), (0.38, math.sqrt(4 - 0.38**2)), item_id),
        "tick": line((0.1, 1.6), (0.1, math.sqrt(3.99)), item_id),
        "on_pitch_tick": line((0.26, 1.68), (0.26, math.sqrt(4 - 0.26**2)), item_id),
        "foreign": "",
    }[extra]
    other = ""
    if extra == "foreign":
        other = f"""(footprint "Test:Mark" (layer "F.Cu") (at 10 10)
            (property "Reference" "X1" (at 8 8) (layer "F.Fab"))
            {line((-1, 0), (1, 0), item_id)})"""
    findings = check_silk_overlap(board(tmp_path, graphics=graphics, other=other), rules).violations
    identified = [v for v in findings if any(item_id in item for item in v.items)]
    assert identified
    if extra != "overdraw":  # both partially overdrawn segments are ambiguous
        assert identified == findings
    if extra == "duplicate":
        assert len(findings) == 1
    if extra == "foreign":
        assert all("X1" in v.message and "C1" in v.message for v in findings)


@pytest.mark.parametrize("with_pad", [True, False])
def test_partial_chords_need_a_pad_at_the_cut_edge(tmp_path, rules, with_pad):
    # The clearance around this pad extends through the top of the circle.
    graphics = " ".join(line((x, -math.sqrt(4 - x * x)), (x, 0.8)) for x in OFFSETS)
    if with_pad:
        graphics += '(pad "1" smd rect (at 0.4 1.5) (size 1 1) (layers "F.Cu" "F.Mask"))'
    pcb = board(tmp_path, offsets=(), graphics=graphics)
    assert bool(check_silk_overlap(pcb, rules).violations) == (not with_pad)


def test_reference_on_sector(tmp_path, rules):
    findings = check_silk_overlap(board(tmp_path, ref_layer="F.SilkS"), rules).violations
    assert findings and all("C1 (reference)" in v.items for v in findings)


def test_isolated_parallel_pair(tmp_path, rules):
    findings = check_silk_overlap(board(tmp_path, offsets=OFFSETS[:2]), rules).violations
    assert any(all("fp_line" in item for item in v.items) for v in findings)


def test_sector_crosses_exposed_pad_and_mask_graphic(tmp_path, rules):
    pcb = board(
        tmp_path,
        graphics="""
        (pad "1" smd rect (at 0.4 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask"))
        (fp_rect (start 0.2 0.8) (end 0.6 1.2)
            (stroke (width 0) (type solid)) (fill solid) (layer "F.Mask"))""",
    )
    for check in (check_silk_over_copper, check_silk_over_mask_opening):
        findings = check(pcb, rules).violations
        assert any("C1 (fp_line)" in v.items for v in findings)


@pytest.mark.parametrize("face", ["F", "B"])
@pytest.mark.parametrize(
    "case",
    ["plus", "shift", "unequal", "wrong_pad", "third", "no_pads", "far", "inside", "oblique"],
)
def test_radial_positive_lead_mark(tmp_path, rules, face, case):
    x = 2.25 if case == "wrong_pad" else -2.25
    y = 1.1
    width = 0.35
    if case == "far":
        x, y = x * 1.5, y * 1.5
    if case == "inside":
        x, y = -1.0, 1.0
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    graphics = line((x - width, y), (x + width, y), first)
    if case == "shift":
        x += 0.04
    if case == "unequal":
        width -= 0.08
    tilt = math.radians(20 if case == "oblique" else 0)
    dx, dy = width * math.sin(tilt), width * math.cos(tilt)
    graphics += line((x - dx, y - dy), (x + dx, y + dy), second)
    if case != "no_pads":
        graphics += """
          (pad "1" thru_hole rect (at -0.75 0) (size 0.6 0.6) (drill 0.3) (layers "*.Cu" "*.Mask"))
          (pad "2" thru_hole circle (at 0.75 0) (size 0.6 0.6) (drill 0.3) (layers "*.Cu" "*.Mask"))
        """
    extra_id = str(uuid.uuid4())
    if case == "third":
        graphics += line((x - 0.18, y - 0.18), (x + 0.18, y + 0.18), extra_id)
    board(tmp_path, graphics=graphics, rotation=38)
    path = tmp_path / "sector.kicad_pcb"
    if face == "B":
        path.write_text(path.read_text().replace('"F.', '"B.'))
    findings = check_silk_overlap(PCB.load(path), rules).violations
    if case == "third":
        assert len(findings) == 2
        assert all(any(extra_id in item for item in finding.items) for finding in findings)
    elif case in ("far", "inside", "oblique"):
        assert math.dist((x, y), (-0.75, 0)) < math.dist((x, y), (0.75, 0))
        assert len(findings) == 1
        assert any(first in item for item in findings[0].items)
        assert any(second in item for item in findings[0].items)
    else:
        assert len(findings) == (0 if case == "plus" else 1)
