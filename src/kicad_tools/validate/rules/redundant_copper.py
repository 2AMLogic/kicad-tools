"""Redundant / avoidable copper advisory (Issue #8218).

Cosmetic-but-avoidable copper passes every sign-off gate and ships.  This
rule *reports* four patterns (always ``severity="warning"``, never an
error), each with a location:

``redundant_copper_duplicate``
    Exact-duplicate segments -- the same predicate ``PCB.dedupe_copper``
    removes (net + layer + width + unordered endpoints, ~1 micron).
    One warning per *extra* copy, so a board with 15 duplicates reports 15.

``redundant_copper_fragments``
    A run of >= ``FRAGMENT_RUN_MIN`` consecutive same-net, same-layer
    segments each shorter than ``FRAGMENT_MAX_MM`` joined end to end
    (the 0.07-0.09 mm fragment case).

``redundant_copper_detour``
    A chain of segments (joined through degree-2 nodes) whose routed
    length exceeds ``DETOUR_RATIO`` times the straight distance between
    its ends, and is longer than ``DETOUR_MIN_LENGTH_MM``.  The chain ends
    at every junction -- a same-net pad or via, and a same-net track that
    lands on the chain or touches one of its nodes (Issue #10667) -- so two
    conductors meeting at a tap are never read as one long way round.
    Known limitation: intentional length-tuning meanders (match groups,
    diff pairs) are also reported; treat those as expected advisories.

``redundant_copper_staircase``
    A chain of >= ``STAIRCASE_MIN_SEGMENTS`` same-net, same-layer segments
    that descends monotonically, and whose end points can be joined by a
    two-segment (45 degree + H/V, either order) or straight path with fewer
    bends that is clear of foreign copper at the chain's width plus
    clearance (Issue #8831).  ``redundant_copper_detour`` cannot see this: a
    staircase hugging a diagonal has a length ratio near 1.  Repair with
    ``kct optimize-traces`` or ``kct fix-drc --only redundant-copper``.

    The fewer-bend path is reported only when it is legal copper (Issue
    #9817): every leg clears foreign copper at the pair requirement
    ``netclass_clearance_copper`` enforces (``netclass_pair_requirement``:
    the fab floor or the larger of the two nets' authored classes),
    compared through the same acceptance band as that check (Issue #9848),
    and the copper it adds beyond the existing chain lies in no pad's
    mask-opening zone (the opening ``mask_geometry`` builds, grown by the
    mask-to-copper policy clearance) other than on a same-net pad's own
    copper or as one clean entry of the whole proposed path -- the criteria
    ``mask_to_copper``'s ``within_own_pad`` class applies (Issue #9848).
    The complete replacement must also clear the active physical copper gap
    floor against the remaining tracks, pads, vias, and filled zones (Issue
    #9862). An explicit threshold applies to all copper. The fab-view policy
    keeps zone-fill gaps informational. A rejected bend falls through to the
    next candidate.
    Chains end at junctions: same-net pad copper and same-net via centres
    (Issue #9817) -- a pad or via centre is a terminal, never a pass-through
    vertex, so a "shortcut" can never bypass and disconnect it -- and, since
    Issue #10667, same-net tracks: a chain is cut where a track lands on the
    centreline of one of its segments, and at a node a track touches by
    overlap.  Each piece is judged on its own, so a shortcut always starts
    or ends at the tap it would otherwise have orphaned.  A node joined to
    same-net copper only by overlap may be spanned when the replacement
    copper keeps contact with every terminal it spans (Issue #9848).

``redundant_copper_via_offcentre``
    A same-net track end that lands ``OFFCENTRE_MIN_MM`` to
    ``OFFCENTRE_MAX_MM`` from a via centre (on a layer the via spans),
    connecting to it only by overlap.  Ends shared with another segment
    (joints, bends near the via) and layers where a track already ends on
    the via centre are not flagged.

``redundant_copper_pour_stub``
    A track leaving a pad and its optional terminal via are redundant when
    a private native refill preserves direct pad-to-pour contact, the
    physical terminal groups and the plane connections (Issue #10137).
    Opt-in (``kct check --prove-pour-stubs``): the proof runs one kicad-cli
    refill per candidate branch, so it is skipped by default.

``redundant_copper_covered``
    A segment whose both ends lie on the centreline of a strictly longer,
    no-narrower, same-net, same-layer segment's centreline (within
    ``COVERED_TOL_MM``, shared with ``kct pcb prune``), i.e. it is wholly
    hidden under the longer one.  Exact duplicates are only
    reported as ``redundant_copper_duplicate``.  Repair with
    ``kct pcb prune`` (connectivity-guarded), not ``dedupe``, or with the
    verified ``kct fix-drc --only redundant-copper`` (Issue #10667), which
    also rewrites the staircases and detours above.

The repair itself lives in ``kicad_tools.drc.repair_redundant_copper``;
this rule reports, and its chain walk and shortcut gates are what that
repair re-runs so an applied edit is held to the standard that reported it.
Redundant *branches* are the scope of #8216.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from kicad_tools.core.layers import via_spans_layer
from kicad_tools.pcb_prune import COVERED_TOL_MM  # shared with prune (#8734)

from ..clearance_shapes import KSegment, KShape, KVia, copper_gap, segment_shape
from ..copper_refs import board_origin, segment_witness, via_ref
from ..mask_geometry import CURVE_ERROR_MM
from ..violations import DRCResults, DRCViolation
from .base import DRCRule, drc_clear

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

#: Segments shorter than this (mm) count as fragments.
FRAGMENT_MAX_MM = 0.1
#: Minimum number of consecutive fragments reported as a run.
FRAGMENT_RUN_MIN = 3
#: Routed length / straight distance above which a chain is a detour.
DETOUR_RATIO = 2.0
#: Detours shorter than this routed length (mm) are ignored.
DETOUR_MIN_LENGTH_MM = 2.0
#: Off-centre window (mm) for a track end vs. a same-net via centre.
OFFCENTRE_MIN_MM = 0.1
OFFCENTRE_MAX_MM = 0.4
#: Minimum chain length (segments) reported as a staircase (Issue #8831).
STAIRCASE_MIN_SEGMENTS = 3
#: Fallback copper clearance (mm) when no design rules are supplied.
STAIRCASE_DEFAULT_CLEARANCE_MM = 0.2
#: New candidate copper (mm^2) in a mask-opening zone above which the
#: candidate is rejected (geometric noise only; mask_to_copper flags any area).
STAIRCASE_MASK_AREA_TOL_MM2 = 1e-6
#: A degree-2 chain node this close (mm) to a same-net pad/via centre is a
#: centre contact -- a hard stop the walk never spans (Issues #9817/#9848).
#: Mirrors ``_check_via_offcentre``'s centred epsilon.
TERMINAL_CENTRE_EPS_MM = 0.01
#: Rounding allowance for a physically touching pair of copper edges.
_CONTACT_EPS_MM = 1e-9
#: How far (mm) a tap may lie from a replacement leg's centreline and still
#: count as kept by it -- the line ``redundant_copper_covered`` reads.
_COVERED_TOL = 1e-3
#: Projection parameter, at each end of a chain edge, inside which a track
#: end is the edge's own node (already a junction) rather than a mid-span
#: landing; it only absorbs float noise in the projection.
_LANDING_END_EPS = 1e-9
#: Construction uncertainty (mm) of the analytic mask model -- the same
#: budget ``mask_copper`` derives from its export provenance: twice the
#: per-construction curve error plus the 4 nm float term.
_MASK_UNCERTAINTY_MM = 2 * CURVE_ERROR_MM + 0.000004
_MASK_SIDE = {"F.Cu": "F.Mask", "B.Cu": "B.Mask"}


_QUANT = 3


def _point_seg_dist(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Distance from point ``p`` to segment ``a``-``b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _q(point: tuple[float, float]) -> tuple[float, float]:
    return (round(point[0], _QUANT), round(point[1], _QUANT))


def _along(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Where ``p`` falls on the line ``a`` -> ``b`` (0 at ``a``, 1 at ``b``)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 <= 0.0:
        return 0.0
    return ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2


class _MaskZone(NamedTuple):
    """One pad's mask-opening zone (Issue #9817) and its owner's geometry.

    The extra fields beyond the zone itself feed ``mask_to_copper``'s
    ``within_own_pad`` entry criteria on a same-net zone (Issue #9848).
    """

    zone: Any  # the opening grown by the policy clearance
    copper: Any  # the pad's own copper
    net: int  # the pad's net number
    opening: Any  # the opening itself
    margin: float  # the pad's effective mask expansion
    kind: str  # native object kind, e.g. ``pad:rect``


class _ChainOption(NamedTuple):
    """One way to replace a chain the walk ended at a junction (Issue #10667).

    ``steps`` are what the copper gates judge: a cut end segment replaced
    whole where the new leg runs on through its tap.  ``ends`` are the
    replacement's two end nodes, ``landings`` the ``(tap node, at the first
    end)`` pairs the leg must keep, ``trims`` whether each cut end is instead
    trimmed at its junction (``False`` for a whole end segment).
    """

    steps: tuple
    ends: tuple[tuple[float, float], tuple[float, float]]
    landings: tuple
    trims: tuple[bool, bool]


class _ChainGraph(NamedTuple):
    """A (net, layer) group's chain graph with every junction marked.

    ``edges`` / ``node_edges`` are :meth:`RedundantCopperRule._endpoint_graph`'s,
    cut at every same-net track junction (Issue #10667).  ``stops`` are the
    nodes a chain ends at; ``overlap_nodes`` the nodes a same-net pad or via
    spans only by overlap (Issue #9848); ``terminals`` the copper shape of
    every same-net pad and via on the layer.
    """

    edges: list[Any]
    node_edges: dict[tuple[float, float], list[int]]
    stops: frozenset
    overlap_nodes: frozenset
    terminals: tuple


def _pad_kind(pad: Any) -> str:
    """The pad's native object kind, as the mask inventory names it."""
    node = getattr(pad, "_sexp_node", None)
    shape = node.get_string(2) if node is not None else None
    kind = "pad:" + (shape or "unknown")
    if node is not None:
        if node.find_child("padstack") is not None:
            kind += ":padstack"
        if node.find_child("chamfer") is not None:
            kind += ":chamfer"
    return kind


class RedundantCopperRule(DRCRule):
    """Report avoidable copper: duplicates, fragments, detours, off-centre ends."""

    rule_id = "redundant_copper"
    name = "Redundant Copper"
    description = (
        "Reports exact-duplicate segments, runs of very short segments, "
        "long detours and track ends off a via centre (advisory)."
    )

    def __init__(
        self,
        mask_clearance_mm: float = 0.0,
        physical_copper_gap_mm: float | None = None,
        fab_view_gap_policy: Any | None = None,
        prove_pour_stubs: bool = False,
    ) -> None:
        """``prove_pour_stubs``: run the native-refill proof for
        ``redundant_copper_pour_stub`` (Issue #10137).  Off by default: it
        costs one kicad-cli refill per candidate branch plus a baseline.

        ``mask_clearance_mm``: the mask-to-copper policy clearance (mm).

        A staircase shortcut may put no new copper within this distance of a
        pad's mask opening (Issue #9817).  ``0`` still keeps new copper out of
        the opening itself.  ``DRCChecker`` passes the active
        ``mask_to_copper`` policy's (strictest) clearance.

        ``physical_copper_gap_mm`` / ``fab_view_gap_policy`` (Issue #9862):
        the active physical-gap policy -- the explicit threshold, else the
        resolved fab-view bands.  Each shortcut candidate is additionally
        judged against the copper slit floor that policy enforces; when
        neither is given (no active physical-gap pass) the gate is off.
        """
        self.mask_clearance_mm = max(0.0, float(mask_clearance_mm))
        self._prove_pour_stubs = prove_pour_stubs
        self._gap_mm = physical_copper_gap_mm
        self._gap_policy = fab_view_gap_policy
        self._gap_oracle: Any | None = None

    def bind(self, pcb: PCB, design_rules: DesignRules | None) -> None:
        """Set the per-board state the chain gates read, without reporting.

        ``check`` calls this first; the redundant-copper repair
        (``kicad_tools.drc.repair_redundant_copper``) calls it on the board it
        is editing so its candidate shortcuts are judged by exactly the gates
        that reported them (Issue #10667).
        """
        self._origin = board_origin(pcb)
        self._pcb = pcb
        self._clearance = (
            getattr(design_rules, "min_clearance_mm", None) or STAIRCASE_DEFAULT_CLEARANCE_MM
        )
        self._requirement = self._pair_requirement(pcb)
        self._foreign_cache: dict[str, list[Any]] = {}
        self._mask_cache: dict[str, list[_MaskZone]] = {}
        self._gap_oracle = None  # Rebuild lazily for this PCB.

    def invalidate_copper(self) -> None:
        """Forget per-layer copper caches after an edit (pads and masks keep)."""
        self._foreign_cache = {}
        self._gap_oracle = None

    @staticmethod
    def chain_groups(pcb: PCB) -> dict[tuple[int, str], list[Any]]:
        """Routed segments grouped by ``(net_number, layer)``."""
        groups: dict[tuple[int, str], list[Any]] = defaultdict(list)
        for seg in pcb.segments:
            if seg.layer and seg.width > 0:
                groups[(seg.net_number, seg.layer)].append(seg)
        return groups

    def check(self, pcb: PCB, design_rules: DesignRules | None) -> DRCResults:
        results = DRCResults()
        results.rules_checked = 1
        for rid in (
            "redundant_copper_duplicate",
            "redundant_copper_fragments",
            "redundant_copper_detour",
            "redundant_copper_via_offcentre",
            "redundant_copper_covered",
            "redundant_copper_staircase",
        ):
            results.rules_checked_by_rule[rid] = 1
        # The pour-stub proof is opt-in; a skipped proof is a visible 0, not 1.
        results.rules_checked_by_rule["redundant_copper_pour_stub"] = 0

        segments = [s for s in pcb.segments if s.layer and s.width > 0]
        self.bind(pcb, design_rules)
        self._check_duplicates(segments, results)
        for segs in self.chain_groups(pcb).values():
            self._check_chains(segs, results)
            self._check_covered(segs, results)

        self._check_via_offcentre(pcb, segments, results)
        if self._prove_pour_stubs and getattr(pcb, "path", None) and pcb.zones:
            from kicad_tools.validate.pour_stub import prove_pour_stubs

            proof = prove_pour_stubs(pcb)
            results.rules_checked_by_rule["redundant_copper_pour_stub"] = int(proof is not None)
            if proof is not None and proof.partial:
                # Coverage stays 0/1 (a CI-side boolean); the partial case is
                # disclosed here so it never reads as a full proof (Issue #10211).
                results.add(
                    DRCViolation(
                        rule_id="redundant_copper_pour_stub",
                        severity="info",
                        message=(
                            f"Pour-stub proof evaluated {proof.evaluated} of {proof.total} "
                            "candidate branches."
                        ),
                        location=None,
                        layer="",
                    )
                )
            for stub, evidence in proof.verified if proof else ():
                seg = stub.segment
                ox, oy = self._origin
                start = f"({seg.start[0] + ox:.3f}, {seg.start[1] + oy:.3f})"
                end = f"({seg.end[0] + ox:.3f}, {seg.end[1] + oy:.3f})"
                items = [stub.pad_name, seg.uuid or f"{seg.layer} {start} to {end}"]
                via = ""
                if stub.via:
                    x, y = stub.via.position
                    via = f" and via at ({x + ox:.3f}, {y + oy:.3f})"
                    items.append(stub.via.uuid or via.strip())
                results.add(
                    DRCViolation(
                        rule_id="redundant_copper_pour_stub",
                        severity="warning",
                        message=(
                            f"Pad {stub.pad_name} [{stub.net}] remains connected directly "
                            f"through its refilled pour after removing the {seg.layer} track "
                            f"{start} to {end}{via}. All {len(evidence.terminal_uuids)} remaining "
                            f"terminals on this net stay connected."
                        ),
                        location=stub.location,
                        layer=seg.layer,
                        items=tuple(items),
                        nets=(stub.net,),
                    )
                )
        return results

    # ------------------------------------------------------------------

    @staticmethod
    def _net_label(seg: Any) -> str:
        return seg.net_name or (f"net {seg.net_number}" if seg.net_number else "unassigned")

    def _check_duplicates(self, segments: list[Any], results: DRCResults) -> None:
        seen: set[tuple[Any, ...]] = set()
        for seg in segments:
            a, b = _q(seg.start), _q(seg.end)
            key = (
                seg.net_number,
                seg.layer,
                (a, b) if a <= b else (b, a),
                round(seg.width, _QUANT),
            )
            if key not in seen:
                seen.add(key)
                continue
            label = self._net_label(seg)
            results.add(
                DRCViolation(
                    rule_id="redundant_copper_duplicate",
                    severity="warning",
                    message=(
                        f"Duplicate track segment: [{label}] on {seg.layer}, "
                        f"width {seg.width:.3f} mm (removable with `kct pcb dedupe`)"
                    ),
                    location=seg.start,
                    layer=seg.layer,
                    witnesses=(segment_witness(seg, self._origin),),
                    nets=(label,),
                )
            )

    def _check_covered(self, segs: list[Any], results: DRCResults) -> None:
        """Flag segments wholly lying on a longer collinear same-net segment."""
        for seg in segs:
            length = math.dist(seg.start, seg.end)
            for other in segs:
                if other is seg or other.width < seg.width - 1e-9:
                    continue
                # Strictly longer: equal-length containers are duplicates
                # (reported elsewhere) and cannot hide a longer segment.
                if math.dist(other.start, other.end) <= length + COVERED_TOL_MM:
                    continue
                if all(
                    _point_seg_dist(p, other.start, other.end) <= COVERED_TOL_MM
                    for p in (seg.start, seg.end)
                ):
                    label = self._net_label(seg)
                    results.add(
                        DRCViolation(
                            rule_id="redundant_copper_covered",
                            severity="warning",
                            message=(
                                f"Track segment lies within the copper of a longer "
                                f"segment: [{label}] on {seg.layer} "
                                f"(`kct pcb prune` removes this when safe)"
                            ),
                            location=seg.start,
                            layer=seg.layer,
                            witnesses=(segment_witness(seg, self._origin),),
                            nets=(label,),
                        )
                    )
                    break

    def _check_chains(self, segs: list[Any], results: DRCResults) -> None:
        """Fragment, detour and staircase detection over same-net/layer chains."""
        plain_edges, plain_nodes = self._endpoint_graph(segs)
        if not plain_edges:
            return
        visited: set[int] = set()
        for start_idx in range(len(plain_edges)):
            if start_idx not in visited:
                chain = self._walk_chain(start_idx, plain_edges, plain_nodes, visited)
                self._report_chain(chain, results)

        graph = self._junction_graph(segs)
        for chain in self._detour_chains(graph):
            self._report_detour(chain, results)
        self._report_staircases(graph, results, graph.terminals)

    def _report_staircases(
        self, graph: _ChainGraph, results: DRCResults, terminal_shapes: tuple[KShape, ...]
    ) -> None:
        """Report each chain's shortcut, else the pieces its overlap nodes cut
        it into (Issue #9848: never both)."""
        for chain in self._walk_graph(graph, graph.stops):
            first_seg = chain[0][0]
            label, layer = self._net_label(first_seg), first_seg.layer
            if self._report_staircase(chain, label, layer, results, terminal_shapes):
                continue
            if not any(step[2] in graph.overlap_nodes for step in chain[:-1]):
                continue
            piece: list[Any] = []
            for step in chain:
                piece.append(step)
                if step[2] in graph.overlap_nodes:
                    if len(piece) > 1:
                        self._report_staircase(piece, label, layer, results, terminal_shapes)
                    piece = []
            if len(piece) > 1:
                self._report_staircase(piece, label, layer, results, terminal_shapes)

    @staticmethod
    def _endpoint_graph(
        segs: list[Any],
    ) -> tuple[list[Any], dict[tuple[float, float], list[int]]]:
        """``(edges, node_edges)`` of the plain endpoint graph.

        Exact duplicates are dropped so they don't distort node degrees.  An
        edge is ``((a, b), segment)`` with ``a <= b``; ``node_edges`` maps a
        node to the indices of the edges meeting there.
        """
        unique: dict[tuple[Any, ...], Any] = {}
        for seg in segs:
            a, b = _q(seg.start), _q(seg.end)
            if a == b:
                continue
            unique.setdefault((a, b) if a <= b else (b, a), seg)
        edges = list(unique.items())
        node_edges: dict[tuple[float, float], list[int]] = defaultdict(list)
        for i, ((a, b), _s) in enumerate(edges):
            node_edges[a].append(i)
            node_edges[b].append(i)
        return edges, node_edges

    def _junction_graph(self, segs: list[Any]) -> _ChainGraph:
        """The endpoint graph with every junction a chain must stop at.

        Issue #9817: for the staircase shortcut a same-net pad or via is a
        chain terminal, never a pass-through vertex -- straightening across it
        would bypass and disconnect it.  Issue #9848: a node AT a terminal's
        centre stays a hard stop (``stops``); a node on the terminal's copper
        only by overlap (``overlap_nodes``) may be spanned when the
        replacement copper keeps contact with every terminal it spans, else
        the chain falls back to the pieces split at those nodes.

        Issue #10667: the same holds for same-net *tracks*.  A track landing
        mid-span on a chain segment, or touching a chain node by overlap, is a
        junction the endpoint graph never saw: Board 03's reported shortcut
        ran past such a landing and, once applied, left the landing track
        dangling.  :meth:`_junction_edges` cuts the edges there; the cuts are
        stops for the detour measurement too, so a run through a junction is
        never read as one long detour.
        """
        edges, node_edges = self._endpoint_graph(segs)
        if not edges:
            return _ChainGraph(edges, node_edges, frozenset(), frozenset(), ())
        edges, node_edges, junction_stops = self._junction_edges(edges, node_edges, segs)
        terminals = self._terminal_shapes(segs[0].net_number, segs[0].layer)
        centre_stops: set[tuple[float, float]] = set()
        overlap_nodes: set[tuple[float, float]] = set()
        for node, idxs in node_edges.items():
            if len(idxs) != 2:
                continue
            point = KVia(*node, diameter=0.0)
            centres = [centre for centre, shape in terminals if copper_gap(point, shape) <= 1e-6]
            if any(math.dist(node, centre) <= TERMINAL_CENTRE_EPS_MM for centre in centres):
                centre_stops.add(node)
            elif centres:
                overlap_nodes.add(node)
        return _ChainGraph(
            edges,
            node_edges,
            frozenset(junction_stops | centre_stops),
            frozenset(overlap_nodes),
            tuple(shape for _centre, shape in terminals),
        )

    @staticmethod
    def _junction_edges(
        edges: list[Any],
        node_edges: dict[tuple[float, float], list[int]],
        segments: list[Any],
    ) -> tuple[list[Any], dict[tuple[float, float], list[int]], set[tuple[float, float]]]:
        """Cut ``edges`` where a same-net track of this layer joins one mid-span.

        Returns ``(edges, node_edges, stops)``.  Every chain edge a track end
        lands on the centreline of is cut at that end's node -- the one clean
        track join, as ``kct pcb prune`` models it; an end merely touching the
        copper from beside is not a landing -- and each cut node is a stop.  So
        is a two-edge node whose copper a same-net track touches without
        ending there.  Pieces keep the landing segment, so a chain that ends
        on a piece still names it; the pieces of one segment are never in one
        chain.
        """
        cut: dict[int, list[tuple[float, tuple[float, float]]]] = {}
        for index, ((a, b), seg) in enumerate(edges):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length2 = dx * dx + dy * dy
            if length2 <= 0.0:
                continue
            points: dict[tuple[float, float], float] = {}
            for other in segments:
                if other is seg:
                    continue
                for end in (other.start, other.end):
                    u = ((end[0] - a[0]) * dx + (end[1] - a[1]) * dy) / length2
                    if not _LANDING_END_EPS < u < 1.0 - _LANDING_END_EPS:
                        continue
                    cx, cy = a[0] + u * dx, a[1] + u * dy
                    if math.hypot(end[0] - cx, end[1] - cy) > COVERED_TOL_MM:
                        continue
                    node = _q(end)
                    if node != a and node != b:
                        points[node] = u
            if points:
                cut[index] = sorted((u, node) for node, u in points.items())

        split: list[Any] = []
        for index, edge in enumerate(edges):
            (a, b), seg = edge
            chain_pts = [a, *[node for _u, node in cut.get(index, ())], b]
            split.extend(
                (((p, q), seg) if p <= q else ((q, p), seg))
                for p, q in zip(chain_pts, chain_pts[1:], strict=False)
                if p != q
            )
        rebuilt: dict[tuple[float, float], list[int]] = defaultdict(list)
        for i, ((a, b), _s) in enumerate(split):
            rebuilt[a].append(i)
            rebuilt[b].append(i)
        stops: set[tuple[float, float]] = {node for points in cut.values() for _u, node in points}
        # An overlap-held join: a same-net track's copper touches a two-edge
        # node without any of its own edges ending there.  The chain's own
        # neighbours don't count -- a track that also meets an adjacent node
        # is anchored there, and that node is a junction in its own right.
        for node, idxs in rebuilt.items():
            if len(idxs) != 2 or node in stops:
                continue
            first, second = split[idxs[0]][1], split[idxs[1]][1]
            (a1, b1), (a2, b2) = split[idxs[0]][0], split[idxs[1]][0]
            own = {_q(a1), _q(b1), _q(a2), _q(b2)}
            disc = KVia(*node, diameter=max(first.width or 0.0, second.width or 0.0))
            for other in segments:
                if other is first or other is second:
                    continue
                if any(_q(p) in own for p in (other.start, other.end)):
                    continue
                if copper_gap(disc, segment_shape(other)) <= _CONTACT_EPS_MM:
                    stops.add(node)
                    break
        return split, rebuilt, stops

    def _detour_chains(self, graph: _ChainGraph) -> list[list[Any]]:
        """Maximal runs between junctions: every hard stop, and every node a
        same-net pad or via spans only by overlap (a terminal all the same)."""
        return self._walk_graph(graph, graph.stops | graph.overlap_nodes)

    def _staircase_chains(self, graph: _ChainGraph) -> list[list[Any]]:
        """Every chain the staircase rule judges, whole first.

        A chain through an overlap node is tried whole (Issue #9848: legal
        when the replacement keeps every spanned terminal's contact), then as
        the pieces split at those nodes (the pre-#9848 findings).
        """
        chains = self._walk_graph(graph, graph.stops)
        whole = list(chains)
        for chain in whole:
            if not any(step[2] in graph.overlap_nodes for step in chain[:-1]):
                continue
            piece: list[Any] = []
            for step in chain:
                piece.append(step)
                if step[2] in graph.overlap_nodes:
                    if len(piece) > 1:
                        chains.append(piece)
                    piece = []
            if len(piece) > 1:
                chains.append(piece)
        return chains

    @staticmethod
    def _walk_graph(graph: _ChainGraph, stops: frozenset) -> list[list[Any]]:
        chains: list[list[Any]] = []
        visited: set[int] = set()
        for start_idx in range(len(graph.edges)):
            if start_idx in visited:
                continue
            chain = RedundantCopperRule._walk_chain(
                start_idx, graph.edges, graph.node_edges, visited, stops
            )
            if chain:
                chains.append(chain)
        return chains

    def _terminal_shapes(self, net: int, layer: str) -> list[tuple[tuple[float, float], KShape]]:
        """``(centre, copper shape)`` per same-net pad/via on ``layer``."""
        if not net:
            return []
        return [
            ((elem.geometry[0], elem.geometry[1]), elem.kernel_shape())
            for elem in self._foreign_elements(layer)
            if elem.net_number == net and elem.element_type in ("pad", "via")
        ]

    @staticmethod
    def _walk_chain(
        start_idx: int,
        edges: list[Any],
        node_edges: dict[tuple[float, float], list[int]],
        visited: set[int],
        terminals: set[tuple[float, float]] | frozenset[tuple[float, float]] = frozenset(),
    ) -> list[tuple[Any, tuple[float, float], tuple[float, float]]]:
        """Maximal path through degree-2 non-terminal nodes, as (segment, from, to) steps."""

        def extend(idx: int, frm: tuple[float, float]) -> list[Any]:
            out = []
            cur, node = idx, frm
            while True:
                (a, b), seg = edges[cur]
                nxt_node = b if node == a else a
                out.append((seg, node, nxt_node))
                others = [e for e in node_edges[nxt_node] if e != cur]
                if (
                    len(node_edges[nxt_node]) != 2
                    or nxt_node in terminals
                    or not others
                    or others[0] in visited
                ):
                    return out
                visited.add(others[0])
                cur, node = others[0], nxt_node

        visited.add(start_idx)
        (a, b), seg = edges[start_idx]
        forward = extend(start_idx, a)
        # Extend backwards from a (through the start edge's other end).
        backward: list[Any] = []
        if len(node_edges[a]) == 2 and a not in terminals:
            others = [e for e in node_edges[a] if e != start_idx]
            if others and others[0] not in visited:
                visited.add(others[0])
                backward = extend(others[0], a)
        back_steps = [(s, to, frm) for (s, frm, to) in reversed(backward)]
        return back_steps + forward

    def _report_chain(self, chain: list[Any], results: DRCResults) -> None:
        """Report the fragment runs of one plain (un-cut) chain.

        Kept as the hook a recipe subclasses to collect the chains that
        carry a run (board 04's ``merge_fragment_runs``, Issue #9097).
        Detours are no longer measured here: Issue #10667 moved them to the
        junction-bounded runs of :meth:`_report_detour`.
        """
        if not chain:
            return
        first_seg = chain[0][0]
        label = self._net_label(first_seg)
        layer = first_seg.layer

        # Fragment runs: consecutive short segments.
        run: list[Any] = []
        for step in [*chain, None]:
            seg = step[0] if step else None
            if seg is not None and math.dist(seg.start, seg.end) < FRAGMENT_MAX_MM:
                run.append(seg)
                continue
            if len(run) >= FRAGMENT_RUN_MIN:
                results.add(
                    DRCViolation(
                        rule_id="redundant_copper_fragments",
                        severity="warning",
                        message=(
                            f"Run of {len(run)} very short track segments "
                            f"(< {FRAGMENT_MAX_MM} mm each): [{label}] on {layer}"
                        ),
                        location=run[0].start,
                        layer=layer,
                        actual_value=float(len(run)),
                        required_value=float(FRAGMENT_RUN_MIN - 1),
                        witnesses=tuple(segment_witness(s, self._origin) for s in run),
                        nets=(label,),
                    )
                )
            run = []

    def _report_detour(self, chain: list[Any], results: DRCResults) -> None:
        """Detour: routed length vs straight distance between the run's ends.

        The run is walked between junctions (Issue #10667), so two conductors
        meeting at a tap are never read as one long way round.
        """
        if not chain:
            return
        first_seg = chain[0][0]
        label = self._net_label(first_seg)
        layer = first_seg.layer

        pts = self.chain_points(chain)
        routed = sum(math.dist(p, q) for p, q in zip(pts, pts[1:], strict=False))
        ends = (pts[0], pts[-1])
        straight = math.dist(*ends)
        if len(chain) >= 2 and routed >= DETOUR_MIN_LENGTH_MM and straight > 0:
            ratio = routed / straight
            if ratio > DETOUR_RATIO:
                results.add(
                    DRCViolation(
                        rule_id="redundant_copper_detour",
                        severity="warning",
                        message=(
                            f"Track detour: [{label}] on {layer} routes {routed:.2f} mm "
                            f"between points {straight:.2f} mm apart (ratio {ratio:.1f})"
                        ),
                        location=ends[0],
                        layer=layer,
                        actual_value=ratio,
                        required_value=DETOUR_RATIO,
                        witnesses=tuple(segment_witness(s, self._origin) for s, _, _ in chain),
                        nets=(label,),
                    )
                )

    @staticmethod
    def _step_line(step: tuple[Any, tuple[float, float], tuple[float, float]]) -> tuple[Any, Any]:
        """``(segment, (a, b))`` -- the centreline the walk traversed.

        A step on a whole segment is the segment itself; a step on one piece
        of a segment cut at a junction (Issue #10667) is just that piece's
        node-to-node line.  The other piece stays on the board whoever wins
        the shortcut, so it is never copper the shortcut may count as
        replaced or as its own contact.
        """
        seg, frm, to = step
        # The walk rounds its nodes to 1 um, so compare rounded keys, not
        # the segment's own (possibly 1 nm) coordinates.
        whole = (_q(seg.start), _q(seg.end))
        if (frm, to) in ((whole[0], whole[1]), (whole[1], whole[0])):
            return seg, (seg.start, seg.end)
        return seg, (frm, to)

    @classmethod
    def _step_shapes(cls, chain: Sequence[Any]) -> list[KSegment]:
        """The copper of each chain step, exactly as the walk traversed it."""
        shapes: list[KSegment] = []
        for step in chain:
            seg, (a, b) = cls._step_line(step)
            shapes.append(KSegment(a[0], a[1], b[0], b[1], seg.width))
        return shapes

    # -- staircase (Issue #8831) ------------------------------------------

    def _foreign_elements(self, layer: str) -> list[Any]:
        """Segment/via/pad copper on ``layer``, as clearance-kernel elements."""
        from .clearance import CopperElement

        if layer in self._foreign_cache:
            return self._foreign_cache[layer]
        pcb = self._pcb
        elems: list[Any] = []
        for seg in pcb.segments:
            if seg.layer == layer and seg.width > 0:
                elems.append(
                    CopperElement(
                        element_type="segment",
                        layer=layer,
                        net_number=seg.net_number,
                        geometry=(seg.start[0], seg.start[1], seg.end[0], seg.end[1], seg.width),
                        reference="",
                    )
                )
        for via in pcb.vias:
            if via_spans_layer(via.layers, layer):
                elems.append(
                    CopperElement(
                        element_type="via",
                        layer="*",
                        net_number=via.net_number,
                        geometry=(via.position[0], via.position[1], via.size, via.size),
                        reference="",
                        explicit_layers=tuple(via.layers),
                    )
                )
        for fp in getattr(pcb, "footprints", None) or []:
            for pad in getattr(fp, "pads", None) or []:
                pad_layers = getattr(pad, "layers", None) or []
                if layer in pad_layers or "*.Cu" in pad_layers:
                    elems.append(CopperElement.from_pad(pad, fp))
        self._foreign_cache[layer] = elems
        return elems

    def _path_clear(
        self,
        path: list[tuple[tuple[float, float], tuple[float, float]]],
        net: int,
        layer: str,
        width: float,
    ) -> bool:
        from .clearance import (
            CopperElement,
            _calculate_clearance,
            pair_clearance_tolerance_mm,
        )

        for a, b in path:
            cand = CopperElement(
                element_type="segment",
                layer=layer,
                net_number=net,
                geometry=(a[0], a[1], b[0], b[1], width),
                reference="",
            )
            for other in self._foreign_elements(layer):
                if other.net_number == net:
                    continue
                gap = _calculate_clearance(cand, other)[0]
                required = self._required_clearance(net, other.net_number)
                # Same pair rule as ``netclass_clearance_copper`` (Issue
                # #9848): an exact tie is legal copper, not float noise.
                if not drc_clear(gap, required, band_mm=pair_clearance_tolerance_mm(cand, other)):
                    return False
        return True

    def _pair_requirement(self, pcb: Any) -> Any:
        """``netclass_clearance_copper``'s pair requirement for this board.

        Issue #9817: a shortcut cleared only at the fab floor can land inside
        a ``Power`` class's clearance, so it is cleared at exactly the
        requirement that check enforces.  ``None`` means the project declares
        classes the resolver cannot model: no shortcut is then provably legal.
        """
        from kicad_tools.core.project_clearance import ProjectClearanceError

        from .netclass_clearance_copper import netclass_pair_requirement

        path = getattr(pcb, "path", None)
        if path is None or not hasattr(pcb, "nets"):
            return lambda _a, _b: self._clearance
        try:
            return netclass_pair_requirement(path, pcb, self._clearance)
        except ProjectClearanceError:
            return None

    def _required_clearance(self, net_a: int, net_b: int) -> float:
        requirement = self._requirement
        assert requirement is not None
        return max(float(self._clearance), float(requirement(net_a, net_b)))

    def _slit_oracle(self) -> Any | None:
        """The physical-gap slit oracle when a policy is active (Issue #9862).

        ``None`` when no threshold or fab policy was supplied: the
        physical-gap pass itself is inactive then, so the staircase gate is
        too.  Constructed lazily -- the oracle collects the board's copper
        sources once, so a board with no staircase candidates never pays.
        """
        if self._gap_mm is None and self._gap_policy is None:
            return None
        if self._gap_oracle is None:
            from .physical_gap import StaircaseSlitOracle

            self._gap_oracle = StaircaseSlitOracle(self._pcb, self._gap_mm, self._gap_policy)
        return self._gap_oracle

    def _mask_zones(self, side: str) -> list[_MaskZone]:
        """One ``_MaskZone`` per pad opening on mask ``side``.

        The opening is ``mask_geometry``'s (the shape ``mask_to_copper``'s
        source model builds: pad copper grown by its effective expansion),
        grown by the mask-to-copper policy clearance.  A pad that model does
        not support (custom/chamfered) uses its clearance-kernel copper outline
        instead, so it still keeps a zone rather than being skipped.
        """
        if side in self._mask_cache:
            return self._mask_cache[side]
        from ..mask_geometry import (
            ADVANCED_PAD_FEATURES,
            _buffer,
            board_mask_margin,
            pad_mask_margin,
            pad_mask_shapes,
            pad_mask_sides,
        )
        from .clearance import CopperElement

        pcb = self._pcb
        board_margin, declared = board_mask_margin(pcb)
        zones: list[_MaskZone] = []
        for fp in getattr(pcb, "footprints", None) or []:
            for pad in getattr(fp, "pads", None) or []:
                if side[0] not in pad_mask_sides(pad):
                    continue
                margin, _src = pad_mask_margin(pad, fp, board_margin or 0.0, declared)
                if margin is None or not math.isfinite(margin):
                    margin = 0.0
                node = getattr(pad, "_sexp_node", None)
                advanced = node is not None and any(
                    node.find_child(name) is not None for name in ADVANCED_PAD_FEATURES
                )
                try:
                    if advanced:
                        raise ValueError("advanced pad")
                    copper, opening = pad_mask_shapes(pad, fp, margin)
                except ValueError:
                    copper = CopperElement.from_pad(pad, fp).polygon
                    if copper is None:
                        continue
                    opening = _buffer(copper, margin)
                zones.append(
                    _MaskZone(
                        zone=_buffer(opening, self.mask_clearance_mm),
                        copper=copper,
                        net=pad.net_number,
                        opening=opening,
                        margin=margin,
                        kind=_pad_kind(pad),
                    )
                )
        self._mask_cache[side] = zones
        return zones

    def _mask_clear(
        self,
        cand: list[tuple[float, float]],
        chain: Sequence[Any],
        layer: str,
        width: float,
        net: int,
    ) -> bool:
        """No new copper of ``cand`` in a pad's mask-opening zone (Issue #9817).

        "New" is the candidate's copper beyond the chain it replaces; copper
        over a same-net pad's own land is that pad's exposed copper, not new
        exposure.  A same-net pad additionally accepts one clean entry of the
        whole proposed path (Issue #9848): the criteria ``mask_to_copper``'s
        ``within_own_pad`` class applies, so an entry that check scores
        ``info`` is not a reason to withhold the shortcut.  Inner layers
        carry no mask.
        """
        side = _MASK_SIDE.get(layer)
        if side is None:
            return True
        zones = self._mask_zones(side)
        if not zones:
            return True
        from shapely.geometry import LineString  # type: ignore[import-untyped]
        from shapely.ops import unary_union  # type: ignore[import-untyped]

        new = LineString(cand).buffer(width / 2.0)
        old = unary_union(
            [
                LineString([a, b]).buffer(seg.width / 2.0)
                for seg, (a, b) in (self._step_line(step) for step in chain)
            ]
        )
        new = new.difference(old)
        if new.area <= STAIRCASE_MASK_AREA_TOL_MM2:
            return True
        for pad in zones:
            if not pad.zone.intersects(new):
                continue
            exposed = pad.zone.intersection(new)
            if net and pad.net == net:
                exposed = exposed.difference(pad.copper)
            if exposed.area <= STAIRCASE_MASK_AREA_TOL_MM2:
                continue
            if net and pad.net == net and self._own_pad_entry(pad, cand, width):
                continue
            return False
        return True

    def _own_pad_entry(self, pad: _MaskZone, cand: list[tuple[float, float]], width: float) -> bool:
        """Whether the proposed path is one clean entry into the own pad.

        Issue #9848: reuses ``mask_to_copper``'s ``within_own_pad`` criteria,
        per candidate leg -- each leg becomes its own conductor on the
        rewritten board, measured on its own copper, width and authored
        endpoints.  The clipped sliver of new copper alone cannot establish
        a legal entry, and an owner kind or geometry the class does not
        admit keeps the conservative rejection.  A leg clear of the zone
        (beyond the policy reach) is not a measurement and never rejects.
        """
        from shapely.geometry import LineString

        from ..mask_copper import _within_own_pad

        reach = self.mask_clearance_mm + _MASK_UNCERTAINTY_MM
        for a, b in zip(cand, cand[1:], strict=False):
            copper = LineString([a, b]).buffer(width / 2.0)
            if pad.opening.distance(copper) > reach:
                continue
            if not _within_own_pad(
                pad.opening,
                copper,
                pad.copper,
                {"kind": pad.kind},
                {"kind": "segment", "width_mm": width, "endpoints_mm": (a, b)},
                pad.margin,
                self.mask_clearance_mm,
                _MASK_UNCERTAINTY_MM,
            )[0]:
                return False
        return True

    @staticmethod
    def _bend_count(points: list[tuple[float, float]]) -> int:
        bends = 0
        for p, q, r in zip(points, points[1:], points[2:], strict=False):
            cross = (q[0] - p[0]) * (r[1] - q[1]) - (q[1] - p[1]) * (r[0] - q[0])
            dot = (q[0] - p[0]) * (r[0] - q[0]) + (q[1] - p[1]) * (r[1] - q[1])
            if math.hypot(cross, dot) > 1e-9 and abs(math.atan2(cross, dot)) > math.radians(1.0):
                bends += 1
        return bends

    @staticmethod
    def chain_points(chain: Sequence[Any]) -> list[tuple[float, float]]:
        """The polyline a chain's steps traverse, end to end."""
        return [chain[0][1], *[step[2] for step in chain]]

    def _is_staircase(self, chain: Sequence[Any]) -> bool:
        """Whether ``chain`` has a bend to save at all (Issue #8831)."""
        if len(chain) < STAIRCASE_MIN_SEGMENTS:
            return False
        pts = self.chain_points(chain)
        straight = math.dist(pts[0], pts[-1])
        if straight < 1e-6:
            return False
        # Monotone in both axes: a serpentine / fold-back (length tuning,
        # #8603) reverses an axis and is never a staircase.
        for axis in (0, 1):
            deltas = [q[axis] - p[axis] for p, q in zip(pts, pts[1:], strict=False)]
            if any(d > 1e-6 for d in deltas) and any(d < -1e-6 for d in deltas):
                return False
        routed = sum(math.dist(p, q) for p, q in zip(pts, pts[1:], strict=False))
        if routed / straight > DETOUR_RATIO:
            return False  # redundant_copper_detour's territory
        return self._bend_count(pts) >= 2

    @staticmethod
    def shortcut_candidates(pts: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
        """The fewer-bend paths between a chain's ends, preferred first.

        One straight path when the ends share an axis or a 45-degree line;
        else the 45-degree leg followed by the H/V leg, then the other order.
        """
        start, end = pts[0], pts[-1]
        dx, dy = end[0] - start[0], end[1] - start[1]
        diag = min(abs(dx), abs(dy))
        mid_d = (start[0] + math.copysign(diag, dx), start[1] + math.copysign(diag, dy))
        mid_o = (end[0] - math.copysign(diag, dx), end[1] - math.copysign(diag, dy))
        if diag < 1e-6 or math.dist(mid_d, end) < 1e-6:
            return [[start, end]]
        return [[start, mid_d, end], [start, mid_o, end]]

    @classmethod
    def _step_segments(cls, chain: Sequence[Any]) -> list[Any]:
        """One segment per step: the segment itself, or the piece of it the
        step traverses when a junction cut it (Issue #10667).

        The slit oracle then removes exactly the copper the shortcut
        replaces and judges the rest of the landing segment as copper that
        stays.
        """
        from kicad_tools.schema.pcb import Segment

        out: list[Any] = []
        for step in chain:
            seg, (a, b) = cls._step_line(step)
            if (a, b) == (seg.start, seg.end):
                out.append(seg)
            else:
                out.append(
                    Segment(
                        start=a,
                        end=b,
                        width=seg.width,
                        layer=seg.layer,
                        net_number=seg.net_number,
                        net_name=seg.net_name,
                    )
                )
        return out

    def shortcut_reason(
        self,
        chain: Sequence[Any],
        cand: list[tuple[float, float]],
        terminal_shapes: Sequence[KShape],
        width: float | None = None,
    ) -> str | None:
        """Why ``cand`` must not replace ``chain`` as copper; ``None`` when it is legal.

        The gates :meth:`_report_staircase` applies to the copper itself
        (Issues #9817 / #9848 / #9862): clearance at the pair requirement,
        every spanned terminal still touched, no new copper in a mask-opening
        zone, no slit the active physical-gap policy reports.  Whether the
        path keeps the chain's taps and opens no acute corner is
        :meth:`option_reason`, which calls this first.
        """
        if self._requirement is None:
            return "netclass_unresolved"  # net classes cannot be modelled
        if self._bend_count(cand) >= self._bend_count(self.chain_points(chain)):
            return "no_bend_saved"
        first = chain[0][0]
        net, layer = first.net_number, first.layer
        if width is None:
            width = max(seg.width for seg, _f, _t in chain)
        old = self._step_shapes(chain)
        contacts = [
            terminal
            for terminal in terminal_shapes
            if any(copper_gap(segment, terminal) <= _CONTACT_EPS_MM for segment in old)
        ]
        legs = list(zip(cand, cand[1:], strict=False))
        # Every pad or via touched by the original copper must still meet the
        # replacement, including contacts along a segment or at its edge.
        if contacts and not self._contacts_kept(legs, width, contacts):
            return "terminal_contact_lost"
        if not self._path_clear(legs, net, layer, width):
            return "would_violate_clearance"
        if not self._mask_clear(cand, chain, layer, width, net):
            return "new_copper_in_mask_zone"
        # Issue #9862: the replacement must not open a copper slit the active
        # physical-gap policy reports (same-net branches and zone fills
        # included).
        oracle = self._slit_oracle()
        if oracle is not None and oracle.rejects(layer, self._step_segments(chain), cand, width):
            return "would_open_copper_slit"
        return None

    @classmethod
    def chain_options(cls, chain: list[Any]) -> list[_ChainOption]:
        """The ways ``chain`` can be replaced, preferred first (Issue #10667).

        A chain the walk ended at a junction sits on a cut segment whose other
        piece stays.  *Trimming* that segment at the junction keeps its copper
        but makes the junction an explicit vertex -- and a tap meeting the new
        leg at 45 degrees is then a ``sharp_trace_corner`` it never was while
        it sat mid-span.  *Replacing* the whole segment instead runs the new
        leg on through the tap, which stays mid-span, when the leg's line
        passes it.  Each cut end offers both, fewest trims first; a whole end
        segment has the one reading.
        """
        pts = cls.chain_points(chain)
        ends: list[tuple[Any, tuple[float, float], tuple[float, float]] | None] = []
        for index, inner_index in ((0, 1), (-1, -2)):
            seg, (a, b) = cls._step_line(chain[index])
            if (a, b) == (seg.start, seg.end):
                ends.append(None)
                continue
            far = max((seg.start, seg.end), key=lambda p: math.dist(p, pts[inner_index]))
            ends.append((seg, far, pts[index]))
        treatments = [["replace", "trim"] if end is not None else ["keep"] for end in ends]
        combos = sorted(
            ((a, b) for a in treatments[0] for b in treatments[1]),
            key=lambda how: sum(treatment == "trim" for treatment in how),
        )
        options: list[_ChainOption] = []
        for how in combos:
            steps = list(chain)
            ends_out: list[tuple[float, float]] = []
            landings: list[tuple[tuple[float, float], bool]] = []
            for index, treatment in ((0, how[0]), (-1, how[1])):
                end = ends[0 if index == 0 else 1]
                if end is None:
                    ends_out.append(pts[index])
                    continue
                seg, far, node = end
                if treatment == "replace":
                    other = seg.start if _q(far) == _q(seg.end) else seg.end
                    steps[index] = (seg, far, other)
                    ends_out.append(far)
                    landings.append((node, index == 0))
                else:
                    ends_out.append(node)
            options.append(
                _ChainOption(
                    steps=tuple(steps),
                    ends=(ends_out[0], ends_out[1]),
                    landings=tuple(landings),
                    trims=(how[0] == "trim", how[1] == "trim"),
                )
            )
        return options

    def option_reason(
        self,
        chain: Sequence[Any],
        option: _ChainOption,
        cand: list[tuple[float, float]],
        terminal_shapes: Sequence[KShape],
        width: float | None = None,
    ) -> str | None:
        """Why ``cand`` must not replace ``chain`` the way ``option`` reads it.

        :meth:`shortcut_reason` on the copper, then what a junction demands: a
        tap the chain was cut at must stay on the replacement (mid-span when
        the whole cut segment goes), and no vertex of the replacement may be
        a ``sharp_trace_corner``.  Without the second gate the rule asks for a
        rewrite ``kct check`` then reports: Board 03's JOY_X trunk ends at a
        tap it meets at 45 degrees.
        """
        why = self.shortcut_reason(option.steps, cand, terminal_shapes, width)
        if why is not None:
            return why
        for node, at_first in option.landings:
            a, b = (cand[0], cand[1]) if at_first else (cand[-2], cand[-1])
            if _point_seg_dist(node, a, b) > _COVERED_TOL:
                return "tap_off_replacement"
            if not _COVERED_TOL < _along(node, a, b) < 1.0 - _COVERED_TOL:
                return "tap_on_replacement_vertex"
        return self.corner_reason(chain, option, cand, width)

    def corner_reason(
        self,
        chain: Sequence[Any],
        option: _ChainOption,
        cand: list[tuple[float, float]],
        width: float | None = None,
    ) -> str | None:
        """The ``sharp_trace_corner`` the replacement would open, else ``None``.

        Every vertex of ``cand`` is judged with that rule's own test against
        the copper the board has there once the edit is applied: the chain's
        segments gone, a trimmed end segment ending at its junction, the new
        legs present.
        """
        from kicad_tools.schema.pcb import Segment

        from .sharp_trace_corner import sharp_vertex

        first = chain[0][0]
        if width is None:
            width = max(seg.width for seg, _f, _t in chain)
        added = [
            Segment(start=a, end=b, width=width, layer=first.layer, net_number=first.net_number)
            for a, b in zip(cand, cand[1:], strict=False)
        ]
        pts = self.chain_points(chain)
        for index, trims in ((0, option.trims[0]), (-1, option.trims[1])):
            if not trims:
                continue
            seg = chain[index][0]
            node = option.ends[0 if index == 0 else 1]
            kept = max(
                (seg.start, seg.end), key=lambda p: math.dist(p, pts[1 if index == 0 else -2])
            )
            added.append(
                Segment(
                    start=kept,
                    end=node,
                    width=seg.width,
                    layer=first.layer,
                    net_number=first.net_number,
                )
            )
        for node in cand:
            found = sharp_vertex(
                self._pcb,
                first.net_name,
                first.layer,
                node,
                added=added,
                removed=[seg for seg, _f, _t in chain],
            )
            if found is not None:
                return f"sharp_corner_{found[0]:.0f}deg"
        return None

    def _report_staircase(
        self,
        chain: list[Any],
        label: str,
        layer: str,
        results: DRCResults,
        terminal_shapes: Sequence[KShape],
    ) -> bool:
        """Report one staircase shortcut; ``True`` when one was reported."""
        if not self._is_staircase(chain):
            return False
        pts = self.chain_points(chain)
        start, end = pts[0], pts[-1]
        bends = self._bend_count(pts)
        width = max(s.width for s, _f, _t in chain)
        for option in self.chain_options(chain):
            for cand in self.shortcut_candidates(list(option.ends)):
                if self.option_reason(chain, option, cand, terminal_shapes, width) is not None:
                    continue  # a rejected bend falls through to the next one
                new_bends = self._bend_count(cand)
                ox, oy = self._origin
                via_pts = " -> ".join(f"({x + ox:.2f}, {y + oy:.2f})" for x, y in cand)
                results.add(
                    DRCViolation(
                        rule_id="redundant_copper_staircase",
                        severity="warning",
                        message=(
                            f"Track staircase: [{label}] on {layer} uses {len(chain)} segments "
                            f"({bends} bends) from ({start[0] + ox:.2f}, {start[1] + oy:.2f}) to "
                            f"({end[0] + ox:.2f}, {end[1] + oy:.2f}); a clear path with {new_bends} "
                            f"bend(s) saves {bends - new_bends}: {via_pts} "
                            f"(`kct optimize-traces` rewrites this)"
                        ),
                        location=start,
                        layer=layer,
                        actual_value=float(bends),
                        required_value=float(new_bends),
                        witnesses=tuple(segment_witness(s, self._origin) for s, _f, _t in chain),
                        nets=(label,),
                    )
                )
                return True
        return False

    def chain_for(self, witnesses: Sequence[str], kind: str = "staircase") -> list[Any] | None:
        """The chain a staircase / detour finding's ``witnesses`` name, in walk
        order, on the bound board; ``None`` when no chain matches.

        The redundant-copper repair (Issue #10667) re-derives its chains this
        way so it rewrites exactly the copper the finding named -- including a
        piece of a segment a junction cut.
        """
        pcb = self._pcb
        origin = self._origin
        wanted = tuple(witnesses)
        if not wanted:
            return None
        groups = self.chain_groups(pcb)
        first = next((s for s in pcb.segments if segment_witness(s, origin) == wanted[0]), None)
        if first is None:
            return None
        graph = self._junction_graph(groups[(first.net_number, first.layer)])
        chains = self._detour_chains(graph) if kind == "detour" else self._staircase_chains(graph)
        for chain in chains:
            if tuple(segment_witness(seg, origin) for seg, _f, _t in chain) == wanted:
                return chain
        return None

    @staticmethod
    def _contacts_kept(
        legs: list[tuple[tuple[float, float], tuple[float, float]]],
        width: float,
        shapes: list[KShape],
    ) -> bool:
        """Every spanned terminal still touched by the replacement copper."""
        replacement = [KSegment(a[0], a[1], b[0], b[1], width) for a, b in legs]
        return all(
            any(copper_gap(segment, shape) <= _CONTACT_EPS_MM for segment in replacement)
            for shape in shapes
        )

    def _check_via_offcentre(self, pcb: PCB, segments: list[Any], results: DRCResults) -> None:
        # Endpoint multiplicity per (net, layer, point): an end shared with
        # another segment is a normal joint (e.g. a bend inside the via
        # entry), not a track that connects "by overlap only".
        ends: dict[tuple[int, str, tuple[float, float]], int] = defaultdict(int)
        for seg in segments:
            ends[(seg.net_number, seg.layer, _q(seg.start))] += 1
            ends[(seg.net_number, seg.layer, _q(seg.end))] += 1

        for via in pcb.vias:
            vx, vy = via.position
            candidates = [
                s
                for s in segments
                if s.net_number == via.net_number and via_spans_layer(via.layers, s.layer)
            ]
            # Layers where a track already terminates on the via centre are
            # properly connected; skip off-centre ends on those layers.
            centred_layers = {
                s.layer
                for s in candidates
                for end in (s.start, s.end)
                if math.dist(end, (vx, vy)) < OFFCENTRE_MIN_MM / 10
            }
            for seg in candidates:
                if seg.layer in centred_layers:
                    continue
                for end in (seg.start, seg.end):
                    d = math.dist(end, (vx, vy))
                    if not OFFCENTRE_MIN_MM <= d <= OFFCENTRE_MAX_MM:
                        continue
                    if ends[(seg.net_number, seg.layer, _q(end))] > 1:
                        continue
                    label = self._net_label(seg)
                    results.add(
                        DRCViolation(
                            rule_id="redundant_copper_via_offcentre",
                            severity="warning",
                            message=(
                                f"Track end {d:.3f} mm off via centre: [{label}] "
                                f"on {seg.layer} (connects by overlap only)"
                            ),
                            location=end,
                            layer=seg.layer,
                            actual_value=d,
                            witnesses=(
                                segment_witness(seg, self._origin),
                                getattr(via, "uuid", "") or via_ref(via, self._origin),
                            ),
                            nets=(label,),
                        )
                    )
