"""CLI checks against a two-channel fixture and its KiCad 10 S-expression export."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli import main
from kicad_tools.operations.netlist import Netlist, build_netlist_from_schematic
from kicad_tools.sexp import parse_string

FIXTURE = Path(__file__).parent / "fixtures" / "channel_intent"


@pytest.fixture
def bank(tmp_path):
    return Path(shutil.copytree(FIXTURE, tmp_path / "bank"))


def check(bank, source, capsys, status):
    result = main(
        [
            "validate",
            "--channel-intent",
            str(bank / "intent.json"),
            str(bank / f"bank.{source}"),
            str(bank / "bank.kicad_pcb"),
            "--format",
            "json",
        ]
    )
    assert result == status
    rows = json.loads(capsys.readouterr().out)["findings"]
    keys = ["instance", "reference", "source", "pin", "field", "expected", "observed"]
    return sorted(
        (tuple(r[k] for k in keys) for r in rows),
        key=lambda row: tuple("" if v is None else v for v in row),
    )


@pytest.mark.parametrize("source", ["kicad_sch", "net"])
def test_clean_channels(bank, source, capsys):
    assert check(bank, source, capsys, 0) == []


@pytest.mark.parametrize(("source", "label"), [("kicad_sch", "schematic"), ("net", "netlist")])
def test_wrong_capacitor_on_both_sources(bank, source, label, capsys):
    for ext in [source, "kicad_pcb"]:
        path = bank / f"bank.{ext}"
        text = path.read_text().replace("1 µF", "100 nF", 1)
        # KiCad exports the custom field both under fields and as a property.
        path.write_text(text.replace("C106248", "C14663", 2 if ext == "net" else 1))
    assert check(bank, source, capsys, 1) == sorted(
        ("27", "C127", s, None, field, expected, observed)
        for s in (label, "pcb")
        for field, expected, observed in [
            ("Value", "1 µF", "100 nF"),
            ("LCSC", "C106248", "C14663"),
        ]
    )


@pytest.mark.parametrize("missing", [False, True])
def test_pcb_field_drift(bank, capsys, missing):
    pcb = bank / "bank.kicad_pcb"
    old = '"LCSC" "C106248"'
    new = '"Other" "C14663"' if missing else '"LCSC" "C14663"'
    pcb.write_text(pcb.read_text().replace(old, new, 1))
    assert check(bank, "net", capsys, 1) == [
        ("27", "C127", "pcb", None, "LCSC", "C106248", None if missing else "C14663")
    ]


@pytest.mark.parametrize("repeated", [False, True])
def test_swapped_dividers(bank, capsys, repeated):
    pcb = bank / "bank.kicad_pcb"
    doc = parse_string(pcb.read_text())
    path = bank / "bank.net"
    export = parse_string(path.read_text())
    nets = {n.find("name").get_string(): n for n in export.find("nets").find_all("net")}
    expected = []
    for channel in [27, 28] if repeated else [27]:
        low, high = f"T{channel}", f"T{channel + 1}"
        for ref, pin, old, new in [
            (f"R3{channel}", "1", high, low),
            (f"R4{channel}", "2", low, high),
        ]:
            fp = next(
                f for f in doc.find_all("footprint") if f.find("property").get_string(1) == ref
            )
            pad = next(p for p in fp.find_all("pad") if p.get_string(0) == pin)
            net = next(n for n in doc.find_all("net") if n.get_string(1) == new)
            pad.find("net").children = net.children
            node = next(
                n
                for n in nets[old].find_all("node")
                if n.find("ref").get_string() == ref and n.find("pin").get_string() == pin
            )
            nets[old].children.remove(node)
            nets[new].children.append(node)
            for source in ["netlist", "pcb"]:
                expected.append((str(channel), ref, source, pin, None, old, new))
    path.write_text(export.to_string())
    pcb.write_text(doc.to_string())
    assert check(bank, "net", capsys, 1) == sorted(expected)


def test_shared_private_net(bank, capsys):
    pcb = bank / "bank.kicad_pcb"
    pcb.write_text(pcb.read_text().replace('(net 5 "/K28"))', '(net 4 "/K27"))'))
    sch = bank / "bank.kicad_sch"
    sch.write_text(sch.read_text().replace('(label "K28"', '(label "K27"'))
    assert check(bank, "kicad_sch", capsys, 1) == [
        ("28", "C128", source, "1", None, "/K28", "/K27") for source in ("pcb", "schematic")
    ]


def test_absent_instance_on_both_sources(bank, capsys):
    absent = {"C127", "R327", "R427"}
    pcb = bank / "bank.kicad_pcb"
    doc = parse_string(pcb.read_text())
    doc.children = [
        n
        for n in doc.children
        if n.tag != "footprint" or n.find("property").get_string(1) not in absent
    ]
    pcb.write_text(doc.to_string())
    netlist = bank / "bank.net"
    doc = parse_string(netlist.read_text())
    components = doc.find("components")
    components.children = [
        n for n in components.children if n.find("ref").get_string() not in absent
    ]
    for net in doc.find("nets").find_all("net"):
        net.children = [
            n for n in net.children if n.tag != "node" or n.find("ref").get_string() not in absent
        ]
    netlist.write_text(doc.to_string())
    assert check(bank, "net", capsys, 1) == sorted(
        ("27", ref, source, None, "Reference", ref, None)
        for ref in absent
        for source in ("netlist", "pcb")
    )


def test_read_purchasing_fields(bank):
    native = build_netlist_from_schematic(bank / "bank.kicad_sch")
    assert native.get_component("C127").properties["LCSC"] == "C106248"
    # A fields-only export exercises the KiCad field syntax independently.
    doc = parse_string((bank / "bank.net").read_text())
    cap = doc.find("components").find("comp")
    cap.children = [n for n in cap.children if n.tag != "property"]
    assert Netlist.from_sexp(doc).get_component("C127").properties["LCSC"] == "C106248"


@pytest.mark.parametrize(
    "error",
    [
        "private-private",
        "external-private",
        "private-external",
        "missing reference",
        "duplicate reference",
        "typo",
    ],
)
def test_invalid_declaration_fails_before_comparison(bank, capsys, error):
    path = bank / "intent.json"
    intent = json.loads(path.read_text())
    ch28 = intent["instances"]["28"]
    if error == "private-private":
        ch28["nets"]["ref"] = "/REF27"
    elif error == "external-private":
        ch28["nets"]["high"] = "/REF27"
    elif error == "private-external":
        ch28["nets"]["ref"] = "T27"
    elif error == "missing reference":
        del ch28["references"]["lower"]
    elif error == "duplicate reference":
        ch28["references"]["lower"] = "R427"
    else:
        intent["roles"]["upper"]["fileds"] = {"LCSC": "C123"}
    path.write_text(json.dumps(intent))
    assert (
        main(
            [
                "validate",
                "--channel-intent",
                str(path),
                "--netlist",
                str(bank / "bank.net"),
                "--pcb",
                str(bank / "bank.kicad_pcb"),
            ]
        )
        == 2
    )
    assert "Channel intent check failed" in capsys.readouterr().err


def test_missing_numbered_pad(bank, capsys):
    path = bank / "bank.kicad_pcb"
    path.write_text(path.read_text().replace('(pad "2"', '(pad "3"', 1))
    assert check(bank, "net", capsys, 1) == [("27", "C127", "pcb", "2", None, "/REF27", None)]


def test_missing_value_and_undeclared_field(bank, capsys):
    pcb = bank / "bank.kicad_pcb"
    pcb.write_text(pcb.read_text().replace('"Value" "1 µF"', '"Unrequested" "other"', 1))
    assert check(bank, "net", capsys, 1) == [("27", "C127", "pcb", None, "Value", "1 µF", None)]


def export_bank_netlist(bank):
    subprocess.run(
        [
            "kicad-cli",
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadsexpr",
            "-o",
            str(bank / "bank.net"),
            str(bank / "bank.kicad_sch"),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli for a native export")
@pytest.mark.parametrize("miswired", [False, True])
def test_wired_private_nets_match_native_export(bank, capsys, miswired):
    sch = bank / "bank.kicad_sch"
    doc = parse_string(sch.read_text())
    doc.children = [
        node
        for node in doc.children
        if node.tag != "label" or node.get_string(0) not in {"REF27", "REF28"}
    ]
    # Each capacitor and upper resistor share a wire. Move just R427 pin 1
    # from that pair in channel 27 to the capacitor in channel 28.
    for start, end in [
        ("25.40 53.34", "38.10 53.34"),
        ("63.50 53.34" if miswired else "38.10 53.34", "50.80 48.26"),
        ("63.50 53.34", "76.20 53.34"),
        ("76.20 53.34", "88.90 48.26"),
    ]:
        doc.children.append(
            parse_string(f"(wire (pts (xy {start}) (xy {end})) (stroke (width 0) (type default)))")
        )
    sch.write_text(doc.to_string())
    for path in [bank / "intent.json", bank / "bank.kicad_pcb"]:
        text = path.read_text()
        for channel in (27, 28):
            text = text.replace(f"/REF{channel}", f"Net-(C1{channel}-Pad2)")
        path.write_text(text)
    export_bank_netlist(bank)
    for ext, source in [("net", "netlist"), ("kicad_sch", "schematic")]:
        expected = [("27", "R427", source, "1", None, "Net-(C127-Pad2)", "Net-(C128-Pad2)")]
        assert check(bank, ext, capsys, int(miswired)) == (expected if miswired else [])


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli for a native export")
def test_swapped_placement_references_match_native_export(bank, capsys):
    """Swapped placement annotations: direct schematic input agrees with a fresh native netlist."""
    sch = bank / "bank.kicad_sch"
    text = sch.read_text()
    text = text.replace('(reference "R327")', '(reference "TMP")')
    text = text.replace('(reference "R427")', '(reference "R327")')
    sch.write_text(text.replace('(reference "TMP")', '(reference "R427")'))
    export_bank_netlist(bank)
    direct = check(bank, "kicad_sch", capsys, 1)
    exported = check(bank, "net", capsys, 1)
    assert direct
    # Drop the source label: the same defects are reported against either input.
    assert [r[:2] + r[3:] for r in direct] == [r[:2] + r[3:] for r in exported]


def _add_second_project_block(sch, *, last):
    """Add a ``second_project`` annotation with R327/R427 exchanged on the shared path."""
    text = sch.read_text()
    path = "/00000000-0000-0000-0000-000000000001"
    for own, other in (("R327", "R427"), ("R427", "R327")):
        old = f'(instances (project "bank" (path "{path}" (reference "{own}") (unit 1))))'
        extra = f'(project "second_project" (path "{path}" (reference "{other}") (unit 1)))'
        assert old in text
        new = (
            f'(instances (project "bank" (path "{path}" (reference "{own}") (unit 1))) {extra})'
            if last
            else f'(instances {extra} (project "bank" (path "{path}" (reference "{own}") (unit 1))))'
        )
        text = text.replace(old, new)
    sch.write_text(text)


def test_conflicting_placement_annotations_last_one_wins(bank, capsys):
    """With a shared path the last annotation is selected, as a native export does."""
    sch = bank / "bank.kicad_sch"
    original = sch.read_text()

    # Original annotation last: a leading foreign block must not rename anything.
    _add_second_project_block(sch, last=False)
    assert check(bank, "kicad_sch", capsys, 0) == []

    # Foreign annotation last: it is the selected placement, so the dividers read swapped.
    sch.write_text(original)
    _add_second_project_block(sch, last=True)
    findings = check(bank, "kicad_sch", capsys, 1)
    assert {row[1] for row in findings} == {"R327", "R427"}
    assert len(findings) == 6
