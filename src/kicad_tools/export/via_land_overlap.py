"""Plated via drills against the same-face mask opening, stencil opening and SMD land.

Issue #10357. ``kct export --via-land-overlap`` measures the PCBA archive it has
just written -- after assembly generation, before the report and the manifest --
and appends one :class:`~kicad_tools.export.preflight.PreflightResult` named
``via_land_overlap``. A standard export does not run it.

What qualifies
    Every drill hit :func:`~kicad_tools.export.drill_via_match.match_drill_vias`
    verified as a routed board via is one physical hole. On each outer face the
    via reaches, the exported drill disk, the plotted solder-mask opening, the
    plotted stencil opening and one SMD land of the board must share area.
    Contacts at different points of the disk, or split across the two faces, do
    not qualify. A drill whose center lies outside the opening still qualifies
    when its edge reaches it, and is reported with ``edge_overlap`` true.

Where the geometry comes from
    Drill centers and diameters are the shipped Excellon values. Mask and paste
    openings are reconstructed from the plotted members with
    :func:`kicad_tools.validate.gerber_geometry.parse_gerber_geometry`, so a
    windowed stencil (clear flashes cut out of an aperture) is honored. A layer
    is identified by its X2 ``TF.FileFunction`` and falls back to KiCad's
    ``<board>-<Layer>.<ext>`` member name. ``TF.FilePolarity`` decides whether a
    plotted flash is an opening: KiCad plots mask openings dark on a negative
    layer and stencil openings dark on a positive one, and those conventions
    apply when the attribute is absent.

Registration
    Plots and drill files share the origin ``kicad-cli`` was given, so the only
    transform is that origin minus the board's own outline origin. The drill
    file is trusted to the matcher, which pairs its hits with board vias; the
    plots are checked against the board, since a plot made for another origin
    would silently move every opening: at least half the copper of the lands
    that ask for a mask opening has to lie in one, or the face is reported
    ``<Layer>:unregistered`` and left unmeasured. A drill center is quantized
    to half a coordinate step on each axis; the disk is shrunk by the
    worst-case error, so an overlap shallower than the drill file's resolution
    reads as the tangency it may well be.

Declared process disposition
    An intersection is never dropped. Its ``process_disposition`` names the
    declaration that applies to it:

    ``filled_capped``
        The bundle declares a via-in-pad
        :class:`~kicad_tools.manufacturers.fabrication_process.FabricationProcess`
        that requires filled and capped vias (``ManufacturingResult.fabrication_process``,
        resolved for this board's layer configuration and bound into the same
        manifest), and the exported drill lies inside that process's published
        drill window. A manufacturer's bare ``via_in_pad_supported`` capability
        is not consulted.
    ``exposed_thermal``
        The land carries KiCad's ``pad_prop_heatsink`` pad property. A pad
        number or name such as ``EP`` is not a declaration.
    ``undeclared``
        Neither applies.

Coverage
    ``coverage.status`` is ``complete`` only when every routed via matched a
    drill hit, every reached face has a readable, registered mask plot and a
    readable paste plot, and no opening belonged to two lands at once.
    Otherwise it is ``partial`` and ``coverage.reasons`` names
    ``<Layer>:missing``, ``<Layer>:unsupported``, ``<Layer>:duplicate`` or
    ``<Layer>:unregistered`` (that face is left unmeasured), the matcher's own
    reasons (``registration_unknown``, ``unmatched_via``, ...), or
    ``ambiguous_land`` for an opening that lies on two different lands at
    once, of which only the shared part is withheld. Findings on measured
    faces are kept either way, and a zero is only a measured zero under
    complete coverage. The result is ``OK`` when coverage is complete and
    every intersection is declared, ``WARN`` otherwise, and never ``FAIL``.

Record
    ``PreflightResult.data`` is the versioned dictionary below, serialized as-is
    by ``kct export --format json`` and by ``manifest.json``::

        {
          "schema_version": 1,
          "archive": "gerbers/gerbers.zip",
          "archive_sha256": "...",
          "frame": {"name": "excellon", "units": "mm", "origin": "aux_axis_origin"},
          "expected_vias": 499, "matched_vias": 499, "distinct_holes": 22,
          "observations": [
            {"face": "F", "reference": "U1", "pad": "3", "drill_file": "board-PTH.drl",
             "drill_center_mm": [12.7, -3.4], "drill_diameter_mm": 0.3,
             "overlap_area_mm2": 0.0075, "edge_overlap": true,
             "process_disposition": "undeclared", "declarations": []}
          ],
          "dispositions": {"undeclared": 22, "filled_capped": 0, "exposed_thermal": 0},
          "faces": {"F": {"measured": true, "distinct_holes": 10,
                          "observations": 10, "reasons": []}},
          "coverage": {"status": "complete", "reasons": []}
        }

    ``drill_center_mm`` is the coordinate as printed in ``drill_file``. A hole is
    counted once in ``distinct_holes`` however many faces or lands observe it.
    An archive that could not be measured reports ``null`` counts.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, Any, Literal
from zipfile import BadZipFile, ZipFile

from .drill_via_match import ViaMatch, match_drill_vias
from .preflight import PreflightResult

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

CHECK_NAME = "via_land_overlap"
SCHEMA_VERSION = 1

UNDECLARED = "undeclared"
FILLED_CAPPED = "filled_capped"
EXPOSED_THERMAL = "exposed_thermal"
DISPOSITIONS = (UNDECLARED, FILLED_CAPPED, EXPOSED_THERMAL)
HEATSINK_PAD_PROPERTY = "pad_prop_heatsink"
AMBIGUOUS_LAND = "ambiguous_land"

FACE_LAYERS: dict[str, tuple[str, str, str]] = {
    "F": ("F.Cu", "F.Mask", "F.Paste"),
    "B": ("B.Cu", "B.Mask", "B.Paste"),
}
_OPENING_POLARITY = {
    "F.Mask": "negative",
    "B.Mask": "negative",
    "F.Paste": "positive",
    "B.Paste": "positive",
}
_DRILL_SUFFIXES = (".drl", "-drl.gbr")
_PLOT_SUFFIXES = (*_DRILL_SUFFIXES, ".gtl", ".gbl", ".gts", ".gbs", ".gtp", ".gbp", ".gbr")
_FILE_FUNCTION = re.compile(r"%TF\.FileFunction,([^*%]+)\*%")
_FILE_POLARITY = re.compile(r"%TF\.FilePolarity,(Positive|Negative)\*%", re.IGNORECASE)
_MEMBER_LAYER = re.compile(r"-(F_Cu|B_Cu|F_Mask|B_Mask|F_Paste|B_Paste)\.[A-Za-z0-9]+$")
_DIGITS = re.compile(r"(\d+)")
_DETAIL_ROWS = 20
_REGISTRATION_SAMPLE = 128
_UNREADABLE = "\x00"


@dataclass(frozen=True)
class _Opening:
    """One plotted mask or paste layer, as the drill meets it."""

    geometry: Any
    plotted_is_opening: bool


@dataclass(frozen=True)
class _Land:
    """One SMD land: every pad of one reference and pad number on one face.

    ``masked`` tells that one of those pads asks for a solder-mask opening.
    """

    reference: str
    pad: str
    geometry: Any
    heatsink: bool
    masked: bool


def _natural(text: str) -> tuple[Any, ...]:
    """Sort key that orders ``1 < 2 < 10`` inside otherwise lexical text."""
    return tuple(int(part) if part.isdigit() else part for part in _DIGITS.split(text))


def _origin_name(use_aux_origin: bool | None) -> str:
    """The drill-frame origin an archive was plotted with, as the record names it."""
    if use_aux_origin is None:
        return "unknown"
    return "aux_axis_origin" if use_aux_origin else "absolute"


def _record(
    *,
    archive: str | None,
    archive_sha256: str | None,
    use_aux_origin: bool | None,
    expected: int | None,
    matched: int | None,
) -> dict[str, Any]:
    """The empty record every result is filled in from."""
    return {
        "schema_version": SCHEMA_VERSION,
        "archive": archive,
        "archive_sha256": archive_sha256,
        "frame": {"name": "excellon", "units": "mm", "origin": _origin_name(use_aux_origin)},
        "expected_vias": expected,
        "matched_vias": matched,
        "distinct_holes": None,
        "observations": [],
        "dispositions": dict.fromkeys(DISPOSITIONS, 0),
        "faces": {},
        "coverage": {"status": "partial", "reasons": []},
    }


def _details(data: dict[str, Any]) -> str:
    """The single-line listing ``kct export`` prints for a non-OK result."""
    parts: list[str] = []
    if data["distinct_holes"] is not None:
        for face, info in data["faces"].items():
            state = "measured" if info["measured"] else "not measured"
            suffix = f" ({', '.join(info['reasons'])})" if info["reasons"] else ""
            parts.append(
                f"{face} {state}: {info['distinct_holes']} distinct hole(s), "
                f"{info['observations']} observation(s){suffix}"
            )
        rows: list[dict[str, Any]] = data["observations"]
        for row in rows[:_DETAIL_ROWS]:
            x, y = row["drill_center_mm"]
            edge = ", edge overlap" if row["edge_overlap"] else ""
            parts.append(
                f"{row['face']} {row['reference']} pad {row['pad']}: drill "
                f"{row['drill_diameter_mm']:.3f} mm at ({x:g}, {y:g}) mm in "
                f"{row['drill_file']}{edge}, {row['overlap_area_mm2']:.4g} mm2, "
                f"{row['process_disposition']}"
            )
        if len(rows) > _DETAIL_ROWS:
            parts.append(f"... and {len(rows) - _DETAIL_ROWS} more in data.observations")
    reasons: list[str] = data["coverage"]["reasons"]
    if reasons:
        parts.append(f"coverage partial: {', '.join(reasons)}")
    return "; ".join(parts)


def _finish(data: dict[str, Any]) -> PreflightResult:
    """Turn a measured or unmeasured record into the appended preflight result."""
    reasons: list[str] = data["coverage"]["reasons"]
    dispositions: dict[str, int] = data["dispositions"]
    distinct = data["distinct_holes"]
    partial = bool(reasons)
    if distinct is None:
        message = "via land overlap not measured"
    else:
        expected, matched = data["expected_vias"], data["matched_vias"]
        message = ""
        if expected != matched:
            message = f"{expected} expected, {matched} matched plated drill hits for routed vias; "
        noun = "plated via drill" + ("" if distinct == 1 else "s")
        verb = "overlaps" if distinct == 1 else "overlap"
        message += f"{distinct} {noun} {verb} the solder-mask opening, stencil opening "
        message += "and an SMD land on the same face"
        declared = [f"{dispositions[name]} {name}" for name in DISPOSITIONS if dispositions[name]]
        if declared:
            message += ": " + ", ".join(declared)
    message += f" (coverage partial: {', '.join(reasons)})" if partial else " (coverage complete)"
    status: Literal["OK", "WARN"] = "WARN" if partial or dispositions[UNDECLARED] else "OK"
    return PreflightResult(CHECK_NAME, status, message, _details(data), data)


def unmeasured_via_land_overlap(
    reasons: Iterable[str],
    *,
    archive: str | None = None,
    archive_sha256: str | None = None,
    use_aux_origin: bool | None = None,
) -> PreflightResult:
    """The result for an export whose archive could not be measured at all.

    Args:
        reasons: Why nothing was measured (``archive_missing``, ...).
        archive: Bundle-relative archive name, when it is known.
        archive_sha256: SHA-256 of the archive, when it was hashed.
        use_aux_origin: The ``GerberConfig.use_aux_origin`` the export plotted with.

    Returns:
        A ``WARN`` result whose counts are ``None`` and whose coverage is partial.
    """
    data = _record(
        archive=archive,
        archive_sha256=archive_sha256,
        use_aux_origin=use_aux_origin,
        expected=None,
        matched=None,
    )
    data["coverage"] = {"status": "partial", "reasons": sorted(set(reasons))}
    return _finish(data)


def _member_name(name: str) -> str:
    """The file name inside a member path, with any directory removed."""
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _member_texts(archive: Mapping[str, str] | ZipFile) -> dict[str, str]:
    """Text of every drill and Gerber member, keyed by member name."""
    if isinstance(archive, Mapping):
        return dict(archive)
    texts: dict[str, str] = {}
    for member in archive.infolist():
        if member.is_dir() or not _member_name(member.filename).lower().endswith(_PLOT_SUFFIXES):
            continue
        try:
            texts[member.filename] = archive.read(member).decode("utf-8", errors="replace")
        except (BadZipFile, OSError, RuntimeError, UnicodeDecodeError):
            texts[member.filename] = _UNREADABLE
    return texts


def _drill_members(texts: Mapping[str, str]) -> dict[str, str]:
    """The drill members of an archive mapping, which is all the matcher reads."""
    return {
        name: text
        for name, text in texts.items()
        if _member_name(name).lower().endswith(_DRILL_SUFFIXES)
    }


def _function_layer(function: str) -> str | None:
    """Canonical mask or paste layer named by an X2 ``TF.FileFunction``."""
    parts = [part.strip().lower() for part in function.split(",")]
    face = {"top": "F", "bottom": "B"}.get(parts[1] if len(parts) >= 2 else "")
    if face is None:
        return None
    if parts[0] == "soldermask":
        return f"{face}.Mask"
    if parts[0] == "paste":
        return f"{face}.Paste"
    return None


def _identify_plots(texts: Mapping[str, str]) -> dict[str, list[tuple[str, str | None]]]:
    """Map each mask and paste layer to the ``(member, polarity)`` claims on it."""
    claims: dict[str, list[tuple[str, str | None]]] = {}
    for name in sorted(texts):
        text = texts[name]
        layer = None
        polarity = None
        if function := _FILE_FUNCTION.search(text):
            layer = _function_layer(function[1])
        if declared := _FILE_POLARITY.search(text):
            polarity = declared[1].lower()
        if layer is None:
            if match := _MEMBER_LAYER.search(_member_name(name)):
                layer = match[1].replace("_", ".", 1)
        if layer is not None:
            claims.setdefault(layer, []).append((name, polarity))
    return claims


def _face_openings(
    face: str,
    texts: Mapping[str, str],
    claims: Mapping[str, list[tuple[str, str | None]]],
    shift: tuple[float, float],
) -> tuple[list[_Opening], list[str]]:
    """The face's mask and paste openings in the board frame.

    Returns the openings and one reason per layer that could not be read.
    """
    from shapely.affinity import translate  # type: ignore[import-untyped]

    from ..validate.gerber_geometry import parse_gerber_geometry

    openings: list[_Opening] = []
    problems: list[str] = []
    for layer in FACE_LAYERS[face][1:]:
        found = claims.get(layer, [])
        if not found:
            problems.append(f"{layer}:missing")
            continue
        if len(found) != 1:
            problems.append(f"{layer}:duplicate")
            continue
        member, polarity = found[0]
        try:
            geometry = parse_gerber_geometry(texts[member]).geometry
        except ValueError:
            problems.append(f"{layer}:unsupported")
            continue
        conventional = _OPENING_POLARITY[layer]
        openings.append(
            _Opening(
                translate(geometry, shift[0], shift[1]),
                (polarity or conventional) == conventional,
            )
        )
    return openings, problems


def _reached_faces(matches: Iterable[ViaMatch]) -> dict[str, list[int]]:
    """Map each outer face to the indexes of the matched holes that reach it."""
    reached: dict[str, list[int]] = {}
    for index, match in enumerate(matches):
        for face, (copper, _mask, _paste) in FACE_LAYERS.items():
            if copper in match.faces:
                reached.setdefault(face, []).append(index)
    return reached


def _pad_has_property(pad: Any, name: str) -> bool:
    """Whether the pad's source node carries ``(property name)``."""
    node = pad.__dict__.get("_sexp_node")
    if node is None:
        return False
    return any(prop.get_first_atom() == name for prop in node.find_all("property"))


def _lands(board: PCB, face: str) -> list[_Land]:
    """SMD lands with copper on the face, one per (reference, pad number)."""
    from kicad_tools.geometry.copper import pad_copper_outline

    copper, mask, _paste = FACE_LAYERS[face]
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for footprint in board.footprints:
        for pad in footprint.pads:
            layers = set(pad.layers)
            if pad.type != "smd" or not {"*.Cu", copper} & layers:
                continue
            geometry = pad_copper_outline(pad, footprint.position, footprint.rotation)
            if geometry is None or geometry.is_empty:
                continue
            group = grouped.setdefault(
                (footprint.reference, pad.number),
                {"copper": [], "heatsink": False, "masked": False},
            )
            group["copper"].append(geometry)
            group["heatsink"] |= _pad_has_property(pad, HEATSINK_PAD_PROPERTY)
            group["masked"] |= bool({"*.Mask", mask} & layers)
    ordered = sorted(grouped.items(), key=lambda item: (_natural(item[0][0]), _natural(item[0][1])))
    return [
        _Land(
            reference,
            number,
            reduce(or_, group["copper"]),
            group["heatsink"],
            group["masked"],
        )
        for (reference, number), group in ordered
    ]


def _registered(lands: list[_Land], mask: _Opening) -> bool | None:
    """Whether the plotted mask lines up with the board's lands.

    A plot made for the board leaves most of the copper of the lands that ask
    for an opening open; one written for another origin leaves them covered.
    Up to :data:`_REGISTRATION_SAMPLE` of those lands are measured by area, so
    a center dam or an odd outline on a few lands cannot fail the check.
    ``None`` when the face has no such land to check against.
    """
    masked = [land.geometry for land in lands if land.masked]
    if not masked:
        return None
    step = max(1, len(masked) // _REGISTRATION_SAMPLE)
    open_area = total = 0.0
    for geometry in masked[::step]:
        inside = geometry.intersection(mask.geometry).area
        open_area += inside if mask.plotted_is_opening else geometry.area - inside
        total += geometry.area
    return 2 * open_area >= total


def _certain_disk(match: ViaMatch, shift: tuple[float, float]) -> tuple[Any, Any] | None:
    """The drill disk in the board frame, shrunk by the worst-case center error.

    Returns ``(disk, center_point)``, or ``None`` when no part of the exported
    disk is certainly where the drill is.
    """
    from shapely.geometry import Point  # type: ignore[import-untyped]

    radius = match.diameter_mm / 2.0 - math.hypot(*match.tolerance_mm)
    if radius <= 0.0:
        return None
    center = Point(match.center_mm[0] + shift[0], -match.center_mm[1] + shift[1])
    return center.buffer(radius, quad_segs=64), center


def _plot_shift(board: PCB, use_aux_origin: bool | None) -> tuple[float, float]:
    """Translation from the plot frame to the board-relative frame of ``board``.

    Both frames are y down. The plot frame is the sheet minus the origin the
    drill files were written for; ``PCB.load`` made every board position
    relative to the board's outline origin instead.
    """
    if use_aux_origin and board.setup is not None:
        ox, oy = board.setup.aux_axis_origin
    else:
        ox = oy = 0.0
    bx, by = board.board_origin
    return (ox - bx, oy - by)


def _filled_capped_process(fabrication_process: Mapping[str, Any] | None) -> Any | None:
    """The bundle's declared process, when it requires filled and capped vias."""
    from ..manufacturers.fabrication_process import get_fabrication_process

    process = get_fabrication_process((fabrication_process or {}).get("process_id"))
    if process is None or not process.requires_filled_and_capped:
        return None
    return process


def _disposition(drill_mm: float, land: _Land, process: Any | None) -> list[dict[str, str]]:
    """The declarations that apply to one intersection, strongest first.

    The declared via process only reaches a drill inside its published drill
    window; everything else is left to the land's own property, or undeclared.
    """
    declarations: list[dict[str, str]] = []
    if process is not None and not process.eligibility_reasons(layer_count=None, drill_mm=drill_mm):
        declarations.append({"source": "fabrication_process", "process_id": process.process_id})
    if land.heatsink:
        declarations.append({"source": "pad_property", "name": HEATSINK_PAD_PROPERTY})
    return declarations


def _disposition_name(declarations: list[dict[str, str]]) -> str:
    """The one disposition a row reports for the declarations that apply to it."""
    if any(declaration["source"] == "fabrication_process" for declaration in declarations):
        return FILLED_CAPPED
    return EXPOSED_THERMAL if declarations else UNDECLARED


def _observe_hole(
    match: ViaMatch,
    face: str,
    *,
    openings: list[_Opening],
    lands: list[_Land],
    tree: Any,
    shift: tuple[float, float],
    process: Any | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Attribute one drill hole's opening on ``face`` to the SMD lands it meets.

    Returns the rows and whether any part of the opening belonged to two
    different lands at once.
    """
    located = _certain_disk(match, shift)
    if located is None:
        return [], False
    region, center = located
    for opening in openings:
        if opening.plotted_is_opening:
            region = region.intersection(opening.geometry)
        else:
            region = region.difference(opening.geometry)
    if region.area <= 0.0:
        return [], False
    candidates = [int(i) for i in tree.query(region, predicate="intersects")]
    ambiguous = False
    rows: list[dict[str, Any]] = []
    for i in candidates:
        land = lands[i]
        window = region.intersection(land.geometry)
        if window.area <= 0.0:
            continue
        edge_overlap = not window.contains(center)
        if len(candidates) != 1:
            other = reduce(or_, (lands[j].geometry for j in candidates if j != i))
            ambiguous |= window.intersection(other).area > 0.0
            window = window.difference(other)
        if window.area <= 0.0:
            continue
        declarations = _disposition(match.diameter_mm, land, process)
        rows.append(
            {
                "face": face,
                "reference": land.reference,
                "pad": land.pad,
                "drill_file": match.filename,
                "drill_center_mm": list(match.center_mm),
                "drill_diameter_mm": match.diameter_mm,
                "overlap_area_mm2": round(window.area, 9),
                "edge_overlap": edge_overlap,
                "process_disposition": _disposition_name(declarations),
                "declarations": declarations,
            }
        )
    return rows, ambiguous


def measure_via_land_overlap(
    board: PCB,
    archive: Mapping[str, str] | ZipFile,
    *,
    use_aux_origin: bool | None,
    fabrication_process: Mapping[str, Any] | None = None,
    archive_name: str | None = None,
    archive_sha256: str | None = None,
) -> PreflightResult:
    """Measure the shipped archive's plated via drills against same-face SMD lands.

    Args:
        board: The PCB the archive was plotted from, as ``PCB.load`` returns it.
        archive: The shipped ``gerbers.zip`` (loose plot files are not read), or
            a mapping of archive member names to their text.
        use_aux_origin: The ``GerberConfig.use_aux_origin`` the archive was
            plotted with. ``None`` leaves the drill frame unregistered.
        fabrication_process: ``ManufacturingResult.fabrication_process`` of the
            same export, the bundle's declared via-in-pad process.
        archive_name: Bundle-relative archive name for the record.
        archive_sha256: SHA-256 of the archive for the record.

    Returns:
        The ``via_land_overlap`` result to append to ``preflight_results``.
    """
    from shapely import STRtree  # type: ignore[import-untyped]

    from kicad_tools._shapely import require_shapely

    require_shapely("via land overlap measurement")
    try:
        texts = _member_texts(archive)
    except (BadZipFile, OSError):
        return unmeasured_via_land_overlap(
            ["archive_unreadable"],
            archive=archive_name,
            archive_sha256=archive_sha256,
            use_aux_origin=use_aux_origin,
        )
    drills = match_drill_vias(
        board,
        archive if isinstance(archive, ZipFile) else _drill_members(texts),
        use_aux_origin=use_aux_origin,
    )
    data = _record(
        archive=archive_name,
        archive_sha256=archive_sha256,
        use_aux_origin=use_aux_origin,
        expected=drills.expected,
        matched=drills.matched,
    )
    data["distinct_holes"] = 0
    reasons: set[str] = set(drills.reasons)
    reached = _reached_faces(drills.matches)
    rows: list[dict[str, Any]] = []
    observed: dict[str, set[int]] = {}
    if reached:
        claims = _identify_plots(texts)
        process = _filled_capped_process(fabrication_process)
        shift = _plot_shift(board, use_aux_origin)
        for face in (name for name in FACE_LAYERS if name in reached):
            openings, unreadable = _face_openings(face, texts, claims, shift)
            face_rows: list[dict[str, Any]] = []
            seen: set[int] = set()
            ambiguous = False
            lands = [] if unreadable else _lands(board, face)
            if lands and _registered(lands, openings[0]) is False:
                unreadable = [f"{layer}:unregistered" for layer in FACE_LAYERS[face][1:]]
                lands = []
            if lands:
                tree = STRtree([land.geometry for land in lands])
                for index in reached[face]:
                    found, unsure = _observe_hole(
                        drills.matches[index],
                        face,
                        openings=openings,
                        lands=lands,
                        tree=tree,
                        shift=shift,
                        process=process,
                    )
                    ambiguous |= unsure
                    if found:
                        face_rows.extend(found)
                        seen.add(index)
            reasons.update(unreadable)
            if ambiguous:
                reasons.add(AMBIGUOUS_LAND)
            rows.extend(face_rows)
            observed[face] = seen
            data["faces"][face] = {
                "measured": not unreadable,
                "distinct_holes": len(seen),
                "observations": len(face_rows),
                "reasons": [*unreadable, *([AMBIGUOUS_LAND] if ambiguous else [])],
            }
    rows.sort(
        key=lambda row: (
            _natural(row["reference"]),
            _natural(row["pad"]),
            row["drill_center_mm"],
        )
    )
    data["observations"] = rows
    data["distinct_holes"] = len(set().union(*observed.values()))
    for row in rows:
        data["dispositions"][row["process_disposition"]] += 1
    data["coverage"] = {"status": "partial" if reasons else "complete", "reasons": sorted(reasons)}
    return _finish(data)
