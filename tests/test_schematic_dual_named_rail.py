"""A rail carrying two names resolves to one identity in every netlist API (#10240)."""

from __future__ import annotations

from kicad_tools.schematic import blocks as B
from kicad_tools.schematic.models import Schematic

SUPPLY = ("C1", "1"), ("C2", "1")


def _osc(sch: Schematic, i: int, rail_y: float, net: str):
    x, y = (50.8 + 101.6 * (i - 1), 25.4 * (i + 1))
    b = B.OscillatorBlock(sch, x, y, ref=f"Y{i}", cap_ref=f"C{i}", en_tied_to_vcc=False)
    b.connect_to_rails(rail_y, y + 30.48)
    for pin in (b.cap.pin_position("1"), b.ports["VCC"]):
        sch.add_label(net, pin[0], rail_y, snap=False)
    for pin in (b.cap.pin_position("2"), b.ports["GND"]):
        sch.add_label("GND", pin[0], y + 30.48, snap=False)
    return b


def _two_osc(with_power: bool) -> Schematic:
    sch = Schematic("alias")
    first = _osc(sch, 1, 25.4, "VDD_LOCAL")
    _osc(sch, 2, 50.8, "VDD_LOCAL")
    if with_power:
        x = first.cap.pin_position("1")[0]
        sch.add_wire((x, 25.4), (x - 10.16, 25.4))
        sch.add_power("power:+3V3", x - 10.16, 25.4, snap=False)
    return sch


def test_power_symbol_name_wins_in_every_api():
    sch = _two_osc(with_power=True)
    assert sch.get_net_for_pin("C1", "1") == "+3V3"
    assert sch.get_net_for_pin("C2", "1") == "+3V3"
    nets = sch.extract_netlist()
    assert {str(p) for p in nets["+3V3"]} == {"C1.1", "C2.1", "Y1.4", "Y2.4"}
    assert "VDD_LOCAL" not in nets


def test_get_all_pin_nets_agrees_with_get_net_for_pin():
    sch = _two_osc(with_power=True)
    all_nets = sch.get_all_pin_nets()
    assert all_nets
    for (ref, pin), net in all_nets.items():
        assert sch.get_net_for_pin(ref, pin) == net


def test_same_named_local_labels_on_separate_branches_are_one_net():
    sch = _two_osc(with_power=False)
    assert sch.get_net_for_pin("C1", "1") == "VDD_LOCAL"
    assert sch.get_net_for_pin("C2", "1") == "VDD_LOCAL"
    assert {str(p) for p in sch.extract_netlist()["VDD_LOCAL"]} >= {"C1.1", "C2.1"}
    assert sch.are_connected("C1", "1", "C2", "1")


def test_floating_pin_stays_none():
    sch = _two_osc(with_power=True)
    assert sch.get_net_for_pin("Y1", "1") is None  # nothing else on this pin
    assert sch.get_net_for_pin("NOPE", "1") is None
