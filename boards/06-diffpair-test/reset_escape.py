"""Finish the nine-pair fixture's single-ended J4.RST escape (issue #9954)."""

from __future__ import annotations

from collections import defaultdict
from math import dist
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon

from kicad_tools.cli.relocate_in_pad_vias import relocate_pad_edge_vias
from kicad_tools.core.pcb_item_locks import BoardItemLocks
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.schema.pcb import PCB, Segment
from kicad_tools.validate.connectivity import ConnectivityValidator
from kicad_tools.validate.copper_contact import CopperContactGraph, build_copper_contact_graph
from kicad_tools.validate.mask_copper import default_request_for_manufacturer
from kicad_tools.validate.rules.clearance import _pad_polygon

_EPS = 1e-6
Point2D = tuple[float, float]


def _front_neck(
    tracks: list[Segment], via: Point2D, old: Point2D, pad_outline: Polygon
) -> tuple[list[Segment], Point2D] | None:
    """Walk the straight run from the new via back to its bend inside the pad."""
    neighbors = defaultdict(list)
    for seg in tracks:
        if seg.layer == "F.Cu":
            neighbors[seg.start].append((seg, seg.end))
            neighbors[seg.end].append((seg, seg.start))
    run = []
    point = via
    length = dist(old, via)
    dx, dy = (old[0] - via[0]) / length, (old[1] - via[1]) / length
    while True:
        next_legs = [(s, end) for s, end in neighbors[point] if not run or s is not run[-1]]
        if len(next_legs) != 1:
            return None
        seg, end = next_legs[0]
        ex, ey = end[0] - point[0], end[1] - point[1]
        if abs(ex * dy - ey * dx) > _EPS or ex * dx + ey * dy <= _EPS:
            break
        run.append(seg)
        point = end
    if run and pad_outline.contains(Point(point)):
        return run, point
    return None


def _inner_tail(
    tracks: list[Segment], layer: str, bend: Point2D, via: Point2D
) -> tuple[list[Segment], tuple[Segment, Point2D]] | None:
    """Trim only the connected collinear prefix ending at the relocated barrel.

    A previous ring-only move can leave a stub over several old inner tracks.
    The tail may overlap itself, but it must have one outgoing continuation.
    """
    length = dist(bend, via)
    ux, uy = (via[0] - bend[0]) / length, (via[1] - bend[1]) / length
    corridor = LineString([bend, via])
    ranges = []
    for seg in tracks:
        if seg.layer != layer:
            continue
        ends = sorted(
            ((x - bend[0]) * ux + (y - bend[1]) * uy, (x, y)) for x, y in (seg.start, seg.end)
        )
        (lo, _), (hi, far) = ends
        if lo > length + _EPS or hi < -_EPS:
            continue
        if any(abs((x - bend[0]) * uy - (y - bend[1]) * ux) > _EPS for _, (x, y) in ends):
            if corridor.distance(LineString([seg.start, seg.end])) < _EPS:
                return None
            continue
        if lo < -_EPS:
            return None
        ranges.append((lo, hi, seg, far))
    ranges.sort(key=lambda item: item[0])
    if not ranges:
        return None
    reached = ranges[0][0]
    for lo, hi, _, _ in ranges:
        if lo > reached + _EPS:
            return None
        reached = max(hi, reached)
    exits = [(seg, far) for _, hi, seg, far in ranges if hi > length + _EPS]
    if len(exits) != 1:
        return None
    tail = [seg for lo, _, seg, _ in ranges if lo < length - _EPS]
    return tail, exits[0]


def _external_contacts(graph: CopperContactGraph, segments: list[Segment], edited, via_index: int):
    """Direct contacts the edited copper must retain, including interior taps."""
    keys = [
        ("segment", id(segments[item.key[1]]))
        if item.kind == "track" and item.key[0] == "segment"
        else (item.kind, item.key)
        for item in graph.items
    ]
    local = {("via", via_index), *(("segment", id(seg)) for seg in edited)}
    contacts = set()
    for a, b in graph.bonds:
        first, second = keys[a], keys[b]
        if first in local and second not in local:
            contacts.add(second)
        elif second in local and first not in local:
            contacts.add(first)
    return contacts


def repair_reset_escape(path: Path, rules: DesignRules) -> int:
    """Slide the barrel, narrow its front neck and seat it on the inner trunk."""
    pcb = PCB.load(path)
    fp = pcb.get_footprint("J4")
    if fp is None:
        return 0
    pad = next((p for p in fp.pads if p.number == "RST" and p.net_name == "MIPI_RST"), None)
    request = default_request_for_manufacturer("jlcpcb")
    if pad is None or request is None or (outline := _pad_polygon(pad, fp)) is None:
        return 0
    validator = ConnectivityValidator(pcb)
    previous = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    original_segments = list(pcb.segments)
    move_result = relocate_pad_edge_vias(
        pcb, rules, pad_refs={("J4", "RST")}, ring_dam_mm=request.policy.warning_clearance_mm
    )
    if len(move_result.moved) != 1 or move_result.skipped or move_result.unresolvable:
        return 0
    move = move_result.moved[0]
    if (
        len(move.stub_layers) != 2
        or move.stub_layers[0] != "F.Cu"
        or not move.stub_layers[1].startswith("In")
    ):
        return 0
    tracks = list(pcb.segments_in_net(move.net))
    old, via = (move.old_x, move.old_y), (move.new_x, move.new_y)
    neck = _front_neck(tracks, via, old, outline)
    if neck is None:
        return 0
    front, bend = neck
    inner_layer = move.stub_layers[1]
    inner = _inner_tail(tracks, inner_layer, bend, via)
    if inner is None:
        return 0
    tail, (trunk, far) = inner
    removed = front + tail
    locks = BoardItemLocks(pcb._sexp)
    locked_ids = {
        field.get_string(0)
        for node in pcb._sexp.find_children("segment")
        if locks.is_locked(node) and (field := node.find("uuid") or node.find("tstamp")) is not None
    }
    if any(s.locked or s.uuid in locked_ids for s in removed):
        return 0
    # This single-ended net must stay one physical component, including its
    # pads, vias, arcs and retained tracks. Only publish this private copy
    # after validating all layers; a declined repair leaves the file intact.
    if len(previous.components()) != 1:
        return 0
    via_index = next(
        i for i, v in enumerate(pcb.vias) if v.net_number == move.net and v.position == via
    )
    contacts = _external_contacts(previous, original_segments, removed, via_index)
    if pcb.remove_segments(removed) != len(removed):
        return 0
    added = pcb.add_trace(bend, via, width=min(s.width for s in front), layer="F.Cu", net=move.net)
    if trunk in tail:
        added += pcb.add_trace(via, far, width=trunk.width, layer=inner_layer, net=move.net)
    after = build_copper_contact_graph(validator, move.net, with_bonds=True)
    if len(after.components()) != 1 or not contacts <= _external_contacts(
        after, pcb.segments, added, via_index
    ):
        return 0
    pcb.save(path)
    return 1
