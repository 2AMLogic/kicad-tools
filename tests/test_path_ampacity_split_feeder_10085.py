"""Issue #10085: a buried stroke near a T-junction warns only when the feeder is split.

A 0.2 mm stroke lying wholly inside the declared 2.4 mm feeder's copper was
reported as ``uncovered copper`` when the feeder was drawn as two pieces
meeting at the split point, but not when it was drawn as one. The stroke's
copper touches the round cap of the off-route 2.4 mm continuation beyond the
T-junction, so the split-carrier run wrongly demanded that off-route carrier
be on the resolved path -- even though the two on-path feeder pieces alone
already cover the whole stroke.

The x = 75.0 control keeps both warnings: the stroke genuinely crosses onto
the continuation, whose copper is the only cover for its far end.
"""

from __future__ import annotations

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.pcb.reinforce import reinforce_net
from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint, audit_current_paths
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule
from tests.test_pcb_reinforce import _add_th_pad_footprint

_CONTINUATION_GEO = (72.0, 119.6, 77.5375, 119.6, 2.4)


def _feed_spec(current_a: float = 2.5) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="FEED",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def _design_rules_2oz() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
        outer_copper_oz=2.0,
        inner_copper_oz=2.0,
    )


def _split_feeder_board(*, split: bool, stroke_end: float) -> PCB:
    """The issue's reproduction board: J1 -> T-junction -> J2 plus off-route copper.

    Two through-hole single-pad terminals on F.Cu, J1.1 at (55, 119.6) and
    J2.1 at (72, 97), joined by a 2.4 mm feeder and drop. An off-route 2.4 mm
    continuation runs from the junction to (77.5375, 119.6), and a 0.2 mm
    stroke from (60, 119.9) to ``stroke_end`` is buried in the feeder band
    (offset 0.3 mm from its centreline, well inside the 1.2 mm half width).
    """
    pcb = PCB.create(width=200, height=120, center=False)
    _add_th_pad_footprint(pcb, ref="J1", x=55.0, y=119.6, net="NET1", drill=0.5, size=1.0)
    _add_th_pad_footprint(pcb, ref="J2", x=72.0, y=97.0, net="NET1", drill=0.5, size=1.0)
    if split:
        pcb.add_trace((55, 119.6), (65, 119.6), width=2.4, layer="F.Cu", net="NET1")
        pcb.add_trace((65, 119.6), (72, 119.6), width=2.4, layer="F.Cu", net="NET1")
    else:
        pcb.add_trace((55, 119.6), (72, 119.6), width=2.4, layer="F.Cu", net="NET1")
    pcb.add_trace((72, 119.6), (72, 97), width=2.4, layer="F.Cu", net="NET1")
    pcb.add_trace((72, 119.6), (77.5375, 119.6), width=2.4, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 119.9), (stroke_end, 119.9), width=0.2, layer="F.Cu", net="NET1")
    return pcb


def _uncovered_geometry(
    pcb: PCB, spec: CurrentPathSpec
) -> set[tuple[float, float, float, float, float]]:
    """Uncovered segments of the audit as ``(x1, y1, x2, y2, width)`` tuples."""
    audit = audit_current_paths(pcb, [spec])
    return {
        (
            round(s.start[0], 4),
            round(s.start[1], 4),
            round(s.end[0], 4),
            round(s.end[1], 4),
            s.width,
        )
        for s in audit.uncovered.get(spec.net_name, [])
    }


@pytest.mark.parametrize(
    "split,stroke_end,stroke_warns",
    [
        (False, 71.2, False),  # one-piece feeder: only the continuation warns
        (True, 71.2, False),  # split feeder, stroke still inside declared copper
        (True, 70.0, False),  # stroke short of the continuation's cap
        (True, 75.0, True),  # control: stroke crosses onto the continuation
    ],
)
def test_buried_stroke_warns_only_when_it_leaves_declared_copper(
    split: bool, stroke_end: float, stroke_warns: bool
) -> None:
    pcb = _split_feeder_board(split=split, stroke_end=stroke_end)
    uncovered = _uncovered_geometry(pcb, _feed_spec())
    assert _CONTINUATION_GEO in uncovered, "the off-route continuation must always warn"
    strokes = {geo for geo in uncovered if geo[4] == 0.2}
    if stroke_warns:
        assert strokes, f"the stroke crossing onto the continuation must warn, got {uncovered}"
        assert uncovered == {_CONTINUATION_GEO} | strokes
    else:
        assert not strokes, f"buried 0.2 mm stroke must not warn, uncovered={uncovered}"
        assert uncovered == {_CONTINUATION_GEO}


def test_split_and_unsplit_boards_agree_at_71_2() -> None:
    spec = _feed_spec()
    unsplit = _uncovered_geometry(_split_feeder_board(split=False, stroke_end=71.2), spec)
    split = _uncovered_geometry(_split_feeder_board(split=True, stroke_end=71.2), spec)
    assert unsplit == split == {_CONTINUATION_GEO}


@pytest.mark.parametrize(
    "split,stroke_end,expected_warnings",
    [
        (False, 71.2, 1),
        (True, 71.2, 1),
        (True, 70.0, 1),
        (True, 75.0, 2),
    ],
)
def test_rule_reports_expected_warning_count(
    split: bool, stroke_end: float, expected_warnings: int
) -> None:
    pcb = _split_feeder_board(split=split, stroke_end=stroke_end)
    results = PathAmpacityRule(specs=[_feed_spec()]).check(pcb, _design_rules_2oz())
    assert results.errors == []
    uncovered = [v for v in results.warnings if "not covered by any resolved path" in v.message]
    assert len(uncovered) == expected_warnings
    stroke_warnings = [v for v in uncovered if abs(v.location[1] - 119.9) < 1e-6]
    assert len(stroke_warnings) == (1 if expected_warnings == 2 else 0)


def test_reinforce_output_unchanged_by_the_split_and_the_fix() -> None:
    spec = _feed_spec()
    outcomes = {}
    for split in (False, True):
        pcb = _split_feeder_board(split=split, stroke_end=71.2)
        outcome = reinforce_net(pcb, "NET1", current_paths=[spec], all_runs=True)
        outcomes[split] = (
            outcome.run_length_mm,
            outcome.run_segment_count,
            [(round(a.x, 4), round(a.y, 4)) for a in outcome.placed],
        )
    assert outcomes[False] == outcomes[True]
    assert outcomes[True][0] == pytest.approx(39.6, abs=0.05)
    assert len(outcomes[True][2]) == 2
