"""Tests for the per-pin decoupling count check (issue #10134)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import mistakes_cmd
from kicad_tools.explain.checks.decoupling_count import DecouplingCountCheck
from kicad_tools.explain.mistake_waivers import annotate_mistakes
from kicad_tools.explain.mistakes import (
    CheckIncomplete,
    Mistake,
    MistakeCategory,
    MistakeDetector,
    get_default_checks,
)
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_07 = REPO_ROOT / "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG")
  (net 4 "SIG2")
"""


def _ic(ref: str, x: float, y: float) -> str:
    return f"""
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at {x} {y})
    (property "Reference" "{ref}")
    (property "Value" "MCU")
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))
    (pad "4" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "SIG2"))
  )"""


def _cap(ref: str, value: str, x: float, y: float) -> str:
    return f"""
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at {x} {y})
    (property "Reference" "{ref}")
    (property "Value" "{value}")
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )"""


def _pcb(tmp_path: Path, *parts: str) -> PCB:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(_HEADER + "".join(parts) + ")\n")
    return PCB.load(str(f))


# U1 pin 1 (VCC) is at (8, 10).
def test_two_same_value_caps_on_one_pin_flagged(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _cap("C1", "100nF", 8, 11), _cap("C2", "100nF", 8, 12))
    found = DecouplingCountCheck().check(pcb)
    assert len(found) == 1
    m = found[0]
    assert m.category == MistakeCategory.DECOUPLING
    assert m.severity == "info"
    assert m.components == ["U1", "VCC", "C1", "C2"]
    assert "C1 (100nF, 1.0mm)" in m.explanation and "C2 (100nF, 2.0mm)" in m.explanation
    assert m.measurements["cap_count"] == 2.0


def test_equivalent_spellings_count_as_same_value(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _cap("C1", "100nF", 8, 11), _cap("C2", "0.1uF", 8, 12))
    assert len(DecouplingCountCheck().check(pcb)) == 1


def test_different_values_are_clean(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _cap("C1", "100nF", 8, 11), _cap("C2", "10uF", 8, 12))
    assert DecouplingCountCheck().check(pcb) == []


def test_two_ics_each_with_own_cap_is_clean(tmp_path: Path) -> None:
    pcb = _pcb(
        tmp_path,
        _ic("U1", 10, 10),
        _ic("U2", 40, 10),
        _cap("C1", "100nF", 8, 11),
        _cap("C2", "100nF", 38, 11),
    )
    assert DecouplingCountCheck().check(pcb) == []


def test_pin_without_cap_on_decoupled_rail_flagged(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _ic("U2", 40, 10), _cap("C1", "100nF", 8, 11))
    found = DecouplingCountCheck().check(pcb)
    assert len(found) == 1
    assert found[0].components == ["U2", "VCC"]
    assert found[0].severity == "warning"
    assert "30.0mm" in found[0].explanation


def test_unserved_rail_left_to_missing_check_but_listed_in_inventory(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _ic("U1", 10, 10))
    check = DecouplingCountCheck()
    assert check.check(pcb) == []
    inv = check.pin_inventory(pcb)
    assert [(p.ic_reference, p.pad_number, p.caps, p.rail_has_caps) for p in inv] == [
        ("U1", "1", [], False)
    ]


def test_inventory_includes_clean_pins_with_distances(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _cap("C1", "100nF", 8, 11))
    (pin,) = DecouplingCountCheck().pin_inventory(pcb)
    assert [(c.reference, c.value, c.distance_mm) for c in pin.caps] == [("C1", "100nF", 1.0)]
    assert pin.to_dict()["caps"] == [
        {"ref": "C1", "value": "100nF", "distance_mm": 1.0, "source": "geometric"}
    ]


def test_equidistant_cap_assigned_deterministically(tmp_path: Path) -> None:
    # Cap midway between U1 pin 1 (8, 10) and U2 pin 1 (12, 10).
    pcb = _pcb(tmp_path, _ic("U2", 14, 10), _ic("U1", 10, 10), _cap("C1", "100nF", 10, 10))
    inv = {p.ic_reference: p for p in DecouplingCountCheck().pin_inventory(pcb)}
    assert [c.reference for c in inv["U1"].caps] == ["C1"]
    assert inv["U2"].caps == []


def test_multi_pad_pin_on_one_net_inventoried_per_pad(tmp_path: Path) -> None:
    ic = _ic("U1", 10, 10).replace(
        '(pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))',
        '(pad "3" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))',
    )
    pcb = _pcb(tmp_path, ic, _cap("C1", "100nF", 8, 11), _cap("C2", "100nF", 12, 11))
    inv = DecouplingCountCheck().pin_inventory(pcb)
    assert [(p.pad_number, [c.reference for c in p.caps]) for p in inv] == [
        ("1", ["C1"]),
        ("3", ["C2"]),
    ]
    assert DecouplingCountCheck().check(pcb) == []


def test_unparseable_value_does_not_raise(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _cap("C1", "100nF", 8, 11), _cap("C2", "X7R", 8, 12))
    DecouplingCountCheck().check(pcb)


def test_limit_is_constructor_argument(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _cap("C1", "100nF", 8, 11), _cap("C2", "100nF", 8, 12))
    assert DecouplingCountCheck(max_same_value=2).check(pcb) == []


def test_board_without_ics_is_clean(tmp_path: Path) -> None:
    pcb = _pcb(tmp_path, _cap("C1", "100nF", 8, 11))
    assert DecouplingCountCheck().check(pcb) == []


def test_on_by_default_and_runs_through_detector(tmp_path: Path) -> None:
    assert any(isinstance(c, DecouplingCountCheck) for c in get_default_checks())
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _cap("C1", "100nF", 8, 11), _cap("C2", "100nF", 8, 12))
    titles = [m.title for m in MistakeDetector().detect(pcb)]
    assert "Redundant decoupling capacitors on one power pin" in titles


def test_uncovered_pins_on_one_ic_and_net_share_one_finding(tmp_path: Path) -> None:
    ic = _ic("U2", 40, 10).replace(
        '(pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))',
        '(pad "3" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))',
    )
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), ic, _cap("C1", "100nF", 8, 11))
    found = DecouplingCountCheck().check(pcb)
    assert len(found) == 1
    assert "pins 1, 3" in found[0].explanation
    assert found[0].measurements["pin_count"] == 2.0


def test_nearby_cap_assigned_to_other_pin_vs_outside_radius_control(tmp_path: Path) -> None:
    # U1.1 is at (8, 10), C1 is at (9, 10) -> 1.0 mm from U1.1 (assigned to U1.1).
    # Nearby case: U2.1 at (11.2, 10) -> 2.2 mm from C1 (within 5 mm, unassigned).
    nearby_dir = tmp_path / "nearby"
    nearby_dir.mkdir()
    nearby_pcb = _pcb(
        nearby_dir,
        _ic("U1", 10, 10),
        _ic("U2", 13.2, 10),
        _cap("C1", "100nF", 9, 10),
    )
    # Control case: U2.1 at (31.0, 10) -> 22.0 mm from C1 (outside 5 mm).
    distant_dir = tmp_path / "distant"
    distant_dir.mkdir()
    distant_pcb = _pcb(
        distant_dir,
        _ic("U1", 10, 10),
        _ic("U2", 33.0, 10),
        _cap("C1", "100nF", 9, 10),
    )

    (nearby_m,) = DecouplingCountCheck().check(nearby_pcb)
    assert nearby_m.title == "Power pin has no separately assigned decoupling capacitor"
    assert nearby_m.components == ["U2", "VCC", "C1"]
    assert "no decoupling capacitor within" not in nearby_m.explanation
    assert "C1 (100nF) is 2.2mm away on VCC (within 5mm)" in nearby_m.explanation
    assert "assigned to U1.1" in nearby_m.explanation
    assert "within 5mm" not in nearby_m.fix_suggestion
    assert "C1 (assigned to U1.1)" in nearby_m.fix_suggestion
    assert nearby_m.measurements == {"pin_count": 1.0, "nearest_rail_cap_mm": 2.2}

    (distant_m,) = DecouplingCountCheck().check(distant_pcb)
    assert distant_m.title == "Power pin has no decoupling capacitor nearby"
    assert distant_m.components == ["U2", "VCC"]
    assert "no decoupling capacitor within 5mm" in distant_m.explanation
    assert "22.0mm" in distant_m.explanation
    assert "within 5mm of U2 pin 1" in distant_m.fix_suggestion
    assert distant_m.measurements == {"pin_count": 1.0, "nearest_rail_cap_mm": 22.0}


def test_normal_cli_report_covers_both_nearby_shared_and_distant_cases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pcb_file = tmp_path / "both.kicad_pcb"
    pcb_file.write_text(
        _HEADER
        + _ic("U1", 10, 10)
        + _ic("U2", 13.2, 10)
        + _ic("U3", 33.0, 10)
        + _cap("C1", "100nF", 9, 10)
        + ")\n"
    )

    code = mistakes_cmd.main([str(pcb_file), "--category", "decoupling", "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    by_ic = {m["components"][0]: m for m in payload["mistakes"]}
    assert set(by_ic) == {"U2", "U3"}

    assert by_ic["U2"]["title"] == "Power pin has no separately assigned decoupling capacitor"
    assert "assigned to U1.1" in by_ic["U2"]["explanation"]
    assert "C1 (assigned to U1.1)" in by_ic["U2"]["fix_suggestion"]
    assert by_ic["U2"]["key"] == "mistake.decoupling_count|C1,U2|VCC|"

    assert by_ic["U3"]["title"] == "Power pin has no decoupling capacitor nearby"
    assert "no decoupling capacitor within 5mm" in by_ic["U3"]["explanation"]
    assert "22.0mm" in by_ic["U3"]["explanation"]
    assert "within 5mm of U3 pin 1" in by_ic["U3"]["fix_suggestion"]
    assert by_ic["U3"]["key"] == "mistake.decoupling_count|U3|VCC|"

    # Also verify human-readable table output (--verbose) renders both distinctly.
    code_table = mistakes_cmd.main([str(pcb_file), "--category", "decoupling", "--verbose"])
    assert code_table == 0
    table_out = capsys.readouterr().out
    assert "Power pin has no separately assigned decoupling capacitor" in table_out
    assert "Power pin has no decoupling capacitor nearby" in table_out


def test_same_ic_and_net_with_nearby_shared_and_distant_pins_gets_distinct_keys(
    tmp_path: Path,
) -> None:
    # U2 has pad 1 at (11.2, 10) [2.2 mm from C1] and pad 3 at (21.0, 10) [12.0 mm from C1].
    u2 = _ic("U2", 13.2, 10).replace(
        '(pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))',
        '(pad "3" smd rect (at 7.8 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))',
    )
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), u2, _cap("C1", "100nF", 9, 10))
    found = MistakeDetector(checks=[DecouplingCountCheck()]).detect(pcb)
    annotate_mistakes(found, pcb)
    assert len(found) == 2
    keys = {m.key for m in found}
    assert keys == {
        "mistake.decoupling_count|C1,U2|VCC|",
        "mistake.decoupling_count|U2|VCC|",
    }


def test_multi_pin_nearby_shared_with_different_caps_lists_each_assignment(
    tmp_path: Path,
) -> None:
    # U3 has pad 1 at (11.0, 10) near C1 (assigned to U1.1) and pad 3 at (15.0, 10)
    # near C2 (assigned to U2.1).
    u3 = _ic("U3", 13.0, 10).replace(
        '(pad "3" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "SIG"))',
        '(pad "3" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))',
    )
    pcb = _pcb(
        tmp_path,
        _ic("U1", 10, 10),  # U1.1 at (8, 10)
        _ic("U2", 20, 10),  # U2.1 at (18, 10)
        u3,
        _cap("C1", "100nF", 9, 10),  # 1.0 mm to U1.1, 2.0 mm to U3.1
        _cap("C2", "100nF", 17, 10),  # 1.0 mm to U2.1, 2.0 mm to U3.3
    )
    (m,) = DecouplingCountCheck().check(pcb)
    assert m.title == "Power pin has no separately assigned decoupling capacitor"
    assert m.components == ["U3", "VCC", "C1", "C2"]
    assert "pin 1: C1 (100nF, 2.0mm away, assigned to U1.1)" in m.explanation
    assert "pin 3: C2 (100nF, 2.0mm away, assigned to U2.1)" in m.explanation
    assert "C1 assigned to U1.1, C2 assigned to U2.1" in m.fix_suggestion


def test_distance_just_outside_radius_does_not_round_to_radius(tmp_path: Path) -> None:
    # C1 at (9.0, 10.0); U2.1 at (14.04, 10.0) -> 5.04 mm (> 5 mm, but rounds to 5.0 at 1 dp).
    pcb = _pcb(tmp_path, _ic("U1", 10, 10), _ic("U2", 16.04, 10), _cap("C1", "100nF", 9, 10))
    (m,) = DecouplingCountCheck().check(pcb)
    assert m.title == "Power pin has no decoupling capacitor nearby"
    assert "5.04mm away" in m.explanation
    assert "5.0mm away" not in m.explanation
    assert m.measurements["nearest_rail_cap_mm"] == 5.04


@pytest.mark.skipif(not BOARD_07.exists(), reason="board 07 output not present")
def test_board07_public_report_describes_actual_cap_assignments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = mistakes_cmd.main([str(BOARD_07), "--category", "decoupling", "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    mistakes = payload["mistakes"]
    assert len(mistakes) == 3
    for m in mistakes:
        assert m["title"] == "Power pin has no separately assigned decoupling capacitor"
        assert "no decoupling capacitor within" not in m["explanation"]
        assert "within 5mm" not in m["fix_suggestion"]

    by_key = {(m["components"][0], m["components"][1]): m for m in mistakes}
    assert "C12 (100nF)" in by_key[("U1", "+3V3")]["explanation"]
    assert "assigned to U1.6" in by_key[("U1", "+3V3")]["explanation"]
    assert "C22 (100nF)" in by_key[("U1", "+3V3_A")]["explanation"]
    assert "assigned to U1.33" in by_key[("U1", "+3V3_A")]["explanation"]
    assert "C26 (10uF)" in by_key[("U3", "+5V")]["explanation"]
    assert "assigned to U3.3" in by_key[("U3", "+5V")]["explanation"]


# ---------------------------------------------------------------------------
# Schematic association (issue #10134, consuming #10140).  The PCB is the same
# in every row: U1.1 at 0 mm and U2.1 at 20 mm on VCC, C1 at 1 mm (beside U1)
# and C2 at 19 mm (beside U2).  Only the schematic designation changes.
# ---------------------------------------------------------------------------


def _two_ic_board(
    tmp_path: Path,
    c1_target: str | None,
    c2_target: str | None,
    *,
    origin_x: float = 0,
    c1_offset: float = 1,
) -> Path:
    from .test_decoupling_association import make_sheet

    board = tmp_path / "board.kicad_pcb"
    board.write_text(
        _HEADER
        + _ic("U1", origin_x + 2, 10)
        + _ic("U2", origin_x + 22, 10)
        + _cap("C1", "100nF", origin_x + c1_offset, 10)
        + _cap("C2", "100nF", origin_x + 19, 10)
        + ")\n"
    )
    make_sheet(
        tmp_path / "board.kicad_sch",
        [
            ("U1", 50.8, 50.8, "+3V3", None),
            ("U2", 101.6, 50.8, "+3V3", None),
            ("C1", 55.88, 50.8, "+3V3", c1_target),
            ("C2", 106.68, 50.8, "+3V3", c2_target),
        ],
        [(50.8, 50.8, 55.88, 50.8), (101.6, 50.8, 106.68, 50.8)],
    )
    return board


def _rows(board: Path) -> dict[tuple[str, str], list[tuple[str, float, str]]]:
    inv = DecouplingCountCheck().pin_inventory(PCB.load(str(board)))
    return {
        (p.ic_reference, p.pad_number): [(c.reference, c.distance_mm, c.source) for c in p.caps]
        for p in inv
        if p.net_name == "VCC"
    }


def test_schematic_designation_matching_geometry(tmp_path: Path) -> None:
    board = _two_ic_board(tmp_path, None, None)  # C1 -> U1.1, C2 -> U2.1 as drawn
    assert _rows(board) == {
        ("U1", "1"): [("C1", 1.0, "schematic")],
        ("U2", "1"): [("C2", 1.0, "schematic")],
    }
    assert DecouplingCountCheck().check(PCB.load(str(board))) == []


def test_both_caps_designated_for_u1_is_duplicate_and_leaves_u2_empty(tmp_path: Path) -> None:
    board = _two_ic_board(tmp_path, "U1.1", "U1.1")
    assert _rows(board) == {
        ("U1", "1"): [("C1", 1.0, "schematic"), ("C2", 19.0, "schematic")],
        ("U2", "1"): [],
    }
    found = DecouplingCountCheck().check(PCB.load(str(board)))
    redundant = [m for m in found if m.title.startswith("Redundant")]
    assert len(redundant) == 1
    assert "C1 (100nF, 1.0mm)" in redundant[0].explanation
    assert "C2 (100nF, 19.0mm)" in redundant[0].explanation
    assert "nearby" not in redundant[0].explanation
    assert any(m.components[0] == "U2" for m in found if not m.title.startswith("Redundant"))


def test_crossed_designation_shows_both_caps_at_19mm(tmp_path: Path) -> None:
    board = _two_ic_board(tmp_path, "U2.1", "U1.1")
    assert _rows(board) == {
        ("U1", "1"): [("C2", 19.0, "schematic")],
        ("U2", "1"): [("C1", 19.0, "schematic")],
    }
    by_ref = {m.components[0]: m for m in DecouplingCountCheck().check(PCB.load(str(board)))}
    assert set(by_ref) == {"U1", "U2"}
    for ref, distant, nearby, owner in (("U1", "C2", "C1", "U2.1"), ("U2", "C1", "C2", "U1.1")):
        finding = by_ref[ref]
        assert "assigned" in finding.title
        assert f"{distant} at 19.0mm" in finding.explanation
        assert f"{nearby} at 1.0mm away, assigned to {owner}" in finding.explanation
        assert "has no decoupling capacitor within" not in finding.explanation
        assert finding.measurements["nearest_rail_cap_mm"] == 1.0


@pytest.mark.parametrize("origin_x", [0, 4.8])
@pytest.mark.parametrize("gap", [4.999999, 5.0, 5.000001, 5.004, 5.006])
@pytest.mark.parametrize("schematic", [True, False])
def test_cap_uses_measured_distance_at_radius(tmp_path: Path, capsys, origin_x, gap, schematic):
    board = _two_ic_board(tmp_path, "U1.1", "U2.1", origin_x=origin_x, c1_offset=gap)
    if not schematic:
        board.with_suffix(".kicad_sch").unlink()
    assert mistakes_cmd.main([str(board), "--category", "decoupling", "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    findings = [m for m in result["mistakes"] if m["rule_id"] == "mistake.decoupling_count"]
    outside = gap > 5.0
    assert len(findings) == int(outside)
    pin = next(p for p in result["decoupling_inventory"] if (p["ic"], p["pin"]) == ("U1", "1"))
    assert [cap["ref"] for cap in pin["caps"]] == (["C1"] if schematic or not outside else [])
    measured = pin["nearest_rail_cap"]["distance_mm"]
    assert (measured > 5.0) == outside
    if gap == 5.0:
        assert measured == 5.0
    if pin["caps"]:
        assert pin["caps"][0]["distance_mm"] == measured
    if outside:
        assert findings[0]["measurements"]["nearest_rail_cap_mm"] > 5.0


def test_reused_detector_reads_changed_schematic_on_same_board(tmp_path: Path) -> None:
    board = _two_ic_board(tmp_path, "U1.1", "U2.1")
    pcb = PCB.load(board)
    check = DecouplingCountCheck()
    detector = MistakeDetector([check])
    assert detector.detect(pcb) == []

    schematic = board.with_suffix(".kicad_sch")
    before = schematic.read_text()
    override = '"KCT_Decouples" "U2.1"'
    assert before.count(override) == 1
    schematic.write_text(before.replace(override, '"KCT_Decouples" "U1.1"'))

    findings = detector.detect(pcb)
    assert any({"C1", "C2"} <= set(finding.components) for finding in findings)
    by_ic = {pin.ic_reference: [c.reference for c in pin.caps] for pin in check.pin_inventory(pcb)}
    assert by_ic == {"U1": ["C1", "C2"], "U2": []}

    schematic.unlink()
    assert detector.detect(pcb) == []


def test_without_schematic_the_geometric_rule_is_used(tmp_path: Path) -> None:
    board = _two_ic_board(tmp_path, "U1.1", "U1.1")
    board.with_suffix(".kicad_sch").unlink()
    assert _rows(board) == {
        ("U1", "1"): [("C1", 1.0, "geometric")],
        ("U2", "1"): [("C2", 1.0, "geometric")],
    }


def test_unreadable_schematic_falls_back_to_geometry(tmp_path: Path) -> None:
    board = _two_ic_board(tmp_path, "U1.1", "U1.1")
    board.with_suffix(".kicad_sch").write_text("not a schematic")
    assert {k: [c[2] for c in v] for k, v in _rows(board).items()} == {
        ("U1", "1"): ["geometric"],
        ("U2", "1"): ["geometric"],
    }


def test_json_report_lists_every_pin_with_caps_and_distances(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    board = _two_ic_board(tmp_path, "U1.1", "U1.1")
    assert mistakes_cmd.main([str(board), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {(r["ic"], r["pin"]): r for r in payload["decoupling_inventory"] if r["net"] == "VCC"}
    assert [(c["ref"], c["distance_mm"], c["source"]) for c in rows[("U1", "1")]["caps"]] == [
        ("C1", 1.0, "schematic"),
        ("C2", 19.0, "schematic"),
    ]
    assert rows[("U2", "1")]["caps"] == []  # empty pins are listed too
    # Other categories do not carry the inventory.
    assert mistakes_cmd.main([str(board), "--category", "bom_health", "--format", "json"]) == 0
    assert "decoupling_inventory" not in json.loads(capsys.readouterr().out)


def test_stage_suffixed_board_resolves_project_schematic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``board_routed.kicad_pcb`` finds ``board.kicad_sch`` through the project resolver."""
    board = _two_ic_board(tmp_path, "U1.1", "U1.1")
    routed = tmp_path / "board_routed.kicad_pcb"
    board.rename(routed)
    assert mistakes_cmd.main([str(routed), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {(r["ic"], r["pin"]): r for r in payload["decoupling_inventory"] if r["net"] == "VCC"}
    assert [(c["ref"], c["distance_mm"], c["source"]) for c in rows[("U1", "1")]["caps"]] == [
        ("C1", 1.0, "schematic"),
        ("C2", 19.0, "schematic"),
    ]
    assert rows[("U2", "1")]["caps"] == []


def test_stage_suffixed_board_without_schematic_keeps_geometric_fallback(tmp_path: Path) -> None:
    board = _two_ic_board(tmp_path, "U1.1", "U1.1")
    (tmp_path / "board.kicad_sch").unlink()
    routed = tmp_path / "board_routed.kicad_pcb"
    board.rename(routed)
    assert {k: [c[2] for c in v] for k, v in _rows(routed).items()} == {
        ("U1", "1"): ["geometric"],
        ("U2", "1"): ["geometric"],
    }


def test_json_report_parses_the_schematic_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #10241: findings and inventory share one schematic parse."""
    import kicad_tools.placement.decoupling as decoupling

    board = _two_ic_board(tmp_path, "U1.1", "U1.1")
    calls: list[Path] = []
    real = decoupling.schematic_cap_associations

    def counting(path, *args, **kwargs):
        calls.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(decoupling, "schematic_cap_associations", counting)
    assert mistakes_cmd.main([str(board), "--category", "decoupling", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert payload["decoupling_inventory"]


def test_detector_reports_inventory_from_registered_check(tmp_path: Path) -> None:
    """The inventory comes from the registered instance, so its settings apply."""
    board = _two_ic_board(tmp_path, "U1.1", "U1.1")
    pcb = PCB.load(str(board))
    check = DecouplingCountCheck(radius_mm=0.5)
    reports: dict = {}
    mistakes, _ = MistakeDetector([check]).detect_with_coverage(pcb, reports)
    assert [r.to_dict() for r in reports["DecouplingCountCheck"]] == [
        r.to_dict() for r in check.pin_inventory(pcb)
    ]
    assert mistakes
    assert [m.title for m in mistakes] == [m.title for m in check.check(pcb)]
    # Without a reports dict nothing is collected and nothing is kept.
    plain, _ = MistakeDetector([check]).detect_with_coverage(pcb)
    assert [m.title for m in plain] == [m.title for m in mistakes]


@pytest.mark.parametrize("incomplete", [False, True])
def test_reports_preserve_registered_check_override(tmp_path: Path, incomplete: bool) -> None:
    class CustomDecoupling(DecouplingCountCheck):
        def check(self, pcb: PCB) -> list[Mistake]:
            if incomplete:
                raise CheckIncomplete("Designated schematic missing")
            return super().check(pcb)[:1]

    pcb = PCB.load(str(_two_ic_board(tmp_path, "U1.1", "U1.1")))
    detector = MistakeDetector([CustomDecoupling()])
    findings, coverage = detector.detect_with_coverage(pcb)
    assert len(findings) == (0 if incomplete else 1)
    assert coverage[0].status == ("incomplete" if incomplete else "ran")

    reports: dict = {}
    assert detector.detect_with_coverage(pcb, reports) == (findings, coverage)
    assert reports == {}
