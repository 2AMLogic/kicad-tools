"""Tests for the .kct project specification module."""

from __future__ import annotations

import os
import stat
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
import yaml


class TestUnitParsing:
    """Tests for unit value parsing."""

    def test_parse_voltage(self):
        """Test parsing voltage values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("5V")
        assert result.value == 5.0
        assert result.unit == "V"

        result = parse_unit_value("3.3V")
        assert result.value == 3.3
        assert result.unit == "V"

    def test_parse_millivolt(self):
        """Test parsing millivolt values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("100mV")
        assert result.value == 0.1
        assert result.unit == "V"

    def test_parse_current(self):
        """Test parsing current values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("2A")
        assert result.value == 2.0
        assert result.unit == "A"

        result = parse_unit_value("500mA")
        assert result.value == 0.5
        assert result.unit == "A"

    def test_parse_resistance_with_unit(self):
        """Test parsing resistance with explicit unit."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("10kΩ")
        assert result.value == 10000.0
        assert result.unit == "Ω"

    def test_parse_bare_resistance(self):
        """Test parsing bare resistance values like '10k'."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("10k")
        assert result.value == 10000.0
        assert result.unit == "Ω"

        result = parse_unit_value("4.7M")
        assert result.value == 4700000.0
        assert result.unit == "Ω"

    def test_parse_capacitance(self):
        """Test parsing capacitance values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("100nF")
        assert result.value == pytest.approx(100e-9)
        assert result.unit == "F"

        result = parse_unit_value("4.7μF")
        assert result.value == pytest.approx(4.7e-6)
        assert result.unit == "F"

    def test_parse_length(self):
        """Test parsing length values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("0.15mm")
        assert result.value == 0.15
        assert result.unit == "mm"

    def test_parse_percentage(self):
        """Test parsing percentage values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("5%")
        assert result.value == 5.0
        assert result.unit == "%"

    def test_parse_with_suffix(self):
        """Test parsing values with measurement suffix."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("50mV_pp")
        assert result.value == 0.05
        assert result.unit == "V_pp"

    def test_parse_negative(self):
        """Test parsing negative values."""
        from kicad_tools.spec.units import parse_unit_value

        result = parse_unit_value("-40°C")
        assert result.value == -40.0
        assert result.unit == "°C"

    def test_parse_invalid(self):
        """Test that invalid values raise ValueError."""
        from kicad_tools.spec.units import parse_unit_value

        with pytest.raises(ValueError):
            parse_unit_value("invalid")

    def test_unit_value_comparison(self):
        """Test UnitValue comparison."""
        from kicad_tools.spec.units import parse_unit_value

        v1 = parse_unit_value("5V")
        v2 = parse_unit_value("3.3V")
        v3 = parse_unit_value("5V")

        assert v1 == v3
        assert v1 > v2
        assert v2 < v1


class TestProjectSpec:
    """Tests for ProjectSpec schema."""

    def test_create_minimal_spec(self):
        """Test creating a minimal spec."""
        from kicad_tools.spec import DesignIntent, ProjectMetadata, ProjectSpec

        spec = ProjectSpec(
            project=ProjectMetadata(name="Test Project"),
            intent=DesignIntent(summary="A test board"),
        )

        assert spec.project.name == "Test Project"
        assert spec.intent.summary == "A test board"
        assert spec.kct_version == "1.0"

    def test_create_full_spec(self):
        """Test creating a spec with all sections."""
        from kicad_tools.spec import (
            DesignIntent,
            ElectricalRequirements,
            Progress,
            ProjectMetadata,
            ProjectSpec,
            Requirements,
        )
        from kicad_tools.spec.schema import DesignPhase

        spec = ProjectSpec(
            project=ProjectMetadata(
                name="Power Supply",
                revision="B",
                created=date(2024, 1, 15),
                author="test@example.com",
            ),
            intent=DesignIntent(
                summary="USB-C PD power supply",
                use_cases=["Bench power", "Dev board power"],
            ),
            requirements=Requirements(
                electrical=ElectricalRequirements(
                    esd_protection="IEC 61000-4-2 Level 4",
                ),
            ),
            progress=Progress(
                phase=DesignPhase.SCHEMATIC,
            ),
        )

        assert spec.project.revision == "B"
        assert len(spec.intent.use_cases) == 2
        assert spec.requirements.electrical.esd_protection == "IEC 61000-4-2 Level 4"
        assert spec.progress.phase == DesignPhase.SCHEMATIC

    def test_invalid_version(self):
        """Test that invalid KCT version raises error."""
        from pydantic import ValidationError

        from kicad_tools.spec import ProjectMetadata, ProjectSpec

        with pytest.raises(ValidationError):
            ProjectSpec(
                kct_version="2.0",
                project=ProjectMetadata(name="Test"),
            )

    def test_completion_percentage_empty(self):
        """Test completion percentage with no progress."""
        from kicad_tools.spec import ProjectMetadata, ProjectSpec

        spec = ProjectSpec(project=ProjectMetadata(name="Test"))
        assert spec.get_completion_percentage() == 0.0

    def test_completion_percentage(self):
        """Test completion percentage calculation."""
        from kicad_tools.spec import PhaseProgress, Progress, ProjectMetadata, ProjectSpec

        spec = ProjectSpec(
            project=ProjectMetadata(name="Test"),
            progress=Progress(
                phase="schematic",
                phases={
                    "schematic": PhaseProgress(
                        checklist=[
                            "[x] Item 1",
                            "[x] Item 2",
                            "[ ] Item 3",
                            "[ ] Item 4",
                        ]
                    )
                },
            ),
        )

        assert spec.get_completion_percentage() == 50.0


class TestSpecParser:
    """Tests for spec file loading and saving."""

    def test_save_and_load(self):
        """Test saving and loading a spec file."""
        from kicad_tools.spec import (
            DesignIntent,
            ProjectMetadata,
            ProjectSpec,
            load_spec,
            save_spec,
        )

        spec = ProjectSpec(
            project=ProjectMetadata(
                name="Test Project",
                revision="A",
                created=date.today(),
            ),
            intent=DesignIntent(summary="Test board for testing"),
        )

        with tempfile.NamedTemporaryFile(suffix=".kct", delete=False) as f:
            path = Path(f.name)

        try:
            save_spec(spec, path)
            loaded = load_spec(path)

            assert loaded.project.name == "Test Project"
            assert loaded.intent.summary == "Test board for testing"
        finally:
            path.unlink()

    def test_load_nonexistent(self):
        """Test loading nonexistent file raises error."""
        from kicad_tools.spec import load_spec

        with pytest.raises(FileNotFoundError):
            load_spec("/nonexistent/file.kct")

    def test_validate_valid_spec(self):
        """Test validation of valid spec."""
        from kicad_tools.spec import (
            ProjectMetadata,
            ProjectSpec,
            save_spec,
            validate_spec,
        )

        spec = ProjectSpec(project=ProjectMetadata(name="Test"))

        with tempfile.NamedTemporaryFile(suffix=".kct", delete=False) as f:
            path = Path(f.name)

        try:
            save_spec(spec, path)
            is_valid, errors = validate_spec(path)

            assert is_valid
            assert len(errors) == 0
        finally:
            path.unlink()

    def test_validate_invalid_yaml(self):
        """Test validation of invalid YAML."""
        from kicad_tools.spec import validate_spec

        with tempfile.NamedTemporaryFile(suffix=".kct", delete=False, mode="w") as f:
            f.write("invalid: yaml: content: [")
            path = Path(f.name)

        try:
            is_valid, errors = validate_spec(path)
            assert not is_valid
            assert len(errors) > 0
        finally:
            path.unlink()

    def test_create_minimal_spec_function(self):
        """Test create_minimal_spec helper."""
        from kicad_tools.spec import create_minimal_spec

        spec = create_minimal_spec("My Board", summary="Custom summary")

        assert spec.project.name == "My Board"
        assert spec.intent.summary == "Custom summary"
        assert spec.project.created == date.today()

    def test_get_template(self):
        """Test getting templates."""
        from kicad_tools.spec import get_template

        # Test minimal template
        content = get_template("minimal")
        assert "kct_version" in content
        assert "project:" in content

        # Test power supply template
        content = get_template("power_supply")
        assert "Power Supply" in content or "power" in content.lower()

        # Test invalid template
        with pytest.raises(ValueError):
            get_template("invalid_template")


class TestDecisions:
    """Tests for decision recording."""

    def test_add_decision(self):
        """Test adding a decision to spec."""
        from kicad_tools.spec import (
            Decision,
            ProjectMetadata,
            ProjectSpec,
        )
        from kicad_tools.spec.schema import DesignPhase

        spec = ProjectSpec(
            project=ProjectMetadata(name="Test"),
            decisions=[],
        )

        decision = Decision(
            date=date.today(),
            phase=DesignPhase.SCHEMATIC,
            topic="Regulator Selection",
            choice="LM7805",
            rationale="Common, cheap, adequate for requirements",
            alternatives=["TPS562201", "MP1584"],
        )

        spec.decisions.append(decision)

        assert len(spec.decisions) == 1
        assert spec.decisions[0].topic == "Regulator Selection"
        assert spec.decisions[0].choice == "LM7805"
        assert len(spec.decisions[0].alternatives) == 2

    def test_decision_optional_date_and_phase(self):
        """Test that date and phase fields are optional on Decision.

        Validates fix for issue #806: project.kct files should not require
        date and phase fields on decisions.
        """
        from kicad_tools.spec import Decision

        # Should not raise ValidationError even without date and phase
        decision = Decision(
            topic="Resistor value",
            choice="330 ohm",
            rationale="Standard E24 value closest to ideal",
        )

        assert decision.topic == "Resistor value"
        assert decision.choice == "330 ohm"
        assert decision.date is None
        assert decision.phase is None


class TestProgress:
    """Tests for progress tracking."""

    def test_phase_progress(self):
        """Test phase progress tracking."""
        from kicad_tools.spec import PhaseProgress, Progress
        from kicad_tools.spec.schema import PhaseStatus

        progress = Progress(
            phase="schematic",
            phases={
                "concept": PhaseProgress(
                    status=PhaseStatus.COMPLETED,
                    checklist=["[x] Define requirements"],
                ),
                "schematic": PhaseProgress(
                    status=PhaseStatus.IN_PROGRESS,
                    checklist=[
                        "[x] Power input",
                        "[ ] Buck converter",
                    ],
                ),
            },
        )

        assert progress.phases["concept"].status == PhaseStatus.COMPLETED
        assert progress.phases["schematic"].status == PhaseStatus.IN_PROGRESS

    def test_get_current_phase_progress(self):
        """Test getting current phase progress."""
        from kicad_tools.spec import (
            PhaseProgress,
            Progress,
            ProjectMetadata,
            ProjectSpec,
        )
        from kicad_tools.spec.schema import DesignPhase

        spec = ProjectSpec(
            project=ProjectMetadata(name="Test"),
            progress=Progress(
                phase=DesignPhase.LAYOUT,
                phases={
                    "layout": PhaseProgress(
                        checklist=["[ ] Placement", "[ ] Routing"],
                    ),
                },
            ),
        )

        current = spec.get_current_phase_progress()
        assert current is not None
        assert len(current.checklist) == 2


class TestCopperWeight:
    """Tests for copper_weight parsing on ManufacturingRequirements."""

    def test_copper_weight_int(self):
        """Test copper_weight accepts plain int (backward compat)."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight=2)
        assert mfg.copper_weight == 2.0

    def test_copper_weight_float(self):
        """Test copper_weight accepts plain float."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight=0.5)
        assert mfg.copper_weight == 0.5

    def test_copper_weight_string_with_oz(self):
        """Test copper_weight accepts '2oz' string."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="2oz")
        assert mfg.copper_weight == 2.0

    def test_copper_weight_fractional_string(self):
        """Test copper_weight accepts '0.5oz' string."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="0.5oz")
        assert mfg.copper_weight == 0.5

    def test_copper_weight_string_with_space(self):
        """Test copper_weight accepts '2 oz' (space-tolerant)."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="2 oz")
        assert mfg.copper_weight == 2.0

    def test_copper_weight_case_insensitive(self):
        """Test copper_weight accepts '2OZ' (case-insensitive)."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="2OZ")
        assert mfg.copper_weight == 2.0

    def test_copper_weight_bare_number_string(self):
        """Test copper_weight accepts bare number string '2'."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(copper_weight="2")
        assert mfg.copper_weight == 2.0

    def test_copper_weight_invalid_unit(self):
        """Test copper_weight rejects invalid unit like '2lb'."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import ManufacturingRequirements

        with pytest.raises(ValidationError, match="copper_weight"):
            ManufacturingRequirements(copper_weight="2lb")

    def test_copper_weight_invalid_string(self):
        """Test copper_weight rejects non-numeric strings."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import ManufacturingRequirements

        with pytest.raises(ValidationError, match="copper_weight"):
            ManufacturingRequirements(copper_weight="abc")

    def test_copper_weight_default_none(self):
        """Test copper_weight defaults to None."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements()
        assert mfg.copper_weight is None

    def test_copper_weight_extracted_from_layers(self):
        """Test copper_weight is promoted from layers dict to top-level field."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(layers={"count": 2, "copper_weight": "2oz"})
        assert mfg.copper_weight == 2.0
        assert "copper_weight" not in mfg.layers

    def test_layers_without_copper_weight(self):
        """Test layers dict without copper_weight still works."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(layers={"count": 2})
        assert mfg.layers == {"count": 2}
        assert mfg.copper_weight is None


class TestLeadProfile:
    """Verified maximum lead dimensions on a BOM entry (Issue #10299)."""

    def test_round_lead_required_diameter(self):
        """A round lead needs its maximum diameter."""
        from kicad_tools.spec.schema import LeadProfile

        lead = LeadProfile(shape="round", diameter_mm=1.0, source="calipers 2026-11-20")
        assert lead.required_diameter_mm() == pytest.approx(1.0)
        assert lead.max_dimensions_mm() == (1.0,)

    def test_rectangular_lead_required_diameter_is_diagonal(self):
        """A rectangular lead needs the diagonal of its maximum width/thickness."""
        from kicad_tools.spec.schema import LeadProfile

        lead = LeadProfile(
            shape="rectangular", width_mm=1.4, thickness_mm=0.8, source="datasheet p. 9"
        )
        # sqrt(1.4^2 + 0.8^2) = 1.6124515...
        assert lead.required_diameter_mm() == pytest.approx(1.61245155, rel=1e-6)
        assert lead.max_dimensions_mm() == (1.4, 0.8)

    def test_round_requires_diameter(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import LeadProfile

        with pytest.raises(ValidationError, match="requires diameter_mm"):
            LeadProfile(shape="round", source="datasheet")

    def test_rectangular_requires_width_and_thickness(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import LeadProfile

        with pytest.raises(ValidationError, match="width_mm and thickness_mm"):
            LeadProfile(shape="rectangular", width_mm=1.4, source="datasheet")
        with pytest.raises(ValidationError, match="width_mm and thickness_mm"):
            LeadProfile(shape="rectangular", thickness_mm=0.8, source="datasheet")

    def test_shape_dimension_mismatch_rejected(self):
        """Round takes diameter only; rectangular takes width/thickness only."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import LeadProfile

        with pytest.raises(ValidationError, match="diameter_mm only"):
            LeadProfile(shape="round", diameter_mm=1.0, width_mm=1.4, source="datasheet")
        with pytest.raises(ValidationError, match="width_mm and thickness_mm; "):
            LeadProfile(
                shape="rectangular", width_mm=1.4, thickness_mm=0.8, diameter_mm=1.0, source="s"
            )

    def test_blank_source_rejected(self):
        """Dimensions without provenance cannot be traced, so they are refused."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import LeadProfile

        with pytest.raises(ValidationError, match="require a source"):
            LeadProfile(shape="round", diameter_mm=1.0, source="   ")

    def test_dimensions_must_be_positive(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import LeadProfile

        with pytest.raises(ValidationError):
            LeadProfile(shape="round", diameter_mm=0, source="datasheet")
        with pytest.raises(ValidationError):
            LeadProfile(shape="rectangular", width_mm=-1.4, thickness_mm=0.8, source="datasheet")


class TestFinishedHoleTolerance:
    """Fabricator lower finished-hole tolerance (Issue #10299)."""

    def test_declared_with_source(self):
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(
            finished_hole_tolerance_mm=-0.08,
            finished_hole_tolerance_source="JLCPCB capabilities, through-hole tolerances",
        )
        assert mfg.finished_hole_tolerance_mm == -0.08
        assert mfg.finished_hole_tolerance_source == "JLCPCB capabilities, through-hole tolerances"

    def test_zero_is_allowed(self):
        """Zero (hole never smaller than nominal) is a valid lower tolerance."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(
            finished_hole_tolerance_mm=0.0, finished_hole_tolerance_source="fab quote"
        )
        assert mfg.finished_hole_tolerance_mm == 0.0

    def test_requires_source(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import ManufacturingRequirements

        with pytest.raises(ValidationError, match="finished_hole_tolerance_source"):
            ManufacturingRequirements(finished_hole_tolerance_mm=-0.08)

    def test_positive_rejected(self):
        """The sign convention is lower/negative; a positive value misunderstands it."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import ManufacturingRequirements

        with pytest.raises(ValidationError):
            ManufacturingRequirements(
                finished_hole_tolerance_mm=0.08, finished_hole_tolerance_source="misread"
            )

    def test_unset_by_default(self):
        from kicad_tools.spec.schema import ManufacturingRequirements

        assert ManufacturingRequirements().finished_hole_tolerance_mm is None
        assert ManufacturingRequirements().finished_hole_tolerance_source is None


class TestBOMEntryLeadDeclarations:
    """Lead declarations on bom_entries load from a .kct file (Issue #10299)."""

    def test_lead_and_pad_leads_parse(self, tmp_path):
        from kicad_tools.spec.parser import load_spec

        spec_file = tmp_path / "project.kct"
        spec_file.write_text(
            "kct_version: '1.0'\n"
            "project:\n"
            "  name: T\n"
            "bom_entries:\n"
            "  - ref: Q1-Q8\n"
            "    part: STW70N60DM2\n"
            "    lead:\n"
            "      shape: rectangular\n"
            "      width_mm: 1.4\n"
            "      thickness_mm: 0.8\n"
            "      source: STW70N60DM2 datasheet package drawing, pp. 9-10\n"
            "    pad_leads:\n"
            "      '2':\n"
            "        shape: round\n"
            "        diameter_mm: 1.0\n"
            "        source: measured with calipers 2026-11-20\n"
        )
        spec = load_spec(spec_file)
        assert spec.bom_entries is not None
        entry = spec.bom_entries[0]
        assert entry.lead is not None
        assert entry.lead.shape == "rectangular"
        assert entry.lead.width_mm == pytest.approx(1.4)
        assert entry.lead.thickness_mm == pytest.approx(0.8)
        assert entry.lead.source == "STW70N60DM2 datasheet package drawing, pp. 9-10"
        assert entry.pad_leads is not None
        assert entry.pad_leads["2"].shape == "round"
        assert entry.pad_leads["2"].diameter_mm == pytest.approx(1.0)

    def test_lead_declarations_round_trip(self, tmp_path):
        """save_spec -> load_spec preserves the lead declarations verbatim."""
        from kicad_tools.spec.parser import load_spec, save_spec
        from kicad_tools.spec.schema import BOMEntry, LeadProfile, ProjectMetadata, ProjectSpec

        spec = ProjectSpec(
            project=ProjectMetadata(name="T"),
            bom_entries=[
                BOMEntry(
                    ref="Q1",
                    part="STW70N60DM2",
                    lead=LeadProfile(
                        shape="rectangular", width_mm=1.4, thickness_mm=0.8, source="p. 9"
                    ),
                    pad_leads={"3": LeadProfile(shape="round", diameter_mm=1.0, source="calipers")},
                )
            ],
        )
        path = tmp_path / "project.kct"
        save_spec(spec, path)
        loaded = load_spec(path)
        assert loaded.bom_entries is not None
        entry = loaded.bom_entries[0]
        assert entry.lead == spec.bom_entries[0].lead
        assert entry.pad_leads == spec.bom_entries[0].pad_leads

    def test_tolerance_parses_from_spec(self, tmp_path):
        from kicad_tools.spec.parser import load_spec

        spec_file = tmp_path / "project.kct"
        spec_file.write_text(
            "kct_version: '1.0'\n"
            "project:\n"
            "  name: T\n"
            "requirements:\n"
            "  manufacturing:\n"
            "    finished_hole_tolerance_mm: -0.08\n"
            "    finished_hole_tolerance_source: JLCPCB capabilities\n"
        )
        spec = load_spec(spec_file)
        assert spec.requirements is not None
        assert spec.requirements.manufacturing is not None
        assert spec.requirements.manufacturing.finished_hole_tolerance_mm == pytest.approx(-0.08)


class TestFinishedHoleToleranceUpper:
    """Optional upper bound of the fabricator's finished-hole tolerance (Issue #10476)."""

    def test_declared_alongside_the_lower_tolerance(self):
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(
            finished_hole_tolerance_mm=-0.08,
            finished_hole_tolerance_upper_mm=0.13,
            finished_hole_tolerance_source="JLCPCB capabilities, through-hole tolerances",
        )
        assert mfg.finished_hole_tolerance_mm == -0.08
        assert mfg.finished_hole_tolerance_upper_mm == 0.13

    def test_may_be_declared_without_a_lower_tolerance(self):
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(
            finished_hole_tolerance_upper_mm=0.13,
            finished_hole_tolerance_source="fab quote 2026-11-25",
        )
        assert mfg.finished_hole_tolerance_mm is None
        assert mfg.finished_hole_tolerance_upper_mm == 0.13

    def test_zero_is_allowed(self):
        """Zero (hole never larger than nominal) is a valid upper tolerance."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements(
            finished_hole_tolerance_upper_mm=0.0, finished_hole_tolerance_source="fab quote"
        )
        assert mfg.finished_hole_tolerance_upper_mm == 0.0

    def test_requires_source(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import ManufacturingRequirements

        with pytest.raises(ValidationError, match="finished_hole_tolerance_source"):
            ManufacturingRequirements(finished_hole_tolerance_upper_mm=0.13)
        with pytest.raises(ValidationError, match="finished_hole_tolerance_source"):
            ManufacturingRequirements(
                finished_hole_tolerance_upper_mm=0.13, finished_hole_tolerance_source=" "
            )

    def test_negative_rejected(self):
        """The sign convention is upper/positive; a negative value belongs to the lower bound."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import ManufacturingRequirements

        with pytest.raises(ValidationError):
            ManufacturingRequirements(
                finished_hole_tolerance_upper_mm=-0.08, finished_hole_tolerance_source="misread"
            )

    def test_unset_by_default(self):
        from kicad_tools.spec.schema import BOMEntry, ManufacturingRequirements

        mfg = ManufacturingRequirements()
        assert mfg.finished_hole_tolerance_upper_mm is None
        assert mfg.finished_hole_guarantees is None
        assert BOMEntry(ref="C1", part="LGU2E471MELB").mounting_holes is None


class TestMountingHoleDeclaration:
    """A sourced mounting drawing on a BOM entry (Issue #10476)."""

    def test_pads_limits_and_source_parse(self):
        from kicad_tools.spec.schema import MountingHoleDeclaration

        declaration = MountingHoleDeclaration(
            pads=["1", "2"],
            hole_diameter={"min_mm": 1.90, "max_mm": 2.10},
            pitch={"min_mm": 9.90, "max_mm": 10.10},
            source="Nichicon LGU drawing, p. 1",
        )
        assert declaration.pads == ["1", "2"]
        assert declaration.hole_diameter.min_mm == pytest.approx(1.90)
        assert declaration.hole_diameter.max_mm == pytest.approx(2.10)
        assert declaration.pitch is not None
        assert declaration.pitch.min_mm == pytest.approx(9.90)
        assert declaration.pitch.max_mm == pytest.approx(10.10)
        assert declaration.source == "Nichicon LGU drawing, p. 1"

    def test_integer_pad_numbers_normalize(self):
        from kicad_tools.spec.schema import MountingHoleDeclaration

        declaration = MountingHoleDeclaration(
            pads=[1, 2], hole_diameter={"min_mm": 1.9, "max_mm": 2.1}, source="drawing"
        )
        assert declaration.pads == ["1", "2"]

    def test_requires_exactly_two_pads(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleDeclaration

        limits = {"min_mm": 1.9, "max_mm": 2.1}
        with pytest.raises(ValidationError):
            MountingHoleDeclaration(pads=["1"], hole_diameter=limits, source="drawing")
        with pytest.raises(ValidationError):
            MountingHoleDeclaration(pads=["1", "2", "3"], hole_diameter=limits, source="drawing")
        with pytest.raises(ValidationError):
            MountingHoleDeclaration(pads="1 2", hole_diameter=limits, source="drawing")

    def test_requires_two_distinct_pads(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleDeclaration

        with pytest.raises(ValidationError):
            MountingHoleDeclaration(
                pads=["1", "1"], hole_diameter={"min_mm": 1.9, "max_mm": 2.1}, source="drawing"
            )
        with pytest.raises(ValidationError):
            MountingHoleDeclaration(
                pads=["1", " "], hole_diameter={"min_mm": 1.9, "max_mm": 2.1}, source="drawing"
            )

    def test_requires_hole_diameter_limits(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleDeclaration

        with pytest.raises(ValidationError):
            MountingHoleDeclaration(pads=["1", "2"], source="drawing")

    def test_pitch_limits_are_optional(self):
        """A drawing without a pitch tolerance declares only the hole diameter."""
        from kicad_tools.spec.schema import MountingHoleDeclaration

        declaration = MountingHoleDeclaration(
            pads=["1", "2"], hole_diameter={"min_mm": 1.9, "max_mm": 2.1}, source="drawing"
        )
        assert declaration.pitch is None

    def test_requires_source(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleDeclaration

        limits = {"min_mm": 1.9, "max_mm": 2.1}
        with pytest.raises(ValidationError, match="source"):
            MountingHoleDeclaration(pads=["1", "2"], hole_diameter=limits)
        with pytest.raises(ValidationError, match="source"):
            MountingHoleDeclaration(pads=["1", "2"], hole_diameter=limits, source="   ")

    def test_limits_must_be_ordered(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleDeclaration

        with pytest.raises(ValidationError, match="min_mm <= max_mm"):
            MountingHoleDeclaration(
                pads=["1", "2"],
                hole_diameter={"min_mm": 2.1, "max_mm": 1.9},
                source="drawing",
            )
        with pytest.raises(ValidationError, match="min_mm <= max_mm"):
            MountingHoleDeclaration(
                pads=["1", "2"],
                hole_diameter={"min_mm": 1.9, "max_mm": 2.1},
                pitch={"min_mm": 10.1, "max_mm": 9.9},
                source="drawing",
            )

    def test_limits_must_be_positive(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleDeclaration

        with pytest.raises(ValidationError):
            MountingHoleDeclaration(
                pads=["1", "2"], hole_diameter={"min_mm": 0, "max_mm": 2.1}, source="drawing"
            )
        with pytest.raises(ValidationError):
            MountingHoleDeclaration(
                pads=["1", "2"],
                hole_diameter={"min_mm": 1.9, "max_mm": 2.1},
                pitch={"min_mm": 9.9, "max_mm": -10.1},
                source="drawing",
            )

    def test_parses_from_spec(self, tmp_path):
        from kicad_tools.spec.parser import load_spec

        spec_file = tmp_path / "project.kct"
        spec_file.write_text(
            "kct_version: '1.0'\n"
            "project:\n"
            "  name: T\n"
            "bom_entries:\n"
            "  - ref: C1-C2\n"
            "    part: LGU2E471MELB\n"
            "    mounting_holes:\n"
            "      pads: ['1', '2']\n"
            "      hole_diameter: {min_mm: 1.90, max_mm: 2.10}\n"
            "      pitch: {min_mm: 9.90, max_mm: 10.10}\n"
            "      source: Nichicon LGU drawing, p. 1\n"
        )
        spec = load_spec(spec_file)
        assert spec.bom_entries is not None
        declaration = spec.bom_entries[0].mounting_holes
        assert declaration is not None
        assert declaration.pads == ["1", "2"]
        assert declaration.hole_diameter.min_mm == pytest.approx(1.90)
        assert declaration.pitch is not None
        assert declaration.pitch.max_mm == pytest.approx(10.10)
        assert declaration.source == "Nichicon LGU drawing, p. 1"
        # A mounting declaration is independent of a lead profile.
        assert spec.bom_entries[0].lead is None

    def test_round_trip(self, tmp_path):
        """save_spec -> load_spec preserves the mounting declaration verbatim."""
        from kicad_tools.spec.parser import load_spec, save_spec
        from kicad_tools.spec.schema import (
            BOMEntry,
            MountingHoleDeclaration,
            ProjectMetadata,
            ProjectSpec,
        )

        spec = ProjectSpec(
            project=ProjectMetadata(name="T"),
            bom_entries=[
                BOMEntry(
                    ref="C1",
                    part="LGU2E471MELB",
                    mounting_holes=MountingHoleDeclaration(
                        pads=["1", "2"],
                        hole_diameter={"min_mm": 1.9, "max_mm": 2.1},
                        pitch={"min_mm": 9.9, "max_mm": 10.1},
                        source="Nichicon LGU drawing, p. 1",
                    ),
                )
            ],
        )
        path = tmp_path / "project.kct"
        save_spec(spec, path)
        loaded = load_spec(path)
        assert loaded.bom_entries is not None
        assert loaded.bom_entries[0].mounting_holes == spec.bom_entries[0].mounting_holes


class TestFinishedHoleGuarantee:
    """Order-specific finished-hole interval guaranteed for selected holes (Issue #10476)."""

    def test_declared(self):
        from kicad_tools.spec.schema import FinishedHoleGuarantee

        guarantee = FinishedHoleGuarantee(
            refs=["C1"],
            min_mm=1.90,
            max_mm=2.10,
            source="JLCPCB order 48210 engineering confirmation",
        )
        assert guarantee.refs == ["C1"]
        assert guarantee.min_mm == pytest.approx(1.90)
        assert guarantee.max_mm == pytest.approx(2.10)

    def test_single_reference_string_accepted(self):
        from kicad_tools.spec.schema import FinishedHoleGuarantee

        guarantee = FinishedHoleGuarantee(refs="C1", min_mm=1.9, max_mm=2.1, source="fab note")
        assert guarantee.refs == ["C1"]

    def test_requires_references(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import FinishedHoleGuarantee

        with pytest.raises(ValidationError):
            FinishedHoleGuarantee(refs=[], min_mm=1.9, max_mm=2.1, source="fab note")
        with pytest.raises(ValidationError):
            FinishedHoleGuarantee(refs=[""], min_mm=1.9, max_mm=2.1, source="fab note")

    def test_requires_source(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import FinishedHoleGuarantee

        with pytest.raises(ValidationError, match="source"):
            FinishedHoleGuarantee(refs=["C1"], min_mm=1.9, max_mm=2.1)
        with pytest.raises(ValidationError, match="source"):
            FinishedHoleGuarantee(refs=["C1"], min_mm=1.9, max_mm=2.1, source="  ")

    def test_interval_must_be_ordered_and_positive(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import FinishedHoleGuarantee

        with pytest.raises(ValidationError, match="min_mm <= max_mm"):
            FinishedHoleGuarantee(refs=["C1"], min_mm=2.1, max_mm=1.9, source="fab note")
        with pytest.raises(ValidationError):
            FinishedHoleGuarantee(refs=["C1"], min_mm=0, max_mm=2.1, source="fab note")

    def test_parses_from_spec(self, tmp_path):
        from kicad_tools.spec.parser import load_spec

        spec_file = tmp_path / "project.kct"
        spec_file.write_text(
            "kct_version: '1.0'\n"
            "project:\n"
            "  name: T\n"
            "requirements:\n"
            "  manufacturing:\n"
            "    finished_hole_tolerance_mm: -0.08\n"
            "    finished_hole_tolerance_upper_mm: 0.13\n"
            "    finished_hole_tolerance_source: JLCPCB capabilities, through-hole tolerances\n"
            "    finished_hole_guarantees:\n"
            "      - refs: [C1]\n"
            "        min_mm: 1.90\n"
            "        max_mm: 2.10\n"
            "        source: JLCPCB order 48210 engineering confirmation\n"
        )
        spec = load_spec(spec_file)
        assert spec.requirements is not None
        assert spec.requirements.manufacturing is not None
        mfg = spec.requirements.manufacturing
        assert mfg.finished_hole_tolerance_upper_mm == pytest.approx(0.13)
        assert mfg.finished_hole_guarantees is not None
        (guarantee,) = mfg.finished_hole_guarantees
        assert guarantee.refs == ["C1"]
        assert guarantee.min_mm == pytest.approx(1.90)
        assert guarantee.max_mm == pytest.approx(2.10)
        assert guarantee.source == "JLCPCB order 48210 engineering confirmation"


class TestWireTerminationDeclarations:
    """Soldered wire or ferrule landings declared without a component MPN (Issue #10473)."""

    def test_wire_terminations_parse_without_bom_entry(self, tmp_path):
        from kicad_tools.spec.parser import collect_unknown_keys, load_spec

        spec_file = tmp_path / "project.kct"
        spec_file.write_text(
            "kct_version: '1.0'\n"
            "project:\n"
            "  name: T\n"
            "wire_terminations:\n"
            "  - ref: J1\n"
            "    pad: 2\n"
            "    diameter_mm: 3.10\n"
            "    source: ferrule datasheet maximum barrel outside diameter\n"
        )
        spec = load_spec(spec_file)
        assert spec.bom_entries is None
        assert spec.wire_terminations is not None
        (wire,) = spec.wire_terminations
        assert wire.ref == "J1"
        assert wire.pad == "2"
        assert wire.diameter_mm == pytest.approx(3.10)
        assert wire.source == "ferrule datasheet maximum barrel outside diameter"
        assert collect_unknown_keys(spec) == []

    def test_declaration_without_diameter_stays_unverified(self):
        """A landing may be declared before its prepared end is measured."""
        from kicad_tools.spec.schema import WireTermination

        wire = WireTermination(ref="J1", pad="3")
        assert wire.diameter_mm is None
        assert wire.source is None

    def test_pad_accepts_integer_or_string(self):
        from kicad_tools.spec.schema import WireTermination

        wire = WireTermination(ref=" J1 ", pad=2)
        assert wire.ref == "J1"
        assert wire.pad == "2"
        assert WireTermination(ref="J1", pad="A1").pad == "A1"

    @pytest.mark.parametrize("pad", [None, True, 2.5, "", "  "])
    def test_invalid_pad_rejected(self, pad):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import WireTermination

        with pytest.raises(ValidationError, match="pad number"):
            WireTermination(ref="J1", pad=pad)

    @pytest.mark.parametrize("ref", ["", "  "])
    def test_blank_reference_rejected(self, ref):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import WireTermination

        with pytest.raises(ValidationError, match="reference designator"):
            WireTermination(ref=ref, pad="1")

    def test_diameter_requires_a_source(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import WireTermination

        with pytest.raises(ValidationError, match="source"):
            WireTermination(ref="J1", pad="1", diameter_mm=2.4)

    def test_blank_source_rejected_with_diameter(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import WireTermination

        with pytest.raises(ValidationError, match="source"):
            WireTermination(ref="J1", pad="1", diameter_mm=2.4, source="   ")

    @pytest.mark.parametrize("diameter", [0, -0.1, "2.4", True, float("nan"), float("inf")])
    def test_diameter_must_be_a_positive_finite_number(self, diameter):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import WireTermination

        with pytest.raises(ValidationError, match="diameter_mm"):
            WireTermination(ref="J1", pad="1", diameter_mm=diameter, source="drawing")

    def test_wire_terminations_round_trip(self, tmp_path):
        """save_spec -> load_spec preserves the declarations; no BOM entry appears."""
        from kicad_tools.spec.parser import load_spec, save_spec
        from kicad_tools.spec.schema import ProjectMetadata, ProjectSpec, WireTermination

        spec = ProjectSpec(
            project=ProjectMetadata(name="T"),
            wire_terminations=[
                WireTermination(ref="J1", pad=1, diameter_mm=2.4, source="calipers 2026-11-26"),
                WireTermination(ref="J1", pad="2"),
            ],
        )
        path = tmp_path / "project.kct"
        save_spec(spec, path)
        loaded = load_spec(path)
        assert loaded.bom_entries is None
        assert loaded.wire_terminations == spec.wire_terminations
        assert "bom_entries" not in path.read_text()


class TestSmdLandPattern:
    """Verified SMD land-pattern declarations on a BOM entry (Issue #10505)."""

    def test_outer_span_derives_pitch(self):
        from kicad_tools.spec.schema import SmdLandPattern

        pattern = SmdLandPattern(
            pad_shape="rect",
            pad_depth_mm=3.00,
            pad_width_mm=3.50,
            outer_span_mm=8.00,
            source="Stackpole RHC series, page 2",
        )
        assert pattern.resolved_pitch_mm() == pytest.approx(5.00)
        assert pattern.resolved_outer_span_mm() == pytest.approx(8.00)

    def test_pitch_derives_outer_span(self):
        from kicad_tools.spec.schema import SmdLandPattern

        pattern = SmdLandPattern(
            pad_shape="roundrect",
            pad_depth_mm=1.225,
            pad_width_mm=3.35,
            pitch_mm=5.925,
            source="KiCad R_2512_6332Metric",
        )
        assert pattern.resolved_pitch_mm() == pytest.approx(5.925)
        assert pattern.resolved_outer_span_mm() == pytest.approx(7.150)

    def test_both_pitch_and_outer_span_consistent(self):
        from kicad_tools.spec.schema import SmdLandPattern

        pattern = SmdLandPattern(
            pad_shape="rect",
            pad_depth_mm=3.00,
            pad_width_mm=3.50,
            pitch_mm=5.00,
            outer_span_mm=8.00,
            source="Stackpole RHC series, page 2",
        )
        assert pattern.resolved_pitch_mm() == pytest.approx(5.00)
        assert pattern.resolved_outer_span_mm() == pytest.approx(8.00)

    def test_inconsistent_pitch_and_outer_span_rejected(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import SmdLandPattern

        with pytest.raises(ValidationError, match="outer_span_mm"):
            SmdLandPattern(
                pad_shape="rect",
                pad_depth_mm=3.00,
                pad_width_mm=3.50,
                pitch_mm=5.00,
                outer_span_mm=8.50,
                source="Stackpole RHC series, page 2",
            )

    def test_missing_both_pitch_and_outer_span_rejected(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import SmdLandPattern

        with pytest.raises(ValidationError, match="pitch_mm"):
            SmdLandPattern(
                pad_shape="rect",
                pad_depth_mm=3.00,
                pad_width_mm=3.50,
                source="Stackpole RHC series, page 2",
            )

    def test_overlapping_pads_rejected(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import SmdLandPattern

        with pytest.raises(ValidationError, match="overlap"):
            SmdLandPattern(
                pad_shape="rect",
                pad_depth_mm=3.00,
                pad_width_mm=3.50,
                outer_span_mm=6.00,
                source="Stackpole RHC series, page 2",
            )

    def test_blank_or_missing_source_rejected(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import SmdLandPattern

        with pytest.raises(ValidationError, match="source"):
            SmdLandPattern(
                pad_shape="rect",
                pad_depth_mm=3.00,
                pad_width_mm=3.50,
                outer_span_mm=8.00,
                source="   ",
            )

    def test_non_positive_or_invalid_shape_rejected(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import SmdLandPattern

        with pytest.raises(ValidationError):
            SmdLandPattern(
                pad_shape="rect",
                pad_depth_mm=0.0,
                pad_width_mm=3.50,
                outer_span_mm=8.00,
                source="Stackpole RHC series, page 2",
            )
        with pytest.raises(ValidationError):
            SmdLandPattern(
                pad_shape="trapezoid",  # type: ignore[arg-type]
                pad_depth_mm=3.00,
                pad_width_mm=3.50,
                outer_span_mm=8.00,
                source="Stackpole RHC series, page 2",
            )

    def test_circle_with_unequal_axes_rejected(self):
        from pydantic import ValidationError

        from kicad_tools.spec.schema import SmdLandPattern

        with pytest.raises(ValidationError, match="circle"):
            SmdLandPattern(
                pad_shape="circle",
                pad_depth_mm=1.0,
                pad_width_mm=1.5,
                pitch_mm=3.0,
                source="drawing, p. 1",
            )

    def test_land_pattern_parses_and_round_trips(self, tmp_path):
        from kicad_tools.spec.parser import load_spec, save_spec
        from kicad_tools.spec.schema import (
            BOMEntry,
            ProjectMetadata,
            ProjectSpec,
            SmdLandPattern,
        )

        spec = ProjectSpec(
            project=ProjectMetadata(name="T"),
            bom_entries=[
                BOMEntry(
                    ref="R40-R41",
                    part="RHC2512FT4K70",
                    land_pattern=SmdLandPattern(
                        pad_shape="rect",
                        pad_depth_mm=3.00,
                        pad_width_mm=3.50,
                        outer_span_mm=8.00,
                        source="Stackpole RHC series, page 2",
                    ),
                )
            ],
        )
        path = tmp_path / "project.kct"
        save_spec(spec, path)
        loaded = load_spec(path)
        assert loaded.bom_entries is not None
        entry = loaded.bom_entries[0]
        assert entry.land_pattern == spec.bom_entries[0].land_pattern


class TestPurchasingEntries:
    """Supplementary procurement rows declared in a .kct file (Issue #10431)."""

    # Synthetic purchasing identifiers only: nothing here names a real product.
    FUSE_ROWS = (
        "purchasing:\n"
        "  - description: Snap-on cover for the 5 x 20 mm fuse holder\n"
        "    quantity: 3\n"
        "    refs: [F1, F2, F3]\n"
        "    manufacturer: Example Fuse Co\n"
        "    mpn: SYN-COVER-1\n"
        "  - description: 5 x 20 mm cartridge, AC\n"
        "    quantity: 1\n"
        "    refs: [F1]\n"
        "    manufacturer: Example Fuse Co\n"
        "    mpn: SYN-CART-AC\n"
        "  - description: 5 x 20 mm cartridge, DC\n"
        "    quantity: 2\n"
        "    refs: [F2, F3]\n"
        "    pending: DC rating and breaking capacity still to be chosen\n"
        "  - description: Chassis ballast resistor, wire wound\n"
        "    quantity: 2\n"
        "    refs: [R9, R10]\n"
        "    manufacturer: Example Resistor Co\n"
        "    mpn: SYN-BALLAST-1\n"
    )

    def _load(self, tmp_path, rows):
        from kicad_tools.spec.parser import load_spec

        path = tmp_path / "project.kct"
        path.write_text(f"kct_version: '1.0'\nproject:\n  name: T\n{rows}")
        return load_spec(path)

    def test_unset_by_default(self):
        from kicad_tools.spec.schema import ProjectMetadata, ProjectSpec

        assert ProjectSpec(project=ProjectMetadata(name="T")).purchasing is None

    def test_fuse_example_parses(self, tmp_path):
        cover, ac, dc, ballast = self._load(tmp_path, self.FUSE_ROWS).purchasing

        assert cover.description == "Snap-on cover for the 5 x 20 mm fuse holder"
        assert cover.quantity == 3
        assert cover.refs == ["F1", "F2", "F3"]
        assert (cover.manufacturer, cover.mpn, cover.pending) == (
            "Example Fuse Co",
            "SYN-COVER-1",
            None,
        )
        assert (ac.refs, ac.quantity, ac.mpn) == (["F1"], 1, "SYN-CART-AC")
        # A pending row keeps its quantity and carries no part number at all.
        assert (dc.quantity, dc.refs, dc.pending) == (
            2,
            ["F2", "F3"],
            "DC rating and breaking capacity still to be chosen",
        )
        assert dc.manufacturer is None and dc.mpn is None
        # A chassis item links to wire lands; another row may omit the board link.
        assert (ballast.refs, ballast.mpn) == (["R9", "R10"], "SYN-BALLAST-1")

    def test_row_without_board_reference_and_empty_list(self, tmp_path):
        rows = (
            "purchasing:\n"
            "  - description: Chassis ballast resistor\n"
            "    quantity: 1\n"
            "    manufacturer: Example Resistor Co\n"
            "    mpn: SYN-BALLAST-1\n"
        )
        only = self._load(tmp_path, rows).purchasing
        assert only[0].refs is None
        assert self._load(tmp_path, "purchasing: []\n").purchasing == []

    @pytest.mark.parametrize("exclude_none", [True, False])
    def test_round_trip(self, tmp_path, exclude_none):
        from kicad_tools.spec.parser import load_spec, save_spec

        path = tmp_path / "project.kct"
        spec = self._load(tmp_path, self.FUSE_ROWS)
        save_spec(spec, path, exclude_none=exclude_none)
        assert load_spec(path).purchasing == spec.purchasing

    def test_blank_surrounding_whitespace_is_trimmed(self, tmp_path):
        rows = (
            "purchasing:\n"
            "  - description: '  Cover  '\n"
            "    quantity: 1\n"
            "    refs: [' F1 ', '']\n"
            "    pending: undecided\n"
        )
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="non-blank designators"):
            self._load(tmp_path, rows)
        rows = rows.replace("' F1 ', ''", "' F1 '")
        (entry,) = self._load(tmp_path, rows).purchasing
        assert (entry.description, entry.refs) == ("Cover", ["F1"])

    @pytest.mark.parametrize(
        ("rows", "message"),
        [
            (("    quantity: 0\n"), "positive whole number"),
            (("    quantity: -2\n"), "positive whole number"),
            (("    quantity: 1.5\n"), "valid integer"),
            (("    quantity: true\n"), "positive whole number"),
            (("    description: '   '\n    quantity: 1\n"), "non-blank description"),
            (("    quantity: 1\n    manufacturer: Example\n"), "needs both manufacturer and mpn"),
            (("    quantity: 1\n    mpn: SYN-1\n"), "needs both manufacturer and mpn"),
            (("    quantity: 1\n"), "needs both manufacturer and mpn"),
            (
                ("    quantity: 1\n    manufacturer: Example\n    mpn: '  '\n"),
                "non-blank",
            ),
            (
                (
                    "    quantity: 1\n    manufacturer: Example\n    mpn: SYN-1\n"
                    "    pending: undecided\n"
                ),
                "must not set manufacturer or mpn",
            ),
            (("    quantity: 1\n    pending: '  '\n"), "pending must be non-blank"),
            (("    quantity: 1\n    pending:\n"), "pending must say"),
            (
                ("    quantity: 1\n    refs: [F1, F1]\n    pending: undecided\n"),
                "repeat a designator",
            ),
            (
                ("    quantity: 1\n    refs: ['']\n    pending: undecided\n"),
                "non-blank designators",
            ),
            (("    quantity: 1\n    refs: F1\n    pending: undecided\n"), "valid list"),
            (("    quantity: 1\n    mpn: 12345\n    pending: undecided\n"), "valid string"),
        ],
        ids=[
            "zero-quantity",
            "negative-quantity",
            "fractional-quantity",
            "boolean-quantity",
            "blank-description",
            "manufacturer-without-mpn",
            "mpn-without-manufacturer",
            "neither-selected-nor-pending",
            "blank-mpn",
            "pending-with-part-number",
            "blank-pending",
            "bare-pending",
            "repeated-designator",
            "blank-designator",
            "designators-not-a-list",
            "numeric-mpn-needs-quoting",
        ],
    )
    def test_invalid_row_is_refused_with_its_input_error(self, tmp_path, rows, message):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match=message) as refused:
            self._load(tmp_path, f"purchasing:\n  - description: Cover\n{rows}")
        assert "purchasing.0" in str(refused.value)

    def test_unquoted_part_number_losing_leading_zeros_is_refused(self, tmp_path):
        """``0031.8221`` unquoted is a YAML float; it must never become ``31.8221``."""
        from pydantic import ValidationError

        rows = "purchasing:\n  - description: Holder\n    quantity: 1\n    mpn: 0031.8221\n"
        with pytest.raises(ValidationError, match="valid string"):
            self._load(tmp_path, rows)
        rows = rows.replace("mpn: 0031.8221", "manufacturer: Example\n    mpn: '0031.8221'")
        assert self._load(tmp_path, rows).purchasing[0].mpn == "0031.8221"

    def test_rows_may_share_a_designator(self, tmp_path):
        rows = (
            "purchasing:\n"
            "  - description: Cover\n"
            "    quantity: 1\n"
            "    refs: [F1]\n"
            "    manufacturer: Example\n"
            "    mpn: SYN-COVER-1\n"
            "  - description: Cartridge\n"
            "    quantity: 1\n"
            "    refs: [F1]\n"
            "    manufacturer: Example\n"
            "    mpn: SYN-CART-AC\n"
        )
        assert [row.refs for row in self._load(tmp_path, rows).purchasing] == [["F1"], ["F1"]]

    SWITCH_ROW = (
        "purchasing:\n"
        "  - description: Panel toggle switch\n"
        "    quantity: 1\n"
        "    refs: [SW1]\n"
        "    manufacturer: Example Switch Co\n"
        "    mpn: SYN-TOGGLE-1\n"
    )
    TERMINAL_MAP = (
        "    terminals:\n"
        "      - terminal: 3\n"
        "        ref: SW1\n"
        "        pad: 1\n"
        "        net: VDD_CTRL\n"
        "      - terminal: 2\n"
        "        ref: SW1\n"
        "        pad: 2\n"
        "        net: ARMED_3V3\n"
        "      - terminal: 1\n"
        "        unused: true\n"
    )

    def test_terminal_map_parses_and_round_trips(self, tmp_path):
        """A panel switch (Issue #10514) wired to a two-pad landing unlike its terminals.

        A terminal or pad number may be a bare integer, and an unused terminal
        is a declaration with no landing.
        """
        from kicad_tools.spec.parser import load_spec, save_spec

        (switch,) = self._load(tmp_path, self.SWITCH_ROW + self.TERMINAL_MAP).purchasing
        arm, common, spare = switch.terminals
        assert (arm.terminal, arm.ref, arm.pad, arm.net, arm.unused) == (
            "3",
            "SW1",
            "1",
            "VDD_CTRL",
            False,
        )
        assert (common.terminal, common.ref, common.pad, common.net) == (
            "2",
            "SW1",
            "2",
            "ARMED_3V3",
        )
        assert (spare.terminal, spare.ref, spare.pad, spare.net, spare.unused) == (
            "1",
            None,
            None,
            None,
            True,
        )
        assert arm.landing == ("SW1", "1", "VDD_CTRL") and spare.landing is None

        path = tmp_path / "project.kct"
        for exclude_none in (True, False):
            spec = self._load(tmp_path, self.SWITCH_ROW + self.TERMINAL_MAP)
            save_spec(spec, path, exclude_none=exclude_none)
            assert load_spec(path).purchasing == [switch]

    @pytest.mark.parametrize(
        ("terminals", "message"),
        [
            (
                "    terminals:\n"
                "      - {terminal: 2, ref: SW1, pad: 1, net: VDD_CTRL}\n"
                "      - {terminal: 2, ref: SW1, pad: 2, net: ARMED_3V3}\n",
                "terminal '2' is declared more than once: SW1 pad 1 (VDD_CTRL) and "
                "SW1 pad 2 (ARMED_3V3)",
            ),
            (
                "    terminals:\n"
                "      - {terminal: 3, ref: SW1, pad: 1, net: VDD_CTRL}\n"
                "      - {terminal: 3, unused: true}\n",
                "terminal '3' is declared more than once: SW1 pad 1 (VDD_CTRL) and unused",
            ),
            (
                "    terminals:\n"
                "      - {terminal: 3, ref: SW1, pad: 1, net: VDD_CTRL, unused: true}\n",
                "terminal '3' is both mapped to a landing and unused",
            ),
            ("    terminals:\n      - {terminal: 3, ref: SW1, pad: 1}\n", "needs ref, pad and net"),
            ("    terminals:\n      - {terminal: 3, unused: false}\n", "needs ref, pad and net"),
            ("    terminals:\n      - {ref: SW1, pad: 1, net: VDD_CTRL}\n", "Field required"),
            (
                "    terminals:\n      - {terminal: '  ', ref: SW1, pad: 1, net: VDD_CTRL}\n",
                "terminal must be non-blank",
            ),
            (
                "    terminals:\n      - {terminal: 3, ref: SW1, pad: '', net: VDD_CTRL}\n",
                "pad must be",
            ),
            (
                "    terminals:\n      - {terminal: 3, ref: SW1, pad: 1.5, net: VDD_CTRL}\n",
                "valid string",
            ),
            (
                "    terminals:\n      - {terminal: true, ref: SW1, pad: 1, net: VDD_CTRL}\n",
                "valid string",
            ),
            ("    terminals: []\n", "at least one terminal"),
        ],
        ids=[
            "one-terminal-two-landings",
            "mapped-and-unused",
            "mapped-and-unused-in-one-entry",
            "landing-without-net",
            "neither-mapped-nor-unused",
            "no-terminal-number",
            "blank-terminal",
            "blank-pad",
            "fractional-pad",
            "boolean-terminal",
            "empty-map",
        ],
    )
    def test_terminal_map_that_contradicts_itself_is_refused(self, tmp_path, terminals, message):
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as refused:
            self._load(tmp_path, self.SWITCH_ROW + terminals)

        assert "purchasing.0.terminals" in str(refused.value)
        assert message in str(refused.value)

    @pytest.mark.parametrize(
        ("refs", "landing"),
        [
            ("[SW1]", "R1"),
            ("[ SW1 ]", "SW9"),
            ("[R2]", "R1"),
        ],
        ids=["unrelated-reference", "whitespace-normalized-list", "other-footprint"],
    )
    def test_terminal_landing_outside_the_rows_refs_is_refused(self, tmp_path, refs, landing):
        """A mapped ``ref`` must be a designator the row declares (Issue #10582)."""
        from pydantic import ValidationError

        row = self.SWITCH_ROW.replace("refs: [SW1]", f"refs: {refs}")
        terminals = (
            f"    terminals:\n      - {{terminal: 3, ref: {landing}, pad: 1, net: VDD_CTRL}}\n"
        )
        with pytest.raises(ValidationError) as refused:
            self._load(tmp_path, row + terminals)

        message = str(refused.value)
        declared = ", ".join(r.strip(" []") for r in [refs])
        assert "purchasing.0" in message
        assert (
            f"terminal '3' maps to {landing}, which is not among the row's declared refs" in message
        )
        assert f"({declared})" in message
        assert f"add {landing} to refs" in message

    def test_terminals_may_land_on_any_declared_reference(self, tmp_path):
        """Every wired footprint is listed once; extra refs and unused terminals need no landing."""
        rows = (
            "purchasing:\n"
            "  - description: Panel switch bridging two landings\n"
            "    quantity: 1\n"
            "    refs: [SW1, SW2, J9]\n"
            "    manufacturer: Example Switch Co\n"
            "    mpn: SYN-TOGGLE-1\n"
            "    terminals:\n"
            "      - {terminal: 3, ref: SW1, pad: 1, net: VDD_CTRL}\n"
            "      - {terminal: 2, ref: SW2, pad: 1, net: ARMED_3V3}\n"
            "      - {terminal: 1, unused: true}\n"
        )
        (switch,) = self._load(tmp_path, rows).purchasing
        assert switch.refs == ["SW1", "SW2", "J9"]
        assert [a.landing for a in switch.terminals] == [
            ("SW1", "1", "VDD_CTRL"),
            ("SW2", "1", "ARMED_3V3"),
            None,
        ]

        only_unused = rows.split("    terminals:\n")[0] + (
            "    terminals:\n      - {terminal: 1, unused: true}\n"
        ).replace("[SW1, SW2, J9]", "[SW1]")
        assert self._load(tmp_path, only_unused).purchasing[0].terminals[0].unused

    @pytest.mark.parametrize("refs", ["    refs: []\n", ""], ids=["empty-list", "omitted"])
    def test_undeclared_refs_leave_the_terminal_map_to_the_board_check(self, tmp_path, refs):
        row = self.SWITCH_ROW.replace("    refs: [SW1]\n", refs)
        (switch,) = self._load(tmp_path, row + self.TERMINAL_MAP).purchasing
        assert not switch.refs
        assert switch.terminals[0].ref == "SW1"

    def test_row_without_a_terminal_map_still_loads_with_any_refs(self, tmp_path):
        (switch,) = self._load(tmp_path, self.SWITCH_ROW).purchasing
        assert switch.refs == ["SW1"] and switch.terminals is None

    def test_pending_row_must_not_declare_a_terminal_map(self, tmp_path):
        from pydantic import ValidationError

        pending = self.SWITCH_ROW.replace(
            "    manufacturer: Example Switch Co\n    mpn: SYN-TOGGLE-1\n",
            "    pending: the switch is not chosen yet\n",
        )
        with pytest.raises(ValidationError) as refused:
            self._load(tmp_path, pending + self.TERMINAL_MAP)

        assert "a pending row must not declare terminals" in str(refused.value)
        assert "purchasing.0" in str(refused.value)


class TestMountingHoleGroupSpec:
    """Tests for the MountingHoleGroupSpec schema (Issue #3352, P_AS1)."""

    def test_basic_construction(self):
        """MountingHoleGroupSpec accepts holes + anchor."""
        from kicad_tools.spec.schema import MountingHoleGroupSpec

        spec = MountingHoleGroupSpec(
            holes=[(0.0, 0.0), (10.0, 0.0)],
            anchor=(5.0, 5.0),
        )
        assert spec.holes == [(0.0, 0.0), (10.0, 0.0)]
        assert spec.anchor == (5.0, 5.0)
        assert spec.hole_diameter_mm == 3.2  # default
        assert spec.keepout_radius_mm == 5.0  # default

    def test_empty_holes_rejected(self):
        """An empty holes list raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleGroupSpec

        with pytest.raises(ValidationError, match="at least one hole"):
            MountingHoleGroupSpec(holes=[], anchor=(0.0, 0.0))

    def test_negative_dimensions_rejected(self):
        """Zero or negative hole_diameter/keepout raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import MountingHoleGroupSpec

        with pytest.raises(ValidationError, match="must be positive"):
            MountingHoleGroupSpec(
                holes=[(0.0, 0.0)],
                anchor=(0.0, 0.0),
                hole_diameter_mm=0.0,
            )
        with pytest.raises(ValidationError, match="must be positive"):
            MountingHoleGroupSpec(
                holes=[(0.0, 0.0)],
                anchor=(0.0, 0.0),
                keepout_radius_mm=-1.0,
            )

    def test_overrides_accepted(self):
        """Construction with explicit hole_diameter / keepout_radius works."""
        from kicad_tools.spec.schema import MountingHoleGroupSpec

        spec = MountingHoleGroupSpec(
            holes=[(0.0, 0.0)],
            anchor=(0.0, 0.0),
            hole_diameter_mm=2.5,
            keepout_radius_mm=3.0,
        )
        assert spec.hole_diameter_mm == 2.5
        assert spec.keepout_radius_mm == 3.0


class TestMechanicalRequirementsEnvelopeHard:
    """Tests for the envelope_hard field on MechanicalRequirements (Issue #3352)."""

    def test_envelope_hard_defaults_false(self):
        """envelope_hard defaults to False (back-compat with existing recipes)."""
        from kicad_tools.spec.schema import MechanicalRequirements

        mech = MechanicalRequirements()
        assert mech.envelope_hard is False

    def test_envelope_hard_true_when_set(self):
        """envelope_hard accepts True."""
        from kicad_tools.spec.schema import MechanicalRequirements

        mech = MechanicalRequirements(envelope_hard=True)
        assert mech.envelope_hard is True

    def test_mounting_hole_group_defaults_none(self):
        """mounting_hole_group defaults to None (back-compat)."""
        from kicad_tools.spec.schema import MechanicalRequirements

        mech = MechanicalRequirements()
        assert mech.mounting_hole_group is None

    def test_mounting_hole_group_attached(self):
        """mounting_hole_group field accepts a MountingHoleGroupSpec."""
        from kicad_tools.spec.schema import (
            MechanicalRequirements,
            MountingHoleGroupSpec,
        )

        group = MountingHoleGroupSpec(
            holes=[(0.0, 0.0), (140.0, 0.0), (0.0, 90.0), (140.0, 90.0)],
            anchor=(5.0, 5.0),
        )
        mech = MechanicalRequirements(mounting_hole_group=group)
        assert mech.mounting_hole_group is group


class TestEscalationPolicy:
    """Tests for the EscalationPolicy schema (Issue #3352, P_AS1)."""

    def test_defaults(self):
        """All EscalationPolicy fields have sensible defaults."""
        from kicad_tools.spec.schema import EscalationPolicy

        policy = EscalationPolicy()
        assert policy.ladder == "layers-first"
        assert policy.max_layers == 4
        assert policy.max_size_tier is None
        assert policy.density_threshold_viols_per_cm2 == 0.5

    def test_all_ladder_values_accepted(self):
        """The five documented ladder values all parse."""
        from kicad_tools.spec.schema import EscalationPolicy

        for value in [
            "layers-first",
            "size-first",
            "layers-only",
            "size-only",
            "none",
        ]:
            policy = EscalationPolicy(ladder=value)
            assert policy.ladder == value

    def test_invalid_ladder_rejected(self):
        """An unknown ladder value raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError):
            EscalationPolicy(ladder="bogus")

    def test_max_layers_must_be_positive(self):
        """max_layers < 1 raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError, match="max_layers must be >= 1"):
            EscalationPolicy(max_layers=0)

    def test_max_size_tier_negative_rejected(self):
        """A negative max_size_tier raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError, match="max_size_tier must be >= 0"):
            EscalationPolicy(max_size_tier=-1)

    def test_density_threshold_negative_rejected(self):
        """A non-positive density threshold raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(
            ValidationError, match="density_threshold_viols_per_cm2 must be positive"
        ):
            EscalationPolicy(density_threshold_viols_per_cm2=0.0)

    def test_attached_to_manufacturing_requirements(self):
        """escalation field on ManufacturingRequirements accepts an EscalationPolicy."""
        from kicad_tools.spec.schema import (
            EscalationPolicy,
            ManufacturingRequirements,
        )

        policy = EscalationPolicy(ladder="size-first", max_layers=6)
        mfg = ManufacturingRequirements(escalation=policy)
        assert mfg.escalation is policy
        assert mfg.escalation.ladder == "size-first"
        assert mfg.escalation.max_layers == 6

    def test_escalation_defaults_none(self):
        """escalation defaults to None on ManufacturingRequirements (back-compat)."""
        from kicad_tools.spec.schema import ManufacturingRequirements

        mfg = ManufacturingRequirements()
        assert mfg.escalation is None

    # Issue #3400: starting_layers field tests.
    def test_starting_layers_default_is_2(self):
        """starting_layers defaults to 2 (historical behaviour preserved)."""
        from kicad_tools.spec.schema import EscalationPolicy

        policy = EscalationPolicy()
        assert policy.starting_layers == 2

    def test_starting_layers_accepts_2_4_6(self):
        """starting_layers accepts the documented values {2, 4, 6}."""
        from kicad_tools.spec.schema import EscalationPolicy

        # 2 with default max_layers=4: trivially valid.
        assert EscalationPolicy(starting_layers=2).starting_layers == 2
        # 4 with default max_layers=4: equal, valid.
        assert EscalationPolicy(starting_layers=4).starting_layers == 4
        # 6 requires bumping max_layers (cross-field constraint).
        assert EscalationPolicy(starting_layers=6, max_layers=6).starting_layers == 6

    def test_starting_layers_rejects_below_2(self):
        """starting_layers < 2 raises ValidationError (Field ge=2 constraint)."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError):
            EscalationPolicy(starting_layers=1)
        with pytest.raises(ValidationError):
            EscalationPolicy(starting_layers=0)

    def test_starting_layers_rejects_above_6(self):
        """starting_layers > 6 raises ValidationError (Field le=6 constraint)."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        with pytest.raises(ValidationError):
            EscalationPolicy(starting_layers=8, max_layers=8)

    def test_starting_layers_must_not_exceed_max_layers(self):
        """Issue #3400: starting_layers > max_layers raises ValidationError."""
        from pydantic import ValidationError

        from kicad_tools.spec.schema import EscalationPolicy

        # Default max_layers is 4, so starting_layers=6 violates the
        # cross-field constraint.
        with pytest.raises(ValidationError, match="starting_layers"):
            EscalationPolicy(starting_layers=6, max_layers=4)
        # Equal is fine (the rung is reachable).
        EscalationPolicy(starting_layers=4, max_layers=4)
        # Strictly below is fine.
        EscalationPolicy(starting_layers=2, max_layers=4)

    def test_starting_layers_parses_from_yaml(self, tmp_path):
        """A project.kct with starting_layers parses through load_spec."""
        from kicad_tools.spec.parser import load_spec

        spec_text = (
            "project:\n"
            "  name: 'starting_layers smoke'\n"
            "  description: 'Issue #3400 smoke test'\n"
            "requirements:\n"
            "  manufacturing:\n"
            "    escalation:\n"
            "      ladder: layers-first\n"
            "      starting_layers: 4\n"
            "      max_layers: 6\n"
        )
        path = tmp_path / "project.kct"
        path.write_text(spec_text)

        spec = load_spec(path)
        assert spec.requirements.manufacturing.escalation.starting_layers == 4
        assert spec.requirements.manufacturing.escalation.max_layers == 6


class TestBackCompatExistingBoards:
    """Smoke test: existing project.kct files parse cleanly without the new fields."""

    def test_softstart_loads(self):
        """The canonical softstart project.kct parses cleanly through the schema.

        ``boards/external/softstart`` is a symlink into the canonical
        ``rjwalters/softstart`` repo (local-only, like chorus-test-revA);
        ``project.kct`` is the real product spec.  This is a back-compat
        smoke test that a real-world spec file loads end-to-end -- it skips
        when the external repo is not checked out (the symlink dangles).
        """
        from pathlib import Path

        from kicad_tools.spec.parser import load_spec

        path = (
            Path(__file__).resolve().parent.parent
            / "boards"
            / "external"
            / "softstart"
            / "project.kct"
        )
        if not path.exists():
            pytest.skip(f"softstart project.kct not found at {path}")
        spec = load_spec(path)
        # The spec parses and carries the rev B chassis dimensions.
        assert spec.requirements.mechanical.dimensions == {
            "width": "150mm",
            "height": "100mm",
        }

    @pytest.mark.parametrize(
        "board_dir",
        [
            "00-simple-led",
            "01-voltage-divider",
            "02-charlieplex-led",
            "03-usb-joystick",
            "04-stm32-devboard",
            "05-bldc-motor-controller",
            "06-diffpair-test",
            "07-matchgroup-test",
        ],
    )
    def test_board_loads(self, board_dir: str):
        """boards/<dir>/project.kct loads without error (back-compat smoke)."""
        from pathlib import Path

        from kicad_tools.spec.parser import load_spec

        path = Path(__file__).resolve().parent.parent / "boards" / board_dir / "project.kct"
        if not path.exists():
            pytest.skip(f"project.kct not found at {path}")
        spec = load_spec(path)
        # Spec object is well-formed
        assert spec.project is not None


# ---------------------------------------------------------------------------
# Round-trip fidelity / append-only decision log (issue #4538)
# ---------------------------------------------------------------------------

_BOARD_DIRS = [
    "00-simple-led",
    "01-voltage-divider",
    "02-charlieplex-led",
    "03-usb-joystick",
    "04-stm32-devboard",
    "05-bldc-motor-controller",
    "06-diffpair-test",
    "07-matchgroup-test",
]


def _repo_board(board_dir: str) -> Path:
    return Path(__file__).resolve().parent.parent / "boards" / board_dir / "project.kct"


def _leaf_values(obj):
    """Yield every scalar leaf of a parsed YAML tree, as strings."""
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _leaf_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _leaf_values(value)
    else:
        yield str(obj)


class TestSpecRoundTripFidelity:
    """A writer must never silently drop content it does not understand.

    ``.kct`` files are human-authored and rewritten in place. Before #4538 the
    spec models used pydantic's default ``extra="ignore"``, so a single
    ``kct spec decide`` destroyed every key the schema did not define --
    ``requirements.components``, ``progress.checklist``, and the unit-bearing
    ``requirements.electrical`` / ``requirements.mechanical`` keys all vanished
    with a zero exit code and no warning.
    """

    def test_unknown_top_level_key_survives_save(self, tmp_path):
        from kicad_tools.spec import load_spec, save_spec

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\nproject:\n  name: "T"\ncustom_top_level:\n  nested: value\n',
            encoding="utf-8",
        )

        spec = load_spec(path)
        save_spec(spec, path)

        reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert reloaded["custom_top_level"] == {"nested": "value"}

    def test_explicitly_null_unknown_key_survives_save(self, tmp_path):
        """``exclude_none`` must prune unset schema fields, never unknown keys."""
        from kicad_tools.spec import load_spec, save_spec

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "mystery_key:\n"
            "requirements:\n"
            "  electrical:\n"
            "    weird:\n"
            "    keep: 5V\n",
            encoding="utf-8",
        )

        save_spec(load_spec(path), path)

        reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "mystery_key" in reloaded
        assert "weird" in reloaded["requirements"]["electrical"]
        assert reloaded["requirements"]["electrical"]["keep"] == "5V"

    def test_unknown_nested_key_survives_save(self, tmp_path):
        from kicad_tools.spec import load_spec, save_spec

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "requirements:\n"
            "  electrical:\n"
            "    input_voltage: 5V\n"
            "  components:\n"
            "    - id: R1\n"
            "      value: 330 ohm\n"
            "progress:\n"
            "  phase: concept\n"
            "  checklist:\n"
            '    - "[x] Schematic"\n',
            encoding="utf-8",
        )

        spec = load_spec(path)
        save_spec(spec, path)

        reloaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert reloaded["requirements"]["electrical"]["input_voltage"] == "5V"
        assert reloaded["requirements"]["components"] == [{"id": "R1", "value": "330 ohm"}]
        assert reloaded["progress"]["checklist"] == ["[x] Schematic"]

    @pytest.mark.parametrize("board_dir", _BOARD_DIRS)
    def test_board_round_trip_loses_no_data(self, board_dir: str, tmp_path):
        """load_spec -> save_spec with no mutation loses no leaf value.

        The single tolerated difference is ``copper_weight``, which a
        pre-existing field validator canonicalizes from ``"2oz"`` to the float
        ``2.0``. That is a documented unit parse, not a dropped key.
        """
        from kicad_tools.spec import load_spec, save_spec

        source = _repo_board(board_dir)
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        before = yaml.safe_load(source.read_text(encoding="utf-8"))
        out = tmp_path / "rt.kct"
        save_spec(load_spec(source), out)
        after = yaml.safe_load(out.read_text(encoding="utf-8"))

        tolerated = set()
        weight = (
            (before.get("requirements") or {}).get("manufacturing", {}).get("copper_weight")
            if isinstance(before.get("requirements"), dict)
            else None
        )
        if weight is not None:
            tolerated.add(str(weight))

        missing = Counter(_leaf_values(before)) - Counter(_leaf_values(after))
        assert not (set(missing) - tolerated), f"{board_dir}: lost leaf values {dict(missing)}"

    def test_collect_unknown_keys_reports_schema_drift(self):
        """Unknown keys are preserved, but they must not be invisible."""
        from kicad_tools.spec import collect_unknown_keys, load_spec

        source = _repo_board("00-simple-led")
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        unknown = collect_unknown_keys(load_spec(source))

        assert "requirements.components" in unknown
        assert "progress.checklist" in unknown
        assert "requirements.electrical.input_voltage" in unknown
        assert "requirements.mechanical.board_width" not in unknown
        assert "requirements.mechanical.layers" not in unknown

    def test_validate_spec_detailed_warns_without_erroring(self):
        from kicad_tools.spec import validate_spec_detailed

        source = _repo_board("00-simple-led")
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        is_valid, errors, warnings = validate_spec_detailed(source)

        assert is_valid, errors
        assert errors == []
        assert any("requirements.components" in w for w in warnings)

    @pytest.mark.parametrize(
        "template_name", ["minimal", "power_supply", "sensor_board", "mcu_breakout"]
    )
    def test_template_parses_and_round_trips(self, template_name: str, tmp_path):
        from kicad_tools.spec import get_template, load_spec, save_spec

        content = get_template(template_name).format(date=date.today().isoformat())
        path = tmp_path / "t.kct"
        path.write_text(content, encoding="utf-8")

        before = yaml.safe_load(content)
        assert isinstance(before, dict), f"{template_name} template is not a YAML mapping"

        save_spec(load_spec(path), path)
        after = yaml.safe_load(path.read_text(encoding="utf-8"))

        missing = Counter(_leaf_values(before)) - Counter(_leaf_values(after))
        assert not missing, f"{template_name}: lost leaf values {dict(missing)}"

    @pytest.mark.parametrize(
        "template_name", ["minimal", "power_supply", "sensor_board", "mcu_breakout"]
    )
    def test_template_carries_decisions_stub(self, template_name: str):
        """Every scaffolded board starts with the design-memory log present."""
        from kicad_tools.spec import get_template

        content = get_template(template_name).format(date=date.today().isoformat())

        assert "\ndecisions: []\n" in content
        assert "kct spec decide" in content
        assert "append" in content.lower()


class TestAppendDecision:
    """``kct spec decide`` must be append-only, not a whole-file rewrite."""

    def _decision(self, topic: str = "PROBE"):
        from kicad_tools.spec import Decision

        return Decision(topic=topic, choice="C", rationale="R", date=date(2026, 1, 1))

    @pytest.mark.parametrize("board_dir", _BOARD_DIRS)
    def test_append_is_byte_level_additive_on_real_boards(self, board_dir: str, tmp_path):
        """The only change to a real board file is the appended entry."""
        from kicad_tools.spec import append_decision

        source = _repo_board(board_dir)
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        original = source.read_text(encoding="utf-8")
        work = tmp_path / "project.kct"
        work.write_text(original, encoding="utf-8")

        append_decision(work, self._decision())
        updated = work.read_text(encoding="utf-8")

        # Every original line survives, in order, unmodified.
        original_lines = original.splitlines()
        updated_lines = updated.splitlines()
        added = [line for line in updated_lines if line not in original_lines]
        remaining = [line for line in updated_lines if line in original_lines]
        assert remaining == original_lines, "existing lines were reordered or rewritten"
        assert any("PROBE" in line for line in added)

        # ... and the parsed data differs only by the appended decision.
        before = yaml.safe_load(original)
        after = yaml.safe_load(updated)
        expected = dict(before)
        expected["decisions"] = [*(before.get("decisions") or []), after["decisions"][-1]]
        assert after == expected

    def test_deciding_twice_leaves_the_first_entry_unchanged(self, tmp_path):
        from kicad_tools.spec import append_decision

        source = _repo_board("00-simple-led")
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        work = tmp_path / "project.kct"
        work.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        pre_existing = yaml.safe_load(work.read_text(encoding="utf-8"))["decisions"]

        append_decision(work, self._decision("FIRST"))
        after_first = yaml.safe_load(work.read_text(encoding="utf-8"))["decisions"]

        append_decision(work, self._decision("SECOND"))
        after_second = yaml.safe_load(work.read_text(encoding="utf-8"))["decisions"]

        assert after_first[: len(pre_existing)] == pre_existing
        assert after_second[: len(after_first)] == after_first
        assert [d["topic"] for d in after_second[-2:]] == ["FIRST", "SECOND"]

    @pytest.mark.parametrize("indent", ["", "  "])
    def test_preserves_block_sequence_indentation_and_comments(self, tmp_path, indent):
        from kicad_tools.spec import append_decision

        original = (
            'kct_version: "1.0"\nproject:\n  name: "T"\n'
            "decisions:\n"
            f"{indent}- date: '2026-01-01'\n"
            f"{indent}  topic: First\n{indent}  choice: A\n{indent}  rationale: R1\n"
            "# Preserve the comment between decisions.\n"
            f"{indent}- topic: Second\n{indent}  choice: B\n{indent}  rationale: R2\n"
        )
        suffix = "\n# Preserve the next section.\nprogress:\n  phase: concept\n"
        path = tmp_path / "p.kct"
        path.write_text(original + suffix, encoding="utf-8")

        append_decision(path, self._decision())

        updated = path.read_text(encoding="utf-8")
        before = yaml.safe_load(original + suffix)
        after = yaml.safe_load(updated)
        assert after == {
            **before,
            "decisions": [*before["decisions"], after["decisions"][-1]],
        }
        assert [d["topic"] for d in after["decisions"]] == ["First", "Second", "PROBE"]
        assert updated.startswith(original)
        assert updated.endswith(suffix)
        assert updated[len(original) : -len(suffix)].startswith(f"{indent}- ")

    def test_creates_the_list_when_no_decisions_key_exists(self, tmp_path):
        from kicad_tools.spec import append_decision

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\nproject:\n  name: "T"\nprogress:\n  phase: concept\n',
            encoding="utf-8",
        )

        append_decision(path, self._decision())

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert [d["topic"] for d in data["decisions"]] == ["PROBE"]
        assert data["project"]["name"] == "T"
        assert data["progress"]["phase"] == "concept"

    def test_appends_into_an_explicitly_empty_list(self, tmp_path):
        from kicad_tools.spec import append_decision

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "decisions: []\n"
            "progress:\n"
            "  phase: concept\n",
            encoding="utf-8",
        )

        append_decision(path, self._decision())

        text = path.read_text(encoding="utf-8")
        assert "progress:" in text
        data = yaml.safe_load(text)
        assert [d["topic"] for d in data["decisions"]] == ["PROBE"]
        assert data["progress"]["phase"] == "concept"

    def test_preserves_unknown_keys_that_follow_the_decisions_block(self, tmp_path):
        from kicad_tools.spec import append_decision

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "requirements:\n"
            "  electrical:\n"
            "    input_voltage: 5V\n"
            "  components:\n"
            "    - id: R1\n"
            "decisions:\n"
            "  - topic: Existing\n"
            "    choice: X\n"
            "    rationale: |\n"
            "      Multi-line rationale.\n"
            "\n"
            "      With a blank line.\n"
            "\n"
            "# trailing comment section\n"
            "progress:\n"
            "  phase: concept\n"
            "  checklist:\n"
            '    - "[x] Schematic"\n',
            encoding="utf-8",
        )

        append_decision(path, self._decision())

        text = path.read_text(encoding="utf-8")
        assert "# trailing comment section" in text, "comments must survive an append"
        data = yaml.safe_load(text)
        assert data["requirements"]["electrical"]["input_voltage"] == "5V"
        assert data["requirements"]["components"] == [{"id": "R1"}]
        assert data["progress"]["checklist"] == ["[x] Schematic"]
        assert [d["topic"] for d in data["decisions"]] == ["Existing", "PROBE"]
        assert data["decisions"][0]["rationale"] == (
            "Multi-line rationale.\n\nWith a blank line.\n"
        )

    # Shapes where the textual splice *succeeds* and still re-parses as valid
    # YAML, but produces the wrong data -- so only the ``reparsed != expected``
    # comparison in ``append_decision`` can catch them. Each entry is
    # (source text, topics the splice alone would have written).
    _UNSPLICEABLE_SHAPES = {
        # A quoted key misses the top-level-key regex, so the splice starts a
        # *second* ``decisions:`` block; pyyaml resolves the duplicate
        # last-wins and the pre-existing entry disappears silently.
        "quoted_key": (
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            '"decisions":\n'
            "  - topic: First\n"
            "    choice: A\n"
            "    rationale: R1\n"
            "progress:\n"
            "  phase: concept\n",
            ["PROBE"],
            ["First", "PROBE"],
        ),
    }

    @pytest.mark.parametrize("shape", sorted(_UNSPLICEABLE_SHAPES))
    def test_unfaithful_splice_falls_back_to_a_structural_rewrite(self, shape, tmp_path):
        """A splice that parses but says the wrong thing must never be written.

        ``_splice_decision`` returns text for this shape and that text
        is valid YAML, so nothing but the ``reparsed != expected`` comparison in
        :func:`append_decision` detects that it is wrong. Deleting that guard
        makes this test fail.
        """
        from kicad_tools.spec import append_decision
        from kicad_tools.spec.parser import _splice_decision

        source, spliced_topics, expected_topics = self._UNSPLICEABLE_SHAPES[shape]

        # The raw splice really does produce wrong data for this shape -- that
        # is what makes the guard load-bearing rather than decorative.
        entry = self._decision().model_dump(exclude_none=True, mode="json")
        raw = _splice_decision(source, entry)
        assert raw is not None, f"{shape}: splice declined, so it never reaches the guard"
        assert [d["topic"] for d in yaml.safe_load(raw)["decisions"]] == spliced_topics

        # append_decision must reject it and take the structural-rewrite path.
        path = tmp_path / "p.kct"
        path.write_text(source, encoding="utf-8")
        append_decision(path, self._decision())

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert [d["topic"] for d in data["decisions"]] == expected_topics
        # The rest of the file survives the fallback rewrite intact.
        assert data["project"]["name"] == "T"
        assert data["progress"]["phase"] == "concept"
        assert data["kct_version"] == "1.0"

    def test_malformed_spec_fails_loudly_without_truncating(self, tmp_path):
        from kicad_tools.spec import append_decision

        path = tmp_path / "p.kct"
        original = "invalid: yaml: content: ["
        path.write_text(original, encoding="utf-8")

        with pytest.raises(ValueError):
            append_decision(path, self._decision())

        assert path.read_text(encoding="utf-8") == original

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    @pytest.mark.parametrize("original_mode", [0o644, 0o664, 0o600])
    def test_writing_preserves_the_files_permission_bits(self, original_mode, tmp_path):
        """Neither write path may silently re-permission an existing ``.kct``.

        The atomic write goes through ``tempfile.mkstemp`` (which creates its
        file 0600) plus ``os.replace`` (which carries that mode onto the
        target), so without an explicit chmod every save would narrow a normal
        0644 spec to 0600 -- cumulatively and invisibly, since git tracks only
        the exec bit.
        """
        from kicad_tools.spec import append_decision, load_spec, save_spec

        path = tmp_path / "p.kct"
        path.write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "T"\n'
            "decisions: []\n"
            "progress:\n"
            "  phase: concept\n",
            encoding="utf-8",
        )
        os.chmod(path, original_mode)
        assert stat.S_IMODE(path.stat().st_mode) == original_mode

        append_decision(path, self._decision())
        assert stat.S_IMODE(path.stat().st_mode) == original_mode, (
            "append_decision changed the file's permission bits"
        )

        save_spec(load_spec(path), path)
        assert stat.S_IMODE(path.stat().st_mode) == original_mode, (
            "save_spec changed the file's permission bits"
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_new_file_permissions_follow_the_umask(self, tmp_path):
        """A *created* spec gets ordinary umask permissions, not mkstemp's 0600.

        The exact bits depend on the ambient umask, so this asserts equivalence
        with what a plain ``Path.write_text`` produces in the same process
        rather than pinning a literal mode.
        """
        from kicad_tools.spec import load_spec, save_spec

        seed = tmp_path / "seed.kct"
        seed.write_text(
            'kct_version: "1.0"\nproject:\n  name: "T"\nprogress:\n  phase: concept\n',
            encoding="utf-8",
        )

        reference = tmp_path / "reference.kct"
        reference.write_text("x\n", encoding="utf-8")

        created = tmp_path / "created.kct"
        save_spec(load_spec(seed), created)

        assert stat.S_IMODE(created.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)

    def test_cli_decide_on_a_board_copy_is_append_only(self, tmp_path):
        """End-to-end through the CLI handler, not just the library."""
        from argparse import Namespace

        from kicad_tools.cli.commands.spec import run_spec_command

        source = _repo_board("00-simple-led")
        if not source.exists():
            pytest.skip(f"project.kct not found at {source}")

        original = source.read_text(encoding="utf-8")
        work = tmp_path / "project.kct"
        work.write_text(original, encoding="utf-8")

        rc = run_spec_command(
            Namespace(
                spec_command="decide",
                spec_file=str(work),
                decide_topic="CLI PROBE",
                decide_choice="C",
                decide_rationale="R",
                decide_alternatives=None,
            )
        )

        assert rc == 0
        updated = work.read_text(encoding="utf-8")
        # Every original line survives verbatim and in order; only the new
        # entry's lines are added.
        original_lines = original.splitlines()
        kept = [line for line in updated.splitlines() if line in original_lines]
        assert kept == original_lines, "decide rewrote bytes it was not asked to touch"
        assert "CLI PROBE" in updated

        before = yaml.safe_load(original)
        after = yaml.safe_load(updated)
        for key in before:
            if key != "decisions":
                assert after[key] == before[key], f"{key} changed"
        assert after["decisions"][:-1] == before["decisions"]


@pytest.mark.parametrize("project", sorted(Path(__file__).parents[1].glob("boards/*/project.kct")))
def test_fleet_board_specs_validate(project):
    """Issue #7250: every committed board spec is schema-valid."""
    from kicad_tools.spec import validate_spec

    ok, errors = validate_spec(project)
    assert ok, errors
