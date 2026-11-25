"""Issue #9019: stitch copper keeps out of the same-net TRACE slit band.

The #8957/#9846 guards hold the stitch VIA annulus and the emitted STUB
TRACK out of the slit band beside same-net SMD *pads*.  Nothing constrained
the same-net TRACE population: on the board 06 seed-42 measurement
(workflow 45193223262, routing source ``046f1bcc3``) the pad-aware stitch
step seated a GND stitch via at (151.29, 72.03) whose 0.45 mm annulus left
0.0290 mm of air to the 0.2 mm GND escape stub at x=150.96 -- neither a
copper union nor clear of the fab's 0.1016 mm ``trace_spacing`` detection
edge, so the fab-view ``physical_copper_gap`` grading reports it as an
error on the saved Python board while the C++ board (different routed
copper, different stitch sites) has ``slits.json == []``.  #9936 assigned
the residual to this issue.

The stitcher now applies the #9208 two-region trace-population band to
every same-net copper pair its placement can make: via annulus vs
same-net tracks, emitted legs vs same-net tracks, and emitted legs vs
same-net vias.  Overlap (gap <= 0, one copper union -- the trace
population accepts the tangent seat, #9208) and gaps at or beyond the
detection edge stay acceptable; the band in between relocates the
ladder, and the commit chokepoint drops a placement no strategy could
seat legally (reported, never silently lost).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kicad_tools.cli.stitch_cmd import (
    PadInfo,
    TrackSegment,
    _resolve_mfr_trace_gap_edge,
    _stub_legs_same_net_copper_band_ok,
    _via_same_net_track_band_ok,
    calculate_via_stub,
    run_stitch,
    trace_points,
)

from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

VIA = 0.45
DRILL = 0.3
CLEARANCE = 0.1
R = VIA / 2  # 0.225

#: jlcpcb 4layer_1oz ``trace_spacing`` detection edge (Issue #9208).
EDGE_TRACE = 0.1016

#: The issue's worked-example pad: 0.3 x 0.7 SMD rect centred at the origin.
PAD_BBOX = (-0.15, -0.35, 0.15, 0.35)

TRACK_HALF = 0.1  # the recipe's 0.2 mm copper, half width

#: Foreign-net pad walls east / west / south of the test pad, so the only
#: live ladder direction is north and every candidate order is pinned.
FOREIGN_BBOX_E = (0.6, -1.0, 1.6, 1.0, 2, "X1", 1.0, 2.0)
FOREIGN_BBOX_W = (-1.6, -1.0, -0.6, 1.0, 2, "X2", 1.0, 2.0)
FOREIGN_BBOX_S = (-1.0, -1.6, 1.0, -0.6, 2, "X3", 2.0, 1.0)
FOREIGN_BBOXES = [FOREIGN_BBOX_E, FOREIGN_BBOX_W, FOREIGN_BBOX_S]

PAD = PadInfo(
    reference="Q1",
    pad_number="1",
    net_number=1,
    net_name="GND",
    x=0.0,
    y=0.0,
    layer="F.Cu",
    width=0.3,
    height=0.7,
)


def _track(x: float, y0: float = -1.5, y1: float = 1.5, layer: str = "F.Cu") -> TrackSegment:
    """A vertical same-net track whose centerline sits at ``x``."""
    return TrackSegment(
        start_x=x, start_y=y0, end_x=x, end_y=y1, width=0.2, layer=layer, net_number=1
    )


def test_trace_gap_edge_resolves_from_jlcpcb_table():
    assert _resolve_mfr_trace_gap_edge("jlcpcb", 4, 1.0) == pytest.approx(EDGE_TRACE)


# -- via annulus vs same-net tracks: the two-region band ---------------------


def test_via_track_band_rejects_the_measured_gap():
    # The board 06 witness: 0.0290 mm of air between the annulus edge and
    # the same-net GND stub.
    x = R + TRACK_HALF + 0.029
    assert not _via_same_net_track_band_ok(0.0, 0.0, VIA, [_track(x)], EDGE_TRACE)


def test_via_track_band_rejects_any_in_band_gap():
    for gap in (0.001, 0.05, 0.101):
        x = R + TRACK_HALF + gap
        assert not _via_same_net_track_band_ok(0.0, 0.0, VIA, [_track(x)], EDGE_TRACE), gap


def test_via_track_band_accepts_overlapping_copper():
    # gap < 0: the annulus reaches the track -- one copper union, and the
    # via is connected to the copper it was placed to tie into.
    x = R + TRACK_HALF - 0.05
    assert _via_same_net_track_band_ok(0.0, 0.0, VIA, [_track(x)], EDGE_TRACE)


def test_via_track_band_accepts_the_tangent_seat():
    # Issue #9208: the TRACE population treats gap == 0 as a union (the C++
    # band's lower edge is ``1e-9 < gap``), unlike the pad population.
    x = R + TRACK_HALF
    assert _via_same_net_track_band_ok(0.0, 0.0, VIA, [_track(x)], EDGE_TRACE)


def test_via_track_band_accepts_gap_at_and_beyond_edge():
    for gap in (EDGE_TRACE, 0.2):
        x = R + TRACK_HALF + gap
        assert _via_same_net_track_band_ok(0.0, 0.0, VIA, [_track(x)], EDGE_TRACE)


def test_via_track_band_disabled_without_edge_or_tracks():
    x = R + TRACK_HALF + 0.029
    assert _via_same_net_track_band_ok(0.0, 0.0, VIA, [_track(x)], 0.0)
    assert _via_same_net_track_band_ok(0.0, 0.0, VIA, [], EDGE_TRACE)
    assert _via_same_net_track_band_ok(0.0, 0.0, VIA, None, EDGE_TRACE)


def test_via_track_band_ignores_other_layers():
    # A track on B.Cu shares no copper with an F.Cu-anchored stitch via's
    # annulus only when the via does not span it; the caller filters by the
    # via's spanned layers (see run_stitch), and an off-span track never
    # reaches the helper.  Layer filtering itself is exercised at the
    # run_stitch level below; here the helper must judge geometry only.
    x = R + TRACK_HALF + 0.029
    assert not _via_same_net_track_band_ok(0.0, 0.0, VIA, [_track(x, layer="F.Cu")], EDGE_TRACE)


# -- emitted legs vs same-net tracks and vias --------------------------------


def test_leg_band_rejects_parallel_track_in_band():
    x = 2 * TRACK_HALF + 0.05
    ok = _stub_legs_same_net_copper_band_ok(
        [(0.0, 0.0), (0.0, 0.55)], TRACK_HALF, [_track(x)], [], EDGE_TRACE
    )
    assert not ok


def test_leg_band_accepts_track_union_and_clear_gaps():
    for x in (
        2 * TRACK_HALF - 0.05,
        TRACK_HALF,
        2 * TRACK_HALF + EDGE_TRACE,
        2 * TRACK_HALF + 0.2,
    ):
        ok = _stub_legs_same_net_copper_band_ok(
            [(0.0, 0.0), (0.0, 0.55)], TRACK_HALF, [_track(x)], [], EDGE_TRACE
        )
        assert ok, x


def test_leg_band_rejects_via_in_band():
    # A leg passing 0.03 mm from a same-net via's annulus edge.
    x = R + TRACK_HALF + 0.03
    ok = _stub_legs_same_net_copper_band_ok(
        [(0.0, 0.0), (0.0, 0.55)], TRACK_HALF, [], [(x, 0.3, VIA, 1)], EDGE_TRACE
    )
    assert not ok


def test_leg_band_accepts_via_union_and_clear_gaps():
    for x in (R + TRACK_HALF - 0.05, R + TRACK_HALF, R + TRACK_HALF + EDGE_TRACE):
        ok = _stub_legs_same_net_copper_band_ok(
            [(0.0, 0.0), (0.0, 0.55)], TRACK_HALF, [], [(x, 0.3, VIA, 1)], EDGE_TRACE
        )
        assert ok, x


def test_leg_band_accepts_the_legs_own_via():
    # The terminating via of the very stub being checked: the leg endpoint
    # sits on its center, a full copper union.
    ok = _stub_legs_same_net_copper_band_ok(
        [(0.0, 0.0), (0.0, 0.55)], TRACK_HALF, [], [(0.0, 0.55, VIA, 1)], EDGE_TRACE
    )
    assert ok


def test_leg_band_disabled_without_edge_or_copper():
    x = TRACK_HALF + 0.05
    assert _stub_legs_same_net_copper_band_ok(
        [(0.0, 0.0), (0.0, 0.55)], TRACK_HALF, [_track(x)], [], 0.0
    )
    assert _stub_legs_same_net_copper_band_ok(
        [(0.0, 0.0), (0.0, 0.55)], TRACK_HALF, None, None, EDGE_TRACE
    )


# -- the placement ladder honours the band -----------------------------------


def _stub(**kw) -> tuple[float, float, tuple[float, float] | None] | None:
    base: dict[str, Any] = {
        "pad": PAD,
        "offset": 0.2,
        "via_size": VIA,
        "existing_vias": [],
        "clearance": CLEARANCE,
        "other_net_tracks": [],
        "other_net_vias": [],
        "other_net_pads": [],
        "trace_width": 0.2,
        "other_net_filled_polygons": [],
        "other_net_pad_bboxes": FOREIGN_BBOXES,
        "via_drill": DRILL,
    }
    base.update(kw)
    return calculate_via_stub(**base)


def test_via_track_control_seats_the_witness_slit():
    # Control (guard off): the ladder's first live rung north seats with
    # the annulus 0.029 mm from the same-net track -- the board 06 defect.
    stub = _stub(same_net_tracks=[_track(R + TRACK_HALF + 0.029)])
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)
    gap = (R + TRACK_HALF + 0.029) - R - TRACK_HALF
    assert gap == pytest.approx(0.029)


def test_via_track_band_relocates_or_refuses_the_rungs():
    # Guard on: every north rung's annulus sits the same 0.029 mm from the
    # vertical track (E/W/S rungs are foreign-blocked, diagonals fall
    # inside the foreign walls), so the straight strategy refuses them all
    # and run_stitch walks the dog-leg / extended ladders.
    stub = _stub(
        same_net_tracks=[_track(R + TRACK_HALF + 0.029)],
        same_net_trace_gap_mm=EDGE_TRACE,
    )
    assert stub is None


def test_via_track_band_accepts_a_clear_track():
    # The track at the detection edge does not constrain the rung.
    stub = _stub(
        same_net_tracks=[_track(R + TRACK_HALF + EDGE_TRACE + 0.2)],
        same_net_trace_gap_mm=EDGE_TRACE,
    )
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)


def test_leg_track_band_refuses_the_in_band_rungs():
    # The emitted north leg passes 0.05 mm from a parallel same-net track;
    # the rung vias themselves OVERLAP the track (union), so only the leg
    # check can reject them -- the U2-column shape.
    stub = _stub(same_net_tracks=[_track(2 * TRACK_HALF + 0.05)])
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)
    stub = _stub(same_net_tracks=[_track(2 * TRACK_HALF + 0.05)], same_net_trace_gap_mm=EDGE_TRACE)
    assert stub is None


def test_leg_via_band_refuses_the_in_band_rungs():
    # A same-net via beside the ladder (isolated from the same-net
    # via-spacing pool, which ``existing_vias`` alone feeds): the rung
    # legs pass 0.03 mm from its annulus and the strategy must refuse.
    via = (R + TRACK_HALF + 0.03, 0.3, VIA, 1)
    stub = _stub(same_net_vias=[via])
    assert stub is not None
    assert stub[:2] == (0.0, 0.55)
    stub = _stub(same_net_vias=[via], same_net_trace_gap_mm=EDGE_TRACE)
    assert stub is None


# -- run_stitch keeps committed copper out of the band -----------------------

_GND_TRACK = """  (segment (start {x} {y0}) (end {x} {y1}) (width 0.2) (layer "F.Cu") (net 1 "GND"))
"""


def _pcb_text(extra: str = "") -> str:
    text = TWO_LAYER_GND_ZONE_PCB.rstrip()
    return text[:-1] + extra + ")\n"


def _witness_pcb(track_x: float) -> str:
    """C1.pad1 at (109.49, 110); a vertical GND track at ``track_x`` whose
    copper leaves the first north rung's via annulus / leg in the band."""
    return _pcb_text(_GND_TRACK.format(x=track_x, y0=108.0, y1=112.0))


def _stitch(pcb: Path, gap_edge: float):
    return run_stitch(
        pcb,
        ["GND"],
        via_size=VIA,
        drill=DRILL,
        clearance=CLEARANCE,
        offset=0.2,
        dry_run=True,
        avoid_pad_overlap=True,
        same_net_trace_gap_mm=gap_edge,
    )


def _via_track_gaps(pcb: Path, result, track_x: float) -> list[float]:
    gaps = []
    for v in result.vias_added:
        # The track spans y 108..112, covering every rung.
        d = abs(v.via_x - track_x)
        gaps.append(d - v.size / 2 - TRACK_HALF)
    return gaps


def _leg_track_gaps(pcb: Path, result, track_x: float) -> list[float]:
    from kicad_tools.cli.stitch_cmd import segment_to_segment_distance

    gaps = []
    for trace in result.traces_added:
        pts = trace_points(trace)
        for (sx, sy), (ex, ey) in zip(pts, pts[1:], strict=False):
            d = segment_to_segment_distance(sx, sy, ex, ey, track_x, 108.0, track_x, 112.0)
            gaps.append(d - 2 * TRACK_HALF)
    return gaps


def test_run_stitch_control_seats_the_via_slit(tmp_path):
    # Guard off: the first north rung commits with its annulus 0.03 mm
    # from the same-net GND track -- the measured (151.29, 72.03) witness.
    track_x = 109.49 + R + TRACK_HALF + 0.03
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_witness_pcb(track_x))
    result = _stitch(pcb, 0.0)
    assert result.vias_added
    gaps = _via_track_gaps(pcb, result, track_x)
    assert any(1e-9 < g < EDGE_TRACE - 1e-9 for g in gaps), gaps


def test_run_stitch_keeps_via_annuli_out_of_the_band(tmp_path):
    track_x = 109.49 + R + TRACK_HALF + 0.03
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_witness_pcb(track_x))
    result = _stitch(pcb, EDGE_TRACE)
    assert result.vias_added or any(
        "same_net_trace_gap" in reason for _pad, reason in result.pads_skipped
    ), "connectivity must survive as a legal seat or an explicit reported drop"
    assert all(not (1e-9 < g < EDGE_TRACE - 1e-9) for g in _via_track_gaps(pcb, result, track_x))


def test_run_stitch_control_seats_the_leg_slit(tmp_path):
    # The track overlaps the rung vias (union) but sits 0.05 mm from the
    # emitted legs -- only the leg check can refuse the rungs.
    track_x = 109.49 + 2 * TRACK_HALF + 0.05
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_witness_pcb(track_x))
    result = _stitch(pcb, 0.0)
    assert result.vias_added
    gaps = _leg_track_gaps(pcb, result, track_x)
    assert any(1e-9 < g < EDGE_TRACE - 1e-9 for g in gaps), gaps


def test_run_stitch_keeps_stub_legs_out_of_the_track_band(tmp_path):
    track_x = 109.49 + 2 * TRACK_HALF + 0.05
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_witness_pcb(track_x))
    result = _stitch(pcb, EDGE_TRACE)
    assert result.vias_added or any(
        "same_net_trace_gap" in reason for _pad, reason in result.pads_skipped
    )
    assert all(not (1e-9 < g < EDGE_TRACE - 1e-9) for g in _leg_track_gaps(pcb, result, track_x))


def test_run_stitch_trace_band_ignores_opposite_layer_track(tmp_path):
    # Passing control: the same witness geometry on B.Cu shares no copper
    # with an F.Cu-spanning stitch via or its F.Cu legs... unless the via
    # spans B.Cu (it does: F.Cu-B.Cu through vias tie the pad into the B.Cu
    # pour).  The track therefore DOES bound the via on B.Cu -- the gate
    # must see it there, so this control pins the opposite-layer semantics:
    # legs (F.Cu only) are unconstrained, the via annulus is constrained on
    # every spanned layer.
    track_x = 109.49 + R + TRACK_HALF + 0.03
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        _pcb_text(
            _GND_TRACK.format(x=track_x, y0=108.0, y1=112.0).replace(
                '(layer "F.Cu")', '(layer "B.Cu")'
            )
        )
    )
    result = _stitch(pcb, 0.0)
    assert result.vias_added
    gaps = _via_track_gaps(pcb, result, track_x)
    assert any(1e-9 < g < EDGE_TRACE - 1e-9 for g in gaps), "control must seat the B.Cu slit"
    guard = _stitch(pcb, EDGE_TRACE)
    assert guard.vias_added or any(
        "same_net_trace_gap" in reason for _pad, reason in guard.pads_skipped
    )
    assert all(not (1e-9 < g < EDGE_TRACE - 1e-9) for g in _via_track_gaps(pcb, guard, track_x))


def test_run_stitch_trace_band_disabled_keeps_today_behavior(tmp_path):
    # same_net_trace_gap_mm=0 (no --mfr table) must not move any seat.
    track_x = 109.49 + R + TRACK_HALF + 0.03
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_witness_pcb(track_x))
    off = _stitch(pcb, 0.0)
    assert off.vias_added
    seat = (off.vias_added[0].via_x, off.vias_added[0].via_y)
    assert seat == (pytest.approx(109.49), pytest.approx(110.52))


# -- four-layer boards: the annulus spans the PHYSICAL stack ------------------
#
# A standard via is written F.Cu -> B.Cu whatever plane it targets
# (``ViaPlacement`` normalizes it), so with the GND plane on In1.Cu a
# same-net track on In2.Cu / B.Cu still forms a slit against the annulus.
# The two-layer fixtures above cannot reach this: the plane target there IS
# B.Cu, so the plane span is already the whole stack.


def _four_layer_pcb_text(layer: str, track_x: float) -> str:
    from tests.test_stitch_mfr import FOUR_LAYER_GND_ZONE_PCB

    track = _GND_TRACK.format(x=track_x, y0=108.0, y1=112.0).replace(
        '(layer "F.Cu")', f'(layer "{layer}")'
    )
    text = FOUR_LAYER_GND_ZONE_PCB.rstrip()
    return text[:-1] + track + ")\n"


@pytest.mark.parametrize("layer", ["In2.Cu", "B.Cu"])
def test_run_stitch_four_layer_inner_plane_guards_below_plane_tracks(tmp_path, layer):
    track_x = 109.49 + R + TRACK_HALF + 0.03
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_four_layer_pcb_text(layer, track_x))
    control = _stitch(pcb, 0.0)
    assert control.vias_added
    assert any(1e-9 < g < EDGE_TRACE - 1e-9 for g in _via_track_gaps(pcb, control, track_x)), (
        "control must seat the slit against the track below the In1.Cu plane"
    )
    guard = _stitch(pcb, EDGE_TRACE)
    assert guard.vias_added or any(
        "same_net_trace_gap" in reason for _pad, reason in guard.pads_skipped
    )
    assert all(not (1e-9 < g < EDGE_TRACE - 1e-9) for g in _via_track_gaps(pcb, guard, track_x))


def test_stub_legs_band_only_sees_tracks_on_the_leg_layer():
    # Centreline 0.3 mm off a 0.2 mm leg leaves a 0.1 mm gap (in band).  On
    # In1.Cu it shares no copper with an F.Cu leg: ``leg_layer`` ignores it.
    on_in1 = [_track(0.3, layer="In1.Cu")]
    points = [(0.0, 0.0), (0.0, 0.6)]

    def ok(**kw):
        return _stub_legs_same_net_copper_band_ok(
            points, TRACK_HALF, on_in1, None, EDGE_TRACE, **kw
        )

    assert not ok()
    assert ok(leg_layer="F.Cu")
    assert not ok(leg_layer="In1.Cu")
