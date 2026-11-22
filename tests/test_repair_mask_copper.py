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


def _run(monkeypatch, board: Path, reports: list[DRCReport], extra=()):
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
            "--no-connectivity-check",
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
