"""Tests for the ``kct create-pcb`` CLI (issue #5808).

``create-pcb`` must resolve component footprints through the *input*
schematic's project ``fp-lib-table`` -- not just the standard KiCad
libraries -- and it must never report ``success: true`` / exit ``0`` when
one or more required footprints could not be resolved and the component was
consequently dropped from the board.

Gating strategy mirrors ``test_sch_assign_footprints.py`` /
``test_sch_suggest_footprint.py``: tests that build their own self-contained
project (schematic + ``fp-lib-table`` + ``.pretty`` directory, all under
``tmp_path``) run everywhere; tests that need a *real* standard-library
footprint (to exercise the global-library fallback specifically) are gated
behind ``detect_kicad_library_path().found``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import create_pcb_cmd
from kicad_tools.footprints.library_path import LibraryPaths, detect_kicad_library_path
from kicad_tools.schematic.models.schematic import Schematic

# Issue #3436: give real board-generation runs a contention-tolerant budget
# under `-n auto --timeout=60` full-suite xdist runs.
pytestmark = pytest.mark.timeout(300)

_LIBS_AVAILABLE = detect_kicad_library_path().found
requires_kicad_libs = pytest.mark.skipif(
    not _LIBS_AVAILABLE, reason="KiCad footprint libraries not installed in this environment"
)

# A minimal, self-contained 2-pad SMD footprint -- mirrors the fixture style
# already used in test_sch_assign_footprints.py, so it needs no real KiCad
# install to parse and place.
_TWO_PAD_FOOTPRINT = """(footprint "OnlyHere"
    (version 20240108)
    (generator "kicadtools_test")
    (layer "F.Cu")
    (attr smd)
    (pad "1" smd roundrect (at -1 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd roundrect (at 1 0) (size 1 1) (layers "F.Cu"))
)
"""

_FP_LIB_TABLE = (
    "(fp_lib_table\n"
    "  (version 7)\n"
    '  (lib (name "local") (type "KiCad") (uri "${KIPRJMOD}/local.pretty")'
    '       (options "") (descr "project-local"))\n'
    ")"
)


def _no_global_libs(*_args, **_kwargs) -> LibraryPaths:
    """Monkeypatch target: force the standard-library search to fail."""
    return LibraryPaths(footprints_path=None, source="auto")


def _build_two_resistor_project(
    tmp_path: Path,
    *,
    footprints: dict[str, str],
    with_project_table: bool = True,
    fp_lib_table_text: str | None = None,
    project_pretty_footprint: bool = True,
) -> Path:
    """Build a self-contained 2-resistor schematic + optional fp-lib-table.

    Args:
        footprints: Mapping of reference -> footprint id (e.g.
            ``{"R1": "local:OnlyHere", "R2": "local:OnlyHere"}``).
        with_project_table: Write a ``.kicad_pro`` + ``fp-lib-table`` sibling.
        fp_lib_table_text: Override the fp-lib-table content (e.g. to test
            a malformed table). Defaults to ``_FP_LIB_TABLE``.
        project_pretty_footprint: Write ``local.pretty/OnlyHere.kicad_mod``.
            Set False to test a project table whose library directory/file
            does not actually exist.

    Returns:
        Path to the written ``.kicad_sch``.
    """
    sch = Schematic(title="create-pcb project-lib test", project_name="repro")
    for ref, x in [("R1", 50), ("R2", 80)]:
        r = sch.add_symbol("Device:R", x=x, y=50, ref=ref, value="10k", footprint=footprints[ref])
        for pin, name in [("1", "SENSE"), ("2", "RETURN")]:
            px, py = r.pin_position(pin)
            sch.add_wire((px, py), (px + 5, py))
            sch.add_label(f"{ref}_{name}", px + 5, py)

    sch_path = tmp_path / "repro.kicad_sch"
    sch.write(sch_path)

    if with_project_table:
        (tmp_path / "repro.kicad_pro").write_text("{}", encoding="utf-8")
        (tmp_path / "fp-lib-table").write_text(
            fp_lib_table_text if fp_lib_table_text is not None else _FP_LIB_TABLE,
            encoding="utf-8",
        )
        if project_pretty_footprint:
            lib_dir = tmp_path / "local.pretty"
            lib_dir.mkdir()
            (lib_dir / "OnlyHere.kicad_mod").write_text(_TWO_PAD_FOOTPRINT, encoding="utf-8")

    return sch_path


# ---------------------------------------------------------------------------
# Self-contained project-fp-lib-table tests (no KiCad libs needed)
# ---------------------------------------------------------------------------


def test_all_footprints_present_reports_success(tmp_path, monkeypatch, capsys):
    """Both components resolve via the project table -> success, exit 0."""
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:OnlyHere", "R2": "local:OnlyHere"},
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 0
    assert doc["success"] is True
    assert doc["placement"]["placed"] == 2
    assert doc["placement"]["failed"] == []
    assert doc["nets"]["missing_footprints"] == []
    assert output_path.exists()


def _native_netlist(schematic: Path):
    import subprocess
    from xml.etree import ElementTree

    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("requires the native KiCad netlist exporter")
    xml = schematic.with_suffix(".xml")
    subprocess.run(
        [cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml), str(schematic)],
        check=True,
        capture_output=True,
        timeout=90,
    )
    return ElementTree.parse(xml)


def _native_symbol_paths(schematic: Path) -> dict[str, set[str]]:
    return {
        component.attrib["ref"]: {
            component.find("sheetpath").attrib["tstamps"] + unit
            for unit in component.findtext("tstamps").split()
        }
        for component in _native_netlist(schematic).findall("./components/comp")
    }


@pytest.mark.parametrize("fallback", [False, True], ids=["native", "python"])
@pytest.mark.parametrize("board_only", [False, True], ids=["shared-sheet", "board-only"])
def test_saved_footprint_paths_match_native_project(tmp_path, monkeypatch, board_only, fallback):
    import shutil
    from copy import deepcopy

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import parse_file

    fixture = Path(__file__).parent / "fixtures" / "sheet_qualified_lvs"
    root = tmp_path / "repeat_root.kicad_sch"
    shutil.copyfile(fixture / root.name, root)
    child = parse_file(fixture / "repeat_child.kicad_sch")
    if not board_only:
        for symbol in child.find_children("symbol"):
            instances = symbol.find_child("instances")
            foreign = deepcopy(instances.find_child("project"))
            foreign.set_value(0, "second-project")
            for placement in foreign.find_children("path"):
                reference = placement.find_child("reference")
                ref = reference.get_string(0)
                if ref.startswith("R"):
                    reference.set_value(0, f"R{int(ref[1:]) + 20}")
            instances.children.insert(0, foreign)
    (tmp_path / "repeat_child.kicad_sch").write_text(
        child.to_string().replace("Resistor_SMD:R_0402_1005Metric", "local:OnlyHere")
    )
    (tmp_path / "fp-lib-table").write_text(_FP_LIB_TABLE)
    library = tmp_path / "local.pretty"
    library.mkdir()
    footprint = _TWO_PAD_FOOTPRINT
    if board_only:
        footprint = footprint.replace(
            "(attr smd)", '(attr smd board_only) (path "/dead0000-0000-4000-8000-000000000001")'
        )
    (library / "OnlyHere.kicad_mod").write_text(footprint)

    expected = _native_symbol_paths(root)
    assert set(expected) == {"R1", "R2", "R11", "R12"}
    if fallback:
        monkeypatch.setattr("kicad_tools.operations.netlist.find_kicad_cli", lambda: None)
    output = tmp_path / "created.kicad_pcb"
    assert create_pcb_cmd.main([str(root), "--output", str(output)]) == 0
    saved = PCB.load(output).footprints
    assert sorted(fp.reference for fp in saved) == sorted(expected)
    for fp in saved:
        assert ("board_only" in fp.attr_flags) == board_only
        assert fp.path in ({""} if board_only else expected[fp.reference])


def _install_multiunit_footprint(tmp_path: Path) -> None:
    (tmp_path / "fp-lib-table").write_text(_FP_LIB_TABLE)
    library = tmp_path / "local.pretty"
    library.mkdir()
    pads = " ".join(
        f'(pad "{n}" smd rect (at {n} 0) (size 0.6 0.6) (layers "F.Cu"))' for n in range(1, 7)
    )
    (library / "Multi.kicad_mod").write_text(f'(footprint "Multi" (layer "F.Cu") {pads})')


@pytest.mark.parametrize("fallback", [False, True], ids=["native", "python"])
@pytest.mark.parametrize(
    ("library", "unit_pins"),
    [
        pytest.param("multiunit_test", [(1, "1"), (2, "3")], id="electrical"),
        pytest.param("graphical_unit", [(1, "1"), (2, None)], id="graphical-last"),
        pytest.param("graphical_unit", [(2, None), (1, "1")], id="graphical-first"),
    ],
)
def test_multiunit_package_has_one_saved_footprint(
    tmp_path, monkeypatch, fallback, caplog, library, unit_pins
):
    from kicad_tools.schema.pcb import PCB

    root = tmp_path / "dual.kicad_sch"
    _install_multiunit_footprint(tmp_path)
    symbols = Path(__file__).parent / "fixtures" / f"{library}.kicad_sym"
    sch = Schematic(title="Two units", project_name=root.stem, local_symbol_libs=[symbols])
    for index, (unit, pin) in enumerate(unit_pins, start=1):
        placed = sch.add_symbol(
            f"{library}:MultiUnitPart",
            50 * index,
            50,
            "U1",
            "MultiUnitPart",
            footprint="local:Multi",
            unit=unit,
        )
        if pin is not None:
            px, py = placed.pin_position(pin)
            sch.add_wire((px, py), (px - 5, py))
            sch.add_label(f"UNIT{unit}", px - 5, py)
    sch.write(root)
    expected = _native_symbol_paths(root)
    assert set(expected) == {"U1"} and len(expected["U1"]) == 2
    if fallback:
        monkeypatch.setattr("kicad_tools.operations.netlist.find_kicad_cli", lambda: None)
    output = tmp_path / "dual.kicad_pcb"
    assert create_pcb_cmd.main([str(root), "--output", str(output)]) == 0
    assert "netlist is missing components" not in caplog.text
    (footprint,) = PCB.load(output).footprints
    assert footprint.reference == "U1" and footprint.path in expected["U1"]
    nets = {p.number: p.net_name.lstrip("/") for p in footprint.pads}
    assert nets["1"] == "UNIT1"
    assert nets["3"] == ("UNIT2" if library == "multiunit_test" else "")


def _make_repeated_multiunit_project(tmp_path: Path, *, top_unit: int, last_unit: int | None):
    from shutil import copyfile

    from kicad_tools.sexp import parse_file, parse_string

    fixtures = Path(__file__).parent / "fixtures"
    root = tmp_path / "repeat_root.kicad_sch"
    copyfile(fixtures / "sheet_qualified_lvs" / root.name, root)
    _install_multiunit_footprint(tmp_path)
    child = tmp_path / "repeat_child.kicad_sch"
    sch = Schematic(
        title="Shared unit",
        project_name=root.stem,
        local_symbol_libs=[fixtures / "multiunit_test.kicad_sym"],
    )
    sym = sch.add_symbol(
        "multiunit_test:MultiUnitPart",
        50,
        50,
        "U1",
        "MultiUnitPart",
        footprint="local:Multi",
        unit=top_unit,
    )
    x, y = sym.pin_position("1" if top_unit == 1 else "3")
    sch.add_wire((x, y), (x - 5, y))
    sch.add_label("UNIT1", x - 5, y)
    sch.write(child)
    hierarchy = parse_file(root)
    doc = parse_file(child)
    symbol = doc.find_child("symbol")
    symbol.remove_child("instances")
    annotations = parse_string(f'(instances (project "{root.stem}"))')
    project = annotations.find_child("project")
    root_id = hierarchy.find_child("uuid").get_string()
    for sheet, unit in zip(hierarchy.find_children("sheet"), (1, last_unit), strict=True):
        sheet_id = sheet.find_child("uuid").get_string()
        placed_unit = "" if unit is None else f"(unit {unit})"
        project.append(
            parse_string(f'(path "/{root_id}/{sheet_id}" (reference "U1") {placed_unit})')
        )
    symbol.append(annotations)
    child.write_text(doc.to_string())
    return root


def test_repeated_sheet_units_bind_the_same_pads_as_native(tmp_path, monkeypatch):
    from kicad_tools.schema.pcb import PCB

    root = _make_repeated_multiunit_project(tmp_path, top_unit=1, last_unit=2)
    native = _native_netlist(root)
    expected = {
        node.attrib["pin"]: net.attrib["name"]
        for net in native.findall("./nets/net")
        for node in net.findall("node")
        if node.attrib["ref"] == "U1" and node.attrib["pin"] in ("1", "3")
    }
    assert expected == {"1": "/MCU_A/UNIT1", "3": "/MCU_B/UNIT1"}
    for backend in ("native", "python"):
        if backend == "python":
            monkeypatch.setattr("kicad_tools.operations.netlist.find_kicad_cli", lambda: None)
        output = tmp_path / f"{backend}.kicad_pcb"
        assert create_pcb_cmd.main([str(root), "--output", str(output)]) == 0
        (footprint,) = PCB.load(output).footprints
        assert footprint.reference == "U1"
        assert {p.number: p.net_name for p in footprint.pads if p.number in expected} == expected


def test_missing_placement_unit_keeps_the_symbols_unit(tmp_path, monkeypatch):
    from kicad_tools.schema.pcb import PCB

    root = _make_repeated_multiunit_project(tmp_path, top_unit=2, last_unit=None)
    monkeypatch.setattr("kicad_tools.operations.netlist.find_kicad_cli", lambda: None)
    output = tmp_path / "inherited.kicad_pcb"
    assert create_pcb_cmd.main([str(root), "--output", str(output)]) == 0
    (footprint,) = PCB.load(output).footprints
    nets = {p.number: p.net_name for p in footprint.pads}
    assert (nets["1"], nets["3"]) == ("/MCU_A/UNIT1", "/MCU_B/UNIT1")


@pytest.mark.parametrize("foreign_first", [True, False])
def test_python_extraction_keeps_project_annotations_with_their_symbols(
    tmp_path, monkeypatch, foreign_first
):
    from copy import deepcopy

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import parse_file

    root = _build_two_resistor_project(
        tmp_path, footprints={"R1": "local:OnlyHere", "R2": "local:OnlyHere"}
    )
    doc = parse_file(root)
    for symbol in doc.find_children("symbol"):
        instances = symbol.find_child("instances")
        foreign = deepcopy(instances.find_child("project"))
        foreign.set_value(0, "another-project")
        reference = foreign.find_child("path").find_child("reference")
        reference.set_value(0, {"R1": "R2", "R2": "R1"}[reference.get_string(0)])
        if foreign_first:
            instances.children.insert(0, foreign)
        else:
            instances.children.append(foreign)
    root.write_text(doc.to_string())
    expected = _native_symbol_paths(root)
    monkeypatch.setattr("kicad_tools.operations.netlist.find_kicad_cli", lambda: None)
    output = tmp_path / "python.kicad_pcb"
    assert create_pcb_cmd.main([str(root), "--output", str(output)]) == 0
    saved = PCB.load(output).footprints
    assert sorted(fp.reference for fp in saved) == sorted(expected) == ["R1", "R2"]
    assert all(fp.path in expected[fp.reference] for fp in saved)


def test_all_footprints_present_preserves_pad_bindings(tmp_path, monkeypatch):
    """A successful import preserves every physical pad's net binding."""
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:OnlyHere", "R2": "local:OnlyHere"},
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    assert rc == 0

    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(output_path))
    for ref in ("R1", "R2"):
        fp = pcb.get_footprint(ref)
        assert fp is not None, f"{ref} missing from saved board"
        pad_numbers = {pad.number for pad in fp.pads}
        assert pad_numbers == {"1", "2"}
        pad_nets = {pad.number: pad.net_name for pad in fp.pads}
        assert pad_nets["1"].endswith(f"{ref}_SENSE")
        assert pad_nets["2"].endswith(f"{ref}_RETURN")


def test_one_missing_footprint_reports_failure(tmp_path, monkeypatch, capsys):
    """R2's footprint cannot be resolved anywhere -> incomplete, nonzero exit."""
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:OnlyHere", "R2": "local:DoesNotExist"},
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 1
    assert doc["success"] is False
    assert doc["placement"]["placed"] == 1
    assert [f["reference"] for f in doc["placement"]["failed"]] == ["R2"]
    assert "R2" in doc["nets"]["missing_footprints"]
    # A diagnostic partial board is still written, clearly flagged incomplete
    # via success=False / exit 1 above -- not silently dropped.
    assert output_path.exists()


def test_all_missing_footprints_reports_failure(tmp_path, monkeypatch, capsys):
    """Neither footprint resolves (project or global) -> incomplete, exit 1."""
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:DoesNotExist", "R2": "local:AlsoMissing"},
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 1
    assert doc["success"] is False
    assert doc["placement"]["placed"] == 0
    assert sorted(f["reference"] for f in doc["placement"]["failed"]) == ["R1", "R2"]


def test_malformed_fp_lib_table_falls_back_gracefully(tmp_path, monkeypatch, capsys):
    """A malformed fp-lib-table must not crash -- it just contributes nothing.

    Both footprints are unresolvable (project table is garbage, no global
    libs), so this still reports failure -- the point is that a malformed
    table degrades to "no project entries" rather than raising.
    """
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:OnlyHere", "R2": "local:OnlyHere"},
        fp_lib_table_text="this is not a valid s-expression at all {{{",
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 1
    assert doc["success"] is False
    assert doc["placement"]["placed"] == 0


def test_missing_fp_lib_table_falls_back_gracefully(tmp_path, monkeypatch, capsys):
    """No fp-lib-table sibling at all -- behaves like "no project table"."""
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:OnlyHere", "R2": "local:OnlyHere"},
        with_project_table=False,
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 1
    assert doc["success"] is False
    assert doc["placement"]["placed"] == 0


def test_no_place_skip_is_not_treated_as_failure(tmp_path, monkeypatch, capsys):
    """``--no-place`` must keep exiting 0 -- an explicit skip, not a failure.

    Even with footprints that could never resolve (no project table, no
    global libs), skipping placement entirely must not trip the new
    failure-detection logic.
    """
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:DoesNotExist", "R2": "local:AlsoMissing"},
        with_project_table=False,
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main(
        [
            str(sch_path),
            "--output",
            str(output_path),
            "--format",
            "json",
            "--no-place",
        ]
    )
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 0
    assert doc["success"] is True
    assert doc["placement"]["skipped"] is True
    assert doc["placement"]["failed"] == []


# ---------------------------------------------------------------------------
# Regression test: issue #5808's own self-contained repro
# ---------------------------------------------------------------------------


def test_project_local_footprint_resolvable_only_via_fp_lib_table(tmp_path, monkeypatch, capsys):
    """Regression test for #5808.

    A footprint that exists *only* in the project's ``local.pretty`` library
    (reachable exclusively through the project's ``fp-lib-table``, with the
    standard KiCad libraries unavailable) must be placed -- not silently
    dropped. Before the fix: 0 components placed, ``success: true``, exit 0.
    After the fix: both components placed, ``success: true``, exit 0.
    """
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:OnlyHere", "R2": "local:OnlyHere"},
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 0
    assert doc["success"] is True
    assert doc["placement"]["placed"] == 2
    assert doc["placement"]["failed"] == []
    assert doc["nets"]["assigned"] == 4
    assert doc["nets"]["missing_footprints"] == []


def test_kiprjmod_resolves_against_input_schematic_when_output_elsewhere(
    tmp_path, monkeypatch, capsys
):
    """``${KIPRJMOD}`` must resolve against the *input* schematic's project
    root even when ``--output`` points somewhere else entirely (issue #5808
    acceptance criterion: "including when the output PCB is written
    elsewhere").
    """
    monkeypatch.setattr("kicad_tools.schema.pcb.detect_kicad_library_path", _no_global_libs)

    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={"R1": "local:OnlyHere", "R2": "local:OnlyHere"},
    )

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    output_path = elsewhere / "out.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 0
    assert doc["success"] is True
    assert doc["placement"]["placed"] == 2
    assert output_path.exists()


# ---------------------------------------------------------------------------
# Standard-library fallback (needs real KiCad footprint libraries)
# ---------------------------------------------------------------------------


@requires_kicad_libs
def test_standard_library_fallback_with_no_project_table(tmp_path, capsys):
    """No project table at all -- must still resolve via the standard
    KiCad libraries, exactly as before this issue's fix (regression guard
    against the project-table code path breaking the pre-existing global
    fallback)."""
    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={
            "R1": "Resistor_SMD:R_0805_2012Metric",
            "R2": "Resistor_SMD:R_0805_2012Metric",
        },
        with_project_table=False,
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 0
    assert doc["success"] is True
    assert doc["placement"]["placed"] == 2
    assert doc["placement"]["failed"] == []


@requires_kicad_libs
def test_malformed_project_table_falls_back_to_standard_library(tmp_path, capsys):
    """A malformed project fp-lib-table degrades gracefully to the standard
    library search, rather than blocking a footprint that the global
    libraries could otherwise resolve."""
    sch_path = _build_two_resistor_project(
        tmp_path,
        footprints={
            "R1": "Resistor_SMD:R_0805_2012Metric",
            "R2": "Resistor_SMD:R_0805_2012Metric",
        },
        fp_lib_table_text="not an s-expression {{{ at all",
        project_pretty_footprint=False,
    )
    output_path = tmp_path / "repro.kicad_pcb"

    rc = create_pcb_cmd.main([str(sch_path), "--output", str(output_path), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)

    assert rc == 0
    assert doc["success"] is True
    assert doc["placement"]["placed"] == 2
