"""
Pre-flight validation checklist for manufacturing export.

Validates PCB design readiness before generating manufacturing packages.
Each check returns OK/WARN/FAIL status. Any FAIL blocks the export.

Example::

    checker = PreflightChecker(
        pcb_path="board.kicad_pcb",
        schematic_path="board.kicad_sch",
        manufacturer="jlcpcb",
    )
    results = checker.run_all()
    for r in results:
        print(f"[{r.status}] {r.name}: {r.message}")

    if checker.has_failures(results):
        print("Export blocked!")
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .bom_spec_overlay import AssemblySpecErrors
from .pnp import _ref_sort_key, is_through_hole_footprint

if TYPE_CHECKING:
    from ..drc.violation import DRCViolation
    from ..manufacturers.base import AssemblyCapabilities, ManufacturerProfile
    from ..schema.pcb import PCB, Footprint, Pad
    from ..spec.schema import (
        BOMEntry,
        LeadProfile,
        ProjectSpec,
        SmdLandPattern,
        WireTermination,
    )

logger = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    """Result of a single pre-flight check."""

    name: str
    status: Literal["OK", "WARN", "FAIL"]
    message: str
    details: str = ""
    # Optional machine-readable record (Issue #8807): ``bom_lcsc_stock`` stores
    # its per-part observations so a later ``--verify`` can re-evaluate them,
    # and (Issue #10386) where on this host its lookups reached.
    data: dict | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        d: dict = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.details:
            d["details"] = self.details
        if self.data is not None:
            d["data"] = self.data
        return d


def _describe_drc_finding(v: DRCViolation) -> str:
    """One-line description of a DRC finding for preflight ``details``.

    KiCad's ``unconnected_items`` description is a generic "Missing
    connection between items", so opens also name the items they join.
    """
    from ..drc.violation import ViolationType

    text = f"{v.type_str}: {v.message}"
    if v.type == ViolationType.UNCONNECTED_ITEMS and v.items:
        text += f" ({' / '.join(v.items[:2])})"
    return text


def report_source_mismatch(
    recorded_source: str | None,
    target: Path | str | None,
    *,
    require_suffix: str | None = None,
) -> str | None:
    """Return the report's recorded source basename when it names another file.

    A DRC/ERC report records which board/schematic it was generated for
    (kicad-cli ``source``, kct-check ``file``, the text-report header).  A
    report generated for a *different* file -- e.g. the ``*_routed.kicad_pcb``
    sibling's ``drc_report.json`` sitting next to the unrouted board -- is no
    evidence about the file being exported (issue #7091).

    Only basenames are compared: recorded sources may be absolute paths from
    another host or cwd-relative (#6532), and may use either path separator.

    Args:
        recorded_source: The source path recorded inside the report.
        target: The file the caller is checking (PCB or schematic).
        require_suffix: When given, only compare if the recorded source ends
            with this suffix (case-insensitive).  ``ERCReport.source_file``
            falls back to the report's *own* path when the JSON has no
            ``source``, so the ERC caller passes ``".kicad_sch"`` to avoid
            mistaking that fallback for a recorded source.

    Returns:
        The recorded source's basename when it differs from ``target``'s,
        else ``None`` (match, no recorded source, or nothing to compare).
    """
    if not recorded_source or target is None:
        return None
    recorded_name = re.split(r"[\\/]", recorded_source.strip())[-1]
    if not recorded_name:
        return None
    if require_suffix is not None and not recorded_name.lower().endswith(require_suffix.lower()):
        return None
    target_name = Path(target).name
    if recorded_name == target_name:
        return None
    return recorded_name


# Relative tolerance: JLCPCB publishes finished 0.5 oz inner copper as 0.0152 mm
# (0.434 oz at 0.035 mm/oz), https://jlcpcb.com/impedance.  15% still keeps
# 0.5 / 1 / 2 oz distinct.
_COPPER_OZ_REL_TOL = 0.15


def _oz_close(a: float, b: float) -> bool:
    return abs(a - b) <= _COPPER_OZ_REL_TOL * b


def _fmt_oz(oz: float | None) -> str:
    return "n/a" if oz is None else f"{oz:g} oz"


def resolve_ordered_copper(pcb: PCB, manufacturer: str) -> dict:
    """Compare the board's declared stackup copper with what the fab provides.

    Issue #7209: the ampacity gate reads copper weight from the board stackup,
    while the order is built from the fab profile.  This reconciles the two.

    Returns a dict with ``layers``, ``stackup_outer_oz`` / ``stackup_inner_oz``
    (``None`` when the board declares no usable stackup), ``profile_options``
    (list of ``[outer, inner]`` the fab offers for this layer count),
    ``ordered_outer_oz`` / ``ordered_inner_oz`` (the matched option, else
    ``None``) and ``status``: ``match`` | ``mismatch`` | ``undeclared`` |
    ``unknown``.  Never raises.
    """
    info: dict = {
        "layers": None,
        "stackup_outer_oz": None,
        "stackup_inner_oz": None,
        "profile_options": [],
        "ordered_outer_oz": None,
        "ordered_inner_oz": None,
        "status": "unknown",
    }
    try:
        from ..manufacturers import get_profile
        from ..physics.stackup import Stackup

        layers = len(pcb.copper_layers)
        info["layers"] = layers
        profile = get_profile(manufacturer)
        options = profile.copper_options(layers)
        info["profile_options"] = [list(o) for o in options]
        derived = Stackup.from_pcb(pcb).outer_inner_copper_oz()
    except Exception as e:  # noqa: BLE001 - preflight must not crash on a stackup quirk
        logger.debug("Could not resolve stackup copper: %s", e)
        return info

    if not options:
        return info  # profile has no entry for this layer count: nothing to compare
    if derived is None or (derived[0] is None and derived[1] is None):
        # Profile default applies: the first entry is what gets ordered.
        info["ordered_outer_oz"], info["ordered_inner_oz"] = options[0]
        info["status"] = "undeclared"
        return info

    outer, inner = derived
    info["stackup_outer_oz"], info["stackup_inner_oz"] = outer, inner
    for opt_outer, opt_inner in options:
        outer_ok = outer is None or _oz_close(outer, opt_outer)
        # opt_inner == 0 means the profile entry has no inner layers (2-layer).
        inner_ok = inner is None or opt_inner <= 0 or _oz_close(inner, opt_inner)
        if outer_ok and inner_ok:
            info["ordered_outer_oz"], info["ordered_inner_oz"] = opt_outer, opt_inner
            info["status"] = "match"
            return info
    info["status"] = "mismatch"
    return info


# ---------------------------------------------------------------------------
# Through-hole lead fit (Issue #10299)
# ---------------------------------------------------------------------------

# Reported margins are rounded to 3 decimals for display; pass/fail verdicts
# use the unrounded values so a -0.0004 mm interference still fails.
_LEAD_MARGIN_DECIMALS = 3


def _lead_record(profile: LeadProfile) -> dict:
    """Serialized form of a declared lead profile (dimensions + source)."""
    rec: dict = {"shape": profile.shape, "source": profile.source}
    if profile.shape == "round":
        rec["diameter_mm"] = profile.diameter_mm
    else:
        rec["width_mm"] = profile.width_mm
        rec["thickness_mm"] = profile.thickness_mm
    return rec


def _wire_record(wire: WireTermination) -> dict:
    """Serialized form of a declared wire termination (prepared diameter + source)."""
    return {"diameter_mm": wire.diameter_mm, "source": wire.source}


def _lead_fit_finding(
    reference: str,
    pad: Pad,
    entry: BOMEntry | None,
    profile: LeadProfile | None,
    tolerance_mm: float | None,
    tolerance_source: str | None,
    tolerance_reason: str,
    profile_error: str | None,
    wire: WireTermination | None = None,
) -> dict:
    """Build one per-pad lead-fit record (Issue #10299).

    The drill geometry is read independently of the copper pad outline:
    ``pad.drill`` is the round finished-hole diameter and
    ``pad.drill_size`` holds both slot axes.  A negative nominal margin
    fails nominal fit; a negative margin at minimum finish fails fit at
    tolerance even when the nominal margin is positive.  Margins are
    computed at full precision (required diameter subtracted from the
    opening diameter) and rounded for reporting; the verdicts use the
    unrounded values.

    A declared wire termination (Issue #10473) is assessed the same way
    from the maximum outside diameter of its prepared end: *profile* is
    the round profile built from the declaration, ``mpn`` stays unset
    because no part describes the pad, and the declaration is carried in
    ``wire`` instead of ``lead``.
    """
    mpn = entry.part if entry is not None else None
    lead_record = _lead_record(profile) if profile is not None and wire is None else None
    wire_record = _wire_record(wire) if wire is not None else None

    # A slot hole: identify it with both axes and stop -- a slot's long
    # axis must not be mistaken for a round hole diameter.
    if pad.drill_size is not None:
        slot_w, slot_h = pad.drill_size
        return {
            "reference": reference,
            "pad": pad.number,
            "mpn": mpn,
            "hole": {"shape": "slot", "size_mm": [slot_w, slot_h]},
            "lead": lead_record,
            "wire": wire_record,
            "status": "unassessed",
            "reason": (
                f"slot hole {slot_w:.{_LEAD_MARGIN_DECIMALS}f} x "
                f"{slot_h:.{_LEAD_MARGIN_DECIMALS}f} mm; only round holes are assessed"
            ),
        }

    hole_mm = float(pad.drill)
    hole: dict = {"shape": "round", "diameter_mm": hole_mm}
    if hole_mm <= 0:
        return {
            "reference": reference,
            "pad": pad.number,
            "mpn": mpn,
            "hole": hole,
            "lead": lead_record,
            "wire": wire_record,
            "status": "unassessed",
            "reason": "through-hole pad has no drill diameter",
        }

    if profile is None:
        if profile_error:
            reason = profile_error
        elif wire is not None:
            reason = (
                f"wire termination declared for pad {pad.number} of {reference} "
                "without a prepared diameter"
            )
        elif entry is None:
            reason = (
                f"no verified lead dimensions: reference {reference} has no "
                "bom_entries assignment in the project spec"
            )
        else:
            reason = f"no verified lead dimensions declared for pad {pad.number} of {mpn}"
        return {
            "reference": reference,
            "pad": pad.number,
            "mpn": mpn,
            "hole": hole,
            "lead": None,
            "wire": wire_record,
            "status": "unassessed",
            "reason": reason,
        }

    # Preserve decimal fabrication dimensions through the tolerance addition.
    diameter = Decimal(str(hole_mm))
    required_mm = profile.required_diameter_decimal_mm()
    nominal_margin = diameter - required_mm
    finding: dict = {
        "reference": reference,
        "pad": pad.number,
        "mpn": mpn,
        "hole": hole,
        "lead": lead_record,
        "wire": wire_record,
        "required_diameter_mm": round(float(required_mm), _LEAD_MARGIN_DECIMALS),
        "nominal_margin_mm": round(float(nominal_margin), _LEAD_MARGIN_DECIMALS),
        "nominal_status": "fail" if nominal_margin < 0 else "pass",
    }

    if tolerance_mm is None:
        # Valid lead data, unknown hole tolerance: report the nominal fit
        # and mark the tolerance result unassessed.
        finding["tolerance"] = {
            "status": "unassessed",
            "reason": tolerance_reason,
        }
        finding["status"] = "fail" if nominal_margin < 0 else "pass"
        return finding

    minimum_hole_mm = diameter + Decimal(str(tolerance_mm))
    minimum_margin = minimum_hole_mm - required_mm
    finding["tolerance"] = {
        "status": "fail" if minimum_margin < 0 else "pass",
        "tolerance_mm": tolerance_mm,
        "source": tolerance_source,
        "minimum_hole_mm": round(float(minimum_hole_mm), _LEAD_MARGIN_DECIMALS),
        "minimum_margin_mm": round(float(minimum_margin), _LEAD_MARGIN_DECIMALS),
    }
    finding["status"] = "fail" if (nominal_margin < 0 or minimum_margin < 0) else "pass"
    return finding


def _format_lead_finding(f: dict) -> str:
    """One ``details`` line per pad, carrying the full per-pad evidence."""
    mpn = f.get("mpn")
    tag = f"{f['reference']} pad {f['pad']}" + (f" [{mpn}]" if mpn else "")

    if f["status"] == "unassessed":
        return f"{tag}: unassessed: {f['reason']}"

    hole = f["hole"]
    hole_desc = (
        f"hole {hole['diameter_mm']:.{_LEAD_MARGIN_DECIMALS}f} mm round"
        if hole["shape"] == "round"
        else (
            f"slot {hole['size_mm'][0]:.{_LEAD_MARGIN_DECIMALS}f} x "
            f"{hole['size_mm'][1]:.{_LEAD_MARGIN_DECIMALS}f} mm"
        )
    )
    wire = f.get("wire")
    lead = f["lead"] or {}
    if wire is not None:
        lead_desc = f"wire termination dia {wire['diameter_mm']:.{_LEAD_MARGIN_DECIMALS}f} mm"
        source = wire.get("source")
    elif lead.get("shape") == "round":
        lead_desc = f"round lead dia {lead['diameter_mm']:.{_LEAD_MARGIN_DECIMALS}f} mm"
        source = lead.get("source")
    else:
        lead_desc = (
            f"rectangular lead {lead['width_mm']:.{_LEAD_MARGIN_DECIMALS}f} x "
            f"{lead['thickness_mm']:.{_LEAD_MARGIN_DECIMALS}f} mm"
        )
        source = lead.get("source")
    src = f" per '{source}'" if source else ""
    head = (
        f"{tag}: {hole_desc} vs {lead_desc} "
        f"(needs {f['required_diameter_mm']:.{_LEAD_MARGIN_DECIMALS}f} mm dia){src}"
    )

    margin_fmt = f".{_LEAD_MARGIN_DECIMALS}f"
    seg = f"nominal margin {f['nominal_margin_mm']:+{margin_fmt}} mm ({f['nominal_status']})"
    tol = f["tolerance"]
    if tol["status"] == "unassessed":
        seg += f"; tolerance unassessed: {tol['reason']}"
    else:
        seg += (
            f"; tolerance {tol['tolerance_mm']:+{margin_fmt}} mm"
            + (f" per '{tol['source']}'" if tol.get("source") else "")
            + f" -> min hole {tol['minimum_hole_mm']:{margin_fmt}} mm, "
            f"min margin {tol['minimum_margin_mm']:+{margin_fmt}} mm ({tol['status']})"
        )
    return f"{head}; {seg}"


def _lead_finding_sort_key(f: dict) -> tuple[int, tuple, tuple[int, int, str]]:
    """Failures first, then unassessed, then passes; ref/pad order within."""
    order = {"fail": 0, "unassessed": 1, "pass": 2}
    pad_no = str(f["pad"])
    pad_key: tuple[int, int, str] = (0, int(pad_no), "") if pad_no.isdigit() else (1, 0, pad_no)
    return (order.get(f["status"], 3), _ref_sort_key(f["reference"]), pad_key)


# ---------------------------------------------------------------------------
# SMD two-terminal land-pattern verification (Issue #10505)
# ---------------------------------------------------------------------------

# Measurements are reported to 3 decimals (0.001 mm = 1 um).  Dimensional
# equality uses +/-0.001 mm (1 um) against exact Decimal target values after
# 9-decimal normalization: this absorbs KiCad 4-decimal coordinate rounding and
# floating-point trig noise across rotated/mirrored placements while catching
# any genuine copper land-pattern discrepancy (>= 2 um).
_SMD_DIM_DECIMALS = 3
_SMD_DIM_TOLERANCE_MM = 0.001
_SMD_DIM_TOLERANCE_DEC = Decimal("0.001")
_SMD_MIN_PITCH_MM = 1e-6
_SMD_AXIS_ALIGN_TOL = 1e-3  # ~0.057 deg max deviation from terminal/transverse axes
_SUPPORTED_SMD_PAD_SHAPES = frozenset({"rect", "roundrect", "oval", "circle"})


def _pad_number_sort_key(pad_no: str) -> tuple[int, int, str]:
    """Sort numeric pad numbers numerically, then non-numeric lexicographically."""
    text = str(pad_no)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def _smd_expected_record(pattern: SmdLandPattern) -> dict:
    """Serialized form of a declared two-terminal SMD land pattern."""
    return {
        "pad_shape": pattern.pad_shape,
        "pad_depth_mm": round(float(pattern.pad_depth_mm), _SMD_DIM_DECIMALS),
        "pad_width_mm": round(float(pattern.pad_width_mm), _SMD_DIM_DECIMALS),
        "pitch_mm": round(float(pattern.resolved_pitch_decimal_mm()), _SMD_DIM_DECIMALS),
        "outer_span_mm": round(float(pattern.resolved_outer_span_decimal_mm()), _SMD_DIM_DECIMALS),
        "source": pattern.source,
    }


def _smd_pattern_signature(pattern: SmdLandPattern) -> tuple:
    """Canonical key for checking whether two rows declare the same pattern."""
    return (
        pattern.pad_shape,
        Decimal(str(pattern.pad_depth_mm)),
        Decimal(str(pattern.pad_width_mm)),
        pattern.resolved_pitch_decimal_mm(),
        pattern.resolved_outer_span_decimal_mm(),
        pattern.source.strip(),
    )


def _copper_smd_pads(fp: Footprint) -> list[Pad]:
    """Return copper-bearing SMD pads on *fp* (ignoring paste/mask-only pads)."""
    return [
        p
        for p in fp.pads
        if p.type == "smd" and any(str(layer).endswith(".Cu") for layer in (p.layers or ()))
    ]


def _measure_two_terminal_smd_copper(
    fp: Footprint,
) -> tuple[dict | None, dict | None, str | None]:
    """Measure a placed two-terminal SMD footprint after undoing rotation and flip.

    KiCad stores ``pad.position`` in footprint-local coordinates (already
    y-mirrored on ``B.Cu``) and ``pad.rotation`` in the board-absolute frame
    (already including ``fp.rotation``).  Subtracting ``fp.rotation`` recovers
    the footprint-local pad angle, and negating local ``y`` and local angle for
    ``B.Cu`` placements undoes front-to-back mirroring.

    Returns ``(rounded_measured, raw_measured, error_reason)``.
    """
    from ..core.geometry import rotate_pad_offset

    pads = _copper_smd_pads(fp)
    if len(pads) != 2:
        return (
            None,
            None,
            f"expected 2 copper SMD pads, found {len(pads)}; "
            "only two-terminal SMD chips are assessed",
        )

    p1, p2 = sorted(
        pads,
        key=lambda p: (_pad_number_sort_key(p.number), p.position[0], p.position[1]),
    )
    cu1 = {str(layer) for layer in p1.layers if str(layer).endswith(".Cu")}
    cu2 = {str(layer) for layer in p2.layers if str(layer).endswith(".Cu")}
    if not (cu1 & cu2) and "*.Cu" not in cu1 and "*.Cu" not in cu2:
        return (
            None,
            None,
            f"copper SMD pads {p1.number} ({', '.join(sorted(cu1))}) and "
            f"{p2.number} ({', '.join(sorted(cu2))}) are on different copper layers",
        )

    for pad in (p1, p2):
        if pad.shape not in _SUPPORTED_SMD_PAD_SHAPES:
            return (
                None,
                None,
                f"pad {pad.number} has unsupported shape '{pad.shape}'; "
                "only rect, roundrect, oval and circle pads are assessed",
            )
        if pad.size[0] <= 0 or pad.size[1] <= 0:
            return (
                None,
                None,
                f"pad {pad.number} has non-positive size ({pad.size[0]}, {pad.size[1]}) mm",
            )

    is_back = fp.layer == "B.Cu"

    def _undo_placement(pad: Pad) -> tuple[float, float, float]:
        lx, ly = float(pad.position[0]), float(pad.position[1])
        local_rot = float(pad.rotation) - float(fp.rotation)
        if is_back:
            return (lx, -ly, -local_rot)
        return (lx, ly, local_rot)

    ux1, uy1, rot1 = _undo_placement(p1)
    ux2, uy2, rot2 = _undo_placement(p2)

    dx = ux2 - ux1
    dy = uy2 - uy1
    pitch = math.hypot(dx, dy)
    if pitch < _SMD_MIN_PITCH_MM:
        return (None, None, "copper SMD pads have coincident centers; terminal axis is undefined")

    # Unit vectors along the terminal axis (u) and transverse axis (t).
    ux, uy = dx / pitch, dy / pitch
    tx, ty = -uy, ux

    raw_pads: list[dict] = []
    rounded_pads: list[dict] = []
    for pad, pad_rot in ((p1, rot1), (p2, rot2)):
        w, h = float(pad.size[0]), float(pad.size[1])
        if pad.shape == "circle":
            if abs(Decimal(f"{w:.9f}") - Decimal(f"{h:.9f}")) > _SMD_DIM_TOLERANCE_DEC:
                return (
                    None,
                    None,
                    f"circular pad {pad.number} has unequal axes ({w:.3f} x {h:.3f} mm)",
                )
            depth, width = w, h
        else:
            # Pad's size[0] axis in the unrotated, unmirrored footprint frame.
            wx, wy = rotate_pad_offset(1.0, 0.0, pad_rot)
            dot_u = abs(wx * ux + wy * uy)
            dot_t = abs(wx * tx + wy * ty)
            if dot_t <= _SMD_AXIS_ALIGN_TOL:
                depth, width = w, h
            elif dot_u <= _SMD_AXIS_ALIGN_TOL:
                depth, width = h, w
            else:
                return (
                    None,
                    None,
                    f"pad {pad.number} is not axis-aligned with the terminal axis "
                    f"(relative rotation {pad_rot:.2f} deg)",
                )
        raw_pads.append(
            {"pad": pad.number, "shape": pad.shape, "depth_mm": depth, "width_mm": width}
        )
        rounded_pads.append(
            {
                "pad": pad.number,
                "shape": pad.shape,
                "depth_mm": round(depth, _SMD_DIM_DECIMALS),
                "width_mm": round(width, _SMD_DIM_DECIMALS),
            }
        )

    # Outer copper span along the terminal axis.
    s1 = ux1 * ux + uy1 * uy
    s2 = ux2 * ux + uy2 * uy
    d1 = raw_pads[0]["depth_mm"]
    d2 = raw_pads[1]["depth_mm"]
    outer_span = max(s1 + d1 / 2.0, s2 + d2 / 2.0) - min(s1 - d1 / 2.0, s2 - d2 / 2.0)

    raw = {"pads": raw_pads, "pitch_mm": pitch, "outer_span_mm": outer_span}
    rounded = {
        "pads": rounded_pads,
        "pitch_mm": round(pitch, _SMD_DIM_DECIMALS),
        "outer_span_mm": round(outer_span, _SMD_DIM_DECIMALS),
    }
    return (rounded, raw, None)


def _dim_matches(measured_mm: float, expected_mm: Decimal) -> bool:
    """Compare a measured copper dimension against an exact target within +/-0.001 mm."""
    return abs(Decimal(f"{measured_mm:.9f}") - expected_mm) <= _SMD_DIM_TOLERANCE_DEC


def _smd_land_pattern_finding(
    fp: Footprint,
    mpn: str | None,
    pattern: SmdLandPattern | None,
    pattern_error: str | None,
) -> dict:
    """Build one per-component SMD land-pattern finding (Issue #10505)."""
    measured, raw, geom_error = _measure_two_terminal_smd_copper(fp)
    expected = _smd_expected_record(pattern) if pattern is not None else None
    source = pattern.source if pattern is not None else None

    if pattern is None or geom_error is not None or raw is None:
        reasons = [r for r in (pattern_error, geom_error) if r]
        return {
            "reference": fp.reference,
            "mpn": mpn,
            "footprint": fp.name,
            "source": source,
            "expected": expected,
            "measured": measured,
            "mismatched_fields": [],
            "status": "unassessed",
            "reason": "; ".join(reasons) if reasons else "unassessed",
        }

    expected_depth = Decimal(str(pattern.pad_depth_mm))
    expected_width = Decimal(str(pattern.pad_width_mm))
    expected_pitch = pattern.resolved_pitch_decimal_mm()
    expected_outer = pattern.resolved_outer_span_decimal_mm()

    mismatched: list[str] = []
    for pad_rec in raw["pads"]:
        pad_id = pad_rec["pad"]
        if pad_rec["shape"] != pattern.pad_shape:
            mismatched.append(f"pad {pad_id} shape")
        if not _dim_matches(pad_rec["depth_mm"], expected_depth):
            mismatched.append(f"pad {pad_id} depth_mm")
        if not _dim_matches(pad_rec["width_mm"], expected_width):
            mismatched.append(f"pad {pad_id} width_mm")
    if not _dim_matches(raw["pitch_mm"], expected_pitch):
        mismatched.append("pitch_mm")
    if not _dim_matches(raw["outer_span_mm"], expected_outer):
        mismatched.append("outer_span_mm")

    return {
        "reference": fp.reference,
        "mpn": mpn,
        "footprint": fp.name,
        "source": source,
        "expected": expected,
        "measured": measured,
        "mismatched_fields": mismatched,
        "status": "fail" if mismatched else "pass",
    }


def _format_measured_smd(measured: dict | None) -> str:
    """Format measured copper geometry for one-line details output."""
    if measured is None:
        return "measured geometry unavailable"
    fmt = f".{_SMD_DIM_DECIMALS}f"
    pad_bits = ", ".join(
        f"pad {p['pad']} {p['shape']} {p['depth_mm']:{fmt}} x {p['width_mm']:{fmt}} mm"
        for p in measured["pads"]
    )
    return (
        f"{pad_bits}, pitch {measured['pitch_mm']:{fmt}} mm, "
        f"outer span {measured['outer_span_mm']:{fmt}} mm"
    )


def _format_smd_land_pattern_finding(f: dict) -> str:
    """One ``details`` line per component carrying full evidence."""
    mpn = f.get("mpn")
    tag = f"{f['reference']}" + (f" [{mpn}]" if mpn else "")
    if f["status"] == "unassessed":
        measured_text = (
            f" (measured: {_format_measured_smd(f['measured'])})"
            if f.get("measured") is not None
            else ""
        )
        return f"{tag}: unassessed: {f['reason']}{measured_text}"

    exp = f["expected"]
    fmt = f".{_SMD_DIM_DECIMALS}f"
    src = f" per '{f['source']}'" if f.get("source") else ""
    verdict = (
        f"fail (mismatches: {', '.join(f['mismatched_fields'])})"
        if f["status"] == "fail"
        else "pass"
    )
    return (
        f"{tag}: {verdict}{src}; "
        f"expected {exp['pad_shape']} {exp['pad_depth_mm']:{fmt}} x {exp['pad_width_mm']:{fmt}} mm, "
        f"pitch {exp['pitch_mm']:{fmt}} mm, outer span {exp['outer_span_mm']:{fmt}} mm; "
        f"measured {_format_measured_smd(f['measured'])}"
    )


def _smd_finding_sort_key(f: dict) -> tuple[int, tuple]:
    """Failures first, then unassessed, then passes; reference order within."""
    order = {"fail": 0, "unassessed": 1, "pass": 2}
    return (order.get(f["status"], 3), _ref_sort_key(f["reference"]))


# ---------------------------------------------------------------------------
# Assembly package/pitch parsing (Issue #7204)
# ---------------------------------------------------------------------------

# KiCad chip lib ids carry imperial and metric codes: ``R_0402_1005Metric``.
# The imperial code is 4-6 digits so ``R_008004_0201Metric`` (008004) parses.
_CHIP_METRIC_RE = re.compile(r"_(\d{4,6})_(\d{4})Metric", re.IGNORECASE)
# Pitch token in KiCad lib ids: ``LQFP-48_7x7mm_P0.5mm``.
_PITCH_RE = re.compile(r"_P(\d+(?:\.\d+)?)mm", re.IGNORECASE)
# BGA-style packages: BGA, WLCSP and bare CSP tokens (``CSP-BGA``).  LFCSP is a
# leadframe (QFN-like) package, so a CSP preceded by a letter does not match.
_BGA_RE = re.compile(r"BGA|WLCSP|(?<![A-Za-z])CSP", re.IGNORECASE)

# Imperial chip code -> body length in mm (the leading digits of the metric
# code: 0402 imperial == 1005 metric == 1.0 mm long).
_IMPERIAL_CHIP_LENGTH_MM = {
    "01005": 0.4,
    "0201": 0.6,
    "0402": 1.0,
    "0603": 1.6,
    "0805": 2.0,
    "1206": 3.2,
    "1210": 3.2,
    "2010": 5.0,
    "2512": 6.3,
}
_PITCH_EPS = 1e-6
# Pitch comparisons allow 0.01 mm: lib-id pitches are nominal metric values
# and some fabs quote limits in mil (FlashPCB's "20 mil" = 0.508 mm is meant
# to admit 0.5 mm-pitch QFPs), so a sub-0.01 mm gap is a unit-rounding
# artefact, not a capability difference.
_PITCH_TOLERANCE_MM = 0.01


@dataclass(frozen=True)
class _AssemblyRequirement:
    """One placement constraint a CPL part puts on the assembly service."""

    reference: str
    kind: Literal["package", "ic_pitch", "bga_pitch"]
    value: float  # chip body length (package) or pitch, in mm
    label: str  # e.g. "0201" or "0.35 mm"
    measured: bool = False  # pitch measured from pads, not read from lib id

    def fits(self, tier: AssemblyCapabilities) -> bool:
        if self.kind == "package":
            if not tier.min_package:
                return True
            min_len = _IMPERIAL_CHIP_LENGTH_MM.get(tier.min_package)
            return min_len is None or self.value >= min_len - _PITCH_EPS
        if self.kind == "bga_pitch":
            return (
                bool(tier.supports_bga)
                and self.value >= tier.min_bga_pitch_mm - _PITCH_TOLERANCE_MM
            )
        return self.value >= tier.min_component_pitch_mm - _PITCH_TOLERANCE_MM

    def describe(self) -> str:
        what = {"package": "package", "ic_pitch": "pin pitch", "bga_pitch": "BGA pitch"}
        text = f"{self.reference} {what[self.kind]} {self.label}"
        return text + " (measured)" if self.measured else text


def _measured_pitch_mm(pads) -> float | None:
    """Minimum centre spacing between same-size copper SMD pads.

    Fallback for lib ids without a ``_P<pitch>mm`` token.  Stacked/coincident
    pads (< 0.05 mm apart) and paste-only apertures are ignored.  Returns
    ``None`` for parts with fewer than 4 such pads (chips, SOT-23).
    """
    copper = [
        p
        for p in pads
        if getattr(p, "type", "") == "smd"
        and any(str(layer).endswith(".Cu") for layer in (getattr(p, "layers", None) or ()))
    ]
    if len(copper) < 4:
        return None
    best: float | None = None
    for i, a in enumerate(copper):
        for b in copper[i + 1 :]:
            if (round(a.size[0], 3), round(a.size[1], 3)) != (
                round(b.size[0], 3),
                round(b.size[1], 3),
            ) and (round(a.size[0], 3), round(a.size[1], 3)) != (
                round(b.size[1], 3),
                round(b.size[0], 3),
            ):
                continue
            d = ((a.position[0] - b.position[0]) ** 2 + (a.position[1] - b.position[1]) ** 2) ** 0.5
            if d < 0.05:
                continue
            if best is None or d < best:
                best = d
    return best


def _requirements_for_footprint(reference: str, lib_id: str, pads) -> list[_AssemblyRequirement]:
    """Derive package/pitch requirements from a footprint's lib id (or pads)."""
    reqs: list[_AssemblyRequirement] = []
    fp_name = lib_id.split(":", 1)[-1]
    chip = _CHIP_METRIC_RE.search(fp_name)
    if chip:
        imperial, metric = chip.group(1), chip.group(2)
        reqs.append(_AssemblyRequirement(reference, "package", int(metric[:2]) / 10.0, imperial))
        return reqs

    is_bga = bool(_BGA_RE.search(fp_name))
    # The "minimum IC pin spacing" limit is about ICs: connector libraries
    # (FFC/FPC, headers) are not judged against it.
    is_connector = lib_id.lower().startswith("connector")
    if is_connector and not is_bga:
        return reqs
    kind: Literal["ic_pitch", "bga_pitch"] = "bga_pitch" if is_bga else "ic_pitch"
    pitch_match = _PITCH_RE.search(fp_name)
    if pitch_match:
        pitch = float(pitch_match.group(1))
        reqs.append(_AssemblyRequirement(reference, kind, pitch, f"{pitch:g} mm"))
        return reqs
    measured = _measured_pitch_mm(pads)
    if measured is not None:
        reqs.append(
            _AssemblyRequirement(reference, kind, measured, f"{measured:.2f} mm", measured=True)
        )
    return reqs


@dataclass
class PreflightConfig:
    """Configuration for which pre-flight checks to run."""

    skip_all: bool = False
    skip_drc: bool = False
    skip_erc: bool = False

    # When True, disables the connectivity safety floor -- the hard abort on
    # net shorts / connectivity errors read from a pre-existing DRC report
    # that fires even when ``skip_all`` is set.  This is an explicit escape
    # hatch for extraordinary, known-safe workarounds; it should almost never
    # be used because shipping a shorted board is not safe.  See
    # ``ManufacturingPackage._check_drc_safety_floor``.
    skip_drc_floor: bool = False

    # Paths to pre-existing report files (avoids re-running kicad-cli)
    drc_report_path: str | Path | None = None
    erc_report_path: str | Path | None = None

    # When True, a DRC/ERC report recorded for a different PCB/schematic is a
    # FAIL instead of a WARN (issue #7091).  ``ManufacturingPackage`` sets this
    # from ``strict_preflight`` so strict exports never ship on a verdict that
    # was computed for another board.
    strict_report_source: bool = False

    # PCBA service tier the package/pitch limits are judged against (Issue
    # #7204): ``"auto"`` (default) judges against the fab's cheapest tier and
    # reports the cheapest tier the board fits; a tier name (``"economic"``,
    # ``"standard"``; matched case-insensitively against the tier's leading
    # word) judges against that tier.  Fabs without published tiers ignore it.
    assembly_tier: str = "auto"

    # Boards ordered, for the ``bom_lcsc_stock`` check (Issue #8121): each
    # assigned LCSC part needs ``placements per board x order_boards`` in
    # stock.  Defaults to JLCPCB's minimum PCBA order.
    order_boards: int = 5

    # LCSC catalog ids the operator holds in their own parts inventory
    # (Issue #8807).  Upper-case; excluded from the stock shortfall verdict and
    # listed as "held in operator inventory, not stock-verified".  Explicit
    # only: never inferred.
    inventory_covered: frozenset[str] = frozenset()


def parse_inventory_covered(raw: str | Iterable[str] | None) -> frozenset[str]:
    """Parse ``--inventory-covered`` ids: comma-separated, upper-cased, blanks dropped."""
    if raw is None:
        return frozenset()
    parts = (
        raw.split(",") if isinstance(raw, str) else [p for chunk in raw for p in chunk.split(",")]
    )
    return frozenset(p.strip().upper() for p in parts if p.strip())


def _describe_stock_record(rec: dict, boards: int) -> str:
    needed = int(rec["per_board"]) * boards
    if rec.get("verified"):
        when = "live"
    else:
        age = rec.get("age_days")
        when = f"unverified {rec.get('source', 'unknown')}" + (
            f", {age}d old" if age is not None else ""
        )
    return f"needs {needed}, stock {rec['stock']} ({when})"


# Where the ``bom_lcsc_stock`` lookups reached on the generating host
# (issue #10386).  Every assigned LCSC number is searched in the parts cache
# and then, when the cache does not settle it, in the offline catalog;
# ``data["sources"]`` folds those per-number outcomes into one state per
# source.  That lets a reader of recorded evidence tell a host that could
# not check -- no catalog file, or one it could not read -- from a catalog
# that was searched and does not list the part.
LOOKUP_SOURCE_CACHE = "parts_cache"
LOOKUP_SOURCE_CATALOG = "offline_catalog"

# Fold order: the first state a run contains wins, so a query that failed is
# never summarised as a search that found nothing.
_LOOKUP_STATE_ORDER: dict[str, tuple[str, ...]] = {
    LOOKUP_SOURCE_CACHE: ("unreadable", "hit", "miss", "not_consulted"),
    LOOKUP_SOURCE_CATALOG: ("unreadable", "hit", "missing", "miss", "not_consulted"),
}

_LOOKUP_SOURCE_LABEL = {
    LOOKUP_SOURCE_CACHE: "parts cache",
    LOOKUP_SOURCE_CATALOG: "offline catalog",
}

_LOOKUP_STATE_TEXT = {
    "hit": "searched, match found",
    "miss": "searched, no matching row",
    "missing": "not available on the generating host (no catalog file)",
    "unreadable": "could not be read on the generating host",
    "not_consulted": "not consulted",
}


def summarize_lookup_states(source: str, states: Iterable[str]) -> str:
    """Fold the per-number lookup states of one source into the run's state."""
    seen = set(states)
    return next((s for s in _LOOKUP_STATE_ORDER[source] if s in seen), "not_consulted")


def describe_lookup_sources(sources: object) -> str:
    """Render a recorded ``sources`` block; empty when nothing was recorded.

    ``data["sources"]`` is absent from bundles recorded before issue #10386
    and from runs that assigned no LCSC number; those make no claim here.
    """
    if not isinstance(sources, dict):
        return ""
    described = []
    for key, label in _LOOKUP_SOURCE_LABEL.items():
        if key in sources:
            state = str(sources[key])
            described.append(f"{label} {_LOOKUP_STATE_TEXT.get(state, state)}")
    return "; ".join(described)


def evaluate_lcsc_stock(
    data: dict, boards: int, covered: frozenset[str], strict: bool
) -> PreflightResult:
    """Judge recorded per-part stock observations (Issue #8807).

    The one verdict function behind both ``kct export``/``readiness --generate``
    (via ``_check_bom_lcsc_stock``) and ``readiness --verify`` (which re-runs it
    on the manifest's recorded ``data``), so a shortfall reads identically in
    both.  ``data`` holds ``parts`` (resolved LCSC ids with ``refs``,
    ``per_board``, ``stock``, ``verified``, ``source``, ``age_days``) and
    ``unresolved`` (ids in neither the cache nor the catalog).  When it also
    holds ``sources`` (issue #10386), "Stock not verified" says what became
    of the lookups the producer made; without it the message is unchanged.
    """
    name = "bom_lcsc_stock"
    boards = max(1, int(boards))
    parts = sorted(data.get("parts", []), key=lambda r: r["lcsc"])
    unresolved = sorted(data.get("unresolved", []))
    covered = frozenset(c.upper() for c in covered)
    in_bom = {r["lcsc"] for r in parts} | set(unresolved)

    live_short: list[str] = []
    soft_short: list[str] = []
    held: list[str] = []
    not_short: list[str] = []
    for rec in parts:
        is_short = rec["stock"] < int(rec["per_board"]) * boards
        if rec["lcsc"] in covered:
            if is_short:
                held.append(rec["lcsc"])
            else:
                not_short.append(rec["lcsc"])
            continue
        if not is_short:
            continue
        refs = sorted(rec["refs"])
        shown = ",".join(refs[:3]) + ("..." if len(refs) > 3 else "")
        entry = f"{shown} -> {rec['lcsc']}: {_describe_stock_record(rec, boards)}"
        (live_short if rec.get("verified") else soft_short).append(entry)
    held.extend(u for u in unresolved if u in covered)
    unused = sorted(covered - in_bom)

    def _fmt(entries: list[str]) -> str:
        suffix = f" (and {len(entries) - 5} more)" if len(entries) > 5 else ""
        return "; ".join(entries[:5]) + suffix

    uncovered_unresolved = [u for u in unresolved if u not in covered]
    notes: list[str] = []
    if uncovered_unresolved and (live_short or soft_short):
        notes.append("stock also unresolved: " + ", ".join(uncovered_unresolved))
    if held:
        notes.append("held in operator inventory, not stock-verified: " + ", ".join(sorted(held)))
    if not_short:
        notes.append(
            "inventory-covered but not short (stale --inventory-covered entry): "
            + ", ".join(sorted(not_short))
        )
    if unused:
        notes.append("inventory-covered but not in the BOM: " + ", ".join(unused))
    note_text = "; ".join(notes)
    stale = bool(not_short or unused)

    def _result(
        status: Literal["OK", "WARN", "FAIL"], message: str, details: str
    ) -> PreflightResult:
        if note_text:
            details = f"{details}; {note_text}" if details else note_text
        return PreflightResult(
            name=name, status=status, message=message, details=details, data=data
        )

    order = f"{boards} board(s)"
    if live_short:
        return _result(
            "FAIL",
            f"{len(live_short)} LCSC part(s) lack live stock for {order}"
            + (f"; {len(soft_short)} more short on unverified stock" if soft_short else ""),
            _fmt(live_short + soft_short),
        )
    if soft_short:
        return _result(
            "FAIL" if strict else "WARN",
            f"{len(soft_short)} LCSC part(s) show insufficient stock for {order} "
            f"on unverified data (stale cache/offline catalog)"
            + (" (strict preflight)" if strict else ""),
            _fmt(soft_short),
        )
    checked = len(parts)
    if uncovered_unresolved:
        looked = describe_lookup_sources(data.get("sources"))
        return _result(
            "FAIL" if strict else "WARN",
            f"Stock not verified: {len(uncovered_unresolved)} assigned LCSC number(s) found in "
            f"neither the parts cache nor the offline catalog"
            + (f"; {checked} other part(s) have recorded stock" if checked else "")
            + (f"; {looked}" if looked else "")
            + (" (strict preflight)" if strict else ""),
            ", ".join(uncovered_unresolved[:10]),
        )
    return _result(
        "WARN" if stale else "OK",
        f"{checked} LCSC part(s) have recorded stock for {order}"
        + (f"; {len(held)} held in operator inventory" if held else ""),
        "",
    )


class PreflightChecker:
    """
    Run pre-flight validation checks before manufacturing export.

    Validates PCB file integrity, schematic availability, board outline,
    component footprints, BOM fields, DRC status, and board dimensions.

    Example::

        checker = PreflightChecker(
            pcb_path="board.kicad_pcb",
            schematic_path="board.kicad_sch",
            manufacturer="jlcpcb",
        )
        results = checker.run_all()
        if checker.has_failures(results):
            print("Cannot proceed with export")
    """

    # Fab *families* whose standard assembly service is SMT-only.
    #
    # Compared against ``self._fab_family`` (NOT the raw ``--mfr`` value):
    # capability tiers such as ``jlcpcb-tier1`` share the parent fab's
    # assembly service and CPL format, so they must inherit the parent's
    # THT policy.  See ``get_fab_family()`` and issue #3497.
    #
    # Currently empty: JLCPCB's assembly capability page lists
    # "(SMT/Thru-hole)" for both its Economic and Standard PCBA tiers
    # (https://jlcpcb.com/capabilities/pcb-assembly-capabilities, fetched
    # 2026-10-18), so JLCPCB CPLs include THT parts (issue #7239).  Add a fab
    # family here to make it auto-exclude THT again.
    _SMT_ONLY_MANUFACTURERS: set[str] = set()

    @classmethod
    def fab_excludes_tht(cls, manufacturer: str) -> bool:
        """True when *manufacturer*'s fab family auto-excludes THT from the CPL.

        Public read of the SMT-only fab-family set, so callers outside
        preflight (e.g. ``kct readiness``) share this single source of truth
        without reaching into the private attribute.  Capability tiers
        resolve to their parent fab family (``jlcpcb-tier1`` -> ``jlcpcb``).
        """
        from ..manufacturers import get_fab_family

        return get_fab_family(manufacturer.lower()) in cls._SMT_ONLY_MANUFACTURERS

    def __init__(
        self,
        pcb_path: str | Path,
        schematic_path: str | Path | None = None,
        manufacturer: str = "jlcpcb",
        output_dir: str | Path | None = None,
        config: PreflightConfig | None = None,
        exclude_tht: bool | None = None,
        bom_source: str = "schematic",
        parts_cache=None,
        parts_catalog=None,
        merge_lcsc: bool = True,
        merge_lcsc_from: str | Path | None = None,
        spec_path: Path | None = None,
        no_spec: bool = False,
    ):
        self.spec_path = spec_path
        self.no_spec = no_spec
        self.pcb_path = Path(pcb_path)
        # LCSC merge source, mirroring AssemblyConfig (issue #8260).
        self._merge_lcsc = merge_lcsc
        self._merge_lcsc_from = Path(merge_lcsc_from) if merge_lcsc_from else None
        self.schematic_path = Path(schematic_path) if schematic_path else None
        self.manufacturer = manufacturer.lower()
        self.output_dir = Path(output_dir) if output_dir else None
        self.config = config or PreflightConfig()
        self._bom_source = bom_source
        # Shared by the CSV merge and the LCSC value and stock checks.
        self._parts_cache = parts_cache
        # Optional injected JlcpartsCatalog (offline fallback for parts the
        # cache does not know, issue #7166).  Default: the catalog file that
        # sits beside the parts cache's ``parts.db``.
        self._parts_catalog = parts_catalog

        # Resolve the fab *family* once.  Export-format concerns (BOM/CPL
        # formatters, THT policy) are properties of the physical fab, not of
        # a capability tier: ``jlcpcb-tier1`` -> ``jlcpcb``.  The CPL writer
        # already resolves the family (``assembly.py`` -> ``get_fab_family``),
        # so preflight must use the same key or its THT predictions desync
        # from the file that actually ships (issue #4591).  DRC limits keep
        # using the *full* manufacturer ID -- see ``_get_manufacturer_limits``.
        try:
            from ..manufacturers import get_fab_family

            self._fab_family = get_fab_family(self.manufacturer)
        except Exception:  # pragma: no cover - defensive; resolver is pure
            self._fab_family = self.manufacturer

        # Determine THT exclusion: explicit override, or default per fab family
        if exclude_tht is not None:
            self._exclude_tht = exclude_tht
        else:
            self._exclude_tht = self._fab_family in self._SMT_ONLY_MANUFACTURERS

        # Lazy-loaded objects
        self._pcb = None
        self._bom = None
        self._assembly_profile_cache: ManufacturerProfile | None = None
        self._assembly_profile_resolved = False

    def run_all(self) -> list[PreflightResult]:
        """Run all applicable pre-flight checks.

        Returns:
            List of PreflightResult, one per check.
        """
        if self.config.skip_all:
            return []

        results: list[PreflightResult] = []

        # Always check PCB file first -- most other checks depend on it
        results.append(self._check_pcb_parseable())

        # Schematic check (needed for BOM/CPL)
        results.append(self._check_schematic_exists())

        # Schematic footprint completeness (fail-loud before export).
        # A footprint-less schematic symbol can never be placed/assembled,
        # so this is a hard gate (issue #3866).
        if self.schematic_path and self.schematic_path.exists():
            results.append(self._check_schematic_footprints())

        # Board outline
        if self._pcb is not None:
            results.append(self._check_board_outline_closed())
            results.append(self._check_footprints_present())
            results.append(self._check_board_dimensions())
            results.append(self._check_drill_holes())
            # Through-hole lead fit against the finished plated hole
            # (Issue #10299).  Runs whenever the board loads: pads without
            # verified lead dimensions report *unassessed* rather than
            # passing silently.
            results.append(self._check_assembly_lead_fit())
            # MPN-specific two-terminal SMD land-pattern check (Issue #10505).
            results.append(self._check_assembly_smd_land_pattern())
            results.append(self._check_stackup_copper())

        # THT-in-CPL warning
        if self._pcb is not None:
            results.append(self._check_tht_in_cpl())

        # Assembly-capability checks against the fab's PCBA service tier(s)
        # (Issue #7204).  Skipped entirely for PCB-only fabs (no assembly).
        if self._pcb is not None and self._assembly_tiers():
            results.append(self._check_bottom_side_in_cpl())
            results.append(self._check_assembly_package_limits())

        # BOM checks: run if schematic is available OR bom_source is "pcb"/"auto"
        can_load_bom = (
            self.schematic_path and self.schematic_path.exists()
        ) or self._bom_source in ("pcb", "auto")
        if can_load_bom:
            results.append(self._check_bom_fields())
            results.append(self._check_bom_lcsc_values())
            results.append(self._check_bom_lcsc_stock())
            results.append(self._check_bom_footprint_match())
            if self._pcb is not None:
                results.append(self._check_bom_cpl_match())

        # DRC check
        if not self.config.skip_drc:
            results.append(self._check_drc())

        # ERC check
        if not self.config.skip_erc:
            results.append(self._check_erc())

        return results

    @staticmethod
    def has_failures(results: list[PreflightResult]) -> bool:
        """Check if any result is a FAIL."""
        return any(r.status == "FAIL" for r in results)

    @staticmethod
    def has_warnings(results: list[PreflightResult]) -> bool:
        """Check if any result is a WARN."""
        return any(r.status == "WARN" for r in results)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_pcb_parseable(self) -> PreflightResult:
        """Verify the PCB file exists and can be parsed."""
        if not self.pcb_path.exists():
            return PreflightResult(
                name="pcb_file",
                status="FAIL",
                message=f"PCB file not found: {self.pcb_path}",
            )

        try:
            from ..schema.pcb import PCB

            self._pcb = PCB.load(str(self.pcb_path))
            return PreflightResult(
                name="pcb_file",
                status="OK",
                message="PCB file parsed successfully",
                details=f"Footprints: {self._pcb.footprint_count}",
            )
        except Exception as e:
            return PreflightResult(
                name="pcb_file",
                status="FAIL",
                message=f"PCB file could not be parsed: {e}",
            )

    def _check_schematic_exists(self) -> PreflightResult:
        """Check that the schematic file exists (needed for BOM/CPL).

        When ``bom_source`` is ``"pcb"``, the schematic is not required
        for BOM generation, so a missing schematic is downgraded to an
        informational OK status instead of WARN.
        """
        if self.schematic_path is None:
            # Try auto-detection using shared find_schematic logic
            from kicad_tools.report.utils import find_schematic

            auto_sch = find_schematic(self.pcb_path)
            if auto_sch is not None:
                self.schematic_path = auto_sch
                return PreflightResult(
                    name="schematic_file",
                    status="OK",
                    message=f"Schematic auto-detected: {auto_sch.name}",
                )
            if self._bom_source == "pcb":
                return PreflightResult(
                    name="schematic_file",
                    status="OK",
                    message="No schematic file (BOM sourced from PCB footprints)",
                )
            return PreflightResult(
                name="schematic_file",
                status="WARN",
                message="No schematic file specified or auto-detected; BOM/CPL generation may fail",
            )

        if not self.schematic_path.exists():
            if self._bom_source == "pcb":
                return PreflightResult(
                    name="schematic_file",
                    status="OK",
                    message=f"Schematic not found: {self.schematic_path.name} (BOM sourced from PCB footprints)",
                )
            return PreflightResult(
                name="schematic_file",
                status="WARN",
                message=f"Schematic file not found: {self.schematic_path}",
                details="BOM and CPL generation will be skipped",
            )

        return PreflightResult(
            name="schematic_file",
            status="OK",
            message=f"Schematic file found: {self.schematic_path.name}",
        )

    def _check_schematic_footprints(self) -> PreflightResult:
        """Fail loud when a component is genuinely missing a footprint.

        Walks the schematic hierarchy via the shared
        ``iter_missing_footprint_symbols`` predicate (same power/DNP skip
        rules as ``sch validate``) to find symbols with a blank or ``~``
        schematic Footprint field, then **reconciles each one against the
        routed PCB**.

        Unlike the pure-schematic ``sch validate`` gate (which has no PCB
        to consult), this is an *export* gate running on a routed PCB that
        is about to be manufactured. A blank schematic Footprint field is
        only a real, export-blocking defect when the component will not be
        built — i.e. it is absent from the PCB, or present on the PCB but
        itself missing a footprint. A schematic-blank field whose component
        IS placed on the PCB with a concrete footprint is manufacturable
        and must NOT block export (e.g. generators that build the PCB with
        footprints but never write them back into the schematic — see
        board-05 and #3869). Without this reconciliation, a footprint-less
        symbol could silently drop out of the PCB and its nets never route
        (issue #3866).
        """
        if self.schematic_path is None:
            return PreflightResult(
                name="schematic_footprints",
                status="OK",
                message="No schematic to check for missing footprints",
            )

        try:
            from kicad_tools.cli.sch_footprint_common import (
                iter_missing_footprint_symbols,
            )

            sch_missing = [
                sym for _node, sym, _sch in iter_missing_footprint_symbols(self.schematic_path)
            ]
        except Exception as e:
            return PreflightResult(
                name="schematic_footprints",
                status="WARN",
                message=f"Could not check schematic footprints: {e}",
            )

        if not sch_missing:
            return PreflightResult(
                name="schematic_footprints",
                status="OK",
                message="All schematic symbols have a footprint assigned",
            )

        # Reconcile against the PCB: a schematic-blank footprint is only an
        # export blocker when the component is not actually built. Map each
        # PCB footprint reference to whether it carries a concrete library
        # footprint name.
        pcb_fp_has_name: dict[str, bool] = {}
        if self._pcb is not None:
            for fp in self._pcb.footprints:
                ref = (getattr(fp, "reference", "") or "").strip()
                if not ref:
                    continue
                has_name = bool((getattr(fp, "name", "") or "").strip())
                # A reference appearing more than once is footprinted if any
                # instance carries a name.
                pcb_fp_has_name[ref] = pcb_fp_has_name.get(ref, False) or has_name

        genuinely_missing: list[str] = []
        reconciled: list[str] = []
        for sym in sch_missing:
            ref = (getattr(sym, "reference", "") or "").strip()
            label = ref or (getattr(sym, "lib_id", "") or "")
            if self._pcb is None:
                # No PCB to reconcile against -- fall back to the
                # schematic-only judgement (fail loud).
                genuinely_missing.append(label)
                continue
            if ref and pcb_fp_has_name.get(ref):
                # Placed on the PCB with a footprint -> manufacturable.
                reconciled.append(label)
            else:
                # Absent from the PCB, or on the PCB without a footprint.
                genuinely_missing.append(label)

        if genuinely_missing:
            refs = ", ".join(genuinely_missing[:10])
            suffix = (
                f" (and {len(genuinely_missing) - 10} more)" if len(genuinely_missing) > 10 else ""
            )
            return PreflightResult(
                name="schematic_footprints",
                status="FAIL",
                message=(
                    f"{len(genuinely_missing)} component(s) missing a footprint "
                    "on the PCB (not manufacturable)"
                ),
                details=(
                    f"References: {refs}{suffix}. "
                    "These components are absent from the PCB or placed without "
                    "a footprint. Run "
                    "`kct sch assign-footprints <schematic> --assign-missing` "
                    "to auto-assign standard parts; unknown parts must be "
                    "assigned by hand before export."
                ),
            )

        # Every schematic-blank symbol is placed on the PCB with a
        # footprint -> manufacturable. Surface the reconciliation as OK.
        detail = ""
        if reconciled:
            shown = ", ".join(reconciled[:10])
            suffix = f" (and {len(reconciled) - 10} more)" if len(reconciled) > 10 else ""
            detail = (
                f"{len(reconciled)} symbol(s) have a blank schematic Footprint "
                f"field but are placed on the PCB with a footprint: {shown}{suffix}"
            )
        return PreflightResult(
            name="schematic_footprints",
            status="OK",
            message="All components have a footprint on the PCB",
            details=detail,
        )

    def _check_board_outline_closed(self) -> PreflightResult:
        """Verify the board outline on Edge.Cuts is a closed polygon."""
        if self._pcb is None:
            return PreflightResult(
                name="board_outline",
                status="FAIL",
                message="Cannot check board outline: PCB not loaded",
            )

        outline = self._pcb.get_board_outline()
        if not outline:
            # Distinguish "no Edge.Cuts content" from "content but not extractable"
            edge_cuts_count = sum(
                1
                for item in self._pcb.graphic_items
                if hasattr(item, "layer") and item.layer == "Edge.Cuts"
            )
            if edge_cuts_count > 0:
                message = (
                    f"Edge.Cuts layer has {edge_cuts_count} graphic element(s) "
                    "but no closed board outline could be extracted"
                )
            else:
                message = "No board outline found on Edge.Cuts layer"
            return PreflightResult(
                name="board_outline",
                status="FAIL",
                message=message,
                details="A closed board outline is required for manufacturing",
            )

        # Check if outline is closed (first point ~= last point)
        if len(outline) < 3:
            return PreflightResult(
                name="board_outline",
                status="FAIL",
                message="Board outline has fewer than 3 points",
                details=f"Found {len(outline)} points; a valid outline needs at least 3",
            )

        first = outline[0]
        last = outline[-1]
        closed = self._pcb._points_close(first, last)

        if not closed:
            return PreflightResult(
                name="board_outline",
                status="FAIL",
                message="Board outline is not closed",
                details=(
                    f"Gap between first point ({first[0]:.3f}, {first[1]:.3f}) "
                    f"and last point ({last[0]:.3f}, {last[1]:.3f})"
                ),
            )

        return PreflightResult(
            name="board_outline",
            status="OK",
            message=f"Board outline is closed ({len(outline)} points)",
        )

    def _check_footprints_present(self) -> PreflightResult:
        """Check that all components have footprints."""
        if self._pcb is None:
            return PreflightResult(
                name="footprints",
                status="FAIL",
                message="Cannot check footprints: PCB not loaded",
            )

        count = self._pcb.footprint_count
        if count == 0:
            return PreflightResult(
                name="footprints",
                status="WARN",
                message="No footprints found in PCB",
                details="The board has no placed components",
            )

        # Check for footprints with empty or placeholder footprint names
        missing_name = []
        for fp in self._pcb.footprints:
            if not fp.name or fp.name.strip() == "":
                missing_name.append(fp.reference)

        if missing_name:
            refs = ", ".join(missing_name[:10])
            suffix = f" (and {len(missing_name) - 10} more)" if len(missing_name) > 10 else ""
            return PreflightResult(
                name="footprints",
                status="WARN",
                message=f"{len(missing_name)} footprint(s) missing library footprint name",
                details=f"References: {refs}{suffix}",
            )

        return PreflightResult(
            name="footprints",
            status="OK",
            message=f"All {count} footprints have library footprint names",
        )

    def _check_board_dimensions(self) -> PreflightResult:
        """Check that board dimensions are within manufacturer limits."""
        if self._pcb is None:
            return PreflightResult(
                name="board_dimensions",
                status="FAIL",
                message="Cannot check dimensions: PCB not loaded",
            )

        size = self._board_size_mm()
        if size is None:
            return PreflightResult(
                name="board_dimensions",
                status="WARN",
                message="Cannot determine board dimensions: no outline found",
            )
        width, height = size

        # Get manufacturer limits
        max_w, max_h = self._get_manufacturer_limits()

        details = f"Board size: {width:.2f} x {height:.2f} mm"

        if width > max_w or height > max_h:
            return PreflightResult(
                name="board_dimensions",
                status="FAIL",
                message=f"Board exceeds manufacturer limits ({max_w:.0f} x {max_h:.0f} mm)",
                details=details,
            )

        if width < 5.0 or height < 5.0:
            return PreflightResult(
                name="board_dimensions",
                status="WARN",
                message="Board is very small (< 5 mm on one side)",
                details=details,
            )

        return PreflightResult(
            name="board_dimensions",
            status="OK",
            message=f"Board dimensions within limits ({width:.2f} x {height:.2f} mm)",
        )

    def _board_size_mm(self) -> tuple[float, float] | None:
        """Board outline bounding box (width, height) in mm, or ``None``."""
        if self._pcb is None:
            return None
        try:
            outline = self._pcb.get_board_outline()
        except Exception:
            return None
        if not outline:
            return None
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        return (max(xs) - min(xs), max(ys) - min(ys))

    def _check_drill_holes(self) -> PreflightResult:
        """Check that the board has drill holes (vias or through-hole pads)."""
        if self._pcb is None:
            return PreflightResult(
                name="drill_holes",
                status="FAIL",
                message="Cannot check drill holes: PCB not loaded",
            )

        via_count = 0
        th_pad_count = 0

        # Count vias
        try:
            vias = list(self._pcb.vias)
            via_count = len(vias)
        except (AttributeError, TypeError):
            pass

        # Count through-hole pads on footprints
        for fp in self._pcb.footprints:
            for pad in fp.pads:
                if pad.type in ("thru_hole", "through_hole"):
                    th_pad_count += 1

        total = via_count + th_pad_count

        if total == 0:
            # A board with no drill holes is unusual but not necessarily wrong
            return PreflightResult(
                name="drill_holes",
                status="WARN",
                message="No drill holes found (no vias or through-hole pads)",
                details="Verify this is an all-SMD design with no mounting holes",
            )

        return PreflightResult(
            name="drill_holes",
            status="OK",
            message=f"Drill holes present ({via_count} vias, {th_pad_count} TH pads)",
        )

    # ------------------------------------------------------------------
    # Through-hole lead fit (Issue #10299)
    # ------------------------------------------------------------------

    def _load_project_spec(self) -> tuple[ProjectSpec | None, AssemblySpecErrors]:
        """Use the BOM export's spec selection and retain per-profile errors."""
        from .bom_spec_overlay import find_spec_file, load_assembly_spec

        if self.no_spec:
            return None, AssemblySpecErrors()
        try:
            path = self.spec_path or find_spec_file(self.pcb_path.parent)
            if path is not None:
                return load_assembly_spec(path)
        except (ImportError, OSError, ValueError) as exc:
            logger.warning("Project spec load failed: %s", exc)
        return None, AssemblySpecErrors()

    def _check_assembly_lead_fit(self) -> PreflightResult:
        """Compare declared maximum lead dimensions with the finished hole.

        Issue #10299: a designer declares *verified* maximum lead
        dimensions for a part assignment (``bom_entries`` ``lead``, or a
        ``pad_leads`` override per pad number) plus the fabricator's lower
        finished-hole tolerance
        (``requirements.manufacturing.finished_hole_tolerance_mm``), and
        this check compares them with the placed footprint's plated
        component holes.  For circular holes it reports the centred
        geometric fit -- nominal margin (finished diameter minus required
        diameter) and, when the tolerance is declared, the margin at
        minimum finish.  A negative margin is a confirmed interference
        and fails preflight.  Missing lead data, slot holes and an
        unknown tolerance are reported as unassessed, never as a fit;
        one pad's missing data never hides another pad's failure.

        Issue #10473: a pad named by a ``wire_terminations`` declaration
        is assessed by the maximum outside diameter of the prepared wire
        or ferrule end that enters it instead of its part's lead, so a
        wire land needs no ``bom_entries`` MPN.  Sibling pads keep the
        component lead lookup.

        Vias (not pads), SMD pads and non-plated holes are excluded.  The
        result describes a centred geometric fit only -- one input to
        reviewing placement and lead-pitch tolerances.
        """
        name = "assembly_lead_fit"
        if self._pcb is None:
            return PreflightResult(
                name=name,
                status="OK",
                message="No PCB loaded; through-hole lead fit not assessed",
                data={"pads": [], "summary": {"pads": 0}},
            )

        spec, spec_errors = self._load_project_spec()
        tolerance_reason = (
            spec_errors.tolerance
            or "no finished-hole tolerance declared in requirements.manufacturing"
        )
        tolerance_mm: float | None = None
        tolerance_source: str | None = None
        entries: dict[str, tuple[int, BOMEntry]] = {}
        wires: dict[tuple[str, str], list[tuple[int, WireTermination]]] = {}
        if spec is not None:
            mfg = spec.requirements.manufacturing if spec.requirements else None
            if mfg is not None:
                from ..manufacturers import get_fab_family

                if mfg.target_fab and get_fab_family(mfg.target_fab) != self._fab_family:
                    tolerance_reason = f"no applicable tolerance for {self.manufacturer} (spec targets {mfg.target_fab})"
                else:
                    tolerance_mm = mfg.finished_hole_tolerance_mm
                    tolerance_source = mfg.finished_hole_tolerance_source
            from ..spec.schema import LeadProfile
            from .bom_spec_overlay import expand_ref_range

            for row, assignment in enumerate(spec.bom_entries or []):
                for ref in expand_ref_range(assignment.ref):
                    entries[ref] = (row, assignment)
            for row, termination in enumerate(spec.wire_terminations or []):
                wires.setdefault((termination.ref, termination.pad), []).append((row, termination))

        findings: list[dict] = []
        matched_wires: set[tuple[str, str]] = set()
        for fp in self._pcb.footprints:
            if fp.dnp:
                continue
            for pad in fp.pads:
                # Excludes vias (not pads), SMD pads and np_thru_hole
                # (non-plated) pads by type.
                if pad.type not in ("thru_hole", "through_hole"):
                    continue
                entry = None
                profile_error = None
                profile: LeadProfile | None = None
                wire: WireTermination | None = None
                declared = wires.get((fp.reference, pad.number))
                if declared:
                    matched_wires.add((fp.reference, pad.number))
                    # The prepared wire or ferrule end enters a declared pad,
                    # not its part's lead (Issue #10473).
                    row, wire = declared[0]
                    profile_error = spec_errors.wires.get(row)
                    if len(declared) > 1:
                        profile_error = (
                            f"{len(declared)} wire terminations declared for "
                            f"{fp.reference} pad {pad.number}"
                        )
                    elif profile_error is None and wire.diameter_mm is not None:
                        profile = LeadProfile(
                            shape="round",
                            diameter_mm=wire.diameter_mm,
                            source=wire.source or "",
                        )
                elif fp.reference in entries:
                    row, entry = entries[fp.reference]
                    profile_error = spec_errors.pad_containers.get(row) or spec_errors.profiles.get(
                        (row, pad.number)
                    )
                    if profile_error is None:
                        profile = (entry.pad_leads or {}).get(pad.number) or entry.lead
                        if profile is None:
                            profile_error = spec_errors.profiles.get((row, None))
                findings.append(
                    _lead_fit_finding(
                        fp.reference,
                        pad,
                        entry,
                        profile,
                        tolerance_mm,
                        tolerance_source,
                        tolerance_reason,
                        profile_error,
                        wire=wire,
                    )
                )

        failed = [f for f in findings if f["status"] == "fail"]
        unassessed = [f for f in findings if f["status"] == "unassessed"]
        tolerance_unassessed = [
            f
            for f in findings
            if f["status"] != "unassessed" and f["tolerance"]["status"] == "unassessed"
        ]
        unmatched_wires = [
            f"{ref} pad {pad}: no matching plated through-hole pad on PCB"
            for (ref, pad) in wires
            if (ref, pad) not in matched_wires
        ]
        not_applied = [*spec_errors.unattached_wires, *unmatched_wires]
        data: dict = {
            "pads": findings,
            "wire_terminations_not_applied": list(not_applied),
            "summary": {
                "pads": len(findings),
                "failed": len(failed),
                "unassessed": len(unassessed),
                "tolerance_unassessed": len(tolerance_unassessed),
                "finished_hole_tolerance_mm": tolerance_mm,
                "finished_hole_tolerance_source": tolerance_source,
            },
        }

        if not findings and not not_applied:
            return PreflightResult(
                name=name,
                status="OK",
                message="No plated through-hole component pads; lead fit not applicable",
                data=data,
            )

        def _refs(fs: list[dict], limit: int = 5) -> str:
            shown = ", ".join(f"{f['reference']} pad {f['pad']}" for f in fs[:limit])
            if len(fs) > limit:
                shown += f" (and {len(fs) - limit} more)"
            return shown

        if failed:
            message = f"Through-hole lead fit fails on {len(failed)} pad(s): {_refs(failed)}"
            status: Literal["OK", "WARN", "FAIL"] = "FAIL"
        elif unassessed or tolerance_unassessed or not_applied:
            bits = []
            if unassessed:
                bits.append(
                    f"{len(unassessed)} pad(s) unassessed "
                    "(no verified lead or prepared wire dimensions, or unsupported geometry)"
                )
            if tolerance_unassessed:
                bits.append(
                    f"{len(tolerance_unassessed)} pad(s) assessed without a finished-hole tolerance"
                )
            if not_applied:
                bits.append(f"{len(not_applied)} wire termination declaration(s) not applied")
            message = "Through-hole lead fit unverified: " + "; ".join(bits) + "; no failures"
            status = "WARN"
        else:
            message = (
                f"Through-hole lead fit verified: all {len(findings)} plated "
                "through-hole pad(s) fit"
            )
            status = "OK"

        lines = [_format_lead_finding(f) for f in sorted(findings, key=_lead_finding_sort_key)]
        if len(lines) > 40:  # pragma: no cover - large boards
            lines = lines[:40] + [f"(and {len(lines) - 40} more pads in data.pads)"]
        lines.extend(f"Wire termination declaration not applied: {note}" for note in not_applied)
        lines.append(
            "Assessment covers centred geometric fit only (not lead pitch or placement tolerances)."
        )
        return PreflightResult(
            name=name,
            status=status,
            message=message,
            details="\n".join(lines),
            data=data,
        )

    def _check_assembly_smd_land_pattern(self) -> PreflightResult:
        """Compare placed two-terminal SMD copper against declared MPN drawings.

        Issue #10505: a designer declares a source-attributed manufacturer SMD
        land pattern on a ``.kct`` ``bom_entries`` assignment (``land_pattern:``
        with pad shape, depth along the terminal axis, transverse width,
        pitch/outer span, and mandatory drawing/page ``source``).  This check
        reads the placed PCB without mutating it, undoes footprint rotation and
        front-to-back mirroring, and compares both copper pads' shape, depth,
        width, center pitch, and outer copper span within ``+/-0.001 mm``.

        Matching by footprint or package family name alone never verifies a
        part: only a valid pattern declared for the reference's exact BOM MPN
        counts.  Missing MPNs, missing or source-less declarations, and
        unusable pad geometry are reported as unassessed with explicit reasons
        without hiding findings on other references.
        """
        name = "assembly_smd_land_pattern"
        if self._pcb is None:
            return PreflightResult(
                name=name,
                status="OK",
                message="No PCB loaded; SMD land pattern not assessed",
                data={
                    "components": [],
                    "tolerance_mm": _SMD_DIM_TOLERANCE_MM,
                    "summary": {"components": 0, "passed": 0, "failed": 0, "unassessed": 0},
                },
            )

        spec, spec_errors = self._load_project_spec()
        entries: dict[str, tuple[int, BOMEntry]] = {}
        patterns_by_mpn: dict[str, dict[tuple, SmdLandPattern]] = {}
        if spec is not None:
            from .bom_spec_overlay import expand_ref_range

            for row, assignment in enumerate(spec.bom_entries or []):
                mpn_key = assignment.part.strip()
                if mpn_key and assignment.land_pattern is not None:
                    sig = _smd_pattern_signature(assignment.land_pattern)
                    patterns_by_mpn.setdefault(mpn_key, {})[sig] = assignment.land_pattern
                for ref in expand_ref_range(assignment.ref):
                    entries[ref] = (row, assignment)

        findings: list[dict] = []
        for fp in self._pcb.footprints:
            if (
                fp.dnp
                or fp.exclude_from_bom
                or "board_only" in getattr(fp, "attr_flags", ())
                or is_through_hole_footprint(fp)
            ):
                continue

            copper_pads = _copper_smd_pads(fp)
            entry_pair = entries.get(fp.reference)
            has_declared_pattern = entry_pair is not None and (
                entry_pair[1].land_pattern is not None or entry_pair[0] in spec_errors.land_patterns
            )
            if len(copper_pads) != 2 and not has_declared_pattern:
                continue

            mpn: str | None = None
            pattern: SmdLandPattern | None = None
            pattern_error: str | None = None
            if entry_pair is None:
                pattern_error = (
                    f"no MPN or SMD land pattern: reference {fp.reference} has no "
                    "bom_entries assignment in the project spec"
                )
            else:
                row, entry = entry_pair
                mpn = entry.part.strip() or None
                if mpn is None:
                    pattern_error = f"missing MPN on bom_entries assignment for {fp.reference}"
                elif row in spec_errors.land_patterns:
                    pattern_error = spec_errors.land_patterns[row]
                elif entry.land_pattern is not None:
                    pattern = entry.land_pattern
                else:
                    mpn_patterns = patterns_by_mpn.get(mpn)
                    if mpn_patterns and len(mpn_patterns) == 1:
                        pattern = next(iter(mpn_patterns.values()))
                    elif mpn_patterns and len(mpn_patterns) > 1:
                        pattern_error = f"conflicting SMD land-pattern declarations for MPN {mpn}"
                    else:
                        pattern_error = f"no verified SMD land pattern declared for MPN {mpn}"

            findings.append(_smd_land_pattern_finding(fp, mpn, pattern, pattern_error))

        passed = [f for f in findings if f["status"] == "pass"]
        failed = [f for f in findings if f["status"] == "fail"]
        unassessed = [f for f in findings if f["status"] == "unassessed"]
        data: dict = {
            "components": findings,
            "tolerance_mm": _SMD_DIM_TOLERANCE_MM,
            "summary": {
                "components": len(findings),
                "passed": len(passed),
                "failed": len(failed),
                "unassessed": len(unassessed),
            },
        }

        if not findings:
            return PreflightResult(
                name=name,
                status="OK",
                message="No populated two-terminal SMD chips; SMD land-pattern check not applicable",
                data=data,
            )

        def _refs(fs: list[dict], limit: int = 5) -> str:
            shown = ", ".join(f["reference"] for f in fs[:limit])
            if len(fs) > limit:
                shown += f" (and {len(fs) - limit} more)"
            return shown

        if failed:
            message = f"SMD land pattern mismatch on {len(failed)} component(s): {_refs(failed)}"
            status: Literal["OK", "WARN", "FAIL"] = "FAIL"
        elif unassessed:
            message = (
                f"SMD land pattern unverified: {len(unassessed)} two-terminal SMD "
                "component(s) unassessed (missing MPN, source, or usable geometry); no failures"
            )
            status = "WARN"
        else:
            message = (
                f"SMD land pattern verified: all {len(findings)} two-terminal SMD "
                "component(s) match declared drawings"
            )
            status = "OK"

        lines = [
            _format_smd_land_pattern_finding(f) for f in sorted(findings, key=_smd_finding_sort_key)
        ]
        if len(lines) > 40:  # pragma: no cover - large boards
            lines = lines[:40] + [f"(and {len(lines) - 40} more components in data.components)"]
        lines.append(
            f"Dimensional comparison tolerance: +/-{_SMD_DIM_TOLERANCE_MM:.{_SMD_DIM_DECIMALS}f} mm "
            "(two-terminal SMD copper pads; placement rotation and mirroring undone)."
        )
        return PreflightResult(
            name=name,
            status=status,
            message=message,
            details="\n".join(lines),
            data=data,
        )

    def _check_stackup_copper(self) -> PreflightResult:
        """Fail when the stackup's copper weight is not one the fab order provides (#7209)."""
        if self._pcb is None:
            return PreflightResult(
                name="stackup_copper",
                status="OK",
                message="No PCB loaded; stackup copper not reconciled",
            )
        info = resolve_ordered_copper(self._pcb, self.manufacturer)
        status = info["status"]
        if status == "match":
            return PreflightResult(
                name="stackup_copper",
                status="OK",
                message=(
                    f"Stackup copper matches the {self.manufacturer} order "
                    f"(outer {_fmt_oz(info['ordered_outer_oz'])}, "
                    f"inner {_fmt_oz(info['ordered_inner_oz'])})"
                ),
            )
        if status == "mismatch":
            offered = "; ".join(
                f"outer {_fmt_oz(o)} / inner {_fmt_oz(i)}" for o, i in info["profile_options"]
            )
            return PreflightResult(
                name="stackup_copper",
                status="FAIL",
                message=(
                    f"Board stackup declares outer {_fmt_oz(info['stackup_outer_oz'])} / "
                    f"inner {_fmt_oz(info['stackup_inner_oz'])} copper, which "
                    f"{self.manufacturer} does not provide for {info['layers']} layers"
                ),
                details=(
                    f"Profile offers: {offered}. The ampacity gate trusts the stackup, so "
                    "the board could pass on copper nobody orders. Correct the stackup to an "
                    "offered build, or, if the fab does offer this copper, add the "
                    "option to the manufacturer profile's design_rules."
                ),
            )
        return PreflightResult(
            name="stackup_copper",
            status="OK",
            message="No usable stackup copper to reconcile; profile default copper applies",
        )

    def _check_tht_in_cpl(self) -> PreflightResult:
        """Warn when THT components are present on a board targeting an SMT-only assembler."""
        if self._pcb is None:
            return PreflightResult(
                name="tht_in_cpl",
                status="WARN",
                message="Cannot check THT components: PCB not loaded",
            )

        tht_refs = [
            fp.reference
            for fp in self._pcb.footprints
            if is_through_hole_footprint(fp)
            and not getattr(fp, "exclude_from_pos_files", False)
            and not getattr(fp, "dnp", False)
        ]

        if not tht_refs:
            return PreflightResult(
                name="tht_in_cpl",
                status="OK",
                message="No through-hole components in CPL-eligible footprints",
            )

        refs = ", ".join(sorted(tht_refs)[:10])
        suffix = f" (and {len(tht_refs) - 10} more)" if len(tht_refs) > 10 else ""

        if self._exclude_tht:
            return PreflightResult(
                name="tht_in_cpl",
                status="OK",
                message=f"{len(tht_refs)} through-hole component(s) excluded from CPL",
                details=f"THT references: {refs}{suffix}",
            )

        if self._fab_family in self._SMT_ONLY_MANUFACTURERS:
            return PreflightResult(
                name="tht_in_cpl",
                status="WARN",
                message=(
                    f"{len(tht_refs)} through-hole component(s) in CPL "
                    f"for SMT-only assembler ({self.manufacturer})"
                ),
                details=f"THT references: {refs}{suffix}; use --include-tht to keep them or they will be auto-excluded",
            )

        return PreflightResult(
            name="tht_in_cpl",
            status="OK",
            message=f"{len(tht_refs)} through-hole component(s) included in CPL",
            details=f"THT references: {refs}{suffix}",
        )

    # ------------------------------------------------------------------
    # Assembly-capability checks (Issue #7204)
    # ------------------------------------------------------------------

    def _assembly_profile(self) -> ManufacturerProfile | None:
        """The manufacturer profile, or ``None`` when it cannot be resolved."""
        if not self._assembly_profile_resolved:
            self._assembly_profile_resolved = True
            try:
                from ..manufacturers import get_profile

                self._assembly_profile_cache = get_profile(self.manufacturer)
            except Exception:
                self._assembly_profile_cache = None
        return self._assembly_profile_cache

    def _assembly_tiers(self) -> list[AssemblyCapabilities]:
        """The fab's PCBA service tiers, cheapest first (``[]`` = PCB-only)."""
        profile = self._assembly_profile()
        if profile is None:
            return []
        return profile.get_assembly_tiers()

    def _is_tiered(self) -> bool:
        """Whether the fab publishes more than one PCBA service tier."""
        profile = self._assembly_profile()
        return bool(profile is not None and profile.assembly_tiers)

    def _fab_display_name(self) -> str:
        profile = self._assembly_profile()
        return profile.name if profile is not None else self.manufacturer

    def _selected_tier(self) -> tuple[int, str]:
        """Index of the tier to judge against, plus a note for ``details``.

        ``auto`` (and any unmatched name) selects the cheapest tier.
        """
        requested = (self.config.assembly_tier or "auto").strip().lower()
        if requested == "auto":
            return 0, ""
        tiers = self._assembly_tiers()
        if not self._is_tiered():
            return 0, (
                f"{self._fab_display_name()} publishes a single assembly service; "
                f"--assembly-tier {requested} ignored"
            )
        for idx, tier in enumerate(tiers):
            name = (tier.tier_name or "").lower()
            if name == requested or name.split(" ")[0] == requested:
                return idx, ""
        names = ", ".join((t.tier_name or "?").split(" ")[0].lower() for t in tiers)
        return 0, f"unknown assembly tier '{requested}' (choices: auto, {names}); using auto"

    def _cpl_footprints(self) -> list:
        """Footprints that land in the CPL (same filter as ``extract_placements``)."""
        if self._pcb is None:
            return []
        return [
            fp
            for fp in self._pcb.footprints
            if not getattr(fp, "exclude_from_pos_files", False)
            and not getattr(fp, "dnp", False)
            and not (self._exclude_tht and is_through_hole_footprint(fp))
        ]

    def _check_bottom_side_in_cpl(self) -> PreflightResult:
        """Flag bottom-side CPL parts and the assembly tier they require.

        Always WARN (never FAIL), even under ``--strict-preflight`` or an
        explicit ``--assembly-tier``: double-sided placement is a cost and
        service-tier decision, not an unbuildable board.
        """
        name = "bottom_side_in_cpl"
        bottom = sorted(
            (fp.reference for fp in self._cpl_footprints() if fp.layer == "B.Cu"),
            key=_ref_sort_key,
        )
        if not bottom:
            return PreflightResult(
                name=name,
                status="OK",
                message="No bottom-side components in CPL (single-sided assembly)",
            )

        refs = ", ".join(bottom[:10])
        suffix = f" (and {len(bottom) - 10} more)" if len(bottom) > 10 else ""
        details = f"Bottom-side references: {refs}{suffix}"
        fab = self._fab_display_name()
        tiers = self._assembly_tiers()
        need = next((t for t in tiers if t.supports_double_sided), None)
        count = f"{len(bottom)} bottom-side component(s)"

        if need is None:
            message = f"{count}: {fab}'s assembly service does not list double-sided placement"
        elif self._is_tiered():
            terms = ["double-sided"]
            if need.requires_rails_and_fiducials:
                terms.append("edge rails + fiducials required")
            if need.min_build_days:
                terms.append(f">={need.min_build_days}-day build")
            message = f"{count}: requires {fab} {need.display_name} ({'; '.join(terms)})"
        else:
            message = f"{count}: double-sided assembly (two placement passes, higher cost)"

        size = self._board_size_mm()
        if need is not None and need.min_board_size_mm and size is not None:
            board = sorted(size)
            minimum = sorted(need.min_board_size_mm)
            if board[0] < minimum[0] or board[1] < minimum[1]:
                min_w, min_h = need.min_board_size_mm
                message += (
                    f"; board is {size[0]:.1f}x{size[1]:.1f} mm, below "
                    f"{need.display_name}'s {min_w:g}x{min_h:g} mm single-PCB "
                    "minimum: panelize (`kct panel`)"
                )

        return PreflightResult(name=name, status="WARN", message=message, details=details)

    def _assembly_requirements(self) -> list[_AssemblyRequirement]:
        """Package/pitch requirements of every CPL part (lib id first, then measured)."""
        reqs: list[_AssemblyRequirement] = []
        for fp in self._cpl_footprints():
            lib_id = getattr(fp, "name", "") or ""
            ref = getattr(fp, "reference", "") or lib_id
            reqs.extend(_requirements_for_footprint(ref, lib_id, getattr(fp, "pads", ())))
        return reqs

    def _check_assembly_package_limits(self) -> PreflightResult:
        """Compare chip package size and IC/BGA pitch against the PCBA tier(s).

        Within the selected tier -> OK.  Beyond it but within a costlier tier
        -> WARN naming the tier needed.  Beyond every tier the fab offers ->
        FAIL (no service at that fab can place it), except for pitches that
        were *measured* from pads rather than read from the lib id, which cap
        at WARN because the measurement is a heuristic.
        """
        name = "assembly_package_limits"
        tiers = self._assembly_tiers()
        tiered = self._is_tiered()
        fab = self._fab_display_name()
        selected_idx, note = self._selected_tier()
        selected = tiers[selected_idx]
        auto = (self.config.assembly_tier or "auto").strip().lower() == "auto"

        reqs = self._assembly_requirements()
        impossible: list[_AssemblyRequirement] = []
        needs: dict[int, list[_AssemblyRequirement]] = {}
        required_idx = 0
        for req in reqs:
            idx = next((i for i, t in enumerate(tiers) if req.fits(t)), None)
            if idx is None:
                impossible.append(req)
                continue
            required_idx = max(required_idx, idx)
            if idx > selected_idx:
                needs.setdefault(idx, []).append(req)

        def _limits(tier: AssemblyCapabilities) -> str:
            parts = []
            if tier.min_package:
                parts.append(f"min package {tier.min_package}")
            parts.append(f"IC pitch {tier.min_component_pitch_mm:g} mm")
            parts.append(f"BGA pitch {tier.min_bga_pitch_mm:g} mm")
            return ", ".join(parts)

        def _join(items: list[_AssemblyRequirement]) -> str:
            shown = "; ".join(r.describe() for r in items[:10])
            if len(items) > 10:
                shown += f" (and {len(items) - 10} more)"
            return shown

        detail_lines: list[str] = []
        if note:
            detail_lines.append(note)
        detail_lines.append(f"{len(reqs)} package/pitch requirement(s) checked")

        if impossible:
            hard = [r for r in impossible if not r.measured]
            detail_lines.append(f"Beyond every {fab} assembly tier: {_join(impossible)}")
            for idx in sorted(needs):
                detail_lines.append(f"Needs {tiers[idx].display_name}: {_join(needs[idx])}")
            best = tiers[-1]
            where = f"{fab} {best.display_name}" if tiered else f"{fab} assembly"
            return PreflightResult(
                name=name,
                status="FAIL" if hard else "WARN",
                message=(
                    f"{len(impossible)} part(s) beyond {where} limits ({_limits(best)})"
                    + ("" if hard else "; pitch measured from pads, verify")
                ),
                details="; ".join(detail_lines),
            )

        if needs:
            top = max(needs)
            flagged = [r for idx in sorted(needs) for r in needs[idx]]
            detail_lines.append(f"Parts: {_join(flagged)}")
            if auto:
                message = (
                    f"Cheapest {fab} tier that fits: {tiers[top].display_name} "
                    f"({len(flagged)} part(s) beyond {selected.display_name} limits: "
                    f"{_limits(selected)})"
                )
            else:
                message = (
                    f"{len(flagged)} part(s) beyond {fab} {selected.display_name} limits; "
                    f"requires {tiers[top].display_name}"
                )
            return PreflightResult(
                name=name, status="WARN", message=message, details="; ".join(detail_lines)
            )

        if tiered:
            if auto:
                message = (
                    f"All placed packages fit {fab} {tiers[required_idx].display_name} "
                    f"({_limits(tiers[required_idx])})"
                )
            else:
                message = (
                    f"All placed packages within {fab} {selected.display_name} limits "
                    f"({_limits(selected)})"
                )
        else:
            message = f"All placed packages within {fab} assembly limits ({_limits(selected)})"
        return PreflightResult(
            name=name, status="OK", message=message, details="; ".join(detail_lines)
        )

    def _check_bom_fields(self) -> PreflightResult:
        """Check that BOM items have required fields for manufacturing."""
        try:
            bom = self._load_bom()
        except Exception as e:
            return PreflightResult(
                name="bom_fields",
                status="WARN",
                message=f"Could not extract BOM: {e}",
            )

        if not bom.items:
            return PreflightResult(
                name="bom_fields",
                status="WARN",
                message="BOM has no items",
            )

        # Check for items missing footprint
        missing_fp = [item for item in bom.items if not item.footprint and not item.is_virtual]
        # Check for items missing value
        missing_val = [item for item in bom.items if not item.value and not item.is_virtual]
        # Check for LCSC part numbers (warn only)
        missing_lcsc = [
            item for item in bom.items if not item.lcsc and not item.is_virtual and not item.dnp
        ]

        issues: list[str] = []
        # Informational notes that do NOT constitute a problem (e.g. missing
        # LCSC fields that the spec overlay will populate).  Kept separate so
        # they never inflate the problem count or force a WARN status.
        notes: list[str] = []
        status: Literal["OK", "WARN", "FAIL"] = "OK"

        if missing_fp:
            refs = ", ".join(item.reference for item in missing_fp[:5])
            issues.append(f"{len(missing_fp)} item(s) missing footprint: {refs}")
            status = "FAIL"

        if missing_val:
            refs = ", ".join(item.reference for item in missing_val[:5])
            issues.append(f"{len(missing_val)} item(s) missing value: {refs}")
            if status != "FAIL":
                status = "WARN"

        if missing_lcsc:
            total = len([i for i in bom.items if not i.is_virtual and not i.dnp])
            # Preflight loads a *raw* BOM (Step 0) before the assembly
            # pipeline applies the spec overlay (Step 1).  Items whose LCSC
            # will be populated from the project ``.kct`` spec are not
            # genuinely missing -- warning about them here produces a false
            # positive (issue #3926).  Resolve which refs the spec covers and
            # only warn about the genuinely-unresolved remainder.
            spec_lcsc_refs = self._spec_lcsc_refs()
            unresolved = [item for item in missing_lcsc if item.reference not in spec_lcsc_refs]
            resolvable = len(missing_lcsc) - len(unresolved)

            if unresolved:
                msg = f"{len(unresolved)}/{total} active item(s) missing LCSC part number"
                if resolvable:
                    msg += f" ({resolvable} resolvable from spec)"
                issues.append(msg)
                if status != "FAIL":
                    status = "WARN"
            elif resolvable:
                # Every missing-LCSC item is covered by the spec overlay:
                # record an informational note instead of a false WARN.
                notes.append(f"{resolvable} active item(s) will receive LCSC from spec overlay")

        if not issues:
            active_count = len([i for i in bom.items if not i.is_virtual])
            return PreflightResult(
                name="bom_fields",
                status="OK",
                message=f"All {active_count} BOM items have required fields",
                details="; ".join(notes),
            )

        return PreflightResult(
            name="bom_fields",
            status=status,
            message=f"BOM field issues: {len(issues)} problem(s)",
            details="; ".join(issues + notes),
        )

    def check_bom_lcsc_values(self) -> PreflightResult:
        """Run the ``bom_lcsc_values`` check on its own (Issue #7260).

        The same check :meth:`run_all` runs, for callers that only need the
        LCSC value/package verdict (the MCP ``validate_assembly_bom`` tool).
        Loads the PCB first, when it parses, so the IC package-family /
        pin-count clause (#7166) sees the board's footprints.
        """
        if self._pcb is None and self.pcb_path.exists():
            with contextlib.suppress(Exception):
                from ..schema.pcb import PCB

                self._pcb = PCB.load(str(self.pcb_path))  # type: ignore[assignment]
        return self._check_bom_lcsc_values()

    def _check_bom_lcsc_values(self) -> PreflightResult:
        """Validate assigned LCSC parts against known value AND package.

        For every active BOM item that HAS an LCSC number, look the part
        up in the local parts cache, falling back to the offline jlcparts
        catalog (read-only; no network, nothing written to the cache).
        When the part is known and its parametric value clearly disagrees
        with the BOM row value (e.g. 100nF part on a 16nF row -- issue
        #3590), its chip package disagrees with the row's footprint (an
        0402 part on 0805 footprints -- issue #3597), or its IC package
        family / pin count disagrees with the PCB footprint (a SOT-25-5
        part on a SOT-23-6 footprint -- issue #7166), the check FAILS.

        Items whose part is in neither source cannot be validated.  They
        are counted separately and turn the result into a WARN that names
        them (issue #7166): "not checked" must never read as "checked and
        fine".  The WARN never blocks export, even under
        ``--strict-preflight`` -- a fresh CI runner has an empty cache.

        A known part on a ``PinHeader_RxC`` / ``PinSocket_RxC`` footprint
        whose record states no readable pin count (or conflicting ones) is
        ``unverified`` too (issue #7446), tagged with its reason.  By default
        it joins the same WARN.  Under ``strict_report_source`` (set from
        ``--strict-preflight``, as for the #7091 report-source check) it is a
        FAIL: unlike a cache miss it is a deterministic fact about the part
        record, and the check that matters -- does a 2-pin part sit on an
        8-pin header? -- never ran.  Parts missing from the cache stay WARN
        even under strict.
        """
        try:
            bom = self._load_bom()
            items = self._items_with_spec_overlay(bom)
        except Exception as e:
            return PreflightResult(
                name="bom_lcsc_values",
                status="WARN",
                message=f"Could not extract BOM: {e}",
            )

        try:
            cache = self._get_parts_cache()
        except Exception as e:
            return PreflightResult(
                name="bom_lcsc_values",
                status="WARN",
                message=f"Parts cache unavailable for LCSC value validation: {e}",
            )

        catalog = self._parts_catalog
        if catalog is None:
            with contextlib.suppress(Exception):
                from ..parts.jlcparts_catalog import CATALOG_FILENAME, JlcpartsCatalog

                catalog = JlcpartsCatalog(Path(cache.db_path).parent / CATALOG_FILENAME)

        from .lcsc_value_check import verify_lcsc_assignment

        # PCB pad numbers per reference, for the IC pin-count check.  A
        # reference carried by more than one footprint is ambiguous: give it
        # no pads (the check then falls back to the footprint name).
        pads_by_ref: dict[str, list[str] | None] = {}
        if self._pcb is not None:
            for fp in self._pcb.footprints:
                pads = [str(p.number) for p in fp.pads]
                pads_by_ref[fp.reference] = None if fp.reference in pads_by_ref else pads

        from .lcsc_value_check import UNVERIFIED_HEADER_REASONS

        mismatches: list[str] = []
        unverified: list[str] = []
        header_unverified: list[str] = []
        sources: set[str] = set()
        exemptions = self._spec_exemptions()
        exempt_seen: dict[str, tuple[str, str]] = {}
        exempt_conflicts: list[str] = []
        bom_refs = {i.reference for i in items}
        stale_exempt = sorted(r for r in exemptions if r not in bom_refs)
        assigned = 0
        for item in items:
            if item.is_virtual or item.dnp:
                continue
            if not item.lcsc:
                if item.reference in exemptions:
                    exempt_seen[item.reference] = exemptions[item.reference]
                continue
            assigned += 1
            verdict = verify_lcsc_assignment(
                cache,
                catalog,
                item.lcsc,
                item.value,
                item.reference,
                footprint=item.footprint,
                pad_numbers=pads_by_ref.get(item.reference),
            )
            if verdict.source:
                sources.add(verdict.source)
            if item.reference in exemptions:
                if (
                    verdict.status == "unverified"
                    and verdict.reason not in UNVERIFIED_HEADER_REASONS
                ):
                    # Declared non-LCSC part carrying a number no source knows:
                    # accepted, but recorded (issue #8261).  This is the ONLY
                    # verdict an exemption softens.
                    assigned -= 1
                    exempt_seen[item.reference] = exemptions[item.reference]
                    continue
                # A real LCSC part is on the BOM row: spec and BOM disagree.
                # Record the conflict, then judge the part like any other ref
                # -- an exemption must never hide a wrong part (#8113) or a
                # strict header FAIL (issue #8261 review).
                ex_cat, ex_why = exemptions[item.reference]
                exempt_conflicts.append(f"{item.reference} -> {item.lcsc} [{ex_cat}: {ex_why}]")
            if verdict.status == "unverified":
                if verdict.reason in UNVERIFIED_HEADER_REASONS:
                    entry = f"{item.reference} -> {item.lcsc} ({verdict.reason})"
                    header_unverified.append(entry)
                    unverified.append(entry)
                else:
                    unverified.append(f"{item.reference} -> {item.lcsc}")
                continue
            if verdict.mismatch is not None:
                mismatches.append(
                    f"{item.reference} ({item.value}) -> {item.lcsc}: "
                    f"{verdict.mismatch.describe()} [{verdict.source}]"
                )

        conflict_note = (
            f"; {len(exempt_conflicts)} ref(s) declared exempt in the spec carry a "
            f"verifiable LCSC part on the BOM (spec and BOM disagree)"
            if exempt_conflicts
            else ""
        )
        if mismatches:
            shown = "; ".join(mismatches[:5])
            suffix = f" (and {len(mismatches) - 5} more)" if len(mismatches) > 5 else ""
            return PreflightResult(
                name="bom_lcsc_values",
                status="FAIL",
                message=(
                    f"{len(mismatches)} BOM item(s) assigned an LCSC part "
                    f"whose value or package does not match the BOM row" + conflict_note
                ),
                details=f"{shown}{suffix}"
                + self._exempt_suffix(exempt_seen, exempt_conflicts, stale_exempt),
            )

        if not assigned:
            return PreflightResult(
                name="bom_lcsc_values",
                status="OK",
                message="No assigned LCSC parts to validate"
                + (f"; {len(exempt_seen)} declared exempt" if exempt_seen else ""),
                details=self._exempt_suffix(exempt_seen, [], stale_exempt, lead=True),
            )

        verified = assigned - len(unverified)
        against = " and ".join(sorted(sources)) or "no source"
        if header_unverified and self.config.strict_report_source:
            shown = ", ".join(header_unverified[:8])
            n_hdr = len(header_unverified)
            suffix = f" (and {n_hdr - 8} more)" if n_hdr > 8 else ""
            others = len(unverified) - n_hdr
            also = f"; {others} more unverifiable (not in parts cache or offline catalog)"
            return PreflightResult(
                name="bom_lcsc_values",
                status="FAIL",
                message=(
                    f"{n_hdr} pin header/socket part(s): pin count not verifiable "
                    f"(strict preflight){also if others else ''}" + conflict_note
                ),
                details=f"pin count not verifiable: {shown}{suffix}"
                + self._exempt_suffix(exempt_seen, exempt_conflicts, stale_exempt),
            )
        if unverified:
            shown = ", ".join(unverified[:8])
            suffix = f" (and {len(unverified) - 8} more)" if len(unverified) > 8 else ""
            not_found = len(unverified) - len(header_unverified)
            why = [
                f"{n} {text}"
                for n, text in (
                    (not_found, "not in parts cache or offline catalog"),
                    (len(header_unverified), "header part(s) with pin count not verifiable"),
                )
                if n
            ]
            reasons = (
                "not in parts cache or offline catalog"
                if not header_unverified
                else "header part(s) with pin count not verifiable"
                if not not_found
                else "; ".join(why)
            )
            return PreflightResult(
                name="bom_lcsc_values",
                status="WARN",
                message=(
                    f"{verified} of {assigned} assigned item(s) verified ({against}); "
                    f"{len(unverified)} unverifiable ({reasons})" + conflict_note
                ),
                details=f"unverified: {shown}{suffix}"
                + self._exempt_suffix(exempt_seen, exempt_conflicts, stale_exempt),
            )

        if exempt_conflicts:
            # Every part checked out, but the spec declares refs exempt that
            # carry a verifiable LCSC part: never a clean OK (issue #8261).
            return PreflightResult(
                name="bom_lcsc_values",
                status="WARN",
                message=(
                    f"{assigned} of {assigned} assigned item(s) verified against {against}; "
                    f"no LCSC value/package mismatches" + conflict_note
                ),
                details=self._exempt_suffix(exempt_seen, exempt_conflicts, stale_exempt, lead=True),
            )

        return PreflightResult(
            name="bom_lcsc_values",
            status="OK",
            message=(
                f"{assigned} of {assigned} assigned item(s) verified against {against}; "
                f"no LCSC value/package mismatches"
                + (f"; {len(exempt_seen)} declared exempt" if exempt_seen else "")
            ),
            details=self._exempt_suffix(exempt_seen, [], stale_exempt, lead=True),
        )

    def check_bom_lcsc_stock(self) -> PreflightResult:
        """Run the ``bom_lcsc_stock`` check on its own (Issue #8121)."""
        return self._check_bom_lcsc_stock()

    def _check_bom_lcsc_stock(self) -> PreflightResult:
        """Flag assigned LCSC parts whose recorded stock cannot cover the order.

        Required stock per LCSC number is its placements per board (summed
        over references) times ``config.order_boards``.  Read-only like
        :meth:`_check_bom_lcsc_values`: it consults the local parts cache and
        the offline catalog and never touches the network.  Parts in neither
        source are left to ``bom_lcsc_values`` and not repeated here.

        A shortfall on a live-verified observation (``Part.stock_verified``)
        is a FAIL.  On any other observation (stale cache, offline catalog,
        unknown source) it is a WARN -- a legacy cache row with no stock info
        also reads ``stock=0`` -- escalated to FAIL under
        ``strict_report_source`` (``--strict-preflight``).

        The recorded ``data`` also says where the lookups reached on this
        host (``sources``, issue #10386), so a later reader can tell an
        unverified part from a host that had no source to check it against.
        """
        name = "bom_lcsc_stock"
        boards = max(1, int(self.config.order_boards))
        try:
            bom = self._load_bom()
            items = self._items_with_spec_overlay(bom)
        except Exception as e:
            return PreflightResult(name=name, status="WARN", message=f"Could not extract BOM: {e}")

        try:
            cache = self._get_parts_cache()
        except Exception as e:
            return PreflightResult(
                name=name,
                status="WARN",
                message=f"Parts cache unavailable for LCSC stock check: {e}",
            )
        catalog = self._parts_catalog
        if catalog is None:
            with contextlib.suppress(Exception):
                from ..parts.jlcparts_catalog import CATALOG_FILENAME, JlcpartsCatalog

                catalog = JlcpartsCatalog(Path(cache.db_path).parent / CATALOG_FILENAME)

        from .lcsc_value_check import verify_lcsc_assignment

        exemptions = self._spec_exemptions()
        refs_by_lcsc: dict[str, list[str]] = {}
        for item in items:
            if item.is_virtual or item.dnp or not item.lcsc:
                continue
            if item.reference in exemptions and (
                verify_lcsc_assignment(cache, catalog, item.lcsc, "", item.reference).part is None
            ):
                continue  # declared non-LCSC part with an unknown number (issue #8261)
            # An exempt ref with a real, known part still ships it: check its stock.
            refs_by_lcsc.setdefault(item.lcsc, []).append(item.reference)

        if not refs_by_lcsc and not self.config.inventory_covered:
            return PreflightResult(
                name=name,
                status="OK",
                message="No assigned LCSC parts to check stock for",
                data={
                    "order_boards": boards,
                    "inventory_covered": [],
                    "strict": bool(self.config.strict_report_source),
                    "parts": [],
                    "unresolved": [],
                },
            )

        records: list[dict] = []
        unresolved: list[str] = []
        cache_states: set[str] = set()
        catalog_states: set[str] = set()
        for lcsc, refs in sorted(refs_by_lcsc.items()):
            verdict = verify_lcsc_assignment(cache, catalog, lcsc, "", refs[0])
            cache_states.add(verdict.lookups.cache)
            catalog_states.add(verdict.lookups.catalog)
            if verdict.part is None:
                unresolved.append(lcsc)  # not in any source: also reported by bom_lcsc_values
                continue
            part = verdict.part
            age_days = None
            if part.fetched_at is not None:
                now = datetime.now(tz=part.fetched_at.tzinfo)
                age_days = max(0, (now - part.fetched_at).days)
            records.append(
                {
                    "lcsc": lcsc,
                    "refs": sorted(refs),
                    "per_board": len(refs),
                    "stock": part.stock,
                    "verified": bool(part.stock_verified),
                    "source": part.stock_source,
                    "age_days": age_days,
                }
            )
        data = {
            "order_boards": boards,
            "inventory_covered": sorted(self.config.inventory_covered),
            "strict": bool(self.config.strict_report_source),
            "parts": records,
            "unresolved": unresolved,
            "sources": {
                LOOKUP_SOURCE_CACHE: summarize_lookup_states(LOOKUP_SOURCE_CACHE, cache_states),
                LOOKUP_SOURCE_CATALOG: summarize_lookup_states(
                    LOOKUP_SOURCE_CATALOG, catalog_states
                ),
            },
        }
        return evaluate_lcsc_stock(
            data,
            boards,
            frozenset(c.upper() for c in self.config.inventory_covered),
            bool(self.config.strict_report_source),
        )

    def _check_bom_footprint_match(self) -> PreflightResult:
        """Check that BOM component count matches PCB footprint count."""
        if self._pcb is None:
            return PreflightResult(
                name="bom_pcb_match",
                status="WARN",
                message="Cannot check BOM/PCB match: PCB not loaded",
            )

        try:
            bom = self._load_bom()
        except Exception as e:
            return PreflightResult(
                name="bom_pcb_match",
                status="WARN",
                message=f"Cannot check BOM/PCB match: {e}",
            )

        # BOM references (non-virtual, non-DNP)
        bom_refs = {item.reference for item in bom.items if not item.is_virtual and not item.dnp}

        # PCB footprint references.  Footprints that declare themselves absent
        # from the BOM (``exclude_from_bom``) or board-only (``board_only``),
        # e.g. fiducials, are intentionally not in the BOM, so they are not
        # orphans.  Only the orphan (PCB-not-BOM) side is relaxed: a BOM ref
        # is matched against *every* footprint, so it never FAILs because of
        # attributes.
        pcb_refs = {fp.reference for fp in self._pcb.footprints}
        orphan_candidates = {
            fp.reference
            for fp in self._pcb.footprints
            if not (fp.exclude_from_bom or "board_only" in fp.attr_flags)
        }

        in_bom_not_pcb = bom_refs - pcb_refs
        in_pcb_not_bom = orphan_candidates - bom_refs

        issues: list[str] = []
        status: Literal["OK", "WARN", "FAIL"] = "OK"

        if in_bom_not_pcb:
            # Unplaced components cannot be assembled -- this is a hard failure
            status = "FAIL"
            refs = ", ".join(sorted(in_bom_not_pcb)[:10])
            issues.append(f"{len(in_bom_not_pcb)} in BOM but not on PCB: {refs}")
        if in_pcb_not_bom:
            # Orphaned footprints may be intentional (mounting holes, test points)
            if status != "FAIL":
                status = "WARN"
            refs = ", ".join(sorted(in_pcb_not_bom)[:10])
            issues.append(f"{len(in_pcb_not_bom)} on PCB but not in BOM: {refs}")

        if issues:
            return PreflightResult(
                name="bom_pcb_match",
                status=status,
                message="BOM/PCB reference mismatch",
                details="; ".join(issues),
            )

        return PreflightResult(
            name="bom_pcb_match",
            status="OK",
            message=f"BOM and PCB references match ({len(bom_refs)} components)",
        )

    def _check_bom_cpl_match(self) -> PreflightResult:
        """Check that BOM references match CPL-eligible PCB footprints.

        The CPL (pick-and-place) file is generated from PCB footprints with
        filtering applied: DNP components, footprints excluded from position
        files, and (when targeting SMT-only assemblers) through-hole
        components are omitted.  This check compares the set of active BOM
        references against the CPL-eligible footprint references so that
        assembly mismatches are caught before export.
        """
        if self._pcb is None:
            return PreflightResult(
                name="bom_cpl_match",
                status="WARN",
                message="Cannot check BOM/CPL match: PCB not loaded",
            )

        try:
            bom = self._load_bom()
        except Exception as e:
            return PreflightResult(
                name="bom_cpl_match",
                status="WARN",
                message=f"Cannot check BOM/CPL match: {e}",
            )

        # BOM references (non-virtual, non-DNP) -- same filter as _check_bom_footprint_match
        bom_refs = {item.reference for item in bom.items if not item.is_virtual and not item.dnp}

        # CPL-eligible footprint references -- mirrors extract_placements() in pnp.py
        cpl_refs = set()
        tht_excluded_refs = set()
        dnp_excluded_refs = set()
        for fp in self._pcb.footprints:
            if getattr(fp, "exclude_from_pos_files", False):
                continue
            if getattr(fp, "dnp", False):
                dnp_excluded_refs.add(fp.reference)
                continue
            if self._exclude_tht and is_through_hole_footprint(fp):
                tht_excluded_refs.add(fp.reference)
                continue
            cpl_refs.add(fp.reference)

        # THT-excluded and DNP-excluded components are expected mismatches
        in_bom_not_cpl = bom_refs - cpl_refs - tht_excluded_refs - dnp_excluded_refs
        in_cpl_not_bom = cpl_refs - bom_refs

        issues: list[str] = []
        if in_bom_not_cpl:
            refs = ", ".join(sorted(in_bom_not_cpl)[:10])
            suffix = f" (and {len(in_bom_not_cpl) - 10} more)" if len(in_bom_not_cpl) > 10 else ""
            issues.append(f"{len(in_bom_not_cpl)} in BOM but not in CPL: {refs}{suffix}")
        if in_cpl_not_bom:
            refs = ", ".join(sorted(in_cpl_not_bom)[:10])
            suffix = f" (and {len(in_cpl_not_bom) - 10} more)" if len(in_cpl_not_bom) > 10 else ""
            issues.append(f"{len(in_cpl_not_bom)} in CPL but not in BOM: {refs}{suffix}")

        if issues:
            return PreflightResult(
                name="bom_cpl_match",
                status="FAIL",
                message="BOM/CPL reference mismatch",
                details="; ".join(issues),
            )

        info_parts = [f"{len(cpl_refs)} components"]
        if tht_excluded_refs:
            info_parts.append(f"{len(tht_excluded_refs)} THT excluded")
        if dnp_excluded_refs:
            info_parts.append(f"{len(dnp_excluded_refs)} DNP excluded")

        return PreflightResult(
            name="bom_cpl_match",
            status="OK",
            message=f"BOM and CPL references match ({', '.join(info_parts)})",
        )

    def _check_drc(self) -> PreflightResult:
        """Check DRC status from a pre-existing report file."""
        report_path = self.config.drc_report_path
        # Issue #7190: only an auto-discovered report must prove it was
        # generated for this PCB's content; an explicit --drc-report is the
        # caller's assertion and stays trusted.
        auto_discovered = report_path is None

        if report_path is None:
            # Try to find a DRC report next to the PCB
            auto_path = self.pcb_path.parent / "drc_report.json"
            if not auto_path.exists():
                auto_path = self.pcb_path.parent / "drc_report.txt"
            if auto_path.exists():
                report_path = auto_path
            else:
                return PreflightResult(
                    name="drc",
                    status="WARN",
                    message="No DRC report found; run kicad-cli or kct check first",
                    details="Use --drc-report to specify a report file, or --skip-drc to skip",
                )

        report_path = Path(report_path)
        if not report_path.exists():
            return PreflightResult(
                name="drc",
                status="WARN",
                message=f"DRC report not found: {report_path}",
            )

        # Issue #6724: an empty report (kicad-cli killed before writing it)
        # parses as zero findings; it is not evidence of a clean DRC.
        if report_path.stat().st_size == 0:
            return PreflightResult(
                name="drc",
                status="WARN",
                message=f"DRC report is empty: {report_path}",
                details="kicad-cli may have been killed or crashed; re-run DRC",
            )

        try:
            from ..drc.report import DRCReport

            report = DRCReport.load(report_path)

            # Issue #7091: a report generated for a different PCB (e.g. the
            # ``*_routed`` sibling's report auto-discovered next to the
            # unrouted board) is not evidence for this board -- treat it like
            # a missing report.
            other = report_source_mismatch(report.pcb_name, self.pcb_path)
            if other is not None:
                return PreflightResult(
                    name="drc",
                    status="FAIL" if self.config.strict_report_source else "WARN",
                    message=f"DRC report is for {other}, not {self.pcb_path.name}",
                    details=(
                        f"{report_path} was generated for another board; re-run DRC "
                        "on this PCB or pass --drc-report"
                    ),
                )

            # Issue #7153: a kct-check report records the manufacturer/tier
            # whose rules produced it.  A report built for other rules (a
            # board's ``jlcpcb-tier1`` report read by a ``--mfr jlcpcb``
            # run) is not evidence for this run's rule set.
            mfr_mismatch = self._drc_report_manufacturer_mismatch(report)

            error_count = report.error_count
            warning_count = report.warning_count
            # Issue #7091: since #4498 KiCad's ``unconnected_items`` (opens)
            # live outside ``violations``/``error_count``; count the
            # error-severity ones explicitly so an unrouted board FAILs.
            open_errors = report.connectivity_errors()

            if error_count > 0 or open_errors:
                errors = report.errors
                if open_errors:
                    message = (
                        f"DRC has {error_count} error(s), {len(open_errors)} unconnected item(s)"
                    )
                else:
                    message = f"DRC has {error_count} error(s)"
                # List the first few findings from both collections.
                if errors and open_errors:
                    shown = [*errors[:3], *open_errors[:3]]
                else:
                    shown = [*errors, *open_errors][:5]
                details = "; ".join(_describe_drc_finding(v) for v in shown)
                if mfr_mismatch is not None:
                    details = f"{details}; {mfr_mismatch[0]}" if details else mfr_mismatch[0]
                return PreflightResult(
                    name="drc",
                    status="FAIL",
                    message=message,
                    details=details,
                )

            if mfr_mismatch is not None:
                message, details = mfr_mismatch
                return PreflightResult(
                    name="drc",
                    status="FAIL" if self.config.strict_report_source else "WARN",
                    message=message,
                    details=details,
                )

            # Issue #7190: a passing verdict counts only when the report is
            # bound to this PCB's content.  Checked here, on the pass branch,
            # so an unbound report with errors still FAILs above.
            if auto_discovered:
                unbound = self._unbound_report_result(
                    "drc", report_path, self.pcb_path, report.pcb_sha256
                )
                if unbound is not None:
                    return unbound

            if warning_count > 0:
                return PreflightResult(
                    name="drc",
                    status="WARN",
                    message=f"DRC passed with {warning_count} warning(s)",
                )

            return PreflightResult(
                name="drc",
                status="OK",
                message="DRC: 0 errors, 0 warnings",
            )
        except Exception as e:
            return PreflightResult(
                name="drc",
                status="WARN",
                message=f"Could not parse DRC report: {e}",
            )

    def _drc_report_manufacturer_mismatch(self, report) -> tuple[str, str] | None:
        """Return ``(message, details)`` when the DRC report's rules differ.

        Compares the report's recorded manufacturer with this run's, both
        resolved to canonical ids.  Capability tiers are NOT folded into
        their fab family: ``jlcpcb-tier1`` and ``jlcpcb`` check different
        rules, so a tier difference is a mismatch (issue #7153).  Reports
        without a recorded manufacturer (kicad-cli) are never a mismatch.
        """
        report_mfr = getattr(report, "manufacturer", None)
        if not report_mfr:
            return None
        from ..manufacturers import canonical_manufacturer_id

        report_id = canonical_manufacturer_id(report_mfr)
        run_id = canonical_manufacturer_id(self.manufacturer)
        if report_id == run_id:
            return None

        message = f"DRC report was generated for {report_id}, not {run_id}"
        parts = [f"its 0-error verdict was computed against {report_id} rules"]
        overrides = getattr(report, "fabrication_overrides", None)
        if overrides:
            option = overrides.get("paid_factory_option")
            note = "it also applied a board fabrication_overrides block"
            if option:
                note += f" ({option})"
            parts.append(note)
        parts.append(
            f"re-run DRC with --mfr {run_id}, or export with --mfr {report_id} to match the report"
        )
        return message, "; ".join(parts)

    def _check_erc(self) -> PreflightResult:
        """Check ERC status from a pre-existing report file."""
        report_path = self.config.erc_report_path
        auto_discovered = report_path is None  # Issue #7190, see _check_drc

        if report_path is None:
            # Try to find an ERC report next to the schematic
            search_dir = self.schematic_path.parent if self.schematic_path else self.pcb_path.parent
            auto_path = search_dir / "erc_report.json"
            if not auto_path.exists():
                auto_path = search_dir / "erc_report.txt"
            if auto_path.exists():
                report_path = auto_path
            else:
                return PreflightResult(
                    name="erc",
                    status="WARN",
                    message="No ERC report found; run kicad-cli first",
                    details="Use --erc-report to specify a report file, or --skip-erc to skip",
                )

        report_path = Path(report_path)
        if not report_path.exists():
            return PreflightResult(
                name="erc",
                status="WARN",
                message=f"ERC report not found: {report_path}",
            )

        # Issue #6724: an empty report (kicad-cli killed before writing it)
        # parses as zero findings; it is not evidence of a clean ERC.
        if report_path.stat().st_size == 0:
            return PreflightResult(
                name="erc",
                status="WARN",
                message=f"ERC report is empty: {report_path}",
                details="kicad-cli may have been killed or crashed; re-run ERC",
            )

        try:
            from ..erc.report import ERCReport

            report = ERCReport.load(report_path)

            # Issue #7091: a report recorded for a different schematic is not
            # evidence for this one.  ``require_suffix`` skips the fallback
            # where ``source_file`` is the report's own path (no ``source``).
            other = report_source_mismatch(
                report.source_file, self.schematic_path, require_suffix=".kicad_sch"
            )
            if other is not None and self.schematic_path is not None:
                return PreflightResult(
                    name="erc",
                    status="FAIL" if self.config.strict_report_source else "WARN",
                    message=f"ERC report is for {other}, not {self.schematic_path.name}",
                    details=(
                        f"{report_path} was generated for another schematic; re-run "
                        "ERC on this schematic or pass --erc-report"
                    ),
                )

            error_count = report.error_count
            warning_count = report.warning_count

            if error_count > 0:
                errors = report.errors
                return PreflightResult(
                    name="erc",
                    status="FAIL",
                    message=f"ERC has {error_count} error(s)",
                    details="; ".join(f"{v.type_str}: {v.description}" for v in errors[:5]),
                )

            # Issue #7190: same pass-branch binding rule as _check_drc.
            if auto_discovered:
                unbound = self._unbound_report_result(
                    "erc",
                    report_path,
                    self._erc_report_schematic(report_path, report.source_file),
                    report.schematic_sha256,
                )
                if unbound is not None:
                    return unbound

            if warning_count > 0:
                return PreflightResult(
                    name="erc",
                    status="WARN",
                    message=f"ERC passed with {warning_count} warning(s)",
                )

            return PreflightResult(
                name="erc",
                status="OK",
                message="ERC: 0 errors, 0 warnings",
            )
        except Exception as e:
            return PreflightResult(
                name="erc",
                status="WARN",
                message=f"Could not parse ERC report: {e}",
            )

    def _erc_report_schematic(self, report_path: Path, recorded_source: str) -> Path | None:
        """The schematic an auto-discovered ERC report should be bound to.

        The configured schematic when known; otherwise the ``.kicad_sch``
        the report names, looked up beside the report.
        """
        if self.schematic_path is not None:
            return Path(self.schematic_path)
        name = re.split(r"[\\/]", (recorded_source or "").strip())[-1]
        if not name.lower().endswith(".kicad_sch"):
            return None
        candidate = report_path.parent / name
        return candidate if candidate.is_file() else None

    def _unbound_report_result(
        self,
        name: str,
        report_path: Path,
        source_path: Path | None,
        recorded_hash: str | None,
    ) -> PreflightResult | None:
        """Downgrade a passing auto-discovered report that is not content-bound.

        Issue #7190: a ``drc_report.json``/``erc_report.json`` found beside
        the board says nothing about which *version* of the board it checked
        (a git checkout resets mtimes).  It may pass the board only when it
        is bound (see :mod:`kicad_tools.drc.report_binding`):

        * ``MISMATCH`` (records a different hash: the file changed since the
          report was generated) is FAIL under ``strict_report_source``, else
          WARN -- the same shape as the #7091/#7153 mismatch returns;
        * ``UNHASHED`` (no hash, no readiness provenance) is WARN even when
          strict, matching #7091's missing-report policy.

        Returns ``None`` when the report is bound.
        """
        from ..drc.report_binding import BindingStatus, binding_status

        status = binding_status(
            report_path, source_path, recorded_hash, hierarchical=(name == "erc")
        )
        if status is BindingStatus.BOUND:
            return None

        label = name.upper()
        # Issue #6532: the details land in committed manifests; never record
        # an absolute host path there.
        from ..cli.check_cmd import portable_report_path

        shown = portable_report_path(report_path)
        if name == "drc":
            target, key, rerun = (
                "PCB",
                "pcb_sha256",
                "re-run kct check --output or pass --drc-report",
            )
        else:
            target, key, rerun = (
                "schematic",
                "schematic_sha256",
                "re-run kct build's ERC step or pass --erc-report",
            )

        if status is BindingStatus.MISMATCH:
            return PreflightResult(
                name=name,
                status="FAIL" if self.config.strict_report_source else "WARN",
                message=(
                    f"{label} report is not bound to this {target} "
                    "(stale: content changed since it was generated)"
                ),
                details=(
                    f"{shown} records a {key} that differs from the current "
                    f"{target}'s; its verdict describes an older version -- {rerun}"
                ),
            )
        return PreflightResult(
            name=name,
            status="WARN",
            message=f"{label} report is not bound to this {target} (unhashed); {rerun}",
            details=(
                f"{shown} records no {key} and no output/readiness.json records it "
                f"as produced from this {target}, so its verdict is not evidence for the "
                f"current {target}"
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _spec_lcsc_refs(self) -> set[str]:
        """Return references whose LCSC will be populated by the spec overlay.

        Preflight (Step 0) inspects a *raw* BOM, but the assembly pipeline
        (Step 1) applies the project ``.kct`` spec overlay before shipping
        the BOM.  This helper mirrors the overlay's ref resolution so that
        ``_check_bom_fields`` can tell which "missing LCSC" items will in
        fact be filled in from the spec, and avoid a false-positive warning
        (issue #3926).

        Only references that both (a) match a spec ``bom_entries`` entry and
        (b) carry a non-empty ``lcsc`` value in that entry are returned --
        an entry with only an MPN does not resolve the LCSC gap.

        The lookup is best-effort: if no spec file is found or it cannot be
        parsed, an empty set is returned so the check falls back to its
        original (warn-on-missing) behaviour.
        """
        try:
            from .bom_spec_overlay import expand_ref_range

            spec, _ = self._load_project_spec()
            if spec is None or not spec.bom_entries:
                return set()

            refs: set[str] = set()
            for entry in spec.bom_entries:
                if not entry.lcsc:
                    continue
                for ref in expand_ref_range(entry.ref):
                    refs.add(ref)
            return refs
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("Spec LCSC resolution failed (ignoring): %s", e)
            return set()

    def _spec_exemptions(self) -> dict[str, tuple[str, str]]:
        """Refs the ``.kct`` spec declares deliberately non-LCSC (issue #8261).

        Best-effort like the overlay: no/invalid spec means no exemptions.
        """
        try:
            from .bom_spec_overlay import declared_exemptions

            spec, _ = self._load_project_spec()
            if spec is None:
                return {}
            return declared_exemptions(spec.bom_entries) if spec.bom_entries else {}
        except Exception as e:
            logger.debug("Spec exemption lookup failed (ignoring): %s", e)
            return {}

    @classmethod
    def _exempt_suffix(
        cls,
        exempt_seen: dict[str, tuple[str, str]],
        conflicts: list[str],
        stale: list[str],
        lead: bool = False,
    ) -> str:
        """Details text for declared exemptions, conflicts and stale refs (#8261)."""
        parts: list[str] = []
        if exempt_seen:
            parts.append(cls._format_exempt(exempt_seen))
        if conflicts:
            parts.append("exempt ref carries a verifiable LCSC part: " + "; ".join(conflicts))
        if stale:
            parts.append("stale exemption (no such ref): " + ", ".join(stale))
        text = " | ".join(parts)
        return text if lead or not text else f" | {text}"

    @staticmethod
    def _format_exempt(exempt_seen: dict[str, tuple[str, str]]) -> str:
        return "exempt (declared): " + "; ".join(
            f"{ref} [{cat}: {why}]" for ref, (cat, why) in sorted(exempt_seen.items())
        )

    def _get_parts_cache(self):
        if self._parts_cache is None:
            from ..parts.cache import PartsCache

            self._parts_cache = PartsCache()
        return self._parts_cache

    def _items_with_spec_overlay(self, bom) -> list:
        """Return copies of ``bom.items`` with the ``.kct`` overlay and CSV merge applied.

        Mirrors the assembly export (issues #7180, #8154) so ``bom_lcsc_values``
        and ``bom_lcsc_stock`` validate the parts the written BOM actually
        carries: schematic fields, then the ``.kct`` LCSC/MPN overlay, then
        LCSC numbers merged from an existing ``bom_<fab>.csv`` in
        ``output_dir``.  Works on copies: the cached BOM other checks read
        (e.g. ``bom_fields``) is left untouched.  Best-effort -- a missing or
        malformed spec/CSV is logged and skipped, matching the export pipeline.
        """
        import copy

        items = [copy.copy(i) for i in bom.items]
        spec_refs: set[str] = set()
        exempt_refs: set[str] = set()
        try:
            from .bom_spec_overlay import (
                apply_spec_overlay,
                declared_exemptions,
                resolved_refs,
            )

            spec, _ = self._load_project_spec()
            if spec and spec.bom_entries:
                spec_refs = resolved_refs(apply_spec_overlay(items, spec.bom_entries))
                exempt_refs = set(declared_exemptions(spec.bom_entries))
        except Exception as e:
            logger.warning("Spec overlay failed in preflight (continuing without): %s", e)

        # Use the same catalogue evidence as assembly export for CSV assignments.
        if self.output_dir is not None:
            try:
                from .assembly import AssemblyConfig
                from .bom_formats import (
                    apply_existing_lcsc_assignments,
                    read_existing_lcsc_assignments,
                )

                cfg = AssemblyConfig()
                if self._merge_lcsc:
                    csv_path = self._merge_lcsc_from or (
                        self.output_dir / cfg.bom_filename.format(manufacturer=self._fab_family)
                    )
                    if csv_path.is_file():
                        existing = read_existing_lcsc_assignments(csv_path)
                        if existing:
                            try:
                                cache = self._get_parts_cache()
                            except Exception as e:
                                logger.debug(
                                    "Parts cache unavailable for CSV-merge validation: %s", e
                                )
                                cache = None
                            apply_existing_lcsc_assignments(
                                [
                                    i
                                    for i in items
                                    if i.reference not in exempt_refs
                                    and not (i.reference in spec_refs and i.mpn and not i.lcsc)
                                ],
                                existing,
                                parts_cache=cache,
                            )
            except Exception as e:
                logger.warning("BOM CSV merge failed in preflight (continuing without): %s", e)
        return items

    def _load_bom(self):
        """Load and cache BOM data.

        Respects the ``bom_source`` setting: when set to ``"pcb"`` or
        ``"auto"`` (with no usable schematic), the BOM is extracted
        from PCB footprints instead of schematic symbols.
        """
        if self._bom is not None:
            return self._bom

        if self._bom_source == "pcb":
            from ..schema.bom import extract_bom_from_pcb

            self._bom = extract_bom_from_pcb(str(self.pcb_path))
            return self._bom

        if self._bom_source == "auto":
            # Try schematic first, fall back to PCB
            if self.schematic_path and self.schematic_path.exists():
                from ..schema.bom import backfill_footprints_from_pcb, extract_bom

                self._bom = extract_bom(str(self.schematic_path))
                # Mirror the BOM CSV pipeline: back-fill blank footprints
                # from the PCB so the schematic-only preflight check sees
                # the same footprint metadata that ships in the BOM CSV.
                # Best-effort -- bom_fields will still flag any real gaps.
                with contextlib.suppress(Exception):
                    backfill_footprints_from_pcb(self._bom.items, self.pcb_path)
            else:
                from ..schema.bom import extract_bom_from_pcb

                self._bom = extract_bom_from_pcb(str(self.pcb_path))
            return self._bom

        # Default: schematic source
        if self.schematic_path is None or not self.schematic_path.exists():
            raise FileNotFoundError("Schematic file not available for BOM extraction")

        from ..schema.bom import backfill_footprints_from_pcb, extract_bom

        self._bom = extract_bom(str(self.schematic_path))
        # Mirror the BOM CSV pipeline: back-fill blank footprints from the
        # PCB so the preflight bom_fields check matches what is actually
        # written to bom_<manufacturer>.csv (spec-overlay-style schematics
        # leave instance-level Footprint properties blank).
        # Best-effort -- bom_fields will still flag any real gaps.
        with contextlib.suppress(Exception):
            backfill_footprints_from_pcb(self._bom.items, self.pcb_path)
        return self._bom

    def _get_manufacturer_limits(self) -> tuple[float, float]:
        """Get max board dimensions for the configured manufacturer.

        Returns:
            Tuple of (max_width_mm, max_height_mm).
        """
        try:
            from ..manufacturers import get_profile

            profile = get_profile(self.manufacturer)
            rules = profile.get_design_rules(layers=2)
            return (rules.max_board_width_mm, rules.max_board_height_mm)
        except Exception:
            # Fall back to generous defaults
            return (400.0, 500.0)
