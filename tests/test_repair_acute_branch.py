"""The acute-branch repairer (Issue #10732): the edits ``kct fix-drc --only acute-branch`` makes.

Each case is a small two-layer board with one carrier and its taps in the
shapes Board 03 carries, written to disk because the repairer reads a file and
rebuilds its working board from the kept edits.  The Board 03 cases at the end
run the same planner on the committed copper, with no KiCad; the saved-board
half of the transaction (native DRC, the mask assessment, the refill) is
exercised by ``tests/test_fix_drc_acute_branch.py`` with stand-ins.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_tools.drc.repair_acute_branch import AcuteBranchRepairer
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.redundant_copper import RedundantCopperRule
from kicad_tools.validate.rules.sharp_trace_corner import SharpTraceCornerRule
from kicad_tools.validate.violations import DRCViolation as Finding

Point = tuple[float, float]
BOARD_DIR = Path(__file__).resolve().parent.parent / "boards" / "03-usb-joystick"
BOARD = BOARD_DIR / "output" / "usb_joystick_routed.kicad_pcb"


@dataclass(frozen=True)
class _Seg:
    start: Point
    end: Point
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 1
    locked: bool = False


@dataclass(frozen=True)
class _Via:
    at: Point
    net_number: int = 1


def _write(tmp_path: Path, items: list, name: str = "board.kicad_pcb") -> Path:
    """A two-layer board holding ``items``, serialized once by the parser itself
    so an unchanged board then round-trips byte for byte."""
    body = []
    for index, item in enumerate(items):
        if isinstance(item, _Seg):
            (ax, ay), (bx, by) = item.start, item.end
            locked = " (locked yes)" if item.locked else ""
            body.append(
                f"  (segment (start {ax} {ay}) (end {bx} {by}) (width {item.width}) "
                f'(layer "{item.layer}") (net {item.net_number}){locked} '
                f'(uuid "s{index:04d}-0000-4000-8000-000000000000"))\n'
            )
        else:
            x, y = item.at
            body.append(
                f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
                f'(net {item.net_number} "SIG"))\n'
            )
    path = tmp_path / name
    path.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        '  (net 0 "") (net 1 "SIG") (net 2 "OTHER")\n' + "".join(body) + ")\n"
    )
    PCB.load(path).save(path)
    return path


def _repairer(path: Path) -> AcuteBranchRepairer:
    return AcuteBranchRepairer(path, rule=RedundantCopperRule())


def _sharp(pcb: PCB) -> list:
    return SharpTraceCornerRule().check(pcb, None).violations


def _at(finding) -> Point:
    """A finding's vertex as the board wrote it (the rule projects it mid-span)."""
    x, y = finding.location
    return (round(x, 6), round(y, 6))


def _uuid_of(pcb: PCB, seg: _Seg) -> str:
    return next(
        s.uuid
        for s in pcb.segments
        if s.start == seg.start and s.end == seg.end and s.width == seg.width
    )


def _tap_path(pcb: PCB, anchor: Point, carrier: _Seg) -> list[Point]:
    """The SIG tracks' centreline from ``anchor`` to where it meets ``carrier``."""
    (ax, ay), (bx, by) = carrier.start, carrier.end
    length = math.hypot(bx - ax, by - ay)

    def on_carrier(point: Point) -> bool:
        return abs((point[0] - ax) * (by - ay) - (point[1] - ay) * (bx - ax)) <= 1e-6 * length

    body = {(carrier.start, carrier.end), (carrier.end, carrier.start)}
    segs = [
        s
        for s in pcb.segments
        if s.net_number == 1 and s.layer == "F.Cu" and (s.start, s.end) not in body
    ]
    path = [anchor]
    point = anchor
    while not on_carrier(point):
        seg = next((s for s in segs if point in (s.start, s.end)), None)
        assert seg is not None, f"the tap from {anchor} does not reach the carrier"
        segs.remove(seg)
        point = seg.end if math.dist(seg.start, point) < 1e-6 else seg.start
        path.append(point)
    return path


def _right_angle_entry(path: list[Point], anchor: Point, carrier: _Seg) -> None:
    """The tap keeps its anchor and meets the carrier line perpendicular, inside it."""
    assert path[0] == anchor
    (ax, ay), (bx, by) = carrier.start, carrier.end
    landing = path[-1]
    along = math.dist((ax, ay), (bx, by))
    ux, uy = (bx - ax) / along, (by - ay) / along
    across = abs((landing[0] - ax) * uy - (landing[1] - ay) * ux)
    assert across < 1e-6  # on the carrier's centreline
    assert 0 < math.dist((ax, ay), landing) < along  # inside the carrier's extent
    last = (landing[0] - path[-2][0], landing[1] - path[-2][1])
    assert abs(last[0] * ux + last[1] * uy) < 1e-3 * math.hypot(*last)  # perpendicular


# --- unsplit carrier -------------------------------------------------------


def test_tap_on_an_unsplit_carrier_meets_it_at_a_right_angle(tmp_path):
    """Board 03 [BTN3 B.Cu]: a 45-degree tap from a via into one straight carrier.

    The carrier is untouched; the tap keeps its anchor, width, net and layer
    and turns onto the perpendicular for its last leg.
    """
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    tap = _Seg((31.2, 31.6), (33.2, 33.6))
    path = _write(tmp_path, [carrier, tap, _Via((31.2, 31.6))])
    before = PCB.load(path)
    assert [(v.nets[0], v.location) for v in _sharp(before)] == [("SIG", (33.2, 33.6))]
    carrier_uuid, tap_uuid = _uuid_of(before, carrier), _uuid_of(before, tap)

    repairer = _repairer(path)
    result = repairer.plan(_sharp(before))
    assert result.unrepaired == []
    (edit,) = result.edits
    assert edit.kind == "rewrite_tap" and edit.rule_id == "sharp_trace_corner"
    assert edit.witnesses == (tap_uuid,) and edit.uuid == tap_uuid
    assert [r.uuid for r in edit.removed] == [tap_uuid]
    assert len(edit.added) == 2 and {a.width for a in edit.added} == {0.2}

    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert _sharp(saved) == []
    kept = next(s for s in saved.segments if s.uuid == carrier_uuid)
    assert (kept.start, kept.end, kept.width) == (carrier.start, carrier.end, 0.2)
    assert tap_uuid not in {s.uuid for s in saved.segments}
    new = [s for s in saved.segments if s.uuid != carrier_uuid]
    assert {(s.net_name, s.layer, s.width) for s in new} == {("SIG", "F.Cu", 0.2)}
    _right_angle_entry(_tap_path(saved, (31.2, 31.6), carrier), (31.2, 31.6), carrier)


def test_a_dogleg_keeps_the_taps_bearing_and_lands_as_near_the_foot_as_it_can(tmp_path):
    """The landing is the one nearest the anchor's perpendicular foot that stays
    inside the carrier by half the tap's width: (33.2, 32.9), not a hand-picked spot."""
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    tap = _Seg((31.2, 31.6), (33.2, 33.6))
    path = _write(tmp_path, [carrier, tap, _Via((31.2, 31.6))])
    repairer = _repairer(path)
    (edit,) = repairer.plan(_sharp(PCB.load(path))).edits
    assert edit.legs == ((31.2, 31.6), (32.5, 32.9), (33.2, 32.9))


def test_straight_entry_when_the_anchor_lies_over_the_carrier(tmp_path):
    """Board 03 [RESET B.Cu]: the foot of the anchor is on the carrier, so the
    tap is one perpendicular leg from the anchor."""
    carrier = _Seg((34.0, 25.6), (58.4, 25.6))
    tap = _Seg((44.0, 25.2), (44.4, 25.6))
    path = _write(tmp_path, [carrier, tap, _Via((44.0, 25.2))])
    repairer = _repairer(path)
    result = repairer.plan(_sharp(PCB.load(path)))
    (edit,) = result.edits
    assert edit.legs == ((44.0, 25.2), (44.0, 25.6))
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    assert _sharp(PCB.load(out)) == []


# --- split carrier ---------------------------------------------------------


@pytest.mark.parametrize("landing", [(33.2, 33.6), (33.2, 34.0)])
def test_tap_on_a_split_carrier(tmp_path, landing):
    """A carrier split at an explicit vertex (as ``kct pcb prune`` leaves it),
    with the tap inside one piece or exactly at the split point."""
    carrier = [_Seg((33.2, 32.8), (33.2, 34.0)), _Seg((33.2, 34.0), (33.2, 35.2))]
    tap = _Seg((31.2, 31.6), landing)
    path = _write(tmp_path, [*carrier, tap, _Via((31.2, 31.6))])
    before = PCB.load(path)
    assert [(v.nets[0], v.location) for v in _sharp(before)] == [("SIG", landing)]
    carrier_uuids = {_uuid_of(before, seg) for seg in carrier}
    repairer = _repairer(path)
    result = repairer.plan(_sharp(before))
    assert result.unrepaired == [] and len(result.edits) == 1
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert _sharp(saved) == []
    kept = {(s.start, s.end) for s in saved.segments if s.uuid in carrier_uuids}
    assert kept == {(seg.start, seg.end) for seg in carrier}


# --- gates -----------------------------------------------------------------


def test_a_blocked_entry_is_unrepaired_and_leaves_the_board_as_it_was(tmp_path):
    """Foreign copper crosses every right-angle entry: the tap stays, with the
    clearance gate as the reason."""
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    tap = _Seg((32.0, 33.6), (33.2, 34.8))
    foreign = _Seg((32.6, 33.0), (32.6, 35.2), net_number=2)
    path = _write(tmp_path, [carrier, tap, foreign, _Via((32.0, 33.6))])
    original = path.read_bytes()
    repairer = _repairer(path)
    result = repairer.plan(_sharp(PCB.load(path)))
    assert result.edits == []
    assert [(u.net_name, u.reason) for u in result.unrepaired] == [
        ("SIG", "would_violate_clearance")
    ]
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    assert out.read_bytes() == original


def test_a_locked_tap_is_left_alone_and_its_neighbour_lands_clear_of_it(tmp_path):
    """A locked 45-degree tap keeps its landing; the other tap's straight entry
    would share that vertex at 45 degrees, which the rule's own test refuses,
    so it lands a tap width away."""
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    locked_tap = _Seg((32.0, 32.4), (33.2, 33.6), locked=True)
    tap = _Seg((32.0, 33.6), (33.2, 34.8))
    path = _write(tmp_path, [carrier, locked_tap, tap, _Via((32.0, 32.4)), _Via((32.0, 33.6))])
    before = PCB.load(path)
    assert sorted(v.location for v in _sharp(before)) == [(33.2, 33.6), (33.2, 34.8)]
    repairer = _repairer(path)
    result = repairer.plan(_sharp(before))
    assert [(u.net_name, u.reason) for u in result.unrepaired] == [("SIG", "locked_segment")]
    (edit,) = result.edits
    assert edit.witnesses == (_uuid_of(before, tap),)
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert [v.location for v in _sharp(saved)] == [(33.2, 33.6)]
    entry = _tap_path(saved, (32.0, 33.6), carrier)
    _right_angle_entry(entry, (32.0, 33.6), carrier)
    assert math.dist(entry[-1], (33.2, 33.6)) >= 0.2 - 1e-6


def test_an_entry_that_duplicates_kept_copper_is_refused_and_the_next_one_taken(tmp_path):
    """The straight entry is a track already on the board: the redundant-copper
    rule would report the new leg as a duplicate, so the dogleg after it is kept."""
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    existing = _Seg((32.0, 33.6), (33.2, 33.6))
    tap = _Seg((32.0, 33.6), (33.2, 34.8))
    path = _write(tmp_path, [carrier, existing, tap, _Via((32.0, 33.6))])
    repairer = _repairer(path)
    result = repairer.plan(_sharp(PCB.load(path)))
    assert result.unrepaired == []
    (edit,) = result.edits
    assert edit.legs != ((32.0, 33.6), (33.2, 33.6))
    assert edit.legs[0] == (32.0, 33.6)
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert _sharp(saved) == []
    assert not RedundantCopperRule().check(saved, None).violations


def test_a_kept_track_meeting_the_new_leg_mid_span_is_judged_too(tmp_path):
    """Board 03 [VBUS F.Cu]: the straight entry would run through the vertex
    where two kept tracks of the net meet, opening a 45-degree angle there (a
    tap inside a continuous track, #10675); the dogleg past it is taken."""
    carrier = _Seg((35.2, 9.2), (35.2, 18.8), width=0.5)
    tap = _Seg((35.2, 17.2), (36.4, 16.0), width=0.5)
    kept = [
        _Seg((36.0, 16.4), (36.0, 16.0), width=0.5),
        _Seg((36.0, 16.0), (38.0, 14.0), width=0.5),
        _Seg((36.4, 16.0), (36.8, 16.0), width=0.5),  # the tap's run runs on east
    ]
    path = _write(tmp_path, [carrier, tap, *kept])
    before = PCB.load(path)
    assert [_at(v) for v in _sharp(before)] == [(35.2, 17.2)]
    repairer = _repairer(path)
    result = repairer.plan(_sharp(before))
    assert result.unrepaired == []
    (edit,) = result.edits
    assert edit.legs[0] == (36.4, 16.0) and len(edit.legs) == 3
    assert edit.legs[-1] != (35.2, 16.0)
    _right_angle_entry(list(edit.legs), (36.4, 16.0), carrier)
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    assert _sharp(PCB.load(out)) == []


def test_duplicate_tap_pieces_go_with_the_tap(tmp_path):
    """A board not yet deduped: two identical copies of the tap form one leg,
    and the rewrite takes both off."""
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    tap = _Seg((31.2, 31.6), (33.2, 33.6))
    path = _write(tmp_path, [carrier, tap, tap, _Via((31.2, 31.6))])
    before = PCB.load(path)
    assert len(_sharp(before)) == 1
    repairer = _repairer(path)
    result = repairer.plan(_sharp(before))
    (edit,) = result.edits
    assert len(edit.removed) == 2
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert _sharp(saved) == []
    assert not RedundantCopperRule().check(saved, None).violations
    _right_angle_entry(_tap_path(saved, (31.2, 31.6), carrier), (31.2, 31.6), carrier)


def test_two_taps_of_one_carrier_are_both_rerouted(tmp_path):
    """Board 03 [BTN3 B.Cu]: both taps are acute and compete for the carrier;
    the second one's straight entry is the first one's old landing."""
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    taps = [_Seg((31.2, 31.6), (33.2, 33.6)), _Seg((32.0, 33.6), (33.2, 34.8))]
    path = _write(tmp_path, [carrier, *taps, _Via((31.2, 31.6)), _Via((32.0, 33.6))])
    before = PCB.load(path)
    assert len(_sharp(before)) == 2
    repairer = _repairer(path)
    result = repairer.plan(_sharp(before))
    assert result.unrepaired == [] and len(result.edits) == 2
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert _sharp(saved) == []
    landings = []
    for via in ((31.2, 31.6), (32.0, 33.6)):
        entry = _tap_path(saved, via, carrier)
        _right_angle_entry(entry, via, carrier)
        landings.append(entry[-1])
    assert landings[0] != landings[1]


def test_taps_that_compete_for_one_landing_are_planned_in_the_order_that_fits_both(tmp_path):
    """Board 03 [VBUS F.Cu]: two taps of one vertex come from opposite sides
    of a diagonal carrier.  Planned in the rule's order the first takes the
    only landing the second can reach; the other order gives both a cross."""
    carrier = _Seg((36.8, 16.0), (40.8, 12.0), width=0.5)
    first = _Seg((40.0, 12.8), (40.0, 6.4), width=0.5)
    second = _Seg((40.0, 12.8), (40.8, 12.8), width=0.5)
    path = _write(tmp_path, [carrier, first, second])
    before = PCB.load(path)
    assert [_at(v) for v in _sharp(before)] == [(40.0, 12.8)]
    repairer = _repairer(path)
    result = repairer.plan(_sharp(before))
    assert result.unrepaired == [] and len(result.edits) == 2
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert _sharp(saved) == []
    for anchor in ((40.0, 6.4), (40.8, 12.8)):
        _right_angle_entry(_tap_path(saved, anchor, carrier), anchor, carrier)
    # The second tap's straight leg is kept -- one leg, the anchor's foot --
    # and the first meets it at the same landing, from the other side.
    (straight,) = [s for s in saved.segments if s.start == second.end or s.end == second.end]
    assert {straight.start, straight.end} == {second.end, (40.4, 12.4)}


# --- not this repair's shape ------------------------------------------------


def test_a_vertex_without_a_carrier_is_reported_not_repaired(tmp_path):
    """Three taps meet at a bare vertex (a Y): nothing passes through it."""
    taps = [
        _Seg((0.0, 0.0), (2.0, 0.0)),
        _Seg((0.0, 0.0), (0.5773502692, 1.0)),
        _Seg((0.0, 0.0), (-1.7320508076, 1.0)),
    ]
    path = _write(tmp_path, taps)
    original = path.read_bytes()
    assert len(_sharp(PCB.load(path))) == 1
    result = _repairer(path).plan(_sharp(PCB.load(path)))
    assert result.edits == []
    assert [u.reason for u in result.unrepaired] == ["no_carrier"]
    out = tmp_path / "out.kicad_pcb"
    _repairer(path).save(out)
    assert out.read_bytes() == original


def test_two_carriers_crossing_are_reported_not_repaired(tmp_path):
    """Two straight runs, each split at a shared vertex, cross there at 45
    degrees: every leg is carrier copper, so there is no tap to rewrite."""
    k = 1.4142135624
    crossing = [
        _Seg((2.0, 0.0), (0.0, 0.0)),
        _Seg((0.0, 0.0), (-2.0, 0.0)),
        _Seg((-k, -k), (0.0, 0.0)),
        _Seg((0.0, 0.0), (k, k)),
    ]
    path = _write(tmp_path, crossing)
    assert len(_sharp(PCB.load(path))) == 1
    result = _repairer(path).plan(_sharp(PCB.load(path)))
    assert result.edits == []
    assert [u.reason for u in result.unrepaired] == ["no_branch"]


def test_findings_of_other_rules_in_the_list_are_passed_through(tmp_path):
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    tap = _Seg((31.2, 31.6), (33.2, 33.6))
    path = _write(tmp_path, [carrier, tap, _Via((31.2, 31.6))])
    other = Finding(
        rule_id="pour_slot", severity="warning", message="slot", location=None, layer=""
    )
    result = _repairer(path).plan([*_sharp(PCB.load(path)), other])
    assert result.repaired == 1 and result.total == 2
    assert [(u.rule_id, u.reason) for u in result.unrepaired] == [("pour_slot", "unsupported_rule")]


# --- veto ------------------------------------------------------------------


def test_veto_restores_the_tap_byte_for_byte_and_refuses_that_landing(tmp_path):
    """A refused edit is put back as it was written (the #9939 pattern), and
    the tap's next candidate is planned in its place."""
    carrier = _Seg((33.2, 32.8), (33.2, 35.2))
    tap = _Seg((31.2, 31.6), (33.2, 33.6))
    path = _write(tmp_path, [carrier, tap, _Via((31.2, 31.6))])
    original = path.read_bytes()
    repairer = _repairer(path)
    (edit,) = repairer.plan(_sharp(PCB.load(path))).edits
    staged = tmp_path / "staged.kicad_pcb"
    repairer.save(staged)
    assert staged.read_bytes() != original

    gone = repairer.veto(edit, "new_drc_error")
    assert [(g.uuid, g.reason) for g in gone] == [(edit.uuid, "new_drc_error")]
    assert repairer.edits == ()
    restored = tmp_path / "restored.kicad_pcb"
    repairer.save(restored)
    assert restored.read_bytes() == original

    again = repairer.plan(_sharp(PCB.load(path)))
    (other,) = again.edits
    assert other.finding == edit.finding and other.path_key != edit.path_key
    assert again.unrepaired == []


# --- Board 03 --------------------------------------------------------------


def _board03(tmp_path: Path) -> tuple[Path, PCB]:
    """The committed routed copper after ``kct pcb dedupe``, as readiness runs it."""
    staged = tmp_path / "usb_joystick_routed.kicad_pcb"
    staged.write_bytes(BOARD.read_bytes())
    pcb = PCB.load(staged)
    pcb.dedupe_copper()
    pcb.save(staged)
    return staged, PCB.load(staged)


def _board03_repairer(board: Path) -> AcuteBranchRepairer:
    checker = DRCChecker(
        PCB.load(board), manufacturer="jlcpcb-tier1", layers=4, warn_on_inactive_skew_rules=False
    )
    return AcuteBranchRepairer(
        board, rule=checker.redundant_copper_rule(), design_rules=checker.design_rules
    )


def test_board03_acute_branches_are_rerouted_from_the_committed_copper(tmp_path):
    """The census the issue measured (25 sharp corners on the published board)
    and the planner's result on it: every edit rewrites one tap -- the carrier
    and its other taps stay -- and each residual names why it has no entry.

    The published copper still carries the staircases the redundant-copper
    cleanup rewrites, so a right-angle entry that turns its own feeding tracks
    into a reportable staircase is refused here more often than on the cleaned
    board (22 sites to 3 there; see ``engineering/acute-branch-10732.json``).
    """
    board, pcb = _board03(tmp_path)
    findings = _sharp(pcb)
    assert len(findings) == 25
    assert {v.corner_class for v in findings} == {"junction"}

    repairer = _board03_repairer(board)
    started = time.monotonic()
    result = repairer.plan(findings)
    assert time.monotonic() - started < 300, "the committed board's plan should not take minutes"
    assert result.repaired >= 18
    assert {u.reason for u in result.unrepaired} <= {
        "new_redundant_copper_staircase",
        "sharp_corner_45deg",
    }

    original = {s.uuid: s for s in pcb.segments}
    for edit in result.edits:
        assert edit.kind == "rewrite_tap" and edit.net_name and edit.layer
        assert len(edit.removed) >= 1 and 1 <= len(edit.added) <= 2
        # Width, net and layer are the tap's own.
        assert {s.width for s in edit.removed} == {edit.width} == {a.width for a in edit.added}
        assert all(
            original[r.uuid].layer == edit.layer and original[r.uuid].net_name == edit.net_name
            for r in edit.removed
        )
        # Every removed track ended at a reported vertex of its net and layer.
        sites = {
            (v.nets[0], v.layer, round(v.location[0], 3), round(v.location[1], 3)) for v in findings
        }
        for removed in edit.removed:
            assert any(
                (edit.net_name, edit.layer, round(end[0], 3), round(end[1], 3)) in sites
                for end in (removed.start, removed.end)
            )
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    after = _sharp(PCB.load(out))
    assert len(after) <= 8
    # What the plan could not clear is what the rule still reports, net for net.
    assert Counter(v.nets[0] for v in after) == Counter(u.net_name for u in result.unrepaired)


def test_board03_veto_restores_the_copper_byte_for_byte(tmp_path):
    board, pcb = _board03(tmp_path)
    repairer = _board03_repairer(board)
    plan = repairer.plan(_sharp(pcb))
    assert plan.edits, "nothing to veto: the committed board has no acute branch"
    first = plan.edits[0]
    gone = repairer.veto(first, "new_sharp_trace_corner")
    assert gone[0].uuid == first.uuid and gone[0].reason == "new_sharp_trace_corner"
    assert len(gone) == len(plan.edits)
    assert repairer.edits == ()
    restored = tmp_path / "restored.kicad_pcb"
    repairer.save(restored)
    assert restored.read_bytes() == board.read_bytes()
