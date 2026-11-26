"""Nominal component-opening evidence for a mask-to-copper assessment."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .mask_copper import MaskCopperAssessment, MaskCopperMeasurement


_CATEGORIES = (
    "foreign_net",
    "same_net_beyond_land",
    "registration_or_near_pad_only",
    "no_nominal_exposure",
    "unevaluated",
    "excluded",
)
_OPENING_COUNTS = ("plotted", "matched", "unresolved", "excluded")
_MEASURED_COUNTS = ("evaluated", "foreign_net", "same_net_beyond_land")


def construction_uncertainty_mm(provenance):
    """Two independent native constructions plus 4 nm of Gerber rounding."""
    return 2 * provenance.get("max_error_mm_per_construction", 0) + 0.000004


def _ownership(item, component, layer):
    if not item["kind"].startswith("pad:"):
        return "excluded", "non_pad_" + item["kind"]
    footprint = (item.get("footprint_name") or "").lower()
    if "mountinghole" in footprint:
        return "excluded", "mounting_hole"
    if item.get("pad_type") == "np_thru_hole":
        return "excluded", "non_plated_hole"
    if layer is None:
        return "unresolved", "missing_copper_layer"
    land = item["layers_geometry"].get(layer)
    if land is None or land.is_empty or component.intersection(land).area == 0:
        return "unresolved", "no_attributed_land"
    if not item["net"]:
        fiducial = "fiducial" in footprint or item.get("pad_property") in (
            "pad_prop_fiducial_glob",
            "pad_prop_fiducial_loc",
        )
        return "excluded", "unnetted_fiducial" if fiducial else "unnetted_pad"
    return "matched", None


def opening_inventory(components, openings, objects, attributed_lands, layer, measurement_starts):
    """Reconcile connected plotted material with the exact opening instances measured.

    The assessor can measure several source openings and a residual bridge
    separately on a single merged component. Keep their rows together. A
    source spanning disconnected components has no per-component pair area.
    """
    from shapely import STRtree  # type: ignore[import-untyped]

    components = [c for c in components if not c.is_empty]
    index = STRtree(components)
    sources: list[list[int]] = [[] for _ in components]
    split = set()
    for i, opening in enumerate(openings):
        material = opening[1]
        hits = [j for j in index.query(material) if material.intersection(components[j]).area > 0]
        if len(hits) > 1:
            split.add(i)
        for j in hits:
            sources[j].append(i)
    inventory = []
    for component, parts in zip(components, sources, strict=True):
        uuids = sorted({u for i in parts for u in openings[i][0]})
        item = {}
        if len(uuids) == 1:
            uuid = uuids[0]
            # A separate mask pad can have a proven electrical owner in the
            # assessor. Preserve the aperture UUID when using that land.
            item = attributed_lands.get(uuid, objects[uuid])
        if not uuids:
            status, reason = "unresolved", "unattributed_region"
        elif len(parts) != 1 or openings[parts[0]][2] == "merged_region":
            status, reason = "unresolved", "merged_region"
        elif parts[0] in split:
            status, reason = "unresolved", "multi_component_source"
        else:
            status, reason = _ownership(item, component, layer)
        inventory.append(
            {
                "opening_uuids": uuids,
                "bounds_mm": list(component.bounds),
                "status": status,
                "reason": reason,
                "owner_ref": item.get("pad_ref"),
                "owner_net": item.get("net") or None,
                "footprint_ref": item.get("footprint_ref"),
                "measurement_indices": [
                    n
                    for i in parts
                    for n in range(measurement_starts[i], measurement_starts[i + 1])
                ],
            }
        )
    return inventory


def _category(m: MaskCopperMeasurement, ownership: str) -> str:
    if ownership == "excluded":
        return "excluded"
    if ownership != "matched":
        return "unevaluated"
    if m.exposed_area_mm2 > 0:
        if m.same_net is False:
            return "foreign_net"
        if m.same_net is None or m.exposed_beyond_owner_mm2 is None:
            return "unevaluated"
        if m.exposed_beyond_owner_mm2 > 0:
            return "same_net_beyond_land"
    if m.disposition in ("violation", "uncertain", "warning"):
        return "registration_or_near_pad_only"
    return "no_nominal_exposure"


def _measured_openings(openings, indices):
    """Distinct matched lands with fully classified pairs, including quiet lands."""
    counts = dict.fromkeys(_MEASURED_COUNTS, 0)
    unknown = set(indices["unevaluated"])
    exposed = {k: set(indices[k]) for k in ("foreign_net", "same_net_beyond_land")}
    for opening in openings:
        rows = opening["measurement_indices"]
        if opening["status"] == "matched" and unknown.isdisjoint(rows):
            counts["evaluated"] += 1
            for kind, exposed_rows in exposed.items():
                if not exposed_rows.isdisjoint(rows):
                    counts[kind] += 1
    if not counts["evaluated"] and (unknown or any(o["status"] == "unresolved" for o in openings)):
        return {"evaluated": 0, "foreign_net": None, "same_net_beyond_land": None}
    return counts


def nominal_exposure(assessment: MaskCopperAssessment) -> dict[str, Any]:
    """Count observed pairs and preserve unknowns when coverage is partial."""
    export = assessment.geometry_provenance.get("export", {})
    faces: dict[str, dict[str, Any]] = {}
    for side in ("F.Mask", "B.Mask"):
        layer = side.replace(".Mask", ".Cu")
        missing = [k for k in (side, layer) if k not in export.get("layers_wkt", {})]
        openings = (assessment.aperture_inventory or {}).get(side)
        ownership = {
            n: opening["status"]
            for opening in openings or ()
            for n in opening["measurement_indices"]
        }
        indices: dict[str, list[int]] = {k: [] for k in _CATEGORIES}
        for n, m in enumerate(assessment.measurements):
            if m.mask_side == side:
                category = (
                    "unevaluated" if missing else _category(m, ownership.get(n, "unresolved"))
                )
                indices[category].append(n)
        counts = Counter(o["status"] for o in openings or ())
        if assessment.aperture_inventory is None or (openings is None and not missing):
            status = "inventory_unavailable"
        elif missing:
            status = "missing_layer"
        elif assessment.coverage != "complete" or counts["unresolved"] or indices["unevaluated"]:
            status = "partial"
        else:
            status = "complete"
        counts["plotted"] = len(openings or ())
        faces[side] = {
            "status": status,
            "missing_layers": missing,
            "gerber_sha256": {k: export.get("gerber_sha256", {}).get(k) for k in (side, layer)},
            "openings": openings,
            "opening_counts": {
                k: counts[k] if openings is not None else None for k in _OPENING_COUNTS
            },
            "measured_opening_counts": (
                _measured_openings(openings, indices)
                if openings is not None and not missing and assessment.coverage == "complete"
                else dict.fromkeys(_MEASURED_COUNTS)
            ),
            "exclusions_by_reason": dict(
                Counter(o["reason"] for o in openings or () if o["status"] == "excluded")
            ),
            "measurement_indices": indices,
            "finding_counts": {
                k: len(rows) if rows or status == "complete" else None
                for k, rows in indices.items()
            },
        }

    def totals(field, keys):
        combined = {}
        for key in keys:
            values = [face[field][key] for face in faces.values()]
            known = sum(v for v in values if v is not None)
            combined[key] = known if known or None not in values else None
        return combined

    status = "complete" if all(f["status"] == "complete" for f in faces.values()) else "partial"
    if assessment.aperture_inventory is None:
        status = "inventory_unavailable"
    return {
        "artwork_source": "internal_replot" if export else None,
        "status": status,
        "nominal_area_threshold_mm2": 0.0,
        "construction_uncertainty_mm": (
            construction_uncertainty_mm(assessment.geometry_provenance.get("objects", {}))
            if assessment.aperture_inventory is not None
            else None
        ),
        "opening_counts": totals("opening_counts", _OPENING_COUNTS),
        "measured_opening_counts": totals("measured_opening_counts", _MEASURED_COUNTS),
        "finding_counts": totals("finding_counts", _CATEGORIES),
        "faces": faces,
    }
