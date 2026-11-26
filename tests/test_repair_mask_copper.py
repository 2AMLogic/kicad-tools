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
import math
from inspect import signature
from pathlib import Path
from unittest.mock import patch

import pytest
from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

from kicad_tools.cli import fix_drc_cmd
from kicad_tools.drc.repair_mask_copper import MaskCopperRepairer, MaskRepairResult
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.router.quantize import _is_quantum_aligned
from kicad_tools.sexp import parse_file
from kicad_tools.validate.mask_copper import MaskCopperAssessment

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
    tmp_path.mkdir(parents=True, exist_ok=True)
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
        mask_copper_assessments=[MaskCopperAssessment(coverage="complete")],
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


def test_verified_mask_move_keeps_collateral_traces_aligned(tmp_path, monkeypatch):
    from collections import Counter

    from kicad_tools.router.quantize import is_45_aligned

    # The finding names only trk-a. Its horizontal approach and the vertical
    # neighbor become skewed when their common endpoint moves to the pad.
    # An unrelated authored skew must remain exactly as drawn.
    other = _seg("other", (110, 110), (113, 111), net=2)
    joined = _seg("joined", (100.7, 100.3), (100.7, 105), width=0.3)
    board = _board(tmp_path, "\n".join([ENTRY, joined, other]))
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", lambda path: (Counter(), 0))
    _run(monkeypatch, board, [_report(_finding("trk-a")), _report()])
    tracks = MaskCopperRepairer(board)
    off_angle = set()
    for uid in tracks._segment_nodes():
        start, end = _end(board, uid)
        if not is_45_aligned(end[0] - start[0], end[1] - start[1]):
            off_angle.add(uid)
    assert off_angle == {"other"}
    assert _end(board, "other") == [(110, 110), (113, 111)]
    assert _end(board, "trk-a")[1] == _end(board, "joined")[0] == (100, 100)
    legs = {
        (tuple(_end(board, uid)), node.get("width").get_float())
        for uid, node in tracks._segment_nodes().items()
        if uid not in {"other", "joined", "trk-a"}
    }
    assert legs == {
        (((95, 100.3), _end(board, "trk-a")[0]), 0.2),
        ((_end(board, "joined")[1], (100.7, 105)), 0.3),
    }


def test_finding_on_new_leg_rolls_back_whole_junction(tmp_path, monkeypatch, capsys):
    from collections import Counter

    board = _board(tmp_path, ENTRY + "\n" + _seg("joined", (100.7, 100.3), (100.7, 105)))
    original = board.read_bytes()
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", lambda path: (Counter(), 0))
    found_leg = []

    def check(path, **kwargs):
        ids = MaskCopperRepairer(path)._segment_nodes().keys() - {"trk-a", "joined"}
        if not ids:
            return _report(_finding("trk-a"))
        assert len(ids) == 2
        found_leg.extend(ids)
        obstructed = _finding(sorted(ids)[0])
        obstructed.items[0] = "another-opening"
        return _report(obstructed)

    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", check)
    code = fix_drc_cmd.main(
        [str(board), "--only", "mask-copper", "--max-displacement", "1", "--no-connectivity-check"]
    )
    assert code != 0 and found_leg
    assert "new_mask_finding" in capsys.readouterr().out
    assert board.read_bytes() == original


def test_mask_move_refuses_both_elbows_when_a_bonded_endpoint_would_separate(
    tmp_path, monkeypatch, capsys
):
    # The tap already enters R1 by a second path. Both possible elbows take
    # the approach away from its original contact at the tip of the tap.
    tap = _seg("tap", (101.8, 100.49), (100.68, 100.49))
    board = _board(tmp_path, ENTRY + "\n" + tap)
    original = board.read_bytes()
    assert _saved_copper(board, "trk-a")[0].intersects(_saved_copper(board, "tap")[0])
    _run(monkeypatch, board, [_report(_finding("trk-a"))], extra=("--format", "json"))
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert result["repaired"] == 0 and board.read_bytes() == original
    assert [(u["uuid"], u["reason"]) for u in result["unrepaired"]] == [
        ("trk-a", "would_bend_trace")
    ]
    assert "neither elbow" in result["unrepaired"][0]["detail"]


def test_bend_undo_restores_group_members_and_each_authored_endpoint(tmp_path):
    copper = _seg("trk-a", (95, 100.30004), (100.70003, 100.30004))
    joined = _seg("joined", (100.7001, 100.30009), (100.7001, 105))
    group = '(group "escapes" (uuid "group-a") (members "joined"))'
    board = _board(tmp_path, "\n".join([copper, joined, group]))
    old = {uid: _end(board, uid) for uid in ("trk-a", "joined")}
    proposal = MaskCopperRepairer(board)
    result = proposal.repair([_finding("trk-a")], max_displacement=1.0)
    proposal.align_moved_tracks(MaskCopperRepairer(board), result)
    action = result.actions[0]
    assert len(action.bends) == 2
    added = next(bend.added_uuid for bend in action.bends if bend.uuid == "joined")
    assert proposal.doc.get("group").get("members").get_atoms() == ["joined", added]
    proposal.save()
    undo = MaskCopperRepairer(board)
    assert undo.undo_action(action)
    undo.save()
    assert set(undo._segment_nodes()) == old.keys()
    assert {uid: _end(board, uid) for uid in old} == old
    assert undo.doc.get("group").get("members").get_atoms() == ["joined"]


def test_bend_refuses_a_collateral_track_in_a_generator_subgroup(tmp_path, monkeypatch, capsys):
    joined = _seg("joined", (100.7, 100.3), (100.7, 105))
    group = '(group "escapes" (uuid subgroup) (members joined))'
    generator = "(generated (uuid tuning) (members subgroup))"
    board = _board(tmp_path, "\n".join([ENTRY, joined, generator, group]))
    original = board.read_bytes()
    _run(monkeypatch, board, [_report(_finding("trk-a")), _report()], extra=("--format", "json"))
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert result["repaired"] == 0
    assert board.read_bytes() == original
    assert "cannot split locked or generated track joined" in result["unrepaired"][0]["detail"]


@pytest.mark.parametrize("identity", ["uuid", "tstamp", "anonymous"])
def test_off_angle_track_introduced_during_refill_is_never_published(
    tmp_path, monkeypatch, capsys, identity
):
    from collections import Counter

    from kicad_tools.cli import runner

    zone = '(zone (net 2) (net_name "N2") (layer "F.Cu") (uuid "zone-a"))'
    board = _board(tmp_path, PASS + zone)
    output = tmp_path / "prior.kicad_pcb"
    output.write_bytes(b"previous verified board")
    before = board.read_bytes()

    def refill(candidate, *, require_save):
        edited = MaskCopperRepairer(candidate)
        extra = _seg("refill-skew", (110, 110), (112, 111), net=2)
        if identity == "anonymous":
            extra = extra.replace('(uuid "refill-skew")', "")
        elif identity == "tstamp":
            extra = extra.replace("(uuid ", "(tstamp ")
        from kicad_tools.sexp import parse_string

        edited.doc.append(parse_string(extra))
        edited.save()
        return runner.KiCadCLIResult(success=True)

    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", lambda path: (Counter(), 0))
    monkeypatch.setattr(runner, "run_refill_zones", refill)
    _run(monkeypatch, board, [_report(_pt_finding("trk-p")), _report()], extra=("-o", str(output)))
    out = capsys.readouterr()
    assert "would_bend_trace" in out.out
    assert ("110 110" if identity == "anonymous" else "refill-skew") in out.err
    assert output.read_bytes() == b"previous verified board" and board.read_bytes() == before


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


def _pour_slot_warning(x: float = 99.0, y: float = 100.3) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.from_string("pour_slot"),
        type_str="pour_slot",
        severity=Severity.WARNING,
        message="Pour slot beside the move",
        locations=[Location(x_mm=x, y_mm=y, layer="F.Cu")],
        items=["Trace@F.Cu:w0.2:95/100.3~100/100"],
        nets=["N1", "N2"],
    )


@pytest.mark.parametrize(
    ("manufacturer", "coverage", "target", "expected_code"),
    [
        ("pcbway", [], True, 4),
        ("pcbway", ["complete"], True, 4),
        ("jlcpcb", [], False, 4),
        ("jlcpcb", ["complete", "incomplete"], False, 4),
        ("jlcpcb", ["not_run"], False, 4),
        ("jlcpcb", ["complete"], False, 0),
    ],
)
def test_guard_requires_mask_coverage(tmp_path, manufacturer, coverage, target, expected_code):
    board = _board(tmp_path, ENTRY)
    original = board.read_bytes()
    finding = _finding("trk-a")
    report = tmp_path / "check.json"
    report.write_text(
        json.dumps(
            {
                "manufacturer": "jlcpcb",
                "violations": [
                    {
                        "rule_id": finding.type_str,
                        "severity": "error",
                        "message": finding.message,
                        "items": finding.items,
                        "location": [100.0, 100.3],
                        "layer": "F.Cu",
                    }
                ]
                if target
                else [],
                "mask_copper_assessments": [
                    MaskCopperAssessment(coverage=status).to_dict() for status in coverage
                ],
            }
        )
    )
    code = fix_drc_cmd.main(
        [
            str(board),
            "--only",
            "mask-copper",
            "--no-new-warnings",
            "--transactional",
            "--mfr",
            manufacturer,
            "--max-displacement",
            "1.0",
            "--drc-report",
            str(report),
        ]
    )
    assert code == expected_code
    assert board.read_bytes() == original


def test_live_report_preserves_mask_coverage():
    from kicad_tools.drc.compat import drc_results_to_report
    from kicad_tools.validate.violations import DRCResults

    assessment = MaskCopperAssessment(coverage="complete")
    results = DRCResults(mask_copper_assessments=[assessment])
    assert drc_results_to_report(results).mask_copper_assessments == [assessment]


def test_a_new_warning_reverts_the_move_only_under_no_new_warnings(tmp_path, monkeypatch, capsys):
    """Issue #9424: a mask repair must not trade an error for a warning on request."""
    before = _report(_finding("trk-a"))
    after = _report(_pour_slot_warning())
    clean = _report()
    # calls: detection, mask verification, baseline with the move undone, final re-check
    guarded = _board(tmp_path / "guarded", ENTRY)
    _run(monkeypatch, guarded, [before, after, clean, after], extra=["--no-new-warnings"])
    assert _end(guarded, "trk-a")[1] == (100.7, 100.3)  # reverted
    assert "new_drc_error" in capsys.readouterr().out

    default = _board(tmp_path / "default", ENTRY)
    _run(monkeypatch, default, [before, after, after])
    assert _end(default, "trk-a")[1] == (100.0, 100.0)  # a warning never vetoed a move


def test_a_preexisting_warning_keeps_the_move_under_no_new_warnings(tmp_path, monkeypatch):
    board = _board(tmp_path, ENTRY)
    # The live detection report is the pre-edit baseline for the guarded pass.
    before = _report(_finding("trk-a"), _pour_slot_warning(x=99.1, y=100.35))
    after = _report(_pour_slot_warning(x=99.0, y=100.3))
    _run(monkeypatch, board, [before, after, after], extra=["--no-new-warnings"])
    assert _end(board, "trk-a")[1] == (100.0, 100.0)


def test_a_new_mask_warning_on_the_moved_conductor_reverts_the_move(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, ENTRY)
    before = _report(_finding("trk-a"))
    after = _report(_finding("trk-a", severity=Severity.WARNING))  # same pair, now a warning
    new_pair = _report(
        DRCViolation(
            type=ViolationType.MASK_TO_COPPER,
            type_str="mask_to_copper",
            severity=Severity.WARNING,
            message="Mask-to-copper disjoint: warning",
            locations=[Location(x_mm=100.0, y_mm=100.3, layer="F.Cu")],
            items=["other-pad", "trk-a"],
        )
    )
    _run(monkeypatch, board, [before, new_pair, new_pair], extra=["--no-new-warnings"])
    assert _end(board, "trk-a")[1] == (100.7, 100.3)  # reverted
    assert "new_mask_finding" in capsys.readouterr().out

    # An error that only drops to a warning on the same pair is still progress.
    again = _board(tmp_path / "again", ENTRY)
    _run(monkeypatch, again, [before, after, after], extra=["--no-new-warnings"])
    assert _end(again, "trk-a")[1] == (100.0, 100.0)


@pytest.mark.parametrize(
    ("before_level", "after_level", "kept"),
    [(Severity.WARNING, Severity.ERROR, False), (Severity.ERROR, Severity.WARNING, True)],
)
def test_guard_preserves_non_mask_severity(tmp_path, monkeypatch, before_level, after_level, kept):
    from dataclasses import replace

    board = _board(tmp_path, ENTRY)
    before_other = replace(
        _pour_slot_warning(),
        type=ViolationType.from_string("physical_copper_gap"),
        type_str="physical_copper_gap",
        severity=before_level,
    )
    after = _report(replace(before_other, severity=after_level))
    baseline = _report(_finding("trk-a"), before_other)
    _run(monkeypatch, board, [baseline, after, baseline, after], extra=["--no-new-warnings"])
    assert _end(board, "trk-a")[1] == ((100, 100) if kept else (100.7, 100.3))


@pytest.mark.parametrize("refill_only", [False, True])
@pytest.mark.parametrize("finding", ["unattributed_mask", "unattributed_non_mask", "promoted_mask"])
def test_guard_checks_unattributed_and_post_refill_findings(
    tmp_path, monkeypatch, refill_only, finding
):
    from collections import Counter
    from dataclasses import replace

    from kicad_tools.cli import runner

    zone = '(zone (net 2) (net_name "N2") (layer "F.Cu") (uuid "zone-a"))'
    board = _board(tmp_path, ENTRY + zone)
    original = board.read_bytes()
    target = _finding("trk-a")
    warning = replace(_finding("trk-a"), items=["other-pad", "trk-a"], severity=Severity.WARNING)
    before = _report(target)
    if finding == "promoted_mask":
        before.violations.append(replace(warning, severity=Severity.INFO))
    elif finding == "unattributed_mask":
        warning = replace(warning, items=["unrelated-opening", "zone-uuid"])
    else:
        warning = _pour_slot_warning(x=180, y=190)
    refilled = False

    def refill(path, *, require_save):
        nonlocal refilled
        refilled = True
        return runner.KiCadCLIResult(success=True)

    def assess(path, **kwargs):
        if _end(path, "trk-a")[1] != (100.0, 100.0):
            return before
        return _report(warning) if refilled or not refill_only else _report()

    monkeypatch.setattr(runner, "run_refill_zones", refill)
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", lambda path: (Counter(), 0))
    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", assess)
    fix_drc_cmd.main(
        [
            str(board),
            "--only",
            "mask-copper",
            "--no-new-warnings",
            "--no-connectivity-check",
            "--max-displacement",
            "1.0",
        ]
    )
    assert board.read_bytes() == original


def test_guard_compares_stock_warning_band_when_elbow_margin_needs_wider_measurements(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from kicad_tools.cli import check_cmd
    from kicad_tools.drc import compat

    board = _board(tmp_path, ENTRY)
    bands = []
    gaps = Mock(required={("pad", "track"): 0.30})

    def assess(rule_set):
        band = rule_set.checker.mask_copper_request.policy.warning_clearance_mm
        bands.append(band)
        return SimpleNamespace(violations=[], mask_copper_assessments=[band])

    def report(results, path):
        # The 0.20 mm gap shows up only in the expanded measurement pass.
        if results.mask_copper_assessments[0] >= 0.20:
            return _report(_finding("other", severity=Severity.WARNING))
        return _report()

    monkeypatch.setattr(check_cmd, "run_check_rule_set", assess)
    monkeypatch.setattr(compat, "drc_results_to_report", report)
    saved = fix_drc_cmd._run_python_drc(
        board, manufacturer="jlcpcb", mask_copper=True, guard_warnings=True, elbow_gaps=gaps
    )
    assert saved is not None and not saved.violations
    assert bands == [0.30, 0.15]
    gaps.check.assert_called_once_with([0.30])


def test_guard_rechecks_geometry_left_by_selective_undo(tmp_path, monkeypatch):
    from dataclasses import replace

    other = _seg("trk-b", (105, 100.3), (110.7, 100.3))
    board = _board2(tmp_path, ENTRY + other, r2_at="110 100")
    original = board.read_bytes()
    second = replace(_finding("trk-b"), items=["pad-r2-1", "trk-b"])
    baseline = _report(_finding("trk-a"), second)
    partial_seen = False

    def assess(path, **kwargs):
        nonlocal partial_seen
        if _end(path, "trk-a")[1] != (100, 100):
            return baseline
        if _end(path, "trk-b")[1] == (110, 100):
            return _report(second)  # retain the first move, undo the second
        partial_seen = True
        return _report(second, _pour_slot_warning(x=180, y=190))

    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", assess)
    fix_drc_cmd.main(
        [
            str(board),
            "--only",
            "mask-copper",
            "--no-new-warnings",
            "--no-connectivity-check",
            "--max-displacement",
            "1.0",
        ]
    )
    assert partial_seen
    assert board.read_bytes() == original


def test_guard_restores_whole_pass_when_connectivity_regresses(tmp_path, monkeypatch):
    from dataclasses import replace

    other = _seg("trk-b", (105, 100.3), (110.7, 100.3), net=2)
    board = _board2(tmp_path, ENTRY + other, r2_at="110 100")
    board.write_text(
        board.read_text().replace(
            '(net 1 "N1") (uuid "pad-r2-1")', '(net 2 "N2") (uuid "pad-r2-1")'
        )
    )
    original = board.read_bytes()
    second = replace(_finding("trk-b"), items=["pad-r2-1", "trk-b"])

    def connected_nets(path):
        if path.read_bytes() == original:
            return 2
        assert _end(path, "trk-a")[1] == (100, 100)
        return 1 if _end(path, "trk-b")[1] == (110, 100) else 2

    monkeypatch.setattr(fix_drc_cmd, "_count_connected_nets", connected_nets)
    monkeypatch.setattr(fix_drc_cmd, "_regressed_nets", lambda before, after: {"N2"})
    code = _run(
        monkeypatch,
        board,
        [_report(_finding("trk-a"), second), _report()],
        extra=["--no-new-warnings"],
        connectivity=True,
    )
    assert code == 3
    assert board.read_bytes() == original


def test_later_mask_detection_failure_rolls_back_saved_pass(tmp_path, monkeypatch):
    board = _board(tmp_path, ENTRY)
    original = board.read_bytes()
    seen_saved = False

    def assess(path, **kwargs):
        nonlocal seen_saved
        if path != board:
            # A real provisional move passed its assessment.
            assert _end(path, "trk-a")[1] == (100, 100)
            return _report()
        if board.read_bytes() == original:
            return _report(_finding("trk-a"))
        seen_saved = True
        assert _end(board, "trk-a")[1] == (100, 100)
        return None

    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", assess)
    code = fix_drc_cmd.main(
        [
            str(board),
            "--only",
            "mask-copper",
            "--no-new-warnings",
            "--transactional",
            "--max-passes",
            "3",
            "--no-connectivity-check",
            "--max-displacement",
            "1.0",
        ]
    )
    assert code == 4
    assert seen_saved
    assert board.read_bytes() == original


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


def _shared_exit_fixture(tmp_path, *, reverse=False, extra=""):
    junction = (101.125, 100.0)
    ends = {
        "lead-a": ((100.0, 100.0), junction),
        "lead-b": ((100.0, 100.0), junction),
        "short": (junction, (101.525, 100.4)),
        "long": (junction, (101.925, 100.8)),
    }
    copper = [_seg(uid, *(pair[::-1] if reverse else pair)) for uid, pair in ends.items()]
    # The rounded barrel blocks translating the outgoing tracks to the right.
    copper.append(
        "(via (at 101.925 100) (size 0.60) (drill 0.30) "
        '(layers "F.Cu" "B.Cu") (net 2) (uuid "obstacle"))'
    )
    return _board(tmp_path, "\n".join([*copper, extra])), ends


@pytest.mark.parametrize("reverse", [False, True])
def test_shared_exit_slides_without_changing_track_angles_and_undoes_together(tmp_path, reverse):
    board, ends = _shared_exit_fixture(tmp_path, reverse=reverse)
    original = {uid: _end(board, uid) for uid in ends}
    before = board.read_bytes()
    findings = [_pt_finding(uid, measured=0.025) for uid in ("short", "long")]
    repairer = MaskCopperRepairer(board)
    opts = {"max_displacement": 0.50, "clearance_mm": 0.127, "warning_clearance_mm": 0.15}
    assert repairer.repair(findings, dry_run=True, **opts).repaired == 1
    assert board.read_bytes() == before
    result = repairer.repair(findings, **opts)
    (action,) = result.actions
    repairer.save(board)
    assert {uid for _, uid, *_ in action.points} == set(ends)
    assert not repairer.off_angle_tracks()
    for uid in ("short", "long"):
        assert _end(board, uid)[0 if reverse else 1] == original[uid][0 if reverse else 1]
        copper, _ = _saved_copper(board, uid)
        assert copper.distance(Point(101.0, 100.0)) > 0.15
    assert repairer.undo_action(action)
    repairer.save(board)
    assert {uid: _end(board, uid) for uid in ends} == original


def test_shared_exit_with_a_fixed_pad_end_declines_without_mutating_the_group(tmp_path):
    via = '(via (at 100 100) (size 0.50) (drill 0.25) (layers "F.Cu" "B.Cu") (net 1) (uuid "bond"))'
    board, _ = _shared_exit_fixture(tmp_path, extra=via)
    before = board.read_bytes()
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_pt_finding("short", measured=0.025)], clearance_mm=0.127)
    assert not result.actions
    assert not repairer.modified
    assert board.read_bytes() == before


def test_shared_exit_respects_project_clearance_at_the_translated_pad_lead(tmp_path):
    board, _ = _shared_exit_fixture(
        tmp_path, extra=_via("above-lead", (100.45, 100.82), size=0.30, drill=0.15, net=2)
    )
    finding = _pt_finding("short", measured=0.025)
    assert MaskCopperRepairer(board).repair([finding], clearance_mm=0.127).repaired == 1
    board.with_suffix(".kicad_pro").write_text(
        json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": 0.60}]}})
    )
    repairer = MaskCopperRepairer(board)
    assert repairer.repair([finding], clearance_mm=0.127).repaired == 0
    assert not repairer.modified


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        ('(generated (uuid tuning) (members "lead-a"))', 0),
        ('(group "leads" (uuid child) (members "lead-a")) (generated (uuid g) (members CHILD))', 0),
        ('(generated (uuid g) (members child)) (group "exits" (uuid child) (members long))', 0),
        ('(group "leads" (uuid child) (members "lead-a"))', 1),
    ],
    ids=["direct-lead", "nested-lead", "nested-outgoing", "ordinary-group"],
)
def test_shared_exit_respects_generator_ownership(tmp_path, groups, expected):
    board, _ = _shared_exit_fixture(tmp_path, extra=groups)
    repairer = MaskCopperRepairer(board)
    original = repairer.doc.to_string()
    result = repairer.repair([_pt_finding("short", measured=0.025)], clearance_mm=0.127)
    assert result.repaired == expected
    assert repairer.modified == bool(expected)
    if not expected:
        assert repairer.doc.to_string() == original


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


@pytest.mark.parametrize("reverse", [False, True])
def test_pass_through_moves_shared_junction_and_keeps_its_via_fixed(tmp_path, reverse):
    anchor, junction, remote = (95, 100.62), (105, 100.62), (108, 103)
    track = _seg("trk-p", junction, anchor) if reverse else _seg("trk-p", anchor, junction)
    fork, inner = (108, 100.8), (95, 105)
    board = _board(
        tmp_path,
        "\n".join(
            [
                track,
                _via("fixed", anchor),
                _seg("joined", junction, remote),
                _seg("fork", fork, junction),
                _seg("other-layer", anchor, inner, layer="B.Cu"),
            ]
        ),
    )
    original = {
        uid: _saved_copper(board, uid)[1]
        for uid in ("trk-p", "fixed", "joined", "fork", "other-layer")
    }
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_pt_finding("trk-p")], max_displacement=1.0, clearance_mm=0.1)
    assert result.repaired == 1
    repairer.save(board)
    copper, ends = _saved_copper(board, "trk-p")
    moved = ends[0 if reverse else 1]
    action = result.actions[0]
    assert (action.old_x, action.old_y) == junction
    assert (round(action.new_x, 4), round(action.new_y, 4)) == moved
    assert ends[1 if reverse else 0] == _saved_copper(board, "fixed")[1] == anchor
    assert _end(board, "joined") == [moved, remote]
    assert _end(board, "fork") == [fork, moved]
    assert _end(board, "other-layer") == [anchor, inner]
    assert moved != junction and _pad_poly(board, "R1/1").distance(copper) >= 0.11 - 1e-6
    undo = MaskCopperRepairer(board)
    assert undo.undo_action(result.actions[0])
    undo.save(board)
    assert {uid: _saved_copper(board, uid)[1] for uid in original} == original


# A tap ending 0.19 mm off a centreline only just touches the 0.2 mm copper edge,
# on the side the nudge moves the track away from.
TAP_OFFSET = -0.19


@pytest.mark.parametrize(
    "block",
    [
        "locked_neighbor",
        "fixed_end_too_close",
        "two_vias",
        "unbroken_track",
        "arc_midpoint",
        "locked_tap_on_target",
        "tap_on_joined_leg",
    ],
)
def test_pass_through_preserves_an_immovable_junction(tmp_path, block):
    anchor = (99.5, 100.62) if block == "fixed_end_too_close" else (95, 100.62)
    junction = (105, 100.62)
    joined = _seg("joined", junction, (108, 103))
    if block == "locked_neighbor":
        joined = joined.replace('(uuid "joined")', '(locked yes) (uuid "joined")')
    copper = [_seg("trk-p", anchor, junction), _via("fixed", anchor), joined]
    if block == "two_vias":
        copper.append(_via("other", junction))
    if block == "unbroken_track":
        copper.append(_seg("tee", (105, 99), (105, 102)))
    if block == "arc_midpoint":
        copper.append(
            "(arc (start 104 101.62) (mid 105 100.62) (end 106 101.62) "
            '(width 0.2) (layer "F.Cu") (net 1) (uuid "arc"))'
        )
    if block == "locked_tap_on_target":
        tap = _seg("tap", (104.6, 100.62 + TAP_OFFSET), (104.6, 98))
        copper.append(tap.replace('(uuid "tap")', '(locked yes) (uuid "tap")'))
    if block == "tap_on_joined_leg":
        copper.append(_seg("tap", (106.5, 101.81 + TAP_OFFSET), (110, 99)))
    board = _board(tmp_path, "\n".join(copper))
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 0
    reason = {
        "fixed_end_too_close": "infeasible",
        "locked_tap_on_target": "would_break_same_net_contact",
        "tap_on_joined_leg": "would_break_same_net_contact",
    }.get(block, "junction_at_moved_end")
    assert result.skipped(reason) == 1
    assert not repairer.modified


def test_pass_through_declines_when_a_via_annulus_touches_the_track_shoulder(tmp_path):
    # The via centre is 0.39 mm off the centreline -- far outside a centreline
    # tolerance -- yet its 0.3 mm annulus meets the 0.1 mm half-width copper.
    anchor, junction = (95, 100.62), (105, 100.62)
    copper = [
        _seg("trk-p", anchor, junction),
        _via("fixed", anchor),
        _seg("joined", junction, (108, 103)),
        _via("shoulder", (104.6, 100.23)),
    ]
    repairer = MaskCopperRepairer(_board(tmp_path, "\n".join(copper)))
    result = repairer.repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 0 and result.skipped("would_break_same_net_contact") == 1
    assert not repairer.modified


def test_pass_through_keeps_a_collinear_overlap_that_survives_the_move(tmp_path):
    # A stationary track continuing the dragged leg's far end overlaps its
    # copper before and after, so the contact is preserved and the move stands.
    anchor, junction, remote = (95, 100.62), (105, 100.62), (108, 103)
    copper = [
        _seg("trk-p", anchor, junction),
        _via("fixed", anchor),
        _seg("joined", junction, remote),
        _seg("tail", remote, (110, 104.59)),
    ]
    repairer = MaskCopperRepairer(_board(tmp_path, "\n".join(copper)))
    result = repairer.repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == 1 and not result.skipped("junction_at_moved_end")


@pytest.mark.parametrize(
    "kind, layer, detail, blocked",
    [
        (
            "smd custom",
            "F.Cu",
            "(primitives (gr_rect (start -2.75 -0.05) (end -2.68 0.05) (width 0) (fill yes)))",
            True,
        ),
        (
            "smd custom",
            "F.Cu",
            "(primitives (gr_rect (start -1.75 -0.05) (end -1.68 0.05) (width 0) (fill yes)))",
            False,
        ),
        ("smd custom", "F.Cu", "(primitives (unknown_primitive))", True),
        ("smd custom", "B.Cu", "(primitives (unknown_primitive))", False),
        ("thru_hole rect", "*.Cu", "(drill 0.15 (offset -2.75 0))", True),
        ("thru_hole rect", "*.Cu", "(drill 0.15 (offset -1.75 0))", False),
        (
            "thru_hole rect",
            "*.Cu",
            '(drill 0.15) (padstack (mode custom) (layer "F.Cu" (shape rect) (size 6 1)))',
            True,
        ),
    ],
)
def test_fixed_via_pivot_checks_rotated_pad_copper_on_its_layer(
    tmp_path, kind, layer, detail, blocked
):
    # A 90-degree pad rotates negative local X toward positive board Y.
    # The first rectangle touches the old track by its edge. The second sits 1 mm farther away.
    anchor, junction = (95, 100.62), (105, 100.62)
    copper = [
        _seg("trk-p", anchor, junction),
        _via("fixed", anchor),
        _seg("joined", junction, (108, 103)),
        f'(footprint "test:P5" (layer "F.Cu") (at 104.5 97.84) '
        f'(property "Reference" "P5") (pad "1" {kind} (at 0 0 90) (size 0.2 0.2) '
        f'(layers "{layer}") (net 1 "N1") (uuid "test-pad") {detail}))',
    ]
    repairer = MaskCopperRepairer(_board(tmp_path, "\n".join(copper)))
    result = repairer.repair([_pt_finding("trk-p")], max_displacement=1.0)
    assert result.repaired == (0 if blocked else 1)
    assert bool(result.skipped("would_break_same_net_contact")) == blocked


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "contact, tap_end",
    [
        ("track", 90.551),
        ("via", 90.366),
        ("pad", 90.316),
        ("centered-custom", 90.316),
        ("offset-custom", 90.316),
        ("retained", 90.95),
    ],
)
def test_verified_fixed_via_move_preserves_physical_contact(tmp_path, capsys, contact, tap_end):
    from kicad_tools.cli import main as kct
    from kicad_tools.cli.kicad_toolchain import evidence_toolchain
    from kicad_tools.core.sexp_file import save_pcb
    from kicad_tools.sexp import parse_string

    # P2 already reaches P3 along a second path. A lost branch contact can
    # therefore leave both the native opens count and every pad group intact.
    fixture = Path(__file__).parent / "fixtures" / "mask_fixed_via_junction.kicad_pcb"
    doc = parse_file(fixture)
    track = "cc000000-0000-4000-8000-000000000050"
    tap = "cc000000-0000-4000-8000-000000000070"
    pad_contact = contact in {"pad", "centered-custom", "offset-custom"}
    shoulder = 89.25 if pad_contact else tap_end
    doc.add(
        parse_string(
            f"(segment (start 145.5 88.45) (end 145.5 {shoulder}) (width 0.15) "
            f'(layer "F.Cu") (net 1) (locked yes) (uuid "{tap}"))'
        )
    )
    if contact == "via":
        doc.add(parse_string(_via("cc000000-0000-4000-8000-000000000071", (145.5, tap_end))))
    elif pad_contact:
        # All three descriptions print the same copper. The broad feed contains
        # the offset anchor, and its narrow continuation contains the custom neck.
        for uid, start, end, width in [
            ("74", shoulder, 89.75, 0.32),
            ("75", 89.75, tap_end, 0.15),
        ]:
            doc.add(
                parse_string(
                    f"(segment (start 145.5 {start}) (end 145.5 {end}) (width {width}) "
                    f'(layer "F.Cu") (net 1) (locked yes) '
                    f'(uuid "cc000000-0000-4000-8000-0000000000{uid}"))'
                )
            )
        anchor_y = 89.5 if contact == "offset-custom" else 90.436
        shape, size = "rect", 0.45
        primitives = ""
        if contact != "pad":
            shape, size = "custom", 0.30
            neck = ""
            if contact == "offset-custom":
                neck = "(gr_rect (start -0.04 0) (end 0.04 0.936) (width 0) (fill yes))"
            primitives = (
                "(options (clearance outline) (anchor circle)) "
                f"(primitives (gr_rect (start -0.225 {90.211 - anchor_y:.6f}) "
                f"(end 0.225 {90.661 - anchor_y:.6f}) (width 0) (fill yes)) {neck})"
            )
        doc.add(
            parse_string(
                f'(footprint "Test:Contact" (layer "F.Cu") (at 145.5 {anchor_y}) '
                '(uuid "cc000000-0000-4000-8000-000000000072") '
                '(property "Reference" "P5" (at 0 -1) (layer "F.Fab")) '
                f'(pad "1" smd {shape} (at 0 0) (size {size} {size}) (layers "F.Cu") '
                f'(net 1 "N1") (uuid "cc000000-0000-4000-8000-000000000073") {primitives}))'
            )
        )
    board = tmp_path / fixture.name
    save_pcb(doc, board)
    original = board.read_bytes()
    old_track, _ = _saved_copper(board, track)
    with evidence_toolchain(mounts=[tmp_path]):
        # The pcbnew worker is written beside its input, so take the reference
        # reading in a private stage just as fix-drc does.
        with fix_drc_cmd._with_project_rules(board, board, private=True) as staged:
            groups = fix_drc_cmd._mask_native_pad_groups(staged)
        assert len(groups) == 1 and len(groups[0]) == (5 if pad_contact else 4)
        kct(
            [
                "fix-drc",
                str(board),
                "--only",
                "mask-copper",
                "--mfr",
                "jlcpcb-tier1",
                "--max-passes",
                "1",
                "--max-displacement",
                "2",
                "--verify",
                "--format",
                "json",
            ]
        )
        with fix_drc_cmd._with_project_rules(board, board, private=True) as staged:
            assert fix_drc_cmd._mask_native_pad_groups(staged) == groups
    report = json.loads(capsys.readouterr().out)["mask_copper"]
    assert report["violations"] == 1
    if contact == "retained":
        copper, _ = _saved_copper(board, track)
        stationary, _ = _saved_copper(board, tap)
        assert report["repaired"] == 1, report
        assert not copper.equals(old_track)
        assert old_track.intersection(stationary).area > 0
        assert copper.intersection(stationary).area > 0
    else:
        assert report["repaired"] == 0
        assert [item["reason"] for item in report["unrepaired"]] == ["would_break_same_net_contact"]
        assert board.read_bytes() == original


def test_via_fixed_junction_clears_both_openings_within_displacement_limit(tmp_path):
    anchor, junction = (95, 100.62), (105, 100.62)
    copper = [
        _seg("trk-p", anchor, junction),
        _via("fixed", anchor),
        _seg("joined", junction, (108, 103)),
    ]
    board = _board2(tmp_path, "\n".join(copper), "102.8 100.01")
    findings = [_pt_finding("trk-p"), _pt_finding("trk-p", pad="pad-r2-1", measured=0.01)]
    repairer = MaskCopperRepairer(board)
    refused = repairer.repair(findings, max_displacement=0.15)
    assert refused.skipped("exceeds_max_displacement") and not repairer.modified

    result = repairer.repair(findings, max_displacement=1.0)
    assert result.repaired == 1 and set(result.actions[0].openings) == {PAD, "pad-r2-1"}
    repairer.save(board)
    shape, _ = _saved_copper(board, "trk-p")
    for ref in ("R1/1", "R2/1"):
        assert _pad_poly(board, ref).distance(shape) >= 0.11 - 1e-6


def test_via_fixed_junction_checks_clearance_on_other_joined_track(tmp_path):
    anchor, junction = (95, 100.62), (105, 100.62)
    copper = [
        _seg("trk-p", anchor, junction),
        _via("fixed", anchor),
        _seg("joined", junction, (108, 103)),
        _seg("n2", (105.68, 101.57), (105.72, 101.57), net=2),
    ]
    repairer = MaskCopperRepairer(_board(tmp_path, "\n".join(copper)))
    result = repairer.repair([_pt_finding("trk-p")], max_displacement=1.0, clearance_mm=0.1)
    assert result.repaired == 0 and result.skipped("would_violate_clearance") == 1
    assert not repairer.modified


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
    # The drill-violating destination is not taken: the via either stays or
    # lands somewhere that keeps the manufacturer's hole spacing (issue
    # #9939's bounded search around a vetoed first choice).
    assert result.repaired in (0, 1)
    if result.repaired == 0:
        assert result.skipped("would_violate_clearance") == 1
        assert "hole-to-hole" in result.unrepaired[0].detail
        return
    action = result.actions[0]
    moved = (action.new_x, action.new_y)
    assert moved != dest
    assert math.dist(moved, (dest[0] + 0.7, dest[1])) - 0.3 >= rules.min_hole_to_hole_mm - 1e-6
    assert _pad_poly(board, "R1/1").distance(Point(moved).buffer(0.3)) >= 0.11 - 1e-6


def _board_with_reported_approach(tmp_path):
    # The front trace already overlaps the same opening. Bending it would
    # transfer its visible overlap to a new segment when the bend is split.
    centre = (101.05, 99.15)
    copper = "\n".join(
        [
            _via("via-p", centre),
            _seg("approach", (101.05, 99.7), centre),
            _seg("back", centre, (102.05, 99.15), layer="B.Cu"),
        ]
    )
    board = _board(tmp_path, copper)
    findings = [
        _finding("approach", "same-net pass-through"),
        _pt_finding("via-p", measured=math.hypot(0.05, 0.35) - 0.30),
    ]
    return board, findings


@pytest.mark.parametrize(
    ("bound", "expected_gap"), [(0.5, 0.17), (0.09, 0.11)], ids=["preferred", "required"]
)
def test_via_with_reported_approach_preserves_its_line_and_undoes(tmp_path, bound, expected_gap):
    board, findings = _board_with_reported_approach(tmp_path)
    original = board.read_bytes()
    centre = _saved_copper(board, "via-p")[1]
    repairer = MaskCopperRepairer(board)
    assert not repairer.repair(findings, max_displacement=0.02).actions
    assert board.read_bytes() == original

    result = repairer.repair(findings, max_displacement=bound, warning_clearance_mm=0.15)
    (action,) = result.actions
    assert action.action == "relocate_clear"
    repairer.save(board)
    assert _end(board, "approach")[0] == (101.05, 99.7)
    assert _end(board, "approach")[1][0] == 101.05
    assert _end(board, "back")[0][1] == 99.15
    pad = repairer._openings()[PAD][0]
    assert Point(action.new_x, action.new_y).distance(pad) - 0.30 >= expected_gap - 1e-6
    assert action.displacement_mm <= bound + 1e-6
    reloaded = MaskCopperRepairer(board)
    assert reloaded.undo_action(action)
    reloaded.save(board)
    assert _saved_copper(board, "via-p")[1] == centre
    assert _end(board, "approach") == [(101.05, 99.7), centre]
    assert _end(board, "back") == [centre, (102.05, 99.15)]
    assert len(parse_file(board).find_all("segment")) == 2


def test_public_via_repair_with_reported_approach_restores_input_on_native_veto(
    tmp_path, monkeypatch, capsys
):
    from collections import Counter

    board, findings = _board_with_reported_approach(tmp_path)
    original = board.read_bytes()
    centre = _saved_copper(board, "via-p")[1]

    def native_state(candidate):
        if _saved_copper(candidate, "via-p")[1] == centre:
            return Counter(), 0
        assert _end(candidate, "approach")[1][0] == 101.05
        return Counter({("items_not_allowed", "via rule", ("via-p",)): 1}), 0

    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", native_state)
    _run(
        monkeypatch,
        board,
        [_report(*findings), _report(findings[0])],
        extra=("--max-displacement", "0.5", "--max-passes", "1", "--verify", "--format", "json"),
    )
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert result["repaired"] == 0
    via = next(u for u in result["unrepaired"] if u["uuid"] == "via-p")
    assert "1 new errors, native opens 0 to 0" in via["detail"]
    assert board.read_bytes() == original


def test_via_alternate_move_keeps_approach_lines_and_adds_aligned_tails(tmp_path):
    """Issue #9939: a vetoed first choice is worked around, not forced.

    The primary destination is blocked by a foreign track, so the search
    relocates the via instead.  Whatever it accepts keeps every approach on
    its own line (no off-angle drag) and any added connection lies wholly
    inside the via land on the 0/45/90/135 set.  Undo restores the board
    exactly, including removing the added copper.
    """
    from kicad_tools.router.quantize import _is_quantum_aligned

    copper = (
        PASS_VIA
        + "\n"
        + _seg("trk-f", (102.35, 98.6), (101.35, 99.6))
        + "\n"
        + _seg("trk-b", (101.35, 98.2), (101.35, 99.6), layer="B.Cu")
    )
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    probe = MaskCopperRepairer(_board(plain_dir, copper))
    probe_result = probe.repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))], max_displacement=1.0
    )
    assert probe_result.repaired == 1
    dest = (probe_result.actions[0].new_x, probe_result.actions[0].new_y)

    # A foreign wall sits where the primary push lands, closer than the
    # project's own clearance: the drag is vetoed, the search must go around.
    blocker = _seg("trk-x", (101.95, 99.6), (101.95, 100.2), net=2, layer="B.Cu")
    board = _board(tmp_path, copper + "\n" + blocker)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))], max_displacement=1.0
    )
    assert result.repaired == 1 and not result.unrepaired
    action = result.actions[0]
    assert action.action == "relocate_clear"
    repairer.save(board)

    centre = (round(action.new_x, 4), round(action.new_y, 4))
    assert centre != dest
    assert _pad_poly(board, "R1/1").distance(Point(centre).buffer(0.3)) >= 0.11 - 1e-6
    blocker_geom, _ends = _saved_copper(board, "trk-x")
    assert Point(centre).buffer(0.3).distance(blocker_geom) >= 0.2 - 1e-6

    # Each slid end stays on its own approach line, inside the new land.
    assert {move[0] for move in action.end_moves} == {"trk-f", "trk-b"}
    for uuid, tag, _ox, _oy, new_x, new_y in action.end_moves:
        ends = dict(zip(["start", "end"], _end(board, uuid), strict=True))
        moved, fixed = ends[tag], ends["end" if tag == "start" else "start"]
        original = (101.35, 99.6)
        cross = (moved[0] - fixed[0]) * (original[1] - fixed[1]) - (moved[1] - fixed[1]) * (
            original[0] - fixed[0]
        )
        assert abs(cross) < 1e-3  # collinear: the approach kept its angle
        assert math.dist(moved, centre) + 0.1 <= 0.3 + 1e-6  # inside the land, width included

    # Each layer needs its own connection from the slid end to the via centre.
    assert sorted((tail[6], tail[7]) for tail in action.added_segments) == [
        ("B.Cu", 1),
        ("F.Cu", 1),
    ]
    reloaded = MaskCopperRepairer(board)
    for tail in action.added_segments:
        uuid, sx, sy, ex, ey, width, layer, net = tail
        track = {"F.Cu": "trk-f", "B.Cu": "trk-b"}[layer]
        assert (sx, sy) == _end(board, track)[1]
        assert (ex, ey) == centre
        segment = reloaded._segment_by_uuid(uuid)
        assert segment.find("layer").get_first_atom() == layer
        assert segment.find("net").get_first_atom() == net
        assert _is_quantum_aligned(ex - sx, ey - sy)
        for point in ((sx, sy), (ex, ey)):
            assert math.dist(point, centre) + width / 2 <= 0.3 + 1e-6
        assert set(_end(board, tail[0])) == {
            (round(sx, 6), round(sy, 6)),
            (round(ex, 6), round(ey, 6)),
        }

    assert reloaded.undo_action(action)
    reloaded.save(board)
    assert _saved_copper(board, "via-p")[1] == (101.35, 99.6)
    for uuid, _tag, *_rest in action.end_moves:
        assert _end(board, uuid)[1] == (101.35, 99.6)
    for tail in action.added_segments:
        with pytest.raises(AssertionError):
            _end(board, tail[0])


@pytest.mark.parametrize("net_atom", [1, "N1", "123"], ids=["number", "name", "numeric-name"])
def test_via_tail_plan_builds_aligned_in_land_connections(tmp_path, net_atom):
    """Issue #9939: the kept-line contact plan, exercised directly.

    A diagonal and a vertical approach both end at the via; after a move to
    (101.45, 99.48) each end slides along its own line and a 0/45/90/135
    connection runs to the new centre wholly inside the land, at the file's
    own coordinate precision.  Applying and undoing the action round-trips
    the board, added copper included.
    """
    from kicad_tools.drc.repair_mask_copper import MaskRepairResult, MaskViaRelocateAction
    from kicad_tools.router.primitives import _make_uuid
    from kicad_tools.router.quantize import _is_quantum_aligned, off_angle_degrees

    copper = (
        PASS_VIA
        + "\n"
        + _seg("trk-f", (102.75, 98.85), (101.35, 99.6))
        + "\n"
        + _seg("trk-b", (101.35, 98.2), (101.35, 99.6), layer="B.Cu")
    )
    board = _board(tmp_path, copper)
    net_name = "N1" if isinstance(net_atom, int) else net_atom
    if isinstance(net_atom, str):
        text = "\n".join(
            line for line in board.read_text().splitlines() if not line.startswith("  (net ")
        )
        text = text.replace('(net 1 "N1")', f'(net "{net_name}")').replace(
            "(net 1)", f'(net "{net_name}")'
        )
        board.write_text(text + "\n")
    repairer = MaskCopperRepairer(board)
    joined = repairer._anchored_ends(101.35, 99.6, net_name)
    assert [tag for _u, _n, tag, _e in joined] == ["end", "end"]
    plan = repairer._via_tail_plan(joined, (101.45, 99.48), 0.6, 1.0)
    assert plan is not None
    end_moves, tails, _final_lines = plan
    assert len(end_moves) == 2 and len(tails) == 2

    # The vertical approach keeps its line and connects horizontally; the
    # diagonal approach keeps its line and connects at exactly 45 degrees.
    by_uuid = {move[0]: move for move in end_moves}
    assert by_uuid["trk-b"][4:] == (101.35, 99.48)
    assert by_uuid["trk-f"][4:] == (101.493256, 99.523256)
    for start_x, start_y, end_x, end_y, width, layer, net in tails:
        assert _is_quantum_aligned(end_x - start_x, end_y - start_y)
        # exactly on the 0/45/90/135 set, not merely within a rounding quantum
        assert off_angle_degrees(end_x - start_x, end_y - start_y) <= 0.01
        # wholly inside the new land, track width included
        for point in ((start_x, start_y), (end_x, end_y)):
            assert math.dist(point, (101.45, 99.48)) + width / 2 <= 0.3 - 0.001 + 1e-9

    # The slid ends stay collinear with their original lines.
    for move, (fixed_x, fixed_y) in zip(
        sorted(end_moves), [(101.35, 98.2), (102.75, 98.85)], strict=True
    ):
        _uuid, _tag, _ox, _oy, new_x, new_y = move
        cross = (new_x - fixed_x) * (99.6 - fixed_y) - (new_y - fixed_y) * (101.35 - fixed_x)
        assert abs(cross) < 2e-4  # on the line to within serialization rounding

    # Apply records the action, moves the via and ends, and inserts the
    # connections as ordinary segments beside the existing ones; undo
    # removes them again and restores every original coordinate.
    added = [(_make_uuid(), *tail) for tail in tails]
    points = [("via", "via-p", None, 101.35, 99.6)]
    points.extend(
        ("segment", uuid, tag, old_x, old_y) for uuid, tag, old_x, old_y, _nx, _ny in end_moves
    )
    points.extend(("segment", tail[0], None, tail[1], tail[2]) for tail in added)
    action = MaskViaRelocateAction(
        action="relocate_clear",
        uuid="via-p",
        kind="via",
        pad_uuid="pad-r1-1",
        pad_ref="R1/1",
        net_name=net_name,
        layer="F.Cu-B.Cu",
        old_x=101.35,
        old_y=99.6,
        new_x=101.45,
        new_y=99.48,
        dx=0.1,
        dy=-0.12,
        displacement_mm=math.hypot(0.1, 0.12),
        detail="unit plan",
        openings=["pad-r1-1"],
        points=points,
        end_moves=end_moves,
        added_segments=added,
    )
    result = MaskRepairResult()
    assert repairer._apply_via_relocate(action, result, False) is None
    assert result.repaired == 1 and repairer.modified
    repairer.save(board)
    assert _saved_copper(board, "via-p")[1] == (101.45, 99.48)
    assert _end(board, "trk-f")[1] == by_uuid["trk-f"][4:]
    assert _end(board, "trk-b")[1] == (101.35, 99.48)
    reloaded = MaskCopperRepairer(board)
    for tail in added:
        segment = reloaded._segment_by_uuid(tail[0])
        assert segment.find("net").get_first_atom() == net_atom
        assert reloaded._net_name(segment) == net_name
        assert set(_end(board, tail[0])) == {
            (round(tail[1], 6), round(tail[2], 6)),
            (round(tail[3], 6), round(tail[4], 6)),
        }
        assert _saved_copper(board, tail[0])[0].length > 0  # a real segment, not noise

    assert reloaded.undo_action(action)
    reloaded.save(board)
    assert _saved_copper(board, "via-p")[1] == (101.35, 99.6)
    assert _end(board, "trk-f")[1] == (101.35, 99.6)
    assert _end(board, "trk-b")[1] == (101.35, 99.6)
    for tail in added:
        with pytest.raises(AssertionError):
            _end(board, tail[0])


def test_via_alternate_blocked_in_every_direction_stays_in_place(tmp_path):
    """Issue #9939: a via with no legal candidate is left alone, with a reason.

    Foreign copper fences every direction that would clear the pad opening,
    so the bounded search finds nothing and the board is untouched.
    """
    fence = "\n".join(
        [
            # Left wall past the pad, right wall, floor below, roof above:
            # every opening-clearing destination crowds one of them below
            # the project's own clearance (0.2 mm without a project).
            _seg("wall-l", (101.85, 98.4), (101.85, 100.8), net=2),
            _seg("wall-r", (102.85, 98.4), (102.85, 100.8), net=2),
            _seg("wall-b", (101.85, 98.4), (102.85, 98.4), net=2),
            _seg("wall-t", (101.85, 100.8), (102.85, 100.8), net=2),
        ]
    )
    copper = PASS_VIA + "\n" + _seg("trk-f", (101.35, 99.6), (103, 99.6)) + "\n" + fence
    board = _board(tmp_path, copper)
    original = board.read_bytes()
    result = MaskCopperRepairer(board).repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))], max_displacement=0.6
    )
    assert result.repaired == 0
    assert result.skipped("would_violate_clearance") == 1
    assert "project rules" in result.unrepaired[0].detail
    assert board.read_bytes() == original


@pytest.mark.parametrize(("wall_x", "required"), [(102.09, 0.3), (103.26, 1.5)])
def test_via_alternate_respects_authored_net_class_over_manufacturer_floor(
    tmp_path, wall_x, required
):
    """Issue #9939: a destination that only passes the manufacturer floor is
    refused when the project's authored net class demands more clearance.

    The farther wall exercises a class whose clearance exceeds the old
    fixed search box. Both boards have a legal alternate and a constrained
    control in which the via has to stay put.
    """
    from kicad_tools.manufacturers.base import DesignRules

    copper = PASS_VIA + "\n" + _seg("trk-f", (101.35, 98.2), (101.35, 99.6))
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    probe = MaskCopperRepairer(_board(plain_dir, copper))
    probe_result = probe.repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))], max_displacement=1.0
    )
    assert probe_result.repaired == 1
    dest = (probe_result.actions[0].new_x, probe_result.actions[0].new_y)

    rules = DesignRules(
        min_clearance_mm=0.127,
        min_trace_width_mm=0.127,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.15,
    )
    blocker = _seg("trk-x", (wall_x, 99.3), (wall_x, 100.0), net=2)
    board = _board(tmp_path, copper + "\n" + blocker)
    board.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "classes": [
                        {"name": "Default", "clearance": 0.2},
                        {"name": "Wide", "clearance": required},
                    ],
                    "netclass_assignments": {"N2": "Wide"},
                }
            }
        )
    )
    result = MaskCopperRepairer(board).repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))],
        max_displacement=1.0,
        clearance_mm=rules.min_clearance_mm,
        design_rules=rules,
    )
    assert result.repaired == 1 and not result.unrepaired
    action = result.actions[0]
    moved = (round(action.new_x, 4), round(action.new_y, 4))
    assert moved != dest
    blocker_geom, _ends = _saved_copper(board, "trk-x")
    assert Point(moved).buffer(0.3).distance(blocker_geom) >= required - 1e-6
    assert _pad_poly(board, "R1/1").distance(Point(moved).buffer(0.3)) >= 0.11 - 1e-6

    # With no room to go around, the same board refuses: every candidate
    # that clears the opening stays inside the authored class's reach.
    tight_dir = tmp_path / "tight"
    tight_dir.mkdir()
    tight = _board(tight_dir, copper + "\n" + blocker)
    tight.with_suffix(".kicad_pro").write_text(board.with_suffix(".kicad_pro").read_text())
    tight_result = MaskCopperRepairer(tight).repair(
        [_pt_finding("via-p", measured=0.05, location=(101.05, 99.6))],
        max_displacement=0.1,
        clearance_mm=rules.min_clearance_mm,
        design_rules=rules,
    )
    assert tight_result.repaired == 0
    assert tight_result.skipped("would_violate_clearance") == 1
    assert "project rules" in tight_result.unrepaired[0].detail


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
    # Both findings persist. Undo trk-2 first so it has a specific
    # disposition when undoing trk-a fails and the batch is discarded.
    board = _board2(tmp_path, ENTRY + "\n" + _seg("trk-2", (106, 100.3), (103.7, 100.3)), "103 100")
    original = board.read_bytes()
    f1 = _own_finding("trk-a", PAD, "R1/1")
    f2 = _own_finding("trk-2", "pad-r2-1", "R2/1")
    still = _report(f1, f2)
    real_undo = MaskCopperRepairer.undo_action

    def selective_undo(self, action):
        return False if action.uuid == "trk-a" else real_undo(self, action)

    monkeypatch.setattr(MaskCopperRepairer, "undo_action", selective_undo)
    code = _run(monkeypatch, board, [still, still, still], extra=("--format", "json"))
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    records = payload["mask_copper"]["unrepaired"]
    assert sorted((record["uuid"], record["reason"]) for record in records) == [
        ("trk-2", "finding_not_cleared"),
        ("trk-a", "recheck_unavailable"),
    ]
    assert board.read_bytes() == original


# ---------------------------------------------------------------------------
# Issue #9939: a jog between two parallel runs slides along them
# ---------------------------------------------------------------------------


def _jog_fixture(tmp_path, *, copies=1, anchor=(98.0, 100.4), extra="", lock=""):
    """The pad lead enters at (99, 100); the jog leaves it at 45 degrees.

    Each track is repeated ``copies`` times, like the coincident BTN2 routes on
    Board 03, so the whole group has to move together.
    """
    shapes = {
        "lead": ((100.5, 100.0), (99.0, 100.0)),
        "jog": ((99.0, 100.0), (98.6, 100.4)),
        "run": ((98.6, 100.4), anchor),
    }
    tracks = [
        _seg(f"{name}-{i}", a, b).replace(
            "(segment ", f"(segment {lock} " if (name, i) == ("run", 0) else "(segment "
        )
        for name, (a, b) in shapes.items()
        for i in range(copies)
    ]
    return _board(tmp_path, "\n".join([*tracks, extra])), shapes


def _entry_finding(conductor: str):
    finding = _finding(conductor)
    finding.required_value_mm = 0.09
    finding.actual_value_mm = 0.0
    return finding


_JOG_OPTS = {"max_displacement": 1.0, "clearance_mm": 0.127, "warning_clearance_mm": 0.15}


@pytest.mark.parametrize("copies", [1, 3])
def test_jog_between_parallel_runs_slides_without_changing_any_angle(tmp_path, copies):
    board, shapes = _jog_fixture(tmp_path, copies=copies)
    before = board.read_bytes()
    original = {f"{n}-{i}": _end(board, f"{n}-{i}") for n in shapes for i in range(copies)}
    repairer = MaskCopperRepairer(board)
    findings = [_entry_finding(f"jog-{i}") for i in range(copies)]
    assert repairer.repair(findings, dry_run=True, **_JOG_OPTS).repaired == 1
    assert board.read_bytes() == before

    result = repairer.repair(findings, **_JOG_OPTS)
    assert result.repaired == 1
    (action,) = result.actions
    assert action.action == "nudge_clear" and action.dy == 0 and action.dx < 0
    assert len(action.points) == 4 * copies  # both jog ends and both joined run ends
    repairer.save(board)
    assert not repairer.off_angle_tracks()
    for i in range(copies):
        lead, jog, run = (_end(board, f"{n}-{i}") for n in ("lead", "jog", "run"))
        # The pad end of the lead and the downstream anchor never move.
        assert lead[0] == original[f"lead-{i}"][0]
        assert run[1] == original[f"run-{i}"][1]
        # The joints follow the jog along their own lines.
        assert lead[1] == jog[0] and jog[1] == run[0]
        assert lead[1][1] == 100.0 and run[0][1] == 100.4
        assert math.isclose(jog[0][0] - jog[1][0], 0.4, abs_tol=1e-9)
        copper, _ = _saved_copper(board, f"jog-{i}")
        assert copper.distance(_pad_poly(board, "R1/1")) >= 0.15

    assert repairer.undo_action(action)
    repairer.save(board)
    assert {k: _end(board, k) for k in original} == original


def test_jog_slide_declines_when_a_run_would_collapse_and_leaves_the_board_alone(tmp_path):
    # The downstream run is too short to give up the 0.25 mm the jog needs.
    board, _ = _jog_fixture(tmp_path, anchor=(98.4, 100.4))
    repairer = MaskCopperRepairer(board)
    before = repairer.doc.to_string()
    pads = repairer._openings()
    slid = repairer._slide_jog_along_runs(
        repairer._segment_nodes()["jog-0"],
        "jog-0",
        PAD,
        pads,
        MaskRepairResult(),
        1.0,
        False,
        0.127,
        0.09,
        0.02,
        0.15,
        set(),
    )
    assert slid is False
    assert not repairer.modified and repairer.doc.to_string() == before


def test_jog_slide_vetoed_by_other_net_copper_or_a_split_restores_everything(tmp_path):
    crowd = _seg("foreign", (98.75, 99.4), (98.75, 99.72), net=2)
    cases = {
        "foreign-copper": crowd,
        # A thin same-net branch touching the middle of the jog would be orphaned.
        "branch-on-jog": _seg("branch", (98.8, 100.2), (98.8, 103.0), width=0.04),
        "locked-run": "",
    }
    for name, extra in cases.items():
        sub = tmp_path / name
        sub.mkdir()
        board, _ = _jog_fixture(sub, extra=extra, lock="locked" if name == "locked-run" else "")
        repairer = MaskCopperRepairer(board)
        before = repairer.doc.to_string()
        result = repairer.repair([_entry_finding("jog-0")], dry_run=True, **_JOG_OPTS)
        assert not any(a.action == "nudge_clear" for a in result.actions), name
        assert repairer.doc.to_string() == before, name


# ---------------------------------------------------------------------------
# Issue #9939: a chamfer between perpendicular runs slides along both
# ---------------------------------------------------------------------------


def _chamfer_fixture(tmp_path, *, run_end=(101.4, 102.0), copies=1, extra="", lock=""):
    """The pad lead runs out through the pad's right edge (x = 101).

    The 45 degree chamfer leaves the edge exactly at (101, 100) and turns into
    a downstream run along x = 101.4, like the TQFP escapes on Board 03.
    """
    shapes = {
        "lead": ((100.0, 100.0), (101.0, 100.0)),
        "chamfer": ((101.0, 100.0), (101.4, 100.4)),
        "run": ((101.4, 100.4), run_end),
    }
    tracks = [
        _seg(f"{name}-{i}", a, b).replace(
            "(segment ", f"(segment {lock} " if (name, i) == ("run", 0) else "(segment "
        )
        for name, (a, b) in shapes.items()
        for i in range(copies)
    ]
    return _board(tmp_path, "\n".join([*tracks, extra])), shapes


# Other-net copper above the pad that a plain recentre of the chamfer's pad end
# (to the pad centre) would crowd, the way Board 03's U1/33 entry is declined.
_CROWD_RECENTRE = _seg("crowd", (100.3, 100.45), (100.8, 100.45), net=2)


@pytest.mark.parametrize("copies", [1, 2])
def test_chamfer_between_perpendicular_runs_slides_each_end_along_its_run(tmp_path, copies):
    board, shapes = _chamfer_fixture(tmp_path, copies=copies, extra=_CROWD_RECENTRE)
    before = board.read_bytes()
    original = {f"{n}-{i}": _end(board, f"{n}-{i}") for n in shapes for i in range(copies)}
    repairer = MaskCopperRepairer(board)
    findings = [_entry_finding(f"chamfer-{i}") for i in range(copies)]
    assert repairer.repair(findings, dry_run=True, **_JOG_OPTS).repaired == 1
    assert board.read_bytes() == before

    result = repairer.repair(findings, **_JOG_OPTS)
    assert result.repaired == 1
    (action,) = result.actions
    assert action.action == "nudge_clear" and "chamfer" in action.detail
    assert len(action.points) == 4 * copies and len(action.shifts) == len(action.points)
    repairer.save(board)
    assert not repairer.off_angle_tracks()
    for i in range(copies):
        lead, chamfer, run = (_end(board, f"{n}-{i}") for n in ("lead", "chamfer", "run"))
        # The pad centre and the downstream anchor never move.
        assert lead[0] == original[f"lead-{i}"][0]
        assert run[1] == original[f"run-{i}"][1]
        # The joints stay connected, each on its own line.
        assert lead[1] == chamfer[0] and chamfer[1] == run[0]
        assert lead[1][1] == 100.0 and run[0][0] == 101.4
        # Still a 45 degree chamfer, now clear of the pad opening.
        assert lead[1][0] > 101.0
        assert math.isclose(
            abs(chamfer[1][0] - chamfer[0][0]), abs(chamfer[1][1] - chamfer[0][1]), abs_tol=1e-9
        )
        copper, _ = _saved_copper(board, f"chamfer-{i}")
        assert copper.distance(_pad_poly(board, "R1/1")) >= 0.15

    assert repairer.undo_action(action)
    repairer.save(board)
    assert {k: _end(board, k) for k in original} == original


def test_chamfer_slide_declines_and_leaves_the_board_alone_when_vetoed(tmp_path):
    # Every case also crowds the plain recentre, so only the slide's own veto is tested.
    cases = {
        # Other-net copper beside the chamfer's new position.
        "foreign-copper": (
            _seg("beside", (101.55, 99.8), (101.55, 100.15), net=2),
            "",
            (101.4, 102.0),
        ),
        "locked-run": ("", "locked", (101.4, 102.0)),
        # A via at the chamfer's far end cannot be dragged, as at Board 03's U1/17.
        "via-at-far-end": (
            '  (via (at 101.4 100.4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            '(net 1) (uuid "via-far"))',
            "",
            (101.4, 102.0),
        ),
    }
    for name, (extra, lock, run_end) in cases.items():
        sub = tmp_path / name
        sub.mkdir()
        board, _ = _chamfer_fixture(
            sub, extra=f"{_CROWD_RECENTRE}\n{extra}", lock=lock, run_end=run_end
        )
        repairer = MaskCopperRepairer(board)
        before = repairer.doc.to_string()
        result = repairer.repair([_entry_finding("chamfer-0")], dry_run=True, **_JOG_OPTS)
        assert not result.actions and not result.repaired, name
        assert repairer.doc.to_string() == before, name


# ---------------------------------------------------------------------------
# Issue #10183: 4 dp rounding of the slid ends never trips the angle census
# ---------------------------------------------------------------------------


def _rounding_chamfer_fixture(tmp_path, *, diagonal, signs, offset, reach, chamfer):
    """A chamfer between runs whose coordinates sit off the 4 dp grid.

    Axis-aligned runs (``diagonal=False``) give a 45 degree chamfer; 45 degree
    runs give a horizontal one. ``offset`` shifts every point by a 6 dp amount
    so the slid ends round to 4 dp by different amounts, and ``signs`` mirrors
    the layout about the pad centre.
    """
    if diagonal:
        joints = ((reach, reach), (reach + chamfer, reach), (reach + chamfer + 1.0, reach - 1.0))
    else:
        joints = ((reach, 0.0), (reach + chamfer, chamfer), (reach + chamfer, chamfer + 1.0))

    def at(dx, dy):
        return (
            round(100 + signs[0] * dx + offset[0], 6),
            round(100 + signs[1] * dy + offset[1], 6),
        )

    points = [at(0.0, 0.0), *(at(*joint) for joint in joints)]
    shapes = dict(
        zip(("lead", "chamfer", "run"), zip(points, points[1:], strict=False), strict=True)
    )
    copper = [_seg(name, a, b) for name, (a, b) in shapes.items()]
    # Other-net copper that makes a plain recentre of the pad end decline.
    copper.append(_seg("crowd", at(0.3, 0.9), at(0.9, 0.9), net=2))
    return _board(tmp_path, "\n".join(copper)), shapes


@pytest.mark.parametrize("signs", [(1, 1), (-1, 1), (1, -1), (-1, -1)])
@pytest.mark.parametrize("diagonal", [False, True], ids=["axis-runs", "diagonal-runs"])
@pytest.mark.parametrize("offset", [(0.0, 0.0), (3.7e-05, 1.9e-05), (8.7e-05, 5e-05)])
@pytest.mark.parametrize("reach, chamfer", [(0.7, 0.8), (1.2, 0.8), (1.2, 0.4)])
def test_chamfer_slide_rounding_keeps_the_angle_census_clean(
    tmp_path, signs, diagonal, offset, reach, chamfer
):
    """Measured refutation of the #10183 hypothesis, kept as a regression guard.

    ``moved`` supplies candidate geometry and action metadata. When writing the
    board, ``_apply_nudge`` adds each point's shift to its original endpoint
    and rounds each coordinate to four decimals. Independent endpoint rounding
    can put the chamfer one 0.1 um quantum off its exact angle. The census
    accepts that difference, so these slides preserve the angle on disk.
    """
    board, shapes = _rounding_chamfer_fixture(
        tmp_path, diagonal=diagonal, signs=signs, offset=offset, reach=reach, chamfer=chamfer
    )
    original = {name: _end(board, name) for name in shapes}
    assert not MaskCopperRepairer(board).off_angle_tracks()
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_entry_finding("chamfer")], **_JOG_OPTS)
    (action,) = result.actions
    assert action.action == "nudge_clear" and "chamfer" in action.detail
    repairer.save(board)

    lead, chamfer_seg, run = (_end(board, name) for name in shapes)
    # The slide moved both joints, and neither anchor.
    assert lead[1] != original["lead"][1] and run[0] != original["run"][0]
    assert lead[0] == original["lead"][0] and run[1] == original["run"][1]
    assert lead[1] == chamfer_seg[0] and chamfer_seg[1] == run[0]
    assert not repairer.off_angle_tracks()
    for name, (start, end) in zip(shapes, (lead, chamfer_seg, run), strict=True):
        assert _is_quantum_aligned(end[0] - start[0], end[1] - start[1]), name
    copper, _ = _saved_copper(board, "chamfer")
    assert copper.distance(_pad_poly(board, "R1/1")) >= 0.09 - 1e-3


# ---------------------------------------------------------------------------
# Issue #9939: an elbow that hugs the pad edge takes its mirror elbow
# ---------------------------------------------------------------------------

_ELBOW_OPTS = {"max_displacement": 1.0, "clearance_mm": 0.127, "warning_clearance_mm": 0.15}


def _overlap_pass_through(conductor: str):
    finding = _finding(conductor, "same-net pass-through")
    finding.required_value_mm = 0.09
    finding.actual_value_mm = 0.0
    return finding


def _elbow_fixture(tmp_path, *, turn_start=(98.9, 99.5), end=(99.5, 100.1), extra="", lock=""):
    """A track along the pad's top edge (y = 99.5) that turns in at the corner.

    ``anchor`` (98.5, 99.5) and ``end`` stay put; the two legs trade places, so
    the corner moves from (98.9, 99.5) to (99.1, 100.1) with the default end.
    """
    shapes = {
        "hug": ((98.5, 99.5), (98.9, 99.5)),
        "turn": (turn_start, end),
    }
    tracks = [
        _seg(name, a, b).replace(
            "(segment ", f"(segment {lock} " if name == "turn" else "(segment "
        )
        for name, (a, b) in shapes.items()
    ]
    return _board(tmp_path, "\n".join([*tracks, extra]))


@pytest.mark.parametrize(
    "turn_start, hug_end",
    [((98.9, 99.5), (99.1, 100.1)), ((98.905, 99.505), (99.095, 100.095))],
)
def test_elbow_flip_moves_the_corner_into_the_pad_keeping_every_angle_and_undoes(
    tmp_path, turn_start, hug_end
):
    board = _elbow_fixture(tmp_path, turn_start=turn_start)
    before = board.read_bytes()
    original = {n: _end(board, n) for n in ("hug", "turn")}
    repairer = MaskCopperRepairer(board)
    assert (
        repairer.repair([_overlap_pass_through("hug")], dry_run=True, **_ELBOW_OPTS).repaired == 1
    )
    assert board.read_bytes() == before

    result = repairer.repair([_overlap_pass_through("hug")], **_ELBOW_OPTS)
    assert result.repaired == 1
    (action,) = result.actions
    assert action.action == "nudge_clear" and len(action.points) == 2
    repairer.save(board)
    hug, turn = _end(board, "hug"), _end(board, "turn")
    # Both outer ends stay fixed; the shared corner is now strictly inside the pad.
    assert hug[0] == original["hug"][0] and turn[1] == original["turn"][1]
    assert hug[1] == hug_end and turn[0] == (99.1, 100.1)
    inside = _pad_poly(board, "R1/1").buffer(-0.015)
    assert inside.contains(Point(hug[1])) and inside.contains(Point(turn[0]))
    # The legs only traded places: lengths and angles are unchanged.
    assert not repairer.off_angle_tracks()
    assert math.dist(*hug) == pytest.approx(math.dist(*original["turn"]))
    assert math.dist(*turn) == pytest.approx(math.dist(*original["hug"]))

    assert repairer.undo_action(action)
    repairer.save(board)
    assert {n: _end(board, n) for n in original} == original


def test_elbow_selective_undo_preserves_both_authored_ends_when_other_action_is_saved(
    tmp_path, monkeypatch, capsys
):
    from collections import Counter

    board = _board2(
        tmp_path,
        "\n".join(
            [
                _seg("hug", (98.5, 99.5), (98.9, 99.5)),
                _seg("turn", (98.905, 99.505), (99.5, 100.1)),
                _seg("entry", (115, 100.3), (120.7, 100.3)),
            ]
        ),
        "120 100",
    )
    original = {uid: _end(board, uid) for uid in ("hug", "turn")}
    elbow = _overlap_pass_through("hug")
    entry = _own_finding("entry", "pad-r2-1", "R2/1")
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_state", lambda path: (Counter(), 0))
    _run(
        monkeypatch,
        board,
        [_report(elbow, entry), _report(elbow)],
        extra=("--format", "json", "--max-passes", "1"),
    )
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert [a["segment_uuid"] for a in result["actions"]] == ["entry"]
    assert [(u["uuid"], u["reason"]) for u in result["unrepaired"]] == [
        ("hug", "finding_not_cleared")
    ]
    assert _end(board, "entry")[1] == (120, 100)
    assert {uid: _end(board, uid) for uid in original} == original


@pytest.mark.timeout(180)
@pytest.mark.parametrize("pad_clearance, permitted", [(0.32, False), (0.20, True)])
def test_elbow_keeps_native_clearance_when_pad_connectivity_is_disabled(
    tmp_path, monkeypatch, capsys, pad_clearance, permitted
):
    from kicad_tools.cli.kicad_toolchain import evidence_toolchain
    from kicad_tools.drc.geometric import run_geometric_drc

    # The flipped Signal track misses the copper-only TP1 pad by 0.2884 mm.
    fixture = Path(__file__).parent / "fixtures" / "mask_elbow_clearance.kicad_pcb"
    board = tmp_path / fixture.name
    board.write_text(
        fixture.read_text().replace("(clearance 0.32)", f"(clearance {pad_clearance})")
    )
    board.with_suffix(".kicad_pro").write_text("{}")
    original = board.read_bytes()

    def unexpected_pad_check(*args):
        pytest.fail("--no-connectivity-check requested a pad partition")

    monkeypatch.setattr(fix_drc_cmd, "_mask_pad_islands", unexpected_pad_check)
    monkeypatch.setattr(fix_drc_cmd, "_mask_native_pad_groups", unexpected_pad_check)
    with evidence_toolchain(mounts=[tmp_path]):
        fix_drc_cmd.main(
            [
                str(board),
                "--only",
                "mask-copper",
                "--mfr",
                "jlcpcb-tier1",
                "--max-displacement",
                "1",
                "--max-passes",
                "1",
                "--no-connectivity-check",
                "--format",
                "json",
            ]
        )
        saved = run_geometric_drc(board, refill_zones=False, all_track_errors=True)
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert saved.ran and all(v.type_str != "clearance" for v in saved.error_violations)
    assert result["repaired"] == int(permitted)
    if permitted:
        assert _end(board, "e1dda6eb-ce7b-52c5-b8ff-623efb07a385")[1] == (52.6, 40.2)
    else:
        assert "1 new errors" in result["unrepaired"][0]["detail"]
        assert board.read_bytes() == original


def test_elbow_flip_declines_when_the_mirror_corner_stays_outside_the_pad(tmp_path):
    # The mirror corner (98.9, 99.9) is left of the pad edge (x = 99).
    board = _elbow_fixture(tmp_path, end=(99.3, 99.9))
    repairer = MaskCopperRepairer(board)
    before = repairer.doc.to_string()
    result = repairer.repair([_overlap_pass_through("hug")], **_ELBOW_OPTS)
    assert result.repaired == 0
    assert [u.reason for u in result.unrepaired] == ["not_own_pad_entry"]
    assert not repairer.modified and repairer.doc.to_string() == before


def _stub_elbow_fixture(tmp_path, *, anchor_y=100.9, extra="", lock=""):
    """A track hugging the pad's left edge (x = 99) that hands over to a stub.

    ``hug`` runs up the edge to y = 100 and ``turn`` is the 0.25 mm stub into
    the pad. Swapping the legs puts the corner at (99.2, ``anchor_y``) below the
    pad, so the entry is a vertical track and the corner stays outside the pad.
    """
    tracks = [
        _seg("hug", (98.95, anchor_y), (98.95, 100.0)),
        _seg("turn", (98.95, 100.0), (99.2, 100.0)).replace(
            "(segment ", f"(segment {lock} " if lock else "(segment "
        ),
    ]
    return _board(tmp_path, "\n".join([*tracks, extra]))


def test_elbow_flip_takes_a_corner_that_stays_below_the_pad_and_undoes(tmp_path):
    board = _stub_elbow_fixture(tmp_path)
    before = board.read_bytes()
    original = {n: _end(board, n) for n in ("hug", "turn")}
    repairer = MaskCopperRepairer(board)
    assert (
        repairer.repair([_overlap_pass_through("hug")], dry_run=True, **_ELBOW_OPTS).repaired == 1
    )
    assert board.read_bytes() == before

    result = repairer.repair([_overlap_pass_through("hug")], **_ELBOW_OPTS)
    assert result.repaired == 1
    (action,) = result.actions
    repairer.save(board)
    hug, turn = _end(board, "hug"), _end(board, "turn")
    # The outer ends stay put; the corner moves below the pad, outside it.
    assert hug[0] == original["hug"][0] and turn[1] == original["turn"][1]
    assert hug[1] == turn[0] == (99.2, 100.9)
    pad = _pad_poly(board, "R1/1")
    assert not pad.buffer(-0.015).contains(Point(hug[1]))
    # The entry runs straight into the pad; the other leg clears its opening.
    assert turn[0][0] == turn[1][0] and pad.buffer(-0.015).contains(Point(turn[1]))
    assert LineString(hug).buffer(0.1).distance(pad) >= 0.09 + 0.02 - 1e-9
    assert not repairer.off_angle_tracks()
    assert math.dist(*hug) == pytest.approx(math.dist(*original["turn"]))

    assert repairer.undo_action(action)
    repairer.save(board)
    assert {n: _end(board, n) for n in original} == original


@pytest.mark.parametrize(("slack", "expected"), [(0.21, True), (0.22, False)])
def test_outside_elbow_respects_explicit_margin_at_the_boundary(tmp_path, slack, expected):
    board = _stub_elbow_fixture(tmp_path)
    repairer = MaskCopperRepairer(board)
    before = repairer.doc.to_string()
    result = repairer.repair([_overlap_pass_through("hug")], margin_mm=slack, **_ELBOW_OPTS)
    assert result.repaired == int(expected)
    if expected:
        assert "straight entry" in result.actions[0].detail
    else:
        assert not repairer.modified and repairer.doc.to_string() == before


@pytest.mark.parametrize(
    ("board_margin", "fp_margin", "pad_margin", "version", "expected"),
    [
        (0.0, None, 0.02, 20240108, False),
        (0.02, None, None, 20240108, False),
        (0.0, 0.02, None, 20240108, False),
        (0.0, 0.02, 0.0, 20240108, False),
        (0.0, 0.02, 0.0, 20260206, True),
        (0.02, None, -0.02, 20260206, True),
    ],
)
def test_outside_elbow_uses_the_effective_mask_opening(
    tmp_path, board_margin, fp_margin, pad_margin, version, expected
):
    # At y=100.72 the flipped 0.2 mm trace clears the copper by 0.12 mm.
    board = _stub_elbow_fixture(tmp_path, anchor_y=100.72)
    source = board.read_text().replace("20240108", str(version))
    source = source.replace("pad_to_mask_clearance 0", f"pad_to_mask_clearance {board_margin}")
    if fp_margin is not None:
        source = source.replace(
            '(property "Reference"', f'(solder_mask_margin {fp_margin}) (property "Reference"'
        )
    if pad_margin is not None:
        source = source.replace(
            f'(uuid "{PAD}")', f'(solder_mask_margin {pad_margin}) (uuid "{PAD}")'
        )
    board.write_text(source)
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_overlap_pass_through("hug")], **_ELBOW_OPTS)
    assert result.repaired == int(expected)
    if not expected:
        assert not repairer.modified


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    ("expansion", "margin", "expected"),
    [(0.0, None, True), (0.015, None, False), (0.0, 0.03, True), (0.0, 0.031, False)],
)
def test_verified_outside_elbow_clears_the_plotted_opening(
    tmp_path, capsys, expansion, margin, expected
):
    from kicad_tools.cli.kicad_toolchain import evidence_toolchain

    fixture = Path(__file__).parent / "fixtures" / "mask_elbow_outside_pad.kicad_pcb"
    board = tmp_path / fixture.name
    lead = "e1dda6eb-ce7b-52c5-b8ff-623efb07a385"
    board.write_text(
        fixture.read_text().replace("(solder_mask_margin 0)", f"(solder_mask_margin {expansion})")
    )
    board.with_suffix(".kicad_pro").write_text("{}")
    before = board.read_bytes()
    with evidence_toolchain([tmp_path]):
        fix_drc_cmd.main(
            [
                str(board),
                "--only",
                "mask-copper",
                "--mfr",
                "jlcpcb-tier1",
                "--max-displacement",
                "2",
                "--max-passes",
                "1",
                "--format",
                "json",
                *(["--margin", str(margin)] if margin is not None else []),
            ]
        )
        report = json.loads(capsys.readouterr().out)["mask_copper"]
        checked = fix_drc_cmd._run_python_drc(board, manufacturer="jlcpcb-tier1", mask_copper=True)
    assert report["repaired"] == int(expected)
    assert checked is not None
    measured = next(
        v for v in checked.violations if v.type == ViolationType.MASK_TO_COPPER and lead in v.items
    )
    if expected:
        assert measured.actual_value_mm == pytest.approx(0.12, abs=1e-6)
    else:
        assert measured.is_error and board.read_bytes() == before


@pytest.mark.timeout(180)
def test_saved_elbow_rolls_back_a_gap_beyond_the_usual_advisory_range(
    tmp_path, capsys, monkeypatch
):
    from shapely.geometry import box  # type: ignore[import-untyped]

    from kicad_tools.cli.kicad_toolchain import evidence_toolchain

    fixture = Path(__file__).parent / "fixtures" / "mask_elbow_outside_pad.kicad_pcb"
    board = tmp_path / fixture.name
    board.write_text(
        fixture.read_text()
        .replace("40.82", "41.0")
        .replace("(solder_mask_margin 0)", "(solder_mask_margin 0.015)")
    )
    board.with_suffix(".kicad_pro").write_text("{}")
    before = board.read_bytes()
    # Give the predictor the unexpanded land: it will propose an elbow with a
    # 0.300 mm modeled gap. The native plot sees 0.285 mm, beyond the ordinary
    # 0.150 mm warning range but short of the requested 0.090 + 0.210 mm.
    monkeypatch.setattr(
        MaskCopperRepairer,
        "_elbow_mask_apertures",
        {("ad22088e-c1cf-5aaf-8d08-4a5294d93e8f", "F.Cu"): box(52.3, 39.6, 54.0, 40.6)},
    )
    with evidence_toolchain([tmp_path]):
        fix_drc_cmd.main(
            [
                str(board),
                "--only",
                "mask-copper",
                "--mfr",
                "jlcpcb-tier1",
                "--margin",
                "0.21",
                "--max-displacement",
                "2",
                "--max-passes",
                "1",
                "--format",
                "json",
            ]
        )
    report = json.loads(capsys.readouterr().out)["mask_copper"]
    assert report["repaired"] == 0
    assert any(item["reason"] == "mask_margin_not_met" for item in report["unrepaired"])
    assert board.read_bytes() == before


def test_elbow_flip_below_the_pad_declines_when_its_lead_leg_stays_in_the_opening(tmp_path):
    # The swapped lead leg would run along y = 100.7, 0.1 mm under the pad's
    # edge: inside the mask gap, so the swap only trades one finding for another.
    board = _stub_elbow_fixture(tmp_path, anchor_y=100.7)
    repairer = MaskCopperRepairer(board)
    before = repairer.doc.to_string()
    result = repairer.repair([_overlap_pass_through("hug")], **_ELBOW_OPTS)
    assert result.repaired == 0
    assert [u.reason for u in result.unrepaired] == ["not_own_pad_entry"]
    assert not repairer.modified and repairer.doc.to_string() == before


def test_elbow_flip_below_the_pad_vetoed_by_foreign_copper_or_a_lock_restores_everything(
    tmp_path,
):
    cases = {
        # Foreign copper 0.1 mm from the swapped lead leg (clearance floor 0.127).
        "foreign-copper": (_seg("foreign", (99.0, 101.2), (99.4, 101.2), net=2), ""),
        "locked-neighbour": ("", "locked"),
    }
    for name, (extra, lock) in cases.items():
        sub = tmp_path / name
        sub.mkdir()
        board = _stub_elbow_fixture(sub, extra=extra, lock=lock)
        repairer = MaskCopperRepairer(board)
        before = repairer.doc.to_string()
        result = repairer.repair([_overlap_pass_through("hug")], **_ELBOW_OPTS)
        assert result.repaired == 0, name
        assert not repairer.modified and repairer.doc.to_string() == before, name


def test_verified_elbow_flip_that_leaves_the_finding_restores_every_end(
    tmp_path, monkeypatch, capsys
):
    board = _stub_elbow_fixture(tmp_path)
    before = board.read_bytes()
    elbow = _overlap_pass_through("hug")
    _run(monkeypatch, board, [_report(elbow), _report(elbow)], extra=("--format", "json"))
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert [(u["uuid"], u["reason"]) for u in result["unrepaired"]] == [
        ("hug", "finding_not_cleared")
    ]
    assert board.read_bytes() == before


def test_elbow_flip_vetoed_by_foreign_copper_a_lock_or_a_branch_restores_everything(tmp_path):
    cases = {
        # Foreign copper 0.012 mm from the mirror leg (clearance floor 0.127).
        "foreign-copper": (_seg("foreign", (98.6, 99.9), (98.6, 100.0), net=2), ""),
        "locked-neighbour": ("", "locked"),
        # A third same-net track at the corner would have to be dragged with it.
        "branch-at-corner": (_seg("branch", (98.9, 99.5), (98.9, 98.5)), ""),
    }
    for name, (extra, lock) in cases.items():
        sub = tmp_path / name
        sub.mkdir()
        board = _elbow_fixture(sub, extra=extra, lock=lock)
        repairer = MaskCopperRepairer(board)
        before = repairer.doc.to_string()
        result = repairer.repair([_overlap_pass_through("hug")], **_ELBOW_OPTS)
        assert result.repaired == 0, name
        assert not repairer.modified and repairer.doc.to_string() == before, name


def test_via_waits_a_pass_when_an_elbow_flip_already_moved_a_track_that_reaches_it(tmp_path):
    # The anchor of the elbow is a via. Flipping the elbow edits the track that
    # ends at the via; moving the via in the same pass would edit it twice.
    board = _elbow_fixture(tmp_path, extra=_via("anchor-via", (98.5, 99.5)))
    repairer = MaskCopperRepairer(board)
    findings = [_overlap_pass_through("hug"), _pt_finding("anchor-via", measured=0.02)]
    result = repairer.repair(findings, **_ELBOW_OPTS)
    assert [a.uuid for a in result.actions] == ["hug"]
    assert [(u.uuid, u.reason) for u in result.unrepaired] == [
        ("anchor-via", "segment_end_already_moved")
    ]


def _via_pad_entry(tmp_path, *, locked=False):
    via, corner, inside = (100.4, 98.975), (100.4, 99.375), (100.0, 99.775)
    entry = _seg("entry", corner, inside)
    if locked:
        entry = entry.replace("(segment ", "(segment locked ")
    return _board(tmp_path, "\n".join([_via("fixed", via), _seg("lead", via, corner), entry]))


def test_disjoint_via_approach_turns_outside_pad_and_undoes(tmp_path):
    # The lead from a fixed via ends 0.025 mm from the mask. Its diagonal
    # neighbour already enters the pad. Extend that diagonal to a perpendicular via lead.
    board = _via_pad_entry(tmp_path)
    repairer = MaskCopperRepairer(board)
    original = {uid: _end(board, uid) for uid in ("lead", "entry")}
    via, inside = original["lead"][0], original["entry"][1]
    result = repairer.repair([_pt_finding("lead", measured=0.025)], **_ELBOW_OPTS)
    assert result.repaired == 1
    repairer.save(board)
    assert _end(board, "lead") == [via, (100.6, 99.175)]
    assert _end(board, "entry") == [(100.6, 99.175), inside]
    assert _saved_copper(board, "fixed")[1] == via
    assert not repairer.off_angle_tracks()
    assert repairer.undo_action(result.actions[0])
    repairer.save(board)
    assert {uid: _end(board, uid) for uid in original} == original


def _tangent_via_entry(tmp_path):
    # The tap just meets the round cap of the diagonal entry. Moving the
    # opposite end farther along its line leaves that cap in the same place.
    via, corner, inside = (100.375, 99.0), (100.375, 99.375), (100.0, 99.75)
    return _board(
        tmp_path,
        "\n".join(
            [
                _via("fixed", via),
                _seg("lead", via, corner),
                _seg("entry", corner, inside),
                _seg("tap", (99.775, 99.75), (99.5, 99.75), width=0.25),
            ]
        ),
    )


def test_via_entry_extension_preserves_tangent_at_its_fixed_end(tmp_path):
    board = _tangent_via_entry(tmp_path)
    old_lead_ends = _end(board, "lead")
    old_entry, old_ends = _saved_copper(board, "entry")
    tap_copper, tap_ends = _saved_copper(board, "tap")
    assert old_entry.intersects(tap_copper)
    assert old_entry.intersection(tap_copper).geom_type == "Point"
    before = board.read_bytes()
    repairer = MaskCopperRepairer(board)
    finding = _pt_finding("lead", measured=0.025)
    assert repairer.repair([finding], dry_run=True, **_ELBOW_OPTS).repaired == 1
    assert board.read_bytes() == before

    result = repairer.repair([finding], **_ELBOW_OPTS)
    (action,) = result.actions
    repairer.save(board)
    entry, ends = _saved_copper(board, "entry")
    assert ends[1] == old_ends[1]
    assert entry.covers(old_entry) and entry.intersects(tap_copper)
    assert tuple(_end(board, "tap")) == tap_ends
    assert _pad_poly(board, "R1/1").distance(_saved_copper(board, "lead")[0]) >= 0.11
    assert not repairer.off_angle_tracks()
    assert repairer.undo_action(action)
    repairer.save(board)
    assert tuple(_end(board, "entry")) == old_ends
    assert _end(board, "lead") == old_lead_ends


def test_locked_via_entry_corner_leaves_all_copper_untouched(tmp_path):
    repairer = MaskCopperRepairer(_via_pad_entry(tmp_path, locked=True))
    before = repairer.doc.to_string()
    result = repairer.repair([_pt_finding("lead", measured=0.025)], **_ELBOW_OPTS)
    assert result.repaired == 0 and result.unrepaired[0].reason == "junction_at_moved_end"
    assert repairer.doc.to_string() == before


@pytest.mark.parametrize("make_board", [_via_pad_entry, _tangent_via_entry])
def test_via_entry_recheck_veto_preserves_board_bytes(tmp_path, monkeypatch, capsys, make_board):
    board = make_board(tmp_path)
    before = board.read_bytes()
    reported = _pt_finding("lead", measured=0.025)
    neighbour = _finding("entry")
    _run(monkeypatch, board, [_report(reported), _report(neighbour)], extra=("--format", "json"))
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert [(u["uuid"], u["reason"]) for u in result["unrepaired"]] == [
        ("lead", "new_mask_finding")
    ]
    assert board.read_bytes() == before


# ---------------------------------------------------------------------------
# Issue #9939: a disjoint pass-through jog slides along its runs (Board 03 C7/1)
# ---------------------------------------------------------------------------


def _disjoint_jog_fixture(tmp_path, *, extra="", lock=""):
    """A jog between two vertical runs whose lower end stops 0.05 mm short of the pad.

    The pad's top edge is y = 99.5 and the lower run enters it at x = 100.6, so
    the jog's near end (100.6, 99.35) sits 0.05 mm from the opening: a disjoint
    pass-through finding, not an own-pad entry.
    """
    shapes = {
        "run": ((100.2, 96.0), (100.2, 98.95)),
        "jog": ((100.2, 98.95), (100.6, 99.35)),
        "lead": ((100.6, 99.35), (100.6, 100.0)),
    }
    tracks = [
        _seg(name, a, b).replace("(segment ", f"(segment {lock} " if name == "run" else "(segment ")
        for name, (a, b) in shapes.items()
    ]
    return _board(tmp_path, "\n".join([*tracks, extra])), shapes


def test_disjoint_pass_through_jog_slides_along_its_runs_without_changing_an_angle(tmp_path):
    board, shapes = _disjoint_jog_fixture(tmp_path)
    before = board.read_bytes()
    original = {name: _end(board, name) for name in shapes}
    repairer = MaskCopperRepairer(board)
    finding = _pt_finding("jog", measured=0.05)
    assert repairer.repair([finding], dry_run=True, **_JOG_OPTS).repaired == 1
    assert board.read_bytes() == before

    result = repairer.repair([finding], **_JOG_OPTS)
    assert result.repaired == 1
    (action,) = result.actions
    assert "jog" in action.detail and action.dx == 0 and action.dy < 0
    # A disjoint finding needs only the last few hundredths of a millimetre.
    assert abs(action.dy) <= 0.25
    repairer.save(board)
    assert not repairer.off_angle_tracks()
    run, jog, lead = (_end(board, name) for name in ("run", "jog", "lead"))
    # The downstream anchor and the pad end never move; the joints follow the jog.
    assert run[0] == original["run"][0] and lead[1] == original["lead"][1]
    assert run[1] == jog[0] and jog[1] == lead[0]
    assert jog[1][0] - jog[0][0] == pytest.approx(0.4) and jog[1][1] - jog[0][1] == pytest.approx(
        0.4
    )
    copper, _ = _saved_copper(board, "jog")
    assert copper.distance(_pad_poly(board, "R1/1")) >= 0.09

    assert repairer.undo_action(action)
    repairer.save(board)
    assert {name: _end(board, name) for name in shapes} == original


def test_disjoint_jog_slide_is_not_tried_beyond_its_bound(tmp_path):
    # The slide bound, not the geometry, declines: the jog needs 0.04 mm and gets 0.01 mm.
    board, _ = _disjoint_jog_fixture(tmp_path)
    repairer = MaskCopperRepairer(board)
    pads = repairer._openings()
    slid = repairer._slide_jog_along_runs(
        repairer._segment_nodes()["jog"],
        "jog",
        PAD,
        pads,
        MaskRepairResult(),
        0.01,
        False,
        0.127,
        0.09,
        0.02,
        0.15,
        set(),
    )
    assert slid is False and not repairer.modified


def test_disjoint_jog_slide_vetoed_by_foreign_copper_or_a_lock_leaves_the_slide_unapplied(tmp_path):
    cases = {
        # Other-net copper where the jog's near end would land.
        "foreign-copper": (_seg("foreign", (100.6, 99.0), (100.9, 99.0), net=2), ""),
        "locked-run": ("", "locked"),
    }
    for name, (extra, lock) in cases.items():
        sub = tmp_path / name
        sub.mkdir()
        board, _ = _disjoint_jog_fixture(sub, extra=extra, lock=lock)
        repairer = MaskCopperRepairer(board)
        before = repairer.doc.to_string()
        slid = repairer._slide_jog_along_runs(
            repairer._segment_nodes()["jog"],
            "jog",
            PAD,
            repairer._openings(),
            MaskRepairResult(),
            0.25,
            True,
            0.127,
            0.09,
            0.02,
            0.15,
            set(),
        )
        assert slid is False, name
        assert repairer.doc.to_string() == before, name


def test_verified_disjoint_jog_slide_that_creates_a_finding_restores_every_end(
    tmp_path, monkeypatch, capsys
):
    board, shapes = _disjoint_jog_fixture(tmp_path)
    before = board.read_bytes()
    reported = _pt_finding("jog", measured=0.05)
    neighbour = _finding("lead")  # the slide creates a new finding on a joined run
    _run(monkeypatch, board, [_report(reported), _report(neighbour)], extra=("--format", "json"))
    result = json.loads(capsys.readouterr().out)["mask_copper"]
    assert [(u["uuid"], u["reason"]) for u in result["unrepaired"]] == [("jog", "new_mask_finding")]
    assert board.read_bytes() == before


# ---------------------------------------------------------------------------
# Issue #10217: the repair caller clamps a disjoint pass-through slide to 0.25 mm
# ---------------------------------------------------------------------------


def _disjoint_chamfer_fixture(tmp_path):
    """A 45 degree chamfer between a vertical and a horizontal run, short of the pad.

    The pad's top edge is y = 99.5 and its right edge x = 101. The chamfer
    runs from (100.6, 98.95) to (101.0, 99.35), so its far end sits beside the
    pad's top-right corner without entering it: a disjoint pass-through
    finding. Neither run reaches the pad, so the chamfer is not an own-pad entry.
    """
    shapes = {
        "run": ((100.6, 96.0), (100.6, 98.95)),
        "chamfer": ((100.6, 98.95), (101.0, 99.35)),
        "lead": ((101.0, 99.35), (104.0, 99.35)),
    }
    tracks = [_seg(name, a, b) for name, (a, b) in shapes.items()]
    return _board(tmp_path, "\n".join(tracks)), shapes


@pytest.mark.parametrize(
    ("fixture", "conductor", "required", "slide_method"),
    [
        (_disjoint_jog_fixture, "jog", 0.285, "_slide_jog_along_runs"),
        (_disjoint_chamfer_fixture, "chamfer", 0.165, "_slide_chamfer_between_runs"),
    ],
)
def test_disjoint_pass_through_slide_longer_than_the_bound_falls_back_to_the_nudge(
    tmp_path, fixture, conductor, required, slide_method
):
    # Each conductor needs a 0.255 mm slide, one 0.005 mm step beyond the bound.
    board, _ = fixture(tmp_path)
    repairer = MaskCopperRepairer(board)
    finding = _pt_finding(conductor, measured=0.05, required=required)
    slide = getattr(repairer, slide_method)
    with patch.object(repairer, slide_method, wraps=slide) as attempt:
        result = repairer.repair([finding], **_JOG_OPTS)
    assert result.repaired == 1 and not result.unrepaired
    (action,) = result.actions
    assert action.action == "nudge_clear"
    assert action.detail.startswith("moved "), action.detail

    # A change smaller than one slide step leaves the same copper. Check the
    # limit the caller supplied while still running the real slide above.
    attempt.assert_called_once()
    call = signature(slide).bind(*attempt.call_args.args, **attempt.call_args.kwargs)
    assert call.arguments["max_displacement"] == 0.25


def test_disjoint_pass_through_chamfer_slides_along_its_runs_and_undoes(tmp_path):
    board, shapes = _disjoint_chamfer_fixture(tmp_path)
    before = board.read_bytes()
    original = {name: _end(board, name) for name in shapes}
    repairer = MaskCopperRepairer(board)
    finding = _pt_finding("chamfer", measured=0.05)
    assert repairer.repair([finding], dry_run=True, **_JOG_OPTS).repaired == 1
    assert board.read_bytes() == before

    result = repairer.repair([finding], **_JOG_OPTS)
    assert result.repaired == 1
    (action,) = result.actions
    assert action.action == "nudge_clear" and "chamfer" in action.detail
    # Within the bound, so the slide (not the nudge) is what repaired it.
    assert 0 < action.displacement_mm <= 0.25
    repairer.save(board)
    assert not repairer.off_angle_tracks()
    run, chamfer, lead = (_end(board, name) for name in ("run", "chamfer", "lead"))
    # Each anchor stays on its own line and the joints stay connected.
    assert run[0] == original["run"][0] and lead[1] == original["lead"][1]
    assert run[1] == chamfer[0] and chamfer[1] == lead[0]
    assert run[1][0] == 100.6 and lead[0][1] == 99.35
    # Still a 45 degree chamfer, now further from the pad opening.
    assert math.isclose(
        abs(chamfer[1][0] - chamfer[0][0]), abs(chamfer[1][1] - chamfer[0][1]), abs_tol=1e-9
    )
    assert chamfer != original["chamfer"]
    copper, _ = _saved_copper(board, "chamfer")
    assert copper.distance(_pad_poly(board, "R1/1")) >= 0.09

    assert repairer.undo_action(action)
    repairer.save(board)
    assert {name: _end(board, name) for name in shapes} == original


def _bonded_via_finding():
    found = _pt_finding("via-bond", measured=0.0)
    found.message = "Mask-to-copper overlap: exposed copper via (net N1); same-net pass-through"
    return found


def test_direct_pad_via_keeps_its_connection_and_undoes_as_one_action(tmp_path):
    from kicad_tools.manufacturers import get_profile

    board = _board(tmp_path, _via("via-bond", (101.20, 100.35)))
    board.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "meta": {"version": 3},
                    "classes": [{"name": "Default"}, {"name": "Power", "track_width": 0.24}],
                    "netclass_patterns": [{"netclass": "Power", "pattern": "N1"}],
                }
            }
        )
    )
    repairer = MaskCopperRepairer(board)
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=2)
    repair = repairer.repair(
        [_bonded_via_finding()], design_rules=rules, clearance_mm=0.15, warning_clearance_mm=0.15
    )
    assert repair.repaired == 1
    action = repair.actions[0]
    assert len(action.added_segments) == 1
    uid, ax, ay, vx, vy, width, layer, net = action.added_segments[0]
    assert layer == "F.Cu" and net == 1 and width == 0.24
    assert _is_quantum_aligned(vx - ax, vy - ay)
    repairer.save(board)
    pad, (via, seat), (bridge, _) = (
        _pad_poly(board, "R1/1"),
        _saved_copper(board, "via-bond"),
        _saved_copper(board, uid),
    )
    assert seat == (vx, vy) and pad.distance(via) >= 0.15
    assert pad.contains(Point(ax, ay)) and bridge.intersects(via) and bridge.intersects(pad)
    assert not MaskCopperRepairer(board).off_angle_tracks()
    assert repairer.undo_action(action)
    repairer.save(board)
    assert _saved_copper(board, "via-bond")[1] == (101.2, 100.35)
    assert uid not in MaskCopperRepairer(board)._segment_nodes()


@pytest.mark.parametrize(
    "obstacle", ["project clearance", "drill spacing", "locked via", "generated via", "contact"]
)
def test_direct_pad_via_veto_preserves_the_copper(tmp_path, obstacle):
    from kicad_tools.manufacturers import get_profile

    copper = _via("via-bond", (101.20, 100.35))
    if obstacle == "project clearance":
        # The closest mask-safe seat leaves 0.175 mm to this rear track.
        copper += _seg("rear", (102.045, 98.0), (102.045, 102.0), layer="B.Cu", net=2)
    elif obstacle == "drill spacing":
        copper += _via("other-drill", (102.22, 100.35), size=0.4, drill=0.3, net=2)
    elif obstacle == "locked via":
        copper += '(group "protected" (uuid "grp") (locked yes) (members "via-bond"))'
    elif obstacle == "generated via":
        copper += "(generated (uuid tuning) (members via-bond))"
    else:
        # Its locked tip touches the original annulus by width on the rear.
        copper += _seg("tap", (100.3, 100.35), (100.95, 100.35), layer="B.Cu").replace(
            "(segment ", "(segment (locked yes) "
        )
    board = _board(tmp_path, copper)
    rules = get_profile("jlcpcb-tier1").get_design_rules(layers=2)
    opts = {
        "max_displacement": 0.28,
        "design_rules": rules,
        "clearance_mm": 0.127,
        "warning_clearance_mm": 0.15,
    }
    if obstacle == "project clearance":
        # First demonstrate that the manufacturer's smaller floor would permit it.
        board.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "net_settings": {
                        "meta": {"version": 3},
                        "classes": [{"name": "Default", "clearance": 0.127}],
                    }
                }
            )
        )
        assert (
            MaskCopperRepairer(board).repair([_bonded_via_finding()], dry_run=True, **opts).repaired
            == 1
        )
        board.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "net_settings": {
                        "meta": {"version": 3},
                        "classes": [
                            {"name": "Default", "clearance": 0.127},
                            {"name": "Power", "clearance": 0.2},
                        ],
                        "netclass_patterns": [{"netclass": "Power", "pattern": "N1"}],
                    }
                }
            )
        )
    before = board.read_bytes()
    repairer = MaskCopperRepairer(board)
    result = repairer.repair([_bonded_via_finding()], **opts)
    assert not result.actions and not repairer.modified
    assert result.unrepaired and board.read_bytes() == before
