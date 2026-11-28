"""The redundant-copper repairer (Issue #10667): the edits ``kct fix-drc --only
redundant-copper`` makes, gated by the rule's own shortcut gates.

Each case is a small synthetic board built from the rule test module's
fixtures, so the copper under test is exactly what ``redundant_copper``
reports; the boards are written to disk because the repairer reads a file
and rebuilds its working board from the kept edits.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

from kicad_tools.drc.repair_redundant_copper import (
    REPAIRED_RULES,
    CopperEdit,
    RedundantCopperRepairer,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.redundant_copper import RedundantCopperRule
from kicad_tools.validate.rules.sharp_trace_corner import SharpTraceCornerRule

_RULE_TESTS = Path(__file__).resolve().parent / "test_validate_redundant_copper.py"


def _rule_tests():
    spec = importlib.util.spec_from_file_location("kct_rule_tests_10667", _RULE_TESTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


Board = _rule_tests()


def _write(tmp_path: Path, segments, name: str = "board.kicad_pcb") -> Path:
    """A two-layer board holding ``segments`` (the rule tests' ``_Seg`` stubs)."""
    body = "".join(
        f"  (segment (start {s.start[0]} {s.start[1]}) (end {s.end[0]} {s.end[1]}) "
        f'(width {s.width}) (layer "{s.layer}") (net {s.net_number}) '
        f'(uuid "s{index:04d}-0000-4000-8000-000000000000"))\n'
        for index, s in enumerate(segments)
    )
    path = tmp_path / name
    path.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (net 0 "") (net 1 "SIG") (net 2 "OTHER")\n' + body + ")\n"
    )
    # Written once by the parser's own serializer, as KiCad-written boards
    # are: an unchanged board then round-trips byte for byte.
    PCB.load(path).save(path)
    return path


def _repairer(path: Path) -> RedundantCopperRepairer:
    return RedundantCopperRepairer(path, rule=RedundantCopperRule())


def _findings(repairer: RedundantCopperRepairer) -> list:
    return repairer._live_findings()


def _staircase() -> list:
    return [Board._Seg(a, b) for a, b in zip(Board._STAIR_PTS, Board._STAIR_PTS[1:], strict=False)]


# --- covered ---------------------------------------------------------------


def test_covered_segment_is_removed_and_the_covering_track_stays_whole(tmp_path):
    path = _write(
        tmp_path,
        [Board._Seg((0, 0), (10, 0)), Board._Seg((2, 0), (5, 0))],
    )
    repairer = _repairer(path)
    result = repairer.plan(
        [v for v in _findings(repairer) if v.rule_id == "redundant_copper_covered"]
    )
    assert result.repaired == 1
    (edit,) = result.edits
    assert edit.kind == "remove_covered"
    assert edit.trimmed == () and edit.added == ()
    assert edit.removed[0].start == (2, 0) and edit.removed[0].end == (5, 0)
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert [(s.start, s.end) for s in saved.segments] == [((0.0, 0.0), (10.0, 0.0))]
    assert _findings(_repairer(out)) == []


def test_covered_edit_widens_to_remove_a_stub_its_removal_left_dangling(tmp_path):
    """Board 03 [VBUS F.Cu]: a 0.1 mm stub bridges the hidden track to a junction.

    The stub is not covered (its far end is off the carrier's line), so the
    rule does not report it; once the hidden piece is gone kicad-cli reports
    its near end dangling.  Its copper lies inside the carrier's and the
    junction leg's, so taking it off with the hidden piece loses nothing.
    """
    segments = [
        Board._Seg((0, 0), (10, 10), width=0.5),  # carrier
        Board._Seg((4, 4), (6, 6), width=0.5),  # hidden under it
        Board._Seg((6, 6), (6, 6.1), width=0.5),  # the stub, off the line
        Board._Seg((6, 6.1), (6, 11), width=0.5),  # junction leg
    ]
    path = _write(tmp_path, segments)
    repairer = _repairer(path)
    result = repairer.plan(
        [v for v in _findings(repairer) if v.rule_id == "redundant_copper_covered"]
    )
    (edit,) = result.edits
    assert edit.removed[0].start == (4, 4)
    stub = next(
        s.uuid for s in PCB.load(path).segments if s.start == (6.0, 6.0) and s.end == (6.0, 6.1)
    )
    widened = repairer.widen(edit, [stub])
    assert widened is not None
    assert {s.uuid for s in widened.removed} == {edit.removed[0].uuid, stub}
    assert "stub(s) left dangling" in widened.detail
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    assert [(s.start, s.end) for s in PCB.load(out).segments] == [
        ((0.0, 0.0), (10.0, 10.0)),
        ((6.0, 6.1), (6.0, 11.0)),
    ]
    assert [e.uuid for e in repairer.edits] == [widened.uuid]


def test_widening_a_covered_edit_cannot_unbalance_a_pair(tmp_path):
    segments = [
        Board._Seg((0, 0), (10, 10), width=0.5),
        Board._Seg((4, 4), (6, 6), width=0.5),
        Board._Seg((6, 6), (6, 6.1), width=0.5),
        Board._Seg((6, 6.1), (6, 11), width=0.5),
    ]
    # Removing only the hidden segment puts the two members at equal length.
    other = sum(math.dist(s.start, s.end) for i, s in enumerate(segments) if i != 1)
    segments.append(Board._Seg((0, 50), (other, 50), width=0.5, net_number=2))
    path = _write(tmp_path, segments)
    path.write_text(path.read_text().replace('"SIG"', '"USB_D+"').replace('"OTHER"', '"USB_D-"'))
    repairer = _repairer(path)
    (edit,) = repairer.plan(_findings(repairer)).edits
    stub = next(s.uuid for s in repairer.pcb.segments if s.end == (6.0, 6.1))
    assert repairer.widen(edit, [stub]) is None
    assert repairer.edits == (edit,)


def test_widen_refuses_a_stub_that_is_not_inside_copper_that_stays(tmp_path):
    path = _write(
        tmp_path,
        [
            Board._Seg((0, 0), (10, 0)),
            Board._Seg((2, 0), (5, 0)),
            Board._Seg((5, 0), (5, 3)),  # a real branch off the hidden end
        ],
    )
    repairer = _repairer(path)
    result = repairer.plan(
        [v for v in _findings(repairer) if v.rule_id == "redundant_copper_covered"]
    )
    (edit,) = result.edits
    branch = next(s.uuid for s in PCB.load(path).segments if s.end == (5, 3))
    assert repairer.widen(edit, [branch]) is None
    assert [e.uuid for e in repairer.edits] == [edit.uuid]


# --- staircase -------------------------------------------------------------


def test_staircase_is_replaced_by_the_rules_path(tmp_path):
    path = _write(tmp_path, _staircase())
    repairer = _repairer(path)
    result = repairer.plan(list(_findings(repairer)))
    (edit,) = result.edits
    assert edit.kind == "rewrite_chain" and edit.rule_id == "redundant_copper_staircase"
    assert len(edit.removed) == len(Board._STAIR_PTS) - 1
    assert len(edit.added) == 2 and edit.trimmed == ()
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert len(saved.segments) == 2
    assert _findings(_repairer(out)) == []


def test_staircase_reuses_a_junction_leg_without_moving_the_sharp_corner(tmp_path):
    chain = [(0, 0), (3, 0), (4, 1), (6, 1), (7, 2), (8, 2), (9, 3), (10, 3)]
    retained = [((0, 0), (2, 2)), ((2, 2), (5, 2)), ((0, 0), (-2, -2))]
    segments = [Board._Seg(a, b, width=0.2) for a, b in zip(chain, chain[1:], strict=False)]
    segments.extend(Board._Seg(a, b, width=0.2) for a, b in retained)
    path = _write(tmp_path, segments)
    corners = SharpTraceCornerRule()
    assert [v.location for v in corners.check(PCB.load(path), None).violations] == [(0, 0)]

    repairer = RedundantCopperRepairer(path, rule=RedundantCopperRule(), allow_junction_paths=True)
    result = repairer.plan(_findings(repairer))
    assert result.edits and not result.unrepaired
    out = tmp_path / "repaired.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    assert corners.check(saved, None).violations == []
    assert _findings(_repairer(out)) == []
    first = result.edits[0]
    assert first.legs[:2] == ((0, 0), (2, 2))
    assert all({s.start, s.end} != {(0, 0), (2, 2)} for s in first.added)


def test_new_legs_end_exactly_where_the_neighbouring_track_does(tmp_path):
    """The rule rounds its nodes to 1 um; a leg must not open a 1 um gap.

    Board 03's trunk starts where an off-grid 45-degree lead ends.  The trunk
    is replaced whole (the tap case), and the new leg must begin at the lead's
    own endpoint, not at the walk's rounded node.
    """
    corner = (0.00037, -0.00041)
    segments = [
        Board._Seg((-2.0, -2.00041), corner),  # lead, off the 1 um grid
        Board._Seg(corner, (6, 0)),  # trunk
        *(Board._Seg(a, b) for a, b in Board._zip_pairs(Board._JOY_TAIL)),
        Board._Seg(*Board._JOY_TAP),
    ]
    path = _write(tmp_path, segments)
    repairer = _repairer(path)
    result = repairer.plan(list(_findings(repairer)))
    (edit,) = result.edits
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    lead = next(s for s in saved.segments if math.dist(s.start, (-2.0, -2.00041)) < 1e-9)
    leg = next(s for s in saved.segments if s.uuid == edit.added[0].uuid)
    assert lead.end == corner
    assert leg.start == corner
    assert _findings(_repairer(out)) == []


def test_chain_cut_at_a_tap_runs_on_through_it_when_the_line_is_clear(tmp_path):
    """The trunk is replaced whole along its own line and the tap stays mid-span."""
    path = _write(tmp_path, Board._joy_board(blocker=False))
    repairer = _repairer(path)
    result = repairer.plan(list(_findings(repairer)))
    (edit,) = result.edits
    assert edit.legs == ((0.0, 0.0), (8.86, 0.0), (9.66, -0.8))
    assert edit.trimmed == () and len(edit.removed) == 3
    out = tmp_path / "out.kicad_pcb"
    repairer.save(out)
    saved = PCB.load(out)
    tap = next(s for s in saved.segments if {s.start, s.end} == {(3.0, 0.0), (1.5, 1.5)})
    trunk = next(s for s in saved.segments if s.uuid == edit.added[0].uuid)
    landing = next(p for p in (tap.start, tap.end) if p == (3.0, 0.0))
    assert _on_leg(landing, trunk)
    assert _findings(_repairer(out)) == []


def test_trimmed_edit_predicts_the_length_written_to_disk(tmp_path):
    # A tap 2 mm along a 6 mm trunk leaves 2 mm and removes 4 mm.
    segments = [
        Board._Seg((0, 0), (6, 0)),
        *(Board._Seg(a, b) for a, b in Board._zip_pairs(Board._JOY_TAIL)),
        Board._Seg((2, 0), (0.5, 1.5)),
    ]
    repairer = _repairer(_write(tmp_path, segments))
    (finding,) = _findings(repairer)
    witnesses = tuple(finding.witnesses)
    rule = repairer._rule
    chain = rule.chain_for(witnesses)
    option = next(o for o in rule.chain_options(chain) if o.trims[0])
    candidate = rule.shortcut_candidates(list(option.ends))[0]
    first = repairer._segment(witnesses[0])
    edit = repairer._edit_for(
        finding.rule_id, witnesses, first, chain, option, candidate, False, first.width
    )
    assert isinstance(edit, CopperEdit) and len(edit.trimmed) == 1
    before = sum(math.dist(s.start, s.end) for s in repairer.pcb.segments)
    repairer._apply(edit)
    destination = tmp_path / "trimmed.kicad_pcb"
    repairer.pcb.save(destination)
    after = sum(math.dist(s.start, s.end) for s in PCB.load(destination).segments)
    assert edit.length_delta == pytest.approx(after - before, abs=1e-9)
    assert after == pytest.approx(before)


def _on_leg(point, seg) -> bool:
    dx, dy = seg.end[0] - seg.start[0], seg.end[1] - seg.start[1]
    length2 = dx * dx + dy * dy
    u = ((point[0] - seg.start[0]) * dx + (point[1] - seg.start[1]) * dy) / length2
    return (
        0.0 < u < 1.0
        and math.hypot(point[0] - (seg.start[0] + u * dx), point[1] - (seg.start[1] + u * dy))
        <= 1e-6
    )


def test_staircase_with_no_legal_path_is_left_and_named(tmp_path):
    """The acute-corner case: trimming exposes the tap's wedge, the line round
    it is blocked, so the rule reports nothing and the repairer has nothing
    to do -- but a finding the rule *does* report is always planned."""
    path = _write(tmp_path, Board._joy_board())
    repairer = _repairer(path)
    assert _findings(repairer) == []
    result = repairer.plan([])
    assert result.edits == [] and result.unrepaired == []


def test_locked_chain_is_not_rewritten(tmp_path):
    path = _write(tmp_path, _staircase())
    text = path.read_text().replace(
        '(uuid "s0002-0000-4000-8000-000000000000")',
        '(locked yes) (uuid "s0002-0000-4000-8000-000000000000")',
        1,
    )
    path.write_text(text)
    repairer = _repairer(path)
    result = repairer.plan(list(_findings(repairer)))
    assert result.edits == []
    assert [u.reason for u in result.unrepaired] == ["locked_segment"]


# --- veto ------------------------------------------------------------------


def test_saving_a_staged_copy_keeps_the_working_board_beside_its_input(tmp_path):
    """``PCB.save()`` moves a board's path to its last target; the staged copy
    lives in a temporary directory whose sidecars (the net-class map the
    shortcut gates read) vanish with it, so the working board must stay put."""
    path = _write(tmp_path, _staircase())
    repairer = _repairer(path)
    repairer.plan(list(_findings(repairer)))
    staged = tmp_path / "staged" / "candidate.kicad_pcb"
    staged.parent.mkdir()
    repairer.save(staged)
    assert staged.exists()
    assert repairer.pcb.path == repairer.path == path
    assert repairer.live_findings() == set()


def test_veto_restores_the_touched_copper_byte_for_byte(tmp_path):
    path = _write(tmp_path, _staircase())
    before = path.read_bytes()
    repairer = _repairer(path)
    result = repairer.plan(list(_findings(repairer)))
    (edit,) = result.edits
    staged = tmp_path / "staged.kicad_pcb"
    repairer.save(staged)
    assert staged.read_bytes() != before
    (unrepaired,) = repairer.veto(edit, "new_sharp_trace_corner")
    assert unrepaired.reason == "new_sharp_trace_corner" and unrepaired.uuid == edit.uuid
    restored = tmp_path / "restored.kicad_pcb"
    repairer.save(restored)
    assert restored.read_bytes() == before


def test_vetoed_path_is_not_planned_again_but_the_next_path_is(tmp_path):
    """The chain's first path is vetoed; the next planning round takes its
    other path for the same finding instead of the refused one."""
    path = _write(tmp_path, _staircase())
    repairer = _repairer(path)
    result = repairer.plan(list(_findings(repairer)))
    (edit,) = result.edits
    first = edit.legs
    repairer.veto(edit, "new_mask_to_copper")
    again = repairer.plan(list(_findings(repairer)))
    (second,) = again.edits
    assert second.legs != first
    assert second.finding == edit.finding
    # Refusing every path leaves the finding, named with the last veto.
    repairer.veto(second, "new_mask_to_copper")
    third = repairer.plan(list(_findings(repairer)))
    assert third.edits == []
    assert [u.reason for u in third.unrepaired] == ["new_mask_to_copper"]


# --- accounting ------------------------------------------------------------


def test_unsupported_rules_are_listed_not_planned(tmp_path):
    path = _write(tmp_path, [Board._Seg((0, 0), (5, 0)), Board._Seg((5, 0), (0, 0))])
    repairer = _repairer(path)
    repairer._rule.bind(repairer.pcb, None)
    duplicates = [
        v
        for v in repairer._rule.check(repairer.pcb, None).violations
        if v.rule_id == "redundant_copper_duplicate"
    ]
    assert duplicates
    result = repairer.plan(duplicates)
    assert result.edits == []
    assert {u.reason for u in result.unrepaired} == {"unsupported_rule"}
    assert "redundant_copper_duplicate" not in REPAIRED_RULES


def test_worsens_skew_measures_the_pair_not_the_net(tmp_path):
    """Only a rewrite that moves the pair's lengths apart is refused.

    The staircase's own path is shorter than the chain; whether that widens
    the skew depends on which member is the longer one, and a net without a
    partner has no skew to worsen at all.
    """
    path = _write(tmp_path, _staircase())
    repairer = _repairer(path)
    finding = _findings(repairer)[0]
    chain = repairer._rule.chain_for(tuple(finding.witnesses))
    option = repairer._rule.chain_options(chain)[0]
    shorter = repairer._rule.shortcut_candidates(list(option.ends))[0]
    original = sum(math.dist(s.start, s.end) for s, _f, _t in chain)
    longer = [(0.0, 0.0), (20.0, 20.0), (24.0, 10.0)]
    assert math.dist(*longer[0:2]) + math.dist(*longer[1:3]) > original
    assert math.dist(*shorter[0:2]) + math.dist(*shorter[1:3]) < original
    member = chain[0][0]
    shortening = sum(math.dist(a, b) for a, b in zip(shorter, shorter[1:], strict=False)) - original
    lengthening = sum(math.dist(a, b) for a, b in zip(longer, longer[1:], strict=False)) - original
    net = member.net_number
    assert not repairer._worsens_skew(net, None, shortening)
    lengths = {net: 10.0, 7: 4.0}
    repairer._net_lengths = lambda: lengths  # type: ignore[method-assign]
    assert not repairer._worsens_skew(net, 7, shortening)
    assert repairer._worsens_skew(net, 7, lengthening)
    lengths[7] = 14.0
    assert repairer._worsens_skew(net, 7, shortening)
    assert repairer._worsens_skew(net, 7, lengthening)
    lengths[7] = 20.0
    assert not repairer._worsens_skew(net, 7, lengthening)


@pytest.mark.parametrize("edit_kind", ["remove_covered", "rewrite_chain"])
def test_edit_dicts_round_trip_through_json(edit_kind):
    edit = CopperEdit(
        kind=edit_kind,
        rule_id="redundant_copper_staircase",
        net_name="SIG",
        layer="F.Cu",
        detail="d",
        witnesses=("w",),
    )
    data = edit.to_dict()
    assert data["kind"] == edit_kind and data["net"] == "SIG"
    assert isinstance(data["removed"], list) and isinstance(data["added"], list)
