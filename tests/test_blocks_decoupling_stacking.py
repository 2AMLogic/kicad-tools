"""Decoupling provenance follows the placed blocks and their final electrical nets."""

import warnings

import pytest

from kicad_tools.schematic import blocks
from kicad_tools.schematic.grid import get_symbol_search_paths
from kicad_tools.schematic.models import Schematic

pytestmark = pytest.mark.skipif(
    not any((path / "Device.kicad_sym").is_file() for path in get_symbol_search_paths()),
    reason="These block integration tests need the KiCad symbol libraries.",
)


def oscillator(sch, i, rail_y, net, **kwargs):
    x, y = (50.8 + 101.6 * (i - 1), 25.4 * (i + 1))
    block = blocks.OscillatorBlock(
        sch, x, y, ref=f"Y{i}", cap_ref=f"C{i}", en_tied_to_vcc=False, **kwargs
    )
    gnd_y = y + 30.48
    block.connect_to_rails(rail_y, gnd_y)
    for pin in (block.cap.pin_position("1"), block.ports["VCC"]):
        sch.add_label(net, pin[0], rail_y, snap=False)
    for pin in (block.cap.pin_position("2"), block.ports["GND"]):
        sch.add_label("GND", pin[0], gnd_y, snap=False)
    return block


def quiet_report(sch):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        return sch.decoupling_report()


def test_same_height_separate_rails():
    sch = Schematic("Two supplies")
    oscillator(sch, 1, 25.4, "+3V3")
    oscillator(sch, 2, 25.4, "+1V8")
    report = quiet_report(sch)
    assert set(report) == {"+3V3", "+1V8"}
    for i, net in enumerate(("+3V3", "+1V8"), 1):
        assert report[net]["count"] == 1
        (cap,) = report[net]["caps"]
        assert cap["ref"] == f"C{i}" and cap["value"] == "100nF"
        assert f"Y{i}" in cap["source"]
        assert cap["role"] == "bypass" and cap["from_default"] is True


def test_different_heights_shared_rail_warns_once_naming_only_defaults():
    sch = Schematic("Shared supply")
    oscillator(sch, 1, 25.4, "+3V3")
    oscillator(sch, 2, 50.8, "+3V3")
    oscillator(sch, 3, 76.2, "+3V3", decoupling_cap="100nF")
    with pytest.warns(UserWarning) as emitted:
        report = sch.decoupling_report()
    assert len(emitted) == 1
    warning = str(emitted[0].message)
    assert all(name in warning for name in ("+3V3", "Y1", "Y2"))
    assert "Y3" not in warning
    assert report["+3V3"]["count"] == 3
    assert [cap["from_default"] for cap in report["+3V3"]["caps"]] == [True, True, False]


@pytest.mark.parametrize("separate", [False, True])
def test_local_supply_labels_resolve_to_the_power_net(separate):
    sch = Schematic("Power rail aliases")
    first = oscillator(sch, 1, 25.4, "VDD_LOCAL")
    second = oscillator(sch, 2, 50.8, "VDD_OTHER" if separate else "VDD_LOCAL")
    feeds = [(first, 25.4, "+3V3")]
    if separate:
        feeds.append((second, 50.8, "+1V8"))
    for block, y, supply in feeds:
        x = block.cap.pin_position("1")[0]
        sch.add_wire((x, y), (x - 10.16, y))
        sch.add_power(f"power:{supply}", x - 10.16, y, snap=False)

    if separate:
        report = quiet_report(sch)
        assert {net: [cap["ref"] for cap in group["caps"]] for net, group in report.items()} == {
            "+3V3": ["C1"],
            "+1V8": ["C2"],
        }
    else:
        with pytest.warns(UserWarning, match=r"\+3V3.*Y1.*Y2") as emitted:
            report = sch.decoupling_report()
        assert len(emitted) == 1
        assert set(report) == {"+3V3"}
        assert [cap["ref"] for cap in report["+3V3"]["caps"]] == ["C1", "C2"]


def test_editing_a_default_capacitor_value_makes_it_a_caller_override():
    sch = Schematic("Edited decoupling")
    oscillator(sch, 1, 25.4, "VDD")
    second = oscillator(sch, 2, 50.8, "VDD")
    second.cap.value = "47nF"

    first_cap, edited_cap = quiet_report(sch)["VDD"]["caps"]
    assert first_cap["from_default"] is True
    assert edited_cap["value"] == "47nF"
    assert edited_cap["from_default"] is False
    assert "Y2" in edited_cap["source"]


def test_global_supply_name_propagates_through_local_aliases():
    sch = Schematic("Transitive supply aliases")
    oscillator(sch, 1, 25.4, "LOCAL_A")
    oscillator(sch, 2, 50.8, "LOCAL_B")
    # A wire away from either oscillator joins the two local names. Its
    # global label supplies the name KiCad uses in the netlist.
    left, middle, right = (228.6, 25.4), (233.68, 25.4), (238.76, 25.4)
    sch.add_wire(left, middle)
    sch.add_wire(middle, right)
    sch.add_label("LOCAL_A", *left, snap=False)
    sch.add_label("LOCAL_B", *middle, snap=False)
    sch.add_global_label("SUPPLY", *right, snap=False)

    with pytest.warns(UserWarning, match="SUPPLY.*Y1.*Y2") as emitted:
        report = sch.decoupling_report()
    assert len(emitted) == 1
    assert list(report) == ["SUPPLY"]
    assert report["SUPPLY"]["count"] == 2


def test_floating_then_unnamed_then_labeled_and_schematic_local():
    sch = Schematic("Changing connectivity")
    a = blocks.OscillatorBlock(sch, 50.8, 50.8)
    b = blocks.OscillatorBlock(sch, 152.4, 50.8, ref="Y2", cap_ref="C2", decoupling_cap="47nF")
    assert quiet_report(sch)[None]["count"] == 2
    assert quiet_report(Schematic("Other design")) == {}
    sch.add_wire(a.cap.pin_position("1"), b.cap.pin_position("1"))
    auto_name = sch.get_net_for_pin("C1", "1")
    assert auto_name and quiet_report(sch)[auto_name]["count"] == 2
    sch.add_label("VDD", *a.cap.pin_position("1"), snap=False)
    report = quiet_report(sch)
    assert set(report) == {"VDD"}
    assert [(c["value"], c["from_default"]) for c in report["VDD"]["caps"]] == [
        ("100nF", True),
        ("47nF", False),
    ]


def test_one_mcu_retains_its_four_intentional_caps():
    sch = Schematic("MCU bypass bank")
    mcu = blocks.MCUBlock(sch, 100, 100, mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx")
    sch.add_label("VDD", *mcu.bypass_caps[0].pin_position("1"), snap=False)
    rail = quiet_report(sch)["VDD"]
    assert rail["count"] == 4
    assert {cap["source"] for cap in rail["caps"]} == {rail["caps"][0]["source"]}
    assert all(cap["value"] == "100nF" and cap["from_default"] for cap in rail["caps"])


def test_opamp_bypass_registered_but_crystal_loads_excluded():
    sch = Schematic("Analog supplies")
    opamp = blocks.OpAmpBlock(sch, 76.2, 76.2)
    blocks.CrystalOscillator(sch, 152.4, 76.2, cap_ref_start=10)
    caps = [cap for rail in quiet_report(sch).values() for cap in rail["caps"]]
    assert len(caps) == 1
    assert (caps[0]["ref"], caps[0]["role"]) == (opamp.components["C_bypass"].reference, "bypass")


def test_factory_preserves_partial_mcu_array_overrides_and_role():
    sch = Schematic("Cap array")
    blocks.create_mcu_decoupling_array(sch, 76.2, 76.2, supply_pins=2, bulk_value="4.7uF")
    caps = [cap for rail in quiet_report(sch).values() for cap in rail["caps"]]
    assert [(c["ref"], c["role"], c["from_default"]) for c in caps] == [
        ("C1", "bypass", True),
        ("C2", "bypass", True),
        ("C3", "bulk", False),
    ]


@pytest.mark.parametrize(
    ("factory", "kwargs", "expected"),
    [
        (
            blocks.LDOBlock,
            {"input_cap": "10uF"},
            [
                ("C1", "input", "10uF", False),
                ("C2", "output", "10uF", True),
                ("C3", "output", "100nF", True),
            ],
        ),
        (blocks.LDOBlock, {"output_caps": []}, [("C1", "input", "10uF", True)]),
        (
            blocks.create_12v_buck,
            {},
            [("C1", "input", "100uF", True), ("C2", "output", "330uF", True)],
        ),
        (
            blocks.CANTransceiver,
            {"termination": "split", "decoupling_cap": "100nF"},
            [("C1", "bypass", "100nF", False)],
        ),
        (blocks.CurrentSenseShunt, {"amplifier": True}, [("C1", "bypass", "100nF", True)]),
        (
            blocks.GateDriverBlock,
            {},
            [("C4", "bypass", "10uF", True), ("C5", "bypass", "100nF", True)],
        ),
        (
            blocks.UCC27211GateDriver,
            {"vcc_bypass_cap_value": "100nF"},
            [("C2", "bulk", "10uF", True), ("C3", "bypass", "100nF", False)],
        ),
        (blocks.BarrelJackInput, {}, [("C1", "bulk", "100uF", True)]),
        (blocks.USBPowerInput, {"filter_cap": "10uF"}, [("C1", "bulk", "10uF", False)]),
        (blocks.BatteryInput, {}, [("C1", "bulk", "10uF", True)]),
    ],
)
def test_supply_caps_and_overrides_in_block_families(factory, kwargs, expected):
    sch = Schematic(factory.__name__)
    factory(sch, 76.2, 76.2, **kwargs)
    caps = [c for rail in quiet_report(sch).values() for c in rail["caps"]]
    assert sorted((c["ref"], c["role"], c["value"], c["from_default"]) for c in caps) == expected


def test_cascaded_factory_preserves_each_regulators_presets():
    sch = Schematic("48 V supply")
    blocks.create_dual_supply_cascade(sch, 76.2, 177.8, 76.2, vin=48, v_mid=12, vout=5)
    caps = sorted(
        (c for rail in quiet_report(sch).values() for c in rail["caps"]), key=lambda c: c["ref"]
    )
    assert [(c["role"], c["value"]) for c in caps] == [
        ("input", "100uF"),
        ("output", "330uF"),
        ("input", "10uF"),
        ("output", "10uF"),
    ]
    assert all(c["from_default"] for c in caps)
    assert len({c["source"] for c in caps}) == 2


def test_explicit_registration_tracks_component_edits_and_removal():
    sch = Schematic("Serviceable board")
    cap = sch.add_symbol("Device:C", 76.2, 76.2, "C1", "1uF")
    sch.register_decoupling_cap(cap, "Input connector", "bulk")
    cap.value = "4.7uF"
    cap.reference = "C2"
    assert quiet_report(sch)[None]["caps"] == [
        {
            "ref": "C2",
            "value": "4.7uF",
            "source": "Input connector",
            "role": "bulk",
            "from_default": False,
        }
    ]
    sch.remove_symbol("C2")
    assert quiet_report(sch) == {}
