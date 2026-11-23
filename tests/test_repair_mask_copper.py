"""Re-centring own-pad track entries for ``mask_to_copper`` (issue #9334).

Fixtures are synthetic boards: one rect pad (R1/1, net N1) centred at (100, 100)
spanning x 99..101, y 99.5..100.5.  The mask findings are built directly (the
native assessment needs kicad-cli); ``fix-drc`` end-to-end runs stub the
re-check so the revert path is exercised without it.  The baseline-comparison
regression tests at the bottom drive the real ``kct check`` rule set on both
sides of the move instead (PR #9369 review): a stub cannot observe that this
engine names a track by its geometry and reports a closest-approach location,
both of which shift with the copper.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kicad_tools.cli import fix_drc_cmd
from kicad_tools.drc.repair_mask_copper import MaskCopperRepairer
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.sexp import parse_file

PAD = "pad-r1-1"

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
    (pad "1" smd rect (at 0 0) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1") (uuid "pad-r1-1"))
  )
%s
)
"""

OWN_ENTRY = "same-net own-pad entry"


def _board(tmp_path: Path, copper: str) -> Path:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(BOARD % copper)
    return path


def _seg(uuid: str, a, b, net: int = 1, width: float = 0.2, layer: str = "F.Cu") -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net {net}) (uuid "{uuid}"))'
    )


def _finding(conductor: str, marker: str = OWN_ENTRY, severity: Severity = Severity.ERROR):
    return DRCViolation(
        type=ViolationType.MASK_TO_COPPER,
        type_str="mask_to_copper",
        severity=severity,
        message=f"Mask-to-copper overlap: error; opening R1/1; {marker}",
        locations=[Location(x_mm=100.0, y_mm=100.3, layer="F.Cu")],
        items=[PAD, conductor],
    )


def _end(path: Path, uuid: str) -> list[tuple[float, float]]:
    doc = parse_file(path)
    for node in doc.find_all("segment"):
        if str(node.find("uuid").get_first_atom()).strip('"') == uuid:
            s = [float(a) for a in node.find("start").get_atoms()]
            e = [float(a) for a in node.find("end").get_atoms()]
            return [(s[0], s[1]), (e[0], e[1])]
    raise AssertionError(uuid)


# Off-centre entry: starts outside at (95, 100.3), ends inside the pad at (100.7, 100.3).
ENTRY = _seg("trk-a", (95, 100.3), (100.7, 100.3))


def test_off_centre_entry_end_moves_to_pad_centre(tmp_path):
    board = _board(tmp_path, ENTRY)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-a")], max_displacement=1.0, clearance_mm=0.1)
    assert result.repaired == 1 and not result.unrepaired
    repairer.save(board)
    start, end = _end(board, "trk-a")
    assert start == (95.0, 100.3)  # the outside end is never touched
    assert end == (100.0, 100.0)


def test_dry_run_predicts_without_modifying(tmp_path):
    board = _board(tmp_path, ENTRY)
    before = board.read_text()
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-a")], max_displacement=1.0, dry_run=True)
    assert result.repaired == 1 and len(result.actions) == 1
    assert not repairer.modified
    assert board.read_text() == before


def test_undo_restores_the_original_end(tmp_path):
    board = _board(tmp_path, ENTRY)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-a")], max_displacement=1.0)
    assert repairer.undo_action(result.actions[0])
    repairer.save(board)
    assert _end(board, "trk-a")[1] == (100.7, 100.3)


def test_same_net_joined_segment_end_moves_with_it_and_undoes(tmp_path):
    board = _board(tmp_path, ENTRY + "\n" + _seg("trk-b", (100.7, 100.3), (100.7, 105)))
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-a")], max_displacement=1.0)
    assert result.repaired == 1
    repairer.save(board)
    assert _end(board, "trk-a")[1] == (100.0, 100.0)
    assert _end(board, "trk-b")[0] == (100.0, 100.0)  # still connected
    undo = MaskCopperRepairer(board)
    assert undo.undo_action(result.actions[0])
    undo.save(board)
    assert _end(board, "trk-a")[1] == (100.7, 100.3)
    assert _end(board, "trk-b")[0] == (100.7, 100.3)


def test_move_longer_than_max_displacement_is_declined(tmp_path):
    board = _board(tmp_path, ENTRY)
    result = MaskCopperRepairer(board).repair([_finding("trk-a")], max_displacement=0.5)
    assert result.repaired == 0
    assert result.skipped("exceeds_max_displacement") == 1


@pytest.mark.parametrize(
    "copper, finding, reason",
    [
        # pass-through: no end inside the pad
        (
            _seg("trk-a", (95, 100.3), (105, 100.3)),
            _finding("trk-a", "same-net pass-through"),
            "not_own_pad_entry",
        ),
        # same marker but geometry has no end inside
        (_seg("trk-a", (95, 100.3), (105, 100.3)), _finding("trk-a"), "no_end_inside_pad"),
        # both ends inside the pad
        (_seg("trk-a", (99.5, 100.3), (100.7, 100.3)), _finding("trk-a"), "both_ends_inside_pad"),
        # different-net finding
        (ENTRY, _finding("trk-a", "different-net"), "not_own_pad_entry"),
        # segment on another net than the pad even though the message says same-net
        (_seg("trk-a", (95, 100.3), (100.7, 100.3), net=2), _finding("trk-a"), "not_same_net"),
        # wrong layer
        (
            _seg("trk-a", (95, 100.3), (100.7, 100.3), layer="B.Cu"),
            _finding("trk-a"),
            "pad_not_on_segment_layer",
        ),
        # conductor is a via
        (
            '  (via (at 100.7 100.3) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "trk-a"))',
            _finding("trk-a"),
            "not_a_straight_segment",
        ),
        # a via sits at the moved end
        (
            ENTRY
            + '\n  (via (at 100.7 100.3) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))',
            _finding("trk-a"),
            "junction_at_moved_end",
        ),
        # another net's track ends at the moved end
        (
            ENTRY + "\n" + _seg("trk-b", (100.7, 100.3), (103, 101), net=2),
            _finding("trk-a"),
            "junction_at_moved_end",
        ),
    ],
)
def test_other_classes_are_never_touched(tmp_path, copper, finding, reason):
    board = _board(tmp_path, copper)
    before = board.read_text()
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([finding], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped(reason) == 1, result.unrepaired
    assert not repairer.modified
    assert board.read_text() == before


def test_warning_findings_are_not_repaired(tmp_path):
    board = _board(tmp_path, ENTRY)
    result = MaskCopperRepairer(board).repair(
        [_finding("trk-a", severity=Severity.WARNING)], max_displacement=1.0
    )
    assert result.total_violations == 0 and result.repaired == 0


def test_move_that_crowds_other_net_copper_is_declined(tmp_path):
    # The straight path to the centre passes 0.06 mm (centre to centre) from this
    # N2 track near x = 99; the entry as authored keeps 0.2 mm.
    board = _board(tmp_path, ENTRY + "\n" + _seg("trk-n2", (96, 99.9), (99, 99.9), net=2))
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-a")], max_displacement=1.0, clearance_mm=0.1)
    assert result.repaired == 0
    assert result.skipped("would_violate_clearance") == 1
    assert not repairer.modified


# ---------------------------------------------------------------------------
# fix-drc integration: --only mask-copper, re-check, revert
# ---------------------------------------------------------------------------


def _report(*violations: DRCViolation) -> DRCReport:
    return DRCReport(
        source_file="b.kicad_pcb",
        created_at=None,
        pcb_name="b.kicad_pcb",
        violations=list(violations),
    )


def _run(monkeypatch, board: Path, reports: list[DRCReport], extra=(), *, connectivity=False):
    """Run fix-drc --only mask-copper; ``reports`` feed successive kct-check runs."""
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
            *([] if connectivity else ["--no-connectivity-check"]),
            "--max-displacement",
            "1.0",
            *extra,
        ]
    )


def test_fix_drc_only_mask_copper_repairs_and_verifies(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, ENTRY)
    before = _report(_finding("trk-a"))
    clean = _report()
    # calls: detection, mask verification, final re-check
    code = _run(monkeypatch, board, [before, clean, clean])
    assert code == 0
    assert _end(board, "trk-a")[1] == (100.0, 100.0)
    assert "MASK-TO-COPPER: 1/1" in capsys.readouterr().out


def test_fix_drc_reverts_a_move_that_does_not_clear_its_finding(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, ENTRY)
    before = _report(_finding("trk-a"))
    still = _report(_finding("trk-a"))  # same finding after the move
    code = _run(monkeypatch, board, [before, still, still])
    assert _end(board, "trk-a")[1] == (100.7, 100.3)  # reverted
    assert code != 0
    assert "finding_not_cleared" in capsys.readouterr().out


def test_fix_drc_reverts_a_move_that_creates_a_new_mask_finding(tmp_path, monkeypatch):
    board = _board(tmp_path, ENTRY)
    before = _report(_finding("trk-a"))
    new = DRCViolation(
        type=ViolationType.MASK_TO_COPPER,
        type_str="mask_to_copper",
        severity=Severity.ERROR,
        message=f"Mask-to-copper overlap: error; {OWN_ENTRY}",
        items=[PAD, "some-other-conductor"],
    )
    after = _report(new)  # the old finding is gone, a new one names the pad
    _run(monkeypatch, board, [before, after, after])
    assert _end(board, "trk-a")[1] == (100.7, 100.3)


def test_fix_drc_reverts_every_move_when_the_recheck_cannot_run(tmp_path, monkeypatch):
    board = _board(tmp_path, ENTRY)
    feed = iter([_report(_finding("trk-a"))])

    def fake(*args, **kwargs):
        return next(feed, None)

    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", fake)
    fix_drc_cmd.main(
        [
            str(board),
            "--only",
            "mask-copper",
            "--no-connectivity-check",
            "--max-displacement",
            "1.0",
        ]
    )
    assert _end(board, "trk-a")[1] == (100.7, 100.3)


def test_fix_drc_dry_run_leaves_the_board_alone(tmp_path, monkeypatch):
    board = _board(tmp_path, ENTRY)
    original = board.read_text()
    _run(monkeypatch, board, [_report(_finding("trk-a"))], extra=("--dry-run",))
    assert board.read_text() == original


# ---------------------------------------------------------------------------
# Issue #9365: net-class clearance gate (post-save) and double-fault revert
# ---------------------------------------------------------------------------


def _clearance_error(
    x: float = 99.0, y: float = 100.3, items: list[str] | None = None
) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.from_string("clearance"),
        type_str="clearance",
        severity=Severity.ERROR,
        message="Clearance violation (0.150mm < 0.200mm)",
        locations=[Location(x_mm=x, y_mm=y, layer="F.Cu")],
        items=items if items is not None else ["Track [N1] on F.Cu", "Track [N2] on F.Cu"],
        nets=["N1", "N2"],
        required_value_mm=0.2,
        actual_value_mm=0.15,
    )


def test_fix_drc_reverts_a_move_that_creates_a_new_clearance_error(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, ENTRY)
    before = _report(_finding("trk-a"))
    after = _report(_clearance_error())  # mask finding cleared, new net-class error beside it
    clean = _report()
    # calls: detection, mask verification, baseline with the move undone, final re-check
    _run(monkeypatch, board, [before, after, clean, after])
    assert _end(board, "trk-a")[1] == (100.7, 100.3)  # reverted
    assert "new_drc_error" in capsys.readouterr().out


def test_fix_drc_keeps_a_move_when_the_error_was_already_there(tmp_path, monkeypatch):
    board = _board(tmp_path, ENTRY)
    before = _report(_finding("trk-a"))
    # The same pre-existing error as the real engine reports it on each side
    # of the move: the entry's descriptor names its geometry (so it changes
    # with the move) and the location is the closest-approach point (which
    # shifts with it).  Feeding one hand-built object to both runs would
    # assume the very identity stability under test (PR #9369 review).
    moved_entry = "Trace@F.Cu:w0.2:95/100.3~100/100"
    undone_entry = "Trace@F.Cu:w0.2:95/100.3~100.7/100.3"
    n2 = "Trace@F.Cu:w0.2:95/100.55~96/100.55"
    after = _report(_clearance_error(x=96.5, y=100.35, items=[moved_entry, n2]))
    baseline = _report(_clearance_error(x=96.675, y=100.425, items=[undone_entry, n2]))
    code = _run(monkeypatch, board, [before, after, baseline, after])
    assert _end(board, "trk-a")[1] == (100.0, 100.0)
    assert code != 0  # the pre-existing error is still reported, not hidden


def test_fix_drc_keeps_a_move_when_the_new_error_is_far_from_it(tmp_path, monkeypatch):
    board = _board(tmp_path, ENTRY)
    before = _report(_finding("trk-a"))
    elsewhere = _report(_clearance_error(x=50.0, y=50.0))
    clean = _report()
    _run(monkeypatch, board, [before, elsewhere, clean, elsewhere])
    assert _end(board, "trk-a")[1] == (100.0, 100.0)


def test_fix_drc_restores_the_snapshot_when_undo_action_fails(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, ENTRY)
    original = board.read_text()
    before = _report(_finding("trk-a"))
    still = _report(_finding("trk-a"))  # forces the revert, whose undo then fails
    monkeypatch.setattr(MaskCopperRepairer, "undo_action", lambda self, action: False)
    _run(monkeypatch, board, [before, still, still])
    assert _end(board, "trk-a")[1] == (100.7, 100.3)  # the move is not left in the file
    assert board.read_text() == original  # restored byte for byte
    assert "could not revert mask recentre" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# PR #9369 review: the baseline comparison against the real rule set
# ---------------------------------------------------------------------------


def _recheck(board: Path) -> DRCReport:
    """``kct check``'s real rule set on the board as saved (move applied)."""
    after = fix_drc_cmd._run_python_drc(board, project_pcb_path=board)
    assert after is not None
    return after


def _flagged(board: Path, after: DRCReport, actions: list) -> set[str]:
    """``_moves_with_new_errors``'s verdict; its own baseline run is real too."""
    return fix_drc_cmd._moves_with_new_errors(
        after=after,
        actions=actions,
        output_path=board,
        pcb_path=board,
        args=argparse.Namespace(mfr="jlcpcb", layers=None),
    )


def test_a_preexisting_error_keeps_the_move_on_the_real_engine(tmp_path):
    # The entry already runs 0.05 mm (floor: 0.127 mm) beside an N2 track on
    # its untouched outside portion: a clearance error the re-centre did not
    # cause.  With the real rule set naming the track by its geometry, the
    # finding's items and location differ between the two runs, so an
    # item-keyed baseline would classify it as new and revert the move.
    board = _board(tmp_path, ENTRY + "\n" + _seg("trk-n2", (95, 100.55), (96, 100.55), net=2))
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-a")], max_displacement=1.0)
    assert result.repaired == 1
    repairer.save(board)
    after = _recheck(board)
    assert any(v.type_str == "clearance_segment_segment" for v in after.violations)
    assert _flagged(board, after, result.actions) == set()


def test_a_move_between_floors_is_flagged_on_the_real_engine(tmp_path):
    # N1 carries an authored 0.25 mm net class; the re-centred line keeps
    # 0.149 mm to the N2 stub -- above the fab floor (0.127 mm, so the
    # repairer's own gate does not decline the move) but below the class,
    # exactly the gap this gate exists to catch.  With the move undone the
    # gap is 0.3 mm, so the baseline run reports no such error.
    project = {
        "meta": {"filename": "b.kicad_pro", "version": 3},
        "board": {"design_settings": {"rules": {"min_clearance": 0.1}}},
        "net_settings": {
            "meta": {"version": 4},
            "classes": [
                {"name": "Default", "clearance": 0.1, "priority": 2147483647},
                {"name": "N1cls", "clearance": 0.25, "priority": 0},
            ],
            "netclass_assignments": None,
            "netclass_patterns": [{"netclass": "N1cls", "pattern": "N1"}],
        },
    }
    board = _board(tmp_path, ENTRY + "\n" + _seg("trk-n2", (96.5, 99.8), (97.5, 99.8), net=2))
    board.with_suffix(".kicad_pro").write_text(json.dumps(project))
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-a")], max_displacement=1.0)
    assert result.repaired == 1
    repairer.save(board)
    after = _recheck(board)
    assert any(v.type_str == "netclass_clearance_copper" and v.is_error for v in after.violations)
    assert _flagged(board, after, result.actions) == {"trk-a"}


# ---------------------------------------------------------------------------
# Issue #9891: pull-in for tapered escapes that stop short of the pad
# ---------------------------------------------------------------------------


def test_pull_in_moves_short_terminus_to_pad_centre(tmp_path):
    """An entry that stopped 0.1 mm outside the pad is pulled to the centre."""
    copper = _seg("trk-p", (100.3, 102.0), (100.3, 100.6))
    board = _board(tmp_path, copper)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-p")], max_displacement=1.0, clearance_mm=0.1)
    assert result.repaired == 1 and not result.unrepaired
    repairer.save(board)
    start, end = _end(board, "trk-p")
    assert start == (100.3, 102.0)
    assert end == (100.0, 100.0)
    assert result.actions[0].action == "pull_in"
    assert result.actions[0].end_tag == "end"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("pad_floor", "footprint_floor", "x", "layer", "net", "accept"),
    [
        (0.30, 0, 99.75, "F.Cu", 2, False),
        (0.20, 0, 99.75, "F.Cu", 2, True),
        (0.30, 0, 99.65, "F.Cu", 2, True),
        (0, 0.30, 99.75, "F.Cu", 2, False),
        (0.20, 0.30, 99.75, "F.Cu", 2, True),
        (0.30, 0, 99.75, "B.Cu", 2, True),
        (0.30, 0, 99.75, "F.Cu", 1, True),
    ],
)
def test_pull_in_uses_the_foreign_pad_clearance(
    tmp_path, dry_run, pad_floor, footprint_floor, x, layer, net, accept
):
    # KiCad measures 0.2359 mm to the near pad after the pull-in. The pad
    # has copper only, so a mask opening search alone cannot find it.
    obstacle = f'''(footprint "test:TP" (layer "{layer}") (at {x} 101.1)
        (clearance {footprint_floor})
        (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "{layer}")
        (clearance {pad_floor}) (net {net} "N{net}") (uuid "pad-tp")))'''
    segment = _seg("trk-p", (100.35, 102), (100.55, 100.55))
    board = _board(tmp_path, segment + obstacle)
    original = board.read_bytes()
    repairer = MaskCopperRepairer(board)
    initial = repairer.doc.to_string()
    result = repairer.repair(
        [_finding("trk-p")], max_displacement=1.0, dry_run=dry_run, clearance_mm=0.127
    )
    assert result.repaired == int(accept)
    assert result.skipped("would_violate_clearance") == int(not accept)
    if dry_run or not accept:
        assert board.read_bytes() == original
        assert repairer.doc.to_string() == initial
    else:
        repairer.save(board)
        assert _end(board, "trk-p")[1] == (100, 100)


@pytest.mark.parametrize("connectivity", [False, True])
def test_native_pull_in_check_preserves_prior_output_on_new_error(
    tmp_path, monkeypatch, capsys, connectivity
):
    from collections import Counter

    board = _board(tmp_path, _seg("trk-p", (100.35, 102), (100.55, 100.55)))
    original = board.read_bytes()
    output = tmp_path / "other.kicad_pcb"
    output.write_bytes(b"previous output")
    native = iter(
        [(Counter(), 0), (Counter({("clearance", "board rule", ("pad", "track")): 1}), 0)]
    )
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", lambda candidate: next(native))
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_pad_groups", lambda candidate: [])
    _run(
        monkeypatch,
        board,
        [_report(_finding("trk-p")), _report()],
        extra=("--output", str(output), "--format", "json"),
        connectivity=connectivity,
    )
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert result["repaired"] == 0
    assert "1 new errors" in result["unrepaired"][-1]["detail"]
    assert board.read_bytes() == original
    assert output.read_bytes() == b"previous output"


@pytest.mark.parametrize(
    ("floor", "ref", "accept"), [(0.30, "TP9", False), (0.20, "TP9", True), (0.30, "TP8", True)]
)
def test_pull_in_checks_a_dragged_neighbor_against_its_project_rules(tmp_path, floor, ref, accept):
    # Only the joined track approaches TP9. Its clearance falls from 0.5655
    # to 0.2384 mm. The primary track stays over a millimetre away.
    pcb_path = _board(
        tmp_path,
        _seg("trk-p", (100.35, 102), (100.55, 100.55))
        + _seg("joined", (102.3, 99.8), (100.55, 100.55))
        + """(footprint "test:Probe" (layer "F.Cu") (at 101.15 99.46)
                          (property "Reference" "TP9" (at 0 2) (layer "F.SilkS"))
                          (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu")
                          (clearance 0.2) (net 2 "N2") (uuid "neighbor")))""",
    )
    project = tmp_path / "source.kicad_pcb"
    project.with_suffix(".kicad_dru").write_text(
        f"""(version 1)
        (rule "probe clearance" (condition "A.Type == 'Track' && B.memberOfFootprint('{ref}')")
          (constraint physical_clearance (min {floor}mm)))"""
    )
    # A later --output pass loads this copper while resolving the input project's rules.
    repairer = MaskCopperRepairer(pcb_path, project_pcb_path=project)
    result = repairer.repair([_finding("trk-p")], max_displacement=1.0, clearance_mm=0.127)
    assert result.repaired == int(accept)
    assert result.skipped("would_violate_clearance") == int(not accept)
    if accept:
        assert result.actions[0].joined == [("joined", "end")]


@pytest.mark.parametrize(
    ("far_x", "repairable"), [(101.999, True), (102.0, True), (102.001, False)]
)
def test_pull_in_includes_exactly_45_degrees(tmp_path, far_x, repairable):
    board = _board(tmp_path, _seg("trk-diagonal", (far_x, 102), (100.55, 100.55)))
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-diagonal")], max_displacement=1.0, clearance_mm=0.1)
    assert result.repaired == int(repairable)
    assert result.skipped("entry_too_shallow") == int(not repairable)
    repairer.save(board)
    assert _end(board, "trk-diagonal")[1] == ((100, 100) if repairable else (100.55, 100.55))


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "fourth_pad", [(105, 102.5), (99.88, 101.1)], ids=["isolated", "joined-by-unsafe-move"]
)
def test_pull_in_keeps_an_attached_arc_on_a_partially_routed_net(tmp_path, dry_run, fourth_pad):
    from kicad_tools.validate.connectivity import ConnectivityValidator

    def pad(ref, x, y):
        return (
            f'(footprint "test:pad" (layer "F.Cu") (at {x} {y}) '
            f'(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")) '
            '(pad "1" smd rect (at 0 0) (size .55 .55) (layers "F.Cu") (net 1 "N1")))'
        )

    copper = (
        _seg("pull", (100.35, 102), (100.55, 100.62), width=0.3)
        + "(arc (start 100.55 100.62) (mid 101.775 100.73) (end 103 100.62) "
        '(width .2) (layer "F.Cu") (net 1))'
        + pad("U2", 103, 100.62)
        + pad("U3", 100.35, 102)
        # In the second case the proposed move joins U4 but splits U2 off.
        # The total number of opens stays at one.
        + pad("U4", *fourth_pad)
    )
    board = _board(tmp_path, copper)
    assert ConnectivityValidator(board)._contact_graph(1).open_count() == 1
    repairer = MaskCopperRepairer(board)
    original = repairer.doc.to_string()
    result = repairer.repair([_finding("pull")], max_displacement=1, dry_run=dry_run)
    assert result.repaired == 0 and not repairer.modified
    assert repairer.doc.to_string() == original
    repairer.save(board)
    assert ConnectivityValidator(board)._contact_graph(1).open_count() == 1


@pytest.mark.parametrize("locked_item", ["primary", "joined"])
@pytest.mark.parametrize(
    "lock,locked", [("locked", True), ("(locked yes)", True), ("(locked no)", False)]
)
def test_pull_in_respects_segment_locks(tmp_path, locked_item, lock, locked):
    endpoint = (100.55, 100.55)
    primary = _seg("primary", (100.3, 102), endpoint)
    joined = _seg("joined", endpoint, (101.5, 101.5))
    if locked_item == "primary":
        primary = primary.replace("(segment ", f"(segment {lock} ")
    else:
        joined = joined.replace("(segment ", f"(segment {lock} ")
    board = _board(tmp_path, primary + joined)
    repairer = MaskCopperRepairer(board)
    original = repairer.doc.to_string()
    result = repairer.repair([_finding("primary")], max_displacement=1)
    assert result.repaired == int(not locked)
    if locked:
        assert repairer.doc.to_string() == original


@pytest.mark.parametrize(
    "endpoint", [(100.55, 100.55), (100.3, 100.3)], ids=["pull-in", "recentre"]
)
@pytest.mark.parametrize("member", ["primary", "joined", "unrelated"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_mask_repair_respects_group_locks(tmp_path, endpoint, member, dry_run):
    primary = _seg("primary", (100.3, 102), endpoint)
    joined = _seg("joined", endpoint, (101.5, 101.5))
    unrelated = _seg("unrelated", (90, 90), (90, 91))
    groups = (
        f'(group "fixture" (uuid inner) (locked no) (members "{member}"))'
        '(group "assembly" (uuid outer) (locked yes) (members inner))'
    )
    board = _board(tmp_path, primary + joined + unrelated + groups)
    repairer = MaskCopperRepairer(board)
    original = repairer.doc.to_string()
    result = repairer.repair([_finding("primary")], max_displacement=1, dry_run=dry_run)
    assert result.repaired == int(member == "unrelated")
    if dry_run or member != "unrelated":
        assert repairer.doc.to_string() == original


def test_pull_in_declined_when_entry_crosses_too_shallow(tmp_path):
    """A pull-in whose entry would cross the pad edge at over 45 degrees cannot
    clear the finding, so it is declined (``entry_too_shallow``)."""
    copper = _seg("trk-s", (98.0, 101.5), (100.5, 100.7))
    board = _board(tmp_path, copper)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-s")], max_displacement=1.0, clearance_mm=0.1)
    assert result.repaired == 0
    assert result.skipped("entry_too_shallow") == 1
    assert _end(board, "trk-s") == [(98.0, 101.5), (100.5, 100.7)]


def test_pull_in_declined_beyond_reach(tmp_path):
    """A terminus 0.7 mm from the pad is mid-route, not an escape end."""
    copper = _seg("trk-f", (100.3, 103.0), (100.3, 101.2))
    board = _board(tmp_path, copper)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_finding("trk-f")], max_displacement=2.0, clearance_mm=0.1)
    assert result.repaired == 0
    assert result.skipped("no_end_inside_pad") == 1


def test_undo_restores_the_moved_end_when_the_other_sits_at_the_target(tmp_path):
    """Regression (issue #9891): when the segment's other end already sits at
    the move target, undo must restore the *moved* end.  The pre-#9891 undo
    matched the first endpoint at the target coordinates and restored the
    wrong one, corrupting the segment instead of reverting the move."""
    from kicad_tools.drc.repair_mask_copper import MaskRepairAction

    copper = _seg("trk-z", (100.0, 100.0), (100.0, 100.0))
    board = _board(tmp_path, copper)
    # A pull-in style action whose target the segment's start already sits on
    # (the board-06 escape chains produce exactly this after a joined move).
    action = MaskRepairAction(
        action="pull_in",
        uuid="trk-z",
        pad_uuid=PAD,
        pad_ref="R1/1",
        net_name="N1",
        layer="F.Cu",
        old_x=101.3,
        old_y=100.3,
        new_x=100.0,
        new_y=100.0,
        displacement_mm=1.044,
        end_tag="end",
    )
    undo = MaskCopperRepairer(board)
    assert undo.undo_action(action)
    undo.save(board)
    assert _end(board, "trk-z") == [(100.0, 100.0), (101.3, 100.3)]


def test_joined_end_is_not_moved_twice_by_a_later_finding(tmp_path):
    """A joined member's near-pad end is often its own pull-in candidate; the
    second finding on it must be declined (``segment_end_already_moved``)
    rather than stacked on the first move, which undo cannot revert."""
    copper = (
        _seg("trk-a", (100.5, 100.6), (100.3, 100.6))
        + "\n"
        + _seg("trk-b", (100.1, 100.6), (100.3, 100.6))
    )
    board = _board(tmp_path, copper)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair(
        [_finding("trk-a"), _finding("trk-b")], max_displacement=1.0, clearance_mm=0.1
    )
    assert result.repaired == 1
    assert result.skipped("segment_end_already_moved") == 1
    repairer.save(board)
    undo = MaskCopperRepairer(board)
    assert undo.undo_action(result.actions[0])
    undo.save(board)
    assert _end(board, "trk-a") == [(100.5, 100.6), (100.3, 100.6)]
    assert _end(board, "trk-b") == [(100.1, 100.6), (100.3, 100.6)]


# ---------------------------------------------------------------------------
# Issue #9896: disjoint same-net pass-through nudges (segment and via)
# ---------------------------------------------------------------------------

PASS_THROUGH = "same-net pass-through"
DISJOINT = "Mask-to-copper disjoint: violation"

# A second same-net pad footprint; the leading "(at x y" of R1's placement is
# patched per test so R2/1 can sit anywhere the case needs.
_BOARD_TWO_PADS = BOARD.replace(
    """  (footprint "test:R1" (layer "F.Cu") (at 100 100)""",
    """  (footprint "test:R1" (layer "F.Cu") (at 100 100)""",
)


def _board2(tmp_path: Path, copper: str, r2_at: str) -> Path:
    extra = (
        f'  (footprint "test:R2" (layer "F.Cu") (at {r2_at})\n'
        '    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS")\n'
        "      (effects (font (size 1 1) (thickness 0.15))))\n"
        '    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")\n'
        "      (effects (font (size 1 1) (thickness 0.15))))\n"
        '    (pad "1" smd rect (at 0 0) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask") '
        '(net 1 "N1") (uuid "pad-r2-1"))\n'
        "  )\n"
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(BOARD % (extra + copper))
    return path


def _via(uuid: str, at, size: float = 0.6, drill: float = 0.3, net: int = 1) -> str:
    return (
        f"  (via (at {at[0]} {at[1]}) (size {size}) (drill {drill}) "
        f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "{uuid}"))'
    )


def _pt_finding(
    conductor: str,
    pad: str = PAD,
    measured: float = 0.02,
    required: float = 0.09,
    location=(100.0, 100.52),
):
    return DRCViolation(
        type=ViolationType.MASK_TO_COPPER,
        type_str="mask_to_copper",
        severity=Severity.ERROR,
        message=f"{DISJOINT}; exposed area 0 mm²; opening R1/1; {PASS_THROUGH}",
        locations=[Location(x_mm=location[0], y_mm=location[1], layer="F.Cu")],
        items=[pad, conductor],
        required_value_mm=required,
        actual_value_mm=measured,
    )


def _saved_copper(path: Path, uuid: str):
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

    doc = parse_file(path)
    for node in doc.find_all("segment"):
        if str(node.find("uuid").get_first_atom()).strip('"') == uuid:
            s = tuple(float(a) for a in node.find("start").get_atoms())
            e = tuple(float(a) for a in node.find("end").get_atoms())
            w = float(node.find("width").get_first_atom())
            return LineString([s, e]).buffer(w / 2, quad_segs=16), (s, e)
    for node in doc.find_all("via"):
        if str(node.find("uuid").get_first_atom()).strip('"') == uuid:
            at = tuple(float(a) for a in node.find("at").get_atoms())
            size = float(node.find("size").get_first_atom())
            return Point(at).buffer(size / 2, quad_segs=32), at
    raise AssertionError(uuid)


def _pad_poly(path: Path, pad_ref: str):
    repairer = MaskCopperRepairer(path)
    entry = next(e for e in repairer._openings().values() if e[4] == pad_ref)
    return entry[0]


# A pass-through segment beside the pad: copper y 100.52..100.72, so the
# measured opening distance is 0.02 mm (the pad's top edge is y = 100.5).
PASS = _seg("trk-p", (95, 100.62), (105, 100.62))


def test_pass_through_segment_moves_away_from_the_opening(tmp_path):
    board = _board(tmp_path, PASS)
    result = MaskCopperRepairer(board).repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 1 and not result.unrepaired
    action = result.actions[0]
    assert action.action == "nudge_clear" and action.uuid == "trk-p"
    repairer = MaskCopperRepairer(board)
    repairer.repair([_pt_finding("trk-p")], max_displacement=1.0)
    repairer.save(board)
    copper, ends = _saved_copper(board, "trk-p")
    deltas = [
        (x - ox, y - oy)
        for (x, y), (ox, oy) in zip(ends, [(95.0, 100.62), (105.0, 100.62)], strict=True)
    ]
    assert deltas[0] == deltas[1]  # the whole segment translates by one delta
    assert deltas[0][1] > 0.05  # away from the pad below
    assert _pad_poly(board, "R1/1").distance(copper) == pytest.approx(0.11, abs=1e-6)


def test_pass_through_segment_drag_joins_and_undoes_exactly(tmp_path):
    board = _board(tmp_path, PASS + "\n" + _seg("trk-q", (105, 100.62), (108, 103)))
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 1
    repairer.save(board)
    assert _end(board, "trk-q")[0] == _end(board, "trk-p")[1]  # the joined end moved with it
    assert _end(board, "trk-q")[1] == (108.0, 103.0)  # the far end stays put
    undo = MaskCopperRepairer(board)
    assert undo.undo_action(result.actions[0])
    undo.save(board)
    assert _end(board, "trk-p") == [(95.0, 100.62), (105.0, 100.62)]
    assert _end(board, "trk-q")[0] == (105.0, 100.62)


def test_pass_through_segment_refused_when_a_via_anchors_its_end(tmp_path):
    copper = PASS + "\n" + _via("v-anchor", (95, 100.62))
    board = _board(tmp_path, copper)
    before = board.read_text()
    result = MaskCopperRepairer(board).repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped("junction_at_moved_end") == 1
    assert not result.unrepaired[0].uuid or board.read_text() == before
    assert board.read_text() == before


def test_pass_through_segment_refused_by_other_net_copper(tmp_path):
    # The nudge moves the track up 0.09 mm; an N2 track 0.05 mm beyond the
    # destination is crowded below the 0.1 mm floor while the authored track
    # keeps 0.13 mm.
    copper = PASS + "\n" + _seg("trk-n2", (99, 100.95), (103, 100.95), net=2)
    board = _board(tmp_path, copper)
    result = MaskCopperRepairer(board).repair(
        [_pt_finding("trk-p")], max_displacement=1.0, clearance_mm=0.1
    )
    assert result.repaired == 0
    assert result.skipped("would_violate_clearance") == 1
    assert not MaskCopperRepairer(board).modified


def test_pass_through_segment_refused_when_push_approaches_another_opening(tmp_path):
    # R2's opening sits 0.11 mm above the authored copper -- clear of the
    # error band -- and 0.02 mm above the nudged copper.
    board = _board2(tmp_path, PASS, "104 101.33")
    result = MaskCopperRepairer(board).repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped("would_approach_other_opening") == 1
    assert "R2/1" in result.unrepaired[0].detail


def test_pass_through_segment_refused_when_an_end_would_leave_its_own_pad(tmp_path):
    # The segment starts inside R2's opening 0.045 mm under its edge; the
    # 0.09 mm nudge would pull it out, turning R2's own-pad entry into a
    # new pass-through finding.
    copper = _seg("trk-p", (95.2, 100.62), (105, 100.62))
    board = _board2(tmp_path, copper, "95.5 100.15")
    result = MaskCopperRepairer(board).repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped("would_leave_own_pad") == 1


def test_pass_through_findings_grouped_into_one_move_per_conductor(tmp_path):
    # R1 and R2 sit side by side; the segment passes 0.02 mm above both
    # openings, so one grouped translation must clear the pair.
    copper = _seg("trk-p", (99, 100.62), (104, 100.62))
    board = _board2(tmp_path, copper, "102.9 100")
    findings = [_pt_finding("trk-p"), _pt_finding("trk-p", pad="pad-r2-1", location=(103, 100.52))]
    repairer = MaskCopperRepairer(board)
    result = repairer.repair(findings, max_displacement=1.0)
    assert result.total_violations == 2
    assert result.repaired == 1 and len(result.actions) == 1
    assert set(result.actions[0].openings) == {"pad-r1-1", "pad-r2-1"}
    repairer.save(board)
    copper_geom, _ends = _saved_copper(board, "trk-p")
    assert _pad_poly(board, "R1/1").distance(copper_geom) == pytest.approx(0.11, abs=1e-6)
    assert _pad_poly(board, "R2/1").distance(copper_geom) == pytest.approx(0.11, abs=1e-6)


def test_pass_through_between_facing_pads_is_infeasible(tmp_path):
    # The bar passes between two facing openings 0.02/0.03 mm from either
    # side: no translation clears both.
    copper = _seg("trk-p", (101.12, 99), (101.12, 101))
    board = _board2(tmp_path, copper, "102.25 100")
    findings = [_pt_finding("trk-p"), _pt_finding("trk-p", pad="pad-r2-1", location=(101.23, 100))]
    result = MaskCopperRepairer(board).repair(findings, max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped("infeasible") == 2  # every member finding carries the reason


# A pass-through via just off the pad's right edge: disc left edge x = 101.05,
# measured opening distance 0.05 mm.
PASS_VIA = _via("via-p", (101.35, 99.6))


@pytest.mark.parametrize(
    ("lock_flag", "groups", "movable"),
    [
        ("locked", "", False),
        ("(locked yes)", "", False),
        ("(locked no)", "", True),
        ("", "", True),
        ("", '(group "g" (uuid "g") (locked yes) (members "TARGET"))', False),
        (
            "(locked no)",
            '(group "child" (uuid "c") (locked no) (members "TARGET"))'
            '(group "parent" (uuid "p") (locked yes) (members "c"))',
            False,
        ),
        ("", '(group "g" (uuid "g") (locked no) (members "TARGET"))', True),
        ("", '(group "g" (uuid "g") (locked yes) (members "other"))', True),
    ],
)
@pytest.mark.parametrize(
    ("conductor", "locked_item", "reason"),
    [
        ("via-p", "via-p", "locked_via"),
        ("via-p", "trk-q", "locked_attached_track"),
        ("trk-p", "trk-p", "locked_segment"),
        ("trk-p", "trk-q", "junction_at_moved_end"),
    ],
)
def test_nudge_respects_authored_copper_locks(
    tmp_path, monkeypatch, capsys, conductor, locked_item, reason, lock_flag, groups, movable
):
    if conductor == "via-p":
        copper = PASS_VIA + "\n" + _seg("trk-q", (101.35, 99.6), (103, 99.6))
        finding = _pt_finding(conductor, measured=0.05, location=(101.05, 99.6))
    else:
        copper = PASS + "\n" + _seg("trk-q", (105, 100.62), (108, 103))
        finding = _pt_finding(conductor)
    uuid = f'(uuid "{locked_item}")'
    copper = copper.replace(uuid, f"{lock_flag} {uuid}")
    board = _board(tmp_path, copper + "\n" + groups.replace("TARGET", locked_item))
    original = board.read_bytes()
    before = [_saved_copper(board, item)[1] for item in (conductor, "trk-q")]
    reports = [_report(finding), _report() if movable else _report(finding)]
    _run(monkeypatch, board, reports, extra=("--format", "json"))
    repair = json.loads(capsys.readouterr().out)["mask_copper"]
    assert repair["repaired"] == int(movable)
    assert [item["reason"] for item in repair["unrepaired"]] == ([] if movable else [reason])
    assert (board.read_bytes() != original) == movable
    after = [_saved_copper(board, item)[1] for item in (conductor, "trk-q")]
    assert [old != new for old, new in zip(before, after, strict=True)] == [movable, movable]


def test_pass_through_via_moves_away_dragging_its_tracks(tmp_path):
    copper = (
        PASS_VIA
        + "\n"
        + _seg("trk-f", (101.35, 99.6), (103, 99.6))
        + "\n"
        + _seg("trk-b", (101.35, 99.6), (102, 99), layer="B.Cu")
    )
    board = _board(tmp_path, copper)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))], max_displacement=1.0
    )
    assert result.repaired == 1 and not result.unrepaired
    action = result.actions[0]
    assert action.uuid == "via-p"
    repairer.save(board)
    disc, at = _saved_copper(board, "via-p")
    assert _pad_poly(board, "R1/1").distance(disc) >= 0.11 - 1e-6
    # Both attached track ends (F.Cu and B.Cu) moved with the via: connected.
    assert _end(board, "trk-f")[0] == at
    assert _end(board, "trk-b")[0] == at
    assert _end(board, "trk-f")[1] == (103.0, 99.6)
    undo = MaskCopperRepairer(board)
    assert undo.undo_action(action)
    undo.save(board)
    assert _saved_copper(board, "via-p")[1] == (101.35, 99.6)
    assert _end(board, "trk-f")[0] == (101.35, 99.6)
    assert _end(board, "trk-b")[0] == (101.35, 99.6)


def test_pass_through_via_refused_by_drill_clearance_at_the_destination(tmp_path):
    from kicad_tools.manufacturers.base import DesignRules

    # First place the foreign via from the push's actual destination: its
    # drill sits 0.4 mm (hole-to-hole) from where the via would land, while
    # its copper keeps clear of the via both before and after the nudge.
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = _board(plain_dir, PASS_VIA)
    probe = MaskCopperRepairer(plain)
    probe_result = probe.repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))], max_displacement=1.0
    )
    assert probe_result.repaired == 1
    dest = probe_result.actions[0].new_x, probe_result.actions[0].new_y

    copper = PASS_VIA + "\n" + _via("via-n2", (dest[0] + 0.7, dest[1]), size=0.4, drill=0.3, net=2)
    board = _board(tmp_path, copper)
    rules = DesignRules(
        min_clearance_mm=0.127,
        min_trace_width_mm=0.127,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.15,
    )
    result = MaskCopperRepairer(board).repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))],
        max_displacement=1.0,
        design_rules=rules,
    )
    assert result.repaired == 0
    assert result.skipped("would_violate_clearance") == 1
    assert "hole-to-hole" in result.unrepaired[0].detail


def test_pass_through_overlap_relation_and_model_mismatch_are_refused(tmp_path):
    from kicad_tools.drc.repair_mask_copper import MaskCopperRepairer as R

    board = _board(tmp_path, PASS)
    # Overlap relation (the marker says pass-through but the copper crosses
    # the pad): left to the overlap workflows.
    overlap = DRCViolation(
        type=ViolationType.MASK_TO_COPPER,
        type_str="mask_to_copper",
        severity=Severity.ERROR,
        message=f"Mask-to-copper overlap: violation; opening R1/1; {PASS_THROUGH}",
        locations=[Location(x_mm=100.0, y_mm=100.3, layer="F.Cu")],
        items=[PAD, "trk-p"],
        required_value_mm=0.09,
    )
    result = R(board).repair([overlap], max_displacement=1.0)
    assert result.skipped("not_own_pad_entry") == 1
    # The measurement disagrees with the opening model by more than the
    # agreement window: the move is not verified against the real opening.
    result = R(board).repair([_pt_finding("trk-p", measured=0.5)], max_displacement=1.0)
    assert result.skipped("opening_model_mismatch") == 1


def test_fix_drc_repairs_and_verifies_a_pass_through_nudge(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, PASS)
    before = _report(_pt_finding("trk-p"))
    clean = _report()
    code = _run(monkeypatch, board, [before, clean, clean])
    assert code == 0
    ends = _end(board, "trk-p")
    assert ends != [(95.0, 100.62), (105.0, 100.62)]  # the nudge was applied and kept
    assert _pad_poly(board, "R1/1").distance(_saved_copper(board, "trk-p")[0]) == pytest.approx(
        0.11, abs=1e-6
    )
    assert "MASK-TO-COPPER: 1/1" in capsys.readouterr().out


def test_fix_drc_reverts_a_pass_through_nudge_that_does_not_clear(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, PASS)
    before = _report(_pt_finding("trk-p"))
    still = _report(_pt_finding("trk-p"))
    code = _run(monkeypatch, board, [before, still, still])
    assert code != 0
    assert _end(board, "trk-p") == [(95.0, 100.62), (105.0, 100.62)]  # reverted
    assert "finding_not_cleared" in capsys.readouterr().out


# Each case runs the pinned KiCad DRC three times (before, --verify inside
# fix_drc_cmd, after): #9943 measured 37.8 s and 44.8 s for the two slowest
# cases serially on the pinned image, close to CI's 60 s default under a
# contended runner — so these take a 180 s cap like the other native-DRC
# tests (see tests/test_route_unverified_verdict_6273.py).
@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "via_y, opens, refusal",
    [
        (100.204, 1, "pad_connection_lost"),
        (100.202, 1, "native opens"),
        (100.601, 1, None),
        (100.0, 2, None),
    ],
)
def test_mask_nudge_preserves_existing_pad_island_on_incomplete_net(
    tmp_path, capsys, via_y, opens, refusal
):
    from kicad_tools.cli.kicad_toolchain import evidence_toolchain
    from kicad_tools.drc.geometric import run_geometric_drc

    # R1 is already disconnected. The via joins R4 to the front track at
    # y=100.601. At y=100.202 KiCad still connects a 1 µm annulus contact.
    fixture = Path(__file__).parent / "fixtures" / "mask_pass_through_via_contact.kicad_pcb"
    board = tmp_path / fixture.name
    # Move the via and the end of its back-layer trace together.
    board.write_text(fixture.read_text().replace("102.5 100.202", f"102.5 {via_y}"))
    original = board.read_bytes()
    with evidence_toolchain(mounts=[tmp_path]):
        before = run_geometric_drc(board, refill_zones=False)
        assert before.ran and before.unconnected_count == opens
        fix_drc_cmd.main(
            [
                str(board),
                "--only",
                "mask-copper",
                "--mfr",
                "jlcpcb-tier1",
                "--max-displacement",
                "2",
                "--verify",
                "--format",
                "json",
            ]
        )
        after = run_geometric_drc(board, refill_zones=False)
    assert after.ran and after.unconnected_count == opens
    result = json.loads(capsys.readouterr().out)
    assert result["mask_copper"]["violations"] == 1
    assert result["total_repaired"] == int(refusal is None)
    assert (board.read_bytes() != original) == (refusal is None)
    if refusal:
        rejected = result["mask_copper"]["unrepaired"][0]
        assert refusal in (rejected.get("detail") or rejected["reason"])


def test_mask_nudges_cannot_trade_pad_connections_on_the_same_partial_net(tmp_path, capsys):
    from kicad_tools.cli.kicad_toolchain import evidence_toolchain
    from kicad_tools.drc.geometric import run_geometric_drc

    fixture = Path(__file__).parent / "fixtures" / "mask_pass_through_compensating_opens.kicad_pcb"
    board = tmp_path / fixture.name
    original = fixture.read_bytes()
    board.write_bytes(original)
    # Losing R4's via contact and gaining R14's both yield four native opens.
    # One-count-per-net is constant even though R2, R3 and R4 are split.
    with evidence_toolchain(mounts=[tmp_path]):
        assert run_geometric_drc(board, refill_zones=False).unconnected_count == 4
        fix_drc_cmd.main(
            [
                str(board),
                "--only",
                "mask-copper",
                "--mfr",
                "jlcpcb-tier1",
                "--max-displacement",
                "2",
                "--verify",
                "--format",
                "json",
            ]
        )
    result = json.loads(capsys.readouterr().out)
    assert result["mask_copper"]["repaired"] < 2
    assert any(
        entry["reason"] == "pad_connection_lost" for entry in result["mask_copper"]["unrepaired"]
    )
    trace = "bb000000-0000-4000-8000-000000000080"
    assert _end(board, trace) == _end(fixture, trace)


@pytest.mark.parametrize("fail_on_read", [1, 2])
def test_unavailable_native_pad_groups_preserve_prior_output(
    tmp_path, monkeypatch, capsys, fail_on_read
):
    from collections import Counter

    board = _board(tmp_path, PASS)
    original = board.read_bytes()
    output = tmp_path / "previous.kicad_pcb"
    output.write_bytes(b"existing output")
    calls = 0

    def read_groups(candidate):
        nonlocal calls
        calls += 1
        if calls == fail_on_read:
            raise RuntimeError("native pad groups unavailable")
        return [frozenset({"left", "right"})]

    monkeypatch.setattr(fix_drc_cmd, "_mask_native_pad_groups", read_groups)
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", lambda candidate: (Counter(), 1))
    _run(
        monkeypatch,
        board,
        [_report(_pt_finding("trk-p")), _report(), _report()],
        extra=("--output", str(output), "--format", "json"),
        connectivity=True,
    )
    repair = json.loads(capsys.readouterr().out)["mask_copper"]
    assert calls == fail_on_read and repair["repaired"] == 0
    assert repair["unrepaired"][-1]["reason"] == "recheck_unavailable"
    assert "native pad groups unavailable" in repair["unrepaired"][-1]["detail"]
    assert board.read_bytes() == original
    assert output.read_bytes() == b"existing output"


@pytest.mark.parametrize("outcome", ["saved", "refill_failed", "saved_error", "mask_reappeared"])
def test_mask_refill_publishes_only_verified_saved_copper(tmp_path, monkeypatch, capsys, outcome):
    from collections import Counter

    from kicad_tools.cli import runner

    zone = '(zone (net 2) (net_name "N2") (layer "F.Cu") (uuid "zone-a") (priority 2))'
    board = _board(tmp_path, PASS + zone)
    board.with_suffix(".kicad_dru").write_text("(version 1)")
    original = board.read_bytes()
    output = tmp_path / "other.kicad_pcb"
    if outcome != "saved":
        output.write_bytes(b"previous output")
    original_output = output.read_bytes() if output.exists() else None

    def native_state(candidate):
        assert candidate != board
        assert candidate.name == board.name
        assert candidate.with_suffix(".kicad_dru").read_text() == "(version 1)"
        saved = "priority 3" in candidate.read_text()
        return (Counter({"clearance": 1}) if saved and outcome == "saved_error" else Counter(), 0)

    def refill(candidate, *, require_save):
        assert require_save
        assert candidate != board
        assert board.read_bytes() == original
        assert (output.read_bytes() if output.exists() else None) == original_output
        candidate.write_text(candidate.read_text().replace("priority 2", "priority 3"))
        return runner.KiCadCLIResult(success=outcome != "refill_failed", stderr="refill refused")

    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", native_state)
    monkeypatch.setattr(runner, "run_refill_zones", refill)
    finding = _report(_pt_finding("trk-p"))
    final = finding if outcome == "mask_reappeared" else _report()
    _run(
        monkeypatch,
        board,
        [finding, _report(), final],
        extra=("--output", str(output), "--format", "json"),
    )
    result = json.loads(capsys.readouterr().out)
    assert board.read_bytes() == original
    assert result["total_repaired"] == int(outcome == "saved")
    if outcome == "saved":
        assert "priority 3" in output.read_text()
        assert _end(output, "trk-p") != _end(board, "trk-p")
    else:
        assert output.read_bytes() == original_output
        assert result["mask_copper"]["unrepaired"][-1]["reason"] == "recheck_unavailable"


def test_moves_with_new_errors_attributes_a_via_centre_location(tmp_path, monkeypatch):
    # A dragged track's segment-to-via error reports at the via centre, one
    # measured gap inside the via's own copper: the attribution radius must
    # count that gap or the move that caused it is kept (#9896).
    board = _board(tmp_path, PASS)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 1
    repairer.save(board)

    error = DRCViolation(
        type=ViolationType.from_string("netclass_clearance_copper"),
        type_str="netclass_clearance_copper",
        severity=Severity.ERROR,
        message="Segment to via clearance 0.1090mm < netclass 'Digital' minimum 0.1500mm",
        locations=[Location(x_mm=100.0, y_mm=101.21, layer="F.Cu")],
        items=["Trace@F.Cu:w0.2:95/100.71~105/100.71", "Via@100/101.21:F.Cu-B.Cu:d0.3/s0.6"],
        nets=["N1", "N2"],
        required_value_mm=0.15,
        actual_value_mm=0.109,
    )
    after = _report(error)
    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", lambda *a, **k: _report())
    flagged = fix_drc_cmd._moves_with_new_errors(
        after=after,
        actions=result.actions,
        output_path=board,
        pcb_path=board,
        args=argparse.Namespace(mfr="jlcpcb", layers=None),
    )
    # The nudged copper sits 0.40 mm from the via centre -- outside the old
    # 0.35 mm reach, inside reach + the measured 0.109 mm gap.
    assert flagged == {"trk-p"}


# ---------------------------------------------------------------------------
# Issue #9958: two pass-through nudges that drag a shared track endpoint
# ---------------------------------------------------------------------------

# trk-a runs right as a pass-through 0.02 mm above the opening; trk-b shares
# its left endpoint and runs down past the opening's left edge, also 0.02 mm
# clear.  trk-a's upward nudge drags trk-b's top end along trk-b's own line,
# which leaves trk-b's body 0.02 mm from the opening on the dragged geometry,
# so trk-b stays a live candidate after the first move -- the stacked second
# edit the issue reports.
OVERLAP_PAIR = (
    _seg("trk-a", (104, 100.62), (98.88, 100.62))
    + "\n"
    + _seg("trk-b", (98.88, 100.62), (98.88, 98.5))
)


def _overlap_applied(board: Path) -> list:
    """Apply the overlapping pair through the production solver and save."""
    repairer = MaskCopperRepairer(board)
    first = repairer.repair([_pt_finding("trk-a")], max_displacement=1.0)
    assert first.repaired == 1
    repairer.save(board)
    # A fresh repair call has no memory of the first action, so trk-b's
    # finding names the dragged geometry and is nudged on top of it.
    second_repairer = MaskCopperRepairer(board)
    second = second_repairer.repair([_pt_finding("trk-b")], max_displacement=1.0)
    assert second.repaired == 1
    second_repairer.save(board)
    return [*first.actions, *second.actions]


def test_overlapping_pass_through_pair_defers_the_second_nudge(tmp_path):
    """A pass-through conductor an earlier action dragged must be deferred
    (``segment_end_already_moved``), not nudged again on the dragged
    geometry: the stacked edit cannot be undone in application order, which
    aborts the whole batch (issue #9958)."""
    board = _board(tmp_path, OVERLAP_PAIR + "\n" + _seg("trk-c", (100, 99.38), (102.5, 99.38)))
    findings = [_pt_finding("trk-a"), _pt_finding("trk-b"), _pt_finding("trk-c")]
    repairer = MaskCopperRepairer(board)
    result = repairer.repair(findings, max_displacement=1.0)
    assert result.repaired == 2  # the independent move stays eligible
    assert [a.uuid for a in result.actions] == ["trk-a", "trk-c"]
    assert result.skipped("segment_end_already_moved") == 1
    repairer.save(board)
    undo = MaskCopperRepairer(board)
    for action in result.actions:
        assert undo.undo_action(action)
    undo.save(board)
    assert _end(board, "trk-a") == [(104.0, 100.62), (98.88, 100.62)]
    assert _end(board, "trk-b") == [(98.88, 100.62), (98.88, 98.5)]
    assert _end(board, "trk-c") == [(100.0, 99.38), (102.5, 99.38)]


def test_undo_still_fails_on_a_genuine_endpoint_mismatch(tmp_path):
    """Deferral must not loosen the undo guard: a point that no longer sits
    at the action's recorded post-move position still fails the undo, and
    the caller falls back to the snapshot (issue #9958 premise)."""
    board = _board(tmp_path, OVERLAP_PAIR)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_pt_finding("trk-a")], max_displacement=1.0)
    assert result.repaired == 1
    repairer.save(board)
    # Shift trk-b's dragged endpoint 0.05 mm past its recorded post-move
    # position, as a later edit the undo guard cannot account for would.
    corrupt = MaskCopperRepairer(board)
    start = corrupt._segment_by_uuid("trk-b").find("start")
    start.set_value(0, round(float(start.get_atoms()[0]) + 0.05, 4))
    start.set_value(1, round(float(start.get_atoms()[1]) + 0.05, 4))
    corrupt.save(board)
    assert not MaskCopperRepairer(board).undo_action(result.actions[0])


def test_verify_undoes_overlapping_moves_in_reverse_order(tmp_path, monkeypatch):
    """When an overlapping pair does reach the verifier, it rolls back only
    if the undos run last-applied-first: undoing in application order hits
    the shared endpoint at the second action's position and fails (issue
    #9958)."""
    from kicad_tools.drc.repair_mask_copper import MaskRepairResult

    board = _board(tmp_path, OVERLAP_PAIR)
    actions = _overlap_applied(board)
    result = MaskRepairResult(actions=actions, repaired=2)
    before = _report(_pt_finding("trk-a"), _pt_finding("trk-b"))
    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", lambda *a, **k: None)
    fix_drc_cmd._verify_mask_actions(
        result,
        before,
        argparse.Namespace(mfr="jlcpcb", layers=None),
        board,
        board,
        {},
    )
    assert result.actions == [] and result.repaired == 0
    assert {(u.uuid, u.reason) for u in result.unrepaired} == {
        ("trk-a", "recheck_unavailable"),
        ("trk-b", "recheck_unavailable"),
    }
    assert _end(board, "trk-a") == [(104.0, 100.62), (98.88, 100.62)]
    assert _end(board, "trk-b") == [(98.88, 100.62), (98.88, 98.5)]


def test_baseline_reconstruction_undoes_overlapping_moves_in_reverse(tmp_path, monkeypatch):
    """The pre-existing-error baseline is only trustworthy when the
    reconstruction undo succeeds; with the overlapping pair applied, that
    requires reverse application order (issue #9958)."""
    board = _board(tmp_path, OVERLAP_PAIR)
    actions = _overlap_applied(board)
    # A pre-existing clearance error beside the shared endpoint: it matches
    # its baseline counterpart, so no move is flagged -- but only if the
    # baseline run happens, which needs both undos to succeed.
    error = _clearance_error(x=98.9, y=100.6)
    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", lambda *a, **k: _report(error))
    flagged = fix_drc_cmd._moves_with_new_errors(
        after=_report(error),
        actions=actions,
        output_path=board,
        pcb_path=board,
        args=argparse.Namespace(mfr="jlcpcb", layers=None),
    )
    assert flagged == set()


def _own_finding(conductor: str, pad_uuid: str, pad_ref: str) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.MASK_TO_COPPER,
        type_str="mask_to_copper",
        severity=Severity.ERROR,
        message=f"Mask-to-copper overlap: error; opening {pad_ref}; {OWN_ENTRY}",
        locations=[Location(x_mm=100.0, y_mm=100.3, layer="F.Cu")],
        items=[pad_uuid, conductor],
    )


def test_failed_mask_batch_reports_each_action_once(tmp_path, monkeypatch, capsys):
    """When the selective rollback fails partway, the fail-closed fallback
    must not re-report actions the rollback already disposed of: each action
    uuid appears under exactly one reason in the JSON (issue #9958)."""
    # Two independent own-pad entries, one per pad; both fail verification
    # (neither finding cleared) and the undo of the second-applied move
    # fails, so the batch is discarded.
    board = _board2(tmp_path, ENTRY + "\n" + _seg("trk-2", (106, 100.3), (103.7, 100.3)), "103 100")
    f1 = _own_finding("trk-a", PAD, "R1/1")
    f2 = _own_finding("trk-2", "pad-r2-1", "R2/1")
    still = _report(f1, f2)
    real_undo = MaskCopperRepairer.undo_action

    def selective_undo(self, action):
        return False if action.uuid == "trk-2" else real_undo(self, action)

    monkeypatch.setattr(MaskCopperRepairer, "undo_action", selective_undo)
    code = _run(monkeypatch, board, [still, still, still], extra=("--format", "json"))
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    records = payload["mask_copper"]["unrepaired"]
    uuids = [r["uuid"] for r in records]
    assert len(uuids) == len(set(uuids))
    assert set(uuids) >= {"trk-a", "trk-2"}
    assert board.read_text()  # the fail-closed path kept the file intact
