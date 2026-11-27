"""Tests for the pre-flight validation checker."""

import csv
import json
from pathlib import Path

import pytest

from kicad_tools.export.preflight import (
    PreflightChecker,
    PreflightConfig,
    PreflightResult,
)

# ---------------------------------------------------------------------------
# PreflightResult tests
# ---------------------------------------------------------------------------


class TestPreflightResult:
    """Tests for the PreflightResult dataclass."""

    def test_to_dict_basic(self):
        r = PreflightResult(name="test", status="OK", message="all good")
        d = r.to_dict()
        assert d["name"] == "test"
        assert d["status"] == "OK"
        assert d["message"] == "all good"
        assert "details" not in d

    def test_to_dict_with_details(self):
        r = PreflightResult(name="test", status="FAIL", message="bad", details="extra info")
        d = r.to_dict()
        assert d["details"] == "extra info"

    def test_status_values(self):
        for status in ("OK", "WARN", "FAIL"):
            r = PreflightResult(name="t", status=status, message="m")
            assert r.status == status


# ---------------------------------------------------------------------------
# PreflightChecker -- PCB file checks
# ---------------------------------------------------------------------------


class TestPreflightPCBFile:
    """Tests for the PCB file check."""

    def test_missing_pcb_fails(self, tmp_path):
        checker = PreflightChecker(
            pcb_path=tmp_path / "nonexistent.kicad_pcb",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        pcb_result = _find_result(results, "pcb_file")
        assert pcb_result is not None
        assert pcb_result.status == "FAIL"

    def test_valid_pcb_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        pcb_result = _find_result(results, "pcb_file")
        assert pcb_result is not None
        assert pcb_result.status == "OK"

    def test_unparseable_pcb_fails(self, tmp_path):
        pcb = tmp_path / "broken.kicad_pcb"
        pcb.write_text("this is not valid s-expression")
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        pcb_result = _find_result(results, "pcb_file")
        assert pcb_result is not None
        assert pcb_result.status == "FAIL"


# ---------------------------------------------------------------------------
# PreflightChecker -- schematic checks
# ---------------------------------------------------------------------------


class TestPreflightSchematic:
    """Tests for the schematic file check."""

    def test_no_schematic_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=None,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        sch_result = _find_result(results, "schematic_file")
        assert sch_result is not None
        assert sch_result.status in ("OK", "WARN")

    def test_missing_schematic_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=tmp_path / "nonexistent.kicad_sch",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        sch_result = _find_result(results, "schematic_file")
        assert sch_result is not None
        assert sch_result.status == "WARN"

    def test_existing_schematic_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        sch_result = _find_result(results, "schematic_file")
        assert sch_result is not None
        assert sch_result.status == "OK"

    def test_auto_detect_schematic(self, tmp_path):
        """If schematic_path is None, auto-detect from PCB name."""
        pcb = _create_minimal_pcb(tmp_path)
        # Create matching schematic
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=None,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        sch_result = _find_result(results, "schematic_file")
        assert sch_result is not None
        assert sch_result.status == "OK"
        assert "auto-detected" in sch_result.message


# ---------------------------------------------------------------------------
# PreflightChecker -- schematic footprint completeness gate (issue #3866)
# ---------------------------------------------------------------------------


_SCH_WITH_MISSING_FP = """(kicad_sch
    (version 20231120)
    (generator "test")
    (uuid "00000000-0000-0000-0000-0000000000ff")
    (paper "A4")
    (lib_symbols)
    (symbol
        (lib_id "Device:R")
        (at 50 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Reference" "R1" (at 0 0 0))
        (property "Value" "10k" (at 0 0 0))
        (property "Footprint" "" (at 0 0 0))
        (pin "1" (uuid "11111111-1111-1111-1111-000000000001"))
        (pin "2" (uuid "11111111-1111-1111-1111-000000000002"))
        (instances (project "test" (path "/" (reference "R1") (unit 1))))
    )
    (sheet_instances (path "/" (page "1")))
)
"""

_SCH_ALL_FP_SET = """(kicad_sch
    (version 20231120)
    (generator "test")
    (uuid "00000000-0000-0000-0000-0000000000fe")
    (paper "A4")
    (lib_symbols)
    (symbol
        (lib_id "Device:R")
        (at 50 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "22222222-2222-2222-2222-222222222222")
        (property "Reference" "R1" (at 0 0 0))
        (property "Value" "10k" (at 0 0 0))
        (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 0 0 0))
        (pin "1" (uuid "22222222-2222-2222-2222-000000000001"))
        (pin "2" (uuid "22222222-2222-2222-2222-000000000002"))
        (instances (project "test" (path "/" (reference "R1") (unit 1))))
    )
    (sheet_instances (path "/" (page "1")))
)
"""


class TestPreflightSchematicFootprints:
    """The schematic-footprint completeness gate fails loud (issue #3866)."""

    def test_missing_footprint_fails(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        sch = tmp_path / "board.kicad_sch"
        sch.write_text(_SCH_WITH_MISSING_FP)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        fp_result = _find_result(results, "schematic_footprints")
        assert fp_result is not None
        assert fp_result.status == "FAIL"
        assert "R1" in fp_result.details
        assert "assign-footprints" in fp_result.details
        # The overall preflight is blocked.
        assert checker.has_failures(results)

    def test_all_footprints_present_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        sch = tmp_path / "board.kicad_sch"
        sch.write_text(_SCH_ALL_FP_SET)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        fp_result = _find_result(results, "schematic_footprints")
        assert fp_result is not None
        assert fp_result.status == "OK"

    def test_blank_schematic_footprint_but_placed_on_pcb_ok(self, tmp_path):
        """Export reconciliation: a schematic-blank Footprint field whose
        component IS placed on the PCB with a footprint must NOT block
        export (issue #3866 / board-05).

        The export gate runs on a routed PCB about to be manufactured. A
        component that is on the PCB with a concrete footprint is
        manufacturable even if its *schematic* Footprint field is blank
        (e.g. a generator that builds the PCB but never writes footprints
        back into the schematic -- the board-05 case). This is the
        reconciliation the Judge required."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1"])
        sch = tmp_path / "board.kicad_sch"
        # R1 has a BLANK schematic Footprint field...
        sch.write_text(_SCH_WITH_MISSING_FP)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        fp_result = _find_result(results, "schematic_footprints")
        assert fp_result is not None
        # ...but R1 IS on the PCB with a footprint -> manufacturable -> OK.
        assert fp_result.status == "OK", fp_result.details
        assert not checker.has_failures(results)

    def test_blank_schematic_footprint_absent_from_pcb_fails(self, tmp_path):
        """Export reconciliation: a schematic component with a blank
        Footprint field that is ALSO absent from the PCB is genuinely
        unmanufacturable and MUST block export (issue #3866)."""
        # PCB has a different component (C9), so R1 from the schematic is
        # absent from the PCB entirely.
        pcb = _create_pcb_with_footprints(tmp_path, ["C9"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text(_SCH_WITH_MISSING_FP)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        fp_result = _find_result(results, "schematic_footprints")
        assert fp_result is not None
        assert fp_result.status == "FAIL", fp_result.message
        assert "R1" in fp_result.details
        assert "assign-footprints" in fp_result.details
        assert checker.has_failures(results)

    def test_blank_schematic_footprint_on_pcb_without_footprint_fails(self, tmp_path):
        """Export reconciliation: a schematic component present on the PCB
        but WITHOUT a library footprint name is unmanufacturable and MUST
        block export (issue #3866)."""
        # A PCB footprint for R1 with an EMPTY library footprint name.
        pcb_content = """(kicad_pcb (version 20231014) (generator "pcbnew")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (footprint ""
    (layer "F.Cu")
    (at 25 25)
    (attr smd)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(pcb_content)
        sch = tmp_path / "board.kicad_sch"
        sch.write_text(_SCH_WITH_MISSING_FP)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        fp_result = _find_result(results, "schematic_footprints")
        assert fp_result is not None
        assert fp_result.status == "FAIL", fp_result.message
        assert "R1" in fp_result.details
        assert checker.has_failures(results)


# ---------------------------------------------------------------------------
# PreflightChecker -- board outline checks
# ---------------------------------------------------------------------------


class TestPreflightBoardOutline:
    """Tests for board outline validation."""

    def test_no_outline_fails(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, include_outline=False)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        outline_result = _find_result(results, "board_outline")
        assert outline_result is not None
        assert outline_result.status == "FAIL"

    def test_closed_outline_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, include_outline=True)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        outline_result = _find_result(results, "board_outline")
        assert outline_result is not None
        assert outline_result.status == "OK"

    def test_gr_rect_outline_ok(self, tmp_path):
        """Board outline defined via gr_rect on Edge.Cuts should pass."""
        pcb_content = """(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_rect (start 0 0) (end 50 50)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
)
"""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(pcb_content)
        checker = PreflightChecker(
            pcb_path=pcb_path,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        outline_result = _find_result(results, "board_outline")
        assert outline_result is not None
        assert outline_result.status == "OK"

    def test_no_outline_error_message_empty(self, tmp_path):
        """When Edge.Cuts has no content, message says 'No board outline found'."""
        pcb = _create_minimal_pcb(tmp_path, include_outline=False)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        outline_result = _find_result(results, "board_outline")
        assert outline_result is not None
        assert "No board outline found" in outline_result.message


# ---------------------------------------------------------------------------
# PreflightChecker -- board dimensions checks
# ---------------------------------------------------------------------------


class TestPreflightBoardDimensions:
    """Tests for board dimension validation."""

    def test_normal_size_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, include_outline=True)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        dim_result = _find_result(results, "board_dimensions")
        assert dim_result is not None
        assert dim_result.status == "OK"


# ---------------------------------------------------------------------------
# PreflightChecker -- drill holes check
# ---------------------------------------------------------------------------


class TestPreflightDrillHoles:
    """Tests for drill holes presence check."""

    def test_no_drill_holes_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, include_outline=True)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        drill_result = _find_result(results, "drill_holes")
        assert drill_result is not None
        # A minimal board with no components: WARN expected
        assert drill_result.status == "WARN"

    def test_with_vias_ok(self, tmp_path):
        pcb = _create_pcb_with_via(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        drill_result = _find_result(results, "drill_holes")
        assert drill_result is not None
        assert drill_result.status == "OK"


# ---------------------------------------------------------------------------
# PreflightChecker -- assembly lead fit (issue #10299)
# ---------------------------------------------------------------------------

# The fabricator's lower finished-hole tolerance (4-space indented for the
# ``requirements.manufacturing`` block).
_LEAD_FIT_TOLERANCE_YAML = (
    "    finished_hole_tolerance_mm: -0.08\n"
    "    finished_hole_tolerance_source: JLCPCB capabilities, through-hole tolerances\n"
)

# The STW70N60DM2 lead from the original report (issue #10299): a
# 1.40 x 0.80 mm rectangular lead whose 1.61245 mm diagonal exceeds a
# 1.50 mm finished hole.
_RECT_LEAD_YAML = (
    "  - ref: Q1\n"
    "    part: STW70N60DM2\n"
    "    lead:\n"
    "      shape: rectangular\n"
    "      width_mm: 1.40\n"
    "      thickness_mm: 0.80\n"
    '      source: "STW70N60DM2 datasheet package drawing, pp. 9-10"\n'
)


def _create_pcb_with_lead_pads(
    directory: Path,
    ref_pads: dict[str, list[str]],
    filename: str = "board.kicad_pcb",
    lib_ids: dict[str, str] | None = None,
) -> Path:
    """Create a PCB whose footprints carry raw ``(pad ...)`` node strings.

    Each *ref_pads* value is a list of full pad S-expression strings, so
    tests control the drill geometry (round, slot, non-plated, SMD)
    exactly.  A closed board outline is included so other checks pass.
    *lib_ids* overrides a reference's footprint library id.
    """
    footprints = []
    for i, (ref, pads) in enumerate(ref_pads.items()):
        x = 10.0 + i * 10.0
        pad_block = "\n    ".join(pads)
        lib_id = (lib_ids or {}).get(ref, "Package_TO_SOT_THT:TO-247-3_Vertical")
        footprints.append(
            f"""  (footprint "{lib_id}"
    (layer "F.Cu")
    (at {x} 25)
    (attr through_hole)
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value "PART" (at 0 1.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    {pad_block}
  )"""
        )

    fp_block = "\n".join(footprints)
    content = f"""(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
{fp_block}
)
"""
    pcb_path = directory / filename
    pcb_path.write_text(content)
    return pcb_path


def _tht_pad(number: str, drill: float) -> str:
    """A plated round-hole through-hole pad."""
    return (
        f'(pad "{number}" thru_hole circle (at 0 0) (size 2.7 2.7) '
        f'(drill {drill}) (layers "*.Cu" "*.Mask") (net 0 ""))'
    )


def _tht_pad_at(number: str, drill: float, x: float, y: float = 0.0) -> str:
    """A plated round-hole through-hole pad at a footprint-local position."""
    return (
        f'(pad "{number}" thru_hole circle (at {x} {y}) (size 2.7 2.7) '
        f'(drill {drill}) (layers "*.Cu" "*.Mask") (net 0 ""))'
    )


def _slot_pad(number: str, w: float, h: float) -> str:
    """A plated slot-hole through-hole pad."""
    return (
        f'(pad "{number}" thru_hole oval (at 0 0) (size 2.7 3.9) '
        f'(drill oval {w} {h}) (layers "*.Cu" "*.Mask") (net 0 ""))'
    )


def _np_pad(number: str, drill: float) -> str:
    """A non-plated through-hole pad (np_thru_hole)."""
    return (
        f'(pad "{number}" np_thru_hole circle (at 0 0) (size 2.7 2.7) '
        f'(drill {drill}) (layers "*.Cu" "*.Mask") (net 0 ""))'
    )


def _smd_pad(number: str) -> str:
    return (
        f'(pad "{number}" smd rect (at 0 0) (size 1.0 1.0) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))'
    )


def _write_lead_spec(
    directory: Path,
    entries: str = "",
    manufacturing: str = "",
) -> Path:
    """Write ``project.kct`` with optional ``bom_entries`` and manufacturing block.

    *entries* is the (already 2-space indented) ``bom_entries`` list YAML;
    *manufacturing* is the (already 4-space indented) content of
    ``requirements.manufacturing``.
    """
    text = "kct_version: '1.0'\nproject:\n  name: T\n"
    if manufacturing:
        text += "requirements:\n  manufacturing:\n" + manufacturing
    if entries:
        text += "bom_entries:\n" + entries
    spec_file = directory / "project.kct"
    spec_file.write_text(text)
    return spec_file


def _run_lead_fit(pcb_path: Path):
    checker = PreflightChecker(
        pcb_path=pcb_path,
        config=PreflightConfig(skip_drc=True, skip_erc=True),
    )
    results = checker.run_all()
    result = _find_result(results, "assembly_lead_fit")
    assert result is not None, "assembly_lead_fit result missing from run_all()"
    return result


class TestPreflightLeadFit:
    """Through-hole lead dimensions vs the finished plated hole (issue #10299)."""

    @pytest.mark.parametrize("no_spec", [False, True])
    def test_export_spec_selection(self, tmp_path, no_spec):
        from kicad_tools.cli.export_cmd import build_manufacturing_config
        from kicad_tools.export.manufacturing import ManufacturingPackage

        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 1.5)]})
        _write_lead_spec(
            tmp_path,
            _RECT_LEAD_YAML.replace("STW70N60DM2", "SMALL").replace("1.40", "0.4"),
            _LEAD_FIT_TOLERANCE_YAML,
        )
        variant = tmp_path / "variant"
        variant.mkdir()
        selected = _write_lead_spec(variant, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)
        config = build_manufacturing_config(
            output_dir=tmp_path,
            spec_path=selected,
            no_spec=no_spec,
            strict_preflight=True,
            skip_drc=True,
            skip_erc=True,
        )
        results = ManufacturingPackage(pcb_path=pcb, config=config)._run_preflight()
        result = _find_result(results, "assembly_lead_fit")
        (pad,) = result.data["pads"]
        if no_spec:
            assert result.status == "WARN"
            assert pad["status"] == "unassessed" and pad["mpn"] is None
        else:
            assert result.status == "FAIL"
            assert pad["mpn"] == "STW70N60DM2"

    @pytest.mark.parametrize(
        ("profile", "reason"),
        [
            ("{shape: rectangular, width_mm: 1.4, source: drawing}", "thickness_mm"),
            ("{shape: oval, source: drawing}", "shape"),
        ],
    )
    def test_invalid_profile_is_local(self, tmp_path, profile, reason):
        pcb = _create_pcb_with_lead_pads(
            tmp_path,
            {
                "Q1": [_tht_pad("1", 1.5), _tht_pad("2", 1.5)],
                "Q2": [_tht_pad("1", 1.5)],
            },
        )
        _write_lead_spec(
            tmp_path,
            _RECT_LEAD_YAML
            + f'    pad_leads:\n      "2": {profile}\n'
            + f"  - ref: Q2\n    part: SECOND\n    lead: {profile}\n",
            _LEAD_FIT_TOLERANCE_YAML,
        )
        result = _run_lead_fit(pcb)
        assert result.status == "FAIL"
        good, override, other = result.data["pads"]
        assert good["status"] == "fail" and good["mpn"] == "STW70N60DM2"
        assert override["mpn"] == good["mpn"] and other["mpn"] == "SECOND"
        for finding in (override, other):
            assert finding["status"] == "unassessed"
            assert reason in finding["reason"]

    @pytest.mark.parametrize(
        ("overrides", "manufacturing", "pad_error", "tolerance_error"),
        [
            (
                "{1: {shape: round, diameter_mm: 1, source: drawing}}",
                _LEAD_FIT_TOLERANCE_YAML,
                None,
                None,
            ),
            ("[]", _LEAD_FIT_TOLERANCE_YAML, "pad_leads", None),
            ("{1: {shape: oval, source: drawing}}", _LEAD_FIT_TOLERANCE_YAML, "shape", None),
            (
                '{"1": {shape: round, diameter_mm: 1, source: drawing}}',
                "    finished_hole_tolerance_mm: -0.08\n",
                None,
                "finished_hole_tolerance_source",
            ),
            (
                "[]",
                "    finished_hole_tolerance_mm: 0.08\n",
                "pad_leads",
                "finished_hole_tolerance_mm",
            ),
        ],
    )
    def test_preserve_nominal_fit_and_exported_assignment(
        self, tmp_path, overrides, manufacturing, pad_error, tolerance_error
    ):
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        pcb = _create_pcb_with_lead_pads(
            tmp_path, {ref: [_tht_pad("1", 1.5)] for ref in ("Q1", "Q2")}
        )
        _write_lead_spec(
            tmp_path,
            _RECT_LEAD_YAML
            + "    lcsc: C12345\n  - ref: Q2\n    part: SECOND\n"
            + "    lead: {shape: round, diameter_mm: 2, source: drawing}\n"
            + f"    pad_leads: {overrides}\n",
            manufacturing,
        )
        result = _run_lead_fit(pcb)
        assert result.status == "FAIL"
        q1, q2 = result.data["pads"]
        assert q1["mpn"] == "STW70N60DM2"
        assert q1["nominal_status"] == "fail"
        assert q1["nominal_margin_mm"] == -0.112
        if tolerance_error:
            assert q1["tolerance"]["status"] == "unassessed"
            assert tolerance_error in q1["tolerance"]["reason"]
            assert "minimum_margin_mm" not in q1["tolerance"]
        else:
            assert q1["tolerance"]["minimum_margin_mm"] == -0.192
        assert q2["mpn"] == "SECOND"
        if pad_error:
            assert q2["status"] == "unassessed"
            assert pad_error in q2["reason"]
        else:
            assert q2["nominal_margin_mm"] == 0.5

        package = AssemblyPackage(
            pcb_path=pcb,
            schematic_path=None,
            config=AssemblyConfig(bom_source="pcb", auto_lcsc=False),
        )
        exported = package.export(tmp_path / "assembly")
        with exported.bom_path.open(newline="", encoding="utf-8-sig") as bom:
            assignments = {row["Designator"]: row["LCSC Part #"] for row in csv.DictReader(bom)}
        assert assignments["Q1"] == "C12345"

    @pytest.mark.parametrize(
        ("fab", "status"), [("pcbway", "unassessed"), ("jlcpcb-tier1", "pass")]
    )
    def test_fabricator_tolerance(self, tmp_path, fab, status):
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 2.2)]})
        _write_lead_spec(
            tmp_path, _RECT_LEAD_YAML, "    target_fab: jlcpcb\n" + _LEAD_FIT_TOLERANCE_YAML
        )
        results = PreflightChecker(pcb, manufacturer=fab).run_all()
        (pad,) = _find_result(results, "assembly_lead_fit").data["pads"]
        assert pad["nominal_status"] == "pass"
        assert pad["tolerance"]["status"] == status
        if status == "unassessed":
            assert fab in pad["tolerance"]["reason"]
            assert "minimum_margin_mm" not in pad["tolerance"]

    @pytest.mark.parametrize(("diameter", "status"), [(1.10, "pass"), (1.10001, "fail")])
    def test_minimum_clearance_before_display_rounding(self, tmp_path, diameter, status):
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 1.2)]})
        _write_lead_spec(
            tmp_path,
            f"  - ref: Q1\n    part: ROUND\n    lead: {{shape: round, diameter_mm: {diameter}, source: drawing}}\n",
            _LEAD_FIT_TOLERANCE_YAML.replace("-0.08", "-0.10"),
        )
        (pad,) = _run_lead_fit(pcb).data["pads"]
        assert pad["status"] == status
        assert pad["tolerance"]["status"] == status
        assert pad["tolerance"]["minimum_margin_mm"] == 0.0

    def test_dnp_footprint_excluded(self, tmp_path):
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 1.5)]})
        pcb.write_text(pcb.read_text().replace("(attr through_hole)", "(attr through_hole dnp)"))
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)
        result = _run_lead_fit(pcb)
        assert result.status == "OK"
        assert result.data["pads"] == []

    def test_rectangular_lead_fails_nominal_fit_on_1_50_mm_hole(self, tmp_path):
        """The original report's case: 1.61245 mm diagonal vs a 1.50 mm hole."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 1.50)]})
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)

        result = _run_lead_fit(pcb)

        assert result.status == "FAIL"
        (finding,) = result.data["pads"]
        assert finding["status"] == "fail"
        assert finding["nominal_status"] == "fail"
        assert finding["nominal_margin_mm"] == pytest.approx(-0.112)
        assert finding["required_diameter_mm"] == pytest.approx(1.612)
        assert finding["tolerance"]["minimum_hole_mm"] == pytest.approx(1.42)
        assert finding["tolerance"]["minimum_margin_mm"] == pytest.approx(-0.192)

    def test_assessed_finding_carries_full_evidence(self, tmp_path):
        """Each finding identifies MPN, reference, pad, hole, lead and source."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("2", 2.20)]})
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)

        result = _run_lead_fit(pcb)

        (finding,) = result.data["pads"]
        assert finding["reference"] == "Q1"
        assert finding["pad"] == "2"
        assert finding["mpn"] == "STW70N60DM2"
        assert finding["hole"] == {"shape": "round", "diameter_mm": pytest.approx(2.2)}
        assert finding["lead"]["shape"] == "rectangular"
        assert finding["lead"]["width_mm"] == pytest.approx(1.4)
        assert finding["lead"]["thickness_mm"] == pytest.approx(0.8)
        assert finding["lead"]["source"] == "STW70N60DM2 datasheet package drawing, pp. 9-10"
        assert finding["status"] == "pass"
        # details carries the same evidence in prose
        assert "Q1 pad 2 [STW70N60DM2]" in result.details
        assert "STW70N60DM2 datasheet package drawing, pp. 9-10" in result.details

    def test_rectangular_lead_passes_on_2_20_mm_hole(self, tmp_path):
        """2.20 mm hole at -0.08 mm tolerance keeps +0.508 mm clearance."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 2.20)]})
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)

        result = _run_lead_fit(pcb)

        assert result.status == "OK"
        (finding,) = result.data["pads"]
        assert finding["status"] == "pass"
        assert finding["tolerance"]["minimum_hole_mm"] == pytest.approx(2.12)
        assert finding["tolerance"]["minimum_margin_mm"] == pytest.approx(0.508)
        assert finding["tolerance"]["tolerance_mm"] == pytest.approx(-0.08)
        assert finding["tolerance"]["source"] == "JLCPCB capabilities, through-hole tolerances"

    def test_rectangular_lead_fails_at_tolerance_on_1_65_mm_hole(self, tmp_path):
        """+0.038 mm nominal turns into -0.042 mm at minimum finish."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 1.65)]})
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)

        result = _run_lead_fit(pcb)

        assert result.status == "FAIL"
        (finding,) = result.data["pads"]
        assert finding["nominal_status"] == "pass"
        assert finding["nominal_margin_mm"] == pytest.approx(0.038)
        assert finding["tolerance"]["status"] == "fail"
        assert finding["tolerance"]["minimum_margin_mm"] == pytest.approx(-0.042)
        assert finding["status"] == "fail"

    def test_round_lead_passes_with_tolerance_margin(self, tmp_path):
        """A 1.00 mm round lead in a 1.20 mm hole keeps +0.120 mm minimum."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 1.20)]})
        _write_lead_spec(
            tmp_path,
            "  - ref: Q1\n"
            "    part: LEAD-R1\n"
            "    lead:\n"
            "      shape: round\n"
            "      diameter_mm: 1.00\n"
            '      source: "measured with calipers 2026-11-20"\n',
            _LEAD_FIT_TOLERANCE_YAML,
        )

        result = _run_lead_fit(pcb)

        assert result.status == "OK"
        (finding,) = result.data["pads"]
        assert finding["required_diameter_mm"] == pytest.approx(1.0)
        assert finding["nominal_margin_mm"] == pytest.approx(0.2)
        assert finding["tolerance"]["minimum_margin_mm"] == pytest.approx(0.120)
        assert finding["status"] == "pass"

    def test_slot_hole_unassessed_with_both_axes(self, tmp_path):
        """A slot's long axis must not be mistaken for a round hole diameter."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_slot_pad("1", 1.00, 2.20)]})
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)

        result = _run_lead_fit(pcb)

        assert result.status == "WARN"
        (finding,) = result.data["pads"]
        assert finding["status"] == "unassessed"
        assert finding["hole"] == {
            "shape": "slot",
            "size_mm": [pytest.approx(1.0), pytest.approx(2.2)],
        }
        assert "slot" in finding["reason"]
        assert "1.000" in finding["reason"]
        assert "2.200" in finding["reason"]
        assert "slot hole 1.000 x 2.200 mm" in result.details

    def test_reference_range_and_pad_override(self, tmp_path):
        """A range entry covers multiple refs; one pad number can override."""
        pcb = _create_pcb_with_lead_pads(
            tmp_path,
            {
                "Q1": [_tht_pad("1", 2.20), _tht_pad("2", 1.20)],
                "Q2": [_tht_pad("1", 2.20)],
            },
        )
        _write_lead_spec(
            tmp_path,
            "  - ref: Q1-Q2\n"
            "    part: STW70N60DM2\n"
            "    lead:\n"
            "      shape: rectangular\n"
            "      width_mm: 1.40\n"
            "      thickness_mm: 0.80\n"
            '      source: "STW70N60DM2 datasheet package drawing, pp. 9-10"\n'
            "    pad_leads:\n"
            "      '2':\n"
            "        shape: round\n"
            "        diameter_mm: 1.00\n"
            '        source: "measured with calipers 2026-11-20"\n',
            _LEAD_FIT_TOLERANCE_YAML,
        )

        result = _run_lead_fit(pcb)

        assert result.status == "OK"
        by_pad = {(f["reference"], f["pad"]): f for f in result.data["pads"]}
        assert set(by_pad) == {("Q1", "1"), ("Q1", "2"), ("Q2", "1")}
        # The part-level rectangular lead applies to Q1 pad 1 and Q2 pad 1
        for key in (("Q1", "1"), ("Q2", "1")):
            f = by_pad[key]
            assert f["mpn"] == "STW70N60DM2"
            assert f["lead"]["shape"] == "rectangular"
            assert f["lead"]["source"] == "STW70N60DM2 datasheet package drawing, pp. 9-10"
            assert f["status"] == "pass"
            assert f["tolerance"]["minimum_margin_mm"] == pytest.approx(0.508)
        # The pad-number override applies to Q1 pad 2 only
        override = by_pad[("Q1", "2")]
        assert override["lead"]["shape"] == "round"
        assert override["lead"]["diameter_mm"] == pytest.approx(1.0)
        assert override["lead"]["source"] == "measured with calipers 2026-11-20"
        assert override["tolerance"]["minimum_margin_mm"] == pytest.approx(0.120)

    def test_missing_lead_data_does_not_hide_other_pad_failure(self, tmp_path):
        """One pad's missing data must not hide another pad's failure."""
        pcb = _create_pcb_with_lead_pads(
            tmp_path,
            {
                "Q1": [_tht_pad("1", 1.50)],
                "J1": [_tht_pad("1", 1.00)],
            },
        )
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)

        result = _run_lead_fit(pcb)

        assert result.status == "FAIL"
        by_ref = {f["reference"]: f for f in result.data["pads"]}
        assert by_ref["Q1"]["status"] == "fail"
        unassessed = by_ref["J1"]
        assert unassessed["status"] == "unassessed"
        assert unassessed["mpn"] is None
        assert "J1" in unassessed["reason"]
        assert "no bom_entries assignment" in unassessed["reason"]

    def test_entry_without_lead_reports_unassessed_with_mpn(self, tmp_path):
        """A declared part without lead dimensions is named in the reason."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 1.50)]})
        _write_lead_spec(tmp_path, "  - ref: Q1\n    part: NOPART\n")

        result = _run_lead_fit(pcb)

        assert result.status == "WARN"
        (finding,) = result.data["pads"]
        assert finding["status"] == "unassessed"
        assert finding["mpn"] == "NOPART"
        assert "no verified lead dimensions declared for pad 1 of NOPART" in finding["reason"]

    def test_pad_override_missing_on_mixed_package(self, tmp_path):
        """A pad without an override on an override-only entry is unassessed."""
        pcb = _create_pcb_with_lead_pads(
            tmp_path, {"Q1": [_tht_pad("1", 1.20), _tht_pad("2", 1.50)]}
        )
        _write_lead_spec(
            tmp_path,
            "  - ref: Q1\n"
            "    part: MIXED\n"
            "    pad_leads:\n"
            "      '1':\n"
            "        shape: round\n"
            "        diameter_mm: 1.00\n"
            '        source: "measured"\n',
        )

        result = _run_lead_fit(pcb)

        by_pad = {f["pad"]: f for f in result.data["pads"]}
        assert by_pad["1"]["status"] == "pass"
        assert by_pad["2"]["status"] == "unassessed"
        assert "pad 2" in by_pad["2"]["reason"]

    def test_unknown_tolerance_reports_nominal_and_marks_tolerance_unassessed(self, tmp_path):
        """Valid lead data + unknown tolerance: nominal fit, tolerance unassessed."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 2.20)]})
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML)

        result = _run_lead_fit(pcb)

        assert result.status == "WARN"
        (finding,) = result.data["pads"]
        assert finding["status"] == "pass"
        assert finding["nominal_margin_mm"] == pytest.approx(0.588)
        assert finding["tolerance"]["status"] == "unassessed"
        assert "finished-hole tolerance" in finding["tolerance"]["reason"]
        assert "minimum_hole_mm" not in finding["tolerance"]
        assert result.data["summary"]["tolerance_unassessed"] == 1
        assert "tolerance unassessed" in result.details

    def test_non_plated_smd_and_via_pads_excluded(self, tmp_path):
        """np_thru_hole, SMD pads and vias never produce lead-fit findings."""
        pcb = _create_pcb_with_lead_pads(
            tmp_path,
            {"Q1": [_np_pad("1", 1.50), _smd_pad("2"), _tht_pad("3", 1.50)]},
        )
        content = pcb.read_text()
        content = content.replace(
            '(net 0 "")\n',
            '(net 0 "")\n  (via (at 25 25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0))\n',
        )
        pcb.write_text(content)
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)

        result = _run_lead_fit(pcb)

        findings = result.data["pads"]
        assert [f["pad"] for f in findings] == ["3"]
        assert result.status == "FAIL"  # pad 3 is the 1.50 mm failure

    def test_no_declarations_cannot_produce_fit_claim(self, tmp_path):
        """A board with no lead declarations reports unassessed, never a fit."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 2.20)]})
        # A spec with BOM entries but no lead declarations anywhere.
        _write_lead_spec(tmp_path, "  - ref: Q1\n    part: STW70N60DM2\n")

        result = _run_lead_fit(pcb)

        assert result.status == "WARN"
        (finding,) = result.data["pads"]
        assert finding["status"] == "unassessed"
        assert result.message.startswith("Through-hole lead fit unverified")

    def test_no_spec_cannot_produce_fit_claim(self, tmp_path):
        """With no .kct at all, every plated pad is unassessed."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 2.20)]})

        result = _run_lead_fit(pcb)

        assert result.status == "WARN"
        assert result.data["summary"]["unassessed"] == 1
        assert result.data["summary"]["failed"] == 0

    def test_no_through_hole_pads_ok(self, tmp_path):
        """An all-SMD board has nothing to assess."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"R1": [_smd_pad("1"), _smd_pad("2")]})

        result = _run_lead_fit(pcb)

        assert result.status == "OK"
        assert "not applicable" in result.message
        assert result.data["pads"] == []

    def test_run_all_serializes_findings(self, tmp_path):
        """The normal preflight entry point exposes per-pad evidence."""
        pcb = _create_pcb_with_lead_pads(tmp_path, {"Q1": [_tht_pad("1", 1.50)]})
        _write_lead_spec(tmp_path, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)

        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        lead_result = _find_result(results, "assembly_lead_fit")
        assert lead_result is not None

        serialized = lead_result.to_dict()
        assert serialized["status"] == "FAIL"
        assert "pads" in serialized["data"]
        # The manifest serializes this dict as JSON.
        json.dumps(serialized)
        (finding,) = serialized["data"]["pads"]
        assert finding["mpn"] == "STW70N60DM2"
        assert finding["reference"] == "Q1"
        assert finding["pad"] == "1"
        assert finding["nominal_margin_mm"] == pytest.approx(-0.112)

    def test_strict_export_blocked_by_lead_fit_failure(self, tmp_path, monkeypatch):
        """A confirmed interference fails the export through strict preflight."""
        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_pcb_with_lead_pads(project_dir, {"Q1": [_tht_pad("1", 1.50)]})
        _write_lead_spec(project_dir, _RECT_LEAD_YAML, _LEAD_FIT_TOLERANCE_YAML)
        (project_dir / "board.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "eeschema"))'
        )
        (project_dir / "board.kicad_pro").write_text("{}")

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        config = ManufacturingConfig(
            include_report=False,
            strict_preflight=True,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert not result.success
        assert any("assembly_lead_fit" in e for e in result.errors)
        lead = [pr for pr in result.preflight_results if pr.name == "assembly_lead_fit"]
        assert lead and lead[0].status == "FAIL"


# ---------------------------------------------------------------------------
# PreflightChecker -- assembly mounting-hole fit (issue #10476)
# ---------------------------------------------------------------------------

_MOUNTING_SOURCE = "Nichicon LGU drawing, p. 1"
_MOUNTING_ORDER_SOURCE = "JLCPCB capabilities, through-hole tolerances"
_MOUNTING_GUARANTEE_SOURCE = "JLCPCB order 48210 engineering confirmation"

# A conventional relay: lead-only, so it must never grow mounting findings.
_RELAY_ENTRY_YAML = (
    "  - ref: K1\n"
    "    part: RELAY-SPDT\n"
    "    lead: {shape: round, diameter_mm: 0.90, source: relay datasheet}\n"
)


def _snap_in_pads(drill: float = 2.00, second_y: float = 5.00) -> list[str]:
    """Pads 1 and 2 of a snap-in capacitor: 10.00 mm apart on a vertical line."""
    return [_tht_pad_at("1", drill, 0.0, -5.0), _tht_pad_at("2", drill, 0.0, second_y)]


def _create_mounting_pcb(
    directory: Path, c1: list[str] | None = None, c2: list[str] | None = None
) -> Path:
    """Snap-in capacitors C1 and C2 beside the four-pad lead-only relay K1."""
    return _create_pcb_with_lead_pads(
        directory,
        {
            "K1": [_tht_pad_at(str(n), 1.20, (n - 3) * 2.54) for n in (1, 2, 3, 4)],
            "C1": _snap_in_pads() if c1 is None else c1,
            "C2": _snap_in_pads() if c2 is None else c2,
        },
        lib_ids={
            "K1": "Relay_THT:Relay_SPDT_Omron-G5LE-1",
            "C1": "Capacitor_THT:CP_Radial_D10.0mm_P5.00mm",
            "C2": "Capacitor_THT:CP_Radial_D10.0mm_P5.00mm",
        },
    )


def _snap_in_order_yaml(upper_mm: str | None = "0.13", lower_mm: str = "-0.08") -> str:
    """JLCPCB's ordinary -0.08/+0.13 mm through-hole tolerance for the spec."""
    text = f"    target_fab: jlcpcb\n    finished_hole_tolerance_mm: {lower_mm}\n"
    if upper_mm is not None:
        text += f"    finished_hole_tolerance_upper_mm: {upper_mm}\n"
    return text + f"    finished_hole_tolerance_source: {_MOUNTING_ORDER_SOURCE}\n"


def _snap_in_entry_yaml(
    pitch: bool = True,
    ref: str = "C1-C2",
    min_diameter: str = "1.90",
    max_diameter: str = "2.10",
) -> str:
    """The LGU standard snap-in pattern: 2.0 +-0.1 mm holes on 10.0 +-0.1 mm centres."""
    text = (
        f"  - ref: {ref}\n"
        "    part: LGU2E471MELB\n"
        "    mounting_holes:\n"
        "      pads: ['1', '2']\n"
        f"      hole_diameter: {{min_mm: {min_diameter}, max_mm: {max_diameter}}}\n"
    )
    if pitch:
        text += "      pitch: {min_mm: 9.90, max_mm: 10.10}\n"
    return text + f"      source: {_MOUNTING_SOURCE}\n"


def _guarantee_item(
    refs: str = "C1",
    low: str = "1.90",
    high: str = "2.10",
    source: str = _MOUNTING_GUARANTEE_SOURCE,
) -> str:
    """One documented finished-hole interval the fab guaranteed for *refs*."""
    return (
        f"      - refs: [{refs}]\n"
        f"        min_mm: {low}\n"
        f"        max_mm: {high}\n"
        f"        source: {source}\n"
    )


def _guarantee_yaml(*items: str) -> str:
    """The spec's ``finished_hole_guarantees`` block holding *items*."""
    return "    finished_hole_guarantees:\n" + "".join(items)


def _write_snap_in_spec(
    directory: Path, entries: str | None = None, order: str | None = None
) -> Path:
    return _write_lead_spec(
        directory,
        _RELAY_ENTRY_YAML + (_snap_in_entry_yaml() if entries is None else entries),
        _snap_in_order_yaml() if order is None else order,
    )


def _run_mounting_fit(pcb_path: Path, manufacturer: str = "jlcpcb"):
    checker = PreflightChecker(
        pcb_path=pcb_path,
        manufacturer=manufacturer,
        config=PreflightConfig(skip_drc=True, skip_erc=True),
    )
    results = checker.run_all()
    result = _find_result(results, "assembly_mounting_hole_fit")
    assert result is not None, "assembly_mounting_hole_fit result missing from run_all()"
    return result


def _holes(result) -> dict[tuple[str, str], dict]:
    return {(f["reference"], f["pad"]): f for f in result.data["diameters"]}


def _fake_assembly_export(self, output_dir=None):
    from kicad_tools.export import assembly

    od = Path(output_dir) if output_dir else self.config.output_dir
    od.mkdir(parents=True, exist_ok=True)
    return assembly.AssemblyPackageResult(output_dir=od)


def _export_project(tmp_path, order: str | None = None) -> Path:
    """A snap-in project directory whose board and spec sit together."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    pcb = _create_mounting_pcb(project_dir)
    _write_snap_in_spec(project_dir, order=order)
    symbols = "\n".join(
        f'(symbol (lib_id "{lib_id}") (unit 1) (in_bom yes) (on_board yes)'
        f' (property "Reference" "{ref}") (property "Value" "{value}"))'
        for ref, lib_id, value in (
            ("C1", "Device:C_Polarized", "LGU2E471MELB"),
            ("C2", "Device:C_Polarized", "LGU2E471MELB"),
            ("K1", "Relay:Relay_SPDT", "RELAY-SPDT"),
        )
    )
    (project_dir / "board.kicad_sch").write_text(
        f'(kicad_sch (version 20231120) (generator "eeschema") {symbols})'
    )
    (project_dir / "board.kicad_pro").write_text("{}")
    return pcb


class TestPreflightMountingHoleFit:
    """Snap-in mounting holes vs a sourced drawing and the fab's finished-hole interval."""

    def test_snap_in_holes_report_upper_order_overrun(self, tmp_path):
        """The reported case: four nominally correct 2.00 mm holes overrun 2.10 mm by 0.03 mm."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(tmp_path)

        result = _run_mounting_fit(pcb)

        assert result.status == "FAIL"
        holes = _holes(result)
        assert set(holes) == {("C1", "1"), ("C1", "2"), ("C2", "1"), ("C2", "2")}
        for finding in holes.values():
            assert finding["mpn"] == "LGU2E471MELB"
            assert finding["hole"] == {"shape": "round", "diameter_mm": pytest.approx(2.00)}
            assert finding["drawing"] == {
                "source": _MOUNTING_SOURCE,
                "min_mm": pytest.approx(1.90),
                "max_mm": pytest.approx(2.10),
            }
            assert finding["nominal"] == {"status": "pass"}
            order = finding["order"]
            assert order["kind"] == "general"
            assert order["source"] == _MOUNTING_ORDER_SOURCE
            assert order["min_mm"] == pytest.approx(1.92)
            assert order["max_mm"] == pytest.approx(2.13)
            assert order["min"] == {"status": "pass"}
            assert order["max"] == {
                "status": "fail",
                "side": "upper",
                "overrun_mm": pytest.approx(0.03),
            }
            assert order["status"] == "fail"
            assert finding["status"] == "fail"
        pitch = {f["reference"]: f for f in result.data["pitch"]}
        assert set(pitch) == {"C1", "C2"}
        for finding in pitch.values():
            assert finding["pads"] == ["1", "2"]
            assert finding["mpn"] == "LGU2E471MELB"
            assert finding["measured_mm"] == pytest.approx(10.00)
            assert finding["drawing"] == {
                "source": _MOUNTING_SOURCE,
                "min_mm": pytest.approx(9.90),
                "max_mm": pytest.approx(10.10),
            }
            assert finding["nominal"] == {"status": "pass"}
            assert finding["status"] == "pass"
        assert result.message == (
            "Mounting-hole fit fails on 4 measurement(s): C1 pad 1, C1 pad 2, C2 pad 1, C2 pad 2"
        )
        assert "C1 pad 1 [LGU2E471MELB]" in result.details
        assert "exceeds drawing max by 0.030 mm" in result.details
        assert "C1 pads 1-2 [LGU2E471MELB]: pitch 10.000 mm" in result.details

    def test_lead_only_relay_keeps_its_lead_fit_result(self, tmp_path):
        """The relay has no mounting findings; the capacitors have no lead findings."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(tmp_path)

        checker = PreflightChecker(
            pcb_path=pcb, config=PreflightConfig(skip_drc=True, skip_erc=True)
        )
        results = checker.run_all()
        by_ref: dict[str, list[dict]] = {}
        for finding in _find_result(results, "assembly_lead_fit").data["pads"]:
            by_ref.setdefault(finding["reference"], []).append(finding)
        assert [f["status"] for f in by_ref["K1"]] == ["pass"] * 4
        assert {f["mpn"] for f in by_ref["K1"]} == {"RELAY-SPDT"}
        for finding in by_ref["C1"] + by_ref["C2"]:
            assert finding["status"] == "unassessed"
            assert finding["lead"] is None
            assert "no verified lead dimensions declared" in finding["reason"]

        mounting = _find_result(results, "assembly_mounting_hole_fit")
        assert {f["reference"] for f in mounting.data["diameters"]} == {"C1", "C2"}
        assert {f["reference"] for f in mounting.data["pitch"]} == {"C1", "C2"}

    def test_guarantee_takes_precedence_for_the_selected_holes(self, tmp_path):
        """C1's documented 1.90-2.10 mm guarantee passes; C2 keeps the ordinary overrun."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(
            tmp_path, order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1"))
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "FAIL"
        holes = _holes(result)
        for pad in ("1", "2"):
            guaranteed = holes["C1", pad]
            assert guaranteed["nominal"] == {"status": "pass"}
            assert guaranteed["order"] == {
                "kind": "guarantee",
                "source": _MOUNTING_GUARANTEE_SOURCE,
                "min_mm": pytest.approx(1.90),
                "max_mm": pytest.approx(2.10),
                "min": {"status": "pass"},
                "max": {"status": "pass"},
                "status": "pass",
            }
            assert guaranteed["status"] == "pass"
            ordinary = holes["C2", pad]
            assert ordinary["order"]["kind"] == "general"
            assert ordinary["order"]["source"] == _MOUNTING_ORDER_SOURCE
            assert ordinary["order"]["max"] == {
                "status": "fail",
                "side": "upper",
                "overrun_mm": pytest.approx(0.03),
            }
            assert ordinary["status"] == "fail"
        assert result.message == "Mounting-hole fit fails on 2 measurement(s): C2 pad 1, C2 pad 2"

    @pytest.mark.parametrize(
        ("drill", "max_diameter", "max_result"),
        [
            ("2.00", "2.13", {"status": "pass"}),
            ("2.001", "2.13", {"status": "fail", "side": "upper", "overrun_mm": 0.001}),
            ("2.0001", "2.13", {"status": "fail", "side": "upper", "overrun_mm": 0.0}),
        ],
    )
    def test_order_limit_is_inclusive_and_judged_before_display_rounding(
        self, tmp_path, drill, max_diameter, max_result
    ):
        """2.13 mm at a 2.13 mm limit passes; 2.1301 mm fails although it displays as 0.000."""
        pcb = _create_mounting_pcb(
            tmp_path,
            c1=[_tht_pad_at("1", float(drill), 0.0, -5.0), _tht_pad_at("2", 2.00, 0.0, 5.0)],
            c2=[_tht_pad_at("1", 2.00, 0.0, -5.0), _tht_pad_at("2", 2.00, 0.0, 5.0)],
        )
        _write_snap_in_spec(
            tmp_path,
            entries=_snap_in_entry_yaml(max_diameter=max_diameter),
            order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C2")),
        )

        result = _run_mounting_fit(pcb)

        assert _holes(result)["C1", "1"]["order"]["max"] == max_result
        assert _holes(result)["C1", "1"]["status"] == max_result["status"]
        if max_result["status"] == "fail":
            assert result.status == "FAIL"
            assert result.message == "Mounting-hole fit fails on 1 measurement(s): C1 pad 1"
        else:
            assert result.status == "OK"

    def test_pitch_verdict_precedes_display_rounding(self, tmp_path):
        """A 10.1004 mm pitch is over a 10.10 mm limit although the overrun displays as 0.000."""
        pcb = _create_mounting_pcb(tmp_path, c1=_snap_in_pads(second_y=5.1004))
        _write_snap_in_spec(
            tmp_path, order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1, C2"))
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "FAIL"
        (finding,) = [f for f in result.data["pitch"] if f["reference"] == "C1"]
        assert finding["measured_mm"] == pytest.approx(10.1004)
        assert finding["nominal"]["status"] == "fail"
        assert finding["nominal"]["side"] == "upper"
        assert finding["nominal"]["overrun_mm"] == 0.0

    @pytest.mark.parametrize(
        ("second_x", "expected"),
        [(0.0, "pass"), (0.002, "fail"), (0.004, "fail")],
    )
    def test_pitch_overrun_below_report_resolution_fails(self, tmp_path, second_x, expected):
        """A 10.1000002 mm pitch is over the inclusive 10.10 mm limit despite 6-decimal rounding."""
        pads = [
            _tht_pad_at("1", 2.00, 0.0, 0.0),
            _tht_pad_at("2", 2.00, second_x, 10.10),
        ]
        pcb = _create_mounting_pcb(tmp_path, c1=pads)
        _write_snap_in_spec(
            tmp_path, order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1, C2"))
        )

        result = _run_mounting_fit(pcb)

        (finding,) = [f for f in result.data["pitch"] if f["reference"] == "C1"]
        assert finding["status"] == expected

    def test_guarantee_without_target_fab_is_not_applied(self, tmp_path):
        """An order-specific guarantee needs an identifiable matching fab to override the tolerance."""
        pcb = _create_mounting_pcb(tmp_path)
        order = (
            "    finished_hole_tolerance_mm: -0.08\n"
            "    finished_hole_tolerance_upper_mm: 0.13\n"
            f"    finished_hole_tolerance_source: {_MOUNTING_ORDER_SOURCE}\n"
        ) + _guarantee_yaml(_guarantee_item(refs="C1"))
        _write_snap_in_spec(tmp_path, order=order)

        result = _run_mounting_fit(pcb, manufacturer="pcbway")

        holes = _holes(result)
        for pad in ("1", "2"):
            assert holes["C1", pad]["order"].get("kind") != "guarantee"
            assert holes["C1", pad]["status"] == "fail"

    def test_rotated_footprint_pitch_honours_inclusive_limit(self, tmp_path):
        """A -179 degree placement computes 5.079999999999999 mm for an exact 5.08 mm pitch."""
        pads = [_tht_pad_at("1", 2.00, 0.635, 0.0), _tht_pad_at("2", 2.00, 5.715, 0.0)]
        pcb = _create_mounting_pcb(tmp_path, c1=pads, c2=pads)
        pcb.write_text(pcb.read_text().replace("(at 20 25)", "(at 20 25 -179)"))
        _write_snap_in_spec(
            tmp_path,
            entries=_snap_in_entry_yaml().replace(
                "{min_mm: 9.90, max_mm: 10.10}", "{min_mm: 5.08, max_mm: 5.18}"
            ),
            order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1, C2")),
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "OK"
        pitch = {f["reference"]: f for f in result.data["pitch"]}
        assert pitch["C1"]["measured_mm"] == pytest.approx(5.08)
        assert pitch["C1"]["nominal"] == {"status": "pass"}
        assert pitch["C2"]["measured_mm"] == pytest.approx(5.08)

    def test_missing_upper_tolerance_leaves_upper_side_unassessed(self, tmp_path):
        """Without an upper tolerance the 1.92 mm minimum is assessed; the maximum is not."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(tmp_path, order=_snap_in_order_yaml(upper_mm=None))

        result = _run_mounting_fit(pcb)

        assert result.status == "WARN"
        for finding in result.data["diameters"]:
            assert finding["nominal"] == {"status": "pass"}
            order = finding["order"]
            assert order["min_mm"] == pytest.approx(1.92)
            assert order["min"] == {"status": "pass"}
            assert "max_mm" not in order
            assert order["max"]["status"] == "unassessed"
            assert "upper finished-hole tolerance" in order["max"]["reason"]
            assert order["status"] == "unassessed"
            assert finding["status"] == "unassessed"
        assert result.data["summary"]["failed"] == 0
        assert result.data["summary"]["order_unassessed"] == 4
        assert result.message.startswith("Mounting-hole fit unverified")
        assert "max unassessed" in result.details

    def test_missing_upper_tolerance_preserves_lower_overrun(self, tmp_path):
        """A drawing minimum of 2.00 mm is undercut by the 1.92 mm order minimum."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(
            tmp_path,
            entries=_snap_in_entry_yaml(min_diameter="2.00"),
            order=_snap_in_order_yaml(upper_mm=None),
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "FAIL"
        for finding in result.data["diameters"]:
            assert finding["nominal"] == {"status": "pass"}
            assert finding["order"]["min"] == {
                "status": "fail",
                "side": "lower",
                "overrun_mm": pytest.approx(0.08),
            }
            assert finding["order"]["max"]["status"] == "unassessed"
            assert finding["status"] == "fail"

    def test_pitch_mismatch_identifies_the_pair(self, tmp_path):
        """Moving one hole to 10.20 mm fails that pair's pitch and nothing else."""
        pcb = _create_mounting_pcb(tmp_path, c1=_snap_in_pads(second_y=5.20))
        _write_snap_in_spec(
            tmp_path, order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1, C2"))
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "FAIL"
        assert [f["status"] for f in result.data["diameters"]] == ["pass"] * 4
        pitch = {f["reference"]: f for f in result.data["pitch"]}
        assert pitch["C2"]["status"] == "pass"
        assert pitch["C1"]["measured_mm"] == pytest.approx(10.20)
        assert pitch["C1"]["nominal"] == {
            "status": "fail",
            "side": "upper",
            "overrun_mm": pytest.approx(0.10),
        }
        assert pitch["C1"]["status"] == "fail"
        assert result.message == "Mounting-hole fit fails on 1 measurement(s): C1 pads 1-2"
        assert "C1 pads 1-2 [LGU2E471MELB]: pitch 10.200 mm" in result.details

    @pytest.mark.parametrize(
        ("drill", "nominal_result"),
        [
            ("2.12", {"status": "fail", "side": "upper", "overrun_mm": 0.02}),
            ("1.85", {"status": "fail", "side": "lower", "overrun_mm": 0.05}),
        ],
    )
    def test_nominal_diameter_mismatch_identifies_the_pad(self, tmp_path, drill, nominal_result):
        """A 2.12 mm nominal hole is a nominal mismatch, distinct from an order overrun."""
        pcb = _create_mounting_pcb(
            tmp_path,
            c1=[_tht_pad_at("1", float(drill), 0.0, -5.0), _tht_pad_at("2", 2.00, 0.0, 5.0)],
        )
        _write_snap_in_spec(
            tmp_path, order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1, C2"))
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "FAIL"
        holes = _holes(result)
        assert holes["C1", "1"]["nominal"] == nominal_result
        assert holes["C1", "1"]["status"] == "fail"
        assert holes["C1", "1"]["order"]["status"] == "pass"
        assert holes["C1", "2"]["status"] == "pass"
        assert [f["status"] for f in result.data["pitch"]] == ["pass", "pass"]
        assert result.message == "Mounting-hole fit fails on 1 measurement(s): C1 pad 1"

    def test_missing_pad_unassessed_while_other_holes_report(self, tmp_path):
        pcb = _create_mounting_pcb(tmp_path, c1=[_tht_pad_at("1", 2.00, 0.0, -5.0)])
        _write_snap_in_spec(tmp_path)

        result = _run_mounting_fit(pcb)

        holes = _holes(result)
        assert holes["C1", "2"] == {
            "reference": "C1",
            "pad": "2",
            "mpn": "LGU2E471MELB",
            "hole": None,
            "drawing": {
                "source": _MOUNTING_SOURCE,
                "min_mm": pytest.approx(1.90),
                "max_mm": pytest.approx(2.10),
            },
            "status": "unassessed",
            "reason": "declared pad 2 not found on C1",
        }
        assert holes["C1", "1"]["nominal"] == {"status": "pass"}
        assert holes["C1", "1"]["order"]["max"]["status"] == "fail"
        assert holes["C2", "1"]["status"] == "fail"
        pitch = {f["reference"]: f for f in result.data["pitch"]}
        assert pitch["C1"]["status"] == "unassessed"
        assert pitch["C1"]["reason"] == "declared pad 2 not found on C1"
        assert pitch["C2"]["status"] == "pass"
        assert "C1 pad 2 [LGU2E471MELB]: unassessed: declared pad 2 not found on C1" in (
            result.details
        )

    def test_slot_unassessed_with_both_axes(self, tmp_path):
        """A slot's long axis must not be mistaken for a round hole diameter or a pitch."""
        pcb = _create_mounting_pcb(
            tmp_path, c2=[_tht_pad_at("1", 2.00, 0.0, -5.0), _slot_pad("2", 1.00, 2.20)]
        )
        _write_snap_in_spec(tmp_path)

        result = _run_mounting_fit(pcb)

        holes = _holes(result)
        slot = holes["C2", "2"]
        assert slot["status"] == "unassessed"
        assert slot["hole"] == {
            "shape": "slot",
            "size_mm": [pytest.approx(1.0), pytest.approx(2.2)],
        }
        assert slot["reason"] == "slot hole 1.000 x 2.200 mm; only round holes are assessed"
        assert holes["C2", "1"]["order"]["max"]["status"] == "fail"
        pitch = {f["reference"]: f for f in result.data["pitch"]}
        assert pitch["C2"]["status"] == "unassessed"
        assert "slot" in pitch["C2"]["reason"]
        assert pitch["C1"]["status"] == "pass"

    @pytest.mark.parametrize(
        ("order", "manufacturer", "reason"),
        [
            ("", "jlcpcb", "no finished-hole tolerance declared"),
            (
                "    target_fab: jlcpcb\n    finished_hole_tolerance_mm: -0.08\n"
                f"    finished_hole_tolerance_source: {_MOUNTING_ORDER_SOURCE}\n",
                "pcbway",
                "no applicable tolerance for pcbway",
            ),
            (
                "    target_fab: jlcpcb\n"
                f"    finished_hole_tolerance_source: {_MOUNTING_ORDER_SOURCE}\n"
                + _guarantee_yaml(_guarantee_item(refs="C1, C2")),
                "pcbway",
                "no applicable tolerance for pcbway",
            ),
        ],
    )
    def test_missing_or_inapplicable_order_evidence_cannot_pass(
        self, tmp_path, order, manufacturer, reason
    ):
        """Neither an undeclared nor another fab's tolerance or guarantee yields a pass."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(tmp_path, order=order)

        result = _run_mounting_fit(pcb, manufacturer=manufacturer)

        assert result.status == "WARN"
        assert result.data["summary"]["failed"] == 0
        for finding in result.data["diameters"]:
            assert finding["nominal"] == {"status": "pass"}
            assert finding["order"]["status"] == "unassessed"
            assert reason in json.dumps(finding["order"])
            assert finding["status"] == "unassessed"
        assert [f["status"] for f in result.data["pitch"]] == ["pass", "pass"]
        expected = (
            "order unassessed: no applicable tolerance for pcbway"
            if manufacturer == "pcbway"
            else "min unassessed: no finished-hole tolerance declared"
        )
        assert expected in result.details

    @pytest.mark.parametrize(
        ("c2", "hole_reason", "pitch_reason"),
        [
            (
                [_tht_pad_at("1", 2.00, 0.0, -5.0), _np_pad("2", 2.00)],
                "declared pad 2 on C2 is not a plated through-hole pad",
                "declared pad 2 on C2 is not a plated through-hole pad",
            ),
            (
                [_tht_pad_at("1", 2.00, 0.0, -5.0), _smd_pad("2")],
                "declared pad 2 on C2 is not a plated through-hole pad",
                "declared pad 2 on C2 is not a plated through-hole pad",
            ),
            (
                [
                    _tht_pad_at("1", 2.00, 0.0, -5.0),
                    _tht_pad_at("2", 2.00, 0.0, 5.0),
                    _tht_pad_at("2", 2.00, 0.0, 5.0),
                ],
                None,
                "declared pad 2 matches 2 pads; the pair is ambiguous",
            ),
            (
                [
                    _tht_pad_at("1", 2.00, 0.0, -5.0),
                    '(pad "2" thru_hole circle (at 0 5) (size 2.7 2.7) (drill 2.00 (offset 0.5 0)) '
                    '(layers "*.Cu" "*.Mask") (net 0 ""))',
                ],
                None,
                "declared pad 2 has an offset drill; its hole centre is not measured",
            ),
            (
                [_tht_pad_at("1", 2.00, 0.0, -5.0), _tht_pad_at("2", 0.0, 0.0, 5.0)],
                "through-hole pad has no drill diameter",
                "declared pad 2 has no drill diameter",
            ),
        ],
    )
    def test_unmeasurable_pair_is_unassessed_with_its_reason(
        self, tmp_path, c2, hole_reason, pitch_reason
    ):
        """Each unusable member is named; C1 and C2 pad 1 still report their results."""
        pcb = _create_mounting_pcb(tmp_path, c2=c2)
        _write_snap_in_spec(
            tmp_path, order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1, C2"))
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "WARN"
        holes = _holes(result)
        assert holes["C1", "1"]["status"] == "pass"
        assert holes["C2", "1"]["status"] == "pass"
        if hole_reason is None:
            assert all(f["status"] == "pass" for (ref, pad), f in holes.items() if pad == "2")
        else:
            unassessed = holes["C2", "2"]
            assert unassessed["status"] == "unassessed"
            assert unassessed["reason"] == hole_reason
        pitch = {f["reference"]: f for f in result.data["pitch"]}
        assert pitch["C1"]["status"] == "pass"
        assert pitch["C2"]["status"] == "unassessed"
        assert pitch["C2"]["reason"] == pitch_reason
        assert pitch["C2"]["pads"] == ["1", "2"]

    def test_first_guarantee_declared_for_a_reference_applies(self, tmp_path):
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(
            tmp_path,
            order=_snap_in_order_yaml()
            + _guarantee_yaml(
                _guarantee_item(refs="C1"),
                _guarantee_item(refs="C1", low="2.00", high="2.00", source="later note"),
                _guarantee_item(refs="C2"),
            ),
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "OK"
        order = _holes(result)["C1", "1"]["order"]
        assert order["source"] == _MOUNTING_GUARANTEE_SOURCE
        assert (order["min_mm"], order["max_mm"]) == (pytest.approx(1.90), pytest.approx(2.10))

    def test_guarantee_range_covers_each_reference(self, tmp_path):
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(
            tmp_path, order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1-C2"))
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "OK"
        for finding in result.data["diameters"]:
            assert finding["order"]["kind"] == "guarantee"

    def test_lead_and_mounting_declarations_are_independent(self, tmp_path):
        """One assignment may declare both; each check reports its own verdict."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(
            tmp_path,
            entries=_snap_in_entry_yaml().replace(
                "    mounting_holes:",
                "    lead: {shape: rectangular, width_mm: 1.00, thickness_mm: 0.80, "
                "source: snap terminal drawing}\n    mounting_holes:",
            ),
            order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1, C2")),
        )

        checker = PreflightChecker(
            pcb_path=pcb, config=PreflightConfig(skip_drc=True, skip_erc=True)
        )
        results = checker.run_all()

        lead = _find_result(results, "assembly_lead_fit")
        capacitors = [f for f in lead.data["pads"] if f["reference"] != "K1"]
        assert [f["status"] for f in capacitors] == ["pass"] * 4
        assert {f["lead"]["shape"] for f in capacitors} == {"rectangular"}
        mounting = _find_result(results, "assembly_mounting_hole_fit")
        assert mounting.status == "OK"
        assert [f["status"] for f in mounting.data["diameters"]] == ["pass"] * 4

    def test_invalid_lower_tolerance_is_listed_and_leaves_order_unassessed(self, tmp_path):
        """A dropped tolerance declaration is named once and yields no order verdict."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(
            tmp_path,
            order=(
                "    target_fab: jlcpcb\n"
                "    finished_hole_tolerance_mm: 0.08\n"
                f"    finished_hole_tolerance_source: {_MOUNTING_ORDER_SOURCE}\n"
            ),
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "WARN"
        (listed,) = result.data["invalid_declarations"]
        assert listed["field"] == "finished_hole_tolerance_mm"
        assert "finished_hole_tolerance_mm" in listed["reason"]
        for finding in result.data["diameters"]:
            assert finding["order"]["status"] == "unassessed"
            assert finding["order"]["min"]["reason"] == listed["reason"]
            assert finding["order"]["max"]["reason"] == listed["reason"]
        assert "invalid declaration finished_hole_tolerance_mm" in result.details

    def test_no_pitch_limits_declares_no_pitch_finding(self, tmp_path):
        """A drawing without a pitch tolerance verifies only the hole diameters."""
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(
            tmp_path,
            entries=_snap_in_entry_yaml(pitch=False),
            order=_snap_in_order_yaml() + _guarantee_yaml(_guarantee_item(refs="C1, C2")),
        )

        result = _run_mounting_fit(pcb)

        assert result.status == "OK"
        assert result.data["pitch"] == []
        assert [f["status"] for f in result.data["diameters"]] == ["pass"] * 4
        assert "verified" in result.message

    @pytest.mark.parametrize("declarations", [None, "lead", "assignment"])
    def test_no_mounting_declaration_is_not_applicable(self, tmp_path, declarations):
        pcb = _create_mounting_pcb(tmp_path)
        if declarations == "lead":
            _write_lead_spec(tmp_path, _RELAY_ENTRY_YAML, _snap_in_order_yaml())
        elif declarations == "assignment":
            _write_lead_spec(
                tmp_path,
                _RELAY_ENTRY_YAML + "  - ref: C1-C2\n    part: LGU2E471MELB\n",
                _snap_in_order_yaml(),
            )

        result = _run_mounting_fit(pcb)

        assert result.status == "OK"
        assert "not applicable" in result.message
        assert result.data["diameters"] == []
        assert result.data["pitch"] == []
        assert result.data["invalid_declarations"] == []

    def test_invalid_mounting_declaration_preserves_assignment(self, tmp_path):
        """A malformed mounting drawing leaves the BOM assignment and other checks intact."""
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        pcb = _create_mounting_pcb(tmp_path)
        _write_lead_spec(
            tmp_path,
            _RELAY_ENTRY_YAML + "  - ref: C1-C2\n    part: LGU2E471MELB\n    lcsc: C51423\n"
            "    mounting_holes:\n      pads: ['1', '2']\n"
            "      hole_diameter: {min_mm: 2.10, max_mm: 1.90}\n"
            f"      source: {_MOUNTING_SOURCE}\n",
            _snap_in_order_yaml(),
        )

        checker = PreflightChecker(
            pcb_path=pcb, config=PreflightConfig(skip_drc=True, skip_erc=True)
        )
        results = checker.run_all()
        result = _find_result(results, "assembly_mounting_hole_fit")

        assert result.status == "WARN"
        assert result.data["diameters"] == []
        assert result.data["pitch"] == []
        (invalid,) = result.data["invalid_declarations"]
        assert invalid["field"] == "bom_entries[1].mounting_holes"
        assert invalid["refs"] == ["C1", "C2"]
        assert "min_mm <= max_mm" in invalid["reason"]
        assert "bom_entries[1].mounting_holes" in result.details
        relay = [
            f["status"]
            for f in _find_result(results, "assembly_lead_fit").data["pads"]
            if f["reference"] == "K1"
        ]
        assert relay == ["pass"] * 4

        package = AssemblyPackage(
            pcb_path=pcb,
            schematic_path=None,
            config=AssemblyConfig(bom_source="pcb", auto_lcsc=False),
        )
        exported = package.export(tmp_path / "assembly")
        with exported.bom_path.open(newline="", encoding="utf-8-sig") as bom:
            grouped = {row["Designator"]: row["LCSC Part #"] for row in csv.DictReader(bom)}
        assert grouped["C1,C2"] == "C51423"

    def test_invalid_guarantee_is_dropped_alone(self, tmp_path):
        """One malformed guarantee neither drops the valid one nor the BOM assignments."""
        from kicad_tools.export.bom_spec_overlay import load_assembly_spec

        pcb = _create_mounting_pcb(tmp_path)
        spec_file = _write_snap_in_spec(
            tmp_path,
            entries=_snap_in_entry_yaml(ref="C1-C2")
            + _snap_in_entry_yaml(ref="C3", pitch=False).replace(
                "part: LGU2E471MELB", "part: OTHER"
            ),
            order=_snap_in_order_yaml()
            + _guarantee_yaml(_guarantee_item(refs="C2", high="1.00"), _guarantee_item()),
        )

        spec, errors = load_assembly_spec(spec_file)
        assert errors.guarantees == {
            "finished_hole_guarantees[0]": "invalid finished_hole_guarantees[0]: Value error, "
            "limits require min_mm <= max_mm"
        }
        assert spec is not None and spec.bom_entries is not None
        assert spec.bom_entries[0].lead is not None
        assert spec.bom_entries[1].mounting_holes is not None
        assert spec.bom_entries[2].mounting_holes is not None
        assert spec.requirements is not None
        assert spec.requirements.manufacturing is not None
        assert len(spec.requirements.manufacturing.finished_hole_guarantees or []) == 1

        result = _run_mounting_fit(pcb)

        (invalid,) = result.data["invalid_declarations"]
        assert invalid == {
            "field": "finished_hole_guarantees[0]",
            "refs": [],
            "reason": "invalid finished_hole_guarantees[0]: Value error, limits require "
            "min_mm <= max_mm",
        }
        assert "finished_hole_guarantees[0]" in result.details
        holes = _holes(result)
        assert holes["C1", "1"]["order"]["kind"] == "guarantee"
        assert holes["C1", "1"]["status"] == "pass"
        assert holes["C2", "1"]["order"]["kind"] == "general"
        assert holes["C2", "1"]["status"] == "fail"

    def test_invalid_upper_tolerance_keeps_lower_side(self, tmp_path):
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(tmp_path, order=_snap_in_order_yaml(upper_mm="-0.05"))

        result = _run_mounting_fit(pcb)

        assert result.status == "WARN"
        for finding in result.data["diameters"]:
            assert finding["order"]["min"] == {"status": "pass"}
            assert finding["order"]["max"]["status"] == "unassessed"
            assert "finished_hole_tolerance_upper_mm" in finding["order"]["max"]["reason"]
        (invalid,) = result.data["invalid_declarations"]
        assert invalid["field"] == "finished_hole_tolerance_upper_mm"

    def test_dnp_footprint_excluded(self, tmp_path):
        pcb = _create_mounting_pcb(tmp_path)
        pcb.write_text(pcb.read_text().replace("(attr through_hole)", "(attr through_hole dnp)"))
        _write_snap_in_spec(tmp_path)

        result = _run_mounting_fit(pcb)

        assert result.status == "OK"
        assert result.data["diameters"] == []
        assert result.data["pitch"] == []

    def test_run_all_serializes_findings(self, tmp_path):
        pcb = _create_mounting_pcb(tmp_path)
        _write_snap_in_spec(tmp_path)

        serialized = _run_mounting_fit(pcb).to_dict()
        json.dumps(serialized)

        assert serialized["name"] == "assembly_mounting_hole_fit"
        assert serialized["status"] == "FAIL"
        assert len(serialized["data"]["diameters"]) == 4
        assert len(serialized["data"]["pitch"]) == 2
        assert serialized["data"]["summary"]["holes"] == 4
        assert serialized["data"]["summary"]["pitch_pairs"] == 2
        assert serialized["data"]["summary"]["failed"] == 4
        assert serialized["data"]["summary"]["unassessed"] == 0

    @pytest.mark.parametrize("lower_mm", ["-0.08", "0.08"], ids=["valid-lower", "invalid-lower"])
    def test_strict_export_blocked_by_overrun(self, tmp_path, monkeypatch, lower_mm):
        """A confirmed order overrun fails the export through strict preflight."""
        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage

        pcb = _export_project(tmp_path, order=_snap_in_order_yaml(lower_mm=lower_mm))
        monkeypatch.setattr(assembly.AssemblyPackage, "export", _fake_assembly_export)

        config = ManufacturingConfig(
            include_report=False,
            strict_preflight=True,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert not result.success
        assert any("assembly_mounting_hole_fit" in e for e in result.errors)
        assert [pr.name for pr in result.preflight_results if pr.status == "FAIL"] == [
            "assembly_mounting_hole_fit"
        ]
        mounting = [
            pr for pr in result.preflight_results if pr.name == "assembly_mounting_hole_fit"
        ]
        assert mounting and mounting[0].status == "FAIL"
        assert result.manifest_path is None
        holes = _holes(mounting[0])
        assert set(holes) == {("C1", "1"), ("C1", "2"), ("C2", "1"), ("C2", "2")}
        for finding in holes.values():
            order = finding["order"]
            assert order["source"] == _MOUNTING_ORDER_SOURCE
            assert order["max_mm"] == pytest.approx(2.13)
            assert order["max"]["overrun_mm"] == pytest.approx(0.03)
        if lower_mm == "0.08":
            (invalid,) = mounting[0].data["invalid_declarations"]
            assert invalid["field"] == "finished_hole_tolerance_mm"
            for finding in holes.values():
                assert finding["order"]["min"] == {
                    "status": "unassessed",
                    "reason": invalid["reason"],
                }

    def test_ordinary_export_records_findings_in_manifest(self, tmp_path, monkeypatch):
        """Without strict preflight the export proceeds and the manifest carries the findings."""
        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage

        pcb = _export_project(tmp_path)
        monkeypatch.setattr(assembly.AssemblyPackage, "export", _fake_assembly_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert result.success, result.errors
        assert any("assembly_mounting_hole_fit" in warning for warning in result.warnings)
        manifest = json.loads(result.manifest_path.read_text())
        (recorded,) = [
            entry
            for entry in manifest["preflight"]
            if entry["name"] == "assembly_mounting_hole_fit"
        ]
        assert recorded["status"] == "FAIL"
        overruns = [
            f for f in recorded["data"]["diameters"] if f["order"]["max"]["status"] == "fail"
        ]
        assert {(f["reference"], f["pad"]) for f in overruns} == {
            ("C1", "1"),
            ("C1", "2"),
            ("C2", "1"),
            ("C2", "2"),
        }
        assert all(f["order"]["max"]["overrun_mm"] == pytest.approx(0.03) for f in overruns)
        assert {f["reference"] for f in recorded["data"]["pitch"]} == {"C1", "C2"}


# ---------------------------------------------------------------------------
# PreflightChecker -- SMD land-pattern check (Issue #10505)
# ---------------------------------------------------------------------------

_STACKPOLE_RHC2512_YAML = (
    "  - ref: R40-R41\n"
    "    part: RHC2512FT4K70\n"
    "    land_pattern:\n"
    "      pad_shape: rect\n"
    "      pad_depth_mm: 3.00\n"
    "      pad_width_mm: 3.50\n"
    "      outer_span_mm: 8.00\n"
    '      source: "Stackpole RHC series, page 2"\n'
)


def _smd_chip_pad(
    number: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    shape: str = "rect",
    rotation: float = 0.0,
    side: str = "F",
) -> str:
    """Format one copper SMD pad S-expression."""
    at = f"{x} {y} {rotation}" if rotation else f"{x} {y}"
    rratio = " (roundrect_rratio 0.204082)" if shape == "roundrect" else ""
    layers = f'"{side}.Cu" "{side}.Paste" "{side}.Mask"'
    return (
        f'(pad "{number}" smd {shape} (at {at}) (size {w} {h})'
        f'{rratio} (layers {layers}) (net 0 ""))'
    )


def _stock_2512_pads(*, rotation: float = 0.0, side: str = "F") -> list[str]:
    """KiCad stock R_2512_6332Metric copper pads (1.225 x 3.35 mm, ±2.9625 mm)."""
    return [
        _smd_chip_pad(
            "1", -2.9625, 0.0, 1.225, 3.35, shape="roundrect", rotation=rotation, side=side
        ),
        _smd_chip_pad(
            "2", 2.9625, 0.0, 1.225, 3.35, shape="roundrect", rotation=rotation, side=side
        ),
    ]


def _stackpole_rhc2512_pads(
    *,
    rotation: float = 0.0,
    side: str = "F",
    pad1_shape: str = "rect",
    pad2_shape: str = "rect",
    pad_depth: float = 3.00,
    pad_width: float = 3.50,
    pitch: float = 5.00,
) -> list[str]:
    """Stackpole RHC2512 drawing copper pads (3.00 x 3.50 mm, 5.00 mm pitch, 8.00 mm outer)."""
    half = pitch / 2.0
    return [
        _smd_chip_pad(
            "1", -half, 0.0, pad_depth, pad_width, shape=pad1_shape, rotation=rotation, side=side
        ),
        _smd_chip_pad(
            "2", half, 0.0, pad_depth, pad_width, shape=pad2_shape, rotation=rotation, side=side
        ),
    ]


def _create_pcb_with_smd_footprints(
    directory: Path,
    footprints: list[dict],
    *,
    filename: str = "board.kicad_pcb",
) -> Path:
    """Write a minimal valid PCB containing the specified SMD footprints.

    Each entry in *footprints* is a dict with keys:
    ``ref``, ``pads`` (list of pad s-expr strings), and optional
    ``lib_id`` (default ``Resistor_SMD:R_2512_6332Metric``),
    ``layer`` (default ``F.Cu``), ``rotation`` (default ``0``),
    ``attr`` (default ``smd``).
    """
    fp_blocks = []
    for idx, spec in enumerate(footprints):
        ref = spec["ref"]
        lib_id = spec.get("lib_id", "Resistor_SMD:R_2512_6332Metric")
        layer = spec.get("layer", "F.Cu")
        rot = spec.get("rotation", 0.0)
        attr = spec.get("attr", "smd")
        at = f"{10 + idx * 15} 20 {rot}" if rot else f"{10 + idx * 15} 20"
        pad_lines = "\n    ".join(spec["pads"])
        fp_blocks.append(
            f'  (footprint "{lib_id}" (layer "{layer}")\n'
            f"    (at {at})\n"
            f'    (property "Reference" "{ref}" (at 0 0) (layer "{layer[0]}.SilkS"))\n'
            f'    (property "Value" "4k7" (at 0 2) (layer "{layer[0]}.Fab"))\n'
            f"    (attr {attr})\n"
            f"    {pad_lines}\n"
            f"  )"
        )
    fp_block = "\n".join(fp_blocks)
    content = f"""(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_line (start 0 0) (end 80 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 80 0) (end 80 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 80 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
{fp_block}
)
"""
    pcb_path = directory / filename
    pcb_path.write_text(content)
    return pcb_path


def _run_smd_land_pattern(pcb_path: Path):
    checker = PreflightChecker(
        pcb_path=pcb_path,
        config=PreflightConfig(skip_drc=True, skip_erc=True),
    )
    results = checker.run_all()
    result = _find_result(results, "assembly_smd_land_pattern")
    assert result is not None, "assembly_smd_land_pattern result missing from run_all()"
    return result


class TestPreflightSmdLandPattern:
    """MPN-specific two-terminal SMD land-pattern checks (Issue #10505)."""

    def test_stock_2512_fails_and_is_invariant_to_rotation_and_back_flip(self, tmp_path):
        """Stock KiCad R_2512_6332Metric fails Stackpole RHC2512FT4K70 at 0, 90 and B.Cu."""
        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [
                {
                    "ref": "R40",
                    "layer": "F.Cu",
                    "rotation": 0,
                    "pads": _stock_2512_pads(rotation=0, side="F"),
                },
                {
                    "ref": "R41",
                    "layer": "B.Cu",
                    "rotation": 90,
                    "pads": _stock_2512_pads(rotation=90, side="B"),
                },
            ],
        )
        _write_lead_spec(tmp_path, _STACKPOLE_RHC2512_YAML)
        before_bytes = pcb.read_bytes()

        result = _run_smd_land_pattern(pcb)

        assert pcb.read_bytes() == before_bytes
        assert result.status == "FAIL"
        by_ref = {c["reference"]: c for c in result.data["components"]}
        assert set(by_ref) == {"R40", "R41"}
        for ref in ("R40", "R41"):
            finding = by_ref[ref]
            assert finding["mpn"] == "RHC2512FT4K70"
            assert finding["source"] == "Stackpole RHC series, page 2"
            assert finding["status"] == "fail"
            assert finding["expected"] == {
                "pad_shape": "rect",
                "pad_depth_mm": pytest.approx(3.0),
                "pad_width_mm": pytest.approx(3.5),
                "pitch_mm": pytest.approx(5.0),
                "outer_span_mm": pytest.approx(8.0),
                "source": "Stackpole RHC series, page 2",
            }
            assert finding["measured"] == {
                "pads": [
                    {
                        "pad": "1",
                        "shape": "roundrect",
                        "depth_mm": pytest.approx(1.225),
                        "width_mm": pytest.approx(3.35),
                    },
                    {
                        "pad": "2",
                        "shape": "roundrect",
                        "depth_mm": pytest.approx(1.225),
                        "width_mm": pytest.approx(3.35),
                    },
                ],
                "pitch_mm": pytest.approx(5.925),
                "outer_span_mm": pytest.approx(7.150),
            }
            assert set(finding["mismatched_fields"]) == {
                "pad 1 shape",
                "pad 1 depth_mm",
                "pad 1 width_mm",
                "pad 2 shape",
                "pad 2 depth_mm",
                "pad 2 width_mm",
                "pitch_mm",
                "outer_span_mm",
            }
        # Same physical pattern yields identical measurements after rotation and flip
        assert by_ref["R40"]["measured"] == by_ref["R41"]["measured"]
        assert by_ref["R40"]["mismatched_fields"] == by_ref["R41"]["mismatched_fields"]
        assert "R40 [RHC2512FT4K70]" in result.details
        assert "Stackpole RHC series, page 2" in result.details

    @pytest.mark.parametrize(
        ("layer", "rotation"),
        [("F.Cu", 0), ("F.Cu", 90), ("B.Cu", 0), ("B.Cu", 90)],
    )
    def test_stackpole_drawing_passes_across_orientations_and_sides(
        self, tmp_path, layer, rotation
    ):
        """Stackpole drawing pads pass identically at 0/90 deg on F.Cu and B.Cu."""
        side = layer[0]
        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [
                {
                    "ref": "R40",
                    "layer": layer,
                    "rotation": rotation,
                    "pads": _stackpole_rhc2512_pads(rotation=rotation, side=side),
                },
                {
                    "ref": "R41",
                    "layer": "F.Cu",
                    "rotation": 0,
                    "pads": _stackpole_rhc2512_pads(rotation=0, side="F"),
                },
            ],
        )
        _write_lead_spec(tmp_path, _STACKPOLE_RHC2512_YAML)

        result = _run_smd_land_pattern(pcb)

        assert result.status == "OK"
        by_ref = {c["reference"]: c for c in result.data["components"]}
        assert by_ref["R40"]["status"] == "pass"
        assert by_ref["R41"]["status"] == "pass"
        assert by_ref["R40"]["mismatched_fields"] == []
        assert by_ref["R40"]["measured"] == by_ref["R41"]["measured"]
        assert by_ref["R40"]["measured"]["pitch_mm"] == pytest.approx(5.0)
        assert by_ref["R40"]["measured"]["outer_span_mm"] == pytest.approx(8.0)

    def test_vertical_local_pads_with_pad_angle_invariant_after_back_flip(self, tmp_path):
        """Vertically-authored pads with non-zero local angles match after B.Cu flip + rotation."""
        # Front at 0 deg: pads along Y at (0, -2.5) and (0, +2.5), each pad rotated 90 deg
        # so its size[0]=3.00 aligns with Y (terminal axis) and size[1]=3.50 with X.
        front_pads = [
            _smd_chip_pad("1", 0.0, -2.5, 3.00, 3.50, shape="rect", rotation=90.0, side="F"),
            _smd_chip_pad("2", 0.0, 2.5, 3.00, 3.50, shape="rect", rotation=90.0, side="F"),
        ]
        # Flipped to B.Cu (y negated, local angle -> -90) and footprint rotated +90 deg
        # (board-absolute pad angle = -90 + 90 = 0 deg).
        back_rot90_pads = [
            _smd_chip_pad("1", 0.0, 2.5, 3.00, 3.50, shape="rect", rotation=0.0, side="B"),
            _smd_chip_pad("2", 0.0, -2.5, 3.00, 3.50, shape="rect", rotation=0.0, side="B"),
        ]
        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [
                {"ref": "R40", "layer": "F.Cu", "rotation": 0, "pads": front_pads},
                {"ref": "R41", "layer": "B.Cu", "rotation": 90, "pads": back_rot90_pads},
            ],
        )
        _write_lead_spec(tmp_path, _STACKPOLE_RHC2512_YAML)

        result = _run_smd_land_pattern(pcb)

        assert result.status == "OK"
        by_ref = {c["reference"]: c for c in result.data["components"]}
        assert by_ref["R40"]["measured"] == by_ref["R41"]["measured"]
        assert by_ref["R40"]["status"] == "pass"
        assert by_ref["R41"]["status"] == "pass"

    def test_single_pad_shape_mismatch_and_unassessed_siblings(self, tmp_path):
        """One rounded pad flags only that pad's shape; sibling unassessed parts preserve findings."""
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [
                {
                    "ref": "R40",
                    "pads": _stackpole_rhc2512_pads(pad1_shape="rect", pad2_shape="roundrect"),
                },
                {
                    # Another 2512 MPN in the same family, no drawing declared.
                    "ref": "R42",
                    "pads": _stock_2512_pads(),
                },
                {
                    # Source-less land_pattern declaration.
                    "ref": "R43",
                    "pads": _stock_2512_pads(),
                },
                {
                    # Missing MPN (no bom_entries row at all).
                    "ref": "R44",
                    "pads": _stock_2512_pads(),
                },
            ],
        )
        _write_lead_spec(
            tmp_path,
            "  - ref: R40\n"
            "    part: RHC2512FT4K70\n"
            "    land_pattern:\n"
            "      pad_shape: rect\n"
            "      pad_depth_mm: 3.00\n"
            "      pad_width_mm: 3.50\n"
            "      outer_span_mm: 8.00\n"
            '      source: "Stackpole RHC series, page 2"\n'
            "  - ref: R42\n"
            "    part: RHC2512FT10K0\n"
            "  - ref: R43\n"
            "    part: CRCW25124K70FKEG\n"
            "    lcsc: C54321\n"
            "    land_pattern:\n"
            "      pad_shape: rect\n"
            "      pad_depth_mm: 3.00\n"
            "      pad_width_mm: 3.50\n"
            "      outer_span_mm: 8.00\n",
        )

        result = _run_smd_land_pattern(pcb)

        assert result.status == "FAIL"
        by_ref = {c["reference"]: c for c in result.data["components"]}

        # R40: only pad 2 shape mismatches; dimensions and pad 1 match
        r40 = by_ref["R40"]
        assert r40["status"] == "fail"
        assert r40["mismatched_fields"] == ["pad 2 shape"]

        # R42: same 2512 footprint and RHC2512 family name, different exact MPN -> unassessed
        r42 = by_ref["R42"]
        assert r42["status"] == "unassessed"
        assert r42["mpn"] == "RHC2512FT10K0"
        assert "RHC2512FT10K0" in r42["reason"]
        assert r42["measured"] is not None

        # R43: source-less declaration -> unassessed with source reason, MPN retained
        r43 = by_ref["R43"]
        assert r43["status"] == "unassessed"
        assert r43["mpn"] == "CRCW25124K70FKEG"
        assert "source" in r43["reason"].lower()

        # R44: no BOM assignment -> unassessed with missing MPN reason
        r44 = by_ref["R44"]
        assert r44["status"] == "unassessed"
        assert r44["mpn"] is None
        assert "bom_entries" in r44["reason"]

        # Invalid land_pattern on R43 must not break BOM export assignment
        package = AssemblyPackage(
            pcb_path=pcb,
            schematic_path=None,
            config=AssemblyConfig(bom_source="pcb", auto_lcsc=False),
        )
        exported = package.export(tmp_path / "assembly")
        with exported.bom_path.open(newline="", encoding="utf-8-sig") as bom:
            assignments = {row["Designator"]: row["LCSC Part #"] for row in csv.DictReader(bom)}
        assert assignments["R43"] == "C54321"

    def test_blank_mpn_unassessed(self, tmp_path):
        """A BOM entry with a blank MPN cannot claim verification even with a pattern."""
        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [{"ref": "R40", "pads": _stackpole_rhc2512_pads()}],
        )
        _write_lead_spec(
            tmp_path,
            "  - ref: R40\n"
            '    part: ""\n'
            "    land_pattern:\n"
            "      pad_shape: rect\n"
            "      pad_depth_mm: 3.00\n"
            "      pad_width_mm: 3.50\n"
            "      outer_span_mm: 8.00\n"
            '      source: "Stackpole RHC series, page 2"\n',
        )

        result = _run_smd_land_pattern(pcb)

        assert result.status == "WARN"
        (finding,) = result.data["components"]
        assert finding["status"] == "unassessed"
        assert finding["mpn"] is None
        assert "MPN" in finding["reason"]

    def test_unusable_geometry_reports_specific_unassessed_reason(self, tmp_path):
        """Skewed pads, unsupported shapes and coincident centers report unassessed."""
        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [
                {
                    "ref": "R40",
                    "pads": [
                        _smd_chip_pad("1", -2.5, 0.0, 3.0, 3.5, shape="custom"),
                        _smd_chip_pad("2", 2.5, 0.0, 3.0, 3.5, shape="rect"),
                    ],
                },
                {
                    "ref": "R41",
                    "pads": [
                        _smd_chip_pad("1", -2.5, 0.0, 3.0, 3.5, shape="rect", rotation=30.0),
                        _smd_chip_pad("2", 2.5, 0.0, 3.0, 3.5, shape="rect", rotation=0.0),
                    ],
                },
            ],
        )
        _write_lead_spec(tmp_path, _STACKPOLE_RHC2512_YAML)

        result = _run_smd_land_pattern(pcb)

        assert result.status == "WARN"
        by_ref = {c["reference"]: c for c in result.data["components"]}
        assert by_ref["R40"]["status"] == "unassessed"
        assert "custom" in by_ref["R40"]["reason"]
        assert by_ref["R41"]["status"] == "unassessed"
        assert "axis-aligned" in by_ref["R41"]["reason"]

    def test_dnp_tht_and_multi_terminal_footprints_excluded(self, tmp_path):
        """DNP, through-hole and >2-terminal SMD parts do not create chip findings."""
        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [
                {"ref": "R40", "attr": "smd dnp", "pads": _stock_2512_pads()},
                {
                    "ref": "Q1",
                    "lib_id": "Package_TO_SOT_SMD:SOT-23",
                    "pads": [
                        _smd_chip_pad("1", -0.95, 1.1, 0.6, 0.7),
                        _smd_chip_pad("2", 0.95, 1.1, 0.6, 0.7),
                        _smd_chip_pad("3", 0.0, -1.1, 0.6, 0.7),
                    ],
                },
                {
                    "ref": "J1",
                    "lib_id": "Connector_PinHeader:PinHeader_1x02",
                    "attr": "through_hole",
                    "pads": [_tht_pad("1", 1.0), _tht_pad("2", 1.0)],
                },
            ],
        )
        _write_lead_spec(tmp_path, _STACKPOLE_RHC2512_YAML)

        result = _run_smd_land_pattern(pcb)

        assert result.status == "OK"
        assert result.data["components"] == []

    @pytest.mark.parametrize(("delta", "expected_status"), [(0.001, "pass"), (0.002, "fail")])
    def test_numerical_comparison_precision_boundary(self, tmp_path, delta, expected_status):
        """Comparison tolerance is +/-0.001 mm: 1 um passes, 2 um fails."""
        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [
                {
                    "ref": "R40",
                    "pads": _stackpole_rhc2512_pads(pad_width=3.50 + delta),
                }
            ],
        )
        _write_lead_spec(tmp_path, _STACKPOLE_RHC2512_YAML)

        result = _run_smd_land_pattern(pcb)

        assert result.data["tolerance_mm"] == pytest.approx(0.001)
        (finding,) = result.data["components"]
        assert finding["status"] == expected_status

    def test_strict_export_blocked_and_pcb_stays_byte_identical(self, tmp_path, monkeypatch):
        """A confirmed SMD land-pattern mismatch blocks strict export without modifying the PCB."""
        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_pcb_with_smd_footprints(
            project_dir,
            [
                {"ref": "R40", "pads": _stock_2512_pads()},
                {"ref": "R41", "rotation": 90, "pads": _stock_2512_pads(rotation=90)},
            ],
        )
        _write_lead_spec(project_dir, _STACKPOLE_RHC2512_YAML)
        (project_dir / "board.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "eeschema"))'
        )
        (project_dir / "board.kicad_pro").write_text("{}")

        original_bytes = pcb.read_bytes()

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        config = ManufacturingConfig(
            include_report=False,
            strict_preflight=True,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert not result.success
        assert any("assembly_smd_land_pattern" in e for e in result.errors)
        smd = [pr for pr in result.preflight_results if pr.name == "assembly_smd_land_pattern"]
        assert smd and smd[0].status == "FAIL"
        json.dumps(smd[0].to_dict())
        assert pcb.read_bytes() == original_bytes

    @pytest.mark.parametrize("no_spec", [False, True])
    def test_export_spec_selection(self, tmp_path, no_spec):
        """SMD land-pattern check honors explicit --spec and --no-spec selection."""
        from kicad_tools.cli.export_cmd import build_manufacturing_config
        from kicad_tools.export.manufacturing import ManufacturingPackage

        pcb = _create_pcb_with_smd_footprints(
            tmp_path, [{"ref": "R40", "pads": _stock_2512_pads()}]
        )
        _write_lead_spec(
            tmp_path,
            "  - ref: R40\n    part: OTHER\n",
        )
        variant = tmp_path / "variant"
        variant.mkdir()
        selected = _write_lead_spec(variant, _STACKPOLE_RHC2512_YAML)
        config = build_manufacturing_config(
            output_dir=tmp_path,
            spec_path=selected,
            no_spec=no_spec,
            strict_preflight=True,
            skip_drc=True,
            skip_erc=True,
        )
        results = ManufacturingPackage(pcb_path=pcb, config=config)._run_preflight()
        result = _find_result(results, "assembly_smd_land_pattern")
        assert result is not None
        (comp,) = result.data["components"]
        if no_spec:
            assert result.status == "WARN"
            assert comp["status"] == "unassessed" and comp["mpn"] is None
        else:
            assert result.status == "FAIL"
            assert comp["mpn"] == "RHC2512FT4K70"

    def test_asymmetric_single_pad_depth_flags_only_offending_pad_and_outer_span(self, tmp_path):
        """Changing only pad 1 depth flags pad 1 depth_mm and outer_span_mm, not pad 2 or pitch."""
        pcb = _create_pcb_with_smd_footprints(
            tmp_path,
            [
                {
                    "ref": "R40",
                    "pads": [
                        _smd_chip_pad("1", -2.5, 0.0, 2.00, 3.50, shape="rect"),
                        _smd_chip_pad("2", 2.5, 0.0, 3.00, 3.50, shape="rect"),
                    ],
                }
            ],
        )
        _write_lead_spec(tmp_path, _STACKPOLE_RHC2512_YAML)

        result = _run_smd_land_pattern(pcb)

        assert result.status == "FAIL"
        (finding,) = result.data["components"]
        assert finding["mismatched_fields"] == ["pad 1 depth_mm", "outer_span_mm"]
        assert finding["measured"]["pitch_mm"] == pytest.approx(5.0)
        assert finding["measured"]["outer_span_mm"] == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# PreflightChecker -- DRC check
# ---------------------------------------------------------------------------


class TestPreflightDRC:
    """Tests for the DRC check."""

    def test_no_report_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_erc=True),
        )
        results = checker.run_all()
        drc_result = _find_result(results, "drc")
        assert drc_result is not None
        assert drc_result.status == "WARN"

    def test_skip_drc(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        drc_result = _find_result(results, "drc")
        assert drc_result is None  # Should not appear when skipped

    # -- Issue #7091: opens and source binding ---------------------------------

    @staticmethod
    def _write_kicad_cli_drc(
        path: Path,
        source: str,
        violations: list | None = None,
        unconnected: list | None = None,
    ) -> Path:
        """Write a kicad-cli-format DRC JSON report."""
        path.write_text(
            json.dumps(
                {
                    "$schema": "https://schemas.kicad.org/drc.v1.json",
                    "source": source,
                    "date": "2026-10-18T00:00:00+0000",
                    "kicad_version": "10.0.6",
                    "coordinate_units": "mm",
                    "violations": violations or [],
                    "unconnected_items": unconnected or [],
                    "schematic_parity": [],
                }
            )
        )
        return path

    @staticmethod
    def _open(severity: str = "error") -> dict:
        return {
            "type": "unconnected_items",
            "severity": severity,
            "description": "Missing connection between items",
            "items": [
                {"description": "Pad 1 [VCC] of C1", "pos": {"x": 1.0, "y": 1.0}},
                {"description": "Pad 1 [VCC] of C2", "pos": {"x": 2.0, "y": 1.0}},
            ],
        }

    def _drc(self, pcb: Path, report: Path) -> PreflightResult:
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(drc_report_path=str(report)),
        )
        return checker._check_drc()

    def test_unconnected_items_only_fails(self, tmp_path):
        """A report with only error-severity opens FAILs (was OK before #7091)."""
        pcb = _create_minimal_pcb(tmp_path)
        report = self._write_kicad_cli_drc(
            tmp_path / "drc_report.json", "board.kicad_pcb", unconnected=[self._open()] * 3
        )
        result = self._drc(pcb, report)
        assert result.status == "FAIL", result
        assert "0 error(s), 3 unconnected item(s)" in result.message
        assert "unconnected_items" in result.details

    def test_unconnected_items_fail_blocks_export(self, tmp_path):
        """The FAIL is what --strict-preflight blocks on."""
        pcb = _create_minimal_pcb(tmp_path)
        self._write_kicad_cli_drc(
            tmp_path / "drc_report.json", "board.kicad_pcb", unconnected=[self._open()]
        )
        checker = PreflightChecker(pcb_path=pcb, config=PreflightConfig(skip_erc=True))
        results = checker.run_all()
        assert _find_result(results, "drc").status == "FAIL"
        assert PreflightChecker.has_failures(results)

    def test_warning_severity_unconnected_items_do_not_fail(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = self._write_kicad_cli_drc(
            tmp_path / "drc_report.json",
            "board.kicad_pcb",
            unconnected=[self._open("warning")],
        )
        assert self._drc(pcb, report).status == "OK"

    def test_matching_clean_report_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = self._write_kicad_cli_drc(
            tmp_path / "drc_report.json", "/other/host/abs/path/board.kicad_pcb"
        )
        result = self._drc(pcb, report)
        assert result.status == "OK", result
        assert result.message == "DRC: 0 errors, 0 warnings"

    def test_report_for_other_board_warns_auto_discovered(self, tmp_path):
        """The ``*_routed`` sibling's clean report is not evidence for this board."""
        pcb = _create_minimal_pcb(tmp_path, filename="x.kicad_pcb")
        self._write_kicad_cli_drc(tmp_path / "drc_report.json", "x_routed.kicad_pcb")
        checker = PreflightChecker(pcb_path=pcb, config=PreflightConfig(skip_erc=True))
        result = _find_result(checker.run_all(), "drc")
        assert result.status == "WARN", result
        assert result.message == "DRC report is for x_routed.kicad_pcb, not x.kicad_pcb"

    def test_report_for_other_board_warns_explicit(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, filename="x.kicad_pcb")
        report = self._write_kicad_cli_drc(
            tmp_path / "custom.json", "C:\\boards\\x_routed.kicad_pcb"
        )
        result = self._drc(pcb, report)
        assert result.status == "WARN"
        assert "x_routed.kicad_pcb" in result.message

    def test_report_without_source_judged_as_before(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = self._write_kicad_cli_drc(
            tmp_path / "drc_report.json", "", unconnected=[self._open()]
        )
        assert self._drc(pcb, report).status == "FAIL"

    def test_text_report_unconnected_items_fail(self, tmp_path):
        """Text ``.rpt`` reports keep opens in ``violations``; still FAIL."""
        pcb = _create_minimal_pcb(tmp_path)
        report = tmp_path / "drc_report.txt"
        report.write_text(
            "** Drc report for board.kicad_pcb **\n"
            "** Created on 2026-10-18T00:00:00 **\n\n"
            "** Found 0 DRC violations **\n\n"
            "** Found 1 unconnected pads **\n"
            "[unconnected_items]: Missing connection between items\n"
            "    Local override; error\n"
            "    @(1.0000 mm, 1.0000 mm): Pad 1 [VCC] of C1\n"
            "    @(2.0000 mm, 1.0000 mm): Pad 1 [VCC] of C2\n\n"
            "** End of Report **\n"
        )
        result = self._drc(pcb, report)
        assert result.status == "FAIL", result
        assert "1 unconnected item(s)" in result.message


# ---------------------------------------------------------------------------
# PreflightChecker -- ERC check
# ---------------------------------------------------------------------------


class TestPreflightERC:
    """Tests for the ERC check."""

    def test_no_report_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True),
        )
        results = checker.run_all()
        erc_result = _find_result(results, "erc")
        assert erc_result is not None
        assert erc_result.status == "WARN"

    def test_skip_erc(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        erc_result = _find_result(results, "erc")
        assert erc_result is None

    # -- Issue #7091: source binding -------------------------------------------

    @staticmethod
    def _write_erc(path: Path, source: str | None) -> Path:
        data: dict = {
            "$schema": "https://schemas.kicad.org/erc.v1.json",
            "kicad_version": "10.0.6",
            "coordinate_units": "mm",
            "sheets": [{"path": "/", "uuid_path": "/", "violations": []}],
        }
        if source is not None:
            data["source"] = source
        path.write_text(json.dumps(data))
        return path

    def _erc(self, tmp_path: Path, sch_name: str, report: Path) -> PreflightResult:
        pcb = _create_minimal_pcb(tmp_path)
        sch = tmp_path / sch_name
        sch.write_text("(kicad_sch)")
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, erc_report_path=str(report)),
        )
        return checker._check_erc()

    def test_report_for_other_schematic_warns(self, tmp_path):
        report = self._write_erc(tmp_path / "erc_report.json", "/elsewhere/other.kicad_sch")
        result = self._erc(tmp_path, "board.kicad_sch", report)
        assert result.status == "WARN", result
        assert result.message == "ERC report is for other.kicad_sch, not board.kicad_sch"

    def test_matching_schematic_ok(self, tmp_path):
        report = self._write_erc(tmp_path / "erc_report.json", "board.kicad_sch")
        assert self._erc(tmp_path, "board.kicad_sch", report).status == "OK"

    def test_sourceless_report_no_false_warn(self, tmp_path):
        """``source_file`` falls back to the report's own path; not a mismatch."""
        report = self._write_erc(tmp_path / "erc_report.json", None)
        assert self._erc(tmp_path, "board.kicad_sch", report).status == "OK"

    def test_no_schematic_skips_comparison(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        report = self._write_erc(tmp_path / "erc_report.json", "other.kicad_sch")
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, erc_report_path=str(report)),
        )
        assert checker._check_erc().status == "OK"


# ---------------------------------------------------------------------------
# PreflightChecker -- BOM/PCB match severity
# ---------------------------------------------------------------------------


class TestPreflightBomPcbMatchSeverity:
    """Tests for bom_pcb_match FAIL vs WARN based on mismatch direction."""

    def _make_checker_with_mocks(
        self, monkeypatch, bom_items, pcb_refs, exclude_from_bom=(), board_only=()
    ):
        """Create a PreflightChecker with mocked BOM and PCB data.

        Args:
            monkeypatch: pytest monkeypatch fixture.
            bom_items: list of BOMItem instances for the BOM.
            pcb_refs: list of reference strings for PCB footprints.
            exclude_from_bom: refs whose footprint has ``exclude_from_bom``.
            board_only: refs whose footprint has the ``board_only`` attr flag.
        """
        from unittest.mock import MagicMock

        from kicad_tools.schema.bom import BOM

        checker = PreflightChecker.__new__(PreflightChecker)
        checker.config = PreflightConfig()

        # Mock _pcb with footprints that have .reference attributes
        mock_pcb = MagicMock()
        fps = []
        for ref in pcb_refs:
            fp = MagicMock()
            fp.reference = ref
            # Explicit values: a bare MagicMock attribute is truthy and would
            # silently exempt every footprint.
            fp.exclude_from_bom = ref in exclude_from_bom
            fp.attr_flags = frozenset({"board_only"}) if ref in board_only else frozenset()
            fps.append(fp)
        mock_pcb.footprints = fps
        checker._pcb = mock_pcb

        # Mock _load_bom to return our BOM
        bom = BOM(items=bom_items)
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        return checker

    def _make_bom_item(self, reference, **kwargs):
        from kicad_tools.schema.bom import BOMItem

        defaults = {
            "value": "100nF",
            "footprint": "C_0402",
            "lib_id": "Device:C",
        }
        defaults.update(kwargs)
        return BOMItem(reference=reference, **defaults)

    def test_all_match_ok(self, monkeypatch):
        """When BOM and PCB refs match exactly, status is OK."""
        items = [self._make_bom_item("C1"), self._make_bom_item("R1")]
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1", "R1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "OK"
        assert "2 components" in result.message

    def test_unplaced_components_fail(self, monkeypatch):
        """BOM refs not on PCB (unplaced) should produce FAIL."""
        items = [
            self._make_bom_item("C1"),
            self._make_bom_item("R1"),
            self._make_bom_item("U1"),
        ]
        # PCB only has C1 -- R1 and U1 are unplaced
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "FAIL"
        assert "in BOM but not on PCB" in result.details

    def test_orphaned_footprints_warn(self, monkeypatch):
        """PCB refs not in BOM (orphaned) should produce WARN, not FAIL."""
        items = [self._make_bom_item("C1")]
        # PCB has C1 plus extra MH1 (mounting hole)
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1", "MH1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "WARN"
        assert "on PCB but not in BOM" in result.details

    def test_both_directions_fail_takes_precedence(self, monkeypatch):
        """When both unplaced and orphaned exist, FAIL takes precedence."""
        items = [self._make_bom_item("C1"), self._make_bom_item("R1")]
        # PCB has C1 and MH1 -- R1 is unplaced, MH1 is orphaned
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1", "MH1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "FAIL"
        assert "in BOM but not on PCB" in result.details
        assert "on PCB but not in BOM" in result.details

    def test_virtual_and_dnp_excluded(self, monkeypatch):
        """Virtual and DNP items should not cause mismatches."""
        items = [
            self._make_bom_item("C1"),
            # Power symbol (virtual -- lib_id starts with "power:")
            self._make_bom_item("PWR1", lib_id="power:VCC"),
            # DNP component
            self._make_bom_item("R99", dnp=True),
        ]
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "OK"

    def test_empty_bom_with_pcb_warns(self, monkeypatch):
        """Empty BOM with PCB footprints should WARN (orphaned footprints)."""
        checker = self._make_checker_with_mocks(monkeypatch, [], ["C1", "R1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "WARN"

    def test_bom_with_empty_pcb_fails(self, monkeypatch):
        """BOM items with no PCB footprints should FAIL (unplaced)."""
        items = [self._make_bom_item("C1"), self._make_bom_item("R1")]
        checker = self._make_checker_with_mocks(monkeypatch, items, [])

        result = checker._check_bom_footprint_match()
        assert result.status == "FAIL"

    def test_has_failures_blocks_export(self, monkeypatch):
        """Verify that FAIL from bom_pcb_match is detected by has_failures."""
        items = [self._make_bom_item("C1"), self._make_bom_item("R1")]
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "FAIL"
        assert PreflightChecker.has_failures([result]) is True

    def test_orphaned_only_does_not_block_export(self, monkeypatch):
        """Verify that WARN from orphaned footprints does NOT block export."""
        items = [self._make_bom_item("C1")]
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1", "MH1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "WARN"
        assert PreflightChecker.has_failures([result]) is False

    def test_excluded_fiducials_do_not_warn(self, monkeypatch):
        """Footprints flagged exclude_from_bom / board_only are not orphans."""
        items = [self._make_bom_item("C1")]
        checker = self._make_checker_with_mocks(
            monkeypatch,
            items,
            ["C1", "FID1", "FID2", "TP1"],
            exclude_from_bom={"FID1"},
            board_only={"FID2", "TP1"},
        )

        result = checker._check_bom_footprint_match()
        assert result.status == "OK"

    def test_footprint_without_exempt_attrs_still_warns(self, monkeypatch):
        """A non-exempt footprint missing from the BOM still warns."""
        items = [self._make_bom_item("C1")]
        checker = self._make_checker_with_mocks(
            monkeypatch,
            items,
            ["C1", "FID1", "R7"],
            exclude_from_bom={"FID1"},
        )

        result = checker._check_bom_footprint_match()
        assert result.status == "WARN"
        assert "R7" in result.details
        assert "FID1" not in result.details

    def test_excluded_footprint_in_bom_still_matches(self, monkeypatch):
        """An exempt footprint that is also in the BOM is not a FAIL."""
        items = [self._make_bom_item("C1"), self._make_bom_item("FID1")]
        checker = self._make_checker_with_mocks(
            monkeypatch, items, ["C1", "FID1"], exclude_from_bom={"FID1"}
        )

        result = checker._check_bom_footprint_match()
        assert result.status == "OK"


# ---------------------------------------------------------------------------
# PreflightChecker -- bom_fields LCSC spec-overlay awareness (issue #3926)
# ---------------------------------------------------------------------------


class TestPreflightBomFieldsSpecOverlay:
    """`_check_bom_fields` must not falsely warn about LCSC that the spec fills.

    Preflight loads a *raw* BOM (Step 0) before the assembly pipeline applies
    the ``.kct`` spec overlay (Step 1).  Items whose LCSC will be populated
    from the spec are not genuinely missing -- warning about them is the false
    positive fixed in issue #3926.
    """

    def _make_checker(self, monkeypatch, bom_items, spec_lcsc_refs):
        """Build a bare checker with mocked BOM and spec-resolution."""
        from kicad_tools.schema.bom import BOM

        checker = PreflightChecker.__new__(PreflightChecker)
        checker.config = PreflightConfig()
        checker.pcb_path = Path("board.kicad_pcb")

        bom = BOM(items=bom_items)
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)
        monkeypatch.setattr(checker, "_spec_lcsc_refs", lambda: set(spec_lcsc_refs))
        return checker

    def _item(self, reference, **kwargs):
        from kicad_tools.schema.bom import BOMItem

        defaults = {"value": "100nF", "footprint": "C_0402", "lib_id": "Device:C", "lcsc": ""}
        defaults.update(kwargs)
        return BOMItem(reference=reference, **defaults)

    def test_all_missing_lcsc_covered_by_spec_no_warn(self, monkeypatch):
        """When every missing-LCSC item is spec-covered, no WARN fires."""
        items = [self._item("C1"), self._item("R1", lib_id="Device:R")]
        checker = self._make_checker(monkeypatch, items, {"C1", "R1"})

        result = checker._check_bom_fields()

        assert result.status == "OK"
        assert "missing LCSC" not in result.message
        assert "missing LCSC" not in result.details
        assert "spec overlay" in result.details

    def test_partial_spec_coverage_warns_only_uncovered(self, monkeypatch):
        """Only the items NOT covered by the spec are counted in the WARN."""
        items = [
            self._item("C1"),
            self._item("C2"),
            self._item("R1", lib_id="Device:R"),
        ]
        # Spec covers only C1, C2 -- R1 remains genuinely missing.
        checker = self._make_checker(monkeypatch, items, {"C1", "C2"})

        result = checker._check_bom_fields()

        assert result.status == "WARN"
        assert "1/3 active item(s) missing LCSC part number" in result.details
        assert "2 resolvable from spec" in result.details

    def test_no_spec_all_missing_still_warns(self, monkeypatch):
        """With no spec coverage, every missing-LCSC item still warns."""
        items = [self._item("C1"), self._item("R1", lib_id="Device:R")]
        checker = self._make_checker(monkeypatch, items, set())

        result = checker._check_bom_fields()

        assert result.status == "WARN"
        assert "2/2 active item(s) missing LCSC part number" in result.details
        assert "resolvable from spec" not in result.details

    def test_items_with_lcsc_never_warn(self, monkeypatch):
        """Items that already carry an LCSC are never counted as missing."""
        items = [self._item("C1", lcsc="C111"), self._item("R1", lcsc="C222")]
        checker = self._make_checker(monkeypatch, items, set())

        result = checker._check_bom_fields()

        assert result.status == "OK"
        assert "missing LCSC" not in result.details

    def test_spec_lcsc_refs_reads_kct_file(self, monkeypatch, tmp_path):
        """`_spec_lcsc_refs` resolves refs (incl. ranges) from a real .kct."""
        (tmp_path / "project.kct").write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            '  revision: "A"\n'
            "bom_entries:\n"
            "  - ref: C1\n"
            "    part: MPN1\n"
            "    lcsc: C111\n"
            "  - ref: R1-R3\n"
            "    part: MPN2\n"
            "    lcsc: C222\n"
            "  - ref: U1\n"  # MPN only, no lcsc -> must NOT be resolved
            "    part: MPN3\n"
        )
        # No .git boundary in tmp_path, so find_spec_file finds project.kct.
        checker = PreflightChecker(pcb_path=tmp_path / "board.kicad_pcb")

        refs = checker._spec_lcsc_refs()

        assert refs == {"C1", "R1", "R2", "R3"}
        assert "U1" not in refs

    def test_spec_lcsc_refs_no_spec_returns_empty(self, monkeypatch, tmp_path):
        """No .kct file present -> empty set, preserving legacy warn behaviour."""
        # tmp_path has no .kct and no .git; find_spec_file walks up harmlessly.
        checker = PreflightChecker(pcb_path=tmp_path / "board.kicad_pcb")

        assert checker._spec_lcsc_refs() == set()


# ---------------------------------------------------------------------------
# PreflightChecker -- skip all
# ---------------------------------------------------------------------------


class TestPreflightSkipAll:
    """Tests for skip_all config."""

    def test_skip_all_returns_empty(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_all=True),
        )
        results = checker.run_all()
        assert results == []


# ---------------------------------------------------------------------------
# PreflightChecker -- static helpers
# ---------------------------------------------------------------------------


class TestPreflightStaticHelpers:
    """Tests for static helper methods."""

    def test_has_failures_true(self):
        results = [
            PreflightResult(name="a", status="OK", message="ok"),
            PreflightResult(name="b", status="FAIL", message="bad"),
        ]
        assert PreflightChecker.has_failures(results) is True

    def test_has_failures_false(self):
        results = [
            PreflightResult(name="a", status="OK", message="ok"),
            PreflightResult(name="b", status="WARN", message="meh"),
        ]
        assert PreflightChecker.has_failures(results) is False

    def test_has_warnings_true(self):
        results = [
            PreflightResult(name="a", status="OK", message="ok"),
            PreflightResult(name="b", status="WARN", message="meh"),
        ]
        assert PreflightChecker.has_warnings(results) is True

    def test_has_warnings_false(self):
        results = [
            PreflightResult(name="a", status="OK", message="ok"),
        ]
        assert PreflightChecker.has_warnings(results) is False

    def test_has_failures_empty(self):
        assert PreflightChecker.has_failures([]) is False

    def test_has_warnings_empty(self):
        assert PreflightChecker.has_warnings([]) is False


# ---------------------------------------------------------------------------
# Integration -- manufacturing package with preflight
# ---------------------------------------------------------------------------


class TestPreflightManufacturingIntegration:
    """Tests for preflight integration in ManufacturingPackage."""

    def test_export_with_preflight_ok(self, tmp_path, monkeypatch):
        """Manufacturing export should succeed when preflight passes."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "eeschema"))'
        )
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
        from kicad_tools.export.preflight import PreflightConfig

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom = od / "bom_jlcpcb.csv"
            bom.write_text("Comment,Designator\n")
            return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert result.success, f"Errors: {result.errors}"
        assert len(result.preflight_results) > 0
        assert not PreflightChecker.has_failures(result.preflight_results)

    def test_export_blocked_by_preflight_fail(self, tmp_path):
        """Manufacturing export should fail when the PCB cannot be loaded.

        Previously this test relied on the preflight ``file_exists`` check
        catching a missing PCB.  After issue #2741 the export pipeline
        constructs an ``AssemblyPackage`` up-front (so a missing schematic
        cannot leak a partial package), which means a missing PCB now
        fails in ``AssemblyPackage.__init__`` before preflight runs.  The
        observable contract -- "export fails, nothing is written" -- is
        unchanged.
        """
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
        from kicad_tools.export.preflight import PreflightConfig

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        # Non-existent PCB will cause a fatal failure
        pkg = ManufacturingPackage(
            pcb_path=tmp_path / "nonexistent.kicad_pcb",
            manufacturer="jlcpcb",
            config=config,
        )
        out_dir = tmp_path / "output"
        result = pkg.export(out_dir)

        assert not result.success
        assert any("PCB file not found" in e for e in result.errors)
        # No output directory should be created on the fail-fast path
        assert not out_dir.exists()

    def test_skip_preflight_bypasses_checks(self, tmp_path, monkeypatch):
        """With skip_all=True, preflight checks are not run."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_pro").write_text("{}")
        # A sibling .kicad_sch is needed because AssemblyPackage now fails
        # fast when the BOM is schematic-sourced and the schematic cannot
        # be auto-discovered (regression: see issue #2741).
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
        from kicad_tools.export.preflight import PreflightConfig

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert result.success
        assert result.preflight_results == []

    def test_preflight_results_in_manifest(self, tmp_path, monkeypatch):
        """Preflight results should be included in manifest.json."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "eeschema"))'
        )
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
        from kicad_tools.export.preflight import PreflightConfig

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom = od / "bom_jlcpcb.csv"
            bom.write_text("Comment,Designator\n")
            return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert result.success
        assert result.manifest_path is not None
        assert result.manifest_path.exists()

        manifest = json.loads(result.manifest_path.read_text())
        assert "preflight" in manifest
        assert isinstance(manifest["preflight"], list)
        assert len(manifest["preflight"]) > 0
        # Each entry should have name, status, message
        for entry in manifest["preflight"]:
            assert "name" in entry
            assert "status" in entry
            assert "message" in entry


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestPreflightCLI:
    """Tests for preflight CLI flags."""

    def test_skip_preflight_flag(self, tmp_path, monkeypatch):
        """--skip-preflight should skip all checks."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_pro").write_text("{}")
        # Sibling .kicad_sch needed: AssemblyPackage now fails fast when
        # the BOM is schematic-sourced and no schematic is discoverable
        # (regression: see issue #2741).
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        from kicad_tools.cli.export_cmd import main as export_main

        rc = export_main(
            [
                str(pcb),
                "--skip-preflight",
                "--no-report",
                "--no-project-zip",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0

    def test_skip_drc_flag(self, tmp_path, monkeypatch):
        """--skip-drc should skip only DRC."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "eeschema"))'
        )
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom = od / "bom_jlcpcb.csv"
            bom.write_text("Comment,Designator\n")
            return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        from kicad_tools.cli.export_cmd import main as export_main

        rc = export_main(
            [
                str(pcb),
                "--skip-drc",
                "--skip-erc",
                "--no-report",
                "--no-project-zip",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0

    def test_json_format_output(self, tmp_path, monkeypatch, capsys):
        """--format json should produce JSON output."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_pro").write_text("{}")
        # Sibling .kicad_sch needed: AssemblyPackage now fails fast when
        # the BOM is schematic-sourced and no schematic is discoverable
        # (regression: see issue #2741).
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        from kicad_tools.cli.export_cmd import main as export_main

        rc = export_main(
            [
                str(pcb),
                "--skip-preflight",
                "--no-report",
                "--no-project-zip",
                "--format",
                "json",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "success" in data
        assert data["success"] is True

    def test_parser_recognizes_preflight_flags(self):
        """The main parser should recognize preflight flags."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "export",
                "board.kicad_pcb",
                "--skip-preflight",
                "--skip-drc",
                "--skip-erc",
                "--drc-report",
                "/tmp/drc.json",
                "--erc-report",
                "/tmp/erc.json",
                "--format",
                "json",
            ]
        )
        assert args.export_skip_preflight is True
        assert args.export_skip_drc is True
        assert args.export_skip_erc is True
        assert args.export_drc_report == "/tmp/drc.json"
        assert args.export_erc_report == "/tmp/erc.json"
        assert args.export_format == "json"


# ---------------------------------------------------------------------------
# PreflightChecker -- BOM/CPL match check
# ---------------------------------------------------------------------------


class TestPreflightBomCplMatch:
    """Tests for the BOM/CPL cross-reference check."""

    def test_bom_cpl_match_ok(self, tmp_path, monkeypatch):
        """When BOM refs and CPL-eligible PCB refs match, result is OK."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "R2", "C1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        # Parse PCB first
        checker.run_all()

        # Monkeypatch _load_bom to return matching refs
        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.name == "bom_cpl_match"
        assert result.status == "OK"
        assert "3 components" in result.message

    def test_bom_cpl_match_bom_only(self, tmp_path, monkeypatch):
        """Items in BOM but not in CPL should cause FAIL."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "C1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "FAIL"
        assert "R2" in result.details
        assert "in BOM but not in CPL" in result.details

    def test_bom_cpl_match_cpl_only(self, tmp_path, monkeypatch):
        """Items in CPL but not in BOM should cause FAIL."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "R2", "C1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "FAIL"
        assert "in CPL but not in BOM" in result.details

    def test_bom_cpl_match_dnp_excluded(self, tmp_path, monkeypatch):
        """DNP items in BOM should be excluded from comparison."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "C1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(
                    reference="R2", value="10k", footprint="R_0402", lib_id="Device:R", dnp=True
                ),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "OK"
        assert "2 components" in result.message

    def test_bom_cpl_match_virtual_excluded(self, tmp_path, monkeypatch):
        """Virtual (power symbol) items in BOM should be excluded."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="#PWR01", value="GND", footprint="", lib_id="power:GND"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "OK"

    def test_bom_cpl_match_no_pcb(self, tmp_path):
        """Without a loaded PCB, check should return WARN."""
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=tmp_path / "nonexistent.kicad_pcb",
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        # PCB won't be loaded since file doesn't exist
        result = checker._check_bom_cpl_match()
        assert result.status == "WARN"
        assert "PCB not loaded" in result.message

    def test_bom_cpl_match_bom_load_fails(self, tmp_path, monkeypatch):
        """If BOM cannot be loaded, check should return WARN."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        monkeypatch.setattr(
            checker, "_load_bom", lambda: (_ for _ in ()).throw(RuntimeError("parse error"))
        )

        result = checker._check_bom_cpl_match()
        assert result.status == "WARN"
        assert "parse error" in result.message

    def test_bom_cpl_match_in_run_all(self, tmp_path, monkeypatch):
        """The bom_cpl_match check should appear in run_all() results."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            ]
        )

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        monkeypatch.setattr(
            "kicad_tools.export.preflight.PreflightChecker._load_bom",
            lambda self: bom,
        )

        results = checker.run_all()
        cpl_result = _find_result(results, "bom_cpl_match")
        assert cpl_result is not None
        assert cpl_result.status == "OK"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_result(results: list[PreflightResult], name: str) -> PreflightResult | None:
    """Find a preflight result by check name."""
    for r in results:
        if r.name == name:
            return r
    return None


def _create_minimal_pcb(
    directory: Path,
    filename: str = "board.kicad_pcb",
    include_outline: bool = True,
) -> Path:
    """Create a minimal valid KiCad PCB file for testing."""
    outline = ""
    if include_outline:
        # A closed rectangular outline on Edge.Cuts
        outline = """
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
"""

    content = f"""(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
{outline}
)
"""
    pcb_path = directory / filename
    pcb_path.write_text(content)
    return pcb_path


def _create_pcb_with_via(directory: Path) -> Path:
    """Create a PCB with a via for drill hole testing."""
    content = """(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (via (at 25 25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
"""
    pcb_path = directory / "board.kicad_pcb"
    pcb_path.write_text(content)
    return pcb_path


def _create_pcb_with_footprints(
    directory: Path,
    references: list[str],
    filename: str = "board.kicad_pcb",
) -> Path:
    """Create a PCB with footprints for BOM/CPL testing.

    Each reference in *references* becomes a simple SMD footprint on F.Cu.
    A closed board outline is included so that other checks pass.
    """
    footprints = []
    for i, ref in enumerate(references):
        x = 10.0 + i * 5.0
        footprints.append(
            f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at {x} 25)
    (attr smd)
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )"""
        )

    fp_block = "\n".join(footprints)
    content = f"""(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
{fp_block}
)
"""
    pcb_path = directory / filename
    pcb_path.write_text(content)
    return pcb_path


def _create_pcb_with_mixed_footprints(
    directory: Path,
    smd_refs: list[str],
    tht_refs: list[str],
    dnp_refs: list[str] | None = None,
    filename: str = "board.kicad_pcb",
) -> Path:
    """Create a PCB with SMD and THT footprints for testing.

    Args:
        directory: Directory to write the PCB file.
        smd_refs: References for SMD footprints.
        tht_refs: References for through-hole footprints.
        dnp_refs: References for DNP footprints (added as SMD with dnp flag).
        filename: Output filename.
    """
    footprints = []
    x = 10.0
    for ref in smd_refs:
        footprints.append(
            f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at {x} 25)
    (attr smd)
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )"""
        )
        x += 5.0

    for ref in tht_refs:
        footprints.append(
            f"""  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
    (layer "F.Cu")
    (at {x} 25)
    (attr through_hole)
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value "Conn_01x04" (at 0 1.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 0 ""))
  )"""
        )
        x += 5.0

    for ref in dnp_refs or []:
        footprints.append(
            f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at {x} 25)
    (attr smd dnp)
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value "DNP" (at 0 1.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )"""
        )
        x += 5.0

    fp_block = "\n".join(footprints)
    content = f"""(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
{fp_block}
)
"""
    pcb_path = directory / filename
    pcb_path.write_text(content)
    return pcb_path


# ---------------------------------------------------------------------------
# THT-in-CPL preflight check tests
# ---------------------------------------------------------------------------


class TestPreflightTHTInCPL:
    """Tests for the THT-in-CPL preflight check."""

    def test_no_tht_components(self, tmp_path):
        """All-SMD board should produce OK result."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "R2"])
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()
        result = checker._check_tht_in_cpl()
        assert result.status == "OK"
        assert "No through-hole" in result.message

    @pytest.fixture
    def smt_only_jlcpcb(self, monkeypatch):
        """Model a still-SMT-only fab (JLCPCB itself assembles THT, issue #7239)."""
        monkeypatch.setattr(PreflightChecker, "_SMT_ONLY_MANUFACTURERS", {"jlcpcb"})

    def test_tht_present_jlcpcb_default_included(self, tmp_path):
        """JLCPCB assembles THT (issue #7239): default includes THT in the CPL."""
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], ["J1"])
        for mfr in ("jlcpcb", "jlcpcb-tier1"):
            checker = PreflightChecker(
                pcb_path=pcb,
                manufacturer=mfr,
                config=PreflightConfig(skip_drc=True, skip_erc=True),
            )
            assert checker._exclude_tht is False, mfr
            checker.run_all()
            result = checker._check_tht_in_cpl()
            assert result.status == "OK", mfr
            assert "included in CPL" in result.message
            assert "J1" in result.details

    def test_tht_present_jlcpcb_excluded(self, tmp_path, smt_only_jlcpcb):
        """An SMT-only fab with THT should show excluded message."""
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], ["J1"])
        checker = PreflightChecker(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()
        result = checker._check_tht_in_cpl()
        assert result.status == "OK"
        assert "excluded from CPL" in result.message
        assert "J1" in result.details

    def test_tht_present_jlcpcb_include_override(self, tmp_path, smt_only_jlcpcb):
        """JLCPCB with explicit exclude_tht=False should warn about THT."""
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], ["J1"])
        checker = PreflightChecker(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            exclude_tht=False,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()
        result = checker._check_tht_in_cpl()
        assert result.status == "WARN"
        assert "SMT-only" in result.message

    def test_tht_present_generic_ok(self, tmp_path):
        """Generic manufacturer with THT should show included message."""
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], ["J1"])
        checker = PreflightChecker(
            pcb_path=pcb,
            manufacturer="generic",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()
        result = checker._check_tht_in_cpl()
        assert result.status == "OK"
        assert "included" in result.message


class TestPreflightBomCplMatchTHT:
    """Tests for BOM/CPL match check accounting for THT exclusions."""

    def test_tht_excluded_not_false_positive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(PreflightChecker, "_SMT_ONLY_MANUFACTURERS", {"jlcpcb"})
        """THT components excluded from CPL should not cause BOM/CPL mismatch."""
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1", "C1"], ["J1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            manufacturer="jlcpcb",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
                BOMItem(
                    reference="J1", value="Conn", footprint="PinHeader", lib_id="Connector:Conn"
                ),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "OK"
        assert "THT excluded" in result.message

    def test_dnp_excluded_not_false_positive(self, tmp_path, monkeypatch):
        """DNP components on PCB should not cause BOM/CPL mismatch."""
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], [], dnp_refs=["R2"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            manufacturer="jlcpcb",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "OK"
        assert "DNP excluded" in result.message


# ---------------------------------------------------------------------------
# Capability-tier manufacturers resolve THT policy via the fab family (#4591)
# ---------------------------------------------------------------------------


class TestPreflightFabFamilyTHT:
    """``jlcpcb-tier1`` must inherit the ``jlcpcb`` SMT-only THT policy.

    The CPL writer resolves the fab *family* (``get_fab_family()``) before
    picking a formatter, so a preflight that keyed off the raw ``--mfr``
    string predicted "THT included" for a CPL that in fact excluded THT --
    producing a false ``tht_in_cpl`` OK and a spurious ``bom_cpl_match``
    FAIL (issue #4591).
    """

    @pytest.fixture(autouse=True)
    def _smt_only_jlcpcb(self, monkeypatch):
        """Model a still-SMT-only fab (JLCPCB itself assembles THT, issue #7239)."""
        monkeypatch.setattr(PreflightChecker, "_SMT_ONLY_MANUFACTURERS", {"jlcpcb"})

    def test_tier_id_resolves_exclude_tht(self, tmp_path):
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], ["J1"])
        base = PreflightChecker(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        tier = PreflightChecker(
            pcb_path=pcb,
            manufacturer="jlcpcb-tier1",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        assert base._exclude_tht is True
        assert tier._exclude_tht is True
        assert tier._fab_family == "jlcpcb"

    def test_tier_id_tht_in_cpl_reports_excluded(self, tmp_path):
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], ["J1", "J2"])
        checker = PreflightChecker(
            pcb_path=pcb,
            manufacturer="jlcpcb-tier1",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()
        result = checker._check_tht_in_cpl()
        assert result.status == "OK"
        assert "excluded from CPL" in result.message
        assert "included in CPL" not in result.message
        # Details still enumerate the THT references.
        assert "J1" in result.details
        assert "J2" in result.details

    def test_tier_id_bom_cpl_match_no_false_fail(self, tmp_path, monkeypatch):
        """A BOM/CPL delta that is purely THT exclusion must report OK."""
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1", "C1"], ["J1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            manufacturer="jlcpcb-tier1",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        # J1 is on the PCB but absent from the BOM -- exactly the shape that
        # produced "1 in CPL but not in BOM" FAIL before the fix.
        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "OK"
        assert "THT excluded" in result.message

    def test_include_tht_override_wins_for_both_ids(self, tmp_path):
        """Explicit ``exclude_tht=False`` (--include-tht) keeps precedence."""
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], ["J1"])
        for mfr in ("jlcpcb", "jlcpcb-tier1"):
            checker = PreflightChecker(
                pcb_path=pcb,
                manufacturer=mfr,
                exclude_tht=False,
                config=PreflightConfig(skip_drc=True, skip_erc=True),
            )
            assert checker._exclude_tht is False, mfr
            checker.run_all()
            result = checker._check_tht_in_cpl()
            assert result.status == "WARN", mfr
            assert "SMT-only" in result.message
            # The user-facing message names the ID the user actually passed.
            assert mfr in result.message

    def test_non_jlc_family_still_includes_tht(self, tmp_path):
        pcb = _create_pcb_with_mixed_footprints(tmp_path, ["R1"], ["J1"])
        checker = PreflightChecker(
            pcb_path=pcb,
            manufacturer="generic",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        assert checker._exclude_tht is False
        checker.run_all()
        result = checker._check_tht_in_cpl()
        assert result.status == "OK"
        assert "included in CPL" in result.message

    def test_drc_limits_still_use_full_manufacturer_id(self, tmp_path, monkeypatch):
        """Tier DRC limits must NOT be resolved through the fab family."""
        import kicad_tools.manufacturers as mfr_mod

        seen: list[str] = []
        real_get_profile = mfr_mod.get_profile

        def _spy(manufacturer_id: str):
            seen.append(manufacturer_id)
            return real_get_profile(manufacturer_id)

        monkeypatch.setattr(mfr_mod, "get_profile", _spy)

        pcb = _create_pcb_with_footprints(tmp_path, ["R1"])
        checker = PreflightChecker(
            pcb_path=pcb,
            manufacturer="jlcpcb-tier1",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker._get_manufacturer_limits()
        assert seen == ["jlcpcb-tier1"]
