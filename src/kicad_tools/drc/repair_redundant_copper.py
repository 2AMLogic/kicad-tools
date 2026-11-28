"""Redundant-copper repair: the edits ``kct fix-drc --only redundant-copper`` makes (Issue #10667).

``redundant_copper`` (``validate/rules/redundant_copper.py``, issue #8218)
reports avoidable same-net copper as warnings: a segment hidden under a longer
collinear one (``_covered``), a staircase a two-segment path could replace
(``_staircase``) and a run far longer than the distance between its ends
(``_detour``).  ``kct readiness`` refuses any that survive its cleanup, and
the global passes that reduce them trade them for other findings:
``kct pcb prune`` splits the covering track at every tap, which turns each
landing into an explicit acute junction (``sharp_trace_corner``), and
``kct optimize-traces`` moves copper its per-net native DRC accepts but
``kct check`` does not.

This module makes the three edits, one finding at a time, so that
``fix-drc``'s verified transaction can keep each one or put it back:

* **covered** -- the hidden segment is removed and the covering track is
  *not* split: the copper is unchanged and no landing becomes an explicit
  junction.  When kicad-cli then reports a neighbouring end as dangling
  (Board 03's 0.11 mm VBUS stub), the edit widens to remove that stub too,
  provided the stub's copper lies entirely inside same-net copper that stays.
* **staircase / detour** -- the chain's segments are replaced by one of the
  rule's own fewer-bend paths (a 45-degree leg and an H/V leg, in either
  order, or one straight leg) between the chain's two end nodes.  The rule
  judges each path with its own gates -- clearance at the pair requirement,
  mask-opening zones, terminal contact, the physical-gap slit oracle, a tap
  kept on the replacement and no acute corner opened
  (:meth:`RedundantCopperRule.option_reason`) -- so what is planned here is
  exactly what the rule asked for.  A chain the rule cut at a junction
  either replaces its cut segment whole, running on through the tap, or is
  trimmed at the tap, as the rule's option says.

What the rule's gates cannot see -- a native ``track_dangling``, a mask
opening the rule's zone model misses -- is the calling transaction's job: it
re-checks the saved board and hands the offending edit back to
:meth:`RedundantCopperRepairer.veto`, which also remembers the path it took
so the next planning round tries the chain's next legal path.

Before either, the repair adds what the rule leaves to it: locked,
generator-owned or grouped copper is never touched, and a
differential-pair member's rewrite may not widen the pair's length skew.

An edit is a value, not a mutation: the board is the input plus the edits
kept so far (:meth:`RedundantCopperRepairer.board`), so a veto puts the
touched copper back exactly as it was written, without an undo walk.

Usage:
    repairer = RedundantCopperRepairer("board.kicad_pcb", rule=rule)
    result = repairer.plan(violations)
    repairer.veto(result.edits[0], "new_drc_error")
    repairer.save("board-fixed.kicad_pcb")
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shapely.geometry import LineString  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]

from ..core.pcb_item_locks import BoardItemLocks
from ..schema.pcb import PCB, Segment
from ..sexp import SExp, parse_string
from ..validate.rules.redundant_copper import RedundantCopperRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules

Point2 = tuple[float, float]

#: Chain rules this module repairs; the other ``redundant_copper_*`` findings
#: (duplicates, fragments, off-centre via ends) are other tools' and are
#: listed as ``unsupported_rule``.
REPAIRED_RULES = frozenset(
    {"redundant_copper_covered", "redundant_copper_staircase", "redundant_copper_detour"}
)
#: A differential pair's length skew may move by less than this (mm) and still
#: count as unchanged: the rewrite is length-neutral to within float noise.
SKEW_TOLERANCE_MM = 1e-3
#: Planning rounds: each edit can expose a finding the detection run did not
#: have (a leg lying on kept copper), and a round that edits nothing ends it.
_MAX_PLAN_ROUNDS = 5


@dataclass(frozen=True)
class RemovedSegment:
    """A segment an edit takes off the board."""

    uuid: str
    start: Point2
    end: Point2
    width: float


@dataclass(frozen=True)
class TrimmedSegment:
    """A chain end segment an edit shortens to the junction it was cut at."""

    uuid: str
    kept: Point2
    end: Point2  # the junction the chain ended at (the new far end)
    width: float


@dataclass(frozen=True)
class AddedSegment:
    """A replacement leg an edit puts on the board."""

    uuid: str
    start: Point2
    end: Point2
    width: float


@dataclass
class CopperEdit:
    """One finding's edit: what comes off, what is trimmed, what goes on.

    ``kind`` is ``remove_covered`` or ``rewrite_chain``.  The replacement
    legs are all ``width`` wide -- the chain's widest segment, the width the
    rule's gates verified.  ``legs`` are the rule's shortcut polyline, in
    order; ``duplicate_copper`` records that a leg lies on kept same-net
    copper (a ``redundant_copper_covered`` the next covered edit removes).
    """

    kind: str
    rule_id: str
    net_name: str
    layer: str
    detail: str
    removed: tuple[RemovedSegment, ...] = ()
    trimmed: tuple[TrimmedSegment, ...] = ()
    added: tuple[AddedSegment, ...] = ()
    legs: tuple[Point2, ...] = ()
    width: float = 0.0
    template: str = ""
    witnesses: tuple[str, ...] = ()
    duplicate_copper: bool = False

    @property
    def uuid(self) -> str:
        """The finding's first witness: the edit's identity in reports."""
        if self.witnesses:
            return self.witnesses[0]
        return self.added[0].uuid if self.added else ""

    @property
    def finding(self) -> tuple[str, tuple[str, ...]]:
        """``(rule, witnesses)``: the finding the edit clears."""
        return self.rule_id, self.witnesses

    @property
    def uuids(self) -> frozenset[str]:
        """Every segment uuid the edit takes off, trims or puts on."""
        return frozenset(
            {s.uuid for s in self.removed}
            | {s.uuid for s in self.trimmed}
            | {s.uuid for s in self.added}
        )

    @property
    def path_key(self) -> tuple[Point2, ...]:
        """The replacement path, to 1 um -- what a veto remembers."""
        return tuple(_q(p) for p in self.legs)

    def to_dict(self) -> dict[str, Any]:
        def point(p: Point2) -> tuple[float, float]:
            return (round(p[0], 4), round(p[1], 4))

        return {
            "kind": self.kind,
            "rule": self.rule_id,
            "net": self.net_name,
            "layer": self.layer,
            "detail": self.detail,
            "removed": [s.uuid for s in self.removed],
            "trimmed": [s.uuid for s in self.trimmed],
            "added": [
                {"uuid": s.uuid, "start": point(s.start), "end": point(s.end), "width": s.width}
                for s in self.added
            ],
        }


@dataclass
class CopperUnrepaired:
    """A finding no edit could clear, and why."""

    rule_id: str
    uuid: str
    net_name: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_id,
            "uuid": self.uuid,
            "net": self.net_name,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class RedundantCopperResult:
    """What one planning run kept and what it could not clear."""

    total: int = 0
    edits: list[CopperEdit] = field(default_factory=list)
    unrepaired: list[CopperUnrepaired] = field(default_factory=list)

    @property
    def repaired(self) -> int:
        return len(self.edits)

    def skip(self, rule_id: str, uuid: str, net_name: str, reason: str, detail: str = "") -> None:
        self.unrepaired.append(
            CopperUnrepaired(
                rule_id=rule_id, uuid=uuid, net_name=net_name, reason=reason, detail=detail
            )
        )

    def by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for edit in self.edits:
            counts[edit.rule_id] = counts.get(edit.rule_id, 0) + 1
        return counts


def _q(point: Point2) -> Point2:
    return (round(point[0], 3), round(point[1], 3))


def _rule_id(violation: Any) -> str:
    """A finding's rule under either report type.

    ``kct check``'s own findings carry ``rule_id``; ``fix-drc``'s report
    (``drc.compat``) carries the same name as ``type_str``.
    """
    return str(getattr(violation, "type_str", None) or getattr(violation, "rule_id", ""))


def _on_line(p: Point2, a: Point2, b: Point2, tol: float = 1e-3) -> bool:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 <= 0.0:
        return math.dist(p, a) <= tol
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy)) <= tol


class RedundantCopperRepairer:
    """Plan the redundant-copper edits for a board and keep the surviving ones.

    Args:
        pcb_path: The board to edit.  It is read once; the working board is
            rebuilt from that text plus the kept edits, so nothing is written
            until :meth:`save`.
        rule: The ``redundant_copper`` rule as ``kct check`` configures it for
            this run (``DRCChecker.redundant_copper_rule``).  Its chain walk,
            options and gates are what the repair re-runs.
        design_rules: The checker's manufacturer rules, for the rule.
    """

    def __init__(
        self,
        pcb_path: str | Path,
        *,
        rule: RedundantCopperRule,
        design_rules: DesignRules | None = None,
    ) -> None:
        self.path = Path(pcb_path)
        self._rule = rule
        self._design_rules = design_rules
        self._edits: list[CopperEdit] = []
        self._rejected: dict[tuple[str, tuple[str, ...]], dict[tuple[Point2, ...], str]] = {}
        self.pcb: PCB = PCB.load(self.path)
        self._seg_nodes: list[Any] = []
        self._sync_nodes()
        self._locks = BoardItemLocks(self.pcb._sexp)

    # ------------------------------------------------------------------
    # Board state
    # ------------------------------------------------------------------

    @property
    def edits(self) -> tuple[CopperEdit, ...]:
        """The edits kept so far, in planning order."""
        return tuple(self._edits)

    def _sync_nodes(self) -> None:
        """``(segment ...)`` tree nodes in board order, parallel to ``pcb.segments``."""
        self._seg_nodes = [
            child
            for child in self.pcb._sexp.children
            if not child.is_atom and child.name == "segment"
        ]
        if len(self._seg_nodes) != len(self.pcb.segments):
            raise RuntimeError(
                f"board has {len(self._seg_nodes)} (segment ...) nodes but "
                f"{len(self.pcb.segments)} parsed segments"
            )

    def _rebuild(self) -> None:
        """The board as the input plus every kept edit, applied in order."""
        self.pcb = PCB.load(self.path)
        self._sync_nodes()
        for edit in self._edits:
            self._apply(edit)

    def _segment(self, uuid: str) -> Segment | None:
        return next((s for s in self.pcb.segments if s.uuid == uuid), None)

    def _exact(self, node: Point2) -> Point2:
        """A chain node as the neighbouring track wrote it (the walk rounds to 1 um)."""
        for seg in self.pcb.segments:
            for end in (seg.start, seg.end):
                if _q(end) == node:
                    return end
        return node

    def _apply(self, edit: CopperEdit) -> None:
        """Put one edit on ``self.pcb`` (the tree and the parsed list alike)."""
        origin = self.pcb._board_origin
        for removed in edit.removed:
            seg = self._segment(removed.uuid)
            if seg is None:
                raise RuntimeError(f"segment {removed.uuid} is not on the board")
            index = self.pcb.segments.index(seg)
            node = self._seg_nodes[index]
            # By identity: two segments with equal fields are still two items.
            tree = next(i for i, child in enumerate(self.pcb._sexp.children) if child is node)
            del self.pcb._sexp.children[tree]
            del self.pcb._segments[index]
            del self._seg_nodes[index]
        for trimmed in edit.trimmed:
            seg = self._segment(trimmed.uuid)
            if seg is None:
                raise RuntimeError(f"segment {trimmed.uuid} is not on the board")
            index = self.pcb.segments.index(seg)
            # The kept end stays; the other end moves to the junction.
            moved = "end" if math.dist(seg.start, trimmed.kept) <= 1e-6 else "start"
            child = self._seg_nodes[index].find(moved)
            child.set_value(0, trimmed.end[0] + origin[0])
            child.set_value(1, trimmed.end[1] + origin[1])
            if moved == "start":
                seg.start = trimmed.end
            else:
                seg.end = trimmed.end
        for added in edit.added:
            self._add_segment(added, edit, origin)
        self._sync_nodes()

    def _add_segment(self, added: AddedSegment, edit: CopperEdit, origin: Point2) -> None:
        node = parse_string(edit.template)
        for tag, point in (("start", added.start), ("end", added.end)):
            child = node.find(tag)
            assert child is not None, f"segment template has no ({tag} ...)"
            while len(child.children) < 2:
                child.children.append(SExp(value=0.0))
            child.set_value(0, point[0] + origin[0])
            child.set_value(1, point[1] + origin[1])
        node.find("width").set_value(0, added.width)  # type: ignore[union-attr]
        node.find("uuid").set_value(0, added.uuid)  # type: ignore[union-attr]
        self.pcb._sexp.children.append(node)
        self.pcb._segments.append(
            Segment(
                start=added.start,
                end=added.end,
                width=added.width,
                layer=edit.layer,
                net_number=self._net_number(edit.net_name),
                net_name=edit.net_name,
                uuid=added.uuid,
            )
        )
        self._sync_nodes()

    def _net_number(self, net_name: str) -> int:
        for net in self.pcb.nets.values():
            if net.name == net_name:
                return net.number
        return 0

    def save(self, output_path: str | Path | None = None) -> None:
        """Write the board with every kept edit applied.

        The working board keeps resolving its project sidecars (the
        net-class map the shortcut gates read) beside the *input*: a staged
        copy in a temporary directory would lose them once that directory
        goes, and the rule would then judge the next chain at the fab floor.
        """
        target = Path(output_path) if output_path else self.path
        self._rebuild()
        self.pcb.save(target)
        self.pcb._path = self.path  # noqa: SLF001 - PCB.save() moves it to the last target

    def touched_geometry(self, edit: CopperEdit) -> BaseGeometry:
        """The copper an edit took off or put on, as one shapely geometry.

        Board-relative like the parsed segments; the caller shifts it by the
        board origin when attributing a finding reported in sheet coordinates.
        """
        pieces = [
            LineString([a, b]).buffer(width / 2.0)
            for a, b, width in (
                *((s.start, s.end, s.width) for s in edit.removed),
                *((s.kept, s.end, s.width) for s in edit.trimmed),
                *((s.start, s.end, s.width) for s in edit.added),
            )
        ]
        return unary_union(pieces) if pieces else LineString()

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan(self, violations: Sequence[Any]) -> RedundantCopperResult:
        """Gate and apply one edit per finding until none is left that can clear.

        The findings are re-read from the board after every edit rather than
        taken from ``violations`` once: an edit changes the chains around it
        (Board 03's two BTN3 staircases share a node, and rewriting one
        merges the other into a longer chain), so a witness list from the
        detection run is stale by the second edit.  ``violations`` still
        sizes the run and names the rules this module does not repair.  A
        round that clears nothing ends the planning, and whatever the rule
        still reports then is :attr:`RedundantCopperResult.unrepaired`.
        """
        result = RedundantCopperResult(total=len(violations))
        for violation in violations:
            rule_id = _rule_id(violation)
            if rule_id not in REPAIRED_RULES:
                nets = list(getattr(violation, "nets", ()) or ())
                result.skip(rule_id, "", nets[0] if nets else "", "unsupported_rule")
        pairs = _pair_partners(self.pcb)
        declined: dict[tuple[str, tuple[str, ...]], str] = {}
        for _round in range(_MAX_PLAN_ROUNDS):
            progressed = False
            for finding in self._live_findings():
                rule_id = finding.rule_id
                witnesses = tuple(finding.witnesses)
                if not witnesses:
                    continue
                # A finding declined last round is tried again: another net's
                # edit since then may have moved what blocked it.
                declined.pop((rule_id, witnesses), None)
                first = self._segment(witnesses[0])
                if first is None:
                    declined[(rule_id, witnesses)] = "segment_not_found"
                    continue
                if rule_id == "redundant_copper_covered":
                    edit: CopperEdit | str = self._plan_covered(first, rule_id, witnesses)
                else:
                    edit = self._plan_chain(rule_id, witnesses, first, pairs)
                if isinstance(edit, CopperEdit):
                    self._edits.append(edit)
                    self._apply(edit)
                    # The next candidate's clearance gate must see this copper.
                    self._rule.invalidate_copper()
                    result.edits.append(edit)
                    progressed = True
                else:
                    declined[(rule_id, witnesses)] = edit
            if not progressed:
                break
        for finding in self._live_findings():
            witnesses = tuple(finding.witnesses)
            result.unrepaired.append(
                CopperUnrepaired(
                    rule_id=finding.rule_id,
                    uuid=witnesses[0] if witnesses else "",
                    net_name=finding.nets[0] if finding.nets else "",
                    reason=declined.get((finding.rule_id, witnesses), "not_planned"),
                )
            )
        return result

    def live_findings(self) -> set[tuple[str, tuple[str, ...]]]:
        """Identities of the repairable findings the rule reports on the board
        as it stands -- ``CopperEdit.finding`` tuples, so a caller can tell a
        finding the kept edits cleared from one they did not."""
        return {(v.rule_id, tuple(v.witnesses)) for v in self._live_findings()}

    def _live_findings(self) -> list[Any]:
        """What the rule reports on the board as it stands now."""
        self._rule.bind(self.pcb, self._design_rules)
        return [
            v
            for v in self._rule.check(self.pcb, self._design_rules).violations
            if v.rule_id in REPAIRED_RULES
        ]

    def _lock_reason(self, segs: Iterable[Segment]) -> str | None:
        by_uuid = {s.uuid: node for s, node in zip(self.pcb.segments, self._seg_nodes, strict=True)}
        for seg in segs:
            node = by_uuid.get(seg.uuid)
            if node is None:
                return "segment_not_found"
            if self._locks.is_locked(node):
                return "locked_segment"
            if self._locks.is_generator_owned(node):
                return "generator_owned"
            if self._locks.is_group_member(node):
                return "group_member"
        return None

    def _plan_covered(
        self, first: Segment, rule_id: str, witnesses: tuple[str, ...]
    ) -> CopperEdit | str:
        """Remove the hidden segment; the covering track stays whole."""
        locked = self._lock_reason([first])
        if locked:
            return locked
        return CopperEdit(
            kind="remove_covered",
            rule_id=rule_id,
            net_name=first.net_name,
            layer=first.layer,
            detail=(
                f"removed {first.width:.3f} mm track hidden under a longer collinear one "
                f"({first.start[0]:.3f}, {first.start[1]:.3f}) -> "
                f"({first.end[0]:.3f}, {first.end[1]:.3f})"
            ),
            removed=(RemovedSegment(first.uuid, first.start, first.end, first.width),),
            template=self._seg_nodes[self.pcb.segments.index(first)].to_string(),
            witnesses=witnesses,
        )

    def _plan_chain(
        self, rule_id: str, witnesses: tuple[str, ...], first: Segment, pairs: dict[int, int]
    ) -> CopperEdit | str:
        """Replace the chain the finding names with one of the rule's legal paths."""
        rule = self._rule
        chain = rule.chain_for(
            witnesses, kind="detour" if rule_id.endswith("detour") else "staircase"
        )
        if chain is None:
            return "chain_not_found"
        segments = [seg for seg, _f, _t in chain]
        locked = self._lock_reason(segments)
        if locked:
            return locked
        if len({seg.net_number for seg in segments}) != 1 or {seg.layer for seg in segments} != {
            first.layer
        }:
            return "chain_not_single_net_layer"
        width = max(seg.width for seg in segments)
        graph = rule._junction_graph(rule.chain_groups(self.pcb)[(first.net_number, first.layer)])
        rejected = self._rejected.get((rule_id, witnesses), {})
        reason = "no_candidate"
        legal: list[tuple[Any, list[Point2], bool]] = []
        for option in rule.chain_options(chain):
            for cand in rule.shortcut_candidates(list(option.ends)):
                if tuple(_q(p) for p in cand) in rejected:
                    reason = rejected[tuple(_q(p) for p in cand)]
                    continue
                why = rule.option_reason(chain, option, cand, graph.terminals, width)
                if why is not None:
                    reason = why
                    continue
                legal.append((option, cand, self._duplicates_copper(cand, width, first, segments)))
        if not legal:
            return reason
        # The first clean path; a path that doubles kept copper only when it
        # is the only legal one (the covered edit then removes the double).
        option, cand, duplicates = next((item for item in legal if not item[2]), legal[0])
        if self._worsens_skew(first, pairs.get(first.net_number), chain, option, cand, width):
            return "would_widen_pair_skew"
        return self._edit_for(rule_id, witnesses, first, chain, option, cand, duplicates, width)

    def _edit_for(
        self,
        rule_id: str,
        witnesses: tuple[str, ...],
        first: Segment,
        chain: list[Any],
        option: Any,
        cand: list[Point2],
        duplicates: bool,
        width: float,
    ) -> CopperEdit | str:
        """The edit that applies the rule's ``option`` with the path ``cand``.

        The rule works to 1 um; the legs are rebuilt from the end nodes as
        the board wrote them, so a new leg ends exactly where its neighbour
        does.  Interior steps are whole segments and go.  An end segment
        the option replaces goes whole; one it trims keeps its copper beyond
        the junction.
        """
        rule = self._rule
        exact_ends = []
        for index in (0, -1):
            seg, (a, b) = rule._step_line(chain[index])
            end_node = option.ends[0 if index == 0 else 1]
            if (a, b) == (seg.start, seg.end) or not option.trims[0 if index == 0 else 1]:
                for endpoint in (seg.start, seg.end):
                    if _q(endpoint) == end_node:
                        exact_ends.append(endpoint)
                        break
                else:
                    exact_ends.append(self._exact(end_node))
            else:
                exact_ends.append(self._exact(end_node))
        paths = rule.shortcut_candidates(exact_ends)
        try:
            exact = paths[
                [tuple(_q(p) for p in path) for path in paths].index(tuple(_q(p) for p in cand))
            ]
        except ValueError:
            return "path_not_reproducible"
        pts = rule.chain_points(chain)
        removed: list[RemovedSegment] = []
        trimmed: list[TrimmedSegment] = []
        for step in chain[1:-1]:
            removed.append(RemovedSegment(step[0].uuid, step[0].start, step[0].end, step[0].width))
        for index, trims in ((0, option.trims[0]), (-1, option.trims[1])):
            seg, (a, b) = rule._step_line(chain[index])
            if (a, b) == (seg.start, seg.end) or not trims:
                if not any(r.uuid == seg.uuid for r in removed):
                    removed.append(RemovedSegment(seg.uuid, seg.start, seg.end, seg.width))
                continue
            cut = exact_ends[0 if index == 0 else 1]
            inner = pts[1 if index == 0 else -2]
            # The chain's piece must run to the segment's own far end; a
            # piece with junctions at both ends is a one-step chain, never
            # a staircase or a detour.
            if _q(inner) not in (_q(seg.start), _q(seg.end)):
                return "cut_piece_interior"
            kept = max((seg.start, seg.end), key=lambda p: math.dist(p, inner))
            trimmed.append(TrimmedSegment(seg.uuid, kept, cut, seg.width))
        legs = [(a, b, width) for a, b in zip(exact, exact[1:], strict=False)]
        added = tuple(
            AddedSegment(
                uuid=self.pcb.mint_uuid(
                    "redundant-copper", first.net_name, first.layer, *a, *b, leg_width, index
                ),
                start=a,
                end=b,
                width=leg_width,
            )
            for index, (a, b, leg_width) in enumerate(legs)
        )
        source = removed[0].uuid if removed else trimmed[0].uuid
        template_segment = self._segment(source)
        if template_segment is None:
            return "segment_not_found"
        template = self._seg_nodes[self.pcb.segments.index(template_segment)].to_string()
        through = f", running through {len(option.landings)} tap(s)" if option.landings else ""
        return CopperEdit(
            kind="rewrite_chain",
            rule_id=rule_id,
            net_name=first.net_name,
            layer=first.layer,
            detail=(
                f"replaced {len(removed) + len(trimmed)} segment(s) with {len(added)}{through}: "
                + " -> ".join(f"({p[0]:.3f}, {p[1]:.3f})" for p in exact)
            ),
            removed=tuple(removed),
            trimmed=tuple(trimmed),
            added=added,
            legs=tuple(exact),
            width=width,
            template=template,
            witnesses=witnesses,
            duplicate_copper=duplicates,
        )

    def _duplicates_copper(
        self, cand: list[Point2], width: float, first: Segment, chain: list[Segment]
    ) -> bool:
        """Does a leg lie on same-net copper that stays (a new covered segment)?"""
        kept = [
            s
            for s in self.pcb.segments
            if s.layer == first.layer and s.net_number == first.net_number and s not in chain
        ]
        for a, b in zip(cand, cand[1:], strict=False):
            for seg in kept:
                if seg.width + 1e-6 < width:
                    continue
                if _on_line(a, seg.start, seg.end) and _on_line(b, seg.start, seg.end):
                    return True
        return False

    def _worsens_skew(
        self,
        member: Segment,
        partner: int | None,
        chain: list[Any],
        option: Any,
        cand: list[Point2],
        width: float,
    ) -> bool:
        """Would the rewrite widen |length(member) - length(partner)| beyond tolerance?"""
        if partner is None:
            return False
        lengths = self._net_lengths()
        before = abs(lengths.get(member.net_number, 0.0) - lengths.get(partner, 0.0))
        old = 0.0
        for index, trims in ((0, option.trims[0]), (-1, option.trims[1])):
            seg, (a, b) = self._rule._step_line(chain[index])
            if (a, b) == (seg.start, seg.end) or not trims:
                old += math.dist(seg.start, seg.end)
            else:
                pts = self._rule.chain_points(chain)
                inner = pts[1 if index == 0 else -2]
                kept = max((seg.start, seg.end), key=lambda p: math.dist(p, inner))
                old += math.dist(kept, self._exact(option.ends[0 if index == 0 else 1]))
        for step in chain[1:-1]:
            old += math.dist(step[0].start, step[0].end)
        new = sum(math.dist(a, b) for a, b in zip(cand, cand[1:], strict=False))
        after = abs(lengths.get(member.net_number, 0.0) - old + new - lengths.get(partner, 0.0))
        return after > before + SKEW_TOLERANCE_MM

    def _net_lengths(self) -> dict[int, float]:
        lengths: dict[int, float] = {}
        for seg in self.pcb.segments:
            lengths[seg.net_number] = lengths.get(seg.net_number, 0.0) + math.dist(
                seg.start, seg.end
            )
        return lengths

    # ------------------------------------------------------------------
    # Veto / retry
    # ------------------------------------------------------------------

    def veto(self, edit: CopperEdit, reason: str) -> list[CopperUnrepaired]:
        """Put an edit's copper back; the board no longer carries it.

        The path it took is remembered as refused for its finding, so the
        next :meth:`plan` tries the chain's other paths before giving up.
        Edits planned after it go too: each was gated against a board that
        carried this one (Board 03's BTN3 staircases merge once the first is
        rewritten), so they are dropped here and planned again by the next
        :meth:`plan`, against the board as it now stands.  Returns one
        :class:`CopperUnrepaired` per edit put back, the vetoed one first.
        """
        if edit not in self._edits:
            return []
        index = self._edits.index(edit)
        dropped = self._edits[index:]
        del self._edits[index:]
        if edit.legs:
            self._rejected.setdefault(edit.finding, {})[edit.path_key] = reason
        self._rebuild()
        return [
            CopperUnrepaired(
                rule_id=e.rule_id,
                uuid=e.uuid,
                net_name=e.net_name,
                reason=reason if e is edit else "replanned",
            )
            for e in dropped
        ]

    def reset(self) -> None:
        """Keep no edit: the board is the input again."""
        self._edits = []
        self._rebuild()

    def widen(self, edit: CopperEdit, dangling: Iterable[str]) -> CopperEdit | None:
        """A covered edit that also removes the stubs its removal left dangling.

        Each named track must be a same-net, same-layer segment whose copper
        lies entirely inside same-net copper that stays once the hidden
        segment and the other stubs are gone -- nothing is lost but the
        overhang kicad-cli reports.  ``None`` when any stub fails that test.
        """
        if edit.kind != "remove_covered":
            return None
        gone = {s.uuid for s in edit.removed}
        stubs = [s for uuid in dangling if (s := self._segment(uuid)) is not None]
        if not stubs or len(stubs) != len(list(dangling)):
            return None
        for stub in stubs:
            if stub.layer != edit.layer or stub.net_name != edit.net_name or stub.uuid in gone:
                return None
        stub_ids = {stub.uuid for stub in stubs}
        net_number = stubs[0].net_number
        keep = [
            LineString([s.start, s.end]).buffer(s.width / 2.0)
            for s in self.pcb.segments
            if s.layer == edit.layer
            and s.net_number == net_number
            and s.uuid not in gone
            and s.uuid not in stub_ids
        ]
        copper = unary_union(keep) if keep else LineString()
        for stub in stubs:
            shape = LineString([stub.start, stub.end]).buffer(stub.width / 2.0)
            if shape.difference(copper).area > 1e-6:
                return None
        locked = self._lock_reason(stubs)
        if locked:
            return None
        widened = CopperEdit(
            kind=edit.kind,
            rule_id=edit.rule_id,
            net_name=edit.net_name,
            layer=edit.layer,
            detail=edit.detail + f"; also removed {len(stubs)} stub(s) left dangling",
            removed=(
                *edit.removed,
                *(RemovedSegment(s.uuid, s.start, s.end, s.width) for s in stubs),
            ),
            trimmed=edit.trimmed,
            added=edit.added,
            legs=edit.legs,
            width=edit.width,
            template=edit.template,
            witnesses=edit.witnesses,
            duplicate_copper=edit.duplicate_copper,
        )
        self._edits[self._edits.index(edit)] = widened
        self._rebuild()
        return widened


def _pair_partners(pcb: PCB) -> dict[int, int]:
    """``{net_number: partner_number}`` for the board's differential pairs."""
    from ..router.diffpair_detection import detect_diff_pairs

    names = {number: net.name for number, net in pcb.nets.items() if net.name}
    by_name = {name: number for number, name in names.items()}
    pairs: dict[int, int] = {}
    for detected in detect_diff_pairs(names):
        a = by_name.get(detected.pair.positive.net_name)
        b = by_name.get(detected.pair.negative.net_name)
        if a is not None and b is not None:
            pairs[a] = b
            pairs[b] = a
    return pairs
