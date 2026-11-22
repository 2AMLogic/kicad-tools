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
  than the policy clearance.  The repair translates the whole conductor away
  from the opening until the modelled distance clears the policy clearance
  plus a margin: a segment moves with every same-net end joined to it, a via
  drags every same-net track end anchored at it.  One grouped edit per
  conductor must clear **every** opening the conductor has findings against,
  and a candidate that would crowd another opening (or whose joined ends
  would leave a pad they terminate in) is declined.

Everything else (different net, pour, arc, overlap of any kind, a straddle,
both ends inside, a junction anchoring a segment end, an exotic pad shape,
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from shapely.affinity import rotate, translate  # type: ignore[import-untyped]
from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import nearest_points, unary_union  # type: ignore[import-untyped]

from ..sexp import SExp
from .net_compat import resolve_net_atom
from .repair_copper_gap import CopperGapRepairer
from .violation import DRCViolation, ViolationType

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules

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


@dataclass
class MaskRepairAction:
    """One applied (or, in a dry run, predicted) segment-end move."""

    action: str  # always "recentre"
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

    def __str__(self) -> str:
        return (
            f"recentre: track [{self.net_name}] end ({self.old_x:.4f}, {self.old_y:.4f}) -> "
            f"({self.new_x:.4f}, {self.new_y:.4f}) on pad {self.pad_ref} - {self.detail}"
        )


@dataclass
class MaskNudgeAction:
    """One applied (or, in a dry run, predicted) pass-through translation.

    A pass-through conductor (segment or via) and every same-net end that
    moves with it translate by the same ``(dx, dy)``, so one action records
    the whole grouped edit: ``points`` names every moved point with its
    pre-move coordinates (a via's own point carries ``tag=None``), and
    ``openings`` lists every opening uuid the move was computed to clear.
    ``old_x``/``new_x`` carry a reference point (the via centre or the
    segment midpoint) for reporting.
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

    def __str__(self) -> str:
        return (
            f"nudge_clear: {self.kind} [{self.net_name}] ({self.old_x:.4f}, {self.old_y:.4f}) -> "
            f"({self.new_x:.4f}, {self.new_y:.4f}) clear of {self.pad_ref} - {self.detail}"
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


class MaskCopperRepairer(CopperGapRepairer):
    """Re-centres own-pad track entries and nudges same-net pass-throughs.

    Subclasses :class:`CopperGapRepairer` (issue #9896) so a via nudge reuses
    the same machinery the copper-gap via repair validates with: the shared
    clearance engine gate for the destination (``_target_clearance_reason``,
    copper *and* drill-to-drill), the dragged-track clearance gate
    (``_dragged_track_reason``), anchored-end collection with locked-track
    refusal (``_anchored_ends``) and the endpoint update helper.
    """

    def __init__(self, pcb_path: str | Path):
        # ``DrillClearanceRepairer.__init__`` parses the file and builds the
        # same ``nets`` / ``net_names`` indexes this class historically did.
        super().__init__(pcb_path)

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
        _, name = resolve_net_atom(None if atom is None else str(atom), self.nets, self.net_names)
        return name or ""

    @staticmethod
    def _xy(node: SExp | None) -> tuple[float, float] | None:
        if node is None:
            return None
        atoms = node.get_atoms()
        if len(atoms) < 2:
            return None
        return float(atoms[0]), float(atoms[1])

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
            for pad in fp.pads:
                lx, ly = pad.position
                rx, ry = rotate_pad_offset(lx, ly, fp.rotation)
                cx = fp.position[0] + rx + ox
                cy = fp.position[1] + ry + oy
                poly = self._pad_polygon(pad, cx, cy)
                if poly is None:
                    # Keep the pad as an obstacle/owner marker with its box so a
                    # clearance check still sees it, but flag the shape.
                    w, h = pad.size
                    poly = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                    shape = "unsupported"
                else:
                    shape = pad.shape
                if pad.uuid:
                    pads[_bare(pad.uuid)] = (
                        poly,
                        (cx, cy),
                        list(pad.layers),
                        pad.net_name or "",
                        f"{fp.reference}/{pad.number}",
                        shape,
                    )
        return pads

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
            if layer in layers or "*.Cu" in layers:
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
                if self._net_name(node) != net_name or node.find("locked") is not None:
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
            reason = self._repair_one(
                seg, conductor, openings[0], pads, result, max_displacement, dry_run, clearance_mm
            )
            if reason is not None:
                result.skip(reason, violation, conductor)
            else:
                moved.add(conductor)
        for action in result.actions:
            if isinstance(action, MaskRepairAction):
                touched_by_action.add(action.uuid)
                touched_by_action.update(uuid for uuid, _tag in action.joined)
            else:
                touched_by_action.update(point[1] for point in action.points)

        for conductor, members in pass_throughs.items():
            if conductor in moved:
                result.skip("segment_already_moved", members[0], conductor)
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
        if seg.find("locked") is not None or "locked" in [str(a) for a in seg.get_atoms()]:
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
            return self._nudge_segment_clear(
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
        """Translate a whole pass-through segment clear of its openings."""
        entries = [pads[opening] for opening in openings]
        if any(entry[5] == "unsupported" for entry in entries):
            return "unverified_pad_shape", ""
        net_name = self._net_name(seg)
        if not net_name or any(entry[3] != net_name for entry in entries):
            return "not_same_net", ""
        if seg.find("locked") is not None or "locked" in [str(a) for a in seg.get_atoms()]:
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
        for point in (start, end):
            members_at, blocked = self._junction(layer, point, seg_uuid, net_name)
            if blocked:
                return "junction_at_moved_end", ""
            for member in members_at:
                if member not in joined:
                    joined.append(member)

        refusal = self._opening_model_refusal(pairs, copper, pads, seg_uuid in touched_by_action)
        if refusal is not None:
            return refusal

        push = self._push_clear_of_openings(
            copper, [(pads[o][0], pads[o][1]) for o in openings], required + margin_mm
        )
        if push is None:
            return "infeasible", ""
        dx, dy = push
        displacement = math.hypot(dx, dy)
        if displacement < _TOLERANCE_MM / 2:
            return "already_cleared", ""
        if displacement > max_displacement:
            return "exceeds_max_displacement", f"needs {displacement:.4f}mm"

        new_start = (start[0] + dx, start[1] + dy)
        new_end = (end[0] + dx, end[1] + dy)
        moves: list[tuple[BaseGeometry, BaseGeometry, str]] = [
            (
                copper,
                LineString([new_start, new_end]).buffer(width / 2, quad_segs=16),
                layer,
            )
        ]
        points: list[tuple[str, str, str | None, float, float]] = [
            ("segment", seg_uuid, "start", start[0], start[1]),
            ("segment", seg_uuid, "end", end[0], end[1]),
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
            old_x=(start[0] + end[0]) / 2,
            old_y=(start[1] + end[1]) / 2,
            new_x=(start[0] + end[0]) / 2 + dx,
            new_y=(start[1] + end[1]) / 2 + dy,
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
        """Move a pass-through via (dragging its anchored track ends) clear."""
        entries = [pads[opening] for opening in openings]
        if any(entry[5] == "unsupported" for entry in entries):
            return "unverified_pad_shape", ""
        net_name = self._net_name(via_node)
        if not net_name or any(entry[3] != net_name for entry in entries):
            return "not_same_net", ""
        if via_node.find("locked") is not None:
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

        push = self._push_clear_of_openings(
            copper, [(pads[o][0], pads[o][1]) for o in openings], required + margin_mm
        )
        if push is None:
            return "infeasible", ""
        dx, dy = push
        displacement = math.hypot(dx, dy)
        if displacement < _TOLERANCE_MM / 2:
            return "already_cleared", ""
        if displacement > max_displacement:
            return "exceeds_max_displacement", f"needs {displacement:.4f}mm"

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
        if isinstance(action, MaskNudgeAction):
            return self._undo_nudge(action)
        segments = self._segment_nodes()
        targets = [(action.uuid, None), *((u, t) for u, t in action.joined)]
        restore: list[SExp] = []
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
                    restore.append(end_node)
                    break
            else:
                return False
        for end_node in restore:
            end_node.set_value(0, round(action.old_x, 4))
            end_node.set_value(1, round(action.old_y, 4))
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
            for kind, uuid, _tag, _old_x, _old_y in action.points:
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
        for uuid in (action.uuid, *(u for u, _ in action.joined)):
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
