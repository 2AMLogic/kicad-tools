"""End-to-end persisted-checkpoint / ``--resume`` regression for the
partial-net entry-copper re-land (issue #9962, defect found in review of
PR #9937).

The defect: when a partially connected net's re-search failed, the router
re-committed the net's entry copper unconditionally.  If another net had
legally taken the vacated space since the rip-up, that re-land overlapped it
and both real checkpoint writers (the ``--checkpoint`` best-so-far writer and
the ``--checkpoint-interval`` time-based writer, both driven through
``_emit_route_checkpoint``) persisted the crossing; an ordinary ``--resume``
then seeded its output from the shorted checkpoint.  Pinned KiCad measured
one ``tracks_crossing`` error on exactly those files (PR #9937 auditor
comment); the fix gates the re-land on ``_entry_copper_still_legal``.

This regression builds the auditor's six-pad public coupon in ``tmp_path``,
recreates the defect's routing state deterministically on the loaded board
(A's entry copper ripped, B legally crossing the vacated corridor, A's
re-search failed), persists the committed board through the real checkpoint
writers, and finally runs the real ``kct route --resume`` command on the
written checkpoint.  Every assertion reads the *persisted PCB files* back
with kicad-tools' own parser -- not router in-memory state -- so no
``kicad-cli`` is needed and the test runs in the default suite.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from kicad_tools.cli import main as kct_main

from .test_known_failure_8486 import _commit_copper, _rip_copper

if TYPE_CHECKING:
    from kicad_tools.router.primitives import Segment

# The auditor's public reproduction flags (PR #9937 comment), plus the
# hermetic stage switches the verdict-level route tests use
# (tests/test_route_unverified_verdict_6273.py): no optimizer, pours,
# placement feedback, oracle rounds or current-path analysis, so the resume
# run exercises the routing loop + checkpoint writers only.
ROUTE_ARGS = [
    "--backend",
    "python",
    "--layers",
    "2",
    "--strict-layers",
    "--grid",
    ".2",
    "--clearance",
    ".2",
    "--trace-width",
    ".2",
    "--iterations",
    "1",
    "--timeout",
    "80",
    "--per-net-timeout",
    "8",
    "--no-cache",
    "--seed",
    "1",
    "--no-optimize",
    "--no-sync-check",
    "--no-auto-pour",
    "--no-placement-feedback",
    "--oracle-rounds",
    "0",
    "--no-current-paths",
]

NET_A = "A"
NET_B = "B"


def _write_coupon(directory: Path) -> Path:
    """Write the public two-net checkpoint regression coupon into *directory*.

    The coupon from the PR #9937 audit has three pads on A, two on B and one
    wide pad on W. All six pads are on F.Cu. The router can use F.Cu and
    B.Cu. A and B start open so a full route rips and re-searches them.
    """
    parts = {
        NET_A: [(4, 0.65, 0.6, 1.2), (4, 15.35, 0.6, 1.2), (18, 0.65, 0.6, 0.6)],
        NET_B: [(0.6, 8, 1.1, 0.6), (10.96, 8, 0.9, 0.6)],
        "W": [(12, 8, 0.65, 15.6)],
    }
    board = [
        "(kicad_pcb (version 20240108) (generator pcbnew) (general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (39 "F.Mask" user) (44 "Edge.Cuts" user))',
        '(setup (pad_to_mask_clearance 0)) (net 0 "")',
        '(gr_rect (start -1 -1) (end 21 17) (stroke (width .05) (type default)) (fill none) (layer "Edge.Cuts"))',
    ]
    library = directory / "Coupon.pretty"
    library.mkdir(exist_ok=True)
    (directory / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "Coupon") (type "KiCad") (uri "'
        "${KIPRJMOD}/Coupon.pretty"
        '") (options "") (descr "")))'
    )
    for net, (name, positions) in enumerate(parts.items(), 1):
        board.append(f'(net {net} "{name}")')
        attr = "(attr board_only exclude_from_pos_files exclude_from_bom)"
        placed = [
            f'(footprint "Coupon:{name}" (layer "F.Cu") (at 0 0) {attr}',
            f'(property "Reference" "{name}" (at 0 0) (layer "F.SilkS") hide '
            "(effects (font (size 1 1) (thickness .15))))",
        ]
        lib = [f'(footprint "{name}" (version 20240108) (generator pcbnew) (layer "F.Cu") {attr}']
        for number, (x, y, w, h) in enumerate(positions, 1):
            pad = f'(pad "{number}" smd rect (at {x} {y}) (size {w} {h}) (layers "F.Cu" "F.Mask")'
            lib.append(pad + ")")
            placed.append(pad + f' (net {net} "{name}"))')
        (library / (name + ".kicad_mod")).write_text("\n".join(lib) + ")\n")
        board.append("\n".join(placed) + ")")
    for x, y, big_x, big_y in [(-1, -1, 0, 17), (0, -1, 20, 0), (20, -1, 21, 17), (0, 16, 20, 17)]:
        board.append(
            f'(zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (hatch edge .5)\n'
            "        (connect_pads (clearance .2)) (min_thickness .25)\n"
            "        (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) "
            "(copperpour allowed) (footprints allowed))\n"
            "        (fill (thermal_gap .3) (thermal_bridge_width .3)) "
            f"(polygon (pts (xy {x} {y}) (xy {big_x} {y}) (xy {big_x} {big_y}) (xy {x} {big_y}))))"
        )
    pcb = directory / "coupon.kicad_pcb"
    pcb.write_text("\n".join(board) + ")\n")
    (directory / "coupon.kicad_pro").write_text("{}\n")
    return pcb


def _same_layer_crossings(pcb_path: Path) -> list[str]:
    """Same-layer copper overlaps between nets A and B on a written PCB.

    Reads the persisted file with the repo's own parser and measures
    edge-to-edge segment clearance with the exact centerline primitive, so
    the assertion is on the board a resume or KiCad would see -- not on
    router in-memory state.
    """
    from kicad_tools.geometry.copper import segment_centerline_distance
    from kicad_tools.router.optimizer.pcb import parse_segments

    by_net = parse_segments(pcb_path.read_text())
    crossings = []
    for seg_a in by_net.get(NET_A, []):
        for seg_b in by_net.get(NET_B, []):
            if seg_a.layer != seg_b.layer:
                continue
            distance = segment_centerline_distance(seg_a.start, seg_a.end, seg_b.start, seg_b.end)
            clearance = distance - (seg_a.width + seg_b.width) / 2
            if clearance < -1e-9:
                crossings.append(
                    f"{seg_a.start}->{seg_a.end} (w={seg_a.width}) overlaps "
                    f"{seg_b.start}->{seg_b.end} (w={seg_b.width}) on "
                    f"{seg_a.layer}: edge-to-edge {clearance:.3f} mm"
                )
    return crossings


def _assert_no_crossing(pcb_path: Path) -> None:
    crossings = _same_layer_crossings(pcb_path)
    assert not crossings, f"{pcb_path.name} persists a same-layer A/B crossing: {crossings}"


def _same_track(a: Segment, b: Segment) -> bool:
    """Whether two parsed segments are the same track: same layer, width and
    endpoints, up to coordinate rounding and endpoint order.
    """
    if a.layer != b.layer or not math.isclose(a.width, b.width, abs_tol=1e-6):
        return False
    for ends_b in ([(b.x1, b.y1), (b.x2, b.y2)], [(b.x2, b.y2), (b.x1, b.y1)]):
        if all(
            math.isclose(pa[0], pb[0], abs_tol=1e-6) and math.isclose(pa[1], pb[1], abs_tol=1e-6)
            for pa, pb in zip([(a.x1, a.y1), (a.x2, a.y2)], ends_b, strict=True)
        ):
            return True
    return False


def _recreate_defect_state(router, net_map: dict[str, int], guard) -> None:
    """Commit the board state the defective re-land published a short from.

    The auditor's measured trajectory on the public coupon: net A entered
    the iteration holding a vertical front-layer track joining two of its
    three pads; the rip-up vacated it; net B legally routed across at
    y = 8 mm; A's re-search found no path.  Building that state directly
    (the failed search via a patched ``_route_net_negotiated``, as the
    known-failure unit cases do) makes the scenario deterministic instead
    of wall-clock dependent.
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    a_id, b_id = net_map[NET_A], net_map[NET_B]
    entry = [
        Route(
            a_id,
            NET_A,
            segments=[Segment(4.0, 0.65, 4.0, 15.35, 0.2, Layer.F_CU, a_id)],
            vias=[],
        )
    ]
    blocker = [
        Route(
            b_id,
            NET_B,
            segments=[Segment(0.6, 8.0, 10.96, 8.0, 0.2, Layer.F_CU, b_id)],
            vias=[],
        )
    ]
    pairs = router._connected_pad_pairs(a_id, entry)
    assert len(pairs) == 1, "A's entry track must join exactly the two vertical pads"
    _commit_copper(router, entry)
    router._known_failure_entry = {a_id: (pairs, list(entry))}
    guard.begin_iteration(1, {a_id: None})
    _rip_copper(router, entry)
    _commit_copper(router, blocker)  # net B legally takes the vacated space
    with patch.object(router, "_route_net_negotiated", return_value=[]):
        out = router._route_net_known_failure_aware(a_id, 1.0, 30.0, guard, 1)
    # The negotiated loop's reroute/recovery sites commit a nonempty return
    # exactly like this (usage-marked and appended to the live board) -- the
    # path the re-landed copper took into the checkpoint writers.
    for route in out:
        router.grid.mark_route_usage(route)
        router.routes.append(route)


@pytest.mark.requires_router_cpp
@pytest.mark.timeout(180)
def test_checkpoint_and_resume_keep_the_persisted_coupon_legal(tmp_path, capfd) -> None:
    """The real checkpoint writers never persist the re-land short, and a
    ``--resume`` from the unverified checkpoint never emits one either.

    The route log is captured at the fd level (``capfd``): the routing loop
    writes through the CLI's fd-level progress path, which ``capsys`` cannot
    see.
    """
    from kicad_tools.router.access_ripup import RipupCycleGuard
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.rules import DesignRules

    coupon = _write_coupon(tmp_path)
    best = tmp_path / "best.kicad_pcb"  # named best-checkpoint writer target
    interval_out = tmp_path / "interval.kicad_pcb"  # time-based writer target
    resumed = tmp_path / "resumed.kicad_pcb"
    resumed_best = tmp_path / "resumed-best.kicad_pcb"

    router, net_map = load_pcb_for_routing(
        str(coupon),
        rules=DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_clearance=0.2,
            grid_resolution=0.2,
        ),
        force_python=True,
        strict_drc=False,
        edge_clearance=0,
    )
    guard = RipupCycleGuard()
    _recreate_defect_state(router, net_map, guard)

    # Persist the committed board through the real writers, exactly as the
    # negotiated loop's checkpoint hook does.
    from kicad_tools.cli.route_cmd import _make_checkpoint_callback
    from kicad_tools.router.checkpoint import BestCheckpointWriter
    from kicad_tools.router.core import _emit_route_checkpoint

    writer = BestCheckpointWriter(best, command="route", source_pcb=coupon)
    callback = _make_checkpoint_callback(
        coupon,
        interval_out,
        30.0,
        router_provider=lambda: router,
        best_checkpoint=writer,
    )
    _emit_route_checkpoint(
        callback, router.routes, 0, completion=router._count_fully_connected_nets
    )
    out_cp = capfd.readouterr().out
    assert "checkpoint: new best" in out_cp, out_cp[-2000:]
    assert "checkpoint: wrote best-so-far" in out_cp, out_cp[-2000:]
    assert best.exists() and interval_out.exists()

    from kicad_tools.router.optimizer.pcb import parse_segments

    # Net B's blocker track is what keeps the crossing checks below
    # non-vacuous (net A correctly contributes no copper to the checkpoint),
    # so pin it per net rather than counting either net's segments.
    snapshot = parse_segments(best.read_text())
    assert snapshot.get(NET_B), "the best checkpoint lost net B's blocker track"
    interval_snapshot = parse_segments(interval_out.read_text())
    assert interval_snapshot.get(NET_B), "the interval checkpoint lost net B's blocker track"
    _assert_no_crossing(best)
    _assert_no_crossing(interval_out)

    # The sidecar honestly marks the candidate unverified (the coupon keeps
    # its intentional opens); the resume below must still stay legal.
    sidecar = json.loads((tmp_path / "best.checkpoint.json").read_text())
    assert sidecar["verified"] is False
    assert "score" in sidecar

    # The real --resume path: seed from the persisted checkpoint
    # (_apply_checkpoint_and_resume) and assert the resumed output.
    kct_main(
        [
            "route",
            str(coupon),
            "--resume",
            str(best),
            "--output",
            str(resumed),
            "--checkpoint",
            str(resumed_best),
            *ROUTE_ARGS,
        ]
    )
    out2 = capfd.readouterr().out
    assert "--resume: seeding from" in out2, out2[-2000:]
    assert resumed.exists()
    # The resume's own best-checkpoint write is score-gated (it must beat the
    # seed board's RouteScore), so on a trajectory that never improves on the
    # seed the file legitimately stays unwritten; when it is written, it must
    # be just as legal.
    if resumed_best.exists():
        _assert_no_crossing(resumed_best)
    _assert_no_crossing(resumed)
    # Completion cannot drop below the checkpoint's: the seed copper -- net
    # B's blocker, the only copper the checkpoint carries -- survives into
    # the resumed output as itself, not merely as some routed copper.
    resumed_snapshot = parse_segments(resumed.read_text())
    assert resumed_snapshot.get(NET_B), "resume dropped the seed copper (net B's blocker)"
    for seed in snapshot[NET_B]:
        assert any(_same_track(seed, kept) for kept in resumed_snapshot[NET_B]), (
            f"resume dropped the seed segment {seed.start}->{seed.end} on {seed.layer}"
        )
