"""Mid-path via groups, through-hole pairs and partial reinforcement (Issue #9725).

A tripled layer change -- three same-net vias in a row joined by a bar on
both layers -- used to read as a loop whenever a bar was a sub-span of a
longer track (the branch running on over the row, or the lane passing under
it): the parallel-group test judges whole original segments, so the bars
never qualified and the whole net was ``ambiguous``. These fixtures pin:

* the group resolves as ONE layer change whose capacity is the sum of its
  barrels, under the pitch / both-bar contact rule, and nothing looser;
* a through-hole pair drawn on both outer layers resolves as one
  ``ParallelGroup`` only along the same route;
* a genuine loop stays ambiguous, its witness naming both branches, and
  reinforcement anchors only the loop-free lane between the endpoints.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise

import pytest

from kicad_tools.physics.ampacity import via_barrel_current_capacity
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    describe_witness,
    reinforcement_eligible_segment_ids,
    reinforcement_path_resolutions,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule
from kicad_tools.validate.rules.reinforcement_incomplete import WiredLeg
from tests.test_current_paths import _add_pad_footprint
from tests.test_path_ampacity_check import _design_rules_2oz

DRILL = 1.4
PAD = 2.2


def _spec(current_a: float = 3.0, *, source: str = "J1", sink: str = "J2") -> CurrentPathSpec:
    return CurrentPathSpec(
        name="force",
        net_name="P",
        source=PathEndpoint(source, "1"),
        sink=PathEndpoint(sink, "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def tripled(
    *,
    pitch: float = 2.4,
    count: int = 3,
    layers: int = 4,
    lane_under: bool = False,
) -> PCB:
    """A top-layer branch changing layer through ``count`` barrels in a row.

    The branch (F.Cu, 3 mm) runs from J1 straight over the whole row and ends
    on the far barrel, so its tail IS the top bar. A separate bottom bar of
    the branch's width spans the row, and a long bottom lane leaves from the
    far barrel to J2. With ``lane_under`` the bottom lane instead passes
    under the whole row from a second feeder J3, so the bottom bar is a
    sub-span of the lane.
    """
    pcb = PCB.create(layers=layers)
    _add_pad_footprint(pcb, ref="J1", x=30, y=10, net="P")
    ys = [40 + i * pitch for i in range(count)]
    far = ys[-1]
    _add_pad_footprint(pcb, ref="J2", x=200, y=far, net="P")
    for y in ys:
        pcb.add_via(30, y, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((30, 10), (30, far), width=3, layer="F.Cu", net="P")
    if lane_under:
        _add_pad_footprint(pcb, ref="J3", x=30, y=far + 60, net="P")
        pcb.add_trace((30, far + 60), (30, ys[0]), width=3, layer="B.Cu", net="P")
        pcb.add_trace((30, far), (200, far), width=4, layer="B.Cu", net="P")
    else:
        pcb.add_trace((30, ys[0]), (30, far), width=3, layer="B.Cu", net="P")
        pcb.add_trace((30, far), (200, far), width=4, layer="B.Cu", net="P")
    return pcb


# --------------------------------------------------------------- AC1 / AC5


@pytest.mark.parametrize("layers", [2, 4])
@pytest.mark.parametrize("reverse", [False, True])
def test_tripled_layer_change_resolves_as_one_summed_change(layers, reverse):
    """(a) 3 vias, 1.4 mm drill, 2.2 mm pad, 2.4 mm pitch: one change, 3 barrels."""
    pcb = tripled(layers=layers)
    spec = _spec(source="J2", sink="J1") if reverse else _spec()
    result = resolve_current_path(pcb, spec)
    assert result.ok, result.reason + describe_witness(result.witness)
    assert len(result.layer_changes) == 1
    (change,) = result.layer_changes
    assert change.count == 3
    assert {change.from_layer, change.to_layer} == {"F.Cu", "B.Cu"}
    assert change.current_capacity_a() == pytest.approx(3 * via_barrel_current_capacity(DRILL))
    # It is a real series layer change: the ampacity check judges it.
    assert result.series_layer_changes == result.layer_changes
    # Every bar and lane segment is evidence checked at the full current.
    assert {id(s) for s in result.segments} == {id(s) for s in pcb.segments}


def test_summed_capacity_clears_a_current_one_barrel_cannot():
    one = via_barrel_current_capacity(DRILL)
    spec = _spec(current_a=2 * one)  # above one barrel, below three
    result = PathAmpacityRule([spec]).check(tripled(), _design_rules_2oz())
    assert not [v for v in result.violations if "changes layer" in v.message]


def test_two_barrels_too_few_for_the_current_still_fail_the_check():
    one = via_barrel_current_capacity(DRILL)
    spec = _spec(current_a=2.5 * one)
    result = PathAmpacityRule([spec]).check(tripled(count=2), _design_rules_2oz())
    found = [v for v in result.violations if "changes layer" in v.message]
    assert len(found) == 1
    assert "through 2 via(s)" in found[0].message
    assert found[0].actual_value == pytest.approx(2 * one)


@pytest.mark.parametrize("exit_y", [42.4, 44.8])
def test_inner_layer_exit_cannot_claim_outer_bar_capacity(exit_y):
    pcb = tripled()
    # Keep the bottom lane as an exit from the proved pair, but route the
    # declared path to a separate sink through just one inner-layer ring.
    _add_pad_footprint(pcb, ref="J3", x=80, y=exit_y, net="P")
    pcb.add_trace((30, exit_y), (80, exit_y), width=2, layer="In1.Cu", net="P")
    spec = _spec(current_a=2.5 * via_barrel_current_capacity(DRILL), sink="J3")
    resolution = resolve_current_path(pcb, spec)
    assert resolution.status == "ambiguous"
    assert not resolution.layer_changes
    findings = PathAmpacityRule([spec]).check(pcb, _design_rules_2oz())
    assert any(v.severity == "error" and "is ambiguous" in v.message for v in findings.violations)


def test_offset_inner_layer_track_touching_a_ring_rejects_group():
    pcb = tripled()
    pcb.add_trace((31, 42.4), (80, 42.4), width=0.4, layer="In1.Cu", net="P")
    assert resolve_current_path(pcb, _spec()).status == "ambiguous"


def _saved_row_board(
    tmp_path, bar_width=3.0, branch="F.Cu", lane="B.Cu", split_feeder=False, via_offsets=(0, 0, 0)
):
    from tests.test_pcb_reinforce import _write_single_pad_footprint_lib

    pcb = PCB.create(width=220, height=100, layers=4, center=False)
    footprint = _write_single_pad_footprint_lib(tmp_path)
    for ref, x, pad_y in [("J1", 30, 10), ("J2", 200, 44.8)]:
        pcb.add_footprint_from_file(footprint, ref, x, pad_y)
        pcb.assign_net_to_footprint_pad(ref, "1", "P")
    for barrel_y, via_offset in zip((40, 42.4, 44.8), via_offsets, strict=True):
        pcb.add_via(30 + via_offset, barrel_y, size=PAD, drill=DRILL, net="P")
    feeder_ys = (10, 40, 44.8) if split_feeder else (10, 44.8)
    for start, end in zip(feeder_ys, feeder_ys[1:], strict=False):
        pcb.add_trace((30, start), (30, end), width=bar_width, layer=branch, net="P")
    pcb.add_trace((30, 40), (30, 44.8), width=3, layer=lane, net="P")
    pcb.add_trace((30, 44.8), (200, 44.8), width=4, layer=lane, net="P")
    return pcb


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "offsets,expected",
    [((0.25,) * 3, 3), ((-0.25,) * 3, 3), ((1.9,) * 3, 3), ((0, 0.25, 0), 3), ((3,) * 3, 0)],
)
def test_saved_via_row_attaches_by_pad_overlap(tmp_path, capsys, offsets, expected, reverse):
    import argparse
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_pcb_reinforce import _reinforce_args

    pcb = _saved_row_board(tmp_path, via_offsets=offsets)
    board = tmp_path / "row.kicad_pcb"
    pcb.save(board)
    spec = _spec(source="J2", sink="J1") if reverse else _spec()
    sidecar = tmp_path / "current_paths.json"
    sidecar.write_text(json.dumps({"paths": [spec.to_dict()]}))
    audit = argparse.Namespace(
        pcb_command="current-paths-audit", pcb=str(board), current_paths=str(sidecar), format="json"
    )
    assert run_pcb_command(audit) == 0
    (result,) = json.loads(capsys.readouterr().out)["resolutions"]
    assert result["status"] == ("resolved" if expected else "unresolved")
    assert [c["via_count"] for c in result["layer_changes"]] == ([expected] if expected else [])
    args = _reinforce_args(
        board, net="P", layer="B.Cu", current_paths=str(sidecar), format="json", anchor_mode="ends"
    )
    assert run_pcb_command(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["anchors_placed"] == len(PCB.load(board).vias) - 3 == (2 if expected else 0)


def _written_row(pcb, filename, sidecar, capsys):
    import argparse
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_pcb_reinforce import _reinforce_args

    pcb.save(filename)
    args = argparse.Namespace(
        pcb_command="current-paths-audit",
        pcb=str(filename),
        current_paths=str(sidecar),
        format="json",
    )
    assert run_pcb_command(args) == 0
    (path,) = json.loads(capsys.readouterr().out)["resolutions"]
    args = _reinforce_args(
        filename,
        net="P",
        layer="B.Cu",
        current_paths=str(sidecar),
        format="json",
        anchor_mode="ends",
    )
    assert run_pcb_command(args) == 0
    report = json.loads(capsys.readouterr().out)
    before = {via.position for via in pcb.vias}
    anchors = sorted(via.position for via in PCB.load(filename).vias if via.position not in before)
    return path, report["run_length_mm"], anchors


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "front_joints,back_joints,offset,return_x,overlap,width_step",
    [
        ((41.2,), (), 0.25, None, 0, 0),
        ((), (41.2,), 0.25, None, 0, 0),
        ((41.2, 43.6), (41.2, 43.6), 0.0, None, 0, 0),
        ((41.2,), (43.6,), 0.25, None, 0, 0),
        ((41.2,), (43.6,), 0.25, 31.4, 0, 0),
        ((41.2, 43.6), (41.2, 43.6), 0.25, None, 0.025, 0),
        ((41.2,), (), 0.25, None, 0, 0.001),
        ((41.2, 43.6), (41.2,), 0.25, None, 0.4, 0.3),
        ((41.2,), (43.6,), 0.25, 31.4, 0.025, 0.001),
    ],
)
def test_subdividing_bars_preserves_public_audit_and_written_anchors(
    tmp_path, capsys, front_joints, back_joints, offset, return_x, overlap, width_step, reverse
):
    import json

    pcb = _saved_row_board(tmp_path, via_offsets=(offset,) * 3)
    pcb.add_footprint_from_file(tmp_path / "TestPad.kicad_mod", "J4", 216, 44.8)
    pcb.assign_net_to_footprint_pad("J4", "1", "P")
    pcb.add_trace((200, 44.8), (216, 44.8), width=4, layer="B.Cu", net="P")
    if return_x is not None:
        pcb.add_trace((return_x, 39), (60, 39), width=0.4, layer="F.Cu", net="P")
        pcb.add_via(60, 39, size=1, drill=0.5, net="P")
        pcb.add_trace((60, 39), (60, 44.8), width=0.4, layer="B.Cu", net="P")

    spec = _spec(source="J4", sink="J1") if reverse else _spec(sink="J4")
    sidecar = tmp_path / "current_paths.json"
    sidecar.write_text(json.dumps({"paths": [spec.to_dict()]}))

    def measure(filename):
        path, run, anchors = _written_row(pcb, filename, sidecar, capsys)
        return (
            path["status"],
            [change["via_count"] for change in path["layer_changes"]],
            run,
            anchors,
        )

    joined = measure(tmp_path / "joined.kicad_pcb")
    if return_x is None:
        assert joined[:3] == ("resolved", [3], 190.8)
        assert joined[3][0][0] < 34 and joined[3][1][0] > 210
    else:
        assert joined[:2] == ("ambiguous", [])

    for layer, joints in (("F.Cu", front_joints), ("B.Cu", back_joints)):
        if not joints:
            continue
        (seg,) = [s for s in pcb.segments if s.layer == layer and s.start[0] == s.end[0] == 30]
        pcb.remove_segments([seg])
        ends = [seg.start[1], *joints, seg.end[1]]
        for i, (y0, y1) in enumerate(zip(ends, ends[1:], strict=False)):
            start = y0 - overlap / 2 if i else y0
            end = y1 + overlap / 2 if i < len(joints) else y1
            width = seg.width - width_step if i and layer == "F.Cu" else seg.width
            pcb.add_trace((30, end), (30, start), width=width, layer=layer, net="P")
    assert measure(tmp_path / "divided.kicad_pcb") == joined
    if width_step and return_x is None:
        path = resolve_current_path(pcb, spec)
        assert path.min_width is not None
        assert path.min_width.width_mm == pytest.approx(3.0 - width_step)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("pitch", [2.4, 3.5])
@pytest.mark.parametrize(
    "joint", ["lane-overlap", "lane-covered", "lane-caps", "lane-open", "feed-caps"]
)
def test_physical_row_joints_preserve_rating_and_written_lane(
    tmp_path, capsys, pitch, joint, reverse
):
    import json

    from tests.test_pcb_reinforce import _write_single_pad_footprint_lib

    pcb = PCB.create(width=245, height=215, layers=4, center=False)
    far = 40 + 2 * pitch
    footprint = _write_single_pad_footprint_lib(tmp_path)
    for ref, point in (("J1", (30, 10)), ("J2", (200, far)), ("J4", (216, far))):
        pcb.add_footprint_from_file(footprint, ref, *point)
        pcb.assign_net_to_footprint_pad(ref, "1", "P")
    for y in (40, 40 + pitch, far):
        pcb.add_via(30.25, y, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((30, 10), (30, far), width=0.4, layer="F.Cu", net="P")
    pcb.add_trace((30, 40), (30, far), width=0.4, layer="B.Cu", net="P")
    pcb.add_trace((30, far), (200, far), width=4, layer="B.Cu", net="P")
    pcb.add_trace((200, far), (216, far), width=4, layer="B.Cu", net="P")
    spec = _spec(source="J4", sink="J1") if reverse else _spec(sink="J4")
    sidecar = tmp_path / "current_paths.json"
    sidecar.write_text(json.dumps({"paths": [spec.to_dict()]}))
    original, run, anchors = _written_row(pcb, tmp_path / "original.kicad_pcb", sidecar, capsys)
    assert original["status"] == "resolved" and len(anchors) == 2

    if joint == "feed-caps":
        pcb.remove_segments([next(s for s in pcb.segments if s.layer == "F.Cu")])
        cuts = [40 + pitch / 2, 40 + 1.5 * pitch]
        for a, b in zip(
            (10, *(y + 0.0125 for y in cuts)), (*(y - 0.0125 for y in cuts), far), strict=True
        ):
            pcb.add_trace((30, a), (30, b), width=0.4, layer="F.Cu", net="P")
    elif joint == "lane-covered":
        pcb.add_trace((60, far), (145, far), width=0.3, layer="B.Cu", net="P")
    else:
        pcb.remove_segments(
            [next(s for s in pcb.segments if s.start == (30, far) and s.end == (200, far))]
        )
        overlap = {"lane-overlap": 0.025, "lane-caps": -0.025, "lane-open": -5.0}[joint]
        pcb.add_trace((30, far), (110 + overlap / 2, far), width=4, layer="B.Cu", net="P")
        pcb.add_trace((200, far), (110 - overlap / 2, far), width=4, layer="B.Cu", net="P")

    path, actual_run, actual_anchors = _written_row(
        pcb, tmp_path / "divided.kicad_pcb", sidecar, capsys
    )
    if joint == "lane-open":
        assert (path["status"], path["layer_changes"], actual_run, actual_anchors) == (
            "unresolved",
            [],
            0,
            [],
        )
    else:
        assert path["status"] == "resolved", path["reason"]
        (change,) = path["layer_changes"]
        assert (change["via_count"], change["capacity_a"]) == (3, 14.1398)
        assert (actual_run, actual_anchors) == (run, anchors)
        assert reinforcement_eligible_segment_ids(pcb, [spec]) == {id(s) for s in pcb.segments}
        if joint == "lane-covered":
            assert path["min_width_mm"] == original["min_width_mm"]
        if joint == "feed-caps":
            neck = (0.4**2 - 0.025**2) ** 0.5
            assert path["min_width_mm"] == pytest.approx(neck, abs=0.0001)
            result = resolve_current_path(pcb, spec)
            assert {id(seg) for seg in result.segments} == {id(seg) for seg in pcb.segments}
            findings = PathAmpacityRule([replace(spec, continuous_a=10)]).check(
                pcb, _design_rules_2oz()
            )
            assert {
                v.location for v in findings.violations if v.actual_value == pytest.approx(neck)
            } == {(30, round(y, 3)) for y in cuts}


def test_fine_bar_subdivision_connects_distinct_offset_track_contacts(tmp_path):
    # The small annuli are 2.4 mm apart on a 0.4 mm bar. Its 0.3 mm
    # pieces cannot individually touch two adjacent barrels.
    pcb = _saved_row_board(tmp_path, bar_width=0.4)
    pcb.remove_vias(pcb.vias)
    for y in (40, 42.4, 44.8):
        pcb.add_via(30.25, y, size=1.4, drill=0.65, net="P")
    feeder = next(s for s in pcb.segments if s.layer == "F.Cu")
    pcb.remove_segments([feeder])
    stations = [10, *(round(40 + i * 0.3, 6) for i in range(17))]
    for a, b in zip(stations, stations[1:], strict=False):
        pcb.add_trace((30, a), (30, b), width=0.4, layer="F.Cu", net="P")
    path = resolve_current_path(pcb, _spec())
    assert path.ok, path.reason
    assert [change.count for change in path.series_layer_changes] == [3]
    assert {id(s) for s in path.segments} == {id(s) for s in pcb.segments}


@pytest.mark.parametrize(
    ("points", "anchors", "credited"),
    [
        (((30, 10), (30, 44.8)), (32, 34), False),
        (((30, 10), (30, 44.8)), (3, 30), False),
        (((30, 10), (30, 44.8)), (33, 3), True),
        (((30, 10), (30, 44.8)), (31.2, 33), True),
        (((28, 41.2), (32, 41.2)), (1, 3), False),
        (((28, 39.2), (32, 43.2)), (1, 5), False),
    ],
    ids=["before-wire", "after-wire", "between", "at-anchor", "crossing", "diagonal-crossing"],
)
def test_cap_joint_ampacity_only_credits_wire_carried_across_the_pinch(points, anchors, credited):
    pcb = tripled()
    pcb.remove_segments([next(s for s in pcb.segments if s.layer == "F.Cu")])
    pinch_y = 41.2
    gap = 0.025
    pcb.add_trace((30, 10), (30, pinch_y - gap / 2), width=0.4, layer="F.Cu", net="P")
    pcb.add_trace((30, 44.8), (30, pinch_y + gap / 2), width=0.4, layer="F.Cu", net="P")
    spec = replace(_spec(10), pulsed_a=100, duty_cycle=0.01, pulse_duration_s=1)
    resolution = resolve_current_path(pcb, spec)
    assert resolution.ok
    (joint,) = resolution.cap_joints
    assert joint.pinch == pytest.approx((30, pinch_y))
    assert joint.span.width == pytest.approx((0.4**2 - gap**2) ** 0.5)

    leg = WiredLeg(net="P", layer="F.Cu", points=points, width=0.4, gauge=14, anchor_arc=anchors)
    findings = PathAmpacityRule([spec], wired_legs=[leg]).check(pcb, _design_rules_2oz())
    messages = [v.message for v in findings.errors if v.location == (30, pinch_y)]
    assert any("too narrow" in message for message in messages) == (not credited)
    assert any("would fuse" in message for message in messages) == (not credited)


@pytest.mark.parametrize(("end_offset", "covered"), [(4.0, True), (10.0, False)])
def test_placed_wire_credit_across_bonded_track_ends(end_offset, covered):
    from kicad_tools.pcb.reinforce import reinforce_net
    from kicad_tools.pcb.reinforcement_sidecar import build_wire_records
    from kicad_tools.validate.checker import DRCChecker
    from tests.test_pcb_reinforce import _add_th_pad_footprint

    pcb = PCB.create(width=165, height=67, layers=4, center=False)
    y = 36.8
    for ref, point in (("J1", (30, 10)), ("J2", (145, y))):
        _add_th_pad_footprint(pcb, ref=ref, x=point[0], y=point[1], net="P", drill=0.9, size=2.2)
    for via_y in (32, 34.4, y):
        pcb.add_via(30.25, via_y, size=2.2, drill=1.4, net="P")
    pcb.add_trace((30, 10), (30, y), width=3, layer="F.Cu", net="P")
    pcb.add_trace((30, 32), (30, y), width=2, layer="B.Cu", net="P")
    pcb.add_trace((30, y), (136.325, y), width=2, layer="B.Cu", net="P")
    pcb.add_trace((137.675, y), (145, y), width=2, layer="B.Cu", net="P")

    spec = _spec(3.6)
    outcome = reinforce_net(
        pcb,
        "P",
        wire_gauge_awg=16,
        layer="B.Cu",
        anchor_mode="ends",
        current_paths=[spec],
        end_offset_mm=end_offset,
        max_end_shift_mm=22,
    )
    assert len(outcome.placed) == 2
    wires = build_wire_records(outcome, 0.0, 0.0)
    checker = DRCChecker(
        pcb, manufacturer="jlcpcb", layers=4, current_path_specs=[spec], reinforcement_wires=wires
    )
    (joint,) = resolve_current_path(pcb, spec).cap_joints
    assert joint.pinch == pytest.approx((137, y))
    # Physical anchor holes independently identify whether the placed wire crosses this station.
    anchor_xs = sorted(v.position[0] for v in pcb.vias if v.position[0] != 30.25)
    assert len(anchor_xs) == 2 and (anchor_xs[0] < 137 < anchor_xs[1]) == covered
    errors = checker.check_path_ampacity().errors
    assert any(v.location == (137, y) and "too narrow" in v.message for v in errors) == (
        not covered
    )

    from tests.test_reinforcement_courtyard import _body

    _body(pcb, "MID_WIRE", 90, y, side="B", half=0.5)
    _body(pcb, "END_STUB", 142, y, side="B", half=0.5)
    hits = checker.check_wire_run_courtyard_overlap().errors
    assert any("[run]" in v.message and "MID_WIRE" in v.message for v in hits)
    if not covered:
        assert not any("END_STUB" in v.message for v in hits)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("large", [(0,), (1,), (2,), (), (0, 1, 2)])
def test_pitch_uses_largest_pad_in_this_row(large, reverse):
    # At 3 mm the small pads' 2.8 mm bound fails, and a row containing a
    # 2.2 mm pad has a 4.4 mm bound regardless of its position.
    pcb = tripled(pitch=3)
    pcb.remove_vias(pcb.vias)
    drills = []
    for i, y in enumerate((40, 43, 46)):
        diameter, drill = (2.2, 1.4) if i in large else (1.4, 0.65)
        pcb.add_via(30, y, size=diameter, drill=drill, net="P")
        drills.append(drill)
    # A larger via outside this row cannot change its pitch bound.
    pcb.add_via(150, 80, size=4, drill=1.4, net="P")
    path = resolve_current_path(pcb, _spec(source="J2", sink="J1") if reverse else _spec())
    if large:
        assert path.status == "resolved", path.reason
        (change,) = path.series_layer_changes
        assert change.count == 3
        assert change.current_capacity_a() == pytest.approx(
            sum(via_barrel_current_capacity(d) for d in drills)
        )
    else:
        assert path.status == "ambiguous" and not path.series_layer_changes


@pytest.mark.parametrize(
    "bar_width,y,offset,row_offset,expected",
    [
        (3, 41.2, None, 0, "resolved"),
        (3, 41.2, 0, 0, "ambiguous"),
        (3, 41.2, 1.25, 0, "ambiguous"),
        (3, 41.2, 1.4, 0, "ambiguous"),
        (3, 41.2, 1.61, 0, "ambiguous"),
        (3, 41.2, 1.85, 0, "resolved"),
        # The bypass may join the feeder ahead of the first barrel.
        (3, 39.0, 1.4, 0, "ambiguous"),
        (3, 39.0, 1.85, 0, "resolved"),
        # A separate path can bypass an entire off-centre row too.
        (3, 39.0, 0, 0.25, "ambiguous"),
        (3, 39.0, 1.4, 0.25, "ambiguous"),
        (3, 39.0, 1.85, 0.25, "resolved"),
        # A narrow bar leaves the barrel ring exposed on the outer layer.
        (0.4, 42.4, 1.2, 0, "ambiguous"),
        (0.4, 42.4, 1.45, 0, "resolved"),
    ],
)
@pytest.mark.parametrize("branch,lane", [("F.Cu", "B.Cu"), ("B.Cu", "F.Cu")])
@pytest.mark.parametrize("split_feeder", [False, True])
def test_cli_via_group_with_outer_bar_bypass(
    tmp_path, capsys, bar_width, y, offset, row_offset, expected, branch, lane, split_feeder
):
    import argparse
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command

    pcb = _saved_row_board(tmp_path, bar_width, branch, lane, split_feeder, (row_offset,) * 3)
    if offset is not None:
        # Some current can bypass the row through this fourth via if the
        # narrow trace reaches the edge of a bar or ring.
        pcb.add_trace((30 + offset, y), (60, y), width=0.4, layer=branch, net="P")
        pcb.add_via(60, y, size=1, drill=0.5, net="P")
        pcb.add_trace((60, y), (60, 44.8), width=0.4, layer=lane, net="P")
    board = tmp_path / "group.kicad_pcb"
    pcb.save(board)
    specs = [_spec(), replace(_spec(source="J2", sink="J1"), name="reverse")]
    sidecar = tmp_path / "current_paths.json"
    sidecar.write_text(json.dumps({"paths": [spec.to_dict() for spec in specs]}))
    args = argparse.Namespace(
        pcb_command="current-paths-audit", pcb=str(board), current_paths=str(sidecar), format="json"
    )
    assert run_pcb_command(args) == 0
    results = json.loads(capsys.readouterr().out)["resolutions"]
    assert len(results) == 2
    for result in results:
        assert result["status"] == expected
        assert [c["via_count"] for c in result["layer_changes"]] == (
            [3] if expected == "resolved" else []
        )
        if expected == "ambiguous":
            assert len(result["witness"]["loop_branches"]) == 2
    errors = PathAmpacityRule(specs).check(pcb, _design_rules_2oz()).violations
    assert sum(v.severity == "error" and "is ambiguous" in v.message for v in errors) == (
        2 if expected == "ambiguous" else 0
    )


def _saved_row_with_downstream_lane(tmp_path):
    pcb = _saved_row_board(tmp_path)
    pcb.add_footprint_from_file(tmp_path / "TestPad.kicad_mod", "J4", 216, 44.8)
    pcb.assign_net_to_footprint_pad("J4", "1", "P")
    pcb.add_trace((200, 44.8), (216, 44.8), width=4, layer="B.Cu", net="P")
    return pcb


@pytest.mark.parametrize(
    "offset,mid_lane,expected_anchors",
    [(1.4, False, 0), (1.85, False, 2), (None, False, 2), (1.4, True, 0), (1.85, True, 2)],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_cli_reinforcement_checks_physical_lane_bypasses(
    tmp_path, capsys, offset, mid_lane, expected_anchors, reverse
):
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_pcb_reinforce import _reinforce_args

    pcb = _saved_row_with_downstream_lane(tmp_path)

    # A centered return bypasses only the row. The second return bypasses
    # the downstream J2–J4 lane when its copper reaches the feeder's edge.
    pcb.add_trace((30, 38), (60, 38), width=0.4, layer="F.Cu", net="P")
    pcb.add_via(60, 38, size=1, drill=0.5, net="P")
    pcb.add_trace((60, 38), (60, 44.8), width=0.4, layer="B.Cu", net="P")
    if offset is not None:
        x, y = (208, 46.8) if mid_lane else (216, 44.8)
        pcb.add_trace((30 + offset, 35), (x, 35), width=0.4, layer="F.Cu", net="P")
        pcb.add_trace((x, 35), (x, y), width=0.4, layer="F.Cu", net="P")
        if mid_lane:
            # Its annulus touches the middle of the B.Cu lane off-center.
            # Removing the whole lane would also remove its unavoidable half.
            pcb.add_via(x, y, size=1, drill=0.5, net="P")
    board = tmp_path / "lane.kicad_pcb"
    pcb.save(board)
    spec = _spec(source="J4", sink="J1") if reverse else _spec(sink="J4")
    sidecar = tmp_path / "current_paths.json"
    sidecar.write_text(json.dumps({"paths": [spec.to_dict()]}))

    args = _reinforce_args(
        board, net="P", current_paths=str(sidecar), format="json", anchor_mode="ends"
    )
    assert run_pcb_command(args) == 0
    report = json.loads(capsys.readouterr().out)
    written = PCB.load(board)
    assert len(written.vias) == len(pcb.vias) + expected_anchors
    added = [v for v in written.vias if v.drill > DRILL]
    assert report["anchors_placed"] == len(added) == expected_anchors
    assert [p["anchored_segments"] for p in report["unresolved_paths"]] == [expected_anchors // 2]
    if expected_anchors:
        assert sorted((round(v.position[0], 3), v.position[1]) for v in added) == [
            (203.958, 44.8),
            (213.542, 44.8),
        ]


@pytest.mark.parametrize(
    "return_y,bypassed", [(None, False), (44.8, True), (46.7, True), (47.25, False)]
)
@pytest.mark.parametrize("reverse", [False, True])
def test_downstream_return_after_a_genuine_via_row(tmp_path, capsys, return_y, bypassed, reverse):
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_pcb_reinforce import _reinforce_args

    pcb = _saved_row_with_downstream_lane(tmp_path)
    if return_y is not None:
        # At y=46.7 the track's copper overlaps the 4 mm lane while the
        # centerlines never meet. At y=47.25 the same return is 0.25 mm clear.
        points = [(194, return_y), (194, 60), (216, 60), (216, 44.8)]
        for start, end in zip(points, points[1:], strict=False):
            pcb.add_trace(start, end, width=0.4, layer="B.Cu", net="P")
    board = tmp_path / "downstream.kicad_pcb"
    pcb.save(board)
    original = board.read_bytes()
    spec = _spec(source="J4", sink="J1") if reverse else _spec(sink="J4")
    sidecar = tmp_path / "current_paths.json"
    sidecar.write_text(json.dumps({"paths": [spec.to_dict()]}))

    result = resolve_current_path(PCB.load(board), spec)
    assert result.status == ("ambiguous" if bypassed else "resolved")
    assert [change.count for change in result.layer_changes] == ([] if bypassed else [3])
    args = _reinforce_args(
        board, net="P", layer="B.Cu", current_paths=str(sidecar), format="json", anchor_mode="ends"
    )
    assert run_pcb_command(args) == 0
    report = json.loads(capsys.readouterr().out)
    added = [via for via in PCB.load(board).vias if via.drill > DRILL]
    assert report["anchors_placed"] == len(added) == (0 if bypassed else 2)
    if bypassed:
        branches = result.witness["loop_branches"]
        assert sorted(branch["segments"] for branch in branches) == [2, 3]
        assert branches[0]["start"] == branches[1]["start"]
        assert branches[0]["end"] == branches[1]["end"]
        assert "loop branches" in report["unresolved_paths"][0]["reason"]
        assert board.read_bytes() == original
    else:
        assert sorted((round(v.position[0], 3), v.position[1]) for v in added) == [
            (32.145, 44.8),
            (213.542, 44.8),
        ]


@pytest.mark.parametrize(
    "landing,expected", [("via", "ambiguous"), ("gap", "resolved"), ("no_via", "resolved")]
)
def test_downstream_return_only_through_a_physical_layer_contact(tmp_path, landing, expected):
    pcb = _saved_row_with_downstream_lane(tmp_path)
    # Only the annulus can connect this front-layer return to the back lane.
    y = 47.55 if landing == "gap" else 46.9
    pcb.add_trace((194, y), (194, 60), width=0.4, layer="F.Cu", net="P")
    pcb.add_trace((194, 60), (216, 44.8), width=0.4, layer="F.Cu", net="P")
    if landing != "no_via":
        pcb.add_via(194, y, size=1, drill=0.5, net="P")
    for source, sink in [("J1", "J4"), ("J4", "J1")]:
        result = resolve_current_path(pcb, _spec(source=source, sink=sink))
        assert result.status == expected
        assert not result.loop_free_segments


@pytest.mark.parametrize(
    "splits", [(), (0.0,), (-1.028,), (0.058,), (1.028,), (-0.4, 0.058, 1.028)]
)
@pytest.mark.parametrize("return_on_front", [False, True])
def test_subdividing_a_uniform_lane_at_an_anchor_preserves_its_topology(splits, return_on_front):
    pcb = tripled()
    lane = next(s for s in pcb.segments if s.layer == "B.Cu" and s.end[0] == 200)
    x, y = 134.942, lane.start[1]
    pcb.remove_segments([lane])
    points = [lane.start, *((x + offset, y) for offset in splits), lane.end]
    for start, end in pairwise(points):
        pcb.add_trace(start, end, width=2, layer="B.Cu", net="P")
    pcb.add_via(x, y, size=2.2, drill=1.416, net="P")
    if return_on_front:
        branch = [(x, y), (x, y + 15), (lane.end[0], y + 15), lane.end]
        for start, end in pairwise(branch):
            pcb.add_trace(start, end, width=1, layer="F.Cu", net="P")
    for source, sink in [("J1", "J2"), ("J2", "J1")]:
        result = resolve_current_path(pcb, _spec(3.6, source=source, sink=sink))
        assert result.status == ("ambiguous" if return_on_front else "resolved"), result.reason
        assert [change.count for change in result.series_layer_changes] == (
            [] if return_on_front else [3]
        )


@pytest.mark.parametrize("end", [136.4, 137.0])
def test_anchor_on_a_lane_with_a_fully_contained_stroke_preserves_its_throat(end):
    from kicad_tools.pcb.reinforce import _declared_leg_spans

    pcb = tripled()
    lane = next(s for s in pcb.segments if s.layer == "B.Cu" and s.end[0] == 200)
    y = lane.start[1]
    pcb.remove_segments([lane])
    pcb.add_trace(lane.start, (136.6, y), width=2, layer="B.Cu", net="P")
    pcb.add_trace((137.4, y), lane.end, width=2, layer="B.Cu", net="P")
    pcb.add_via(134.942, y, size=2.2, drill=1.416, net="P")
    # The second endpoint can extend into the round cap of the 2 mm stroke.
    pcb.add_trace((135, y), (end, y), width=0.2, layer="B.Cu", net="P")
    overlay = pcb.segments[-1]
    for source, sink in [("J1", "J2"), ("J2", "J1")]:
        result = resolve_current_path(pcb, _spec(3.6, source=source, sink=sink))
        assert result.ok, result.reason
        assert [change.count for change in result.series_layer_changes] == [3]
        assert result.min_width.width_mm == pytest.approx((2**2 - 0.8**2) ** 0.5)
        assert overlay in result.covered_segments
        lane_pieces = [s for s in result.covered_segments if s.start[1] == s.end[1] == y]
        spans = _declared_leg_spans(lane_pieces, [result], {})
        assert sum(abs(s.end[0] - s.start[0]) for s in spans) == pytest.approx(
            lane.end[0] - lane.start[0]
        )


def test_downstream_return_shunting_only_the_middle_of_a_long_track(tmp_path):
    pcb = _saved_row_with_downstream_lane(tmp_path)
    lane = pcb.segments[-1]
    points = [(80, 46.7), (80, 60), (125, 60), (125, 46.7)]
    for start, end in zip(points, points[1:], strict=False):
        pcb.add_trace(start, end, width=0.4, layer="B.Cu", net="P")
    # Cutting the whole long track would open the board, but its middle is
    # shunted. Only the separate track beyond J2 remains eligible.
    for source, sink in [("J1", "J4"), ("J4", "J1")]:
        result = resolve_current_path(pcb, _spec(source=source, sink=sink))
        assert result.status == "ambiguous"
        assert result.loop_free_segments == (lane,)


def test_group_with_oblique_feeder_at_modeled_barrel():
    pcb = tripled()
    _add_pad_footprint(pcb, ref="J3", x=80, y=25, net="P")
    pcb.add_trace((80, 25), (30, 42.4), width=2, layer="F.Cu", net="P")
    result = resolve_current_path(pcb, _spec(source="J3"))
    assert result.ok, result.reason
    assert [change.count for change in result.series_layer_changes] == [3]


@pytest.mark.parametrize(("offset", "modeled"), [(0, True), (1.4, False)])
def test_partial_reinforcement_with_a_feeder_bypass(offset, modeled):
    pcb = tripled()
    _add_pad_footprint(pcb, ref="J4", x=220, y=44.8, net="P")
    pcb.add_trace((200, 44.8), (220, 44.8), width=4, layer="B.Cu", net="P")
    pcb.add_trace((30 + offset, 39), (60, 39), width=0.4, layer="F.Cu", net="P")
    pcb.add_via(60, 39, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((60, 39), (60, 44.8), width=0.4, layer="B.Cu", net="P")

    result = resolve_current_path(pcb, _spec(sink="J4"))
    assert result.status == "ambiguous"
    # A modeled junction proves the downstream J2-J4 lane carries the full
    # load. An edge-only contact leaves the centerline route incomplete.
    assert [(seg.start, seg.end) for seg in result.loop_free_segments] == (
        [((200, 44.8), (220, 44.8))] if modeled else []
    )


def test_anchor_between_a_feeder_loop_and_load_preserves_every_series_piece():
    pcb = tripled()
    y = 44.8
    _add_pad_footprint(pcb, ref="J4", x=220, y=y, net="P")
    # A real bypass upstream of J2 does not carry current around this lane.
    pcb.add_trace((30, 39), (60, 39), width=0.4, layer="F.Cu", net="P")
    pcb.add_via(60, 39, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((60, 39), (60, y), width=0.4, layer="B.Cu", net="P")
    pcb.add_via(209.942, y, size=2.2, drill=1.416, net="P")
    stations = [200, 209.6, 210, 210.97, 220]
    for start, end in pairwise(stations):
        pcb.add_trace((start, y), (end, y), width=2, layer="B.Cu", net="P")
    for source, sink in [("J1", "J4"), ("J4", "J1")]:
        result = resolve_current_path(pcb, _spec(source=source, sink=sink))
        assert result.status == "ambiguous"
        assert {(s.start[0], s.end[0]) for s in result.loop_free_segments} == set(
            pairwise(stations)
        )


def test_two_vias_only_resolve_with_two_barrels():
    """(d) two vias."""
    result = resolve_current_path(tripled(count=2), _spec())
    assert result.ok, result.reason
    (change,) = result.layer_changes
    assert change.count == 2


def test_lane_passing_under_the_row_resolves():
    """The bottom bar is a sub-span of a lane fed from both sides."""
    pcb = tripled(lane_under=True)
    result = resolve_current_path(pcb, _spec())
    assert result.ok, result.reason + describe_witness(result.witness)
    (change,) = result.layer_changes
    assert change.count == 3


@pytest.mark.parametrize("pitch,expected", [(4.4, "resolved"), (4.5, "ambiguous")])
def test_pitch_bound_is_two_pad_diameters(pitch, expected):
    assert resolve_current_path(tripled(pitch=pitch), _spec()).status == expected


def test_pitch_just_outside_stays_ambiguous_with_witness():
    """(b) 5.0 mm pitch is beyond 2 x 2.2 mm: not one structure."""
    pcb = tripled(pitch=5.0)
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"
    assert result.witness
    # Only the bottom lane, which every route crosses, stays anchorable: the
    # branch (partly over the loop) and the bottom bar (in it) do not.
    lane = pcb.segments[-1]
    assert (lane.start, lane.end) == ((30, 50.0), (200, 50.0))
    assert reinforcement_eligible_segment_ids(pcb, [_spec()]) == {id(lane)}


def test_barrel_missing_a_bar_contact_stays_ambiguous():
    """(c) the middle barrel misses the bottom bar: no group is proved."""
    pcb = PCB.create(layers=4)
    _add_pad_footprint(pcb, ref="J1", x=30, y=10, net="P")
    _add_pad_footprint(pcb, ref="J2", x=200, y=44, net="P")
    for y in (40, 42, 44):
        pcb.add_via(30, y, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((30, 10), (30, 44), width=3, layer="F.Cu", net="P")
    # Bottom bar jogs round the middle barrel: it joins the outer two only.
    pcb.add_trace((30, 40), (34, 40), width=1, layer="B.Cu", net="P")
    pcb.add_trace((34, 40), (34, 44), width=1, layer="B.Cu", net="P")
    pcb.add_trace((34, 44), (30, 44), width=1, layer="B.Cu", net="P")
    pcb.add_trace((30, 44), (200, 44), width=4, layer="B.Cu", net="P")
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"


def test_unmodeled_via_pad_on_the_bar_rejects_the_group():
    """A same-net barrel whose pad touches the bar without a modeled contact."""
    pcb = tripled(count=2, pitch=4.0)
    pcb.add_via(30.5, 42, size=PAD, drill=DRILL, net="P")  # off both centrelines
    result = resolve_current_path(pcb, _spec())
    assert not result.ok


@pytest.mark.parametrize(
    "extra_via_y,expected", [(None, "resolved"), (42, "ambiguous"), (46, "resolved")]
)
def test_via_group_contact_uses_width_of_touched_span(extra_via_y, expected):
    pcb = PCB.create(layers=2)
    _add_pad_footprint(pcb, ref="J1", x=30, y=10, net="P")
    _add_pad_footprint(pcb, ref="J2", x=200, y=48, net="P")
    for y in (40, 44, 48):
        pcb.add_via(30, y, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((30, 10), (30, 48), width=1, layer="F.Cu", net="P")
    pcb.add_trace((30, 40), (30, 44), width=4, layer="B.Cu", net="P")
    pcb.add_trace((30, 44), (30, 48), width=1, layer="B.Cu", net="P")
    pcb.add_trace((30, 48), (200, 48), width=3, layer="B.Cu", net="P")
    if extra_via_y is not None:
        pcb.add_via(32, extra_via_y, size=1, drill=0.5, net="P")

    result = resolve_current_path(pcb, _spec())
    assert result.status == expected
    assert [change.count for change in result.layer_changes] == ([3] if result.ok else [])


def test_bars_on_both_layers_continuing_are_a_doubled_run_not_a_change():
    """Both layers run on past both ends: stitching, never a credited change."""
    pcb = PCB.create(layers=2)
    _add_pad_footprint(pcb, ref="J1", x=10, y=50, net="P")
    _add_pad_footprint(pcb, ref="J2", x=100, y=50, net="P")
    for x in (40, 42.4, 44.8):
        pcb.add_via(x, 50, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((10, 50), (100, 50), width=2, layer="F.Cu", net="P")
    pcb.add_trace((10, 50), (100, 50), width=2, layer="B.Cu", net="P")
    result = resolve_current_path(pcb, _spec())
    # Coincident doubled copper resolves as a parallel group, as before, and
    # no via group is credited as a layer change.
    assert result.ok, result.reason
    assert result.parallel_groups
    assert not result.series_layer_changes


def _with_loop(pcb: PCB) -> PCB:
    """Add a genuine same-layer loop on the lane near J2."""
    pcb.add_trace((150, 44.8), (160, 54.8), width=0.5, layer="B.Cu", net="P")
    pcb.add_trace((160, 54.8), (170, 44.8), width=0.5, layer="B.Cu", net="P")
    return pcb


def test_genuine_loop_elsewhere_stays_ambiguous_and_names_both_branches():
    """(e) the via group is fine; the detour off the lane is a real loop."""
    pcb = _with_loop(tripled())
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"
    branches = result.witness["loop_branches"]
    assert len(branches) == 2
    text = describe_witness(result.witness)
    assert "[loop branches: " in text
    # One branch is the lane span, the other the two-segment detour.
    spans = sorted(b["segments"] for b in branches)
    assert spans == [1, 2]
    for branch in branches:
        assert sorted([tuple(branch["start"]), tuple(branch["end"])]) == [
            (150.0, 44.8),
            (170.0, 44.8),
        ]


def test_loop_at_a_declared_endpoint_splits_at_that_pad():
    """The endpoint pad is a junction of the loop even with only two arms."""
    result = resolve_current_path(_lane_with_loop_and_spur(), _spec())
    branches = result.witness["loop_branches"]
    assert sorted(b["segments"] for b in branches) == [1, 3]
    for branch in branches:
        assert branch["layers"] == ["F.Cu"]
        assert {tuple(branch["start"]), tuple(branch["end"])} == {(10.0, 50.0), (30.0, 50.0)}
    assert branches[0]["start"] == branches[1]["start"]


def test_loop_branches_take_the_origin():
    pcb = _with_loop(tripled())
    witness = resolve_current_path(pcb, _spec()).witness
    shifted = describe_witness(witness, (100.0, 50.0))
    assert "(250.000, 94.800)" in shifted
    assert "(270.000, 94.800)" in shifted


# ----------------------------------------------------------------------- AC2


def _th_pair(*, detour: float = 0.0) -> PCB:
    pcb = PCB.create(layers=2)
    _add_pad_footprint(pcb, ref="J1", x=10, y=50, net="P")
    _add_pad_footprint(pcb, ref="J2", x=40, y=50, net="P")
    pcb.add_trace((10, 50), (40, 50), width=2, layer="F.Cu", net="P")
    if detour:
        pcb.add_trace((10, 50), (10, 50 + detour), width=2, layer="B.Cu", net="P")
        pcb.add_trace((10, 50 + detour), (40, 50 + detour), width=2, layer="B.Cu", net="P")
        pcb.add_trace((40, 50 + detour), (40, 50), width=2, layer="B.Cu", net="P")
    else:
        pcb.add_trace((10, 50), (40, 50), width=2, layer="B.Cu", net="P")
    return pcb


def test_through_hole_pair_on_both_layers_is_one_parallel_group():
    result = resolve_current_path(_th_pair(), _spec())
    assert result.ok, result.reason
    assert len(result.parallel_groups) == 1
    assert len(result.parallel_groups[0].branches) == 2


def test_through_hole_pair_with_a_detour_stays_ambiguous():
    """A detour of more than half the narrowest width is not credited."""
    result = resolve_current_path(_th_pair(detour=1.5), _spec())
    assert result.status == "ambiguous"
    assert not result.parallel_groups


# ----------------------------------------------------------------------- AC3


def _lane_with_loop_and_spur() -> PCB:
    """J1 -> loop -> straight two-segment lane -> J2, plus an off-path spur.

    The loop is a same-layer detour beside the first 20 mm (never a
    qualifying group); the spur leaves the lane at x=100 for a test point.
    """
    pcb = PCB.create(layers=2)
    _add_pad_footprint(pcb, ref="J1", x=10, y=50, net="P")
    _add_pad_footprint(pcb, ref="J2", x=200, y=50, net="P")
    _add_pad_footprint(pcb, ref="TP1", x=100, y=80, net="P")
    pcb.add_trace((10, 50), (30, 50), width=3, layer="F.Cu", net="P")  # loop a
    pcb.add_trace((10, 50), (10, 58), width=3, layer="F.Cu", net="P")  # loop b
    pcb.add_trace((10, 58), (30, 58), width=3, layer="F.Cu", net="P")
    pcb.add_trace((30, 58), (30, 50), width=3, layer="F.Cu", net="P")
    pcb.add_trace((30, 50), (100, 50), width=3, layer="F.Cu", net="P")  # lane 1
    pcb.add_trace((100, 50), (200, 50), width=3, layer="F.Cu", net="P")  # lane 2
    pcb.add_trace((100, 50), (100, 80), width=1, layer="F.Cu", net="P")  # spur
    pcb.add_trace((100, 80), (110, 90), width=1, layer="F.Cu", net="P")  # spur tail
    return pcb


@pytest.mark.parametrize("reverse", [False, True])
def test_partial_reinforcement_takes_only_the_loop_free_lane(reverse):
    pcb = _lane_with_loop_and_spur()
    spec = _spec(source="J2", sink="J1") if reverse else _spec()
    lane = {id(pcb.segments[4]), id(pcb.segments[5])}

    result = resolve_current_path(pcb, spec)
    assert result.status == "ambiguous"
    assert {id(s) for s in result.loop_free_segments} == lane
    assert result.segments == ()  # never a resolved path

    partition = reinforcement_path_resolutions(pcb, [spec])
    assert partition.eligible == ()
    assert [r.spec.name for r in partition.unresolved] == ["force"]
    assert [r.spec.name for r in partition.partial] == ["force"]
    assert reinforcement_eligible_segment_ids(pcb, [spec]) == lane


@pytest.mark.parametrize("landing", ["insulated", "bonded"])
def test_physical_return_only_excludes_the_lane_it_bypasses(landing):
    pcb = _lane_with_loop_and_spur()
    # An opposite-layer trace from the through-hole source ends at the joint
    # between the lanes. Only a via actually connects those coincident points.
    pcb.add_trace((10, 50), (100, 50), width=1, layer="B.Cu", net="P")
    if landing == "bonded":
        pcb.add_via(100, 50, size=1, drill=0.5, net="P")
    lane = pcb.segments[4:6] if landing == "insulated" else [pcb.segments[5]]
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"
    assert {id(s) for s in result.loop_free_segments} == {id(s) for s in lane}


def test_partial_reinforcement_respects_declared_ineligibility():
    pcb = _lane_with_loop_and_spur()
    spec = replace(_spec(), reinforcement_eligible=False)
    assert reinforcement_path_resolutions(pcb, [spec]).partial == ()
    assert not reinforcement_eligible_segment_ids(pcb, [spec])


def _straight_lane_between_rows(stations, *, bypass=False, cut=False, footprint=None):
    """A series lane between two via rows, with a genuine loop on the front."""
    pcb = PCB.create(width=235, height=90, layers=4, center=False)
    for ref, x in (("J1", 12), ("J2", 218)):
        if footprint is None:
            _add_pad_footprint(pcb, ref=ref, x=x, y=55, net="P")
        else:
            pcb.add_footprint_from_file(footprint, ref, x, 55)
            pcb.assign_net_to_footprint_pad(ref, "1", "P")
    for x in (43, 45.5, 48, 172, 174.5, 177):
        pcb.add_via(x, 55, size=PAD, drill=DRILL, net="P")
    for a, b in ((12, 18), (18, 28), (28, 48), (172, 218)):
        pcb.add_trace((a, 55), (b, 55), width=2, layer="F.Cu", net="P")
    for a, b in pairwise(((18, 55), (18, 67), (28, 67), (28, 55))):
        pcb.add_trace(a, b, width=0.8, layer="F.Cu", net="P")
    for a, b in pairwise(stations):
        parts = ((a, 105), (115, b)) if cut and a < 105 < 115 < b else ((a, b),)
        for left, right in parts:
            pcb.add_trace((left, 55), (right, 55), width=3, layer="B.Cu", net="P")
    if bypass:
        for a, b in pairwise(((43, 55), (43, 72), (177, 72), (177, 55))):
            pcb.add_trace(a, b, width=1, layer="B.Cu", net="P")
    return pcb


@pytest.mark.parametrize("mode", ["ends", "spaced"])
@pytest.mark.parametrize("reverse_track", [False, True])
def test_series_lane_between_rows_is_independent_of_track_boundaries(mode, reverse_track):
    from kicad_tools.pcb.reinforce import reinforce_net

    reference = None
    for stations in (
        (43, 45.5, 48, 172, 174.5, 177),
        (43, 177),
        (43, 56, 164, 177),
        (43, 48.25, 48.5, 171.5, 171.75, 177),
    ):
        if reverse_track:
            stations = stations[::-1]
        pcb = _straight_lane_between_rows(stations)
        specs = [_spec(), replace(_spec(source="J2", sink="J1"), name="reverse")]
        tracks = list(pcb.segments)
        result = reinforce_net(
            pcb,
            "P",
            layer="B.Cu",
            current_paths=specs,
            anchor_mode=mode,
            all_runs=True,
            dry_run=True,
        )
        assert [u.status for u in result.unresolved_paths] == ["ambiguous", "ambiguous"]
        anchors = sorted((p.x, p.y) for p in result.placed)
        assert len(anchors) >= 2 and all(48 <= x <= 172 and y == 55 for x, y in anchors)
        if mode == "ends":
            (leg,) = [leg for leg in result.legs if leg.anchors]
            assert leg.length_mm == pytest.approx(124)
            assert len(anchors) == 2
            assert set(leg.paths) == {"force", "reverse"}
        if reference is None:
            reference = anchors
        assert anchors == reference
        assert [id(s) for s in pcb.segments] == [id(s) for s in tracks]
        if len(stations) == 2:
            assert id(tracks[-1]) not in reinforcement_eligible_segment_ids(pcb, specs)


def test_series_lane_shared_by_declarations_ends_at_each_terminal_row():
    from kicad_tools.pcb.reinforce import reinforce_net

    reference = None
    for stations in ((43, 45.5, 48, 98, 100.5, 103, 172, 174.5, 177), (43, 177)):
        pcb = _straight_lane_between_rows(stations)
        _add_pad_footprint(pcb, ref="J3", x=98, y=35, net="P")
        for x in (98, 100.5, 103):
            pcb.add_via(x, 55, size=PAD, drill=DRILL, net="P")
        pcb.add_trace((98, 55), (103, 55), width=2, layer="F.Cu", net="P")
        pcb.add_trace((98, 55), (98, 35), width=2, layer="F.Cu", net="P")
        specs = [_spec(), replace(_spec(sink="J3"), name="tap")]
        result = reinforce_net(pcb, "P", current_paths=specs, layer="B.Cu", dry_run=True)
        legs = sorted((leg.length_mm, sorted(leg.paths)) for leg in result.legs if leg.anchors)
        assert len(legs) == 2
        assert [names for _, names in legs] == [["force", "tap"], ["force"]]
        if reference is None:
            reference = legs
        assert legs == reference


@pytest.mark.parametrize(
    "stations", [(43, 177), (43, 45.5, 48, 172, 174.5, 177), (43, 56, 164, 177)]
)
def test_saved_inter_row_wire_checks_its_own_ends_and_drills(tmp_path, stations):
    from kicad_tools.pcb.reinforce import reinforce_net
    from kicad_tools.validate.rules.reinforcement_incomplete import ReinforcementIncompleteRule
    from tests.test_pcb_reinforce import _write_single_pad_footprint_lib

    pcb = _straight_lane_between_rows(stations, footprint=_write_single_pad_footprint_lib(tmp_path))
    result = reinforce_net(pcb, "P", current_paths=[_spec()], layer="B.Cu", dry_run=False)
    assert len(result.placed) == 2
    board = tmp_path / "series.kicad_pcb"
    pcb.save(board)
    written = PCB.load(board)
    rule = ReinforcementIncompleteRule([_spec()])
    design_rules = _design_rules_2oz()
    assert not rule.check(written, design_rules).violations
    (leg,) = rule.fully_wired_legs(written, design_rules)
    assert sorted((leg.points[0], leg.points[-1])) == [(48, 55), (172, 55)]

    # A missing wire hole cannot be supplied by the nearby plated barrels.
    hole = next(v for v in written.vias if v.drill > DRILL)
    written.remove_vias([hole])
    assert not rule.fully_wired_legs(written, design_rules)
    assert any("[a]" in v.message for v in rule.check(written, design_rules).violations)


@pytest.mark.parametrize("defect", ["bypass", "cut"])
@pytest.mark.parametrize("stations", [(43, 177), (43, 45.5, 48, 172, 174.5, 177)])
def test_parallel_return_or_cut_leaves_the_lane_unanchored(stations, defect):
    from kicad_tools.pcb.reinforce import reinforce_net

    pcb = _straight_lane_between_rows(stations, **{defect: True})
    result = reinforce_net(
        pcb, "P", layer="B.Cu", current_paths=[_spec()], all_runs=True, dry_run=True
    )
    assert not result.placed
    assert result.unresolved_paths[0].status == ("unresolved" if defect == "cut" else "ambiguous")


def test_unmodeled_copper_withholds_partial_reinforcement():
    """A same-net pour may bypass any 'bridge': nothing is loop-free then."""
    pcb = _lane_with_loop_and_spur()
    from kicad_tools.schema.pcb import Zone

    net = pcb.get_net_by_name("P")
    pcb._zones.append(
        Zone(net.number, "P", "F.Cu", polygon=[(50, 40), (150, 40), (150, 45), (50, 45)])
    )
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"
    assert result.loop_free_segments == ()
    assert not reinforcement_eligible_segment_ids(pcb, [_spec()])


def test_reinforce_ends_anchors_the_lane_and_reports_the_loop():
    from kicad_tools.pcb.reinforce import reinforce_net

    pcb = _lane_with_loop_and_spur()
    outcome = reinforce_net(pcb, "P", current_paths=[_spec()], dry_run=True, anchor_mode="ends")
    (unresolved,) = outcome.unresolved_paths
    assert unresolved.status == "ambiguous"
    assert unresolved.anchored_segments == 2
    anchored = [leg for leg in outcome.legs if leg.anchors]
    assert anchored, [(leg.status, leg.reason) for leg in outcome.legs]
    # Only the lane (y = 50, x from 30 to 200) receives anchors: never the
    # loop beside J1 (x <= 30) nor the spur to TP1.
    for leg in anchored:
        for anchor in leg.anchors:
            assert anchor.y == pytest.approx(50.0)
            assert 30.0 <= anchor.x <= 200.0


def test_cli_audit_quotes_loop_branches_in_the_sheet_frame(tmp_path, capsys):
    import argparse
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_witness_frame_8928 import _loop_board

    pcb_path, sidecar = _loop_board(tmp_path)
    args = argparse.Namespace(
        pcb_command="current-paths-audit",
        pcb=str(pcb_path),
        current_paths=str(sidecar),
        format="json",
    )
    assert run_pcb_command(args) == 0
    (res,) = json.loads(capsys.readouterr().out)["resolutions"]
    branches = res["witness"]["loop_branches"]
    assert len(branches) == 2
    # Board-relative junctions (50, 50) and (70, 50), shifted by (48.5, 55).
    for b in branches:
        assert sorted([tuple(b["start"]), tuple(b["end"])]) == [(98.5, 105.0), (118.5, 105.0)]
    assert "[loop branches: " in res["reason"]


def test_cli_reinforce_json_reports_partial_anchoring(tmp_path, capsys):
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_pcb_reinforce import _reinforce_args
    from tests.test_witness_frame_8928 import _loop_board

    pcb_path, sidecar = _loop_board(tmp_path)
    rc = run_pcb_command(
        _reinforce_args(pcb_path, current_paths=str(sidecar), format="json", dry_run=True)
    )
    assert rc == 0
    captured = capsys.readouterr()
    (entry,) = json.loads(captured.out)["unresolved_paths"]
    # The lane J1 -> J2 is ONE segment the loop rides for 20 mm: no part of
    # it is loop-free, so the whole path still gets no anchors.
    assert entry["anchored_segments"] == 0
    assert "it gets no anchors" in captured.err
