"""Own-pour relief, edge fragments, deferred jog slides and swept via connections (issue #10664).

Fixtures are synthetic boards like ``test_repair_mask_copper.py``'s: R1/1 is a
rect pad (net N1) centred at (100, 100) spanning x 99..101, y 99.5..100.5.
The mask findings are built directly (the native assessment needs kicad-cli);
``fix-drc`` runs stub the re-check, the refill and the native state so the
kept/reverted paths are exercised without them.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

from kicad_tools.cli import fix_drc_cmd
from kicad_tools.drc.repair_mask_copper import (
    MaskCopperRepairer,
    MaskFragmentAction,
    MaskPourReliefAction,
    MaskRepairResult,
)
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.router.quantize import is_45_aligned
from kicad_tools.sexp import SExp, parse_file
from kicad_tools.validate.mask_copper import MaskCopperAssessment

PAD = "pad-r1-1"
PAD2 = "pad-r1-2"
ZONE = "zone-n1"

BOARD = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "N1")
  (net 2 "N2")
  (footprint "test:R1" (layer "F.Cu") (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1") (uuid "pad-r1-1")%s)
    %s
  )
  %s
)
"""

PAD_TWO = (
    '(pad "2" smd rect (at 0 3) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask") '
    '(net 1 "N1") (uuid "pad-r1-2"))'
)

OWN_ENTRY = "same-net own-pad entry"
PASS_THROUGH = "same-net pass-through"
DISJOINT = "Mask-to-copper disjoint: uncertain"
OVERLAP = "Mask-to-copper overlap: violation"

RELIEF = {"warning_clearance_mm": 0.15}


def _zone(style: str = "", gap: float = 0.1) -> str:
    connect = (
        f"(connect_pads {style} (clearance 0.1))" if style else "(connect_pads (clearance 0.1))"
    )
    return (
        f'  (zone (net 1) (net_name "N1") (layer "F.Cu") (uuid "{ZONE}") '
        f"{connect} (min_thickness 0.25) "
        f"(fill yes (thermal_gap {gap}) (thermal_bridge_width 0.4))"
        " (polygon (pts (xy 97 97) (xy 103 97) (xy 103 103) (xy 97 103))))"
    )


def _board(
    tmp_path: Path, copper: str, *, pad_extra: str = "", second_pad: str = "", zone=""
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "b.kicad_pcb"
    body = "\n".join(part for part in (zone, copper) if part)
    path.write_text(BOARD % (pad_extra, second_pad, body))
    return path


def _seg(uuid: str, a, b, net: int = 1, width: float = 0.2, layer: str = "F.Cu") -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net {net}) (uuid "{uuid}"))'
    )


def _via(uuid: str, at, size: float = 0.6, drill: float = 0.3, net: int = 1) -> str:
    return (
        f"  (via (at {at[0]} {at[1]}) (size {size}) (drill {drill}) "
        f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "{uuid}"))'
    )


def _finding(
    conductor: str,
    marker: str,
    pad: str = PAD,
    *,
    relation: str = OVERLAP,
    measured: float = 0.0,
    required: float = 0.09,
    location=(100.0, 99.6),
) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.MASK_TO_COPPER,
        type_str="mask_to_copper",
        severity=Severity.ERROR,
        message=f"{relation}; opening R1/1; {marker}",
        locations=[Location(x_mm=location[0], y_mm=location[1], layer="F.Cu")],
        items=[pad, conductor],
        required_value_mm=required,
        actual_value_mm=measured,
    )


def _end(path: Path, uuid: str) -> list[tuple[float, float]]:
    doc = parse_file(path)
    for node in doc.find_all("segment"):
        if str(node.find("uuid").get_first_atom()).strip('"') == uuid:
            s = [float(a) for a in node.find("start").get_atoms()]
            e = [float(a) for a in node.find("end").get_atoms()]
            return [(s[0], s[1]), (e[0], e[1])]
    raise AssertionError(uuid)


def _uuids(path: Path) -> set[str]:
    doc = parse_file(path)
    return {str(n.find("uuid").get_first_atom()).strip('"') for n in doc.find_all("segment")}


def _pad_gap(path: Path, pad_uuid: str) -> float | None:
    doc = parse_file(path)
    for pad in doc.find_all("pad"):
        if str(pad.find("uuid").get_first_atom()).strip('"') == pad_uuid:
            gap = pad.find_child("thermal_gap")
            return None if gap is None else gap.get_float(0)
    raise AssertionError(pad_uuid)


# ---------------------------------------------------------------------------
# Own-pour relief
# ---------------------------------------------------------------------------


def test_pour_relief_widens_the_pad_gap_past_the_examined_band_and_undoes(tmp_path):
    board = _board(tmp_path, "", zone=_zone())
    finding = _finding(ZONE, "exposed copper zone (net N1); same-net")
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([finding], **RELIEF)
    assert result.repaired == 1 and not result.unrepaired
    action = result.actions[0]
    assert isinstance(action, MaskPourReliefAction)
    assert (action.uuid, action.pad_uuid, action.old_gap_mm, action.new_gap_mm) == (
        ZONE,
        PAD,
        None,
        0.2,
    )
    repairer.save(board)
    assert _pad_gap(board, PAD) == 0.2
    assert "0.1000" in str(action) and "0.2000" in str(action)

    undo = MaskCopperRepairer(board)
    assert undo.undo_action(action)
    undo.save(board)
    assert _pad_gap(board, PAD) is None


def test_pour_relief_replaces_a_narrow_pad_override_and_restores_it(tmp_path):
    board = _board(tmp_path, "", pad_extra=" (thermal_gap 0.1)", zone=_zone())
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding(ZONE, "same-net")], **RELIEF)
    assert result.repaired == 1
    action = result.actions[0]
    assert action.old_gap_mm == 0.1
    repairer.save(board)
    assert _pad_gap(board, PAD) == 0.2
    undo = MaskCopperRepairer(board)
    assert undo.undo_action(action)
    undo.save(board)
    assert _pad_gap(board, PAD) == 0.1


def test_pour_relief_uses_the_error_clearance_without_an_advisory_band(tmp_path):
    board = _board(tmp_path, "", zone=_zone())
    result = MaskCopperRepairer(board).repair([_finding(ZONE, "same-net")])
    assert result.repaired == 1
    # 0.09 + 0.02 margin, rounded up to the 0.05 mm step.
    assert result.actions[0].new_gap_mm == 0.15


def test_pour_relief_dry_run_leaves_the_pad_alone(tmp_path):
    board = _board(tmp_path, "", zone=_zone())
    before = board.read_text()
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding(ZONE, "same-net")], dry_run=True, **RELIEF)
    assert result.repaired == 1 and not repairer.modified
    assert board.read_text() == before


@pytest.mark.parametrize(
    ("zone", "pad_extra", "reason"),
    [
        # KiCad saves a solid zone as (connect_pads yes (clearance ...)).
        (_zone("yes"), "", "pour_relief_not_thermal"),
        (_zone("thru_hole_only"), "", "pour_relief_not_thermal"),
        (_zone("no"), "", "pour_relief_not_thermal"),
        (_zone(), " (zone_connect 2)", "pour_relief_not_thermal"),
        (_zone(gap=0.3), "", "pour_relief_already_wide"),
        (_zone(), " (thermal_gap 0.2)", "pour_relief_already_wide"),
    ],
)
def test_pour_relief_is_declined_where_it_cannot_apply(tmp_path, zone, pad_extra, reason):
    board = _board(tmp_path, "", pad_extra=pad_extra, zone=zone)
    before = board.read_text()
    result = MaskCopperRepairer(board).repair([_finding(ZONE, "same-net")], **RELIEF)
    assert result.repaired == 0
    assert [u.reason for u in result.unrepaired] == [reason]
    assert board.read_text() == before


@pytest.mark.parametrize(
    ("pad_mode", "footprint_mode", "zone_style", "thermal"),
    [
        (1, 2, "yes", True),
        (-1, 1, "yes", True),
        (-1, 2, "", False),
        (-1, -1, "", True),
        (-1, -1, "yes", False),
        (0, 1, "", False),
    ],
)
def test_pour_relief_resolves_pad_and_footprint_overrides(
    tmp_path, pad_mode, footprint_mode, zone_style, thermal
):
    board = _board(tmp_path, "", zone=_zone(zone_style), pad_extra=f" (zone_connect {pad_mode})")
    doc = parse_file(board)
    doc.find("footprint").append(SExp.list("zone_connect", footprint_mode))
    board.write_text(doc.to_string())
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding(ZONE, "same-net")], **RELIEF)
    repairer.save(board)
    assert [u.reason for u in result.unrepaired] == ([] if thermal else ["pour_relief_not_thermal"])
    assert _pad_gap(board, PAD) == (0.2 if thermal else None)


def test_pour_relief_reads_the_board_not_the_message(tmp_path):
    foreign_zone = _zone().replace('(net 1) (net_name "N1")', '(net 2) (net_name "N2")')
    board = _board(tmp_path, "", zone=foreign_zone)
    before = board.read_text()
    result = MaskCopperRepairer(board).repair([_finding(ZONE, "same-net")], **RELIEF)
    assert result.repaired == 0 and [u.reason for u in result.unrepaired] == ["not_same_net"]
    assert board.read_text() == before


def test_pour_relief_needs_a_required_value(tmp_path):
    board = _board(tmp_path, "", zone=_zone())
    result = MaskCopperRepairer(board).repair([_finding(ZONE, "same-net", required=None)], **RELIEF)
    assert result.repaired == 0 and result.unrepaired[0].reason == "no_required_value"


# ---------------------------------------------------------------------------
# Edge fragments
# ---------------------------------------------------------------------------

# A pad exit shaped like Board 04's NRST at U2/7: an in-pad lead, a 45-degree
# jog to the pad edge, a fragment no longer than its width standing on the
# edge, the straight exit off the pad's axis, then a jog between parallel runs.
EXIT = [
    _seg("trk-lead", (100.4, 100.0), (99.1, 100.0)),
    _seg("trk-jog", (99.1, 100.0), (99.0, 100.1)),
    _seg("trk-frag", (99.0, 100.1), (99.0, 100.125)),
    _seg("trk-exit", (99.0, 100.125), (98.8, 100.125)),
]
EXIT_TAIL = [
    _seg("trk-run", (98.8, 100.125), (98.5, 100.425)),
    _seg("trk-far", (98.5, 100.425), (97.5, 100.425)),
]


def _exit_board(tmp_path: Path, extra: str = "", fragment: str | None = None, exit_seg="") -> Path:
    parts = [*EXIT[:2], EXIT[2] if fragment is None else fragment]
    parts.append(exit_seg or EXIT[3])
    parts.extend(EXIT_TAIL)
    return _board(tmp_path, "\n".join([*parts, extra]))


def _exit_board_with_pour(tmp_path: Path, zone: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "b.kicad_pcb"
    path.write_text(BOARD % ("", "", "\n".join([zone, *EXIT, *EXIT_TAIL])))
    return path


def test_edge_fragment_is_removed_and_undone_exactly(tmp_path):
    board = _exit_board(tmp_path)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-frag", PASS_THROUGH)])
    assert result.repaired == 1 and not result.unrepaired
    action = result.actions[0]
    assert isinstance(action, MaskFragmentAction)
    assert (action.uuid, action.pad_uuid, action.openings) == ("trk-frag", PAD, [PAD])
    repairer.save(board)
    assert "trk-frag" not in _uuids(board)
    for uid in ("trk-lead", "trk-jog", "trk-exit", "trk-run", "trk-far"):
        assert _end(board, uid) == _end(_exit_board(tmp_path / "ref"), uid)

    undo = MaskCopperRepairer(board)
    assert undo.undo_action(action)
    undo.save(board)
    assert _end(board, "trk-frag") == [(99.0, 100.1), (99.0, 100.125)]


def test_edge_fragment_dry_run_predicts_without_removal(tmp_path):
    board = _exit_board(tmp_path)
    before = board.read_text()
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-frag", PASS_THROUGH)], dry_run=True)
    assert result.repaired == 1 and not repairer.modified
    assert board.read_text() == before


def test_edge_fragment_removed_after_the_exit_was_carried_on(tmp_path):
    """The exit already extended past the fragment leaves its end inside the exit's copper."""
    board = _exit_board(
        tmp_path,
        fragment=_seg("trk-frag", (99.0, 100.1), (99.0, 100.125)),
        exit_seg=_seg("trk-exit", (99.1, 100.125), (98.8, 100.125)),
    )
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-frag", PASS_THROUGH)])
    assert result.repaired == 1, result.unrepaired
    repairer.save(board)
    assert "trk-frag" not in _uuids(board)


@pytest.mark.parametrize(
    ("fragment", "extra", "reason"),
    [
        # 0.12 mm apart: the jog's end no longer sits inside the exit's copper.
        (
            _seg("trk-frag", (99.0, 100.1), (99.0, 100.22)),
            _seg("trk-exit", (99.0, 100.22), (98.8, 100.22)),
            "would_disconnect_copper",
        ),
        # A via stands on the fragment's end.
        (EXIT[2], _via("v1", (99.0, 100.125)), "fragment_junction_not_simple"),
        # A third track meets the fragment mid-run.
        (
            _seg("trk-frag", (99.0, 100.1), (99.0, 100.3)),
            "\n".join(
                [
                    _seg("trk-exit", (99.0, 100.3), (98.8, 100.3)),
                    _seg("trk-tap", (99.0, 100.2), (98.5, 100.2)),
                ]
            ),
            "fragment_has_mid_run_contact",
        ),
    ],
)
def test_edge_fragment_rule_declines_when_copper_depends_on_it(tmp_path, fragment, extra, reason):
    exit_seg = next((part for part in extra.split("\n") if '"trk-exit"' in part), "")
    board = _exit_board(tmp_path, extra, fragment, exit_seg)
    before = board.read_text()
    repairer = MaskCopperRepairer(board)
    result = MaskRepairResult()
    refusal = repairer._remove_edge_fragment(
        repairer._segment_by_uuid("trk-frag"),
        "trk-frag",
        [PAD],
        repairer._openings(),
        result,
        False,
    )
    assert refusal == reason
    assert result.repaired == 0 and not repairer.modified
    assert board.read_text() == before


def test_a_track_longer_than_its_width_keeps_the_historical_reason(tmp_path):
    board = _board(tmp_path, _seg("trk-long", (100.4, 100.0), (99.0, 100.0)))
    result = MaskCopperRepairer(board).repair([_finding("trk-long", PASS_THROUGH)])
    assert result.repaired == 0
    assert result.unrepaired[0].reason == "not_own_pad_entry"
    assert result.unrepaired[0].detail == ""


def test_edge_fragment_undo_refuses_when_the_fragment_is_back(tmp_path):
    board = _exit_board(tmp_path)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-frag", PASS_THROUGH)])
    assert not MaskCopperRepairer(board).undo_action(result.actions[0])


# ---------------------------------------------------------------------------
# A jog slide deferred behind an exit extension
# ---------------------------------------------------------------------------


def test_jog_slide_is_deferred_behind_the_exit_extension_then_applies(tmp_path):
    findings = [
        _finding(
            "trk-run", PASS_THROUGH, relation=DISJOINT, measured=0.1, location=(98.8, 100.125)
        ),
        _finding("trk-frag", PASS_THROUGH),
        _finding("trk-exit", OWN_ENTRY),
    ]
    board = _exit_board(tmp_path)
    repairer = MaskCopperRepairer(board)
    first = repairer.repair(findings, max_displacement=0.5, warning_clearance_mm=0.15)
    assert first.repaired == 2, first.unrepaired
    assert [a.action for a in first.actions] == ["remove_fragment", "extend_entry"]
    assert [(u.uuid, u.reason) for u in first.unrepaired] == [
        ("trk-run", "segment_end_already_moved")
    ]
    repairer.save(board)
    assert _end(board, "trk-exit")[0] == (99.1, 100.125)  # carried on, still horizontal
    assert _end(board, "trk-run") == [(98.8, 100.125), (98.5, 100.425)]  # not translated

    # The next pass sees the settled copper and slides the jog along its runs.
    next_pass = MaskCopperRepairer(board)
    second = next_pass.repair([findings[0]], max_displacement=0.5, warning_clearance_mm=0.15)
    assert second.repaired == 1, second.unrepaired
    assert "slid a jog" in second.actions[0].detail
    next_pass.save(board)
    exit_end, run_start = _end(board, "trk-exit")[1], _end(board, "trk-run")[0]
    assert exit_end == run_start == (98.73, 100.125)
    assert _end(board, "trk-run")[1] == _end(board, "trk-far")[0] == (98.43, 100.425)
    for uid in ("trk-exit", "trk-run", "trk-far"):
        a, b = _end(board, uid)
        assert is_45_aligned(b[0] - a[0], b[1] - a[1])


# ---------------------------------------------------------------------------
# Via relocation with a connection inside the swept land
# ---------------------------------------------------------------------------

# A via in a channel: the pad on its left, a foreign wall on its right, its two
# approaches in one horizontal line through it (Board 04's SWCLK via beside
# J1/3).  The F.Cu approach runs into the pad, the B.Cu one arrives from under
# the wall.  Only a move up the channel clears the opening, and every such move
# is longer than the via land, so no connection inside the land can serve it.
CHANNEL = [
    _via("via-p", (101.35, 99.6)),
    _seg("trk-f", (100.5, 99.6), (101.35, 99.6)),
    _seg("trk-b", (102.0, 99.6), (101.35, 99.6), layer="B.Cu"),
    _seg("wall", (101.92, 98.0), (101.92, 100.2), net=2),
]


def _channel_board(tmp_path: Path) -> Path:
    return _board(tmp_path, "\n".join(CHANNEL))


def _approaches(repairer: MaskCopperRepairer):
    joined = repairer._anchored_ends(101.35, 99.6, "N1")
    return joined


def test_tail_plan_offers_a_swept_connection_only_when_asked(tmp_path):
    repairer = MaskCopperRepairer(_channel_board(tmp_path))
    joined = _approaches(repairer)
    assert [tag for _u, _n, tag, _e in joined] == ["end", "end"]
    destination = (101.35, 99.27)
    assert repairer._via_tail_plan(joined, destination, 0.6, 1.0) is None
    plan = repairer._via_tail_plan(joined, destination, 0.6, 1.0, swept=True)
    assert plan is not None
    end_moves, tails, _lines = plan
    assert len(end_moves) == 2 and len(tails) == 2
    for sx, sy, ex, ey, width, layer, _net in tails:
        assert (ex, ey) == destination
        assert is_45_aligned(ex - sx, ey - sy)
        # Outside the new land, inside the land the via sweeps to get there.
        assert math.dist((sx, sy), destination) + width / 2 > 0.3
        swept = LineString([(101.35, 99.6), destination]).buffer(0.3, quad_segs=32)
        assert LineString([(sx, sy), (ex, ey)]).buffer(width / 2, quad_segs=16).within(swept)
    assert {layer for _sx, _sy, _ex, _ey, _w, layer, _n in tails} == {"F.Cu", "B.Cu"}
    # Every slid end stays on its own approach line.
    for uid, _tag, ox, oy, nx, ny in end_moves:
        assert (ox, oy) == (101.35, 99.6)
        assert math.hypot(nx - ox, ny - oy) < 1e-9


def test_swept_tail_project_refusal_names_the_connection_that_crowds(tmp_path):
    repairer = MaskCopperRepairer(_channel_board(tmp_path))
    far = (Point(110, 110).buffer(0.1), 0.15, frozenset({"F.Cu"}), "far track", [math.inf])
    near = (Point(101.35, 99.5).buffer(0.2), 0.15, frozenset({"F.Cu"}), "crossing track", [0.2])
    exposed = [
        (
            Point(101.35, 99.6).buffer(0.3),
            LineString([(101.35, 99.6), (101.35, 99.27)]).buffer(0.1),
            "F.Cu",
        )
    ]
    assert repairer._exposed_tail_project_refusal(([], []), exposed) is None
    assert repairer._exposed_tail_project_refusal(([far], []), exposed) is None
    refused = repairer._exposed_tail_project_refusal(([near], []), exposed)
    assert refused is not None and "swept connection" in refused and "crossing track" in refused


def test_via_in_a_channel_relocates_along_it_with_swept_connections(tmp_path):
    board = _channel_board(tmp_path)
    original = _channel_board(tmp_path / "ref")
    repairer = MaskCopperRepairer(board)
    result = repairer.repair(
        [_finding("via-p", PASS_THROUGH, relation=DISJOINT, measured=0.05, location=(101.1, 99.6))],
        max_displacement=1.0,
    )
    assert result.repaired == 1, result.unrepaired
    action = result.actions[0]
    assert action.action == "relocate_clear" and "swept land" in action.detail
    repairer.save(board)

    centre = (action.new_x, action.new_y)
    assert repairer._openings()[PAD][0].distance(Point(centre).buffer(0.3)) >= 0.11 - 1e-6
    wall = LineString([(101.92, 98.0), (101.92, 100.2)]).buffer(0.1, quad_segs=16)
    assert Point(centre).buffer(0.3).distance(wall) >= 0.15 - 1e-6
    assert centre[1] < 99.35 and abs(centre[0] - 101.35) < 0.2  # up the channel
    assert len(action.added_segments) == 2
    for uuid, sx, sy, ex, ey, width, layer, net in action.added_segments:
        assert (ex, ey) == centre and is_45_aligned(ex - sx, ey - sy)
        assert layer in {"F.Cu", "B.Cu"} and net == 1
        assert _end(board, uuid) == [(sx, sy), (ex, ey)]
        # Outside the new land, inside the land the via swept from its old centre.
        assert math.dist((sx, sy), centre) + width / 2 > 0.3
        swept = LineString([(101.35, 99.6), centre]).buffer(0.3, quad_segs=32)
        assert LineString([(sx, sy), (ex, ey)]).buffer(width / 2, quad_segs=16).within(swept)
    # The approaches kept their lines and still reach the junction the via left.
    assert _end(board, "trk-f")[1] == (101.35, 99.6)
    assert _end(board, "trk-b")[1] == (101.35, 99.6)

    reloaded = MaskCopperRepairer(board)
    assert reloaded.undo_action(action)
    reloaded.save(board)
    assert _uuids(board) == {"trk-f", "trk-b", "wall"}
    for uid in ("trk-f", "trk-b", "wall"):
        assert _end(board, uid) == _end(original, uid)
    doc = parse_file(board)
    via = next(v for v in doc.find_all("via"))
    assert [float(a) for a in via.find("at").get_atoms()] == [101.35, 99.6]


# ---------------------------------------------------------------------------
# fix-drc integration: the verified pass keeps, and reverts, the new edits
# ---------------------------------------------------------------------------


def _report(*violations: DRCViolation) -> DRCReport:
    return DRCReport(
        source_file="b.kicad_pcb",
        created_at=None,
        pcb_name="b.kicad_pcb",
        violations=list(violations),
        mask_copper_assessments=[MaskCopperAssessment(coverage="complete")],
    )


@pytest.fixture
def native_stubs(monkeypatch):
    """No zones are refilled and no native DRC runs in these runs."""
    refills: list[Path] = []

    def refill(candidate, *, require_save):
        refills.append(candidate)
        return type("Result", (), {"success": True, "stderr": ""})()

    monkeypatch.setattr("kicad_tools.cli.runner.run_refill_zones", refill)
    monkeypatch.setattr(
        fix_drc_cmd, "_mask_native_state", lambda path: fix_drc_cmd._MaskNativeState()
    )
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_pad_groups", lambda board: [])
    return refills


def _run(monkeypatch, board: Path, reports: list[DRCReport], extra=()):
    feed = iter(reports)
    last = [reports[-1]]

    def fake_python_drc(*args, **kwargs):
        last[0] = next(feed, last[0])
        return last[0]

    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", fake_python_drc)
    return fix_drc_cmd.main(
        [
            str(board),
            "--only",
            "mask-copper",
            "--no-connectivity-check",
            "--max-displacement",
            "1.0",
            *extra,
        ]
    )


def test_fix_drc_keeps_a_pour_relief_the_recheck_confirms(tmp_path, monkeypatch, native_stubs):
    board = _board(tmp_path, "", zone=_zone())
    finding = _finding(ZONE, "same-net")
    code = _run(monkeypatch, board, [_report(finding), _report(), _report()])
    assert code == 0
    assert _pad_gap(board, PAD) == 0.2
    assert len(native_stubs) == 1  # the relief is graded on the refilled board


def test_fix_drc_reverts_a_pour_relief_whose_pad_finding_persists(
    tmp_path, monkeypatch, native_stubs
):
    board = _board(tmp_path, "", second_pad=PAD_TWO, zone=_zone())
    first, second = _finding(ZONE, "same-net"), _finding(ZONE, "same-net", PAD2)
    # After the refill only R1/2 still carries the error: R1/1 keeps its relief.
    code = _run(monkeypatch, board, [_report(first, second), _report(second), _report(second)])
    assert code == 2
    assert _pad_gap(board, PAD) == 0.2
    assert _pad_gap(board, PAD2) is None


def test_fix_drc_keeps_a_fragment_removal_the_recheck_confirms(tmp_path, monkeypatch, native_stubs):
    board = _exit_board(tmp_path)
    finding = _finding("trk-frag", PASS_THROUGH)
    code = _run(monkeypatch, board, [_report(finding), _report(), _report()])
    assert code == 0
    assert "trk-frag" not in _uuids(board)


def test_fix_drc_reverts_a_removal_that_breaks_copper_contact(tmp_path, monkeypatch, native_stubs):
    """The pass's own contact check is the gate the fragment rule models."""
    # The pass compares physical contact only on a board that carries a pour.
    foreign = _zone().replace('(net 1) (net_name "N1")', '(net 2) (net_name "N2")')
    board = _exit_board_with_pour(tmp_path, foreign)
    finding = _finding("trk-frag", PASS_THROUGH)
    original = MaskCopperRepairer.disconnected_copper_nets

    def split(self, before, names, *, removed=frozenset()):
        return original(self, before, names, removed=removed) | {"N1"}

    monkeypatch.setattr(MaskCopperRepairer, "disconnected_copper_nets", split)
    # Exit 1: nothing was repaired, the one move having been rolled back.
    code = _run(monkeypatch, board, [_report(finding), _report(), _report()])
    assert code == 1
    assert "trk-frag" in _uuids(board)


def test_disconnected_copper_nets_tolerates_a_removed_item(tmp_path):
    board = _exit_board(tmp_path)
    before = MaskCopperRepairer(board)
    repairer = MaskCopperRepairer(board)
    result = MaskRepairResult()
    seg = repairer._segment_by_uuid("trk-frag")
    assert (
        repairer._remove_edge_fragment(seg, "trk-frag", [PAD], repairer._openings(), result, False)
        is None
    )
    assert repairer.disconnected_copper_nets(before, {"N1"}) == {"N1"}
    assert repairer.disconnected_copper_nets(before, {"N1"}, removed={"trk-frag"}) == set()
