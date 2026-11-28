"""Acute-branch repair: the edits ``kct fix-drc --only acute-branch`` makes (Issue #10732).

``sharp_trace_corner`` reports the vertex where a tap track meets the side of a
same-net carrier at an acute angle.  Board 03's shape is a 45-degree entry into
a straight run; 22 of them survive the redundant-copper cleanup on the
published copper.  Board 04's recipe helper ``repair_sharp_corners`` only
rewrites two-segment fold-backs (on Board 03 it leaves 21 of the 25 and opens
clearance violations doing it), and hand-picked coordinates are excluded from
the Board 03 reissue (#8900).

This module rewrites the *tap*.  The carrier, its other taps and the tap's
anchor, width, net and layer stay as they are; the tap meets the carrier at a
right angle instead:

* **straight** -- when the anchor lies over the carrier, one leg from the
  anchor perpendicular to it;
* **dogleg** -- otherwise the tap keeps its own bearing for a first leg and
  turns onto the perpendicular for the last one.  The landing slides along the
  carrier between the anchor's perpendicular foot and the acute vertex, as
  near the foot as the carrier allows (inside its extent by half the tap's
  width, at least a tap width from every other landing), so the entry is the
  most direct one the board has room for.  A landing another tap already has is
  offered last.

Nothing is hand-picked: the carrier is read from the rule's own junction
search at the reported vertex, and every coordinate comes from the anchor, the
vertex and the carrier's line.

Each candidate is gated before it is applied -- locked copper, the
differential-pair length skew, foreign clearance at the pair requirement, pad
mask-opening zones, the physical-gap slit oracle (the ``redundant_copper``
rule's gates), ``sharp_trace_corner``'s own test at every new vertex, and no
``redundant_copper`` finding naming a new leg -- and the first legal candidate
in that order is kept.  What the gates cannot see is the calling
transaction's (``kct fix-drc --only acute-branch``): it re-checks the saved,
refilled board with ``kct check``'s rule set and native DRC and hands a
failing edit back to :meth:`veto`, which refuses the landing so the next
planning round takes the tap's next candidate.

An edit is a value, not a mutation: the board is the input plus the edits kept
so far, so a veto puts the tap back exactly as it was written, without an
undo walk.  The edit stack, the veto/retry protocol and the lock and skew
gates are the redundant-copper repairer's (Issue #10667), which this class
extends; only the findings and the planning are its own.

Usage:
    repairer = AcuteBranchRepairer("board.kicad_pcb", rule=rule)
    result = repairer.plan(violations)
    repairer.veto(result.edits[0], "new_drc_error")
    repairer.save("board-fixed.kicad_pcb")
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations, islice, permutations
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..schema.pcb import PCB, Segment
from ..validate.rules.redundant_copper import RedundantCopperRule
from ..validate.rules.sharp_trace_corner import (
    _JUNCTION_TOL,
    SHARP_TRACE_CORNER_RULE_ID,
    _copper_discs,
    _group_junctions,
    _is_buried,
    _wedge_is_open,
    sharp_vertex,
)
from .repair_redundant_copper import (
    AddedSegment,
    CopperEdit,
    CopperUnrepaired,
    RedundantCopperRepairer,
    RedundantCopperResult,
    RemovedSegment,
    _pair_partners,
    _rule_id,
)

if TYPE_CHECKING:
    from kicad_tools.explain.checks.acid_trap import AcidTrapCheck
    from kicad_tools.manufacturers.base import DesignRules

Point2 = tuple[float, float]

#: The rule this module repairs, by the rule's own id.
REPAIRED_RULE = SHARP_TRACE_CORNER_RULE_ID
#: Sine of the smallest direction difference that is a different bearing.
_ANGLE_TOL = 1e-4
#: Planning rounds: an edit can free a landing for a tap that had no candidate,
#: and a round that edits nothing ends it.
_MAX_PLAN_ROUNDS = 6
#: Orders tried when a vertex has several taps (4 taps' worth; more is greedy).
_MAX_TASK_ORDERS = 24


class AcuteBranchResult(RedundantCopperResult):
    """What one planning run kept and what it could not clear.

    The redundant-copper result's shape (Issue #10667): ``kct fix-drc`` reads
    either edit pass the same way.
    """


@dataclass(frozen=True)
class _Task:
    """One tap to rewrite: its collinear pieces from the vertex, longest first."""

    junction: Point2
    angle: float
    pieces: tuple[Segment, ...]

    @property
    def tap_uuid(self) -> str:
        return self.pieces[0].uuid


@dataclass(frozen=True)
class _Site:
    """The rule's junction at one reported vertex, read for the repair."""

    net_number: int
    net_name: str
    layer: str
    junction: Point2
    segments: tuple[Segment, ...]
    carrier: Point2
    tasks: tuple[_Task, ...] = ()
    reason: str = ""

    @property
    def task_keys(self) -> tuple[tuple[str, ...], ...]:
        return tuple((task.tap_uuid,) for task in self.tasks)

    @property
    def vertex_key(self) -> tuple[str, ...]:
        """The finding's identity when no tap can be rewritten."""
        return (f"({self.junction[0]:.2f}, {self.junction[1]:.2f})",)


@dataclass(frozen=True)
class _Finding:
    """A live finding: one tap at one acute vertex (the rule reports the vertex)."""

    rule_id: str
    witnesses: tuple[str, ...]
    net_number: int
    net_name: str
    layer: str
    junction: Point2
    reason: str = ""


@dataclass(frozen=True)
class _Candidate:
    """A replacement centreline for the tap."""

    path: tuple[Point2, ...]


def _delta(a: Point2, b: Point2) -> Point2:
    return (a[0] - b[0], a[1] - b[1])


def _dot(a: Point2, b: Point2) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _cross(a: Point2, b: Point2) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _unit(v: Point2) -> Point2:
    length = math.hypot(*v)
    return (v[0] / length, v[1] / length)


def _parallel(a: Point2, b: Point2) -> bool:
    """Whether two non-zero vectors lie on one line, in either sense."""
    return abs(_cross(a, b)) <= _ANGLE_TOL * math.hypot(*a) * math.hypot(*b)


def _round(point: Point2) -> Point2:
    """A coordinate as the board writer keeps it (1 nm)."""
    return (round(point[0], 6), round(point[1], 6))


def _far_end(seg: Segment, point: Point2) -> Point2:
    """The end of ``seg`` away from ``point``, which must be one of its ends."""
    if math.dist(seg.start, point) <= math.dist(seg.end, point):
        return (float(seg.end[0]), float(seg.end[1]))
    return (float(seg.start[0]), float(seg.start[1]))


def _strictly_inside(seg: Segment, point: Point2) -> bool:
    """``point`` on ``seg``'s centreline, further than the rule's tolerance from both ends."""
    dx, dy = seg.end[0] - seg.start[0], seg.end[1] - seg.start[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return False
    x, y = point[0] - seg.start[0], point[1] - seg.start[1]
    along, across = x * dx + y * dy, x * dy - y * dx
    tolerance = _JUNCTION_TOL * math.sqrt(length_sq)
    return tolerance < along < length_sq - tolerance and abs(across) < tolerance


def _net_groups(pcb: PCB) -> dict[tuple[int, str, str], list[Segment]]:
    """The rule's ``(net, name, layer)`` grouping of a board's tracks."""
    groups: dict[tuple[int, str, str], list[Segment]] = defaultdict(list)
    for seg in pcb.segments:
        net = pcb.get_net(seg.net_number)
        name = seg.net_name or (net.name if net else f"Net {seg.net_number}")
        groups[(seg.net_number, name, seg.layer)].append(seg)
    return groups


def _tap_pieces(segments: list[Segment], junction: Point2, leg: Segment) -> tuple[Segment, ...]:
    """The board tracks that form ``leg``: ending at the vertex, on its ray, its width."""
    ray = _delta(leg.end, junction)
    pieces = []
    for seg in segments:
        if abs(seg.width - leg.width) > 1e-6:
            continue
        if min(math.dist(seg.start, junction), math.dist(seg.end, junction)) > _JUNCTION_TOL:
            continue
        out = _delta(_far_end(seg, junction), junction)
        if _dot(out, ray) > 0 and _parallel(out, ray):
            pieces.append(seg)
    # The longest piece carries the others (all run from the vertex along one
    # ray); ties go to the uuid so the finding's identity is stable.
    return tuple(sorted(pieces, key=lambda s: (-math.dist(s.start, s.end), s.uuid or "")))


def _site(
    joint: Any,
    check: AcidTrapCheck,
    segments: list[Segment],
    net_number: int,
    net_name: str,
    layer: str,
) -> _Site:
    """Read the rule's junction ``joint`` for the repair.

    A carrier is a track that passes through the vertex, or two collinear
    tracks that meet there (a carrier split at the vertex).  Every leg on the
    carrier's line is carrier copper; of an acute pair, a leg that is not is a
    tap, and each tap is one task.
    """
    from kicad_tools.explain.checks.acid_trap import is_acute_corner_angle

    junction = joint.location
    legs = list(joint.legs.values())
    out = {id(leg): _delta(leg.end, junction) for leg in legs}
    carrier_dirs: list[Point2] = [
        _unit(_delta(seg.end, seg.start)) for seg in segments if _strictly_inside(seg, junction)
    ]
    for first, second in combinations(legs, 2):
        a, b = out[id(first)], out[id(second)]
        if _parallel(a, b) and _dot(a, b) < 0:
            carrier_dirs.append(a)
    tasks: dict[str, _Task] = {}
    reason = "no_carrier"
    if carrier_dirs:
        reason = "no_branch"
        for first, second in combinations(legs, 2):
            angle = check._calculate_angle(first, second, junction)
            if (
                angle is None
                or not is_acute_corner_angle(angle)
                or not _wedge_is_open(first, second, junction, angle)
            ):
                continue
            for leg in (first, second):
                if any(_parallel(out[id(leg)], direction) for direction in carrier_dirs):
                    continue
                pieces = _tap_pieces(segments, junction, leg)
                if pieces:
                    task = _Task(junction=junction, angle=angle, pieces=pieces)
                    tasks.setdefault(task.tap_uuid, task)
        if tasks:
            reason = ""
    return _Site(
        net_number=net_number,
        net_name=net_name,
        layer=layer,
        junction=junction,
        segments=tuple(segments),
        carrier=_unit(carrier_dirs[0]) if carrier_dirs else (0.0, 0.0),
        tasks=tuple(tasks.values()),
        reason=reason,
    )


def _site_at(
    pcb: PCB, net_number: int, net_name: str, layer: str, junction: Point2
) -> _Site | None:
    """The junction the rule finds at ``junction`` on ``pcb`` as it stands."""
    from kicad_tools.explain.checks.acid_trap import AcidTrapCheck

    check = AcidTrapCheck()
    segments = _net_groups(pcb).get((net_number, net_name, layer))
    if not segments:
        return None
    for joint in _group_junctions(segments, check):
        if math.dist(joint.location, junction) <= 0.02:
            return _site(joint, check, segments, net_number, net_name, layer)
    return None


def _acute_sites(pcb: PCB) -> list[_Site]:
    """Every junction the rule reports, read for the repair."""
    from kicad_tools.explain.checks.acid_trap import AcidTrapCheck

    check = AcidTrapCheck()
    discs = _copper_discs(pcb)
    sites: list[_Site] = []
    for (net_number, net_name, layer), segments in _net_groups(pcb).items():
        for joint in _group_junctions(segments, check):
            if not next(iter(joint.open_angles(check)), None) or _is_buried(
                discs, net_name, layer, joint.location
            ):
                continue
            sites.append(_site(joint, check, segments, net_number, net_name, layer))
    return sites


def _findings(pcb: PCB) -> list[_Finding]:
    """One entry per acute tap, and one per acute vertex no tap can clear."""
    findings: list[_Finding] = []
    for site in _acute_sites(pcb):
        for witnesses in site.task_keys or (site.vertex_key,):
            findings.append(
                _Finding(
                    rule_id=REPAIRED_RULE,
                    witnesses=witnesses,
                    net_number=site.net_number,
                    net_name=site.net_name,
                    layer=site.layer,
                    junction=site.junction,
                    reason="" if site.tasks else site.reason,
                )
            )
    return findings


def _run_extent(
    segments: Sequence[Segment], junction: Point2, carrier: Point2, skip: set[str]
) -> tuple[float, float]:
    """``[lo, hi]`` along ``carrier`` from ``junction`` of the straight run through it."""
    pieces: list[tuple[float, float]] = []
    for seg in segments:
        if seg.uuid in skip or seg.width <= 0:
            continue
        if not _parallel(_delta(seg.end, seg.start), carrier):
            continue
        a, b = _dot(_delta(seg.start, junction), carrier), _dot(_delta(seg.end, junction), carrier)
        if abs(_cross(carrier, _delta(seg.start, junction))) > _JUNCTION_TOL:
            continue
        pieces.append((min(a, b), max(a, b)))
    carrying = [p for p in pieces if p[0] - _JUNCTION_TOL <= 0.0 <= p[1] + _JUNCTION_TOL]
    if not carrying:
        return 0.0, 0.0
    lo = min(p[0] for p in carrying)
    hi = max(p[1] for p in carrying)
    grown = True
    while grown:
        grown = False
        for a, b in pieces:
            if a - _JUNCTION_TOL <= hi and b + _JUNCTION_TOL >= lo and (a < lo or b > hi):
                lo, hi = min(lo, a), max(hi, b)
                grown = True
    return lo, hi


def _landings(
    segments: Sequence[Segment],
    junction: Point2,
    carrier: Point2,
    lo: float,
    hi: float,
    skip: set[str],
) -> list[tuple[float, float]]:
    """``(along-carrier position, width)`` of every other track that meets the run."""
    landings: list[tuple[float, float]] = []
    for seg in segments:
        if seg.uuid in skip or seg.width <= 0:
            continue
        if _parallel(_delta(seg.end, seg.start), carrier):
            continue  # a split point of the run: its copper runs on through
        for end in (seg.start, seg.end):
            e = _delta(end, junction)
            s = _dot(e, carrier)
            if (
                abs(_cross(carrier, e)) <= _JUNCTION_TOL
                and lo - _JUNCTION_TOL <= s <= hi + _JUNCTION_TOL
            ):
                landings.append((s, seg.width))
    return landings


def _free_spans(
    lo: float, hi: float, holes: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """``[lo, hi]`` less the ``holes``: the stretches a landing may take."""
    spans: list[tuple[float, float]] = []
    cur = lo
    for a, b in sorted(holes):
        if b <= lo or a >= hi or b <= cur:
            continue
        if a > cur:
            spans.append((cur, a))
        cur = b
    if cur < hi:
        spans.append((cur, hi))
    return [(a, b) for a, b in spans if b - a > 1e-6]


def _path(
    anchor: Point2, junction: Point2, carrier: Point2, normal: Point2, s_foot: float, s: float
) -> tuple[Point2, ...] | None:
    """The tap's new centreline for a landing ``s`` along the carrier.

    At the foot itself one straight leg; between the foot and the vertex a
    dogleg whose first leg keeps the tap's bearing and whose last leg is the
    perpendicular onto the carrier.  ``None`` for a landing off that stretch.
    """
    landing = (junction[0] + s * carrier[0], junction[1] + s * carrier[1])
    if abs(s - s_foot) <= _JUNCTION_TOL:
        path = [anchor, landing]
    else:
        if abs(s_foot) <= _JUNCTION_TOL or not 0.0 < s / s_foot < 1.0:
            return None
        t = 1.0 - s / s_foot
        path = [
            anchor,
            (
                anchor[0] + t * (junction[0] - anchor[0]),
                anchor[1] + t * (junction[1] - anchor[1]),
            ),
            landing,
        ]
    rounded = [_round(p) for p in path]
    if any(math.dist(a, b) <= 1e-6 for a, b in zip(rounded, rounded[1:], strict=False)):
        return None
    if not _parallel(_delta(rounded[-1], rounded[-2]), normal):
        return None
    return tuple(rounded)


def _candidates(site: _Site, task: _Task) -> list[_Candidate]:
    """The replacement paths for ``task``'s tap, preferred first.

    The landing slides along the carrier between the anchor's perpendicular
    foot and the acute vertex.  It stays inside the run by half the tap's width
    and at least a tap width from every other landing, and a dogleg's last leg
    is at least a tap wide.  Within that: the straight leg, then the doglegs nearest
    the foot (the least copper), then a landing another tap already has.
    """
    junction, carrier = site.junction, site.carrier
    main = task.pieces[0]
    anchor = _far_end(main, junction)
    width = main.width
    skip = {seg.uuid for seg in task.pieces}
    lo, hi = _run_extent(site.segments, junction, carrier, skip)
    normal = (-carrier[1], carrier[0])
    to_anchor = _delta(anchor, junction)
    if _dot(to_anchor, normal) < 0:
        normal = (-normal[0], -normal[1])
    s_foot, reach = _dot(to_anchor, carrier), _dot(to_anchor, normal)
    if reach <= 0:
        return []
    margin = width / 2.0
    landings = _landings(site.segments, junction, carrier, lo, hi, skip)
    # A dogleg's last leg is ``reach * |s| / |s_foot|`` long: keep it a tap wide.
    s_min = width * abs(s_foot) / reach
    low, high = sorted((s_foot, 0.0))
    inside = (max(low, lo + margin), min(high, hi - margin))
    holes = [(s - max(width, w), s + max(width, w)) for s, w in landings]
    holes.append((-s_min, s_min))
    spans = _free_spans(inside[0], inside[1], holes)
    on_landing = [s for s, _w in landings]

    def foot(s: float) -> float:
        return abs(s - s_foot)

    offers: list[float] = []
    if (
        any(a - _JUNCTION_TOL <= s_foot <= b + _JUNCTION_TOL for a, b in spans)
        or s_foot in on_landing
    ):
        offers.append(s_foot)
    for a, b in spans:
        offers.extend(sorted((a, b, (a + b) / 2.0), key=foot))
    for s in sorted(on_landing, key=foot):
        if abs(s) >= s_min:
            offers.append(s)
    candidates: list[_Candidate] = []
    seen: set[tuple[Point2, ...]] = set()
    for s in offers:
        path = _path(anchor, junction, carrier, normal, s_foot, s)
        if path is not None and path not in seen:
            seen.add(path)
            candidates.append(_Candidate(path=path))
    return candidates


def _judged_points(
    pcb: PCB, site: _Site, removed: list[Segment], legs: list[tuple[Point2, Point2]]
) -> list[Point2]:
    """Every vertex the rule can see on the new copper: the path's own, and
    each point where a kept track of the net meets a new leg mid-span (a tap
    landing inside the replacement, which the rule reports too, #10675)."""
    points: list[Point2] = [legs[0][0]]
    points.extend(b for _a, b in legs)
    kept = [s for s in pcb.segments if s.layer == site.layer and s.net_number == site.net_number]
    for a, b in legs:
        dx, dy = b[0] - a[0], b[1] - a[1]
        reach = math.hypot(dx, dy)
        tolerance = _JUNCTION_TOL * reach
        for seg in kept:
            if any(seg is gone for gone in removed):
                continue
            for end in (seg.start, seg.end):
                x, y = end[0] - a[0], end[1] - a[1]
                along, across = x * dx + y * dy, x * dy - y * dx
                if tolerance < along < reach * reach - tolerance and abs(across) < tolerance:
                    points.append((float(end[0]), float(end[1])))
    return points


class AcuteBranchRepairer(RedundantCopperRepairer):
    """Plan the acute-branch edits for a board and keep the surviving ones.

    Args:
        pcb_path: The board to edit.  It is read once; the working board is
            rebuilt from that text plus the kept edits, so nothing is written
            until :meth:`save`.
        rule: The ``redundant_copper`` rule as ``kct check`` configures it for
            this run (``DRCChecker.redundant_copper_rule``).  Its clearance,
            mask and slit gates judge every candidate.
        design_rules: The checker's manufacturer rules, for the rule.
    """

    def __init__(
        self,
        pcb_path: str | Path,
        *,
        rule: RedundantCopperRule,
        design_rules: DesignRules | None = None,
    ) -> None:
        super().__init__(pcb_path, rule=rule, design_rules=design_rules)
        self._rule_bound = False

    def _rebuild(self) -> None:
        super()._rebuild()
        self._forget_board()

    def _apply(self, edit: CopperEdit) -> None:
        super()._apply(edit)
        self._forget_board()

    def _forget_board(self) -> None:
        """The board changed: the gates rebind on their next use."""
        self._rule_bound = False

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------

    def live_findings(self) -> set[tuple[str, tuple[str, ...]]]:
        """Identities of the acute taps the rule reports on the board as it
        stands -- ``CopperEdit.finding`` tuples."""
        return {(f.rule_id, f.witnesses) for f in self._live_findings()}

    def _live_findings(self) -> list[_Finding]:
        """What the rule reports on the board as it stands, one entry per tap."""
        return _findings(self.pcb)

    def _task_at(self, site: _Site, tap_uuid: str) -> tuple[_Site, _Task] | None:
        """The task ``tap_uuid`` still names at ``site``'s vertex, on the board as it stands."""
        current = _site_at(self.pcb, site.net_number, site.net_name, site.layer, site.junction)
        if current is None:
            return None
        for task in current.tasks:
            if task.tap_uuid == tap_uuid:
                return current, task
        return None

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan(self, violations: Sequence[Any]) -> AcuteBranchResult:
        """Gate and apply one edit per tap until none is left that can clear.

        Like the redundant-copper plan, the findings are re-read from the board
        after every round: one tap rewritten can free a landing for a tap on
        the same carrier.  ``violations`` sizes the run and names the rules
        this module does not repair.  A round that clears nothing ends the
        planning, and whatever the rule still reports then is
        :attr:`AcuteBranchResult.unrepaired`.
        """
        result = AcuteBranchResult(total=len(violations))
        for violation in violations:
            rule_id = _rule_id(violation)
            if rule_id != REPAIRED_RULE:
                nets = list(getattr(violation, "nets", ()) or ())
                result.skip(rule_id, "", nets[0] if nets else "", "unsupported_rule")
        pairs = _pair_partners(self.pcb)
        declined: dict[tuple[str, tuple[str, ...]], str] = {}
        for _round in range(_MAX_PLAN_ROUNDS):
            progressed = False
            for site in _acute_sites(self.pcb):
                for witnesses in site.task_keys or (site.vertex_key,):
                    declined.pop((REPAIRED_RULE, witnesses), None)
                if not site.tasks:
                    declined[(REPAIRED_RULE, site.vertex_key)] = site.reason
                    continue
                kept = self._plan_site(site, pairs.get(site.net_number), declined)
                result.edits.extend(kept)
                progressed = progressed or bool(kept)
            if not progressed:
                break
        for finding in self._live_findings():
            key = (finding.rule_id, finding.witnesses)
            result.unrepaired.append(
                CopperUnrepaired(
                    rule_id=finding.rule_id,
                    uuid=finding.witnesses[0] if finding.witnesses else "",
                    net_name=finding.net_name,
                    reason=declined.get(key, "not_planned"),
                )
            )
        return result

    def _plan_site(
        self,
        site: _Site,
        partner: int | None,
        declined: dict[tuple[str, tuple[str, ...]], str],
    ) -> list[CopperEdit]:
        """Rewrite as many of the vertex's taps as can be, in the order that rewrites the most.

        Two taps of one vertex compete for the carrier: planned first, one can
        take the landing the other needs (Board 03's VBUS trunk).  With more
        than one tap every order is tried on the board, and the one that keeps
        the most edits is applied again.  ``declined`` receives why each tap
        that got no edit has none.
        """
        tasks = tuple(site.tasks)
        orders: Iterable[tuple[_Task, ...]] = (tasks,)
        if len(tasks) > 1:
            orders = islice(permutations(tasks), _MAX_TASK_ORDERS)
        best: tuple[_Task, ...] | None = None
        best_count = -1
        for order in orders:
            count = len(self._apply_order(site, order, partner))
            self._unapply(count)
            if count > best_count:
                best, best_count = order, count
                if best_count == len(tasks):
                    break
        assert best is not None
        return self._apply_order(site, best, partner, declined)

    def _apply_order(
        self,
        site: _Site,
        order: tuple[_Task, ...],
        partner: int | None,
        declined: dict[tuple[str, tuple[str, ...]], str] | None = None,
    ) -> list[CopperEdit]:
        """Plan and apply ``order``'s taps in turn: the edits kept, in order.

        Once the whole order is on the board, no ``redundant_copper`` finding
        may have appeared: not one a new leg is part of, and not one a
        right-angle entry uncovers on the carrier it lands on (Board 03's
        JOY_X trunk is a staircase the rule cannot report under a 45-degree
        tap).  The order's last edit is put back until none is left, so the
        next plan takes that tap's next landing.
        """
        baseline = self._redundant_census(site.net_number)
        kept: list[CopperEdit] = []
        for task in order:
            key = (REPAIRED_RULE, (task.tap_uuid,))
            found = self._task_at(site, task.tap_uuid)
            if found is None:
                if declined is not None:
                    declined.pop(key, None)  # a sibling's edit cleared this tap's angle
                continue
            current, live = found
            edit = self._plan_tap(current, live, partner)
            if isinstance(edit, CopperEdit):
                self._edits.append(edit)
                self._apply(edit)
                kept.append(edit)
            elif declined is not None:
                declined[key] = edit
        while kept:
            fresh = sorted(
                rule for rule, _message in self._redundant_census(site.net_number) - baseline
            )
            if not fresh:
                break
            edit = kept.pop()
            self.veto(edit, f"new_{fresh[0]}")
            if declined is not None:
                declined[(REPAIRED_RULE, edit.witnesses)] = f"new_{fresh[0]}"
        if declined is not None:
            for edit in kept:
                declined.pop((REPAIRED_RULE, edit.witnesses), None)
        return kept

    def _unapply(self, count: int) -> None:
        """Take the last ``count`` kept edits off the board, refusing nothing."""
        for _ in range(count):
            self._edits.pop()
        self._rebuild()

    def _bind_gates(self) -> None:
        if not self._rule_bound:
            self._rule.bind(self.pcb, self._design_rules)
            self._rule_bound = True

    def _redundant_census(self, net_number: int) -> set[tuple[str, str]]:
        """The ``redundant_copper`` findings on one net's tracks, as the rule reports them.

        The rule's own detectors, run on the net's per-layer groups only: an
        edit cannot change another net's findings, and the whole-board run
        costs seconds on a routed board.
        """
        from ..validate.violations import DRCResults

        self._bind_gates()
        rule = self._rule
        results = DRCResults()
        mine = [
            s for s in self.pcb.segments if s.net_number == net_number and s.layer and s.width > 0
        ]
        groups: dict[str, list[Segment]] = defaultdict(list)
        for seg in mine:
            groups[seg.layer].append(seg)
        rule._check_duplicates(mine, results)  # noqa: SLF001 - the rule's own detectors
        for group in groups.values():
            rule._check_chains(group, results)  # noqa: SLF001
            rule._check_covered(group, results)  # noqa: SLF001
        rule._check_via_offcentre(self.pcb, mine, results)  # noqa: SLF001
        return {(v.rule_id, v.message) for v in results.violations}

    def _plan_tap(self, site: _Site, task: _Task, partner: int | None) -> CopperEdit | str:
        """The edit that rewrites ``task``'s tap, or why none can."""
        pieces = list(task.pieces)
        locked = self._lock_reason(pieces)
        if locked:
            return locked
        self._bind_gates()
        if self._rule._requirement is None:  # noqa: SLF001 - the rule's own gate state
            return "netclass_unresolved"
        reason = "no_candidate"
        for candidate in _candidates(site, task):
            edit = self._tap_edit(site, task, candidate.path)
            if isinstance(edit, str):
                return edit
            refused = self._rejected.get(edit.finding, {}).get(edit.path_key)
            if refused is not None:
                reason = refused
                continue
            if self._worsens_skew(site.net_number, partner, edit.length_delta):
                reason = "would_widen_pair_skew"
                continue
            why = self._gate(site, task, candidate.path)
            if why is not None:
                reason = why
                continue
            return edit
        return reason

    def _gate(self, site: _Site, task: _Task, path: tuple[Point2, ...]) -> str | None:
        """Why ``path`` must not replace the tap, or ``None`` when it may."""
        rule = self._rule
        width = task.pieces[0].width
        legs = list(zip(path, path[1:], strict=False))
        removed = list(task.pieces)
        if not rule._path_clear(list(legs), site.net_number, site.layer, width):  # noqa: SLF001
            return "would_violate_clearance"
        steps = [(seg, seg.start, seg.end) for seg in removed]
        if not rule._mask_clear(list(path), steps, site.layer, width, site.net_number):  # noqa: SLF001
            return "new_copper_in_mask_zone"
        oracle = rule._slit_oracle()  # noqa: SLF001
        if oracle is not None and oracle.rejects(site.layer, removed, list(path), width):
            return "would_open_copper_slit"
        added = [
            Segment(
                start=a,
                end=b,
                width=width,
                layer=site.layer,
                net_number=site.net_number,
                net_name=site.net_name,
            )
            for a, b in legs
        ]
        discs = rule._copper_discs()  # noqa: SLF001
        for point in _judged_points(self.pcb, site, removed, legs):
            found = sharp_vertex(
                self.pcb,
                site.net_name,
                site.layer,
                point,
                added=added,
                removed=removed,
                copper_discs=discs,
            )
            if found is not None:
                return f"sharp_corner_{found[0]:.0f}deg"
        return None

    def _tap_edit(self, site: _Site, task: _Task, path: tuple[Point2, ...]) -> CopperEdit | str:
        """The edit that replaces the tap with ``path`` at the tap's width."""
        width = task.pieces[0].width
        source = self._segment(task.tap_uuid)
        if source is None:
            return "segment_not_found"
        template = self._seg_nodes[self.pcb.segments.index(source)].to_string()
        removed = tuple(
            RemovedSegment(seg.uuid, seg.start, seg.end, seg.width) for seg in task.pieces
        )
        added = tuple(
            AddedSegment(
                uuid=self.pcb.mint_uuid(
                    "acute-branch", site.net_name, site.layer, *a, *b, width, index
                ),
                start=a,
                end=b,
                width=width,
            )
            for index, (a, b) in enumerate(zip(path, path[1:], strict=False))
        )
        return CopperEdit(
            kind="rewrite_tap",
            rule_id=REPAIRED_RULE,
            net_name=site.net_name,
            layer=site.layer,
            detail=(
                f"tap met the {site.net_name} carrier at {task.angle:.0f} deg at "
                f"({site.junction[0]:.3f}, {site.junction[1]:.3f}); now "
                + " -> ".join(f"({p[0]:.3f}, {p[1]:.3f})" for p in path)
                + f" at {width:.3f} mm"
            ),
            removed=removed,
            added=added,
            legs=path,
            width=width,
            template=template,
            witnesses=(task.tap_uuid,),
        )
