"""JLCPCB grades silk over component drills as an error and via drills as info."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import DesignRules, get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap  # noqa: E402

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (44 "Edge.Cuts" user))
  (net 0 "")
  (setup (tenting front back))
"""

_VIA = """  (via (at 20 20) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0))
"""

_NPTH = """  (footprint "X:M" (layer "F.Cu") (at 30 20)
    (property "Reference" "MH1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" np_thru_hole circle (at 0 0) (size 1.6 1.6) (drill 1.0)
      (layers "*.Cu" "*.Mask")))
"""


def _silk(x: float, layer: str = "F.SilkS") -> str:
    return (
        f"  (gr_line (start {x} 18) (end {x} 22) (stroke (width 0.15) (type solid)) "
        f'(layer "{layer}"))\n'
    )


def _load(tmp_path: Path, body: str) -> PCB:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    return PCB.load(path)


def _jlc():
    return get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1)


def _other():
    return get_profile("pcbway").get_design_rules(layers=2, copper_oz=1)


def test_jlcpcb_profile_wires_the_band():
    """Every stackup of both jlcpcb profiles carries the 0.0 danger band."""
    for mfr in ("jlcpcb", "jlcpcb-tier1"):
        profile = get_profile(mfr)
        rules = profile.get_design_rules(layers=2, copper_oz=1)
        assert rules.silk_to_hole_danger_mm == 0.0, mfr
        deeper = profile.get_design_rules(layers=4, copper_oz=1)
        assert deeper is not None and deeper.silk_to_hole_danger_mm == 0.0, mfr
    assert _other().silk_to_hole_danger_mm is None


@pytest.mark.parametrize("profile", ["jlcpcb", "jlcpcb-tier1"])
@pytest.mark.parametrize("tented", [True, False])
def test_via_and_component_drills_are_graded_separately(tmp_path, profile, tented):
    via = _VIA.replace(
        "(net 0)",
        f"(tenting (front {'yes' if tented else 'no'}) (back {'yes' if tented else 'no'})) (net 0)",
    )
    pth = (
        _NPTH.replace("np_thru_hole", "thru_hole")
        .replace('"MH1"', '"J1"')
        .replace("(at 30 20)", "(at 40 20)")
    )
    pcb = _load(tmp_path, via + _NPTH + pth + _silk(20) + _silk(30) + _silk(40, "B.SilkS"))
    rules = get_profile(profile).get_design_rules(layers=2, copper_oz=1)
    findings = check_silk_hole_overlap(pcb, rules).violations
    assert {(v.items[1], v.layer, v.severity) for v in findings} == {
        ("via at (20.000, 20.000)", "F.SilkS", "info"),
        ("MH1 pad 1 hole", "F.SilkS", "error"),
        ("J1 pad 1 hole", "B.SilkS", "error"),
    }
    assert all(v.rule_id == "silk_hole_overlap" and v.required_value == 0.0 for v in findings)


def test_silk_over_via_is_warning_under_other_manufacturer(tmp_path):
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)), _other())
    assert [v.severity for v in results.violations] == ["warning"]


def test_none_field_keeps_warning(tmp_path):
    rules = dataclasses.replace(_jlc(), silk_to_hole_danger_mm=None)
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)), rules)
    assert [v.severity for v in results.violations] == ["warning"]


def test_bare_design_rules_keep_warning(tmp_path):
    rules = DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=0.1,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.4,
        min_annular_ring_mm=0.05,
    )
    assert rules.silk_to_hole_danger_mm is None
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)), rules)
    assert [v.severity for v in results.violations] == ["warning"]


def test_npth_hole_is_error_under_jlcpcb(tmp_path):
    results = check_silk_hole_overlap(_load(tmp_path, _NPTH + _silk(30.0)), _jlc())
    assert [v.severity for v in results.violations] == ["error"]
    assert "MH1 pad 1" in results.violations[0].items[1]


def test_back_side_silk_over_via_is_info(tmp_path):
    """Holes pierce every layer, so B.SilkS ink over a drill is flagged too."""
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0, layer="B.SilkS")), _jlc())
    assert [v.severity for v in results.violations] == ["info"]


def test_silk_clear_of_holes_stays_clean(tmp_path):
    assert not check_silk_hole_overlap(
        _load(tmp_path, _VIA + _NPTH + _silk(25.0)), _jlc()
    ).violations


def test_positive_band_buffers_the_component_drill(tmp_path):
    rules = dataclasses.replace(_jlc(), silk_to_hole_danger_mm=0.3)
    # 0.5 mm drill radius + 0.075 mm ink radius + 0.125 mm of air.
    pcb = _load(tmp_path, _NPTH + _silk(30.7))
    results = check_silk_hole_overlap(pcb, rules)
    assert [v.severity for v in results.violations] == ["error"]
    assert results.violations[0].required_value == 0.3
    assert not check_silk_hole_overlap(pcb, _jlc()).violations


def test_no_profile_keeps_the_advisory_geometry_query(tmp_path):
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)))
    assert [v.severity for v in results.violations] == ["warning"]
