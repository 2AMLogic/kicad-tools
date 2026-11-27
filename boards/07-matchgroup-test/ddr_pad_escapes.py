"""Board 07 fixture pad exits and length compensation for the DDR byte."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from itertools import pairwise
from math import dist, inf, isclose
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

from kicad_tools.core.pcb_item_locks import BoardItemLocks
from kicad_tools.core.sexp_file import load_pcb, save_pcb
from kicad_tools.router.rule_area_resolve import all_rule_area_zones
from kicad_tools.schema.pcb import PCB, Segment
from kicad_tools.sexp import SExp
from kicad_tools.validate.mask_geometry import (
    ADVANCED_PAD_FEATURES,
    board_mask_margin,
    pad_mask_margin,
    pad_mask_shapes,
)

XY = tuple[float, float]
# JLCPCB's 0.15 mm warning band plus the plotted-outline construction allowance.
MASK_CLEARANCE = 0.161
COPPER_CLEARANCE = 0.15


def _xy(x: float, y: float) -> XY:
    return round(x, 6), round(y, 6)


def _corner_options(points: list[XY]):
    """Slide a diagonal-to-horizontal shoulder in its local octilinear frame."""
    (a, h), (b, k) = points[1:3]
    if a <= 0 or a != h or k != h or b <= a:
        return
    # Put the diagonal after a straight pad exit. The total length is identical.
    for i in range(1, int(round((b - a) / 0.005)) + 1):
        x = round(i * 0.005, 6)
        yield [(0, 0), (x, 0), (x + a, h), *points[2:]]
    for i in range(1, 21):
        step = i * 0.025
        launch = (a + step, h + step)
        # Leave the mask on a longer diagonal and rejoin the original horizontal.
        if a + 2 * step <= b:
            yield [(0, 0), launch, (a + 2 * step, h), *points[2:]]
        if len(points) < 4:
            continue
        cx, cy = points[3]
        if cx >= b + step - 1e-6 and isclose(cx - b, cy - h, abs_tol=1e-6):
            # Parallel diagonal on the far side of the shoulder: slide the whole leg.
            yield [(0, 0), launch, (b + step, h + step), *points[3:]]
        if len(points) != 5:
            continue
        dx, dy = points[4]
        for offset in range(-20, 21):
            x = b + offset * 0.025
            if not launch[0] < x < dx:
                continue
            if cx == b and cy > h and isclose(dx - cx, dy - cy, abs_tol=1e-6):
                # The terminal vertical and diagonal can exchange order around a via.
                yield [(0, 0), launch, (x, launch[1]), (x, dy), (dx, dy)]
            elif dx == cx and dy < cy and isclose(cx - b, h - cy, abs_tol=1e-6):
                # Rejoin the original vertical on a 45-degree segment.
                y = launch[1] - (dx - x)
                if y > dy:
                    yield [(0, 0), launch, (x, launch[1]), (dx, y), (dx, dy)]


def _contact_limits(line, width, neighbors, *, keep_contact_depth=False):
    """Bound each distance and retain same-net contacts, including mid-track taps."""
    limits = []
    for geom, clearance in neighbors:
        old = line.distance(geom)
        upper = width / 2 if clearance is None and old <= width / 2 + 1e-6 else inf
        if keep_contact_depth and upper < inf:
            # A formerly overlapping joint must not be reduced to a tangent.
            upper = old
        lower = min(old, width / 2 + (0.001 if clearance is None else clearance))
        limits.append((geom, lower - 1e-6, upper + 1e-6))
    return limits


def choose_escape(
    points: list[XY],
    width: float,
    opening: BaseGeometry,
    neighbors: list[tuple[BaseGeometry, float | None]],
) -> list[XY] | None:
    """Keep both landings and choose the least length change that clears real copper."""
    if len(points) < 3 or LineString(points[1:]).distance(opening) - width / 2 >= MASK_CLEARANCE:
        return None
    ox, oy = points[0]
    sx = 1 if points[1][0] >= ox else -1
    sy = 1 if points[1][1] >= oy else -1
    local = [_xy((x - ox) * sx, (y - oy) * sy) for x, y in points]
    original = LineString(points)
    # An existing close neighbor (including the track at the far landing) may
    # keep its original gap. A candidate cannot bring that copper any closer.
    guards = _contact_limits(original, width, neighbors)
    options = []
    for candidate in _corner_options(local):
        path = []
        for x, y in candidate:
            xy = _xy(ox + sx * x, oy + sy * y)
            if not path or xy != path[-1]:
                path.append(xy)
        if len(path) < 3 or LineString(path[1:]).distance(opening) - width / 2 < MASK_CLEARANCE:
            continue
        line = LineString(path)
        if not line.is_simple:
            continue
        cost = (round(abs(line.length - original.length), 6), line.hausdorff_distance(original))
        options.append((cost, path, line))
    for _, path, line in sorted(options, key=lambda opt: (opt[0], len(opt[1]), opt[1])):
        if all(low <= line.distance(geom) <= high for geom, low, high in guards):
            return path
    return None


def _other_end(seg: Segment, point: XY) -> XY:
    return _xy(*(seg.end if _xy(*seg.start) == point else seg.start))


def _neighbors(points, ids, owner, net, copper, masks):
    region = LineString(points).envelope.buffer(1.5)
    result = [
        (g, COPPER_CLEARANCE if n != net else None)
        for uid, (n, g) in copper.items()
        if uid not in ids and uid != owner and region.intersects(g)
    ]
    result.extend(
        (g, MASK_CLEARANCE) for key, g in masks.items() if key != owner and region.intersects(g)
    )
    return result


def _compensate(net, delta, excluded, neck, board, ends, nodes, landings, copper, masks):
    """Move the crossbar of one untapped rectangular tuning bump on this net.

    Moving a crossbar by half the added length shortens its two legs equally.
    Both external endpoints and the copper of all the other nets stay fixed.
    """
    options = []
    for cross in board.segments:
        if cross.net_name != net or cross.layer != "F.Cu" or cross.locked:
            continue
        a, b = _xy(*cross.start), _xy(*cross.end)
        if a in landings or b in landings or len(ends[net, a]) != 2 or len(ends[net, b]) != 2:
            continue
        left = next(seg for seg in ends[net, a] if seg is not cross)
        right = next(seg for seg in ends[net, b] if seg is not cross)
        chain = [left, cross, right]
        ids = {seg.uuid for seg in chain}
        if len(ids) != 3 or ids & excluded or not ids <= nodes.keys():
            continue
        if any(seg.locked or seg.width != cross.width for seg in chain):
            continue
        p, q = _other_end(left, a), _other_end(right, b)
        vx, vy = _xy(a[0] - p[0], a[1] - p[1])
        leg = dist(p, a)
        if (vx, vy) != _xy(b[0] - q[0], b[1] - q[1]) or vx * vy != 0:
            continue
        if vx * (b[0] - a[0]) + vy * (b[1] - a[1]) != 0 or leg <= abs(delta) / 2 + cross.width:
            continue
        shift = delta / (2 * leg)
        proposed = [
            p,
            _xy(a[0] - shift * vx, a[1] - shift * vy),
            _xy(b[0] - shift * vx, b[1] - shift * vy),
            q,
        ]
        old, new = LineString([p, a, b, q]), LineString(proposed)
        near = _neighbors([p, a, b, q], ids, None, net, copper, masks)
        if new.distance(neck) <= cross.width / 2:
            continue
        limits = _contact_limits(old, cross.width, near)
        if not all(low <= new.distance(g) <= high for g, low, high in limits):
            continue
        clearance = min((new.distance(g) for g, gap in near if gap is not None), default=1.5)
        options.append((-clearance, cross.uuid, chain, proposed))
    if not options:
        return None
    _, _, chain, proposed = min(options, key=lambda x: x[:2])
    return chain, proposed


def _replace_chain(doc, origin, chain, revised, nodes, copper):
    original_nodes = [nodes[s.uuid] for s in chain]
    for uid in (seg.uuid for seg in chain):
        del copper[uid]
        del nodes[uid]
    for i, (start, end) in enumerate(pairwise(revised)):
        if i < len(original_nodes):
            node = original_nodes[i]
            uid = chain[i].uuid
        else:
            node = deepcopy(original_nodes[-1])
            uid = str(uuid5(NAMESPACE_URL, f"kct/board07/ddr/{chain[-1].uuid}/{i}"))
            node.find("uuid").set_atom(0, uid)
            doc.append(node)
        for tag, point in (("start", start), ("end", end)):
            coord = node.find(tag)
            for axis in (0, 1):
                coord.set_atom(axis, round(point[axis] + origin[axis], 6))
        copper[uid] = (
            chain[0].net_name,
            LineString([start, end]).buffer(chain[0].width / 2, quad_segs=32),
        )
    for node in original_nodes[len(revised) - 1 :]:
        doc.remove(node)


@dataclass
class _EscapeLayer:
    """Copper, mask and fixed landings on one outer layer of the fixture."""

    ends: dict[tuple[str, XY], list[Segment]]
    copper: dict[str, tuple[str, BaseGeometry]]
    masks: dict[str, BaseGeometry]
    nodes: dict[str, SExp]
    landings: set[XY]
    pad_centers: dict[str, XY]
    track_keepouts: list[BaseGeometry]


def _index_escape_layer(doc, board: PCB, layer: str) -> _EscapeLayer | None:
    ends: dict[tuple[str, XY], list[Segment]] = defaultdict(list)
    copper: dict[str, tuple[str, BaseGeometry]] = {}
    masks: dict[str, BaseGeometry] = {}
    layer_pads = {}
    locks = BoardItemLocks(doc)
    nodes = {
        uid.get_string(): node
        for node in doc.find_all("segment")
        if (uid := node.find("uuid")) and not locks.is_locked(node)
    }
    for seg in board.segments:
        if seg.layer == layer:
            for point in (seg.start, seg.end):
                ends[(seg.net_name, _xy(*point))].append(seg)
            copper[seg.uuid] = (
                seg.net_name,
                LineString([seg.start, seg.end]).buffer(seg.width / 2, quad_segs=32),
            )
    landings = set()
    for arc in board.arcs:
        if arc.layer == layer:
            copper[arc.uuid] = (
                arc.net_name,
                LineString(arc.centerline_points()).buffer(arc.width / 2),
            )
            landings.update((_xy(*arc.start), _xy(*arc.end)))
    for via in board.vias:
        if layer in via.layers or "*.Cu" in via.layers:
            landings.add(_xy(*via.position))
            copper[via.uuid] = via.net_name, Point(via.position).buffer(via.size / 2, quad_segs=32)
    base_margin, declared = board_mask_margin(board)
    if base_margin is None:
        return None
    version_node = doc.find("version")
    version = version_node.get_int() if version_node else 0
    for fp in board.footprints:
        for pad in fp.pads:
            if not {layer, "*.Cu"}.intersection(pad.layers):
                continue
            raw = pad._sexp_node
            if pad.drill_offset != (0.0, 0.0) or (
                raw is not None
                and any(raw.find_child(tag) is not None for tag in ADVANCED_PAD_FEATURES)
            ):
                return None
            margin, _ = pad_mask_margin(pad, fp, base_margin, declared, board_version=version or 0)
            if margin is None:
                return None
            try:
                poly, mask = pad_mask_shapes(pad, fp, margin)
            except ValueError:
                # A pad we cannot bound may obstruct the proposed copper.
                return None
            center = board.get_pad_position(fp.reference, pad.number)
            if center is None:
                continue
            landings.add(_xy(*center))
            key = f"{fp.reference}/{pad.number}"
            if key in copper:
                # Two lands with the same reference and number cannot own one escape.
                return None
            copper[key] = pad.net_name, poly
            if {layer.replace(".Cu", ".Mask"), "*.Mask"}.intersection(pad.layers):
                masks[key] = mask
                layer_pads[key] = _xy(*center)
    track_keepouts = []
    for area in all_rule_area_zones(board):
        if area.keepout.tracks_allowed or not {layer, "*.Cu", "F&B.Cu"}.intersection(
            area.layers or [area.layer]
        ):
            continue
        rings = area.rule_area_contours or [area.polygon]
        if not rings[0] or len(rings[0]) < 3:
            return None
        polygon = Polygon(rings[0], holes=rings[1:])
        if not polygon.is_valid:
            return None
        track_keepouts.append(polygon)
    return _EscapeLayer(ends, copper, masks, nodes, landings, layer_pads, track_keepouts)


def clear_ddr_pad_escapes(path: Path, nets: list[str]) -> int:
    """Move only unbranched, unlocked F.Cu runs between a DDR pad and its next landing."""
    doc = load_pcb(path)
    board = PCB(doc)
    surface = _index_escape_layer(doc, board, "F.Cu")
    if surface is None:
        return 0
    ends, copper, masks = surface.ends, surface.copper, surface.masks
    nodes, landings = surface.nodes, surface.landings
    targets = []
    for key, center in surface.pad_centers.items():
        net = copper[key][0]
        if key.split("/")[0] in {"U1", "U2"} and net in nets:
            targets.append((nets.index(net), key, net, center, masks[key]))

    edits = 0
    for _, key, net, center, mask in sorted(targets):
        points = [center]
        chain: list[Segment] = []
        for _ in range(4):
            choices = [s for s in ends[net, points[-1]] if s not in chain]
            if len(choices) != 1:
                break
            seg = choices[0]
            if seg.locked or seg.uuid not in nodes or (chain and seg.width != chain[0].width):
                break
            chain.append(seg)
            points.append(_other_end(seg, points[-1]))
            if points[-1] in landings:
                break
        if len(chain) < 2:
            continue
        ids = {s.uuid for s in chain}
        neighbors = _neighbors(points, ids, key, net, copper, masks)
        revised = choose_escape(points, chain[0].width, mask, neighbors)
        if revised is None:
            continue
        changes = [(chain, revised)]
        delta = LineString(revised).length - LineString(points).length
        if abs(delta) > 1e-6:
            neck = LineString(revised).buffer(chain[0].width / 2)
            compensation = _compensate(
                net, delta, ids, neck, board, ends, nodes, landings, copper, masks
            )
            if compensation is None:
                continue
            changes.append(compensation)
        for changed_chain, changed_points in changes:
            _replace_chain(doc, board.board_origin, changed_chain, changed_points, nodes, copper)
        edits += 1
    if edits:
        save_pcb(doc, path)
    return edits


def _escapes():
    """Reviewed bends in mm from each pad center, before the first recipe fill."""
    ground = [
        (-0.10, -0.30),
        (-0.10, -0.40),
        (-0.15, -0.45),
        (-0.15, -0.70),
        (-0.20, -0.75),
        (-0.20, -0.80),
    ]
    for pad in ("U2/13", "U3/7", "U5/13"):
        yield pad, "GND", "F.Cu", ground, [ground[0], (-0.10, -0.70), ground[-1]]
    yield (
        "U4/B5",
        "GND",
        "F.Cu",
        [(0.18, -0.18), (0.33, -0.33), (0.33, -0.38), (0.43, -0.48)],
        [(0.18, -0.18), (0.43, -0.43), (0.43, -0.48)],
    )

    # Move the header's diagonal beyond its through-hole land, keeping its length.
    for pin, net, layer, neck in (
        (2, "A0", "F.Cu", 0.82),
        (4, "A2", "F.Cu", 0.94),
        (5, "A3", "B.Cu", 0.80),
        (7, "A5", "B.Cu", 0.92),
    ):
        end = (neck + 5.60, -1.20)
        yield (
            f"J3/{pin}",
            net,
            layer,
            [(0, 0), (neck, 0), (neck + 1.20, -1.20), end],
            [(0, 0), (1.05, 0), (2.25, -1.20), end],
        )
    # Replace the nearest two tuning bumps with one that clears the A1 track.
    yield (
        "J3/9",
        "A7",
        "B.Cu",
        [
            (0, 0),
            (0.4, 0),
            (0.4, 0.4927),
            (0.8, 0.4927),
            (0.8, 0),
            (1.2, 0),
            (1.2, 0.4927),
            (1.6, 0.4927),
            (1.6, 0),
        ],
        [(0, 0), (1.2, 0), (1.2, 0.74), (1.6, 0.74), (1.6, 0)],
    )
    yield (
        "U5/1",
        "A0",
        "F.Cu",
        [(0, 0), (0, -0.2), (-0.2, -0.4), (-0.2, -0.8)],
        [(0, 0), (0, -0.6), (-0.2, -0.8)],
    )
    # The short straight on CLK_P leaves its partner enough coupled length.
    yield (
        "U3/1",
        "MIPI_CLK_P",
        "F.Cu",
        [(0, 0), (0, -0.1657), (-0.495, -0.6607)],
        [(0, 0), (0, -0.09), (-0.495, -0.585), (-0.495, -0.6607)],
    )
    for pin, net, y in (("E1", "TMDS_D2_P", 0.235), ("F1", "TMDS_D2_N", -0.235)):
        yield (
            f"U4/{pin}",
            net,
            "F.Cu",
            [(0, 0), (-0.235, y), (-0.7, y)],
            [(0, 0), (-0.465, 0), (-0.7, y)],
        )


def _pad_relative(center: XY, offsets: list[XY]) -> list[XY]:
    return [(round(center[0] + x, 6), round(center[1] + y, 6)) for x, y in offsets]


def _match_escape(surface: _EscapeLayer, net: str, points: list[XY]):
    """Match the entire unlocked run; an intermediate landing or branch owns the bend."""
    for point in points[1:-1]:
        if point in surface.landings or len(surface.ends[net, point]) != 2:
            return []
    chain = []
    for start, end in pairwise(points):
        matches = [
            seg
            for seg in surface.ends[net, start]
            if any((round(x, 6), round(y, 6)) == end for x, y in (seg.start, seg.end))
        ]
        if len(matches) != 1:
            return []
        seg = matches[0]
        if seg.uuid not in surface.nodes:
            return []
        if chain and seg.width != chain[0].width:
            return []
        chain.append(seg)
    return chain


def clear_fixture_pad_escapes(path: Path) -> int:
    """Splice the measured chains only where the original landings and clearance survive."""
    doc = load_pcb(path)
    board = PCB(doc)
    layers = {name: _index_escape_layer(doc, board, name) for name in ("F.Cu", "B.Cu")}
    changes = 0
    for key, net, layer, old, new in _escapes():
        surface = layers[layer]
        if not surface or key not in surface.pad_centers or surface.copper[key][0] != net:
            continue
        center = surface.pad_centers[key]
        original, revised = _pad_relative(center, old), _pad_relative(center, new)
        chain = _match_escape(surface, net, original)
        if not chain:
            continue
        ids = {seg.uuid for seg in chain}
        nearby = _neighbors(original, ids, key, net, surface.copper, surface.masks)
        nearby.append((surface.copper[key][1], None))
        limits = _contact_limits(
            LineString(original), chain[0].width, nearby, keep_contact_depth=True
        )
        line = LineString(revised)
        if any(line.distance(area) <= chain[0].width / 2 for area in surface.track_keepouts):
            continue
        if not all(low <= line.distance(geom) <= high for geom, low, high in limits):
            continue
        _replace_chain(doc, board.board_origin, chain, revised, surface.nodes, surface.copper)
        changes += 1
    if changes:
        save_pcb(doc, path)
    return changes
