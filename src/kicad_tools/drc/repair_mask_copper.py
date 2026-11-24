"""Mask-to-copper repair: re-centre a track that enters its own pad off-centre,
or nudge a same-net pass-through clear of its own pad's opening.

``mask_to_copper`` (issue #8858, ``validate/mask_copper.py``) excuses a
same-net track that crosses into its own pad cleanly (``within_own_pad``,
info).  An entry that is shallow, or that arrives at a roundrect corner, keeps
copper under the mask beside the opening and stays an error.  This module
repairs two classes (issue #9334, split from #9332/#9326; the pass-through
nudge is issue #9896):

* **own-pad entry** -- the conductor is a straight ``segment`` (not a via,
  arc or pour), it is on the same net as the single pad that owns the
  opening, exactly one of its ends is inside the pad and the other outside,
  and the repair moves the inside end to the pad centre, which makes the
  entry run straight at the pad.
* **same-net pass-through** (``disjoint`` relation only) -- the conductor, a
  straight ``segment`` or a ``via``, passes *beside* the opening of a pad on
  its own net with no endpoint inside, keeping copper under the mask closer
  than the policy clearance.  The repair moves the conductor clear of the
  opening by the policy clearance plus a margin. A via carries every same-net
  track end anchored at it. A segment carries its joined track ends and can
  pivot at a fixed via when its opposite end shares a movable junction.  One grouped edit per
  conductor must clear **every** opening the conductor has findings against,
  and a candidate that would crowd another opening (or whose joined ends
  would leave a pad they terminate in) is declined.

Everything else (different net, pour, arc, overlap of any kind, a straddle,
both ends inside, an immovable junction, an exotic pad shape,
or a measured clearance the pad-shape opening model cannot reproduce) is
left untouched and listed as unrepaired with a reason.  A move that would
bring the copper closer than the clearance rule to other-net copper, or a
via destination that fails the shared clearance engine (copper, pad or
drill-to-drill), is declined (``would_violate_clearance``).

The repairer cannot re-run the native mask assessment itself; ``fix-drc``
re-checks the saved board and reverts any move that did not clear its finding
or produced a new one (``cli/fix_drc_cmd.py``, ``_run_mask_pass``).

Usage:
    repairer = MaskCopperRepairer("board.kicad_pcb")
    result = repairer.repair(violations, max_displacement=0.5)
    repairer.save("board-fixed.kicad_pcb")
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from shapely.affinity import rotate, translate  # type: ignore[import-untyped]
from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import nearest_points, unary_union  # type: ignore[import-untyped]

from ..schema.pcb import Arc
from ..sexp import SExp
from .mask_trace_angles import MaskTraceAngleError, MaskTraceBend, off_angle_tracks
from .repair_copper_gap import CopperGapRepairer
from .violation import DRCViolation, ViolationType

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules
    from kicad_tools.schema.pcb import PCB, Pad

#: Message marker the mask rule writes for a same-net segment that enters its
#: own pad (``validate/mask_copper.py``).
OWN_PAD_ENTRY_MARKER = "same-net own-pad entry"

#: Message markers a disjoint same-net pass-through finding carries: the net
#: relation plus the disjoint measurement relation.
PASS_THROUGH_MARKER = "same-net pass-through"
DISJOINT_RELATION_MARKER = "Mask-to-copper disjoint"

_TOLERANCE_MM = 0.01
# The mask rule counts an authored end as inside the pad within its construction
# budget (~10 um); a rounded-corner polygon here can differ by a few um more.
_INSIDE_EPS_MM = 0.015
# The native plotted opening can differ from the pad-shape model by the plot's
# chord error and coordinate quantisation (mask_copper_geometry reports a
# ~10.1 um budget).  A candidate the model cannot reproduce within this
# agreement window is refused rather than pushed on a wrong opening shape.
_MASK_UNCERTAINTY_MM = 0.0101
_MODEL_AGREEMENT_MM = 0.05
# Default slack a pass-through nudge must open beyond the policy clearance:
# covers the plot uncertainty above plus re-measurement room, so the saved
# geometry lands past the rule's "uncertain" band rather than inside it.
PASS_THROUGH_MARGIN_MM = 0.02
# Disjoint-via relocation search (issue #9939): when the shortest push is
# vetoed by copper, pad or drill clearance, alternative displacements are
# tried on this many directions (15-degree steps, matching the measured
# Board 03 trial) at ascending magnitudes on this step grid, bounded by the
# caller's ``max_displacement``.
_VIA_SEARCH_DIRECTIONS = 24
_VIA_SEARCH_STEP_MM = 0.01
# Radial copper margin a kept-line connection must retain inside the via
# land after including track width, so rounding can never push added copper
# past the annulus.
_TAIL_LAND_MARGIN_MM = 0.001
# Shortest added connection segment (mm).  A shorter contact collapses into
# the slid endpoint (the end lands inside the land and already touches the
# annulus) instead of serializing a near-zero-length segment.
_MIN_TAIL_MM = 0.005
# Slide search for a jog between two parallel runs: candidate step, the
# perpendicular offset within which a joined run still counts as on the common
# axis, and the shortest run a slide may leave behind.
_SLIDE_STEP_MM = 0.005
_RUN_AXIS_TOLERANCE_MM = 0.0002
_MIN_RUN_MM = 0.05
# The four connection line directions through the new via centre (each used
# in both orientations): 0, 45, 90 and 135 degrees.
_ALIGNED_TAIL_DIRS = ((1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, -1.0))


@dataclass
class MaskRepairAction:
    """One applied (or, in a dry run, predicted) segment-end move."""

    action: str  # "recentre" or "pull_in" (issue #9891)
    uuid: str  # the segment's uuid
    pad_uuid: str
    pad_ref: str
    net_name: str
    layer: str
    old_x: float
    old_y: float
    new_x: float
    new_y: float
    displacement_mm: float
    detail: str = ""
    # Same-net segments whose end shared the moved point and moved with it:
    # (segment uuid, "start"|"end").
    joined: list[tuple[str, str]] = field(default_factory=list)
    # Which of the primary segment's own ends moved ("start"|"end"); with
    # ``joined`` this names every endpoint an undo restores, so a later
    # finding on the same endpoint is declined instead of double-moved
    # (issue #9891: a joined member's near-pad end is often its own
    # pull-in candidate).
    end_tag: str = ""
    bends: list[MaskTraceBend] = field(default_factory=list)
    joined_origins: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"{self.action}: track [{self.net_name}] end ({self.old_x:.4f}, {self.old_y:.4f}) -> "
            f"({self.new_x:.4f}, {self.new_y:.4f}) on pad {self.pad_ref} - {self.detail}"
        )


@dataclass
class MaskNudgeAction:
    """One applied (or, in a dry run, predicted) pass-through nudge.

    A nudge can translate the conductor or keep one segment end fixed at a
    via. Every moved point shares the same ``(dx, dy)``. The grouped edit
    records each point and its pre-move coordinates in ``points`` (a via's own
    point carries ``tag=None``). ``openings`` lists every opening UUID to clear.
    The reported reference point is the moving junction, translated segment
    midpoint or via centre.
    """

    action: str  # always "nudge_clear"
    uuid: str  # the conductor's uuid
    kind: str  # "segment" | "via"
    pad_uuid: str
    pad_ref: str
    net_name: str
    layer: str
    old_x: float
    old_y: float
    new_x: float
    new_y: float
    dx: float
    dy: float
    displacement_mm: float
    detail: str = ""
    openings: list[str] = field(default_factory=list)
    points: list[tuple[str, str, str | None, float, float]] = field(default_factory=list)
    bends: list[MaskTraceBend] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"nudge_clear: {self.kind} [{self.net_name}] ({self.old_x:.4f}, {self.old_y:.4f}) -> "
            f"({self.new_x:.4f}, {self.new_y:.4f}) clear of {self.pad_ref} - {self.detail}"
        )


@dataclass
class MaskViaRelocateAction(MaskNudgeAction):
    """One applied (or, in a dry run, predicted) via relocation with kept
    approach lines (issue #9939's disjoint-via increment).

    The via centre translates by ``(dx, dy)`` like a nudge, but an anchored
    track end does not have to follow it verbatim: each end may slide along
    its own segment line to the point where a 0/45/90/135-degree connection
    -- recorded in ``added_segments`` and lying wholly inside the new via
    land -- runs to the new centre, so no approach changes angle.  ``points``
    still names every moved item (the via, each slid end and every added
    connection) for finding-attribution; undo removes the added copper.
    """

    #: ``(segment uuid, end tag, old_x, old_y, new_x, new_y)`` -- one entry
    #: per anchored end that slid along its own line to its connection point.
    end_moves: list[tuple[str, str, float, float, float, float]] = field(default_factory=list)
    #: ``(uuid, start_x, start_y, end_x, end_y, width, layer, net)`` for each
    #: short aligned connection the action adds inside the new via land.
    added_segments: list[tuple[str, float, float, float, float, float, str, int | str]] = field(
        default_factory=list
    )


@dataclass
class MaskUnrepaired:
    """A finding the repairer left alone, with the reason."""

    reason: str
    uuid: str = ""
    location: tuple[float, float] | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        data: dict = {"reason": self.reason, "uuid": self.uuid}
        if self.location is not None:
            data["x"], data["y"] = self.location
        if self.detail:
            data["detail"] = self.detail
        return data


@dataclass
class MaskRepairResult:
    """Summary of a mask-to-copper repair run."""

    total_violations: int = 0
    repaired: int = 0
    actions: list[MaskRepairAction | MaskNudgeAction] = field(default_factory=list)
    unrepaired: list[MaskUnrepaired] = field(default_factory=list)

    def skip(
        self,
        reason: str,
        violation: DRCViolation,
        uuid: str = "",
        detail: str = "",
    ) -> None:
        loc = violation.locations[0] if violation.locations else None
        self.unrepaired.append(
            MaskUnrepaired(
                reason=reason,
                uuid=uuid,
                location=(loc.x_mm, loc.y_mm) if loc is not None else None,
                detail=detail,
            )
        )

    def skipped(self, reason: str) -> int:
        return sum(1 for u in self.unrepaired if u.reason == reason)

    def summary(self) -> str:
        lines = [f"Mask-to-copper repair: {self.repaired}/{self.total_violations} findings fixed"]
        reasons: dict[str, int] = {}
        for item in self.unrepaired:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        for reason, count in sorted(reasons.items()):
            lines.append(f"  Skipped ({reason}): {count}")
        return "\n".join(lines)


def _num(node: SExp | None) -> float:
    """First atom of ``node`` as a number (0.0 when absent)."""
    atom = node.get_first_atom() if node is not None else None
    return float(atom) if atom is not None else 0.0


def _bare(value: object) -> str:
    return str(value).strip().strip('"')


def mask_copper_violations(violations: list[DRCViolation]) -> list[DRCViolation]:
    """The error-severity ``mask_to_copper`` findings among ``violations``."""
    return [v for v in violations if v.type == ViolationType.MASK_TO_COPPER and v.is_error]


def _item_uuids(violation: DRCViolation) -> list[str]:
    """Opening uuids followed by the conductor uuid, as ``mask_to_copper`` emits."""
    raw = violation.item_uuids if any(violation.item_uuids) else violation.items
    return [_bare(u) for u in raw]


def finding_key(violation: DRCViolation) -> tuple[str, ...]:
    """Identity of a mask finding: its opening uuids plus the conductor uuid."""
    return tuple(_item_uuids(violation))


#: Maximum distance (mm) from the pad polygon within which a track end that
#: stopped short of its own pad may be pulled in to the pad centre.  The
#: router's tapered pad escapes end at or just outside the pad edge (issue
#: #9891); beyond this the "end" is a mid-route vertex, not an escape
#: terminus, and dragging it would rewrite routing.
_PULL_IN_REACH_MM = 0.3
#: Largest angle (degrees) between the pulled entry direction and the crossed
#: pad-edge normal for which the pull-in is offered.  ``mask_to_copper``'s
#: ``within_own_pad`` class excuses one straight entry up to 45 degrees from
#: perpendicular; a steeper pull-in cannot clear the finding, so it is not
#: attempted (the re-check would only revert it).
_PULL_IN_MAX_ANGLE_DEG = 45.0


class MaskCopperRepairer(CopperGapRepairer):
    """Re-centres own-pad track entries and nudges same-net pass-throughs.

    Subclasses :class:`CopperGapRepairer` (issue #9896) so a via nudge reuses
    the same machinery the copper-gap via repair validates with: the shared
    clearance engine gate for the destination (``_target_clearance_reason``,
    copper *and* drill-to-drill), the dragged-track clearance gate
    (``_dragged_track_reason``), anchored-end collection with locked-track
    refusal (``_anchored_ends``) and the endpoint update helper.
    """

    def __init__(self, pcb_path: str | Path, *, project_pcb_path: str | Path | None = None):
        # ``DrillClearanceRepairer.__init__`` parses the file and builds the
        # same ``nets`` / ``net_names`` indexes this class historically did.
        super().__init__(pcb_path)
        self._project_pcb_path = Path(project_pcb_path) if project_pcb_path else self.path
        self._pivot_pad_bounds: dict[str, BaseGeometry | None] = {}

    @cached_property
    def _board_rules(self) -> BoardClearanceRules:
        from kicad_tools.router.board_clearance_rules import BoardClearanceRules

        return BoardClearanceRules.from_board(self._project_pcb_path)

    # ------------------------------------------------------------------
    # Board model (absolute file coordinates throughout)
    # ------------------------------------------------------------------

    def _segment_nodes(self) -> dict[str, SExp]:
        out: dict[str, SExp] = {}
        for node in self.doc.find_all("segment"):
            uuid_node = node.find("uuid")
            atom = uuid_node.get_first_atom() if uuid_node else None
            if atom is not None:
                out[_bare(atom)] = node
        return out

    def _net_name(self, node: SExp) -> str:
        net_node = node.find("net")
        atom = net_node.get_first_atom() if net_node else None
        # The parser preserves quoted names as strings, including names such as "123".
        if isinstance(atom, str):
            return atom
        return self.nets.get(atom, "") if isinstance(atom, int) else ""

    def off_angle_tracks(self) -> Counter[str]:
        return off_angle_tracks(self.doc)

    def align_moved_tracks(
        self,
        before: MaskCopperRepairer,
        result: MaskRepairResult,
        clearance_mm: float | None = None,
    ) -> None:
        """Prepare legal legs for verification, including incidental junction members."""
        original, tracks = before._segment_nodes(), self._segment_nodes()
        skewed = self.off_angle_tracks() - before.off_angle_tracks()
        if not skewed:
            return
        pads = before._openings()
        owners: dict[str, set[int]] = {}
        moving_items = []
        for index, act in enumerate(result.actions):
            if isinstance(act, MaskNudgeAction):
                ids = {u for kind, u, *_ in act.points if kind == "segment"}
                moving_items.append({u for _, u, *_ in act.points})
            else:
                ids = {act.uuid, *(u for u, _ in act.joined)}
                moving_items.append(ids)
            for uid in ids:
                owners.setdefault(uid, set()).add(index)
        bends = []
        rejected: dict[int, str] = {}
        for uid in sorted(skewed):
            if uid not in owners:
                raise MaskTraceAngleError(f"off-angle track {uid} belongs to no proposed repair")
            try:
                if uid not in original or len(owners[uid]) != 1:
                    raise MaskTraceAngleError(f"off-angle track {uid} has no unique original move")
                track = original[uid]
                if before._locks.is_locked(track) or before._locks.is_generator_owned(track):
                    raise MaskTraceAngleError(f"cannot split locked or generated track {uid}")
                index = next(iter(owners[uid]))
                act = result.actions[index]
                openings = (
                    act.openings if isinstance(act, MaskNudgeAction) and act.uuid == uid else []
                )
                bend = before._choose_bend(
                    uid,
                    track,
                    tracks[uid],
                    moving_items[index],
                    pads,
                    clearance_mm,
                    openings,
                )
                bends.append((index, bend))
            except MaskTraceAngleError as exc:
                rejected.update(dict.fromkeys(owners[uid], str(exc)))
        # Edits sharing a conductor must unwind together in reverse order.
        # Independent repairs can still go through the normal verification.
        while True:
            extra = {
                i: f"shares track {uid} with a declined mask repair"
                for uid, indices in owners.items()
                if indices & rejected.keys()
                for i in indices - rejected.keys()
            }
            if not extra:
                break
            rejected.update(extra)
        for index in sorted(rejected, reverse=True):
            act = result.actions[index]
            if not self.undo_action(act):
                raise MaskTraceAngleError(f"could not restore {act.uuid}: {rejected[index]}")
            result.unrepaired.append(
                MaskUnrepaired("would_bend_trace", act.uuid, detail=rejected[index])
            )
        for index, bend in bends:
            if index in rejected:
                continue
            uid = bend.uuid
            self._insert_segment_after_last(bend.split(self.doc, tracks[uid]))
            result.actions[index].bends.append(bend)
        result.actions = [act for i, act in enumerate(result.actions) if i not in rejected]
        result.repaired = len(result.actions)

    def _choose_bend(
        self,
        uid: str,
        old: SExp,
        new: SExp,
        moving: set[str],
        pads: dict,
        clearance_mm: float | None,
        openings: list[str],
    ) -> MaskTraceBend:
        """Try the two legal elbows against the original copper and its bonded endpoints."""
        start, end = self._xy(old.find("start")), self._xy(old.find("end"))
        layer_node = old.find("layer")
        if start is None or end is None or layer_node is None:
            raise MaskTraceAngleError(f"cannot read track {uid}")
        layer, net = _bare(layer_node.get_first_atom()), self._net_name(old)
        radius = _num(old.find("width")) / 2
        old_copper = LineString([start, end]).buffer(radius, quad_segs=16)
        best = None
        best_gap = -1.0
        for bend in MaskTraceBend.candidates(uid, old, new):
            moved = self._xy(new.find(bend.moved_tag))
            if moved is None:
                raise MaskTraceAngleError(f"cannot read moved endpoint of track {uid}")
            copper = LineString([bend.fixed, bend.elbow, moved]).buffer(radius, quad_segs=16)
            moves = [(old_copper, copper, layer)]
            if self._loses_same_net_contact(moves, layer, net, moving, pads, protect_ends=True):
                continue
            if self._copper_floor_reason(moves, layer, net, uid, pads, clearance_mm) is not None:
                continue
            # The fixed-end approach can run past the very opening the nudge
            # needs to clear. Test the whole two-leg copper on both sides.
            gap = min((copper.distance(pads[u][0]) for u in openings), default=0.0)
            if gap > best_gap + 1e-6:
                best, best_gap = bend, gap
        if best is not None:
            return best
        raise MaskTraceAngleError(f"neither elbow preserves contact and clearance for track {uid}")

    def disconnected_bends(
        self, before: MaskCopperRepairer, actions: list[MaskRepairAction | MaskNudgeAction]
    ) -> set[str]:
        """Check all physical copper groups, including taps without a pad on either side."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.connectivity import ConnectivityValidator

        names = {act.net_name for act in actions if act.bends}
        if not names:
            return set()
        old_board, new_board = PCB(before.doc), PCB(self.doc)
        old, new = ConnectivityValidator(old_board), ConnectivityValidator(new_board)
        disconnected = set()
        for name in names:
            old_net, new_net = old_board.get_net_by_name(name), new_board.get_net_by_name(name)
            if (
                old_net is None
                or new_net is None
                or not new._contact_graph(new_net.number).preserves_connections(
                    old._contact_graph(old_net.number)
                )
            ):
                disconnected.add(name)
        return disconnected

    @staticmethod
    def _xy(node: SExp | None) -> tuple[float, float] | None:
        if node is None:
            return None
        atoms = node.get_atoms()
        if len(atoms) < 2:
            return None
        return float(atoms[0]), float(atoms[1])

    def _joined_origins(
        self, joined: list[tuple[str, SExp, str]]
    ) -> dict[tuple[str, str], tuple[float, float]]:
        # A junction accepts coordinates within 0.01 mm of the primary end.
        # Retain each authored coordinate for a selective rollback.
        return {
            (uid, tag): point
            for uid, node, tag in joined
            if (point := self._xy(node.find(tag))) is not None
        }

    def _openings(
        self,
    ) -> dict[str, tuple[BaseGeometry, tuple[float, float], list[str], str, str, str]]:
        """``uuid -> (polygon, centre, layers, net name, ref, shape)`` in file coords.

        Distinct from the inherited :meth:`CopperGapRepairer._pads`: this is
        the mask **opening** model (uuid-keyed, carrying the pad shape), not
        the copper-gap pad inventory, so the two never mix.
        """
        from kicad_tools.core.geometry import rotate_pad_offset
        from kicad_tools.schema.pcb import PCB

        pcb = PCB(self.doc)
        ox, oy = pcb._board_origin
        pads: dict[str, tuple[BaseGeometry, tuple[float, float], list[str], str, str, str]] = {}
        for fp in pcb.footprints:
            for index, pad in enumerate(fp.pads):
                lx, ly = pad.position
                rx, ry = rotate_pad_offset(lx, ly, fp.rotation)
                cx = fp.position[0] + rx + ox
                cy = fp.position[1] + ry + oy
                identity = _bare(pad.uuid) if pad.uuid else f"{fp.reference}:pad:{index}"
                poly = self._pad_polygon(pad, cx, cy)
                if poly is None:
                    # Keep the pad as an obstacle/owner marker with its box so a
                    # clearance check still sees it, but flag the shape.
                    w, h = pad.size
                    poly = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                    shape = "unsupported"
                else:
                    shape = pad.shape
                node = pad._sexp_node
                if (
                    shape == "unsupported"
                    or pad.drill_offset != (0.0, 0.0)
                    or (node is not None and (node.find("padstack") or node.find("chamfer")))
                ):
                    self._pivot_pad_bounds[identity] = self._pad_contact_bound(
                        pad, fp.reference, cx, cy
                    )
                pads[identity] = (
                    poly,
                    (cx, cy),
                    list(pad.layers),
                    pad.net_name or "",
                    f"{fp.reference}/{pad.number}",
                    shape,
                )
        return pads

    @staticmethod
    def _pad_contact_bound(pad: Pad, ref: str, cx: float, cy: float) -> BaseGeometry | None:
        """Enclose the physical copper of a pad whose opening we cannot model."""
        from kicad_tools.cli.relocate_in_pad_vias import _pad_copper_shift
        from kicad_tools.router.fixed_copper import bounding_pad_copper

        node = pad._sexp_node
        shift = _pad_copper_shift(pad)
        if node is None or shift is None or node.find("padstack") is not None:
            return None
        cx, cy = cx + shift[0], cy + shift[1]
        if pad.shape in {"rect", "roundrect", "circle", "oval"}:
            w, h = pad.size
            if w <= 0 or h <= 0:
                return None
            rect = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            return rotate(rect, -pad.rotation, origin=(cx, cy))
        try:
            bound = bounding_pad_copper(
                node.to_string(),
                reference=ref,
                pad_number=pad.number,
                x=cx,
                y=cy,
                rotation=pad.rotation,
                source_net=pad.net_name or "",
                source_net_id=pad.net_number,
            )
        except ValueError:
            return None
        return bound.geometry if bound is not None else None

    @staticmethod
    def _pad_polygon(pad, cx: float, cy: float) -> BaseGeometry | None:
        w, h = pad.size
        if w <= 0 or h <= 0:
            return None
        shape = pad.shape
        if shape == "rect":
            poly: BaseGeometry = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        elif shape == "roundrect":
            r = min(max(pad.roundrect_rratio, 0.0) * min(w, h), min(w, h) / 2)
            inner = box(cx - w / 2 + r, cy - h / 2 + r, cx + w / 2 - r, cy + h / 2 - r)
            poly = inner.buffer(r, quad_segs=16) if r > 0 else inner
        elif shape == "circle":
            poly = Point(cx, cy).buffer(w / 2, quad_segs=32)
        elif shape == "oval":
            if w >= h:
                half = (w - h) / 2
                poly = LineString([(cx - half, cy), (cx + half, cy)]).buffer(h / 2, quad_segs=16)
            else:
                half = (h - w) / 2
                poly = LineString([(cx, cy - half), (cx, cy + half)]).buffer(w / 2, quad_segs=16)
        else:
            return None
        # KiCad stores the pad angle in the absolute frame with y pointing down,
        # the same negated convention ``rotate_pad_offset`` applies to centres.
        if pad.rotation % 360:
            poly = rotate(poly, -pad.rotation, origin=(cx, cy))
        return poly

    def _other_net_obstacles(
        self, layer: str, net_name: str, skip_uuid: str, pads: dict
    ) -> list[BaseGeometry]:
        obstacles: list[BaseGeometry] = []
        for uuid, node in self._segment_nodes().items():
            if uuid == skip_uuid or self._net_name(node) == net_name:
                continue
            layer_node = node.find("layer")
            if layer_node is None or _bare(layer_node.get_first_atom()) != layer:
                continue
            start, end = self._xy(node.find("start")), self._xy(node.find("end"))
            width_node = node.find("width")
            if start is None or end is None or width_node is None:
                continue
            width = _num(width_node)
            obstacles.append(LineString([start, end]).buffer(width / 2, quad_segs=16))
        for via in self.doc.find_all("via"):
            if self._net_name(via) == net_name:
                continue
            at = self._xy(via.find("at"))
            size_node = via.find("size")
            if at is None or size_node is None:
                continue
            obstacles.append(Point(at).buffer(_num(size_node) / 2, quad_segs=16))
        for poly, _, layers, pad_net, _, _ in pads.values():
            if pad_net == net_name and net_name:
                continue
            if (
                layer in layers
                or "*.Cu" in layers
                or ("F&B.Cu" in layers and layer in {"F.Cu", "B.Cu"})
            ):
                obstacles.append(poly)
        return obstacles

    def _junction(
        self, layer: str, point: tuple[float, float], skip_uuid: str, net_name: str
    ) -> tuple[list[tuple[str, SExp, str]], bool]:
        """Segment ends sharing ``point``, and whether the point is unmovable.

        Same-net segment ends at the point move with it.  A via, or a segment
        of another net or a locked one, makes the point unmovable.
        """
        members: list[tuple[str, SExp, str]] = []
        for uuid, node in self._segment_nodes().items():
            if uuid == skip_uuid:
                continue
            layer_node = node.find("layer")
            if layer_node is None or _bare(layer_node.get_first_atom()) != layer:
                continue
            for tag in ("start", "end"):
                xy = self._xy(node.find(tag))
                if xy is None or math.dist(xy, point) >= _TOLERANCE_MM:
                    continue
                if self._net_name(node) != net_name or self._locks.is_locked(node):
                    return members, True
                members.append((uuid, node, tag))
        for via in self.doc.find_all("via"):
            at = self._xy(via.find("at"))
            if at is not None and math.dist(at, point) < _TOLERANCE_MM:
                return members, True
        return members, False

    # ------------------------------------------------------------------
    # Repair
    # ------------------------------------------------------------------

    def repair(  # type: ignore[override]  # unrelated repair families share the base name
        self,
        violations: list[DRCViolation],
        max_displacement: float = 0.5,
        dry_run: bool = False,
        clearance_mm: float | None = None,
        design_rules: DesignRules | None = None,
        margin_mm: float = PASS_THROUGH_MARGIN_MM,
        warning_clearance_mm: float | None = None,
    ) -> MaskRepairResult:
        """Repair the own-pad entry and disjoint pass-through subsets.

        Args:
            violations: Any violations; only error-severity ``mask_to_copper``
                findings are considered.
            max_displacement: Longest end move allowed, in mm.
            dry_run: Compute actions without touching the document.
            clearance_mm: Copper clearance a moved track must keep from other-net
                copper (``None``: only a track that gets *closer* than it already
                was to zero is declined, i.e. an overlap).
            design_rules: Active manufacturer rules.  A pass-through via move is
                re-validated against the shared clearance engine (copper,
                pad and drill-to-drill) before it is applied, mirroring the
                copper-gap via repair.
            margin_mm: Slack a pass-through nudge must open beyond the policy
                clearance, in mm.
            warning_clearance_mm: Preferred mask gap for a shared pad exit
                when the manufacturer's policy includes an advisory tier.
        """
        result = MaskRepairResult()
        findings = mask_copper_violations(violations)
        result.total_violations = len(findings)
        if not findings:
            return result

        segments = self._segment_nodes()
        pads = self._openings()
        moved: set[str] = set()
        # Conductors an earlier action this run already moved or dragged: a
        # later finding against them measures stale geometry, so the opening
        # model check must not compare it against the report's measurement.
        touched_by_action: set[str] = set()
        pass_throughs: dict[str, list[DRCViolation]] = {}

        for violation in findings:
            uuids = _item_uuids(violation)
            if len(uuids) < 2:
                result.skip("no_item_uuids", violation)
                continue
            conductor = uuids[-1]
            openings = uuids[:-1]
            if (
                PASS_THROUGH_MARKER in violation.message
                and DISJOINT_RELATION_MARKER in violation.message
            ):
                # One grouped edit per conductor clears every opening it has
                # findings against (issue #9896); collected and repaired below.
                pass_throughs.setdefault(conductor, []).append(violation)
                continue
            if OWN_PAD_ENTRY_MARKER not in violation.message:
                result.skip("not_own_pad_entry", violation, conductor)
                continue
            seg = segments.get(conductor)
            if seg is None:
                result.skip("not_a_straight_segment", violation, conductor)
                continue
            if len(openings) != 1 or openings[0] not in pads:
                result.skip("opening_not_a_single_pad", violation, conductor)
                continue
            if conductor in moved:
                result.skip("segment_already_moved", violation, conductor)
                continue
            if conductor in touched_by_action:
                result.skip("segment_end_already_moved", violation, conductor)
                continue
            if self._slide_jog_along_runs(
                seg,
                conductor,
                openings[0],
                pads,
                result,
                max_displacement,
                dry_run,
                clearance_mm,
                violation.required_value_mm,
                margin_mm,
                warning_clearance_mm,
                touched_by_action,
            ):
                # A jog between parallel runs slides whole; no end is recentred.
                moved.add(conductor)
                action = result.actions[-1]
                if isinstance(action, MaskNudgeAction):
                    touched_by_action.update(point[1] for point in action.points)
                continue
            reason = self._repair_one(
                seg, conductor, openings[0], pads, result, max_displacement, dry_run, clearance_mm
            )
            if reason == "no_end_inside_pad":
                # Issue #9891: the router's tapered pad escapes stop at (or a
                # hair outside) the pad edge, so no authored end is strictly
                # inside and the recentre declines.  A terminus that merely
                # stopped short is pulled in to the pad centre instead --
                # the same single clean entry the recentre produces.
                reason = self._pull_in_one(
                    seg,
                    conductor,
                    openings[0],
                    pads,
                    result,
                    max_displacement,
                    dry_run,
                    clearance_mm,
                )
            if reason is not None:
                result.skip(reason, violation, conductor)
            else:
                moved.add(conductor)
                # A joined track cannot receive a second own-pad move in this pass.
                action = result.actions[-1]
                if isinstance(action, MaskRepairAction):
                    touched_by_action.add(action.uuid)
                    touched_by_action.update(uuid for uuid, _tag in action.joined)

        for conductor, members in pass_throughs.items():
            if conductor in moved:
                result.skip("segment_already_moved", members[0], conductor)
                continue
            if conductor in touched_by_action:
                # An earlier action this run already dragged an end of this
                # conductor; nudging it again stacks a second edit on the
                # same endpoint, which cannot be undone in application order
                # and aborts the whole batch in verification (issue #9958).
                for violation in members:
                    result.skip("segment_end_already_moved", violation, conductor)
                continue
            refusal = self._repair_pass_through(
                conductor,
                members,
                segments,
                pads,
                result,
                max_displacement,
                dry_run,
                clearance_mm,
                design_rules,
                margin_mm,
                touched_by_action,
                warning_clearance_mm,
            )
            if refusal is None:
                moved.add(conductor)
                # Later conductors this nudge dragged measure stale geometry;
                # record them like the recentre joins above.
                action = result.actions[-1]
                if isinstance(action, MaskNudgeAction):
                    touched_by_action.update(point[1] for point in action.points)
            else:
                reason, detail = refusal
                for violation in members:
                    result.skip(reason, violation, conductor, detail)
        return result

    def _repair_one(
        self,
        seg: SExp,
        seg_uuid: str,
        pad_uuid: str,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
    ) -> str | None:
        """Move one segment end; return ``None`` on success, else the skip reason."""
        poly, centre, pad_layers, pad_net, pad_ref, shape = pads[pad_uuid]
        if shape == "unsupported":
            return "unverified_pad_shape"

        net_name = self._net_name(seg)
        if not net_name or net_name != pad_net:
            return "not_same_net"
        if self._locks.is_locked(seg):
            return "locked_segment"

        layer_node = seg.find("layer")
        layer = _bare(layer_node.get_first_atom()) if layer_node else ""
        if layer not in pad_layers and "*.Cu" not in pad_layers:
            return "pad_not_on_segment_layer"

        start_node, end_node = seg.find("start"), seg.find("end")
        start, end = self._xy(start_node), self._xy(end_node)
        width_node = seg.find("width")
        if start is None or end is None or width_node is None:
            return "malformed_segment"
        width = _num(width_node)

        grown = poly.buffer(_INSIDE_EPS_MM)
        start_in = grown.contains(Point(start))
        end_in = grown.contains(Point(end))
        if start_in == end_in:
            return "both_ends_inside_pad" if start_in else "no_end_inside_pad"
        inside, outside = (start, end) if start_in else (end, start)
        inside_node = start_node if start_in else end_node

        displacement = math.dist(inside, centre)
        if displacement < _TOLERANCE_MM / 2:
            return "end_already_centred"
        if displacement > max_displacement:
            return "exceeds_max_displacement"
        joined, blocked = self._junction(layer, inside, seg_uuid, net_name)
        if blocked:
            return "junction_at_moved_end"

        # Clearance gate: never bring the track closer than ``clearance_mm`` to
        # other-net copper than it already was.  ``fix-drc`` passes the
        # manufacturer floor here, which is below a net class's clearance, so
        # this only stops floor-crossing moves; a move that lands between the
        # floor and the class clearance is caught by fix-drc's post-save
        # re-check (``_run_mask_pass``, Issue #9365).
        required = clearance_mm if clearance_mm is not None else 0.0
        old_shape = LineString([outside, inside]).buffer(width / 2, quad_segs=16)
        new_shape = LineString([outside, centre]).buffer(width / 2, quad_segs=16)
        moves = [(old_shape, new_shape)]
        for _, jnode, jtag in joined:
            jw = _num(jnode.find("width")) or width
            jother = self._xy(jnode.find("end" if jtag == "start" else "start"))
            if jother is None:
                return "malformed_segment"
            moves.append(
                (
                    LineString([jother, inside]).buffer(jw / 2, quad_segs=16),
                    LineString([jother, centre]).buffer(jw / 2, quad_segs=16),
                )
            )
        for obstacle in self._other_net_obstacles(layer, net_name, seg_uuid, pads):
            for old_s, new_s in moves:
                new_d = new_s.distance(obstacle)
                if new_d < required - 1e-6 and new_d < old_s.distance(obstacle) - 1e-6:
                    return "would_violate_clearance"

        action = MaskRepairAction(
            action="recentre",
            uuid=seg_uuid,
            pad_uuid=pad_uuid,
            pad_ref=pad_ref,
            net_name=net_name,
            layer=layer,
            old_x=inside[0],
            old_y=inside[1],
            new_x=round(centre[0], 4),
            new_y=round(centre[1], 4),
            displacement_mm=displacement,
            detail=f"moved track end {displacement:.4f}mm to the pad centre",
            joined=[(u, t) for u, _, t in joined],
            joined_origins=self._joined_origins(joined),
            end_tag="start" if start_in else "end",
        )
        result.actions.append(action)
        if not dry_run and inside_node is not None:
            inside_node.set_value(0, action.new_x)
            inside_node.set_value(1, action.new_y)
            for _, jnode, jtag in joined:
                end_node = jnode.find(jtag)
                if end_node is not None:
                    end_node.set_value(0, action.new_x)
                    end_node.set_value(1, action.new_y)
            self.modified = True
        result.repaired += 1
        return None

    def _pull_in_one(
        self,
        seg: SExp,
        seg_uuid: str,
        pad_uuid: str,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
    ) -> str | None:
        """Drag a same-net track end that stopped short of its pad to its centre.

        The complement of :meth:`_repair_one`'s recentre for the ``no end
        inside`` population (issue #9891): the conductor is a straight segment
        on the opening pad's net whose nearest endpoint sits within
        ``_PULL_IN_REACH_MM`` of the pad polygon with the other endpoint
        outside it.  The near endpoint moves to the pad centre, making the
        segment a single straight entry that crosses the pad edge within
        ``_PULL_IN_MAX_ANGLE_DEG`` of perpendicular -- the shape
        ``mask_to_copper``'s ``within_own_pad`` class excuses.  Returns
        ``None`` on success, else the skip reason.  Everything else (a via at
        the moved end, a foreign or locked junction, a clearance-losing move)
        is declined like the recentre; ``fix-drc``'s re-check still reverts a
        move that does not clear its finding.
        """
        poly, centre, pad_layers, pad_net, pad_ref, shape = pads[pad_uuid]
        if shape == "unsupported":
            return "unverified_pad_shape"

        net_name = self._net_name(seg)
        if not net_name or net_name != pad_net:
            return "not_same_net"
        if self._locks.is_locked(seg):
            return "locked_segment"

        layer_node = seg.find("layer")
        layer = _bare(layer_node.get_first_atom()) if layer_node else ""
        if layer not in pad_layers and "*.Cu" not in pad_layers:
            return "pad_not_on_segment_layer"

        start_node, end_node = seg.find("start"), seg.find("end")
        start, end = self._xy(start_node), self._xy(end_node)
        width_node = seg.find("width")
        if start is None or end is None or width_node is None:
            return "malformed_segment"
        width = _num(width_node)

        grown = poly.buffer(_INSIDE_EPS_MM)
        if grown.contains(Point(start)) or grown.contains(Point(end)):
            return "end_inside_pad_recentre_applies"
        near, near_node, far = start, start_node, end
        if math.dist(end, centre) < math.dist(start, centre):
            near, near_node, far = end, end_node, start
        if poly.distance(Point(near)) > _PULL_IN_REACH_MM:
            # Beyond the reach the conductor is mid-route, not an escape
            # terminus; keep the historical reason for this population.
            return "no_end_inside_pad"

        # The straight entry the pull-in would create: from the far end to the
        # pad centre.  It must cross the pad boundary within the angle the
        # within_own_pad class excuses, else the move cannot clear the finding.
        entry = LineString([far, centre])
        crossing = entry.intersection(poly.boundary)
        if crossing.is_empty:
            return "entry_misses_pad"
        if not self._entry_within_angle(poly, crossing, far, centre):
            return "entry_too_shallow"

        displacement = math.dist(near, centre)
        if displacement < _TOLERANCE_MM / 2:
            return "end_already_centred"
        if displacement > max_displacement:
            return "exceeds_max_displacement"
        joined, blocked = self._junction(layer, near, seg_uuid, net_name)
        if blocked:
            return "junction_at_moved_end"

        # Every segment dragged by this pull-in must retain its copper clearance.
        required = clearance_mm if clearance_mm is not None else 0.0
        old_shape = LineString([far, near]).buffer(width / 2, quad_segs=16)
        new_shape = LineString([far, centre]).buffer(width / 2, quad_segs=16)
        moves = [(old_shape, new_shape)]
        for _, jnode, jtag in joined:
            jw = _num(jnode.find("width")) or width
            jother = self._xy(jnode.find("end" if jtag == "start" else "start"))
            if jother is None:
                return "malformed_segment"
            moves.append(
                (
                    LineString([jother, near]).buffer(jw / 2, quad_segs=16),
                    LineString([jother, centre]).buffer(jw / 2, quad_segs=16),
                )
            )
        for obstacle in self._other_net_obstacles(layer, net_name, seg_uuid, pads):
            for old_s, new_s in moves:
                new_d = new_s.distance(obstacle)
                if new_d < required - 1e-6 and new_d < old_s.distance(obstacle) - 1e-6:
                    return "would_violate_clearance"

        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.connectivity import ConnectivityValidator

        pcb = PCB(self.doc)
        if self._board_rules.dru_error:
            return "unverified_clearance_rules"
        if self._pull_in_crowds_pad(pcb, layer, net_name, pads, moves):
            return "would_violate_clearance"

        action = MaskRepairAction(
            action="pull_in",
            uuid=seg_uuid,
            pad_uuid=pad_uuid,
            pad_ref=pad_ref,
            net_name=net_name,
            layer=layer,
            old_x=near[0],
            old_y=near[1],
            new_x=round(centre[0], 4),
            new_y=round(centre[1], 4),
            displacement_mm=displacement,
            detail=f"pulled track end {displacement:.4f}mm in to the pad centre",
            joined=[(u, t) for u, _, t in joined],
            joined_origins=self._joined_origins(joined),
            end_tag="start" if near_node is start_node else "end",
        )
        net = pcb.get_net_by_name(net_name)
        if net is None:
            return "net_geometry_unverified"
        previous = ConnectivityValidator(pcb)._contact_graph(net.number)
        endpoints = [near_node, *(jnode.find(jtag) for _, jnode, jtag in joined)]
        original = [(node, list(node.children)) for node in endpoints if node is not None]
        keep = False
        try:
            for node, _ in original:
                node.set_value(0, action.new_x)
                node.set_value(1, action.new_y)
            current = ConnectivityValidator(PCB(self.doc))._contact_graph(net.number)
            if not current.preserves_connections(previous):
                return "would_disconnect_copper"
            keep = not dry_run
        finally:
            if not keep:
                for node, children in original:
                    node.children[:] = children
        result.actions.append(action)
        self.modified |= keep
        result.repaired += 1
        return None

    def _pull_in_crowds_pad(
        self,
        pcb: PCB,
        layer: str,
        net_name: str,
        pads: dict,
        moves: list[tuple[BaseGeometry, BaseGeometry]],
    ) -> bool:
        """Check the candidate and its dragged tracks against each pad's actual rules."""
        from kicad_tools.router.board_clearance_rules import (
            item_props_for_type,
            pad_clearance_override,
        )

        rules = self._board_rules
        track = item_props_for_type(
            "track", net_name, False, layer=layer, footprint_ref="", footprint_lib_id=""
        )
        for fp in pcb.footprints:
            for index, pad in enumerate(fp.pads):
                if not (
                    layer in pad.layers
                    or "*.Cu" in pad.layers
                    or ("F&B.Cu" in pad.layers and layer in {"F.Cu", "B.Cu"})
                ):
                    continue
                pad_net = pcb.get_net(pad.net_number)
                pad_name = pad.net_name or (pad_net.name if pad_net else "")
                obstacle = item_props_for_type(
                    "pad",
                    pad_name,
                    pad.type == "thru_hole",
                    pad.type,
                    fp.layer,
                    fp.reference,
                    fp.name,
                )
                required = rules.physical_clearance(track, obstacle, layer)
                if pad_name != net_name:
                    local = pad_clearance_override(fp, pad)
                    electrical = (
                        rules.clearance(track, obstacle, layer)
                        if local is None
                        else max(local, rules.min_clearance or 0.0)
                    )
                    required = max(required, electrical)
                if required <= 0:
                    continue
                identity = _bare(pad.uuid) if pad.uuid else f"{fp.reference}:pad:{index}"
                polygon = pads[identity][0]
                for old, new in moves:
                    gap = new.distance(polygon)
                    if gap < required - 1e-6 and gap < old.distance(polygon) - 1e-6:
                        return True
        return False

    @staticmethod
    def _entry_within_angle(
        poly: BaseGeometry,
        crossing: BaseGeometry,
        far: tuple[float, float],
        centre: tuple[float, float],
    ) -> bool:
        """True when the ``far -> centre`` entry crosses the pad edge within 45°.

        The crossing point is matched to the nearest straight piece of the
        polygon boundary; the angle between the entry direction and that
        piece's normal must not exceed ``_PULL_IN_MAX_ANGLE_DEG``.  A
        multi-point crossing (a corner clip) is judged on the piece nearest
        the pad centre, where the entry actually lands.
        """
        from shapely.geometry import MultiPoint

        if isinstance(crossing, MultiPoint):
            points = [(p.x, p.y) for p in crossing.geoms]
            point = min(points, key=lambda xy: math.dist(xy, centre))
        else:
            point = (crossing.x, crossing.y)
        direction = (centre[0] - far[0], centre[1] - far[1])
        length = math.hypot(*direction)
        if length < 1e-9:
            return False
        unit = (direction[0] / length, direction[1] / length)
        best = None
        coords = list(poly.boundary.coords)
        for (x1, y1), (x2, y2) in zip(coords, coords[1:], strict=False):
            ex, ey = x2 - x1, y2 - y1
            edge_len = math.hypot(ex, ey)
            if edge_len < 1e-9:
                continue
            # Distance from the crossing point to this edge piece.
            t = ((point[0] - x1) * ex + (point[1] - y1) * ey) / (edge_len * edge_len)
            t = max(0.0, min(1.0, t))
            dist = math.hypot(point[0] - (x1 + t * ex), point[1] - (y1 + t * ey))
            if best is None or dist < best[0]:
                normal = (ey / edge_len, -ex / edge_len)
                best = (dist, normal)
        if best is None:
            return False
        normal = best[1]
        cos = abs(unit[0] * normal[0] + unit[1] * normal[1])
        threshold = math.cos(math.radians(_PULL_IN_MAX_ANGLE_DEG))
        # Normalizing an exact diagonal can round just below cos(45°).
        return bool(cos >= threshold or math.isclose(cos, threshold, rel_tol=0.0, abs_tol=1e-12))

    # ------------------------------------------------------------------
    # Pass-through repair (issue #9896)
    # ------------------------------------------------------------------

    def _repair_pass_through(
        self,
        conductor: str,
        members: list[DRCViolation],
        segments: dict[str, SExp],
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        design_rules: DesignRules | None,
        margin_mm: float,
        touched_by_action: set[str],
        warning_clearance_mm: float | None,
    ) -> tuple[str, str] | None:
        """One grouped pass-through repair; ``None`` on success.

        Every member finding must name exactly one pad opening; the single
        edit computed here has to clear them all, so a member the model
        cannot resolve refuses the whole group.
        """
        openings: list[str] = []
        required: float | None = None
        pairs: list[tuple[str, float | None]] = []
        for violation in members:
            uuids = _item_uuids(violation)
            if len(uuids) != 2 or uuids[0] not in pads:
                return "opening_not_a_single_pad", ""
            if uuids[0] not in openings:
                openings.append(uuids[0])
            pairs.append((uuids[0], violation.actual_value_mm))
            if violation.required_value_mm is not None:
                required = max(required or 0.0, violation.required_value_mm)
        if required is None:
            return "no_required_value", ""

        seg = segments.get(conductor)
        if seg is not None:
            refusal = self._nudge_segment_clear(
                seg,
                conductor,
                openings,
                pairs,
                required,
                pads,
                result,
                max_displacement,
                dry_run,
                clearance_mm,
                margin_mm,
                touched_by_action,
            )
            if refusal and refusal[0] == "would_violate_clearance" and len(openings) == 1:
                # Translating the entire outgoing track can crowd the far end.
                # A shared pad exit can slide along its existing departure.
                if self._slide_shared_pad_exit(
                    seg,
                    conductor,
                    openings[0],
                    pads,
                    result,
                    max_displacement,
                    dry_run,
                    clearance_mm,
                    required,
                    margin_mm,
                    warning_clearance_mm,
                    touched_by_action,
                ):
                    return None
            return refusal
        via_node = self._via_by_uuid(conductor)
        if via_node is not None:
            return self._nudge_via_clear(
                via_node,
                conductor,
                openings,
                pairs,
                required,
                pads,
                result,
                max_displacement,
                dry_run,
                clearance_mm,
                design_rules,
                margin_mm,
                touched_by_action,
            )
        return "not_a_straight_segment", ""

    def _nudge_segment_clear(
        self,
        seg: SExp,
        seg_uuid: str,
        openings: list[str],
        pairs: list[tuple[str, float | None]],
        required: float,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        margin_mm: float,
        touched_by_action: set[str],
    ) -> tuple[str, str] | None:
        """Move a pass-through track, preserving an attached via at a fixed end."""
        entries = [pads[opening] for opening in openings]
        if any(entry[5] == "unsupported" for entry in entries):
            return "unverified_pad_shape", ""
        net_name = self._net_name(seg)
        if not net_name or any(entry[3] != net_name for entry in entries):
            return "not_same_net", ""
        if self._locks.is_locked(seg):
            return "locked_segment", ""

        layer_node = seg.find("layer")
        layer = _bare(layer_node.get_first_atom()) if layer_node else ""
        if not layer:
            return "malformed_segment", ""
        for entry in entries:
            if layer not in entry[2] and "*.Cu" not in entry[2]:
                return "pad_not_on_segment_layer", ""

        start, end = self._xy(seg.find("start")), self._xy(seg.find("end"))
        width_node = seg.find("width")
        if start is None or end is None or width_node is None:
            return "malformed_segment", ""
        width = _num(width_node)
        copper = LineString([start, end]).buffer(width / 2, quad_segs=16)

        joined: list[tuple[str, SExp, str]] = []
        fixed = None
        for point in (start, end):
            members_at, blocked = self._junction(layer, point, seg_uuid, net_name)
            if blocked:
                # An attached via and all copper on its other layers can stay
                # exactly where they are while the opposite junction moves.
                if fixed is not None or not any(
                    self._net_name(via) == net_name
                    and layer in self._via_span_layer_set(via)
                    and (at := self._xy(via.find("at"))) is not None
                    and math.dist(at, point) < _TOLERANCE_MM
                    for via in self.doc.find_all("via")
                ):
                    return "junction_at_moved_end", ""
                fixed = point
                continue
            for member in members_at:
                if member not in joined:
                    joined.append(member)
        if fixed is not None and not joined:
            return "junction_at_moved_end", ""

        refusal = self._opening_model_refusal(pairs, copper, pads, seg_uuid in touched_by_action)
        if refusal is not None:
            return refusal

        if fixed is None:
            reference = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            push = self._push_clear_of_openings(
                copper, [(pads[o][0], pads[o][1]) for o in openings], required + margin_mm
            )
        else:
            reference = end if fixed == start else start
            if self._has_fixed_junction_contact(reference, layer, net_name):
                return "junction_at_moved_end", ""
            push = self._shift_junction_clear(
                fixed, reference, [entry[0] for entry in entries], required + margin_mm + width / 2
            )
        if push is None:
            return "infeasible", ""
        dx, dy = push
        displacement = math.hypot(dx, dy)
        if displacement < _TOLERANCE_MM / 2:
            return "already_cleared", ""
        if displacement > max_displacement:
            return "exceeds_max_displacement", f"needs {displacement:.4f}mm"
        moving_uuids = {seg_uuid, *(member[0] for member in joined)}

        new_start = start if start == fixed else (start[0] + dx, start[1] + dy)
        new_end = end if end == fixed else (end[0] + dx, end[1] + dy)
        moves: list[tuple[BaseGeometry, BaseGeometry, str]] = [
            (
                copper,
                LineString([new_start, new_end]).buffer(width / 2, quad_segs=16),
                layer,
            )
        ]
        points: list[tuple[str, str, str | None, float, float]] = [
            ("segment", seg_uuid, tag, point[0], point[1])
            for tag, point in (("start", start), ("end", end))
            if point != fixed
        ]
        for join_uuid, join_node, join_tag in joined:
            join_width_node = join_node.find("width")
            join_end = self._xy(join_node.find(join_tag)) if join_tag else None
            join_other = self._xy(join_node.find("end" if join_tag == "start" else "start"))
            if join_width_node is None or join_end is None or join_other is None:
                return "malformed_segment", ""
            join_width = _num(join_width_node) or width
            moves.append(
                (
                    LineString([join_other, join_end]).buffer(join_width / 2, quad_segs=16),
                    LineString(
                        [
                            join_other,
                            (join_end[0] + dx, join_end[1] + dy),
                        ]
                    ).buffer(join_width / 2, quad_segs=16),
                    layer,
                )
            )
            points.append(("segment", join_uuid, join_tag, join_end[0], join_end[1]))

        if fixed is not None and self._loses_same_net_contact(
            moves, layer, net_name, moving_uuids, pads
        ):
            return "would_break_same_net_contact", ""
        refusal = self._copper_floor_reason(moves, layer, net_name, seg_uuid, pads, clearance_mm)
        if refusal is not None:
            return refusal
        refusal = self._other_opening_reason(
            openings,
            moves,
            [(start, new_start, frozenset({layer})), (end, new_end, frozenset({layer}))],
            pads,
            required,
            displacement,
        )
        if refusal is not None:
            return refusal

        first = pads[openings[0]]
        refs = ", ".join(pads[o][4] for o in openings)
        action = MaskNudgeAction(
            action="nudge_clear",
            uuid=seg_uuid,
            kind="segment",
            pad_uuid=openings[0],
            pad_ref=first[4],
            net_name=net_name,
            layer=layer,
            old_x=reference[0],
            old_y=reference[1],
            new_x=reference[0] + dx,
            new_y=reference[1] + dy,
            dx=dx,
            dy=dy,
            displacement_mm=displacement,
            detail=(
                f"moved {displacement:.4f}mm clear of {refs}"
                + (f", dragging {len(joined)} joined end(s)" if joined else "")
            ),
            openings=list(openings),
            points=points,
        )
        return self._apply_nudge(action, result, dry_run)

    def _slide_shared_pad_exit(
        self,
        seg: SExp,
        seg_uuid: str,
        opening: str,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        required: float,
        margin_mm: float,
        warning_clearance_mm: float | None,
        touched: set[str],
    ) -> bool:
        """Trim coincident outgoing tracks along their ray and slide their pad leads.

        Every moved point receives the same translation. The distal ends
        of the outgoing tracks stay fixed, each lead stays straight, and
        the existing nudge action owns the entire edit for rollback.
        """
        if opening in self._pivot_pad_bounds:
            return False
        pad, _, _, net, ref, _ = pads[opening]
        width = _num(seg.find("width"))
        layer_node = seg.find("layer")
        if layer_node is None:
            return False
        layer = _bare(layer_node.get_first_atom())
        start, end = self._xy(seg.find("start")), self._xy(seg.find("end"))
        if start is None or end is None or width <= 0:
            return False
        tag, junction, distal = ("start", start, end)
        if pad.distance(Point(end)) < pad.distance(Point(start)):
            tag, junction, distal = "end", end, start
        departure = LineString([junction, distal])
        if departure.length < _TOLERANCE_MM:
            return False
        ux = (distal[0] - junction[0]) / departure.length
        uy = (distal[1] - junction[1]) / departure.length
        members, blocked = self._junction(layer, junction, seg_uuid, net)
        if blocked or not members or self._has_fixed_junction_contact(junction, layer, net):
            return False
        members.append((seg_uuid, seg, tag))
        moving = {uid for uid, _, _ in members}
        if moving & touched or any(self._locks.is_generator_owned(node) for _, node, _ in members):
            return False
        leads: set[str] = set()
        legs = []
        shortest_exit = math.inf
        for uid, node, join_tag in members:
            other_tag = "end" if join_tag == "start" else "start"
            other = self._xy(node.find(other_tag))
            if (
                self._xy(node.find(join_tag)) != junction
                or other is None
                or node.find("width") is None
                or _num(node.find("width")) != width
            ):
                return False
            if pad.contains(Point(other)):
                joined, fixed = self._junction(layer, other, uid, net)
                if fixed or any(item[0] not in moving for item in joined):
                    return False
                if self._has_fixed_junction_contact(other, layer, net):
                    return False
                leads.add(uid)
            else:
                dx, dy = other[0] - junction[0], other[1] - junction[1]
                length = dx * ux + dy * uy
                if length <= 0 or abs(dx * uy - dy * ux) > 1e-6:
                    return False
                shortest_exit = min(shortest_exit, length)
            legs.append((uid, join_tag, other_tag, other))
        if not leads or len(legs) - len(leads) < 2:
            return False
        rules = self._board_rules
        if rules.dru_error:
            return False
        copper_floor = max(clearance_mm or 0.0, rules.net_requirement(net, self.net_names))

        # Cut the departure where its full copper width clears the opening.
        # Try the manufacturer's advisory band first when it fits.
        for gap in dict.fromkeys((max(warning_clearance_mm or 0, required), required)):
            halo = pad.buffer(width / 2 + gap + margin_mm)
            inside = departure.intersection(halo)
            if not halo.covers(Point(junction)) or not isinstance(inside, LineString):
                continue
            shift = inside.length
            if not 0 < shift <= max_displacement or shift + _TOLERANCE_MM >= shortest_exit:
                continue
            dx, dy = ux * shift, uy * shift
            new_join = round(junction[0] + dx, 4), round(junction[1] + dy, 4)
            moves = []
            points: list[tuple[str, str, str | None, float, float]] = []
            moved_points = [(junction, new_join, frozenset({layer}))]
            for uid, join_tag, other_tag, other in legs:
                new_other = other
                if uid in leads:
                    new_other = round(other[0] + dx, 4), round(other[1] + dy, 4)
                    if not pad.covers(Point(new_other).buffer(width / 2)):
                        break
                    points.append(("segment", uid, other_tag, *other))
                    moved_points.append((other, new_other, frozenset({layer})))
                points.append(("segment", uid, join_tag, *junction))
                old = LineString([junction, other]).buffer(width / 2, quad_segs=16)
                new = LineString([new_join, new_other]).buffer(width / 2, quad_segs=16)
                moves.append((old, new, layer))
            if len(moves) != len(legs):
                continue
            if self._loses_same_net_contact(moves, layer, net, moving, pads, protect_ends=True):
                continue
            if self._copper_floor_reason(moves, layer, net, seg_uuid, pads, copper_floor):
                continue
            if self._other_opening_reason([opening], moves, moved_points, pads, required, shift):
                continue
            action = MaskNudgeAction(
                action="nudge_clear",
                uuid=seg_uuid,
                kind="segment",
                pad_uuid=opening,
                pad_ref=ref,
                net_name=net,
                layer=layer,
                old_x=junction[0],
                old_y=junction[1],
                new_x=new_join[0],
                new_y=new_join[1],
                dx=dx,
                dy=dy,
                displacement_mm=shift,
                detail=f"slid shared exit {shift:.4f}mm along its outgoing tracks at {ref}",
                openings=[opening],
                points=points,
            )
            return self._apply_nudge(action, result, dry_run) is None
        return False

    def _slide_jog_along_runs(
        self,
        seg: SExp,
        seg_uuid: str,
        opening: str,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        required: float | None,
        margin_mm: float,
        warning_clearance_mm: float | None,
        touched: set[str],
    ) -> bool:
        """Slide a jog between two parallel runs along them, clear of the opening.

        The conductor is a straight track whose two ends each join only
        same-net tracks that run along one common axis (the pad lead on one
        side, the downstream run on the other), and any coincident copies of
        the jog. Translating the jog (and every copy) along that axis moves
        each joined run end along its own line, so every angle is kept, the
        far ends of the runs stay fixed, and the jog leaves the opening it
        entered. All points receive one translation so the existing nudge
        action owns the edit for rollback. Returns ``True`` when applied.
        """
        if required is None or opening in self._pivot_pad_bounds:
            return False
        pad, _, pad_layers, net, ref, shape = pads[opening]
        if shape == "unsupported" or not net or self._net_name(seg) != net:
            return False
        if self._locks.is_locked(seg):
            return False
        layer_node, width_node = seg.find("layer"), seg.find("width")
        if layer_node is None or width_node is None:
            return False
        layer = _bare(layer_node.get_first_atom())
        if layer not in pad_layers and "*.Cu" not in pad_layers:
            return False
        width = _num(width_node)
        start, end = self._xy(seg.find("start")), self._xy(seg.find("end"))
        if start is None or end is None or width <= 0 or math.dist(start, end) < _TOLERANCE_MM:
            return False
        near, far = (
            (end, start)
            if pad.distance(Point(end)) < pad.distance(Point(start))
            else (
                start,
                end,
            )
        )

        copies: dict[str, SExp] = {seg_uuid: seg}
        runs: list[tuple[str, SExp, str, tuple[float, float], tuple[float, float]]] = []
        sides: list[int] = []
        for point, opposite in ((near, far), (far, near)):
            members, blocked = self._junction(layer, point, seg_uuid, net)
            if blocked or self._has_fixed_junction_contact(point, layer, net):
                return False
            count = 0
            for uid, node, tag in members:
                other = self._xy(node.find("end" if tag == "start" else "start"))
                if other is None or node.find("width") is None:
                    return False
                if math.dist(other, opposite) < _TOLERANCE_MM:
                    copies[uid] = node
                    continue
                runs.append((uid, node, tag, point, other))
                count += 1
            sides.append(count)
        if min(sides) == 0:
            return False
        if set(copies) & touched or any(
            self._locks.is_generator_owned(node) for node in copies.values()
        ):
            return False

        # One common axis for every run; the jog itself must cross it.
        _, _, _, first_point, first_other = runs[0]
        axis_len = math.dist(first_point, first_other)
        if axis_len < _TOLERANCE_MM:
            return False
        ux = (first_point[0] - first_other[0]) / axis_len
        uy = (first_point[1] - first_other[1]) / axis_len
        jog = (far[0] - near[0], far[1] - near[1])
        if abs(jog[0] * uy - jog[1] * ux) < 0.1 * math.hypot(*jog):
            return False
        low, high = -math.inf, math.inf
        for uid, node, _tag, point, other in runs:
            vx, vy = point[0] - other[0], point[1] - other[1]
            if abs(vx * uy - vy * ux) > _RUN_AXIS_TOLERANCE_MM or self._locks.is_locked(node):
                return False
            if self._locks.is_generator_owned(node) or uid in touched:
                return False
            along = vx * ux + vy * uy
            # The run keeps its direction and a minimum length either way.
            if along > 0:
                low = max(low, _MIN_RUN_MM - along)
            else:
                high = min(high, -along - _MIN_RUN_MM)
        if not low < 0 < high:
            return False

        rules = self._board_rules
        if rules.dru_error:
            return False
        copper_floor = max(clearance_mm or 0.0, rules.net_requirement(net, self.net_names))

        def jogged(point: tuple[float, float], dx: float, dy: float) -> tuple[float, float]:
            return round(point[0] + dx, 4), round(point[1] + dy, 4)

        jog_copper = LineString([near, far]).buffer(width / 2, quad_segs=16)
        # Prefer the manufacturer's advisory band when the slide fits it.
        for gap in dict.fromkeys((max(warning_clearance_mm or 0.0, required), required)):
            for sign in (1.0, -1.0):
                found = None
                for step in range(1, int(max_displacement / _SLIDE_STEP_MM + 1e-9) + 1):
                    signed = sign * step * _SLIDE_STEP_MM
                    if not low < signed < high:
                        break
                    dx, dy = round(signed * ux, 4), round(signed * uy, 4)
                    if translate(jog_copper, dx, dy).distance(pad) >= gap + margin_mm - 1e-9:
                        found = (dx, dy)
                        break
                if found is None:
                    continue
                dx, dy = found
                moves: list[tuple[BaseGeometry, BaseGeometry, str]] = []
                points: list[tuple[str, str, str | None, float, float]] = []
                moved_points: list[
                    tuple[tuple[float, float], tuple[float, float], frozenset[str]]
                ] = []
                for uid, node in copies.items():
                    s, e = self._xy(node.find("start")), self._xy(node.find("end"))
                    w = _num(node.find("width")) or width
                    if s is None or e is None:
                        return False
                    new_s, new_e = jogged(s, dx, dy), jogged(e, dx, dy)
                    moves.append(
                        (
                            LineString([s, e]).buffer(w / 2, quad_segs=16),
                            LineString([new_s, new_e]).buffer(w / 2, quad_segs=16),
                            layer,
                        )
                    )
                    points.append(("segment", uid, "start", *s))
                    points.append(("segment", uid, "end", *e))
                    moved_points += [(s, new_s, frozenset({layer})), (e, new_e, frozenset({layer}))]
                for uid, node, tag, point, other in runs:
                    w = _num(node.find("width")) or width
                    new_point = jogged(point, dx, dy)
                    moves.append(
                        (
                            LineString([other, point]).buffer(w / 2, quad_segs=16),
                            LineString([other, new_point]).buffer(w / 2, quad_segs=16),
                            layer,
                        )
                    )
                    points.append(("segment", uid, tag, *point))
                    moved_points.append((point, new_point, frozenset({layer})))
                if not self._slide_keeps_connections(points, dx, dy, net):
                    continue
                if self._copper_floor_reason(moves, layer, net, seg_uuid, pads, copper_floor):
                    continue
                displacement = math.hypot(dx, dy)
                if self._other_opening_reason(
                    [opening], moves, moved_points, pads, required, displacement
                ):
                    continue
                new_near = jogged(near, dx, dy)
                action = MaskNudgeAction(
                    action="nudge_clear",
                    uuid=seg_uuid,
                    kind="segment",
                    pad_uuid=opening,
                    pad_ref=ref,
                    net_name=net,
                    layer=layer,
                    old_x=near[0],
                    old_y=near[1],
                    new_x=new_near[0],
                    new_y=new_near[1],
                    dx=dx,
                    dy=dy,
                    displacement_mm=displacement,
                    detail=(
                        f"slid a jog of {len(copies)} track(s) {displacement:.4f}mm along "
                        f"its parallel runs clear of {ref}"
                    ),
                    openings=[opening],
                    points=points,
                )
                return self._apply_nudge(action, result, dry_run) is None
        return False

    def _slide_keeps_connections(
        self,
        points: list[tuple[str, str, str | None, float, float]],
        dx: float,
        dy: float,
        net_name: str,
    ) -> bool:
        """Whether translating ``points`` leaves every same-net contact group joined.

        Compares the net's whole copper-contact graph (tracks, arcs, vias, pads
        and fill) before and after the edit, so a jog that stops touching a pad
        its attached run still reaches is accepted while a real split is not.
        The document is restored before returning.
        """
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.connectivity import ConnectivityValidator

        pcb = PCB(self.doc)
        net = pcb.get_net_by_name(net_name)
        if net is None:
            return False
        previous = ConnectivityValidator(pcb)._contact_graph(net.number)
        original: list[tuple[SExp, list]] = []
        try:
            for _kind, uuid, tag, old_x, old_y in points:
                node = self._segment_by_uuid(uuid)
                end_node = None if node is None or tag is None else node.find(tag)
                if end_node is None:
                    return False
                original.append((end_node, list(end_node.children)))
                end_node.set_value(0, round(old_x + dx, 4))
                end_node.set_value(1, round(old_y + dy, 4))
            current = ConnectivityValidator(PCB(self.doc))._contact_graph(net.number)
            return bool(current.preserves_connections(previous))
        finally:
            for end_node, children in original:
                end_node.children[:] = children

    def _has_fixed_junction_contact(
        self, point: tuple[float, float], layer: str, net_name: str
    ) -> bool:
        """A straight-end move cannot drag an arc or an unbroken crossing track."""
        for node in self.doc.find_all("arc") + self.doc.find_all("segment"):
            layer_node = node.find("layer")
            if (
                layer_node is None
                or _bare(layer_node.get_first_atom()) != layer
                or self._net_name(node) != net_name
            ):
                continue
            start, end = self._xy(node.find("start")), self._xy(node.find("end"))
            if start is None or end is None:
                continue
            at_end = min(math.dist(point, start), math.dist(point, end)) < _TOLERANCE_MM
            if node.tag == "arc":
                try:
                    path = Arc.from_sexp(node).centerline_points()
                except ValueError:
                    return True
                if LineString(path).distance(Point(point)) <= _TOLERANCE_MM + 0.00001:
                    return True
            elif not at_end and LineString([start, end]).distance(Point(point)) < _TOLERANCE_MM:
                return True
        return False

    def _loses_same_net_contact(
        self,
        moves: list[tuple[BaseGeometry, BaseGeometry, str]],
        layer: str,
        net_name: str,
        moving: set[str],
        pads: dict,
        *,
        protect_ends: bool = False,
    ) -> bool:
        """Whether a reshaped leg would stop touching same-net copper it touches now.

        Compares the physical copper (track and arc widths, via annuli, pad
        faces), not centerlines: a branch that meets a leg by its width, or a
        pad beside it, is a real contact. A contact that survives the move
        (including a collinear overlap) is fine; one that would separate
        declines the whole grouped edit. An arc or pad whose shape cannot be
        modelled declines when it is anywhere near a reshaped leg.
        """
        olds = [old for old, _new, _layer in moves]
        news = [new for _old, new, _layer in moves]
        reach = unary_union([old.buffer(_TOLERANCE_MM) for old in olds])
        # Contact kept only if the new copper still overlaps by more than the
        # editor's four-decimal rounding grid.
        shrunk = [new.buffer(-0.0002) for new in news]
        neighbours: list[BaseGeometry] = []
        for uuid, node in self._segment_nodes().items():
            if uuid in moving or self._net_name(node) != net_name:
                continue
            layer_node = node.find("layer")
            if layer_node is None or _bare(layer_node.get_first_atom()) != layer:
                continue
            start, end = self._xy(node.find("start")), self._xy(node.find("end"))
            width_node = node.find("width")
            if start is None or end is None or width_node is None:
                return True
            neighbours.append(LineString([start, end]).buffer(_num(width_node) / 2, quad_segs=16))
            if protect_ends:
                neighbours.extend(
                    Point(p).buffer(_num(width_node) / 2, quad_segs=16) for p in (start, end)
                )
        for node in self.doc.find_all("arc"):
            layer_node = node.find("layer")
            if (
                layer_node is None
                or _bare(layer_node.get_first_atom()) != layer
                or self._net_name(node) != net_name
            ):
                continue
            width_node = node.find("width")
            try:
                path = LineString(Arc.from_sexp(node).centerline_points())
            except ValueError:
                return True
            if width_node is None:
                return True
            neighbours.append(path.buffer(_num(width_node) / 2, quad_segs=16))
        for via in self.doc.find_all("via"):
            at = self._xy(via.find("at"))
            size_node = via.find("size")
            uid = via.find("uuid")
            if protect_ends and uid is not None and _bare(uid.get_first_atom()) in moving:
                continue
            if (
                at is None
                or size_node is None
                or self._net_name(via) != net_name
                or layer not in self._via_span_layer_set(via)
            ):
                continue
            neighbours.append(Point(at).buffer(_num(size_node) / 2, quad_segs=16))
        for identity, (poly, _centre, pad_layers, pad_net, _ref, _shape) in pads.items():
            if pad_net != net_name or not (
                layer in pad_layers
                or "*.Cu" in pad_layers
                or ("F&B.Cu" in pad_layers and layer in {"F.Cu", "B.Cu"})
            ):
                continue
            if identity in self._pivot_pad_bounds:
                # A primitive or displaced land can reach well past its anchor.
                # An enclosing shape can only rule out contact at a distance.
                bound = self._pivot_pad_bounds[identity]
                if bound is None or reach.intersects(bound):
                    return True
                continue
            neighbours.append(poly)
        for neighbour in neighbours:
            if not reach.intersects(neighbour):
                continue
            for old, kept in zip(olds, shrunk, strict=True):
                if old.intersects(neighbour) and not kept.intersects(neighbour):
                    return True
        return False

    @staticmethod
    def _shift_junction_clear(
        fixed: tuple[float, float],
        junction: tuple[float, float],
        openings: list[BaseGeometry],
        centreline_clearance: float,
    ) -> tuple[float, float] | None:
        """Move the free end of a track until its whole width clears every opening."""
        # The editor writes four decimal places. Leave one unit of that grid
        # so rounding the moved junction cannot consume the requested margin.
        centreline_clearance += 0.0001
        if any(opening.distance(Point(fixed)) < centreline_clearance for opening in openings):
            return None
        x, y = junction
        for _ in range(16):
            track = LineString([fixed, (x, y)])
            obstacle = min(openings, key=track.distance)
            gap = track.distance(obstacle)
            if gap >= centreline_clearance - 1e-9:
                return x - junction[0], y - junction[1]
            face, nearest = nearest_points(obstacle, track)
            # A point a fraction t along the track moves by t times the
            # junction displacement. At the via (t=0) it cannot move.
            fraction = track.project(nearest, normalized=True) if track.length else 0.0
            if fraction <= 1e-9 or gap <= 1e-9:
                return None
            scale = (centreline_clearance - gap) / (fraction * gap)
            x += (nearest.x - face.x) * scale
            y += (nearest.y - face.y) * scale
        return None

    def _nudge_via_clear(
        self,
        via_node: SExp,
        via_uuid: str,
        openings: list[str],
        pairs: list[tuple[str, float | None]],
        required: float,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        design_rules: DesignRules | None,
        margin_mm: float,
        touched_by_action: set[str],
    ) -> tuple[str, str] | None:
        """Move a pass-through via (dragging its anchored track ends) clear.

        When the shortest push is declined by the clearance gates, or the
        destination it selects needs more than ``max_displacement``,
        alternative displacements are searched (issue #9939): bounded by the
        caller's maximum, ordered shortest first, re-validated against the
        project's authored net classes on every spanned layer.  A candidate
        that survives keeps each anchored approach on its own line and adds a
        short 0/45/90/135-degree connection wholly inside the new via land
        instead of dragging the ends off angle.  A via with no legal
        candidate stays where it is, reported with the primary refusal.
        """
        entries = [pads[opening] for opening in openings]
        if any(entry[5] == "unsupported" for entry in entries):
            return "unverified_pad_shape", ""
        net_name = self._net_name(via_node)
        if not net_name or any(entry[3] != net_name for entry in entries):
            return "not_same_net", ""
        if self._locks.is_locked(via_node):
            return "locked_via", ""

        at = self._xy(via_node.find("at"))
        size_node = via_node.find("size")
        if at is None or size_node is None:
            return "malformed_via", ""
        size = _num(size_node)
        span = self._via_span_orders(via_node)
        if span is None:
            return "malformed_via", ""
        for entry in entries:
            if not self._pad_layers_meet_span(entry[2], span):
                return "pad_not_on_via_span", ""

        copper = Point(at).buffer(size / 2, quad_segs=32)

        joined = self._anchored_ends(at[0], at[1], net_name)
        if joined is None:
            return "locked_attached_track", ""

        refusal = self._opening_model_refusal(pairs, copper, pads, via_uuid in touched_by_action)
        if refusal is not None:
            return refusal

        # Authored project rules for every candidate this via could take
        # (issue #9939): a destination that only passes the manufacturer's
        # floor can still violate the project's net classes, and the saved
        # board would then fail its native re-check.
        project = self._via_project_obstacles(
            via_node,
            net_name,
            at,
            size,
            self._via_span_layer_set(via_node),
            joined,
            pads,
            max_displacement,
        )

        push = self._push_clear_of_openings(
            copper, [(pads[o][0], pads[o][1]) for o in openings], required + margin_mm
        )
        primary_reason: tuple[str, str] | None = None
        primary_push: tuple[float, float] | None = None
        if push is not None:
            displacement = math.hypot(*push)
            if displacement < _TOLERANCE_MM / 2:
                return "already_cleared", ""
            if displacement > max_displacement:
                primary_reason = "exceeds_max_displacement", f"needs {displacement:.4f}mm"
            else:
                primary_push = push
                primary_reason = self._via_drag_candidate(
                    via_node,
                    via_uuid,
                    openings,
                    pads,
                    result,
                    at,
                    size,
                    copper,
                    joined,
                    push,
                    required,
                    displacement,
                    max_displacement,
                    dry_run,
                    clearance_mm,
                    design_rules,
                    touched_by_action,
                    project,
                )
                if primary_reason is None:
                    return None
        else:
            primary_reason = "infeasible", ""

        # The shortest push did not survive its gates: search alternative
        # displacements with the kept-line contact model (issue #9939).
        if project is None:
            return primary_reason
        for candidate in self._via_alternate_candidates(
            at, size, openings, pads, required + margin_mm, max_displacement, primary_push
        ):
            outcome = self._via_tails_candidate(
                via_node,
                via_uuid,
                openings,
                pads,
                result,
                at,
                size,
                copper,
                joined,
                candidate,
                required,
                math.hypot(*candidate),
                max_displacement,
                dry_run,
                clearance_mm,
                design_rules,
                touched_by_action,
                project,
            )
            if outcome is None:
                return None
        return primary_reason

    def _via_drag_candidate(
        self,
        via_node: SExp,
        via_uuid: str,
        openings: list[str],
        pads: dict,
        result: MaskRepairResult,
        at: tuple[float, float],
        size: float,
        copper: BaseGeometry,
        joined: list[tuple[str, SExp, str, tuple[float, float]]],
        push: tuple[float, float],
        required: float,
        displacement: float,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        design_rules: DesignRules | None,
        touched_by_action: set[str],
        project: tuple | None,
    ) -> tuple[str, str] | None:
        """Evaluate the shortest push with the historical drag contact model."""
        dx, dy = push
        new_at = (at[0] + dx, at[1] + dy)
        span_layers = self._via_span_layer_set(via_node)
        moves: list[tuple[BaseGeometry, BaseGeometry, str]] = [
            (copper, Point(new_at).buffer(size / 2, quad_segs=32), name)
            for name in sorted(span_layers)
        ]
        points: list[tuple[str, str, str | None, float, float]] = [
            ("via", via_uuid, None, at[0], at[1])
        ]
        for join_uuid, join_node, join_tag, (join_x, join_y) in joined:
            join_layer_node = join_node.find("layer")
            join_layer = (
                _bare(join_layer_node.get_first_atom()) if join_layer_node is not None else ""
            )
            if not join_layer or join_tag is None:
                return "malformed_segment", ""
            join_width_node = join_node.find("width")
            join_other = self._xy(join_node.find("end" if join_tag == "start" else "start"))
            if join_width_node is None or join_other is None:
                return "malformed_segment", ""
            join_width = _num(join_width_node)
            moves.append(
                (
                    LineString([join_other, (join_x, join_y)]).buffer(join_width / 2, quad_segs=16),
                    LineString([join_other, new_at]).buffer(join_width / 2, quad_segs=16),
                    join_layer,
                )
            )
            points.append(("segment", join_uuid, join_tag, join_x, join_y))

        net_name = self._net_name(via_node)
        if design_rules is not None:
            reason = self._target_clearance_reason(at[0], at[1], new_at[0], new_at[1], design_rules)
            if reason is not None and self._fail_closed_reason_is_local(reason, new_at, size, pads):
                return "would_violate_clearance", reason
            dragged = self._dragged_track_reason(
                joined, round(new_at[0], 4), round(new_at[1], 4), net_name, design_rules
            )
            if dragged is not None:
                return "would_violate_clearance", dragged
        for span_layer in sorted(span_layers):
            refusal = self._copper_floor_reason(
                moves, span_layer, net_name, via_uuid, pads, clearance_mm
            )
            if refusal is not None:
                return refusal
        if project is not None:
            project_detail = self._via_project_refusal(
                project,
                Point(new_at).buffer(size / 2, quad_segs=32),
                moves[len(span_layers) :],  # the via disc is checked separately
                new_at,
            )
            if project_detail is not None:
                return "would_violate_clearance", project_detail
        refusal = self._other_opening_reason(
            openings,
            moves,
            [(at, new_at, frozenset(span_layers))],
            pads,
            required,
            displacement,
        )
        if refusal is not None:
            return refusal

        first = pads[openings[0]]
        refs = ", ".join(pads[o][4] for o in openings)
        action = MaskNudgeAction(
            action="nudge_clear",
            uuid=via_uuid,
            kind="via",
            pad_uuid=openings[0],
            pad_ref=first[4],
            net_name=net_name,
            layer=self._via_span(via_node),
            old_x=at[0],
            old_y=at[1],
            new_x=new_at[0],
            new_y=new_at[1],
            dx=dx,
            dy=dy,
            displacement_mm=displacement,
            detail=(
                f"moved via {displacement:.4f}mm clear of {refs}, "
                f"dragging {len(joined)} track end(s)"
            ),
            openings=list(openings),
            points=points,
        )
        return self._apply_nudge(action, result, dry_run)

    def _via_alternate_candidates(
        self,
        at: tuple[float, float],
        size: float,
        openings: list[str],
        pads: dict,
        target_gap: float,
        max_displacement: float,
        primary: tuple[float, float] | None,
    ) -> list[tuple[float, float]]:
        """Alternative via displacements that clear every opening, shortest first.

        The shortest push (``primary``, when it exists) comes first, then a
        bounded polar grid: ``_VIA_SEARCH_DIRECTIONS`` directions at
        ascending magnitudes up to the caller's maximum.  A candidate is
        kept only when the whole translated disc clears every opening by
        ``target_gap`` (exact: the disc-to-polygon distance equals the
        centre distance minus the radius).  Rounded duplicates collapse.
        """
        seen: set[tuple[float, float]] = set()
        out: list[tuple[float, float]] = []

        def consider(dx: float, dy: float) -> None:
            centre = (round(at[0] + dx, 4), round(at[1] + dy, 4))
            key = (round(centre[0] - at[0], 4), round(centre[1] - at[1], 4))
            if key in seen or math.hypot(*key) > max_displacement + 1e-9:
                return
            point = Point(centre)
            for opening in openings:
                if pads[opening][0].distance(point) - size / 2 < target_gap - 1e-9:
                    return
            seen.add(key)
            out.append(key)

        if primary is not None:
            consider(*primary)
        for step in range(1, int(max_displacement / _VIA_SEARCH_STEP_MM) + 1):
            magnitude = round(step * _VIA_SEARCH_STEP_MM, 4)
            for index in range(_VIA_SEARCH_DIRECTIONS):
                angle = math.tau * index / _VIA_SEARCH_DIRECTIONS
                consider(magnitude * math.cos(angle), magnitude * math.sin(angle))
        out.sort(key=lambda d: (round(math.hypot(*d), 4), round(math.atan2(d[1], d[0]), 6)))
        return out

    def _via_tail_plan(
        self,
        joined: list[tuple[str, SExp, str, tuple[float, float]]],
        new_at: tuple[float, float],
        size: float,
        max_displacement: float,
    ) -> tuple[list, list, list] | None:
        """Kept-line contact plan for one destination; ``None`` when infeasible.

        Each anchored end slides along its own segment line to the point
        where a 0/45/90/135-degree connection to the new via centre starts,
        chosen shortest-connection first.  The connection, its width
        included, must lie wholly inside the new via land with
        ``_TAIL_LAND_MARGIN_MM`` of radial margin, and the slide stays
        within ``max_displacement``.  Returns
        ``(end_moves, tails, final_lines)`` with serialized 4-decimal
        coordinates: ``end_moves`` entries carry the old and new end point,
        ``tails`` entries the connection's ``(start, end, width, layer,
        net)`` (uuids are minted when the plan is accepted), and
        ``final_lines`` the ``(old, new, layer)`` buffered copper of each
        approach for the clearance gates.
        """
        radius = size / 2
        centre = (round(new_at[0], 4), round(new_at[1], 4))
        end_moves: list[tuple[str, str, float, float, float, float]] = []
        tails: list[tuple[float, float, float, float, float, str, int | str]] = []
        final_lines: list[tuple[BaseGeometry, BaseGeometry, str]] = []
        for join_uuid, join_node, join_tag, end in joined:
            layer_node = join_node.find("layer")
            layer = _bare(layer_node.get_first_atom()) if layer_node is not None else ""
            width_node = join_node.find("width")
            other = self._xy(join_node.find("end" if join_tag == "start" else "start"))
            net_node = join_node.find("net")
            net_value = net_node.get_first_atom() if net_node is not None else None
            if not layer or join_tag is None or width_node is None or other is None:
                return None
            if not isinstance(net_value, (int, str)):
                return None
            width = _num(width_node)
            # The approach line through ``other`` and the anchored end; the
            # connection point stays on it, so the segment never changes angle.
            line_dx, line_dy = end[0] - other[0], end[1] - other[1]
            line_len = math.hypot(line_dx, line_dy)
            if line_len < _TOLERANCE_MM:
                return None
            best: tuple[float, float, float, float] | None = None
            for ux, uy in _ALIGNED_TAIL_DIRS:
                for sign in (1.0, -1.0):
                    rx, ry = sign * ux, sign * uy
                    det = line_dx * ry - line_dy * rx
                    if abs(det) < 1e-12:
                        continue
                    cx, cy = centre[0] - other[0], centre[1] - other[1]
                    s = (cx * ry - cy * rx) / det
                    t = (line_dx * cy - line_dy * cx) / det
                    slide = abs(s - 1.0) * line_len
                    tail_len = abs(t) * math.hypot(rx, ry)
                    if slide > max_displacement + 1e-9:
                        continue
                    if s * line_len < _TOLERANCE_MM:
                        continue
                    if tail_len + width / 2 > radius - _TAIL_LAND_MARGIN_MM:
                        continue
                    qx, qy = other[0] + s * line_dx, other[1] + s * line_dy
                    candidate = (tail_len, slide, qx, qy)
                    if best is None or candidate[:2] < best[:2]:
                        best = candidate
            if best is None:
                return None
            _tail_len, _slide, qx, qy = best
            # Serialize the slid end and its connection at the file's full
            # coordinate precision (KiCad's 1 nm grid): rounding each axis
            # to the editor's four decimals independently can break the
            # exact leg equality an aligned approach or connection carries
            # (issue #9939: one Board 03 approach measured 0.0196 deg off
            # after a one-quantum asymmetry, and a 0.01 mm tail once
            # collapsed to zero length).  The via centre itself keeps the
            # editor grid like every other nudge.
            new_end = (round(qx, 6), round(qy, 6))
            end_moves.append((join_uuid, join_tag, end[0], end[1], new_end[0], new_end[1]))
            if math.dist(new_end, centre) >= _MIN_TAIL_MM:
                tails.append(
                    (new_end[0], new_end[1], centre[0], centre[1], width, layer, net_value)
                )
            final_lines.append(
                (
                    LineString([other, end]).buffer(width / 2, quad_segs=16),
                    LineString([other, new_end]).buffer(width / 2, quad_segs=16),
                    layer,
                )
            )
        return end_moves, tails, final_lines

    def _via_tails_candidate(
        self,
        via_node: SExp,
        via_uuid: str,
        openings: list[str],
        pads: dict,
        result: MaskRepairResult,
        at: tuple[float, float],
        size: float,
        copper: BaseGeometry,
        joined: list[tuple[str, SExp, str, tuple[float, float]]],
        push: tuple[float, float],
        required: float,
        displacement: float,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        design_rules: DesignRules | None,
        touched_by_action: set[str],
        project: tuple,
    ) -> tuple[str, str] | None:
        """Evaluate one alternative destination with the kept-line contact model."""
        from ..router.primitives import _make_uuid
        from ..router.quantize import _is_quantum_aligned

        net_name = self._net_name(via_node)
        span_layers = self._via_span_layer_set(via_node)
        new_at = (round(at[0] + push[0], 4), round(at[1] + push[1], 4))
        plan = self._via_tail_plan(joined, new_at, size, max_displacement)
        if plan is None:
            return "no_aligned_contact", ""
        end_moves, tails, final_lines = plan
        for start_x, start_y, end_x, end_y, *_rest in tails:
            if not _is_quantum_aligned(
                round(end_x, 6) - round(start_x, 6), round(end_y, 6) - round(start_y, 6)
            ):
                return "no_aligned_contact", ""
        new_disc = Point(new_at).buffer(size / 2, quad_segs=32)
        moves: list[tuple[BaseGeometry, BaseGeometry, str]] = [
            (copper, new_disc, name) for name in sorted(span_layers)
        ]
        moves.extend(final_lines)

        # Authored net classes first: cheap, and it is the gate the
        # manufacturer floor cannot express (issue #9939).
        refusal_detail = self._via_project_refusal(project, new_disc, final_lines, new_at)
        if refusal_detail is not None:
            return "would_violate_clearance", refusal_detail
        if design_rules is not None:
            reason = self._target_clearance_reason(at[0], at[1], new_at[0], new_at[1], design_rules)
            if reason is not None and self._fail_closed_reason_is_local(reason, new_at, size, pads):
                return "would_violate_clearance", reason
        for span_layer in sorted(span_layers):
            refusal = self._copper_floor_reason(
                moves, span_layer, net_name, via_uuid, pads, clearance_mm
            )
            if refusal is not None:
                return refusal
        refusal = self._other_opening_reason(
            openings,
            moves,
            [(at, new_at, frozenset(span_layers))],
            pads,
            required,
            displacement,
        )
        if refusal is not None:
            return refusal

        added: list[tuple[str, float, float, float, float, float, str, int | str]] = [
            (_make_uuid(), *tail) for tail in tails
        ]
        points: list[tuple[str, str, str | None, float, float]] = [
            ("via", via_uuid, None, at[0], at[1])
        ]
        points.extend(
            ("segment", uuid, tag, old_x, old_y)
            for uuid, tag, old_x, old_y, _new_x, _new_y in end_moves
        )
        points.extend(("segment", tail[0], None, tail[1], tail[2]) for tail in added)
        first = pads[openings[0]]
        refs = ", ".join(pads[o][4] for o in openings)
        action = MaskViaRelocateAction(
            action="relocate_clear",
            uuid=via_uuid,
            kind="via",
            pad_uuid=openings[0],
            pad_ref=first[4],
            net_name=net_name,
            layer=self._via_span(via_node),
            old_x=at[0],
            old_y=at[1],
            new_x=new_at[0],
            new_y=new_at[1],
            dx=push[0],
            dy=push[1],
            displacement_mm=displacement,
            detail=(
                f"moved via {displacement:.4f}mm clear of {refs}, "
                f"keeping {len(joined)} approach line(s) with "
                f"{len(added)} in-land connection(s)"
            ),
            openings=list(openings),
            points=points,
            end_moves=end_moves,
            added_segments=added,
        )
        return self._apply_via_relocate(action, result, dry_run)

    def _via_project_obstacles(
        self,
        via_node: SExp,
        net_name: str,
        at: tuple[float, float],
        size: float,
        span_layers: frozenset[str],
        joined: list[tuple[str, SExp, str, tuple[float, float]]],
        pads: dict,
        max_displacement: float,
    ) -> tuple | None:
        """Foreign copper and holes near the search window with the project's
        own per-pair requirements; ``None`` when the authored rules cannot
        be read (the search then declines instead of guessing).

        Every obstacle carries its requirement from
        :class:`BoardClearanceRules` (authored net classes, board minima and
        ``.kicad_dru`` included), the copper layers it applies to, and the
        pre-move gaps of the via disc and each anchored approach.  Pads
        reuse the opening model's polygons; pad holes use the pad centre
        (``drill_offset`` is negligible on real boards and this gate is
        second to the native re-check anyway).
        """
        from kicad_tools.router.board_clearance_rules import (
            item_props_for_type,
            pad_clearance_override,
        )

        rules = self._board_rules
        if rules.dru_error:
            return None
        drill_node = via_node.find("drill")
        drill = _num(drill_node) if drill_node is not None else 0.0
        via_props = item_props_for_type("via", net_name, True)
        old_disc = Point(at).buffer(size / 2, quad_segs=32)
        old_lines: list[tuple[BaseGeometry, str]] = []
        for _uuid, node, tag, end in joined:
            other = self._xy(node.find("end" if tag == "start" else "start"))
            layer_node = node.find("layer")
            if other is None or layer_node is None:
                continue
            old_lines.append(
                (
                    LineString([other, end]).buffer(_num(node.find("width")) / 2, quad_segs=16),
                    _bare(layer_node.get_first_atom()),
                )
            )
        copper: list[tuple[BaseGeometry, float, frozenset[str], str, list[float]]] = []
        holes: list[tuple[tuple[float, float], float, float, float, str]] = []

        def add_copper(
            geom: BaseGeometry, required: float, layers: frozenset[str], description: str
        ) -> None:
            if required <= 0:
                return
            # A moved endpoint can close the old gap by at most the caller's
            # displacement bound. Check the whole approach on its copper layer.
            gaps = [old_disc.distance(geom)]
            gaps.extend(
                line.distance(geom) if layer in layers else math.inf for line, layer in old_lines
            )
            if min(gaps) <= required + max_displacement + _TOLERANCE_MM:
                copper.append((geom, required, layers, description, gaps))

        def add_hole(
            centre: tuple[float, float], radii: float, required: float, description: str
        ) -> None:
            gap = math.dist(at, centre) - radii
            if required > 0 and gap <= required + max_displacement + _TOLERANCE_MM:
                holes.append((centre, radii, required, gap, description))

        joined_uuids = {uuid for uuid, _node, _tag, _end in joined}
        for uuid, node in self._segment_nodes().items():
            if uuid in joined_uuids or self._net_name(node) == net_name:
                continue
            layer_node = node.find("layer")
            layer = _bare(layer_node.get_first_atom()) if layer_node is not None else ""
            if not layer or layer not in span_layers:
                continue
            seg_start, seg_end = self._xy(node.find("start")), self._xy(node.find("end"))
            width_node = node.find("width")
            if seg_start is None or seg_end is None or width_node is None:
                continue
            geom = LineString([seg_start, seg_end]).buffer(_num(width_node) / 2, quad_segs=16)
            other_net = self._net_name(node)
            required = rules.clearance(
                via_props,
                item_props_for_type(
                    "track", other_net, False, layer=layer, footprint_ref="", footprint_lib_id=""
                ),
                layer,
            )
            add_copper(
                geom, required, frozenset({layer}), f"track on net {other_net or 'unassigned'}"
            )

        for other_via in self.doc.find_all("via"):
            other_at = self._xy(other_via.find("at"))
            other_size = other_via.find("size")
            if other_at is None or other_size is None or math.dist(other_at, at) < _TOLERANCE_MM:
                continue
            other_net = self._net_name(other_via)
            if other_net == net_name:
                continue
            layers = self._via_span_layer_set(other_via)
            if not (layers & span_layers):
                continue
            geom = Point(other_at).buffer(_num(other_size) / 2, quad_segs=32)
            other_props = item_props_for_type("via", other_net, True)
            required = rules.clearance(via_props, other_props)
            description = f"via at ({other_at[0]:.2f}, {other_at[1]:.2f})"
            add_copper(geom, required, layers, description)
            other_drill = other_via.find("drill")
            if other_drill is not None and drill > 0:
                add_hole(
                    other_at,
                    (drill + _num(other_drill)) / 2,
                    rules.hole_to_hole(via_props, other_props),
                    description,
                )

        from kicad_tools.schema.pcb import PCB

        pcb = PCB(self.doc)
        for fp in pcb.footprints:
            for index, pad in enumerate(fp.pads):
                identity = _bare(pad.uuid) if pad.uuid else f"{fp.reference}:pad:{index}"
                entry = pads.get(identity)
                if entry is None:
                    continue
                poly, centre, pad_layers, pad_net, ref, _shape = entry
                if pad_net == net_name:
                    continue
                meets = (
                    bool(span_layers & set(pad_layers))
                    or "*.Cu" in pad_layers
                    or ("F&B.Cu" in pad_layers and bool(span_layers & {"F.Cu", "B.Cu"}))
                )
                if not meets:
                    continue
                pad_props = item_props_for_type(
                    "pad",
                    pad_net,
                    pad.type == "thru_hole",
                    pad.type,
                    fp.layer,
                    fp.reference,
                    fp.name,
                )
                layers = frozenset(
                    name
                    for name in span_layers
                    if name in pad_layers or "*.Cu" in pad_layers or "F&B.Cu" in pad_layers
                )
                local = pad_clearance_override(fp, pad)
                electrical = (
                    rules.clearance(via_props, pad_props)
                    if local is None
                    else max(local, rules.min_clearance or 0.0)
                )
                required = max(rules.physical_clearance(via_props, pad_props), electrical)
                add_copper(poly, required, layers, f"pad {ref}")
                if pad.drill > 0 and drill > 0:
                    add_hole(
                        centre,
                        (drill + pad.drill) / 2,
                        rules.hole_clearance(via_props, pad_props),
                        f"pad {ref} hole",
                    )
        return copper, holes

    def _via_project_refusal(
        self,
        project: tuple,
        new_disc: BaseGeometry,
        final_lines: list[tuple[BaseGeometry, BaseGeometry, str]],
        new_at: tuple[float, float],
    ) -> str | None:
        """Detail string when a destination violates an authored project rule.

        A move is refused only when it would *newly* sit below a project
        requirement (a violation the pre-move board already carried is not
        this move's doing), mirroring the transaction's new-error
        attribution.
        """
        copper, holes = project
        for geom, required, layers, desc, old in copper:
            new_gap = new_disc.distance(geom)
            if new_gap < required - 1e-6 and new_gap < old[0] - 1e-6:
                return f"project rules need {required:.3f}mm to {desc}; move leaves {new_gap:.3f}mm"
            for index, (line, layer) in enumerate(
                (new_line, layer) for _old, new_line, layer in final_lines
            ):
                if layer not in layers:
                    continue
                previous = old[index + 1] if index + 1 < len(old) else math.inf
                new_gap = line.distance(geom)
                if new_gap < required - 1e-6 and new_gap < previous - 1e-6:
                    return (
                        f"project rules need {required:.3f}mm to {desc}; "
                        f"moved approach leaves {new_gap:.3f}mm on {layer}"
                    )
        for centre, radii, required, old_gap, desc in holes:
            new_gap = math.dist(new_at, centre) - radii
            if new_gap < required - 1e-6 and new_gap < old_gap - 1e-6:
                return (
                    f"project rules need {required:.3f}mm hole spacing to {desc}; "
                    f"move leaves {new_gap:.3f}mm"
                )
        return None

    def _apply_via_relocate(
        self, action: MaskViaRelocateAction, result: MaskRepairResult, dry_run: bool
    ) -> tuple[str, str] | None:
        """Record one kept-line via relocation; ``None`` on success."""
        targets: list[tuple[SExp, float, float]] = []
        via_at: SExp | None = None
        if not dry_run:
            via_node = self._via_by_uuid(action.uuid)
            at_node = None if via_node is None else via_node.find("at")
            if at_node is None:
                return "malformed_via", ""
            via_at = at_node
            targets.append((at_node, action.old_x, action.old_y))
            for uuid, tag, old_x, old_y, _new_x, _new_y in action.end_moves:
                node = self._segment_by_uuid(uuid)
                end_node = node.find(tag) if node is not None and tag else None
                if end_node is None:
                    return "malformed_segment", ""
                targets.append((end_node, old_x, old_y))
            for tail in action.added_segments:
                if self._segment_by_uuid(tail[0]) is not None:
                    return "added_segment_already_present", ""
            # Resolve every point first so a lookup miss can neither leave
            # the document half-translated nor record an unapplied action.
            for end_node, old_x, old_y in targets:
                xy = self._xy(end_node)
                if xy is None or math.dist(xy, (old_x, old_y)) > _TOLERANCE_MM:
                    return "stale_geometry", ""
        result.actions.append(action)
        if not dry_run and via_at is not None:
            via_at.set_value(0, round(action.new_x, 4))
            via_at.set_value(1, round(action.new_y, 4))
            for uuid, tag, _old_x, _old_y, new_x, new_y in action.end_moves:
                node = self._segment_by_uuid(uuid)
                end_node = node.find(tag) if node is not None and tag else None
                if end_node is not None:
                    # Full file precision: the slid end must stay exactly on
                    # its approach line (see ``_via_tail_plan``).
                    end_node.set_value(0, round(new_x, 6))
                    end_node.set_value(1, round(new_y, 6))
            for tail in action.added_segments:
                self._insert_segment_after_last(self._tail_segment_node(tail))
            self.modified = True
        result.repaired += 1
        return None

    def _tail_segment_node(
        self, tail: tuple[str, float, float, float, float, float, str, int | str]
    ) -> SExp:
        """Build the SExp node for one added in-land connection."""
        uuid, start_x, start_y, end_x, end_y, width, layer, net = tail
        seg = SExp(name="segment")
        seg.append(SExp.list("start", round(start_x, 6), round(start_y, 6)))
        seg.append(SExp.list("end", round(end_x, 6), round(end_y, 6)))
        seg.append(SExp.list("width", round(width, 4)))
        seg.append(SExp.list("layer", layer))
        seg.append(SExp.list("uuid", uuid))
        seg.append(SExp.list("net", SExp.quoted_atom(net) if isinstance(net, str) else net))
        return seg

    def _insert_segment_after_last(self, node: SExp) -> None:
        """Insert a segment node after the document's last top-level segment."""
        index = None
        for position, child in enumerate(self.doc.children):
            if child.name == "segment":
                index = position
        if index is None:
            self.doc.children.append(node)
        else:
            self.doc.children.insert(index + 1, node)

    def _undo_via_relocate(self, action: MaskViaRelocateAction) -> bool:
        """Reverse one kept-line via relocation, removing its added copper.

        Returns ``False`` when a node is gone or no point sits at its
        post-move position (callers fall back to a snapshot restore).
        """
        via_node = self._via_by_uuid(action.uuid)
        at_node = None if via_node is None else via_node.find("at")
        xy = self._xy(at_node)
        if at_node is None or xy is None:
            return False
        if math.dist(xy, (round(action.new_x, 4), round(action.new_y, 4))) > _TOLERANCE_MM:
            return False
        restore: list[tuple[SExp, float, float]] = [(at_node, action.old_x, action.old_y)]
        for uuid, tag, old_x, old_y, new_x, new_y in action.end_moves:
            node = self._segment_by_uuid(uuid)
            end_node = node.find(tag) if node is not None and tag else None
            xy = self._xy(end_node)
            if (
                end_node is None
                or xy is None
                or math.dist(xy, (round(new_x, 6), round(new_y, 6))) > _TOLERANCE_MM
            ):
                return False
            restore.append((end_node, old_x, old_y))
        added: list[SExp] = []
        for tail in action.added_segments:
            node = self._segment_by_uuid(tail[0])
            if node is None:
                return False
            added.append(node)
        for node in added:
            try:
                self.doc.children.remove(node)
            except ValueError:
                return False
        for end_node, old_x, old_y in restore:
            end_node.set_value(0, old_x)
            end_node.set_value(1, old_y)
        self.modified = True
        return True

    def _apply_nudge(
        self, action: MaskNudgeAction, result: MaskRepairResult, dry_run: bool
    ) -> tuple[str, str] | None:
        """Record one nudge and translate its points; ``None`` on success."""
        if not dry_run:
            # Resolve every point first so a lookup miss can neither leave
            # the document half-translated nor record an unapplied action.
            targets: list[tuple[SExp, float, float]] = []
            for kind, uuid, tag, old_x, old_y in action.points:
                node = self._segment_by_uuid(uuid) if kind == "segment" else self._via_by_uuid(uuid)
                end_node = None if node is None else node.find(tag) if tag else node.find("at")
                if end_node is None:
                    return "malformed_segment", ""
                targets.append((end_node, old_x, old_y))
        result.actions.append(action)
        if not dry_run:
            for end_node, old_x, old_y in targets:
                end_node.set_value(0, round(old_x + action.dx, 4))
                end_node.set_value(1, round(old_y + action.dy, 4))
            self.modified = True
        result.repaired += 1
        return None

    @staticmethod
    def _push_clear_of_openings(
        shape: BaseGeometry,
        openings: list[tuple[BaseGeometry, tuple[float, float]]],
        target_gap: float,
        iterations: int = 16,
    ) -> tuple[float, float] | None:
        """Translate ``shape`` away from the openings until it clears ``target_gap``.

        Each round re-measures the true minimum distance against the binding
        (nearest) opening and pushes along the ray from that opening's centre
        through the shape's nearest point, re-anchoring the direction on the
        moving geometry so a corner converges (the same contract as
        ``CopperGapRepairer._push_clear_of``).  A corner approach converges
        only asymptotically (each round clears about half the residue as the
        nearest point slides along the opening's edge), so every round pushes
        1.5x the residue: an overshoot simply lands beyond the target and
        exits, and the final position is re-verified against every opening
        before the translation is returned.  Returns the total translation,
        or ``None`` when it does not converge (opposing openings).
        """
        dx = dy = 0.0
        for _ in range(iterations):
            current = translate(shape, dx, dy)
            gap, centre, poly = min(
                (current.distance(opening), centre, opening) for opening, centre in openings
            )
            if gap >= target_gap - 1e-9:
                return dx, dy
            nearest = nearest_points(poly, current)[1]
            ux, uy = nearest.x - centre[0], nearest.y - centre[1]
            norm = math.hypot(ux, uy)
            if norm < 1e-9:
                return None
            push = (target_gap - gap) * 1.5
            dx += ux / norm * push
            dy += uy / norm * push
        current = translate(shape, dx, dy)
        if min(current.distance(opening) for opening, _ in openings) >= target_gap - 1e-9:
            return dx, dy
        return None

    def _opening_model_refusal(
        self,
        pairs: list[tuple[str, float | None]],
        copper: BaseGeometry,
        pads: dict,
        touched: bool,
    ) -> tuple[str, str] | None:
        """Refuse a candidate whose opening model disagrees with the measurement.

        The pad-shape opening model is faithful only while the plotted mask
        matches the pad copper (zero board expansion, no per-pad margin).  A
        disjoint finding whose measured clearance the model cannot reproduce
        is left to the workflows that own board-specific exceptions.  A
        conductor an earlier action this run already moved or dragged
        (``touched``) no longer sits at the measured geometry, so its
        measurement cannot validate the model and is not compared.
        """
        for opening, measured in pairs:
            model = pads[opening][0].distance(copper)
            if measured is None:
                if model <= 1e-9:
                    return "conductor_overlaps_opening", ""
                continue
            if touched:
                continue
            if abs(model - measured) > _MODEL_AGREEMENT_MM:
                return (
                    "opening_model_mismatch",
                    f"modelled {model:.4f}mm vs measured {measured:.4f}mm at {pads[opening][4]}",
                )
        return None

    def _copper_floor_reason(
        self,
        moves: list[tuple[BaseGeometry, BaseGeometry, str]],
        layer: str,
        net_name: str,
        skip_uuid: str,
        pads: dict,
        clearance_mm: float | None,
    ) -> tuple[str, str] | None:
        """Decline a move that crowds other-net copper below the floor."""
        required = clearance_mm if clearance_mm is not None else 0.0
        for obstacle in self._other_net_obstacles(layer, net_name, skip_uuid, pads):
            for old_shape, new_shape, _layer in moves:
                new_d = new_shape.distance(obstacle)
                if new_d < required - 1e-6 and new_d < old_shape.distance(obstacle) - 1e-6:
                    return (
                        "would_violate_clearance",
                        f"would clear other-net copper by only {new_d:.3f}mm on {layer}",
                    )
        return None

    def _other_opening_reason(
        self,
        openings: list[str],
        moves: list[tuple[BaseGeometry, BaseGeometry, str]],
        moved_points: list[tuple[tuple[float, float], tuple[float, float], frozenset[str]]],
        pads: dict,
        required: float,
        displacement: float,
    ) -> tuple[str, str] | None:
        """Refuse a nudge that would create a mask finding at another opening.

        Two guards (issue #9896): the moved copper must not drop below the
        error threshold at a pad opening it did not already sit that close to
        (``would_approach_other_opening``), and a moved point that terminates
        inside another pad's opening must stay inside it -- pulling an
        own-pad entry out of its opening merely re-creates the finding as a
        pass-through (``would_leave_own_pad``).
        """
        threshold = required + _MASK_UNCERTAINTY_MM
        reach = required + displacement + 0.3
        worst: dict[str, tuple[float, float]] = {}
        for old_shape, new_shape, item_layer in moves:
            for pad_uuid, entry in pads.items():
                poly, _centre, pad_layers, _net, _ref, _shape = entry
                if pad_uuid in openings:
                    continue
                if item_layer not in pad_layers and "*.Cu" not in pad_layers:
                    continue
                post = new_shape.distance(poly)
                if post > reach:
                    continue
                pre = old_shape.distance(poly)
                previous = worst.get(pad_uuid)
                worst[pad_uuid] = (
                    pre if previous is None else min(previous[0], pre),
                    post if previous is None else min(previous[1], post),
                )
        for pad_uuid, (pre, post) in worst.items():
            if post < threshold - 1e-9 and post < pre - 1e-9:
                return (
                    "would_approach_other_opening",
                    f"would clear opening {pads[pad_uuid][4]} by only "
                    f"{post:.3f}mm (was {pre:.3f}mm)",
                )
        for old_xy, new_xy, point_layers in moved_points:
            for pad_uuid, entry in pads.items():
                poly, _centre, pad_layers, _net, pad_ref, _shape = entry
                if pad_uuid in openings:
                    continue
                compatible = bool(point_layers & set(pad_layers)) or "*.Cu" in pad_layers
                if not compatible:
                    continue
                grown = poly.buffer(_INSIDE_EPS_MM)
                if grown.contains(Point(old_xy)) and not grown.contains(Point(new_xy)):
                    return "would_leave_own_pad", f"moved end would leave pad {pad_ref}"
        return None

    def _via_by_uuid(self, uuid: str) -> SExp | None:
        if not uuid:
            return None
        for via_node in self.doc.find_all("via"):
            if self._via_uuid(via_node) == uuid:
                return via_node
        return None

    # The shared clearance engine fail-closes on foreign pads whose drill it
    # cannot model (slots, offsets, custom shapes) with **no distance test**
    # (``relocate_in_pad_vias._check_hole_to_copper``), so one slotted USB
    # shield pad blocks every via move on the board.  These verdicts are only
    # about the named pad's unknown geometry, so a move far from that pad
    # honors them not at all; the engine's distance-measured verdicts are
    # always honored.
    _FAIL_CLOSED_VERDICTS = ("unmodeled drill", "unproven for custom pad")
    _PAD_IN_VERDICT = re.compile(r"pad ([^ ]+)-([^ ]+)$")

    def _fail_closed_reason_is_local(
        self,
        reason: str,
        new_at: tuple[float, float],
        via_size: float,
        pads: dict,
    ) -> bool:
        """Whether an engine refusal must keep blocking this via move.

        ``True`` for every measured verdict; a fail-closed coverage verdict
        blocks only while the named pad is within the destination's reach
        plus the pad's own bounding-box diagonal (a generous stand-in for any
        slot the pad may carry) plus 1 mm of slack.
        """
        if not any(marker in reason for marker in self._FAIL_CLOSED_VERDICTS):
            return True
        match = self._PAD_IN_VERDICT.search(reason)
        if match is None:
            return True
        ref = f"{match.group(1)}/{match.group(2)}"
        entries = [e for e in pads.values() if e[4] == ref]
        if not entries:
            return True
        point = Point(new_at)
        for entry in entries:
            min_x, min_y, max_x, max_y = entry[0].bounds
            reach = via_size / 2 + math.hypot(max_x - min_x, max_y - min_y) / 2 + 1.0
            if box(min_x - reach, min_y - reach, max_x + reach, max_y + reach).intersects(point):
                return True
        return False

    @staticmethod
    def _via_span_orders(via_node: SExp) -> tuple[tuple[int, str], tuple[int, str]] | None:
        """Physical stack-order bounds of a via's layer span."""
        from kicad_tools.validate.copper_refs import _clean, _layer_order

        layers_node = via_node.find("layers")
        names = [_bare(a) for a in layers_node.get_atoms()] if layers_node is not None else []
        orders = [_layer_order(_clean(name)) for name in names if name]
        if len(orders) < 2:
            return None
        return min(orders), max(orders)

    @staticmethod
    def _pad_layers_meet_span(
        pad_layers: list[str], span: tuple[tuple[int, str], tuple[int, str]]
    ) -> bool:
        """Whether any of the pad's copper layers lies inside a via span."""
        from kicad_tools.validate.copper_refs import _clean, _layer_order

        low, high = span
        for raw in pad_layers:
            name = _clean(raw)
            if name == "*.Cu":
                return True
            if name == "F&B.Cu":
                if low <= _layer_order("F.Cu") <= high or low <= _layer_order("B.Cu") <= high:
                    return True
                continue
            order = _layer_order(name)
            if order < (2_000_000, name) and low <= order <= high:
                return True
        return False

    @staticmethod
    def _via_span_layer_set(via_node: SExp) -> frozenset[str]:
        """Copper layer names named by a via's span (endpoints)."""
        layers_node = via_node.find("layers")
        names = [_bare(a) for a in layers_node.get_atoms()] if layers_node is not None else []
        return frozenset(names)

    # ------------------------------------------------------------------
    # Undo / save
    # ------------------------------------------------------------------

    def undo_action(  # type: ignore[override]  # unrelated action types share the base name
        self, action: MaskRepairAction | MaskNudgeAction
    ) -> bool:
        """Put a move (and anything that moved with it) back where it was.

        Returns ``False`` when a node is gone or no point sits at its
        post-move position (callers fall back to a snapshot restore).
        """
        tracks = self._segment_nodes() if action.bends else {}
        if not all(bend.intact(tracks) for bend in action.bends):
            return False
        if isinstance(action, MaskViaRelocateAction):
            restored = self._undo_via_relocate(action)
        elif isinstance(action, MaskNudgeAction):
            restored = self._undo_nudge(action)
        else:
            restored = self._undo_recentre(action)
        if restored:
            for bend in action.bends:
                bend.restore(self.doc, tracks)
        return restored

    def _undo_recentre(self, action: MaskRepairAction) -> bool:
        segments = self._segment_nodes()
        targets = [(action.uuid, action.end_tag or None), *((u, t) for u, t in action.joined)]
        restore: list[tuple[SExp, tuple[float, float]]] = []
        for uuid, tag in targets:
            node = segments.get(_bare(uuid))
            if node is None:
                return False
            tags = (tag,) if tag else ("start", "end")
            for candidate in tags:
                end_node = node.find(candidate)
                xy = self._xy(end_node)
                if (
                    end_node is not None
                    and xy is not None
                    and math.dist(xy, (action.new_x, action.new_y)) < _TOLERANCE_MM
                ):
                    point = action.joined_origins.get(
                        (uuid, candidate), (action.old_x, action.old_y)
                    )
                    restore.append((end_node, point))
                    break
            else:
                return False
        for end_node, (x, y) in restore:
            end_node.set_value(0, x)
            end_node.set_value(1, y)
        self.modified = True
        return True

    def _undo_nudge(self, action: MaskNudgeAction) -> bool:
        """Reverse one nudge: every recorded point returns to its own
        pre-move coordinates, unrounded, so sub-micron geometry survives.

        Returns ``False`` when a node is gone or a point no longer sits at
        its post-move position (callers fall back to a snapshot restore).
        """
        restore: list[tuple[SExp, float, float]] = []
        for kind, uuid, tag, old_x, old_y in action.points:
            node = self._segment_by_uuid(uuid) if kind == "segment" else self._via_by_uuid(uuid)
            end_node = None if node is None else node.find(tag) if tag else node.find("at")
            xy = self._xy(end_node)
            if (
                end_node is None
                or xy is None
                or math.dist(xy, (round(old_x + action.dx, 4), round(old_y + action.dy, 4)))
                > _TOLERANCE_MM
            ):
                return False
            restore.append((end_node, old_x, old_y))
        for end_node, old_x, old_y in restore:
            end_node.set_value(0, old_x)
            end_node.set_value(1, old_y)
        self.modified = True
        return True

    def moved_geometry(self, action: MaskRepairAction | MaskNudgeAction) -> BaseGeometry | None:
        """Copper of the moved conductor (and joined ends) as it sits in this board.

        Used to attribute a re-check finding to a move by proximity.  ``None``
        when a node is gone or malformed.
        """
        if isinstance(action, MaskNudgeAction):
            shapes: list[BaseGeometry] = []
            items = {(kind, uid) for kind, uid, *_ in action.points}
            items.update(("segment", bend.added_uuid) for bend in action.bends)
            for kind, uuid in items:
                node = self._segment_by_uuid(uuid) if kind == "segment" else self._via_by_uuid(uuid)
                if node is None:
                    return None
                if kind == "segment":
                    start, end = self._xy(node.find("start")), self._xy(node.find("end"))
                    width_node = node.find("width")
                    if start is None or end is None or width_node is None:
                        return None
                    shapes.append(
                        LineString([start, end]).buffer(_num(width_node) / 2, quad_segs=16)
                    )
                else:
                    at = self._xy(node.find("at"))
                    size_node = node.find("size")
                    if at is None or size_node is None:
                        return None
                    shapes.append(Point(at).buffer(_num(size_node) / 2, quad_segs=32))
            if not shapes:
                return None
            return shapes[0] if len(shapes) == 1 else unary_union(shapes)
        segments = self._segment_nodes()
        recentre_shapes: list[BaseGeometry] = []
        for uuid in (
            action.uuid,
            *(u for u, _ in action.joined),
            *(b.added_uuid for b in action.bends),
        ):
            node = segments.get(_bare(uuid))
            if node is None:
                return None
            start, end = self._xy(node.find("start")), self._xy(node.find("end"))
            width_node = node.find("width")
            if start is None or end is None or width_node is None:
                return None
            recentre_shapes.append(
                LineString([start, end]).buffer(_num(width_node) / 2, quad_segs=16)
            )
        return recentre_shapes[0] if len(recentre_shapes) == 1 else unary_union(recentre_shapes)

    def save(self, output_path: str | Path | None = None) -> None:
        from ..core.sexp_file import save_pcb

        save_pcb(self.doc, Path(output_path) if output_path else self.path)
