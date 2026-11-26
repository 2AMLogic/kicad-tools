"""Issue #9647: a thin branch leaving an endpoint pad is not path evidence.

A declared current path's first thin branch segment can lie wholly inside a
large endpoint pad. ``_bfs_path`` used to add every wholly pad-internal
segment at a route hub as path evidence, so the resolver reported the sense
branch's 0.3 mm neck as the resolved path's minimum width -- and
``path_ampacity`` raised a false "too narrow / would fuse" error on exactly
the nets that carry Kelvin/sense taps.

The branch must be excluded at *both* endpoints, while copper that really is
in series with the route (a pad-internal trunk piece, a stub to a via buried
in the pad) stays in the result.
"""

from __future__ import annotations

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    audit_current_paths,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule

from .test_current_paths import _add_pad_footprint, _seg_len


def _branch_spec(*, source=("J1", "1"), sink=("J2", "1")) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="TRUNK",
        net_name="NET1",
        source=PathEndpoint(*source),
        sink=PathEndpoint(*sink),
        continuous_a=15.0,
    )


def _branch_board(
    *,
    branch_at: str,
    divider: bool = True,
    branch_layer: str = "F.Cu",
    pad_size: float = 5.0,
):
    """A 4.0 mm J1->J2 trunk with a 0.3 mm branch leaving one endpoint pad.

    The branch's first segment ``(ex, ey) -> (ex+1, ey+1)`` lies wholly
    inside that pad's ``pad_size`` copper (its far end is sqrt(2) ~ 1.41 mm
    from the pad centre), so it is wholly pad-internal copper: the branch is
    not a dead-end stub, so the #9494 pruning cannot remove it either.
    """
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    pcb.get_footprint("J1").pads[0].size = (pad_size, pad_size)
    pcb.get_footprint("J2").pads[0].size = (pad_size, pad_size)
    if divider:
        _add_pad_footprint(pcb, ref="R1", x=130, y=70, net="NET1")
    pcb.add_trace(("J1", "1"), (60, 50), width=4.0, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 50), ("J2", "1"), width=4.0, layer="F.Cu", net="NET1")
    ex, ey = (120, 50) if branch_at == "sink" else (20, 50)
    pcb.add_trace((ex, ey), (ex + 1, ey + 1), width=0.3, layer=branch_layer, net="NET1")
    pcb.add_trace((ex + 1, ey + 1), (ex + 1, 70), width=0.3, layer=branch_layer, net="NET1")
    if divider:
        pcb.add_trace((ex + 1, 70), ("R1", "1"), width=0.3, layer=branch_layer, net="NET1")
    return pcb


def _trunk_ids(pcb):
    return {id(s) for s in pcb.segments if s.width == 4.0}


def _branch_ids(pcb):
    return {id(s) for s in pcb.segments if s.width == 0.3}


@pytest.mark.parametrize("branch_at", ["sink", "source"])
@pytest.mark.parametrize("divider", [True, False], ids=["divider_pad", "dangling"])
def test_in_pad_branch_segment_is_not_path_evidence(branch_at, divider):
    pcb = _branch_board(branch_at=branch_at, divider=divider)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == _trunk_ids(pcb)
    assert not _branch_ids(pcb) & {id(s) for s in result.segments}
    assert result.length_mm == pytest.approx(sum(_seg_len(s) for s in pcb.segments[:2]))
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(4.0)


def test_in_pad_branch_on_opposite_layer_is_not_path_evidence():
    # A thru-hole endpoint pad covers *.Cu, so a B.Cu branch's first segment
    # is just as pad-internal as an F.Cu one.
    pcb = _branch_board(branch_at="sink", branch_layer="B.Cu")
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == _trunk_ids(pcb)
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(4.0)


@pytest.mark.parametrize("branch_at", ["sink", "source"])
def test_in_pad_branch_stays_uncovered_audit_evidence(branch_at):
    pcb = _branch_board(branch_at=branch_at)
    audit = audit_current_paths(pcb, [_branch_spec()])
    assert audit.all_resolved
    assert not audit.fully_covered
    assert {id(s) for s in audit.uncovered["NET1"]} == _branch_ids(pcb)


def test_small_pad_branch_is_still_excluded():
    # The default 1.0 mm pad also swallows a short first branch segment.
    pcb = _branch_board(branch_at="sink", divider=True, pad_size=1.0)
    branch = pcb.segments[2]
    branch.start, branch.end = (120, 50), (120.3, 50.2)
    pcb.segments[3].start = (120.3, 50.2)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == _trunk_ids(pcb)


def test_pad_internal_trunk_piece_stays_on_route():
    # The trunk's last span runs on inside the J2 pad to the pad centre:
    # wholly pad-internal copper that genuinely is in series with the route
    # must stay evidence (and keep carrying its 1.0 mm neck).
    pcb = _branch_board(branch_at="sink", divider=False, pad_size=5.0)
    pcb._segments = []
    pcb.add_trace(("J1", "1"), (60, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((60, 50), (118, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((118, 50), (120, 50), width=1.0, layer="F.Cu", net="NET1", dedupe=False)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == {
        id(s) for s in pcb.segments if s.width in (4.0, 1.0)
    }
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(1.0)


def test_stub_to_via_buried_in_pad_stays_on_route():
    # A single via dropped inside the endpoint pad: the stub from the pad
    # centre to the via is wholly pad-internal, but it is the only copper
    # joining the pad to the B.Cu trunk, so it stays evidence.
    pcb = _branch_board(branch_at="sink", divider=False, pad_size=5.0)
    pcb._segments = []
    pcb.add_trace((20, 50), (21, 50), width=1.5, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_via(21, 50, net="NET1", dedupe=False)
    pcb.add_trace((21, 50), (120, 50), width=3.0, layer="B.Cu", net="NET1")
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    stub = next(s for s in pcb.segments if s.width == 1.5)
    trunk = next(s for s in pcb.segments if s.width == 3.0)
    assert {id(s) for s in result.segments} == {id(stub), id(trunk)}
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(1.5)


def test_split_trunk_keeps_whole_in_pad_continuation():
    # Judge review of #9647: a trunk split inside the endpoint pad must keep
    # every buried piece, across segment and width changes -- dropping the
    # final 0.3 mm span reported a 4.0 mm minimum and hid the undersized neck
    # (merge base: 3 segments, 100 mm, 0.3 mm minimum, ampacity exit 2).
    pcb = _branch_board(branch_at="sink", divider=False, pad_size=5.0)
    pcb._segments = []
    pcb.add_trace(("J1", "1"), (118, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((118, 50), (119, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((119, 50), (120, 50), width=0.3, layer="F.Cu", net="NET1", dedupe=False)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == {id(s) for s in pcb.segments}
    assert result.length_mm == pytest.approx(100.0)
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(0.3)


@pytest.mark.parametrize("fcu_first", [True, False], ids=["fcu-first", "bcu-first"])
def test_parallel_arm_keeps_buried_neck_in_any_file_order(fcu_first):
    # Judge review of #9647: a parallel arm the BFS chain did not ride still
    # owns its pad-internal neck. Taking attachments from a single BFS
    # predecessor chain made the verdict depend on file order (F.Cu first:
    # 8 mm effective width, exit 0; B.Cu first: 0.3 mm, exit 2).
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    pcb.get_footprint("J1").pads[0].size = (5.0, 5.0)
    pcb.get_footprint("J2").pads[0].size = (5.0, 5.0)
    fcu = [("F.Cu", (20, 50), (120, 50), 4.0)]
    bcu = [("B.Cu", (20, 50), (118, 50), 4.0), ("B.Cu", (118, 50), (120, 50), 0.3)]
    for layer, start, end, width in fcu + bcu if fcu_first else bcu + fcu:
        pcb.add_trace(start, end, width=width, layer=layer, net="NET1", dedupe=False)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    neck = next(s for s in pcb.segments if s.width == 0.3)
    assert id(neck) in {id(s) for s in result.segments}
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(0.3)


@pytest.mark.parametrize("branch_layer", ["F.Cu", "B.Cu"])
def test_off_centre_tap_sharing_trunk_entry_is_excluded(branch_layer):
    # Judge review of #9647: a sense tap leaving the trunk's own off-centre
    # entry point inside the pad is a branch, not the continuation -- the
    # trunk must not inherit the tap's 0.3 mm neck (2 segments, 100 mm,
    # 4.0 mm minimum) even though the tap's first buried segment touches the
    # trunk's attachment point.
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="R1", x=130, y=70, net="NET1")
    for ref in ("J1", "J2"):
        pcb.get_footprint(ref).pads[0].size = (5.0, 5.0)
    pcb.add_trace(("J1", "1"), (118, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((118, 50), (120, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((118, 50), (118, 51), width=0.3, layer=branch_layer, net="NET1", dedupe=False)
    pcb.add_trace((118, 51), (130, 70), width=0.3, layer=branch_layer, net="NET1", dedupe=False)
    trunk = resolve_current_path(pcb, _branch_spec())
    assert trunk.ok, trunk.reason
    assert {id(s) for s in trunk.segments} == {id(s) for s in pcb.segments if s.width == 4.0}
    assert trunk.length_mm == pytest.approx(100.0)
    assert trunk.min_width is not None and trunk.min_width.width_mm == pytest.approx(4.0)
    sense = resolve_current_path(
        pcb,
        CurrentPathSpec(
            name="SENSE",
            net_name="NET1",
            source=PathEndpoint("J2", "1"),
            sink=PathEndpoint("R1", "1"),
            continuous_a=0.01,
        ),
    )
    assert sense.ok, sense.reason


def _off_centre_board(tap_layer: str, *, external: bool = True):
    """A trunk stopping at (118,50) inside the J2 pad, with a 0.3 mm tap.

    Nothing reaches the J2 pad centre (120,50), so no centre-reaching chain
    exists and the empty-goal fallback governs (#9677). With ``external`` the
    tap continues out of the pad to R1; without it the buried copper ends
    inside the pad.
    """
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="R1", x=130, y=70, net="NET1")
    for ref in ("J1", "J2"):
        pcb.get_footprint(ref).pads[0].size = (5.0, 5.0)
    pcb.add_trace(("J1", "1"), (118, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((118, 50), (118, 51), width=0.3, layer=tap_layer, net="NET1", dedupe=False)
    if external:
        pcb.add_trace((118, 51), (130, 70), width=0.3, layer=tap_layer, net="NET1", dedupe=False)
    return pcb


@pytest.mark.parametrize("tap_layer", ["F.Cu", "B.Cu"])
def test_exiting_off_centre_tap_is_not_path_evidence(tap_layer):
    # #9677: when no copper reaches the pad centre, a buried seed feeding an
    # external branch that leaves the pad where the trunk does not land is
    # that branch's head, not the trunk's continuation -- the trunk must
    # resolve to exactly its own 4.0 mm segment (98 mm).
    pcb = _off_centre_board(tap_layer)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == _trunk_ids(pcb)
    assert result.length_mm == pytest.approx(98.0)
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(4.0)


@pytest.mark.parametrize("tap_layer", ["F.Cu", "B.Cu"])
def test_exiting_off_centre_tap_stays_uncovered_audit_evidence(tap_layer):
    # The same board through the independent post-write audit: both tap
    # segments belong to no declared path, so a trunk-only declaration
    # leaves them uncovered.
    pcb = _off_centre_board(tap_layer)
    audit = audit_current_paths(pcb, [_branch_spec()])
    assert audit.all_resolved
    assert not audit.fully_covered
    assert {id(s) for s in audit.uncovered["NET1"]} == _branch_ids(pcb)


@pytest.mark.parametrize("tap_layer", ["F.Cu", "B.Cu"])
def test_ending_off_centre_seed_keeps_documented_fallback(tap_layer):
    # Control for the documented #9651 fallback: with the external segment
    # removed, the buried copper ends inside the pad and stays the best
    # on-route evidence (2 segments, 99 mm, 0.3 mm minimum). This catches a
    # blanket deletion of seed evidence.
    pcb = _off_centre_board(tap_layer, external=False)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == {id(s) for s in pcb.segments}
    assert result.length_mm == pytest.approx(99.0)
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(0.3)
    audit = audit_current_paths(pcb, [_branch_spec()])
    assert audit.all_resolved
    assert audit.fully_covered


def _plated_spec(*, reverse: bool = False, current_a: float = 13.42) -> CurrentPathSpec:
    """The #10291 two-pad declaration, at its full 13.42 A load."""
    endpoints = (("J2", "1"), ("J1", "1")) if reverse else (("J1", "1"), ("J2", "1"))
    return CurrentPathSpec(
        name="TRUNK",
        net_name="NET1",
        source=PathEndpoint(*endpoints[0]),
        sink=PathEndpoint(*endpoints[1]),
        continuous_a=current_a,
    )


def _plated_terminal_board(
    *,
    split_fcu: bool = False,
    split_bcu: bool = False,
    back_width: float | None = 2.75,
    sense_stub: bool = False,
) -> PCB:
    """The public two-pad fixture of #10291, with subdividable entries.

    J1 is the 3.15 x 3.15 mm roundrect plated terminal at (20, 20) and J2
    the 2.5 x 4.5 mm oval terminal at (20.65, 32.2); each layer runs a
    2.75 mm track from J1's centre through the shared arm landing
    (20.65, 21.5) to J2. Each layer's straight entry from the pad centre
    to the landing can be split at its exact collinear midpoint
    (20.325, 20.75), independently per layer (#10300) -- both copper
    films stay identical, only the vertex count changes. ``sense_stub``
    hangs a 0.3 mm Kelvin tap off the split F.Cu vertex, wholly inside
    the pad before it exits to a third pad.
    """
    pcb = PCB.create(width=60, height=60, center=False)
    centre, bend, end = (20, 20), (20.65, 21.5), (20.65, 32.2)
    for ref, pos, shape, size in [
        ("J1", centre, "roundrect", (3.15, 3.15)),
        ("J2", end, "oval", (2.5, 4.5)),
    ]:
        _add_pad_footprint(pcb, ref=ref, x=pos[0], y=pos[1], net="NET1")
        pad = pcb.footprints[-1].pads[0]
        pad.size, pad.shape = size, shape
    if sense_stub:
        _add_pad_footprint(pcb, ref="R1", x=25, y=15, net="NET1")
    for layer in ("F.Cu", "B.Cu"):
        entry_width = 2.75 if layer == "F.Cu" else back_width
        split = split_fcu if layer == "F.Cu" else split_bcu
        if entry_width is not None:
            if split:
                pcb.add_trace(centre, (20.325, 20.75), width=entry_width, layer=layer, net="NET1")
                pcb.add_trace((20.325, 20.75), bend, width=entry_width, layer=layer, net="NET1")
            else:
                pcb.add_trace(centre, bend, width=entry_width, layer=layer, net="NET1")
        pcb.add_trace(bend, end, width=2.75, layer=layer, net="NET1")
    if sense_stub:
        pcb.add_trace((20.325, 20.75), (20.9, 20.2), width=0.3, layer="F.Cu", net="NET1")
        pcb.add_trace((20.9, 20.2), ("R1", "1"), width=0.3, layer="F.Cu", net="NET1")
    return pcb


def _plated_design_rules() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
        outer_copper_oz=2.0,
        inner_copper_oz=2.0,
    )


@pytest.mark.parametrize("back_width", [None, 0.30, 2.75])
@pytest.mark.parametrize("reverse", [True, False])
def test_matched_entries_inside_plated_terminal(back_width, reverse):
    pcb = _plated_terminal_board(back_width=back_width)
    centre = (20, 20)
    spec = _branch_spec(source=("J2", "1"), sink=("J1", "1")) if reverse else _branch_spec()
    resolution = resolve_current_path(pcb, spec)
    assert resolution.status == "resolved"
    assert {id(s) for s in resolution.segments} == {id(s) for s in pcb.segments}
    groups = [
        group
        for group in resolution.parallel_groups
        if any(s.start == centre for s in group.segments)
    ]
    if back_width is not None:
        assert len(groups) == 1
        assert groups[0].layer_widths() == {"F.Cu": 2.75, "B.Cu": back_width}
    else:
        assert not groups


@pytest.mark.parametrize(
    "split_fcu,split_bcu",
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["unsplit", "fcu-split", "bcu-split", "both-split"],
)
@pytest.mark.parametrize("reverse", [True, False])
def test_split_entry_keeps_plated_terminal_ampacity(split_fcu, split_bcu, reverse):
    # #10300: subdividing a straight in-pad entry at a collinear vertex
    # changes nothing electrically -- both copper films stay identical --
    # so the paired terminal entries must stay one two-layer group: each
    # layer's branch is its chained pieces from the pad centre to the
    # shared landing, and every piece stays covered power-path evidence.
    # Before the fix the split layer's pieces dropped out (3/5 or 4/6
    # segments, 2.75 mm minimum, uncovered-segment warnings, one
    # ampacity error per split entry).
    pcb = _plated_terminal_board(split_fcu=split_fcu, split_bcu=split_bcu)
    spec = _plated_spec(reverse=reverse)
    resolution = resolve_current_path(pcb, spec)
    assert resolution.status == "resolved", resolution.reason
    assert {id(s) for s in resolution.segments} == {id(s) for s in pcb.segments}
    assert resolution.min_width is not None
    assert resolution.min_width.width_mm == pytest.approx(5.50)
    entries = [
        group
        for group in resolution.parallel_groups
        if any(s.start == (20, 20) for s in group.segments)
    ]
    assert len(entries) == 1
    assert entries[0].layer_widths() == {"F.Cu": 2.75, "B.Cu": 2.75}
    audit = audit_current_paths(pcb, [spec])
    assert audit.all_resolved
    assert audit.fully_covered
    report = PathAmpacityRule(specs=[spec]).check(pcb, _plated_design_rules())
    assert report.errors == []
    assert report.warnings == []


@pytest.mark.parametrize("back_width", [None, 0.30], ids=["back-missing", "back-narrow"])
@pytest.mark.parametrize("reverse", [True, False])
def test_split_entry_keeps_terminal_controls(back_width, reverse):
    # The #10292 controls keep their verdicts with the front entry split:
    # removing the back entry still reports the single-layer 2.75 mm, and
    # narrowing it to 0.30 mm still reports 3.05 mm -- both must fail the
    # full-load check, and the split front pieces must stay covered.
    pcb = _plated_terminal_board(split_fcu=True, back_width=back_width)
    spec = _plated_spec(reverse=reverse)
    resolution = resolve_current_path(pcb, spec)
    assert resolution.status == "resolved", resolution.reason
    assert {id(s) for s in resolution.segments} == {id(s) for s in pcb.segments}
    assert resolution.min_width is not None
    expected = 2.75 if back_width is None else 3.05
    assert resolution.min_width.width_mm == pytest.approx(expected)
    audit = audit_current_paths(pcb, [spec])
    assert audit.all_resolved
    assert audit.fully_covered
    assert PathAmpacityRule(specs=[spec]).check(pcb, _plated_design_rules()).errors


@pytest.mark.parametrize("reverse", [True, False])
def test_split_entry_sense_stub_contributes_no_power_path_width(reverse):
    # A separate Kelvin tap hanging off the split entry's own vertex
    # inside the pad is a branch: it stays out of the entry branches (its
    # 0.3 mm must not become the terminal's reported width) and out of
    # the power-path evidence, in both path directions.
    pcb = _plated_terminal_board(split_fcu=True, sense_stub=True)
    spec = _plated_spec(reverse=reverse)
    resolution = resolve_current_path(pcb, spec)
    assert resolution.status == "resolved", resolution.reason
    stub_ids = {id(s) for s in pcb.segments if s.width == 0.3}
    assert stub_ids
    assert not stub_ids & {id(s) for s in resolution.segments}
    assert resolution.min_width is not None
    assert resolution.min_width.width_mm == pytest.approx(5.50)
    entries = [
        group
        for group in resolution.parallel_groups
        if any(s.start == (20, 20) for s in group.segments)
    ]
    assert len(entries) == 1
    assert entries[0].layer_widths() == {"F.Cu": 2.75, "B.Cu": 2.75}
