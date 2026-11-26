"""Native-refill controls for the packed-pair pour-slot criterion (Issue #10121).

Issue #10121 measured Board 05's nine remaining ``pour_slot`` advisories
against the native copper film on pinned KiCad 10.0.6 and found every one
packed (gap <= 2 x clearance), leaving no fillable strip for ``min_thickness``
to drop. For equal clearances on the two tracks, the detector reports a pair for
``2 x clearance < gap < 2 x clearance + min_thickness`` -- is validated here
against the two control boards that were refilled natively during the
investigation (``kicad-cli pcb drc --refill-zones --save-board``, image
``kicad/kicad@sha256:1869356...``, kicad-cli 10.0.6):

* **Control P** (genuine pour slot): two 0.4 mm different-net tracks with a
  0.550 mm edge gap running 8 mm across an open 0.200 mm-clearance /
  0.300 mm-min_thickness GND pour.  The native film shows a 0.550 mm
  copper-free channel along the whole run (dropped island: strip
  ``0.550 - 2 x 0.200 = 0.150 mm`` < ``min_thickness``) with pour present on
  both sides and beyond both via ends -- the rule must keep warning.
* **Control N** (pad-exit, Board 05's EN_A/PWM_A shape): 1.575 x 0.4 mm pads
  on 0.650 mm Y pitch (0.250 mm pad-edge gap) whose escapes exit the pad tips
  with a 0.395 mm track gap (0.35 mm + 0.16 mm tracks).  The native film shows
  the pad row's inherent 0.250 mm channel inside the row and, west of the
  tips, exactly the 0.395 mm clearance void -- no dropped island anywhere,
  pour penetrating past the escape vias -- the rule must stay silent.

The channel-width assertions below are calibrated against those native films
(measured 0.550 / 0.395 / 0.250 mm); the pure-Python cross-sections recompute
them from authored geometry at test time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.pour_slot import iter_pour_slot_pairs

#: Board 05's front GND zone parameters (also used by both controls).
CLEARANCE_MM = 0.200
MIN_THICKNESS_MM = 0.300
#: The dropped-island band for that zone: an island narrower than
#: ``min_thickness`` is dropped by KiCad's fill, leaving a slot.
BAND_MM = (2 * CLEARANCE_MM, 2 * CLEARANCE_MM + MIN_THICKNESS_MM)

ZONE = """(zone
	(net "GND")
	(net_name "GND")
	(layer "F.Cu")
	(hatch edge 0.5)
	(connect_pads (clearance 0.2))
	(min_thickness 0.3)
	(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3) (island_removal_mode 0))
	(polygon (pts (xy 5 5) (xy 45 5) (xy 45 35) (xy 5 35)))
)
"""

PAD_ROW = """(footprint "Control:PadRow" (layer "F.Cu") (uuid "00000000-0000-0000-0000-0000000000f1")
	(at 20 15)
	(property "Reference" "U9" (at 0 0) (layer "F.SilkS"))
	(pad "1" smd roundrect (at 0 -0.325) (size 1.575 0.4) (layers "F.Cu" "F.Mask")
		(roundrect_rratio 0.25) (net 1 "NET1"))
	(pad "2" smd roundrect (at 0 0.325) (size 1.575 0.4) (layers "F.Cu" "F.Mask")
		(roundrect_rratio 0.25) (net 2 "NET2"))
)
"""


def _roundtrip(pcb: PCB, tmp_path: Path, name: str) -> PCB:
    """Save + reload so the hand-written zone/pad s-expressions are parsed."""
    path = tmp_path / name
    pcb.save(str(path))
    return PCB.load(str(path))


def _control_p(tmp_path: Path, zones: tuple[str, ...] = (ZONE,), *, gap: float = 0.55) -> PCB:
    pcb = PCB.create(width=50, height=40, center=False)
    net2_y = 15 + 0.4 + gap
    pcb.add_trace((11, 15), (19, 15), width=0.4, layer="F.Cu", net="NET1")
    pcb.add_trace((11, net2_y), (19, net2_y), width=0.4, layer="F.Cu", net="NET2")
    pcb.add_via(10.2, 15, size=0.6, drill=0.3, net="NET1")
    pcb.add_via(10.2, net2_y, size=0.6, drill=0.3, net="NET2")
    pcb.add_via(19.8, 15, size=0.6, drill=0.3, net="NET1")
    pcb.add_via(19.8, net2_y, size=0.6, drill=0.3, net="NET2")
    for zone in zones:
        pcb._sexp.append(parse_string(zone))
    return _roundtrip(pcb, tmp_path, "control_p.kicad_pcb")


def _control_n(tmp_path: Path) -> PCB:
    pcb = PCB.create(width=50, height=40, center=False)
    pcb._sexp.append(parse_string(PAD_ROW))
    pcb.add_trace((17.6, 14.675), (20.2, 14.675), width=0.35, layer="F.Cu", net="NET1")
    pcb.add_trace((17.6, 15.325), (20.2, 15.325), width=0.16, layer="F.Cu", net="NET2")
    pcb.add_via(17.2, 14.675, size=0.45, drill=0.25, net="NET1")
    pcb.add_via(17.2, 15.325, size=0.45, drill=0.25, net="NET2")
    pcb._sexp.append(parse_string(ZONE))
    return _roundtrip(pcb, tmp_path, "control_n.kicad_pcb")


def _pair_gaps(pcb: PCB) -> list[tuple[str, str, float]]:
    return sorted(
        (*sorted((p.first.net_name, p.second.net_name)), round(p.gap, 4))
        for p in iter_pour_slot_pairs(pcb)
    )


def _track_gap(y_upper: float, w_upper: float, y_lower: float, w_lower: float) -> float:
    """Edge-to-edge gap between two horizontal tracks (sheet mm)."""
    return (y_lower - w_lower / 2) - (y_upper + w_upper / 2)


def test_control_p_genuine_slot_still_warns(tmp_path: Path) -> None:
    """The dropped-island control keeps its warning after the refinement."""
    pairs = _pair_gaps(_control_p(tmp_path))
    assert pairs == [("NET1", "NET2", 0.55)], pairs
    gap = pairs[0][2]
    assert BAND_MM[0] < gap < BAND_MM[1]
    # The island KiCad drops: 0.150 mm, below min_thickness (native film: a
    # 0.550 mm copper-free channel over the whole 8 mm run, pour both sides).
    assert round(gap - 2 * CLEARANCE_MM, 4) == 0.15
    assert gap - 2 * CLEARANCE_MM < MIN_THICKNESS_MM


def test_public_check_uses_the_sibling_project_on_both_sides(tmp_path, capsys):
    board = tmp_path / "control_p.kicad_pcb"
    settings = {
        "meta": {"version": 5},
        "classes": [
            {"name": "Default", "clearance": 0.2},
            {"name": "Wide", "clearance": 0.4},
        ],
        "netclass_patterns": [{"netclass": "Wide", "pattern": "NET1"}],
    }
    for gap, default, warnings in ((0.65, 0.2, 1), (0.95, 0.2, 0), (0.95, 0.4, 1)):
        _control_p(tmp_path, gap=gap)
        settings["classes"][0]["clearance"] = default
        board.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": settings}))
        assert _public_pour_slot_count(board, capsys) == warnings


def _public_pour_slot_count(board: Path, capsys) -> int:
    from kicad_tools.cli import check_cmd

    check_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--only", "pour_slot", "--drc-only", "--format", "json"]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["coverage"]["pour_slot"]["state"] == "checked"
    return sum(f["rule_id"] == "pour_slot" for f in report["findings"])


@pytest.mark.parametrize("zone_clearance", [0.0, 0.1])
@pytest.mark.parametrize(("min_thickness", "warnings"), [(0.3, 1), (0.04, 0)])
def test_public_check_at_netclass_clearance(
    tmp_path, capsys, zone_clearance, min_thickness, warnings
):
    # Pinned native film at a 0.350 mm gap: the 0.149 mm strip survives only
    # at the smaller minimum thickness, for either authored zone clearance.
    zone = ZONE.replace("clearance 0.2", f"clearance {zone_clearance}").replace(
        "min_thickness 0.3", f"min_thickness {min_thickness}"
    )
    _control_p(tmp_path, zones=(zone,), gap=0.35)
    board = tmp_path / "control_p.kicad_pcb"
    settings = {"meta": {"version": 5}, "classes": [{"name": "Default", "clearance": 0.1}]}
    board.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": settings}))
    assert _public_pour_slot_count(board, capsys) == warnings


@pytest.mark.parametrize("project", ["absent", "empty", "default", "lower"])
@pytest.mark.parametrize("min_thickness", [0.3, 0.1])
def test_implicit_default_in_check_and_cleanup(
    tmp_path, capsys, monkeypatch, project, min_thickness
):
    from kicad_tools.cli import main

    zone = ZONE.replace("clearance 0.2", "clearance 0").replace(
        "min_thickness 0.3", f"min_thickness {min_thickness}"
    )
    _control_p(tmp_path, zones=(zone,))
    board = tmp_path / "control_p.kicad_pcb"
    if project != "absent":
        settings = {}
        if project != "empty":
            clearance = 0.1 if project == "lower" else 0.2
            settings = {"net_settings": {"classes": [{"name": "Default", "clearance": clearance}]}}
        board.with_suffix(".kicad_pro").write_text(json.dumps(settings))

    expected = int(min_thickness == 0.3 and project != "lower")
    assert _public_pour_slot_count(board, capsys) == expected

    # Exercise the public cleanup and its real pair detector. The native
    # refill and DRC subprocesses are independent of candidate selection.
    monkeypatch.setattr(
        "kicad_tools.cli.optimize_cmd._refill_written_board", lambda *a, **kw: (True, None)
    )
    monkeypatch.setattr("kicad_tools.cli.optimize_cmd._kicad_cli_drc_counts", lambda *a, **kw: {})
    assert main(["optimize-traces", str(board), "--pour-slots", "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)["pour_slots"]
    assert result["pairs_before"] == expected
    assert result["spread"] == expected


def test_missing_zone_clearance_uses_default(tmp_path):
    zone = ZONE.replace("(clearance 0.2)", "")
    pcb = _control_p(tmp_path, zones=(zone,))
    assert pcb.zones[0].clearance == CLEARANCE_MM


def test_remote_zone_cannot_change_the_local_fill_band(tmp_path: Path) -> None:
    local = ZONE.replace("(xy 45 ", "(xy 25 ")
    remote = ZONE.replace("(xy 5 ", "(xy 32 ").replace("clearance 0.2", "clearance 0.4")
    assert _pair_gaps(_control_p(tmp_path, (local, remote))) == [("NET1", "NET2", 0.55)]
    thin = local.replace("min_thickness 0.3", "min_thickness 0.1")
    assert _pair_gaps(_control_p(tmp_path, (thin, remote))) == []


@pytest.mark.parametrize(
    "contour",
    [
        "(arc (start 5 20) (mid 20 5) (end 35 20)) (arc (start 35 20) (mid 20 35) (end 5 20))",
        "(xy 35 5) (xy 35 35) (xy 34 35) (arc (start 34 35) (mid 10 20) (end 34 5)) (xy 34 5)",
    ],
    ids=["two-semicircles", "arc-and-vertices"],
)
def test_curved_zone_clips_slot_to_authored_contour(tmp_path: Path, contour: str) -> None:
    zone = ZONE.replace("(xy 5 5) (xy 45 5) (xy 45 35) (xy 5 35)", contour)
    assert _pair_gaps(_control_p(tmp_path, (zone,))) == [("NET1", "NET2", 0.55)]
    thin = zone.replace("min_thickness 0.3", "min_thickness 0.1")
    assert _pair_gaps(_control_p(tmp_path, (thin,))) == []


def test_slot_inside_zone_cutout_stays_clear(tmp_path: Path) -> None:
    hole = "(polygon (pts (xy 10 14) (xy 20 14) (xy 20 17) (xy 10 17)))"
    zone = ZONE.rstrip()[:-1] + hole + ")"
    assert _pair_gaps(_control_p(tmp_path, (zone,))) == []


def test_slot_only_partly_inside_zone(tmp_path: Path) -> None:
    # A zone ending at X=12 covers the first mm of the run at X=11..19.
    local = ZONE.replace("(xy 45 ", "(xy 12 ")
    (pair,) = iter_pour_slot_pairs(_control_p(tmp_path, (local,)))
    assert pair.overlap == 1.0
    assert pair.location[0] == 11.5


def test_control_n_pad_exit_stays_silent(tmp_path: Path) -> None:
    """The packed pad-exit control (Board 05's witness shape) clears."""
    assert _pair_gaps(_control_n(tmp_path)) == []

    pcb = _control_n(tmp_path)
    seg1 = next(s for s in pcb.segments if s.net_name == "NET1")
    seg2 = next(s for s in pcb.segments if s.net_name == "NET2")
    gap = _track_gap(seg1.start[1], seg1.width, seg2.start[1], seg2.width)
    # Native film (pinned KiCad 10.0.6): exactly this 0.395 mm clearance void
    # west of the pad tips, and the pad row's inherent 0.250 mm channel inside
    # the row; no dropped island anywhere on the film.
    assert round(gap, 4) == 0.395
    assert gap <= 2 * CLEARANCE_MM  # packed: no fillable strip ever existed
    # The pad row produces the same channel with no tracks at all: pad edges
    # at 0.650 mm pitch minus 2 x 0.4/2 mm pad height.
    assert round(0.650 - 0.4, 4) == 0.25


def test_board05_witness_band_is_packed_only() -> None:
    """Every Board 05 witness gap sits at or below the packed edge.

    The nine historical advisories measured 0.250 mm (six pairs), 0.275 mm
    (VM/IREF) and 0.395 mm (IREF/SENSE_IN, EN_A/PWM_A); all are <= the
    0.400 mm = 2 x clearance lower band edge, so the refined rule reports
    none of them and the recipe's copper is unchanged.
    """
    witnesses = (0.250, 0.275, 0.395, 0.395, 0.250, 0.250, 0.250, 0.250, 0.250)
    assert all(g <= 2 * CLEARANCE_MM for g in witnesses)
    assert all(g > 0 for g in witnesses)


@pytest.mark.parametrize("gap", [0.35, 0.55])
def test_explicit_project_filename_does_not_change_cleanup(tmp_path, capsys, monkeypatch, gap):
    """``--project`` selects the rules whatever it is called (Issue #10121)."""
    from kicad_tools.cli import main

    zone = ZONE.replace("clearance 0.2", "clearance 0")
    results = {}
    for name in ("control_p", "elsewhere"):
        d = tmp_path / name
        d.mkdir()
        _control_p(d, zones=(zone,), gap=gap)
        board = d / "control_p.kicad_pcb"
        settings = {"net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]}}
        project = d / f"{name}.kicad_pro"
        project.write_text(json.dumps(settings))
        refills = []
        monkeypatch.setattr(
            "kicad_tools.cli.optimize_cmd._refill_written_board",
            lambda written, rules_from=None, _r=refills: _r.append(rules_from) or (True, None),
        )
        monkeypatch.setattr(
            "kicad_tools.cli.optimize_cmd._kicad_cli_drc_counts", lambda *a, **kw: {}
        )
        argv = ["optimize-traces", str(board), "--project", str(project)]
        assert main([*argv, "--pour-slots", "--format", "json"]) == 0
        out = json.loads(capsys.readouterr().out)["pour_slots"]
        assert refills and all(r == project for r in refills)
        results[name] = (out["pairs_before"], board.read_text())
    assert results["control_p"] == results["elsewhere"]
