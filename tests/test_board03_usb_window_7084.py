"""Board 03 ``usb_geometry()`` via-in-pad window exception (issue #7084).

Under the board's own ``jlcpcb-tier1`` profile via-in-pad drops the USB_D
launch vias into the ESD lands (board-relative y = 11.737 / 17.419), against
y = 12.638 / 16.890 under plain ``jlcpcb``.  The fixed y = 13..16.8 window
clipped both tier-1 ends, so the gate measured a coupled In2.Cu run far
shorter than the one the router laid.  The operator ruled (2026-10-19,
Q10 = 1) to widen the window *only* for vias inside the ESD / termination
lands: plain ``jlcpcb`` keeps the fixed window.  The 4.0 mm coupled bound,
the length and skew bounds and the MCU-launch limit are unchanged.

Issue #10561: the common run is the copper *inside* the window.  The old
filter dropped any track with an endpoint outside it, so the same plotted
copper measured 4.071 mm or "missing" depending on where KiCad split a
centerline; the run is now clipped to the window and collinear pieces of one
width are joined before coupling is measured.
"""

from __future__ import annotations

import dataclasses
import datetime
import importlib.util
import math
import re
import sys
from pathlib import Path

import pytest

from kicad_tools.geometry.copper import pad_size_box
from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parents[1] / "boards" / "03-usb-joystick"
# Issue #10561: staged under engineering/ until the producer-run reissue (#8900)
# moves it into the live recipe, which must stay equal to the archived copy.
STAGED_ROUTING_PLAN = BOARD / "engineering" / "routing_plan_10561.py"
ROUTED = BOARD / "output" / "usb_joystick_routed.kicad_pcb"

# Measured launch-via positions (board-relative y, mm), seed 42, lattice.
TIER1_LAUNCH_Y = (11.737, 17.419)
JLCPCB_LAUNCH_Y = (12.638, 16.890)

# The tier-1 route's USB_D launch vias at y >= 11 mm (same route).
TIER1_LAUNCH_VIAS = {
    "USB_D+": [(37.05, 11.737), (39.379, 17.419)],
    "USB_D-": [(38.95, 11.737), (40.479, 17.419)],
}

# The USB_D copper at y >= 11 mm (ESD launch to termination) of a
# ``--manufacturer jlcpcb-tier1`` lattice route of board 03 (seed 42,
# committed net-class map, the ``tests/router/test_board03_tier1_common_run.py``
# recipe), as board-relative (layer, x0, y0, x1, y1).  The connector-side
# copper (y < 11 mm) is kept from the committed board.
TIER1_CORRIDOR = {
    "USB_D+": [
        ("F.Cu", 37.2, 11.2, 37.0, 11.4),
        ("F.Cu", 37.0, 11.4, 37.0, 11.588),
        ("F.Cu", 37.0, 11.588, 37.05, 11.638),
        ("F.Cu", 37.05, 11.638, 37.05, 11.737),
        ("F.Cu", 39.45, 17.49, 39.379, 17.419),
        ("In2.Cu", 37.05, 11.737, 37.113, 11.8),
        ("In2.Cu", 37.113, 11.8, 38.025, 11.8),
        ("In2.Cu", 38.025, 11.8, 38.025, 13.073),
        ("In2.Cu", 38.025, 13.073, 38.225, 13.272),
        ("In2.Cu", 38.225, 13.272, 38.225, 14.472),
        ("In2.Cu", 38.225, 14.472, 38.825, 15.073),
        ("In2.Cu", 38.825, 15.073, 38.825, 16.865),
        ("In2.Cu", 38.825, 16.865, 39.379, 17.419),
    ],
    "USB_D-": [
        ("F.Cu", 38.8, 11.2, 39.0, 11.4),
        ("F.Cu", 39.0, 11.4, 39.0, 11.588),
        ("F.Cu", 39.0, 11.588, 38.95, 11.638),
        ("F.Cu", 38.95, 11.638, 38.95, 11.737),
        ("F.Cu", 40.55, 17.49, 40.479, 17.419),
        ("In2.Cu", 38.95, 11.737, 38.887, 11.8),
        ("In2.Cu", 38.887, 11.8, 38.375, 11.8),
        ("In2.Cu", 38.375, 11.8, 38.375, 12.927),
        ("In2.Cu", 38.375, 12.927, 38.575, 13.127),
        ("In2.Cu", 38.575, 13.127, 38.575, 14.328),
        ("In2.Cu", 38.575, 14.328, 39.175, 14.927),
        ("In2.Cu", 39.175, 14.927, 39.175, 16.6),
        ("In2.Cu", 39.175, 16.6, 39.66, 16.6),
        ("In2.Cu", 39.66, 16.6, 40.479, 17.419),
    ],
}


@pytest.fixture
def routing_plan():
    spec = importlib.util.spec_from_file_location("b03_routing_plan_7084w", STAGED_ROUTING_PLAN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(spec.name, None)


def _with_tier1_corridor(pcb: PCB) -> PCB:
    """Swap the committed board's USB_D launch-to-termination copper for tier-1's."""
    nets = {pcb.nets[n].name: n for n in pcb.nets}
    corridor = [
        s
        for s in pcb.segments
        if pcb.nets[s.net_number].name in TIER1_CORRIDOR and min(s.start[1], s.end[1]) >= 11.0
    ]
    assert corridor, "committed board lost its USB_D corridor copper"
    template = corridor[0]
    kept = [s for s in pcb.segments if s not in corridor]
    added = [
        dataclasses.replace(
            template,
            layer=layer,
            start=(x0, y0),
            end=(x1, y1),
            net_number=nets[name],
            net_name=name,
            uuid=f"tier1-{name}-{i}",
        )
        for name, segments in TIER1_CORRIDOR.items()
        for i, (layer, x0, y0, x1, y1) in enumerate(segments)
    ]
    pcb._segments[:] = kept + added
    _move_launch_vias(pcb, TIER1_LAUNCH_VIAS)
    return pcb


def _move_launch_vias(pcb: PCB, vias: dict[str, list[tuple[float, float]]]) -> PCB:
    """Replace the USB_D vias at y >= 11 mm with vias at ``vias``' positions."""
    nets = {pcb.nets[n].name: n for n in pcb.nets}
    launch = [v for v in pcb.vias if v.net_name in vias and v.position[1] >= 11.0]
    assert launch, "committed board lost its USB_D launch vias"
    template = launch[0]
    pcb._vias[:] = [v for v in pcb.vias if v not in launch] + [
        dataclasses.replace(
            template,
            position=pos,
            net_number=nets[name],
            net_name=name,
            uuid=f"launch-{name}-{i}",
        )
        for name, positions in vias.items()
        for i, pos in enumerate(positions)
    ]
    return pcb


def _load(monkeypatch, routing_plan, pcb: PCB) -> None:
    monkeypatch.setattr(routing_plan.PCB, "load", classmethod(lambda cls, path: pcb))


# Issue #10561: the USB_D copper the window above sees, as two KiCad
# partitions of the same centerlines (board-relative mm; ``net -> (width,
# points)``, each successive pair of points one In2.Cu track).  The six
# tracks of ``USB_RUN`` cross both window edges on each net, so the old
# endpoint filter dropped them.  ``USB_RUN_SPLITS`` subdivides the first and
# last track of each net at a point on its centerline: ten tracks, identical
# plotted copper.
USB_RUN = {
    "USB_D+": (0.1016, [(37.005, 12.7154), (39.5886, 15.299), (39.5886, 16.810)]),
    "USB_D-": (
        0.20,
        [
            (38.175, 12.8),
            (38.175, 13.1275),
            (40.175, 15.1275),
            (40.175, 16.515),
            (40.43, 16.77),
        ],
    ),
}
USB_RUN_SPLITS = {
    "USB_D+": {0: (37.501, 13.2114), 1: (39.5886, 16.799)},
    "USB_D-": {0: (38.175, 13.001), 3: (40.299, 16.639)},
}
USB_RUN_COMMON_MM = 4.215927
USB_RUN_LENGTHS_MM = {"USB_D+": 5.164762, "USB_D-": 4.904052}
USB_RUN_SKEW_MM = 0.260711
# A break in one net: ``net -> (track index, near point, far point)``, both
# points on that track's centerline.  The copper between them is removed,
# leaving a run on both sides inside the window.
USB_RUN_GAPS = {
    "USB_D+": (0, (38.5, 14.2104), (38.6, 14.3104)),
    "USB_D-": (1, (39.0, 13.9525), (39.1, 14.0525)),
}
# Tracks that end on a window corner, coming from outside: ``(start, end)``.
CORNER_CONTACTS = [(36.5, 12.0, 37.5, 13.0), (41.0, 17.5, 40.3, 16.8)]


def _subdivided(runs, nets):
    """``runs`` with the split points of each net in ``nets`` inserted."""
    out = {}
    for name, (width, points) in runs.items():
        out[name] = (width, points)
        if name in nets:
            split = USB_RUN_SPLITS[name]
            out[name] = (
                width,
                [q for i, p in enumerate(points) for q in (p, split.get(i)) if q is not None],
            )
    return out


def _tracks(points, gap=None):
    """Successive point pairs as tracks; ``gap``'s copper is cut from one track."""
    for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
        if gap is not None and gap[0] == index:
            yield start, gap[1]
            yield gap[2], end
        else:
            yield start, end


def _saved_run(
    tmp_path,
    runs,
    gaps=None,
    moved=None,
    extra=(),
    name="run",
):
    """Save ``runs`` as tracks and return the board path.

    ``gaps``: ``net -> USB_RUN_GAPS`` entry.  ``moved``: ``net`` or
    ``(net, track index)`` -> layer other than In2.Cu.  ``extra``:
    ``(net, width, start, end)`` tracks.
    """
    pcb = PCB.create(width=75, height=45, layers=4, center=False)
    for net, (width, points) in runs.items():
        pcb.add_net(net)
        gap = (gaps or {}).get(net)
        for index, (start, end) in enumerate(_tracks(points, gap)):
            layer = (moved or {}).get(net, (moved or {}).get((net, index), "In2.Cu"))
            pcb.add_trace(start, end, width=width, layer=layer, net=net)
    for net, width, start, end in extra:
        pcb.add_net(net)
        pcb.add_trace(start, end, width=width, layer="In2.Cu", net=net)
    path = tmp_path / f"{name}.kicad_pcb"
    pcb.save(path)
    return path


def _measured_common_mm(exc):
    """The coupled length recorded in a ``USB branch bounds failed`` message."""
    return float(re.search(r"common=([0-9.]+)", str(exc)).group(1))


def test_lands_are_the_boards_pads(routing_plan):
    """The exception's land boxes are the board's own U2 / R3 / R4 pad boxes."""
    pcb = PCB.load(ROUTED)
    fps = {fp.reference: fp for fp in pcb.footprints}
    expected = {
        ("esd", "USB_D+"): ("U2", "4"),
        ("esd", "USB_D-"): ("U2", "6"),
        ("termination", "USB_D+"): ("R4", "1"),
        ("termination", "USB_D-"): ("R3", "1"),
    }
    tables = {
        "esd": routing_plan.USB_ESD_LANDS_7084,
        "termination": routing_plan.USB_TERMINATION_LANDS_7084,
    }
    for (kind, net), (ref, number) in expected.items():
        fp = fps[ref]
        pad = next(p for p in fp.pads if p.number == number)
        assert pad.net_name == net
        minx, miny, maxx, maxy = pad_size_box(pad, fp.position, fp.rotation).bounds
        assert tables[kind][net] == pytest.approx((minx, maxx, miny, maxy), abs=1e-3)


def test_exception_is_tied_to_7487_and_not_expired(routing_plan):
    assert routing_plan.USB_VIA_IN_PAD_EXCEPTION_ISSUE == 7487
    expires = datetime.date.fromisoformat(routing_plan.USB_VIA_IN_PAD_EXCEPTION_EXPIRES)
    assert datetime.date.today() <= expires, (
        "board 03's #7487 via-in-pad usb_geometry() window exception expired on "
        f"{expires}; re-rule it or remove it (see routing_plan.py)"
    )


def test_base_window_and_bounds_unchanged(routing_plan):
    assert routing_plan.USB_COMMON_RUN_WINDOW == (37.5, 40.3, 13.0, 16.8)


def test_via_in_land_widens_the_window(routing_plan):
    pcb = _move_launch_vias(PCB.load(ROUTED), TIER1_LAUNCH_VIAS)
    x0, x1, y0, y1 = routing_plan.usb_common_run_window(pcb)
    assert (x0, x1) == (37.5, 40.3)
    assert y0 == pytest.approx(TIER1_LAUNCH_Y[0], abs=1e-5)
    assert y1 == pytest.approx(TIER1_LAUNCH_Y[1], abs=1e-5)


def test_only_the_edge_with_an_in_land_via_moves(routing_plan):
    vias = {
        "USB_D+": [(37.05, 11.737), (39.45, 16.89)],
        "USB_D-": [(38.95, 12.6375), (40.55, 16.89)],
    }
    pcb = _move_launch_vias(PCB.load(ROUTED), vias)
    _, _, y0, y1 = routing_plan.usb_common_run_window(pcb)
    assert y0 == pytest.approx(11.737, abs=1e-5)
    assert y1 == 16.8


@pytest.mark.parametrize(
    "vias",
    [
        # Plain jlcpcb: just outside both lands.
        {
            "USB_D+": [(37.05, 12.6375), (39.45, 16.89)],
            "USB_D-": [(38.95, 12.6375), (40.55, 16.89)],
        },
        # Right y, wrong x: beside the land, not in it.
        {"USB_D+": [(37.6, 11.737), (39.0, 17.419)], "USB_D-": [(38.5, 11.737), (41.0, 17.419)]},
        # Swapped nets: a via in the *other* net's land does not count.
        {
            "USB_D+": [(38.95, 11.737), (40.479, 17.419)],
            "USB_D-": [(37.05, 11.737), (39.379, 17.419)],
        },
    ],
    ids=["jlcpcb-outside", "beside-land", "wrong-net-land"],
)
def test_via_outside_land_does_not_widen(routing_plan, vias):
    pcb = _move_launch_vias(PCB.load(ROUTED), vias)
    assert routing_plan.usb_common_run_window(pcb) == routing_plan.USB_COMMON_RUN_WINDOW


def test_committed_jlcpcb_board_meets_the_common_run_bound(routing_plan):
    pcb = PCB.load(ROUTED)
    assert routing_plan.usb_common_run_window(pcb) == routing_plan.USB_COMMON_RUN_WINDOW
    geometry = routing_plan.usb_geometry(ROUTED)
    # 4.071 mm until #10561 clipped the two tracks that straddle the window
    # edges instead of dropping them.
    assert geometry["common_coupled_mm"] == pytest.approx(4.343427, abs=1e-6)
    assert geometry["branch_skew_mm"] == pytest.approx(0.0, abs=0.001)
    assert max(geometry["lengths_mm"].values()) <= 16.0


def test_tier1_launch_corridor_meets_the_common_run_bound(routing_plan, monkeypatch):
    _load(monkeypatch, routing_plan, _with_tier1_corridor(PCB.load(ROUTED)))
    geometry = routing_plan.usb_geometry(ROUTED)
    assert geometry["common_coupled_mm"] >= 4.0
    # 5.131 mm until #10561 counted the clipped window-edge tracks.
    assert geometry["common_coupled_mm"] == pytest.approx(5.643664, abs=1e-6)
    assert max(geometry["lengths_mm"].values()) <= 16.0
    assert geometry["branch_skew_mm"] <= routing_plan.USB_FS_MAX_SKEW_MM


def test_tier1_corridor_without_in_land_vias_still_fails(routing_plan, monkeypatch):
    """Without the via-in-pad launches the fixed window clips the run: < 4.0 mm."""
    pcb = _with_tier1_corridor(PCB.load(ROUTED))
    _move_launch_vias(
        pcb,
        {
            "USB_D+": [(37.05, 12.6375), (39.45, 16.89)],
            "USB_D-": [(38.95, 12.6375), (40.55, 16.89)],
        },
    )
    _load(monkeypatch, routing_plan, pcb)
    with pytest.raises(ValueError, match="USB branch bounds failed") as excinfo:
        routing_plan.usb_geometry(ROUTED)
    assert _measured_common_mm(excinfo.value) == pytest.approx(3.901426, abs=1e-6)
    assert _measured_common_mm(excinfo.value) < 4.0


@pytest.mark.parametrize(
    "nets",
    [(), ("USB_D+", "USB_D-"), ("USB_D+",), ("USB_D-",)],
    ids=["six-track", "ten-track", "split-d-plus-only", "split-d-minus-only"],
)
def test_track_partition_does_not_change_the_measurement(routing_plan, tmp_path, nets):
    """Issue #10561: subdividing a straight, constant-width run changes nothing."""
    path = _saved_run(tmp_path, _subdivided(USB_RUN, nets))
    geometry = routing_plan.usb_geometry(path)
    assert geometry["common_coupled_mm"] == pytest.approx(USB_RUN_COMMON_MM, abs=1e-6)
    for net, length in USB_RUN_LENGTHS_MM.items():
        assert geometry["lengths_mm"][net] == pytest.approx(length, abs=1e-6)
    assert geometry["branch_skew_mm"] == pytest.approx(USB_RUN_SKEW_MM, abs=1e-6)


def test_six_and_ten_track_representations_agree(routing_plan, tmp_path):
    """Both saved representations of one centerline agree to 0.000001 mm."""
    six = routing_plan.usb_geometry(_saved_run(tmp_path, USB_RUN, name="six"))
    ten = routing_plan.usb_geometry(
        _saved_run(tmp_path, _subdivided(USB_RUN, ("USB_D+", "USB_D-")), name="ten")
    )
    assert six["common_coupled_mm"] == pytest.approx(ten["common_coupled_mm"], abs=1e-6)
    assert six["branch_skew_mm"] == pytest.approx(ten["branch_skew_mm"], abs=1e-6)
    for net, length in USB_RUN_LENGTHS_MM.items():
        assert six["lengths_mm"][net] == pytest.approx(length, abs=1e-6)
        assert ten["lengths_mm"][net] == pytest.approx(six["lengths_mm"][net], abs=1e-6)


@pytest.mark.parametrize("net", ["USB_D+", "USB_D-"])
def test_a_break_in_either_run_is_rejected(routing_plan, tmp_path, net):
    """Two in-window pieces of one net are not a common run."""
    index, near, far = USB_RUN_GAPS[net]
    start, end = USB_RUN[net][1][index : index + 2]
    assert math.dist(start, near) + math.dist(near, far) + math.dist(far, end) == pytest.approx(
        math.dist(start, end), abs=1e-6
    ), "the gap must lie on the track's centerline"
    path = _saved_run(tmp_path, USB_RUN, gaps={net: USB_RUN_GAPS[net]})
    with pytest.raises(ValueError, match=f"Disconnected USB common run: {re.escape(net)}"):
        routing_plan.usb_geometry(path)


@pytest.mark.parametrize("net", ["USB_D+", "USB_D-"])
def test_a_run_on_another_layer_is_rejected(routing_plan, tmp_path, net):
    path = _saved_run(tmp_path, USB_RUN, moved={net: "In1.Cu"})
    with pytest.raises(ValueError, match=f"Missing USB common run: {re.escape(net)}"):
        routing_plan.usb_geometry(path)


def test_a_run_that_leaves_the_layer_partway_is_rejected(routing_plan, tmp_path):
    """The diagonal of D+ on another layer leaves too little coupled copper."""
    path = _saved_run(tmp_path, USB_RUN, moved={("USB_D+", 0): "In1.Cu"})
    with pytest.raises(ValueError, match="USB branch bounds failed") as excinfo:
        routing_plan.usb_geometry(path)
    assert _measured_common_mm(excinfo.value) < 4.0


@pytest.mark.parametrize("corner", CORNER_CONTACTS)
def test_a_track_touching_a_window_corner_contributes_no_length(routing_plan, tmp_path, corner):
    """Corner contact is zero in-window length, not a second piece of the run."""
    start, end = corner[:2], corner[2:]
    path = _saved_run(tmp_path, USB_RUN, extra=[("USB_D+", 0.1016, start, end)])
    geometry = routing_plan.usb_geometry(path)
    assert geometry["common_coupled_mm"] == pytest.approx(USB_RUN_COMMON_MM, abs=1e-6)
    # The whole-branch length is still measured over every original track.
    expected = USB_RUN_LENGTHS_MM["USB_D+"] + math.dist(start, end)
    assert geometry["lengths_mm"]["USB_D+"] == pytest.approx(expected, abs=1e-6)


def test_a_split_is_invisible_to_the_coupling_rule(routing_plan, tmp_path):
    """The rule credits a whole piece, so a straight run that is coupled along
    only part of its length must reach it as one piece however it was split.

    D+ runs the window's full height; D- sits beside it only from y = 15 up.
    Split at y = 14, the lower piece is outside the coupling window on its
    own and would lose its credit; the joined run measures 3.8 mm either way.
    """
    runs = {
        "USB_D+": (0.2, [(38.0, 13.0), (38.0, 16.8)]),
        "USB_D-": (0.2, [(38.3, 15.0), (38.3, 16.8)]),
    }
    split = {"USB_D+": (0.2, [(38.0, 13.0), (38.0, 14.0), (38.0, 16.8)]), "USB_D-": runs["USB_D-"]}
    rule = routing_plan.DiffPairRoutingContinuityRule(engaged_pairs=set())
    coupled = []
    for name, board in (("whole", runs), ("split", split)):
        pcb = PCB.load(_saved_run(tmp_path, board, name=name))
        window = routing_plan.usb_common_run_window(pcb)

        def common(net):
            tracks = [s for s in pcb.segments if pcb.nets[s.net_number].name == net]
            return routing_plan.usb_common_run(tracks, window, net)

        coupled.append(rule._coupled_length(common("USB_D+"), common("USB_D-")))
    assert coupled == pytest.approx([3.8, 3.8], abs=1e-6)


def test_collinear_join_keeps_bends_breaks_and_width_changes(routing_plan):
    """Only a continuation of one width along one line is joined."""
    track = object()
    join = routing_plan._join_collinear
    split_run = [
        ((0, 0), (1, 0), 0.2, track),
        ((1, 0), (2, 0), 0.2, track),
        ((2, 0), (3, 0), 0.2, track),
    ]
    assert join(split_run) == [((0, 0), (3, 0), 0.2, track)]
    bend = [((0, 0), (1, 0), 0.2, track), ((1, 0), (1, 1), 0.2, track)]
    assert join(bend) == bend
    width_change = [((0, 0), (1, 0), 0.2, track), ((1, 0), (2, 0), 0.25, track)]
    assert join(width_change) == width_change
    break_ = [((0, 0), (1, 0), 0.2, track), ((1.1, 0), (2, 0), 0.2, track)]
    assert join(break_) == break_
    doubles_back = [((2, 0), (1, 0), 0.2, track), ((1, 0), (1.5, 0), 0.2, track)]
    assert join(doubles_back) == doubles_back


# Review of #10571: a split that leaves a sliver (8 nm) of a track inside the
# window must not change the measurement.  A sliver is real copper, not a
# point contact.
SLIVER_RUN = {
    "USB_D+": (0.1, [(38.0, 12.0), (38.0, 16.0), (38.70711, 16.70711)]),
    "USB_D-": (0.1, [(38.15, 12.0), (38.15, 16.0), (38.85711, 16.70711)]),
}


def test_a_sliver_split_at_the_window_edge_does_not_flip_the_gate(routing_plan, tmp_path):
    """Saved-board threshold case: 4.000004552 mm stays above the 4.0 mm gate."""
    base = routing_plan.usb_geometry(_saved_run(tmp_path, SLIVER_RUN, name="whole"))
    assert base["common_coupled_mm"] == pytest.approx(4.000004552, abs=1e-6)
    split = {
        "USB_D+": (
            0.1,
            [(38.0, 12.0), (38.0, 13.000008), (38.0, 16.0), (38.70711, 16.70711)],
        ),
        "USB_D-": SLIVER_RUN["USB_D-"],
    }
    geometry = routing_plan.usb_geometry(_saved_run(tmp_path, split, name="sliver"))
    assert geometry["common_coupled_mm"] == pytest.approx(base["common_coupled_mm"], abs=1e-6)
    assert geometry["branch_skew_mm"] == pytest.approx(base["branch_skew_mm"], abs=1e-6)


def test_sliver_pieces_inside_the_window_stay_connected(routing_plan, tmp_path):
    """Three 8 nm splits on one vertical are continuous copper, not a break."""
    runs = dict(USB_RUN)
    width, points = runs["USB_D+"]
    # Points 1 and 2 are the ends of the x = 39.5886 vertical.
    runs["USB_D+"] = (
        width,
        [points[0], points[1], (39.5886, 16.0), (39.5886, 16.000008), (39.5886, 16.000016)]
        + points[2:],
    )
    geometry = routing_plan.usb_geometry(_saved_run(tmp_path, runs, name="slivers"))
    six = routing_plan.usb_geometry(_saved_run(tmp_path, USB_RUN, name="six2"))
    assert geometry["common_coupled_mm"] == pytest.approx(six["common_coupled_mm"], abs=1e-6)
