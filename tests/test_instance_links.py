"""Footprint-to-symbol instance link check (Issue #10109).

A two-sheet synthetic project mirrors the acceptance controls from the
issue: the clean board's footprint paths all resolve (multi-unit U2 links
through one of its four unit UUIDs), and the damaged variant applies the
three path faults the issue describes -- C1's path removed, C2 carrying
C4's path, C3 pointing at an absent UUID -- without touching anything the
electrical checks read.  Library-identity controls change a nickname or a
footprint name while leaving the link itself intact.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult
from kicad_tools.sync.instance_links import (
    check_instance_links,
    collect_symbol_instances,
)

ROOT_UUID = "11111111-1111-1111-1111-111111111111"
SHEET_UUID = "33333333-3333-3333-3333-333333333333"

SYMBOL_UUIDS = {
    "R1": "22222222-2222-2222-2222-222222222222",
    "U2_A": "aaaa0000-0000-0000-0000-000000000001",
    "U2_B": "aaaa0000-0000-0000-0000-000000000002",
    "U2_C": "aaaa0000-0000-0000-0000-000000000003",
    "U2_D": "aaaa0000-0000-0000-0000-000000000004",
    "C1": "c1000000-0000-0000-0000-000000000001",
    "C2": "c2000000-0000-0000-0000-000000000002",
    "C3": "c3000000-0000-0000-0000-000000000003",
    "C4": "c4000000-0000-0000-0000-000000000004",
    "D1": "d1000000-0000-0000-0000-000000000001",
}

ABSENT_UUID = "dead0000-0000-0000-0000-00000000beef"


def _root_symbol(
    lib_id: str,
    reference: str,
    value: str,
    footprint: str,
    uuid: str,
    *,
    unit: int = 1,
    on_board: bool = True,
    dnp: bool = False,
) -> str:
    return f"""  (symbol
    (lib_id "{lib_id}")
    (at 100 100 0)
    (unit {unit})
    (exclude_from_sim no)
    (in_bom yes)
    (on_board {"yes" if on_board else "no"})
    (dnp {"yes" if dnp else "no"})
    (uuid "{uuid}")
    (property "Reference" "{reference}" (at 95 95 0))
    (property "Value" "{value}" (at 105 105 0))
    (property "Footprint" "{footprint}" (at 100 110 0))
    (pin "1" (uuid "00000000-0000-0000-0000-{uuid[-12:]}p1"))
  )"""


ROOT_SCH = f"""(kicad_sch
  (version 20250114)
  (generator "eeschema")
  (uuid "{ROOT_UUID}")
  (paper "A4")
  (lib_symbols)
{_root_symbol("Device:R", "R1", "10k", "Resistor_SMD:R_0805_2012Metric", SYMBOL_UUIDS["R1"])}
{_root_symbol("Analog:U2", "U2", "PIC16F887", "Package_DIP:DIP-40_W15.24mm", SYMBOL_UUIDS["U2_A"], unit=1)}
{_root_symbol("Analog:U2", "U2", "PIC16F887", "Package_DIP:DIP-40_W15.24mm", SYMBOL_UUIDS["U2_B"], unit=2)}
{_root_symbol("Analog:U2", "U2", "PIC16F887", "Package_DIP:DIP-40_W15.24mm", SYMBOL_UUIDS["U2_C"], unit=3)}
{_root_symbol("Analog:U2", "U2", "PIC16F887", "Package_DIP:DIP-40_W15.24mm", SYMBOL_UUIDS["U2_D"], unit=4)}
{_root_symbol("Mechanical:X1", "X1", "sim only", "", "eeee0000-0000-0000-0000-00000000000x", on_board=False)}
  (symbol
    (lib_id "power:GND")
    (at 50 50 0)
    (unit 1)
    (uuid "eeee0000-0000-0000-0000-00000000000p")
    (property "Reference" "#PWR01" (at 50 45 0))
    (property "Value" "GND" (at 50 55 0))
  )
  (sheet
    (at 150 50)
    (size 30 20)
    (uuid "{SHEET_UUID}")
    (property "Sheetname" "Child" (at 150 45 0))
    (property "Sheetfile" "child.kicad_sch" (at 150 75 0))
  )
)
"""

CHILD_SCH = f"""(kicad_sch
  (version 20250114)
  (generator "eeschema")
  (uuid "55555555-5555-5555-5555-555555555555")
  (paper "A4")
  (lib_symbols)
{_root_symbol("Device:C", "C1", "100n", "Capacitor_SMD:C_0402_1005Metric", SYMBOL_UUIDS["C1"])}
{_root_symbol("Device:C", "C2", "100n", "Capacitor_SMD:C_0402_1005Metric", SYMBOL_UUIDS["C2"])}
{_root_symbol("Device:C", "C3", "100n", "Capacitor_SMD:C_0402_1005Metric", SYMBOL_UUIDS["C3"])}
{_root_symbol("Device:C", "C4", "100n", "Capacitor_SMD:C_0402_1005Metric", SYMBOL_UUIDS["C4"])}
{_root_symbol("Device:D", "D1", "LED", "LED_SMD:LED_0603_1608Metric", SYMBOL_UUIDS["D1"], dnp=True)}
)
"""


def _footprint(
    lib_id: str,
    reference: str,
    value: str,
    path: str | None,
    fp_uuid: str,
    *,
    board_only: bool = False,
) -> str:
    path_line = f'    (path "{path}")' if path is not None else ""
    attr_line = "    (attr smd board_only)\n" if board_only else "    (attr smd)\n"
    return f"""  (footprint "{lib_id}"
    (layer "F.Cu")
    (uuid "{fp_uuid}")
    (at 100 100)
{attr_line}
    (property "Reference" "{reference}" (at 0 -1.2) (layer "F.SilkS"))
    (property "Value" "{value}" (at 0 1.2) (layer "F.Fab"))
{path_line}
    (pad "1" smd rect (at -0.9 0) (size 1 1.2) (layers "F.Cu" "F.Paste" "F.Mask"))
    (pad "2" smd rect (at 0.9 0) (size 1 1.2) (layers "F.Cu" "F.Paste" "F.Mask"))
  )"""


def _sheet_path(symbol_key: str) -> str:
    return f"/{SHEET_UUID}/{SYMBOL_UUIDS[symbol_key]}"


def _root_path(symbol_key: str) -> str:
    return f"/{SYMBOL_UUIDS[symbol_key]}"


def _board_text(
    *,
    c1_path: str | None = "keep",
    c2_path: str | None = "keep",
    c3_path: str | None = "keep",
    c1_lib: str = "Capacitor_SMD:C_0402_1005Metric",
    c2_lib: str = "Capacitor_SMD:C_0402_1005Metric",
    include_x1: bool = False,
) -> str:
    """The board.  ``"keep"`` means the reference's own clean path."""

    def cap_path(ref: str, override: str | None) -> str | None:
        if override == "keep":
            return _sheet_path(ref)
        return override

    footprints = [
        _footprint(
            "Resistor_SMD:R_0805_2012Metric",
            "R1",
            "10k",
            _root_path("R1"),
            "bbbb0000-0000-0000-0000-0000000000r1",
        ),
        # U2's single footprint links through unit 3's UUID: any unit of a
        # multi-unit symbol is a valid link.
        _footprint(
            "Package_DIP:DIP-40_W15.24mm",
            "U2",
            "PIC16F887",
            _root_path("U2_C"),
            "bbbb0000-0000-0000-0000-0000000000u2",
        ),
        _footprint(
            c1_lib, "C1", "100n", cap_path("C1", c1_path), "bbbb0000-0000-0000-0000-0000000000c1"
        ),
        _footprint(
            c2_lib, "C2", "100n", cap_path("C2", c2_path), "bbbb0000-0000-0000-0000-0000000000c2"
        ),
        _footprint(
            "Capacitor_SMD:C_0402_1005Metric",
            "C3",
            "100n",
            cap_path("C3", c3_path),
            "bbbb0000-0000-0000-0000-0000000000c3",
        ),
        _footprint(
            "Capacitor_SMD:C_0402_1005Metric",
            "C4",
            "100n",
            _sheet_path("C4"),
            "bbbb0000-0000-0000-0000-0000000000c4",
        ),
        _footprint(
            "LED_SMD:LED_0603_1608Metric",
            "D1",
            "LED",
            _sheet_path("D1"),
            "bbbb0000-0000-0000-0000-0000000000d1",
        ),
        # Board-only footprint: excluded from the check by design.
        _footprint(
            "Fiducial:Fiducial_1mm_Mask3mm",
            "FID1",
            "Fiducial",
            None,
            "bbbb0000-0000-0000-0000-00000000fid1",
            board_only=True,
        ),
        # Mounting hole with no schematic symbol: reference-level drift is
        # the sync report's domain, not a link finding.
        _footprint(
            "MountingHole:MountingHole_3.2mm_M3",
            "H1",
            "M3",
            None,
            "bbbb0000-0000-0000-0000-0000000000h1",
        ),
    ]
    if include_x1:
        footprints.append(
            _footprint(
                "Mechanical:X1",
                "X1",
                "sim only",
                None,
                "bbbb0000-0000-0000-0000-0000000000x1",
            )
        )
    body = "\n".join(footprints)
    return f"""(kicad_pcb
  (version 20250114)
  (generator "pcbnew")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
{body}
)
"""


def _write_project(
    tmp_path: Path,
    board_text: str,
) -> tuple[Path, Path]:
    proj = tmp_path / "project"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "root.kicad_sch").write_text(ROOT_SCH)
    (proj / "child.kicad_sch").write_text(CHILD_SCH)
    pcb_path = proj / "board.kicad_pcb"
    pcb_path.write_text(board_text)
    return proj / "root.kicad_sch", pcb_path


def _categories_by_reference(report) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for finding in report.findings:
        out.setdefault(finding.reference, []).append(finding.category)
    return out


def test_collect_symbol_instances_multi_unit_and_exclusions(tmp_path: Path) -> None:
    """U2 contributes one path per unit; excluded/power symbols do not."""
    sch, _ = _write_project(tmp_path, _board_text())
    index = collect_symbol_instances(sch)
    assert index.sheet_placements == 2  # root + child
    assert index.paths_by_reference["R1"] == {_root_path("R1")}
    assert index.paths_by_reference["U2"] == {
        _root_path("U2_A"),
        _root_path("U2_B"),
        _root_path("U2_C"),
        _root_path("U2_D"),
    }
    for ref in ("C1", "C2", "C3", "C4", "D1"):
        assert index.paths_by_reference[ref] == {_sheet_path(ref)}
    assert "X1" in index.excluded_references
    assert "X1" not in index.paths_by_reference
    assert "#PWR01" not in index.paths_by_reference
    assert index.reference_count == 7


def test_clean_project_links_intact(tmp_path: Path) -> None:
    """The clean two-sheet board cross-probes: every eligible link resolves."""
    sch, pcb = _write_project(tmp_path, _board_text())
    report = check_instance_links(sch, pcb)
    assert report.is_clean
    assert report.checked == 7  # R1, U2, C1..C4, D1
    assert report.linked == 7
    assert report.skipped_board_only == ["FID1"]
    assert report.skipped_no_symbol == ["H1"]
    assert report.warning_lines() == []
    assert report.to_dict()["findings"] == []


def test_damaged_paths_reported_by_reference(tmp_path: Path) -> None:
    """C1 path removed, C2 carrying C4's path, C3 pointing at an absent UUID."""
    sch, pcb = _write_project(
        tmp_path,
        _board_text(
            c1_path=None,
            c2_path=_sheet_path("C4"),
            c3_path=f"/{ROOT_UUID}/{ABSENT_UUID}",
        ),
    )
    report = check_instance_links(sch, pcb)
    by_ref = _categories_by_reference(report)

    assert by_ref["C1"] == ["missing_path"]
    # C2's link is both stale (the path is C4's) and a duplicate of C4's.
    assert sorted(by_ref["C2"]) == ["duplicate_path", "stale_path"]
    assert by_ref["C3"] == ["stale_path"]
    assert by_ref["C4"] == ["duplicate_path"]

    missing = next(f for f in report.findings if f.reference == "C1")
    assert "no symbol-instance path" in missing.detail
    stale_c2 = next(
        f for f in report.findings if f.reference == "C2" and f.category == "stale_path"
    )
    assert "C4" in stale_c2.detail
    stale_c3 = next(f for f in report.findings if f.reference == "C3")
    assert "no symbol instance" in stale_c3.detail
    dup_c4 = next(f for f in report.findings if f.reference == "C4")
    assert "C2" in dup_c4.detail

    # Untouched references stay linked (C4 keeps its own valid path).
    assert report.linked == 4  # R1, U2, C4, D1
    assert report.checked == 7
    assert report.count("missing_path") == 1
    assert report.count("duplicate_path") == 2
    assert report.count("stale_path") == 2

    lines = report.warning_lines()
    assert lines[0].startswith("footprint-to-symbol instance links: 7 checked, 4 linked")
    assert any(line.startswith("C1: missing path") for line in lines)


def test_library_identity_findings_separate_from_paths(tmp_path: Path) -> None:
    """A dropped nickname and a wrong library are reported with links intact."""
    sch, pcb = _write_project(
        tmp_path,
        _board_text(
            c1_lib="C_0402_1005Metric",  # nickname dropped, link intact
            c2_lib="Resistor_SMD:R_0402_1005Metric",  # wrong library entirely
        ),
    )
    report = check_instance_links(sch, pcb)
    by_ref = _categories_by_reference(report)
    assert by_ref["C1"] == ["nickname_missing"]
    assert by_ref["C2"] == ["lib_id_mismatch"]
    # The path links themselves are all intact.
    assert report.linked == 7
    assert report.count("missing_path") == 0
    assert report.count("stale_path") == 0


def test_excluded_symbol_footprint_skipped_separately(tmp_path: Path) -> None:
    """A footprint for an on_board=no symbol is its own skip bucket."""
    sch, pcb = _write_project(tmp_path, _board_text(include_x1=True))
    report = check_instance_links(sch, pcb)
    assert report.is_clean
    assert report.skipped_excluded_symbol == ["X1"]


def test_dnp_symbol_link_is_checked(tmp_path: Path) -> None:
    """DNP occupies a footprint, so its link still matters for editing."""
    sch, pcb = _write_project(tmp_path, _board_text())
    report = check_instance_links(sch, pcb)
    assert "D1" not in _categories_by_reference(report)

    dnp_missing_board = _board_text().replace(f'(path "{_sheet_path("D1")}")', "")
    sch2, pcb2 = _write_project(tmp_path / "dnp", dnp_missing_board)
    report2 = check_instance_links(sch2, pcb2)
    assert _categories_by_reference(report2)["D1"] == ["missing_path"]


def test_manufacturing_handoff_surfaces_report(tmp_path: Path) -> None:
    """The editable-project handoff records the report and warns on defects."""
    sch, pcb = _write_project(
        tmp_path,
        _board_text(
            c1_path=None,
            c2_path=_sheet_path("C4"),
            c3_path=f"/{ROOT_UUID}/{ABSENT_UUID}",
        ),
    )
    pkg = ManufacturingPackage(pcb_path=pcb, schematic_path=sch)
    result = ManufacturingResult(output_dir=tmp_path / "out")
    pkg._check_symbol_links(result)

    assert result.instance_links is not None
    assert result.instance_links["checked"] == 7
    assert len(result.instance_links["findings"]) == 5
    assert any("C1" in w and "missing path" in w for w in result.warnings)
    assert result.warnings[0].startswith("footprint-to-symbol instance links:")


def test_project_zip_step_runs_the_link_check(tmp_path: Path, monkeypatch) -> None:
    """_generate_project_zip wires the check into the handoff (Issue #10109)."""
    sch, pcb = _write_project(tmp_path, _board_text(c1_path=None))
    pkg = ManufacturingPackage(pcb_path=pcb, schematic_path=sch)

    def _fake_zip(
        pcb_path, out_dir, zip_name="kicad_project.zip", schematic_path=None, *, project_root=None
    ):
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / zip_name
        target.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        return target

    import kicad_tools.export.manufacturing as mfg_mod

    monkeypatch.setattr(mfg_mod, "_create_project_zip", _fake_zip)
    result = ManufacturingResult(output_dir=tmp_path / "out")
    pkg._generate_project_zip(tmp_path / "out", result)

    assert result.project_zip_path is not None
    assert result.instance_links is not None
    assert result.instance_links["checked"] == 7
    assert any("C1: missing path" in w for w in result.warnings)


def test_manufacturing_handoff_clean_records_empty_findings(tmp_path: Path) -> None:
    sch, pcb = _write_project(tmp_path, _board_text())
    pkg = ManufacturingPackage(pcb_path=pcb, schematic_path=sch)
    result = ManufacturingResult(output_dir=tmp_path / "out")
    pkg._check_symbol_links(result)
    assert result.instance_links == {
        "checked": 7,
        "linked": 7,
        "findings": [],
        "skipped_board_only": ["FID1"],
        "skipped_no_symbol": ["H1"],
        "skipped_excluded_symbol": [],
        "sheet_placements": 2,
        "symbol_references": 7,
    }
    assert result.warnings == []


def test_readme_section_only_for_broken_links(tmp_path: Path) -> None:
    """A clean board's README stays byte-identical; defects add a section."""
    sch, pcb = _write_project(
        tmp_path,
        _board_text(c1_path=None, c2_path=_sheet_path("C4")),
    )
    pkg = ManufacturingPackage(pcb_path=pcb, schematic_path=sch)
    result = ManufacturingResult(output_dir=tmp_path / "out")
    pkg._check_symbol_links(result)

    (tmp_path / "out").mkdir()
    pkg._generate_readme(tmp_path / "out", result)
    readme = (tmp_path / "out" / "README.txt").read_text()
    assert "Editing handoff" in readme
    assert "C1" in readme

    # Clean board: no section.
    sch2, pcb2 = _write_project(tmp_path / "clean", _board_text())
    pkg2 = ManufacturingPackage(pcb_path=pcb2, schematic_path=sch2)
    result2 = ManufacturingResult(output_dir=tmp_path / "out2")
    pkg2._check_symbol_links(result2)
    (tmp_path / "out2").mkdir()
    pkg2._generate_readme(tmp_path / "out2", result2)
    readme2 = (tmp_path / "out2" / "README.txt").read_text()
    assert "Editing handoff" not in readme2


def test_footprint_path_token_parsed_and_preserved(tmp_path: Path) -> None:
    """The schema Footprint exposes the (path ...) token; saving round-trips."""
    from kicad_tools.schema.pcb import PCB

    _, pcb = _write_project(tmp_path, _board_text())
    board = PCB.load(pcb)
    r1 = next(fp for fp in board.footprints if fp.reference == "R1")
    assert r1.path == _root_path("R1")
    fid = next(fp for fp in board.footprints if fp.reference == "FID1")
    assert fid.path == ""

    out = tmp_path / "roundtrip.kicad_pcb"
    board.save(out)
    reloaded = PCB.load(out)
    r1b = next(fp for fp in reloaded.footprints if fp.reference == "R1")
    assert r1b.path == _root_path("R1")


def test_pcb_path_with_extra_schematic_root_is_stale(tmp_path: Path) -> None:
    board = _board_text().replace(_root_path("R1"), f"/{ROOT_UUID}{_root_path('R1')}")
    sch, pcb = _write_project(tmp_path, board)
    report = check_instance_links(sch, pcb)
    assert [(f.reference, f.category) for f in report.findings] == [("R1", "stale_path")]


def test_created_component_saves_native_sheet_link(tmp_path: Path) -> None:
    from kicad_tools.operations.netlist import Netlist, NetlistComponent
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.workflow import PCBFromSchematic

    sch, pcb = _write_project(tmp_path, _board_text())
    local = sch.parent / "Fixture.pretty"
    local.mkdir()
    (local / "Land.kicad_mod").write_text('(footprint "Land" (layer "F.Cu") (attr smd))')
    (sch.parent / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "Fixture")(type "KiCad")(uri "${KIPRJMOD}/Fixture.pretty")(options "")(descr "")))'
    )
    workflow = PCBFromSchematic(sch)
    refs = ["R1", "C1", "U2"]
    workflow._netlist = Netlist(
        components=[NetlistComponent(ref, "part", "Fixture:Land", "Device:R") for ref in refs]
    )
    workflow.create_pcb()
    for i, ref in enumerate(refs):
        assert workflow.add_component(ref, 5 + 5 * i, 10) is not None
    workflow.save(pcb)
    saved = {fp.reference: fp.path for fp in PCB.load(pcb).footprints}
    assert saved["R1"] == _root_path("R1")
    assert saved["C1"] == _sheet_path("C1")
    assert saved["U2"] in {
        _root_path("U2_A"),
        _root_path("U2_B"),
        _root_path("U2_C"),
        _root_path("U2_D"),
    }
