"""Tests for the per-pin decoupling count check (issue #10134)."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.explain.checks.decoupling_count import DecouplingCountCheck
from kicad_tools.explain.mistakes import MistakeCategory, MistakeDetector, get_default_checks
from kicad_tools.schema.pcb import PCB

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
    assert pin.to_dict()["caps"] == [{"ref": "C1", "value": "100nF", "distance_mm": 1.0}]


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
