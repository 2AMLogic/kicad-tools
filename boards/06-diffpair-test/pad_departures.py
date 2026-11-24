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
its modeled propagation delay inside the board's PCIe skew budget.

Every repair is guarded: the departure is derived from the actual pad and its
connected copper, and anything locked, branched off the recognized shape, or
not reconnectable as one physical component is left untouched, file intact.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise
from math import dist
from pathlib import Path

from shapely.geometry import LineString, Point

from kicad_tools.cli.relocate_in_pad_vias import relocate_pad_edge_vias
from kicad_tools.core.pcb_item_locks import BoardItemLocks
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.router.quantize import dogleg_points
from kicad_tools.router.rules import net_class_map_from_path
from kicad_tools.schema.pcb import PCB, Segment
from kicad_tools.validate.connectivity import ConnectivityValidator
from kicad_tools.validate.copper_contact import CopperContactGraph, build_copper_contact_graph
from kicad_tools.validate.diffpair_skew import derive_pair_skews
from kicad_tools.validate.mask_copper import default_request_for_manufacturer
from kicad_tools.validate.mask_geometry import board_mask_margin, pad_mask_margin, pad_mask_shapes
from kicad_tools.validate.rules.diffpair_length_skew import measured_skew_verdict
from kicad_tools.validate.rules.impedance import ImpedanceRule

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
    base_margin, declared = board_mask_margin(pcb)
    if base_margin is None:
        return 0
    version_node = pcb._sexp.find("version")
    version = version_node.get_int() if version_node else 0
    margin, _ = pad_mask_margin(pad, fp, base_margin, declared, board_version=version or 0)
    if margin is None or margin < 0:
        return 0
    try:
        pad_copper, opening = pad_mask_shapes(pad, fp, margin)
    except ValueError:
        return 0
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
) -> bool:
    """Reconnect one data pad to its original trunk, pending its partner's checks."""
    center = pcb.get_pad_position(ref, escape.pad)
    if center is None:
        return False
    locked = _locked_ids(pcb)
    net = escape.net
    walk = _walk_chain(pcb, net, _xy(center), locked)
    if walk is None:
        return False
    tracks, old = walk
    if _xy((old[0] - center[0], old[1] - center[1])) != escape.old_offset:
        return False
    vias = [(i, v) for i, v in enumerate(pcb.vias) if v.net_name == net and _xy(v.position) == old]
    if len(vias) != 1:
        return False
    via_index, via = vias[0]
    node = pcb.via_source_node(via)
    if node is None or BoardItemLocks(pcb._sexp).is_locked(node):
        return False
    size = node.find("size")
    if size is None:
        return False
    if (via.size, via.drill) != (0.6, 0.25) or diameter + _EPS < max(
        rules.min_via_diameter_mm, via.drill + 2 * rules.min_annular_ring_mm
    ):
        return False
    approach = _inner_approach(pcb, escape, _xy(center), old, tracks, locked)
    if approach is None:
        return False
    inner, joint = approach
    edited = [*tracks, *inner]
    if escape.inner_end_offset is None and any(abs(s.width - width) > _EPS for s in edited):
        return False
    if any(s.width > width + _EPS for s in edited) or abs(inner[-1].width - width) > _EPS:
        return False

    original = list(pcb.segments)
    validator = ConnectivityValidator(pcb)
    before = build_copper_contact_graph(validator, via.net_number, with_bonds=True)
    if len(before.components()) != 1:
        return False
    contacts, ends = _external_contacts(before, original, edited, via_index)
    target = _xy((center[0] + escape.new_offset[0], center[1] + escape.new_offset[1]))
    if pcb.remove_segments(edited) != len(edited) or not pcb.relocate_via(via, target):
        return False
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
    return len(after.components()) == 1 and contacts <= current and ends <= current_ends


def _raw_trace_skew(pcb: PCB, escapes: tuple[_PairEscape, _PairEscape]) -> float:
    # Only straight traces change. Retained arcs and via barrels cancel when
    # comparing the signed difference before and after this candidate.
    positive, negative = (
        sum(dist(s.start, s.end) for s in pcb.segments if s.net_name == escape.net)
        for escape in escapes
    )
    return positive - negative


def _within_project_skew_budget(
    pcb: PCB, path: Path, escapes: tuple[_PairEscape, _PairEscape]
) -> bool:
    # Board 06 writes this map beside the routed board and passes it to kct check.
    try:
        classes = net_class_map_from_path(path.with_name("net_class_map.json"), pcb_path=path)
    except (OSError, TypeError, ValueError):
        return False
    for pair in derive_pair_skews(pcb, classes, num_copper_layers=len(pcb.copper_layers)):
        if (pair.p_name, pair.n_name) == (escapes[0].net, escapes[1].net):
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
        or not _within_project_skew_budget(pcb, path, escapes)
    ):
        return 0
    pcb.save(path)
    return 2


def clear_pad_departures(path: Path, rules: DesignRules) -> int:
    """Rewrite the sideband and paired data escapes the strict mask view flags.

    Each sideband pad and each differential pair has a separate save boundary.
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
    return edits + _clear_pair_departures(
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
