"""Pad geometry used for routing.

Router Pad retains shape but not corner radius. These conservative predicates
describe its rectangle, not exact circle/roundrect/oval copper. The grid acceptance
paths separately distinguish circles. Bounds alone are for broad phase.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .geometry import segment_to_segment_distance
from .primitives import Pad

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Pad as BoardPad


_CHAMFER_CORNERS = frozenset({"top_left", "top_right", "bottom_left", "bottom_right"})


def routing_pad_chamfer(pad: BoardPad) -> tuple[frozenset[str], float] | None:
    """Read the selected corners and cut ratio, or return None for unsupported geometry.

    A missing corner list leaves the pad unchamfered. KiCad's default ratio
    for selected corners is 0.2. Both routing models support ratios up to 0.5.
    A ``chamfer`` child on a shape other than ``roundrect`` is refused: KiCad
    reads such a pad as a chamfered rectangle whatever shape the file declares
    (Issue #10720), so the declared shape's plain outline over-claims it.
    """
    node = getattr(pad, "_sexp_node", None)
    if node is None:
        return frozenset(), 0.0
    if pad.shape.lower() != "roundrect":
        return None if node.find_child("chamfer") is not None else (frozenset(), 0.0)
    chamfers = node.find_children("chamfer")
    if not chamfers:
        return frozenset(), 0.0
    if len(chamfers) != 1 or not chamfers[0].children:
        return None
    selected: set[str] = set()
    for corner in chamfers[0].children:
        if not corner.is_atom or corner.value not in _CHAMFER_CORNERS:
            return None
        selected.add(corner.value)

    ratios = node.find_children("chamfer_ratio")
    if not ratios:
        return frozenset(selected), 0.2
    if len(ratios) != 1 or len(ratios[0].children) != 1 or not ratios[0].children[0].is_atom:
        return None
    ratio = ratios[0].get_float(0)
    if ratio is None or not 0.0 <= ratio <= 0.5:
        return None
    return frozenset(selected), ratio


def pad_local_point(pad: Pad, x: float, y: float) -> tuple[float, float]:
    """Transform a board point into the residual-angle pad frame.

    KiCad applies clockwise (negative mathematical) board rotations. Inverting
    that transform uses the positive residual angle after IO's cardinal swap.
    """
    x, y = x - pad.x, y - pad.y
    if pad.rotation == 0:
        return x, y
    angle = math.radians(pad.rotation)
    c, s = math.cos(angle), math.sin(angle)
    return c * x - s * y, s * x + c * y


def pad_point_distance(pad: Pad, x: float, y: float) -> float:
    """Unsigned distance to the rectangular envelope (zero inside/on copper)."""
    x, y = pad_local_point(pad, x, y)
    return math.hypot(max(abs(x) - pad.width / 2, 0), max(abs(y) - pad.height / 2, 0))


def pad_contains_point(pad: Pad, x: float, y: float) -> bool:
    """Membership in the rotated rectangle, not its enclosing board AABB."""
    x, y = pad_local_point(pad, x, y)
    return abs(x) <= pad.width / 2 and abs(y) <= pad.height / 2


def pad_segment_distance(pad: Pad, x1: float, y1: float, x2: float, y2: float) -> float:
    """Exact unsigned minimum centerline-to-rectangle distance."""
    x1, y1 = pad_local_point(pad, x1, y1)
    x2, y2 = pad_local_point(pad, x2, y2)
    w, h = pad.width / 2, pad.height / 2
    if (-w <= x1 <= w and -h <= y1 <= h) or (-w <= x2 <= w and -h <= y2 <= h):
        return 0.0
    corners = [(-w, -h), (w, -h), (w, h), (-w, h)]
    return min(
        segment_to_segment_distance(x1, y1, x2, y2, *a, *b)
        for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
    )


def pad_directional_extent(pad: Pad, dx: float, dy: float) -> float:
    """Support extent along a board direction (unit vector gives millimeters)."""
    # Translate-free inverse rotation of the direction, then rectangle support.
    lx, ly = pad_local_point(pad, pad.x + dx, pad.y + dy)
    return abs(lx) * pad.width / 2 + abs(ly) * pad.height / 2
