"""Width-only edits of authored copper arcs during trace optimization."""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.schema.pcb import Arc
from kicad_tools.sexp import parse_string

from .collision import CollisionChecker

# Sweeping a chord with this additional radius encloses the true circular copper.
_CHORD_ERROR_MM = 0.001


@dataclass(frozen=True)
class ArcWidthEdit:
    start: int
    end: int
    width: float


def apply_arc_width_edits(text: str, edits: Iterable[ArcWidthEdit]) -> str:
    """Replace just the width atoms in the original board's text."""
    parts: list[str] = []
    cursor = 0
    for edit in sorted(edits, key=lambda e: e.start):
        parts.extend((text[cursor : edit.start], str(edit.width)))
        cursor = edit.end
    parts.append(text[cursor:])
    return "".join(parts)


def project_arc_width_edits(
    board: str,
    router,
    checker: CollisionChecker,
    widths: dict[str, float],
    *,
    net_filter: str | None,
    net_pattern: str | None,
    protected_nets: set[str],
) -> dict[str, list[ArcWidthEdit]]:
    """Reserve the curved copper in the collision grid and propose safe width edits."""
    from .pcb import _RE_WIDTH, _extract_balanced_blocks, parse_net_names, resolve_block_net

    blocks = _extract_balanced_blocks(board, "arc")
    if not blocks:
        return {}
    names = parse_net_names(board)
    ids = {name: number for number, name in router.net_names.items()}
    candidates = []
    for start, _, block in blocks:
        width_field = _RE_WIDTH.search(block)
        identity = resolve_block_net(block, names, ids)
        if width_field is None or identity is None:
            continue
        arc = Arc.from_sexp(parse_string(block))
        _, name = identity
        owner = ids.get(name, 0)
        layer = Layer.from_kicad_name(arc.layer)
        points = arc.centerline_points(max_error_mm=_CHORD_ERROR_MM)

        def chords(width: float) -> list[Segment]:
            return [
                Segment(*first, *last, width + 2 * _CHORD_ERROR_MM, layer, owner, name)
                for first, last in pairwise(points)
            ]

        # The grid's segment index cannot store curves. The chords are only
        # collision geometry and are never serialized to the board.
        router.grid.mark_route(Route(owner, name, chords(arc.width)))
        target = widths.get(name)
        if (
            target is None
            or owner <= 0
            or name in protected_nets
            or (net_filter and name != net_filter)
            or (net_pattern and not fnmatch.fnmatchcase(name, net_pattern))
            or abs(target - arc.width) <= 1e-6
        ):
            continue
        edit = ArcWidthEdit(start + width_field.start(1), start + width_field.end(1), target)
        candidates.append((name, owner, target > arc.width, chords(target), edit))

    edits = defaultdict(list)
    for name, owner, widens, segments, edit in candidates:
        if widens:
            if not all(
                checker.path_is_clear(s.x1, s.y1, s.x2, s.y2, s.layer, s.width, owner)
                for s in segments
            ):
                continue
            # Subsequent arcs and straight-track passes see this accepted width.
            router.grid.mark_route(Route(owner, name, segments))
        edits[name].append(edit)
    return dict(edits)
