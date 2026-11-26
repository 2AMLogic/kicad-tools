"""Nominal reinforcement-clearance measurements on the shipped Gerber archive (Issue #10422).

For every run the ``<board>.reinforcement.json`` sidecar records, the export
measures the copper and mask artwork that will actually ship.

**Which openings.** The joined mask strip on the run's own face, and each
recorded anchor's opening on both faces. The strip is the chain of plotted
strokes whose end points join the run's anchors; an anchor opening is the mask
material standing on the anchor. They are kept apart in the result, so a close
approach at an anchor is told from one along a span.

**Two independent edge-to-edge minima** per opening, in mm. The first is to any
plotted copper of another net on that face, covered or not. The second is to the
part of that copper the same face's final mask leaves exposed; a partly exposed
conductor is clipped to the mask openings before the distance is taken. Each
minimum names its own nearest net, the pad when the artwork identifies one, the
run, face and opening it belongs to, and the copper and mask archive members it
was measured against, with their SHA-256 digests. ``overlap_mm2`` is how much of
the nearest conductor the opening covers, and ``exposed`` on the second minimum
says whether that is a positive area: a shifted strip that crosses foreign
copper reads ``0.0`` with ``exposed`` true, an edge contact ``0.0`` with
``exposed`` false.

**No verdict.** The numbers are nominal geometry for engineering review. No
voltage or creepage threshold is applied, the board-specific electrical spacing
decision stays where it was, and the existing fabrication checks do not read
these values.

Coverage is reported, never guessed
===================================

A measurement is ``complete`` only where the artwork proves the match it rests
on. Otherwise it is ``incomplete`` with a reason for the affected opening, and
valid results on the other face are kept:

* ``missing_artwork`` -- a copper or mask member is absent from the archive,
  ambiguous, unreadable, or not the layer its name claims.
* ``missing_net_attribute`` -- no plotted copper on the face carries the run's
  X2 net, so the run cannot be found in the artwork.
* ``unverified_registration`` -- a recorded anchor does not stand on the run's
  copper in the plotted frame (the sidecar-to-plot origin does not match the
  artwork), or no plotted mask opening stands on an anchor (the mask is not
  registered with the copper there).
* ``ambiguous_run_ownership`` -- another record claims the same plotted strip,
  or a record of another net claims the same anchor opening.
* ``opening_not_plotted`` -- no plotted strip stroke joins the anchors.
* ``unattributed_copper`` (one minimum only) -- copper plotted without an X2
  net attribute lies no farther than the nearest attributed copper, so it could
  change that minimum.

A complete face with no eligible foreign copper reports no candidate. Copper
plotted after an attribute reset is not "no foreign copper": its net is unknown,
and it is treated as such.
"""

from __future__ import annotations

import hashlib
import math
import re
import zipfile
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import shapely  # type: ignore[import-untyped]
from shapely import STRtree
from shapely.geometry import MultiPoint, Point, box  # type: ignore[import-untyped]
from shapely.ops import nearest_points, unary_union  # type: ignore[import-untyped]

from ..validate.gerber_geometry import (
    GERBER_CHORD_ERROR_MM,
    GerberFeature,
    parse_gerber_geometry,
)
from ..validate.mask_copper import ReinforceRun, grow_reinforce_chain, reinforcement_run_records
from ..validate.mask_copper_exposure import construction_uncertainty_mm
from ..validate.rules.reinforcement_incomplete import MASK_TOLERANCE_MM
from .preflight import PreflightResult

SCHEMA = "kct.reinforcement-clearance.v1"
CHECK_NAME = "reinforcement_clearance"
SCOPE = "nominal geometry of the shipped Gerber archive for engineering review; no spacing verdict"

#: The outer copper and mask layers, in the order they are reported.
LAYERS = ("B.Cu", "F.Cu", "B.Mask", "F.Mask")
SIDES = ("B", "F")
_MEMBER_SUFFIX = {"F.Cu": "F_Cu", "B.Cu": "B_Cu", "F.Mask": "F_Mask", "B.Mask": "B_Mask"}
#: ``%TF.FileFunction`` of each layer as KiCad writes it: ``(kind, side)``.
_FILE_FUNCTION = {
    "F.Cu": ("Copper", "Top"),
    "B.Cu": ("Copper", "Bot"),
    "F.Mask": ("Soldermask", "Top"),
    "B.Mask": ("Soldermask", "Bot"),
}
_MEMBER_RE = {
    layer: re.compile(rf"-{suffix}\.[A-Za-z0-9]+$") for layer, suffix in _MEMBER_SUFFIX.items()
}
_FILE_FUNCTION_RE = re.compile(r"%TF\.FileFunction,([^*\s]+)\*%")

ALL_COPPER = "all_copper"
EXPOSED = "exposed"

# Two independently constructed curves differ by at most the construction
# budget: a gap that small is edge contact, not a distance.
_UNCERTAINTY_MM = construction_uncertainty_mm(GERBER_CHORD_ERROR_MM)
# Areas at or below this (mm²) are numerical zero, not an exposure.
_AREA_EPSILON_MM2 = 1e-9
_TIE_MM = 1e-9


@dataclass
class ClearanceMinimum:
    """One of an opening's two edge-to-edge minima, in mm."""

    kind: str
    status: str
    reason: str | None = None
    detail: str | None = None
    distance_mm: float | None = None
    net: str | None = None
    pad: str | None = None
    overlap_mm2: float | None = None
    exposed: bool | None = None
    location_mm: list[float] | None = None
    members: dict[str, Any] = field(default_factory=dict)
    #: the run, face and opening the minimum belongs to, so it stands alone.
    run: str | None = None
    face: str | None = None
    opening: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "run": self.run,
            "face": self.face,
            "opening": self.opening,
            "reason": self.reason,
            "detail": self.detail,
            "distance_mm": self.distance_mm,
            "net": self.net,
            "pad": self.pad,
            "overlap_mm2": self.overlap_mm2,
            "exposed": self.exposed,
            "location_mm": self.location_mm,
            "members": self.members,
        }


@dataclass
class OpeningMeasurement:
    """The two minima of one plotted opening a sidecar run claims."""

    opening: str
    face: str
    anchor: int | None
    anchor_mm: list[float] | None
    status: str
    reason: str | None
    detail: str | None
    all_copper: ClearanceMinimum
    exposed: ClearanceMinimum

    def to_dict(self) -> dict[str, Any]:
        return {
            "opening": self.opening,
            "face": self.face,
            "anchor": self.anchor,
            "anchor_mm": self.anchor_mm,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "all_copper": self.all_copper.to_dict(),
            "exposed": self.exposed.to_dict(),
        }


@dataclass
class RunMeasurement:
    """Every opening one sidecar run claims."""

    label: str
    net: str
    layer: str
    anchors: list[list[float]]
    openings: list[OpeningMeasurement]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "net": self.net,
            "layer": self.layer,
            "anchors": self.anchors,
            "openings": [o.to_dict() for o in self.openings],
        }


@dataclass
class ReinforcementClearanceReport:
    """The nominal measurements of one Gerber archive against one sidecar."""

    coverage: str
    members: dict[str, dict[str, Any]]
    runs: list[RunMeasurement]
    archive: dict[str, Any]
    plot_origin_mm: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "scope": SCOPE,
            "coverage": self.coverage,
            "archive": self.archive,
            "plot_origin_mm": self.plot_origin_mm,
            "coordinate_frame": "sheet-absolute (sidecar frame), y down, mm",
            "members": self.members,
            "runs": [run.to_dict() for run in self.runs],
        }

    def preflight_result(self) -> PreflightResult:
        """The report as one entry of the package's preflight report (Issue #10422).

        The status says only whether every measurement could be made. It never
        judges the distances, so it never becomes an export failure.
        """
        openings = [o.to_dict() for run in self.runs for o in run.openings]
        minima = [m for o in openings for m in (o["all_copper"], o["exposed"])]
        complete = sum(m["status"] == "complete" for m in minima)
        status: Literal["OK", "WARN"] = "OK" if self.coverage == "complete" else "WARN"
        counted = f"{complete} of {len(minima)}" if status == "WARN" else f"{complete}"
        return PreflightResult(
            name=CHECK_NAME,
            status=status,
            message=(
                f"Reinforcement clearance: {counted} measurement(s) complete on "
                f"{len(openings)} opening(s) of the shipped Gerbers "
                "(nominal, no spacing verdict)"
            ),
            details="; ".join(describe_opening(o) for o in openings),
            data=self.to_dict(),
        )


def describe_opening(opening: dict[str, Any]) -> str:
    """One line per opening, e.g. ``B span: 0.500 mm any copper (SCAP_POS); ...``."""
    where = f"{opening['face']} {opening['opening']}" + (
        f" {opening['anchor']}" if opening["anchor"] else ""
    )
    if opening["status"] == "incomplete":
        return f"{where}: incomplete ({opening['reason']})"
    return (
        f"{where}: {_describe_minimum(opening['all_copper'])}; "
        f"{_describe_minimum(opening['exposed'])}"
    )


def summary_lines(report: dict[str, Any]) -> list[str]:
    """A ``to_dict()`` report as README lines: one header per run, one line per opening."""
    lines: list[str] = []
    for run in report["runs"]:
        lines.append(f"  {run['label']}, net {run['net']} on {run['layer']}")
        lines.extend(f"    {describe_opening(o)}" for o in run["openings"])
    return lines


def _describe_minimum(minimum: dict[str, Any]) -> str:
    what = "any copper" if minimum["kind"] == ALL_COPPER else "exposed copper"
    if minimum["status"] == "incomplete":
        return f"incomplete ({minimum['reason']})"
    distance = minimum["distance_mm"]
    if distance is None:
        return (
            "no copper of another net on this face"
            if minimum["kind"] == ALL_COPPER
            else "no exposed copper of another net on this face"
        )
    who = minimum["net"] or "(no net)"
    if minimum["pad"]:
        who += f" {minimum['pad']}"
    text = f"{distance:.3f} mm {what} ({who})"
    if minimum["kind"] == EXPOSED and minimum["exposed"]:
        text += f", exposed {minimum['overlap_mm2']:.3f} mm²"
    return text


# --------------------------------------------------------------------------- archive members


@dataclass
class _Layer:
    """One copper or mask member of the archive, read into geometry."""

    layer: str
    member: str | None = None
    sha256: str | None = None
    image: Any = None
    features: tuple[GerberFeature, ...] = ()
    by_index: dict[int, GerberFeature] = field(default_factory=dict)
    problem: str | None = None

    def identity(self) -> dict[str, Any]:
        return {"layer": self.layer, "member": self.member, "sha256": self.sha256}

    def record(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "status": "missing_artwork" if self.problem else "read",
            "member": self.member,
            "sha256": self.sha256,
            "detail": self.problem,
        }


def _load_layers(names: Iterable[str], read: Callable[[str], bytes]) -> dict[str, _Layer]:
    """Select each layer by its full member name before reading any payloads."""
    names = sorted(names)
    layers = {}
    for layer in LAYERS:
        entry = layers[layer] = _Layer(layer)
        matches = [name for name in names if _MEMBER_RE[layer].search(name)]
        if not matches:
            entry.problem = "no archive member for this layer"
        elif len(matches) > 1:
            entry.problem = "ambiguous archive members: " + ", ".join(matches)
        else:
            entry.member = matches[0]
            try:
                data = read(matches[0])
            except (OSError, EOFError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
                entry.problem = f"archive member unreadable: {exc}"
            else:
                entry.sha256 = hashlib.sha256(data).hexdigest()
                entry.problem = _read_layer(entry, data)
    return layers


def _read_layer(entry: _Layer, data: bytes) -> str | None:
    """Fill ``entry`` from one member's bytes; returns why it could not be read."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "member is not text"
    function = _FILE_FUNCTION_RE.search(text)
    if function:
        names = function.group(1).split(",")
        if (names[0], names[-1]) != _FILE_FUNCTION[entry.layer]:
            return f"FileFunction is {function.group(1)}, not the layer its name claims"
    try:
        parsed = parse_gerber_geometry(text, with_features=True)
    except ValueError as exc:
        return f"unreadable Gerber: {exc}"
    entry.image = parsed.geometry
    entry.features = parsed.features
    entry.by_index = {feature.index: feature for feature in parsed.features}
    return None


# --------------------------------------------------------------------------- one face


class _Face:
    """The plotted copper and mask of one face, indexed for the minima."""

    def __init__(self, side: str, layers: dict[str, _Layer]) -> None:
        self.side = side
        self.copper = layers[f"{side}.Cu"]
        self.mask = layers[f"{side}.Mask"]
        self.by_net: dict[str, list[GerberFeature]] = defaultdict(list)
        self._own_trees: dict[str, Any] = {}
        self._candidates: dict[tuple[str | None, bool], tuple[list[Any], list[Any], Any]] = {}
        self._exposed: dict[int, Any] = {}
        self._mask_tree: Any = None
        self._hulls: Any = None
        for feature in self.copper.features:
            if feature.net is not None:
                self.by_net[feature.net].append(feature)

    def problem(self) -> str | None:
        for layer in (self.copper, self.mask):
            if layer.problem:
                return f"{layer.layer}: {layer.problem}"
        return None

    def members(self) -> dict[str, Any]:
        return {"copper": self.copper.identity(), "mask": self.mask.identity()}

    def on_copper(self, point: Any, net: str, tolerance: float) -> bool:
        """``True`` when copper attributed to ``net`` lies within ``tolerance`` of ``point``."""
        own = self.by_net.get(net)
        if not own:
            return False
        if net not in self._own_trees:
            self._own_trees[net] = STRtree([feature.geometry for feature in own])
        minx, miny, maxx, maxy = point.bounds
        window = box(minx - tolerance, miny - tolerance, maxx + tolerance, maxy + tolerance)
        return any(
            own[int(i)].geometry.distance(point) <= tolerance
            for i in self._own_trees[net].query(window)
        )

    def anchor_opening(self, point: Any, tolerance: float) -> list[GerberFeature]:
        """Mask flashes and regions standing on ``point``; strip strokes are not anchor openings."""
        if self._hulls is None:
            # The hull, not the material: an opening that surrounds its anchor
            # still matches, exactly as ``mask_copper`` attributes openings.
            features = [f for f in self.mask.features if f.operation != "draw"]
            hulls = [f.geometry.convex_hull for f in features]
            self._hulls = (hulls, features, STRtree(hulls) if hulls else None)
        hulls, features, tree = self._hulls
        if tree is None:
            return []
        minx, miny, maxx, maxy = point.bounds
        window = box(minx - tolerance, miny - tolerance, maxx + tolerance, maxy + tolerance)
        return [
            features[int(i)]
            for i in tree.query(window)
            if hulls[int(i)].distance(point) <= tolerance
        ]

    def registered(
        self, point: Any, net: str, opening: list[GerberFeature], tolerance: float
    ) -> str | None:
        """Why an anchor's opening is not registered with its copper, or ``None``.

        The opening is the mask material standing on the anchor point, as in
        ``mask_copper`` (by hull, so an opening that surrounds its anchor
        matches). That alone accepts a mask shifted by less than the opening's
        radius, so it is corroborated: a plotted pad or via opening is
        concentric with its copper whatever the mask expansion, so the centre of
        the run's own copper flashed on the anchor must coincide, to within
        ``tolerance``, with the centre of an opening standing on the anchor.
        """
        if not opening:
            if self.mask.image is None:
                return f"the {self.mask.layer} member was not read"
            distance = self.mask.image.distance(point)
            return (
                f"no plotted {self.mask.layer} opening stands on the anchor (tolerance "
                f"{tolerance:.2f} mm); the nearest plotted mask material is {distance:.3f} mm away"
            )
        centres = [feature.geometry.centroid for feature in opening]
        for flash in self.by_net.get(net, ()):
            if flash.operation == "flash":
                centre = flash.geometry.centroid
                if centre.distance(point) <= tolerance and all(
                    centre.distance(other) <= tolerance for other in centres
                ):
                    return None
        return (
            f"no {net!r} copper flash registers to both the sidecar point "
            f"and the {self.mask.layer} opening within {tolerance:.3f} mm"
        )

    def exposed(self, feature: GerberFeature) -> Any | None:
        """The feature's material inside a plotted mask opening; ``None`` when there is none."""
        if self._mask_tree is None:
            self._mask_tree = STRtree([f.geometry for f in self.mask.features])
        if feature.index not in self._exposed:
            # Intersection distributes over the mask's union, so the feature
            # is clipped against only the openings whose bounds reach it, and
            # against none at all when one opening covers it whole.
            geometry = feature.geometry
            pieces = []
            hits = self._mask_tree.query(box(*geometry.bounds))
            if len(hits):
                openings = [self.mask.features[int(i)].geometry for i in hits]
                overlapping = [
                    g
                    for g, hit in zip(openings, shapely.intersects(geometry, openings), strict=True)
                    if hit
                ]
                for opening in overlapping:
                    if shapely.covers(opening, geometry):
                        pieces = [geometry]
                        break
                    piece = geometry.intersection(opening)
                    if not piece.is_empty:
                        pieces.append(piece)
            clipped = pieces[0] if len(pieces) == 1 else unary_union(pieces) if pieces else None
            self._exposed[feature.index] = (
                clipped if clipped is not None and clipped.area > _AREA_EPSILON_MM2 else None
            )
        return self._exposed[feature.index]

    def candidates(self, net: str | None, exposed_only: bool):
        """``(features, geometries, bounds)`` of the copper a minimum is measured against.

        ``net`` names the run: the copper of every other net is returned. ``None``
        returns the copper plotted without an X2 net attribute, whose net is
        unknown. With ``exposed_only`` each geometry is clipped to the plotted
        mask openings, and copper with nothing exposed drops out.
        """
        key = (net, exposed_only)
        if key not in self._candidates:
            if net is None:
                features = [f for f in self.copper.features if f.net is None]
            else:
                features = [f for name, own in self.by_net.items() if name != net for f in own]
            geometries = [self.exposed(f) if exposed_only else f.geometry for f in features]
            kept = [pair for pair in zip(features, geometries, strict=True) if pair[1] is not None]

            bounds = shapely.bounds([geometry for _f, geometry in kept])
            self._candidates[key] = (
                [f for f, _g in kept],
                [g for _f, g in kept],
                bounds,
            )
        return self._candidates[key]

    def nearest(self, opening: Any, net: str | None, exposed_only: bool):
        """``(feature, geometry, distance)`` of the nearest candidate copper.

        At zero exposed distance, positive exposure takes priority over contact.
        Remaining ties resolve to the earliest plotted, so the report is
        reproducible. ``None`` when the face has no such copper.

        A bounding-box distance is a lower bound on the true distance, so the
        candidates are visited in that order and the exact distance is taken
        only until the next lower bound can no longer beat the best found: the
        result is the exact nearest without measuring the whole face.
        """
        features, geometries, bounds = self.candidates(net, exposed_only)
        if not features:
            return None
        minx, miny, maxx, maxy = opening.bounds
        gap_x = np.maximum(np.maximum(bounds[:, 0] - maxx, minx - bounds[:, 2]), 0.0)
        gap_y = np.maximum(np.maximum(bounds[:, 1] - maxy, miny - bounds[:, 3]), 0.0)
        lower = np.hypot(gap_x, gap_y)
        best, distance = None, math.inf
        best_rank = (True, math.inf)
        for index in np.argsort(lower, kind="stable"):
            if best is not None and lower[index] > distance + _TIE_MM:
                break
            candidate = opening.distance(geometries[index])
            if candidate > distance + _TIE_MM:
                continue
            overlap = (
                exposed_only and candidate == 0 and _positive_exposure(opening, geometries[index])
            )
            rank = (not overlap, features[index].index)
            if candidate < distance - _TIE_MM or (
                abs(candidate - distance) <= _TIE_MM and rank < best_rank
            ):
                best, distance, best_rank = index, candidate, rank
        return features[best], geometries[best], distance


# --------------------------------------------------------------------------- minima


def _positive_exposure(opening: Any, geometry: Any) -> bool:
    certain = opening.buffer(-_UNCERTAINTY_MM).intersection(geometry.buffer(-_UNCERTAINTY_MM))
    return certain.area > _AREA_EPSILON_MM2


def _measure(
    opening: Any, face: _Face, net: str, kind: str, origin: list[float]
) -> ClearanceMinimum:
    """One edge-to-edge minimum from ``opening`` to copper of another net."""
    exposed_only = kind == EXPOSED
    minimum = ClearanceMinimum(kind, "complete", members=face.members())
    unknown = face.nearest(opening, None, exposed_only)
    foreign = face.nearest(opening, net, exposed_only)
    if unknown is not None and (foreign is None or unknown[2] <= foreign[2] + _TIE_MM):
        minimum.status, minimum.reason = "incomplete", "unattributed_copper"
        minimum.detail = (
            f"copper plotted without an X2 net attribute is {unknown[2]:.3f} mm away, "
            "no farther than the nearest attributed copper"
        )
        return minimum
    if foreign is None:
        minimum.detail = (
            "no copper of another net is plotted on this face"
            if not exposed_only
            else "no copper of another net is exposed on this face"
        )
        return minimum
    feature, geometry, distance = foreign
    if distance <= _UNCERTAINTY_MM:
        distance = 0.0
    minimum.distance_mm = round(distance, 6)
    minimum.net = feature.net
    if feature.pad and feature.pad[0]:
        minimum.pad = f"{feature.pad[0]}/{feature.pad[1]}"
    overlap = opening.intersection(geometry).area if distance == 0.0 else 0.0
    minimum.overlap_mm2 = round(overlap, 6)
    if exposed_only:
        # Exposure is positive area beyond the construction budget, so edge
        # contact stays a zero gap with nothing exposed.
        minimum.exposed = overlap > _AREA_EPSILON_MM2 and _positive_exposure(opening, geometry)

    point = nearest_points(opening, geometry)[0]
    minimum.location_mm = [round(point.x + origin[0], 6), round(point.y + origin[1], 6)]
    minimum.detail = _conductor(feature)
    return minimum


def _conductor(feature: GerberFeature) -> str:
    if feature.operation == "flash":
        return (
            f"pad {feature.pad[0]}/{feature.pad[1]}" if feature.pad and feature.pad[0] else "flash"
        )
    return "track" if feature.operation == "draw" else "region"


# --------------------------------------------------------------------------- runs


@dataclass
class _AnchorPlan:
    """One recorded anchor resolved against both faces."""

    point: Any
    sheet: list[float]
    confirmed: dict[str, bool]
    openings: dict[str, list[GerberFeature]]
    #: per side, why the anchor's opening is not registered with its copper.
    unregistered: dict[str, str | None]


@dataclass
class _RunPlan:
    run: ReinforceRun
    anchors: list[_AnchorPlan]
    strokes: list[GerberFeature]
    ambiguous_span: bool = False
    ambiguous_anchors: set[tuple[int, str]] = field(default_factory=set)


def _plan_runs(
    runs: list[ReinforceRun], faces: dict[str, _Face], origin: list[float], tolerance: float
) -> list[_RunPlan]:
    """Resolve every run's anchors and strip in the artwork, then arbitrate ownership.

    An anchor is confirmed on a face when the run's own net's plotted copper
    stands on it there, which is what corroborates the sidecar-to-plot origin.
    An anchor's opening is the mask material standing on it, which corroborates
    that the mask is registered with the copper. The strip is the chain of
    plotted strokes whose end points join the run's confirmed anchors (the
    ``mask_copper`` continuity rule), grown only for a run whose two anchors
    both stand on its copper.
    """
    plans: list[_RunPlan] = []
    for run in runs:
        own = faces[run.mask_side[0]]
        anchors = []
        for x, y in run.anchors:
            center = Point(x - origin[0], y - origin[1])
            openings = {side: faces[side].anchor_opening(center, tolerance) for side in SIDES}
            anchors.append(
                _AnchorPlan(
                    center,
                    [x, y],
                    {side: faces[side].on_copper(center, run.net, tolerance) for side in SIDES},
                    openings,
                    {
                        side: faces[side].registered(center, run.net, openings[side], tolerance)
                        for side in SIDES
                    },
                )
            )
        strokes: list[GerberFeature] = []
        if len(anchors) == 2 and all(a.confirmed[own.side] for a in anchors):
            pieces = [
                (
                    feature.index,
                    feature.geometry,
                    MultiPoint([feature.start, feature.end])
                    if feature.start is not None and feature.end is not None
                    else feature.geometry,
                )
                for feature in own.mask.features
                if feature.operation == "draw"
            ]
            admitted = grow_reinforce_chain(pieces, [a.point for a in anchors], tolerance)
            strokes = [own.mask.by_index[key] for key, _material, _probe in admitted]
        plans.append(_RunPlan(run, anchors, strokes))

    # Unique ownership: a strip two records claim is neither run's, and an
    # anchor opening records of different nets claim is not measured for either.
    stroke_claims: dict[tuple[str, int], list[_RunPlan]] = defaultdict(list)
    anchor_claims: dict[tuple[str, int], list[tuple[_RunPlan, int]]] = defaultdict(list)
    for plan in plans:
        for stroke in plan.strokes:
            stroke_claims[(plan.run.mask_side, stroke.index)].append(plan)
        for position, anchor in enumerate(plan.anchors):
            for side, features in anchor.openings.items():
                for feature in features:
                    anchor_claims[(side, feature.index)].append((plan, position))
    for claimants in stroke_claims.values():
        if len(claimants) > 1:
            for plan in claimants:
                plan.ambiguous_span = True
    for (side, _index), rivals in anchor_claims.items():
        if len({plan.run.net for plan, _position in rivals}) > 1:
            for plan, position in rivals:
                plan.ambiguous_anchors.add((position, side))
    return plans


def _where(face: str, opening: str, anchor: int | None) -> str:
    return f"{face} {opening}" + (f" {anchor}" if anchor else "")


def _unmeasured(
    opening: str,
    face: _Face,
    anchor: int | None,
    anchor_mm: list[float] | None,
    reason: str,
    detail: str,
    run: str,
) -> OpeningMeasurement:
    members = face.members()
    where = _where(face.side, opening, anchor)

    def minimum(kind: str) -> ClearanceMinimum:
        return ClearanceMinimum(
            kind,
            "incomplete",
            reason,
            detail,
            members=members,
            run=run,
            face=face.side,
            opening=where,
        )

    return OpeningMeasurement(
        opening,
        face.side,
        anchor,
        anchor_mm,
        "incomplete",
        reason,
        detail,
        minimum(ALL_COPPER),
        minimum(EXPOSED),
    )


def _measured(
    opening: Any,
    kind: str,
    face: _Face,
    net: str,
    anchor: int | None,
    anchor_mm: list[float] | None,
    origin: list[float],
    run: str,
) -> OpeningMeasurement:
    measurement = OpeningMeasurement(
        kind,
        face.side,
        anchor,
        anchor_mm,
        "complete",
        None,
        None,
        _measure(opening, face, net, ALL_COPPER, origin),
        _measure(opening, face, net, EXPOSED, origin),
    )
    for minimum in (measurement.all_copper, measurement.exposed):
        minimum.run, minimum.face = run, face.side
        minimum.opening = _where(face.side, kind, anchor)
    return measurement


def _face_refusal(plan: _RunPlan, face: _Face, anchor: _AnchorPlan) -> tuple[str, str] | None:
    """Why an opening of this run on this face cannot be measured, before its own checks."""
    run = plan.run
    problem = face.problem()
    if problem:
        return "missing_artwork", problem
    if run.net not in face.by_net:
        return "missing_net_attribute", (
            f"no plotted copper on {face.copper.layer} carries X2 net {run.net!r}"
        )
    if not anchor.confirmed[face.side]:
        return "unverified_registration", (
            f"anchor ({anchor.sheet[0]:.3f}, {anchor.sheet[1]:.3f}) mm does not stand on "
            f"{run.net!r} copper plotted on {face.copper.layer}: the plot origin does not "
            "match the sidecar"
        )
    return None


def _span(plan: _RunPlan, face: _Face, origin: list[float], tolerance: float) -> OpeningMeasurement:
    def fail(reason: str, detail: str) -> OpeningMeasurement:
        return _unmeasured("span", face, None, None, reason, detail, plan.run.label)

    for anchor in plan.anchors:
        refusal = _face_refusal(plan, face, anchor)
        if refusal:
            return fail(*refusal)
    for anchor in plan.anchors:
        if anchor.unregistered[face.side]:
            return fail(
                "unverified_registration",
                f"at the anchor ({anchor.sheet[0]:.3f}, {anchor.sheet[1]:.3f}) mm "
                f"{anchor.unregistered[face.side]}: the mask is not registered with the "
                "copper at the run's anchors",
            )
    if plan.ambiguous_span:
        return fail("ambiguous_run_ownership", "another sidecar record claims the plotted strip")
    if not plan.strokes:
        return fail("opening_not_plotted", f"no plotted {face.mask.layer} stroke joins the anchors")
    material = unary_union([stroke.geometry for stroke in plan.strokes])
    joined = [
        part
        for part in shapely.get_parts(material)
        if all(part.distance(anchor.point) <= tolerance for anchor in plan.anchors)
    ]
    if not joined:
        return fail("opening_not_plotted", "no continuous plotted strip reaches both anchors")
    opening = unary_union(joined).difference(
        unary_union([f.geometry for a in plan.anchors for f in a.openings[face.side]])
    )
    if opening.is_empty:
        return fail(
            "opening_not_plotted", "the plotted strip lies wholly within the anchor openings"
        )
    return _measured(opening, "span", face, plan.run.net, None, None, origin, plan.run.label)


def _anchor(
    plan: _RunPlan,
    anchor: _AnchorPlan,
    position: int,
    face: _Face,
    origin: list[float],
    tolerance: float,
) -> OpeningMeasurement:
    def fail(reason: str, detail: str) -> OpeningMeasurement:
        return _unmeasured(
            "anchor", face, position + 1, anchor.sheet, reason, detail, plan.run.label
        )

    refusal = _face_refusal(plan, face, anchor)
    if refusal:
        return fail(*refusal)
    if (position, face.side) in plan.ambiguous_anchors:
        return fail(
            "ambiguous_run_ownership",
            "a sidecar record of another net claims this anchor opening",
        )
    if anchor.unregistered[face.side]:
        return fail(
            "unverified_registration",
            f"{anchor.unregistered[face.side]}: the anchor is not open on this face or the "
            "mask is not registered with the copper",
        )
    opening = unary_union([feature.geometry for feature in anchor.openings[face.side]])
    return _measured(
        opening, "anchor", face, plan.run.net, position + 1, anchor.sheet, origin, plan.run.label
    )


def _run_measurement(
    plan: _RunPlan, faces: dict[str, _Face], origin: list[float], tolerance: float
) -> RunMeasurement:
    own = faces[plan.run.mask_side[0]]
    far = faces["F" if own.side == "B" else "B"]
    openings: list[OpeningMeasurement] = []
    if len(plan.anchors) == 2:
        openings.append(_span(plan, own, origin, tolerance))
    for position, anchor in enumerate(plan.anchors):
        openings.append(_anchor(plan, anchor, position, own, origin, tolerance))
        openings.append(_anchor(plan, anchor, position, far, origin, tolerance))
    return RunMeasurement(
        plan.run.label,
        plan.run.net,
        plan.run.layer,
        [list(anchor.sheet) for anchor in plan.anchors],
        openings,
    )


# --------------------------------------------------------------------------- entry point


def measure_reinforcement_clearance(
    gerber_archive: str | Path | None,
    wires: Any,
    *,
    plot_origin_mm: tuple[float, float] = (0.0, 0.0),
) -> ReinforcementClearanceReport:
    """Measure the shipped copper and mask of every run in ``wires`` (Issue #10422).

    ``gerber_archive`` is the Gerber ZIP the package will ship, or a directory
    holding the same members loose; ``None`` is a package with no Gerbers, and
    every opening is then ``missing_artwork``. ``wires`` are the
    ``<board>.reinforcement.json`` records; with none the report is ``not_run``.
    ``plot_origin_mm`` is where the plot put its origin in the sidecar's
    sheet-absolute frame (kicad-cli's ``--use-drill-file-origin`` shifts the
    artwork by the board's aux origin). It is corroborated, never trusted: an
    opening is measured only where its run's anchors stand on the run's own
    plotted copper and on a plotted mask opening.
    """
    origin: list[float] = []
    for value in plot_origin_mm:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("plot_origin_mm must be finite")
        origin.append(value)
    runs = reinforcement_run_records(wires)
    source = Path(gerber_archive) if gerber_archive is not None else None
    problem = None
    archive_digest = None
    if source is None:
        problem = "the package has no Gerber archive"
    else:
        try:
            if source.is_dir():
                layers = _load_layers(
                    (p.name for p in source.iterdir() if p.is_file()),
                    lambda name: (source / name).read_bytes(),
                )
            else:
                archive_digest = hashlib.sha256(source.read_bytes()).hexdigest()
                with zipfile.ZipFile(source) as archive_file:
                    layers = _load_layers(archive_file.namelist(), archive_file.read)
        except (OSError, zipfile.BadZipFile) as exc:
            problem = f"Gerber archive unreadable: {exc}"
    if problem:
        layers = {layer: _Layer(layer, problem=problem) for layer in LAYERS}
    faces = {side: _Face(side, layers) for side in SIDES}
    tolerance = MASK_TOLERANCE_MM + _UNCERTAINTY_MM
    plans = _plan_runs(runs, faces, origin, tolerance)
    measured = [_run_measurement(plan, faces, origin, tolerance) for plan in plans]
    minima = [m for run in measured for o in run.openings for m in (o.all_copper, o.exposed)]
    if not measured:
        coverage = "not_run"
    elif all(minimum.status == "complete" for minimum in minima):
        coverage = "complete"
    else:
        coverage = "incomplete"
    archive = {"member": source.name if source is not None else None, "sha256": archive_digest}
    return ReinforcementClearanceReport(
        coverage,
        {layer: entry.record() for layer, entry in layers.items()},
        measured,
        archive,
        origin,
    )
