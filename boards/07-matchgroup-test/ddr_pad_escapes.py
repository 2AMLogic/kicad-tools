"""Clear the pad shoulders on the four-layer fixture's single-ended DDR tracks."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import pairwise
from math import dist, inf, isclose
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry

from kicad_tools.core.sexp_file import load_pcb, save_pcb
from kicad_tools.schema.pcb import PCB, Segment
from kicad_tools.validate.mask_geometry import board_mask_margin, pad_mask_margin, pad_mask_shapes

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


def _contact_limits(line, width, neighbors):
    """Bound each distance and retain same-net contacts, including mid-track taps."""
    limits = []
    for geom, clearance in neighbors:
        old = line.distance(geom)
        upper = width / 2 if clearance is None and old <= width / 2 + 1e-6 else inf
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


def clear_ddr_pad_escapes(path: Path, nets: list[str]) -> int:
    """Move only unbranched, unlocked F.Cu runs between a DDR pad and its next landing."""
    doc = load_pcb(path)
    board = PCB(doc)
    ends: dict[tuple[str, XY], list[Segment]] = defaultdict(list)
    copper: dict[str, tuple[str, BaseGeometry]] = {}
    masks: dict[str, BaseGeometry] = {}
    nodes = {
        uid.get_string(): node for node in doc.find_all("segment") if (uid := node.find("uuid"))
    }
    for seg in board.segments:
        if seg.layer == "F.Cu":
            for point in (seg.start, seg.end):
                ends[(seg.net_name, _xy(*point))].append(seg)
            copper[seg.uuid] = (
                seg.net_name,
                LineString([seg.start, seg.end]).buffer(seg.width / 2, quad_segs=32),
            )
    landings = set()
    for arc in board.arcs:
        if arc.layer == "F.Cu":
            copper[arc.uuid] = (
                arc.net_name,
                LineString(arc.centerline_points()).buffer(arc.width / 2),
            )
            landings.update((_xy(*arc.start), _xy(*arc.end)))
    for via in board.vias:
        if "F.Cu" in via.layers or "*.Cu" in via.layers:
            landings.add(_xy(*via.position))
            copper[via.uuid] = via.net_name, Point(via.position).buffer(via.size / 2, quad_segs=32)
    targets = []
    base_margin, declared = board_mask_margin(board)
    if base_margin is None:
        return 0
    version_node = doc.find("version")
    version = version_node.get_int() if version_node else 0
    for fp in board.footprints:
        for pad in fp.pads:
            if not {"F.Cu", "*.Cu"}.intersection(pad.layers):
                continue
            margin, _ = pad_mask_margin(pad, fp, base_margin, declared, board_version=version or 0)
            if margin is None:
                return 0
            try:
                poly, mask = pad_mask_shapes(pad, fp, margin)
            except ValueError:
                # A pad we cannot bound may obstruct the proposed copper.
                return 0
            center = board.get_pad_position(fp.reference, pad.number)
            if center is None:
                continue
            landings.add(_xy(*center))
            key = f"{fp.reference}/{pad.number}"
            copper[key] = pad.net_name, poly
            if {"F.Mask", "*.Mask"}.intersection(pad.layers):
                masks[key] = mask
                if fp.reference in {"U1", "U2"} and pad.net_name in nets:
                    targets.append(
                        (nets.index(pad.net_name), key, pad.net_name, _xy(*center), mask)
                    )

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
