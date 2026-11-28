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


#: ``(gap, Default clearance, Wide clearance, expected pour_slot warnings)``.
#: ``NET1`` sits in the ``Wide`` net class, so only a checker that reads the
#: sibling project tells the two unequal rows from their default-only reading.
PROJECT_MATRIX = [
    (0.55, 0.2, 0.2, 1),
    (0.55, 0.2, 0.4, 0),
    (0.65, 0.2, 0.4, 1),
    (0.95, 0.2, 0.4, 0),
    (0.95, 0.4, 0.4, 1),
]


def _write_project(board: Path, default: float, wide: float) -> None:
    """Give ``NET1`` a ``Wide`` net class beside ``board``."""
    settings = {
        "meta": {"version": 5},
        "classes": [
            {"name": "Default", "clearance": default},
            {"name": "Wide", "clearance": wide},
        ],
        "netclass_patterns": [{"netclass": "Wide", "pattern": "NET1"}],
    }
    board.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": settings}))


def _pour_slot_count(results) -> int:
    return sum(v.rule_id == "pour_slot" for v in results.violations)


@pytest.mark.parametrize(("gap", "default", "wide", "warnings"), PROJECT_MATRIX)
def test_check_and_audit_use_the_sibling_project_on_both_sides(
    tmp_path, capsys, monkeypatch, gap, default, wide, warnings
):
    from kicad_tools.audit.auditor import ManufacturingAudit

    pcb = _control_p(tmp_path, gap=gap)
    board = tmp_path / "control_p.kicad_pcb"
    _write_project(board, default, wide)
    assert _public_pour_slot_count(board, capsys) == warnings
    audit = ManufacturingAudit(board, manufacturer="jlcpcb")
    # The manufacturing checker owns pour_slot. Native geometry has a separate tally.
    monkeypatch.setattr(audit, "_merge_geometric_drc", lambda status: None)
    drc = audit._check_drc(pcb)
    assert drc.violations_by_type
    assert drc.violations_by_type.get("pour_slot", 0) == warnings


def _public_pour_slot_count(board: Path, capsys) -> int:
    from kicad_tools.cli import check_cmd

    check_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--only", "pour_slot", "--drc-only", "--format", "json"]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["coverage"]["pour_slot"]["state"] == "checked"
    return sum(f["rule_id"] == "pour_slot" for f in report["findings"])


@pytest.mark.parametrize(("gap", "default", "wide", "warnings"), PROJECT_MATRIX)
def test_unparameterized_checker_matches_public_check(
    tmp_path, capsys, gap, default, wide, warnings
):
    """``check_all()`` and ``check_pour_slot()`` with no clearances read the
    sibling project, exactly as ``kct check`` does (Issue #10683)."""
    from kicad_tools.validate import DRCChecker

    _control_p(tmp_path, gap=gap)
    board = tmp_path / "control_p.kicad_pcb"
    _write_project(board, default, wide)
    assert _public_pour_slot_count(board, capsys) == warnings

    checker = DRCChecker(PCB.load(str(board)), manufacturer="jlcpcb")
    assert _pour_slot_count(checker.check_all()) == warnings
    assert _pour_slot_count(checker.check_pour_slot()) == warnings


@pytest.mark.parametrize("use_symlink", [False, True], ids=["file", "symlink"])
def test_relative_load_keeps_the_selected_project_after_chdir(tmp_path, monkeypatch, use_symlink):
    from kicad_tools.validate import DRCChecker

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    name = Path("control_p.kicad_pcb")
    _control_p(second, gap=0.95)
    if use_symlink:
        (first / name).symlink_to(second / name)
    else:
        (first / name).write_bytes((second / name).read_bytes())
    _write_project(first / name, 0.4, 0.4)
    _write_project(second / name, 0.2, 0.2)

    monkeypatch.chdir(first)
    pcb = PCB.load(name)
    monkeypatch.chdir(second)
    checker = DRCChecker(pcb, manufacturer="jlcpcb")
    assert pcb.path == name
    assert _pour_slot_count(checker.check_pour_slot()) == 1
    assert _pour_slot_count(checker.check_all()) == 1
    assert _pour_slot_count(checker.check_pour_slot({"": 0.2})) == 0


def test_relative_save_as_binds_later_checks_and_saves_to_the_new_project(tmp_path, monkeypatch):
    from kicad_tools.validate import DRCChecker

    pcb = _control_p(tmp_path, gap=0.95)
    _write_project(tmp_path / "control_p.kicad_pcb", 0.4, 0.4)
    destination = tmp_path / "destination"
    destination.mkdir()
    monkeypatch.chdir(destination)
    name = Path("copy.kicad_pcb")
    pcb.save(name)
    _write_project(destination / name, 0.2, 0.2)

    monkeypatch.chdir(tmp_path)
    with pytest.raises(OSError):
        pcb.save(Path("control_p.kicad_pcb") / "invalid.kicad_pcb")
    assert pcb.path == name
    checker = DRCChecker(pcb, manufacturer="jlcpcb")
    assert _pour_slot_count(checker.check_pour_slot()) == 0
    assert _pour_slot_count(checker.check_all()) == 0
    pcb.save()
    assert not (tmp_path / name).exists()
    assert (destination / name).is_file()


@pytest.mark.parametrize(("gap", "default", "wide", "warnings"), PROJECT_MATRIX)
def test_mfr_validate_matches_public_check(tmp_path, capsys, gap, default, wide, warnings):
    """``kct mfr validate`` counts ``pour_slot`` the way ``kct check`` does,
    in JSON and text mode (Issue #10683)."""
    from kicad_tools.cli.mfr import main as mfr_main

    _control_p(tmp_path, gap=gap)
    board = tmp_path / "control_p.kicad_pcb"
    _write_project(board, default, wide)
    assert _public_pour_slot_count(board, capsys) == warnings

    mfr_main(["validate", str(board), "jlcpcb", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["violations"] == []
    poured = sum(w["rule_id"] == "pour_slot" for w in data["warnings"])
    assert poured == warnings
    # Text mode prints a single tally for every warning: check it against the
    # non-pour-slot warnings of the JSON run.
    other = len(data["warnings"]) - poured

    mfr_main(["validate", str(board), "jlcpcb"])
    out = capsys.readouterr().out
    assert f"Warnings: {other + warnings} (run `kct check {board} --mfr jlcpcb` to list)" in out


def test_explicit_clearances_override_the_sibling_project(tmp_path) -> None:
    """Explicit clearances win over the project beside ``pcb.path`` (Issue #10683)."""
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules
    from kicad_tools.validate import DRCChecker

    # The project puts NET1 in ``Wide`` (0.40): the pair is packed, no slot.
    _control_p(tmp_path, gap=0.55)
    board = tmp_path / "control_p.kicad_pcb"
    _write_project(board, 0.2, 0.4)
    checker = DRCChecker(PCB.load(str(board)), manufacturer="jlcpcb")
    assert _pour_slot_count(checker.check_pour_slot()) == 0
    # An explicit mapping that ignores the project keeps 0.20: the slot is back.
    assert _pour_slot_count(checker.check_pour_slot({"": 0.2})) == 1
    assert _pour_slot_count(checker.check_all(pour_slot_clearances={"": 0.2})) == 1

    # Every net in ``Default`` (0.20): a slot. An explicit rules object with
    # the wider clearance packs it again.
    _write_project(board, 0.2, 0.2)
    checker = DRCChecker(PCB.load(str(board)), manufacturer="jlcpcb")
    assert _pour_slot_count(checker.check_pour_slot()) == 1
    wide = BoardClearanceRules(class_clearance={"Default": 0.4})
    assert _pour_slot_count(checker.check_pour_slot(wide)) == 0
    assert _pour_slot_count(checker.check_all(pour_slot_clearances=wide)) == 0


@pytest.mark.parametrize("project", ["absent", "empty"])
def test_checker_without_a_project_keeps_the_default_clearance(tmp_path, project) -> None:
    """No readable sibling project: the default-clearance reading is unchanged."""
    from kicad_tools.validate import DRCChecker
    from kicad_tools.validate.rules.pour_slot import PourSlotRule

    pcb = _control_p(tmp_path)
    board = tmp_path / "control_p.kicad_pcb"
    if project == "empty":
        board.with_suffix(".kicad_pro").write_text("{}")
    checker = DRCChecker(PCB.load(str(board)), manufacturer="jlcpcb")
    expected = _pour_slot_count(PourSlotRule(None).check(pcb, checker.design_rules))
    assert expected == 1
    assert _pour_slot_count(checker.check_all()) == expected
    assert _pour_slot_count(checker.check_pour_slot()) == expected


def test_checker_without_a_board_path_keeps_the_default_clearances(monkeypatch) -> None:
    """An in-memory or mocked PCB never resolves clearances from disk."""
    from unittest.mock import MagicMock

    import kicad_tools.validate.checker as checker_module
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules
    from kicad_tools.validate import DRCChecker
    from kicad_tools.validate.violations import DRCResults

    def _no_disk_read(pcb_path):
        raise AssertionError(f"clearances must not be read from {pcb_path!r}")

    monkeypatch.setattr(BoardClearanceRules, "from_board", _no_disk_read)

    # ``PCB.create()`` is never saved, so ``pcb.path`` is ``None``.
    memory = DRCChecker(PCB.create(width=50, height=40, center=False), manufacturer="jlcpcb")
    assert memory.pcb.path is None
    assert _pour_slot_count(memory.check_all()) == 0
    assert _pour_slot_count(memory.check_pour_slot()) == 0

    # A ``MagicMock`` PCB: ``pcb.path`` is a mock, neither ``None`` nor a path.
    seen = []

    class _Rule:
        def __init__(self, netclass_clearances=None):
            seen.append(netclass_clearances)

        def check(self, pcb, design_rules):
            return DRCResults()

    monkeypatch.setattr(checker_module, "PourSlotRule", _Rule)
    pcb = MagicMock()
    pcb.board_origin = (0.0, 0.0)
    mocked = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
    assert mocked.check_pour_slot().violations == []
    assert seen == [None]


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


def _write_connected_board(board: Path, gap: float, zones: tuple[str, ...]) -> None:
    pcb = PCB.create(width=50, height=40, center=False)
    pads = [(7, 7, "GND")]
    for net, y, pad_y in (("NET1", 15, 12), ("NET2", 15.4 + gap, 19)):
        pcb.add_trace((11, y), (19, y), width=0.4, layer="F.Cu", net=net)
        for x in (11, 19):
            pcb.add_trace((x, y), (x, pad_y), width=0.4, layer="F.Cu", net=net)
            pads.append((x, pad_y, net))
    for i, (x, y, net) in enumerate(pads):
        number = pcb.add_net(net).number
        pcb._sexp.append(
            parse_string(
                f'(footprint "Test:Pad" (layer "F.Cu") (at {x} {y}) '
                f'(property "Reference" "TP{i}" (at 0 0) (layer "F.Fab")) '
                f'(pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu") (net {number} "{net}")))'
            )
        )
    for zone in zones:
        pcb._sexp.append(parse_string(zone))
    pcb.save(board)


@pytest.mark.parametrize("right_edge", [48, 49])
def test_remote_cutout_preserves_public_slot_and_spread(tmp_path, capsys, monkeypatch, right_edge):
    from kicad_tools.cli import main, optimize_cmd

    board = tmp_path / "control.kicad_pcb"
    _write_connected_board(board, 2.15, (ZONE,))
    board.with_suffix(".kicad_dru").write_text(
        """(version 1)
(rule "track to pour" (condition "A.Type == 'Track' && B.Type == 'Zone'")
    (constraint clearance (min 1mm)))
"""
    )
    pcb = PCB.load(board)
    pcb._sexp.append(
        parse_string(
            f"(gr_rect (start 22 1) (end {right_edge} 39) (stroke (width 0.05) (type default)) "
            '(fill none) (layer "Edge.Cuts"))'
        )
    )
    pcb.save(board)
    # The strips at X=11..19 see the same copper on either side of the point
    # where the distant hole becomes larger than the remaining laminate.
    assert _public_pour_slot_count(board, capsys) == 1
    monkeypatch.setattr(optimize_cmd, "_kicad_cli_drc_counts", lambda *a, **kw: {})
    monkeypatch.setattr(optimize_cmd, "_refill_written_board", lambda *a, **kw: (True, None))
    assert (
        main(["optimize-traces", str(board), "--net", "GND", "--pour-slots", "--format", "json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["pour_slots"]["spread"] == 1
    assert _public_pour_slot_count(board, capsys) == 0


def _saved_gap_and_film(board: Path, pour_net: str = "GND"):
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    saved = PCB.load(board)
    lower, upper = sorted(
        (s for s in saved.segments if s.start[1] == s.end[1]), key=lambda seg: seg.start[1]
    )
    bottom = lower.start[1] + lower.width / 2
    top = upper.start[1] - upper.width / 2
    pour = next(zone for zone in saved.zones if zone.net_name == pour_net)
    film = unary_union([Polygon(ring).buffer(0) for ring in pour.filled_polygons])
    assert film.area > 0
    sections = [film.intersection(LineString([(x, bottom), (x, top)])).length for x in (13, 15, 17)]
    return top - bottom, film, sections


@pytest.mark.parametrize(
    ("pour_net", "moves", "expected_gap", "strip"),
    [("NET1", 0, 0.750, 0.3494), ("NET2", 0, 0.750, 0.5494), ("GND", 1, 0.905, 0.3040)],
)
def test_track_connected_to_pour_in_check_cleanup_and_fill(
    tmp_path, capsys, pour_net, moves, expected_gap, strip
):
    import shutil

    from kicad_tools.cli import main

    if not shutil.which("kicad-cli"):
        pytest.skip("native refill requires kicad-cli")
    board = tmp_path / "connected.kicad_pcb"
    _write_connected_board(board, 0.750, (ZONE.replace('"GND"', f'"{pour_net}"'),))
    project = {"net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]}}
    board.with_suffix(".kicad_pro").write_text(json.dumps(project))
    board.with_suffix(".kicad_dru").write_text(
        """(version 1)
(rule "NET2 zone clearance" (condition "A.NetName == 'NET2' && B.Type == 'Zone'")
    (constraint clearance (min 0.4mm)))
"""
    )
    assert _public_pour_slot_count(board, capsys) == moves
    assert (
        main(["optimize-traces", str(board), "--net", "GND", "--pour-slots", "--format", "json"])
        == 0
    )
    cleanup = json.loads(capsys.readouterr().out)["pour_slots"]
    assert cleanup["pairs_before"] == cleanup["spread"] == moves
    assert cleanup["pairs_after"] == 0
    saved_gap, _, sections = _saved_gap_and_film(board, pour_net)
    assert saved_gap == pytest.approx(expected_gap)
    assert sections == pytest.approx([strip] * 3, abs=0.002)


@pytest.mark.parametrize(("gap", "expected_gap", "warnings"), [(0.55, 0.55, 0), (0.705, 0.905, 1)])
def test_clearance_precedence_in_check_cleanup_and_fill(
    tmp_path, capsys, gap, expected_gap, warnings
):
    import shutil

    from kicad_tools.cli import main

    if not shutil.which("kicad-cli"):
        pytest.skip("native refill requires kicad-cli")

    films = []
    global_rule = '(rule "Global" (constraint clearance (min 0.2mm)))'
    layer_rule = '(rule "Front" (layer "F.Cu") (constraint clearance (min 0.3mm)))'
    net_rule = (
        """(rule "Ground" (condition "A.NetName == 'GND'") (constraint clearance (min 0.3mm)))"""
    )
    for floor_source, zone_clearance, rules in (
        ("board", 0.1, []),
        ("board", 0.3, []),
        ("custom", 0.1, ['(rule "Global" (constraint clearance (min 0.3mm)))']),
        ("layer", 0.1, [global_rule, layer_rule]),
        ("net", 0.1, [global_rule, net_rule]),
        ("reversed", 0.1, [layer_rule, global_rule]),
    ):
        directory = tmp_path / f"{floor_source}-{zone_clearance}"
        directory.mkdir()
        board = directory / "control_p.kicad_pcb"
        zone = ZONE.replace("clearance 0.2", f"clearance {zone_clearance}")
        _write_connected_board(board, gap, (zone,))
        if rules:
            board.with_suffix(".kicad_dru").write_text("\n".join(["(version 1)", *rules, ""]))
        board_min = 0.3 if floor_source == "board" else 0.0
        default_clearance = 0.3 if floor_source in {"layer", "net", "reversed"} else 0.2
        project = {
            "board": {"design_settings": {"rules": {"min_clearance": board_min}}},
            "net_settings": {"classes": [{"name": "Default", "clearance": default_clearance}]},
        }
        board.with_suffix(".kicad_pro").write_text(json.dumps(project))
        reversed_order = floor_source == "reversed"
        expected_warnings = int(gap == 0.55) if reversed_order else warnings
        assert _public_pour_slot_count(board, capsys) == expected_warnings
        assert (
            main(
                ["optimize-traces", str(board), "--net", "GND", "--pour-slots", "--format", "json"]
            )
            == 0
        )
        report = json.loads(capsys.readouterr().out)
        assert report["pour_slots"]["spread"] == expected_warnings
        assert report["pour_slots"]["pairs_after"] == 0
        saved_gap, film, sections = _saved_gap_and_film(board)
        assert saved_gap == pytest.approx(0.705 if reversed_order else expected_gap)
        assert sections == pytest.approx(
            [0.304 if warnings or reversed_order else 0.0] * 3, abs=0.002
        )
        if not reversed_order:
            films.append(film)
    for film in films[1:]:
        assert films[0].symmetric_difference(film).area == pytest.approx(0)


def _priority_zone(net: str, priority: int, x_max: int, clearance: float) -> str:
    return (
        ZONE.replace('(net "GND")', f'(net "{net}")')
        .replace('(net_name "GND")', f'(net_name "{net}")')
        .replace("clearance 0.2", f"clearance {clearance}")
        .replace("(hatch edge 0.5)", f"(priority {priority})\n\t(hatch edge 0.5)")
        .replace("(xy 45 5) (xy 45 35)", f"(xy {x_max} 5) (xy {x_max} 35)")
    )


def test_higher_priority_zone_governs_overlapping_lower_zone(tmp_path: Path) -> None:
    """A lower-priority outline cannot change the slot decision under a higher one."""
    high = _priority_zone("GND", 3, 32, 0.4)
    low = _priority_zone("LOW", 0, 45, 0.2)
    high_only = _pair_gaps(_control_p(tmp_path, (high,)))
    low_only = _pair_gaps(_control_p(tmp_path, (low,)))
    both = _pair_gaps(_control_p(tmp_path, (high, low)))
    both_reversed = _pair_gaps(_control_p(tmp_path, (low, high)))
    assert high_only == []  # 0.55 mm is packed against the 0.4 mm clearances
    assert low_only == [("NET1", "NET2", 0.55)]  # genuine slot still warns
    assert both == both_reversed == high_only
    # The same pair next to (outside) the high zone is governed by the low zone.
    narrow = _priority_zone("GND", 3, 8, 0.4)
    assert _pair_gaps(_control_p(tmp_path, (narrow, low))) == low_only


_RULE_AREA = """(zone
	(net 0)
	(net_name "")
	(layers "F.Cu")
	(uuid "00000000-0000-0000-0000-0000000000a1")
	(name "WideSpacing")
	(hatch edge 0.5)
	(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
	(polygon (pts (xy {x0} 10) (xy {x1} 10) (xy {x1} 25) (xy {x0} 25)))
)
"""


def _rules_board(tmp_path: Path, condition: str, *, gap: float, area_x=(32, 42)):
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules

    area = _RULE_AREA.format(x0=area_x[0], x1=area_x[1])
    pcb = _control_p(tmp_path, (ZONE, area), gap=gap)
    board = tmp_path / "control_p.kicad_pcb"
    board.with_suffix(".kicad_dru").write_text(
        f'(version 1)\n(rule "Wide clearance"\n  (condition "{condition}")\n'
        "  (constraint clearance (min 0.3mm)))\n"
    )
    return pcb, BoardClearanceRules.from_board(board)


@pytest.mark.parametrize(
    "condition",
    [
        "A.Type == 'Track' && A.intersectsArea('WideSpacing')",
        "A.Type == 'Zone' && A.Layer == 'B.Cu'",
    ],
)
def test_unrelated_rule_does_not_change_pour_slot_decision(tmp_path, condition):
    """A rule scoped away from these tracks neither hides nor invents a slot."""
    for gap, expected in ((0.55, 1), (0.705, 0)):
        pcb, rules = _rules_board(tmp_path, condition, gap=gap)
        assert len(list(iter_pour_slot_pairs(pcb, rules))) == expected, (condition, gap)


def test_rule_area_on_the_tracks_applies_its_clearance(tmp_path):
    cond = "A.Type == 'Track' && A.intersectsArea('WideSpacing')"
    # 0.3 mm applies: 0.55 is packed (2 x 0.3 = 0.6); 0.705 lies in (0.6, 0.9).
    for gap, expected in ((0.55, 0), (0.705, 1)):
        pcb, rules = _rules_board(tmp_path, cond, gap=gap, area_x=(5, 25))
        assert len(list(iter_pour_slot_pairs(pcb, rules))) == expected, gap


def test_unresolvable_rule_keeps_genuine_slot(tmp_path):
    """A predicate the evaluator cannot decide must not erase the 0.55 mm slot."""
    pcb, rules = _rules_board(tmp_path, "A.insideArea('WideSpacing')", gap=0.55)
    assert len(list(iter_pour_slot_pairs(pcb, rules))) == 1


def test_undecided_rule_needs_a_confirmed_slot(tmp_path):
    """0.705 mm is a slot only if the rule applies; undecided means no pair to move."""
    pcb, rules = _rules_board(
        tmp_path, "A.Type == 'Track' && A.enclosedByArea('WideSpacing')", gap=0.705
    )
    assert list(iter_pour_slot_pairs(pcb, rules)) == []


def test_enclosed_by_area_is_decided_from_geometry(tmp_path):
    """A distant area leaves the rule inapplicable; one over the tracks applies it.

    Review of #10128: the undecided reading used to decide the warning, so a
    distant exemption/tightening flipped the answer for the same tracks.
    """
    cond = "A.Type == 'Track' && A.enclosedByArea('WideSpacing')"
    for area_x, gap, expected in (
        ((32, 42), 0.55, 1),  # distant: default clearance, genuine slot
        ((32, 42), 0.705, 0),  # distant: default clearance, no slot
        ((5, 25), 0.55, 0),  # local: 0.3 mm applies, 0.55 is packed
        ((5, 25), 0.705, 1),  # local: 0.3 mm applies, slot in (0.6, 0.9)
    ):
        pcb, rules = _rules_board(tmp_path, cond, gap=gap, area_x=area_x)
        assert len(list(iter_pour_slot_pairs(pcb, rules))) == expected, (area_x, gap)


@pytest.mark.parametrize("local_first", [True, False])
def test_same_named_rule_areas_ignore_serialization_order(tmp_path, local_first):
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules

    local = _RULE_AREA.format(x0=5, x1=25)
    distant = _RULE_AREA.format(x0=32, x1=42).replace("0000000000a1", "0000000000a2")
    areas = (local, distant) if local_first else (distant, local)
    pcb = _control_p(tmp_path, (ZONE, *areas), gap=0.705)
    board = tmp_path / "control_p.kicad_pcb"
    board.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "Wide clearance"\n'
        "  (condition \"A.Type == 'Track' && A.intersectsArea('WideSpacing')\")\n"
        "  (constraint clearance (min 0.3mm)))\n"
    )
    rules = BoardClearanceRules.from_board(board)
    assert len(list(iter_pour_slot_pairs(pcb, rules))) == 1


@pytest.mark.parametrize("gap", [0.55, 0.705])
@pytest.mark.parametrize(
    ("predicate", "area_x", "applies"),
    [
        ("A.Width > 0.5mm", None, False),
        ("A.Width < 0.5mm", None, True),
        ("A.enclosedByArea('WideSpacing')", (32, 42), False),
        ("A.enclosedByArea('WideSpacing')", (5, 25), True),
    ],
)
def test_conditional_exemption_in_check_cleanup_and_fill(
    tmp_path, capsys, gap, predicate, area_x, applies
):
    import shutil

    from kicad_tools.cli import main

    if not shutil.which("kicad-cli"):
        pytest.skip("native refill requires kicad-cli")
    board = tmp_path / "conditional.kicad_pcb"
    zones = (ZONE,) if area_x is None else (ZONE, _RULE_AREA.format(x0=area_x[0], x1=area_x[1]))
    _write_connected_board(board, gap, zones)
    settings = {
        "meta": {"version": 5},
        "classes": [{"name": name, "clearance": 0.3} for name in ("Default", "Signal")],
        "netclass_patterns": [{"netclass": "Signal", "pattern": "NET*"}],
    }
    board.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": settings}))
    board.with_suffix(".kicad_dru").write_text(
        f"""(version 1)
(rule "Exemption" (condition "A.Type == 'Track' && {predicate}") (constraint clearance (min 0.2mm)))
"""
    )
    expected_spread = int(gap == (0.55 if applies else 0.705))
    assert _public_pour_slot_count(board, capsys) == expected_spread
    assert (
        main(["optimize-traces", str(board), "--net", "GND", "--pour-slots", "--format", "json"])
        == 0
    )
    cleanup = json.loads(capsys.readouterr().out)["pour_slots"]
    assert cleanup["pairs_before"] == cleanup["spread"] == expected_spread
    assert cleanup["pairs_after"] == 0
    saved_gap, _, sections = _saved_gap_and_film(board)
    assert saved_gap == pytest.approx(0.705 if applies else (0.905 if expected_spread else gap))
    assert sections == pytest.approx([0.304 if applies or expected_spread else 0.0] * 3, abs=0.002)


@pytest.mark.parametrize("gap", [0.55, 0.705, 0.84])
def test_unresolved_clearance_is_advisory_only(tmp_path, gap):
    from kicad_tools.optim.pour_slot_cleanup import cleanup_pour_slots
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules
    from kicad_tools.validate.copper_refs import board_origin
    from kicad_tools.validate.rules.pour_slot import PourSlotRule

    pcb = _control_p(tmp_path, gap=gap)
    board = tmp_path / "control_p.kicad_pcb"
    # At 0.84 mm only the middle NET1 clearance (0.50) can leave a slot.
    board.with_suffix(".kicad_dru").write_text(
        """(version 1)
(rule "Long" (condition "A.NetName == 'NET1' && A.Length > 9mm") (constraint clearance (min 0.5mm)))
(rule "Longer" (condition "A.NetName == 'NET1' && A.Length > 10mm") (constraint clearance (min 0.8mm)))
"""
    )
    rules = BoardClearanceRules.from_board(board)
    rule = PourSlotRule(rules)
    (pair,) = rule.iter_pairs(pcb)
    finding = rule._violation(pair, board_origin(pcb))
    assert "possible" in finding.message.lower()
    assert "Long" in finding.message and finding.required_value is None
    before = [(s.start, s.end, s.width) for s in pcb.segments]
    result = cleanup_pour_slots(pcb, rules, referee=lambda _: {}, pack=True)
    assert result.pairs_before == result.pairs_after == result.left_alone == 1
    assert not result.changed
    assert "Long" in result.left_alone_reasons[0]
    assert [(s.start, s.end, s.width) for s in pcb.segments] == before


@pytest.mark.parametrize(("pour", "expected"), [("allowed", 1), ("not_allowed", 0)])
def test_copperpour_keepout_leaves_no_slot(tmp_path: Path, pour: str, expected: int) -> None:
    """A copper-pour keepout over the gap leaves no fill, so no slot (review fix)."""
    area = _RULE_AREA.format(x0=9, x1=21).replace("(copperpour allowed)", f"(copperpour {pour})")
    pcb = _control_p(tmp_path, (ZONE, area))
    assert len(list(iter_pour_slot_pairs(pcb))) == expected


@pytest.mark.parametrize(("permission", "moves"), [("not_allowed", 0), ("allowed", 1)])
def test_footprint_pour_keepout_in_check_cleanup_and_fill(tmp_path, capsys, permission, moves):
    import shutil

    from kicad_tools.cli import main
    from kicad_tools.cli.runner import run_refill_zones

    if not shutil.which("kicad-cli"):
        pytest.skip("native refill requires kicad-cli")
    board = tmp_path / "footprint-keepout.kicad_pcb"
    area = _RULE_AREA.format(x0=9, x1=21).replace("copperpour allowed", f"copperpour {permission}")
    footprint = (
        '(footprint "Test:Keepout" (layer "F.Cu") (at 0 0) '
        '(property "Reference" "U9" (at 0 0) (layer "F.Fab")) ' + area + ")"
    )
    _write_connected_board(board, 0.750, (ZONE, footprint))
    board.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "NET2 to zone" '
        """(condition "A.NetName == 'NET2' && B.Type == 'Zone'") """
        "(constraint clearance (min 0.4mm)))\n"
    )
    assert _public_pour_slot_count(board, capsys) == moves
    refill = run_refill_zones(board, require_save=True)
    assert refill.success, refill.stderr
    assert _public_pour_slot_count(board, capsys) == moves
    assert (
        main(["optimize-traces", str(board), "--net", "GND", "--pour-slots", "--format", "json"])
        == 0
    )
    cleanup = json.loads(capsys.readouterr().out)["pour_slots"]
    assert cleanup["pairs_before"] == cleanup["spread"] == moves
    assert cleanup["pairs_after"] == 0
    gap, _, strips = _saved_gap_and_film(board)
    assert gap == pytest.approx(0.905 if moves else 0.750)
    assert strips == pytest.approx([0.304 if moves else 0.0] * 3, abs=0.002)


@pytest.mark.parametrize("selector", ["*.Cu", "F&B.Cu"])
@pytest.mark.parametrize(("subject", "expected"), [("pour", 1), ("keepout", 0), ("named-area", 0)])
def test_zone_selectors_before_and_after_native_save(tmp_path, capsys, selector, subject, expected):
    import shutil

    from kicad_tools.cli.runner import run_refill_zones

    if not shutil.which("kicad-cli"):
        pytest.skip("native save requires kicad-cli")
    board = tmp_path / "layer-selectors.kicad_pcb"
    layers = f'(layers "{selector}")'
    if subject == "pour":
        _write_connected_board(board, 0.550, (ZONE.replace('(layer "F.Cu")', layers),))
    else:
        area = _RULE_AREA.format(x0=9, x1=21).replace('(layers "F.Cu")', layers)
        if subject == "keepout":
            area = area.replace("copperpour allowed", "copperpour not_allowed")
        _write_connected_board(board, 0.550, (ZONE, area))
        if subject == "named-area":
            board.with_suffix(".kicad_dru").write_text(
                '(version 1)\n(rule "Wide clearance" '
                """(condition "A.Type == 'Track' && A.intersectsArea('WideSpacing')") """
                "(constraint clearance (min 0.3mm)))\n"
            )
    assert _public_pour_slot_count(board, capsys) == expected
    refill = run_refill_zones(board, require_save=True)
    assert refill.success, refill.stderr
    assert selector not in [item for zone in PCB.load(board).zones for item in zone.layers]
    assert _public_pour_slot_count(board, capsys) == expected


@pytest.mark.parametrize(
    ("selector", "expected_layers"),
    [
        ("*.Cu", {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}),
        ("F&B.Cu", {"F.Cu", "B.Cu"}),
        ("In1.Cu", {"In1.Cu"}),
        ("*.In.Cu", set()),  # KiCad 10.0.6 saves this unknown zone layer as Rescue.
    ],
)
def test_wildcard_pour_on_four_layer_stack(tmp_path, selector, expected_layers):
    pcb = PCB.create(width=50, height=40, center=False, layers=4)
    for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
        pcb.add_trace((11, 15), (19, 15), width=0.4, layer=layer, net="NET1")
        pcb.add_trace((11, 15.95), (19, 15.95), width=0.4, layer=layer, net="NET2")
    pcb._sexp.append(parse_string(ZONE.replace('(layer "F.Cu")', f'(layers "{selector}")')))
    saved = _roundtrip(pcb, tmp_path, "four-layer.kicad_pcb")
    assert {pair.layer for pair in iter_pour_slot_pairs(saved)} == expected_layers
