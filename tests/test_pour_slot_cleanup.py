"""Tests for the pour-slot cleanup pass (Issue #8440)."""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.optim.pour_slot_cleanup import _plan_shift, cleanup_pour_slots
from kicad_tools.router.quantize import ANGLE_TOL_DEG, off_angle_degrees
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.pour_slot import PourSlotRule, iter_pour_slot_pairs


@dataclass
class _Zone:
    rule_area_contours: list = field(default_factory=list)
    polygon = ((-50, -50), (100, -50), (100, 100), (-50, 100))
    layer: str = "F.Cu"
    layers: list = field(default_factory=list)
    keepout: object = None
    clearance: float = 0.3
    min_thickness: float = 0.25


# Threshold with the defaults above: 2 * 0.3 + 0.25 = 0.85 mm.


def _board(gap, *, b_end=(10.0, None), extra=None):
    pcb = PCB.create(width=50, height=50, center=False)
    pcb.zones.append(_Zone())
    pitch = gap + 0.2
    pcb.add_trace((10, 10), (20, 10), width=0.2, layer="F.Cu", net="A")
    by = 10 + pitch
    pcb.add_trace((10, by), (b_end[0] + 10, by), width=0.2, layer="F.Cu", net="B")
    for fn in extra or []:
        fn(pcb)
    return pcb


def _ok(_pcb):
    return {"clearance": 0, "unconnected": 0}


def _gaps(pcb):
    return sorted(round(p.gap, 3) for p in iter_pour_slot_pairs(pcb))


def _seg(pcb, name):
    return next(s for s in pcb.segments if s.net_name == name)


def test_spread_widens_gap_to_threshold():
    pcb = _board(0.7)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.spread == 1 and res.left_alone == 0
    assert list(iter_pour_slot_pairs(pcb)) == []
    gap = abs(_seg(pcb, "B").start[1] - _seg(pcb, "A").start[1]) - 0.2
    assert gap >= 0.85


def test_cleanup_uses_the_checker_bounds_for_unequal_clearances():
    for gap, thickness, count in ((0.65, 0.3, 1), (0.65, 0.025, 0), (0.95, 0.3, 0)):
        pcb = _board(gap)
        pcb.zones[0].clearance = 0.2
        pcb.zones[0].min_thickness = thickness
        result = cleanup_pour_slots(pcb, referee=_ok, netclass_clearances={"A": 0.4})
        assert result.pairs_before == count
        assert result.spread == count
        assert result.pairs_after == 0


def test_pack_when_spread_blocked_by_third_trace():
    def blocker(pcb):  # a net-C via just beyond where spread would land
        pcb.add_via(15, 11.4, size=0.6, net="C")  # blocks spreading B
        pcb.add_via(15, 9.3, size=0.6, net="C")  # blocks spreading A

    pcb = _board(0.7, extra=[blocker])
    res = cleanup_pour_slots(pcb, referee=_ok, pack=True)
    assert res.packed == 1 and res.spread == 0
    gap = abs(_seg(pcb, "B").start[1] - _seg(pcb, "A").start[1]) - 0.2
    assert abs(gap - 0.3) < 1e-3


def test_pack_is_off_by_default():
    def blocker(pcb):
        pcb.add_via(15, 11.4, size=0.6, net="C")
        pcb.add_via(15, 9.3, size=0.6, net="C")

    pcb = _board(0.7, extra=[blocker])
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.packed == 0 and res.spread == 0
    assert res.left_alone == 1 and not res.changed
    assert [(s.start, s.end) for s in pcb.segments] == before


def test_left_alone_by_reason_counts_each_pair_once():
    def blocker(pcb):
        pcb.add_via(15, 11.4, size=0.6, net="C")
        pcb.add_via(15, 9.3, size=0.6, net="C")
        pcb.add_via(15, 10.4, size=0.6, net="D")

    pcb = _board(0.7, extra=[blocker])
    res = cleanup_pour_slots(pcb, referee=_ok, pack=True)
    by_reason = res.to_dict()["left_alone_by_reason"]
    assert res.left_alone >= 1
    assert sum(by_reason.values()) == res.left_alone
    assert len(res.left_alone_reasons) == res.left_alone


def test_left_alone_when_neither_is_clean():
    def blocker(pcb):
        pcb.add_via(15, 11.4, size=0.6, net="C")  # blocks spreading B
        pcb.add_via(15, 9.3, size=0.6, net="C")  # blocks spreading A
        pcb.add_via(15, 10.4, size=0.6, net="D")  # blocks packing either run

    pcb = _board(0.7, extra=[blocker])
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.left_alone >= 1
    assert [(s.start, s.end) for s in pcb.segments] == before


def test_referee_regression_reverts_edit():
    calls = []

    def referee(_pcb):
        calls.append(1)
        return {"clearance": 0, "unconnected": 0} if len(calls) == 1 else {"unconnected": 1}

    pcb = _board(0.7)
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=referee)
    assert not res.changed and res.left_alone == 1
    assert sorted((s.start, s.end) for s in pcb.segments) == sorted(before)


def test_unavailable_referee_refuses_without_editing():
    pcb = _board(0.7)
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=lambda _p: None)
    assert res.refused_reason and not res.changed
    assert [(s.start, s.end) for s in pcb.segments] == before


def test_connected_neighbour_follows_moved_run():
    def stub(pcb):  # B continues perpendicular from its end
        pcb.add_trace((20, 10.9), (20, 14), width=0.2, layer="F.Cu", net="B")

    pcb = _board(0.7, extra=[stub])
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.spread == 1
    run = next(s for s in pcb.segments if s.net_name == "B" and s.start[0] != s.end[0])
    stub_seg = next(s for s in pcb.segments if s.net_name == "B" and s.start[0] == s.end[0])
    assert stub_seg.start == run.end  # connectivity preserved


def _diagonal(pcb):  # B continues at 45 degrees from its end
    pcb.add_trace((20, 10.9), (24, 14.9), width=0.2, layer="F.Cu", net="B")


def _collinear(pcb):  # B continues straight on from its end as a second segment
    pcb.add_trace((20, 10.9), (30, 10.9), width=0.2, layer="F.Cu", net="B")


def _max_off_angle(pcb):
    return max(
        off_angle_degrees(s.end[0] - s.start[0], s.end[1] - s.start[1]) for s in pcb.segments
    )


def _joined(a, b):
    return any(
        abs(p[0] - q[0]) < 1e-3 and abs(p[1] - q[1]) < 1e-3
        for p in (a.start, a.end)
        for q in (b.start, b.end)
    )


def _connected(pcb, name):
    """True when the ``name`` segments form one chain joined at their endpoints."""
    segs = [s for s in pcb.segments if s.net_name == name]
    chain = [segs[0]]
    while True:
        more = [
            s for s in segs if all(s is not t for t in chain) and any(_joined(s, t) for t in chain)
        ]
        if not more:
            return len(chain) == len(segs)
        chain += more


def _net_geometry(pcb, name):
    return sorted((s.start, s.end) for s in pcb.segments if s.net_name == name)


def _b_segments(pcb):
    return [s for s in pcb.segments if s.net_name == "B"]


def test_diagonal_neighbour_keeps_its_angle_when_the_run_spreads():
    pcb = _board(0.7, extra=[_diagonal])
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.spread == 1 and list(iter_pour_slot_pairs(pcb)) == []
    assert _max_off_angle(pcb) <= ANGLE_TOL_DEG
    assert _connected(pcb, "B")
    # The joint slides along the diagonal to the run's shifted line; nothing
    # else about either segment changes.
    assert _net_geometry(pcb, "B") == [
        ((10.0, 11.055), (20.155, 11.055)),
        ((20.155, 11.055), (24.0, 14.9)),
    ]


def test_diagonal_neighbour_keeps_its_angle_when_the_run_packs():
    def blocked_spread(pcb):
        _diagonal(pcb)
        pcb.add_via(15, 11.4, size=0.6, net="C")  # blocks spreading B
        pcb.add_via(15, 9.3, size=0.6, net="C")  # blocks spreading A

    pcb = _board(0.7, extra=[blocked_spread])
    res = cleanup_pour_slots(pcb, referee=_ok, pack=True)
    assert res.packed == 1 and res.spread == 0
    assert _max_off_angle(pcb) <= ANGLE_TOL_DEG
    assert _connected(pcb, "B")
    assert _net_geometry(pcb, "B") == [((10.0, 10.5), (19.6, 10.5)), ((19.6, 10.5), (24.0, 14.9))]


def test_diagonal_on_each_end_slides_both_joints_and_keeps_the_far_via():
    def both_ends(pcb):
        _diagonal(pcb)
        pcb.add_trace((6, 6.9), (10, 10.9), width=0.2, layer="F.Cu", net="B")
        pcb.add_via(24, 14.9, net="B")

    pcb = _board(0.7, extra=[both_ends])
    vias = sorted(v.position for v in pcb.vias)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.spread == 1
    assert _max_off_angle(pcb) <= ANGLE_TOL_DEG
    assert _connected(pcb, "B")
    assert sorted(v.position for v in pcb.vias) == vias
    # Each corner travels along its own diagonal to the shifted line: the leg
    # arriving from below is lengthened, the leg leaving upwards is shortened.
    assert _net_geometry(pcb, "B") == [
        ((6.0, 6.9), (10.155, 11.055)),
        ((10.155, 11.055), (20.155, 11.055)),
        ((20.155, 11.055), (24.0, 14.9)),
    ]


def test_collinear_continuation_is_reached_through_a_45_degree_jog():
    pcb = _board(0.7, extra=[_collinear])
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.spread == 1
    assert _max_off_angle(pcb) <= ANGLE_TOL_DEG
    assert _connected(pcb, "B")
    assert _net_geometry(pcb, "B") == [
        ((10.0, 11.055), (19.845, 11.055)),
        ((20.0, 10.9), (19.845, 11.055)),
        ((20.0, 10.9), (30.0, 10.9)),
    ]


def test_locked_diagonal_neighbour_leaves_the_pair_alone():
    def locked_diagonal(pcb):
        _diagonal(pcb)
        next(s for s in _b_segments(pcb) if s.start[1] != s.end[1]).locked = True
        pcb.add_via(15, 9.3, size=0.6, net="C")  # A cannot spread either

    pcb = _board(0.7, extra=[locked_diagonal])
    before = sorted((s.start, s.end) for s in pcb.segments)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert not res.changed and res.left_alone == 1
    assert sorted((s.start, s.end) for s in pcb.segments) == before


def test_diagonal_edit_reverted_by_the_referee_restores_the_geometry():
    calls = []

    def referee(_pcb):
        calls.append(1)
        return {"clearance": 0, "unconnected": 0} if len(calls) == 1 else {"unconnected": 1}

    pcb = _board(0.7, extra=[_diagonal])
    before = sorted((s.start, s.end, s.width, s.layer) for s in pcb.segments)
    res = cleanup_pour_slots(pcb, referee=referee)
    assert not res.changed and res.left_alone == 1
    assert sorted((s.start, s.end, s.width, s.layer) for s in pcb.segments) == before


def _stub_pair():
    """Both runs 0.1 mm long; B continues straight on and A cannot spread."""
    pcb = PCB.create(width=50, height=50, center=False)
    pcb.zones.append(_Zone())
    pcb.add_trace((10, 10), (10.1, 10), width=0.2, layer="F.Cu", net="A")
    pcb.add_trace((10, 10.9), (10.1, 10.9), width=0.2, layer="F.Cu", net="B")
    pcb.add_trace((10.1, 10.9), (20, 10.9), width=0.2, layer="F.Cu", net="B")
    pcb.add_via(10.05, 9.3, size=0.6, net="C")
    return pcb


def test_no_room_for_the_jog_leaves_the_pair_alone():
    pcb = _stub_pair()
    before = sorted((s.start, s.end) for s in pcb.segments)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.pairs_before == 1
    assert not res.changed and res.left_alone == 1
    assert sorted((s.start, s.end) for s in pcb.segments) == before


def test_plan_shift_reports_a_jog_the_run_has_no_room_for():
    pcb = _stub_pair()
    stub = next(s for s in _b_segments(pcb) if s.start[0] == 10.0)
    plan, why = _plan_shift(pcb, stub, 0.0, 0.155, [])
    assert plan == [] and why == "no room for jog"


def test_plan_shift_refuses_a_zero_length_run_without_dividing_by_zero():
    pcb = _board(0.7)
    dot = next(s for s in _b_segments(pcb))
    dot.end = dot.start
    plan, why = _plan_shift(pcb, dot, 0.0, 0.155, [])
    assert plan == [] and why == "move would collapse a connected track"


def _via_ends(pcb):
    pcb.add_via(20, 10.9, net="B")
    pcb.add_via(20, 10.0, net="A")
    pcb.add_via(10, 10.9, net="B")
    pcb.add_via(10, 10.0, net="A")


def test_via_ended_runs_are_spread_with_45_degree_jogs():
    pcb = _board(0.7, extra=[_via_ends])
    vias = sorted(v.position for v in pcb.vias)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.changed and res.left_alone == 0
    assert sorted(v.position for v in pcb.vias) == vias  # vias never move
    assert list(iter_pour_slot_pairs(pcb)) == []
    for name in ("A", "B"):
        for via_xy in [v.position for v in pcb.vias if v.net_name == name]:
            # some same-net segment still ends exactly on the via
            assert any(
                s.net_name == name
                and (
                    (abs(s.start[0] - via_xy[0]) < 1e-3 and abs(s.start[1] - via_xy[1]) < 1e-3)
                    or (abs(s.end[0] - via_xy[0]) < 1e-3 and abs(s.end[1] - via_xy[1]) < 1e-3)
                )
                for s in pcb.segments
            )
    diag = [
        s
        for s in pcb.segments
        if abs(abs(s.end[0] - s.start[0]) - abs(s.end[1] - s.start[1])) < 1e-3
        and abs(s.end[0] - s.start[0]) > 1e-3
    ]
    assert len(diag) >= 2


def test_via_ended_run_left_alone_when_jog_blocked():
    def blocker(pcb):
        _via_ends(pcb)
        for y in (9.3, 11.4):  # blocks every spread leg
            pcb.add_via(17, y, size=0.6, net="C")
            pcb.add_via(13, y, size=0.6, net="C")
        pcb.add_via(15, 10.45, size=0.6, net="D")  # blocks packing

    pcb = _board(0.7, extra=[blocker])
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert not res.changed and res.left_alone >= 1
    assert [(s.start, s.end) for s in pcb.segments] == before


def test_different_net_via_at_endpoint_refuses():
    def via(pcb):
        pcb.add_via(20, 10.9, net="C")
        pcb.add_via(20, 10.0, net="C")

    pcb = _board(0.7, extra=[via])
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert not res.changed and res.left_alone == 1


def test_via_jog_referee_regression_reverts():
    calls = []

    def referee(_pcb):
        calls.append(1)
        return {"unconnected": 0} if len(calls) == 1 else {"unconnected": 1}

    pcb = _board(0.7, extra=[_via_ends])
    before = sorted((s.start, s.end) for s in pcb.segments)
    res = cleanup_pour_slots(pcb, referee=referee)
    assert not res.changed
    assert sorted((s.start, s.end) for s in pcb.segments) == before


def test_detection_shared_with_rule():
    pcb = _board(0.7)
    pairs = list(iter_pour_slot_pairs(pcb))
    violations = PourSlotRule().check(pcb, None).violations
    assert len(pairs) == len(violations) == 1
    assert pairs[0].location == violations[0].location


def test_idempotent():
    pcb = _board(0.7)
    cleanup_pour_slots(pcb, referee=_ok)
    snap = sorted((s.start, s.end) for s in pcb.segments)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert not res.changed
    assert sorted((s.start, s.end) for s in pcb.segments) == snap


def _bus(n_runs=4, gap=0.7, walls=False):
    pcb = PCB.create(width=50, height=50, center=False)
    pcb.zones.append(_Zone())
    for i in range(n_runs):
        y = 10 + i * (gap + 0.2)
        pcb.add_trace((10, y), (20, y), width=0.2, layer="F.Cu", net=f"N{i}")
    if walls:  # exactly clearance-wide channel on both sides
        top = 10 + (n_runs - 1) * (gap + 0.2)
        pcb.add_trace((8, 9.5), (22, 9.5), width=0.2, layer="F.Cu", net="WL")
        pcb.add_trace((8, top + 0.5), (22, top + 0.5), width=0.2, layer="F.Cu", net="WH")
    return pcb


def test_bus_is_repitched_jointly():
    pcb = _bus()
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.buses_repitched == 1
    assert list(iter_pour_slot_pairs(pcb)) == []
    ys = sorted(s.start[1] for s in pcb.segments)
    assert all(b - a - 0.2 >= 0.85 for a, b in zip(ys, ys[1:], strict=False))


def test_boxed_in_bus_reports_channel_message():
    pcb = _bus(walls=True)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.buses_repitched == 0
    assert any(r.startswith("channel too narrow: needs") and "has" in r for r in res.bus_reports)
