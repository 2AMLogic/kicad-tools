"""Stroke-painted capacitor polarity marks and separate ink that crosses them."""

import math

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.silkscreen import check_silk_overlap


@pytest.mark.parametrize("extra", ["", "crossing", "duplicate", "foreign"])
def test_radial_sector_keeps_unrelated_overlaps(tmp_path, extra):
    stroke = '(stroke (width 0.15) (type solid)) (layer "F.SilkS")'

    def line(x, y, x2, y2):
        return f"(fp_line (start {x} {y}) (end {x2} {y2}) {stroke})"

    circle = f"(fp_circle (center 0 0) (end 2 0) {stroke} (fill no))"
    hatches = []
    for x in (0.30, 0.34, 0.38, 0.42, 0.46, 0.50):
        y = math.sqrt(2**2 - x**2)
        hatches.append(line(x, y, x, -y))
    graphics = [circle, *hatches]
    if extra == "crossing":
        graphics.append(line(-1, 0, 1, 0))
    if extra == "duplicate":
        graphics.append(hatches[0])
    other = ""
    if extra == "foreign":
        other = f'(footprint "Test:Mark" (layer "F.Cu") (at 10 10) (property "Reference" "X1" (at 8 8) (layer "F.Fab")) {line(-1, 0, 1, 0)})'
    board = tmp_path / "sector.kicad_pcb"
    board.write_text(f"""(kicad_pcb (version 20240108) (generator "test")
        (footprint "Capacitor_THT:CP_Radial_Test" (layer "F.Cu") (at 10 10)
            (property "Reference" "C1" (at 0 4) (layer "F.Fab"))
            {" ".join(graphics)}) {other})""")
    rules = get_profile("jlcpcb").get_design_rules(2)
    findings = check_silk_overlap(PCB.load(board), rules).violations
    assert bool(findings) == bool(extra)
    if extra == "foreign":
        assert all("X1" in v.message for v in findings)
    if extra == "duplicate":
        assert len(findings) == 1
