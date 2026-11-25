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
