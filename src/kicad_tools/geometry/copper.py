"""Shared, checker-agnostic trace-copper geometry (Issue #4176).

This module is the single source of truth for "what copper does a PCB trace
segment actually occupy" — the real shapely polygon of a segment's copper,
built as the segment centerline (``LineString``) buffered outward by half its
``width``.  It mirrors the courtyard-polygon precedent (#4182,
:mod:`kicad_tools.geometry.courtyard`) that swapped a bbox/tolerance
approximation for real shapely polygon intersection.

Why this exists
---------------

``NetStatusAnalyzer`` (and ``kct check``'s connectivity rule, which defers to
it) historically unioned trace copper on *endpoint proximity* — a
``POSITION_TOLERANCE = 0.01`` mm Euclidean radius between segment endpoints,
pad centers, and via centers — never testing whether the copper *shapes*
actually touch.  A segment endpoint landing near-but-not-on a pad's copper
therefore blessed an electrically-open net (kct reported "complete" while
KiCad's ``pcb drc`` reported the pad unconnected; see Issue #4176).

KiCad's connectivity engine bonds two conductors only when their copper
geometry physically overlaps or touches.  The pad/via/zone copper polygons
already exist in :mod:`kicad_tools.validate.connectivity`
(``_pad_copper_polygon`` / ``_fill_solid_region`` / ``_via_copper_geom``); the
only missing primitive was the trace-segment polygon this module provides.

It also owns the single pad-outline primitive, :func:`pad_copper_outline`
(Issues #6452, #6453): the exact rect / roundrect / circle / oval copper of a
pad, never eroded.  Pad-pad and pad-fill contact is overlap or a shared
edge (within a tessellation allowance, Issue #6490); pad-via contact is a
positive intersection area (:data:`CONTACT_AREA_EPSILON`).  Every
connectivity model builds its pad
geometry from it via :mod:`kicad_tools.validate.copper_contact`.

``shapely`` is a core dependency (see :mod:`kicad_tools._shapely`); these
helpers return ``None`` when it is unavailable so callers on a real-geometry
path can fail loud rather than silently degrade.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import has_shapely as _has_shapely
from kicad_tools.core.geometry import point_to_segment_distance, rotate_pad_offset

if TYPE_CHECKING:
    from kicad_tools.sexp import SExp

if _has_shapely():  # pragma: no cover - import guard exercised by environment
    from shapely.geometry import LineString as _ShapelyLineString  # type: ignore[import-untyped]
    from shapely.geometry import Point as _ShapelyPoint
    from shapely.geometry import Polygon as _ShapelyPolygon


# Minimum intersection area (mm^2) that counts as copper contact between a
# pad outline and a via annulus (and between two via annuli).  Far below any
# real contact (a 0.05 mm deep contact on a 0.3 mm pad is ~0.015 mm^2) and
# far above floating-point noise on a shared edge (Issues #6452, #6453).
# Exact pad outlines against other pads or fills instead bond at zero gap,
# matching kicad-cli on a shared edge (Issue #6490; see
# ``kicad_tools.validate.copper_contact._TOUCH``).
CONTACT_AREA_EPSILON: float = 1e-6

# Inward erosion (mm) for pad shapes this module does NOT model exactly
# (``trapezoid``, ``custom`` primitives).  Their
# size box over-reaches the real copper at the corners, so the box is shrunk
# by this margin to stay a conservative under-approximation: such a pad can
# only bond *less* readily than its real copper, never across a clearance
# moat.  Exact outlines (rect / roundrect / circle / oval) need no erosion --
# the shrink existed only to cancel the box's corner over-reach, and applying
# it to real copper hid shallow thermal-spoke contact (Issue #6453).
PAD_FALLBACK_ERODE: float = 0.1

# Pad shapes whose copper outline :func:`pad_copper_outline` builds exactly.
EXACT_PAD_SHAPES: frozenset[str] = frozenset({"rect", "roundrect", "circle", "oval", "obround"})


# KiCad's stored chamfer ratio when ``(chamfer_ratio ...)`` is omitted (Issue #10669).
_DEFAULT_CHAMFER_RATIO = 0.2


def pad_chamfered_outline(
    source: SExp | None, default_ratio: float
) -> tuple[tuple[float, float], ...]:
    """Boundary of a mixed chamfered/rounded pad, relative to its centre in board axes.

    ``source`` is the pad's raw ``(pad ...)`` node.  A corner listed in
    ``(chamfer ...)`` is a straight cut of ``chamfer_ratio * min(w, h)`` along
    each edge; every other corner keeps its ``(roundrect_rratio ...)`` radius,
    or ``default_ratio * min(w, h)`` when that token is absent.  The vertices
    are already turned by the pad's absolute ``(at x y ANGLE)``, so a caller
    adds only the copper centre.

    An omitted (or unparseable) ``(chamfer_ratio ...)`` is KiCad's stored
    default of 0.2, not "no cut": KiCad cuts the named corner regardless.

    Returns ``()`` when no corner is cut -- no ``(chamfer ...)`` list, or an
    explicit zero ratio -- and the ordinary ``roundrect``
    construction stands.  One construction shared by the router loaders
    (:func:`kicad_tools.router.io._chamfered_outlines`) and
    :func:`pad_copper_outline` (Issue #10669).
    """
    if source is None or (corners := source.find_child("chamfer")) is None:
        return ()
    ratio = source.find_child("chamfer_ratio")
    size = source.find_child("size")
    if size is None:
        return ()
    width, height = size.get_float(0) or 0.0, size.get_float(1) or 0.0
    short = min(width, height)
    chamfer_ratio = ratio.get_float(0) if ratio is not None else None
    if chamfer_ratio is None:
        chamfer_ratio = _DEFAULT_CHAMFER_RATIO
    cut = min(0.5, chamfer_ratio) * short
    if cut <= 0:
        return ()
    rounding = source.find_child("roundrect_rratio")
    rratio = rounding.get_float(0) if rounding is not None else None
    rounded = (default_ratio if rratio is None else max(0.0, min(0.5, rratio))) * short
    at = source.find_child("at")
    rotation = (at.get_float(2) or 0.0) if at is not None else 0.0
    selected = {item.value for item in corners.children}
    vertices: list[tuple[float, float]] = []
    for name, sx, sy, angle in (
        ("top_left", -1, -1, 180),
        ("top_right", 1, -1, 270),
        ("bottom_right", 1, 1, 0),
        ("bottom_left", -1, 1, 90),
    ):
        # A chamfer replaces the round on that corner. Its two tangent
        # points describe one straight cut. Uncut corners retain the same
        # inscribed arc resolution as pad_gate_polygon.
        chamfered = name in selected
        radius = cut if chamfered else rounded
        steps = 1 if chamfered or radius == 0 else 32
        cx, cy = sx * (width / 2 - radius), sy * (height / 2 - radius)
        for i in range(steps + 1):
            theta = math.radians(angle + 90 * i / steps)
            x, y = cx + radius * math.cos(theta), cy + radius * math.sin(theta)
            vertices.append(rotate_pad_offset(x, y, rotation))
    return tuple(vertices)


def pad_outline_is_exact(pad: Any) -> bool:
    """Whether :func:`pad_copper_outline` models ``pad``'s copper exactly.

    A ``roundrect`` is exact whether or not corners are chamfered (Issue #10669).
    """
    shape = (getattr(pad, "shape", "") or "").lower()
    return shape in EXACT_PAD_SHAPES


# The refusal half of ``custom_pad_outline_detail``: the router's ValueError
# reads "Cannot preserve placement-excluded custom pad REF.PIN: <detail>; only
# gr_circle and filled or stroked gr_poly primitives ...".  The detail is the
# part a check finding wants (it names the primitive kind, e.g. "unsupported
# primitive 'gr_arc'"); the wrapper sentence is routing-speak, and the pad
# identity is added by the calling rule, which knows the footprint reference.
_CUSTOM_REFUSAL_DETAIL = re.compile(r"^[^:]+: (.*?); only ", re.DOTALL)


def custom_pad_outline_detail(
    pad: Any, center: tuple[float, float]
) -> tuple[Any | None, str | None]:
    """Board-frame copper of a ``custom`` pad and, on refusal, the reason.

    Issue #9498.  The schema pad only carries the anchor's nominal ``(size)``;
    the primitives (SOT-89's tab, for one) live in the retained source node, so
    this reuses :func:`kicad_tools.router.fixed_copper.custom_pad_copper` -- the
    outline the DSN export, router and connectivity already honour (#5376,
    #7525, #9468) -- rather than a second reading of the same S-expression.

    ``center`` is the pad *anchor* in board coordinates; the pad's ABSOLUTE
    angle is applied exactly once inside ``custom_pad_copper``.  Returns
    ``(geometry, None)`` when the builder produced the anchor-plus-primitives
    union, ``(None, detail)`` when it refused (unsupported primitive kind --
    ``gr_rect`` / ``gr_arc`` / ``gr_line`` remain refused, Issue #9501 --
    layer-specific padstack, ...), and ``(None, None)`` when the pad is not
    ``custom``, has no source node, or shapely is unavailable.  The ``detail``
    names the primitive kind where the refusal is about one.

    Memoised on the pad object (keyed by the centre and the pad's ABSOLUTE
    rotation, each rounded to nm): one pad is asked for its outline by several
    rules per ``kct check`` run, and the builder re-parses the pad's
    S-expression and unions shapely geometry each time.  The rotation is part
    of the key because it is the other mutable input ``custom_pad_copper``
    reads: an in-place rotate (``recovery.applicator.
    _apply_rotate_component``) leaves ``fp.position`` -- and with it a pad at
    the footprint anchor's board centre -- untouched while advancing the pad's
    absolute angle, so a centre-only key would hand back the pre-rotation
    outline.
    """
    if (getattr(pad, "shape", "") or "").lower() != "custom":
        return None, None
    node = getattr(pad, "_sexp_node", None)
    if node is None or not _has_shapely():
        return None, None
    cache_key = (
        round(center[0], 9),
        round(center[1], 9),
        round(float(getattr(pad, "rotation", 0.0) or 0.0), 9),
    )
    cached = pad.__dict__.get("_custom_outline_detail")
    if cached is not None and cached[0] == cache_key:
        return cached[1], cached[2]
    from kicad_tools.router.fixed_copper import custom_pad_copper

    try:
        copper = custom_pad_copper(
            node.to_string(),
            reference="",
            pad_number=str(getattr(pad, "number", "")),
            x=center[0],
            y=center[1],
            rotation=float(getattr(pad, "rotation", 0.0) or 0.0),
            source_net="",
            source_net_id=0,
        )
    except ValueError as exc:
        match = _CUSTOM_REFUSAL_DETAIL.match(str(exc))
        detail = match.group(1) if match is not None else str(exc)
        pad.__dict__["_custom_outline_detail"] = (cache_key, None, detail)
        return None, detail
    geometry = copper.geometry
    if geometry.is_empty:
        pad.__dict__["_custom_outline_detail"] = (cache_key, None, "primitives enclose no copper")
        return None, "primitives enclose no copper"
    pad.__dict__["_custom_outline_detail"] = (cache_key, geometry, None)
    return geometry, None


def custom_pad_board_outline(pad: Any, center: tuple[float, float]) -> Any | None:
    """Board-frame copper of a ``custom`` pad: its anchor plus filled polygons.

    Thin wrapper over :func:`custom_pad_outline_detail` for callers that only
    need the geometry: ``None`` when the pad is not ``custom``, has no source
    node, or its copper is something the builder refuses (see the detail
    variant for the named reason, Issue #9498); callers then keep their
    nominal-box fallback.
    """
    geometry, _refusal = custom_pad_outline_detail(pad, center)
    return geometry


def pad_board_center(
    pad: Any,
    fp_position: tuple[float, float],
    fp_rotation: float,
) -> tuple[float, float]:
    """Board-frame position of a pad's anchor (its ``(at x y)``).

    Pad coordinates are footprint-local; they rotate with the footprint
    (KiCad's negated-angle convention, see
    :func:`kicad_tools.core.geometry.rotate_pad_offset`).
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    px, py = pad.position
    rx, ry = rotate_pad_offset(px, py, fp_rotation)
    return (fp_position[0] + rx, fp_position[1] + ry)


def pad_size_box(
    pad: Any,
    fp_position: tuple[float, float],
    fp_rotation: float,
    erode: float = 0.0,
) -> Any | None:
    """A pad's sharp ``size`` box in the board frame, optionally eroded.

    The pre-#6453 pad approximation: correct only for ``rect`` pads, and an
    over-reach at the corners of every rounded shape.  Kept for shapes
    :func:`pad_copper_outline` does not model exactly and for legacy
    (``strict=False``) net-status, whose historical behaviour is pinned.
    Erosion that would empty the box returns the un-eroded box.
    """
    if not _has_shapely():
        return None
    cx, cy = pad_board_center(pad, fp_position, fp_rotation)
    w, h = pad.size
    if w <= 0 or h <= 0:
        return _ShapelyPoint((cx, cy))
    angle = math.radians(-(getattr(pad, "rotation", 0.0) or 0.0))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    box = _ShapelyPolygon(
        [(cx + x * cos_a - y * sin_a, cy + x * sin_a + y * cos_a) for x, y in corners]
    )
    if erode > 0:
        eroded = box.buffer(-erode)
        if not eroded.is_empty:
            return eroded
    return box


def pad_copper_outline(
    pad: Any,
    fp_position: tuple[float, float],
    fp_rotation: float,
) -> Any | None:
    """Board-frame shapely geometry of a pad's copper (Issues #6452, #6453).

    The single pad-outline primitive every connectivity model shares:

    * ``rect`` -> the exact rectangle;
    * ``roundrect`` -> rectangle with corner radius
      ``roundrect_rratio * min(w, h)`` (KiCad's default ratio 0.25); a corner
      named in ``(chamfer ...)`` is a straight cut instead
      (:func:`pad_chamfered_outline`, Issue #10669);
    * ``circle`` -> disk of diameter ``min(w, h)``;
    * ``oval`` / ``obround`` -> stadium (a circle when ``w == h``).

    The outline is oriented by the pad's **absolute** angle (``pad.rotation``
    already includes the footprint rotation; KiCad's negated-angle
    convention, Issue #6460), centred on the pad anchor plus its rotated
    ``(offset ...)``, and is **not** eroded: contact is decided against the
    other copper exactly as KiCad collides a pad's effective shape with a
    zone fill (a shared edge touches, Issue #6490).  With the true rounded corners there is no over-reach to cancel --
    the old 0.1 mm erosion of a sharp box both hid real shallow contact
    (#6453) and was load-bearing only because the box's corners grazed
    foreign pours.

    A ``custom`` pad uses its real anchor-plus-filled-polygon copper (Issue
    #9468, :func:`custom_pad_board_outline`).  Shapes not modelled exactly
    (``trapezoid``, and a ``custom`` pad whose copper that helper refuses)
    fall back to the size box eroded by
    :data:`PAD_FALLBACK_ERODE` (un-eroded if the erosion empties it), which
    can only under-report contact.  A pad with a non-positive size is its
    bare anchor point.  Returns ``None`` when shapely is unavailable.
    """
    if not _has_shapely():
        return None
    cx, cy = pad_board_center(pad, fp_position, fp_rotation)
    custom = custom_pad_board_outline(pad, (cx, cy))
    if custom is not None:
        return custom
    w, h = pad.size
    if w <= 0 or h <= 0:
        return _ShapelyPoint((cx, cy))
    angle = math.radians(-(getattr(pad, "rotation", 0.0) or 0.0))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    ox, oy = getattr(pad, "drill_offset", (0.0, 0.0)) or (0.0, 0.0)

    def _to_board(lx: float, ly: float) -> tuple[float, float]:
        lx += ox
        ly += oy
        return (cx + lx * cos_a - ly * sin_a, cy + lx * sin_a + ly * cos_a)

    shape = (getattr(pad, "shape", "") or "").lower()
    exact = pad_outline_is_exact(pad)
    if exact and shape == "roundrect":
        # A chamfered corner is copper the size box does not have (Issue
        # #10669).  The vertices arrive already turned by the pad's absolute
        # angle, so only the copper centre is added; a repeated vertex marks
        # an unrounded corner.
        vertices = pad_chamfered_outline(getattr(pad, "_sexp_node", None), 0.25)
        if vertices:
            ccx, ccy = _to_board(0.0, 0.0)
            ring: list[tuple[float, float]] = []
            for x, y in vertices:
                point = (ccx + x, ccy + y)
                if not ring or point != ring[-1]:
                    ring.append(point)
            return _ShapelyPolygon(ring)
    radius = 0.0
    if exact and shape in ("circle", "oval", "obround"):
        radius = min(w, h) / 2.0
    elif exact and shape == "roundrect":
        ratio = getattr(pad, "roundrect_rratio", 0.25)
        ratio = 0.25 if ratio is None else max(0.0, min(float(ratio), 0.5))
        radius = ratio * min(w, h)
    if exact and shape == "circle":
        core: Any = _ShapelyPoint(_to_board(0.0, 0.0))
    elif radius > 0.0:
        hx, hy = w / 2.0 - radius, h / 2.0 - radius
        corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
        if hx <= 1e-12 and hy <= 1e-12:
            core = _ShapelyPoint(_to_board(0.0, 0.0))
        elif hx <= 1e-12 or hy <= 1e-12:
            ends = [(-hx, -hy), (hx, hy)]
            core = _ShapelyLineString([_to_board(*e) for e in ends])
        else:
            core = _ShapelyPolygon([_to_board(*c) for c in corners])
    else:
        corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        core = _ShapelyPolygon([_to_board(*c) for c in corners])
    if radius > 0.0:
        return core.buffer(radius, quad_segs=32)
    if not exact and PAD_FALLBACK_ERODE > 0:
        eroded = core.buffer(-PAD_FALLBACK_ERODE, join_style="mitre")
        if not eroded.is_empty:
            return eroded
    return core


def segment_copper_polygon(
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
) -> Any | None:
    """Build a board-frame shapely polygon approximating a trace's copper.

    A KiCad trace segment is a rectangle of length ``|end - start|`` and
    thickness ``width`` with semicircular end caps (round line caps).  That is
    exactly ``LineString([start, end]).buffer(width / 2)``, matching the
    ``buffer()`` idiom :meth:`ConnectivityValidator._via_copper_geom` already
    uses for via circles.

    Two trace copper polygons are electrically bonded iff they
    ``intersects()`` — including the near-miss case Issue #4176 reports, where
    an endpoint sits just outside neighboring copper.

    Degenerate inputs are handled defensively (mirroring the ``w <= 0 or
    h <= 0`` guard in :meth:`ConnectivityValidator._pad_copper_polygon`):

    * ``width <= 0`` — a zero/negative-width segment has no copper thickness;
      return the bare centerline geometry (a ``LineString``, or a ``Point``
      when the segment is also zero-length) so an exact endpoint coincidence
      still registers as contact without manufacturing spurious area.
    * ``start == end`` (zero length) with positive width — return the round
      copper pad (a disk of radius ``width / 2``) the trace's end cap would
      occupy.

    Returns ``None`` when shapely is unavailable.
    """
    if not _has_shapely():
        return None

    if start == end:
        point = _ShapelyPoint(start)
        if width <= 0:
            return point
        return point.buffer(width / 2)

    line = _ShapelyLineString([start, end])
    if width <= 0:
        return line
    return line.buffer(width / 2)


def collinear_interval_overlap(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
    tol: float = 1e-6,
) -> float | None:
    """Signed interval overlap of two collinear centrelines. A negative value measures a gap."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= tol or math.dist(c, d) <= tol:
        return None
    stations = []
    for x, y in (c, d):
        x, y = x - a[0], y - a[1]
        if abs(dx * y - dy * x) > tol * length:
            return None
        stations.append((dx * x + dy * y) / length)
    return min(length, max(stations)) - max(0.0, min(stations))


def collinear_overlap_length(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
    tol: float = 1e-6,
) -> float | None:
    """Length shared by two finite collinear centrelines, or None for a gap or bend."""
    overlap = collinear_interval_overlap(a, b, c, d, tol)
    return max(0.0, overlap) if overlap is not None and overlap >= -tol else None


def extends_collinear(
    prev_start: tuple[float, float],
    prev_end: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
    tol: float = 1e-4,
) -> bool:
    """True when ``start->end`` continues ``prev_start->prev_end`` as one straight run.

    The single collinearity test behind trace-run fusion (#8277 router, #8303
    creepage census): the second segment starts at the first one's end (within
    ``tol``), points the same way, and lies on the first one's line.  Callers
    own the layer / width / net equality checks, which differ by segment type.
    """
    if abs(prev_end[0] - start[0]) > tol or abs(prev_end[1] - start[1]) > tol:
        return False
    dx, dy = prev_end[0] - prev_start[0], prev_end[1] - prev_start[1]
    ex, ey = end[0] - prev_start[0], end[1] - prev_start[1]
    length = math.hypot(dx, dy)
    ahead = (end[0] - start[0]) * dx + (end[1] - start[1]) * dy
    return length > tol and ahead > 0.0 and abs(dx * ey - dy * ex) <= tol * length


def point_segment_distance(
    point: tuple[float, float],
    seg_start: tuple[float, float],
    seg_end: tuple[float, float],
) -> float:
    """Shortest distance from ``point`` to the closed segment ``start-end``.

    Delegates to :func:`kicad_tools.core.geometry.point_to_segment_distance`,
    whose rebased-difference form is exactly translation-invariant (#3714,
    #7224).
    """
    return point_to_segment_distance(
        point[0], point[1], seg_start[0], seg_start[1], seg_end[0], seg_end[1]
    )


def _orientation(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
    """Signed area of the triangle ``p, q, r`` (positive = counter-clockwise)."""
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def segment_centerline_distance(
    a_start: tuple[float, float],
    a_end: tuple[float, float],
    b_start: tuple[float, float],
    b_end: tuple[float, float],
) -> float:
    """Exact minimum distance between two closed 2-D segments.

    Segments that cross (or touch) return ``0.0``; otherwise the minimum is
    always attained at one of the four endpoint-to-segment distances, which
    is the standard closed-form result for convex sets.

    This is the analytic equivalent of
    ``LineString(a).distance(LineString(b))``, evaluated without constructing
    shapely objects — the connectivity extractor calls it on every segment
    pair, and it is also exact for the degenerate zero-length case (where a
    "segment" is a point).
    """
    # A *proper* crossing (each segment strictly straddles the other's
    # supporting line) is the one configuration whose minimum distance is
    # NOT attained at an endpoint, so it needs its own test.  Every
    # improper contact — touching, T-shaped, collinear overlap — puts an
    # endpoint of one segment on the other, which the endpoint minimum
    # below already reports as 0.
    o1 = _orientation(a_start, a_end, b_start)
    o2 = _orientation(a_start, a_end, b_end)
    o3 = _orientation(b_start, b_end, a_start)
    o4 = _orientation(b_start, b_end, a_end)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return 0.0
    return min(
        point_segment_distance(b_start, a_start, a_end),
        point_segment_distance(b_end, a_start, a_end),
        point_segment_distance(a_start, b_start, b_end),
        point_segment_distance(a_end, b_start, b_end),
    )


def segments_copper_touch(
    a_start: tuple[float, float],
    a_end: tuple[float, float],
    a_width: float,
    b_start: tuple[float, float],
    b_end: tuple[float, float],
    b_width: float,
) -> bool:
    """True iff two same-layer trace segments' swept copper touches.

    A trace's copper is its centerline buffered by ``width / 2`` (see
    :func:`segment_copper_polygon`), i.e. a capsule.  Two capsules intersect
    **iff** the distance between their centerlines is at most the sum of
    their radii — so this is exactly
    ``segment_copper_polygon(a...).intersects(segment_copper_polygon(b...))``
    but exact (shapely's ``buffer`` approximates the round caps with a
    finite-resolution polygon, which *under*-covers the true copper) and far
    cheaper.

    Two consequences matter for connectivity (issue #5060):

    * contact is decided over the **full** swept copper, so a T-junction
      landing on a track's interior — or a side-on width overlap with no
      shared endpoint at all — is contact, exactly as KiCad's own
      connectivity engine sees it; and
    * the predicate is **invariant** under splitting a segment into
      collinear subsegments with the same copper union: the centerline
      distance minimum is attained at some point of the centerline, which
      lies on one of the subsegments, and no subsegment centerline reaches
      anywhere the whole centerline did not.

    Widths are clamped at zero, so a width-less segment contributes only its
    bare centerline (never negative reach).
    """
    reach = max(a_width or 0.0, 0.0) / 2.0 + max(b_width or 0.0, 0.0) / 2.0
    # Cheap axis-aligned reject before the exact test: the connectivity
    # extractor evaluates this over every segment pair on the board.
    for axis in (0, 1):
        if (
            min(a_start[axis], a_end[axis]) > max(b_start[axis], b_end[axis]) + reach
            or min(b_start[axis], b_end[axis]) > max(a_start[axis], a_end[axis]) + reach
        ):
            return False
    return segment_centerline_distance(a_start, a_end, b_start, b_end) <= reach


@lru_cache(maxsize=4096)
def pad_waiver_polygon(x: float, y: float, w: float, h: float, shape: str, rotation: float):
    """Cache static pad copper, using the same primitives as the audit.

    Returns the shapely polygon of a rect / circle / oval pad at board
    position ``(x, y)`` rotated by ``rotation`` degrees, or ``None`` for a
    shape it does not model (``roundrect``, custom) or a degenerate size.
    Moved here from ``router/lattice/obstacles.py`` (``_pad_waiver_polygon``)
    by issue #6837 so engine-free pair planning (``router.pairplan``) can
    measure exact pad copper without importing the lattice engine; the lattice
    imports it back under its old name, so there is one cache, not two.
    """
    from shapely.affinity import rotate, translate  # type: ignore[import-untyped]
    from shapely.geometry import Point, box

    if w <= 0 or h <= 0 or shape == "roundrect":
        return None
    if shape == "circle" or (shape == "oval" and abs(w - h) < 1e-6):
        return Point(x, y).buffer(min(w, h) / 2)
    if shape == "rect":
        polygon = box(-w / 2, -h / 2, w / 2, h / 2)
    elif shape == "oval":
        radius = min(w, h) / 2
        core = (
            box(-(w / 2 - radius), 0, w / 2 - radius, 0)
            if w >= h
            else box(0, -(h / 2 - radius), 0, h / 2 - radius)
        )
        polygon = core.buffer(radius)
    else:
        return None
    return translate(rotate(polygon, -rotation, origin=(0, 0)), x, y)


def standard_pad_polygon(
    cx: float, cy: float, w: float, h: float, shape: str, rotation: float, rratio: float
):
    """Board-space standard pad copper used by physical-gap DRC and route shortcuts."""
    import shapely  # type: ignore[import-untyped]
    from shapely.affinity import rotate, translate
    from shapely.geometry import Point

    if w <= 0 or h <= 0:
        return None

    if shape == "circle" or (shape in ("oval", "obround") and abs(w - h) < 1e-6):
        # Symmetric disc -- rotation and the (origin) translate are no-ops
        # on a circle, so build it directly at the board position.
        return Point(cx, cy).buffer(min(w, h) / 2.0)

    if shape in ("oval", "obround"):
        # Stadium / capsule: a core rectangle shrunk by the minor
        # half-axis on the long dimension, buffered by that radius so the
        # short ends become true semicircles.
        r = min(w, h) / 2.0
        if w >= h:
            core = shapely.box(-(w / 2.0 - r), 0.0, (w / 2.0 - r), 0.0)
        else:
            core = shapely.box(0.0, -(h / 2.0 - r), 0.0, (h / 2.0 - r))
        poly = core.buffer(r)
    elif shape == "roundrect":
        r = rratio * min(w, h)
        if r <= 0:
            poly = shapely.box(-w / 2.0, -h / 2.0, w / 2.0, h / 2.0)
        else:
            inner = shapely.box(-(w / 2.0 - r), -(h / 2.0 - r), (w / 2.0 - r), (h / 2.0 - r))
            poly = inner.buffer(r, join_style=1)  # round joins -> rounded corners
    else:  # "rect" and any unknown shape -> exact rectangle (no over-approx)
        poly = shapely.box(-w / 2.0, -h / 2.0, w / 2.0, h / 2.0)

    # Match KiCad's forward transform, without adding footprint rotation a
    # second time. AABB/cardinal tests cannot detect the sign error (#5227).
    poly = rotate(poly, -rotation, origin=(0, 0), use_radians=False)
    return translate(poly, cx, cy)


@lru_cache(maxsize=4096)
def pad_gate_polygon(
    x: float, y: float, w: float, h: float, shape: str, rotation: float, rratio: float | None
):
    """Pad copper as the HV commit gate measures it, ``roundrect`` included (#8327).

    :func:`pad_waiver_polygon` returns ``None`` for a ``roundrect`` pad, which
    sent the lattice search's attach-zone waiver (``_pad_zone_waives``) to its
    whole-segment-in-one-zone fallback for nearly every SMD pad: an edge that
    crosses a rated footprint's attach-zone boundary near the foreign pad of
    the same footprint was refused although the gate accepts its residual, so
    a two-terminal tap part could become unreachable.  This models the
    roundrect the way the clearance kernel does (``_pad_core_local``): the
    file's ``rratio`` (clamped to KiCad's ``[0, 0.5]``) rounds the corners;
    an unknown ratio is the enclosing rectangle, which holds the real copper
    and so can only under-report a gap.  Every other shape defers to
    :func:`pad_waiver_polygon`.
    """
    if shape != "roundrect":
        return pad_waiver_polygon(x, y, w, h, shape, rotation)
    from shapely.affinity import rotate, translate
    from shapely.geometry import LineString, Point, box

    if w <= 0 or h <= 0:
        return None
    radius = 0.0 if rratio is None else max(0.0, min(float(rratio), 0.5)) * min(w, h)
    if radius <= 0.0:
        polygon = box(-w / 2, -h / 2, w / 2, h / 2)
    else:
        hx, hy = w / 2 - radius, h / 2 - radius
        if hx <= 1e-12 and hy <= 1e-12:
            core = Point(0.0, 0.0)
        elif hx <= 1e-12 or hy <= 1e-12:
            core = LineString([(-max(hx, 0.0), -max(hy, 0.0)), (max(hx, 0.0), max(hy, 0.0))])
        else:
            core = box(-hx, -hy, hx, hy)
        polygon = core.buffer(radius, quad_segs=32)
    return translate(rotate(polygon, -rotation, origin=(0, 0)), x, y)
