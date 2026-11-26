"""Same-net ``physical_copper_gap`` repair (issue #9332).

Fixtures are synthetic 2-layer boards under the jlcpcb profile: one rect pad
(R1/1, net N1) centred at (100, 100) spanning x 99..101, y 99.5..100.5, a
same-net fan-out via 0.10 mm off the pad's top edge, and (for the tangent
class) a same-net track at y = 105.3 with a via 0.05 mm off its edge.  Unit
tests build the findings directly (the descriptors ``kct check`` writes into
``items``); the fix-drc end-to-end tests exercise both the stubbed wiring and
the real ``kct check`` rule set with the fab-view gap bands resolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli import fix_drc_cmd
from kicad_tools.drc.repair_copper_gap import CopperGapRepairer, copper_gap_violations
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.sexp import parse_file
from tests.conftest import make_assembly_ready

PAD = "pad-r1-1"
# The descriptors ``validate/copper_refs.py`` writes into a finding's items.
VIA_PAD = "Via@100/100.9:F.Cu-B.Cu:d0.3/s0.6"
VIA_TANGENT = "Via@100/105.75:F.Cu-B.Cu:d0.3/s0.6"
TRACE = "Trace@F.Cu:w0.2:95/105.3~105/105.3"

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
  (gr_rect (start 85 85) (end 120 116) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts"))
  (footprint "test:R1" (layer "F.Cu") (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1.2 -0.8) (end 1.2 0.8) (stroke (width 0.05) (type solid)) (fill no)
      (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1") (uuid "pad-r1-1"))
    (model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.step"
      (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
  )
  (footprint "test:R2" (layer "F.Cu") (at 108 92)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -0.75 -0.75) (end 0.75 0.75) (stroke (width 0.05) (type solid)) (fill no)
      (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1") (uuid "pad-r2-1"))
    (model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.step"
      (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
  )
%s
)
"""

# A same-net via 0.10 mm off its own pad (pad edge y=100.5, via edge y=100.6).
# A different-net via 0.10 mm off the same pad (never touched by the repairer).
DIFF_NET_VIA = (
    '  (via (at 101.4 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-n2"))'
)


def _seg(uuid: str, a, b, net: int = 1, width: float = 0.2, layer: str = "F.Cu") -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net {net}) (uuid "{uuid}"))'
    )


# The net's two pads joined by a 45-degree escape, so only the slit fails
# connectivity-wise on the real-rule boards (the near-miss via dangles off it).
LINK = _seg("trk-link", (100, 100), (108, 92))
PAD_VIA = (
    '  (via (at 100 100.9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
    '(net 1) (uuid "via-p"))\n' + LINK
)

# A same-net track tangent to its own via: track edge y=105.4, via edge y=105.45.
TANGENT = _seg("trk-t", (95, 105.3), (105, 105.3)) + (
    '  (via (at 100 105.75) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-t"))\n'
    + LINK
)
# Sub-micron variant of TANGENT: the file coordinates sit 0.3 um off whole
# micrometres, so the descriptor (TRACE above) quantises 95.0003 -> 95 and
# 105.0003 -> 105 while the segment itself stays sub-micron -- the endpoint
# match must tolerate the quantisation (judge note on PR #9396).
TANGENT_SUBUM = _seg("trk-t", (95.0003, 105.3), (105.0003, 105.3)) + (
    '  (via (at 100 105.75) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-t"))\n'
    + LINK
)
# The judge's short repro on PR #9396: a same-net B.Cu track attached at the
# tangent via's position, plus a foreign B.Cu track it would cross once the
# via snaps onto the F.Cu centreline and drags the attached end with it
# (signed clearance -0.1351 mm between the two nets).
N1_BCUT = _seg("trk-n1b", (100, 105.75), (110, 105.75), layer="B.Cu")
N2_BCUT = _seg("trk-n2b", (103, 103), (103, 105.37), net=2, layer="B.Cu")
# A track ending at the fan-out via's centre: it must move with the via.
ATTACHED = _seg("trk-v", (100, 103), (100, 100.9))


# A same-net obround pad (the shape name ``physical_gap`` collects) beside a
# fan-out via 0.10 mm off the pad copper.  The placement is parameterised:
# the pad's absolute ``at`` angle orients the copper in board coordinates
# while the footprint angle places the pad's local offset (#9399).
def _obround_pad(footprint_at: str, pad_at: str, via_xy: tuple[float, float]) -> str:
    return (
        f'  (footprint "test:R3" (layer "F.Cu") (at {footprint_at})\n'
        '    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS")\n'
        "      (effects (font (size 1 1) (thickness 0.15))))\n"
        '    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")\n'
        "      (effects (font (size 1 1) (thickness 0.15))))\n"
        f'    (pad "1" smd obround (at {pad_at}) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask") '
        '(net 1 "N1") (uuid "pad-r3-1"))\n'
        "  )\n"
        f'  (via (at {via_xy[0]} {via_xy[1]}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net 1) (uuid "via-o"))'
    )


OBROUND = "pad-r3-1"


def _board(tmp_path: Path, copper: str) -> Path:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(BOARD % copper)
    return path


def _finding(
    items: list[str],
    nets: tuple[str, ...] = ("N1",),
    severity: Severity = Severity.ERROR,
    required: float = 0.15,
    actual: float = 0.1,
) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.PHYSICAL_COPPER_GAP,
        type_str="physical_copper_gap",
        severity=severity,
        message=f"Physical copper gap {actual:.4f}mm < {required:.4f}mm",
        locations=[Location(x_mm=100.0, y_mm=100.55, layer="F.Cu")],
        items=list(items),
        nets=list(nets),
        required_value_mm=required,
        actual_value_mm=actual,
    )


def _via(path: Path, uuid: str) -> tuple[float, float]:
    doc = parse_file(path)
    for node in doc.find_all("via"):
        uuid_node = node.find("uuid")
        if uuid_node and str(uuid_node.get_first_atom()).strip('"') == uuid:
            at = node.find("at")
            return float(at.get_atoms()[0]), float(at.get_atoms()[1])
    raise AssertionError(uuid)


def _end(path: Path, uuid: str, tag: str) -> tuple[float, float]:
    doc = parse_file(path)
    for node in doc.find_all("segment"):
        if str(node.find("uuid").get_first_atom()).strip('"') == uuid:
            atoms = node.find(tag).get_atoms()
            return float(atoms[0]), float(atoms[1])
    raise AssertionError(uuid)


# ---------------------------------------------------------------------------
# Unit: via beside its own pad
# ---------------------------------------------------------------------------


def test_via_beside_own_pad_moves_clear_of_the_pad(tmp_path):
    board = _board(tmp_path, PAD_VIA)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([PAD, VIA_PAD])], max_displacement=1.0)
    assert result.repaired == 1 and result.opened == 1 and not result.unrepaired
    repairer.save(board)
    x, y = _via(board, "via-p")
    assert x == 100.0
    # The move opens the measured gap to the finding's 0.15 mm edge + margin.
    assert y == pytest.approx(100.96, abs=0.005)


@pytest.mark.parametrize(
    ("footprint_at", "pad_at", "via_xy", "expected_xy"),
    [
        # Unrotated 2x1 stadium: pad edge y=105.5, via edge y=105.6.
        ("110 105", "0 0", (110.0, 105.9), (110.0, 105.96)),
        # An absolute pad angle of 90 turns the stadium end-on (copper
        # spans x 109.5..110.5, y 104..106), so the slit opens along x.
        ("110 105", "0 0 90", (110.9, 105.0), (110.96, 105.0)),
        # The same board copper from a rotated footprint: the 90-degree
        # footprint angle moves the (2, 0) local offset to (0, -2), putting
        # the pad centre back at (110, 105); the pad angle stays absolute.
        ("110 107 90", "2 0 90", (110.9, 105.0), (110.96, 105.0)),
    ],
)
def test_via_beside_own_obround_pad_is_repaired(
    tmp_path, footprint_at, pad_at, via_xy, expected_xy
):
    # Regression (#9399): the hand-rolled pad model matched "oblong", a shape
    # name KiCad never writes, so obround pad sides were declined
    # ``side_not_found``.  The rule's own ``clearance._pad_polygon`` model is
    # reused now, so the pad resolves and the via is pushed clear -- also
    # when the pad angle or a rotated footprint orients the copper, and the
    # saved coordinates catch a wrong orientation or placement even when
    # the repair reports success.
    board = _board(tmp_path, _obround_pad(footprint_at, pad_at, via_xy))
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps(
        [_finding([OBROUND, f"Via@{via_xy[0]:g}/{via_xy[1]:g}:F.Cu-B.Cu:d0.3/s0.6"])],
        max_displacement=1.0,
    )
    assert result.repaired == 1 and result.opened == 1 and not result.unrepaired
    repairer.save(board)
    x, y = _via(board, "via-o")
    assert x == pytest.approx(expected_xy[0], abs=0.005)
    assert y == pytest.approx(expected_xy[1], abs=0.005)


def test_via_snaps_to_pad_centre_when_a_process_is_declared(tmp_path):
    from kicad_tools.manufacturers.base import DesignRules

    rules = DesignRules(
        min_clearance_mm=0.127,
        min_trace_width_mm=0.127,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.15,
        via_in_pad_supported=True,
        via_in_pad_process_id="pofv",
    )
    board = _board(tmp_path, PAD_VIA)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps(
        [_finding([PAD, VIA_PAD])], max_displacement=1.0, design_rules=rules
    )
    assert result.repaired == 1 and result.snapped_to_pad == 1
    repairer.save(board)
    assert _via(board, "via-p") == (100.0, 100.0)


def test_small_positive_pad_gap_is_repaired_not_treated_as_merged(tmp_path):
    # Regression (judge note on PR #9396): gaps up to 0.005 mm were treated
    # as contact, so a real 0.004 mm slit (the real checker reports
    # 0.004 mm against a 0.15 mm requirement) was declined ``already_merged``
    # and the via left in place.  Only actual copper contact counts as
    # merged now.
    near = _board(tmp_path, PAD_VIA.replace("(at 100 100.9)", "(at 100 100.804)"))
    repairer = CopperGapRepairer(near)
    result = repairer.repair_gaps(
        [_finding([PAD, "Via@100/100.804:F.Cu-B.Cu:d0.3/s0.6"], actual=0.004)],
        max_displacement=1.0,
    )
    assert result.repaired == 1 and result.opened == 1 and not result.unrepaired
    repairer.save(near)
    assert _via(near, "via-p") == (100.0, pytest.approx(100.96, abs=0.005))

    # Actual copper contact (the via's edge exactly on the pad's edge) is
    # still a stale finding.
    contact = _board(tmp_path, PAD_VIA.replace("(at 100 100.9)", "(at 100 100.8)"))
    merged = CopperGapRepairer(contact).repair_gaps(
        [_finding([PAD, "Via@100/100.8:F.Cu-B.Cu:d0.3/s0.6"], actual=0.0)],
        max_displacement=1.0,
    )
    assert merged.repaired == 0
    assert merged.skipped("already_merged") == 1


def test_attached_track_moves_with_the_via_and_undoes(tmp_path):
    board = _board(tmp_path, PAD_VIA + "\n" + ATTACHED)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([PAD, VIA_PAD])], max_displacement=1.0)
    assert result.repaired == 1
    assert result.actions[0].joined == [("trk-v", "end", 100.0, 100.9)]
    repairer.save(board)
    assert _end(board, "trk-v", "end")[1] == pytest.approx(100.96, abs=0.005)
    undo = CopperGapRepairer(board)
    assert undo.undo_move(result.actions[0])
    undo.save(board)
    assert _via(board, "via-p") == (100.0, 100.9)
    assert _end(board, "trk-v", "end") == (100.0, 100.9)


def test_undo_restores_each_ends_own_original_coordinates(tmp_path):
    # Regression (judge note on PR #9396): undo wrote the via's old centre to
    # every joined end, so an end sitting 6 um off the via centre came back
    # moved.  Each end's original coordinates are recorded and restored now.
    attached = _seg("trk-v", (100, 103), (100.006, 100.9))
    board = _board(tmp_path, PAD_VIA + "\n" + attached)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([PAD, VIA_PAD])], max_displacement=1.0)
    assert result.repaired == 1
    assert result.actions[0].joined == [("trk-v", "end", 100.006, 100.9)]
    repairer.save(board)
    undo = CopperGapRepairer(board)
    assert undo.undo_move(result.actions[0])
    undo.save(board)
    assert _via(board, "via-p") == (100.0, 100.9)
    assert _end(board, "trk-v", "end") == (100.006, 100.9)


def test_undo_restores_an_unattached_via(tmp_path):
    board = _board(tmp_path, PAD_VIA)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([PAD, VIA_PAD])], max_displacement=1.0)
    assert repairer.undo_move(result.actions[0])
    repairer.save(board)
    assert _via(board, "via-p") == (100.0, 100.9)


# ---------------------------------------------------------------------------
# Unit: track tangent to its own via
# ---------------------------------------------------------------------------


def test_tangent_track_snaps_the_via_onto_the_centreline(tmp_path):
    board = _board(tmp_path, TANGENT)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)])
    assert result.repaired == 1 and result.snapped_to_track == 1 and not result.unrepaired
    repairer.save(board)
    assert _via(board, "via-t") == (100.0, 105.3)
    # The track itself never moves.
    assert _end(board, "trk-t", "start") == (95.0, 105.3)
    assert _end(board, "trk-t", "end") == (105.0, 105.3)


def test_tangent_track_with_submicron_endpoints_still_snaps(tmp_path):
    # Regression (judge note on PR #9396): exact endpoint equality against the
    # 1 um-quantised descriptor silently declined sub-micron geometry as
    # ``track_not_found`` -- 35% of candidate segments on board 07.
    board = _board(tmp_path, TANGENT_SUBUM)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)])
    assert result.repaired == 1 and result.snapped_to_track == 1 and not result.unrepaired
    repairer.save(board)
    assert _via(board, "via-t") == (100.0, 105.3)
    # The track itself never moves (sub-micron coordinates included).
    assert _end(board, "trk-t", "start") == (95.0003, 105.3)
    assert _end(board, "trk-t", "end") == (105.0003, 105.3)


def test_tangent_track_descriptor_sort_flip_still_resolves(tmp_path):
    # Endpoints whose raw and 1 um-quantised sorts disagree (both x quantise
    # to 100 while the raw x order is opposite to the y order): the descriptor
    # sorts (100/100) first, the raw coordinates sort (100.0, 105.3) first,
    # so the match must try both orderings.
    copper = _seg("trk-f", (100.0004, 100), (100.0, 105.3)) + (
        '  (via (at 100.45 102.65) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net 1) (uuid "via-f"))'
    )
    board = _board(tmp_path, copper)
    result = CopperGapRepairer(board).repair_gaps(
        [
            _finding(
                ["Trace@F.Cu:w0.2:100/100~100/105.3", "Via@100.45/102.65:F.Cu-B.Cu:d0.3/s0.6"],
                required=0.127,
                actual=0.05,
            )
        ]
    )
    assert result.repaired == 1 and result.snapped_to_track == 1 and not result.unrepaired


def test_track_descriptor_resolves_only_on_its_own_layer(tmp_path):
    # Regression (judge note on PR #9396): segment resolution ignored the
    # descriptor's layer, so an identical-geometry B.Cu segment on another
    # net shadowed the named F.Cu segment and the finding was declined
    # ``not_same_net``.
    copper = _seg("trk-b2", (95, 105.3), (105, 105.3), net=2, layer="B.Cu") + "\n" + TANGENT
    board = _board(tmp_path, copper)
    result = CopperGapRepairer(board).repair_gaps(
        [_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)]
    )
    assert result.repaired == 1 and result.snapped_to_track == 1 and not result.unrepaired


def test_foreign_layer_twin_is_not_resolved(tmp_path):
    # Regression (judge note on PR #9396): with only a B.Cu twin present, an
    # F.Cu descriptor still resolved to it -- even on the same net, the
    # repair would act on the wrong layer's copper.
    copper = _seg("trk-t", (95, 105.3), (105, 105.3), layer="B.Cu") + (
        '  (via (at 100 105.75) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net 1) (uuid "via-t"))'
    )
    board = _board(tmp_path, copper)
    result = CopperGapRepairer(board).repair_gaps(
        [_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)]
    )
    assert result.repaired == 0
    assert result.skipped("track_not_found") == 1


def test_via_descriptor_resolves_only_on_its_named_span(tmp_path):
    # Regression (judge note on PR #9396): a descriptor naming the span
    # ``F.Cu-In1.Cu`` resolved to the fixture's through via (``F.Cu-B.Cu``).
    board = _board(tmp_path, TANGENT)
    result = CopperGapRepairer(board).repair_gaps(
        [
            _finding(
                [TRACE, "Via@100/105.75:F.Cu-In1.Cu:d0.3/s0.6"],
                required=0.127,
                actual=0.05,
            )
        ]
    )
    assert result.repaired == 0
    assert result.skipped("via_not_found") == 1


def test_dry_run_predicts_without_modifying(tmp_path):
    board = _board(tmp_path, PAD_VIA + "\n" + TANGENT)
    before = board.read_text()
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps(
        [_finding([PAD, VIA_PAD]), _finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)],
        max_displacement=1.0,
        dry_run=True,
    )
    assert result.repaired == 2 and len(result.actions) == 2
    assert not repairer.modified
    assert board.read_text() == before


def test_second_finding_on_a_moved_via_is_skipped(tmp_path):
    board = _board(tmp_path, PAD_VIA)
    finding = _finding([PAD, VIA_PAD])
    result = CopperGapRepairer(board).repair_gaps([finding, finding], max_displacement=1.0)
    assert result.repaired == 1
    # The applied move rewrote the via's position, so the duplicate finding's
    # descriptor no longer resolves.
    assert result.skipped("via_not_found") == 1
    # In a dry run the document is untouched, so the same via resolves twice.
    dry = CopperGapRepairer(board).repair_gaps(
        [finding, finding], max_displacement=1.0, dry_run=True
    )
    assert dry.repaired == 1
    assert dry.skipped("via_already_moved") == 1


# ---------------------------------------------------------------------------
# Unit: classes that are never touched
# ---------------------------------------------------------------------------


def test_different_net_finding_is_never_selected(tmp_path):
    board = _board(tmp_path, DIFF_NET_VIA)
    finding = _finding([PAD, "Via@101.4/100:F.Cu-B.Cu:d0.3/s0.6"], nets=("N1", "N2"))
    assert copper_gap_violations([finding]) == []
    result = CopperGapRepairer(board).repair_gaps([finding], max_displacement=1.0)
    assert result.total_violations == 0 and result.repaired == 0


def test_warning_finding_is_never_selected(tmp_path):
    finding = _finding([PAD, VIA_PAD], severity=Severity.WARNING)
    assert copper_gap_violations([finding]) == []


@pytest.mark.parametrize(
    "items, reason",
    [
        # zone-fill side (a raw uuid, not a pad of this board)
        (["zone-abcdef", VIA_PAD], "side_not_found"),
        # via-via pair
        ([VIA_PAD, VIA_TANGENT], "unsupported_pair"),
        # track-track pair (no via to move)
        ([TRACE, "Trace@F.Cu:w0.2:95/100.3~105/100.3"], "unsupported_pair"),
        # the via named by the finding is not on the board
        ([PAD, "Via@150/150:F.Cu-B.Cu:d0.3/s0.6"], "via_not_found"),
        # the track named by the finding is not on the board
        (["Trace@F.Cu:w0.2:90/105.3~91/105.3", VIA_TANGENT], "track_not_found"),
    ],
)
def test_unsupported_or_unresolvable_pairs_are_skipped(tmp_path, items, reason):
    board = _board(tmp_path, PAD_VIA + "\n" + TANGENT)
    before = board.read_text()
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding(items, required=0.127)], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped(reason) == 1, result.unrepaired
    assert not repairer.modified
    assert board.read_text() == before


def test_via_on_a_different_net_than_the_pad_is_declined(tmp_path):
    board = _board(tmp_path, DIFF_NET_VIA)
    # The finding claims same-net, but the via's own net disagrees.
    finding = _finding([PAD, "Via@101.4/100:F.Cu-B.Cu:d0.3/s0.6"])
    result = CopperGapRepairer(board).repair_gaps([finding], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped("not_same_net") == 1


@pytest.mark.parametrize(
    ("flag", "locked"), [("locked", True), ("(locked yes)", True), ("(locked no)", False)]
)
@pytest.mark.parametrize(
    ("copper", "items"), [(PAD_VIA, [PAD, VIA_PAD]), (TANGENT, [TRACE, VIA_TANGENT])]
)
def test_gap_repair_respects_via_lock(tmp_path, flag, locked, copper, items):
    board = _board(tmp_path, copper.replace("(via ", f"(via {flag} "))
    result = CopperGapRepairer(board).repair_gaps([_finding(items)], max_displacement=1.0)
    assert result.repaired == int(not locked)
    assert result.skipped("locked_via") == int(locked)


@pytest.mark.parametrize(
    ("copper", "items", "via_id"),
    [(PAD_VIA, [PAD, VIA_PAD], "via-p"), (TANGENT, [TRACE, VIA_TANGENT], "via-t")],
)
def test_gap_repair_preserves_group_locked_via(tmp_path, copper, items, via_id):
    groups = f'(group "fab" (uuid "g") (locked yes) (members "{via_id}"))'
    board = _board(tmp_path, copper + "\n" + groups)
    original = board.read_bytes()
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_finding(items)], max_displacement=1.0)
    assert result.repaired == 0
    assert result.skipped("locked_via") == 1
    assert not repairer.modified
    assert board.read_bytes() == original


def test_move_longer_than_max_displacement_is_declined(tmp_path):
    board = _board(tmp_path, TANGENT)
    result = CopperGapRepairer(board).repair_gaps(
        [_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)], max_displacement=0.1
    )
    assert result.repaired == 0
    assert result.skipped("exceeds_max_displacement") == 1


def test_move_that_crowds_foreign_copper_is_declined(tmp_path):
    from kicad_tools.manufacturers import get_profile

    rules = get_profile("jlcpcb").get_design_rules(2)
    # The via snaps onto its track at (100, 105.3); its 0.3 mm disc would
    # then sit 0.10 mm from this N2 track's edge (min clearance 0.127).
    copper = (
        _seg("trk-short", (95, 105.3), (100.1, 105.3))
        + "\n"
        + _seg("trk-n2", (100.5, 105.3), (102, 105.3), net=2)
        + "\n"
        + '  (via (at 100 105.75) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        '(net 1) (uuid "via-t"))'
    )
    board = _board(tmp_path, copper)
    short_trace = "Trace@F.Cu:w0.2:95/105.3~100.1/105.3"
    result = CopperGapRepairer(board).repair_gaps(
        [_finding([short_trace, VIA_TANGENT], required=0.127, actual=0.05)],
        max_displacement=1.0,
        design_rules=rules,
    )
    assert result.repaired == 0
    assert result.skipped("would_violate_clearance") == 1
    assert _via(board, "via-t") == (100.0, 105.75)


def test_dragged_track_that_would_short_foreign_copper_is_declined(tmp_path):
    # Regression (judge note on PR #9396): the clearance gate validated the
    # via's destination only, so snapping the tangent via dragged its
    # attached B.Cu track across a foreign B.Cu track (signed clearance
    # -0.1351 mm) and the short was saved with ``repaired=1``.  The dragged
    # copper is validated too now and the move is declined.
    from kicad_tools.manufacturers import get_profile

    rules = get_profile("jlcpcb").get_design_rules(2)
    board = _board(tmp_path, TANGENT + "\n" + N1_BCUT + "\n" + N2_BCUT)
    before = board.read_text()
    result = CopperGapRepairer(board).repair_gaps(
        [_finding([TRACE, VIA_TANGENT], required=0.127, actual=0.05)],
        max_displacement=1.0,
        design_rules=rules,
    )
    assert result.repaired == 0
    assert result.skipped("would_violate_clearance") == 1
    assert board.read_text() == before
    assert _via(board, "via-t") == (100.0, 105.75)


# ---------------------------------------------------------------------------
# fix-drc integration: --only copper-gap
# ---------------------------------------------------------------------------


def _report(*violations: DRCViolation) -> DRCReport:
    return DRCReport(
        source_file="b.kicad_pcb",
        created_at=None,
        pcb_name="b.kicad_pcb",
        violations=list(violations),
    )


def _run(monkeypatch, board: Path, reports: list[DRCReport], extra=()):
    """Run fix-drc --only copper-gap; ``reports`` feed successive kct-check runs."""
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
            "copper-gap",
            "--no-connectivity-check",
            "--max-displacement",
            "1.0",
            *extra,
        ]
    )


def test_fix_drc_only_copper_gap_repairs_and_verifies(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, PAD_VIA)
    before = _report(_finding([PAD, VIA_PAD]))
    clean = _report()
    # calls: detection, final re-check
    code = _run(monkeypatch, board, [before, clean])
    assert code == 0, capsys.readouterr().out
    assert _via(board, "via-p")[1] == pytest.approx(100.96, abs=0.005)
    assert "COPPER GAP (same-net): 1/1" in capsys.readouterr().out


def test_fix_drc_only_copper_gap_reports_unrepaired_in_json(tmp_path, monkeypatch, capsys):
    import json

    board = _board(tmp_path, DIFF_NET_VIA)
    finding = _finding([PAD, "Via@101.4/100:F.Cu-B.Cu:d0.3/s0.6"], nets=("N1", "N2"))
    _run(monkeypatch, board, [_report(finding)], extra=("--dry-run", "--format", "json"))
    data = json.loads(capsys.readouterr().out)
    # A different-net finding is not even targeted.
    assert data["copper_gap"]["violations"] == 0
    assert "copper_gap" in data


def test_fix_drc_dry_run_leaves_the_board_alone(tmp_path, monkeypatch):
    board = _board(tmp_path, PAD_VIA)
    original = board.read_text()
    _run(monkeypatch, board, [_report(_finding([PAD, VIA_PAD]))], extra=("--dry-run",))
    assert board.read_text() == original


def test_fix_drc_reports_a_copper_gap_repair_in_json(tmp_path, monkeypatch, capsys):
    import json

    board = _board(tmp_path, PAD_VIA)
    before = _report(_finding([PAD, VIA_PAD]))
    clean = _report()
    _run(monkeypatch, board, [before, clean], extra=("--format", "json"))
    data = json.loads(capsys.readouterr().out)
    assert data["copper_gap"]["violations"] == 1
    assert data["copper_gap"]["repaired"] == 1
    assert data["copper_gap"]["opened"] == 1
    assert data["copper_gap"]["actions"][0]["via_uuid"] == "via-p"


# ---------------------------------------------------------------------------
# Real rule set: the finding exists before and is gone after
# ---------------------------------------------------------------------------


def _same_net_gap_errors(report) -> list:
    return [
        v
        for v in report.violations
        if v.type_str == "physical_copper_gap" and v.is_error and len(set(v.nets)) == 1
    ]


def _diff_net_gap_errors(report) -> list:
    return [
        v
        for v in report.violations
        if v.type_str == "physical_copper_gap" and v.is_error and len(set(v.nets)) > 1
    ]


def _clearance_errors(report) -> list:
    return [
        v for v in report.violations if v.type_str == "clearance_segment_segment" and v.is_error
    ]


def _real_report(board: Path):
    report = fix_drc_cmd._run_python_drc(
        board, manufacturer="jlcpcb", layers=2, project_pcb_path=board, copper_gap=True
    )
    assert report is not None
    return report


def test_real_rule_via_beside_own_pad_is_found_then_repaired(tmp_path, capsys):
    board = _board(tmp_path, PAD_VIA)
    make_assembly_ready(board)

    before = _real_report(board)
    (finding,) = _same_net_gap_errors(before)
    assert PAD in finding.items
    assert any(str(i).startswith("Via@") for i in finding.items)
    assert finding.required_value_mm == pytest.approx(0.15)

    fix_drc_cmd.main([str(board), "--only", "copper-gap", "--no-connectivity-check"])
    out = capsys.readouterr().out
    # The re-check re-measures with the fab-view gap bands: nothing targeted
    # is left (the residual info/warning advisories keep the exit code at 2,
    # the same "violations remain" contract as any other fix-drc category).
    assert "kct-check: 0 targeted error(s)" in out
    assert "COPPER GAP (same-net): 1/1" in out

    after = _real_report(board)
    assert _same_net_gap_errors(after) == []
    x, y = _via(board, "via-p")
    assert x == 100.0
    assert y == pytest.approx(100.96, abs=0.005)


def test_real_rule_tangent_track_slit_is_merged(tmp_path, capsys):
    board = _board(tmp_path, TANGENT)
    make_assembly_ready(board)

    before = _real_report(board)
    (finding,) = _same_net_gap_errors(before)
    assert TRACE in finding.items
    assert finding.required_value_mm == pytest.approx(0.127)

    fix_drc_cmd.main([str(board), "--only", "copper-gap", "--no-connectivity-check"])
    out = capsys.readouterr().out
    assert "kct-check: 0 targeted error(s)" in out

    after = _real_report(board)
    assert _same_net_gap_errors(after) == []
    assert _via(board, "via-t") == (100.0, 105.3)


def test_real_rule_different_net_gap_is_left_unchanged(tmp_path):
    board = _board(tmp_path, PAD_VIA + "\n" + DIFF_NET_VIA)
    make_assembly_ready(board)

    before = _real_report(board)
    assert len(_same_net_gap_errors(before)) == 1
    assert len(_diff_net_gap_errors(before)) == 1

    fix_drc_cmd.main([str(board), "--only", "copper-gap", "--no-connectivity-check"])

    after = _real_report(board)
    assert _same_net_gap_errors(after) == []
    # The different-net via did not move and its finding remains.
    assert _via(board, "via-n2") == (101.4, 100.0)
    assert len(_diff_net_gap_errors(after)) == 1


def test_real_rule_dragged_track_short_is_declined_not_written(tmp_path, capsys):
    # Regression (judge note on PR #9396), end-to-end on the real rule set:
    # snapping the tangent via dragged its attached B.Cu track across a
    # foreign B.Cu track.  Before the fix the saved board's only error was
    # ``clearance_segment_segment`` at -0.1351 mm between the two nets; the
    # dragged copper is validated now, the move is declined and the board is
    # left exactly as it was.
    board = _board(tmp_path, TANGENT + "\n" + N1_BCUT + "\n" + N2_BCUT)
    make_assembly_ready(board)

    before = _real_report(board)
    assert len(_same_net_gap_errors(before)) == 1
    assert not _clearance_errors(before)

    fix_drc_cmd.main([str(board), "--only", "copper-gap", "--no-connectivity-check"])
    out = capsys.readouterr().out
    assert "would_violate_clearance" in out

    # Nothing was written: the via never moved and no short appeared.
    assert _via(board, "via-t") == (100.0, 105.75)
    after = _real_report(board)
    assert not _clearance_errors(after)
    assert len(_same_net_gap_errors(after)) == 1


# ---------------------------------------------------------------------------
# Same-net trace shelf at a routed branch (issue #10367)
# ---------------------------------------------------------------------------
#
# The reporter's four-segment board, placed clear of the fixture's pads: a
# 0.375 mm trunk leaving (90, 108), and a three-segment branch (diagonal,
# 0.4 mm horizontal "shelf", diagonal) that ends 0.25 mm wide at (91.4, 107).
# The 0.4 mm shelf sits 0.0336 mm from the trunk.

TRUNK_END = (99.887, 108)
B_NODE_1 = (90.4086, 107.5914)
B_NODE_2 = (90.8086, 107.5914)
FAR_END = (91.4, 107)
SHELF_SEGS = [
    _seg("sh-trunk", (90, 108), TRUNK_END, width=0.375),
    _seg("sh-b1", (90, 108), B_NODE_1, width=0.375),
    _seg("sh-b2", B_NODE_1, B_NODE_2, width=0.375),
    _seg("sh-b3", B_NODE_2, FAR_END, width=0.25),
]
SHELF = "\n".join(SHELF_SEGS)
TRUNK_REF = "Trace@F.Cu:w0.375:90/108~99.887/108"
SHELF_REF = "Trace@F.Cu:w0.375:90.409/107.591~90.809/107.591"


def _shelf_finding(**kwargs) -> DRCViolation:
    kwargs.setdefault("required", 0.127)
    kwargs.setdefault("actual", 0.0336)
    return _finding([SHELF_REF, TRUNK_REF], **kwargs)


def _segments(path: Path) -> list[tuple]:
    """``(start, end, width, layer, net, uuid)`` of every segment, file order."""
    out = []
    for node in parse_file(path).find_all("segment"):
        start, end = node.find("start").get_atoms(), node.find("end").get_atoms()
        out.append(
            (
                (float(start[0]), float(start[1])),
                (float(end[0]), float(end[1])),
                float(node.find("width").get_first_atom()),
                str(node.find("layer").get_first_atom()).strip('"'),
                int(node.find("net").get_first_atom()),
                str(node.find("uuid").get_first_atom()).strip('"'),
            )
        )
    return out


def _geometry(path: Path) -> list[tuple]:
    """Segments without their uuids, order-insensitive."""
    return sorted((tuple(sorted((s[0], s[1]))), s[2], s[3], s[4]) for s in _segments(path))


def _jlcpcb_rules():
    from kicad_tools.manufacturers import get_profile

    return get_profile("jlcpcb").get_design_rules(2)


@pytest.mark.parametrize("with_rules", [False, True])
def test_shelf_at_a_branch_is_rewritten_to_the_shortcut(tmp_path, with_rules):
    board = _board(tmp_path, SHELF)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps(
        [_shelf_finding()], design_rules=_jlcpcb_rules() if with_rules else None
    )
    assert result.repaired == 1 and result.rewritten_shelves == 1 and not result.unrepaired
    (action,) = result.actions
    assert action.action == "rewrite_shelf"
    # Both end nodes are the preserved ones; the shelf is the finding's segment.
    assert (action.via_x, action.via_y) == (90.0, 108.0)
    assert (action.new_x, action.new_y) == FAR_END
    assert action.shelf.shelf == (B_NODE_1, B_NODE_2)
    repairer.save(board)

    segments = _segments(board)
    assert len(segments) == 3
    # The trunk is untouched, byte for byte.
    assert ((90.0, 108.0), TRUNK_END, 0.375, "F.Cu", 1, "sh-trunk") in segments
    new = sorted(s for s in segments if s[5] != "sh-trunk")
    # 45-degree leg at the first width, then the H/V leg at the last width;
    # the far end keeps its exact coordinates.
    assert [(s[0], s[1], s[2]) for s in new] == [
        ((90.0, 108.0), (91.0, 107.0), 0.375),
        ((91.0, 107.0), FAR_END, 0.25),
    ] or [(s[0], s[1], s[2]) for s in new] == [
        ((91.0, 107.0), FAR_END, 0.25),
        ((90.0, 108.0), (91.0, 107.0), 0.375),
    ]
    assert all(s[3] == "F.Cu" and s[4] == 1 for s in new)
    assert len({s[5] for s in segments}) == 3  # fresh, unique uuids


def test_real_rule_shelf_is_found_then_repaired(tmp_path, capsys):
    board = _board(tmp_path, SHELF)
    make_assembly_ready(board)

    before = _real_report(board)
    (finding,) = _same_net_gap_errors(before)
    assert set(finding.items) == {SHELF_REF, TRUNK_REF}
    assert finding.actual_value_mm == pytest.approx(0.0336, abs=0.0005)
    assert not _clearance_errors(before)

    fix_drc_cmd.main([str(board), "--only", "copper-gap", "--no-connectivity-check"])
    out = capsys.readouterr().out
    assert "kct-check: 0 targeted error(s)" in out
    assert "COPPER GAP (same-net): 1/1" in out
    assert "[REWRITE_SHELF]" in out

    after = _real_report(board)
    assert _same_net_gap_errors(after) == []
    assert not _clearance_errors(after)
    geometry = _geometry(board)
    assert (((90.0, 108.0), TRUNK_END), 0.375, "F.Cu", 1) in geometry
    assert (((90.0, 108.0), (91.0, 107.0)), 0.375, "F.Cu", 1) in geometry
    assert (((91.0, 107.0), FAR_END), 0.25, "F.Cu", 1) in geometry
    assert len(geometry) == 3


def test_shelf_dry_run_names_the_shelf_and_both_ends_without_writing(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, SHELF)
    before = board.read_text()
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_shelf_finding()], dry_run=True)
    assert result.repaired == 1 and not repairer.modified
    assert board.read_text() == before
    (action,) = result.actions
    for fragment in ("90.4086, 107.5914", "90.8086, 107.5914", "90.0000, 108.0000", "91.4000"):
        assert fragment in action.detail
    assert len(action.shelf.removed) == 3 and len(action.shelf.added) == 2

    _run(monkeypatch, board, [_report(_shelf_finding())], extra=("--dry-run",))
    assert board.read_text() == before
    assert "[REWRITE_SHELF]" in capsys.readouterr().out


def test_shelf_rewrite_undoes_in_place_and_in_a_fresh_document(tmp_path):
    board = _board(tmp_path, SHELF)
    original = _segments(board)
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_shelf_finding()])
    (action,) = result.actions
    # In place: the three originals (uuids included) come back.
    assert repairer.undo_move(action)
    repairer.save(board)
    assert sorted(_segments(board)) == sorted(original)

    # From the saved file, as the fix-drc per-action rollback does.
    again = CopperGapRepairer(board)
    redo = again.repair_gaps([_shelf_finding()])
    again.save(board)
    assert _geometry(board) != _geometry_of(original)
    fresh = CopperGapRepairer(board)
    assert fresh.undo_move(redo.actions[0])
    fresh.save(board)
    assert sorted(_segments(board)) == sorted(original)


def _geometry_of(segments: list[tuple]) -> list[tuple]:
    return sorted((tuple(sorted((s[0], s[1]))), s[2], s[3], s[4]) for s in segments)


def test_shelf_undo_declines_when_a_replacement_segment_is_gone(tmp_path):
    board = _board(tmp_path, SHELF)
    repairer = CopperGapRepairer(board)
    (action,) = repairer.repair_gaps([_shelf_finding()]).actions
    repairer.save(board)
    other = CopperGapRepairer(board)
    victim = other._segment_by_uuid(action.shelf.added[0][0])
    other.doc.remove(victim)
    assert not other.undo_move(action)


def test_second_finding_on_a_rewritten_chain_is_not_rewritten_twice(tmp_path):
    board = _board(tmp_path, SHELF)
    finding = _shelf_finding()
    dry = CopperGapRepairer(board).repair_gaps([finding, finding], dry_run=True)
    assert dry.repaired == 1 and dry.skipped("shelf_already_rewritten") == 1
    live = CopperGapRepairer(board).repair_gaps([finding, finding])
    assert live.repaired == 1 and len(live.unrepaired) == 1


_PAD_AT_NODE = (
    '  (footprint "test:R9" (layer "F.Cu") (at 90.4086 107.5914)\n'
    '    (property "Reference" "R9" (at 0 -1.5 0) (layer "F.SilkS")\n'
    "      (effects (font (size 1 1) (thickness 0.15))))\n"
    '    (property "Value" "R" (at 0 1.5 0) (layer "F.Fab")\n'
    "      (effects (font (size 1 1) (thickness 0.15))))\n"
    '    (pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "F.Cu" "F.Paste" "F.Mask") '
    '(net 2 "N2") (uuid "pad-r9-1"))\n'
    "  )"
)


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        # a pad (any net) under an interior node
        (_PAD_AT_NODE, "protected_pad"),
        # a via under an interior node
        (
            '  (via (at 90.8086 107.5914) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            '(net 1) (uuid "via-i"))',
            "protected_via",
        ),
        # a same-net track on the other layer ends at an interior node
        (_seg("lc", B_NODE_2, (95, 105), layer="B.Cu"), "protected_layer_change"),
        # a tap whose end meets an interior node (a T-junction)
        (_seg("tap", B_NODE_1, (90.4086, 105)), "protected_tap"),
        # a same-net track crossing an interior node
        (_seg("tapx", (90.4086, 105), (90.4086, 110)), "protected_tap"),
    ],
)
def test_protected_interior_node_declines_the_rewrite(tmp_path, extra, reason):
    board = _board(tmp_path, SHELF + "\n" + extra)
    before = board.read_text()
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_shelf_finding()])
    assert result.repaired == 0
    assert result.skipped(reason) == 1, result.unrepaired
    assert not repairer.modified
    assert board.read_text() == before


def test_reinforcement_endpoint_on_an_interior_node_protects_the_chain(tmp_path):
    import json

    board = _board(tmp_path, SHELF)
    sidecar = board.with_name(board.name + ".reinforcement.json")
    wire = {
        "net": "N1",
        "layer": "F.Cu",
        "leg_start": {"x": B_NODE_2[0], "y": B_NODE_2[1]},
        "leg_end": {"x": 95.0, "y": 100.0},
        "anchors": [{"x": B_NODE_2[0], "y": B_NODE_2[1]}],
    }
    sidecar.write_text(json.dumps({"version": 1, "wires": [wire]}))
    before = board.read_text()
    result = CopperGapRepairer(board).repair_gaps([_shelf_finding()])
    assert result.repaired == 0 and result.skipped("protected_reinforcement") == 1
    assert board.read_text() == before

    # Another net's wire at the same place does not protect this net.
    wire["net"] = "N2"
    sidecar.write_text(json.dumps({"version": 1, "wires": [wire]}))
    assert CopperGapRepairer(board).repair_gaps([_shelf_finding()]).repaired == 1

    # A repair pass that loads a copy still finds the input board's sidecar.
    wire["net"] = "N1"
    sidecar.write_text(json.dumps({"version": 1, "wires": [wire]}))
    copy = tmp_path / "copy.kicad_pcb"
    copy.write_text(board.read_text())
    declined = CopperGapRepairer(copy, project_pcb_path=board).repair_gaps([_shelf_finding()])
    assert declined.skipped("protected_reinforcement") == 1


def test_shelf_rewrite_that_crowds_foreign_copper_is_declined(tmp_path):
    # A 0.2 mm N2 track 0.199 mm from the original branch but 0.013 mm from
    # the shortcut: the rewrite would trade one finding for a worse one.
    foreign = _seg("n2", (90.9, 106.7), (91.1, 106.7), net=2, width=0.2)
    board = _board(tmp_path, SHELF + "\n" + foreign)
    before = board.read_text()
    repairer = CopperGapRepairer(board)
    result = repairer.repair_gaps([_shelf_finding()], design_rules=_jlcpcb_rules())
    assert result.repaired == 0 and result.skipped("would_violate_clearance") == 1
    assert not repairer.modified and board.read_text() == before
    # The same refusal without profile rules: the finding's own edge applies.
    again = CopperGapRepairer(board).repair_gaps([_shelf_finding()])
    assert again.skipped("would_violate_clearance") == 1


def test_shelf_rewrite_that_leaves_a_new_same_net_slit_is_declined(tmp_path):
    # Same geometry, same net: no clearance rule applies, but the shortcut
    # would leave a 0.013 mm slit to that copper -- a new physical gap finding.
    spur = _seg("spur", (90.9, 106.7), (91.1, 106.7), width=0.2)
    board = _board(tmp_path, SHELF + "\n" + spur)
    before = board.read_text()
    result = CopperGapRepairer(board).repair_gaps([_shelf_finding()])
    assert result.repaired == 0 and result.skipped("gap_not_cleared") == 1
    assert board.read_text() == before


@pytest.mark.parametrize(
    ("flag", "locked"), [("(locked yes)", True), ("locked", True), ("(locked no)", False)]
)
@pytest.mark.parametrize("victim", [1, 2, 3])
def test_shelf_rewrite_respects_locked_segments(tmp_path, flag, locked, victim):
    segs = list(SHELF_SEGS)
    segs[victim] = segs[victim].replace("(segment ", f"(segment {flag} ")
    board = _board(tmp_path, "\n".join(segs))
    result = CopperGapRepairer(board).repair_gaps([_shelf_finding()])
    assert result.repaired == int(not locked)
    assert result.skipped("locked_segment") == int(locked)


def test_shelf_rewrite_respects_a_locked_group(tmp_path):
    groups = '(group "fab" (uuid "g") (locked yes) (members "sh-b3"))'
    board = _board(tmp_path, SHELF + "\n" + groups)
    before = board.read_bytes()
    result = CopperGapRepairer(board).repair_gaps([_shelf_finding()])
    assert result.repaired == 0 and result.skipped("locked_segment") == 1
    assert board.read_bytes() == before


def test_a_locked_trunk_is_not_a_reason_to_decline(tmp_path):
    # The trunk is preserved byte for byte, so locking it blocks nothing.
    segs = list(SHELF_SEGS)
    segs[0] = segs[0].replace("(segment ", "(segment (locked yes) ")
    board = _board(tmp_path, "\n".join(segs))
    assert CopperGapRepairer(board).repair_gaps([_shelf_finding()]).repaired == 1


def test_shelf_chain_with_a_single_width_keeps_it_on_both_legs(tmp_path):
    segs = [s.replace("(width 0.25)", "(width 0.375)") for s in SHELF_SEGS]
    board = _board(tmp_path, "\n".join(segs))
    repairer = CopperGapRepairer(board)
    assert repairer.repair_gaps([_shelf_finding()]).repaired == 1
    repairer.save(board)
    widths = {s[2] for s in _segments(board)}
    assert widths == {0.375}


def test_shelf_chain_with_a_straight_shortcut_becomes_one_segment(tmp_path):
    # Ends 1 mm apart on a 45-degree line: one straight leg replaces three.
    chain = [
        _seg("st-trunk", (90, 108), TRUNK_END, width=0.375),
        _seg("st-b1", (90, 108), (90.5, 107.9), width=0.375),
        _seg("st-b2", (90.5, 107.9), (90.7, 107.5), width=0.375),
        _seg("st-b3", (90.7, 107.5), (91, 107), width=0.375),
    ]
    board = _board(tmp_path, "\n".join(chain))
    repairer = CopperGapRepairer(board)
    finding = _finding(
        ["Trace@F.Cu:w0.375:90.5/107.9~90.7/107.5", TRUNK_REF], required=0.127, actual=0.05
    )
    result = repairer.repair_gaps([finding])
    assert result.repaired == 1, result.unrepaired
    repairer.save(board)
    assert ((90.0, 108.0), (91.0, 107.0), 0.375) in [(s[0], s[1], s[2]) for s in _segments(board)]
    assert len(_segments(board)) == 2


def test_straight_shortcut_keeps_a_width_step_at_the_projected_node():
    pts = [(0.0, 0.0), (0.4, -0.1), (0.8, -0.5), (1.0, -1.0)]
    (legs,) = CopperGapRepairer._shelf_candidates(pts, 0.375, 0.25, (0.8, -0.5))
    assert [w for _a, _b, w in legs] == [0.375, 0.25]
    assert legs[0][0] == (0.0, 0.0) and legs[-1][1] == (1.0, -1.0)
    assert legs[0][1] == legs[1][0]
    assert legs[0][1] == pytest.approx((0.65, -0.65), abs=1e-4)


def test_taper_with_two_width_steps_is_declined(tmp_path):
    segs = list(SHELF_SEGS)
    segs[3] = segs[3].replace("(width 0.25)", "(width 0.2)")
    segs[2] = segs[2].replace("(width 0.375)", "(width 0.3)")
    board = _board(tmp_path, "\n".join(segs))
    result = CopperGapRepairer(board).repair_gaps(
        [_finding([SHELF_REF.replace("w0.375", "w0.3"), TRUNK_REF], required=0.127)]
    )
    assert result.repaired == 0 and result.skipped("width_profile_unsupported") == 1


def test_a_single_segment_branch_has_nothing_to_simplify(tmp_path):
    copper = "\n".join(
        [
            _seg("one-trunk", (90, 108), TRUNK_END, width=0.375),
            _seg("one-branch", (90, 108), (90.4, 107.6), width=0.375),
        ]
    )
    board = _board(tmp_path, copper)
    result = CopperGapRepairer(board).repair_gaps(
        [_finding(["Trace@F.Cu:w0.375:90/108~90.4/107.6", TRUNK_REF], required=0.127)]
    )
    assert result.repaired == 0 and result.skipped("no_simplification") == 1


def test_a_chain_longer_than_one_branch_is_declined(tmp_path):
    # Ten 0.2 mm staircase steps off the trunk: that is a re-route, not a shelf.
    points = [(90.0, 108.0)]
    for i in range(10):
        x, y = points[-1]
        points.append((x + 0.2, y) if i % 2 else (x, y - 0.2))
    segs = [_seg("lg-trunk", (90, 108), TRUNK_END, width=0.375)]
    segs += [
        _seg(f"lg-{i}", a, b, width=0.375)
        for i, (a, b) in enumerate(zip(points, points[1:], strict=False))
    ]
    board = _board(tmp_path, "\n".join(segs))
    (ax, ay), (bx, by) = points[3], points[4]
    shelf = f"Trace@F.Cu:w0.375:{ax:g}/{ay:g}~{bx:g}/{by:g}"
    result = CopperGapRepairer(board).repair_gaps([_finding([shelf, TRUNK_REF], required=0.127)])
    assert result.repaired == 0 and result.skipped("chain_too_long") == 1


@pytest.mark.parametrize(
    "items",
    [
        # two tracks on different layers
        [SHELF_REF, "Trace@B.Cu:w0.375:90/108~99.887/108"],
        # two segments with no branch between them
        [TRUNK_REF, "Trace@F.Cu:w0.375:90/110~95/110"],
        # a pair of equal length: neither is the shelf
        [TRUNK_REF, "Trace@F.Cu:w0.375:90/112~99.887/112"],
    ],
)
def test_track_pairs_that_are_not_a_branch_shelf_stay_unsupported(tmp_path, items):
    extra = _seg("far", (90, 110), (95, 110), width=0.375) + "\n"
    extra += _seg("twin", (90, 112), TRUNK_END[:1] + (112,), width=0.375)
    board = _board(tmp_path, SHELF + "\n" + extra)
    before = board.read_text()
    result = CopperGapRepairer(board).repair_gaps([_finding(items, required=0.127)])
    assert result.repaired == 0
    assert result.skipped("unsupported_pair") == 1, result.unrepaired
    assert board.read_text() == before


def test_fix_drc_json_reports_the_shelf_rewrite(tmp_path, monkeypatch, capsys):
    import json

    board = _board(tmp_path, SHELF)
    code = _run(
        monkeypatch,
        board,
        [_report(_shelf_finding()), _report()],
        extra=("--format", "json"),
    )
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["copper_gap"]["rewritten_shelves"] == 1
    (action,) = data["copper_gap"]["actions"]
    assert action["action"] == "rewrite_shelf"
    assert action["shelf"]["trunk_node"] == [90.0, 108.0]
    assert action["shelf"]["far_node"] == list(FAR_END)
    assert action["shelf"]["removed_segments"] == 3
    assert len(action["shelf"]["added_segments"]) == 2


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (_seg("tap", (92, 107.5914), (92, 107.4), width=0.12), "protected_tap"),
        (
            '(via (at 92 107.5914) (size 0.3) (drill 0.15) (layers "F.Cu" "B.Cu") (net 1) (uuid "v"))',
            "protected_via",
        ),
        (
            "\n".join(_seg(str(y), (91.6, y), (92.25, y), width=0.12) for y in (107.52, 107.68)),
            "protected_tap",
        ),
        # Foreign tracks isolate the newly exposed slit from same-net contact protection.
        (
            "\n".join(
                _seg(str(y), (91.6, y), (92.25, y), net=2, width=0.12) for y in (107.52, 107.68)
            ),
            "gap_not_cleared",
        ),
    ],
)
def test_extended_shelf_preserves_contacts_and_covered_gaps(tmp_path, extra, reason):
    board = _board(
        tmp_path, SHELF.replace("90.8086", "92.8086").replace("91.4 ", "93.4 ") + "\n" + extra
    )
    finding = _finding([SHELF_REF.replace("90.809", "92.809"), TRUNK_REF], required=0.127)
    repairer = CopperGapRepairer(board)
    before = repairer.doc.to_string()
    result = repairer.repair_gaps([finding])
    assert result.skipped(reason) == 1, result.unrepaired
    assert repairer.doc.to_string() == before


@pytest.mark.parametrize(
    ("owner", "reason"), [("generated", "generator_owned"), ("group", "group_member")]
)
def test_shelf_rewrite_preserves_member_ids(tmp_path, owner, reason):
    board = _board(
        tmp_path, SHELF + f'\n({owner} "tuning" (uuid "g") (members "sh-b1" "sh-b2" "sh-b3"))'
    )
    repairer = CopperGapRepairer(board)
    before = repairer.doc.to_string()
    assert repairer.repair_gaps([_shelf_finding()]).skipped(reason) == 1
    assert repairer.doc.to_string() == before


def test_saved_legacy_shelf_undo(tmp_path):
    board = _board(tmp_path, SHELF.replace("(uuid", "(tstamp"))
    repairer = CopperGapRepairer(board)
    original = [n.to_string() for n in repairer.doc.find_children("segment")]
    (action,) = repairer.repair_gaps([_shelf_finding()]).actions
    repairer.save(board)
    fresh = CopperGapRepairer(board)
    assert fresh.undo_move(action)
    assert [n.to_string() for n in fresh.doc.find_children("segment")] == original


@pytest.mark.parametrize(
    ("field", "value"),
    [("end", 94), ("start", 91.5), ("width", 0.2), ("layer", "B.Cu"), ("net", 2)],
)
def test_shelf_undo_preserves_later_edits(tmp_path, field, value):
    board = _board(tmp_path, SHELF)
    repairer = CopperGapRepairer(board)
    (action,) = repairer.repair_gaps([_shelf_finding()]).actions
    repairer.save(board)
    fresh = CopperGapRepairer(board)
    replacement = fresh._segment_by_uuid(action.shelf.added[-1][0])
    replacement.find_child(field).set_value(0, value)
    edited = fresh.doc.to_string()
    assert not fresh.undo_move(action)
    assert fresh.doc.to_string() == edited


def test_unrelated_preexisting_slit_in_shelf_window_is_allowed(tmp_path):
    extra = "\n".join(_seg(str(y), (91.15, y), (91.45, y), width=0.1) for y in (107.53, 107.69))
    board = _board(tmp_path, SHELF + "\n" + extra)
    before = _same_net_gap_errors(_real_report(board))
    assert any(gap.actual_value_mm == pytest.approx(0.06) for gap in before)
    result = CopperGapRepairer(board).repair_gaps([_shelf_finding()])
    assert result.repaired == 1, result.unrepaired


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (
            '(footprint "T" (layer "F.Cu") (at 92 107) (pad "1" smd rect (at 0 0) (size 0.4 0.2) (layers "F.Cu")))',
            "would_violate_clearance",
        ),
        (
            '(footprint "T" (layer "F.Cu") (at 92 107) (pad "" np_thru_hole circle (at 0 0) (size 0.3 0.3) (drill 0.3) (layers "*.Cu")))',
            "hole_clearance",
        ),
        (
            '(zone (net 0) (layer "F.Cu") (keepout (tracks not_allowed)) (polygon (pts (xy 91.9 106.9) (xy 92.1 106.9) (xy 92.1 107.1) (xy 91.9 107.1))))',
            "track_keepout",
        ),
        (
            '(footprint "T" (layer "F.Cu") (at 92 106.635) (pad "1" smd rect (at 0 0) (size 0.4 0.2) (layers "F.Cu") (net 1 "N1")))',
            "gap_not_cleared",
        ),
        (_seg("foreign", (91.7, 106.575), (92.25, 106.575), net=2), "would_violate_clearance"),
    ],
    ids=["unassigned-pad", "npth-drill", "track-keepout", "pad-gap", "netclass"],
)
def test_cli_rejects_unsafe_shelf(tmp_path, capsys, extra, reason):
    import json

    board = _board(
        tmp_path, SHELF.replace("90.8086", "92.8086").replace("91.4 ", "93.4 ") + "\n" + extra
    )
    board.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "classes": [
                        {"name": "Default", "clearance": 0.1},
                        {"name": "HV", "clearance": 0.5},
                    ],
                    "netclass_patterns": [{"netclass": "HV", "pattern": "N2"}],
                }
            }
        )
    )
    make_assembly_ready(board)
    before = board.read_bytes()
    fix_drc_cmd.main([str(board), "--only", "copper-gap"])
    output = capsys.readouterr().out
    assert reason in output
    assert board.read_bytes() == before
