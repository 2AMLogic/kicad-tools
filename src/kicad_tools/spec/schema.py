"""
Pydantic models for .kct project specification format.

Defines the schema for PCB project specifications including:
- Project metadata
- Design intent and interfaces
- Requirements (electrical, mechanical, environmental)
- Suggestions and preferences
- Design decisions with rationale
- Progress tracking and validation results
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

try:
    from pydantic import (
        BaseModel,
        ConfigDict,
        Field,
        ValidationInfo,
        field_validator,
        model_validator,
    )
except ImportError as e:
    raise ImportError(
        "pydantic is required for the spec module. "
        "Install it with: pip install 'kicad-tools[mcp]' or pip install pydantic"
    ) from e


__all__ = [
    "SpecModel",
    "ProjectSpec",
    "ProjectMetadata",
    "ProjectArtifacts",
    "BuildConfig",
    "DesignIntent",
    "Interface",
    "Requirements",
    "ElectricalRequirements",
    "InputRequirements",
    "OutputRail",
    "MechanicalRequirements",
    "MountingHole",
    "MountingHoleGroupSpec",
    "KeepOut",
    "EnvironmentalRequirements",
    "ManufacturingRequirements",
    "EscalationPolicy",
    "Compliance",
    "Suggestions",
    "ComponentSuggestion",
    "BOMEntry",
    "PurchasingEntry",
    "LeadProfile",
    "SmdLandPattern",
    "MountingHoleDeclaration",
    "DimensionLimits",
    "FinishedHoleGuarantee",
    "Decision",
    "Progress",
    "PhaseProgress",
    "Validation",
    "ValidationResult",
    "DesignPhase",
    "PhaseStatus",
]


class SpecModel(BaseModel):
    """Base model for every ``.kct`` schema node.

    ``extra="allow"`` is a deliberate, load-bearing choice, not a convenience.

    A ``.kct`` file is a *human-authored configuration file that kicad-tools
    rewrites in place* (``kct spec decide``, ``kct spec check``, and any caller
    of :func:`kicad_tools.spec.parser.save_spec`). Pydantic's default
    ``extra="ignore"`` is the right policy for parsing a throwaway API payload
    and exactly the wrong policy for a round-tripped config file: every key the
    schema does not define would be dropped from the model at load time and
    therefore **silently deleted from the user's file on the next write**.

    Keeping unknown keys on the model means ``model_dump()`` round-trips them,
    so a writer can never destroy content it does not understand. The obvious
    cost of ``extra="allow"`` -- that it also swallows genuine typos -- is paid
    down by :func:`kicad_tools.spec.parser.collect_unknown_keys`, which
    ``kct spec validate`` reports as warnings so schema drift stays *visible*
    instead of becoming *destructive*.
    """

    model_config = ConfigDict(extra="allow")


class DesignPhase(str, Enum):
    """Design phase enumeration."""

    CONCEPT = "concept"
    ARCHITECTURE = "architecture"
    SCHEMATIC = "schematic"
    LAYOUT = "layout"
    REVIEW = "review"
    PRODUCTION = "production"


class PhaseStatus(str, Enum):
    """Phase status enumeration."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class ProjectArtifacts(SpecModel):
    """Project file artifacts."""

    schematic: str | None = Field(default=None, description="Path to schematic file")
    pcb: str | None = Field(default=None, description="Path to PCB file (unrouted)")
    pcb_routed: str | None = Field(
        default=None,
        description=(
            "Path to routed PCB file. If unset, defaults to '<pcb_stem>_routed.kicad_pcb' "
            "alongside the PCB. Consumed by `kct build` (see _run_step_route)."
        ),
    )
    project: str | None = Field(default=None, description="Path to KiCad project file")


class BuildConfig(SpecModel):
    """Build-pipeline configuration (`kct build`).

    Currently carries the route-step discovery contract. Boards whose
    routing entry point is a recipe script invocation (rather than a
    standalone ``route_demo.py`` / ``route.py``) declare it here so the
    build pipeline invokes the *same* command their CI gates use — with
    all the diff-pair / match-group / seed / layer flags baked into the
    recipe — instead of falling through to the generic ``kct route``
    autorouter, which drops those flags.
    """

    route_recipe: str | None = Field(
        default=None,
        description=(
            "Command that routes the committed unrouted PCB into "
            "``*_routed.kicad_pcb``. Interpreted relative to the project "
            "directory and run with the project's Python interpreter, e.g. "
            "``generate_design.py --step route``. When set, `kct build`'s "
            "route step invokes it verbatim (forwarding the output dir as a "
            "positional argument) instead of probing for route_demo.py / "
            "route.py or falling back to generic `kct route`. See "
            "``_run_step_route`` for the full probe order."
        ),
    )


class ProjectMetadata(SpecModel):
    """Project metadata and identification."""

    name: str = Field(..., description="Project name")
    revision: str = Field(default="A", description="Project revision")
    created: datetime.date | None = Field(default=None, description="Creation date")
    author: str | None = Field(default=None, description="Author name or email")
    description: str | None = Field(default=None, description="Brief project description")
    artifacts: ProjectArtifacts | None = Field(default=None, description="Project file paths")


class Interface(SpecModel):
    """Interface definition for the design."""

    name: str = Field(..., description="Interface name")
    type: str = Field(..., description="Interface type (e.g., usb_c_pd_sink, power_rail, gpio)")
    description: str | None = Field(default=None, description="Interface description")
    # Type-specific fields stored flexibly
    profiles: list[str] | None = Field(default=None, description="Supported profiles for USB PD")
    voltage: str | None = Field(default=None, description="Voltage for power rails")
    current_max: str | None = Field(default=None, description="Maximum current")
    pins: list[str] | None = Field(default=None, description="Pin assignments")
    protocol: str | None = Field(default=None, description="Communication protocol")
    # Off-board assembly fields (issue #3531b): subsystems that connect
    # through a board connector but are not placed on the PCB (e.g.
    # supercap banks wired through terminal blocks).  Reports use these
    # to document where the parts went and to add hand-solder BOM rows.
    off_board: bool = Field(
        default=False,
        description="True when the assembly is not placed on the PCB",
    )
    connector: str | None = Field(
        default=None,
        description="Board connector reference the assembly wires to (e.g. 'J3')",
    )
    part: str | None = Field(
        default=None, description="Part number for the off-board assembly cells"
    )
    qty: int | None = Field(default=None, description="Quantity of cells/parts in the assembly")
    capacitance: str | None = Field(
        default=None, description="Capacitance for energy-storage interfaces"
    )
    assembly: str | None = Field(
        default=None,
        description=(
            "Assembly method (e.g. 'hand_solder'); off-board parts are DNP for fab assembly"
        ),
    )
    wiring: str | None = Field(
        default=None,
        description="Human-readable wiring description (connector pin -> assembly terminal)",
    )


class DesignIntent(SpecModel):
    """High-level design intent - the 'why' before the 'what'."""

    summary: str = Field(..., description="Brief summary of what the board does")
    use_cases: list[str] | None = Field(default=None, description="Intended use cases")
    interfaces: list[Interface] | None = Field(default=None, description="External interfaces")
    constraints: list[str] | None = Field(
        default=None, description="Key constraints driving the design"
    )


class InputRequirements(SpecModel):
    """Input power/signal requirements."""

    voltage: dict[str, str] | None = Field(
        default=None, description="Voltage range (min, max, nominal)"
    )
    current: dict[str, str] | None = Field(
        default=None, description="Current limits (max, typical)"
    )
    frequency: dict[str, str] | None = Field(
        default=None, description="Frequency range for signals"
    )


class OutputRail(SpecModel):
    """Output power rail specification."""

    rail: str = Field(..., description="Rail name or voltage")
    tolerance: str | None = Field(default=None, description="Voltage tolerance (e.g., ±2%)")
    current_max: str | None = Field(default=None, description="Maximum current")
    ripple_max: str | None = Field(default=None, description="Maximum ripple voltage")
    efficiency_min: str | None = Field(default=None, description="Minimum efficiency")
    load_regulation: str | None = Field(default=None, description="Load regulation spec")


class ElectricalRequirements(SpecModel):
    """Electrical requirements and specifications."""

    input: InputRequirements | None = Field(default=None, description="Input requirements")
    outputs: list[OutputRail] | None = Field(default=None, description="Output rail specifications")
    signals: dict[str, Any] | None = Field(
        default=None, description="Signal specifications (impedance, timing, etc.)"
    )
    esd_protection: str | None = Field(default=None, description="ESD protection level")
    isolation: str | None = Field(default=None, description="Isolation requirements")


class MountingHole(SpecModel):
    """Mounting hole specification."""

    x: str = Field(..., description="X position")
    y: str = Field(..., description="Y position")
    diameter: str = Field(..., description="Hole diameter")
    plated: bool = Field(default=False, description="Whether hole is plated")


class KeepOut(SpecModel):
    """Keep-out region specification."""

    region: dict[str, str] = Field(..., description="Region bounds (x, y, width, height)")
    reason: str = Field(..., description="Reason for keep-out")
    layers: list[str] | None = Field(default=None, description="Affected layers")


class MountingHoleGroupSpec(SpecModel):
    """Placeable group of mounting holes with fixed relative geometry.

    Issue #3352 (Q3 reframe): mounting holes form a rigid pattern that can be
    *placed* as a unit when auto-pcb-size escalation grows the board.  The
    group's anchor moves; the holes within it preserve their relative
    positions.  This replaces the older "refuse if any mounting hole present"
    behaviour with a richer primitive: layouts can grow as long as the hole
    group still fits within the escalated envelope at its declared anchor.

    The geometry conventions are:
      - ``holes`` are ``(x, y)`` positions in millimetres *relative to the
        group's local origin* (NOT board coordinates).
      - ``anchor`` is the position in *board coordinates* where the group's
        local origin sits.  The on-board hole position is therefore
        ``(anchor_x + hole_x, anchor_y + hole_y)``.
      - ``hole_diameter_mm`` applies uniformly to all holes in the group
        (M3 = 3.2 mm clearance is the default; matches softstart rev B).
      - ``keepout_radius_mm`` is the no-copper radius around each hole that
        the router treats as a soft obstacle.

    Consumed by the :class:`kicad_tools.pcb.mounting_holes.MountingHoleGroup`
    primitive, which handles the geometric placement / envelope-fit check.

    Attributes:
        holes: List of ``(x, y)`` hole positions in mm, relative to anchor.
        anchor: Default placement position ``(x, y)`` in mm, in board coords.
        hole_diameter_mm: Clearance hole diameter in mm (default 3.2 = M3).
        keepout_radius_mm: No-copper keepout radius in mm (default 5.0).
    """

    holes: list[tuple[float, float]] = Field(
        ...,
        description=(
            "List of (x, y) hole positions in mm, relative to the group's "
            "local origin.  Must contain at least one hole."
        ),
    )
    anchor: tuple[float, float] = Field(
        ...,
        description=(
            "Default placement position (x, y) in mm, in board coordinates. "
            "The auto-pcb-size escalation loop may move this anchor when "
            "the board envelope changes."
        ),
    )
    hole_diameter_mm: float = Field(
        default=3.2,
        description="Clearance hole diameter in mm (default 3.2 mm = M3 clearance).",
    )
    keepout_radius_mm: float = Field(
        default=5.0,
        description=(
            "No-copper keepout radius in mm around each hole.  Treated as a "
            "soft obstacle by the auto-router."
        ),
    )

    @field_validator("holes")
    @classmethod
    def validate_holes_nonempty(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Reject empty hole lists -- a group with zero holes is meaningless."""
        if not v:
            raise ValueError(
                "MountingHoleGroupSpec.holes must contain at least one hole; "
                "omit the field entirely if no mounting holes are required."
            )
        return v

    @field_validator("hole_diameter_mm", "keepout_radius_mm")
    @classmethod
    def validate_positive_dimensions(cls, v: float) -> float:
        """Hole diameter and keepout radius must be positive."""
        if v <= 0:
            raise ValueError(f"Dimension must be positive, got {v}")
        return v


class MechanicalRequirements(SpecModel):
    """Mechanical requirements and form factor."""

    dimensions: dict[str, str] | None = Field(
        default=None, description="Board dimensions (width, height, thickness)"
    )
    mounting_holes: list[MountingHole] | None = Field(
        default=None, description="Mounting hole positions"
    )
    mounting_hole_group: MountingHoleGroupSpec | None = Field(
        default=None,
        description=(
            "Issue #3352: Placeable mounting hole group with fixed relative "
            "geometry.  Preferred over the flat ``mounting_holes`` list when "
            "auto-pcb-size escalation is enabled; the group can be repositioned "
            "as a unit when the board envelope grows, while preserving the hole "
            "pattern.  When both fields are present, ``mounting_hole_group`` "
            "takes precedence for routing/escalation; ``mounting_holes`` is "
            "retained for backwards compatibility with non-grouped layouts."
        ),
    )
    keep_outs: list[KeepOut] | None = Field(default=None, description="Keep-out regions")
    edge_clearance: str | None = Field(
        default=None, description="Required clearance from board edge"
    )
    weight_max: str | None = Field(default=None, description="Maximum board weight")
    enclosure: str | None = Field(default=None, description="Target enclosure reference")
    envelope_hard: bool = Field(
        default=False,
        description=(
            "Issue #3352: When True, ``dimensions`` is a non-negotiable "
            "enclosure constraint and auto-pcb-size escalation refuses to "
            "grow the board, emitting an actionable error suggesting layer / "
            "clearance / BOM levers instead.  Default False -- dimensions "
            "are treated as initial sizing only and may be grown by "
            "escalation when routability requires it."
        ),
    )


class TemperatureRange(SpecModel):
    """Temperature range specification."""

    operating: list[str] | None = Field(default=None, description="Operating range [min, max]")
    storage: list[str] | None = Field(default=None, description="Storage range [min, max]")


class EnvironmentalRequirements(SpecModel):
    """Environmental requirements."""

    temperature: TemperatureRange | None = Field(default=None, description="Temperature ranges")
    humidity: str | None = Field(default=None, description="Humidity rating")
    vibration: str | None = Field(default=None, description="Vibration resistance")
    ip_rating: str | None = Field(default=None, description="IP rating")


_COPPER_WEIGHT_RE = re.compile(r"^\s*([\d.]+)\s*(?:oz)?\s*$", re.IGNORECASE)


def _parse_copper_weight_oz(value: int | float | str) -> float:
    """Parse a copper weight value to float oz.

    Accepts:
      - int or float (returned as-is, e.g. 2 -> 2.0)
      - str like '2oz', '0.5oz', '2 oz', '2OZ', or bare '2'

    Raises ValueError for unrecognised formats.
    """
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(
            f"Invalid copper_weight type: {type(value).__name__}. "
            "Expected a number or string like '2oz', '0.5oz', '1 oz'."
        )
    m = _COPPER_WEIGHT_RE.match(value)
    if m is None:
        raise ValueError(
            f"Invalid copper_weight: '{value}'. "
            "Expected a number or string like '2oz', '0.5oz', '1 oz'."
        )
    return float(m.group(1))


class EscalationPolicy(SpecModel):
    """Issue #3352: Routing escalation policy for the auto-* ladder.

    Declares the order in which the routing system will attempt to overcome
    over-constrained designs.  Each rung in the ladder corresponds to a
    different *axis* of slack (more layers, larger envelope, etc.) and the
    routing system will only escalate along axes the recipe permits.

    The ``ladder`` field selects between five composable strategies:

    - ``"layers-first"`` (default): exhaust the layer escalation ladder
      (2L -> 4L -> 6L) at the current envelope before trying size escalation.
      Cheapest at JLCPCB prototype quantities (qty 5) where layer adds are
      cheaper than envelope upgrades.
    - ``"size-first"``: walk the manufacturer's size-tier ladder before
      adding layers.  Preferred when the board's mechanical envelope is
      genuinely negotiable and the user values 2-layer cost optimization
      at production volume.
    - ``"layers-only"``: layer escalation only; refuse to grow the envelope.
      Equivalent to ``layers-first`` + ``envelope_hard=True`` but declared
      on the manufacturing side rather than the mechanical side.
    - ``"size-only"``: size escalation only; refuse to add layers.
      Useful when layer count is fixed by cost or stackup constraints.
    - ``"none"``: no escalation; refuse to escalate either axis.  The
      routing system reports the best-attempt result and lets the recipe
      author decide how to respond.

    The policy composes with :attr:`MechanicalRequirements.envelope_hard`:
    when the recipe declares the envelope as a hard mechanical constraint,
    any ``ladder`` value containing ``"size"`` falls back to layer-only
    behaviour (the size axis is implicitly disabled).

    The ``max_layers`` ceiling caps the layer escalation ladder.  The
    ``max_size_tier`` ceiling caps the size-tier escalation ladder by
    index into the manufacturer's :data:`MFR_SIZE_TIER_LADDERS` entry.
    A ``None`` ceiling means "use the manufacturer's maximum tier".

    The ``density_threshold_viols_per_cm2`` field is the DRC density
    trigger used by the size-escalation logic to discriminate "true
    envelope over-constraint" from "single hot-spot we can hand-fix".
    Hardcoded for now per Issue #3352 Q4 decision -- promote to a CLI
    flag if empirical evidence shows recipe-by-recipe tuning is needed.

    The ``starting_layers`` field is the lower rung of the layer
    escalation ladder.  Default 2 preserves the historical behaviour
    (probe 2L first, then 4L, then 6L).  Boards that have no realistic
    chance of routing at 2L can opt out of the 2L tax by declaring
    ``starting_layers=4`` (ladder becomes ``[4, 6]``).  Per Issue #3400,
    the field is bounded ``[2, 6]`` and must be ``<= max_layers``.

    Attributes:
        ladder: Escalation strategy selector.
        max_layers: Layer escalation ceiling (default 4 -- covers 2L, 4L).
        starting_layers: Layer escalation floor (default 2 -- start at 2L).
            Set to 4 to skip the 2L probe entirely (Issue #3400).
        max_size_tier: Size-tier ceiling (index into MFR_SIZE_TIER_LADDERS
            for the manufacturer; None = use manufacturer's max).
        density_threshold_viols_per_cm2: DRC violation density that
            triggers escalation (per Issue #3352 Q4: 0.5 viols/cm^2).
    """

    ladder: Literal[
        "layers-first",
        "size-first",
        "layers-only",
        "size-only",
        "none",
    ] = Field(
        default="layers-first",
        description=(
            "Escalation strategy.  See class docstring for the five options "
            "and their cost/policy implications."
        ),
    )
    max_layers: int = Field(
        default=4,
        description=(
            "Maximum layer count the escalation ladder may reach.  Default 4 "
            "covers the common 2L -> 4L transition; recipes that need 6L+ "
            "must opt in explicitly."
        ),
    )
    starting_layers: int = Field(
        default=2,
        ge=2,
        le=6,
        description=(
            "Lower rung of the layer escalation ladder (Issue #3400).  "
            "Default 2 preserves the historical 2L->4L->6L ladder.  Boards "
            "with no realistic chance of routing at 2L can opt out of the "
            "2L tax by declaring ``starting_layers=4`` (ladder becomes "
            "``[4, 6]``).  Must be <= max_layers."
        ),
    )
    max_size_tier: int | None = Field(
        default=None,
        description=(
            "Index into the manufacturer's size-tier ladder (see "
            ":data:`kicad_tools.router.mfr_limits.MFR_SIZE_TIER_LADDERS`) "
            "above which size escalation refuses.  ``None`` (default) means "
            "use the manufacturer's largest available tier."
        ),
    )
    density_threshold_viols_per_cm2: float = Field(
        default=0.5,
        description=(
            "DRC violation density (per cm^2 of board area) that triggers "
            "auto-pcb-size escalation.  Per Issue #3352 Q4: hardcoded at "
            "0.5 viols/cm^2 based on the softstart rev B 132/150 = 0.88 "
            "case.  Tunable here for forward compatibility; promote to a "
            "CLI flag if recipe-by-recipe tuning becomes necessary."
        ),
    )
    packing_overhead: float = Field(
        default=2.5,
        description=(
            "Issue #3403: heuristic packing-density multiplier used by "
            "the pre-route sum-of-clearances area estimator.  The estimator "
            "computes ``packing_overhead * (sum(footprint_area + "
            "clearance_perimeter) + routing_channel_estimate)`` -- the "
            "multiplier accounts for the geometric inefficiency of routing "
            "channels, vias, fillets, and component keepouts not modeled "
            "by the per-footprint terms.  Default 2.5 is a moderate "
            "prototype-density figure; bump to 3.0+ for tightly packed "
            "designs or down to 1.8 for very loose layouts.  Set to 0 to "
            "disable the pre-route check (reactive DRC-density backstop "
            "still applies).  Tunable per recipe via spec or per invocation "
            "via the ``--packing-overhead`` CLI flag."
        ),
    )

    @field_validator("max_layers")
    @classmethod
    def validate_max_layers(cls, v: int) -> int:
        """Layer count must be a positive even number (KiCad convention)."""
        if v < 1:
            raise ValueError(f"max_layers must be >= 1, got {v}")
        return v

    @field_validator("max_size_tier")
    @classmethod
    def validate_max_size_tier(cls, v: int | None) -> int | None:
        """Size-tier index must be non-negative when present."""
        if v is not None and v < 0:
            raise ValueError(f"max_size_tier must be >= 0, got {v}")
        return v

    @field_validator("density_threshold_viols_per_cm2")
    @classmethod
    def validate_density_threshold(cls, v: float) -> float:
        """Density threshold must be positive (negative makes no physical sense)."""
        if v <= 0:
            raise ValueError(f"density_threshold_viols_per_cm2 must be positive, got {v}")
        return v

    @field_validator("packing_overhead")
    @classmethod
    def validate_packing_overhead(cls, v: float) -> float:
        """Packing overhead must be non-negative (0 disables the pre-route check)."""
        if v < 0:
            raise ValueError(f"packing_overhead must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def validate_starting_layers_le_max(self) -> EscalationPolicy:
        """Issue #3400: starting_layers must not exceed max_layers."""
        if self.starting_layers > self.max_layers:
            raise ValueError(
                f"starting_layers ({self.starting_layers}) must be <= "
                f"max_layers ({self.max_layers}); a ladder cannot start "
                f"above its ceiling."
            )
        return self


class DimensionLimits(SpecModel):
    """Inclusive lower and upper limit of one linear drawing dimension, in millimetres."""

    min_mm: float = Field(
        ...,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Smallest allowed value, in millimetres (inclusive).",
    )
    max_mm: float = Field(
        ...,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Largest allowed value, in millimetres (inclusive).",
    )

    @model_validator(mode="after")
    def _validate_order(self) -> DimensionLimits:
        if self.min_mm > self.max_mm:
            raise ValueError("limits require min_mm <= max_mm")
        return self


class FinishedHoleGuarantee(SpecModel):
    """A finished-hole interval the fabricator documented for selected holes (Issue #10476).

    An order-specific guarantee: the fabricator confirmed that the holes of
    *refs* will finish between :attr:`min_mm` and :attr:`max_mm`, inclusive.
    Assembly preflight uses it instead of the general order tolerance for
    those holes, and only for the spec's target fab; :attr:`source` must
    name the order note, email or quote that documents it.
    """

    refs: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Reference designators or dash-separated ranges (e.g. 'C1' or "
            "'C1-C4') whose mounting holes the guarantee covers."
        ),
    )
    min_mm: float = Field(
        ...,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Guaranteed smallest finished-hole diameter, in millimetres (inclusive).",
    )
    max_mm: float = Field(
        ...,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Guaranteed largest finished-hole diameter, in millimetres (inclusive).",
    )
    source: str = Field(
        ...,
        description=(
            "Where the fabricator documented it: an order note, email or quote reference."
        ),
    )

    @field_validator("refs", mode="before")
    @classmethod
    def _wrap_single_ref(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("refs")
    @classmethod
    def _validate_refs(cls, refs: list[str]) -> list[str]:
        if any(not ref.strip() for ref in refs):
            raise ValueError("finished-hole guarantee requires non-empty refs")
        return refs

    @field_validator("source")
    @classmethod
    def _validate_source(cls, source: str) -> str:
        if not source.strip():
            raise ValueError(
                "finished-hole guarantee requires a source (order note, email or quote reference)"
            )
        return source

    @model_validator(mode="after")
    def _validate_interval(self) -> FinishedHoleGuarantee:
        if self.min_mm > self.max_mm:
            raise ValueError("limits require min_mm <= max_mm")
        return self


class ManufacturingRequirements(SpecModel):
    """Manufacturing requirements and constraints."""

    order_thickness_mm: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Nominal finished PCB thickness selected for purchase, in millimetres.",
    )
    layers: dict[str, int] | None = Field(
        default=None, description="Layer count (preferred, min, max)"
    )
    stackup: list[str] | None = Field(
        default=None,
        description=(
            "Optional human-readable description of each PCB layer "
            "(e.g., ['F.Cu (signal)', 'In1.Cu (GND plane)', ...]). "
            "Captured for documentation/audit; not consumed by the router."
        ),
    )
    copper_weight: float | None = Field(
        default=None,
        description="Copper weight in oz/ft^2 (e.g., 1, 2, '2oz', '0.5oz')",
    )
    min_trace: str | None = Field(default=None, description="Minimum trace width")
    min_space: str | None = Field(default=None, description="Minimum spacing")
    min_via: str | None = Field(default=None, description="Minimum via size")
    min_drill: str | None = Field(default=None, description="Minimum drill size")
    target_fab: str | None = Field(default=None, description="Target fabricator (e.g., jlcpcb)")
    assembly: str | None = Field(
        default=None, description="Assembly type (smt, through_hole, both)"
    )
    finish: str | None = Field(default=None, description="Surface finish (HASL, ENIG, etc.)")
    solder_mask: str | None = Field(default=None, description="Solder mask color")
    silkscreen: str | None = Field(default=None, description="Silkscreen color")
    silkscreen_min_reference_height_mm: float | None = Field(
        default=None,
        description=(
            "Issue #9448: declared minimum character height (mm) for reference "
            "designators only.  Unset (the default) holds designators to the "
            "target fab's silkscreen text-height floor like every other silk "
            "text.  When set below that floor it relaxes the designator floor "
            "in the generated .kicad_dru and in `kct check`; labels and board "
            "text keep the fab floor.  Must be >= 0.8 mm.  The declaration is "
            "reported by `kct readiness` as an accepted deviation."
        ),
    )
    escalation: EscalationPolicy | None = Field(
        default=None,
        description=(
            "Issue #3352: Auto-routing escalation policy.  When unset (the "
            "default), no auto-escalation is performed -- the router uses "
            "the declared layer count / envelope and reports the best "
            "attempt.  When set, the router walks the declared ladder "
            "before reporting refusal."
        ),
    )
    finished_hole_tolerance_mm: float | None = Field(
        default=None,
        le=0,
        allow_inf_nan=False,
        strict=True,
        description=(
            "Issue #10299: the fabricator's applicable *lower* "
            "finished-hole tolerance, in millimetres -- the most the "
            "finished (plated) hole may fall below the nominal drill.  "
            "Sign convention: zero or negative; a positive value is "
            "rejected because it would claim the hole only grows.  "
            "Worked example: -0.08 means a 2.20 mm nominal hole may "
            "finish at 2.12 mm minimum, so a lead needing 2.10 mm "
            "still clears by +0.02 mm at worst.  Assembly preflight "
            "uses it for the tolerance-adjusted lead-fit margin.  "
            "Requires ``finished_hole_tolerance_source``."
        ),
    )
    finished_hole_tolerance_source: str | None = Field(
        default=None,
        validate_default=True,
        description=(
            "Issue #10299: where the finished-hole tolerance was published "
            "(e.g. 'JLCPCB capabilities, through-hole tolerances').  "
            "Required when ``finished_hole_tolerance_mm`` or "
            "``finished_hole_tolerance_upper_mm`` is set."
        ),
    )
    finished_hole_tolerance_upper_mm: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        strict=True,
        description=(
            "Issue #10476: the fabricator's applicable *upper* "
            "finished-hole tolerance, in millimetres -- the most the "
            "finished (plated) hole may exceed the nominal drill.  "
            "Sign convention: zero or positive; a negative value is "
            "rejected because it would claim the hole only shrinks.  "
            "Worked example: 0.13 means a 2.00 mm nominal hole may "
            "finish at 2.13 mm maximum, so a mounting drawing's 2.10 mm "
            "limit is overrun by 0.03 mm.  Assembly preflight compares "
            "each evidenced end of the finished-hole interval with a "
            "declared mounting drawing; a missing bound leaves that "
            "side unassessed.  Requires ``finished_hole_tolerance_source``."
        ),
    )
    finished_hole_guarantees: list[FinishedHoleGuarantee] | None = Field(
        default=None,
        description=(
            "Issue #10476: order-specific finished-hole intervals the "
            "fabricator documented for selected references.  For the "
            "holes each one covers it takes precedence over the general "
            "``finished_hole_tolerance_mm`` / ``finished_hole_tolerance_upper_mm``; "
            "other holes keep the general tolerance, and where several "
            "cover one reference the first declared applies.  Like the "
            "general tolerance it applies only to the spec's target fab, "
            "and it is read by the ``assembly_mounting_hole_fit`` preflight "
            "check."
        ),
    )

    @field_validator("copper_weight", mode="before")
    @classmethod
    def validate_copper_weight(cls, v: Any) -> float | None:
        """Parse copper weight from int, float, or oz-bearing string."""
        if v is None:
            return None
        return _parse_copper_weight_oz(v)

    @field_validator("finished_hole_tolerance_source")
    @classmethod
    def validate_finished_hole_tolerance_source(
        cls, source: str | None, info: ValidationInfo
    ) -> str | None:
        if info.data.get("finished_hole_tolerance_mm") is not None and not (source or "").strip():
            raise ValueError(
                "a declared finished-hole tolerance requires a non-empty finished_hole_tolerance_source"
            )
        return source

    @field_validator("finished_hole_tolerance_upper_mm")
    @classmethod
    def validate_finished_hole_tolerance_upper(
        cls, upper: float | None, info: ValidationInfo
    ) -> float | None:
        if (
            upper is not None
            and not (info.data.get("finished_hole_tolerance_source") or "").strip()
        ):
            raise ValueError(
                "a declared finished-hole tolerance requires a non-empty finished_hole_tolerance_source"
            )
        return upper

    @field_validator("silkscreen_min_reference_height_mm", mode="before")
    @classmethod
    def validate_reference_height(cls, v: Any) -> Any:
        """Reject a non-finite, non-numeric or too-small designator height (#9448).

        Runs before coercion so a quoted ``'0.8'`` or ``true`` is rejected,
        matching ``resolve_declared_reference_height_for_pcb``.
        """
        from kicad_tools.manufacturers.base import (
            MIN_DECLARED_REFERENCE_HEIGHT_MM,
            is_valid_declared_reference_height,
        )

        if v is not None and not is_valid_declared_reference_height(v):
            raise ValueError(
                f"silkscreen_min_reference_height_mm must be a finite number >= "
                f"{MIN_DECLARED_REFERENCE_HEIGHT_MM} mm, got {v!r}"
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def extract_copper_weight_from_layers(cls, data: Any) -> Any:
        """Promote copper_weight from layers dict to top-level field if nested."""
        if isinstance(data, dict):
            layers = data.get("layers")
            if isinstance(layers, dict) and "copper_weight" in layers:
                # Only promote if not already set at top level
                if "copper_weight" not in data or data["copper_weight"] is None:
                    data["copper_weight"] = layers.pop("copper_weight")
                else:
                    layers.pop("copper_weight")
        return data

    @model_validator(mode="before")
    @classmethod
    def extract_stackup_from_layers(cls, data: Any) -> Any:
        """Promote ``stackup`` from layers dict to top-level field if nested.

        Boards historically wrote::

            layers:
              preferred: 4
              stackup:
                - "F.Cu (signal)"
                - "In1.Cu (GND plane)"

        ``layers`` is typed ``dict[str, int]`` so a nested ``stackup`` list
        produces a Pydantic validation error. Promote it to the top-level
        ``stackup`` field for backwards compatibility.
        """
        if isinstance(data, dict):
            layers = data.get("layers")
            if isinstance(layers, dict) and "stackup" in layers:
                if "stackup" not in data or data["stackup"] is None:
                    data["stackup"] = layers.pop("stackup")
                else:
                    layers.pop("stackup")
        return data


class Compliance(SpecModel):
    """Compliance and certification requirements."""

    standards: list[str] | None = Field(
        default=None, description="Required standards (FCC, CE, etc.)"
    )
    rohs: bool = Field(default=True, description="RoHS compliance required")
    reach: bool = Field(default=False, description="REACH compliance required")
    ul: str | None = Field(default=None, description="UL certification requirements")


class Requirements(SpecModel):
    """All project requirements."""

    electrical: ElectricalRequirements | None = Field(
        default=None, description="Electrical requirements"
    )
    mechanical: MechanicalRequirements | None = Field(
        default=None, description="Mechanical requirements"
    )
    environmental: EnvironmentalRequirements | None = Field(
        default=None, description="Environmental requirements"
    )
    manufacturing: ManufacturingRequirements | None = Field(
        default=None, description="Manufacturing requirements"
    )
    compliance: Compliance | None = Field(default=None, description="Compliance requirements")


class ComponentSuggestion(SpecModel):
    """Component suggestion with preferences and rationale.

    Accepts two equivalent YAML shapes for ergonomic authoring:

    * Full form (preserves rationale/avoid)::

          regulator:
            preferred: ["LM7805", "TPS562201"]
            rationale: "Common, well-documented regulators"

    * Shorthand (bare string -> ``preferred: [<string>]``)::

          regulator: "LM7805 5V LDO"

    The shorthand is unwrapped by :meth:`unwrap_string_shorthand` so the
    canonical model still has a list-of-strings ``preferred`` field.
    """

    preferred: list[str] | None = Field(default=None, description="Preferred part numbers")
    rationale: str | None = Field(default=None, description="Reason for preference")
    avoid: list[str] | None = Field(default=None, description="Parts to avoid")

    @model_validator(mode="before")
    @classmethod
    def unwrap_string_shorthand(cls, data: Any) -> Any:
        """Allow a bare string to stand in for ``{preferred: [<string>]}``.

        This is symmetric with how the loader is permissive about
        single-element lists elsewhere -- it keeps board authors from
        having to write the full mapping for the common case of a
        one-line component note.
        """
        if isinstance(data, str):
            return {"preferred": [data]}
        return data


class BOMSuggestions(SpecModel):
    """BOM-related suggestions."""

    preferred_vendors: list[str] | None = Field(
        default=None, description="Preferred component vendors"
    )
    lifecycle_min_years: int | None = Field(
        default=None, description="Minimum lifecycle requirement in years"
    )
    cost_target: str | None = Field(default=None, description="Target BOM cost")


class LeadProfile(SpecModel):
    """Verified maximum lead dimensions for a part assignment (Issue #10299).

    Assembly preflight compares these against the *finished* plated hole
    of each matching reference's through-hole pads, so the values must be
    verified **maxima** -- taken from the part datasheet's package drawing
    or a measurement record -- and :attr:`source` must say which.  A
    round lead needs its maximum diameter; a rectangular lead needs its
    maximum width and thickness (preflight derives the required hole
    diameter from their diagonal via :meth:`required_diameter_mm`).
    """

    shape: Literal["round", "rectangular"] = Field(..., description="Lead cross-section shape")
    diameter_mm: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Maximum lead diameter for a round lead, in millimetres.",
    )
    width_mm: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Maximum lead width for a rectangular lead, in millimetres.",
    )
    thickness_mm: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Maximum lead thickness for a rectangular lead, in millimetres.",
    )
    source: str = Field(
        ...,
        description=(
            "Where the dimensions were verified: a drawing/page reference or "
            "measurement record, e.g. 'STW70N60DM2 datasheet package drawing "
            "and dimensions, pp. 9-10'"
        ),
    )

    @model_validator(mode="after")
    def _validate_dimensions(self) -> LeadProfile:
        """Each shape takes exactly its own dimensions; source must be non-blank."""
        if self.shape == "round":
            if self.diameter_mm is None:
                raise ValueError("lead shape 'round' requires diameter_mm")
            if self.width_mm is not None or self.thickness_mm is not None:
                raise ValueError(
                    "lead shape 'round' takes diameter_mm only; "
                    "width_mm/thickness_mm belong to shape 'rectangular'"
                )
        else:
            if self.width_mm is None or self.thickness_mm is None:
                raise ValueError("lead shape 'rectangular' requires width_mm and thickness_mm")
            if self.diameter_mm is not None:
                raise ValueError(
                    "lead shape 'rectangular' takes width_mm and thickness_mm; "
                    "diameter_mm belongs to shape 'round'"
                )
        if not (self.source and self.source.strip()):
            raise ValueError(
                "lead dimensions require a source (drawing/page reference or measurement record)"
            )
        return self

    def required_diameter_mm(self) -> float:
        """Diameter of the smallest round hole the centred lead can pass through.

        A round lead needs its maximum diameter; a rectangular lead needs the
        diagonal of its maximum width and thickness -- a centred rectangle
        only fits a round hole whose diameter clears that diagonal.
        """
        return float(self.required_diameter_decimal_mm())

    def required_diameter_decimal_mm(self) -> Decimal:
        """Calculate clearance against decimal hole and tolerance dimensions."""
        dimensions = [Decimal(str(x)) for x in self.max_dimensions_mm()]
        if self.shape == "round":
            return dimensions[0]
        width, thickness = dimensions
        return (width**2 + thickness**2).sqrt()

    def max_dimensions_mm(self) -> tuple[float, ...]:
        """The declared maximum dimensions, in declaration order."""
        if self.shape == "round":
            return (float(self.diameter_mm or 0.0),)
        return (float(self.width_mm or 0.0), float(self.thickness_mm or 0.0))


class SmdLandPattern(SpecModel):
    """Source-attributed two-terminal SMD land pattern for an exact BOM MPN (Issue #10505).

    Assembly preflight compares these target copper dimensions against the placed
    two-terminal SMD footprint after undoing placement rotation and front-to-back
    mirroring.  Either ``pitch_mm`` (center-to-center spacing), ``outer_span_mm``
    (outer copper-to-copper extent along the terminal axis), or both may be
    declared: for two equal pads along the terminal axis,
    ``outer_span_mm = pitch_mm + pad_depth_mm``.  ``source`` must identify the
    manufacturer drawing and page.
    """

    pad_shape: Literal["rect", "roundrect", "oval", "circle"] = Field(
        ...,
        description="Expected copper pad shape for both terminals (e.g. 'rect', 'roundrect').",
    )
    pad_depth_mm: float = Field(
        ...,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Expected pad extent along the terminal axis, in millimetres.",
    )
    pad_width_mm: float = Field(
        ...,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Expected transverse pad width perpendicular to the terminal axis, in mm.",
    )
    pitch_mm: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description=(
            "Expected center-to-center pad pitch along the terminal axis, in millimetres. "
            "Derived as ``outer_span_mm - pad_depth_mm`` when omitted."
        ),
    )
    outer_span_mm: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description=(
            "Expected outer copper-to-copper span along the terminal axis, in millimetres. "
            "Derived as ``pitch_mm + pad_depth_mm`` when omitted."
        ),
    )
    source: str = Field(
        ...,
        description=(
            "Manufacturer drawing and page reference where the land pattern was taken, "
            "e.g. 'Stackpole RHC series, page 2'."
        ),
    )

    @model_validator(mode="after")
    def _validate_land_pattern(self) -> SmdLandPattern:
        """Require non-blank source, valid span/pitch geometry and non-overlapping pads."""
        if not (self.source and self.source.strip()):
            raise ValueError(
                "SMD land pattern requires a non-empty source (manufacturer drawing and page)"
            )
        if self.pitch_mm is None and self.outer_span_mm is None:
            raise ValueError("SMD land pattern requires pitch_mm, outer_span_mm, or both")

        depth = Decimal(str(self.pad_depth_mm))
        width = Decimal(str(self.pad_width_mm))
        if self.pad_shape == "circle" and abs(depth - width) > Decimal("0.001"):
            raise ValueError(
                f"SMD land pattern with pad_shape 'circle' requires equal pad_depth_mm "
                f"({self.pad_depth_mm}) and pad_width_mm ({self.pad_width_mm})"
            )

        if self.pitch_mm is not None and self.outer_span_mm is not None:
            implied_outer = Decimal(str(self.pitch_mm)) + depth
            declared_outer = Decimal(str(self.outer_span_mm))
            if abs(declared_outer - implied_outer) > Decimal("0.001"):
                raise ValueError(
                    f"outer_span_mm ({self.outer_span_mm}) is inconsistent with "
                    f"pitch_mm + pad_depth_mm ({float(implied_outer):.3f})"
                )

        pitch = self.resolved_pitch_decimal_mm()
        if pitch <= depth:
            raise ValueError(
                "pitch_mm must exceed pad_depth_mm (outer_span_mm must exceed "
                "2 * pad_depth_mm) so the two pads do not overlap"
            )
        return self

    def resolved_pitch_decimal_mm(self) -> Decimal:
        """Exact decimal center-to-center pad pitch along the terminal axis."""
        if self.pitch_mm is not None:
            return Decimal(str(self.pitch_mm))
        return Decimal(str(self.outer_span_mm)) - Decimal(str(self.pad_depth_mm))

    def resolved_outer_span_decimal_mm(self) -> Decimal:
        """Exact decimal outer copper span along the terminal axis."""
        if self.outer_span_mm is not None:
            return Decimal(str(self.outer_span_mm))
        return Decimal(str(self.pitch_mm)) + Decimal(str(self.pad_depth_mm))

    def resolved_pitch_mm(self) -> float:
        """Center-to-center pad pitch along the terminal axis, in millimetres."""
        return float(self.resolved_pitch_decimal_mm())

    def resolved_outer_span_mm(self) -> float:
        """Outer copper span along the terminal axis, in millimetres."""
        return float(self.resolved_outer_span_decimal_mm())


class MountingHoleDeclaration(SpecModel):
    """A manufacturer's PC-board mounting-hole pattern for a part assignment (Issue #10476).

    A snap-in or similar termination is specified by the finished holes its
    package drawing requires, not by a lead a hole must clear.  The
    declaration names the two numbered plated through-hole pads, the
    inclusive finished-hole diameter limits and -- when the drawing gives
    them -- the inclusive limits of the two holes' centre-to-centre pitch.
    :attr:`source` must cite the drawing.  Assembly preflight compares the
    nominal drill and each evidenced end of the fabricator's finished-hole
    interval with the diameter limits, and the nominal pitch between the
    two holes with the pitch limits, independently of ``lead``.
    """

    pads: list[str] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="The two KiCad pad numbers (e.g. ['1', '2']) whose holes the drawing specifies.",
    )
    hole_diameter: DimensionLimits = Field(
        ..., description="Allowed finished-hole diameter of each pad, in millimetres (inclusive)."
    )
    pitch: DimensionLimits | None = Field(
        default=None,
        description=(
            "Allowed centre-to-centre distance of the two holes, in millimetres "
            "(inclusive).  Omit it when the drawing gives no pitch tolerance; "
            "the pitch is then not assessed."
        ),
    )
    source: str = Field(
        ...,
        description="Where the hole pattern was verified: the package drawing's document and page.",
    )

    @field_validator("pads", mode="before")
    @classmethod
    def _normalize_pad_numbers(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [str(item) if type(item) is int else item for item in value]

    @field_validator("pads")
    @classmethod
    def _validate_pads(cls, pads: list[str]) -> list[str]:
        if any(not isinstance(pad, str) or not pad.strip() for pad in pads):
            raise ValueError("mounting_holes pads must be two non-empty pad numbers")
        if pads[0] == pads[1]:
            raise ValueError("mounting_holes requires two distinct pad numbers")
        return pads

    @field_validator("source")
    @classmethod
    def _validate_source(cls, source: str) -> str:
        if not source.strip():
            raise ValueError("mounting_holes requires a source (drawing document and page)")
        return source


class BOMEntry(SpecModel):
    """Per-reference BOM mapping for explicit part assignment.

    Allows the project spec to declare exact MPN and LCSC part numbers
    for specific reference designators, overriding whatever the schematic
    symbol properties may (or may not) contain.

    The ``ref`` field accepts single references (``"U1"``) or dash-separated
    ranges (``"Q1-Q4"``) which are expanded into individual designators.
    """

    ref: str = Field(..., description="Reference designator or range (e.g. 'U1' or 'Q1-Q4')")
    part: str = Field(..., description="Manufacturer Part Number (MPN)")
    source: str = Field(default="LCSC", description="Supplier name (e.g. 'LCSC', 'Digikey')")
    lcsc: str | None = Field(default=None, description="Explicit LCSC part number if known")
    exempt: Literal["consigned", "hand_solder", "dnp_by_intent"] | None = Field(
        default=None,
        description=(
            "Declare this ref deliberately not an LCSC-sourced part so the "
            "unverifiable-LCSC gate (tapeout Gate 4) accepts it (issue #8261)"
        ),
    )
    exempt_reason: str | None = Field(
        default=None, description="Why the ref is exempt (required when 'exempt' is set)"
    )
    lead: LeadProfile | None = Field(
        default=None,
        description=(
            "Issue #10299: verified maximum lead dimensions for this part "
            "assignment, compared by assembly preflight against the finished "
            "plated hole of every matching reference's through-hole pads."
        ),
    )
    pad_leads: dict[str, LeadProfile] | None = Field(
        default=None,
        description=(
            "Issue #10299: per-pad-number lead overrides for packages with "
            "mixed leads, keyed by KiCad pad number (e.g. '2').  Takes "
            "precedence over ``lead`` for that pad number only."
        ),
    )
    land_pattern: SmdLandPattern | None = Field(
        default=None,
        description=(
            "Issue #10505: source-attributed manufacturer SMD land pattern for "
            "this exact BOM MPN, compared by assembly preflight against placed "
            "two-terminal SMD copper pads."
        ),
    )
    mounting_holes: MountingHoleDeclaration | None = Field(
        default=None,
        description=(
            "Issue #10476: the package drawing's mounting-hole pattern for "
            "this part assignment.  Assembly preflight checks each matching "
            "reference's declared plated holes against it, independently of "
            "``lead``."
        ),
    )

    @field_validator("pad_leads", mode="before")
    @classmethod
    def normalize_pad_numbers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = {}
        for key, profile in value.items():
            if type(key) is int:
                key = str(key)
            if not isinstance(key, str) or key in normalized:
                raise ValueError("pad numbers must be unique strings or integers")
            normalized[key] = profile
        return normalized

    @model_validator(mode="after")
    def _validate_exemption(self) -> BOMEntry:
        if self.exempt is None:
            if self.exempt_reason:
                raise ValueError(f"bom_entries '{self.ref}': exempt_reason set without exempt")
            return self
        if not (self.exempt_reason and self.exempt_reason.strip()):
            raise ValueError(
                f"bom_entries '{self.ref}': exempt='{self.exempt}' requires a non-empty exempt_reason"
            )
        if self.lcsc:
            raise ValueError(
                f"bom_entries '{self.ref}': an exempt entry must not set lcsc "
                f"(a part with an LCSC number is verified, not exempted)"
            )
        return self


class PurchasingEntry(SpecModel):
    """One supplementary procurement row: something to buy that is not placed (Issue #10431).

    Accessories and off-board parts a complete assembly needs but that have no
    KiCad placement of their own -- a fuse-holder cover, a cartridge bought
    separately, chassis resistors wired to land footprints. They never enter the
    fabricator BOM or CPL; the manufacturing export writes them beside those as a
    per-board purchasing list, which the submission handoff scales by the board
    quantity.
    """

    description: str = Field(..., description="What to buy")
    quantity: int = Field(
        ...,
        description="Units per board; already covers every linked designator",
    )
    refs: list[str] | None = Field(
        default=None,
        description="Board designators the item belongs to (wire lands included); omit for none",
    )
    manufacturer: str | None = Field(default=None, description="Manufacturer of the selected part")
    mpn: str | None = Field(
        default=None,
        description="Manufacturer Part Number of the selected part; quote it when it starts with a digit",
    )
    pending: str | None = Field(
        default=None,
        description="Set to what is still undecided instead of manufacturer/mpn: the part is not selected yet",
    )

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("purchasing rows need a non-blank description")
        return value

    @field_validator("quantity", mode="before")
    @classmethod
    def _quantity_not_boolean(cls, value: Any) -> Any:
        if type(value) is bool:
            raise ValueError("quantity must be a positive whole number of units per board")
        return value

    @field_validator("quantity")
    @classmethod
    def _quantity_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("quantity must be a positive whole number of units per board")
        return value

    @field_validator("refs")
    @classmethod
    def _refs_not_blank(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        refs = []
        for ref in value:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("refs must list non-blank designators")
            ref = ref.strip()
            if ref in refs:
                raise ValueError(f"refs must not repeat a designator: {ref}")
            refs.append(ref)
        return refs

    @field_validator("manufacturer", "mpn", "pending")
    @classmethod
    def _optional_text_not_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError(f"{info.field_name} must be non-blank when set")
        return value

    @model_validator(mode="after")
    def _selection_is_complete(self) -> PurchasingEntry:
        if self.pending is not None:
            if self.manufacturer is not None or self.mpn is not None:
                raise ValueError("a pending row must not set manufacturer or mpn")
            return self
        if self.manufacturer is None or self.mpn is None:
            raise ValueError(
                "a selected row needs both manufacturer and mpn; "
                "otherwise pending must say what is still to be selected"
            )
        return self


class Suggestions(SpecModel):
    """Design suggestions and preferences (SHOULD, not MUST)."""

    components: dict[str, ComponentSuggestion] | None = Field(
        default=None, description="Component suggestions by function"
    )
    layout: list[str] | None = Field(default=None, description="Layout suggestions")
    bom: BOMSuggestions | None = Field(default=None, description="BOM suggestions")
    notes: list[str] | None = Field(default=None, description="General design notes")


class Decision(SpecModel):
    """Design decision with rationale."""

    topic: str = Field(..., description="Decision topic")
    choice: str = Field(..., description="Chosen option")
    rationale: str = Field(..., description="Reasoning for the decision")
    date: datetime.date | None = Field(default=None, description="Decision date")
    phase: DesignPhase | str | None = Field(
        default=None, description="Design phase when decision was made"
    )
    alternatives: list[str] | None = Field(
        default=None, description="Alternative options considered"
    )
    author: str | None = Field(default=None, description="Who made the decision")


class PhaseProgress(SpecModel):
    """Progress tracking for a single phase."""

    status: PhaseStatus = Field(default=PhaseStatus.PENDING, description="Phase status")
    started: datetime.date | None = Field(default=None, description="Start date")
    completed: datetime.date | None = Field(default=None, description="Completion date")
    checklist: list[str] | None = Field(
        default=None, description="Checklist items with [x] or [ ] prefix"
    )
    notes: str | None = Field(default=None, description="Phase notes")


class Progress(SpecModel):
    """Overall project progress tracking."""

    phase: DesignPhase | str = Field(
        default=DesignPhase.CONCEPT, description="Current design phase"
    )
    phases: dict[str, PhaseProgress] | None = Field(default=None, description="Progress by phase")
    blockers: list[str] | None = Field(default=None, description="Current blockers")


class ValidationResult(SpecModel):
    """Single validation result."""

    status: str = Field(..., description="pass, fail, warning, pending")
    errors: int = Field(default=0, description="Error count")
    warnings: int = Field(default=0, description="Warning count")
    details: list[str] | None = Field(default=None, description="Detailed messages")


class Validation(SpecModel):
    """Validation results from design tools."""

    last_run: datetime.datetime | None = Field(
        default=None, description="Last validation timestamp"
    )
    schematic: dict[str, ValidationResult] | None = Field(
        default=None, description="Schematic validation (ERC, etc.)"
    )
    layout: dict[str, ValidationResult] | None = Field(
        default=None, description="Layout validation (DRC, etc.)"
    )
    bom: dict[str, ValidationResult] | None = Field(
        default=None, description="BOM validation (availability, etc.)"
    )


class ProjectSpec(SpecModel):
    """Complete project specification (.kct file)."""

    kct_version: str = Field(default="1.0", description="KCT format version")
    project: ProjectMetadata = Field(..., description="Project metadata")
    build: BuildConfig | None = Field(
        default=None, description="Build-pipeline configuration (route recipe, etc.)"
    )
    intent: DesignIntent | None = Field(default=None, description="Design intent")
    requirements: Requirements | None = Field(default=None, description="Requirements")
    suggestions: Suggestions | None = Field(default=None, description="Suggestions")
    bom_entries: list[BOMEntry] | None = Field(
        default=None,
        description="Per-reference BOM part mappings (MPN, LCSC)",
    )
    purchasing: list[PurchasingEntry] | None = Field(
        default=None,
        description="Supplementary per-board procurement rows for accessories and off-board parts",
    )
    decisions: list[Decision] | None = Field(default=None, description="Design decisions")
    progress: Progress | None = Field(default=None, description="Progress tracking")
    validation: Validation | None = Field(default=None, description="Validation results")

    @field_validator("kct_version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        """Validate KCT format version."""
        supported = ["1.0"]
        if v not in supported:
            raise ValueError(f"Unsupported kct_version: {v}. Supported: {supported}")
        return v

    def get_completion_percentage(self) -> float:
        """Calculate overall completion percentage based on checklists."""
        if not self.progress or not self.progress.phases:
            return 0.0

        total_items = 0
        completed_items = 0

        for phase in self.progress.phases.values():
            if phase.checklist:
                for item in phase.checklist:
                    total_items += 1
                    if item.startswith("[x]") or item.startswith("[X]"):
                        completed_items += 1

        return (completed_items / total_items * 100) if total_items > 0 else 0.0

    def get_current_phase_progress(self) -> PhaseProgress | None:
        """Get progress for the current phase."""
        if not self.progress or not self.progress.phases:
            return None
        phase_key = (
            self.progress.phase.value
            if isinstance(self.progress.phase, DesignPhase)
            else self.progress.phase
        )
        return self.progress.phases.get(phase_key)
