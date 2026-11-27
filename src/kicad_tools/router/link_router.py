"""Route one KiCad-reported missing link against fixed copper (Issue #5785).

The oracle completion loop (:mod:`kicad_tools.router.oracle_completion`)
first tries to weld a stranded pour pad into its plane with a via.  When no
via site reaches the plane -- a fine-pitch IC pin whose only via sites sit in
the plane's clearance pullback, a pad on a layer the pour never covers -- the
link has to be *routed*: copper from one end of the link to the other, around
everything already on the board.

:func:`route_link` is that router.  It is deliberately small and
self-contained, because a link is short and the board is otherwise finished:

* a uniform multi-layer grid over the link's bounding box plus a margin,
  aligned on the link's first endpoint so a pad's own axis is a grid line;
* every other net's copper (pads, tracks, vias, arcs and unnetted copper
  pads), inflated by ``clearance + trace_width / 2``, blocks a cell; the
  board outline (inset by the edge clearance) bounds the grid;
* other nets' **zone fills** do not block -- KiCad refills a pour around new
  copper -- but cost extra per mm, so a link prefers not to carve a plane;
* a layer change is a through via, allowed only where the via clears foreign
  copper on every layer, every existing drill by the hole-to-hole rule, and
  every pad (no via-in-pad);
* the search starts on the cells inside endpoint A's copper and ends on any
  cell inside endpoint B's copper, so the new copper bonds to both;
* the result is simplified to straight runs and then **checked exactly**
  against the foreign copper with shapely -- grid quantisation can never leak
  a sub-clearance segment into the board.

Credit: KiCadRoutingTools routes oracle links with a dedicated plane-join
router (``py_router/plane_region_connector.py:route_plane_connection_wide``,
https://github.com/drandyhaas/KiCadRoutingTools, MIT).  This module follows the
same idea -- a small dedicated A* for a single missing link -- on our own PCB
model; no KRT code is copied.

:func:`route_link_junction` is the ``kct route-link --join-track`` opt-in
(Issue #10486).  Instead of entering the destination pad it joins a track that
is already electrically connected to that pad, at a junction outside every
pad, so the new copper never meets the pad's mask opening.  The route carries
the junction (:attr:`LinkRoute.junction`) and :func:`append_link_routes`
splits the track there as it writes the branch.

All coordinates here are the :class:`~kicad_tools.schema.pcb.PCB` model's
board-relative millimetres.  :func:`append_link_route` adds the board origin
back when it writes the copper into the file.
"""

from __future__ import annotations

import heapq
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.router.via_clearance import COORD_WRITE_GUARD_MM

if TYPE_CHECKING:
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules
    from kicad_tools.schema.pcb import PCB, Segment

__all__ = [
    "LinkJunction",
    "LinkRoute",
    "LinkRouteRules",
    "LinkTerminal",
    "PlaneCorridor",
    "append_link_route",
    "append_link_routes",
    "collapse_link_route",
    "explain_link_failure",
    "junction_sites",
    "padless_components",
    "route_link",
    "route_link_junction",
    "route_link_via",
]

#: Route endpoints land at least this far inside the terminal copper (mm).
_TERMINAL_INSET = 0.03

#: Issue #10486: tracks tried as junction hosts, nearest first.  Each costs one
#: link search, so the opt-in stays bounded on a net with thousands of tracks.
_JUNCTION_CANDIDATES = 8

#: Join an existing endpoint instead of making a sub-grid sliver (mm).
_JUNCTION_END_TOL = 0.0075
#: A KiCad board stores coordinates in integer nanometres.
_JUNCTION_DIGITS = 6

#: Issue #10486: one candidate junction -- its straight-line distance from the
#: source, the track, and the point on that track's centreline.
_JunctionSite = tuple[float, "Segment", tuple[float, float]]

_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


@dataclass(frozen=True)
class LinkRouteRules:
    """Geometry and cost knobs for :func:`route_link` (all mm)."""

    trace_width: float = 0.2
    clearance: float = 0.2
    via_size: float = 0.6
    via_drill: float = 0.3
    hole_to_hole: float = 0.5
    #: Issue #10393: copper-to-hole clearance (mm) kept between a track or via
    #: and the edge of a non-plated hole (KiCad's board-setup default
    #: ``min_hole_clearance``, ``KICAD_DEFAULT_HOLE_CLEARANCE_MM``).
    hole_clearance: float = 0.25
    edge_clearance: float = 0.5
    grid: float = 0.05
    margin: float = 4.0
    via_cost: float = 2.0
    foreign_fill_cost: float = 4.0
    max_cells: int = 1_500_000
    #: Issue #8974: the fab-view same-net pad slit-band edge (mm) -- the
    #: ``GapBand.detection_edge_mm`` (#9016) of the resolved stackup's
    #: ``pad_spacing`` dfm entry, resolved by the caller (``kct route`` threads
    #: ``DesignRules.same_net_pad_gap_band_edge_mm`` onto the weld's rules).
    #: A link via whose annulus parks inside ``(0, edge)`` beside its own
    #: SMD pad is rejected and the search walks on.  ``0`` (default) keeps
    #: the guard off.
    same_net_pad_gap_mm: float = 0.0
    #: Issue #6374: per-neighbour-net clearance (net number -> mm), the
    #: ``max(class(link net), class(neighbour))`` rule the grid router applies
    #: (``io._get_pair_clearance``).  ``clearance`` stays the floor: a net
    #: mapped below it (or absent) keeps ``clearance``.  ``None`` keeps the
    #: single-scalar behaviour exactly.
    clearance_by_net: Mapping[int, float] | None = None
    #: Issue #8573: ``AttachZone`` regions (as built by
    #: ``pairwise_clearance.build_attach_zones``) inside which the map-derived
    #: floor of a net in ``attach_relax_nets`` relaxes to ``clearance`` -- the
    #: same waiver ``kct creepage --waive-attach-zone`` applies.  Copper of such
    #: a net outside the zone keeps its full floor.  ``net_names`` maps net
    #: number -> name for the zone's name-keyed test.
    attach_zones: Sequence[Any] = ()
    attach_relax_nets: frozenset[int] = frozenset()
    net_names: Mapping[int, str] | None = None
    #: Project clearance rules for this board, evaluated on each copper layer.
    board_rules: BoardClearanceRules | None = None

    width_by_layer: Mapping[str, float] = field(default_factory=dict)

    def width_for(self, layer: str) -> float:
        return self.width_by_layer.get(layer, self.trace_width)

    def clearance_for(self, net_number: int) -> float:
        """Clearance this link must keep from copper on ``net_number``."""
        if self.clearance_by_net is None:
            return self.clearance
        return max(self.clearance, self.clearance_by_net.get(net_number, self.clearance))

    def clearance_parts(
        self,
        link_net_name: str,
        net_number: int,
        layer: str,
        geom: Any,
        item: _Item | None = None,
        moving: str = "Track",
    ) -> list[tuple[Any, float]]:
        """``geom`` (foreign copper of ``net_number`` on ``layer``) split by clearance.

        Inside a shared attach zone, a map-derived floor may relax to the
        larger of the base clearance and the applicable KiCad copper rule.
        """
        floor = self.clearance
        if item is not None and item.hole:
            # Issue #10393: a bare non-plated drill is no copper item, so the
            # copper-pair rules do not apply; it keeps the hole clearance.
            return [(geom, max(self.hole_clearance, self.clearance_for(net_number)))]
        if self.board_rules is not None and self.net_names is not None:
            from .board_clearance_rules import PAD_TYPE_NAMES, ItemProps

            other_name = self.net_names.get(net_number, "")
            if item is None:
                other = ItemProps(None, other_name)
            elif item.is_pad:
                other = ItemProps(
                    "Pad",
                    other_name,
                    plated=item.pad_type == "thru_hole" if item.pad_type else bool(item.drill),
                    pad_type=PAD_TYPE_NAMES.get(item.pad_type or "")
                    or ("Through-hole" if item.drill else "SMD"),
                    layer=item.footprint_layer,
                    footprint_ref=item.ref,
                    footprint_lib_id=item.lib_id,
                )
            elif len(item.layers) > 1:
                other = ItemProps(
                    "Via", other_name, plated=True, footprint_ref="", footprint_lib_id=""
                )
            else:
                other = ItemProps(
                    "Track",
                    other_name,
                    plated=False,
                    layer=layer,
                    footprint_ref="",
                    footprint_lib_id="",
                )
            # ``moving`` is the item being placed: a track mask and a via mask
            # resolve separately, so a ``A.Type == 'Via'`` rule never leaks
            # onto tracks (or the reverse).
            if moving == "Via":
                mover = ItemProps(
                    "Via", link_net_name, plated=True, footprint_ref="", footprint_lib_id=""
                )
            else:
                mover = ItemProps(
                    "Track",
                    link_net_name,
                    plated=False,
                    layer=layer,
                    footprint_ref="",
                    footprint_lib_id="",
                )
            floor = max(floor, self.board_rules.clearance(mover, other, layer))
        full = max(floor, self.clearance_for(net_number))
        if (
            not self.attach_zones
            or net_number not in self.attach_relax_nets
            or full <= floor
            or not self.net_names
            or net_number not in self.net_names
            or not link_net_name
        ):
            return [(geom, full)]
        from shapely.geometry import box  # type: ignore[import-untyped]

        from .pairwise_clearance import _norm_net_key

        a, b = _norm_net_key(link_net_name), _norm_net_key(self.net_names[net_number])
        parts: list[tuple[Any, float]] = []
        rest = geom
        for z in self.attach_zones:
            if a not in z.net_names or b not in z.net_names:
                continue
            if not (z.covers_layer(a, layer) and z.covers_layer(b, layer)):
                continue
            zbox = box(z.min_x, z.min_y, z.max_x, z.max_y)
            inside = rest.intersection(zbox)
            if not inside.is_empty:
                parts.append((inside, floor))
                rest = rest.difference(zbox)
        if not rest.is_empty:
            parts.append((rest, full))
        return parts


@dataclass(frozen=True)
class PlaneCorridor:
    """An inner layer and a closed rectangle in routing coordinates (mm)."""

    layer: str
    x0: float
    y0: float
    x1: float
    y1: float

    def contains(self, x: Any, y: Any, radius: float) -> Any:
        # Works on both grid arrays and exact segment endpoints. The inset of
        # a rectangle is convex, so two legal endpoints contain the full stroke.
        # Origin translation and insetting can shift coincident edges by a
        # few float ulps. This allowance is 1/1000 of KiCad's 1 nm board unit.
        epsilon = 1e-9
        return (
            (x >= self.x0 + radius - epsilon)
            & (x <= self.x1 - radius + epsilon)
            & (y >= self.y0 + radius - epsilon)
            & (y <= self.y1 - radius + epsilon)
        )

    def covers_route(self, route: LinkRoute) -> bool:
        radius = route.width_for(self.layer) / 2
        return all(
            self.contains(x1, y1, radius) and self.contains(x2, y2, radius)
            for x1, y1, x2, y2, layer in route.segments
            if layer == self.layer
        )


@dataclass
class LinkTerminal:
    """One end of a link: the same-net copper the route must bond to.

    ``layers`` maps each copper layer name the item exists on to its
    board-relative shapely geometry there.
    """

    label: str
    point: tuple[float, float]
    layers: dict[str, Any]


@dataclass(frozen=True)
class LinkJunction:
    """Where a :class:`LinkRoute` branches off an existing track (Issue #10486).

    ``point`` lies on the centreline of the straight track ``start`` -> ``end``
    (``width``, ``layer`` and ``uuid`` as the board file has it) that the
    router chose.  All coordinates are board-relative, like the route's.
    """

    point: tuple[float, float]
    layer: str
    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    uuid: str = ""
    coordinate_digits: int = 2

    def file_point(self, origin: tuple[float, float]) -> tuple[float, float]:
        """The common absolute endpoint of both host halves and the branch."""
        return round(self.point[0] + origin[0], self.coordinate_digits), round(
            self.point[1] + origin[1], self.coordinate_digits
        )


@dataclass
class LinkRoute:
    """Copper that closes one link."""

    net_number: int
    net_name: str
    width: float
    via_size: float
    via_drill: float
    segments: list[tuple[float, float, float, float, str]] = field(default_factory=list)
    vias: list[tuple[float, float]] = field(default_factory=list)

    width_by_layer: Mapping[str, float] = field(default_factory=dict)
    #: Issue #10486: the existing track this route branches off, when it does.
    junction: LinkJunction | None = None

    @property
    def coordinate_digits(self) -> int:
        return self.junction.coordinate_digits if self.junction is not None else 2

    def width_for(self, layer: str) -> float:
        return self.width_by_layer.get(layer, self.width)

    def capsules(self) -> list[tuple[float, float, float, float, float]]:
        """``(x1, y1, x2, y2, half_width)`` per segment and via (board-relative)."""
        caps = [
            (x1, y1, x2, y2, self.width_for(layer) / 2) for x1, y1, x2, y2, layer in self.segments
        ]
        caps.extend((x, y, x, y, self.via_size / 2) for x, y in self.vias)
        return caps


# ---------------------------------------------------------------------------
# Board copper model
# ---------------------------------------------------------------------------


def _copper_layer_names(pcb: PCB) -> list[str]:
    from kicad_tools.core.layers import COPPER_LAYER_ORDER, is_declared_copper_layer

    declared = {
        layer.name
        for layer in pcb.layers.values()
        if is_declared_copper_layer(layer.name, layer.type)
    }
    names = [name for name in COPPER_LAYER_ORDER if name in declared]
    return names or ["F.Cu", "B.Cu"]


def _pad_layers(pad_layers: list[str], copper: list[str]) -> list[str]:
    out: list[str] = []
    for layer in copper:
        for spec in pad_layers:
            if spec == layer or spec == "*.Cu" or (spec == "F&B.Cu" and layer in ("F.Cu", "B.Cu")):
                out.append(layer)
                break
    return out


def _padstack_lands(fp: Any, pad: Any, layers: Collection[str]) -> dict[str, Any] | None:
    """Standard copper outlines per layer, or None when the pad has uniform geometry.

    KiCad keeps the front definition on the pad and the other definitions under
    its padstack. An unmodelled or missing layer cannot establish contact.
    """
    node = getattr(pad, "_sexp_node", None)
    stack = node.find_child("padstack") if node is not None else None
    if stack is None:
        return None
    mode = stack.find_child("mode")
    name = mode.get_string(0) if mode is not None else None
    if name == "normal" and not stack.find_children("layer"):
        return None
    if name not in ("front_inner_back", "custom"):
        return {}

    from kicad_tools.geometry.copper import (
        EXACT_PAD_SHAPES,
        pad_copper_outline,
        pad_outline_is_exact,
    )

    by_name: dict[str, Any] = {}
    if pad_outline_is_exact(pad):
        by_name["F.Cu"] = pad_copper_outline(pad, fp.position, fp.rotation)
    for entry in stack.find_children("layer"):
        layer = entry.get_string(0)
        size, shape = entry.find_child("size"), entry.find_child("shape")
        if layer is None or size is None or shape is None:
            continue
        kind = shape.get_string(0)
        w, h = size.get_float(0), size.get_float(1)
        if kind not in EXACT_PAD_SHAPES or entry.find_child("chamfer") is not None:
            continue
        if (
            w is None
            or h is None
            or not (math.isfinite(w) and math.isfinite(h) and w > 0 and h > 0)
        ):
            continue
        offset = entry.find_child("offset")
        dx, dy = (offset.get_float(0), offset.get_float(1)) if offset is not None else (0.0, 0.0)
        rounding = entry.find_child("roundrect_rratio")
        ratio = rounding.get_float(0) if rounding is not None else 0.25
        if any(value is None or not math.isfinite(value) for value in (dx, dy, ratio)):
            continue
        # Detach the view: mutation of a linked Pad writes through to the board.
        land = replace(
            pad,
            _sexp_node=None,
            shape=kind,
            size=(w, h),
            drill_offset=(dx, dy),
            roundrect_rratio=ratio,
        )
        by_name[layer] = pad_copper_outline(land, fp.position, fp.rotation)
    result = {}
    for layer in layers:
        key = "Inner" if name == "front_inner_back" and layer.startswith("In") else layer
        if (outline := by_name.get(key)) is not None:
            result[layer] = outline
    return result


def _pad_geometry(fp: Any, pad: Any) -> Any:
    """Board-relative copper outline of a footprint pad (un-eroded)."""
    from shapely.geometry import LineString, Point, Polygon

    from kicad_tools.core.geometry import rotate_pad_offset

    ox, oy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
    cx, cy = fp.position[0] + ox, fp.position[1] + oy
    w, h = pad.size
    if w <= 0 or h <= 0:
        return Point(cx, cy)
    # A pad's (at x y angle) carries its ABSOLUTE board angle (issue #3902);
    # KiCad's negated-angle convention maps the local box into the board.
    a = math.radians(-(getattr(pad, "rotation", 0.0) or 0.0))
    ca, sa = math.cos(a), math.sin(a)

    def to_board(lx: float, ly: float) -> tuple[float, float]:
        return (cx + lx * ca - ly * sa, cy + lx * sa + ly * ca)

    shape = (getattr(pad, "shape", "") or "").lower()
    if shape in ("circle", "oval"):
        radius = min(w, h) / 2
        half = abs(w - h) / 2
        if half <= 0:
            return Point(cx, cy).buffer(radius)
        ends = [(-half, 0.0), (half, 0.0)] if w >= h else [(0.0, -half), (0.0, half)]
        return LineString([to_board(*e) for e in ends]).buffer(radius)
    return Polygon(
        [
            to_board(-w / 2, -h / 2),
            to_board(w / 2, -h / 2),
            to_board(w / 2, h / 2),
            to_board(-w / 2, h / 2),
        ]
    )


@dataclass
class _Item:
    net: int
    layers: dict[str, Any]
    is_pad: bool = False
    drill: float = 0.0
    #: Pad identity for project rules (``B.Reference`` / ``memberOfFootprint``).
    ref: str | None = None
    lib_id: str | None = None
    footprint_layer: str | None = None
    #: Schema pad ``type`` (``smd`` / ``thru_hole`` / ``connect`` / ...).
    pad_type: str | None = None
    #: Issue #10393: the opening of a non-plated hole -- it pierces every copper
    #: layer and demands ``LinkRouteRules.hole_clearance`` from tracks and vias.
    hole: bool = False


@dataclass
class _BoardModel:
    copper: list[str]
    items: list[_Item]
    drills: list[tuple[float, float, float]]
    pad_geoms: list[Any]
    fills: dict[str, list[tuple[int, Any]]]
    outline: Any
    #: Issue #10486: copper accepted earlier in this invocation, as the track and
    #: via objects a later ``--join-track`` link may join (see ``_commit_to_model``).
    extra_segments: list[Any] = field(default_factory=list)
    extra_vias: list[tuple[float, float, float, int]] = field(default_factory=list)


def _build_model(pcb: PCB) -> _BoardModel:
    from shapely.geometry import LineString, Point, Polygon

    copper = _copper_layer_names(pcb)
    items: list[_Item] = []
    drills: list[tuple[float, float, float]] = []
    pad_geoms: list[Any] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if (getattr(pad, "type", "") or "") == "np_thru_hole":
                drill = float(getattr(pad, "drill", 0.0) or 0.0)
                if drill > 0:
                    geom = _pad_geometry(fp, pad)
                    c = geom.centroid
                    drills.append((c.x, c.y, drill))
                    # Issue #10393: the opening itself, on every copper layer,
                    # plus any annular copper the pad carries.  Both are
                    # unnetted (net 0), so any link net must keep clear.
                    items.append(
                        _Item(
                            0,
                            dict.fromkeys(copper, Point(c.x, c.y).buffer(drill / 2)),
                            True,
                            drill,
                            getattr(fp, "reference", None) or "",
                            getattr(fp, "name", None) or "",
                            fp.layer,
                            "np_thru_hole",
                            hole=True,
                        )
                    )
                    npth_layers = _pad_layers(list(getattr(pad, "layers", []) or []), copper)
                    if npth_layers:
                        items.append(
                            _Item(
                                0,
                                dict.fromkeys(npth_layers, geom),
                                True,
                                drill,
                                getattr(fp, "reference", None) or "",
                                getattr(fp, "name", None) or "",
                                fp.layer,
                                "np_thru_hole",
                            )
                        )
                continue
            layers = _pad_layers(list(getattr(pad, "layers", []) or []), copper)
            if not layers:
                continue
            geom = _pad_geometry(fp, pad)
            pad_drill = float(getattr(pad, "drill", 0.0) or 0.0)
            lands = dict.fromkeys(layers, geom)
            stack = _padstack_lands(fp, pad, layers)
            if stack is not None:
                lands.update(stack)
            pad_geoms.extend(lands.values() if stack is not None else (geom,))
            items.append(
                _Item(
                    int(getattr(pad, "net_number", 0) or 0),
                    lands,
                    True,
                    pad_drill,
                    getattr(fp, "reference", None) or "",
                    getattr(fp, "name", None) or "",
                    fp.layer,
                    getattr(pad, "type", None) or None,
                )
            )
            drill = pad_drill
            if drill > 0:
                c = geom.centroid
                drills.append((c.x, c.y, drill))
    for seg in pcb.segments:
        if seg.layer not in copper:
            continue
        line = LineString([seg.start, seg.end]) if seg.start != seg.end else Point(seg.start)
        items.append(_Item(seg.net_number, {seg.layer: line.buffer(seg.width / 2)}))
    for arc in getattr(pcb, "arcs", []) or []:
        layer = getattr(arc, "layer", None)
        pts = [
            p
            for p in (
                getattr(arc, "start", None),
                getattr(arc, "mid", None),
                getattr(arc, "end", None),
            )
            if p
        ]
        if layer in copper and len(pts) >= 2:
            width = float(getattr(arc, "width", 0.2) or 0.2)
            items.append(
                _Item(
                    int(getattr(arc, "net_number", 0) or 0),
                    {layer: LineString(pts).buffer(width / 2)},
                )
            )
    for via in pcb.vias:
        disk = Point(via.position).buffer((via.size or 0.0) / 2)
        items.append(_Item(via.net_number, dict.fromkeys(copper, disk)))
        if via.drill:
            drills.append((via.position[0], via.position[1], float(via.drill)))
    fills: dict[str, list[tuple[int, Any]]] = {}
    for zone in pcb.zones:
        polys = getattr(zone, "filled_polygons", None) or []
        layers = getattr(zone, "filled_polygon_layers", None) or []
        for idx, pts in enumerate(polys):
            if len(pts) < 3:
                continue
            layer = layers[idx] if idx < len(layers) else getattr(zone, "layer", None)
            if layer not in copper:
                continue
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                fills.setdefault(layer, []).append((zone.net_number, poly))
    outline_pts = pcb.get_board_outline()
    outline = Polygon(outline_pts) if len(outline_pts) >= 3 else None
    if outline is not None and not outline.is_valid:
        outline = outline.buffer(0)
    return _BoardModel(copper, items, drills, pad_geoms, fills, outline)


def terminal_for_pad(pcb: PCB, ref: str, pad_number: str) -> LinkTerminal | None:
    """The copper of footprint pad ``ref.pad_number`` as a :class:`LinkTerminal`."""
    copper = _copper_layer_names(pcb)
    fp = pcb.get_footprint(ref)
    if fp is None:
        return None
    for pad in fp.pads:
        if str(pad.number) == str(pad_number):
            layers = _pad_layers(list(getattr(pad, "layers", []) or []), copper)
            if not layers:
                return None
            geom = _pad_geometry(fp, pad)
            c = geom.centroid
            stack = _padstack_lands(fp, pad, layers)
            # The ordinary named-pad route retains its existing approximation
            # for unmodelled shapes. Junctions separately require exact lands.
            lands = dict.fromkeys(layers, geom)
            if stack is not None:
                lands.update(stack)
            return LinkTerminal(f"{ref}.{pad_number}", (c.x, c.y), lands)
    return None


def terminal_at_copper(
    pcb: PCB, net_number: int, point: tuple[float, float], layer: str | None, kind: str
) -> LinkTerminal | None:
    """The same-net via or track KiCad anchored a link end at (board-relative)."""
    from shapely.geometry import LineString, Point

    copper = _copper_layer_names(pcb)
    probe = Point(point)
    if kind == "via":
        best = None
        for via in pcb.vias:
            if via.net_number != net_number:
                continue
            d = probe.distance(Point(via.position))
            if d <= max((via.size or 0) / 2, 0.05) and (best is None or d < best[0]):
                best = (d, via)
        if best is None:
            return None
        via = best[1]
        disk = Point(via.position).buffer((via.size or 0.0) / 2)
        return LinkTerminal("via", via.position, dict.fromkeys(copper, disk))
    if kind == "track":
        for seg in pcb.segments:
            if seg.net_number != net_number or (layer and seg.layer != layer):
                continue
            line = LineString([seg.start, seg.end]) if seg.start != seg.end else Point(seg.start)
            if line.distance(probe) <= seg.width / 2 + 1e-3:
                return LinkTerminal("track", point, {seg.layer: line.buffer(seg.width / 2)})
    return None


def net_components(model: _BoardModel, net_number: int) -> list[dict[str, Any]]:
    """Copper islands of one net, KiCad-style, as ``{layer: geometry}`` dicts.

    Nodes are the net's pads, tracks, vias and zone-fill islands; two nodes
    join when their copper intersects on a layer they share.  Sorted largest
    first by total copper area, so ``[0]`` is the main body of the net.
    """
    nodes: list[dict[str, Any]] = [dict(it.layers) for it in model.items if it.net == net_number]
    for layer, fills in model.fills.items():
        nodes.extend({layer: poly} for net, poly in fills if net == net_number)
    return _merge_nodes(nodes, model.copper)


def _merge_nodes(nodes: list[dict[str, Any]], copper: list[str]) -> list[dict[str, Any]]:
    """Union ``{layer: geometry}`` nodes that touch on a shared layer, largest first."""
    from shapely import STRtree  # type: ignore[import-untyped]
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    parent = list(range(len(nodes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for layer in copper:
        idx = [i for i, n in enumerate(nodes) if layer in n]
        if len(idx) < 2:
            continue
        geoms = [nodes[i][layer] for i in idx]
        tree = STRtree(geoms)
        left, right = tree.query(geoms, predicate="intersects")
        for li_, ri_ in zip(left, right, strict=True):
            a, b = find(idx[int(li_)]), find(idx[int(ri_)])
            if a != b:
                parent[a] = b
    groups: dict[int, list[int]] = {}
    for i in range(len(nodes)):
        groups.setdefault(find(i), []).append(i)
    comps: list[dict[str, Any]] = []
    for members in groups.values():
        per_layer: dict[str, list[Any]] = {}
        for i in members:
            for layer, geom in nodes[i].items():
                per_layer.setdefault(layer, []).append(geom)
        comps.append({layer: unary_union(gs) for layer, gs in per_layer.items()})
    comps.sort(key=lambda c: -sum(g.area for g in c.values()))
    return comps


def _via_span(layers: list[str], copper: list[str]) -> list[str]:
    """Copper layers a via with these end layers actually touches (blind/buried aware)."""
    idx = [copper.index(layer) for layer in layers if layer in copper]
    if len(idx) != 2 or len(layers) != 2:
        return []
    return copper[min(idx) : max(idx) + 1]


def _join_island(pcb: PCB, model: _BoardModel, net_number: int, pad: LinkTerminal) -> Any:
    """The copper island holding ``pad``, with true via spans and arc geometry.

    Issue #10486.  :func:`net_components` models a via on every layer and an
    arc as its chords, which can call a blind via's far side or a chord's
    neighbour "connected".  Here a via covers only the layers it spans, an arc
    follows its circle, and copper accepted earlier in this run is included.
    """
    from shapely.geometry import LineString, Point

    from kicad_tools.geometry.copper import pad_copper_outline, pad_outline_is_exact

    copper = model.copper
    nodes: list[dict[str, Any]] = []
    destination = None
    for fp in pcb.footprints:
        for pin in fp.pads:
            if pin.net_number != net_number:
                continue
            layers = _pad_layers(pin.layers, copper)
            lands = _padstack_lands(fp, pin, layers)
            if lands is None:
                if not pad_outline_is_exact(pin):
                    continue
                outline = pad_copper_outline(pin, fp.position, fp.rotation)
                lands = dict.fromkeys(layers, outline) if outline is not None else {}
            if not lands:
                continue
            nodes.append(lands)
            if f"{fp.reference}.{pin.number}" == pad.label and destination is None:
                destination = nodes[-1]
    if not destination:
        return None
    for seg in [*pcb.segments, *model.extra_segments]:
        if seg.net_number != net_number or seg.layer not in copper:
            continue
        line = LineString([seg.start, seg.end]) if seg.start != seg.end else Point(seg.start)
        nodes.append({seg.layer: line.buffer(seg.width / 2)})
    for arc in getattr(pcb, "arcs", []) or []:
        if int(getattr(arc, "net_number", 0) or 0) != net_number or arc.layer not in copper:
            continue
        try:
            pts = arc.centerline_points(0.005)
        except ValueError:
            pts = [arc.start, arc.mid, arc.end]
        nodes.append({arc.layer: LineString(pts).buffer(float(arc.width or 0.2) / 2)})
    for via in pcb.vias:
        if via.net_number == net_number:
            center = Point(via.position)
            lands = {
                layer: center.buffer(diameter / 2)
                for layer in _via_span(list(via.layers or []), copper)
                if (diameter := via.copper_diameter(layer)) is not None and diameter > 0
            }
            if lands:
                nodes.append(lands)
    for vx, vy, size, via_net in model.extra_vias:
        if via_net != net_number:
            continue
        nodes.append(dict.fromkeys(copper, Point(vx, vy).buffer(size / 2)))
    for layer, fills in model.fills.items():
        nodes.extend({layer: poly} for net, poly in fills if net == net_number)
    return next(
        (
            comp
            for comp in _merge_nodes(nodes, copper)
            if any(
                layer in comp and comp[layer].intersects(geom)
                for layer, geom in destination.items()
            )
        ),
        None,
    )


def padless_components(
    model: _BoardModel, net_number: int, comps: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Copper islands of ``net_number`` that contain no pad, largest first.

    KiCad reports a ``Zone <-> Zone`` unconnected item when a filled island
    that carries no pad of its own floats free of the rest of the net (the
    zone outline anchor says nothing about *which* island).  Those are the
    islands to bond: pour fragments - possibly with a dangling stub - that no
    pad or plane reaches.
    """
    pads = [it for it in model.items if it.net == net_number and getattr(it, "is_pad", False)]
    out = []
    if comps is None:
        comps = net_components(model, net_number)
    if len(comps) < 2:
        return []
    for comp in comps:
        if any(
            layer in comp and comp[layer].intersects(g)
            for pad in pads
            for layer, g in pad.layers.items()
        ):
            continue
        out.append(comp)
    return out


def component_terminals(
    comp: dict[str, Any], others: list[dict[str, Any]], label: str
) -> tuple[LinkTerminal, LinkTerminal] | None:
    """Terminals joining ``comp`` to the union of ``others`` at their closest approach."""
    from shapely.ops import nearest_points, unary_union

    if not others:
        return None
    rest: dict[str, list[Any]] = {}
    for other in others:
        for layer, geom in other.items():
            rest.setdefault(layer, []).append(geom)
    rest_u = {layer: unary_union(gs) for layer, gs in rest.items()}
    a_all = unary_union(list(comp.values()))
    b_all = unary_union(list(rest_u.values()))
    pa, pb = nearest_points(a_all, b_all)
    return (
        LinkTerminal(f"{label}:island", (pa.x, pa.y), comp),
        LinkTerminal(f"{label}:rest", (pb.x, pb.y), rest_u),
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def route_link(
    pcb: PCB,
    net_number: int,
    net_name: str,
    a: LinkTerminal,
    b: LinkTerminal,
    rules: LinkRouteRules,
    *,
    model: _BoardModel | None = None,
    allowed_layers: Collection[str] | None = None,
    plane_corridor: PlaneCorridor | None = None,
    grid_anchor: tuple[float, float] | None = None,
    grid_digits: int = 2,
) -> LinkRoute | None:
    """Route copper from terminal ``a`` to terminal ``b`` on net ``net_number``.

    Returns ``None`` when no clearance-legal route exists inside the search
    window (or the window would exceed ``rules.max_cells``).

    ``allowed_layers`` (Issue #8310) restricts the link to those copper
    layers: tracks and vias keep off every other layer, and a terminal only
    bonds on an allowed layer.  ``None`` keeps every layer. ``plane_corridor``
    confines tracks on its inner layer to the board-relative rectangle,
    including their full width. The caller validates the native refill on commit.
    """
    import numpy as np
    import shapely
    from shapely.geometry import Point, box
    from shapely.ops import unary_union

    model = model or _build_model(pcb)
    copper = model.copper
    g = rules.grid

    (ax, ay), (bx, by) = a.point, b.point
    x0, x1 = min(ax, bx) - rules.margin, max(ax, bx) + rules.margin
    y0, y1 = min(ay, by) - rules.margin, max(ay, by) + rules.margin
    if model.outline is not None:
        ob = model.outline.bounds
        x0, y0, x1, y1 = max(x0, ob[0]), max(y0, ob[1]), min(x1, ob[2]), min(y1, ob[3])
    # Align the grid on the anchor at the precision its route writer uses.
    anchor_x, anchor_y = grid_anchor or a.point
    gx0, gy0 = round(anchor_x, grid_digits), round(anchor_y, grid_digits)
    ix0 = math.floor((x0 - gx0) / g)
    iy0 = math.floor((y0 - gy0) / g)
    nx = int(math.ceil((x1 - gx0) / g)) - ix0 + 1
    ny = int(math.ceil((y1 - gy0) / g)) - iy0 + 1
    nl = len(copper)
    if nx <= 1 or ny <= 1 or nx * ny * nl > rules.max_cells:
        return None
    xs = np.round(gx0 + (ix0 + np.arange(nx)) * g, grid_digits)
    ys = np.round(gy0 + (iy0 + np.arange(ny)) * g, grid_digits)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    flat_x, flat_y = gx.ravel(), gy.ravel()
    reach = 1.0
    if rules.width_by_layer:
        radius = max(max(rules.width_by_layer.values()), rules.via_size) / 2
        clearance = max(rules.clearance, max((rules.clearance_by_net or {}).values(), default=0.0))
        reach = max(reach, radius + clearance + 0.05)
    window = box(xs[0] - reach, ys[0] - reach, xs[-1] + reach, ys[-1] + reach)
    # Copper whose clearance (plus the moving item's radius) reaches the grid
    # matters however far its raw shape lies from it (project rules can demand
    # several mm), so foreign copper is selected by the search area itself.
    search_box = box(xs[0], ys[0], xs[-1], ys[-1])

    def mask_of(geom: Any) -> Any:
        if geom is None or geom.is_empty:
            return np.zeros((nx, ny), dtype=bool)
        shapely.prepare(geom)
        return shapely.intersects_xy(geom, flat_x, flat_y).reshape(nx, ny)

    # Foreign copper per layer, grouped by the clearance it demands (Issue
    # #6374: ``rules.clearance_for(net)``).  Without a per-net map there is
    # exactly one group at ``rules.clearance`` -- the historical behaviour.
    foreign_by_layer: dict[str, list[tuple[Any, float]]] = {}
    via_foreign_by_layer: dict[str, list[tuple[Any, float]]] = {}
    blocked = np.zeros((nl, nx, ny), dtype=bool)
    fill_cost = np.ones((nl, nx, ny), dtype=float)
    legal_areas = {
        width: model.outline.buffer(-(rules.edge_clearance + width / 2))
        for width in {rules.width_for(layer) for layer in copper}
        if model.outline is not None
    }
    inside = {width: mask_of(area) for width, area in legal_areas.items()}
    for li, layer in enumerate(copper):
        width = rules.width_for(layer)
        copper_reach = max(width, rules.via_size) / 2 + 0.01
        by_clearance: dict[float, list[Any]] = {}
        by_clearance_via: dict[float, list[Any]] = {}
        for it in model.items:
            if it.net == net_number or layer not in it.layers:
                continue
            for moving, sink in (("Track", by_clearance), ("Via", by_clearance_via)):
                for part, c in rules.clearance_parts(
                    net_name, it.net, layer, it.layers[layer], it, moving
                ):
                    if part.distance(search_box) <= c + copper_reach:
                        sink.setdefault(c, []).append(part)
        groups = [(unary_union(geoms), c) for c, geoms in sorted(by_clearance.items())]
        foreign_by_layer[layer] = groups
        via_foreign_by_layer[layer] = [
            (unary_union(geoms), c) for c, geoms in sorted(by_clearance_via.items())
        ]
        for foreign, c in groups:
            blocked[li] |= mask_of(foreign.buffer(c + width / 2 + 0.005))
        if inside:
            blocked[li] |= ~inside[width]
        if allowed_layers is not None and layer not in allowed_layers:
            blocked[li] = True
        fills = [
            poly
            for net, poly in model.fills.get(layer, [])
            if net != net_number and poly.intersects(window)
        ]
        if fills:
            fill_cost[li][mask_of(unary_union(fills))] = rules.foreign_fill_cost

    # Via legality: clear of foreign copper on every layer, of every pad
    # (no via-in-pad), of every drill by the hole-to-hole rule.
    via_bad = np.zeros((nx, ny), dtype=bool)
    for groups in via_foreign_by_layer.values():
        for foreign, c in groups:
            via_bad |= mask_of(foreign.buffer(c + rules.via_size / 2 + 0.005))
    pads_here = [p for p in model.pad_geoms if p.intersects(window)]
    if pads_here:
        via_bad |= mask_of(unary_union(pads_here).buffer(rules.via_size / 2 + 0.05))
    # Issue #8974: the fab-view same-net slit band.  The all-pads mask above
    # bans via-in-pad on every net; what it does not constrain is the
    # annulus of a via seated JUST outside its own SMD pad -- two wedges of
    # air beside the connecting stub that ``physical_copper_gap`` grades as
    # errors even same-net (#8938).  The banned ring is the region form of
    # the shared ``same_net_pad_gap_acceptable`` predicate
    # (``same_net_pad_gap_band_ring``, per-pad bbox rings) -- one band
    # definition feeds both this mask and the point re-checks (#8974
    # review: no second band copy).  The A* walks on to the next node
    # exactly as any other illegal seat.
    band_edge_mm = float(getattr(rules, "same_net_pad_gap_mm", 0.0) or 0.0)
    if band_edge_mm > 0:
        from .via_clearance import same_net_pad_gap_band_ring

        own_pad_boxes = [
            next(iter(it.layers.values())).bounds
            for it in model.items
            if it.is_pad and it.net == net_number and it.drill <= 0
            if next(iter(it.layers.values())).intersects(window)
        ]
        if own_pad_boxes:
            band_ring = unary_union(
                [same_net_pad_gap_band_ring(b, rules.via_size, band_edge_mm) for b in own_pad_boxes]
            )
            via_bad |= mask_of(band_ring)
    drill_disks = [
        # Issue #6671: + the slack the board writer's 0.01 mm rounding can eat.
        Point(dx, dy).buffer(
            dd / 2 + rules.via_drill / 2 + rules.hole_to_hole + COORD_WRITE_GUARD_MM
        )
        for dx, dy, dd in model.drills
        if window.distance(Point(dx, dy))
        <= dd / 2 + rules.via_drill / 2 + rules.hole_to_hole + COORD_WRITE_GUARD_MM
    ]
    if drill_disks:
        via_bad |= mask_of(unary_union(drill_disks))
    if model.outline is not None:
        via_bad |= ~mask_of(model.outline.buffer(-(rules.edge_clearance + rules.via_size / 2)))

    def terminal_mask(t: LinkTerminal) -> Any:
        m = np.zeros((nl, nx, ny), dtype=bool)
        for li, layer in enumerate(copper):
            if layer in t.layers:
                geom = t.layers[layer]
                # Keep endpoints strictly inside the copper they bond to (KiCad
                # joins a track to a pad by its END POINT, not by overlap).
                inset = geom.buffer(-_TERMINAL_INSET)
                m[li] = mask_of(geom if inset.is_empty else inset) & ~blocked[li]
        return m

    # Plane layers accept vias by default. Only lateral moves on the
    # explicitly authorized layer use the inset rectangle.
    corridor_li = -1
    corridor_mask = None
    if plane_corridor is not None:
        if plane_corridor.layer not in copper or plane_corridor.layer in ("F.Cu", "B.Cu"):
            return None
        corridor_li = copper.index(plane_corridor.layer)
        corridor_mask = plane_corridor.contains(gx, gy, rules.width_for(plane_corridor.layer) / 2)
    is_plane = [
        li != corridor_li and layer not in ("F.Cu", "B.Cu") and bool(model.fills.get(layer))
        for li, layer in enumerate(copper)
    ]

    start = terminal_mask(a)
    goal = terminal_mask(b)
    if not start.any() or not goal.any():
        return None

    # A* over (layer, i, j).
    inf = float("inf")
    dist = np.full((nl, nx, ny), inf)
    parent: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    heap: list[tuple[float, float, int, int, int]] = []

    def h(i: int, j: int) -> float:
        return math.hypot(xs[i] - bx, ys[j] - by)

    for li, i, j in zip(*np.nonzero(start), strict=True):
        dist[li, i, j] = 0.0
        heapq.heappush(heap, (h(i, j), 0.0, int(li), int(i), int(j)))

    found: tuple[int, int, int] | None = None
    while heap:
        _f, d, li, i, j = heapq.heappop(heap)
        if d > dist[li, i, j]:
            continue
        if goal[li, i, j]:
            found = (li, i, j)
            break
        corridor_cells = corridor_mask if li == corridor_li else None
        lateral = not is_plane[li] and (corridor_cells is None or corridor_cells[i, j])
        for di, dj in _DIRS if lateral else ():
            ni, nj = i + di, j + dj
            if not (0 <= ni < nx and 0 <= nj < ny) or blocked[li, ni, nj]:
                continue
            if corridor_cells is not None and not corridor_cells[ni, nj]:
                continue
            step = g * (math.sqrt(2.0) if di and dj else 1.0)
            # Diagonal moves must not cut a blocked corner.
            if di and dj and (blocked[li, i + di, j] or blocked[li, i, j + dj]):
                continue
            nd = d + step * 0.5 * (fill_cost[li, i, j] + fill_cost[li, ni, nj])
            if nd < dist[li, ni, nj]:
                dist[li, ni, nj] = nd
                parent[(li, ni, nj)] = (li, i, j)
                heapq.heappush(heap, (nd + h(ni, nj), nd, li, ni, nj))
        if not via_bad[i, j]:
            for nli in range(nl):
                if nli == li or blocked[nli, i, j]:
                    continue
                nd = d + rules.via_cost
                if nd < dist[nli, i, j]:
                    dist[nli, i, j] = nd
                    parent[(nli, i, j)] = (li, i, j)
                    heapq.heappush(heap, (nd + h(i, j), nd, nli, i, j))
    if found is None:
        return None
    if dist[found] == 0.0:
        return None  # the two terminals already touch on the grid: nothing to add

    path = [found]
    while path[-1] in parent:
        path.append(parent[path[-1]])
    path.reverse()

    route = LinkRoute(
        net_number=net_number,
        net_name=net_name,
        width=rules.trace_width,
        width_by_layer=rules.width_by_layer,
        via_size=rules.via_size,
        via_drill=rules.via_drill,
    )
    # Split into per-layer runs; a layer change is a via at that cell.
    runs: list[tuple[int, list[tuple[int, int]]]] = []
    for li, i, j in path:
        if runs and runs[-1][0] == li:
            runs[-1][1].append((i, j))
        else:
            if runs:
                route.vias.append((float(xs[i]), float(ys[j])))
            runs.append((li, [(i, j)]))
    for li, cells in runs:
        pts = _simplify(cells)
        xy = [(float(xs[i]), float(ys[j])) for i, j in pts]
        width = rules.width_for(copper[li])
        xy = _shortcut(xy, foreign_by_layer[copper[li]], width / 2, legal_areas.get(width))
        for (x_a, y_a), (x_b, y_b) in zip(xy, xy[1:], strict=False):
            if (x_a, y_a) == (x_b, y_b):
                continue  # Issue #8417: never emit a zero-length track
            route.segments.append((x_a, y_a, x_b, y_b, copper[li]))

    # Exact check: grid quantisation must never leak a sub-clearance segment.
    if plane_corridor is not None and not plane_corridor.covers_route(route):
        return None
    if _route_violates_foreign(route, foreign_by_layer, via_by_layer=via_foreign_by_layer):
        return None
    return route


def _route_violates_foreign(
    route: LinkRoute,
    foreign_by_layer: dict[str, list[tuple[Any, float]]],
    half_w: float | None = None,
    via_by_layer: dict[str, list[tuple[Any, float]]] | None = None,
) -> bool:
    """True if any segment or via of ``route`` is too close to foreign copper.

    A segment with ``start == end`` is measured as a disc of the trace width
    (a point buffered by ``half_w``), exactly like a real track, so a
    degenerate segment lying on foreign copper is rejected (Issue #8417).
    """
    from shapely.geometry import LineString, Point

    for x_a, y_a, x_b, y_b, layer in route.segments:
        shape = (
            Point(x_a, y_a) if (x_a, y_a) == (x_b, y_b) else LineString([(x_a, y_a), (x_b, y_b)])
        )
        radius = route.width_for(layer) / 2 if half_w is None else half_w
        for foreign, c in foreign_by_layer.get(layer, []):
            if shape.distance(foreign) < c + radius - 1e-4:
                return True
    for vx, vy in route.vias:
        p = Point(vx, vy)
        for groups in (via_by_layer if via_by_layer is not None else foreign_by_layer).values():
            for foreign, c in groups:
                if p.distance(foreign) < c + route.via_size / 2 - 1e-4:
                    return True
    return False


def route_clears_floors(route: LinkRoute, model: _BoardModel, rules: LinkRouteRules) -> bool:
    """True if ``route`` keeps every per-net floor of ``rules`` (Issue #8573).

    An exact re-check of finished geometry -- e.g. the output of
    :func:`collapse_link_route`, which merges segments without a clearance
    oracle -- against the same foreign-copper groups :func:`route_link` used.
    """
    from shapely.ops import unary_union

    by_layer: dict[str, list[tuple[Any, float]]] = {}
    via_layer: dict[str, list[tuple[Any, float]]] = {}
    for layer in model.copper:
        by_c: dict[float, list[Any]] = {}
        by_v: dict[float, list[Any]] = {}
        for it in model.items:
            if it.net != route.net_number and layer in it.layers:
                for moving, sink in (("Track", by_c), ("Via", by_v)):
                    for part, c in rules.clearance_parts(
                        route.net_name, it.net, layer, it.layers[layer], it, moving
                    ):
                        sink.setdefault(c, []).append(part)
        by_layer[layer] = [(unary_union(g), c) for c, g in sorted(by_c.items())]
        via_layer[layer] = [(unary_union(g), c) for c, g in sorted(by_v.items())]
    return not _route_violates_foreign(route, by_layer, via_by_layer=via_layer)


def _via_site_blocker(
    model: _BoardModel, net_number: int, site: tuple[float, float], rules: LinkRouteRules
) -> str | None:
    """Why a through via cannot sit at ``site`` (board-relative), or ``None``.

    The same legality :func:`route_link` applies to its own vias: clear of
    foreign copper on every layer, of every pad, of every drill and of the
    board edge.
    """
    from shapely.geometry import Point

    p = Point(site)
    for it in model.items:
        if it.net == net_number:
            continue
        link_name = (rules.net_names or {}).get(net_number, "")
        for layer, geom in it.layers.items():
            for part, c in rules.clearance_parts(link_name, it.net, layer, geom, it, "Via"):
                need = c + rules.via_size / 2
                if p.distance(part) < need - 1e-4:
                    return f"via site is {p.distance(part):.3f} mm from foreign copper on {layer} (needs {need:.3f} mm)"
    for geom in model.pad_geoms:
        if p.distance(geom) < rules.via_size / 2 + 0.05 - 1e-4:
            return "via site is inside or against a pad (no via-in-pad)"
    # Issue #8974: the same-net slit band route_link's mask enforces -- a
    # seat whose annulus parks inside ``(0, band_edge_mm)`` beside its own
    # SMD pad is a fab-view ``physical_copper_gap`` error even same-net.
    # The shared point predicate decides (bboxes, one band definition with
    # the mask's ring and every other producer); seats with a smaller gap
    # are already rejected by the no-via-in-pad loop above, which fires
    # first (#8974 review: no second band copy).
    band_edge_mm = float(getattr(rules, "same_net_pad_gap_mm", 0.0) or 0.0)
    if band_edge_mm > 0:
        from .via_clearance import same_net_pad_gap_acceptable

        own_pad_boxes = [
            next(iter(it.layers.values())).bounds
            for it in model.items
            if it.is_pad and it.net == net_number and it.drill <= 0
        ]
        if not same_net_pad_gap_acceptable(p.x, p.y, rules.via_size, own_pad_boxes, band_edge_mm):
            return (
                f"via site annulus sits inside the {band_edge_mm:.2f} mm "
                f"fab-view slit band beside its own SMD pad (#8974)"
            )
    for dx, dy, dd in model.drills:
        need = dd / 2 + rules.via_drill / 2 + rules.hole_to_hole + COORD_WRITE_GUARD_MM
        if p.distance(Point(dx, dy)) < need - 1e-4:
            return "via site violates the hole-to-hole rule against an existing drill"
    if model.outline is not None and not model.outline.buffer(
        -(rules.edge_clearance + rules.via_size / 2)
    ).covers(p):
        return "via site is too close to the board edge"
    return None


def route_link_via(
    pcb: PCB,
    net_number: int,
    net_name: str,
    a: LinkTerminal,
    b: LinkTerminal,
    via_at: tuple[float, float],
    rules: LinkRouteRules,
    *,
    model: _BoardModel | None = None,
    allowed_layers: Collection[str] | None = None,
    plane_corridor: PlaneCorridor | None = None,
    failure: dict[str, Any] | None = None,
) -> LinkRoute | None:
    """Route ``a`` -> forced via at ``via_at`` -> ``b`` (Issue #8310).

    The search is split in two legs, each a :func:`route_link` call from a
    terminal to the via's own copper, so every rule ``route_link`` applies
    holds on both legs.  Returns ``None`` if the site is illegal or either leg
    has no route.  ``via_at`` is board-relative (snapped to the 0.01 mm the
    board writer keeps).
    """
    from shapely.geometry import Point

    model = model or _build_model(pcb)
    site = (round(via_at[0], 2), round(via_at[1], 2))
    if _via_site_blocker(model, net_number, site, rules) is not None:
        return None
    layers = [layer for layer in model.copper if allowed_layers is None or layer in allowed_layers]
    if len(layers) < 2:
        return None  # a via needs two layers it may connect
    # A declared current path is traced through the barrel centre. Land both
    # legs on that grid point, including for an off-grid waypoint.
    contact = Point(site) if rules.width_by_layer else Point(site).buffer(rules.via_size / 2)
    vt = LinkTerminal("via", site, dict.fromkeys(layers, contact))
    leg1 = route_link(
        pcb,
        net_number,
        net_name,
        a,
        vt,
        rules,
        model=model,
        allowed_layers=layers,
        grid_anchor=site if rules.width_by_layer else None,
        plane_corridor=plane_corridor,
    )
    leg2 = route_link(
        pcb,
        net_number,
        net_name,
        vt,
        b,
        rules,
        model=model,
        allowed_layers=layers,
        plane_corridor=plane_corridor,
    )
    if leg1 is None or leg2 is None:
        if failure is not None:
            start, end = (a, vt) if leg1 is None else (vt, b)
            failure.update(
                explain_link_failure(
                    pcb,
                    net_number,
                    start,
                    end,
                    rules,
                    model=model,
                    allowed_layers=layers,
                    plane_corridor=plane_corridor,
                )
            )
        return None
    return LinkRoute(
        net_number=net_number,
        net_name=net_name,
        width=rules.trace_width,
        width_by_layer=rules.width_by_layer,
        via_size=rules.via_size,
        via_drill=rules.via_drill,
        segments=leg1.segments + leg2.segments,
        vias=[*leg1.vias, site, *leg2.vias],
    )


def _junction_pad_areas(
    pcb: PCB, model: _BoardModel, destination: LinkTerminal
) -> tuple[dict[str, Any], LinkTerminal]:
    """Pad areas a written junction and its branch must clear on each layer."""
    from shapely.ops import unary_union

    from kicad_tools.geometry.copper import pad_copper_outline, pad_outline_is_exact
    from kicad_tools.validate.mask_geometry import (
        board_mask_margin,
        pad_mask_margin,
        pad_mask_sides,
    )

    board_margin, declared = board_mask_margin(pcb)
    default_margin = board_margin if board_margin is not None else math.nan
    board_version = pcb._file_version()
    uniform = []
    per_layer: dict[str, list[Any]] = {}
    unknown_sites: set[str] = set()
    unknown_target: set[str] = set()
    target: dict[str, list[Any]] = {}
    for fp in pcb.footprints:
        for pin in fp.pads:
            mask_layers = {f"{side}.Cu" for side in pad_mask_sides(pin)}
            layers = set(_pad_layers(pin.layers, model.copper)) | mask_layers
            is_target = f"{fp.reference}.{pin.number}" == destination.label
            stack = _padstack_lands(fp, pin, layers)
            if stack is None:
                outline = pad_copper_outline(pin, fp.position, fp.rotation)
                if outline is None:
                    continue
                if not pad_outline_is_exact(pin):
                    outline = outline.union(_pad_geometry(fp, pin))
                uniform.append(outline)
                lands = dict.fromkeys(layers, outline)
            else:
                lands = stack
                unknown_sites.update(layers - lands.keys())
                if is_target:
                    unknown_target.update(layers - lands.keys())
            if mask_layers:
                margin, _ = pad_mask_margin(
                    pin, fp, default_margin, declared, board_version=board_version
                )
            else:
                margin = 0.0
            if margin is None or not math.isfinite(margin):
                return {}, destination
            for layer, land in lands.items():
                # A contracted opening still leaves the copper to avoid.
                expanded = layer in mask_layers and margin > 0
                area = land.buffer(margin, join_style="mitre") if expanded else land
                if stack is not None or expanded:
                    per_layer.setdefault(layer, []).append(area)
                if is_target:
                    target.setdefault(layer, []).append(area)

    projections = unary_union(uniform)
    sites = {
        layer: unary_union([projections, *per_layer[layer]]) if layer in per_layer else projections
        for layer in model.copper
        if layer not in unknown_sites
    }
    target_areas = {layer: unary_union(shapes) for layer, shapes in target.items()}
    target_areas.update(dict.fromkeys(unknown_target))
    return sites, LinkTerminal(destination.label, destination.point, target_areas)


def junction_sites(
    pcb: PCB,
    model: _BoardModel,
    net_number: int,
    pad: LinkTerminal,
    near: tuple[float, float],
    rules: LinkRouteRules,
    *,
    endpoint_layer: str | None = None,
    allowed_layers: Collection[str] | None = None,
    pad_areas: dict[str, Any] | None = None,
) -> list[_JunctionSite]:
    """The straight tracks a branch from ``near`` can join, nearest first.

    Issue #10486.  A track qualifies when it is on ``net_number``, belongs to
    the copper island holding ``pad`` -- so it is electrically connected to the
    pad, which a same-net island that merely carries the name is not -- lies on
    ``endpoint_layer``, and on one of ``allowed_layers`` when that is given.
    Its junction is the point of its centreline nearest to ``near`` that keeps
    half the new track's width from every pad, so the branch's end cap stays
    out of each pad opening.  Returns at most ``_JUNCTION_CANDIDATES`` entries
    of ``(distance to ``near``, track, junction)``.

    Keeping clear of the pads can only move a junction farther from ``near``,
    so a track is only measured against the pads while it can still be among the
    nearest: on a net with thousands of tracks that is a few dozen of them.
    Consecutive pieces of one drawn track share endpoints, so a junction point
    is offered once, for the nearest track that has it.
    """
    import shapely
    from shapely.geometry import LineString, Point
    from shapely.ops import nearest_points

    if pad_areas is None:
        pad_areas, _ = _junction_pad_areas(pcb, model, pad)
    if not pad_areas:
        return []
    island = _join_island(pcb, model, net_number, pad)
    if island is None:
        return []
    for geom in island.values():
        shapely.prepare(geom)
    from_here = Point(near)
    tracks: list[tuple[float, Segment, Any]] = []
    for seg in [*pcb.segments, *model.extra_segments]:
        if (
            seg.net_number != net_number
            or seg.start == seg.end
            or seg.layer not in island
            or seg.layer not in pad_areas
        ):
            continue
        if endpoint_layer is not None and seg.layer != endpoint_layer:
            continue
        if allowed_layers is not None and seg.layer not in allowed_layers:
            continue
        line = LineString([seg.start, seg.end])
        if not line.buffer(seg.width / 2).intersects(island[seg.layer]):
            continue
        tracks.append((line.distance(from_here), seg, line))
    tracks.sort(key=lambda track: track[0])
    keep_out: dict[tuple[str, float], Any] = {}
    taken: set[tuple[float, float, str]] = set()
    sites: list[_JunctionSite] = []
    for unkept, seg, line in tracks:
        if len(sites) == _JUNCTION_CANDIDATES and unkept > sites[-1][0]:
            break  # every track from here on is farther than the nearest kept
        margin = rules.width_for(seg.layer) / 2
        clearance_key = (seg.layer, margin)
        if clearance_key not in keep_out:
            keep_out[clearance_key] = pad_areas[seg.layer].buffer(margin)
        outside = line.difference(keep_out[clearance_key])
        if outside.is_empty:
            continue
        junction = nearest_points(outside, from_here)[0].coords[0]
        # Consecutive pieces of one drawn track can offer the same point.
        key = (
            round(junction[0], _JUNCTION_DIGITS),
            round(junction[1], _JUNCTION_DIGITS),
            seg.layer,
        )
        if key in taken:
            continue
        taken.add(key)
        sites.append((math.dist(junction, near), seg, junction))
        sites.sort(key=lambda site: site[0])
        del sites[_JUNCTION_CANDIDATES:]
    return sites


def _written_point(
    x: float, y: float, origin: tuple[float, float], digits: int = 2
) -> tuple[float, float]:
    """Board-relative point after the writer rounds absolute coordinates."""
    from kicad_tools.sexp.builders import fmt

    ox, oy = origin
    return fmt(x + ox, digits) - ox, fmt(y + oy, digits) - oy


def _file_nm(point: tuple[float, float], origin: tuple[float, float]) -> tuple[int, int]:
    """Absolute integer coordinates as stored in a KiCad PCB."""
    return round((point[0] + origin[0]) * 1_000_000), round((point[1] + origin[1]) * 1_000_000)


def _native_junction(
    point: tuple[float, float], seg: Segment, origin: tuple[float, float]
) -> tuple[float, float]:
    """Nearest file-representable point on the straight host, without bending it."""
    ax, ay = _file_nm(seg.start, origin)
    bx, by = _file_nm(seg.end, origin)
    steps = math.gcd(bx - ax, by - ay)
    if not steps:
        return seg.start
    # All collinear integer points lie on multiples of this primitive step.
    dx, dy = (bx - ax) // steps, (by - ay) // steps
    px = (point[0] - seg.start[0]) * 1_000_000
    py = (point[1] - seg.start[1]) * 1_000_000
    step = max(0, min(steps, round((px * dx + py * dy) / (dx * dx + dy * dy))))
    return (
        round((ax + step * dx) / 1_000_000 - origin[0], _JUNCTION_DIGITS),
        round((ay + step * dy) / 1_000_000 - origin[1], _JUNCTION_DIGITS),
    )


def _branch_clears_pad(
    route: LinkRoute, pad: LinkTerminal, origin: tuple[float, float] = (0.0, 0.0)
) -> bool:
    """True when the written branch copper clears the destination pad and mask.

    Issue #10486: a branch that ends on a track but crosses the destination
    pad on the way defeats the join.  Segments are measured as written
    (coordinates rounded as the writer rounds them).
    """
    from shapely.geometry import LineString, Point

    from kicad_tools.sexp.builders import fmt

    for x1, y1, x2, y2, layer in route.segments:
        if layer not in pad.layers:
            continue
        geom = pad.layers[layer]
        if geom is None:  # the destination has an unmodelled land on this layer
            return False
        a = _written_point(x1, y1, origin, route.coordinate_digits)
        b = _written_point(x2, y2, origin, route.coordinate_digits)
        line = LineString([a, b]) if a != b else Point(a)
        if line.distance(geom) < fmt(route.width_for(layer)) / 2 - 1e-9:
            return False
    # The link writer emits through vias, whose lands occupy both faces.
    for x, y in route.vias:
        center = Point(_written_point(x, y, origin, route.coordinate_digits))
        if any(
            geom is None or center.distance(geom) < fmt(route.via_size) / 2 - 1e-9
            for geom in pad.layers.values()
        ):
            return False
    return True


def route_link_junction(
    pcb: PCB,
    net_number: int,
    net_name: str,
    source: LinkTerminal,
    pad: LinkTerminal,
    rules: LinkRouteRules,
    *,
    model: _BoardModel | None = None,
    endpoint_layer: str | None = None,
    allowed_layers: Collection[str] | None = None,
    plane_corridor: PlaneCorridor | None = None,
    failure: dict[str, Any] | None = None,
) -> LinkRoute | None:
    """Route from ``source`` onto a track electrically connected to ``pad``.

    Issue #10486: the ``kct route-link --join-track`` opt-in.  Each track from
    :func:`junction_sites` is routed to, nearest first, by :func:`route_link`
    against a point terminal on its centreline, so every routing rule still
    validates the branch and it ends exactly where the track is split.  A route
    counts only when its last segment lies on the track's layer and ends on
    that point: one that reaches the junction on another layer through a via
    parked on the track is not a track joining a track, and is passed over.
    The best route wins (fewest vias, then shortest).  The search stops once a
    via-free route is shorter than the straight-line distance to every
    remaining track, measured from inside the source's own copper.  ``None``
    when no track qualifies or no branch that ends on one routes;
    ``failure["reason"]`` then says which.
    """
    from shapely.geometry import Point

    model = model or _build_model(pcb)
    pad_areas, destination = _junction_pad_areas(pcb, model, pad)
    sites = junction_sites(
        pcb,
        model,
        net_number,
        pad,
        source.point,
        rules,
        endpoint_layer=endpoint_layer,
        allowed_layers=allowed_layers,
        pad_areas=pad_areas,
    )
    if not sites:
        if failure is not None:
            failure["reason"] = (
                "no straight track outside every pad is electrically connected to "
                "the destination pad on the allowed layers"
            )
        return None
    source_reach = max(g.hausdorff_distance(Point(source.point)) for g in source.layers.values())
    best: tuple[tuple[int, float], LinkRoute] | None = None
    from kicad_tools.sexp.builders import fmt

    origin = pcb.board_origin
    for dist, seg, raw in sites:
        if best is not None and not best[1].vias and dist - source_reach > best[0][1]:
            break
        site = (round(raw[0], 2), round(raw[1], 2))
        # A junction the writer treats as the host's own end is that end exactly,
        # so the host, the branch and the report share one coordinate.
        for end in (seg.start, seg.end):
            if math.dist(site, end) <= _JUNCTION_END_TOL:
                site = end
                break
        digits = 2
        written = _written_point(*site, origin)
        if site in (seg.start, seg.end):
            if math.dist(site, written) > 1e-9:
                continue
        elif not _holds(written, seg.start, seg.end, origin):
            # Rounding the projection to the usual route grid would bend the
            # host. Try its actual centreline at the board's native precision.
            digits = _JUNCTION_DIGITS
            site = _native_junction(raw, seg, origin)
            written = _written_point(*site, origin, digits)
        if not _holds(written, seg.start, seg.end, origin):
            continue
        cap = Point(written)
        if cap.distance(pad_areas[seg.layer]) < fmt(rules.width_for(seg.layer)) / 2 - 1e-9:
            continue
        target = LinkTerminal(f"junction {seg.layer}", site, {seg.layer: Point(site)})
        route = route_link(
            pcb,
            net_number,
            net_name,
            source,
            target,
            rules,
            model=model,
            allowed_layers=allowed_layers,
            grid_anchor=site,
            grid_digits=digits,
            plane_corridor=plane_corridor,
        )
        if route is None or not route.segments or route.segments[-1][2:] != (*site, seg.layer):
            continue
        route.junction = LinkJunction(
            site, seg.layer, seg.start, seg.end, seg.width, seg.uuid, digits
        )
        if not _branch_clears_pad(route, destination, origin) or not _branch_clears_pad(
            collapse_link_route(route), destination, origin
        ):
            continue
        length = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2, _ in route.segments)
        if best is None or (len(route.vias), length) < best[0]:
            best = (len(route.vias), length), route
    if best is None and failure is not None:
        failure["reason"] = (
            "no routable branch ends on a track connected to the destination pad "
            f"({len(sites)} tried)"
        )
    return best[1] if best is not None else None


def explain_link_failure(
    pcb: PCB,
    net_number: int,
    a: LinkTerminal,
    b: LinkTerminal,
    rules: LinkRouteRules,
    *,
    model: _BoardModel | None = None,
    allowed_layers: Collection[str] | None = None,
    plane_corridor: PlaneCorridor | None = None,
) -> dict[str, Any]:
    """Why a link did not route (Issue #8310): a terminal fault or a blocker.

    Returns ``{"reason": str}``, plus ``"blocker"`` when foreign copper is the
    cause: the item nearest the straight ``a``-``b`` line -- net, layer, its
    distance and the clearance it must keep.  The straight line is a
    diagnostic yardstick, not the router's path.
    """
    from shapely.geometry import LineString, Point

    model = model or _build_model(pcb)
    for label, t in (("A", a), ("B", b)):
        usable = [la for la in t.layers if allowed_layers is None or la in allowed_layers]
        if not usable:
            return {"reason": f"endpoint {label} ({t.label}) has no copper on the allowed layers"}
    line = LineString([a.point, b.point]) if a.point != b.point else Point(a.point)
    best: tuple[float, dict[str, Any]] | None = None
    near: list[tuple[float, str, int, Any, float]] = []  # corridor candidates
    link_name = getattr(pcb.nets.get(net_number), "name", "") or ""
    for it in model.items:
        if it.net == net_number:
            continue
        for layer, geom in it.layers.items():
            if allowed_layers is not None and layer not in allowed_layers:
                continue
            for part, clearance in rules.clearance_parts(link_name, it.net, layer, geom, it):
                d = line.distance(part)
                need = clearance + rules.width_for(layer) / 2
                near.append((d, layer, it.net, part, clearance))
                slack = d - need
                if best is None or slack < best[0]:
                    name = getattr(pcb.nets.get(it.net), "name", "") or f"net {it.net}"
                    best = (
                        slack,
                        {
                            "net": name,
                            "layer": layer,
                            "kind": "pad" if it.is_pad else "copper",
                            "distance_mm": round(d, 3),
                            "required_mm": round(need, 3),
                        },
                    )
    out: dict[str, Any] = {"reason": "no clearance-legal route inside the search window"}
    if best is not None:
        out["blocker"] = best[1]
    bound = _corridor_bound(pcb, rules, near, line)
    if bound is not None:
        out["corridor"] = bound
        out["reason"] = (
            f"corridor too narrow: {bound['net_a']} and {bound['net_b']} ({bound['layer']}) "
            f"are {bound['gap_mm']:.3f} mm apart but the link needs "
            f">= {bound['required_mm']:.3f} mm "
            f"({bound['clearance_a_mm']:.3f} + {bound['clearance_b_mm']:.3f} clearance "
            f"+ {rules.width_for(bound['layer']):.3f} width)"
        )
        if rules.width_by_layer:
            out["reason"] += f"; maximum corridor width {bound['available_width_mm']:.3f} mm"
    elif rules.width_by_layer:
        layers = allowed_layers or model.copper
        out["reason"] += "; required track widths: " + ", ".join(
            f"{layer} {rules.width_for(layer):.3f} mm" for layer in layers
        )
    if plane_corridor is not None:
        out["reason"] += (
            f" with lateral tracks on {plane_corridor.layer} confined to the authorized corridor"
        )
    return out


def _crosses_line(across: Any, link_line: Any) -> bool:
    """True when the gap straddles the leg and runs predominantly across it."""
    (ax, ay), (bx, by) = across.coords[0], across.coords[-1]
    (lx1, ly1), (lx2, ly2) = link_line.coords[0], link_line.coords[-1]
    ux, uy = bx - ax, by - ay
    vx, vy = lx2 - lx1, ly2 - ly1
    nu, nv = math.hypot(ux, uy), math.hypot(vx, vy)
    if nu == 0 or nv == 0:
        return False
    # Parallel rails on the same side also have a perpendicular connector.
    side_a = vx * (ay - ly1) - vy * (ax - lx1)
    side_b = vx * (by - ly1) - vy * (bx - lx1)
    return bool(side_a * side_b < 0 and abs(ux * vx + uy * vy) / (nu * nv) < 0.5)


def _corridor_bound(
    pcb: PCB,
    rules: LinkRouteRules,
    near: list[tuple[float, str, int, Any, float]],
    link_line: Any,
) -> dict[str, Any] | None:
    """The geometric bound when two foreign items flank the link too tightly (#8546).

    Two foreign conductors on one layer, both near the straight a-b line and on
    opposite sides of it, leave a corridor of ``gap``.  The track fits only if
    ``gap >= clearance_a + clearance_b + width``; when it does not, no router
    could succeed on that layer and the bound is a better answer than "no route".
    """
    from shapely.geometry import LineString
    from shapely.ops import nearest_points

    best: tuple[float, dict[str, Any]] | None = None
    cand = sorted(near, key=lambda t: t[0])[:16]
    if len(cand) < 2 or not link_line.length:
        return None
    # Measure only alongside the finite leg. Long rails can have their
    # nearest edges behind an endpoint, where their gap is irrelevant.
    reach = max(2 * clearance + rules.width_for(layer) for _, layer, _, _, clearance in cand)
    window = link_line.buffer(reach, cap_style="flat")
    clipped = []
    for _, layer, net, geom, clearance in cand:
        edge = geom.intersection(window)
        if not edge.is_empty:
            clipped.append((link_line.distance(edge), layer, net, edge, clearance))
    for i, (da, la, na, ga, ca) in enumerate(clipped):
        for db, lb, nb, gb, cb in clipped[i + 1 :]:
            if la != lb or (na == nb and not rules.width_by_layer):
                continue
            # Flanking means the connecting segment runs across the link, not along it.
            p1, p2 = nearest_points(ga, gb)
            across = LineString([p1, p2]) if p1 != p2 else None
            if across is None or not _crosses_line(across, link_line):
                continue
            required = ca + cb + rules.width_for(la)
            gap = ga.distance(gb)
            if gap >= required - 1e-9 or da > required or db > required:
                continue
            slack = gap - required
            if best is None or slack < best[0]:
                name = lambda n: getattr(pcb.nets.get(n), "name", "") or f"net {n}"  # noqa: E731
                best = (
                    slack,
                    {
                        "layer": la,
                        **(
                            {"available_width_mm": round(max(0.0, gap - ca - cb), 3)}
                            if rules.width_by_layer
                            else {}
                        ),
                        "net_a": name(na),
                        "net_b": name(nb),
                        "gap_mm": round(gap, 3),
                        "required_mm": round(required, 3),
                        "clearance_a_mm": round(ca, 3),
                        "clearance_b_mm": round(cb, 3),
                    },
                )
    return best[1] if best else None


def _octilinear(p: tuple[float, float], q: tuple[float, float]) -> bool:
    dx, dy = abs(q[0] - p[0]), abs(q[1] - p[1])
    return dx < 1e-6 or dy < 1e-6 or abs(dx - dy) < 1e-6


def _shortcut(
    pts: list[tuple[float, float]],
    foreign: Sequence[tuple[Any, float]],
    half_width: float,
    legal_area: Any,
) -> list[tuple[float, float]]:
    """Merge staircase corners: jump to the farthest vertex reachable by one
    legal octilinear segment.  Endpoints (and so terminal contact) are kept.

    The A* grid breaks cost ties arbitrarily, so a diagonal run often comes
    out as a zig-zag of grid-step segments.  Each candidate shortcut is
    re-checked exactly against the foreign copper and the board outline.
    ``foreign`` is ``(geometry, clearance)`` groups: a shortcut must keep
    ``clearance + half_width`` from each group's copper (Issue #6374).
    """
    from shapely.geometry import LineString

    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    k = 0
    while k < len(pts) - 1:
        nxt = k + 1
        for m in range(len(pts) - 1, k + 1, -1):
            if not _octilinear(pts[k], pts[m]):
                continue
            line = LineString([pts[k], pts[m]])
            if any(line.distance(geom) < c + half_width - 1e-4 for geom, c in foreign):
                continue
            if legal_area is not None and not legal_area.covers(line):
                continue
            nxt = m
            break
        out.append(pts[nxt])
        k = nxt
    return out


def _simplify(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Keep only the cells where the move direction changes."""
    if len(cells) <= 2:
        return list(cells) if len(cells) == 2 else [cells[0], cells[0]]
    out = [cells[0]]
    for prev, cur, nxt in zip(cells, cells[1:], cells[2:], strict=False):
        if (cur[0] - prev[0], cur[1] - prev[1]) != (nxt[0] - cur[0], nxt[1] - cur[1]):
            out.append(cur)
    out.append(cells[-1])
    return out


def append_link_route(pcb_path: Path, route: LinkRoute, origin: tuple[float, float]) -> None:
    """Write ``route``'s segments and vias into the board file.

    ``origin`` is the PCB model's ``board_origin``: the route is in
    board-relative coordinates and the file is absolute.
    """
    append_link_routes(pcb_path, [route], origin)


def append_link_routes(
    pcb_path: Path, routes: Sequence[LinkRoute], origin: tuple[float, float]
) -> None:
    """Write several routes into the board file with ONE load and ONE save.

    The result is byte-identical to calling :func:`append_link_route` once per
    route in the same order: each call only appends nodes to the document
    tail, and the serializer is a fixed point of load/save.  A board-03
    oracle attempt commits ~20 routes, and each separate load+save of the
    ~750 KB file cost ~0.4 s (Issue #5911).  A route that carries a junction
    (``LinkRoute.junction``, Issue #10486) also splits its host track, in
    place, before the branch is appended.
    """
    if not routes:
        return
    from kicad_tools.core.sexp_file import load_pcb, save_pcb

    # Issue #8523: write copper in the board's own net dialect.
    name_only = _board_is_name_only(pcb_path)
    doc = load_pcb(pcb_path)
    for route in routes:
        _append_route_nodes(doc, route, origin, name_only=name_only)
    save_pcb(doc, pcb_path)


_NAME_ONLY_NET_RE = re.compile(r'\(net\s+"')


def _board_is_name_only(pcb_path: Path) -> bool:
    """True when the board uses KiCad-10 ``(net "name")`` refs (same test as #4416).

    A numeric ``(net N "name")`` table always has a digit after ``(net``, so a
    quoted-first ref appears only on name-based boards.
    """
    try:
        return bool(_NAME_ONLY_NET_RE.search(Path(pcb_path).read_text()))
    except OSError:
        return False


def collapse_link_route(route: LinkRoute) -> LinkRoute:
    """Return a copy of ``route`` with collinear runs and staircases merged (#8523).

    Reuses the ``merge_collinear`` + ``compress_staircase`` passes from #8397,
    per consecutive same-layer run.  Vias and endpoints are preserved.  No
    clearance oracle is available here; callers gate the result with DRC.
    """
    from dataclasses import replace

    from .optimizer.algorithms import compress_staircase, merge_collinear
    from .optimizer.config import OptimizationConfig
    from .primitives import Segment

    cfg = OptimizationConfig()
    out: list[tuple[float, float, float, float, str]] = []
    i = 0
    segs = route.segments
    via_r = route.via_size / 2.0 + 1e-6
    tol = 1e-6

    def _in_via(x: float, y: float) -> bool:
        return any(math.hypot(x - vx, y - vy) <= via_r for vx, vy in route.vias)

    def _breaks_after(k: int) -> bool:
        # A run must stay one contiguous chain: ``compress_staircase`` joins the
        # first start to the last end and would bridge a gap (e.g. the two legs
        # of ``route_link_via``, which touch the via disk at different points)
        # and strand the via with no track end point inside it.  Vias are
        # always run boundaries so their incident end points survive.
        a, b = segs[k], segs[k + 1]
        if a[4] != b[4]:
            return True
        if abs(a[2] - b[0]) > tol or abs(a[3] - b[1]) > tol:
            return True
        return _in_via(a[2], a[3]) or _in_via(b[0], b[1])

    while i < len(segs):
        j = i + 1
        while j < len(segs) and not _breaks_after(j - 1):
            j += 1
        layer = segs[i][4]
        run = [
            Segment(x1, y1, x2, y2, route.width_for(layer), layer, route.net_number, route.net_name)  # type: ignore[arg-type]
            for x1, y1, x2, y2, _l in segs[i:j]
        ]
        run = compress_staircase(merge_collinear(run, cfg), cfg)
        run = merge_collinear(run, cfg)
        out.extend((s.x1, s.y1, s.x2, s.y2, layer) for s in run)
        i = j
    return replace(route, segments=out, vias=list(route.vias))


#: Tolerance matching a junction's host track to its board-file node by its
#: endpoints (mm): the model's board-relative coordinates are the file's less
#: the board origin, a float subtraction that is not bit-exact.
_HOST_MATCH_TOL = 1e-6

#: ``(node, start, end, layer, width)`` of one ``(segment ...)`` board-file node.
_FileTrack = tuple[Any, tuple[float, float], tuple[float, float], str, float]


def _file_track(node: Any) -> _FileTrack | None:
    """The ``_FileTrack`` of a ``(segment ...)`` node, or ``None``."""
    start, end, layer, width = (node.get(name) for name in ("start", "end", "layer", "width"))
    if start is None or end is None or layer is None or width is None:
        return None
    return (
        node,
        (start.get_float(0) or 0.0, start.get_float(1) or 0.0),
        (end.get_float(0) or 0.0, end.get_float(1) or 0.0),
        layer.get_string(0) or "",
        width.get_float(0) or 0.0,
    )


def _holds(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
    origin: tuple[float, float] = (0.0, 0.0),
) -> bool:
    """True when the point is on the segment at KiCad's nanometre precision."""
    (x, y), (ax, ay), (bx, by) = (_file_nm(p, origin) for p in (point, start, end))
    return (
        (x - ax) * (by - ay) == (y - ay) * (bx - ax)
        and min(ax, bx) <= x <= max(ax, bx)
        and min(ay, by) <= y <= max(ay, by)
    )


def _split_track(doc: Any, route: LinkRoute, origin: tuple[float, float]) -> None:
    """Split the track carrying ``route.junction`` at the junction (Issue #10486).

    The track is the one with the endpoints the router saw, else whichever piece
    of it holds the junction: an earlier link in the same run may already have
    split it.  Each half is a copy of the node with one endpoint moved, so width,
    layer, the net reference in the board's own dialect and any lock flag
    survive verbatim; the half toward the original end takes a fresh uuid.  A
    junction on an endpoint that already exists leaves the node alone.
    """
    from kicad_tools.cli.stitch_cmd import _stitch_uuid
    from kicad_tools.sexp import SExp, parse_string

    junction = route.junction
    assert junction is not None  # the caller only splits a route that carries one
    ox, oy = origin
    at = junction.file_point(origin)
    tracks: list[_FileTrack] = []
    for node in doc.children:
        if node.name != "segment":
            continue
        track = _file_track(node)
        if (
            track is not None
            and track[3] == junction.layer
            and abs(track[4] - junction.width) <= _HOST_MATCH_TOL
            and _holds(at, track[1], track[2])
        ):
            tracks.append(track)
    seen = (
        (junction.start[0] + ox, junction.start[1] + oy),
        (junction.end[0] + ox, junction.end[1] + oy),
    )
    host = next(
        (
            t
            for t in tracks
            if all(
                math.dist(a, b) <= _HOST_MATCH_TOL for a, b in zip((t[1], t[2]), seen, strict=True)
            )
        ),
        None,
    )
    if host is None:
        host = next((t for t in tracks if _holds(t[1], *seen) and _holds(t[2], *seen)), None)
    if host is None:
        raise ValueError(
            f"no track on {junction.layer} holds the junction at {at[0]:g},{at[1]:g} to split"
        )
    node, start, end, _layer, _width = host
    if at in (start, end):
        return
    first, second = parse_string(node.to_string()), parse_string(node.to_string())

    def put(target: SExp, name: str, value: SExp) -> None:
        target.children[[child.name for child in target.children].index(name)] = value

    put(first, "end", SExp.list("end", *at))
    put(second, "start", SExp.list("start", *at))
    # The identity lives in ``uuid`` (KiCad 7+) or ``tstamp`` (older dialects).
    for identity in ("uuid", "tstamp"):
        if second.get(identity) is not None:
            put(
                second,
                identity,
                SExp.list(
                    identity,
                    _stitch_uuid(
                        "junction-seg",
                        junction.point[0],
                        junction.point[1],
                        end[0] - ox,
                        end[1] - oy,
                        junction.layer,
                        route.net_number,
                    ),
                ),
            )
    at_node = doc.children.index(node)
    doc.children[at_node : at_node + 1] = [first, second]


def _append_route_nodes(
    doc: Any, route: LinkRoute, origin: tuple[float, float], name_only: bool = False
) -> None:
    from kicad_tools.cli.stitch_cmd import _stitch_uuid
    from kicad_tools.sexp.builders import segment_node, via_node

    ox, oy = origin
    if route.junction is not None:
        _split_track(doc, route, origin)
    net_name = route.net_name if name_only and route.net_name else None
    for x_a, y_a, x_b, y_b, layer in route.segments:
        # Issue #8495: compare after the rounding ``segment_node`` applies, so a
        # sub-resolution sliver is not serialized as a zero-length segment.
        if _written_point(x_a, y_a, origin, route.coordinate_digits) == _written_point(
            x_b, y_b, origin, route.coordinate_digits
        ):
            continue
        doc.append(
            segment_node(
                start_x=x_a + ox,
                start_y=y_a + oy,
                end_x=x_b + ox,
                end_y=y_b + oy,
                width=route.width_for(layer),
                layer=layer,
                net=route.net_number,
                net_name=net_name,
                uuid_str=_stitch_uuid("oracle-seg", x_a, y_a, x_b, y_b, layer, route.net_number),
                coordinate_digits=route.coordinate_digits,
            )
        )
    for vx, vy in route.vias:
        doc.append(
            via_node(
                x=vx + ox,
                y=vy + oy,
                size=route.via_size,
                drill=route.via_drill,
                layers=("F.Cu", "B.Cu"),
                net=route.net_number,
                net_name=net_name,
                uuid_str=_stitch_uuid("oracle-via", vx, vy, route.net_number),
                coordinate_digits=route.coordinate_digits,
            )
        )
