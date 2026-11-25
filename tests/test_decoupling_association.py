"""Exercise cap ownership on actual KiCad drawings with a shared power rail."""

import json
from uuid import NAMESPACE_URL, uuid5

import pytest


def uid(name):
    return str(uuid5(NAMESPACE_URL, "kct/association/" + name))


def make_sheet(path, parts, wires=(), *, children=(), global_rail=False):
    """Each part has its supply contact at (x, y) and return at (x, y+5.08)."""

    def lib(name, kind, first, second):
        return f'''(symbol "{name}" (in_bom yes) (on_board yes)
          (symbol "{name.split(":")[1]}_1_1"
            (pin {kind} line (at 0 0 270) (length 1.27) (name "{first}") (number "1"))
            (pin {kind} line (at 0 -5.08 90) (length 1.27) (name "{second}") (number "2"))))'''

    def label(net, x, y, key):
        return f'''({"global_label" if global_rail else "label"} "{net}" (at {x} {y} 0)
          (effects (font (size 1.27 1.27))) (uuid {uid(key)}))'''

    body = []
    for ref, x, y, rail, override in parts:
        is_cap = ref.startswith("C")
        lib_id = "Device:C" if is_cap else "Test:IC"
        value = "100nF" if is_cap else "IC"
        field = f'(property "KCT_Decouples" "{override}" (at {x} {y} 0))' if override else ""
        body.extend(
            (
                f'''(symbol (lib_id "{lib_id}") (at {x} {y} 0) (unit 1)
              (in_bom yes) (on_board yes) (uuid {uid(ref)})
              (property "Reference" "{ref}" (at {x} {y} 0))
              (property "Value" "{value}" (at {x} {y} 0)) {field}
              (instances (project "{path.stem}" (path "/{uid(path.stem)}" (reference "{ref}") (unit 1)))))''',
                label(rail, x, y, ref + rail),
                label("GND", x, y + 5.08, ref + "GND"),
            )
        )
    for i, (x1, y1, x2, y2) in enumerate(wires):
        body.append(f"""(wire (pts (xy {x1} {y1}) (xy {x2} {y2}))
            (stroke (width 0) (type default)) (uuid {uid("wire" + str(i))}))""")
    for name, filename in children:
        body.append(f'''(sheet (at 152.4 50.8) (size 15.24 10.16) (uuid {uid(name)})
          (property "Sheetname" "{name}" (at 152.4 48.26 0))
          (property "Sheetfile" "{filename}" (at 152.4 63.5 0)))''')
    path.write_text(f"""(kicad_sch (version 20250114) (generator eeschema)
      (uuid {uid(path.stem)}) (paper "A4")
      (lib_symbols {lib("Device:C", "passive", "~", "~")} {lib("Test:IC", "power_in", "VDD", "GND")})
      {" ".join(body)})""")
    return path


def two_ics(folder, *, override=None, swapped=False):
    return make_sheet(
        folder / "paired.kicad_sch",
        [
            ("U1", 50.8, 50.8, "+3V3", None),
            ("U2", 101.6, 50.8, "+3V3", None),
            ("C1", 106.68 if swapped else 55.88, 50.8, "+3V3", override),
            ("C2", 55.88 if swapped else 106.68, 50.8, "+3V3", None),
        ],
        [(50.8, 50.8, 55.88, 50.8), (101.6, 50.8, 106.68, 50.8)],
    )


def associations(path):
    from kicad_tools.placement.decoupling import schematic_cap_associations

    return {row.cap.reference: row for row in schematic_cap_associations(path)}


def target(row):
    return (row.pin.reference, row.pin.pad) if row.pin else None


@pytest.mark.parametrize("swap", [False, True])
def test_two_ics_on_one_rail_follow_the_drawing(tmp_path, swap):
    rows = associations(two_ics(tmp_path, swapped=swap))
    assert target(rows["C1"]) == ("U2" if swap else "U1", "1")
    assert target(rows["C2"]) == ("U1" if swap else "U2", "1")
    assert rows["C1"].source == rows["C2"].source == "derived"
    assert rows["C1"].cap.supply_pad == "1"
    assert rows["C1"].net == "/+3V3"


def test_override_can_designate_the_other_ics_pin(tmp_path):
    rows = associations(two_ics(tmp_path, override="U2.1"))
    assert target(rows["C1"]) == target(rows["C2"]) == ("U2", "1")
    assert rows["C1"].source == "overridden"
    assert rows["C2"].source == "derived"


@pytest.mark.parametrize("field", ["U2.2", "U99.1", "U2"])
def test_unusable_override_is_visible_and_never_silently_repaired(tmp_path, field):
    row = associations(two_ics(tmp_path, override=field))["C1"]
    assert row.pin is None and row.source == "unassociated"
    assert field in row.reason


def test_lookalike_on_other_sheet_and_distant_pin_are_not_guessed(tmp_path):
    make_sheet(tmp_path / "child.kicad_sch", [("U2", 55.88, 50.8, "+3V3", None)], global_rail=True)
    path = make_sheet(
        tmp_path / "root.kicad_sch",
        [
            ("C1", 50.8, 50.8, "+3V3", None),
            ("U1", 127, 50.8, "+3V3", None),
        ],
        children=[("Interface", "child.kicad_sch")],
        global_rail=True,
    )
    row = associations(path)["C1"]
    assert row.pin is None and row.source == "unassociated" and row.reason


def test_near_label_can_associate_and_a_wired_pin_has_priority(tmp_path):
    parts = [
        ("C1", 50.8, 50.8, "+3V3", None),
        ("U1", 60.96, 50.8, "+3V3", None),
        ("U2", 50.8, 46.99, "+3V3", None),
    ]
    path = make_sheet(tmp_path / "labels.kicad_sch", parts)
    assert target(associations(path)["C1"]) == ("U2", "1")
    path = make_sheet(path, parts, [(50.8, 50.8, 60.96, 50.8)])
    assert target(associations(path)["C1"]) == ("U1", "1")


def test_equal_local_candidates_stay_unassociated(tmp_path):
    path = make_sheet(
        tmp_path / "tie.kicad_sch",
        [
            ("C1", 50.8, 50.8, "+3V3", None),
            ("U1", 45.72, 50.8, "+3V3", None),
            ("U2", 55.88, 50.8, "+3V3", None),
        ],
    )
    assert associations(path)["C1"].pin is None


@pytest.mark.parametrize("global_rail", [True, False])
def test_explicit_cross_sheet_target_requires_the_same_electrical_net(tmp_path, global_rail):
    make_sheet(
        tmp_path / "child.kicad_sch", [("U2", 50.8, 50.8, "+3V3", None)], global_rail=global_rail
    )
    path = make_sheet(
        tmp_path / "root.kicad_sch",
        [("C1", 50.8, 50.8, "+3V3", "U2.1")],
        children=[("Interface", "child.kicad_sch")],
        global_rail=global_rail,
    )
    row = associations(path)["C1"]
    assert target(row) == (("U2", "1") if global_rail else None)
    assert row.source == ("overridden" if global_rail else "unassociated")


def test_two_nearby_caps_can_designate_the_same_pin(tmp_path):
    path = make_sheet(
        tmp_path / "one-bank.kicad_sch",
        [
            ("U1", 50.8, 50.8, "+3V3", None),
            ("U2", 101.6, 50.8, "+3V3", None),
            ("C1", 45.72, 50.8, "+3V3", None),
            ("C2", 55.88, 50.8, "+3V3", None),
        ],
    )
    assert [target(row) for row in associations(path).values()] == [("U1", "1"), ("U1", "1")]


def test_reused_sheet_uses_its_annotated_references_and_local_net(tmp_path):
    child = make_sheet(
        tmp_path / "child.kicad_sch",
        [
            ("C1", 50.8, 50.8, "+3V3", None),
            ("U1", 55.88, 50.8, "+3V3", None),
        ],
    )
    drawing = child.read_text()
    for kind in ("C", "U"):
        default = f'(project "child" (path "/{uid("child")}" (reference "{kind}1") (unit 1)))'
        placements = " ".join(
            f'(path "/{uid("root")}/{uid(name)}" (reference "{kind}{n}") (unit 1))'
            for n, name in enumerate(("BankA", "BankB"), start=1)
        )
        assert default in drawing
        drawing = drawing.replace(default, f'(project "root" {placements})')
    child.write_text(drawing)
    root = make_sheet(
        tmp_path / "root.kicad_sch",
        [],
        children=[
            ("BankA", "child.kicad_sch"),
            ("BankB", "child.kicad_sch"),
        ],
    )
    rows = associations(root)
    assert [(target(r), r.sheet, r.net) for r in rows.values()] == [
        (("U1", "1"), "/BankA", "/BankA/+3V3"),
        (("U2", "1"), "/BankB", "/BankB/+3V3"),
    ]


def test_paired_and_swapped_drawings_have_identical_native_terminal_nets(tmp_path):
    import subprocess
    from xml.etree import ElementTree

    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("requires the native KiCad netlist exporter")
    expected = {
        "/+3V3": {(r, "1") for r in ("C1", "C2", "U1", "U2")},
        "/GND": {(r, "2") for r in ("C1", "C2", "U1", "U2")},
    }
    for swapped in (False, True):
        path = two_ics(tmp_path, swapped=swapped)
        xml = path.with_suffix(".xml")
        subprocess.run(
            [
                str(cli),
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "-o",
                str(xml),
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        native = {
            net.attrib["name"]: {
                (item.attrib["ref"], item.attrib["pin"]) for item in net.findall("node")
            }
            for net in ElementTree.parse(xml).findall("./nets/net")
        }
        assert native == expected
        assert target(associations(path)["C1"]) == ("U2" if swapped else "U1", "1")


def test_public_sch_output_explains_each_association(tmp_path, capsys):
    from kicad_tools.cli import main

    path = two_ics(tmp_path, override="U2.1")
    original = path.read_bytes()
    assert main(["sch", "decoupling", str(path), "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)["associations"]
    assert {r["cap"]: (r["pin"], r["source"]) for r in rows} == {
        "C1": ("U2.1", "overridden"),
        "C2": ("U2.1", "derived"),
    }
    assert main(["sch", "decoupling", str(path)]) == 0
    rendered = capsys.readouterr().out
    assert "C1" in rendered and "U2.1" in rendered and "overridden" in rendered
    assert path.read_bytes() == original
