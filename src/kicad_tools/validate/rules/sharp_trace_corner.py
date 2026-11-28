"""Sharp (acute) trace corner DRC rule (Issue #8840).

JLCDFM flags acute trace corners ("acid traps") that ``kct check`` never
reported: the only detector was the ``explain``-mode
:class:`~kicad_tools.explain.checks.acid_trap.AcidTrapCheck`. This rule uses
its angle calculation at shared endpoints and at branch endpoints along
continuous tracks (issue #10675).

Threshold: :data:`~kicad_tools.explain.checks.acid_trap.MIN_TRACE_ANGLE_DEG`
(90 deg, less ``ANGLE_TOLERANCE_DEG``).  **Unverified** -- this value was not
traced against a JLCDFM report or a fab capability page (retrieved
2026-11-12: none consulted); it is the repo's existing acid-trap limit.

Calibration (issue #8840, field data from the board behind epic #8838): JLC's
DFM tool flagged three vertices, all two-segment 45 deg fold-backs on 0.2/0.3 mm
tracks away from pads and vias.  The rule reports all three (pinned in
``tests/test_validate_sharp_trace_corner.py``).  On that board JLC flagged none
of the 95 joints that were branch points, at vias or inside pads; vertices
under same-net pad/via copper are skipped here for that reason, and because
there is no open wedge in the copper.  "Under" means inside the pad's real
copper outline, not just its inscribed circle (issue #9037).  Branch
junctions with an open wedge are still reported (operator ruling on PR #8848, 2026-11-12): this is stricter than
the observed fab behaviour, not looser.  Eight unflagged two-segment bends on
that board look like the flagged three; they are unknown, not negatives, so
they were not used to narrow the rule.  The angle threshold was not changed.

Sub-types (issue #9037): every finding carries ``corner_class`` --
``"bend"`` when exactly two track directions meet at the vertex (the
population JLCDFM was observed to flag) or ``"junction"`` when three or
more do.  A direction is a distinct bearing leaving the vertex
(``_TraceJunction.leg_count``), not a ``(segment ...)`` element: a
continuous track contributes a direction on each side of a tap, and
overlapping same-bearing tracks -- a stacked duplicate, or a shorter track
lying on a longer one -- count as one direction (issue #10675).  Reporting
is unchanged -- the tag only makes the populations separable, so a consumer
can gate on the fab-equivalent two-direction subset.  Because duplicates
count once, a duplicated bend is not misclassified as a junction.  The
``junction`` message names that count as ``"N-way junction"``
(issue #10704), so ``N`` can exceed the number of segments ending at the
vertex.  The fab-DFM parity table attributes only the bend population to
JLCDFM's category (``sharp_trace_corner#bend``).

Inner layers (issue #9037 decision): in scope.  The fab's three flagged
vertices on the consumer board were all on outer layers (F.Cu/B.Cu), so
inner-layer behaviour was never observed from the fab either way; the rule
still reports inner-layer acute corners like outer ones because the angle
geometry of an acid trap is layer-independent and the findings are
warnings (a plain ``kct check`` still exits 0).  The same-net through-via
burial exemption applies on inner layers via :func:`_covers_layer`.

Rule ID generated:
    - sharp_trace_corner: two same-net, same-layer segments meet at an angle
      below the limit, away from same-net pad/via copper on that layer.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from itertools import combinations
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.explain.checks.acid_trap import AcidTrapCheck
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Segment

SHARP_TRACE_CORNER_RULE_ID = "sharp_trace_corner"


def _copper_discs(pcb: PCB) -> list[tuple[str, tuple[str, ...], float, float, float, Any]]:
    """``(net_name, layers, x, y, radius, outline)`` of pad/via copper that can bury a vertex.

    A pad's ``outline`` is its true copper shape (the clearance kernel's
    polygon, roundrect/oval honoured), so a vertex anywhere inside the pad
    counts, not only one inside its inscribed circle (issue #9037: a vertex
    inside an elongated pad but off its centre was reported).  When no outline
    is available the inscribed circle (half the smaller dimension) is the
    fallback, so a vertex is only treated as buried when it is certainly
    under copper.  Vias are discs (``outline`` is ``None``).
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    from .clearance import CopperElement

    discs: list[tuple[str, tuple[str, ...], float, float, float, Any]] = []
    # ``getattr``: rule tests drive this with duck-typed boards that carry
    # tracks and vias but no footprints.
    for fp in getattr(pcb, "footprints", None) or []:
        for pad in fp.pads:
            dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            try:
                outline = CopperElement.from_pad(pad, fp).polygon
            except Exception:  # unmodelled pad shape: inscribed circle only
                outline = None
            discs.append(
                (
                    pad.net_name,
                    tuple(pad.layers),
                    fp.position[0] + dx,
                    fp.position[1] + dy,
                    min(pad.size) / 2,
                    outline,
                )
            )
    for via in pcb.vias:
        discs.append(
            (via.net_name, tuple(via.layers), via.position[0], via.position[1], via.size / 2, None)
        )
    return discs


def _covers_layer(d_layers: tuple[str, ...], layer: str) -> bool:
    """True when pad/via copper with ``d_layers`` exists on ``layer``.

    A through-via (outer layers listed) or a ``*.Cu`` through-hole pad spans
    every copper layer of the stack, including inner ones.
    """
    if layer in d_layers or "*.Cu" in d_layers:
        return True
    return "F.Cu" in d_layers and "B.Cu" in d_layers and layer.endswith(".Cu")


def _is_buried(discs, net_name: str, layer: str, point: tuple[float, float]) -> bool:
    candidate = None
    for d_net, d_layers, x, y, r, outline in discs:
        if d_net != net_name:
            continue
        if not _covers_layer(d_layers, layer):
            continue
        if math.dist(point, (x, y)) <= r:
            return True
        if outline is not None and not outline.is_empty:
            if candidate is None:
                from shapely.geometry import Point  # type: ignore[import-untyped]

                candidate = Point(point)
            if outline.covers(candidate):
                return True
    return False


_JUNCTION_TOL = 0.01  # mm, matches AcidTrapCheck._find_junction


def _far_end(seg, junction: tuple[float, float]) -> tuple[float, float]:
    if (
        abs(seg.start[0] - junction[0]) < _JUNCTION_TOL
        and abs(seg.start[1] - junction[1]) < _JUNCTION_TOL
    ):
        return (float(seg.end[0]), float(seg.end[1]))
    return (float(seg.start[0]), float(seg.start[1]))


def _point_segment_dist(p, a, b) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / len2))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def _wedge_is_open(seg1, seg2, junction: tuple[float, float], angle_deg: float) -> bool:
    """Whether copper outlines of the two legs leave an unfilled acute notch.

    Judged from the copper outline, not the centrelines: the facing edges of
    the legs only separate at ``s0 = (w1 + w2) / (4 sin(theta / 2))`` along the
    legs, so a leg shorter than that (a width-filled hairpin) leaves solid
    copper, and a thin leg lying entirely inside a wider one adds no notch.

    The router's ``acute_foldback.wedge_is_open`` is a second copy of this
    predicate; ``tests/router/test_acute_foldback_parity_8930.py`` drives both
    over one shared geometry table, so a change to either exemption fails there.
    """
    half = math.radians(angle_deg) / 2
    sin_half = math.sin(half)
    if sin_half <= 0.0:
        # Collinear doubling-back: no wedge to be open.  Matches
        # ``acute_foldback.wedge_is_open`` (Issue #8930).
        return False
    s0 = (seg1.width + seg2.width) / (4 * sin_half)
    p1, p2 = _far_end(seg1, junction), _far_end(seg2, junction)
    if min(math.dist(junction, p1), math.dist(junction, p2)) <= s0:
        return False
    for thin, wide in ((seg1, seg2), (seg2, seg1)):
        margin = wide.width / 2 - thin.width / 2
        if margin >= 0 and all(
            _point_segment_dist(pt, wide.start, wide.end) <= margin for pt in (thin.start, thin.end)
        ):
            return False
    return True


def _nearby_pairs(segments: list[Segment]) -> Iterator[tuple[int, int]]:
    from ..spatial import candidate_pairs

    bounds = []
    for seg in segments:
        ax, ay = seg.start
        bx, by = seg.end
        bounds.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)))
    return candidate_pairs(bounds, _JUNCTION_TOL)


@dataclass
class _TraceJunction:
    """Incident copper at one vertex, grouped by direction and width."""

    location: tuple[float, float]
    legs: dict[tuple[float, float], Segment] = field(default_factory=dict)

    def add_leg(self, seg: Segment, end: tuple[float, float]) -> None:
        dx, dy = end[0] - self.location[0], end[1] - self.location[1]
        length = math.hypot(dx, dy)
        if max(abs(dx), abs(dy)) < _JUNCTION_TOL:
            return
        # Overlapping tracks of the same width on one ray contribute their
        # full extent. A shorter duplicate must not hide an open wedge.
        bearing = round(math.degrees(math.atan2(dy, dx)), 6) % 360
        key = (bearing, seg.width)
        current = self.legs.get(key)
        if current is None or length > math.dist(self.location, current.end):
            self.legs[key] = replace(seg, start=self.location, end=end)

    @property
    def leg_count(self) -> int:
        return len({bearing for bearing, _width in self.legs})

    def open_angles(self, check: AcidTrapCheck) -> Iterator[float]:
        from kicad_tools.explain.checks.acid_trap import is_acute_corner_angle

        for first, second in combinations(self.legs.values(), 2):
            angle = check._calculate_angle(first, second, self.location)
            if (
                angle is not None
                and is_acute_corner_angle(angle)
                and _wedge_is_open(first, second, self.location, angle)
            ):
                yield angle


def _junctions(pcb: PCB, check: AcidTrapCheck) -> Iterator[tuple[str, str, _TraceJunction]]:
    """Find same-net, same-layer contacts, including taps on continuous tracks."""
    groups: dict[tuple[int, str, str], list[Segment]] = defaultdict(list)
    for seg in pcb.segments:
        net = pcb.get_net(seg.net_number)
        name = seg.net_name or (net.name if net else f"Net {seg.net_number}")
        groups[(seg.net_number, name, seg.layer)].append(seg)
    for (_, name, layer), segments in groups.items():
        for junction in _group_junctions(segments, check):
            yield name, layer, junction


def _group_junctions(segments: list[Segment], check: AcidTrapCheck) -> Iterator[_TraceJunction]:
    from ...geometry.copper import collinear_overlap_length
    from ...router.connectivity import _UnionFind

    vertices: dict[tuple[float, float], tuple[tuple[float, float], set[int]]] = {}
    runs = _UnionFind()
    for i, j in _nearby_pairs(segments):
        first, second = segments[i], segments[j]
        if (
            first.width == second.width
            and collinear_overlap_length(first.start, first.end, second.start, second.end)
            is not None
        ):
            runs.union(i, j)
        for point in _contacts(first, second, check):
            # Same location precision as the sharp-corner finding identity.
            key = (round(point[0], 2), round(point[1], 2))
            _, incident = vertices.setdefault(key, (point, set()))
            incident.update((i, j))

    # A split close to a branch does not shorten its physical wedge. Use
    # the extent of each continuous, same-width run, but retain its actual
    # contact sites (including crossings with explicit vertices).
    ends: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for i, seg in enumerate(segments):
        ends[runs.find(i)].extend((seg.start, seg.end))
    spans = {}
    for root, points in ends.items():
        a, b = segments[root].start, segments[root].end
        axis = 0 if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else 1
        spans[root] = min(points, key=itemgetter(axis)), max(points, key=itemgetter(axis))
    for point, incident in vertices.values():
        joint = _TraceJunction(point)
        for i in sorted(incident):
            for end in spans[runs.find(i)]:
                joint.add_leg(segments[i], end)
        yield joint


def _contacts(
    first: Segment, second: Segment, check: AcidTrapCheck
) -> Iterator[tuple[float, float]]:
    shared = check._find_junction(first, second)
    if shared is not None:
        yield shared
    for carrier, branch in ((first, second), (second, first)):
        ax, ay = carrier.start
        dx, dy = carrier.end[0] - ax, carrier.end[1] - ay
        length_sq = dx * dx + dy * dy
        tolerance = _JUNCTION_TOL * math.sqrt(length_sq)
        for px, py in (branch.start, branch.end):
            x, y = px - ax, py - ay
            along, across = x * dx + y * dy, x * dy - y * dx
            # Dot and cross products divided by length give the distances
            # along and across the carrier. Strict interior excludes its
            # endpoints and zero-length tracks.
            if tolerance < along < length_sq - tolerance and abs(across) < tolerance:
                t = along / length_sq
                yield ax + t * dx, ay + t * dy


def sharp_vertex(
    pcb: PCB,
    net_name: str,
    layer: str,
    point: tuple[float, float],
    *,
    added: Sequence[Any] = (),
    removed: Sequence[Any] = (),
    copper_discs: Sequence[tuple[str, tuple[str, ...], float, float, float, Any]] | None = None,
) -> tuple[float, str] | None:
    """``(angle, corner_class)`` were ``sharp_trace_corner`` to report ``point``.

    The vertex ``pcb`` has at ``point`` on ``net_name`` / ``layer`` once the
    tracks ``removed`` (by identity, as ``pcb.segments`` holds them) are gone
    and the ``added`` tracks (:class:`Segment` objects) are present. It uses
    the rule's own junction search, so a branch that lands inside a continuous
    track is judged exactly as the rule judges it (issue #10675): an edit that
    taps a straight run at 45 deg is declined here, not only when the saved
    board is re-checked. ``kct fix-drc --only redundant-copper`` (issue #10667)
    calls it to decline a candidate before it is applied.

    Callers evaluating many candidate vertices on one board can pass
    ``copper_discs`` precomputed via :func:`_copper_discs` to avoid rebuilding
    every pad polygon per call.
    """
    from kicad_tools.explain.checks.acid_trap import AcidTrapCheck

    discs = _copper_discs(pcb) if copper_discs is None else copper_discs
    if _is_buried(discs, net_name, layer, point):
        return None
    tracks = [
        s
        for s in pcb.segments
        if s.layer == layer
        and (s.net_name or "") == net_name
        and all(s is not gone for gone in removed)
    ]
    tracks.extend(added)
    check = AcidTrapCheck()
    for joint in _group_junctions(tracks, check):
        if math.dist(joint.location, point) > 0.02:
            continue
        angle = min(joint.open_angles(check), default=None)
        if angle is not None:
            return angle, "junction" if joint.leg_count >= 3 else "bend"
    return None


class SharpTraceCornerRule(DRCRule):
    """Warn on acute angles where two traces of one net meet."""

    rule_id = SHARP_TRACE_CORNER_RULE_ID
    name = "Sharp Trace Corner"
    description = "Flags acute (< 90 deg) corners between connected trace segments"

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        """Return one ``sharp_trace_corner`` warning per acute junction."""
        del design_rules
        from kicad_tools.explain.checks.acid_trap import AcidTrapCheck

        results = DRCResults()
        results.rules_checked = 1
        discs = _copper_discs(pcb)
        check = AcidTrapCheck()
        for net_name, layer, junction in _junctions(pcb, check):
            loc = junction.location
            angle = min(junction.open_angles(check), default=None)
            if angle is None or _is_buried(discs, net_name, layer, loc):
                continue
            n_legs = junction.leg_count
            corner_class = "junction" if n_legs >= 3 else "bend"
            kind = f"; {n_legs}-way junction" if corner_class == "junction" else ""
            results.add(
                DRCViolation(
                    rule_id=SHARP_TRACE_CORNER_RULE_ID,
                    severity="warning",
                    message=(
                        f"Sharp trace corner of {angle:.1f} deg on {net_name} ({layer}){kind}; "
                        f"reroute with 45/90 deg corners"
                    ),
                    location=loc,
                    layer=layer,
                    actual_value=float(angle),
                    nets=(net_name,),
                    corner_class=corner_class,
                )
            )
        return results
