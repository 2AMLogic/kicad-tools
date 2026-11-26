"""Clear the strict mask-view pad departures on the nine-pair fixture (issue #9989).

Seed-42 leaves five single-ended sideband escapes whose copper sits beside the
pad's own solder-mask opening beyond the one straight entry the checker allows:
the router's width tapers (U4.5, U1.12, U1.13) and two trunk-width departures
wider than their pads (J1.A5, J1.B5).  Each is rewritten as one straight,
centered neck that re-enters the original trunk at the first landing, with the
neck held under the impedance rule's neck-down exemption so no 73.6-ohm finding
appears (the 1.00/1.10 mm negative control in the issue).  U1.12 additionally
slides its escape via out of the ring dam, keeping the inner connection with
the relocation stub -- the same shape as the J4.RST repair in ``reset_escape``.

J1's mirrored USB2 data pads B6 and B7 have a different escape. Each has a
front branch and one inner-layer connection to its barrel. Move both barrels
to the measured 45-degree exits together so their rings clear the mask (#9891).
U3's PCIe receiver pair instead leaves both lands on their long axis. Its two
barrels retain the project diameter and reconnect to the original inner trunks.
Both moves must preserve the receiver's raw trace-length difference and keep
its modeled propagation delay inside the board's PCIe skew budget. Both ends
of the transmitter pair and the J3 receiver exits use trunk-width copper on
their original centerlines, retaining each rail's length and contacts.
The MIPI clock pair's four exits (J4/U4, #10083) are staged together: trunk-width
copper, both negative-rail barrels moved clear of the mask, published only if the
staged copper clears the project gaps and the pair stays inside its skew budget.
The MIPI data pair's three offending exits (J4.4, U4.3, U4.4, #10113) are staged
the same way: each rebuilt as one straight axis piece whose corner sits inside the
pad copper plus one 45-degree rejoin, with the trunk extended along its own line
so both rails shorten equally and the pair keeps its measured skew.
U1's USB2 pair uses two different straight exits to clear its mask openings
while meeting the fixture's coupling, delay and impedance limits (#10235).

Every repair is guarded: the departure is derived from the actual pad and its
connected copper, and anything locked, branched off the recognized shape, or
not reconnectable as one physical component is left untouched, file intact.
"""

from __future__ import annotations

import re
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass, replace
from itertools import pairwise
from math import dist, isfinite
from pathlib import Path
from tempfile import TemporaryDirectory

from shapely import make_valid
from shapely.geometry import LineString, Point, Polygon

from kicad_tools.cli.kicad_toolchain import (
    KiCadEnvironmentError,
    active_evidence_toolchain,
    evidence_toolchain,
)
from kicad_tools.cli.relocate_in_pad_vias import relocate_pad_edge_vias
from kicad_tools.cli.runner import run_drc
from kicad_tools.core.pcb_item_locks import BoardItemLocks
from kicad_tools.drc.report import load_native_connectivity_report
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.router.board_clearance_rules import (
    BoardClearanceRules,
    _layer_matches,
    _parse_length_mm,
    evaluate_condition,
    item_props_for_type,
    pad_clearance_override,
)
from kicad_tools.router.quantize import dogleg_points
from kicad_tools.router.rule_area_resolve import all_rule_area_zones
from kicad_tools.router.rules import net_class_map_from_path
from kicad_tools.schema.pcb import PCB, Pad, Segment, Via
from kicad_tools.sidecars import copy_project_sidecars
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.clearance_shapes import (
    inside_filled_outline_region,
    min_gap_to_outline,
    outline_shapes,
    segment_shape,
)
from kicad_tools.validate.connectivity import ConnectivityValidator
from kicad_tools.validate.copper_contact import (
    CopperContactGraph,
    build_copper_contact_graph,
    collect_copper_items,
)
from kicad_tools.validate.diffpair_skew import derive_pair_skews
from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    check_mask_to_copper,
    default_request_for_manufacturer,
)
from kicad_tools.validate.mask_geometry import board_mask_margin, pad_mask_margin, pad_mask_shapes
from kicad_tools.validate.rules.diffpair_length_skew import measured_skew_verdict
from kicad_tools.validate.rules.impedance import ImpedanceRule
from kicad_tools.validate.rules.physical_gap import StaircaseSlitOracle, fab_gap_policy_from_entry

XY = tuple[float, float]

# The measured seed-42 repair neck: centered, uniform, and narrow enough that
# its one straight entry stays inside the checker's crossing budget at every
# exit angle it can take (45 degrees or steeper).
NECK_WIDTH_MM = 0.18
# The JLCPCB mask warning band plus the plotted-outline construction slack --
# the same margin board 07's DDR escape pass holds (``ddr_pad_escapes``).
BAND_MM = 0.161
# A longer neck stays measured by the impedance rule (the issue's negative
# control at 1.00/1.10 mm read 73.6 ohm against the 50 ohm sideband target).
NECK_MAX_MM = ImpedanceRule.NECK_DOWN_EXEMPT_LENGTH_MM - 0.005
# A kept trunk segment: escape-zone pieces never reach this length.
KEEPER_MIN_MM = 0.5
# Candidate neck lengths step on this grid (mm).
GRID_MM = 0.025

_EPS = 1e-6

# The five seed-42 pad departures (issue #9989): (reference, pad, moves its via)
_TARGETS: tuple[tuple[str, str, bool], ...] = (
    ("J1", "A5", False),
    ("J1", "B5", False),
    ("U1", "12", True),
    ("U1", "13", False),
    ("U4", "5", False),
)


def _xy(point) -> XY:
    return round(float(point[0]), 6), round(float(point[1]), 6)


def _locked_ids(pcb: PCB) -> set[str]:
    locks = BoardItemLocks(pcb._sexp)
    fields = (
        node.find("uuid") or node.find("tstamp")
        for node in pcb._sexp.find_children("segment")
        if locks.is_locked(node)
    )
    return {field.get_string(0) for field in fields if field is not None}


def _external_contacts(
    graph: CopperContactGraph, segments: list[Segment], edited, via_index: int | None
):
    """Direct contacts the edited copper must retain, including interior taps."""
    keys = [
        ("segment", id(segments[item.key[1]]))
        if item.kind == "track" and item.key[0] == "segment"
        else (item.kind, item.key)
        for item in graph.items
    ]
    local = {("segment", id(seg)) for seg in edited}
    if via_index is not None:
        local.add(("via", via_index))
    contacts = set()
    attached_ends = set()
    for a, b in graph.bonds:
        first, second = keys[a], keys[b]
        if first in local and second not in local:
            contacts.add(second)
        elif second in local and first not in local:
            contacts.add(first)
        # An interior crossing can survive while an end of the very same
        # branch comes loose. Track each retained end that touches bonded
        # copper, including ends joined to adjacent pieces of an inner taper.
        for track, neighbor in ((a, b), (b, a)):
            item = graph.items[track]
            if item.kind != "track" or keys[track] in local:
                continue
            for end in item.endpoints:
                if Point(end).distance(graph.items[neighbor].geom) <= item.half_width:
                    attached_ends.add((keys[track], end))
    return contacts, attached_ends


def _keeps_via_approach(prior, current, retained: list[Segment], stub_layers: set[str]) -> bool:
    """A stub can contact a continuous taper at fewer pieces than the old barrel.

    Follow only former contacts from a piece still bonded to the repair.
    Every skipped piece must join unchanged copper exactly at both ends,
    with no fork. This cannot excuse a free end on a returning branch.
    """
    missing = prior - current
    tracks = {("segment", id(s)): s for s in retained if s.layer in stub_layers}
    if not missing <= tracks.keys():
        return False
    joints = defaultdict(set)
    ends = {}
    for key, segment in tracks.items():
        ends[key] = [(segment.layer, _xy(point)) for point in (segment.start, segment.end)]
        for end in ends[key]:
            joints[end].add(key)
    connected = list(prior & current & tracks.keys())
    while connected:
        for end in ends[connected.pop()]:
            for key in joints[end] & missing:
                if all(len(joints[point]) == 2 for point in ends[key]):
                    missing.remove(key)
                    connected.append(key)
    return not missing


def _walk_chain(
    pcb: PCB, net: str, center: XY, locked: set[str]
) -> tuple[list[Segment], XY] | None:
    """Unbranched, unlocked F.Cu run from the pad to its first landing.

    The walk ends at a landing (a via seat, another pad, a mid-track tap) or at
    the near endpoint of a kept trunk segment; that far point is the junction
    the rebuilt escape must still reach.  Returns ``None`` for anything the
    pass does not recognize.
    """
    f_cu = [s for s in pcb.segments if s.layer == "F.Cu" and s.net_name == net]
    at: dict[XY, list[Segment]] = {}
    for seg in f_cu:
        at.setdefault(_xy(seg.start), []).append(seg)
        at.setdefault(_xy(seg.end), []).append(seg)
    landings = {_xy(v.position) for v in pcb.vias if v.net_name == net}
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name == net:
                pos = pcb.get_pad_position(fp.reference, pad.number)
                if pos is not None:
                    landings.add(_xy(pos))
    start = _xy(center)
    chain: list[Segment] = []
    point = start
    junction: XY | None = None
    for _ in range(64):
        if point != start and point in landings:
            junction = point
            break
        choices = [s for s in at.get(point, []) if s not in chain]
        if len(choices) != 1:
            # A tap (two continuations) or a dead end ends the escape zone
            # here; only a point something else lands on is rebuildable.
            junction = point if point != start and len(at.get(point, [])) >= 3 else None
            break
        seg = choices[0]
        if seg.locked or seg.uuid in locked:
            return None
        if len(chain) >= 1 and dist(seg.start, seg.end) >= KEEPER_MIN_MM:
            # A long segment after the escape's first piece is the trunk the
            # repair must preserve; stop without consuming it.
            junction = point
            break
        chain.append(seg)
        point = _xy(seg.end) if _xy(seg.start) == point else _xy(seg.start)
    if not chain or junction is None:
        return None
    return chain, junction


def _on_line(a: XY, b: XY, p: XY, eps: float = 1e-6) -> bool:
    length = dist(a, b)
    if length < eps:
        return False
    cross = (p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])
    return abs(cross) / length <= eps


def _neck_candidate(
    chain: list[Segment], center: XY, junction: XY, pad_copper, opening, pcb: PCB, net: str
) -> list[tuple[XY, XY, float]] | None:
    """``[(neck start/end/width), (wide piece ...)]`` legs, or ``None``.

    The neck runs along the chain's final straight run -- the line the escape
    already travels on -- starting at the last vertex of that run inside the
    pad.  The wide piece rejoins the original junction at the trunk width.
    The neck length is the smallest grid step whose wide copper clears the
    pad's mask opening by the warning band, held under the impedance
    exemption's length so it stays unmeasured.
    """
    points = [_xy(center)]
    for seg in chain:
        points.append(_xy(seg.end) if _xy(seg.start) == points[-1] else _xy(seg.start))
    if points[-1] != _xy(junction):
        return None
    axis_end = points[-1]
    axis_start = points[-2]
    span_axis = dist(axis_start, axis_end)
    if span_axis <= _EPS:
        return None
    ux, uy = (axis_end[0] - axis_start[0]) / span_axis, (axis_end[1] - axis_start[1]) / span_axis
    neck_start = axis_start
    for point in reversed(points[:-1]):
        if not _on_line(axis_start, axis_end, point):
            break
        neck_start = point
    if not pad_copper.buffer(-0.01).contains(Point(neck_start)):
        return None
    span = dist(neck_start, junction)
    if span <= _EPS or span > 2.0:
        return None
    wide = max(s.width for s in chain)
    for seg in pcb.segments:
        if seg.layer == "F.Cu" and seg.net_name == net and seg not in chain:
            if _xy(seg.start) == _xy(junction) or _xy(seg.end) == _xy(junction):
                wide = max(wide, seg.width)
    steps = int(span / GRID_MM) + 1
    for i in range(1, steps + 1):
        neck_len = min(i * GRID_MM, span)
        if neck_len <= _EPS:
            return None
        neck_end = (
            round(neck_start[0] + ux * neck_len, 6),
            round(neck_start[1] + uy * neck_len, 6),
        )
        legs = [(neck_start, neck_end, NECK_WIDTH_MM)]
        if dist(neck_end, junction) > _EPS:
            legs.append((neck_end, _xy(junction), wide))
        wide_copper = LineString([neck_end, junction]).buffer(wide / 2, quad_segs=32)
        if wide_copper.distance(opening) < BAND_MM:
            continue
        if neck_len >= NECK_MAX_MM:
            return None
        return legs
    return None


def _departure_mask_shapes(pcb: PCB, fp, pad):
    """Resolve the pad copper and opening under this board's mask settings."""
    base_margin, declared = board_mask_margin(pcb)
    if base_margin is None:
        return None
    version_node = pcb._sexp.find("version")
    version = version_node.get_int() if version_node else 0
    margin, _ = pad_mask_margin(pad, fp, base_margin, declared, board_version=version or 0)
    if margin is None or margin < 0:
        return None
    try:
        return pad_mask_shapes(pad, fp, margin)
    except ValueError:
        return None


def _repair_pad(
    path: Path,
    rules: DesignRules,
    ring_dam_mm: float,
    error_clearance: float,
    ref: str,
    number: str,
    move_via: bool,
) -> int:
    pcb = PCB.load(path)
    fp = pcb.get_footprint(ref)
    pad = next((p for p in fp.pads if p.number == number), None) if fp else None
    if pad is None or not pad.net_name:
        return 0
    shapes = _departure_mask_shapes(pcb, fp, pad)
    if shapes is None:
        return 0
    pad_copper, opening = shapes
    center = pcb.get_pad_position(ref, number)
    if center is None:
        return 0
    net = pad.net_name
    validator = ConnectivityValidator(pcb)
    previous = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(previous.components()) != 1:
        return 0
    original_segments = list(pcb.segments)
    via_index: int | None = None
    via_note = ""
    if move_via:
        # The slide's hole-to-pour check reads the SAVED fill polygons, which
        # were carved for the pre-repair seats: near U1.12 that pocket sits a
        # fill generation behind the copper and boxes the whole corridor in
        # (issue #9989). The refill inside this same transaction regenerates
        # the pours for the new seat, so grade the slide against copper, not
        # against stale fills: strip them in memory and restore before save.
        saved_fills = [
            (zone, list(zone.filled_polygons), list(zone.filled_polygon_layers))
            for zone in pcb.zones
        ]
        for zone in pcb.zones:
            zone.filled_polygons = []
            zone.filled_polygon_layers = []
        try:
            result = relocate_pad_edge_vias(
                pcb, rules, pad_refs={(ref, number)}, ring_dam_mm=ring_dam_mm
            )
        finally:
            for zone, polygons, layers in saved_fills:
                zone.filled_polygons = polygons
                zone.filled_polygon_layers = layers
        if result.unresolvable or result.skipped:
            reasons = "; ".join(
                f"{item.reason} at ({item.x:.3f}, {item.y:.3f})"
                for item in [*result.skipped, *result.unresolvable]
            )
            print(f"   pad departure {ref}/{number}: via slide declined ({reasons})")
        if len(result.moved) != 1 or result.skipped or result.unresolvable:
            return 0
        move = result.moved[0]
        seat = _xy((move.new_x, move.new_y))
        via_index = next(
            (i for i, v in enumerate(pcb.vias) if v.net_name == net and _xy(v.position) == seat),
            None,
        )
        if via_index is None:
            return 0
        via_note = f", via slid to ({move.new_x:.3f}, {move.new_y:.3f})"
    walk = _walk_chain(pcb, net, _xy(center), _locked_ids(pcb))
    if walk is None:
        return 0
    chain, junction = walk
    legs = _neck_candidate(chain, _xy(center), junction, pad_copper, opening, pcb, net)
    if legs is None:
        return 0
    existing = sorted((tuple(sorted((_xy(s.start), _xy(s.end)))), round(s.width, 6)) for s in chain)
    rebuilt = sorted((tuple(sorted((_xy(a), _xy(b)))), round(w, 6)) for a, b, w in legs)
    if existing == rebuilt:
        return 0
    # The one straight entry must stay inside the checker's crossing budget.
    neck = legs[0]
    neck_copper = LineString([neck[0], neck[1]]).buffer(NECK_WIDTH_MM / 2, quad_segs=32)
    band = (
        neck_copper.difference(opening.buffer(0.011))
        .intersection(opening.buffer(error_clearance + 0.011))
        .area
    )
    if band > 1.45 * NECK_WIDTH_MM * error_clearance + _EPS:
        return 0
    removed = list(chain)
    contacts, attached_ends = _external_contacts(previous, original_segments, removed, via_index)
    if pcb.remove_segments(removed) != len(removed):
        return 0
    for a, b, width in legs:
        pcb.add_trace(a, b, width=width, layer="F.Cu", net=net)
    after = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(after.components()) != 1:
        return 0
    # Include every surviving relocation stub as well as the new neck. An
    # inner approach can meet the stub at the old seat, while a returning
    # branch must still touch this repaired copper at its original end.
    original_ids = {id(s) for s in original_segments}
    added = [s for s in pcb.segments if id(s) not in original_ids]
    current, current_ends = _external_contacts(after, pcb.segments, added, via_index)
    if not attached_ends <= current_ends:
        return 0
    if not contacts <= current:
        retained = [s for s in pcb.segments if id(s) in original_ids and s.net_name == net]
        stub_layers = {s.layer for s in added if s.layer != "F.Cu"}
        if via_index is None or not _keeps_via_approach(contacts, current, retained, stub_layers):
            return 0
    pcb.save(path)
    print(
        f"   pad departure {ref}/{number}: rebuilt as a straight {NECK_WIDTH_MM} mm neck{via_note}"
    )
    return 1


@dataclass(frozen=True)
class _PairEscape:
    pad: str
    net: str
    layer: str
    old_offset: XY
    new_offset: XY
    # On U3, the inner taper continues to this measured trunk endpoint.
    # On J1, the barrel has one constant-width approach segment.
    inner_end_offset: XY | None = None
    # The BGA repair requires the measured full-width front run.
    front_corners: tuple[XY, ...] = ()


def _inner_approach(
    pcb: PCB, escape: _PairEscape, center: XY, old: XY, front: list[Segment], locked: set[str]
) -> tuple[list[Segment], XY] | None:
    end = escape.inner_end_offset
    joint = _xy((center[0] + end[0], center[1] + end[1])) if end is not None else None
    segments = [s for s in pcb.segments if s.net_name == escape.net and s not in front]
    used: list[Segment] = []
    point = old
    while True:
        choices = [s for s in segments if s not in used and point in (_xy(s.start), _xy(s.end))]
        if len(choices) != 1:
            return None
        segment = choices[0]
        if segment.layer != escape.layer or segment.locked or segment.uuid in locked:
            return None
        following = _xy(segment.end) if _xy(segment.start) == point else _xy(segment.start)
        used.append(segment)
        if joint is None or following == joint:
            return used, following
        if not _on_line(old, joint, following) or dist(following, joint) >= dist(point, joint):
            return None
        point = following


def _rebuild_pair_branch(
    pcb: PCB,
    ref: str,
    escape: _PairEscape,
    rules: DesignRules,
    width: float,
    diameter: float,
    protected: set[str] | None = None,
) -> tuple[list[Segment], Via] | None:
    """Reconnect one data pad to its original trunk, pending its partner's checks."""
    center = pcb.get_pad_position(ref, escape.pad)
    if center is None:
        return None
    locked = _locked_ids(pcb) if protected is None else protected
    net = escape.net
    walk = _walk_chain(pcb, net, _xy(center), locked)
    if walk is None:
        return None
    tracks, old = walk
    if _xy((old[0] - center[0], old[1] - center[1])) != escape.old_offset:
        return None
    if escape.front_corners:
        points = [_xy(center)]
        for segment in tracks:
            points.append(
                _xy(segment.end) if _xy(segment.start) == points[-1] else _xy(segment.start)
            )
        if not _matches_measured(points, center, escape.front_corners):
            return None
    vias = [(i, v) for i, v in enumerate(pcb.vias) if v.net_name == net and _xy(v.position) == old]
    if len(vias) != 1:
        return None
    via_index, via = vias[0]
    node = pcb.via_source_node(via)
    if node is None or BoardItemLocks(pcb._sexp).is_locked(node) or via.uuid.lower() in locked:
        return None
    size = node.find("size")
    if size is None:
        return None
    if (via.size, via.drill) != (0.6, 0.25) or diameter + _EPS < max(
        rules.min_via_diameter_mm, via.drill + 2 * rules.min_annular_ring_mm
    ):
        return None
    approach = _inner_approach(pcb, escape, _xy(center), old, tracks, locked)
    if approach is None:
        return None
    inner, joint = approach
    edited = [*tracks, *inner]
    if escape.front_corners and (via.via_type or set(via.layers) != {"F.Cu", "B.Cu"}):
        return None
    if (escape.inner_end_offset is None or escape.front_corners) and any(
        abs(s.width - width) > _EPS for s in edited
    ):
        return None
    if any(s.width > width + _EPS for s in edited) or abs(inner[-1].width - width) > _EPS:
        return None

    original = list(pcb.segments)
    validator = ConnectivityValidator(pcb)
    before = build_copper_contact_graph(validator, via.net_number, with_bonds=True)
    if len(before.components()) != 1:
        return None
    contacts, ends = _external_contacts(before, original, edited, via_index)
    target = _xy((center[0] + escape.new_offset[0], center[1] + escape.new_offset[1]))
    if pcb.remove_segments(edited) != len(edited) or not pcb.relocate_via(via, target):
        return None
    size.set_value(0, diameter)
    via.size = diameter
    added = pcb.add_trace(center, target, width=width, layer="F.Cu", net=net)
    # On U3 the measured clear corridor leaves the barrel diagonally.
    points = dogleg_points(*joint, *target, axis_first=escape.inner_end_offset is not None)
    for a, b in pairwise(points):
        if a != b:
            added.extend(pcb.add_trace(a, b, width=width, layer=escape.layer, net=net))
    after = build_copper_contact_graph(validator, via.net_number, with_bonds=True)
    current, current_ends = _external_contacts(after, pcb.segments, added, via_index)
    if len(after.components()) != 1 or not contacts <= current or not ends <= current_ends:
        return None
    if escape.front_corners and contacts != current:
        return None
    return added, via


def _raw_trace_skew(pcb: PCB, escapes: tuple[_PairEscape, _PairEscape]) -> float:
    # Only straight traces change. Retained arcs and via barrels cancel when
    # comparing the signed difference before and after this candidate.
    positive, negative = (
        sum(dist(s.start, s.end) for s in pcb.segments if s.net_name == escape.net)
        for escape in escapes
    )
    return positive - negative


def _within_project_skew_budget(pcb: PCB, path: Path, nets: tuple[str, str]) -> bool:
    # Board 06 writes this map beside the routed board and passes it to kct check.
    try:
        classes = net_class_map_from_path(path.with_name("net_class_map.json"), pcb_path=path)
    except (OSError, TypeError, ValueError):
        return False
    for pair in derive_pair_skews(pcb, classes, num_copper_layers=len(pcb.copper_layers)):
        if (pair.p_name, pair.n_name) == nets:
            return (
                pair.skew_ps is not None
                and pair.limits.budget is not None
                and measured_skew_verdict(pair) == "pass"
            )
    return False


def _clear_pair_departures(
    path: Path,
    rules: DesignRules,
    *,
    ref: str,
    footprint: str,
    pad_size: XY,
    width: float,
    diameter: float,
    escapes: tuple[_PairEscape, _PairEscape],
    require_timing: bool = False,
) -> int:
    """Save the two measured footprint exits together, before refill and DRC."""
    pcb = PCB.load(path)
    fp = pcb.get_footprint(ref)
    if fp is None or fp.name != footprint or fp.rotation or fp.layer != "F.Cu":
        return 0
    original_skew = _raw_trace_skew(pcb, escapes) if require_timing else None
    for escape in escapes:
        pad = next((p for p in fp.pads if p.number == escape.pad), None)
        if (
            pad is None
            or pad.net_name != escape.net
            or pad.shape != "rect"
            or pad.rotation
            or pad.size != pad_size
        ):
            return 0
        if not _rebuild_pair_branch(pcb, ref, escape, rules, width, diameter):
            return 0
    if original_skew is not None and (
        abs(_raw_trace_skew(pcb, escapes) - original_skew) > _EPS
        or not _within_project_skew_budget(pcb, path, (escapes[0].net, escapes[1].net))
    ):
        return 0
    pcb.save(path)
    return 2


_PCIE_TAPER_WIDTH = 0.225


def _rebuild_pcie_taper(
    pcb: PCB, pad, points: list[XY], protected: set[str]
) -> list[Segment] | None:
    """Widen an unbranched taper only along its existing, measured centerline."""
    net = pad.net_name
    junctions = defaultdict(list)
    for segment in pcb.segments:
        if segment.layer == "F.Cu" and segment.net_name == net:
            for endpoint in (segment.start, segment.end):
                junctions[_xy(endpoint)].append(segment)

    removed: list[Segment] = []
    visited = [points[0]]
    while visited[-1] != points[-1]:
        choices = [s for s in junctions[visited[-1]] if s not in removed]
        if len(choices) != 1:
            return None
        segment = choices[0]
        if (
            segment.locked
            or (segment.uuid or "").lower() in protected
            or not 0 < segment.width < _PCIE_TAPER_WIDTH - _EPS
        ):
            return None
        end = _xy(segment.end) if _xy(segment.start) == visited[-1] else _xy(segment.start)
        if end in visited:
            return None
        removed.append(segment)
        visited.append(end)

    trunks = [s for s in junctions[points[-1]] if s not in removed]
    if len(trunks) != 1 or abs(trunks[0].width - _PCIE_TAPER_WIDTH) > _EPS:
        return None
    original_line, replacement = LineString(visited), LineString(points)
    if (
        original_line.hausdorff_distance(replacement) > _EPS
        or abs(original_line.length - replacement.length) > _EPS
    ):
        return None

    validator = ConnectivityValidator(pcb)
    before = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(before.components()) != 1:
        return None
    contacts, ends = _external_contacts(before, pcb.segments, removed, None)
    if pcb.remove_segments(removed) != len(removed):
        return None
    added = []
    for start, end in pairwise(points):
        added.extend(pcb.add_trace(start, end, width=_PCIE_TAPER_WIDTH, layer="F.Cu", net=net))
    after = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    new_contacts, new_ends = _external_contacts(after, pcb.segments, added, None)
    if len(after.components()) != 1 or new_contacts != contacts or not ends <= new_ends:
        return None
    return added


def _protected_ids(pcb: PCB) -> set[str]:
    """Track UUIDs the repairs must not replace: locked items and group members.

    Replacing an authored group member would leave its UUID dangling. This
    includes a track inside a subgroup of an unlocked tuning pattern.
    """
    protected = {uid.lower() for uid in _locked_ids(pcb) if uid}
    for owner in (pcb._sexp, *pcb._sexp.find_children("footprint")):
        for group in (*owner.find_children("group"), *owner.find_children("generated")):
            members = group.find_child("members")
            if members is not None:
                protected.update(str(member).lower() for member in members.get_atoms())
    return protected


def _copper_gap_ok(
    pcb: PCB,
    path: Path,
    tracks: list[Segment],
    vias: list,
    rules: DesignRules,
) -> bool:
    """Check staged tracks and vias against other-net copper under the project rules.

    Each staged item is graded on every copper layer it occupies, so a through
    barrel is held clear of inner-layer copper as well as the outer layers.
    """
    project = BoardClearanceRules.from_board(path)
    if project.dru_error:
        return False
    names = [layer.name for layer in pcb.copper_layers]
    candidates = [
        (
            track.net_number,
            LineString([track.start, track.end]),
            track.width / 2,
            {track.layer},
            item_props_for_type(
                "track",
                track.net_name,
                False,
                layer=track.layer,
                footprint_ref="",
                footprint_lib_id="",
            ),
        )
        for track in tracks
    ]
    for via in vias:
        through = {"F.Cu", "B.Cu"} <= set(via.layers)
        candidates.append(
            (
                via.net_number,
                Point(via.position),
                via.size / 2,
                set(names) if through else set(via.layers),
                item_props_for_type(
                    "via", via.net_name, True, layer=None, footprint_ref="", footprint_lib_id=""
                ),
            )
        )
    for item in collect_copper_items(ConnectivityValidator(pcb), include_all_pads=True):
        # The enclosing producer regenerates and grades all pours after the edit.
        if item.kind == "fill":
            continue
        net = pcb.get_net(item.net)
        name = net.name if net else ""
        override = None
        if item.kind == "pad":
            fp = pcb.footprints[item.key[0]]
            pad = fp.pads[item.key[1]]
            obstacle = item_props_for_type(
                "pad", name, pad.type == "thru_hole", pad.type, fp.layer, fp.reference, fp.name
            )
            override = pad_clearance_override(fp, pad)
        else:
            obstacle = item_props_for_type(
                item.kind,
                name,
                item.kind == "via",
                layer="F.Cu",
                footprint_ref="",
                footprint_lib_id="",
            )
        for number, shape, radius, layers, props in candidates:
            shared = layers & item.layers
            if item.net == number or not shared:
                continue
            gap = rules.min_clearance_mm
            for layer in shared:
                electrical = (
                    project.clearance(props, obstacle, layer)
                    if override is None
                    else max(override, project.min_clearance or 0)
                )
                gap = max(gap, electrical, project.physical_clearance(props, obstacle, layer))
            if shape.distance(item.geom) < radius + gap - _EPS:
                return False
    return True


def _zone_covers_f_cu(zone) -> bool:
    names = zone.layers or ([zone.layer] if zone.layer else [])
    return any(name in ("F.Cu", "*.Cu", "F&B.Cu") for name in names)


def _zone_shape(zone):
    """The area's physical outline, with any cutouts, or ``None`` if degenerate."""
    rings = [ring for ring in zone.rule_area_contours if len(ring) >= 3]
    if not rings:
        rings = [list(zone.polygon)] if len(zone.polygon) >= 3 else []
    if not rings:
        return None
    shape = Polygon(rings[0], holes=rings[1:])
    if not shape.is_valid:
        # A self-crossing keepout can protect copper in both lobes.
        shape = make_valid(shape)
    return shape if not shape.is_empty and shape.area > 0 else None


def _pcie_taper_authored_rules_ok(pcb: PCB, path: Path, added: list[Segment]) -> bool:
    """Reject proposed F.Cu rails that break an authored keepout or track-width rule.

    Preserve the pair when an applicable width bound or keepout cannot be evaluated.
    """
    rails = [
        (track, LineString([track.start, track.end]).buffer(track.width / 2)) for track in added
    ]
    for zone in all_rule_area_zones(pcb):
        if zone.keepout is None or zone.keepout.tracks_allowed or not _zone_covers_f_cu(zone):
            continue
        area = _zone_shape(zone)
        if area is None or any(area.intersects(copper) for _, copper in rails):
            return False

    project = BoardClearanceRules.from_board(path)
    try:
        from kicad_tools.sexp import parse_string

        tree = parse_string("(rules " + path.with_suffix(".kicad_dru").read_text("utf-8") + ")")
    except OSError:
        return True  # no design-rules file: nothing authored to violate
    except Exception:
        return False
    # Names can repeat. Keep back-only names with an empty list so a known
    # off-layer area is distinct from a reference we cannot resolve.
    named = {}
    for zone in (*pcb.zones, *getattr(pcb, "footprint_rule_areas", ())):
        if zone.name:
            shapes = named.setdefault(zone.name, [])
            if _zone_covers_f_cu(zone):
                shapes.append(_zone_shape(zone))
    for rule in tree.find_all("rule"):
        severity_node = rule.find_child("severity")
        if severity_node is not None and str(severity_node.get_first_atom()).casefold() == "ignore":
            continue  # where an ignored rule wins, KiCad checks nothing
        layer_node = rule.find_child("layer")
        layer = layer_node.get_first_atom() if layer_node is not None else None
        if layer is not None and _layer_matches(str(layer), "F.Cu") is False:
            continue
        cond_node = rule.find_child("condition")
        raw = cond_node.get_first_atom() if cond_node is not None else None
        for constraint in rule.find_all("constraint"):
            atoms = constraint.get_atoms()
            if not atoms or str(atoms[0]) != "track_width":
                continue
            limits = {}
            unknown_bound = False
            for bound in ("min", "max"):
                node = constraint.find_child(bound)
                if node is None:
                    continue
                value = _parse_length_mm(node.get_first_atom()) if len(node.children) == 1 else None
                if value is None or not isfinite(value):
                    # KiCad also accepts expressions. A missing bound is optional,
                    # but a present bound we cannot evaluate still constrains the rail.
                    unknown_bound = True
                else:
                    limits[bound] = value
            for track, copper in rails:
                if (
                    unknown_bound
                    or track.width < limits.get("min", 0.0) - _EPS
                    or track.width > limits.get("max", float("inf")) + _EPS
                ) and _dru_rule_applies(
                    str(raw) if raw is not None else None, track, copper, named, project
                ):
                    return False
    return True


def _dru_rule_applies(condition, track, copper, areas, project) -> bool:
    """Whether a rule's condition may hold for a proposed track (unknown: yes)."""
    if condition is None:
        return True

    def area_test(match) -> str:
        pattern = match.group(1)
        if "*" in pattern or "?" in pattern:
            # KiCad matches the argument as a wildcard pattern, so every
            # on-layer area whose name fits it counts. KiCad's case handling is
            # not modelled: a name that fits only after case-folding makes the
            # match uncertain (and a `!` would invert a wrong guess), so the
            # condition stays undecided and the pair is preserved.
            body = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
            exact = re.compile(body, re.DOTALL)
            folded = re.compile(body, re.DOTALL | re.IGNORECASE)
            hits = [name for name in areas if exact.fullmatch(name)]
            if hits != [name for name in areas if folded.fullmatch(name)]:
                return match.group(0)
            shapes = [shape for name in hits for shape in areas[name]] if hits else None
        else:
            shapes = areas.get(pattern)
        if shapes is None:
            return match.group(0)
        if any(shape is not None and shape.intersects(copper) for shape in shapes):
            return "(1 == 1)"
        if None in shapes:
            return match.group(0)  # An invalid outline leaves the condition undecided.
        return "(1 == 0)"

    condition = re.sub(r"\bA\.intersectsArea\(\s*'([^']*)'\s*\)", area_test, condition)
    props = item_props_for_type(
        "track", track.net_name, False, layer="F.Cu", footprint_ref="", footprint_lib_id=""
    )
    return evaluate_condition(condition, props, props, project) is not False


def _clear_pcie_taper_pair(
    path: Path,
    rules: DesignRules,
    ref: str,
    nets: tuple[str, str],
    exits: tuple[tuple[str, tuple[XY, ...]], ...],
) -> int:
    """Publish both pad exits together after checking copper spacing and pair delay."""
    pcb = PCB.load(path)
    if ref == "U3":
        footprint, size = "Package_QFP:LQFP-48_7x7mm_P0.5mm", (0.7, 0.3)
    else:
        footprint, size = "Connector_PCIE:PCIE_Mini_Edge", (0.5, 0.6)
    fp = pcb.get_footprint(ref)
    if fp is None or fp.name != footprint or fp.rotation or fp.layer != "F.Cu":
        return 0
    protected = _protected_ids(pcb)
    added = []
    for net, (number, offsets) in zip(nets, exits, strict=True):
        pad = next((p for p in fp.pads if p.number == number), None)
        center = pcb.get_pad_position(ref, number)
        if (
            pad is None
            or center is None
            or pad.net_name != net
            or pad.shape != "rect"
            or pad.rotation
            or pad.size != size
        ):
            return 0
        points = [_xy(center), *(_xy((center[0] + dx, center[1] + dy)) for dx, dy in offsets)]
        tracks = _rebuild_pcie_taper(pcb, pad, points, protected)
        if tracks is None:
            return 0
        added.extend(tracks)
    if not _within_project_skew_budget(pcb, path, nets):
        return 0
    if not _copper_gap_ok(pcb, path, added, [], rules):
        return 0
    if not _pcie_taper_authored_rules_ok(pcb, path, added):
        return 0
    pcb.save(path)
    return 2


_CLK_NETS = ("MIPI_CLK+", "MIPI_CLK-")
_CLK_WIDTH = 0.225
# Measured offsets carry four decimals; the router's own coordinates are rounded.
_CLK_TOL = 2e-3


@dataclass(frozen=True)
class _ClockExit:
    """One MIPI clock pad exit as measured on seed 42, and its replacement.

    Offsets are from the pad centre.  ``old`` is the corner list of today's
    router-made run (pad to trunk, or pad to barrel); ``new`` replaces it.
    A run that ends on the trunk keeps its measured end, so ``new`` omits it.
    When ``via`` is set the barrel moves from its old to its new offset, the
    front run ends on it, and the B.Cu side either rejoins the trunk
    (``back_*``) or keeps its run and gains a short bridge to the new seat.
    """

    ref: str
    footprint: str
    pad: str
    pad_size: XY
    net: str
    old: tuple[XY, ...]
    new: tuple[XY, ...]
    via: tuple[XY, XY] | None = None
    back_old: tuple[XY, ...] = ()
    back_new: tuple[XY, ...] = ()


_J4 = "Connector_FFC:FFC_4P_0.5mm"
_U4 = "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm"
_CLK_EXITS = (
    # J4.1: leaves the pad straight for 0.625 mm, past the mask opening, before
    # the 45-degree bend.  The run keeps its original 1.0782 mm, so the positive
    # rail does not change.
    _ClockExit(
        "J4",
        _J4,
        "1",
        (0.3, 0.8),
        "MIPI_CLK+",
        old=((0, 0), (0.05, -0.05), (0.05, -0.3), (0.3, -0.55), (0.3, -0.9539)),
        new=((0, 0), (0, -0.625), (0.3, -0.925)),
    ),
    # J4.2: leave the pad straight for 0.625 mm, then turn 45 degrees and finish
    # horizontally at the barrel. Seed 42 models 19.988 ps against 20.135 ps.
    _ClockExit(
        "J4",
        _J4,
        "2",
        (0.3, 0.8),
        "MIPI_CLK-",
        old=((0, 0), (0.05, 0.05), (0.05, 0.6), (0.1, 0.65)),
        new=((0, 0), (0, 0.625), (0.225, 0.85), (0.5, 0.85)),
        via=((0.1, 0.65), (0.5, 0.85)),
        back_old=((0.1, 0.65), (0.7599, 0.65)),
        back_new=((0.5, 0.85), (0.7, 0.65)),
    ),
    _ClockExit(
        "U4",
        _U4,
        "1",
        (0.7, 0.3),
        "MIPI_CLK+",
        old=((0, 0), (-0.4, -0.4), (-0.9165, -0.4)),
        new=((0, 0), (-0.4, -0.4)),
    ),
    _ClockExit(
        "U4",
        _U4,
        "2",
        (0.7, 0.3),
        "MIPI_CLK-",
        old=((0, 0), (0.6, 0)),
        new=((0, 0), (0.795, 0)),
        via=((0.6, 0), (0.795, 0)),
    ),
)


def _corners(points: list[XY]) -> list[XY]:
    """The polyline with collinear interior vertices removed."""
    out = [points[0]]
    for cur, nxt in zip(points[1:], points[2:], strict=False):
        a = (cur[0] - out[-1][0], cur[1] - out[-1][1])
        b = (nxt[0] - cur[0], nxt[1] - cur[1])
        scale = dist(cur, out[-1]) * dist(nxt, cur)
        if scale <= _EPS:
            continue
        straight = abs(a[0] * b[1] - a[1] * b[0]) <= 1e-3 * scale
        if straight and a[0] * b[0] + a[1] * b[1] > 0:
            continue
        out.append(cur)
    out.append(points[-1])
    return out


def _matches_measured(points: list[XY], center: XY, offsets: tuple[XY, ...]) -> bool:
    corners = _corners(points)
    expected = [_xy((center[0] + dx, center[1] + dy)) for dx, dy in offsets]
    return len(corners) == len(expected) and all(
        dist(a, b) <= _CLK_TOL for a, b in zip(corners, expected, strict=True)
    )


def _clock_run(
    pcb: PCB, net: str, layer: str, start: XY, protected: set[str], stop: XY | None
) -> tuple[list[Segment], list[XY]] | None:
    """Unbranched, unlocked, narrower-than-trunk run on ``layer`` from ``start``.

    It ends at ``stop`` (a barrel landing) or where the trunk-width copper
    begins; a tap, a stray landing, a lock or a group member declines it.
    """
    at: dict[XY, list[Segment]] = defaultdict(list)
    for seg in pcb.segments:
        if seg.layer == layer and seg.net_name == net:
            at[_xy(seg.start)].append(seg)
            at[_xy(seg.end)].append(seg)
    landings = {_xy(v.position) for v in pcb.vias if v.net_name == net}
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name == net:
                position = pcb.get_pad_position(fp.reference, pad.number)
                if position is not None:
                    landings.add(_xy(position))
    chain: list[Segment] = []
    points = [start]
    for _ in range(256):
        point = points[-1]
        if point != start and point in landings:
            return (chain, points) if point == stop else None
        choices = [s for s in at.get(point, []) if s not in chain]
        if len(choices) != 1 or len(at.get(point, [])) != (1 if point == start else 2):
            return None
        seg = choices[0]
        if seg.locked or (seg.uuid or "").lower() in protected:
            return None
        if seg.width >= _CLK_WIDTH - _EPS:
            return (chain, points) if chain and stop is None else None
        chain.append(seg)
        points.append(_xy(seg.end) if _xy(seg.start) == point else _xy(seg.start))
    return None


def _stage_clock_exit(
    pcb: PCB, ex: _ClockExit, protected: set[str]
) -> tuple[list[Segment], list[Via]] | None:
    """Rebuild one clock exit in memory; ``None`` leaves the caller to abandon it all."""
    fp = pcb.get_footprint(ex.ref)
    if fp is None or fp.name != ex.footprint or fp.rotation or fp.layer != "F.Cu":
        return None
    pad = next((p for p in fp.pads if p.number == ex.pad), None)
    position = pcb.get_pad_position(ex.ref, ex.pad)
    if (
        pad is None
        or position is None
        or pad.net_name != ex.net
        or pad.shape != "rect"
        or pad.rotation
        or pad.size != ex.pad_size
    ):
        return None
    center = _xy(position)

    def at(offset: XY) -> XY:
        return _xy((center[0] + offset[0], center[1] + offset[1]))

    old_seat = at(ex.via[0]) if ex.via else None
    front = _clock_run(pcb, ex.net, "F.Cu", center, protected, old_seat)
    if front is None or not _matches_measured(front[1], center, ex.old):
        return None
    removed, points = front
    via = via_index = back = None
    if ex.via:
        seated = [
            (i, v)
            for i, v in enumerate(pcb.vias)
            if v.net_name == ex.net and _xy(v.position) == old_seat
        ]
        if len(seated) != 1:
            return None
        via_index, via = seated[0]
        node = pcb.via_source_node(via)
        if (
            node is None
            or BoardItemLocks(pcb._sexp).is_locked(node)
            or (via.size, via.drill) != (0.6, 0.25)
        ):
            return None
        if ex.back_old:
            back = _clock_run(pcb, ex.net, "B.Cu", old_seat, protected, None)
            if back is None or not _matches_measured(back[1], center, ex.back_old):
                return None
            removed = [*removed, *back[0]]
        else:
            # The barrel keeps its B.Cu run; a bridge reaches the new seat.
            tail = [
                s
                for s in pcb.segments
                if s.layer == "B.Cu"
                and s.net_name == ex.net
                and old_seat in (_xy(s.start), _xy(s.end))
            ]
            if len(tail) != 1 or tail[0].locked or (tail[0].uuid or "").lower() in protected:
                return None
            bridge_width = tail[0].width
    original = list(pcb.segments)
    validator = ConnectivityValidator(pcb)
    before = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(before.components()) != 1:
        return None
    contacts, ends = _external_contacts(before, original, removed, via_index)
    if pcb.remove_segments(removed) != len(removed):
        return None
    if via is not None and not pcb.relocate_via(via, at(ex.via[1])):
        return None
    run = [at(o) for o in ex.new]
    if via is None:
        run.append(points[-1])
    added: list[Segment] = []
    for a, b in pairwise(run):
        added.extend(pcb.add_trace(a, b, width=_CLK_WIDTH, layer="F.Cu", net=ex.net))
    if back is not None:
        rejoin = [at(o) for o in ex.back_new] + [back[1][-1]]
        for a, b in pairwise(rejoin):
            added.extend(pcb.add_trace(a, b, width=_CLK_WIDTH, layer="B.Cu", net=ex.net))
    elif via is not None:
        added.extend(
            pcb.add_trace(at(ex.via[1]), old_seat, width=bridge_width, layer="B.Cu", net=ex.net)
        )
    after = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    current, current_ends = _external_contacts(after, pcb.segments, added, via_index)
    if len(after.components()) != 1 or not ends <= current_ends:
        return None
    if not contacts <= current:
        # The moved barrel can touch fewer pieces of a continuous taper; the
        # bridge must still reach every skipped piece through unchanged copper.
        original_ids = {id(s) for s in original}
        kept = [s for s in pcb.segments if id(s) in original_ids and s.net_name == ex.net]
        layers = {s.layer for s in added if s.layer != "F.Cu"}
        if via is None or not _keeps_via_approach(contacts, current, kept, layers):
            return None
    return added, [via] if via is not None else []


def _authored_rules_ok(
    pcb: PCB,
    path: Path,
    tracks: list[Segment],
    vias: list[Via],
    label: str,
    *,
    require_rules: bool = False,
) -> bool:
    """Have KiCad evaluate authored rules on the replacement copper before publishing it."""
    # A project can impose drill spacing on a moved barrel without a custom rules file.
    if not (
        require_rules
        or path.with_suffix(".kicad_dru").exists()
        or (vias and path.with_suffix(".kicad_pro").is_file())
        or all_rule_area_zones(pcb)
    ):
        return True
    changed = {item.uuid for item in (*tracks, *vias)}
    if not all(changed):
        return False
    try:
        with TemporaryDirectory(prefix="board06-staged-") as tmp:
            candidate = Path(tmp) / path.name
            pcb.save(candidate)
            staged = copy_project_sidecars(path, candidate)
            if require_rules and candidate.with_suffix(".kicad_dru") not in staged:
                return False
            report = candidate.with_suffix(".json")
            active = active_evidence_toolchain()
            toolchain = (
                nullcontext(active)
                if active
                else evidence_toolchain([candidate.parent], required_version=None)
            )
            with toolchain as chain:
                result = run_drc(
                    candidate,
                    output_path=report,
                    kicad_cli=Path(chain.kicad_cli),
                    schematic_parity=False,
                    all_track_errors=True,
                    refill_zones=True,
                )
            if not result.success:
                print(f"   {label} kept: {result.stderr}")
                return False
            native = load_native_connectivity_report(report)
    except (OSError, ValueError, KiCadEnvironmentError) as exc:
        print(f"   {label} kept: {exc}")
        return False
    for finding in [*native.violations, *native.unconnected_items]:
        if finding.is_error and (
            not any(finding.item_uuids) or changed.intersection(finding.item_uuids)
        ):
            print(f"   {label} kept: {finding.message}")
            return False
    return True


def _clear_clock_pair(path: Path, rules: DesignRules) -> int:
    """Publish all four MIPI clock exits together, or leave the file untouched.

    The two rails and both ends are staged in memory, then must clear other
    copper under the project rules and keep the pair inside its skew budget.
    """
    pcb = PCB.load(path)
    protected = _protected_ids(pcb)
    tracks: list[Segment] = []
    vias: list[Via] = []
    for ex in _CLK_EXITS:
        staged = _stage_clock_exit(pcb, ex, protected)
        if staged is None:
            return 0
        tracks.extend(staged[0])
        vias.extend(staged[1])
    if not _within_project_skew_budget(pcb, path, _CLK_NETS):
        return 0
    if not _copper_gap_ok(pcb, path, tracks, vias, rules):
        return 0
    if not _authored_rules_ok(pcb, path, tracks, vias, "MIPI clock exits"):
        return 0
    pcb.save(path)
    print(f"   pad departure MIPI clock pair: {len(_CLK_EXITS)} exits widened to {_CLK_WIDTH} mm")
    return len(_CLK_EXITS)


_DATA_NETS = ("MIPI_D0+", "MIPI_D0-")
_DATA_WIDTH = 0.225


@dataclass(frozen=True)
class _DataExit:
    """Seed-42 F.Cu corners relative to the pad centre, up to the trunk junction.

    MIPI and USB2 keep the far end of the adjoining trunk. USB3 RX2 ends
    on a fixed through-via with the declared (diameter, drill) in mm.
    """

    ref: str
    footprint: str
    pad: str
    pad_size: XY
    net: str
    old: tuple[XY, ...]
    new: tuple[XY, ...]
    trunk_end: XY | None = None
    fixed_via: tuple[float, float] | None = None


_DATA_EXITS = (
    # J4.4: the diagonal from the pad corner doubles back through a jog; the
    # rebuilt exit runs straight up the pad axis to (0, -0.3672) -- inside the
    # 0.8 mm land -- then rejoins the original diagonal line at the trunk.
    _DataExit(
        "J4",
        _J4,
        "4",
        (0.3, 0.8),
        "MIPI_D0-",
        old=((0, 0), (-0.2, -0.2), (-0.2, -0.1672), (0.3828, -0.75)),
        new=((0, 0), (0, -0.3672), (0.3828, -0.75)),
    ),
    # U4.3: the approach diagonal, jog and entry pass beside the opening; the
    # rebuilt exit leaves the land straight to (-0.29, 0) and rejoins at 45
    # degrees, the trunk growing 0.0772 mm to meet it.
    _DataExit(
        "U4",
        _U4,
        "3",
        (0.7, 0.3),
        "MIPI_D0+",
        old=((0, 0), (-0.2, 0.2), (-0.1672, 0.2), (-1.1172, -0.75)),
        new=((0, 0), (-0.29, 0), (-1.04, -0.75)),
    ),
    # U4.4: same shape on the negative rail, its corner at (-0.095, 0) inside
    # the land and the trunk growing 0.0378 mm.
    _DataExit(
        "U4",
        _U4,
        "4",
        (0.7, 0.3),
        "MIPI_D0-",
        old=((0, 0), (-0.2, -0.2), (-0.3328, -0.2), (-1.2828, -1.15)),
        new=((0, 0), (-0.095, 0), (-1.245, -1.15)),
    ),
)


_USB2_NETS = ("USB2_D+", "USB2_D-")
_U1_QFN = "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm"
_USB2_EXITS = (
    _DataExit(
        "U1",
        _U1_QFN,
        "10",
        (0.3, 0.7),
        "USB2_D+",
        old=((0, 0), (0, -0.0025), (0.1975, -0.2), (0.1975, -0.3339), (0.9975, -1.1339)),
        new=((0, 0), (0, -0.650), (0.9975, -1.6475)),
        trunk_end=(0.9975, -8.8839),
    ),
    _DataExit(
        "U1",
        _U1_QFN,
        "11",
        (0.3, 0.7),
        "USB2_D-",
        old=((0, 0), (0, -0.0025), (-0.1975, -0.2), (-0.1975, -0.1661), (0.6025, -0.9661)),
        new=((0, 0), (0, -0.875), (0.6025, -1.4775)),
        trunk_end=(0.6025, -8.7161),
    ),
)


_RX2_NETS = ("USB3_RX2+", "USB3_RX2-")
_J1_USB = "Connector_USB:USB_C_Receptacle_USB2.0"
_RX2_EXITS = (
    _DataExit(
        "J1",
        _J1_USB,
        "A10",
        (0.3, 0.6),
        "USB3_RX2-",
        old=((0, 0), (0, 0.15), (0.1936, 0.15), (0.95, 0.9064), (0.95, 1.10)),
        new=((0, 0), (0.58, 0.58), (0.71, 0.58), (0.95, 0.82), (0.95, 1.10)),
        fixed_via=(0.6, 0.25),
    ),
    _DataExit(
        "J1",
        _J1_USB,
        "A11",
        (0.3, 0.6),
        "USB3_RX2+",
        old=((0, 0), (0, 0.3436), (0.7564, 1.10), (0.95, 1.10)),
        new=((0, 0), (0.60, 0.60), (0.60, 1.10), (0.95, 1.10)),
        fixed_via=(0.6, 0.25),
    ),
)


def _fixed_via_ok(pcb: PCB, ex: _DataExit, fp, pad, seat: XY, protected: set[str]) -> bool:
    seated = [v for v in pcb.vias if v.net_name == ex.net and _xy(v.position) == seat]
    if len(seated) != 1:
        return False
    via = seated[0]
    if (via.size, via.drill) != ex.fixed_via or via.via_type or set(via.layers) != {"F.Cu", "B.Cu"}:
        return False
    locks = BoardItemLocks(pcb._sexp)
    anchors = ((fp, fp._sexp_node), (pad, pad._sexp_node), (via, pcb.via_source_node(via)))
    return all(
        node is not None and not locks.is_locked(node) and item.uuid.lower() not in protected
        for item, node in anchors
    )


def _data_run(
    pcb: PCB,
    ex: _DataExit,
    center: XY,
    junction: XY,
    protected: set[str],
    width: float,
    *,
    exact: bool = False,
) -> tuple[list[Segment], Segment | None] | None:
    """The measured escape run and any adjoining trunk.

    The walk follows the unique continuation from the pad centre, consuming
    only same-width F.Cu pieces of the pad's net; a branch, a stray landing,
    a lock or a group member declines it.  The consumed corner list must
    match the measured control. A fixed via terminates the F.Cu run;
    otherwise exactly one trunk piece must remain attached at the junction.
    """
    at: dict[XY, list[Segment]] = defaultdict(list)
    for seg in pcb.segments:
        if seg.layer == "F.Cu" and seg.net_name == ex.net:
            at[_xy(seg.start)].append(seg)
            at[_xy(seg.end)].append(seg)
    landings = {_xy(v.position) for v in pcb.vias if v.net_name == ex.net}
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name == ex.net:
                position = pcb.get_pad_position(fp.reference, pad.number)
                if position is not None:
                    landings.add(_xy(position))
    chain: list[Segment] = []
    points = [center]
    incoming: Segment | None = None
    for _ in range(64):
        point = points[-1]
        if point == junction:
            break
        choices = {id(s): s for s in at.get(point, []) if s is not incoming}
        if len(choices) != 1:
            return None
        seg = next(iter(choices.values()))
        if seg.locked or (seg.uuid or "").lower() in protected or abs(seg.width - width) > _EPS:
            return None
        far = _xy(seg.end) if _xy(seg.start) == point else _xy(seg.start)
        if far != junction and (far in landings or far in points):
            return None
        chain.append(seg)
        points.append(far)
        incoming = seg
    else:
        return None
    if exact:
        if tuple(_xy((x - center[0], y - center[1])) for x, y in points) != ex.old:
            return None
    elif not _matches_measured(points, center, ex.old):
        return None
    consumed = {id(s) for s in chain}
    remaining = [s for s in at.get(junction, []) if id(s) not in consumed]
    if ex.fixed_via is not None:
        return None if remaining else (chain, None)
    if len(remaining) != 1:
        return None
    trunk = remaining[0]
    if trunk.locked or (trunk.uuid or "").lower() in protected or abs(trunk.width - width) > _EPS:
        return None
    return chain, trunk


def _stage_data_exit(
    pcb: PCB, ex: _DataExit, protected: set[str], width: float
) -> list[Segment] | None:
    """Rebuild one data exit in memory; ``None`` leaves the caller to abandon the pair."""
    fp = pcb.get_footprint(ex.ref)
    if fp is None or fp.name != ex.footprint or fp.rotation or fp.layer != "F.Cu":
        return None
    pad = next((p for p in fp.pads if p.number == ex.pad), None)
    position = pcb.get_pad_position(ex.ref, ex.pad)
    if (
        pad is None
        or position is None
        or pad.net_name != ex.net
        or pad.shape != "rect"
        or pad.rotation
        or pad.size != ex.pad_size
    ):
        return None
    if ex.trunk_end is not None or ex.fixed_via is not None:
        # The measured USB openings have the same outlines as the SMD lands.
        if pad.type != "smd" or not {"F.Cu", "F.Mask"} <= set(pad.layers):
            return None
        shapes = _departure_mask_shapes(pcb, fp, pad)
        if shapes is None or not shapes[0].equals(shapes[1]):
            return None
    center = _xy(position)
    junction = _xy((center[0] + ex.old[-1][0], center[1] + ex.old[-1][1]))
    if ex.fixed_via is not None and not _fixed_via_ok(pcb, ex, fp, pad, junction, protected):
        return None
    walked = _data_run(pcb, ex, center, junction, protected, width)
    if walked is None:
        return None
    edited, trunk = walked
    points = [_xy((center[0] + dx, center[1] + dy)) for dx, dy in ex.new]
    target = points[-1]
    legs = list(pairwise(points))
    if trunk is None:
        if target != junction:
            return None
    else:
        far = _xy(trunk.end) if _xy(trunk.start) == junction else _xy(trunk.start)
        if not _on_line(far, junction, target):
            return None
        old_length, new_length = dist(far, junction), dist(far, target)
        moved = dist(junction, target)
        if ex.trunk_end is not None:
            expected_far = _xy((center[0] + ex.trunk_end[0], center[1] + ex.trunk_end[1]))
            # Only shorten the measured trunk. Never cross or consume its far endpoint.
            if far != expected_far or target == far or abs(moved + new_length - old_length) > _EPS:
                return None
        elif new_length < old_length - _EPS or abs(new_length - old_length - moved) > _CLK_TOL:
            return None
        edited.append(trunk)
        legs.append((far, target))
    original = list(pcb.segments)
    validator = ConnectivityValidator(pcb)
    before = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(before.components()) != 1:
        return None
    contacts, ends = _external_contacts(before, original, edited, None)
    if pcb.remove_segments(edited) != len(edited):
        return None
    added: list[Segment] = []
    for a, b in legs:
        if a != b:
            added.extend(pcb.add_trace(a, b, width=width, layer="F.Cu", net=ex.net))
    after = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    current, current_ends = _external_contacts(after, pcb.segments, added, None)
    if len(after.components()) != 1 or not contacts <= current or not ends <= current_ends:
        return None
    if ex.fixed_via is not None and contacts != current:
        return None
    return added


def _usb2_electrical_limits_ok(pcb: PCB, path: Path) -> bool:
    """Require measured coupling and compliant impedance for the two changed rails."""
    classes = net_class_map_from_path(path.with_name("net_class_map.json"), pcb_path=path)
    if any(net not in classes or not classes[net].target_diff_impedance for net in _USB2_NETS):
        return False
    # Only this pair is declared for the staged check. The continuity rule emits
    # below-threshold pairs even without verbose passing measurements, using the
    # full-precision fractions before rounding their report values.
    checker = DRCChecker(
        pcb, layers=len(pcb.copper_layers), net_class_map={net: classes[net] for net in _USB2_NETS}
    )
    continuity = checker.check_diffpair_routing_continuity()
    if (
        continuity.rules_checked_by_rule.get("diffpair_routing_continuity") != 1
        or continuity.violations
    ):
        return False
    impedance = checker.check_impedance()
    return _USB2_NETS not in checker.impedance_unmeasured_pairs and not any(
        not v.items or set(v.items).intersection(_USB2_NETS) for v in impedance.violations
    )


def _clear_data_pair(
    path: Path,
    rules: DesignRules,
    exits: tuple[_DataExit, ...],
    nets: tuple[str, str],
    width: float,
    label: str,
) -> int:
    """Stage all the exits, verify their contacts, clearances and timing, then save once."""
    pcb = PCB.load(path)
    protected = _protected_ids(pcb)
    tracks: list[Segment] = []
    for ex in exits:
        staged = _stage_data_exit(pcb, ex, protected, width)
        if staged is None:
            return 0
        tracks.extend(staged)
    if not _within_project_skew_budget(pcb, path, nets):
        return 0
    if nets == _USB2_NETS and not _usb2_electrical_limits_ok(pcb, path):
        return 0
    if not _copper_gap_ok(pcb, path, tracks, [], rules):
        return 0
    if not _authored_rules_ok(pcb, path, tracks, [], label + " exits"):
        return 0
    pcb.save(path)
    print(f"   pad departure {label} pair: {len(exits)} exits rebuilt at {width} mm")
    return len(exits)


_TX2_NETS = ("USB3_TX2+", "USB3_TX2-")
_TX2_EXIT = _DataExit(
    "J1",
    "Connector_USB:USB_C_Receptacle_USB2.0",
    "B2",
    (0.3, 0.6),
    _TX2_NETS[0],
    old=((0, 0), (0, 0.15), (0, 0.3657), (0.5834, 0.9491), (0.6806, 0.9491)),
    new=(
        (0, 0),
        (0, 0.2800),
        (0.4300, 0.7100),
        (0.5402, 0.7100),
        (0.5952, 0.7650),
        (0.5952, 0.8637),
        (0.6806, 0.9491),
    ),
)


_BGA_RX2 = (
    _PairEscape(
        "F5",
        "USB3_RX2+",
        "In1.Cu",
        (-0.47, -0.34),
        (-0.56, 0.56),
        (-5.395, -0.34),
        ((0, 0), (-0.03, 0), (-0.37, -0.34), (-0.47, -0.34)),
    ),
    _PairEscape(
        "F6",
        "USB3_RX2-",
        "In2.Cu",
        (-0.39, -0.39),
        (-0.56, -0.56),
        (-0.99, -0.99),
        ((0, 0), (-0.39, -0.39)),
    ),
)


def _tx2_coupling(checker: DRCChecker) -> float | None:
    for measurement in checker.check_diffpair_routing_continuity().violations:
        if set(measurement.nets or ()) == set(_TX2_NETS):
            return measurement.actual_value
    return None


def _tx2_contacts_follow_trunk(pcb, fp, pad, junction, contacts) -> bool:
    """The short bends after the junction may touch the launch, but a tap elsewhere may not."""
    pad_key = ("pad", (pcb.footprints.index(fp), fp.pads.index(pad)))
    if pad_key not in contacts:
        return False
    neighbors = contacts - {pad_key}
    tracks = [s for s in pcb.segments if ("segment", id(s)) in neighbors]
    if len(tracks) != len(neighbors):
        return False
    point = junction
    while tracks:
        joined = [s for s in tracks if point in (_xy(s.start), _xy(s.end))]
        if len(joined) != 1:
            return False
        track = joined[0]
        tracks.remove(track)
        point = _xy(track.end) if point == _xy(track.start) else _xy(track.start)
    return True


def _clear_tx2_exit(path: Path, rules: DesignRules, policy: MaskCopperPolicy) -> int:
    """Replace the four surveyed J1/B2 tracks, retaining the original trunk and pad contacts."""
    pcb = PCB.load(path)
    ex = _TX2_EXIT
    width = 0.275
    fp = pcb.get_footprint(ex.ref)
    if fp is None or fp.name != ex.footprint or fp.rotation or fp.layer != "F.Cu":
        return 0
    pad = next((p for p in fp.pads if p.number == ex.pad), None)
    if (
        pad is None
        or pad.net_name != ex.net
        or pad.type != "smd"
        or pad.shape != "rect"
        or pad.size != ex.pad_size
        or pad.rotation
        or not {"F.Cu", "F.Mask"} <= set(pad.layers)
    ):
        return 0
    protected = _protected_ids(pcb)
    locks = BoardItemLocks(pcb._sexp)
    if any(
        item._sexp_node is None
        or locks.is_locked(item._sexp_node)
        or item.uuid.lower() in protected
        for item in (fp, pad)
    ):
        return 0
    shapes = _departure_mask_shapes(pcb, fp, pad)
    position = pcb.get_pad_position(ex.ref, ex.pad)
    if position is None or shapes is None or not shapes[0].equals(shapes[1]):
        return 0
    center = _xy(position)
    junction = _xy((center[0] + ex.old[-1][0], center[1] + ex.old[-1][1]))
    walked = _data_run(pcb, ex, center, junction, protected, width, exact=True)
    if walked is None:
        return 0
    chain, _ = walked
    validator = ConnectivityValidator(pcb)
    before = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(before.components()) != 1:
        return 0
    contacts = _external_contacts(before, pcb.segments, chain, None)
    if not _tx2_contacts_follow_trunk(pcb, fp, pad, junction, contacts[0]):
        return 0
    project = BoardClearanceRules.from_board(path)
    if not project.class_clearance or project.class_clearance_of("GND") < 0.20:
        return 0
    try:
        classes = net_class_map_from_path(path.with_name("net_class_map.json"), pcb_path=path)
    except (OSError, TypeError, ValueError):
        return 0
    if any(n not in classes or not classes[n].target_diff_impedance for n in _TX2_NETS):
        return 0
    pair_classes = {n: classes[n] for n in _TX2_NETS}
    layers = len(pcb.copper_layers)
    coverage = _tx2_coupling(
        DRCChecker(pcb, layers=layers, net_class_map=pair_classes, emit_measurements=True)
    )
    if coverage is None or pcb.remove_segments(chain) != len(chain):
        return 0
    vertices = [_xy((center[0] + x, center[1] + y)) for x, y in ex.new]
    added = pcb.add_trace(
        vertices[0], vertices[-1], waypoints=vertices[1:-1], width=width, layer="F.Cu", net=ex.net
    )
    after = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(after.components()) != 1 or contacts != _external_contacts(
        after, pcb.segments, added, None
    ):
        return 0
    # The pair's ordinary intra-pair spacing is 0.125 mm. A newly written
    # connector escape must also meet the 0.15 mm local repair floor.
    local_rules = replace(rules, min_clearance_mm=max(rules.min_clearance_mm, 0.15))
    if not _copper_gap_ok(pcb, path, added, [], local_rules) or not _within_project_skew_budget(
        pcb, path, _TX2_NETS
    ):
        return 0
    checker = DRCChecker(pcb, layers=layers, net_class_map=pair_classes, emit_measurements=True)
    new_coverage = _tx2_coupling(checker)
    if new_coverage is None or new_coverage < coverage:
        return 0
    impedance = checker.check_impedance()
    if _TX2_NETS in checker.impedance_unmeasured_pairs or any(
        not finding.items or set(_TX2_NETS).intersection(finding.items)
        for finding in impedance.violations
    ):
        return 0
    if not _authored_rules_ok(pcb, path, added, [], "J1/B2 USB3_TX2+", require_rules=True):
        return 0
    with TemporaryDirectory(prefix="board06-tx2-mask-") as tmp:
        candidate = Path(tmp) / path.name
        pcb.save(candidate)
        copy_project_sidecars(path, candidate)
        mask = check_mask_to_copper(candidate, policy)
    changed = {s.uuid for s in added}
    measured = [m for m in mask.measurements if m.conductor_uuid in changed]
    if (
        mask.coverage != "complete"
        or not any(m.owner_ref == "J1/B2" and m.disposition == "within_own_pad" for m in measured)
        or any(m.disposition in {"violation", "warning", "uncertain"} for m in measured)
    ):
        return 0
    pcb.save(path)
    print("   pad departure J1/B2: rebuilt the USB3_TX2+ connector exit")
    return 1


def _clear_bga_rx2(path: Path, rules: DesignRules, policy: MaskCopperPolicy) -> int:
    """Move both full-width BGA escapes together after checking copper and both mask films."""
    pcb = PCB.load(path)
    fp = pcb.get_footprint("U2")
    protected = _protected_ids(pcb)
    locks = BoardItemLocks(pcb._sexp)
    if (
        fp is None
        or fp.name != "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm"
        or fp.layer != "F.Cu"
        or fp.rotation
        or fp._sexp_node is None
        or locks.is_locked(fp._sexp_node)
        or fp.uuid.lower() in protected
    ):
        return 0
    tracks: list[Segment] = []
    vias: list[Via] = []
    for escape in _BGA_RX2:
        pad = next((pad for pad in fp.pads if pad.number == escape.pad), None)
        if (
            pad is None
            or pad.net_name != escape.net
            or pad.type != "smd"
            or pad.size != (0.45, 0.45)
            or pad.shape != "rect"
            or pad.rotation
            or not {"F.Cu", "F.Mask"} <= set(pad.layers)
            or pad._sexp_node is None
            or locks.is_locked(pad._sexp_node)
            or pad.uuid.lower() in protected
        ):
            return 0
        shapes = _departure_mask_shapes(pcb, fp, pad)
        if shapes is None or not shapes[0].equals(shapes[1]):
            return 0
        staged = _rebuild_pair_branch(pcb, fp.reference, escape, rules, 0.275, 0.60, protected)
        if staged is None:
            return 0
        added, via = staged
        tracks.extend(added)
        vias.append(via)
    if not _copper_gap_ok(pcb, path, tracks, vias, rules):
        return 0
    if not _within_project_skew_budget(pcb, path, _RX2_NETS):
        return 0
    if not _authored_rules_ok(pcb, path, tracks, vias, "U2 USB3 RX2"):
        return 0
    with TemporaryDirectory(prefix="board06-rx2-mask-") as tmp:
        candidate = Path(tmp) / path.name
        pcb.save(candidate)
        copy_project_sidecars(path, candidate)
        mask = check_mask_to_copper(candidate, policy)
    if mask.coverage != "complete":
        return 0
    # An exposed via has an opening of its own. Moving it would change the film.
    sources = mask.geometry_provenance.get("source_objects", {})
    for via in vias:
        layers = sources.get(via.uuid, {}).get("layers_wkt", {})
        if not {"F.Cu", "B.Cu"} <= layers.keys() or {"F.Mask", "B.Mask"} & layers.keys():
            return 0
    affected = {item.uuid for item in (*tracks, *vias)}
    if any(
        measurement.conductor_uuid in affected
        and measurement.disposition in {"violation", "uncertain", "warning"}
        for measurement in mask.measurements
    ):
        return 0
    pcb.save(path)
    print("   U2 USB3 RX2: moved the F5 and F6 barrels clear of the mask openings")
    return len(vias)


def _center_u1_ground_entry(path: Path, rules: DesignRules, mask_clearance: float) -> int:
    """Reconnect the seed-42 U1.32 ground branch from the pad center to its original via."""
    pcb = PCB.load(path)
    fp = pcb.get_footprint("U1")
    if fp is None or fp.name != _U1_QFN or fp.layer != "F.Cu" or fp.rotation:
        return 0
    pad = next((p for p in fp.pads if p.number == "32"), None)
    center = pcb.get_pad_position("U1", "32")
    if (
        pad is None
        or pad.type != "smd"
        or pad.net_name != "GND"
        or center is None
        or pad.rotation
        or pad.shape != "rect"
        or pad.size != (0.3, 0.7)
        or "F.Mask" not in pad.layers
    ):
        return 0

    x, y = center
    branch = _xy((x - 0.70, y + 0.15))
    tip = _xy((x - 0.20, y + 0.15))
    seat = _xy((x - 0.75, y + 0.15))
    turn = _xy((x - 0.60, y))
    # Both original runs extend into the pad opening from the same side.
    # Match their endpoints and widths, including files with reversed segments.
    old = [
        s
        for s in pcb.segments
        if s.net_name == "GND" and s.layer == "F.Cu" and tip in (_xy(s.start), _xy(s.end))
    ]
    if len(old) != 2 or {
        (_xy(s.end) if _xy(s.start) == tip else _xy(s.start), s.width) for s in old
    } != {(branch, 0.20), (seat, 0.15)}:
        return 0
    protected = _protected_ids(pcb)
    if any(s.locked or (s.uuid or "").lower() in protected for s in old):
        return 0
    barrels = [
        i
        for i, via in enumerate(pcb.vias)
        if via.net_name == "GND" and _xy(via.position) == seat and "F.Cu" in via.layers
    ]
    shapes = _departure_mask_shapes(pcb, fp, pad)
    if len(barrels) != 1 or shapes is None:
        return 0
    barrel = barrels[0]
    _, opening = shapes
    if (
        LineString([turn, seat]).distance(opening) < 0.20 / 2 + mask_clearance
        or Point(seat).distance(opening) < pcb.vias[barrel].size / 2 + mask_clearance
    ):
        return 0

    validator = ConnectivityValidator(pcb)
    before = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(before.components()) != 1:
        return 0
    contacts, ends = _external_contacts(before, pcb.segments, old, barrel)
    if pcb.remove_segments(old) != len(old):
        return 0
    tracks = []
    for start, end in pairwise((center, turn, seat)):
        tracks.extend(pcb.add_trace(start, end, width=0.20, layer="F.Cu", net="GND"))
    after = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    new_contacts, new_ends = _external_contacts(after, pcb.segments, tracks, barrel)
    if len(after.components()) != 1 or not contacts <= new_contacts or not ends <= new_ends:
        return 0
    if not _copper_gap_ok(pcb, path, tracks, [], rules):
        return 0
    if not _authored_rules_ok(pcb, path, tracks, [], "U1.32 ground entry"):
        return 0
    pcb.save(path)
    return 1


@dataclass(frozen=True)
class _PowerExit:
    ref: str
    pin: str
    net: str
    # Paths in mm relative to the actual pad center. The last old point is the fixed barrel.
    original: tuple[XY, ...]
    replacement: tuple[tuple[XY, ...], ...]


_POWER_EXITS = (
    _PowerExit(
        "U2",
        "D3",
        "+1V2",
        ((-1.27, 0), (-0.32, 0), (0.33, 0.65)),
        (((-1.27, 0), (0, 0)), ((0, 0.65), (0.33, 0.65))),
    ),
    # Leaving pad 25 on the opposite edge creates a 0.100 mm same-net slit.
    _PowerExit(
        "U3",
        "25",
        "+3V3",
        (
            (0, 0),
            (-0.25, 0),
            (-0.30, -0.05),
            (-0.35, -0.05),
            (-0.40, -0.10),
            (-0.45, -0.10),
            (-0.70, -0.35),
        ),
        (((0, -0.05), (-0.65, -0.05), (-0.70, -0.10), (-0.70, -0.35)),),
    ),
    # The center segment on pad 36 connects its inscribed copper to the +1V2 pour.
    _PowerExit(
        "U3",
        "36",
        "+1V2",
        ((0, 0), (0.35, 0), (0.80, -0.45)),
        (((0, 0), (0.27, 0), (0.72, -0.45), (0.80, -0.45)),),
    ),
)


def _power_exit_mask_ok(
    pcb: PCB, site: _PowerExit, tracks: list[Segment], clearance: float
) -> bool:
    """Only the measured entry pads may share their mask openings with the new copper."""
    entries = {site.pin, "D2"} if site.ref == "U2" else {site.pin}
    runs = [(LineString([s.start, s.end]), s.width / 2) for s in tracks]
    for fp in pcb.footprints:
        for pad in fp.pads:
            if fp.reference == site.ref and pad.number in entries:
                continue
            if not {"F.Mask", "*.Mask"}.intersection(pad.layers):
                continue
            shapes = _departure_mask_shapes(pcb, fp, pad)
            if shapes is None or any(
                line.distance(shapes[1]) < radius + clearance - _EPS for line, radius in runs
            ):
                return False
    return True


def _power_exit_edge_ok(pcb: PCB, path: Path, tracks: list[Segment]) -> bool:
    """Apply the project copper-to-edge minimum to the new track envelopes."""
    minimum = BoardClearanceRules.from_board(path).min_edge_clearance
    outline = pcb.get_board_outline_segments()
    edges = outline_shapes(outline)
    curve_error = getattr(outline, "max_error_mm", 0.0)
    regions = pcb.get_board_outline_filled_regions()
    return all(
        min_gap_to_outline(segment_shape(track), edges) - curve_error >= minimum - _EPS
        and inside_filled_outline_region(regions, *track.start) is None
        for track in tracks
    )


def _clear_power_exit(
    path: Path, rules: DesignRules, site: _PowerExit, mask_clearance: float
) -> int:
    """Replace one measured front-layer departure, preserving its barrel and every external tap."""
    pcb = PCB.load(path)
    fp = pcb.get_footprint(site.ref)
    bga = site.ref == "U2"
    footprint, size = (
        ("Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm", (0.45, 0.45))
        if bga
        else ("Package_QFP:LQFP-48_7x7mm_P0.5mm", (0.70, 0.30))
    )
    if fp is None or fp.name != footprint or fp.layer != "F.Cu" or fp.rotation:
        return 0

    def measured_pad(number: str) -> Pad | None:
        pad = next((p for p in fp.pads if p.number == number), None)
        if (
            pad is None
            or pad.type != "smd"
            or pad.net_name != site.net
            or pad.rotation
            or pad.shape != "rect"
            or pad.size != size
            or not {"F.Cu", "F.Mask"} <= set(pad.layers)
        ):
            return None
        # These measured openings have exactly the outlines of the copper lands.
        shapes = _departure_mask_shapes(pcb, fp, pad)
        return pad if shapes is not None and shapes[0].equals(shapes[1]) else None

    pad = measured_pad(site.pin)
    center = pcb.get_pad_position(site.ref, site.pin)
    if pad is None or center is None:
        return 0

    def point(offset: XY) -> XY:
        return _xy((center[0] + offset[0], center[1] + offset[1]))

    expected = {frozenset((point(a), point(b))) for a, b in pairwise(site.original)}
    tracks = [s for s in pcb.segments if s.net_name == site.net and s.layer == "F.Cu"]
    old = [s for s in tracks if frozenset((_xy(s.start), _xy(s.end))) in expected]
    protected = _protected_ids(pcb)
    if (
        len(old) != len(expected)
        or {frozenset((_xy(s.start), _xy(s.end))) for s in old} != expected
        or any(
            s.locked or not s.uuid or s.uuid.lower() in protected or s.width != 0.20 for s in old
        )
    ):
        return 0
    seat = point(site.original[-1])
    barrels = [
        i
        for i, v in enumerate(pcb.vias)
        if v.net_name == site.net
        and _xy(v.position) == seat
        and set(v.layers) == {"F.Cu", "B.Cu"}
        and v.size == 0.50
        and v.drill == 0.30
    ]
    if len(barrels) != 1:
        return 0
    if bga:
        # D2 reaches D3 directly. Its retained vertical track must actually
        # cross both the pad center and the new horizontal branch to the barrel.
        junction = point(site.replacement[1][0])
        d2_center = pcb.get_pad_position(site.ref, "D2")
        if (
            measured_pad("D2") is None
            or d2_center is None
            or _xy(d2_center) != point(site.original[0])
        ):
            return 0
        span = LineString([center, junction])
        if not any(
            s.width == 0.20 and LineString([s.start, s.end]).buffer(_EPS).covers(span)
            for s in tracks
        ):
            return 0

    validator = ConnectivityValidator(pcb)
    before = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    if len(before.components()) != 1:
        return 0
    contacts, ends = _external_contacts(before, pcb.segments, old, barrels[0])
    if pcb.remove_segments(old) != len(old):
        return 0
    paths = [[point(offset) for offset in offsets] for offsets in site.replacement]
    new_runs = [
        pcb.add_trace(
            points[0], points[-1], waypoints=points[1:-1], width=0.20, layer="F.Cu", net=site.net
        )
        for points in paths
    ]
    added = [track for run in new_runs for track in run]
    after = build_copper_contact_graph(validator, pad.net_number, with_bonds=True)
    new_contacts, new_ends = _external_contacts(after, pcb.segments, added, barrels[0])
    if len(after.components()) != 1 or not contacts <= new_contacts or not ends <= new_ends:
        return 0
    if not _copper_gap_ok(pcb, path, added, [], rules):
        return 0
    if not _power_exit_mask_ok(pcb, site, added, mask_clearance):
        return 0
    if not _power_exit_edge_ok(pcb, path, added):
        return 0
    # The fabricator also sees slits between branches of the same net.
    # Check the final tracks together, with each other branch still present.
    if rules.inner_copper_oz is None:
        return 0
    entry = get_profile("jlcpcb").get_dfm_entry(
        len(pcb.copper_layers), rules.outer_copper_oz, rules.inner_copper_oz
    )
    if entry is None:
        return 0
    gap = StaircaseSlitOracle(pcb, fab_policy=fab_gap_policy_from_entry(entry))
    if any(
        gap.rejects("F.Cu", run, points, 0.20) for run, points in zip(new_runs, paths, strict=True)
    ):
        return 0
    if not _authored_rules_ok(pcb, path, added, [], f"{site.ref}/{site.pin} power entry"):
        return 0
    pcb.save(path)
    return 1


def clear_pad_departures(path: Path, rules: DesignRules) -> int:
    """Rewrite the sideband, differential-pair and power departures flagged by the strict mask check.

    Each single-ended pad or differential pair has a separate save boundary.
    """
    request = default_request_for_manufacturer("jlcpcb")
    if request is None:
        return 0
    edits = 0
    for ref, number, move_via in _TARGETS:
        edits += _repair_pad(
            path,
            rules,
            request.policy.warning_clearance_mm,
            request.policy.clearance_mm,
            ref,
            number,
            move_via,
        )
    edits += _clear_pair_departures(
        path,
        rules,
        ref="J1",
        footprint="Connector_USB:USB_C_Receptacle_USB2.0",
        pad_size=(0.3, 0.6),
        width=0.25,
        diameter=0.45,
        escapes=(
            _PairEscape("B6", "USB2_D+", "In1.Cu", (0.15, -0.55), (0.55, -0.55)),
            _PairEscape("B7", "USB2_D-", "In2.Cu", (0.30, 0.50), (0.55, 0.55)),
        ),
    )
    # Straight, full-width exits clear both mask openings. The extra 0.30 mm
    # on pad 32 preserves the pair's original routed length difference.
    edits += _clear_pair_departures(
        path,
        rules,
        ref="U3",
        footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
        pad_size=(0.7, 0.3),
        width=0.225,
        diameter=0.60,
        require_timing=True,
        escapes=(
            _PairEscape("31", "PCIE_RX+", "In1.Cu", (0.60, 0.10), (0.90, 0.0), (1.65, 1.15)),
            _PairEscape("32", "PCIE_RX-", "In2.Cu", (1.00, 0.20), (1.20, 0.0), (3.70, 2.90)),
        ),
    )
    edits += _clear_data_pair(path, rules, _RX2_EXITS, _RX2_NETS, 0.275, "USB3 RX2")
    edits += _clear_bga_rx2(path, rules, request.policy)
    tx_nets = ("PCIE_TX+", "PCIE_TX-")
    edits += _clear_pcie_taper_pair(
        path,
        rules,
        "U3",
        tx_nets,
        (("28", ((0.25, 0.0), (0.821, 0.571))), ("29", ((0.25, 0.0), (0.821, -0.571)))),
    )
    edits += _clear_pcie_taper_pair(
        path,
        rules,
        "J3",
        tx_nets,
        (
            ("5", ((-0.15, 0.15), (-0.15, 0.20), (-0.9798, 0.20))),
            ("6", ((-0.15, 0.15), (-0.9887, 0.15))),
        ),
    )
    edits += _clear_pcie_taper_pair(
        path,
        rules,
        "J3",
        ("PCIE_RX+", "PCIE_RX-"),
        (
            ("8", ((-0.15, -0.15), (-0.9887, -0.15))),
            ("9", ((-0.15, -0.15), (-0.15, -0.20), (-0.9798, -0.20))),
        ),
    )
    edits += _clear_clock_pair(path, rules)
    edits += _clear_data_pair(path, rules, _DATA_EXITS, _DATA_NETS, _DATA_WIDTH, "MIPI data")
    edits += _clear_data_pair(path, rules, _USB2_EXITS, _USB2_NETS, 0.25, "USB2")
    edits += _clear_tx2_exit(path, rules, request.policy)
    edits += _center_u1_ground_entry(path, rules, request.policy.warning_clearance_mm)
    return edits + sum(
        _clear_power_exit(path, rules, site, request.policy.clearance_mm) for site in _POWER_EXITS
    )
