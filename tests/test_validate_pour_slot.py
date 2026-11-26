"""Tests for the pour-slot advisory rule (Issue #8435)."""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.validate.rules.pour_slot import PourSlotRule


@dataclass
class _Seg:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 1
    net_name: str = "A"


@dataclass
class _Zone:
    polygon = ((-50, -50), (100, -50), (100, 100), (-50, 100))
    net_name: str = "GND"
    layer: str = "F.Cu"
    layers: list = field(default_factory=list)
    keepout: object = None
    clearance: float = 0.3
    min_thickness: float = 0.25


@dataclass
class _PCB:
    segments: list = field(default_factory=list)
    zones: list = field(default_factory=list)


def _pair(gap, **kw):
    """Two 0.2 mm tracks 10 mm long with the given edge-to-edge gap."""
    pitch = gap + 0.2
    return [
        _Seg((0, 0), (10, 0)),
        _Seg((0, pitch), (10, pitch), net_number=2, net_name="B", **kw),
    ]


def _run(segs, zones=(None,), rule=None):
    zs = [_Zone()] if zones == (None,) else list(zones)
    return (rule or PourSlotRule()).check(_PCB(segs, zs), None)


# Threshold with defaults: 2 * 0.3 + 0.25 = 0.85 mm.  A pour slot exists only
# for a gap with a fillable strip the fill then drops: 2 * clearance < gap <
# 2 * clearance + min_thickness, i.e. (0.6, 0.85) mm here (Issue #10121).


def test_flagged_pair():
    res = _run(_pair(0.7))
    assert [v.rule_id for v in res.violations] == ["pour_slot"]
    v = res.violations[0]
    assert v.severity == "warning"
    assert "10.00 mm" in v.message
    assert abs(v.actual_value - 0.7) < 1e-6
    assert abs(v.required_value - 0.85) < 1e-6


def test_packed_pair_not_reported():
    """A pair at or below 2 x clearance leaves no fillable strip to drop.

    The copper-free channel between packed tracks is the unavoidable
    clearance void of two different-net conductors at that spacing -- the
    same void a fine-pitch pad row produces with no tracks at all -- so it
    is not a pour slot (Issue #10121, native-refill controls).
    """
    for gap in (0.05, 0.25, 0.5, 0.599, 0.6):
        assert _run(_pair(gap)).violations == [], gap


def test_packed_boundary_is_the_strips_lower_edge():
    # Just past the packed tolerance a fillable strip exists again and is
    # dropped: 0.6 + 1.5 x tolerance leaves a 0.0015 mm strip (< min_thickness).
    res = _run(_pair(0.6015))
    assert len(res.violations) == 1
    assert abs(res.violations[0].actual_value - 0.6015) < 1e-6


def test_pair_at_or_above_fill_width_is_clean():
    assert _run(_pair(0.85)).violations == []
    assert _run(_pair(1.5)).violations == []


def test_same_net_pair_not_reported():
    segs = _pair(0.5)
    segs[1].net_number, segs[1].net_name = 1, "A"
    assert _run(segs).violations == []


def test_layer_without_zone_not_reported():
    assert _run(_pair(0.5), zones=[_Zone(layer="B.Cu")]).violations == []
    assert _run(_pair(0.5), zones=[]).violations == []


def test_touching_or_overlapping_left_to_drc():
    assert _run(_pair(0.0)).violations == []
    assert _run(_pair(-0.1)).violations == []


def test_unequal_netclass_clearances_bound_each_side_of_the_strip():
    thick = _Zone(clearance=0.2, min_thickness=0.3)
    thin = _Zone(clearance=0.2, min_thickness=0.025)
    for wider in ("A", "B"):
        rule = PourSlotRule({wider: 0.4})
        assert not _run(_pair(0.6), [thick], rule).violations
        (slot,) = _run(_pair(0.65), [thick], rule).violations
        assert abs(slot.required_value - 0.9) < 1e-6
        assert abs(slot.location[1] - (0.525 if wider == "A" else 0.325)) < 1e-6
        assert not _run(_pair(0.65), [thin], rule).violations
        assert not _run(_pair(0.9), [thick], rule).violations
        assert not _run(_pair(0.95), [thick], rule).violations


def test_zone_netclass_sets_a_floor_on_both_facing_clearances():
    zone = _Zone(clearance=0.2, min_thickness=0.3)
    rule = PourSlotRule({"GND": 0.4})
    assert not _run(_pair(0.65), [zone], rule).violations
    (slot,) = _run(_pair(0.95), [zone], rule).violations
    assert abs(slot.required_value - 1.1) < 1e-6


def test_zone_clearance_wins_over_smaller_netclass():
    res = _run(_pair(0.7), rule=PourSlotRule({"B": 0.1}))
    assert abs(res.violations[0].required_value - 0.85) < 1e-6


def test_collinear_non_overlapping_not_reported():
    segs = [_Seg((0, 0), (5, 0)), _Seg((5.1, 0), (10, 0), net_number=2, net_name="B")]
    assert _run(segs).violations == []


def test_side_by_side_but_no_overlap_not_reported():
    segs = [_Seg((0, 0), (5, 0)), _Seg((6, 0.7), (10, 0.7), net_number=2, net_name="B")]
    assert _run(segs).violations == []


def test_diagonal_pair_flagged_and_perpendicular_ignored():
    d = 0.7 / 2**0.5 + 0.2 / 2**0.5  # perpendicular pitch 0.9 along the diagonal normal
    segs = [
        _Seg((0, 0), (10, 10)),
        _Seg((-d, d), (10 - d, 10 + d), net_number=2, net_name="B"),
    ]
    assert len(_run(segs).violations) == 1
    cross = [_Seg((0, 0), (10, 0)), _Seg((5, -3), (5, 3), net_number=2, net_name="B")]
    assert _run(cross).violations == []


def test_keepout_zone_is_not_a_pour():
    assert _run(_pair(0.5), zones=[_Zone(keepout=object())]).violations == []


def test_multi_zone_layer_uses_widest_requirement():
    zones = [_Zone(), _Zone(clearance=0.5, min_thickness=0.25)]
    # Widest requirement: band (1.0, 1.25) mm, so 1.1 mm is flagged.
    res = _run(_pair(1.1), zones=zones)
    assert len(res.violations) == 1
    assert abs(res.violations[0].required_value - 1.25) < 1e-6


def test_each_pair_reported_once():
    segs = _pair(0.7) + [_Seg((0, 1.8), (10, 1.8), net_number=3, net_name="C")]
    # A-B gap 0.7 and B-C gap 0.7 (both flagged); A-C gap 1.6 clean.
    assert len(_run(segs).violations) == 2


def test_duplicated_segment_reports_slot_once():
    # Issue #8503: two identical copies of a track (already reported by
    # redundant_copper_duplicate) next to a neighbour are one slot, not two.
    segs = _pair(0.7)
    segs.append(_Seg((0, 0), (10, 0)))
    assert len(_run(segs).violations) == 1
    # Reversed endpoint order is the same copper too.
    segs.append(_Seg((10, 0), (0, 0)))
    assert len(_run(segs).violations) == 1


def test_duplicate_of_both_tracks_reports_slot_once():
    segs = _pair(0.7) + _pair(0.7)
    assert len(_run(segs).violations) == 1


def test_parallel_copy_with_different_width_is_distinct():
    # Not an exact duplicate (different width): it is distinct geometry
    # (A-C gap 0.675 mm is also inside the dropped-island band).
    segs = _pair(0.7)
    segs.append(_Seg((0, 0), (10, 0), width=0.25))
    assert len(_run(segs).violations) == 2


def test_two_zones_on_one_layer_report_each_pair_once():
    # Issue #8503: several pours sharing a layer must not multiply findings.
    res = _run(_pair(0.7), zones=[_Zone(), _Zone(clearance=0.2)])
    assert len(res.violations) == 1
    # The widest requirement of the layer's pours applies.
    assert abs(res.violations[0].required_value - 0.85) < 1e-6
