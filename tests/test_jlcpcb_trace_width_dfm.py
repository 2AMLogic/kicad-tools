"""JLCDFM trace-width warning for the jlcpcb 4-layer 1 oz stackup (Issue #10173).

JLCDFM rated 0.1016 mm outer-layer tracks Danger and 0.1524 mm Good, while the
published capability (and so the ``dimension_trace_width`` error floor) allows
0.1016 mm.  ``kct check --mfr jlcpcb`` must make that visible as a *warning*
without moving the error floor, flagging inner layers, or touching stackups with
no recorded evidence.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_tools.cli.check_cmd import _apply_dfm_table
from kicad_tools.manufacturers import get_profile
from kicad_tools.validate.rules.dimensions import DimensionRules


class _Net:
    def __init__(self, number: int, name: str):
        self.number = number
        self.name = name


class _Seg:
    def __init__(self, width: float, layer: str, net_number: int = 1):
        self.start = (0.0, 0.0)
        self.end = (5.0, 0.0)
        self.width = width
        self.layer = layer
        self.net_number = net_number


class _PCB:
    """Just enough PCB for ``DimensionRules`` (segments + net lookup)."""

    def __init__(self, segments):
        self.segments = segments
        self.vias = []
        self.pads = []
        self.footprints = []

    def get_net(self, number: int):
        return _Net(number, f"N{number}")


def _trace_findings(segments, layers: int = 4, oz: float = 1.0):
    rules = get_profile("jlcpcb").get_design_rules(layers, oz)
    results = DimensionRules().check(_PCB(segments), rules)
    return [v for v in results.violations if v.rule_id == "dimension_trace_width"]


def test_0_1016_outer_track_warns_and_0_1524_does_not():
    findings = _trace_findings([_Seg(0.1016, "F.Cu"), _Seg(0.1524, "F.Cu", net_number=2)])
    assert len(findings) == 1
    (v,) = findings
    assert v.severity == "warning"  # never an error: the published floor allows it
    assert v.actual_value == pytest.approx(0.1016)
    assert v.required_value == pytest.approx(0.1524)
    assert "JLCDFM" in v.message
    assert v.items == ("N1",)


def test_back_outer_layer_also_warns():
    findings = _trace_findings([_Seg(0.1016, "B.Cu")])
    assert [v.severity for v in findings] == ["warning"]


def test_inner_layer_track_is_not_flagged():
    """The evidence covers outer layers only."""
    assert _trace_findings([_Seg(0.1016, "In1.Cu"), _Seg(0.1016, "In2.Cu")]) == []


def test_width_exactly_at_warning_edge_is_clean():
    assert _trace_findings([_Seg(0.1524, "F.Cu")]) == []


def test_below_error_floor_stays_a_single_error():
    """A track under the published floor is the existing error, not also a warning."""
    findings = _trace_findings([_Seg(0.09, "F.Cu")])
    assert [v.severity for v in findings] == ["error"]


@pytest.mark.parametrize("layers", [2, 6])
def test_stackup_without_recorded_entry_gets_no_new_finding(layers):
    rules = get_profile("jlcpcb").get_design_rules(layers, 1.0)
    assert rules.trace_width_warning_mm is None
    # A track at the stackup's own floor passes exactly as before this rule.
    width = rules.min_trace_width_mm
    assert _trace_findings([_Seg(width, "F.Cu")], layers=layers) == []


def test_other_manufacturer_gets_no_warning():
    rules = get_profile("pcbway").get_design_rules(4, 1.0)
    assert rules.trace_width_warning_mm is None


def test_cli_dfm_table_path_carries_the_warning_width():
    """``kct check``'s ``_apply_dfm_table`` resolves the entry the rule reads."""
    checker = SimpleNamespace(
        pcb=SimpleNamespace(copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]),
        design_rules=get_profile("jlcpcb").get_design_rules(4, 1.0),
    )
    # Drop the warning to prove _apply_dfm_table (not get_design_rules) re-sets it.
    from dataclasses import replace

    checker.design_rules = replace(checker.design_rules, trace_width_warning_mm=None)
    cw: dict = {}
    assert _apply_dfm_table(checker, "jlcpcb", 4, 1.0, 0.5, 1.0, cw) is False
    assert checker.design_rules.trace_width_warning_mm == pytest.approx(0.1524)
    assert cw["dfm_table"]["thresholds"]["trace_width"]["warning_mm"] == pytest.approx(0.1524)
    assert cw["dfm_table"]["thresholds"]["trace_width"]["warning_basis"] == "observed_jlcdfm"


_ARC_PCB = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))
  (net 0 "") (net 1 "N1")
{arcs})
"""


def _arc_findings(tmp_path, specs):
    """Parse real KiCad ``(arc ...)`` tracks and run the width rule on them."""
    from kicad_tools.schema.pcb import PCB

    arcs = "".join(
        f'  (arc (start 0 0) (mid 1 0.5) (end 2 0) (width {w}) (layer "{layer}") (net 1))\n'
        for w, layer in specs
    )
    path = tmp_path / "arc.kicad_pcb"
    path.write_text(_ARC_PCB.format(arcs=arcs))
    pcb = PCB.load(path)
    assert len(pcb.arcs) == len(specs)
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    results = DimensionRules().check(pcb, rules)
    return [v for v in results.violations if v.rule_id == "dimension_trace_width"]


def test_outer_layer_arc_track_warns(tmp_path):
    findings = _arc_findings(tmp_path, [(0.1016, "F.Cu"), (0.1016, "B.Cu")])
    assert [v.severity for v in findings] == ["warning", "warning"]


def test_arc_track_controls_match_straight_tracks(tmp_path):
    findings = _arc_findings(
        tmp_path, [(0.1016, "In1.Cu"), (0.1016, "In2.Cu"), (0.1524, "F.Cu"), (0.09, "In1.Cu")]
    )
    assert [v.severity for v in findings] == ["error"]


@pytest.mark.parametrize(
    ("inner_thickness", "override", "expected_entry", "warns"),
    [
        (0.0175, None, "4layer_1oz", True),
        (0.035, None, "4layer_1oz_inner1oz", False),
        (0.07, None, None, False),
        (0.0175, 2, None, False),
        (0.07, 0.5, "4layer_1oz", True),
    ],
)
def test_declared_stackup_drives_python_and_cli_warnings(
    tmp_path, capsys, inner_thickness, override, expected_entry, warns
):
    import json

    from kicad_tools.cli.check_cmd import main as check
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    copper = " ".join(
        f'(layer "{layer}" (type "copper") (thickness {thickness}))'
        for layer, thickness in (
            ("F.Cu", 0.035),
            ("In1.Cu", inner_thickness),
            ("In2.Cu", inner_thickness),
            ("B.Cu", 0.035),
        )
    )
    board = tmp_path / "controller.kicad_pcb"
    board.write_text(
        _ARC_PCB.format(
            arcs=f"(setup (stackup {copper}))\n"
            '(segment (start 0 0) (end 2 0) (width 0.15) (layer "F.Cu") (net 1))'
        )
    )
    checker = DRCChecker(PCB.load(board), manufacturer="jlcpcb", copper_oz_inner=override)
    python_rows = checker.check_dimensions().violations
    assert any(v.rule_id == "dimension_trace_width" for v in python_rows) is warns

    args = [str(board), "--mfr", "jlcpcb", "--only", "dimensions", "--drc-only", "--format", "json"]
    if override is not None:
        args.extend(["--copper", f"outer=1,inner={override}"])
    check(args)
    report = json.loads(capsys.readouterr().out)
    assert report["copper_weights"]["dfm_table"]["entry"] == expected_entry
    assert (report["verdict"] == "incomplete") is (expected_entry is None)
    assert any(v["rule_id"] == "dimension_trace_width" for v in report["violations"]) is warns


def test_default_checker_uses_the_boards_layer_count(tmp_path):
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    board = tmp_path / "two-layer.kicad_pcb"
    board.write_text(
        _ARC_PCB.format(arcs="").replace(' (1 "In1.Cu" signal) (2 "In2.Cu" signal)', "")
    )
    assert DRCChecker(PCB.load(board)).design_rules.trace_width_warning_mm is None


def test_exported_profile_keeps_its_measured_warning(capsys):
    import json

    from kicad_tools.cli.mfr import main as mfr
    from kicad_tools.manufacturers import DesignRules

    rules = get_profile("jlcpcb").get_design_rules(4, 1)
    mfr(["rules", "jlcpcb", "--layers", "4", "--copper", "1", "--format", "json"])
    cli_rules = json.loads(capsys.readouterr().out)["rules"]
    board = _PCB([_Seg(0.15, "F.Cu")])
    for exported in (rules.to_dict(), cli_rules):
        restored = DesignRules(**exported)
        (finding,) = DimensionRules().check(board, restored).violations
        assert restored.trace_width_warning_mm == pytest.approx(0.1524)
        assert finding.severity == "warning"
        assert "0.1016 mm Danger" in finding.message
        assert "0.1524 mm Good" in finding.message


def test_uncovered_outer_weight_preserves_preset_floors_without_the_advisory():
    rules = get_profile("jlcpcb").get_design_rules(4, 3)
    assert rules.min_clearance_mm == pytest.approx(0.1016)
    assert rules.trace_width_warning_mm is None
