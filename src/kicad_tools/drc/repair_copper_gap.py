"""Same-net ``physical_copper_gap`` repair: close or legalise a slit (#9332).

``physical_copper_gap`` (``validate/rules/physical_gap.py``, issue #8938)
reports two facing edges of the per-layer copper union closer than the fab's
spacing edge -- net-agnostic, because the fab cannot see nets.  A slit whose
two sides are the **same** net is a routing near-miss, not a short risk, and
this module repairs exactly the two same-net classes the issue scopes (split
from #9326; the ``mask_to_copper`` own-pad classes are #9334):

* **via beside its own pad** -- the via is moved along the pad-centre ray
  until the measured gap clears the finding's ``required_value`` plus a
  margin (the board-07 scripted-repair strategy, commit ``c694302cd``), or,
  when the profile declares a via-in-pad process, snapped to the pad centre
  so the copper fully merges;
* **track tangent to its own via** -- the via is snapped onto the track's
  centreline (the canonical track-via join), so the copper fully merges;
* **same-net trace shelf at a routed branch** (#10367) -- a track-track slit
  between a trunk segment and a short staircase of same-net segments that
  leaves the trunk at one of its ends.  The branch chain (the segments
  joined end to end through plain two-segment nodes) is replaced by a
  straight or 45-degree-plus-H/V shortcut that keeps the trunk-attach node,
  the branch's far end and the chain's first and last widths.  The rewrite
  is applied only when the replacement copper clears every neighbour under
  the rule's own copper model and the slit it replaces is gone (see
  :meth:`CopperGapRepairer._repair_shelf`); any pad, via, layer change,
  tap or reinforcement endpoint on an interior node protects the chain.

Everything else is left untouched and listed as unrepaired with a reason:
different-net gaps (never moved by this repairer), zone-fill sides, pad-pad
pairs, track pairs that are not a branch shelf, arcs, footprint polygons and
via-via pairs.  A move
that would crowd foreign copper or drill too close to another hole is
declined (``would_violate_clearance``, re-using the shared clearance gate
inherited from :class:`DrillClearanceRepairer` -- and the same gate covers
the tracks a move drags with it, so a dragged end cannot short a foreign
net the via's own destination clears), same-net segment ends anchored at
the via move with it so the net stays connected, and ``fix-drc``'s
connectivity check remains the net-level backstop.

Usage:
    repairer = CopperGapRepairer("board.kicad_pcb")
    result = repairer.repair_gaps(violations, max_displacement=0.5)
    repairer.save("board-fixed.kicad_pcb")
"""

from __future__ import annotations

import math
import re
import uuid as uuid_lib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]

from ..core.pcb_item_locks import BoardItemLocks
from ..sexp import SExp, parse_string
from .net_compat import resolve_net_atom
from .repair_drill_clearance import DrillClearanceRepairer
from .violation import DRCViolation, ViolationType

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.physical_gap import FabGapPolicy

# Item descriptors (``validate/copper_refs.py``) are quantised to 1 um, so a
# match tolerance of 2 um absorbs the origin add/subtract round trip.
_DESCRIPTOR_TOL_MM = 0.002
_TOLERANCE_MM = 0.01

# ``Via@<x>/<y>:<top>-<bottom>:d<drill>/s<size>`` (copper_refs.via_ref)
_VIA_REF = re.compile(r"^Via@(-?[\d.]+)/(-?[\d.]+):([^:]+):d([\d.]+)/s([\d.]+)$")
# ``Trace@<layer>:w<width>:<x1>/<y1>~<x2>/<y2>`` (copper_refs.segment_ref)
_TRACE_REF = re.compile(r"^Trace@([^:]+):w([\d.]+):(-?[\d.]+)/(-?[\d.]+)~(-?[\d.]+)/(-?[\d.]+)$")

# A shelf rewrite replaces one branch chain, never a general re-route: longer
# chains are declined (``chain_too_long``).
_SHELF_MAX_CHAIN = 8
# Reinforcement sidecar anchors are recorded to 0.1 mm; match them as loosely
# as ``pcb.reinforcement_sidecar`` does (its ``_near`` tolerance).
_REINFORCEMENT_TOL_MM = 0.05
_SHELF_ACTION = "rewrite_shelf"

Point2 = tuple[float, float]


def _in(track: _Trk, tracks: list[_Trk]) -> bool:
    """Identity membership: two segments with equal fields are still two segments."""
    return any(track is t for t in tracks)


class _Trk(NamedTuple):
    """A same-net, same-layer segment: its node, ends and width."""

    node: SExp
    a: Point2
    b: Point2
    width: float


@dataclass
class ShelfRewrite:
    """A branch-chain shelf rewrite: what was removed, what replaced it (#10367).

    ``removed`` holds the original segment nodes as source text, in chain
    order, so :meth:`CopperGapRepairer.undo_move` can put them back even in a
    freshly loaded document; ``added`` identifies the replacement segments
    (uuid, start, end, width) so undo can find them again.
    """

    layer: str
    trunk_node: Point2  # where the chain leaves the trunk (preserved)
    far_node: Point2  # the chain's far end (preserved)
    shelf: tuple[Point2, Point2]  # the finding's shelf segment
    removed: list[str]
    added: list[tuple[str, Point2, Point2, float]]

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "trunk_node": list(self.trunk_node),
            "far_node": list(self.far_node),
            "shelf": [list(self.shelf[0]), list(self.shelf[1])],
            "removed_segments": len(self.removed),
            "added_segments": [
                {"uuid": u, "from": list(a), "to": list(b), "width": w} for u, a, b, w in self.added
            ],
        }


@dataclass
class CopperGapAction:
    """Record of one applied (or, in a dry run, predicted) via move.

    The signed ``displacement_x`` / ``displacement_y`` fields let callers
    reverse the move, and ``joined`` records the same-net segment ends that
    moved with the via so :meth:`CopperGapRepairer.undo_move` can restore
    them together.
    """

    action: str  # "snap_to_pad" | "snap_to_track" | "open_gap"
    uuid: str  # the via's uuid ("" when the via carries none)
    via_x: float
    via_y: float
    new_x: float
    new_y: float
    net_name: str
    detail: str
    displacement_mm: float = 0.0
    displacement_x: float = 0.0
    displacement_y: float = 0.0
    # Same-net segment ends that shared the via position and moved with it:
    # (segment uuid, "start"|"end", original end x, original end y).  The
    # original coordinates let :meth:`CopperGapRepairer.undo_move` restore
    # each end exactly -- an end may sit microns off the via centre, and the
    # via's own centre is the wrong restore value for it.
    joined: list[tuple[str, str, float, float]] = field(default_factory=list)
    shelf: ShelfRewrite | None = None

    def __str__(self) -> str:
        if self.shelf is not None:
            return (
                f"{self.action}: [{self.net_name}] ({self.via_x:.4f}, {self.via_y:.4f}) -> "
                f"({self.new_x:.4f}, {self.new_y:.4f}) - {self.detail}"
            )
        return (
            f"{self.action}: via [{self.net_name}] ({self.via_x:.4f}, {self.via_y:.4f}) -> "
            f"({self.new_x:.4f}, {self.new_y:.4f}) - {self.detail}"
        )


@dataclass
class CopperGapUnrepaired:
    """A finding the repairer left alone, with the reason."""

    reason: str
    uuid: str = ""
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        data: dict = {"reason": self.reason, "uuid": self.uuid}
        if self.location is not None:
            data["x"], data["y"] = self.location
        return data


@dataclass
class CopperGapResult:
    """Summary of a same-net copper-gap repair run."""

    total_violations: int = 0
    repaired: int = 0
    snapped_to_pad: int = 0
    snapped_to_track: int = 0
    opened: int = 0
    rewritten_shelves: int = 0
    actions: list[CopperGapAction] = field(default_factory=list)
    unrepaired: list[CopperGapUnrepaired] = field(default_factory=list)

    def skip(self, reason: str, violation: DRCViolation, uuid: str = "") -> None:
        loc = violation.locations[0] if violation.locations else None
        self.unrepaired.append(
            CopperGapUnrepaired(
                reason=reason,
                uuid=uuid,
                location=(loc.x_mm, loc.y_mm) if loc is not None else None,
            )
        )

    def skipped(self, reason: str) -> int:
        return sum(1 for u in self.unrepaired if u.reason == reason)

    def summary(self) -> str:
        lines = [f"Copper-gap repair: {self.repaired}/{self.total_violations} findings fixed"]
        if self.snapped_to_pad:
            lines.append(f"  Snapped vias to their own pad centre: {self.snapped_to_pad}")
        if self.snapped_to_track:
            lines.append(f"  Snapped vias onto their own track: {self.snapped_to_track}")
        if self.opened:
            lines.append(f"  Moved vias clear of their own pad: {self.opened}")
        if self.rewritten_shelves:
            lines.append(f"  Rewrote same-net trace shelves at branches: {self.rewritten_shelves}")
        reasons: dict[str, int] = {}
        for item in self.unrepaired:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        for reason, count in sorted(reasons.items()):
            lines.append(f"  Skipped ({reason}): {count}")
        return "\n".join(lines)


def copper_gap_violations(violations: list[DRCViolation]) -> list[DRCViolation]:
    """Error-severity same-net ``physical_copper_gap`` findings fix-drc repairs.

    The rule labels same-net findings by their single net name
    (``len(nets) == 1``, ``validate/rules/physical_gap.py``), so the net list
    reproduces the flag on the report side.  Different-net and zone-fill
    ``info`` findings are excluded.
    """
    return [
        v
        for v in violations
        if v.type == ViolationType.PHYSICAL_COPPER_GAP and v.is_error and len(set(v.nets)) == 1
    ]


def _num(node: SExp | None) -> float:
    """First atom of ``node`` as a number (0.0 when absent)."""
    atom = node.get_first_atom() if node is not None else None
    return float(atom) if atom is not None else 0.0


def _bare(value: object) -> str:
    return str(value).strip().strip('"')


def _xy(node: SExp | None) -> tuple[float, float] | None:
    if node is None:
        return None
    atoms = node.get_atoms()
    if len(atoms) < 2:
        return None
    return float(atoms[0]), float(atoms[1])


def _pair_within_tol(
    a: Sequence[tuple[float, float]],
    b: Sequence[tuple[float, float]],
) -> bool:
    """Pointwise endpoint match within ``_DESCRIPTOR_TOL_MM``."""
    return (
        math.dist(a[0], b[0]) <= _DESCRIPTOR_TOL_MM and math.dist(a[1], b[1]) <= _DESCRIPTOR_TOL_MM
    )


class CopperGapRepairer(DrillClearanceRepairer):
    """Repairs same-net ``physical_copper_gap`` slits by moving the via.

    Subclasses :class:`DrillClearanceRepairer` to reuse its net parsing, its
    ``_update_segment_endpoint`` helper for dragging attached track ends, and
    its ``_target_clearance_reason`` gate that re-validates every computed
    move against the shared clearance engine before it is applied.
    """

    def __init__(
        self,
        pcb_path: str | Path,
        project_pcb_path: str | Path | None = None,
        *,
        fab_gap_policy: FabGapPolicy | None = None,
    ):
        self._fab_gap_policy = fab_gap_policy
        super().__init__(pcb_path)
        self._locks = BoardItemLocks(self.doc)
        # The reinforcement sidecar sits next to the *input* board; a repair
        # pass that loads a copy (``--output``) still honours it (#10367).
        self._sidecar_board = Path(project_pcb_path) if project_pcb_path else self.path
        # The physical-gap copper model of the current document, built lazily
        # and dropped whenever the document is edited.
        self._gap_model_cache: tuple[PCB, list, bool] | None = None

    def repair_gaps(
        self,
        violations: list[DRCViolation],
        max_displacement: float = 0.5,
        margin: float = 0.01,
        dry_run: bool = False,
        design_rules: DesignRules | None = None,
    ) -> CopperGapResult:
        """Repair the same-net via/own-pad, track/own-via and track-track shelf subset.

        Args:
            violations: Any violations; only error-severity same-net
                ``physical_copper_gap`` findings are considered.
            max_displacement: Longest via move allowed, in mm.
            margin: Extra gap beyond the finding's ``required_value`` an
                away-move must open, in mm.
            dry_run: Compute actions without touching the document.
            design_rules: Active manufacturer rules.  Every move is
                re-validated against the shared clearance engine before it
                is applied; a move that would introduce a new violation is
                declined.  A declared via-in-pad process additionally allows
                the snap-to-pad-centre strategy.
        """
        result = CopperGapResult()
        findings = copper_gap_violations(violations)
        result.total_violations = len(findings)
        if not findings:
            return result

        pads: dict[str, tuple[BaseGeometry, tuple[float, float], list[str], str, str]] | None = None
        moved: set[int] = set()
        claimed_chains: set[int] = set()

        for violation in findings:
            pair = self._classify_items(violation.items)
            if pair is None:
                tracks = self._track_pair(violation.items)
                if tracks is None:
                    result.skip("unsupported_pair", violation)
                    continue
                shelf_reason, shelf_uuid = self._repair_shelf(
                    violation, tracks, result, dry_run, design_rules, claimed_chains
                )
                if shelf_reason is not None:
                    result.skip(shelf_reason, violation, shelf_uuid)
                continue
            via_desc, other_desc = pair

            via = self._find_via(via_desc)
            if via is None:
                result.skip("via_not_found", violation)
                continue
            via_node, vx, vy, vsize, vnet = via
            if id(via_node) in moved:
                result.skip("via_already_moved", violation)
                continue

            track = self._find_segment(other_desc) if _TRACE_REF.match(other_desc) else None
            if track is not None:
                reason = self._repair_track_pair(
                    via_node,
                    vx,
                    vy,
                    vsize,
                    vnet,
                    track,
                    result,
                    max_displacement,
                    dry_run,
                    design_rules,
                )
            elif _TRACE_REF.match(other_desc):
                reason = "track_not_found"
            else:
                if pads is None:
                    pads = self._pads()
                entry = pads.get(other_desc)
                if entry is None:
                    reason = "side_not_found"
                else:
                    reason = self._repair_pad_pair(
                        violation,
                        via_node,
                        vx,
                        vy,
                        vsize,
                        vnet,
                        entry,
                        result,
                        max_displacement,
                        margin,
                        dry_run,
                        design_rules,
                    )
            if reason is None:
                moved.add(id(via_node))
            else:
                result.skip(reason, violation, self._via_uuid(via_node))
        return result

    # ------------------------------------------------------------------
    # Item resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_items(items: list[str]) -> tuple[str, str] | None:
        """Split a finding's two item descriptors into ``(via, other)``.

        Returns ``None`` unless exactly one side is a via descriptor -- the
        pad-pad, track-track (handled by :meth:`_track_pair`), via-via, zone,
        arc and fp_poly classes are not via moves.
        """
        vias = [str(i) for i in items if _VIA_REF.match(str(i))]
        others = [str(i) for i in items if not _VIA_REF.match(str(i))]
        if len(vias) != 1 or len(others) != 1:
            return None
        return vias[0], others[0]

    @staticmethod
    def _track_pair(items: list[str]) -> tuple[str, str] | None:
        """The two ``Trace@`` descriptors of a track-track finding, else ``None``."""
        traces = [str(i) for i in items if _TRACE_REF.match(str(i))]
        if len(traces) != 2 or len(items) != 2:
            return None
        return traces[0], traces[1]

    def _find_via(self, desc: str) -> tuple[SExp, float, float, float, str] | None:
        """Resolve a ``Via@`` descriptor to ``(node, x, y, size, net)``."""
        match = _VIA_REF.match(desc)
        if match is None:
            return None
        x, y = float(match.group(1)), float(match.group(2))
        span = match.group(3)
        drill, size = float(match.group(4)), float(match.group(5))
        for via_node in self.doc.find_all("via"):
            if self._via_span(via_node) != span:
                continue
            at = _xy(via_node.find("at"))
            if at is None or math.dist(at, (x, y)) > _DESCRIPTOR_TOL_MM:
                continue
            d, s = _num(via_node.find("drill")), _num(via_node.find("size"))
            if abs(d - drill) > _DESCRIPTOR_TOL_MM or abs(s - size) > _DESCRIPTOR_TOL_MM:
                continue
            return via_node, at[0], at[1], s, self._net_name(via_node)
        return None

    @staticmethod
    def _via_span(via_node: SExp) -> str:
        """The layer span ``copper_refs.via_ref`` writes for a via node.

        Same construction as the descriptor (``_clean`` each layer name,
        order by physical stack position, first-last), so a descriptor
        naming a different span never resolves to this via.
        """
        from kicad_tools.validate.copper_refs import _clean, _layer_order

        layers_node = via_node.find("layers")
        names = [_bare(a) for a in layers_node.get_atoms()] if layers_node is not None else []
        ordered = sorted((_clean(n) for n in names if n), key=_layer_order)
        if len(ordered) >= 2:
            return f"{ordered[0]}-{ordered[-1]}"
        return ordered[0] if ordered else "?"

    def _find_segment(
        self, desc: str
    ) -> tuple[tuple[float, float], tuple[float, float], float, str] | None:
        """Resolve a ``Trace@`` descriptor to ``(start, end, width, net)``."""
        seg_node = self._find_segment_node(desc)
        if seg_node is None:
            return None
        start, end = _xy(seg_node.find("start")), _xy(seg_node.find("end"))
        if start is None or end is None:  # pragma: no cover - guarded by the finder
            return None
        return start, end, _num(seg_node.find("width")), self._net_name(seg_node)

    def _find_segment_node(self, desc: str) -> SExp | None:
        """Resolve a ``Trace@`` descriptor to its ``segment`` node."""
        from kicad_tools.validate.copper_refs import _clean

        match = _TRACE_REF.match(desc)
        if match is None:
            return None
        layer = _clean(match.group(1))
        width = float(match.group(2))
        ends = sorted(
            (
                (float(match.group(3)), float(match.group(4))),
                (float(match.group(5)), float(match.group(6))),
            )
        )
        for seg_node in self.doc.find_all("segment"):
            layer_node = seg_node.find("layer")
            seg_layer = _bare(layer_node.get_first_atom()) if layer_node is not None else ""
            if _clean(seg_layer) != layer:
                continue
            w_node = seg_node.find("width")
            if w_node is None or abs(_num(w_node) - width) > _DESCRIPTOR_TOL_MM:
                continue
            start, end = _xy(seg_node.find("start")), _xy(seg_node.find("end"))
            if start is None or end is None:
                continue
            # Descriptor endpoints are quantised to 1 um and sorted *after*
            # quantisation (``copper_refs.segment_ref``), while these are raw
            # file coordinates -- so match as an unordered pair within the
            # descriptor tolerance (the two sorts can disagree for endpoints
            # within ~1 um of each other).
            pair = sorted((start, end))
            if not (_pair_within_tol(pair, ends) or _pair_within_tol(pair, (ends[1], ends[0]))):
                continue
            return seg_node
        return None

    def _net_name(self, node: SExp) -> str:
        net_node = node.find("net")
        atom = net_node.get_first_atom() if net_node else None
        _, name = resolve_net_atom(None if atom is None else str(atom), self.nets, self.net_names)
        return name or ""

    @staticmethod
    def _via_uuid(via_node: SExp) -> str:
        uuid_node = via_node.find("uuid")
        atom = uuid_node.get_first_atom() if uuid_node else None
        return _bare(atom) if atom is not None else ""

    def _pads(
        self,
    ) -> dict[str, tuple[BaseGeometry, tuple[float, float], list[str], str, str]]:
        """``identity -> (polygon, centre, layers, net name, ref)`` in file coords.

        The identity is the same one ``validate/rules/physical_gap.py``
        writes into a finding's ``items`` (``pad.uuid`` else
        ``"<reference>:pad:<enumerate index>"``), so a finding's pad side
        resolves without parsing its message.  The polygon is the rule's own
        model (``clearance._pad_polygon``, board-relative) translated by the
        board origin into the file frame the descriptors use.
        """
        from shapely.affinity import translate  # type: ignore[import-untyped]

        from kicad_tools.core.geometry import rotate_pad_offset
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.clearance import _pad_polygon

        pcb = PCB(self.doc)
        ox, oy = pcb._board_origin
        pads: dict[str, tuple[BaseGeometry, tuple[float, float], list[str], str, str]] = {}
        for fp in pcb.footprints:
            for index, pad in enumerate(fp.pads):
                lx, ly = pad.position
                rx, ry = rotate_pad_offset(lx, ly, fp.rotation)
                cx = fp.position[0] + rx + ox
                cy = fp.position[1] + ry + oy
                identity = pad.uuid or f"{fp.reference}:pad:{index}"
                if identity in pads:
                    continue
                # The same pads ``physical_gap`` collects (``#9399``): any
                # other shape can never be a finding's pad side.
                if pad.type == "np_thru_hole":
                    continue
                if pad.shape not in {"circle", "rect", "oval", "obround", "roundrect"}:
                    continue
                poly = _pad_polygon(pad, fp)
                if poly is None:
                    continue
                if ox or oy:
                    poly = translate(poly, ox, oy)
                pads[identity] = (
                    poly,
                    (cx, cy),
                    list(pad.layers),
                    pad.net_name or "",
                    f"{fp.reference}/{pad.number}",
                )
        return pads

    # ------------------------------------------------------------------
    # Repair strategies
    # ------------------------------------------------------------------

    def _repair_track_pair(
        self,
        via_node: SExp,
        vx: float,
        vy: float,
        vsize: float,
        vnet: str,
        track: tuple[tuple[float, float], tuple[float, float], float, str],
        result: CopperGapResult,
        max_displacement: float,
        dry_run: bool,
        design_rules: DesignRules | None,
    ) -> str | None:
        """Snap a via onto its own track's centreline; ``None`` on success."""
        start, end, width, seg_net = track
        if not vnet or vnet != seg_net:
            return "not_same_net"
        if self._locks.is_locked(via_node):
            return "locked_via"

        ux, uy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(ux, uy)
        if length < 1e-9:
            return "malformed_track"
        t = ((vx - start[0]) * ux + (vy - start[1]) * uy) / (length * length)
        t = min(1.0, max(0.0, t))
        tx, ty = start[0] + t * ux, start[1] + t * uy

        # The via disc centred on the centreline always overlaps the track's
        # buffered copper (the canonical join); verify rather than assume.
        track_copper = LineString([start, end]).buffer(width / 2, quad_segs=16)
        if Point((tx, ty)).buffer(vsize / 2, quad_segs=32).distance(track_copper) > 1e-9:
            return "would_not_merge"

        return self._apply_move(
            via_node,
            vx,
            vy,
            tx,
            ty,
            vnet,
            "snap_to_track",
            "snapped onto its own track centreline",
            result,
            max_displacement,
            dry_run,
            design_rules,
        )

    def _repair_pad_pair(
        self,
        violation: DRCViolation,
        via_node: SExp,
        vx: float,
        vy: float,
        vsize: float,
        vnet: str,
        pad: tuple[BaseGeometry, tuple[float, float], list[str], str, str],
        result: CopperGapResult,
        max_displacement: float,
        margin: float,
        dry_run: bool,
        design_rules: DesignRules | None,
    ) -> str | None:
        """Move a via relative to its own pad; ``None`` on success."""
        poly, centre, _pad_layers, pad_net, _pad_ref = pad
        if not vnet or vnet != pad_net:
            return "not_same_net"
        if self._locks.is_locked(via_node):
            return "locked_via"
        # ``already_merged`` requires actual copper contact (distance 0);
        # a small positive gap is a real slit the repair must clear, not a
        # stale finding (judge note on PR #9396: a measured 0.004 mm gap was
        # wrongly declined as merged by a 0.005 mm contact tolerance).
        if Point((vx, vy)).buffer(vsize / 2, quad_segs=32).distance(poly) <= 1e-9:
            # Already merged: the finding is stale for this pair.
            return "already_merged"

        snap_ok = (
            design_rules is not None
            and bool(design_rules.via_in_pad_supported)
            and bool(design_rules.via_in_pad_process_id)
        )
        if snap_ok:
            tx, ty = centre
            if not Point((tx, ty)).buffer(vsize / 2, quad_segs=32).intersects(poly):
                return "would_not_merge"
            action, detail = "snap_to_pad", "snapped to its own pad centre"
        else:
            # No declared via-in-pad process: a centred via would trip the
            # via_in_pad rule, so move the via clear of the pad instead
            # (the board-07 strategy, commit c694302cd) until the measured
            # gap clears the finding's required value plus the margin.
            required = violation.required_value_mm
            if required is None:
                return "no_required_value"
            target = self._push_clear_of(vx, vy, vsize, poly, centre, required + margin)
            if target is None:
                return "infeasible"
            tx, ty = target
            action = "open_gap"
            detail = f"moved clear of its own pad ({required + margin:.4f}mm gap)"

        return self._apply_move(
            via_node,
            vx,
            vy,
            tx,
            ty,
            vnet,
            action,
            detail,
            result,
            max_displacement,
            dry_run,
            design_rules,
        )

    @staticmethod
    def _push_clear_of(
        vx: float,
        vy: float,
        vsize: float,
        poly: BaseGeometry,
        centre: tuple[float, float],
        target_gap: float,
        iterations: int = 8,
    ) -> tuple[float, float] | None:
        """Push the via along the pad-centre ray until its gap clears ``target_gap``.

        Each round re-measures the true minimum distance (the pad may be
        closer at a corner than along the ray) and pushes the residue; the
        direction re-anchors on the moving position, so a corner pad
        converges in a few rounds.  Returns ``None`` when it does not
        converge (degenerate geometry or a pathological pad).
        """
        tx, ty = vx, vy
        for _ in range(iterations):
            gap = Point((tx, ty)).buffer(vsize / 2, quad_segs=32).distance(poly)
            if gap >= target_gap - 1e-9:
                return tx, ty
            dx, dy = tx - centre[0], ty - centre[1]
            norm = math.hypot(dx, dy)
            if norm < 1e-9:
                return None
            push = target_gap - gap
            tx, ty = tx + dx / norm * push, ty + dy / norm * push
        return None

    # ------------------------------------------------------------------
    # Same-net trace shelf at a routed branch (#10367)
    # ------------------------------------------------------------------

    def _repair_shelf(
        self,
        violation: DRCViolation,
        tracks: tuple[str, str],
        result: CopperGapResult,
        dry_run: bool,
        design_rules: DesignRules | None,
        claimed: set[int],
    ) -> tuple[str | None, str]:
        """Rewrite the branch chain behind a track-track slit.

        The finding's two segments are a *trunk* segment and a *shelf*
        segment that belongs to a chain of same-net, same-layer segments
        (joined end to end through plain two-segment nodes) leaving one of
        the trunk's ends.  The chain is replaced by a straight or
        45-degree-plus-H/V shortcut between the same two end nodes: the first
        leg keeps the chain's first width and the second its last, so a
        single width step survives.

        Before anything is written the replacement is proven against the
        physical-gap rule's own copper model (``physical_gap._collect`` --
        pads, vias, tracks, arcs, filled zones): every neighbouring foreign
        source must keep the profile's clearance, and no slit below the
        finding's ``required_value`` may be introduced or exposed (which also
        proves the original slit is gone).  Continuity holds by
        construction: both end nodes keep their exact coordinates and every
        replacement leg shares a node with the next.  Returns
        ``(skip reason | None, uuid)``.
        """
        a_node, b_node = (self._find_segment_node(d) for d in tracks)
        if a_node is None or b_node is None or a_node is b_node:
            return "unsupported_pair", ""
        layer = self._segment_layer(a_node)
        if layer != self._segment_layer(b_node):
            return "unsupported_pair", ""
        net = self._net_name(a_node)
        if not net or net != self._net_name(b_node):
            return "not_same_net", ""
        required = violation.required_value_mm
        if required is None:
            return "no_required_value", ""

        track_list = self._layer_tracks(layer, net)
        found, failure = self._shelf_chain(a_node, b_node, track_list)
        if found is None:
            return (failure or "unsupported_pair"), ""
        chain, pts, shelf = found
        first_uuid = self._segment_uuid(chain[0].node)
        if any(id(t.node) in claimed for t in chain):
            return "shelf_already_rewritten", first_uuid
        if len(chain) < 2:
            return "no_simplification", first_uuid
        if any(self._locks.is_locked(t.node) for t in chain):
            return "locked_segment", first_uuid
        if any(self._locks.is_generator_owned(t.node) for t in chain):
            return "generator_owned", first_uuid
        if any(self._locks.is_group_member(t.node) for t in chain):
            return "group_member", first_uuid
        protected = self._protected_node_reason(pts, chain, layer, net, track_list)
        if protected is not None:
            return protected, first_uuid

        profile = self._width_profile([t.width for t in chain])
        if profile is None:
            return "width_profile_unsupported", first_uuid
        step = pts[profile[2]] if profile[2] is not None else None
        candidates = self._shelf_candidates(pts, profile[0], profile[1], step)
        if not candidates:
            return "no_simplification", first_uuid

        reason = "would_violate_clearance"
        chosen: list[tuple[Point2, Point2, float]] | None = None
        for legs in candidates:
            if self._legs_duplicate_copper(legs, track_list, chain):
                continue
            reason_i = self._shelf_candidate_reason(legs, chain, layer, net, required, design_rules)
            if reason_i is None:
                chosen = legs
                break
            reason = reason_i
        if chosen is None:
            return reason, first_uuid

        rewrite = ShelfRewrite(
            layer=layer,
            trunk_node=pts[0],
            far_node=pts[-1],
            shelf=(shelf.a, shelf.b),
            removed=[t.node.to_string() for t in chain],
            added=[],
        )
        new_nodes: list[SExp] = []
        for index, (leg_a, leg_b, width) in enumerate(chosen):
            node = parse_string(chain[0].node.to_string())
            for tag, point in (("start", leg_a), ("end", leg_b)):
                point_node = node.find(tag)
                if point_node is not None:
                    point_node.set_value(0, point[0])
                    point_node.set_value(1, point[1])
            width_node = node.find("width")
            if width_node is not None:
                width_node.set_value(0, width)
            seed = f"kct-shelf:{layer}:{net}:{pts[0]}:{pts[-1]}:{index}:{leg_a}:{leg_b}"
            new_id = str(uuid_lib.uuid5(uuid_lib.NAMESPACE_URL, seed))
            for id_tag in ("uuid", "tstamp"):
                id_node = node.find(id_tag)
                if id_node is not None:
                    id_node.set_value(0, new_id)
                    break
            else:
                new_id = ""
            rewrite.added.append((new_id, leg_a, leg_b, width))
            new_nodes.append(node)

        detail = (
            f"replaced a {len(chain)}-segment branch with {len(chosen)} segment(s): shelf "
            f"({shelf.a[0]:.4f}, {shelf.a[1]:.4f}) -> ({shelf.b[0]:.4f}, {shelf.b[1]:.4f}), "
            f"trunk node ({pts[0][0]:.4f}, {pts[0][1]:.4f}), "
            f"far end ({pts[-1][0]:.4f}, {pts[-1][1]:.4f})"
        )
        if not dry_run:
            children = self.doc.children
            positions = [self._child_index(t.node) for t in chain]
            if any(p is None for p in positions):
                return "chain_not_editable", first_uuid
            insert_at = min(p for p in positions if p is not None)
            for pos in sorted((p for p in positions if p is not None), reverse=True):
                del children[pos]
            for offset, node in enumerate(new_nodes):
                children.insert(insert_at + offset, node)
            self._gap_model_cache = None
            self.modified = True
        claimed.update(id(t.node) for t in chain)

        result.actions.append(
            CopperGapAction(
                action=_SHELF_ACTION,
                uuid=first_uuid,
                via_x=pts[0][0],
                via_y=pts[0][1],
                new_x=pts[-1][0],
                new_y=pts[-1][1],
                net_name=net,
                detail=detail,
                shelf=rewrite,
            )
        )
        result.repaired += 1
        result.rewritten_shelves += 1
        return None, first_uuid

    @staticmethod
    def _legs_duplicate_copper(
        legs: list[tuple[Point2, Point2, float]], tracks: list[_Trk], chain: list[_Trk]
    ) -> bool:
        """Does any replacement leg lie wholly on same-net copper that stays?

        Such a leg (typically an H/V leg collinear with the trunk) adds
        nothing but a covered duplicate -- ``redundant_copper_covered`` -- so
        the candidate is skipped in favour of the next one.
        """
        from shapely.geometry import LineString

        for a, b, width in legs:
            for track in tracks:
                if _in(track, chain) or track.width < width - 1e-6:
                    continue
                line = LineString([track.a, track.b])
                if Point(a).distance(line) <= 0.001 and Point(b).distance(line) <= 0.001:
                    return True
        return False

    def _child_index(self, node: SExp) -> int | None:
        """Index of ``node`` among the document's top-level children (by identity)."""
        for index, child in enumerate(self.doc.children):
            if child is node:
                return index
        return None

    @staticmethod
    def _segment_layer(seg_node: SExp) -> str:
        layer_node = seg_node.find("layer")
        return _bare(layer_node.get_first_atom()) if layer_node is not None else ""

    def _layer_tracks(self, layer: str, net: str) -> list[_Trk]:
        """Every same-net segment on ``layer`` with readable geometry."""
        tracks: list[_Trk] = []
        for seg_node in self.doc.find_all("segment"):
            if self._segment_layer(seg_node) != layer or self._net_name(seg_node) != net:
                continue
            start, end = _xy(seg_node.find("start")), _xy(seg_node.find("end"))
            if start is None or end is None:
                continue
            tracks.append(_Trk(seg_node, start, end, _num(seg_node.find("width"))))
        return tracks

    @staticmethod
    def _touches(track: _Trk, point: Point2) -> bool:
        return (
            math.dist(track.a, point) <= _TOLERANCE_MM or math.dist(track.b, point) <= _TOLERANCE_MM
        )

    def _shelf_chain(
        self, a_node: SExp, b_node: SExp, tracks: list[_Trk]
    ) -> tuple[tuple[list[_Trk], list[Point2], _Trk] | None, str | None]:
        """Find the branch chain that holds the shelf and leaves the trunk.

        The shelf is the shorter of the finding's two segments and the trunk
        the longer (a tie is no reading: neither can be told from a branch
        step).  The chain leaves one of the trunk's ends.  Returns
        ``((chain, node points, shelf segment) | None, failure reason)``; the
        reason is ``chain_too_long`` (more than ``_SHELF_MAX_CHAIN``
        segments), ``protected_tap`` (the shelf lies beyond a T-junction on the
        way out of the trunk) or ``None`` (no chain at all).
        """
        by_node = {id(t.node): t for t in tracks}
        a, b = by_node.get(id(a_node)), by_node.get(id(b_node))
        if a is None or b is None:
            return None, None
        len_a, len_b = math.dist(a.a, a.b), math.dist(b.a, b.b)
        if abs(len_a - len_b) <= _TOLERANCE_MM:
            return None, None
        shelf, trunk = (a, b) if len_a < len_b else (b, a)
        best: tuple[float, list[_Trk], list[Point2]] | None = None
        failure: str | None = None
        for trunk_node in (trunk.a, trunk.b):
            for first in tracks:
                if first is trunk or not self._touches(first, trunk_node):
                    continue
                walked = self._walk_chain(first, trunk_node, trunk, tracks)
                if walked is None:
                    continue
                chain, pts, overlong = walked
                if not _in(shelf, chain):
                    if self._shelf_beyond_fork(chain, pts, shelf, trunk, tracks):
                        failure = failure or "protected_tap"
                    continue
                if overlong:
                    failure = "chain_too_long"
                    continue
                length = sum(math.dist(t.a, t.b) for t in chain)
                if best is None or length < best[0]:
                    best = (length, chain, pts)
        if best is None:
            return None, failure
        return (best[1], best[2], shelf), None

    def _shelf_beyond_fork(
        self, chain: list[_Trk], pts: list[Point2], shelf: _Trk, trunk: _Trk, tracks: list[_Trk]
    ) -> bool:
        """Does the walk stop at a T-junction whose other branches hold ``shelf``?"""
        fork = pts[-1]
        if self._touches(trunk, fork):
            return False
        others = [
            t for t in tracks if t is not trunk and self._touches(t, fork) and not _in(t, chain)
        ]
        if len(others) < 2:
            return False
        for other in others:
            walked = self._walk_chain(other, fork, trunk, tracks)
            if walked is not None and _in(shelf, walked[0]):
                return True
        return False

    def _walk_chain(
        self, first: _Trk, start: Point2, trunk: _Trk, tracks: list[_Trk]
    ) -> tuple[list[_Trk], list[Point2], bool] | None:
        """Follow ``first`` away from ``start`` through plain two-segment nodes.

        Stops at a free end, a fork (two or more other segments), or the trunk
        (a loop back).  ``True`` in the result flags a chain longer than
        ``_SHELF_MAX_CHAIN``.
        """
        chain = [first]
        pts: list[Point2] = [start]
        current, at = first, start
        while True:
            if math.dist(current.a, current.b) <= _TOLERANCE_MM:
                return None
            if math.dist(current.a, at) <= _TOLERANCE_MM:
                nxt = current.b
            elif math.dist(current.b, at) <= _TOLERANCE_MM:
                nxt = current.a
            else:
                return None
            pts.append(nxt)
            others = [
                t
                for t in tracks
                if t is not trunk and all(t is not c for c in chain) and self._touches(t, nxt)
            ]
            if self._touches(trunk, nxt) or len(others) != 1:
                return chain, pts, False
            if len(chain) >= _SHELF_MAX_CHAIN:
                return chain, pts, True
            current, at = others[0], nxt
            chain.append(current)

    @staticmethod
    def _width_profile(widths: list[float]) -> tuple[float, float, int | None] | None:
        """``(first width, last width, index of the step node)`` for one width step.

        ``None`` when the widths change more than once (a taper the two-leg
        replacement cannot reproduce).  The step index is the chain node where
        the width changes, ``None`` for a constant width.
        """
        changes = [i for i in range(1, len(widths)) if abs(widths[i] - widths[i - 1]) > 1e-6]
        if len(changes) > 1:
            return None
        return widths[0], widths[-1], (changes[0] if changes else None)

    @staticmethod
    def _shelf_candidates(
        pts: list[Point2], w_first: float, w_last: float, step: Point2 | None
    ) -> list[list[tuple[Point2, Point2, float]]]:
        """Shortcut polylines between the chain's end nodes, preferred first.

        A straight leg when the ends are axis-aligned or on a 45-degree line,
        else a 45-degree leg followed by an H/V leg, else the H/V leg first.
        Each is a list of ``(start, end, width)`` legs; the first leg takes the
        chain's first width and the last leg its last width.
        """
        f, g = pts[0], pts[-1]
        dx, dy = g[0] - f[0], g[1] - f[1]
        ax, ay = abs(dx), abs(dy)
        tol = 1e-4
        if ax < tol and ay < tol:
            return []
        paths: list[list[Point2]] = []
        if ax < tol or ay < tol or abs(ax - ay) < tol:
            if abs(w_first - w_last) <= 1e-6 or step is None:
                paths.append([f, g])
            else:
                # Keep the width step: split the straight run where the
                # original step node projects onto it.
                t = ((step[0] - f[0]) * dx + (step[1] - f[1]) * dy) / (dx * dx + dy * dy)
                t = min(0.95, max(0.05, t))
                paths.append([f, (round(f[0] + t * dx, 4), round(f[1] + t * dy, 4)), g])
        else:
            m = min(ax, ay)
            sx, sy = math.copysign(1.0, dx), math.copysign(1.0, dy)
            paths.append([f, (round(f[0] + sx * m, 4), round(f[1] + sy * m, 4)), g])
            paths.append([f, (round(g[0] - sx * m, 4), round(g[1] - sy * m, 4)), g])

        candidates: list[list[tuple[Point2, Point2, float]]] = []
        for path in paths:
            if len(path) == len(pts) and all(
                math.dist(p, q) <= 1e-6 for p, q in zip(path, pts, strict=True)
            ):
                continue  # already the shortcut: nothing to simplify
            if any(math.dist(p, q) <= 1e-6 for p, q in zip(path, path[1:], strict=False)):
                continue
            widths = [w_first] if len(path) == 2 else [w_first, w_last]
            candidates.append([(path[i], path[i + 1], widths[i]) for i in range(len(path) - 1)])
        return candidates

    def _protected_node_reason(
        self,
        pts: list[Point2],
        chain: list[_Trk],
        layer: str,
        net: str,
        tracks: list[_Trk],
    ) -> str | None:
        """Why an interior chain node must not be rewritten, else ``None``.

        An interior node is protected when it is a pad (any net), a via, a
        layer transition (a same-net segment or arc on another layer ends
        there), a tap (other same-net copper on this layer meets or crosses
        it) or a recorded reinforcement endpoint.
        """
        interior = pts[1:-1]
        if not interior:
            return None
        from shapely.geometry import LineString

        chain_ids = {id(t.node) for t in chain}
        pads = list(self._pads().values())
        vias = [
            (at, _num(via_node.find("size")))
            for via_node in self.doc.find_all("via")
            if (at := _xy(via_node.find("at"))) is not None
        ]
        other_layer_ends = self._other_layer_ends(layer, net)
        arcs_here = self._arc_ends(layer, net)
        wires = self._reinforcement_points(layer, net)

        for index, node in enumerate(interior, start=1):
            half = max(chain[index - 1].width, chain[index].width) / 2
            point = Point(node)
            for poly, _centre, pad_layers, _pad_net, _ref in pads:
                on_layer = (
                    layer in pad_layers
                    or "*.Cu" in pad_layers
                    or ("F&B.Cu" in pad_layers and layer in ("F.Cu", "B.Cu"))
                )
                if on_layer and point.distance(poly) <= half + _TOLERANCE_MM:
                    return "protected_pad"
            if any(math.dist(at, node) <= size / 2 + _TOLERANCE_MM for at, size in vias):
                return "protected_via"
            if any(math.dist(end, node) <= _TOLERANCE_MM for end in other_layer_ends):
                return "protected_layer_change"
            if any(math.dist(end, node) <= _TOLERANCE_MM for end in arcs_here):
                return "protected_tap"
            for other in tracks:
                if id(other.node) in chain_ids:
                    continue
                if point.distance(LineString([other.a, other.b])) <= _TOLERANCE_MM:
                    return "protected_tap"
            if any(math.dist(p, node) <= _REINFORCEMENT_TOL_MM for p in wires):
                return "protected_reinforcement"
        return None

    def _other_layer_ends(self, layer: str, net: str) -> list[Point2]:
        """Ends of same-net segments and arcs routed on a different copper layer."""
        ends: list[Point2] = []
        for kind in ("segment", "arc"):
            for node in self.doc.find_all(kind):
                if self._segment_layer(node) == layer or self._net_name(node) != net:
                    continue
                for tag in ("start", "end"):
                    xy = _xy(node.find(tag))
                    if xy is not None:
                        ends.append(xy)
        return ends

    def _arc_ends(self, layer: str, net: str) -> list[Point2]:
        """Ends of same-net arcs on ``layer`` (copper a segment chain can meet)."""
        ends: list[Point2] = []
        for node in self.doc.find_all("arc"):
            if self._segment_layer(node) != layer or self._net_name(node) != net:
                continue
            for tag in ("start", "end"):
                xy = _xy(node.find(tag))
                if xy is not None:
                    ends.append(xy)
        return ends

    def _reinforcement_points(self, layer: str, net: str) -> list[Point2]:
        """Recorded reinforcement wire anchors and leg ends on this net and layer."""
        from kicad_tools.pcb.reinforcement_sidecar import load_wires_if_present

        points: list[Point2] = []
        for wire in load_wires_if_present(self._sidecar_board):
            if wire.get("net") != net or layer not in (wire.get("layer"), wire.get("copy_layer")):
                continue
            for key in ("leg_start", "leg_end"):
                p = wire.get(key)
                if isinstance(p, dict) and "x" in p and "y" in p:
                    points.append((float(p["x"]), float(p["y"])))
            for p in wire.get("anchors", []):
                if isinstance(p, dict) and "x" in p and "y" in p:
                    points.append((float(p["x"]), float(p["y"])))
        return points

    def _gap_model(self) -> tuple[PCB, list] | None:
        """``(board, copper sources)`` of the current document, or ``None``.

        The sources are the physical-gap rule's own (``physical_gap._collect``).
        Geometry the rule cannot model exactly makes the answer ``None`` (no
        rewrite is then provably legal), except an unfilled zone: it has no
        copper to be near.
        """
        if self._gap_model_cache is None:
            from kicad_tools.schema.pcb import PCB
            from kicad_tools.validate.rules.physical_gap import _collect

            pcb = PCB(self.doc, self._sidecar_board)
            sources, unsupported = _collect(pcb)
            incomplete = any(not str(u).startswith("unfilled zone") for u in unsupported)
            self._gap_model_cache = (pcb, sources, incomplete)
        pcb, sources, incomplete = self._gap_model_cache
        return None if incomplete else (pcb, sources)

    def _shelf_obstacle_reason(
        self,
        pcb: PCB,
        candidate: BaseGeometry,
        layer: str,
        net_number: int,
        rules: DesignRules | None,
    ) -> str | None:
        """Check the swept track against rule areas and drills in board coordinates."""
        from shapely.geometry import Polygon

        from kicad_tools.cli.relocate_in_pad_vias import _pad_drill_focus, _resolve_hole_clearance
        from kicad_tools.router.rule_area_resolve import all_rule_area_zones

        for zone in all_rule_area_zones(pcb):
            if zone.keepout is None or zone.keepout.tracks_allowed:
                continue
            layers = zone.layers or [zone.layer]
            wildcard = "F&B.Cu" if layer in ("F.Cu", "B.Cu") else "*.In.Cu"
            if not {layer, "*.Cu", wildcard}.intersection(layers):
                continue
            if len(zone.polygon) < 3:
                return "gap_model_incomplete"
            polygon = Polygon(zone.polygon)
            if not polygon.is_valid:
                return "gap_model_incomplete"
            if candidate.intersects(polygon):
                return "track_keepout"

        try:
            hole_floor = _resolve_hole_clearance(pcb)
        except (OSError, ValueError):
            return "gap_model_incomplete"
        plated_floor = hole_floor
        if rules is not None:
            plated_floor = max(plated_floor, rules.min_pth_hole_to_track_mm or 0)
            if layer not in ("F.Cu", "B.Cu"):
                plated_floor = max(plated_floor, rules.min_inner_pth_hole_to_copper_mm or 0)
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.type not in ("thru_hole", "np_thru_hole"):
                    continue
                plated = pad.type == "thru_hole"
                if plated and pad.net_number == net_number and net_number:
                    continue
                drill = _pad_drill_focus(pad, fp)
                if drill is None:
                    return "gap_model_incomplete"
                focus, radius = drill
                if candidate.distance(focus) < radius + (plated_floor if plated else hole_floor):
                    return "hole_clearance"
        layers_in_order = [item.name for item in pcb.copper_layers]
        for via in pcb.vias:
            if via.net_number == net_number and net_number:
                continue
            first, last = sorted(layers_in_order.index(name) for name in via.layers)
            if (
                layer in layers_in_order[first : last + 1]
                and candidate.distance(Point(via.position)) < via.drill / 2 + hole_floor
            ):
                return "hole_clearance"
        return None

    def _shelf_candidate_reason(
        self,
        legs: list[tuple[Point2, Point2, float]],
        chain: list[_Trk],
        layer: str,
        net: str,
        required: float,
        design_rules: DesignRules | None,
    ) -> str | None:
        """Why this replacement must not be applied, else ``None``.

        Compare the layer's local slits before and after replacing the chain.
        Check clearance and continued contact with neighbouring copper.
        """
        from shapely import union_all  # type: ignore[import-untyped]
        from shapely.geometry import LineString

        from kicad_tools.core.project_clearance import ProjectClearanceError
        from kicad_tools.schema.pcb import Segment
        from kicad_tools.validate.copper_refs import board_origin, segment_ref
        from kicad_tools.validate.rules.netclass_clearance_copper import netclass_pair_requirement
        from kicad_tools.validate.rules.physical_gap import CopperSource, iter_source_gaps

        model = self._gap_model()
        if model is None:
            return "gap_model_incomplete"
        pcb, sources = model
        origin = board_origin(pcb)
        ox, oy = origin

        def rel(p: Point2) -> Point2:
            return (p[0] - ox, p[1] - oy)

        removed = {
            segment_ref(
                Segment(start=rel(t.a), end=rel(t.b), width=t.width, layer=layer, net_number=0),
                origin,
            )
            for t in chain
        }
        candidate_sources = [
            CopperSource(
                LineString([rel(a), rel(b)]).buffer(width / 2, quad_segs=64),
                layer,
                net,
                f"shelf-candidate-{index}",
            )
            for index, (a, b, width) in enumerate(legs)
        ]
        candidate_geom = union_all([c.geometry for c in candidate_sources])
        foreign_required = design_rules.min_clearance_mm if design_rules is not None else required
        numbers = {n.name: n.number for n in pcb.nets.values()}
        net_number = numbers.get(net, 0)
        try:
            pair_requirement = netclass_pair_requirement(self._sidecar_board, pcb, foreign_required)
        except ProjectClearanceError:
            return "gap_model_incomplete"
        obstacle = self._shelf_obstacle_reason(pcb, candidate_geom, layer, net_number, design_rules)
        if obstacle:
            return obstacle
        policy = self._fab_gap_policy
        scan = policy.detection_edge_mm if policy else required
        removed_sources = [s for s in sources if s.layer == layer and s.identity in removed]
        if len(removed_sources) != len(chain):
            return "gap_model_incomplete"
        original_geom = union_all([s.geometry for s in removed_sources])
        minx, miny, maxx, maxy = original_geom.union(candidate_geom).bounds

        neighbours: list[CopperSource] = []
        for source in sources:
            if source.layer != layer or source.geometry.is_empty or source.identity in removed:
                continue
            clearance = pair_requirement(net_number, numbers.get(source.net, 0))
            reach = max(scan, clearance) + _TOLERANCE_MM
            sx0, sy0, sx1, sy1 = source.geometry.bounds
            if sx1 < minx - reach or sx0 > maxx + reach or sy1 < miny - reach or sy0 > maxy + reach:
                continue
            neighbours.append(source)
            if (
                source.net == net
                and original_geom.intersects(source.geometry)
                and not candidate_geom.intersects(source.geometry)
            ):
                if source.kind == "pad":
                    return "protected_pad"
                if source.identity.startswith("Via@"):
                    return "protected_via"
                return "protected_tap"
            if source.net == net:
                continue
            if (
                candidate_geom.intersects(source.geometry)
                or candidate_geom.distance(source.geometry) < clearance - 1e-6
            ):
                return "would_violate_clearance"

        # A previously covered slit can have two surviving owners. Compare
        # witnesses as well as owners so an old slit elsewhere cannot mask it.
        def witnesses(local: list[CopperSource]) -> set[tuple]:
            return {
                (gap.ids, tuple(sorted((round(p.x, 9), round(p.y, 9)) for p in (gap.a, gap.b))))
                for gap in iter_source_gaps(local, scan)
                if policy is None
                or (
                    "zone" not in gap.kinds
                    and (policy.pad if "pad" in gap.kinds else policy.trace).severity_for(
                        gap.distance
                    )
                    is not None
                )
            }

        after = witnesses(neighbours + candidate_sources)
        if after and after - witnesses(neighbours + removed_sources):
            return "gap_not_cleared"

        return None

    def _undo_shelf(self, action: CopperGapAction) -> bool:
        """Put a rewritten shelf's original segments back.

        Finds the replacement segments (by uuid, else by geometry), removes
        them and re-inserts the recorded originals at the same place.
        ``False`` when a replacement segment is gone or was edited.
        """
        shelf = action.shelf
        if shelf is None:
            return False
        positions: list[int] = []
        for new_id, a, b, width in shelf.added:
            matches = [
                seg
                for seg in self.doc.find_children("segment")
                if self._segment_uuid(seg) == new_id
                and self._net_name(seg) == action.net_name
                and self._segment_layer(seg) == shelf.layer
                and abs(_num(seg.find("width")) - width) < 1e-9
                and {_xy(seg.find("start")), _xy(seg.find("end"))} == {a, b}
            ]
            if len(matches) != 1:
                return False
            index = self._child_index(matches[0])
            if index is None or index in positions:
                return False
            positions.append(index)
        children = self.doc.children
        insert_at = min(positions)
        restored = [parse_string(text) for text in shelf.removed]
        for index in sorted(positions, reverse=True):
            del children[index]
        for offset, node in enumerate(restored):
            children.insert(insert_at + offset, node)
        self._gap_model_cache = None
        self.modified = True
        return True

    # ------------------------------------------------------------------
    # Apply / undo / save
    # ------------------------------------------------------------------

    def _apply_move(
        self,
        via_node: SExp,
        vx: float,
        vy: float,
        tx: float,
        ty: float,
        vnet: str,
        action: str,
        detail: str,
        result: CopperGapResult,
        max_displacement: float,
        dry_run: bool,
        design_rules: DesignRules | None,
    ) -> str | None:
        """Validate and apply one via move; ``None`` on success."""
        displacement = math.hypot(tx - vx, ty - vy)
        if displacement < _TOLERANCE_MM / 2:
            return "no_displacement"
        if displacement > max_displacement:
            return "exceeds_max_displacement"

        joined = self._anchored_ends(vx, vy, vnet)
        if joined is None:
            return "locked_attached_track"

        new_x, new_y = round(tx, 4), round(ty, 4)

        if design_rules is not None:
            reason = self._target_clearance_reason(vx, vy, tx, ty, design_rules)
            if reason is not None:
                return "would_violate_clearance"
            if joined:
                # The via's destination is clear, but the move also drags
                # every attached segment end; the dragged copper itself must
                # clear foreign nets or the move is declined (judge note on
                # PR #9396: a snapped via dragged a B.Cu track across a
                # foreign track, saving a -0.1351 mm short).
                if self._dragged_track_reason(joined, new_x, new_y, vnet, design_rules):
                    return "would_violate_clearance"

        record = CopperGapAction(
            action=action,
            uuid=self._via_uuid(via_node),
            via_x=vx,
            via_y=vy,
            new_x=new_x,
            new_y=new_y,
            net_name=vnet,
            detail=detail,
            displacement_mm=displacement,
            displacement_x=new_x - round(vx, 4),
            displacement_y=new_y - round(vy, 4),
            joined=[(uuid, tag, ex, ey) for uuid, _, tag, (ex, ey) in joined],
        )
        result.actions.append(record)

        if not dry_run:
            at_node = via_node.find("at")
            if at_node is not None:
                at_node.set_value(0, new_x)
                at_node.set_value(1, new_y)
            for _uuid, seg_node, _tag, _orig in joined:
                self._update_segment_endpoint(seg_node, vx, vy, new_x, new_y)
            self._gap_model_cache = None
            self.modified = True

        result.repaired += 1
        if action == "snap_to_pad":
            result.snapped_to_pad += 1
        elif action == "snap_to_track":
            result.snapped_to_track += 1
        else:
            result.opened += 1
        return None

    def _anchored_ends(
        self, vx: float, vy: float, net_name: str
    ) -> list[tuple[str, SExp, str, tuple[float, float]]] | None:
        """Same-net segment ends at the via position; ``None`` when locked.

        Every end moves with the via so the tracks stay connected.  A locked
        same-net segment end anchors the via: moving it would detach a track
        the author pinned, so the move is declined instead.  Each entry
        carries the end's current coordinates so the move can be undone to
        exactly where the end started, not to the via's centre.
        """
        anchored: list[tuple[str, SExp, str, tuple[float, float]]] = []
        for seg_node in self.doc.find_all("segment"):
            if not net_name or self._net_name(seg_node) != net_name:
                continue
            if self._locks.is_locked(seg_node) and self._end_at(seg_node, vx, vy) is not None:
                return None
            for tag in ("start", "end"):
                xy = _xy(seg_node.find(tag))
                if xy is not None and math.dist(xy, (vx, vy)) < _TOLERANCE_MM:
                    anchored.append((self._segment_uuid(seg_node), seg_node, tag, xy))
        return anchored

    @staticmethod
    def _end_at(seg_node: SExp, x: float, y: float) -> str | None:
        for tag in ("start", "end"):
            xy = _xy(seg_node.find(tag))
            if xy is not None and math.dist(xy, (x, y)) < _TOLERANCE_MM:
                return tag
        return None

    @staticmethod
    def _segment_uuid(seg_node: SExp) -> str:
        uuid_node = seg_node.find_child("uuid") or seg_node.find_child("tstamp")
        atom = uuid_node.get_first_atom() if uuid_node else None
        return _bare(atom) if atom is not None else ""

    def _dragged_track_reason(
        self,
        joined: list[tuple[str, SExp, str, tuple[float, float]]],
        new_x: float,
        new_y: float,
        net_name: str,
        design_rules: DesignRules,
    ) -> str | None:
        """Reason string if dragging ``joined`` ends to the via's new position
        would put their copper closer than the profile's minimum clearance to
        foreign copper; ``None`` when every final geometry is clear.

        The via disc at its destination is validated separately by
        ``_target_clearance_reason``; this gate covers the tracks the move
        drags with it.  Same-net and unassigned (net 0) copper are exempt,
        mirroring the pairwise clearance rule's own skip.
        """
        from kicad_tools.validate.copper_refs import _clean, _layer_order

        required = design_rules.min_clearance_mm

        def foreign(net: str) -> bool:
            return bool(net) and net != net_name

        for _uuid, seg_node, tag, _orig in joined:
            layer_node = seg_node.find("layer")
            layer = _clean(_bare(layer_node.get_first_atom())) if layer_node is not None else ""
            if not layer:
                continue
            start, end = _xy(seg_node.find("start")), _xy(seg_node.find("end"))
            if start is None or end is None:
                continue
            moved = (new_x, new_y)
            line = LineString([moved if tag == "start" else start, moved if tag == "end" else end])
            half_width = _num(seg_node.find("width")) / 2

            # Foreign routed segments on the dragged track's own layer.
            for other in self.doc.find_all("segment"):
                if other is seg_node:
                    continue
                other_layer_node = other.find("layer")
                other_layer = (
                    _clean(_bare(other_layer_node.get_first_atom()))
                    if other_layer_node is not None
                    else ""
                )
                if other_layer != layer or not foreign(self._net_name(other)):
                    continue
                o_start, o_end = _xy(other.find("start")), _xy(other.find("end"))
                if o_start is None or o_end is None:
                    continue
                gap = (
                    line.distance(LineString([o_start, o_end]))
                    - half_width
                    - _num(other.find("width")) / 2
                )
                if gap < required - 1e-6:
                    return (
                        f"dragged track would clear foreign copper by only {gap:.3f}mm on {layer}"
                    )

            # Foreign vias whose span crosses the dragged track's layer.
            order = _layer_order(layer)
            for via_node in self.doc.find_all("via"):
                if not foreign(self._net_name(via_node)):
                    continue
                span = self._via_span(via_node)
                if "-" in span:
                    bounds = [_layer_order(part) for part in span.split("-", 1)]
                    if not min(bounds) <= order <= max(bounds):
                        continue
                elif span != layer:
                    continue
                at = _xy(via_node.find("at"))
                if at is None:
                    continue
                gap = (
                    line.distance(Point(at).buffer(_num(via_node.find("size")) / 2, quad_segs=32))
                    - half_width
                )
                if gap < required - 1e-6:
                    return f"dragged track would clear a foreign via by only {gap:.3f}mm on {layer}"

            pad_reason = self._pad_obstacle_reason(line, layer, half_width, net_name, required)
            if pad_reason is not None:
                return pad_reason
        return None

    def _pad_obstacle_reason(
        self,
        line: LineString,
        layer: str,
        half_width: float,
        net_name: str,
        required: float,
    ) -> str | None:
        """Clearance of a dragged track's final geometry to foreign pad copper.

        Uses the rule's own pad model (:func:`clearance._pad_polygon`,
        custom pads included) translated into the file frame, and honours
        the ``*.Cu`` / ``F&B.Cu`` wildcard layer forms through-hole pads
        carry.
        """
        # Same lazy import as ``_pads``; its ignore comment covers the
        # module's untyped status for the whole file.
        from shapely.affinity import translate

        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.clearance import _pad_polygon

        pcb = PCB(self.doc)
        ox, oy = pcb._board_origin
        for fp in pcb.footprints:
            for pad in fp.pads:
                pad_net = pad.net_name or ""
                if not pad_net or pad_net == net_name:
                    continue
                layers = list(pad.layers)
                on_layer = (
                    layer in layers
                    or "*.Cu" in layers
                    or ("F&B.Cu" in layers and layer in ("F.Cu", "B.Cu"))
                )
                if not on_layer:
                    continue
                poly = _pad_polygon(pad, fp)
                if poly is None:
                    continue
                if ox or oy:
                    poly = translate(poly, ox, oy)
                gap = line.distance(poly) - half_width
                if gap < required - 1e-6:
                    return (
                        f"dragged track would clear pad {fp.reference}/{pad.number} "
                        f"by only {gap:.3f}mm on {layer}"
                    )
        return None

    def undo_move(self, action: CopperGapAction) -> bool:
        """Put the moved via (and any segment ends that moved with it) back.

        Returns ``False`` when the via is gone or no end sits at the
        post-move position (callers fall back to a snapshot restore).  A
        ``rewrite_shelf`` action puts the replaced branch chain back instead.
        """
        if action.shelf is not None:
            return self._undo_shelf(action)
        target = None
        for via_node in self.doc.find_all("via"):
            if self._via_uuid(via_node) == action.uuid:
                target = via_node
                break
        if target is None:
            return False
        at_node = target.find("at")
        if at_node is None:
            return False
        atoms = at_node.get_atoms()
        cur_x = float(atoms[0]) if atoms else 0.0
        cur_y = float(atoms[1]) if len(atoms) > 1 else 0.0
        if math.dist((cur_x, cur_y), (action.new_x, action.new_y)) > _TOLERANCE_MM:
            return False

        # Restore the via at its original parsed coordinates, unrounded, so
        # sub-micron precision survives the undo exactly as it was.
        old_x, old_y = action.via_x, action.via_y
        restore: list[tuple[SExp, float, float]] = []
        for uuid, tag, end_x, end_y in action.joined:
            seg_node = self._segment_by_uuid(uuid)
            if seg_node is None:
                return False
            end_node = seg_node.find(tag)
            xy = _xy(end_node)
            if (
                end_node is None
                or xy is None
                or math.dist(xy, (action.new_x, action.new_y)) > _TOLERANCE_MM
            ):
                return False
            restore.append((end_node, end_x, end_y))

        at_node.set_value(0, old_x)
        at_node.set_value(1, old_y)
        for end_node, end_x, end_y in restore:
            # Each end returns to its own recorded pre-move coordinates
            # (not the via's old centre: an attached end may sit microns
            # off it), unrounded so sub-micron geometry survives the undo.
            end_node.set_value(0, end_x)
            end_node.set_value(1, end_y)
        self.modified = True
        return True

    def _segment_by_uuid(self, uuid: str) -> SExp | None:
        if not uuid:
            return None
        for seg_node in self.doc.find_all("segment"):
            if self._segment_uuid(seg_node) == uuid:
                return seg_node
        return None
