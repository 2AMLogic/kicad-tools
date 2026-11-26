"""Tests for the schematic-association-driven ``bypass_distance`` rule (issue #10141).

Fixture: two ICs on ONE rail (``+3V3``).  The schematic draws C1 beside U1 and
C2 beside U2 (``tests/test_decoupling_association.make_sheet``).  The PCB places
the parts either correctly (each cap next to its own IC) or swapped (each cap
next to the *other* IC) -- the case nearest-pad pairing could not see.

PCB layout (mm): U1 pin 1 at (20, 10), U2 pin 1 at (60, 10); a cap placed at
``x`` has its supply pad (pad 1) at ``x - 0.5``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.explain.checks.bypass import BypassCapDistanceCheck
from kicad_tools.explain.mistakes import CheckIncomplete
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.bypass_distance import (
    BYPASS_DISTANCE_RULE_ID,
    BYPASS_UNASSOCIATED_RULE_ID,
    BypassDistanceRule,
    measure_bypass_pairs,
)

from .test_decoupling_association import make_sheet

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "GND")
"""


def _ic(ref: str, x: float) -> str:
    return f"""
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" (layer "F.Cu") (at {x} 10)
    (property "Reference" "{ref}") (property "Value" "IC")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "+3V3"))
    (pad "2" smd rect (at 1.27 0) (size 1 1) (layers "F.Cu") (net 2 "GND")))
"""


def _cap(ref: str, x: float, layer: str = "F.Cu") -> str:
    return f"""
  (footprint "Capacitor_SMD:C_0402" (layer "{layer}") (at {x} 10)
    (property "Reference" "{ref}") (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "{layer}") (net 1 "+3V3"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "{layer}") (net 2 "GND")))
"""


def _board(
    tmp_path: Path,
    *,
    c1: float = 21.5,
    c2: float = 61.5,
    c1_layer: str = "F.Cu",
    override: str | None = None,
    with_schematic: bool = True,
) -> Path:
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text(
        _HEADER + _ic("U1", 20) + _ic("U2", 60) + _cap("C1", c1, c1_layer) + _cap("C2", c2) + ")\n"
    )
    if with_schematic:
        make_sheet(
            tmp_path / "board.kicad_sch",
            [
                ("U1", 50.8, 50.8, "+3V3", None),
                ("U2", 101.6, 50.8, "+3V3", None),
                ("C1", 55.88, 50.8, "+3V3", override),
                ("C2", 106.68, 50.8, "+3V3", None),
            ],
            [(50.8, 50.8, 55.88, 50.8), (101.6, 50.8, 106.68, 50.8)],
        )
    return pcb_path


def _run(pcb_path: Path, **kwargs):
    pcb = PCB.load(str(pcb_path))
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
    return checker.check_bypass_distance(
        schematic_path=pcb_path.with_suffix(".kicad_sch"), **kwargs
    )


def _by_rule(results, rule_id):
    return [v for v in results.violations if v.rule_id == rule_id]


class TestRule:
    def test_correct_caps_are_clean(self, tmp_path):
        results = _run(_board(tmp_path))
        assert results.violations == []

    def test_swapped_caps_are_reported_against_their_designated_ic(self, tmp_path):
        # C1 sits beside U2 and C2 beside U1: nearest-pad pairing calls this clean.
        results = _run(_board(tmp_path, c1=61.5, c2=21.5))
        found = _by_rule(results, BYPASS_DISTANCE_RULE_ID)
        assert sorted(v.items for v in found) == [("C1", "U1"), ("C2", "U2")]
        assert all(v.severity == "warning" for v in found)
        assert sorted(v.actual_value for v in found) == [pytest.approx(39.0), pytest.approx(41.0)]
        assert all(v.required_value == 3.0 for v in found)
        assert all(v.nets == ("+3V3",) for v in found)

    def test_distance_is_pad_to_pad_not_origin_to_origin(self, tmp_path):
        pcb = PCB.load(str(_board(tmp_path)))
        rows = {r.cap: r for r in measure_bypass_pairs(pcb, tmp_path / "board.kicad_sch")}
        assert rows["C1"].distance_mm == pytest.approx(1.0)
        assert rows["C2"].distance_mm == pytest.approx(1.0)

    @pytest.mark.parametrize("near_first", [False, True])
    def test_repeated_supply_lands_use_nearest_copper_pair(self, tmp_path, near_first):
        board = _board(tmp_path, c1=30.5)
        text = board.read_text()
        # U1 at x=20 and C1 at x=30.5. Their closest lands are at x=26 and 28.
        lead = '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "+3V3"))'
        tab = lead.replace("(at 0 0)", "(at 6 0)")
        mask = lead.replace("(at 0 0)", "(at 8 0)").replace("F.Cu", "F.Mask")
        cap = '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "+3V3"))'
        other = cap.replace("(at -0.5 0)", "(at -2.5 0)")
        text = text.replace(lead, mask + (tab + lead if near_first else lead + tab), 1)
        board.write_text(text.replace(cap, other + cap if near_first else cap + other, 1))
        rows = measure_bypass_pairs(PCB.load(str(board)), board.with_suffix(".kicad_sch"))
        row = next(r for r in rows if r.cap == "C1")
        assert row.distance_mm == pytest.approx(2)
        assert row.cap_position == pytest.approx((28, 10))
        assert not _run(board).violations

    def test_max_distance_is_configurable(self, tmp_path):
        board = _board(tmp_path, c1=24.5)  # C1 supply pad at 24.0 -> 4.0mm from U1 pin 1
        assert _by_rule(_run(board), BYPASS_DISTANCE_RULE_ID)  # default 3.0
        assert not _by_rule(_run(board, max_distance_mm=4.5), BYPASS_DISTANCE_RULE_ID)
        assert [
            v.items for v in _by_rule(_run(board, max_distance_mm=0.5), BYPASS_DISTANCE_RULE_ID)
        ]

    @pytest.mark.parametrize("bad", [0, -1.0])
    def test_non_positive_max_distance_is_rejected(self, bad):
        with pytest.raises(ValueError):
            BypassDistanceRule(None, max_distance_mm=bad)

    def test_unassociated_cap_is_a_distinct_info_finding(self, tmp_path):
        results = _run(_board(tmp_path, override="U9.1"))
        assert not _by_rule(results, BYPASS_DISTANCE_RULE_ID)
        found = _by_rule(results, BYPASS_UNASSOCIATED_RULE_ID)
        assert [v.items for v in found] == [("C1",)]
        assert found[0].severity == "info"
        assert "NOT checked" in found[0].message
        assert "U9.1" in found[0].message

    def test_cap_on_opposite_side_from_pin_is_measured_and_labelled(self, tmp_path):
        # Directly under U1 pin 1 on the back side is in range: no finding.
        assert not _run(_board(tmp_path, c1=20.5, c1_layer="B.Cu")).violations
        found = _by_rule(_run(_board(tmp_path, c1=30.5, c1_layer="B.Cu")), BYPASS_DISTANCE_RULE_ID)
        assert [v.items for v in found] == [("C1", "U1")]
        assert "opposite board side" in found[0].message

    def test_cap_missing_from_pcb_is_not_a_silent_pass(self, tmp_path):
        board = _board(tmp_path)
        board.write_text(board.read_text().replace('"Reference" "C1"', '"Reference" "C1X"'))
        found = _by_rule(_run(board), BYPASS_UNASSOCIATED_RULE_ID)
        assert [v.items for v in found] == [("C1",)]
        assert "could not be measured" in found[0].message

    def test_no_schematic_means_not_run(self, tmp_path):
        pcb = PCB.load(str(_board(tmp_path, with_schematic=False)))
        results = DRCChecker(pcb, manufacturer="jlcpcb", layers=2).check_bypass_distance()
        assert results.violations == []
        assert results.rules_checked == 0

    def test_unreadable_schematic_is_visible(self, tmp_path):
        board = _board(tmp_path)
        board.with_suffix(".kicad_sch").write_text("(not a schematic")
        found = _by_rule(_run(board), BYPASS_UNASSOCIATED_RULE_ID)
        assert len(found) == 1 and "NOT checked" in found[0].message


class TestRegistration:
    def test_both_rule_ids_are_advisory(self):
        for rule_id in (BYPASS_DISTANCE_RULE_ID, BYPASS_UNASSOCIATED_RULE_ID):
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_ADVISORY

    def test_category_is_selectable(self):
        assert "bypass_distance" in check_cmd.CHECK_CATEGORIES
        assert "check_bypass_distance" in DRCChecker.CHECK_ALL_METHODS


class TestCLI:
    def _json(self, capsys, board, *extra):
        rc = check_cmd.main([str(board), "--only", "bypass_distance", "--format", "json", *extra])
        data = json.loads(capsys.readouterr().out)
        found = [v for v in data["violations"] if v["rule_id"] == BYPASS_DISTANCE_RULE_ID]
        return rc, data, found

    def test_swapped_caps_reported_and_correct_caps_clean(self, tmp_path, capsys):
        _, _, found = self._json(capsys, _board(tmp_path, c1=61.5, c2=21.5))
        assert sorted(tuple(v["items"]) for v in found) == [("C1", "U1"), ("C2", "U2")]
        clean = tmp_path / "clean"
        clean.mkdir()
        _, _, found = self._json(capsys, _board(clean))
        assert found == []

    def test_cli_max_distance_flag(self, tmp_path, capsys):
        board = _board(tmp_path, c1=24.5)
        assert self._json(capsys, board)[2]
        assert not self._json(capsys, board, "--bypass-max-distance", "5")[2]

    def test_findings_are_warnings_and_waivable(self, tmp_path, capsys):
        board = _board(tmp_path, c1=61.5, c2=21.5)
        # (The exit code is not asserted: the synthetic board's ERC/LVS meta
        # checks fail independently of this rule.)
        _, _, found = self._json(capsys, board)
        assert [v["severity"] for v in found] == ["warning", "warning"]
        assert not any(v.get("waived") for v in found)
        (tmp_path / ".kct_waivers.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "waivers": [
                        {
                            "rule": BYPASS_DISTANCE_RULE_ID,
                            "items": [cap, ic],
                            "reason": "round-trip",
                            "issue": "test",
                        }
                        for cap, ic in (("C1", "U1"), ("C2", "U2"))
                    ],
                }
            )
        )
        _, _, found = self._json(capsys, board)
        assert len(found) == 2 and all(v["waived"] for v in found)

    def test_missing_schematic_is_unknown_not_checked(self, tmp_path, capsys):
        board = _board(tmp_path, with_schematic=False)
        _, data, found = self._json(capsys, board)
        assert found == []
        row = json.dumps(data.get("coverage", data))
        assert "needs_input:schematic" in row

    def test_drc_only_skips_it(self, tmp_path, capsys):
        board = _board(tmp_path, c1=61.5, c2=21.5)
        _, _, found = self._json(capsys, board, "--drc-only")
        assert found == []


class TestExplainCheckSharesTheAssociation:
    def test_swapped_caps_are_reported(self, tmp_path):
        pcb = PCB.load(str(_board(tmp_path, c1=61.5, c2=21.5)))
        mistakes = BypassCapDistanceCheck().check(pcb)
        assert sorted(tuple(m.components) for m in mistakes) == [("C1", "U1"), ("C2", "U2")]
        assert all(m.severity == "warning" for m in mistakes)

    def test_correct_caps_are_clean(self, tmp_path):
        assert BypassCapDistanceCheck().check(PCB.load(str(_board(tmp_path)))) == []

    def test_middle_band_is_info(self, tmp_path):
        pcb = PCB.load(str(_board(tmp_path, c1=24.5)))  # 4.0mm
        mistakes = BypassCapDistanceCheck().check(pcb)
        assert [(m.severity, tuple(m.components)) for m in mistakes] == [("info", ("C1", "U1"))]

    def test_no_schematic_is_incomplete_not_clean(self, tmp_path):
        pcb = PCB.load(str(_board(tmp_path, with_schematic=False)))
        with pytest.raises(CheckIncomplete):
            BypassCapDistanceCheck().check(pcb)
