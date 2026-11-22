"""Preserve connected polarity marks and trim free silk ends at a board edge."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from shapely.affinity import translate  # type: ignore[import-untyped]
from shapely.geometry import LineString, MultiLineString  # type: ignore[import-untyped]
from shapely.ops import nearest_points, unary_union  # type: ignore[import-untyped]

from kicad_tools.schema.pcb import PCB, FootprintGraphic
from kicad_tools.sexp.parser import SExp
from kicad_tools.silkscreen._silk_defaults import SILK_EDGE_CLEARANCE_MM, SILK_HOLE_CLEARANCE_MM
from kicad_tools.silkscreen.place_refs import _inverse_fp_transform, collect_apertures
from kicad_tools.validate.rules.factory_clearance import silk_pad_apertures
from kicad_tools.validate.rules.pin1_marker import Pin1MarkerRule
from kicad_tools.validate.rules.silkscreen import (
    _fp_transform,
    _graphic_descriptor,
    _iter_silk_elements,
    _silk_side,
    _stroke_geometry,
    resolve_silk_keepouts,
)

# Keep the original association with the pin. A larger displacement requires
# a footprint designer to decide where the polarity mark belongs.
_MAX_MARKER_SHIFT_MM = 0.2
# Include plot-coordinate and curve-approximation headroom in a repaired gap.
_PRINT_MARGIN_MM = 0.01


@dataclass
class FootprintLineEdit:
    footprint_ref: str
    layer: str
    uuid: str
    action: str
    old_start: tuple[float, float]
    old_end: tuple[float, float]
    new_start: tuple[float, float]
    new_end: tuple[float, float]


def _write_line(ref, node, graphic, start, end, action):
    points = []
    for name, point in (("start", start), ("end", end)):
        child = node.find(name)
        assert child is not None
        x, y = round(point[0], 6), round(point[1], 6)
        child.set_atom(0, x)
        child.set_atom(1, y)
        points.append((x, y))
    return FootprintLineEdit(
        ref, graphic.layer, graphic.uuid, action, graphic.start, graphic.end, *points
    )


def _joined_lines(markers, graphics):
    """Groups connected end to end in the footprint's own coordinate frame."""
    remaining = {i for i in markers if graphics[i].graphic_type == "line"}
    while remaining:
        group = {min(remaining)}
        ends = {graphics[min(group)].start, graphics[min(group)].end}
        while extra := {
            i for i in remaining - group if ends & {graphics[i].start, graphics[i].end}
        }:
            group.update(extra)
            ends.update(p for i in extra for p in (graphics[i].start, graphics[i].end))
        remaining -= group
        if len(group) >= 2:
            yield sorted(group)


def _shift_marker(fp, graphics, group, apertures, obstacles, outline, clearance, rule):
    """Find one rigid translation, or leave the complete mark for manual repair."""
    forward = _fp_transform(fp)
    ink = unary_union([_stroke_geometry(graphics[i], forward) for i in group])
    required = clearance + _PRINT_MARGIN_MM
    nearby = [geom for geom in apertures if ink.distance(geom) <= required + _MAX_MARKER_SHIFT_MM]
    shifts = set()
    for aperture in nearby:
        distance = ink.distance(aperture)
        if 0 < distance < clearance:
            a, b = nearest_points(aperture, ink)
            scale = (required - distance) / distance
            shifts.add(((b.x - a.x) * scale, (b.y - a.y) * scale))

    inverse = _inverse_fp_transform(fp)
    for dx, dy in sorted(shifts, key=lambda d: (math.hypot(*d), d)):
        if math.hypot(dx, dy) > _MAX_MARKER_SHIFT_MM:
            continue
        shifted = translate(ink, dx, dy)
        if any(shifted.distance(g) < clearance + _PRINT_MARGIN_MM / 2 for g in nearby):
            continue
        if outline is not None and shifted.distance(outline) < SILK_EDGE_CLEARANCE_MM:
            continue
        # A mark joined to other silk, or one that would hit it, stays put.
        if any(ink.intersects(g) or shifted.intersects(g) for g in obstacles):
            continue
        lx, ly = inverse((fp.position[0] + dx, fp.position[1] + dy))
        trial = list(graphics)
        for i in group:
            g = graphics[i]
            trial[i] = replace(
                g, start=(g.start[0] + lx, g.start[1] + ly), end=(g.end[0] + lx, g.end[1] + ly)
            )
        if set(group) <= rule.marker_graphic_indices(fp, trial):
            return trial
    return None


def _item_key(ref, graphic):
    return (
        _silk_side(graphic.layer),
        f"{ref} ({_graphic_descriptor('fp', graphic)})",
        graphic.uuid,
    )


def _move_joined_markers(
    fp,
    nodes,
    graphics,
    markers,
    skipped_markers,
    obstacles_by_side,
    ink,
    areas,
    outline,
    pad_clearance,
    rule,
):
    edits = []
    forward = _fp_transform(fp)
    for group in _joined_lines(markers, graphics):
        side = _silk_side(graphics[group[0]].layer)
        if side is None or not any(
            (fp.reference, _graphic_descriptor("fp", graphics[i])) in skipped_markers for i in group
        ):
            continue
        own = {_item_key(fp.reference, graphics[i]) for i in group}
        if len(own) != len(group) or any(len(ink[key]) != 1 for key in own):
            continue
        obstacles = [
            geom
            for key, shapes in ink.items()
            if key[0] == side and key not in own
            for geom in shapes
        ]
        obstacles.extend(a.polygon for a in areas if (side, "graphic") in a.forbidden)
        shifted = _shift_marker(
            fp,
            graphics,
            group,
            obstacles_by_side[side],
            obstacles,
            outline,
            pad_clearance,
            rule,
        )
        if shifted is None:
            continue
        for i in group:
            g = shifted[i]
            edits.append(
                _write_line(fp.reference, nodes[i], graphics[i], g.start, g.end, "moved_marker")
            )
        # Later groups see exactly the artwork retained by this repair.
        for key in own:
            del ink[key]
        for i in group:
            ink[_item_key(fp.reference, shifted[i])].append(_stroke_geometry(shifted[i], forward))
        graphics[:] = shifted

    return edits


def _trim_free_ends(fp, nodes, graphics, outline, printed_silk, markers):
    edits = []
    forward, inverse = _fp_transform(fp), _inverse_fp_transform(fp)
    for i, (node, g) in enumerate(zip(nodes, graphics, strict=True)):
        if g.graphic_type != "line" or _silk_side(g.layer) is None or g.start == g.end:
            continue
        start, end = forward(g.start), forward(g.end)
        line = LineString([start, end])
        floor = SILK_EDGE_CLEARANCE_MM + g.stroke_width / 2
        if line.distance(outline) >= floor - 1e-6:
            continue
        if markers is None:
            markers = Pin1MarkerRule().marker_graphic_indices(fp, graphics)
        if i in markers:
            continue
        kept = line.difference(outline.buffer(floor + _PRINT_MARGIN_MM))
        if kept.geom_type != "LineString" or kept.is_empty or kept.length < g.stroke_width:
            continue
        # Trim a free end only. Crossing strokes, complete marks, and
        # joints need a choice about the intended artwork.
        new_start, new_end = kept.coords[0], kept.coords[-1]
        if (new_start == start) == (new_end == end):
            continue  # Exactly one of the original endpoints must survive.
        side = _silk_side(g.layer)
        ink = printed_silk()
        key = _item_key(fp.reference, g)
        if len(ink[key]) != 1:
            continue
        if new_start == start:
            trimmed = replace(g, end=inverse(new_end))
            cut = LineString([new_end, end])
        else:
            trimmed = replace(g, start=inverse(new_start))
            cut = LineString([start, new_start])
        # Limit the subtraction to the cut end. Polygon roundoff along
        # a retained line must not make the opposite joint look severed.
        cut_region = cut.buffer(g.stroke_width / 2 + 1e-6)
        removed_ink = (
            ink[key][0].intersection(cut_region).difference(_stroke_geometry(trimmed, forward))
        )
        if any(
            removed_ink.intersects(shape)
            for other, shapes in ink.items()
            if other[0] == side and other != key
            for shape in shapes
        ):
            continue
        edit = _write_line(fp.reference, node, g, trimmed.start, trimmed.end, "trimmed_edge")
        edits.append(edit)
        trimmed = replace(g, start=edit.new_start, end=edit.new_end)
        del ink[key]
        ink[_item_key(fp.reference, trimmed)].append(_stroke_geometry(trimmed, forward))
    return edits


def repair_footprint_lines(
    doc: SExp,
    *,
    skipped_markers: set[tuple[str, str]],
    pad_clearance: float | None,
    mask_clearance: float,
    hole_clearance: float | None,
    dru_path: Path,
) -> list[FootprintLineEdit]:
    pcb = PCB(doc)
    segments = pcb.get_board_outline_segments()
    outline = MultiLineString(segments) if segments else None

    @cache
    def printed_silk():
        # Include the checker's geometry descriptor: legacy lines may have no UUID.
        by_item = defaultdict(list)
        for side, geom, _, _, _, uid, item in _iter_silk_elements(pcb):
            by_item[(side, item, uid)].append(geom)
        return by_item

    skipped_pad_refs = {ref for ref, _ in skipped_markers}
    obstacles_by_side: dict[str, list] = {"F": [], "B": []}
    if skipped_markers:
        margin = pcb.setup.pad_to_mask_clearance if pcb.setup is not None else 0.0
        exact_pads = silk_pad_apertures(pcb.footprints, margin)
        openings = collect_apertures(pcb, mask_clearance, include_via_obstacles=False)
        drill_floor = SILK_HOLE_CLEARANCE_MM if hole_clearance is None else hole_clearance
        for face in ("F", "B"):
            obstacles_by_side[face].extend(geom for geom, _ in exact_pads[face])
            # Preserve the drill floor when a profile needs more room around holes than pads.
            for geom, label in openings[face]:
                extra = (
                    max(0.0, drill_floor - (pad_clearance or 0.0))
                    if label.startswith("drill ")
                    else 0.0
                )
                obstacles_by_side[face].append(geom.buffer(extra) if extra else geom)
    unsupported: list[tuple[str, str]] = []
    areas = (
        resolve_silk_keepouts(pcb, dru_path=dru_path, unsupported_details=unsupported)
        if skipped_pad_refs and dru_path.exists()
        else []
    )
    edits = []
    rule = Pin1MarkerRule()
    for fp in pcb.footprints:
        if fp._sexp_node is None:
            continue
        nodes = [
            n
            for n in fp._sexp_node.children
            if n.name in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_curve")
        ]
        graphics = [FootprintGraphic.from_sexp(n, str(n.name)[3:]) for n in nodes]
        markers = None
        if fp.reference in skipped_pad_refs and pad_clearance is not None and not unsupported:
            markers = rule.marker_graphic_indices(fp, graphics)
            edits.extend(
                _move_joined_markers(
                    fp,
                    nodes,
                    graphics,
                    markers,
                    skipped_markers,
                    obstacles_by_side,
                    printed_silk(),
                    areas,
                    outline,
                    pad_clearance,
                    rule,
                )
            )
        if outline is not None:
            edits.extend(_trim_free_ends(fp, nodes, graphics, outline, printed_silk, markers))
    return edits
