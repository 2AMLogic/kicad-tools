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
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from shapely.affinity import rotate, translate  # type: ignore[import-untyped]
from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import nearest_points, unary_union  # type: ignore[import-untyped]

from ..router.quantize import is_45_aligned, off_angle_degrees
from ..schema.pcb import Arc
from ..sexp import SExp
from .mask_trace_angles import MaskTraceAngleError, MaskTraceBend, off_angle_tracks
from .repair_copper_gap import CopperGapRepairer
from .violation import DRCViolation, ViolationType

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules
    from kicad_tools.schema.pcb import PCB, Pad
    from kicad_tools.validate.mask_geometry import MaskOpening

#: Message marker the mask rule writes for a same-net segment that enters its
#: own pad (``validate/mask_copper.py``).
OWN_PAD_ENTRY_MARKER = "same-net own-pad entry"

#: Message markers a disjoint same-net pass-through finding carries: the net
#: relation plus the disjoint measurement relation.
PASS_THROUGH_MARKER = "same-net pass-through"
DISJOINT_RELATION_MARKER = "Mask-to-copper disjoint"
_OVERLAP_RELATION_MARKER = "Mask-to-copper overlap"
_ViaLead = tuple[float, float, float, float, float, str, int | str]

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
# Verdict of ``_bridge_aligned_pads`` for a topology it does not apply to; the
# finding then keeps its ordinary ``not_own_pad_entry`` decline.
_NOT_A_BRIDGE = "not_a_bridge"
# Perpendicular offset (mm) within which a second pad's centre is treated as a
# near-aligned bridge candidate; ``is_45_aligned`` then gates the pad-to-pad
# angle before any copper is moved (issue #10601).
_BRIDGE_AXIS_TOLERANCE_MM = 0.001
# Step of the slide that carries an escape stopped on its pad's edge on into the
# pad (issue #9939).
_EXTEND_STEP_MM = 0.005
# Longest slide tried for a *disjoint* pass-through jog or chamfer (mm). Such a
# track only needs to gain the last few hundredths of a millimetre of mask gap,
# so a slide that has to travel further is crowding another net; the smaller
# perpendicular nudge stays the repair for it (Board 03 J3/4: a 0.58 mm
# chamfer slide was vetoed by native DRC where the 0.08 mm nudge passes).
_PASS_THROUGH_SLIDE_MAX_MM = 0.25
# The four connection line directions through the new via centre (each used
# in both orientations): 0, 45, 90 and 135 degrees.
_ALIGNED_TAIL_DIRS = ((1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, -1.0))


@dataclass
class MaskRepairAction:
    """One applied (or, in a dry run, predicted) segment-end move."""

    action: str  # "recentre", "pull_in" (issue #9891) or "extend_entry" (issue #9939)
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
    via. Every moved point shares the same ``(dx, dy)`` unless ``shifts`` gives
    each point its own displacement. The grouped edit
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
    #: Optional ``(dx, dy)`` per entry of ``points`` for an edit whose ends move
    #: along different lines (a chamfer between perpendicular runs); empty
    #: means every point shares ``(dx, dy)``.
    shifts: list[tuple[float, float]] = field(default_factory=list)
    # Required plotted separation between the moved conductor and the pad opening.
    minimum_mask_gap_mm: float | None = field(default=None, kw_only=True)

    def shift(self, index: int) -> tuple[float, float]:
        """The displacement applied to ``points[index]``."""
        return self.shifts[index] if index < len(self.shifts) else (self.dx, self.dy)

    def __str__(self) -> str:
        return (
            f"nudge_clear: {self.kind} [{self.net_name}] ({self.old_x:.4f}, {self.old_y:.4f}) -> "
            f"({self.new_x:.4f}, {self.new_y:.4f}) clear of {self.pad_ref} - {self.detail}"
        )


@dataclass
class MaskViaRelocateAction(MaskNudgeAction):
    """Relocate a via and keep its connections in one undoable edit.

    A track end can slide along its original line and join the new centre
    through an aligned segment inside the via land. A pad whose copper touched
    the original annulus can instead reach the new centre through an aligned
    segment starting inside the pad. ``points`` names every moved or added item
    for finding attribution, and undo removes all added copper.
    """

    #: ``(segment uuid, end tag, old_x, old_y, new_x, new_y)`` -- one entry
    #: per anchored end that slid along its own line to its connection point.
    end_moves: list[tuple[str, str, float, float, float, float]] = field(default_factory=list)
    #: ``(uuid, start_x, start_y, end_x, end_y, width, layer, net)`` for each
    #: aligned connection the action adds inside the new via land or to the pad.
    added_segments: list[tuple[str, float, float, float, float, float, str, int | str]] = field(
        default_factory=list
    )
    #: Foreign nets whose tracks were slid aside as part of this transaction, so
    #: connectivity and creepage checks cover every edited net.
    extra_nets: set[str] = field(default_factory=set)


@dataclass
class MaskBridgeAction(MaskNudgeAction):
    """Replace an offset same-net run with a pad-centre-to-pad-centre bridge.

    The run that grazes one pad's opening slides onto the line through two
    aligned same-net pad centres and the join on its far side slides along its
    own direction to meet that line.  The short lead into the second pad is no
    longer needed there (the run now reaches that pad's centre itself), so it
    is re-used as the entry from the join to the first pad's centre: each part
    of the former run then terminates inside a pad, and no segment is added or
    removed, so every conductor keeps its identity through the edit.
    ``points`` names every moved segment for finding attribution; undo
    restores each recorded end.
    """

    #: ``(segment uuid, end tag, old_x, old_y, new_x, new_y)`` per moved end.
    end_moves: list[tuple[str, str, float, float, float, float]] = field(default_factory=list)


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


@dataclass(frozen=True)
class _SwappedElbow:
    """A two-leg elbow after its legs traded places, before anything moves."""

    uuid: str  # the reported conductor
    tag: str  # its end at the elbow
    elbow: tuple[float, float]  # that end as authored
    anchor: tuple[float, float]  # its other end, which stays
    width: float
    other_uuid: str
    other_tag: str
    other_elbow: tuple[float, float]  # as authored; may sit a hair off ``elbow``
    other_width: float
    far: tuple[float, float]  # the other leg's far end, which stays
    corner: tuple[float, float]  # where the swap puts the reported end
    other_corner: tuple[float, float]  # and the other, keeping their offset


@dataclass(frozen=True)
class _ForeignJog:
    """A foreign-net straight jog that can slide along parallel continuing runs."""

    net: str
    layer: str
    axis: tuple[float, float]
    low: float
    high: float
    copies: tuple[tuple[str, tuple[float, float], tuple[float, float], float], ...]
    runs: tuple[tuple[str, str, tuple[float, float], tuple[float, float], float], ...]

    @property
    def moved_uuids(self) -> frozenset[str]:
        return frozenset([*(u for u, *_ in self.copies), *(u for u, *_ in self.runs)])


def _num(node: SExp | None) -> float:
    """First atom of ``node`` as a number (0.0 when absent)."""
    atom = node.get_first_atom() if node is not None else None
    return float(atom) if atom is not None else 0.0


def _bare(value: object) -> str:
    return str(value).strip().strip('"')


def _on_45_grid(ux: float, uy: float) -> bool:
    """Whether a unit direction is a multiple of 45 degrees."""
    ax, ay = abs(ux), abs(uy)
    return ax < 1e-4 or ay < 1e-4 or abs(ax - ay) < 1e-4


def _coord_displacement_within(
    a: tuple[float, float], b: tuple[float, float], limit: float
) -> tuple[bool, float]:
    """Compare and measure board-coordinate Euclidean distance without binary64 drift."""
    if limit < 0 or math.isnan(limit):
        return False, math.dist(a, b)
    with localcontext() as ctx:
        ctx.prec = 50
        dx = Decimal(str(b[0])) - Decimal(str(a[0]))
        dy = Decimal(str(b[1])) - Decimal(str(a[1]))
        dist_sq = dx * dx + dy * dy
        dist = float(dist_sq.sqrt())
        if math.isinf(limit):
            return True, dist
        lim = Decimal(str(limit))
        return dist_sq <= lim * lim, dist


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

    def __init__(
        self,
        pcb_path: str | Path,
        *,
        project_pcb_path: str | Path | None = None,
        will_refill: bool = False,
    ):
        # ``DrillClearanceRepairer.__init__`` parses the file and builds the
        # same ``nets`` / ``net_names`` indexes this class historically did.
        super().__init__(pcb_path)
        self._project_pcb_path = Path(project_pcb_path) if project_pcb_path else self.path
        # The staged CLI pass regenerates the old via antipad before checking a pad bridge.
        self._will_refill = will_refill
        self._pivot_pad_bounds: dict[str, BaseGeometry | None] = {}

    @cached_property
    def _board_rules(self) -> BoardClearanceRules:
        from kicad_tools.router.board_clearance_rules import BoardClearanceRules

        return BoardClearanceRules.from_board(self._project_pcb_path)

    @cached_property
    def _pad_mask_openings(self) -> dict[tuple[str, str], MaskOpening]:
        """Standard pad mask outlines in the same file coordinates as the track ends."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.mask_geometry import (
            ADVANCED_PAD_FEATURES,
            MaskOpening,
            board_mask_margin,
            pad_mask_margin,
            pad_mask_shapes,
            pad_mask_sides,
        )

        pcb = PCB(self.doc)
        board_margin, declared = board_mask_margin(pcb)
        apertures: dict[tuple[str, str], MaskOpening] = {}
        if board_margin is None:
            return apertures
        version = pcb._file_version()
        for fp in pcb.footprints:
            for index, pad in enumerate(fp.pads):
                sides = pad_mask_sides(pad)
                node = pad._sexp_node
                if not sides or (
                    node is not None
                    and any(node.find(name) is not None for name in ADVANCED_PAD_FEATURES)
                ):
                    continue
                margin, source = pad_mask_margin(
                    pad, fp, board_margin, declared, board_version=version
                )
                if margin is None or not math.isfinite(margin):
                    continue
                try:
                    copper, aperture = pad_mask_shapes(pad, fp, margin)
                except ValueError:
                    continue
                if not aperture.is_empty:
                    copper = translate(copper, *pcb._board_origin)
                    aperture = translate(aperture, *pcb._board_origin)
                    identity = _bare(pad.uuid) if pad.uuid else f"{fp.reference}:pad:{index}"
                    for side in sides:
                        apertures[identity, f"{side}.Cu"] = MaskOpening(
                            identity,
                            f"{side}.Mask",
                            "pad",
                            margin,
                            source,
                            margin < 0,
                            aperture,
                            copper,
                        )
        return apertures

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

    def pour_nets(self) -> set[str]:
        """Return named nets whose copper can change when KiCad refills the board."""
        return {
            name
            for zone in self.doc.find_all("zone")
            if zone.find("keepout") is None and (name := self._net_name(zone))
        }

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

    def disconnected_copper_nets(self, before: MaskCopperRepairer, names: set[str]) -> set[str]:
        """Check all physical copper groups, including taps without a pad on either side.

        Matches authored copper items (pads, vias, segments and arcs) by UUID
        when present so a KiCad zone-refill save that reorders segments/vias
        and regenerates filled polygons does not trigger false disconnections.
        Items without a baseline UUID must have a unique geometry match.
        """
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.connectivity import ConnectivityValidator
        from kicad_tools.validate.copper_contact import PAD, TRACK, VIA, CopperItem

        if not names:
            return set()
        old_board, new_board = PCB(before.doc), PCB(self.doc)
        old, new = ConnectivityValidator(old_board), ConnectivityValidator(new_board)

        def identities(board: PCB, item: CopperItem) -> tuple[str, tuple] | None:
            if item.kind == PAD:
                fi, pi = item.key
                fp = board.footprints[fi]
                pad = fp.pads[pi]
                return _bare(pad.uuid), (
                    PAD,
                    fp.reference,
                    pad.number,
                    item.center,
                    item.layers,
                    pad.shape,
                    pad.size,
                    pad.rotation,
                )
            if item.kind == VIA:
                via = board.vias[item.key]
                return _bare(via.uuid), (VIA, via.position, via.size, via.drill, item.layers)
            if item.kind == TRACK:
                kind, idx = item.key
                track = board.segments[idx] if kind == "segment" else board.arcs[idx]
                return _bare(track.uuid), (
                    kind,
                    track.start,
                    track.end,
                    track.width,
                    track.layer,
                    track.mid if isinstance(track, Arc) else None,
                )
            return None  # Fill polygons participate in contact, but are regenerated.

        disconnected = set()
        for name in names:
            old_net, new_net = old_board.get_net_by_name(name), new_board.get_net_by_name(name)
            if old_net is None or new_net is None:
                disconnected.add(name)
                continue
            old_graph = old._contact_graph(old_net.number)
            new_graph = new._contact_graph(new_net.number)
            baseline = [
                (identity, group)
                for item, group in zip(old_graph.items, old_graph.component, strict=True)
                if (identity := identities(old_board, item)) is not None
            ]
            known_ids = {uid for (uid, _), _group in baseline if uid}
            by_uuid: dict[tuple, list[int]] = {}
            by_geometry: dict[tuple, list[int]] = {}
            for item, group in zip(new_graph.items, new_graph.component, strict=True):
                identity = identities(new_board, item)
                if identity is None:
                    continue
                uid, geometry = identity
                if uid in known_ids:
                    by_uuid.setdefault((geometry[0], uid), []).append(group)
                else:
                    by_geometry.setdefault(geometry, []).append(group)

            destinations: dict[int, int] = {}
            for (uid, geometry), old_group in baseline:
                if uid:
                    matches = by_uuid.pop((geometry[0], uid), [])
                else:
                    # A native save can assign the first UUID. Require one
                    # unclaimed match, independent of the saved item order.
                    matches = by_geometry.pop(geometry, [])
                new_group = matches[0] if len(matches) == 1 else None
                if new_group is None or destinations.setdefault(old_group, new_group) != new_group:
                    disconnected.add(name)
                    break
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

    def _other_net_obstacles_with_meta(
        self,
        layer: str,
        net_name: str,
        skip_uuid: str,
        pads: dict,
        *,
        skip_uuids: set[str] | frozenset[str] = frozenset(),
    ) -> list[tuple[BaseGeometry, str, str]]:
        obstacles: list[tuple[BaseGeometry, str, str]] = []
        for uuid, node in self._segment_nodes().items():
            obs_net = self._net_name(node)
            if uuid == skip_uuid or uuid in skip_uuids or obs_net == net_name:
                continue
            layer_node = node.find("layer")
            if layer_node is None or _bare(layer_node.get_first_atom()) != layer:
                continue
            start, end = self._xy(node.find("start")), self._xy(node.find("end"))
            width_node = node.find("width")
            if start is None or end is None or width_node is None:
                continue
            width = _num(width_node)
            obstacles.append(
                (LineString([start, end]).buffer(width / 2, quad_segs=16), "Track", obs_net)
            )
        for via in self.doc.find_all("via"):
            obs_net = self._net_name(via)
            if obs_net == net_name or self._via_uuid(via) in skip_uuids:
                continue
            at = self._xy(via.find("at"))
            size_node = via.find("size")
            if at is None or size_node is None:
                continue
            obstacles.append((Point(at).buffer(_num(size_node) / 2, quad_segs=16), "Via", obs_net))
        for poly, _, layers, pad_net, _, _ in pads.values():
            if pad_net == net_name and net_name:
                continue
            if (
                layer in layers
                or "*.Cu" in layers
                or ("F&B.Cu" in layers and layer in {"F.Cu", "B.Cu"})
            ):
                obstacles.append((poly, "Pad", pad_net))
        return obstacles

    def _other_net_obstacles(
        self, layer: str, net_name: str, skip_uuid: str, pads: dict
    ) -> list[BaseGeometry]:
        return [
            geom
            for geom, _, _ in self._other_net_obstacles_with_meta(layer, net_name, skip_uuid, pads)
        ]

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
            warning_clearance_mm: Preferred mask gap for a shared pad exit or a
                via with an already-reported approach, when the manufacturer's
                policy includes an advisory tier.
        """
        if not math.isfinite(margin_mm) or margin_mm < 0:
            raise ValueError("Mask clearance margin must be a finite, nonnegative distance")
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
        reported_tracks: set[str] = set()
        reported_vias: set[str] = {
            uuids[-1]
            for v in findings
            if len(uuids := _item_uuids(v)) >= 2
            and uuids[-1] not in segments
            and self._via_by_uuid(uuids[-1]) is not None
        }

        for violation in findings:
            uuids = _item_uuids(violation)
            if len(uuids) < 2:
                result.skip("no_item_uuids", violation)
                continue
            conductor = uuids[-1]
            openings = uuids[:-1]
            if conductor in segments:
                reported_tracks.add(conductor)
            if PASS_THROUGH_MARKER in violation.message and (
                DISJOINT_RELATION_MARKER in violation.message
                or (
                    conductor not in segments
                    and _OVERLAP_RELATION_MARKER in violation.message
                    and self._via_by_uuid(conductor) is not None
                )
            ):
                # One grouped edit per conductor clears every opening it has
                # findings against (issue #9896); collected and repaired below.
                pass_throughs.setdefault(conductor, []).append(violation)
                continue
            if OWN_PAD_ENTRY_MARKER not in violation.message:
                # An overlapping pass-through track that hugs the pad edge and
                # turns in at its corner may take the mirror elbow (#9939).
                flip_seg = segments.get(conductor)
                if (
                    PASS_THROUGH_MARKER in violation.message
                    and flip_seg is not None
                    and len(openings) == 1
                    and openings[0] in pads
                    and conductor not in moved
                    and conductor not in touched_by_action
                    and self._flip_elbow_into_pad(
                        flip_seg,
                        conductor,
                        openings[0],
                        pads,
                        result,
                        max_displacement,
                        dry_run,
                        clearance_mm,
                        violation.required_value_mm,
                        margin_mm,
                        touched_by_action,
                        reported_vias,
                        warning_clearance_mm,
                    )
                ):
                    moved.add(conductor)
                    action = result.actions[-1]
                    if isinstance(action, MaskNudgeAction):
                        touched_by_action.update(point[1] for point in action.points)
                    continue
                # An overlapping run beside a pad whose neighbour on the same
                # axis is another pad of its net becomes a straight bridge
                # between the two pad centres (#9939, Board 03 J1/A4).
                bridge_seg = segments.get(conductor)
                if (
                    PASS_THROUGH_MARKER in violation.message
                    and _OVERLAP_RELATION_MARKER in violation.message
                    and bridge_seg is not None
                    and len(openings) == 1
                    and openings[0] in pads
                    and conductor not in moved
                    and conductor not in touched_by_action
                ):
                    bridge_refusal = self._bridge_aligned_pads(
                        bridge_seg,
                        conductor,
                        openings[0],
                        pads,
                        result,
                        max_displacement,
                        dry_run,
                        clearance_mm,
                        violation.required_value_mm,
                        touched_by_action,
                    )
                    if bridge_refusal is None:
                        moved.add(conductor)
                        action = result.actions[-1]
                        if isinstance(action, MaskBridgeAction):
                            touched_by_action.update(point[1] for point in action.points)
                        continue
                    if bridge_refusal[0] != _NOT_A_BRIDGE:
                        result.skip(bridge_refusal[0], violation, conductor, bridge_refusal[1])
                        continue
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
            if self._slide_chamfer_between_runs(
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
                # A chamfer between two perpendicular runs slides out of the
                # opening keeping every angle; recentring it instead skews the
                # chamfer into the pad (#9939).
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
            if reason in ("would_violate_clearance", "exceeds_max_displacement"):
                # Issue #9939: an escape that stops on its pin's edge cannot be
                # recentred without crowding the neighbouring pin (or without
                # moving farther than allowed).  Carry it on
                # along its own line until its whole width ends on the pad.
                reason = self._extend_entry_one(
                    seg,
                    conductor,
                    openings[0],
                    pads,
                    result,
                    max_displacement,
                    dry_run,
                    clearance_mm,
                    violation.required_value_mm,
                    reason,
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
                reported_tracks,
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

    def _extend_entry_one(
        self,
        seg: SExp,
        seg_uuid: str,
        pad_uuid: str,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        required_gap_mm: float | None,
        declined: str,
    ) -> str | None:
        """Carry an escape that stops on its pad's edge on along its own line.

        The recentre aims such an end at the pad centre, a line that can crowd
        the neighbouring pin (issue #9939, Board 03 U1/17).  Here the end keeps
        its direction and slides on, away from the far end, by the shortest
        multiple of ``_EXTEND_STEP_MM`` (never more than the track width) that leaves the whole track width ending
        on the pad and the mask rule's own verdict on the track a clean entry.
        Nothing else moves: the old end lies on the extended copper, so every
        track or via that met it still touches.  Returns ``None`` on success,
        else the junction veto or the original refusal reason.
        """
        from kicad_tools.router.quantize import is_45_aligned
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.connectivity import ConnectivityValidator

        from ..validate.mask_copper import clean_own_pad_entry

        poly, _centre, pad_layers, pad_net, pad_ref, shape = pads[pad_uuid]
        net_name = self._net_name(seg)
        layer_node = seg.find("layer")
        layer = _bare(layer_node.get_first_atom()) if layer_node else ""
        aperture = self._pad_mask_openings.get((pad_uuid, layer))
        if (
            shape == "unsupported"
            or aperture is None
            or required_gap_mm is None
            or not net_name
            or net_name != pad_net
            or self._locks.is_locked(seg)
            or self._locks.is_generator_owned(seg)
            or self._board_rules.dru_error
        ):
            return declined
        start_node, end_node = seg.find("start"), seg.find("end")
        start, end = self._xy(start_node), self._xy(end_node)
        width = _num(seg.find("width"))
        if start is None or end is None or width <= 0:
            return declined
        grown = poly.buffer(_INSIDE_EPS_MM)
        start_in, end_in = grown.contains(Point(start)), grown.contains(Point(end))
        if start_in == end_in:
            return declined
        inside, outside = (start, end) if start_in else (end, start)
        inside_node = start_node if start_in else end_node
        # Only an end that stops on the pad's edge qualifies: the mask rule
        # counts an end strictly inside the pad as an entry, so sliding one
        # that is already there cannot clear the finding.
        if poly.buffer(-_MASK_UNCERTAINTY_MM).contains(Point(inside)):
            return declined
        _, blocked = self._junction(layer, inside, seg_uuid, net_name)
        if blocked:
            return "junction_at_moved_end"
        run = math.dist(inside, outside)
        if run < 1e-9:
            return declined
        unit = ((inside[0] - outside[0]) / run, (inside[1] - outside[1]) / run)
        aligned = is_45_aligned(inside[0] - outside[0], inside[1] - outside[1])

        required = clearance_mm if clearance_mm is not None else 0.0
        old_shape = LineString([outside, inside]).buffer(width / 2, quad_segs=16)
        new = None
        new_shape = old_shape
        moved_dist = 0.0
        # An end that merely stops on the edge needs at most about one track
        # width (0.7 of it at 45 degrees) to put its whole cap on the pad; a
        # longer slide is a different repair (a chamfer, a lead) not this one.
        limit = min(max_displacement, width)
        if not (limit > 0):
            return declined
        steps = int(limit / _EXTEND_STEP_MM + 1e-9)
        for index in range(1, steps + 1):
            step = index * _EXTEND_STEP_MM
            # Quantize the extension relative to the endpoint, preserving
            # its original offset at KiCad's nanometre coordinate precision.
            candidate = (
                round(inside[0] + round(unit[0] * step, 4), 6),
                round(inside[1] + round(unit[1] * step, 4), 6),
            )
            within_limit, candidate_dist = _coord_displacement_within(inside, candidate, limit)
            if not within_limit:
                break
            if not grown.contains(Point(candidate)):
                break
            copper = LineString([outside, candidate]).buffer(width / 2, quad_segs=16)
            if not poly.covers(Point(candidate).buffer(width / 2, quad_segs=16)):
                continue
            if aligned and not is_45_aligned(candidate[0] - outside[0], candidate[1] - outside[1]):
                continue
            if clean_own_pad_entry(
                aperture.geometry,
                copper,
                poly,
                f"pad:{shape}",
                width,
                (outside, candidate),
                clearance_mm=required_gap_mm,
                uncertainty_mm=_MASK_UNCERTAINTY_MM,
                margin_mm=aperture.margin_mm,
            ):
                new, new_shape, moved_dist = candidate, copper, candidate_dist
                break
        if new is None:
            return declined

        # The added copper must keep the clearance the track already had.
        for obstacle in self._other_net_obstacles(layer, net_name, seg_uuid, pads):
            new_d = new_shape.distance(obstacle)
            if new_d < required - 1e-6 and new_d < old_shape.distance(obstacle) - 1e-6:
                return declined
        pcb = PCB(self.doc)
        if self._pull_in_crowds_pad(pcb, layer, net_name, pads, [(old_shape, new_shape)]):
            return declined
        net = pcb.get_net_by_name(net_name)
        if net is None or inside_node is None:
            return declined

        action = MaskRepairAction(
            action="extend_entry",
            uuid=seg_uuid,
            pad_uuid=pad_uuid,
            pad_ref=pad_ref,
            net_name=net_name,
            layer=layer,
            old_x=inside[0],
            old_y=inside[1],
            new_x=new[0],
            new_y=new[1],
            displacement_mm=moved_dist,
            detail=(
                f"carried track end {moved_dist:.4f}mm on along its own "
                "line until its width ends on the pad"
            ),
            end_tag="start" if inside_node is start_node else "end",
        )
        previous = ConnectivityValidator(pcb)._contact_graph(net.number)
        original = list(inside_node.children)
        keep = False
        try:
            inside_node.set_value(0, new[0])
            inside_node.set_value(1, new[1])
            current = ConnectivityValidator(PCB(self.doc))._contact_graph(net.number)
            if not current.preserves_connections(previous):
                return declined
            keep = not dry_run
        finally:
            if not keep:
                inside_node.children[:] = original
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
        reported_tracks: set[str],
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
        if seg is not None and len(openings) == 1:
            # A track that is a jog (or a chamfer) between two runs and only
            # grazes the opening beside the pad is moved the way an own-pad
            # entry's jog is: the junctions follow the runs, so no angle
            # changes. Perpendicular recentring would skew the jog and the
            # verified command then rolls it back for the finding it creates
            # (issue #9939, Board 03 C7/1).
            for slide in (self._slide_jog_along_runs, self._slide_chamfer_between_runs):
                if slide(
                    seg,
                    conductor,
                    openings[0],
                    pads,
                    result,
                    min(max_displacement, _PASS_THROUGH_SLIDE_MAX_MM),
                    dry_run,
                    clearance_mm,
                    required,
                    margin_mm,
                    warning_clearance_mm,
                    touched_by_action,
                ):
                    return None
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
                if self._balance_pad_bend(
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
                    touched_by_action,
                ):
                    return None
            return refusal
        via_node = self._via_by_uuid(conductor)
        if via_node is not None:
            via_at = self._xy(via_node.find("at"))
            if via_at is not None and any(
                uid in touched_by_action
                for uid, node in self._segment_nodes().items()
                if any(
                    (end := self._xy(node.find(tag))) is not None
                    and math.dist(end, via_at) < _TOLERANCE_MM
                    for tag in ("start", "end")
                )
            ):
                # An earlier action this run already moved a track that
                # reaches this via; a second edit on it cannot be attributed
                # to one repair and would roll both back. The next pass sees
                # the settled copper.
                return "segment_end_already_moved", ""
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
                reported_tracks,
                warning_clearance_mm,
                any(_OVERLAP_RELATION_MARKER in v.message for v in members),
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

        def try_push(dx: float, dy: float, floor: float | None) -> tuple[str, str] | None:
            displacement = math.hypot(dx, dy)
            if displacement < _TOLERANCE_MM / 2:
                return "already_cleared", ""
            if displacement > max_displacement:
                return "exceeds_max_displacement", f"needs {displacement:.4f}mm"
            moving_uuids = {seg_uuid, *(member[0] for member in joined)}

            # Check the same coordinates that _apply_nudge will write.
            def shifted(point: tuple[float, float]) -> tuple[float, float]:
                return round(point[0] + dx, 4), round(point[1] + dy, 4)

            new_start = start if start == fixed else shifted(start)
            new_end = end if end == fixed else shifted(end)
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
                        LineString([join_other, shifted(join_end)]).buffer(
                            join_width / 2, quad_segs=16
                        ),
                        layer,
                    )
                )
                points.append(("segment", join_uuid, join_tag, join_end[0], join_end[1]))

            if fixed is not None and self._loses_same_net_contact(
                moves, layer, net_name, moving_uuids, pads
            ):
                return "would_break_same_net_contact", ""
            refusal = self._copper_floor_reason(moves, layer, net_name, seg_uuid, pads, floor)
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

        # Keep the line entering the pad and take the shortest perpendicular
        # lead from the fixed via to its extension. The original pivot remains
        # available if this corner fails a clearance or contact check.
        if fixed is not None and len(joined) == 1 and len(openings) == 1:
            uid, neighbour, tag = joined[0]
            far = self._xy(neighbour.find("end" if tag == "start" else "start"))
            opening = entries[0][0]
            if (
                far is not None
                and self._xy(neighbour.find(tag)) == reference
                and opening.buffer(-_INSIDE_EPS_MM).contains(Point(far))
                and openings[0] not in self._pivot_pad_bounds
                and uid not in touched_by_action
                and not self._locks.is_generator_owned(neighbour)
                and not self._locks.is_generator_owned(seg)
                and not self._board_rules.dru_error
            ):
                shift = self._extend_pad_entry(
                    far, reference, fixed, opening, width / 2 + required + margin_mm
                )
                if shift is not None:
                    floor = max(
                        clearance_mm or 0.0,
                        self._board_rules.net_requirement(net_name, self.net_names),
                    )
                    if try_push(*shift, floor) is None:
                        return None
        if push is None:
            return "infeasible", ""
        return try_push(*push, clearance_mm)

    @staticmethod
    def _extend_pad_entry(
        pad_end: tuple[float, float],
        corner: tuple[float, float],
        via: tuple[float, float],
        opening: BaseGeometry,
        centreline_gap: float,
    ) -> tuple[float, float] | None:
        """Project the via onto the continuation of the entry beyond its corner."""
        from kicad_tools.router.quantize import is_45_aligned

        vx, vy = corner[0] - pad_end[0], corner[1] - pad_end[1]
        length = math.hypot(vx, vy)
        if length < _MIN_RUN_MM:
            return None
        scale = ((via[0] - corner[0]) * vx + (via[1] - corner[1]) * vy) / length**2
        if scale <= 0:
            return None
        new = round(corner[0] + scale * vx, 4), round(corner[1] + scale * vy, 4)
        if (
            math.dist(via, new) < _MIN_RUN_MM
            or LineString([via, new]).distance(opening) < centreline_gap
        ):
            return None
        if not all(is_45_aligned(new[0] - x, new[1] - y) for x, y in (pad_end, via)):
            return None
        return new[0] - corner[0], new[1] - corner[1]

    def _balance_pad_bend(
        self,
        seg: SExp,
        uid: str,
        opening: str,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        required: float,
        margin_mm: float,
        touched: set[str],
    ) -> bool:
        """Split a pad-to-pad bend's axial leg between its two fixed lands."""
        from kicad_tools.schema.pcb import PCB

        _, destination, _, net, ref, _ = pads[opening]
        if self._locks.is_generator_owned(seg) or self._board_rules.dru_error:
            return False
        layer_node = seg.find("layer")
        if layer_node is None:
            return False
        layer = _bare(layer_node.get_first_atom())
        width = _num(seg.find("width"))
        start, end = self._xy(seg.find("start")), self._xy(seg.find("end"))
        if start is None or end is None or width <= 0:
            return False
        # The finding is on the diagonal. The remaining axial track must end
        # at the centre of the opening it passes, regardless of track order.
        span_x, span_y = abs(start[0] - end[0]), abs(start[1] - end[1])
        if span_x < _MIN_RUN_MM or not math.isclose(span_x, span_y, abs_tol=1e-9, rel_tol=0):
            return False
        for fixed_tag, anchor, corner in (("start", start, end), ("end", end, start)):
            owners = [
                pad_id
                for pad_id, (_, centre, layers, name, _, _) in pads.items()
                if pad_id != opening
                and name == net
                and (layer in layers or "*.Cu" in layers)
                and math.dist(anchor, centre) < 1e-6
            ]
            if len(owners) != 1:
                continue
            members, blocked = self._junction(layer, corner, uid, net)
            if blocked or len(members) != 1:
                continue
            tail_id, tail, tail_tag = members[0]
            far = self._xy(tail.find("end" if tail_tag == "start" else "start"))
            if (
                tail_id in touched
                or self._locks.is_generator_owned(tail)
                or _num(tail.find("width")) != width
                or self._xy(tail.find(tail_tag)) != corner
                or far is None
                or math.dist(far, destination) >= 1e-6
            ):
                continue
            dx, dy = (far[0] - corner[0]) / 2, (far[1] - corner[1]) / 2
            distance = math.hypot(dx, dy)
            if (dx != 0 and dy != 0) or not _MIN_RUN_MM <= distance <= max_displacement:
                continue
            # Check the coordinates that _apply_nudge and the split will save.
            # Decline if their precision would skew or stretch the diagonal.
            inner = round(corner[0] + dx, 4), round(corner[1] + dy, 4)
            lead = round(anchor[0] + dx, 6), round(anchor[1] + dy, 6)
            if (
                math.dist(inner, (corner[0] + dx, corner[1] + dy)) > 1e-9
                or math.dist(lead, (anchor[0] + dx, anchor[1] + dy)) > 1e-9
            ):
                continue
            legs = [(anchor, lead), (lead, inner), (inner, far)]
            copper = [LineString(leg).buffer(width / 2, quad_segs=16) for leg in legs]
            if not self._balanced_bend_clears_mask(
                legs, copper, {opening, owners[0]}, layer, width, pads, required, margin_mm
            ):
                continue
            # Both pieces of the split diagonal inherit its original clearance.
            # The shorter tail can already lie closer to the same obstacle.
            diagonal = LineString([anchor, corner]).buffer(width / 2, quad_segs=16)
            tail_copper = LineString([corner, far]).buffer(width / 2, quad_segs=16)
            moves = [
                (diagonal, unary_union(copper[:2]), layer),
                (tail_copper, copper[2], layer),
            ]
            if self._loses_same_net_contact(
                moves, layer, net, {uid, tail_id}, pads, protect_ends=True
            ):
                continue
            floor = max(clearance_mm or 0, self._board_rules.net_requirement(net, self.net_names))
            if self._copper_floor_reason(
                moves, layer, net, uid, pads, floor
            ) or self._pull_in_crowds_pad(
                PCB(self.doc), layer, net, pads, [(old, new) for old, new, _ in moves]
            ):
                continue
            bend = MaskTraceBend(uid, str(uuid4()), fixed_tag, anchor, lead)
            action = MaskNudgeAction(
                action="nudge_clear",
                uuid=uid,
                kind="segment",
                pad_uuid=opening,
                pad_ref=ref,
                net_name=net,
                layer=layer,
                old_x=corner[0],
                old_y=corner[1],
                new_x=inner[0],
                new_y=inner[1],
                dx=dx,
                dy=dy,
                displacement_mm=distance,
                detail=f"distributed the straight leg between two pad centres at {ref}",
                openings=[opening],
                bends=[bend],
                points=[
                    ("segment", uid, bend.moved_tag, *corner),
                    ("segment", tail_id, tail_tag, *corner),
                ],
            )
            if self._apply_nudge(action, result, dry_run) is not None:
                return False
            if not dry_run:
                self._insert_segment_after_last(bend.split(self.doc, seg))
            return True
        return False

    def _balanced_bend_clears_mask(
        self,
        legs: list[tuple[tuple[float, float], tuple[float, float]]],
        copper: list[BaseGeometry],
        owners: set[str],
        layer: str,
        width: float,
        pads: dict,
        required: float,
        margin_mm: float,
    ) -> bool:
        """A nearby opening must either clear the leg or qualify as its own pad entry."""
        from kicad_tools.validate.mask_copper import _within_own_pad

        apertures = self._pad_mask_openings
        if any((uid, layer) not in apertures or uid in self._pivot_pad_bounds for uid in owners):
            return False
        for (uid, side), aperture in apertures.items():
            if side != layer:
                continue
            for ends, shape in zip(legs, copper, strict=True):
                if shape.distance(aperture.geometry) >= required + margin_mm:
                    continue
                if uid not in owners:
                    return False
                item = {"kind": "segment", "width_mm": width, "endpoints_mm": ends}
                qualifies, _, _ = _within_own_pad(
                    aperture.geometry,
                    shape,
                    aperture.copper,
                    {"kind": f"pad:{pads[uid][5]}"},
                    item,
                    aperture.margin_mm,
                    required,
                    _MASK_UNCERTAINTY_MM,
                )
                if not qualifies:
                    return False
        return True

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

    @staticmethod
    def _run_keeps_length(
        runs: list[tuple[str, SExp, str, tuple[float, float], tuple[float, float]]],
        axis: tuple[float, float],
        amount: float,
    ) -> bool:
        """Whether sliding every run's joint ``amount`` along ``axis`` keeps each
        run's direction and a minimum length."""
        for _uid, _node, _tag, point, other in runs:
            along = (point[0] - other[0]) * axis[0] + (point[1] - other[1]) * axis[1]
            if along > 0 and along + amount < _MIN_RUN_MM:
                return False
            if along <= 0 and along + amount > -_MIN_RUN_MM:
                return False
        return True

    def _slide_chamfer_between_runs(
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
        """Slide a chamfer between two perpendicular runs out of a pad opening.

        The conductor is a straight track (and any coincident copies) whose
        near end joins only same-net tracks along the pad lead's line and
        whose far end joins only same-net tracks along one other line. The
        near end slides outward along the lead, and the far end slides along
        its own run by the amount that keeps the chamfer parallel to itself:
        every angle is kept, the runs' far ends stay fixed, and the chamfer
        leaves the opening it grazed. The two ends move by different amounts,
        so the nudge action records one shift per point. An end at a via, an
        arc or a crossing track is never moved. Returns ``True`` when applied.
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
            (end, start) if pad.distance(Point(end)) < pad.distance(Point(start)) else (start, end)
        )

        copies: dict[str, SExp] = {seg_uuid: seg}
        side_runs: list[list[tuple[str, SExp, str, tuple[float, float], tuple[float, float]]]] = []
        for point, opposite in ((near, far), (far, near)):
            members, blocked = self._junction(layer, point, seg_uuid, net)
            if blocked or self._has_fixed_junction_contact(point, layer, net):
                return False
            runs = []
            for uid, node, tag in members:
                other = self._xy(node.find("end" if tag == "start" else "start"))
                if other is None or node.find("width") is None:
                    return False
                if math.dist(other, opposite) < _TOLERANCE_MM:
                    copies[uid] = node
                    continue
                runs.append((uid, node, tag, point, other))
            if not runs:
                return False
            side_runs.append(runs)
        if set(copies) & touched or any(
            self._locks.is_generator_owned(node) for node in copies.values()
        ):
            return False

        # Each side's runs share one axis, pointing from their far end to the joint.
        axes: list[tuple[float, float]] = []
        for runs in side_runs:
            _, _, _, point0, other0 = runs[0]
            length = math.dist(point0, other0)
            if length < _TOLERANCE_MM:
                return False
            ux, uy = (point0[0] - other0[0]) / length, (point0[1] - other0[1]) / length
            for uid, node, _tag, point, other in runs:
                vx, vy = point[0] - other[0], point[1] - other[1]
                if abs(vx * uy - vy * ux) > _RUN_AXIS_TOLERANCE_MM or self._locks.is_locked(node):
                    return False
                if self._locks.is_generator_owned(node) or uid in touched:
                    return False
            axes.append((ux, uy))
        (unx, uny), (ufx, ufy) = axes
        cx, cy = far[0] - near[0], far[1] - near[1]
        clen = math.hypot(cx, cy)
        det = cx * ufy - cy * ufx
        # The two runs and the chamfer must all point in different directions.
        if (
            abs(det) < 0.1 * clen
            or abs(unx * cy - uny * cx) < 0.1 * clen
            or abs(unx * ufy - uny * ufx) < 0.1
        ):
            return False

        rules = self._board_rules
        if rules.dru_error:
            return False
        copper_floor = max(clearance_mm or 0.0, rules.net_requirement(net, self.net_names))

        def moved(point: tuple[float, float], shift: tuple[float, float]) -> tuple[float, float]:
            return round(point[0] + shift[0], 4), round(point[1] + shift[1], 4)

        for gap in dict.fromkeys((max(warning_clearance_mm or 0.0, required), required)):
            for sign in (1.0, -1.0):
                found = None
                for step in range(1, int(max_displacement / _SLIDE_STEP_MM + 1e-9) + 1):
                    lead = sign * step * _SLIDE_STEP_MM
                    # The far end follows its own run so the chamfer stays parallel.
                    follow = lead * (cx * uny - cy * unx) / det
                    scale = 1.0 + lead * (ufx * uny - ufy * unx) / det
                    if max(abs(lead), abs(follow)) > max_displacement:
                        break
                    if scale * clen < _MIN_RUN_MM:
                        break
                    shifts = ((lead * unx, lead * uny), (follow * ufx, follow * ufy))
                    if not all(
                        self._run_keeps_length(runs, axis, amount)
                        for runs, axis, amount in zip(side_runs, axes, (lead, follow), strict=True)
                    ):
                        break
                    candidate = LineString([moved(near, shifts[0]), moved(far, shifts[1])]).buffer(
                        width / 2, quad_segs=16
                    )
                    if candidate.distance(pad) >= gap + margin_mm - 1e-9:
                        found = shifts
                        break
                if found is None:
                    continue
                near_shift, far_shift = found
                moves: list[tuple[BaseGeometry, BaseGeometry, str]] = []
                points: list[tuple[str, str, str | None, float, float]] = []
                shifts_by_point: list[tuple[float, float]] = []
                moved_points: list[
                    tuple[tuple[float, float], tuple[float, float], frozenset[str]]
                ] = []

                def shift_at(point: tuple[float, float]) -> tuple[float, float]:
                    return (
                        near_shift if math.dist(point, near) <= math.dist(point, far) else far_shift
                    )

                for uid, node in copies.items():
                    s, e = self._xy(node.find("start")), self._xy(node.find("end"))
                    w = _num(node.find("width")) or width
                    if s is None or e is None:
                        return False
                    new_s, new_e = moved(s, shift_at(s)), moved(e, shift_at(e))
                    moves.append(
                        (
                            LineString([s, e]).buffer(w / 2, quad_segs=16),
                            LineString([new_s, new_e]).buffer(w / 2, quad_segs=16),
                            layer,
                        )
                    )
                    for tag, old, new in (("start", s, new_s), ("end", e, new_e)):
                        points.append(("segment", uid, tag, *old))
                        shifts_by_point.append(shift_at(old))
                        moved_points.append((old, new, frozenset({layer})))
                for runs, shift in zip(side_runs, (near_shift, far_shift), strict=True):
                    for uid, node, tag, point, other in runs:
                        w = _num(node.find("width")) or width
                        new_point = moved(point, shift)
                        moves.append(
                            (
                                LineString([other, point]).buffer(w / 2, quad_segs=16),
                                LineString([other, new_point]).buffer(w / 2, quad_segs=16),
                                layer,
                            )
                        )
                        points.append(("segment", uid, tag, *point))
                        shifts_by_point.append(shift)
                        moved_points.append((point, new_point, frozenset({layer})))
                if not self._slide_keeps_connections(
                    points, near_shift[0], near_shift[1], net, shifts_by_point
                ):
                    continue
                if self._copper_floor_reason(moves, layer, net, seg_uuid, pads, copper_floor):
                    continue
                displacement = max(math.hypot(*near_shift), math.hypot(*far_shift))
                if self._other_opening_reason(
                    [opening], moves, moved_points, pads, required, displacement
                ):
                    continue
                new_near = moved(near, near_shift)
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
                    dx=near_shift[0],
                    dy=near_shift[1],
                    displacement_mm=displacement,
                    detail=(
                        f"slid a chamfer of {len(copies)} track(s) {displacement:.4f}mm "
                        f"along its perpendicular runs clear of {ref}"
                    ),
                    openings=[opening],
                    points=points,
                    shifts=shifts_by_point,
                )
                return self._apply_nudge(action, result, dry_run) is None
        return False

    def _bridge_aligned_pads(
        self,
        seg: SExp,
        seg_uuid: str,
        opening: str,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        required_mm: float | None,
        touched_by_action: set[str],
    ) -> tuple[str, str] | None:
        """Bridge two aligned same-net pad centres with a run that grazes one of them.

        Applies to an overlapping same-net pass-through finding on a straight
        run ``S`` (a multiple of 45 degrees) that sits beside pad ``A`` while
        its far end reaches pad ``B`` of the same net through one short lead
        ``N``, ``A`` and ``B`` having their centres on one line parallel to
        ``S`` (issue #9939, Board 03 J1/A4 and J1/B9).  ``S`` slides onto that
        line; the single same-net join ``M`` on its other end slides along its
        own direction to meet the line (angle kept); ``N`` is dropped because
        ``S`` now ends on ``B``'s centre; and ``S`` is split at ``A``'s centre
        so each part terminates inside a pad -- a run that merely passes
        through a pad stays a pass-through finding.

        Returns ``None`` when applied (or predicted, in a dry run),
        ``(_NOT_A_BRIDGE, "")`` for a topology this repair does not cover (the
        caller keeps the ordinary decline), or ``(reason, detail)`` for a
        candidate it built and refused.
        """
        not_a_bridge = (_NOT_A_BRIDGE, "")
        entry = pads.get(opening)
        if entry is None or entry[5] == "unsupported":
            return not_a_bridge
        pad_poly, centre_a, pad_layers, pad_net, pad_ref, _shape = entry
        net_name = self._net_name(seg)
        layer_node = seg.find("layer")
        layer = _bare(layer_node.get_first_atom()) if layer_node is not None else ""
        start, end = self._xy(seg.find("start")), self._xy(seg.find("end"))
        width_node = seg.find("width")
        if (
            not net_name
            or pad_net != net_name
            or not layer
            or start is None
            or end is None
            or width_node is None
            or (layer not in pad_layers and "*.Cu" not in pad_layers)
            or self._locks.is_locked(seg)
            or self._locks.is_generator_owned(seg)
        ):
            return not_a_bridge
        width = _num(width_node)
        length = math.dist(start, end)
        if length < _MIN_RUN_MM:
            return not_a_bridge
        ux, uy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
        if not _on_45_grid(ux, uy):
            return not_a_bridge
        nx, ny = -uy, ux
        offset = (centre_a[0] - start[0]) * nx + (centre_a[1] - start[1]) * ny
        if abs(offset) < _TOLERANCE_MM:
            return not_a_bridge

        for b_tag in ("end", "start"):
            d_tag = "start" if b_tag == "end" else "end"
            b_point = end if b_tag == "end" else start
            d_point = start if b_tag == "end" else end
            b_members, b_blocked = self._junction(layer, b_point, seg_uuid, net_name)
            d_members, d_blocked = self._junction(layer, d_point, seg_uuid, net_name)
            if b_blocked or d_blocked or len(b_members) != 1 or len(d_members) != 1:
                continue
            n_uuid, n_node, n_tag = b_members[0]
            m_uuid, m_node, m_tag = d_members[0]
            if n_uuid == m_uuid:
                continue
            n_far = self._xy(n_node.find("end" if n_tag == "start" else "start"))
            m_far = self._xy(m_node.find("end" if m_tag == "start" else "start"))
            n_width_node, m_width_node = n_node.find("width"), m_node.find("width")
            if n_far is None or m_far is None or n_width_node is None or m_width_node is None:
                continue
            # The second pad: same net, on this layer, with the lead ending at its
            # centre and its centre near the line through the first pad's centre.
            b_uuid = next(
                (
                    uid
                    for uid, other in pads.items()
                    if uid != opening
                    and other[3] == net_name
                    and other[5] != "unsupported"
                    and (layer in other[2] or "*.Cu" in other[2])
                    and math.dist(other[1], n_far) < 0.005
                    and abs((other[1][0] - centre_a[0]) * nx + (other[1][1] - centre_a[1]) * ny)
                    < _BRIDGE_AXIS_TOLERANCE_MM
                ),
                None,
            )
            if b_uuid is None:
                continue
            centre_b = pads[b_uuid][1]
            n_dir = (n_far[0] - b_point[0], n_far[1] - b_point[1])
            m_dir = (d_point[0] - m_far[0], d_point[1] - m_far[1])
            n_len, m_len = math.hypot(*n_dir), math.hypot(*m_dir)
            if n_len < _TOLERANCE_MM or m_len < _TOLERANCE_MM:
                continue
            # Neither lead may run along the bridge axis: the join must be a corner
            # whose line meets the new line at one point.
            m_cross = (m_dir[0] * uy - m_dir[1] * ux) / m_len
            if abs((n_dir[0] * uy - n_dir[1] * ux) / n_len) < 1e-3 or abs(m_cross) < 1e-3:
                continue
            if abs(_num(n_width_node) - width) > 1e-3:
                continue
            # The join M keeps its direction and its far end; its near end slides
            # along its own line onto the line through both pad centres.
            s_param = ((centre_a[0] - m_far[0]) * nx + (centre_a[1] - m_far[1]) * ny) / (
                (m_dir[0] * nx + m_dir[1] * ny) / m_len
            )
            q = (m_far[0] + m_dir[0] / m_len * s_param, m_far[1] + m_dir[1] / m_len * s_param)
            if s_param < _MIN_RUN_MM:
                continue
            t_q = (q[0] - centre_a[0]) * ux + (q[1] - centre_a[1]) * uy
            t_b = (centre_b[0] - centre_a[0]) * ux + (centre_b[1] - centre_a[1]) * uy
            if t_q * t_b >= 0 or min(abs(t_q), abs(t_b)) < _MIN_RUN_MM:
                continue

            # A topology match: from here on a failure is a declined candidate.
            start_ab, end_ab = (centre_a, centre_b) if b_tag == "end" else (centre_b, centre_a)
            bridge_dx = round(end_ab[0], 6) - round(start_ab[0], 6)
            bridge_dy = round(end_ab[1], 6) - round(start_ab[1], 6)
            if not is_45_aligned(bridge_dx, bridge_dy):
                off_deg = off_angle_degrees(bridge_dx, bridge_dy)
                return (
                    "pad_centres_not_aligned",
                    f"{pad_ref} and {pads[b_uuid][4]} centres off 45-degree axis by {off_deg:.4f} deg",
                )
            if any(
                self._locks.is_locked(node) or self._locks.is_generator_owned(node)
                for node in (n_node, m_node)
            ):
                return "locked_segment", ""
            if {seg_uuid, n_uuid, m_uuid} & touched_by_action:
                return "segment_end_already_moved", ""
            travel = max(abs(offset), math.dist(q, d_point), n_len)
            if travel > max_displacement:
                return "exceeds_max_displacement", f"needs {travel:.4f}mm"
            if pad_poly.buffer(-_INSIDE_EPS_MM).contains(Point(q)):
                return "no_room_for_entry", ""
            for other_uuid, other in pads.items():
                if other_uuid in (opening, b_uuid):
                    continue
                if not (layer in other[2] or "*.Cu" in other[2]):
                    continue
                grown = other[0].buffer(_INSIDE_EPS_MM)
                if grown.contains(Point(d_point)) or grown.contains(Point(q)):
                    return "junction_at_moved_end", ""

            m_width = _num(m_width_node) or width
            old_copper = unary_union(
                [
                    LineString([start, end]).buffer(width / 2, quad_segs=16),
                    LineString([m_far, d_point]).buffer(m_width / 2, quad_segs=16),
                    LineString([b_point, n_far]).buffer(width / 2, quad_segs=16),
                ]
            )
            new_run = LineString([q, centre_b]).buffer(width / 2, quad_segs=16)
            new_join = LineString([m_far, q]).buffer(m_width / 2, quad_segs=16)
            moves = [
                (
                    LineString([start, end]).buffer(width / 2, quad_segs=16),
                    new_run,
                    layer,
                ),
                (
                    LineString([m_far, d_point]).buffer(m_width / 2, quad_segs=16),
                    new_join,
                    layer,
                ),
            ]
            refusal = self._copper_floor_reason(
                moves, layer, net_name, seg_uuid, pads, clearance_mm
            )
            if refusal is None and self._loses_same_net_contact(
                [(old_copper, unary_union([new_run, new_join]), layer)],
                layer,
                net_name,
                {seg_uuid, n_uuid, m_uuid},
                pads,
            ):
                refusal = ("would_break_same_net_contact", "")
            if refusal is None:
                refusal = self._other_opening_reason(
                    [opening, b_uuid],
                    moves,
                    [(d_point, q, frozenset({layer}))],
                    pads,
                    required_mm or 0.0,
                    travel,
                )
            if refusal is not None:
                return refusal

            m_end = self._xy(m_node.find(m_tag)) or d_point
            n_end = self._xy(n_node.find(n_tag)) or b_point
            n_other = "end" if n_tag == "start" else "start"
            end_moves = [
                (seg_uuid, b_tag, *b_point, *centre_b),
                (seg_uuid, d_tag, *d_point, *centre_a),
                (m_uuid, m_tag, *m_end, *q),
                # The lead becomes the entry from the join to the first pad.
                (n_uuid, n_tag, *n_end, *q),
                (n_uuid, n_other, *n_far, *centre_a),
            ]
            action = MaskBridgeAction(
                action="bridge_aligned_pads",
                uuid=seg_uuid,
                kind="segment",
                pad_uuid=opening,
                pad_ref=pad_ref,
                net_name=net_name,
                layer=layer,
                old_x=d_point[0],
                old_y=d_point[1],
                new_x=q[0],
                new_y=q[1],
                dx=nx * offset,
                dy=ny * offset,
                displacement_mm=travel,
                detail=(
                    f"slid {abs(offset):.4f}mm onto the line through {pad_ref} and "
                    f"{pads[b_uuid][4]} centres, re-using the lead to {pads[b_uuid][4]} "
                    f"as the entry to {pad_ref}"
                ),
                openings=[opening],
                points=[
                    ("segment", seg_uuid, "start", *start),
                    ("segment", seg_uuid, "end", *end),
                    ("segment", m_uuid, m_tag, *m_end),
                    ("segment", n_uuid, n_tag, *n_end),
                ],
                end_moves=end_moves,
            )
            return self._apply_bridge(action, dry_run, result)
        return not_a_bridge

    def _apply_bridge(
        self, action: MaskBridgeAction, dry_run: bool, result: MaskRepairResult
    ) -> tuple[str, str] | None:
        """Record a bridge and, unless dry, write it; ``None`` on success."""
        if not dry_run:
            # Resolve every end first so a lookup miss can neither leave the
            # document half-moved nor record an unapplied action.
            targets: list[tuple[SExp, float, float]] = []
            for uuid, tag, old_x, old_y, new_x, new_y in action.end_moves:
                node = self._segment_by_uuid(uuid)
                end_node = node.find(tag) if node is not None else None
                xy = self._xy(end_node)
                if end_node is None or xy is None or math.dist(xy, (old_x, old_y)) > _TOLERANCE_MM:
                    return "stale_geometry", ""
                targets.append((end_node, new_x, new_y))
            for end_node, new_x, new_y in targets:
                # Full file precision keeps the join exactly on its own line.
                end_node.set_value(0, round(new_x, 6))
                end_node.set_value(1, round(new_y, 6))
            self.modified = True
        result.actions.append(action)
        result.repaired += 1
        return None

    def _undo_bridge(self, action: MaskBridgeAction) -> bool:
        """Reverse a bridge: every recorded end returns to its authored coordinates.

        Returns ``False`` when a node is gone or an end no longer sits at its
        post-move position (callers fall back to a snapshot restore).
        """
        restore: list[tuple[SExp, float, float]] = []
        for uuid, tag, old_x, old_y, new_x, new_y in action.end_moves:
            node = self._segment_by_uuid(uuid)
            end_node = node.find(tag) if node is not None else None
            xy = self._xy(end_node)
            if (
                end_node is None
                or xy is None
                or math.dist(xy, (round(new_x, 6), round(new_y, 6))) > _TOLERANCE_MM
            ):
                return False
            restore.append((end_node, old_x, old_y))
        for end_node, old_x, old_y in restore:
            end_node.set_value(0, old_x)
            end_node.set_value(1, old_y)
        self.modified = True
        return True

    def _anchor_on_reported_via(
        self, anchor: tuple[float, float], net: str, reported_vias: set[str]
    ) -> bool:
        """Whether ``anchor`` sits on a same-net via that still has its own mask finding."""
        for uid in reported_vias:
            via = self._via_by_uuid(uid)
            if via is None or self._net_name(via) != net:
                continue
            pos = self._xy(via.find("at"))
            if pos is not None and math.dist(anchor, pos) < _TOLERANCE_MM:
                return True
        return False

    def _flip_elbow_into_pad(
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
        touched: set[str],
        reported_vias: set[str] | None = None,
        warning_clearance_mm: float | None = None,
    ) -> bool:
        """Swap the two legs of an elbow to make a straight entry into the pad.

        An overlapping pass-through track that runs along a pad's edge and
        turns into the pad at the corner is half under the pad's own opening
        and half beside it, and is never an own-pad entry because no authored
        end is strictly inside the pad. When the track and exactly one other
        same-net track form a two-leg chain between two fixed anchors, the
        mirror elbow (the same two legs in the opposite order) joins the same
        anchors. Every leg keeps its length and angle, so legal directions stay
        legal, and the moved corner becomes the strictly-inside end of a
        straight entry. Both corner ends translate together, preserving any
        authored offset inside the junction tolerance. A corner that stays outside
        the pad is accepted when the far leg's end is inside it and the other leg
        clears the whole opening (a stub handing a hugging track over to the pad).

        The swap is also held to the mask rule before anything moves: a swapped
        leg that overlaps the opening has to be one clean entry, and one that
        misses it has to clear it by the policy gap plus margin.  A swap whose
        entry leg is not clean is slid out along the pad's lead instead
        (:meth:`_slide_swapped_elbow_along_lead`) when a lead continues from
        the entry leg's far end.  Returns ``True`` when applied.
        """
        if required is None or opening in self._pivot_pad_bounds:
            return False
        pad, _, pad_layers, net, ref, shape = pads[opening]
        if shape == "unsupported" or not net or self._net_name(seg) != net:
            return False
        if self._locks.is_locked(seg) or self._locks.is_generator_owned(seg) or seg_uuid in touched:
            return False
        layer_node, width_node = seg.find("layer"), seg.find("width")
        if layer_node is None or width_node is None:
            return False
        layer = _bare(layer_node.get_first_atom())
        if layer not in pad_layers and "*.Cu" not in pad_layers:
            return False
        width = _num(width_node)
        start, end = self._xy(seg.find("start")), self._xy(seg.find("end"))
        if start is None or end is None or width <= 0:
            return False
        rules = self._board_rules
        if rules.dru_error:
            return False
        inside = pad.buffer(-_INSIDE_EPS_MM)

        for elbow, anchor, tag in ((end, start, "end"), (start, end, "start")):
            members, blocked = self._junction(layer, elbow, seg_uuid, net)
            if blocked or len(members) != 1 or self._has_fixed_junction_contact(elbow, layer, net):
                continue
            other_uuid, other, other_tag = members[0]
            if (
                other_uuid in touched
                or self._locks.is_locked(other)
                or self._locks.is_generator_owned(other)
            ):
                continue
            other_elbow = self._xy(other.find(other_tag))
            far = self._xy(other.find("end" if other_tag == "start" else "start"))
            other_width_node = other.find("width")
            if other_elbow is None or far is None or other_width_node is None:
                continue
            other_width = _num(other_width_node) or width
            # Give the first leg the second leg's vector first (the mirror elbow
            # keeps both leg lengths and angles). When that corner stays inside
            # the mask opening, try the two orthogonal L-corners between anchor
            # and far so an axial-plus-45° pad-corner approach can enter the pad
            # perpendicularly while its outer leg clears the opening (#9939).
            from ..router.quantize import _is_quantum_aligned

            mirror = (
                round(anchor[0] + far[0] - other_elbow[0], 4),
                round(anchor[1] + far[1] - other_elbow[1], 4),
            )
            candidates: list[tuple[tuple[float, float], bool]] = [(mirror, True)]
            # If the outer anchor sits on a via with its own finding this pass,
            # let the via relocate first so the orthogonal lead uses its final seat.
            if not self._anchor_on_reported_via(anchor, net, reported_vias or set()):
                for ortho in (
                    (round(anchor[0], 4), round(far[1], 4)),
                    (round(far[0], 4), round(anchor[1], 4)),
                ):
                    if all(
                        math.dist(ortho, prev) >= _TOLERANCE_MM
                        for prev in (*(pt for pt, _ in candidates), elbow)
                    ):
                        candidates.append((ortho, False))

            for flipped, is_mirror in candidates:
                dx, dy = flipped[0] - elbow[0], flipped[1] - elbow[1]
                saved_flipped = (round(elbow[0] + dx, 4), round(elbow[1] + dy, 4))
                other_flipped = (round(other_elbow[0] + dx, 4), round(other_elbow[1] + dy, 4))
                displacement = math.hypot(dx, dy)
                if displacement < _TOLERANCE_MM or displacement > max_displacement:
                    continue
                if (
                    min(math.dist(anchor, saved_flipped), math.dist(other_flipped, far))
                    < _MIN_RUN_MM
                ):
                    continue
                if not is_mirror and not (
                    _is_quantum_aligned(saved_flipped[0] - anchor[0], saved_flipped[1] - anchor[1])
                    and _is_quantum_aligned(far[0] - other_flipped[0], far[1] - other_flipped[1])
                ):
                    continue
                minimum_mask_gap = None
                corner_inside = inside.contains(Point(saved_flipped)) and inside.contains(
                    Point(other_flipped)
                )
                if not is_mirror and corner_inside:
                    # An axis-aligned entry straight into an inside corner must
                    # stay clear of the pad's side edges by at least half width.
                    corner_inside = pad.buffer(-width / 2).contains(Point(saved_flipped))
                if not corner_inside:
                    # The corner stays outside the pad (a stub that hands a track
                    # hugging the pad edge over to the pad, #9939): the swap is
                    # still a legal entry when the far leg now runs straight into
                    # the pad and the other leg clears the whole opening.
                    aperture = self._pad_mask_openings.get((opening, layer))
                    minimum_mask_gap = required + margin_mm
                    if not (
                        inside.contains(Point(far))
                        and aperture is not None
                        and LineString([anchor, saved_flipped]).distance(aperture.geometry)
                        - width / 2
                        >= minimum_mask_gap - 1e-9
                    ):
                        continue

                moves: list[tuple[BaseGeometry, BaseGeometry, str]] = [
                    (
                        LineString([anchor, elbow]).buffer(width / 2, quad_segs=16),
                        LineString([anchor, saved_flipped]).buffer(width / 2, quad_segs=16),
                        layer,
                    ),
                    (
                        LineString([other_elbow, far]).buffer(other_width / 2, quad_segs=16),
                        LineString([other_flipped, far]).buffer(other_width / 2, quad_segs=16),
                        layer,
                    ),
                ]
                points: list[tuple[str, str, str | None, float, float]] = [
                    ("segment", seg_uuid, tag, *elbow),
                    ("segment", other_uuid, other_tag, *other_elbow),
                ]
                if not self._slide_keeps_connections(points, dx, dy, net):
                    continue
                if self._copper_floor_reason(
                    moves, layer, net, seg_uuid, pads, clearance_mm, pairwise_rules=rules
                ):
                    continue
                if self._other_opening_reason(
                    [opening],
                    moves,
                    [
                        (elbow, saved_flipped, frozenset({layer})),
                        (other_elbow, other_flipped, frozenset({layer})),
                    ],
                    pads,
                    required,
                    displacement,
                ):
                    continue
                swap = _SwappedElbow(
                    uuid=seg_uuid,
                    tag=tag,
                    elbow=elbow,
                    anchor=anchor,
                    width=width,
                    other_uuid=other_uuid,
                    other_tag=other_tag,
                    other_elbow=other_elbow,
                    other_width=other_width,
                    far=far,
                    corner=saved_flipped,
                    other_corner=other_flipped,
                )
                if self._swapped_elbow_unclear(swap, opening, layer, pads, required, margin_mm):
                    if self._slide_swapped_elbow_along_lead(
                        swap,
                        opening,
                        layer,
                        net,
                        pads,
                        result,
                        max_displacement,
                        dry_run,
                        clearance_mm,
                        required,
                        margin_mm,
                        warning_clearance_mm,
                        touched,
                    ):
                        return True
                    continue
                action = MaskNudgeAction(
                    action="nudge_clear",
                    uuid=seg_uuid,
                    kind="segment",
                    pad_uuid=opening,
                    pad_ref=ref,
                    net_name=net,
                    layer=layer,
                    old_x=elbow[0],
                    old_y=elbow[1],
                    new_x=saved_flipped[0],
                    new_y=saved_flipped[1],
                    dx=dx,
                    dy=dy,
                    displacement_mm=displacement,
                    detail=f"flipped a two-leg elbow {displacement:.4f}mm for a straight entry into {ref}",
                    openings=[opening],
                    points=points,
                    minimum_mask_gap_mm=minimum_mask_gap,
                )
                return self._apply_nudge(action, result, dry_run) is None
        return False

    def _swapped_elbow_unclear(
        self,
        swap: _SwappedElbow,
        opening: str,
        layer: str,
        pads: dict,
        required: float,
        margin_mm: float,
    ) -> bool:
        """Whether the mask rule would still report a leg of a swapped elbow.

        A leg that overlaps the pad's opening has to be one clean entry by the
        rule's own measure: an authored end strictly inside the pad, and no
        more copper beside the opening than one crossing leaves.  A leg that
        misses the opening has to clear it by the policy gap plus ``margin_mm``.
        Nothing is refused without a modelled opening; the verified re-check
        judges that swap instead.
        """
        from ..validate.mask_copper import clean_own_pad_entry

        aperture = self._pad_mask_openings.get((opening, layer))
        if aperture is None:
            return False
        pad, _centre, _layers, _net, _ref, shape = pads[opening]
        for start, end, width in (
            (swap.anchor, swap.corner, swap.width),
            (swap.other_corner, swap.far, swap.other_width),
        ):
            copper = LineString([start, end]).buffer(width / 2, quad_segs=16)
            gap = copper.distance(aperture.geometry)
            if gap > 0:
                if gap < required + margin_mm - 1e-9:
                    return True
                continue
            if not clean_own_pad_entry(
                aperture.geometry,
                copper,
                pad,
                f"pad:{shape}",
                width,
                (start, end),
                clearance_mm=required,
                uncertainty_mm=_MASK_UNCERTAINTY_MM,
                margin_mm=aperture.margin_mm,
            ):
                return True
        return False

    def _slide_swapped_elbow_along_lead(
        self,
        swap: _SwappedElbow,
        opening: str,
        layer: str,
        net: str,
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
        """Slide a swapped elbow out along the pad's lead until its legs clear the opening.

        The swap ends its second leg at ``far``, where a pad lead carries on
        into the pad.  Carrying that joint out along the lead leaves the lead
        as the pad's only entry.  The swapped corner follows along its own
        leg's line, so the second leg stays parallel to itself and every angle
        is kept; the first leg may double back past its anchor, but no leg may
        shrink to a point.  The smallest slide that puts both swapped legs
        outside the opening by the policy gap plus ``margin_mm`` -- the
        manufacturer's advisory band first, when a slide fits it -- is taken
        behind the same connection, clearance and opening gates as the swap,
        and the lead itself has to be one clean entry.  Returns ``True`` when
        applied.
        """
        from ..validate.mask_copper import clean_own_pad_entry

        aperture = self._pad_mask_openings.get((opening, layer))
        if aperture is None:
            return False
        pad, _centre, _layers, _net, ref, shape = pads[opening]
        members, blocked = self._junction(layer, swap.far, swap.other_uuid, net)
        if blocked or len(members) != 1 or self._has_fixed_junction_contact(swap.far, layer, net):
            return False
        lead_uuid, lead, lead_tag = members[0]
        if (
            lead_uuid in touched
            or self._locks.is_locked(lead)
            or self._locks.is_generator_owned(lead)
        ):
            return False
        lead_end = self._xy(lead.find("end" if lead_tag == "start" else "start"))
        lead_joint = self._xy(lead.find(lead_tag))
        lead_width_node = lead.find("width")
        if lead_end is None or lead_joint is None or lead_width_node is None:
            return False
        lead_width = _num(lead_width_node) or swap.other_width
        into = (lead_end[0] - swap.far[0], lead_end[1] - swap.far[1])
        lead_length = math.hypot(*into)
        run = (swap.corner[0] - swap.anchor[0], swap.corner[1] - swap.anchor[1])
        entry = (swap.far[0] - swap.corner[0], swap.far[1] - swap.corner[1])
        det = run[0] * entry[1] - run[1] * entry[0]
        if lead_length < _TOLERANCE_MM or abs(det) < 1e-9:
            return False
        run_length, entry_length = math.hypot(*run), math.hypot(*entry)
        far_tag = "end" if swap.other_tag == "start" else "start"
        rules = self._board_rules
        first = LineString([swap.anchor, swap.elbow]).buffer(swap.width / 2, quad_segs=16)
        second = LineString([swap.other_elbow, swap.far]).buffer(swap.other_width / 2, quad_segs=16)
        lead_before = LineString([lead_joint, lead_end]).buffer(lead_width / 2, quad_segs=16)

        for gap in dict.fromkeys((max(warning_clearance_mm or 0.0, required), required)):
            for step in range(1, int(max_displacement / _SLIDE_STEP_MM + 1e-9) + 1):
                slide = step * _SLIDE_STEP_MM
                new_far = (
                    round(swap.far[0] - slide * into[0] / lead_length, 4),
                    round(swap.far[1] - slide * into[1] / lead_length, 4),
                )
                # The corner keeps to its leg's line while the second leg stays parallel.
                offset = (new_far[0] - swap.anchor[0], new_far[1] - swap.anchor[1])
                along = (offset[0] * entry[1] - offset[1] * entry[0]) / det
                across = (run[0] * offset[1] - run[1] * offset[0]) / det
                if min(abs(along) * run_length, across * entry_length) < _MIN_RUN_MM:
                    continue
                new_corner = (
                    round(swap.anchor[0] + along * run[0], 4),
                    round(swap.anchor[1] + along * run[1], 4),
                )
                shift = (new_corner[0] - swap.elbow[0], new_corner[1] - swap.elbow[1])
                carried = (new_far[0] - swap.far[0], new_far[1] - swap.far[1])
                displacement = max(math.hypot(*shift), math.hypot(*carried))
                if displacement > max_displacement:
                    break
                new_other = (
                    round(swap.other_elbow[0] + shift[0], 4),
                    round(swap.other_elbow[1] + shift[1], 4),
                )
                new_lead = (
                    round(lead_joint[0] + carried[0], 4),
                    round(lead_joint[1] + carried[1], 4),
                )
                first_moved = LineString([swap.anchor, new_corner]).buffer(
                    swap.width / 2, quad_segs=16
                )
                second_moved = LineString([new_other, new_far]).buffer(
                    swap.other_width / 2, quad_segs=16
                )
                if any(
                    copper.distance(aperture.geometry) < gap + margin_mm - 1e-9
                    for copper in (first_moved, second_moved)
                ):
                    continue
                lead_moved = LineString([new_lead, lead_end]).buffer(lead_width / 2, quad_segs=16)
                if not clean_own_pad_entry(
                    aperture.geometry,
                    lead_moved,
                    pad,
                    f"pad:{shape}",
                    lead_width,
                    (new_lead, lead_end),
                    clearance_mm=required,
                    uncertainty_mm=_MASK_UNCERTAINTY_MM,
                    margin_mm=aperture.margin_mm,
                ):
                    continue
                moves: list[tuple[BaseGeometry, BaseGeometry, str]] = [
                    (first, first_moved, layer),
                    (second, second_moved, layer),
                    (lead_before, lead_moved, layer),
                ]
                points: list[tuple[str, str, str | None, float, float]] = [
                    ("segment", swap.uuid, swap.tag, *swap.elbow),
                    ("segment", swap.other_uuid, swap.other_tag, *swap.other_elbow),
                    ("segment", swap.other_uuid, far_tag, *swap.far),
                    ("segment", lead_uuid, lead_tag, *lead_joint),
                ]
                shifts: list[tuple[float, float]] = [shift, shift, carried, carried]
                if not self._slide_keeps_connections(points, 0.0, 0.0, net, shifts):
                    continue
                if self._copper_floor_reason(
                    moves, layer, net, swap.uuid, pads, clearance_mm, pairwise_rules=rules
                ):
                    continue
                if self._other_opening_reason(
                    [opening],
                    moves,
                    [
                        (swap.elbow, new_corner, frozenset({layer})),
                        (swap.other_elbow, new_other, frozenset({layer})),
                        (swap.far, new_far, frozenset({layer})),
                        (lead_joint, new_lead, frozenset({layer})),
                    ],
                    pads,
                    required,
                    displacement,
                ):
                    continue
                action = MaskNudgeAction(
                    action="nudge_clear",
                    uuid=swap.uuid,
                    kind="segment",
                    pad_uuid=opening,
                    pad_ref=ref,
                    net_name=net,
                    layer=layer,
                    old_x=swap.elbow[0],
                    old_y=swap.elbow[1],
                    new_x=new_corner[0],
                    new_y=new_corner[1],
                    dx=shift[0],
                    dy=shift[1],
                    displacement_mm=displacement,
                    detail=(
                        f"flipped a two-leg elbow {math.hypot(*shift):.4f}mm and slid it "
                        f"{slide:.4f}mm along the {ref} lead for a straight entry"
                    ),
                    openings=[opening],
                    points=points,
                    shifts=shifts,
                    minimum_mask_gap_mm=gap + margin_mm,
                )
                return self._apply_nudge(action, result, dry_run) is None
        return False

    def _slide_keeps_connections(
        self,
        points: list[tuple[str, str, str | None, float, float]],
        dx: float,
        dy: float,
        net_name: str,
        shifts: list[tuple[float, float]] | None = None,
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
            for index, (_kind, uuid, tag, old_x, old_y) in enumerate(points):
                node = self._segment_by_uuid(uuid)
                end_node = None if node is None or tag is None else node.find(tag)
                if end_node is None:
                    return False
                original.append((end_node, list(end_node.children)))
                pdx, pdy = shifts[index] if shifts else (dx, dy)
                end_node.set_value(0, round(old_x + pdx, 4))
                end_node.set_value(1, round(old_y + pdy, 4))
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
        # Extending a straight leg can retain its entire old copper polygon,
        # including a tangent contact at the fixed end. Shrinking that cap
        # would manufacture a gap. Apply the rounding margin to legs that
        # actually give up copper.
        receding = [(old, new.buffer(-0.0002)) for old, new, _layer in moves if not new.covers(old)]
        if not receding:
            return False
        reach = unary_union([old.buffer(_TOLERANCE_MM) for old, _kept in receding])
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
            for old, kept in receding:
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
        reported_tracks: set[str],
        warning_clearance_mm: float | None,
        pad_bond: bool = False,
    ) -> tuple[str, str] | None:
        """Move a pass-through via clear while preserving its track connections.

        An attached track with a mask finding favors a relocation that keeps
        the approach on its original line. Other vias try the shortest direct
        push first. Relocations use bounded, aligned connections inside the
        new via land and check the project's authored clearances on every
        spanned layer. If no candidate passes, report the primary refusal.
        """
        entries = [pads[opening] for opening in openings]
        if any(entry[5] == "unsupported" for entry in entries):
            return "unverified_pad_shape", ""
        net_name = self._net_name(via_node)
        if not net_name or any(entry[3] != net_name for entry in entries):
            return "not_same_net", ""
        if self._locks.is_locked(via_node):
            return "locked_via", ""
        if pad_bond and self._locks.is_generator_owned(via_node):
            return "generator_owned_via", ""

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
            max_displacement + (math.dist(at, entries[0][1]) if pad_bond else 0.0),
        )

        if pad_bond:
            if joined or len(openings) != 1 or design_rules is None or project is None:
                return "unverified_pad_bond", ""
            gap = max(required, warning_clearance_mm or 0.0) + margin_mm
            for bridge_push, bridge in self._pad_bond_bridges(
                via_node,
                via_uuid,
                openings[0],
                pads,
                at,
                size,
                gap,
                required,
                max_displacement,
                design_rules,
            ):
                refusal = self._via_tails_candidate(
                    via_node,
                    via_uuid,
                    openings,
                    pads,
                    result,
                    at,
                    size,
                    copper,
                    joined,
                    bridge_push,
                    required,
                    math.hypot(*bridge_push),
                    max_displacement,
                    dry_run,
                    clearance_mm,
                    design_rules,
                    touched_by_action,
                    project,
                    bridge=bridge,
                )
                if refusal is None:
                    action = result.actions[-1]
                    assert isinstance(action, MaskViaRelocateAction)
                    action.minimum_mask_gap_mm = gap
                    return None
            return (
                "no_safe_pad_bridge",
                "no aligned pad connection clears the project and mask limits",
            )

        push = self._push_clear_of_openings(
            copper, [(pads[o][0], pads[o][1]) for o in openings], required + margin_mm
        )
        primary_reason: tuple[str, str] | None = ("infeasible", "")
        primary_push = None
        if push is not None:
            displacement = math.hypot(*push)
            if displacement < _TOLERANCE_MM / 2:
                return "already_cleared", ""
            if displacement > max_displacement:
                primary_reason = "exceeds_max_displacement", f"needs {displacement:.4f}mm"
            else:
                primary_push = push

        def relocate(preferred_gap: float) -> bool:
            if project is None:
                return False
            for candidate in self._via_alternate_candidates(
                at,
                size,
                openings,
                pads,
                required + margin_mm,
                max_displacement,
                primary_push,
                preferred_gap=preferred_gap,
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
                    return True
            return False

        # Bending an approach that already has a mask finding can put that
        # same exposure on a newly split segment. Keep the original approach
        # line and identity if a bounded relocation passes the project rules.
        preserve_approach = any(uid in reported_tracks for uid, *_ in joined)
        if preserve_approach and relocate(max(required, warning_clearance_mm or 0) + margin_mm):
            return None
        if primary_push is not None:
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
                primary_push,
                required,
                math.hypot(*primary_push),
                max_displacement,
                dry_run,
                clearance_mm,
                design_rules,
                touched_by_action,
                project,
            )
            if primary_reason is None:
                return None
        if not preserve_approach and relocate(required + margin_mm):
            return None
        if (
            not preserve_approach
            and project is not None
            and self._relocate_via_sliding_foreign_jog(
                via_node,
                via_uuid,
                openings,
                pads,
                result,
                at,
                size,
                copper,
                joined,
                primary_push,
                required,
                margin_mm,
                warning_clearance_mm,
                max_displacement,
                dry_run,
                clearance_mm,
                design_rules,
                touched_by_action,
                project,
            )
        ):
            return None
        return primary_reason

    def _relocate_via_sliding_foreign_jog(
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
        primary_push: tuple[float, float] | None,
        required: float,
        margin_mm: float,
        warning_clearance_mm: float | None,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
        design_rules: DesignRules | None,
        touched_by_action: set[str],
        project: tuple,
    ) -> bool:
        """Relocate a via by sliding a blocking foreign-net jog along its parallel runs."""
        from kicad_tools.router.board_clearance_rules import (
            ItemProps,
            item_props_for_type,
            pad_clearance_override,
        )
        from kicad_tools.router.primitives import _make_uuid
        from kicad_tools.router.quantize import _is_quantum_aligned, is_45_aligned
        from kicad_tools.schema.pcb import PCB

        if self._locks.is_generator_owned(via_node):
            return False
        rules = self._board_rules
        if rules.dru_error:
            return False
        net_name = self._net_name(via_node)
        span_layers = self._via_span_layer_set(via_node)
        base_floor = clearance_mm if clearance_mm is not None else 0.0
        via_props = item_props_for_type("via", net_name, True)
        own_track_props = ItemProps("Track", net_name)
        _project_copper, holes = project

        obstacles: list[
            tuple[BaseGeometry, float, float, frozenset[str], str, str, str, float]
        ] = []
        joined_uuids = {uid for uid, *_ in joined}
        for seg_uid, node in self._segment_nodes().items():
            obs_net = self._net_name(node)
            if seg_uid in joined_uuids or obs_net == net_name:
                continue
            layer_node = node.find("layer")
            layer = _bare(layer_node.get_first_atom()) if layer_node is not None else ""
            if not layer or layer not in span_layers:
                continue
            seg_s, seg_e = self._xy(node.find("start")), self._xy(node.find("end"))
            width_node = node.find("width")
            if seg_s is None or seg_e is None or width_node is None:
                continue
            geom = LineString([seg_s, seg_e]).buffer(_num(width_node) / 2, quad_segs=16)
            obs_props = item_props_for_type(
                "track", obs_net, False, layer=layer, footprint_ref="", footprint_lib_id=""
            )
            via_req = max(
                base_floor,
                rules.clearance(via_props, obs_props, layer),
                rules.physical_clearance(via_props, obs_props, layer),
            )
            trk_req = max(
                base_floor,
                rules.clearance(own_track_props, obs_props, layer),
                rules.physical_clearance(own_track_props, obs_props, layer),
            )
            obstacles.append(
                (
                    geom,
                    via_req,
                    trk_req,
                    frozenset({layer}),
                    "track",
                    seg_uid,
                    obs_net,
                    copper.distance(geom),
                )
            )
        for other_via in self.doc.find_all("via"):
            other_at = self._xy(other_via.find("at"))
            other_size = other_via.find("size")
            if other_at is None or other_size is None or math.dist(other_at, at) < _TOLERANCE_MM:
                continue
            obs_net = self._net_name(other_via)
            if obs_net == net_name:
                continue
            layers = self._via_span_layer_set(other_via)
            if not (layers & span_layers):
                continue
            geom = Point(other_at).buffer(_num(other_size) / 2, quad_segs=32)
            obs_props = item_props_for_type("via", obs_net, True)
            via_req = max(
                base_floor,
                rules.clearance(via_props, obs_props),
                rules.physical_clearance(via_props, obs_props),
            )
            trk_req = max(
                base_floor,
                *(
                    max(
                        rules.clearance(own_track_props, obs_props, lyr),
                        rules.physical_clearance(own_track_props, obs_props, lyr),
                    )
                    for lyr in layers & span_layers
                ),
            )
            obstacles.append(
                (
                    geom,
                    via_req,
                    trk_req,
                    layers,
                    "via",
                    self._via_uuid(other_via),
                    obs_net,
                    copper.distance(geom),
                )
            )
        pcb = PCB(self.doc)
        for fp in pcb.footprints:
            for index, pad in enumerate(fp.pads):
                identity = _bare(pad.uuid) if pad.uuid else f"{fp.reference}:pad:{index}"
                entry = pads.get(identity)
                if entry is None:
                    continue
                poly, _centre, pad_layers, pad_net, _ref, _shape = entry
                if pad_net == net_name:
                    continue
                layers = frozenset(
                    name
                    for name in span_layers
                    if name in pad_layers
                    or "*.Cu" in pad_layers
                    or ("F&B.Cu" in pad_layers and name in {"F.Cu", "B.Cu"})
                )
                if not layers:
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
                local = pad_clearance_override(fp, pad)
                elec_via = (
                    rules.clearance(via_props, pad_props)
                    if local is None
                    else max(local, rules.min_clearance or 0.0)
                )
                via_req = max(base_floor, rules.physical_clearance(via_props, pad_props), elec_via)
                elec_trk = max(
                    (
                        rules.clearance(own_track_props, pad_props, lyr)
                        if local is None
                        else max(local, rules.min_clearance or 0.0)
                    )
                    for lyr in layers
                )
                trk_req = max(
                    base_floor,
                    max(
                        rules.physical_clearance(own_track_props, pad_props, lyr) for lyr in layers
                    ),
                    elec_trk,
                )
                obstacles.append(
                    (
                        poly,
                        via_req,
                        trk_req,
                        layers,
                        "pad",
                        identity,
                        pad_net,
                        copper.distance(poly),
                    )
                )

        preferred = max(required, warning_clearance_mm or 0.0) + margin_mm
        candidates = self._via_alternate_candidates(
            at,
            size,
            openings,
            pads,
            required + margin_mm,
            max_displacement,
            primary_push,
            preferred_gap=preferred,
        )
        jog_cache: dict[str, _ForeignJog | None] = {}

        for push in candidates:
            centre = (round(at[0] + push[0], 4), round(at[1] + push[1], 4))
            via_disp = math.dist(at, centre)
            if via_disp > max_displacement + 1e-9:
                continue
            if any(
                (gap := math.dist(centre, h_c) - radii) < h_req - 1e-6 and gap < old_gap - 1e-6
                for h_c, radii, h_req, old_gap, _desc in holes
            ):
                continue

            via_plan = self._via_elbow_or_tail_plan(
                joined,
                at,
                centre,
                size,
                net_name,
                openings,
                pads,
                required,
                margin_mm,
                max_displacement,
                touched_by_action,
            )
            if via_plan is None:
                continue
            via_end_moves, tails, via_final_lines, via_moved_points, via_moved_uuids = via_plan

            new_disc = Point(centre).buffer(size / 2, quad_segs=32)
            blocking_tracks: set[str] = set()
            blocked_by_fixed = False
            for geom, via_req, trk_req, layers, kind, uid, _obs_net, old_d in obstacles:
                if uid in via_moved_uuids:
                    continue
                violated = (
                    new_disc.distance(geom) < via_req - 1e-6
                    and new_disc.distance(geom) < old_d - 1e-6
                )
                if not violated:
                    for old_line, new_line, lyr in via_final_lines:
                        if lyr in layers:
                            nd = new_line.distance(geom)
                            if nd < trk_req - 1e-6 and nd < old_line.distance(geom) - 1e-6:
                                violated = True
                                break
                if violated:
                    if kind != "track":
                        blocked_by_fixed = True
                        break
                    blocking_tracks.add(uid)
            if blocked_by_fixed or not blocking_tracks:
                continue

            # Try every blocker in a stable order (set iteration order depends on
            # PYTHONHASHSEED): the first parsed jog that covers the whole blocker
            # set wins, so one unparseable blocker cannot hide a valid jog.
            jog: _ForeignJog | None = None
            for sample_uid in sorted(blocking_tracks):
                if sample_uid not in jog_cache:
                    jog_cache[sample_uid] = self._parse_foreign_jog(
                        sample_uid, pads, touched_by_action | via_moved_uuids
                    )
                cand_jog = jog_cache[sample_uid]
                if (
                    cand_jog is not None
                    and cand_jog.net != net_name
                    and blocking_tracks <= cand_jog.moved_uuids
                ):
                    jog = cand_jog
                    break
            if jog is None:
                continue

            ux, uy = jog.axis
            c0_s, c0_e, _ = jog.copies[0][1], jog.copies[0][2], jog.copies[0][3]
            mid_x, mid_y = (c0_s[0] + c0_e[0]) / 2, (c0_s[1] + c0_e[1]) / 2
            away_dot = (mid_x - centre[0]) * ux + (mid_y - centre[1]) * uy
            signs = (1.0, -1.0) if away_dot >= 0 else (-1.0, 1.0)

            jog_track_props = ItemProps("Track", jog.net)
            req_via_jog = max(
                base_floor,
                rules.clearance(via_props, jog_track_props, jog.layer),
                rules.physical_clearance(via_props, jog_track_props, jog.layer),
                rules.clearance(own_track_props, jog_track_props, jog.layer),
                rules.physical_clearance(own_track_props, jog_track_props, jog.layer),
            )
            via_shapes_on_jog_layer: list[BaseGeometry] = []
            if jog.layer in span_layers:
                via_shapes_on_jog_layer.append(new_disc)
            via_shapes_on_jog_layer.extend(
                new_l for _old_l, new_l, lyr in via_final_lines if lyr == jog.layer
            )
            if not via_shapes_on_jog_layer:
                continue

            chosen_jog: (
                tuple[
                    float,
                    float,
                    list[tuple[str, str, float, float, float, float]],
                    list[tuple[BaseGeometry, BaseGeometry, str]],
                    list[tuple[tuple[float, float], tuple[float, float], frozenset[str]]],
                ]
                | None
            ) = None
            diagonal_axis = abs(abs(ux) - abs(uy)) < 1e-6

            for sign in signs:
                for step in range(1, int(max_displacement / _SLIDE_STEP_MM + 1e-9) + 1):
                    signed = sign * step * _SLIDE_STEP_MM
                    if not jog.low < signed < jog.high:
                        break
                    if diagonal_axis:
                        mag = round(abs(signed) / math.sqrt(2.0), 4)
                        jdx = math.copysign(mag, signed * ux)
                        jdy = math.copysign(mag, signed * uy)
                    else:
                        jdx = round(signed * ux, 4)
                        jdy = round(signed * uy, 4)
                    jog_disp = math.hypot(jdx, jdy)
                    if jog_disp > max_displacement + 1e-9:
                        break
                    if (
                        chosen_jog is not None
                        and jog_disp >= math.hypot(chosen_jog[0], chosen_jog[1]) - 1e-9
                    ):
                        break

                    jog_end_moves: list[tuple[str, str, float, float, float, float]] = []
                    jog_moves: list[tuple[BaseGeometry, BaseGeometry, str]] = []
                    jog_moved_pts: list[
                        tuple[tuple[float, float], tuple[float, float], frozenset[str]]
                    ] = []
                    angles_ok = True
                    for c_uid, s_pt, e_pt, w in jog.copies:
                        ns = (round(s_pt[0] + jdx, 4), round(s_pt[1] + jdy, 4))
                        ne = (round(e_pt[0] + jdx, 4), round(e_pt[1] + jdy, 4))
                        if not (
                            _is_quantum_aligned(ne[0] - ns[0], ne[1] - ns[1])
                            or is_45_aligned(ne[0] - ns[0], ne[1] - ns[1])
                        ):
                            angles_ok = False
                            break
                        jog_end_moves.append((c_uid, "start", s_pt[0], s_pt[1], ns[0], ns[1]))
                        jog_end_moves.append((c_uid, "end", e_pt[0], e_pt[1], ne[0], ne[1]))
                        jog_moves.append(
                            (
                                LineString([s_pt, e_pt]).buffer(w / 2, quad_segs=16),
                                LineString([ns, ne]).buffer(w / 2, quad_segs=16),
                                jog.layer,
                            )
                        )
                        jog_moved_pts.append((s_pt, ns, frozenset({jog.layer})))
                        jog_moved_pts.append((e_pt, ne, frozenset({jog.layer})))
                    if not angles_ok:
                        continue
                    for r_uid, r_tag, joint_pt, far_pt, w in jog.runs:
                        nj = (round(joint_pt[0] + jdx, 4), round(joint_pt[1] + jdy, 4))
                        if math.dist(far_pt, nj) < _MIN_RUN_MM or not (
                            _is_quantum_aligned(nj[0] - far_pt[0], nj[1] - far_pt[1])
                            or is_45_aligned(nj[0] - far_pt[0], nj[1] - far_pt[1])
                        ):
                            angles_ok = False
                            break
                        jog_end_moves.append((r_uid, r_tag, joint_pt[0], joint_pt[1], nj[0], nj[1]))
                        jog_moves.append(
                            (
                                LineString([far_pt, joint_pt]).buffer(w / 2, quad_segs=16),
                                LineString([far_pt, nj]).buffer(w / 2, quad_segs=16),
                                jog.layer,
                            )
                        )
                        jog_moved_pts.append((joint_pt, nj, frozenset({jog.layer})))
                    if not angles_ok:
                        continue

                    if any(
                        new_j.distance(v_shape) < req_via_jog + 0.001
                        for _old_j, new_j, _lyr in jog_moves
                        for v_shape in via_shapes_on_jog_layer
                    ):
                        continue

                    if self._loses_same_net_contact(
                        jog_moves,
                        jog.layer,
                        jog.net,
                        set(jog.moved_uuids),
                        pads,
                        protect_ends=True,
                    ):
                        continue
                    if self._copper_floor_reason(
                        jog_moves,
                        jog.layer,
                        jog.net,
                        jog.copies[0][0],
                        pads,
                        clearance_mm,
                        pairwise_rules=rules,
                        skip_uuids=via_moved_uuids | {via_uuid},
                    ):
                        continue
                    if self._other_opening_reason(
                        [], jog_moves, jog_moved_pts, pads, required, jog_disp
                    ):
                        continue
                    chosen_jog = (jdx, jdy, jog_end_moves, jog_moves, jog_moved_pts)
                    break

            if chosen_jog is None:
                continue

            jdx, jdy, jog_end_moves, _jog_moves, _jog_moved_pts = chosen_jog
            via_moves: list[tuple[BaseGeometry, BaseGeometry, str]] = [
                (copper, new_disc, name) for name in sorted(span_layers)
            ]
            via_moves.extend(via_final_lines)
            if self._other_opening_reason(
                openings,
                via_moves,
                [(at, centre, frozenset(span_layers)), *via_moved_points],
                pads,
                required,
                max(via_disp, *(math.dist(o, n) for o, n, _ in via_moved_points), 0.0),
            ):
                continue

            all_end_moves = [*via_end_moves, *jog_end_moves]
            if not self._joint_via_jog_engine_and_connectivity_ok(
                at,
                centre,
                size,
                via_uuid,
                all_end_moves,
                tails,
                {net_name, jog.net},
                pads,
                design_rules,
            ):
                continue

            added: list[tuple[str, float, float, float, float, float, str, int | str]] = [
                (_make_uuid(), *tail) for tail in tails
            ]
            points: list[tuple[str, str, str | None, float, float]] = [
                ("via", via_uuid, None, at[0], at[1])
            ]
            points.extend(
                ("segment", uid, tag, ox, oy) for uid, tag, ox, oy, _nx, _ny in all_end_moves
            )
            points.extend(("segment", tail[0], None, tail[1], tail[2]) for tail in added)
            max_disp = max(
                via_disp,
                math.hypot(jdx, jdy),
                *(math.dist((ox, oy), (nx, ny)) for _u, _t, ox, oy, nx, ny in via_end_moves),
            )
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
                new_x=centre[0],
                new_y=centre[1],
                dx=centre[0] - at[0],
                dy=centre[1] - at[1],
                displacement_mm=max_disp,
                detail=(
                    f"moved via {via_disp:.4f}mm clear of {refs}, "
                    f"sliding [{jog.net}] jog {math.hypot(jdx, jdy):.4f}mm along its parallel runs"
                ),
                openings=list(openings),
                points=points,
                end_moves=all_end_moves,
                added_segments=added,
                extra_nets={jog.net},
            )
            if self._apply_via_relocate(action, result, dry_run) is None:
                return True
        return False

    def _via_elbow_or_tail_plan(
        self,
        joined: list[tuple[str, SExp, str, tuple[float, float]]],
        at: tuple[float, float],
        centre: tuple[float, float],
        size: float,
        net_name: str,
        openings: list[str],
        pads: dict,
        required: float,
        margin_mm: float,
        max_displacement: float,
        touched_by_action: set[str],
    ) -> (
        tuple[
            list[tuple[str, str, float, float, float, float]],
            list[tuple[float, float, float, float, float, str, int | str]],
            list[tuple[BaseGeometry, BaseGeometry, str]],
            list[tuple[tuple[float, float], tuple[float, float], frozenset[str]]],
            set[str],
        ]
        | None
    ):
        """Connect every attached approach to ``centre`` by sliding its elbow or adding an in-land tail."""
        from kicad_tools.router.quantize import _is_quantum_aligned, is_45_aligned
        from kicad_tools.validate.mask_copper import clean_own_pad_entry

        if not joined:
            return [], [], [], [], set()
        if any(uid in touched_by_action for uid, *_ in joined):
            return None

        buckets: list[
            tuple[str, tuple[float, float], list[tuple[str, SExp, str, tuple[float, float]]]]
        ] = []
        for item in joined:
            _uid, node, tag, _end = item
            layer_node = node.find("layer")
            layer = _bare(layer_node.get_first_atom()) if layer_node is not None else ""
            other = self._xy(node.find("end" if tag == "start" else "start"))
            if not layer or tag is None or other is None:
                return None
            placed = False
            for b_layer, b_other, members in buckets:
                if b_layer == layer and math.dist(other, b_other) < _TOLERANCE_MM:
                    members.append(item)
                    placed = True
                    break
            if not placed:
                buckets.append((layer, other, [item]))

        end_moves: list[tuple[str, str, float, float, float, float]] = []
        tails: list[tuple[float, float, float, float, float, str, int | str]] = []
        final_lines: list[tuple[BaseGeometry, BaseGeometry, str]] = []
        moved_points: list[tuple[tuple[float, float], tuple[float, float], frozenset[str]]] = []
        moved_uuids: set[str] = set()
        moved_tags: set[tuple[str, str]] = set()
        post_segments: list[tuple[tuple[float, float], tuple[float, float], float, str]] = []

        def record_end(
            uid: str, tag: str, old_pt: tuple[float, float], new_pt: tuple[float, float], layer: str
        ) -> bool:
            if (uid, tag) in moved_tags:
                return False
            moved_tags.add((uid, tag))
            moved_uuids.add(uid)
            end_moves.append((uid, tag, old_pt[0], old_pt[1], new_pt[0], new_pt[1]))
            moved_points.append((old_pt, new_pt, frozenset({layer})))
            return True

        for layer, rep_other, bucket in buckets:
            rep_uid, _rep_node, _rep_tag, rep_end = bucket[0]
            ax, ay = rep_end[0] - rep_other[0], rep_end[1] - rep_other[1]
            a_len = math.hypot(ax, ay)
            if a_len < _TOLERANCE_MM:
                return None

            # 1. Collinear slide along the approach's own line.
            new_vx, new_vy = centre[0] - rep_other[0], centre[1] - rep_other[1]
            new_vlen = math.hypot(new_vx, new_vy)
            if (
                new_vlen >= _MIN_RUN_MM
                and new_vx * ax + new_vy * ay > 0
                and abs(new_vx * ay - new_vy * ax) / a_len <= _RUN_AXIS_TOLERANCE_MM
                and math.dist(rep_end, centre) <= max_displacement + 1e-9
                and not any(self._locks.is_generator_owned(n) for _, n, *_ in bucket)
                and all(
                    (o := self._xy(n.find("end" if t == "start" else "start"))) is not None
                    and (
                        _is_quantum_aligned(centre[0] - o[0], centre[1] - o[1])
                        or is_45_aligned(centre[0] - o[0], centre[1] - o[1])
                    )
                    for _u, n, t, _e in bucket
                )
            ):
                for uid, node, tag, end_pt in bucket:
                    other_pt = self._xy(node.find("end" if tag == "start" else "start"))
                    w = _num(node.find("width"))
                    if (
                        other_pt is None
                        or w <= 0
                        or not record_end(uid, tag, end_pt, centre, layer)
                    ):
                        return None
                    final_lines.append(
                        (
                            LineString([other_pt, end_pt]).buffer(w / 2, quad_segs=16),
                            LineString([other_pt, centre]).buffer(w / 2, quad_segs=16),
                            layer,
                        )
                    )
                    post_segments.append((other_pt, centre, w, layer))
                continue

            # 2. Slide the approach's far elbow along its continuing run(s).
            bucket_uuids = {u for u, *_ in bucket}
            elbow_applied = False
            pad_covers_elbow = any(
                (
                    layer in p_layers
                    or "*.Cu" in p_layers
                    or ("F&B.Cu" in p_layers and layer in {"F.Cu", "B.Cu"})
                )
                and poly.buffer(_INSIDE_EPS_MM).contains(Point(rep_other))
                for poly, _c, p_layers, _p_net, _ref, _sh in pads.values()
            )
            if not pad_covers_elbow and not any(
                self._locks.is_generator_owned(n) for _, n, *_ in bucket
            ):
                junction_members, blocked = self._junction(layer, rep_other, rep_uid, net_name)
                if not blocked and not self._has_fixed_junction_contact(rep_other, layer, net_name):
                    runs: list[tuple[str, SExp, str, tuple[float, float], tuple[float, float]]] = []
                    valid_junction = True
                    for m_uid, m_node, m_tag in junction_members:
                        m_other = self._xy(m_node.find("end" if m_tag == "start" else "start"))
                        m_joint = self._xy(m_node.find(m_tag))
                        if m_other is None or m_joint is None or m_node.find("width") is None:
                            valid_junction = False
                            break
                        if math.dist(m_other, at) < _TOLERANCE_MM:
                            if m_uid not in bucket_uuids:
                                valid_junction = False
                                break
                            continue
                        if (
                            m_uid in touched_by_action
                            or self._locks.is_locked(m_node)
                            or self._locks.is_generator_owned(m_node)
                        ):
                            valid_junction = False
                            break
                        runs.append((m_uid, m_node, m_tag, m_joint, m_other))
                    if valid_junction and runs:
                        _, _, _, p0, o0 = runs[0]
                        vx0, vy0 = p0[0] - o0[0], p0[1] - o0[1]
                        v_len = math.hypot(vx0, vy0)
                        if v_len >= _TOLERANCE_MM:
                            ux, uy = vx0 / v_len, vy0 / v_len
                            det_v = vx0 * ay - vy0 * ax
                            if (
                                _on_45_grid(ux, uy)
                                and abs(det_v) >= 0.1 * v_len * a_len
                                and all(
                                    abs((pt[0] - ot[0]) * uy - (pt[1] - ot[1]) * ux)
                                    <= _RUN_AXIS_TOLERANCE_MM
                                    for _ru, _rn, _rt, pt, ot in runs
                                )
                            ):
                                dx_c = centre[0] - rep_other[0]
                                dy_c = centre[1] - rep_other[1]
                                t_v = (dx_c * ay - dy_c * ax) / det_v
                                k_a = (vx0 * dy_c - vy0 * dx_c) / det_v
                                shift = (t_v * vx0, t_v * vy0)
                                signed_s = shift[0] * ux + shift[1] * uy
                                if (
                                    math.hypot(*shift) <= max_displacement + 1e-9
                                    and k_a * a_len >= _MIN_RUN_MM
                                    and self._run_keeps_length(runs, (ux, uy), signed_s)
                                ):
                                    cand_arm_ok = True
                                    arm_records = []
                                    for uid, node, tag, end_pt in bucket:
                                        other_tag = "end" if tag == "start" else "start"
                                        orig_other = self._xy(node.find(other_tag))
                                        w = _num(node.find("width"))
                                        if orig_other is None or w <= 0:
                                            cand_arm_ok = False
                                            break
                                        new_other = (
                                            round(orig_other[0] + shift[0], 6),
                                            round(orig_other[1] + shift[1], 6),
                                        )
                                        if math.dist(new_other, centre) < _MIN_RUN_MM or not (
                                            _is_quantum_aligned(
                                                centre[0] - new_other[0],
                                                centre[1] - new_other[1],
                                            )
                                            or is_45_aligned(
                                                centre[0] - new_other[0],
                                                centre[1] - new_other[1],
                                            )
                                        ):
                                            cand_arm_ok = False
                                            break
                                        arm_records.append(
                                            (uid, tag, end_pt, other_tag, orig_other, new_other, w)
                                        )
                                    run_records = []
                                    if cand_arm_ok:
                                        for r_uid, r_node, r_tag, r_joint, r_far in runs:
                                            rw = _num(r_node.find("width"))
                                            new_rj = (
                                                round(r_joint[0] + shift[0], 6),
                                                round(r_joint[1] + shift[1], 6),
                                            )
                                            if (
                                                rw <= 0
                                                or math.dist(r_far, new_rj) < _MIN_RUN_MM
                                                or not (
                                                    _is_quantum_aligned(
                                                        new_rj[0] - r_far[0],
                                                        new_rj[1] - r_far[1],
                                                    )
                                                    or is_45_aligned(
                                                        new_rj[0] - r_far[0],
                                                        new_rj[1] - r_far[1],
                                                    )
                                                )
                                            ):
                                                cand_arm_ok = False
                                                break
                                            run_records.append(
                                                (r_uid, r_tag, r_joint, new_rj, r_far, rw)
                                            )
                                    if cand_arm_ok:
                                        for (
                                            uid,
                                            tag,
                                            end_pt,
                                            other_tag,
                                            orig_other,
                                            new_other,
                                            w,
                                        ) in arm_records:
                                            if not record_end(
                                                uid, tag, end_pt, centre, layer
                                            ) or not record_end(
                                                uid, other_tag, orig_other, new_other, layer
                                            ):
                                                return None
                                            final_lines.append(
                                                (
                                                    LineString([orig_other, end_pt]).buffer(
                                                        w / 2, quad_segs=16
                                                    ),
                                                    LineString([new_other, centre]).buffer(
                                                        w / 2, quad_segs=16
                                                    ),
                                                    layer,
                                                )
                                            )
                                            post_segments.append((new_other, centre, w, layer))
                                        for r_uid, r_tag, r_joint, new_rj, r_far, rw in run_records:
                                            if not record_end(r_uid, r_tag, r_joint, new_rj, layer):
                                                return None
                                            final_lines.append(
                                                (
                                                    LineString([r_far, r_joint]).buffer(
                                                        rw / 2, quad_segs=16
                                                    ),
                                                    LineString([r_far, new_rj]).buffer(
                                                        rw / 2, quad_segs=16
                                                    ),
                                                    layer,
                                                )
                                            )
                                            post_segments.append((r_far, new_rj, rw, layer))
                                        elbow_applied = True
            if elbow_applied:
                continue

            # 3. Fallback: kept-line slide + aligned in-land tail. It moves the
            # approach's own endpoint, so a generator-owned member is off limits.
            if any(self._locks.is_generator_owned(n) for _, n, *_ in bucket):
                return None
            tail_plan = self._via_tail_plan(bucket, centre, size, max_displacement)
            if tail_plan is None:
                return None
            b_ends, b_tails, b_lines = tail_plan
            for b_tail in b_tails:
                if not _is_quantum_aligned(
                    round(b_tail[2], 6) - round(b_tail[0], 6),
                    round(b_tail[3], 6) - round(b_tail[1], 6),
                ):
                    return None
                post_segments.append(
                    ((b_tail[0], b_tail[1]), (b_tail[2], b_tail[3]), b_tail[4], b_tail[5])
                )
            for uid, tag, ox, oy, nx, ny in b_ends:
                tail_node = self._segment_by_uuid(uid)
                other_pt = (
                    self._xy(tail_node.find("end" if tag == "start" else "start"))
                    if tail_node is not None
                    else None
                )
                w = _num(tail_node.find("width")) if tail_node is not None else 0.0
                if other_pt is None or not record_end(uid, tag, (ox, oy), (nx, ny), layer):
                    return None
                post_segments.append((other_pt, (nx, ny), w, layer))
            tails.extend(b_tails)
            final_lines.extend(b_lines)

        for seg_s, seg_e, seg_w, seg_layer in post_segments:
            seg_copper = LineString([seg_s, seg_e]).buffer(seg_w / 2, quad_segs=16)
            for op_uid in openings:
                aperture = self._pad_mask_openings.get((op_uid, seg_layer))
                pad_poly, _c, p_layers, _p_net, _ref, shape = pads[op_uid]
                if aperture is not None:
                    gap = seg_copper.distance(aperture.geometry)
                    if gap > 0:
                        if gap < required + margin_mm - 1e-9:
                            return None
                    elif not clean_own_pad_entry(
                        aperture.geometry,
                        seg_copper,
                        pad_poly,
                        f"pad:{shape}",
                        seg_w,
                        (seg_s, seg_e),
                        clearance_mm=required,
                        uncertainty_mm=_MASK_UNCERTAINTY_MM,
                        margin_mm=aperture.margin_mm,
                    ):
                        return None
                elif seg_layer in p_layers or "*.Cu" in p_layers:
                    grown = pad_poly.buffer(_INSIDE_EPS_MM)
                    if not (grown.contains(Point(seg_s)) or grown.contains(Point(seg_e))):
                        if seg_copper.distance(pad_poly) < required + margin_mm - 1e-9:
                            return None

        old_disc = Point(at).buffer(size / 2, quad_segs=32)
        new_disc = Point(centre).buffer(size / 2, quad_segs=32)
        moving_all = moved_uuids | {u for u, *_ in joined}
        for lyr in {line_layer for _o, _n, line_layer in final_lines}:
            layer_moves = [m for m in final_lines if m[2] == lyr]
            layer_moves.append((old_disc, new_disc, lyr))
            if self._loses_same_net_contact(
                layer_moves,
                lyr,
                net_name,
                moving_all,
                pads,
                protect_ends=True,
            ):
                return None

        return end_moves, tails, final_lines, moved_points, moved_uuids

    def _parse_foreign_jog(
        self, seg_uuid: str, pads: dict, touched: set[str]
    ) -> _ForeignJog | None:
        """Identify a foreign straight jog bounded at both ends by parallel runs."""
        from kicad_tools.router.quantize import _is_quantum_aligned, is_45_aligned

        seg = self._segment_by_uuid(seg_uuid)
        if seg is None or seg_uuid in touched:
            return None
        net = self._net_name(seg)
        if not net or self._locks.is_locked(seg) or self._locks.is_generator_owned(seg):
            return None
        layer_node, width_node = seg.find("layer"), seg.find("width")
        if layer_node is None or width_node is None:
            return None
        layer = _bare(layer_node.get_first_atom())
        width = _num(width_node)
        start, end = self._xy(seg.find("start")), self._xy(seg.find("end"))
        if not layer or width <= 0 or start is None or end is None:
            return None
        if math.dist(start, end) < _TOLERANCE_MM or not (
            _is_quantum_aligned(end[0] - start[0], end[1] - start[1])
            or is_45_aligned(end[0] - start[0], end[1] - start[1])
        ):
            return None
        for poly, _c, p_layers, _p_net, _ref, _sh in pads.values():
            if (
                layer in p_layers
                or "*.Cu" in p_layers
                or ("F&B.Cu" in p_layers and layer in {"F.Cu", "B.Cu"})
            ):
                grown = poly.buffer(_INSIDE_EPS_MM)
                if grown.contains(Point(start)) or grown.contains(Point(end)):
                    return None

        copies_dict: dict[str, SExp] = {seg_uuid: seg}
        raw_runs: list[tuple[str, SExp, str, tuple[float, float], tuple[float, float]]] = []
        sides: list[int] = []
        for point, opposite in ((start, end), (end, start)):
            members, blocked = self._junction(layer, point, seg_uuid, net)
            if blocked or self._has_fixed_junction_contact(point, layer, net):
                return None
            count = 0
            for uid, node, tag in members:
                other = self._xy(node.find("end" if tag == "start" else "start"))
                if other is None or node.find("width") is None:
                    return None
                if math.dist(other, opposite) < _TOLERANCE_MM:
                    copies_dict[uid] = node
                    continue
                # Record the run's own endpoint, not the jog's: they agree only
                # to within the junction tolerance, and the move/undo record
                # must restore the coordinate the file actually holds.
                run_joint = self._xy(node.find(tag))
                if run_joint is None:
                    return None
                raw_runs.append((uid, node, tag, run_joint, other))
                count += 1
            sides.append(count)
        if min(sides) == 0:
            return None
        if set(copies_dict) & touched or any(
            self._locks.is_locked(node) or self._locks.is_generator_owned(node)
            for node in copies_dict.values()
        ):
            return None

        _, _, _, first_pt, first_other = raw_runs[0]
        axis_len = math.dist(first_pt, first_other)
        if axis_len < _TOLERANCE_MM:
            return None
        ux = (first_pt[0] - first_other[0]) / axis_len
        uy = (first_pt[1] - first_other[1]) / axis_len
        if not _on_45_grid(ux, uy):
            return None
        jx, jy = end[0] - start[0], end[1] - start[1]
        if abs(jx * uy - jy * ux) < 0.1 * math.hypot(jx, jy):
            return None

        low, high = -math.inf, math.inf
        runs_out: list[tuple[str, str, tuple[float, float], tuple[float, float], float]] = []
        for uid, node, tag, joint_pt, far_pt in raw_runs:
            if (
                uid in touched
                or self._locks.is_locked(node)
                or self._locks.is_generator_owned(node)
            ):
                return None
            vx, vy = joint_pt[0] - far_pt[0], joint_pt[1] - far_pt[1]
            if abs(vx * uy - vy * ux) > _RUN_AXIS_TOLERANCE_MM:
                return None
            rw = _num(node.find("width"))
            if rw <= 0:
                return None
            along = vx * ux + vy * uy
            if along > 0:
                low = max(low, _MIN_RUN_MM - along)
            else:
                high = min(high, -along - _MIN_RUN_MM)
            runs_out.append((uid, tag, joint_pt, far_pt, rw))
        if not low < 0 < high:
            return None

        copies_out: list[tuple[str, tuple[float, float], tuple[float, float], float]] = []
        for uid, node in copies_dict.items():
            cs, ce = self._xy(node.find("start")), self._xy(node.find("end"))
            cw = _num(node.find("width"))
            if cs is None or ce is None or cw <= 0:
                return None
            copies_out.append((uid, cs, ce, cw))

        return _ForeignJog(
            net=net,
            layer=layer,
            axis=(ux, uy),
            low=low,
            high=high,
            copies=tuple(copies_out),
            runs=tuple(runs_out),
        )

    def _joint_via_jog_engine_and_connectivity_ok(
        self,
        at: tuple[float, float],
        centre: tuple[float, float],
        size: float,
        via_uuid: str,
        end_moves: list[tuple[str, str, float, float, float, float]],
        tails: list[tuple[float, float, float, float, float, str, int | str]],
        nets: set[str],
        pads: dict,
        design_rules: DesignRules | None,
    ) -> bool:
        """Verify shared clearance engine and net contact graphs on the temporarily edited doc."""
        from kicad_tools.router.primitives import _make_uuid
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.connectivity import ConnectivityValidator

        pcb_before = PCB(self.doc)
        before_graphs = {}
        for name in nets:
            net_obj = pcb_before.get_net_by_name(name)
            if net_obj is None:
                return False
            before_graphs[name] = (
                net_obj.number,
                ConnectivityValidator(pcb_before)._contact_graph(net_obj.number),
            )

        via_node = self._via_by_uuid(via_uuid)
        at_node = None if via_node is None else via_node.find("at")
        if at_node is None:
            return False

        saved_ends: list[tuple[SExp, list]] = []
        added_nodes: list[SExp] = []
        old_via_children = list(at_node.children)
        try:
            for uid, tag, _ox, _oy, nx, ny in end_moves:
                seg_node = self._segment_by_uuid(uid)
                end_node = None if seg_node is None else seg_node.find(tag)
                if end_node is None:
                    return False
                saved_ends.append((end_node, list(end_node.children)))
                end_node.set_value(0, round(nx, 6))
                end_node.set_value(1, round(ny, 6))
            for tail in tails:
                node = self._tail_segment_node((_make_uuid(), *tail))
                self._insert_segment_after_last(node)
                added_nodes.append(node)

            # Check shared clearance engine before updating via_node's position
            # so _target_clearance_reason locates target_via at (at[0], at[1]).
            if design_rules is not None:
                reason = self._target_clearance_reason(
                    at[0],
                    at[1],
                    centre[0],
                    centre[1],
                    design_rules,
                    before_refill=self._will_refill,
                )
                if reason is not None and self._fail_closed_reason_is_local(
                    reason, centre, size, pads
                ):
                    return False

            at_node.set_value(0, round(centre[0], 4))
            at_node.set_value(1, round(centre[1], 4))
            validator = ConnectivityValidator(PCB(self.doc))
            for name, (net_num, prev_graph) in before_graphs.items():
                if not validator._contact_graph(net_num).preserves_connections(prev_graph):
                    return False
            return True
        finally:
            at_node.children[:] = old_via_children
            for node in added_nodes:
                if node in self.doc.children:
                    self.doc.children.remove(node)
            for end_node, children in saved_ends:
                end_node.children[:] = children

    def _pad_bond_bridges(
        self,
        via_node: SExp,
        via_uuid: str,
        opening: str,
        pads: dict,
        at: tuple[float, float],
        size: float,
        gap: float,
        required: float,
        max_displacement: float,
        design_rules: DesignRules,
    ) -> Iterator[tuple[tuple[float, float], _ViaLead]]:
        """Find straight entries from an isolated SMD pad to a tented via's new centre."""
        from kicad_tools.router.io import authored_net_widths
        from kicad_tools.router.rules import net_class_map_from_path
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.mask_copper import _within_own_pad
        from kicad_tools.validate.rules.silkscreen import _iter_silk_geometries, _via_is_tented

        owner, (cx, cy), layers, net, _, shape = pads[opening]
        faces = set(layers) & {"F.Cu", "B.Cu"}
        if len(faces) != 1 or owner.covers(Point(at)) or owner.distance(Point(at)) >= size / 2:
            return
        layer = faces.pop()
        mask_opening = self._pad_mask_openings.get((opening, layer))
        if mask_opening is None:
            return
        aperture = mask_opening.geometry
        if not aperture.intersects(Point(at).buffer(size / 2)):
            return
        pcb = PCB(self.doc)
        via = next((v for v in pcb.vias if v.uuid == via_uuid), None)
        if via is None or via.via_type not in (None, "through") or via.drill <= 0:
            return
        if any(not _via_is_tented(via, pcb.setup, side) for side in ("F", "B")):
            return
        net_node = via_node.find("net")
        net_value = net_node.get_first_atom() if net_node is not None else None
        if not isinstance(net_value, (int, str)):
            return
        sidecar = self._project_pcb_path.with_name("net_class_map.json")
        try:
            classes = (
                net_class_map_from_path(sidecar, pcb_path=self._project_pcb_path)
                if sidecar.exists()
                else {}
            )
        except (OSError, ValueError, TypeError):
            return
        if net in classes:
            trace_width = classes[net].neck_trace_width or classes[net].trace_width
        else:
            trace_width = authored_net_widths(self._project_pcb_path, [net]).get(net, 0.0)
        width = max(design_rules.min_trace_width_mm, trace_width)
        core = owner.buffer(-width / 2 - _MASK_UNCERTAINTY_MM)
        if core.is_empty:
            return
        # Silk is in board-relative coordinates. Only geometry within the via's
        # maximum reach can intersect the moved drill.
        local = Point(via.position)
        silk_gap = design_rules.min_silk_to_hole_clearance_mm or 0.0
        silk = [
            translate(g, *pcb.board_origin)
            for _, g, *_ in _iter_silk_geometries(pcb)
            if g.distance(local) <= max_displacement + via.drill / 2 + silk_gap
        ]
        for push in self._via_alternate_candidates(
            at, size, [opening], pads, gap, max_displacement, None, preferred_gap=gap
        ):
            x, y = round(at[0] + push[0], 4), round(at[1] + push[1], 4)
            seat = Point(x, y)
            if aperture.distance(seat) - size / 2 < gap - 1e-6 or any(
                g.distance(seat) <= via.drill / 2 + silk_gap for g in silk
            ):
                continue
            # Intersections of allowed track directions with the axes through
            # the pad centre. The whole end cap must fit inside its land.
            anchors = {(cx, y), (x, cy)}
            for sign in (-1, 1):
                anchors.update(((cx, y + sign * (cx - x)), (x + sign * (cy - y), cy)))
            for ax, ay in sorted(anchors, key=lambda p: (math.dist(p, (cx, cy)), p)):
                ax, ay = round(ax, 6), round(ay, 6)
                if not core.covers(Point(ax, ay)):
                    continue
                ends = [(ax, ay), (x, y)]
                trace = LineString(ends).buffer(width / 2, quad_segs=32)
                clean, _, _ = _within_own_pad(
                    aperture,
                    trace,
                    owner,
                    {"kind": f"pad:{shape}"},
                    {"kind": "segment", "width_mm": width, "endpoints_mm": ends},
                    0.0,
                    required,
                    _MASK_UNCERTAINTY_MM,
                )
                if clean:
                    yield push, (ax, ay, x, y, width, layer, net_value)

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
        *,
        preferred_gap: float,
    ) -> list[tuple[float, float]]:
        """Bounded via displacements that clear every opening by ``target_gap``.

        Include the primary push and a polar grid up to the caller's maximum.
        Measure disc-to-opening clearance at the rounded centre. Try candidates
        meeting the preferred gap first, shortest first within each tier.
        """
        clears_preferred: dict[tuple[float, float], bool] = {}

        def consider(dx: float, dy: float) -> None:
            centre = (round(at[0] + dx, 4), round(at[1] + dy, 4))
            key = (round(centre[0] - at[0], 4), round(centre[1] - at[1], 4))
            if key in clears_preferred or math.hypot(*key) > max_displacement + 1e-9:
                return
            point = Point(centre)
            gap = min(pads[opening][0].distance(point) for opening in openings) - size / 2
            if gap < target_gap - 1e-9:
                return
            clears_preferred[key] = gap >= preferred_gap - 1e-9

        if primary is not None:
            consider(*primary)
        for step in range(1, int(max_displacement / _VIA_SEARCH_STEP_MM) + 1):
            magnitude = round(step * _VIA_SEARCH_STEP_MM, 4)
            for index in range(_VIA_SEARCH_DIRECTIONS):
                angle = math.tau * index / _VIA_SEARCH_DIRECTIONS
                consider(magnitude * math.cos(angle), magnitude * math.sin(angle))
        return sorted(
            clears_preferred,
            key=lambda d: (
                not clears_preferred[d],
                round(math.hypot(*d), 4),
                round(math.atan2(d[1], d[0]), 6),
            ),
        )

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
        *,
        bridge: _ViaLead | None = None,
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
        if bridge is not None:
            ax, ay, vx, vy, width, layer, _net = bridge
            trace = LineString([(ax, ay), (vx, vy)]).buffer(width / 2, quad_segs=32)
            tails.append(bridge)
            final_lines.append((copper, trace, layer))
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
            if bridge is not None and self._will_refill:
                reason = self._target_clearance_reason(
                    *at, *new_at, design_rules, before_refill=True
                )
            else:
                reason = self._target_clearance_reason(*at, *new_at, design_rules)
            if reason is not None and self._fail_closed_reason_is_local(reason, new_at, size, pads):
                return "would_violate_clearance", reason
        for span_layer in sorted(span_layers):
            layer_moves = [m for m in moves if m[2] == span_layer] if bridge else moves
            refusal = self._copper_floor_reason(
                layer_moves, span_layer, net_name, via_uuid, pads, clearance_mm
            )
            if refusal is not None:
                return refusal
            if bridge:
                kept = unary_union([new for _, new, _ in layer_moves])
                if self._loses_same_net_contact(
                    [(copper, kept, span_layer)],
                    span_layer,
                    net_name,
                    {via_uuid},
                    pads,
                    protect_ends=True,
                ):
                    return "would_break_same_net_contact", ""
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
            action="bridge_via" if bridge else "relocate_clear",
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
            detail=f"bridged {refs} to the relocated via on {bridge[5]}"
            if bridge
            else (
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
            targets: list[tuple[SExp, float, float, float, float]] = []
            for index, (kind, uuid, tag, old_x, old_y) in enumerate(action.points):
                node = self._segment_by_uuid(uuid) if kind == "segment" else self._via_by_uuid(uuid)
                end_node = None if node is None else node.find(tag) if tag else node.find("at")
                if end_node is None:
                    return "malformed_segment", ""
                targets.append((end_node, old_x, old_y, *action.shift(index)))
        result.actions.append(action)
        if not dry_run:
            for end_node, old_x, old_y, dx, dy in targets:
                end_node.set_value(0, round(old_x + dx, 4))
                end_node.set_value(1, round(old_y + dy, 4))
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
        *,
        pairwise_rules: BoardClearanceRules | None = None,
        skip_uuids: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[str, str] | None:
        """Decline a move that crowds other-net copper below the floor."""
        from kicad_tools.router.board_clearance_rules import ItemProps

        base_required = clearance_mm if clearance_mm is not None else 0.0
        track_props = ItemProps("Track", net_name) if pairwise_rules is not None else None
        for obstacle, obs_kind, obs_net in self._other_net_obstacles_with_meta(
            layer, net_name, skip_uuid, pads, skip_uuids=skip_uuids
        ):
            required = base_required
            if pairwise_rules is not None and track_props is not None:
                obs_props = ItemProps(obs_kind, obs_net)
                required = max(
                    required,
                    pairwise_rules.clearance(track_props, obs_props, layer),
                    pairwise_rules.physical_clearance(track_props, obs_props, layer),
                )
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
        if isinstance(action, MaskBridgeAction):
            restored = self._undo_bridge(action)
        elif isinstance(action, MaskViaRelocateAction):
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
        for index, (kind, uuid, tag, old_x, old_y) in enumerate(action.points):
            node = self._segment_by_uuid(uuid) if kind == "segment" else self._via_by_uuid(uuid)
            end_node = None if node is None else node.find(tag) if tag else node.find("at")
            xy = self._xy(end_node)
            dx, dy = action.shift(index)
            if (
                end_node is None
                or xy is None
                or math.dist(xy, (round(old_x + dx, 4), round(old_y + dy, 4))) > _TOLERANCE_MM
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
