"""Two-tier silk-to-pad bands and silk-to-hole overlap (Issue #8844)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.factory_clearance import check_silk_pad_clearance  # noqa: E402
from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap  # noqa: E402

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (44 "Edge.Cuts" user))
  (net 0 "")
"""
_SETUP = """  (setup (tenting front back))
"""


def _pad_fp(silk_lines: str) -> str:
    return f"""  (footprint "X:Y" (layer "F.Cu") (at 10 10)
    (property "Reference" "R1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    {silk_lines}
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask" "F.Paste")))
"""


def _vline(x: float, layer: str = "F.SilkS", width: float = 0.15) -> str:
    # absolute coords inside a footprint at (10, 10): fp_line is relative
    return (
        f"(fp_line (start {x} -2) (end {x} 2) (stroke (width {width}) (type solid)) "
        f'(layer "{layer}"))'
    )


def _load(tmp_path: Path, body: str) -> PCB:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_HEAD + _SETUP + body + ")\n")
    return PCB.load(path)


def _rules():
    return get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1)


def _gap_x(gap: float, width: float = 0.15) -> float:
    # pad edge at +0.5; stroke half-width is width/2
    return 0.5 + gap + width / 2


@pytest.mark.parametrize(
    "gap,expected",
    [(0.06, ("error",)), (0.15, ("warning",)), (0.30, ())],
)
def test_two_tier_bands(tmp_path, gap, expected):
    pcb = _load(tmp_path, _pad_fp(_vline(_gap_x(gap))))
    results = check_silk_pad_clearance(pcb, _rules())
    assert tuple(v.severity for v in results.violations) == expected
    assert all(v.rule_id == "silk_pad_clearance" for v in results.violations)


def test_band_edges_inclusive(tmp_path):
    for gap, sev in ((0.12, "error"), (0.18, "warning")):
        pcb = _load(tmp_path, _pad_fp(_vline(_gap_x(gap))))
        got = [v.severity for v in check_silk_pad_clearance(pcb, _rules()).violations]
        assert got == [sev], gap


def test_back_silk_does_not_hit_front_pad(tmp_path):
    pcb = _load(tmp_path, _pad_fp(_vline(_gap_x(0.06), layer="B.SilkS")))
    assert not check_silk_pad_clearance(pcb, _rules()).violations


def test_legacy_single_tier_unchanged(tmp_path):
    from dataclasses import replace

    rules = replace(_rules(), silk_to_pad_danger_mm=None, silk_to_pad_warning_mm=None)
    pcb = _load(tmp_path, _pad_fp(_vline(_gap_x(0.10))))
    got = check_silk_pad_clearance(pcb, rules).violations
    assert [v.severity for v in got] == ["error"]
    pcb = _load(tmp_path, _pad_fp(_vline(_gap_x(0.16))))
    assert not check_silk_pad_clearance(pcb, rules).violations


def _hole_board(tmp_path: Path, silk_x: float) -> PCB:
    body = f"""  (via (at 20 20) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0))
  (footprint "X:H" (layer "F.Cu") (at 30 20)
    (property "Reference" "J1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 1.0)
      (layers "*.Cu" "*.Mask")))
  (gr_line (start {silk_x} 18) (end {silk_x} 22) (stroke (width 0.15) (type solid))
    (layer "F.SilkS"))
"""
    return _load(tmp_path, body)


def test_silk_over_tented_via_drill(tmp_path):
    results = check_silk_hole_overlap(_hole_board(tmp_path, 20.0), _rules())
    assert [v.rule_id for v in results.violations] == ["silk_hole_overlap"]
    assert "via" in results.violations[0].items[1]


def test_silk_over_pth_drill(tmp_path):
    results = check_silk_hole_overlap(_hole_board(tmp_path, 30.0), _rules())
    assert [v.rule_id for v in results.violations] == ["silk_hole_overlap"]
    assert "J1 pad 1" in results.violations[0].items[1]


def test_silk_clear_of_holes(tmp_path):
    assert not check_silk_hole_overlap(_hole_board(tmp_path, 25.0), _rules()).violations


def test_registered_in_check_all(tmp_path):
    """``check_all_silkscreen`` runs the rule, and every table names it."""
    from kicad_tools.cli.readiness_cmd import ASSEMBLY_AFFECTING_RULES
    from kicad_tools.validate.checker import DRCChecker
    from kicad_tools.validate.rules.silkscreen import check_all_silkscreen

    results = check_all_silkscreen(_hole_board(tmp_path, 20.0), _rules())
    assert "silk_hole_overlap" in {v.rule_id for v in results.violations}
    # Explicit entry: unknown ids fall into the fab-blocking bucket by default.
    assert "silk_hole_overlap" in DRCChecker.RULE_CATEGORY
    assert "silk_hole_overlap" in ASSEMBLY_AFFECTING_RULES


def test_thin_non_text_silk_graphic_flagged(tmp_path):
    from kicad_tools.validate.rules.silkscreen import check_silkscreen_line_width

    pcb = _load(tmp_path, _pad_fp(_vline(3.0, width=0.10)))
    found = check_silkscreen_line_width(pcb, _rules()).violations
    assert [v.rule_id for v in found] == ["silkscreen_line_width"]


def _margin_board(setup_margin: str | None, fp_margin: float | None) -> str:
    """Two concentric pad/ring pairs: an inheriting pad and a +0.05 mm control.

    A 0.15 mm ink ring of radius 1.76 mm around a 2.80 mm round pad leaves
    ``0.285 - margin`` mm between the ink and the mask aperture, so the
    printed gap follows the effective mask margin directly (Issue #10326).
    """
    setup = "(setup (tenting front back))"
    if setup_margin is not None:
        setup = f"(setup (pad_to_mask_clearance {setup_margin}) (tenting front back))"
    fp_margin_node = f"(solder_mask_margin {fp_margin})" if fp_margin is not None else ""
    body = f"""  (footprint "X:C" (layer "F.Cu") (at 10 10)
    (property "Reference" "J1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    {fp_margin_node}
    (fp_circle (center 0 0) (end 1.76 0) (stroke (width 0.15) (type solid))
      (layer "F.SilkS"))
    (fp_circle (center 5 0) (end 6.76 0) (stroke (width 0.15) (type solid))
      (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 2.8 2.8) (drill 1.7)
      (layers "*.Cu" "*.Mask"))
    (pad "2" thru_hole circle (at 5 0) (size 2.8 2.8) (drill 1.7)
      (layers "*.Cu" "*.Mask") (solder_mask_margin 0.05)))
"""
    return _HEAD + setup + "\n" + body + ")\n"


def _mfr_check_json(tmp_path: Path, capsys, board_text: str) -> dict:
    """Run the reported public JLCPCB check and return its JSON report."""
    import json

    from kicad_tools.cli.check_cmd import main

    path = tmp_path / "b.kicad_pcb"
    path.write_text(board_text)
    main([str(path), "--drc-only", "--format", "json", "--mfr", "jlcpcb"])
    out = capsys.readouterr().out
    return json.loads(out[out.index("{") :])


@pytest.mark.parametrize(
    ("setup_margin", "fp_margin", "expected_gap"),
    [("0", 0.22, 0.065), ("0.22", None, 0.065), ("0.40", 0.05, None)],
)
def test_inherited_mask_margin_reaches_silk_clearance(
    tmp_path, capsys, setup_margin, fp_margin, expected_gap
):
    """Issue #10326: the aperture margin resolves pad, then footprint, then board.

    A 0.065 mm gap must trip the 0.12 mm error band whether the +0.22 mm
    margin is the footprint's (no local override, board at zero) or the
    board's (footprint unset). The +0.05 mm footprint override of a
    +0.40 mm board margin yields a 0.235 mm gap and must clear the rule,
    and the explicit +0.05 mm control pad stays clear in every row.
    """
    data = _mfr_check_json(tmp_path, capsys, _margin_board(setup_margin, fp_margin))
    findings = [v for v in data["violations"] if v["rule_id"] == "silk_pad_clearance"]
    inherited = [v for v in findings if "J1-1" in v["items"]]
    assert not [v for v in findings if "J1-2" in v["items"]]
    if expected_gap is None:
        assert not inherited
    else:
        assert len(inherited) == 1
        assert inherited[0]["severity"] == "error"
        assert inherited[0]["actual_value"] > 0
        assert inherited[0]["actual_value"] == pytest.approx(expected_gap, abs=0.001)
        assert inherited[0]["required_value"] == pytest.approx(0.12)
