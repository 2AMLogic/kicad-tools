"""``relocate_pad_edge_vias`` slides vias out of the mask-dam zone (Issue #7296)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.relocate_in_pad_vias import relocate_pad_edge_vias
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

_HEAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user)
          (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance {mask}))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 1 "SIG1"))
  )
"""

# Right-hand copper edge at x=100.5; a 0.3 mm drill centred at 100.65 is tangent
# to it (gap 0), the board-05 R11.1 shape.  Escape track leaves along +X.
_EDGE_VIA = """  (via (at 100.65 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "v1"))
  (segment (start 100.65 100) (end 104 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "s1"))
)
"""

_RULES = get_profile("jlcpcb").get_design_rules(2)


def _load(tmp_path: Path, body: str, mask: float = 0.0, *, head: str = _HEAD) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(head.format(mask=mask) + body)
    return PCB.load(path)


def _edge_warnings(pcb: PCB) -> list:
    results = ViaInPadRule(report_pad_edge=True).check(pcb, _RULES)
    return [v for v in results.violations if v.rule_id == "via_at_pad_edge"]


def test_edge_via_slides_along_its_escape_track(tmp_path):
    pcb = _load(tmp_path, _EDGE_VIA)
    assert len(_edge_warnings(pcb)) == 1, "precondition"

    result = relocate_pad_edge_vias(pcb, _RULES)

    assert [m.kind for m in result.moved] == ["pad-edge"]
    assert not result.unresolvable
    assert _edge_warnings(pcb) == []
    (via,) = pcb.vias
    # Moved along the track (+X only), by the least whole-step distance that
    # leaves the dam: gap = (x - 0.15) - 100.5 >= 0.1.
    assert via.position[1] == pytest.approx(100.0)
    assert via.position[0] == pytest.approx(100.75, abs=0.03)
    # Connectivity: a stub on the pad layer joins old and new positions.
    assert result.moved[0].stub_layers[0] == "F.Cu"
    stubs = [
        s
        for s in pcb.segments
        if {round(s.start[0], 3), round(s.end[0], 3)} & {100.65} and s.uuid != "s1"
    ]
    assert stubs


def test_via_already_clear_of_the_dam_is_left_alone(tmp_path):
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.8"))
    assert _edge_warnings(pcb) == []
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert result.moved == [] and result.unresolvable == []
    assert pcb.vias[0].position[0] == pytest.approx(100.8)


def test_in_pad_via_is_not_this_passs_business(tmp_path):
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.3"))
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert result.moved == [] and result.unresolvable == []


def test_board_mask_expansion_widens_the_zone(tmp_path):
    """pad_to_mask_clearance 0.1: the aperture grows, so 100.8 is now flagged."""
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.8"), mask=0.1)
    assert len(_edge_warnings(pcb)) == 1
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert len(result.moved) == 1
    assert _edge_warnings(pcb) == []


def test_boxed_in_via_is_reported_and_left_in_place(tmp_path):
    """A foreign pad hugging the only way out leaves no legal spot."""
    boxed = """  (footprint "test:pad" (layer "F.Cu") (at 101.3 100)
    (pad "1" smd rect (at 0 0) (size 0.6 6.0) (layers "F.Cu") (net 2 "SIG2"))
  )
"""
    pcb = _load(tmp_path, boxed + _EDGE_VIA)
    before = pcb.vias[0].position
    result = relocate_pad_edge_vias(pcb, _RULES)
    # The ladder may still find an off-axis spot above/below; whatever it does,
    # it never leaves a warning behind silently nor creates a clearance error.
    if result.unresolvable:
        assert not result.moved
        assert pcb.vias[0].position == before
        assert len(_edge_warnings(pcb)) == 1
    else:
        assert _edge_warnings(pcb) == []


def test_zero_dam_profile_is_a_noop(tmp_path):
    from types import SimpleNamespace

    pcb = _load(tmp_path, _EDGE_VIA)
    rules = SimpleNamespace(
        min_solder_mask_dam_mm=0.0, min_clearance_mm=0.1, min_hole_to_hole_mm=0.5
    )
    result = relocate_pad_edge_vias(pcb, rules)
    assert result.moved == [] and result.unresolvable == []


def test_fix_vias_cli_runs_the_pass_and_saves(tmp_path):
    """``kct fix-vias --relocate-pad-edge`` is the pass's CLI entry point."""
    from kicad_tools.cli.fix_vias_cmd import main

    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEAD.format(mask=0.0) + _EDGE_VIA)

    assert main([str(path), "--mfr", "jlcpcb", "--relocate-pad-edge", "--quiet"]) == 0

    assert _edge_warnings(PCB.load(path)) == []
    # Idempotent: a clean board is a no-op.
    assert main([str(path), "--mfr", "jlcpcb", "--relocate-pad-edge", "--quiet"]) == 0


# ---------------------------------------------------------------------------
# Netclass clearance (board 06 E2E regression on PR #7308)
# ---------------------------------------------------------------------------

# A foreign B.Cu track 0.16 mm (copper to copper) from the via's first legal
# spot at x=100.75: clear of JLCPCB's 0.127 mm floor, short of a 0.2 mm class.
_FOREIGN_TRACK = """  (segment (start 101.31 98) (end 101.31 102) (width 0.2) (layer "B.Cu") (net 2) (uuid "f1"))
"""


def _power_project(pcb_path: Path, pattern: str = "SIG1") -> None:
    import json

    from tests.router.test_authored_netclass_board import _netclass

    project = {
        "meta": {"filename": pcb_path.with_suffix(".kicad_pro").name, "version": 3},
        "net_settings": {
            "meta": {"version": 4},
            "classes": [
                _netclass("Default", 0.127, 2147483647),
                _netclass("Power", 0.2, 0),
            ],
            "netclass_assignments": None,
            "netclass_patterns": [{"netclass": "Power", "pattern": pattern}],
        },
    }
    pcb_path.with_suffix(".kicad_pro").write_text(json.dumps(project, indent=2))


def _netclass_errors(pcb: PCB) -> list:
    from kicad_tools.validate.rules.netclass_clearance_copper import (
        check_netclass_clearance_copper,
    )

    pcb.save(pcb.path)
    reloaded = PCB.load(pcb.path)
    return check_netclass_clearance_copper(pcb.path, reloaded, _RULES).violations


def test_control_without_netclass_lands_inside_the_class_clearance(tmp_path):
    """Non-vacuous control: the fab floor alone accepts the 0.16 mm spot."""
    pcb = _load(tmp_path, _FOREIGN_TRACK + _EDGE_VIA)
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert len(result.moved) == 1
    assert pcb.vias[0].position[0] == pytest.approx(100.75, abs=0.03)
    # The same geometry under a 0.2 mm Power class is a netclass error.
    _power_project(pcb.path)
    assert [v.rule_id for v in _netclass_errors(pcb)] == ["netclass_clearance_copper"]


def test_relocation_honours_the_via_nets_netclass_clearance(tmp_path):
    pcb = _load(tmp_path, _FOREIGN_TRACK + _EDGE_VIA)
    _power_project(pcb.path)
    assert _netclass_errors(PCB.load(pcb.path)) == [], "precondition: clean before the move"

    result = relocate_pad_edge_vias(pcb, _RULES)

    assert len(result.moved) == 1 and not result.unresolvable
    assert _edge_warnings(pcb) == []
    assert pcb.vias[0].position[0] != pytest.approx(100.75, abs=0.03)
    assert _netclass_errors(pcb) == []


def test_relocation_honours_the_foreign_nets_netclass_clearance(tmp_path):
    """The pair requirement is max(class[a], class[b]): the obstacle's class counts."""
    pcb = _load(tmp_path, _FOREIGN_TRACK + _EDGE_VIA)
    _power_project(pcb.path, pattern="SIG2")
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert len(result.moved) == 1 and not result.unresolvable
    assert _netclass_errors(pcb) == []


def test_unmodellable_netclasses_move_nothing(tmp_path):
    import json

    pcb = _load(tmp_path, _FOREIGN_TRACK + _EDGE_VIA)
    _power_project(pcb.path)
    project_path = pcb.path.with_suffix(".kicad_pro")
    project = json.loads(project_path.read_text())
    project["net_settings"]["netclass_patterns"] = [{"netclass": "Power", "pattern": "S[I"}]
    project["net_settings"]["netclass_assignments"] = {"SIG1": ["NoSuchClass"]}
    project_path.write_text(json.dumps(project))
    before = pcb.vias[0].position

    result = relocate_pad_edge_vias(pcb, _RULES)

    assert result.moved == []
    assert len(result.unresolvable) == 1
    assert "netclass" in result.unresolvable[0].reason
    assert pcb.vias[0].position == before


# ---------------------------------------------------------------------------
# Differential pairs: never move one leg alone
# ---------------------------------------------------------------------------


def _pair_board(tmp_path: Path, p_name: str, n_name: str) -> PCB:
    body = (
        _HEAD.format(mask=0.0)
        .replace('"SIG1"', f'"{p_name}"')
        .replace('(net 2 "SIG2")', f'(net 2 "{n_name}")')
    )
    path = tmp_path / "board.kicad_pcb"
    path.write_text(body + _EDGE_VIA)
    return PCB.load(path)


def test_diff_pair_leg_is_skipped_not_moved(tmp_path):
    pcb = _pair_board(tmp_path, "USB_D+", "USB_D-")
    assert len(_edge_warnings(pcb)) == 1, "precondition"
    before = pcb.vias[0].position
    n_segments = len(pcb.segments)

    result = relocate_pad_edge_vias(pcb, _RULES)

    assert result.moved == []
    assert len(result.skipped) == 1
    assert "differential-pair leg" in result.skipped[0].reason
    assert pcb.vias[0].position == before
    assert len(pcb.segments) == n_segments


def test_router_recorded_pair_is_a_pair_too(tmp_path):
    import json

    pcb = _pair_board(tmp_path, "X_POS", "X_NEG")
    pcb.path.with_suffix(".kicad_pro").write_text(
        json.dumps({"meta": {"kct_router_diff_pairs": [["X_POS", "X_NEG"]]}})
    )
    result = relocate_pad_edge_vias(PCB.load(pcb.path), _RULES)
    assert result.moved == [] and len(result.skipped) == 1


def test_lone_plus_suffix_net_without_partner_still_moves(tmp_path):
    """Control: the guard keys on a real partner, not on a +/- suffix."""
    pcb = _pair_board(tmp_path, "VCC+", "SIG2")
    result = relocate_pad_edge_vias(pcb, _RULES)
    assert len(result.moved) == 1


# ---------------------------------------------------------------------------
# Issue #9891: ring-dam mode (mask_to_copper's copper-ring view)
# ---------------------------------------------------------------------------


def test_ring_dam_mode_moves_a_drill_legal_ring_encroacher(tmp_path):
    """A via whose drill clears the dam but whose copper ring reaches into the
    aperture is moved only in ``ring_dam_mm`` mode."""
    # Drill gap = 100.8 - 0.15 - 100.5 = 0.15 >= dam 0.1 (plain rule silent);
    # ring gap = 100.8 - 0.3 - 100.5 = 0.0 < ring dam.
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.8"))
    assert _edge_warnings(pcb) == []

    plain = relocate_pad_edge_vias(pcb, _RULES)
    assert plain.moved == [] and plain.unresolvable == []

    ring = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09)
    assert [m.kind for m in ring.moved] == ["pad-edge"]
    assert not ring.unresolvable
    (via,) = pcb.vias
    # Ring edge clears 0.09: x - 0.3 - 100.5 >= 0.09, on the 0.025 step grid.
    assert via.position[1] == pytest.approx(100.0)
    assert via.position[0] == pytest.approx(100.9, abs=0.03)
    # The move must not create a plain via_at_pad_edge finding either.
    assert _edge_warnings(pcb) == []


@pytest.mark.parametrize("explicit_escape", [False, True])
def test_ring_move_keeps_an_existing_pad_attachment(tmp_path, explicit_escape):
    from kicad_tools.validate.connectivity import ConnectivityValidator

    head = _HEAD.replace(
        "(at 100 100)",
        '(at 100 100) (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))',
    )
    far_pad = (
        '(footprint "test:remote" (layer "F.Cu") (at 104 100) '
        '(property "Reference" "U2" (at 0 0) (layer "F.SilkS")) '
        '(pad "1" smd rect (at 0 0) (size .5 .5) (layers "F.Cu") (net 1 "SIG1")))'
    )
    escape = (
        '(segment (start 100 100) (end 100.78 100) (width .2) (layer "F.Cu") (net 1))'
        if explicit_escape
        else ""
    )
    pcb = _load(tmp_path, far_pad + escape + _EDGE_VIA.replace("100.65", "100.78"), head=head)
    original = pcb._sexp.to_string()
    assert ConnectivityValidator(pcb)._contact_graph(1).open_count() == 0
    result = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09)
    saved = tmp_path / "result.kicad_pcb"
    pcb.save(saved)
    assert ConnectivityValidator(saved)._contact_graph(1).open_count() == 0
    if explicit_escape:
        assert result.moved
    elif not result.moved:
        assert result.unresolvable and pcb._sexp.to_string() == original


@pytest.mark.parametrize(
    "lock,locked", [("locked", True), ("(locked yes)", True), ("(locked no)", False)]
)
def test_ring_move_respects_the_via_lock(tmp_path, lock, locked):
    source = _EDGE_VIA.replace("100.65", "100.8").replace("(via (at", f"(via {lock} (at")
    pcb = _load(tmp_path, source)
    original = pcb._sexp.to_string()
    result = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09)
    assert bool(result.moved) != locked
    if locked:
        assert result.skipped[0].reason == "via is locked"
        assert pcb._sexp.to_string() == original


@pytest.mark.parametrize(("member", "moves"), [("v1", False), ("s1", True)])
def test_ring_move_respects_nested_group_membership(tmp_path, member, moves):
    groups = (
        f'(group "fixture" (uuid inner) (members "{member}"))'
        '(group "assembly" (uuid outer) (locked yes) (members inner))'
    )
    via = _EDGE_VIA.replace("100.65", "100.8").replace("(via (at", "(via (locked no) (at")
    pcb = _load(tmp_path, groups + via)
    original = pcb._sexp.to_string()
    result = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09)
    assert bool(result.moved) == moves
    if not moves:
        assert result.skipped[0].reason == "via is locked"
        assert pcb._sexp.to_string() == original


def test_ring_dam_mode_leaves_a_ring_legal_via_alone(tmp_path):
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.95"))
    ring = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09)
    assert ring.moved == [] and ring.unresolvable == []
    assert pcb.vias[0].position[0] == pytest.approx(100.95)


@pytest.mark.parametrize(
    ("mask_layer", "via", "moves"),
    [
        ("", "via", False),
        ("F.Mask", "via", True),
        ("*.Mask", "via", True),
        ("B.Mask", "via blind", False),
    ],
)
def test_ring_dam_requires_a_mask_opening_on_the_via_side(tmp_path, mask_layer, via, moves):
    layers = f'(layers "F.Cu" "{mask_layer}")' if mask_layer else '(layers "F.Cu")'
    head = _HEAD.replace('(layers "F.Cu" "F.Mask")', layers)
    body = _EDGE_VIA.replace("100.65", "100.8").replace("(via (at", f"({via} (at")
    if via == "via blind":
        head = head.replace('(31 "B.Cu" signal)', '(1 "In1.Cu" signal) (31 "B.Cu" signal)')
        body = body.replace('(layers "F.Cu" "B.Cu")', '(layers "F.Cu" "In1.Cu")')
    pcb = _load(tmp_path, body, head=head)
    result = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09)
    assert len(result.moved) == int(moves)
    assert not result.unresolvable
    assert len(pcb.segments) == 1 + int(moves)
    assert pcb.vias[0].position == pytest.approx((100.9 if moves else 100.8, 100))


@pytest.mark.parametrize("exposed", [False, True])
def test_ring_dam_and_same_net_copper_slit_can_be_repaired_together(tmp_path, exposed):
    head = _HEAD if exposed else _HEAD.replace('(layers "F.Cu" "F.Mask")', '(layers "F.Cu")')
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "100.8"), head=head)
    # The annulus is tangent to the pad copper on both boards. Only one
    # has a mask opening. The independent 0.12 mm copper slit sets the exit.
    result = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09, same_net_pad_gap_mm=0.12)
    assert len(result.moved) == 1
    assert pcb.vias[0].position == pytest.approx((100.925, 100))
    assert not relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09, same_net_pad_gap_mm=0.12).moved


@pytest.mark.parametrize("neighbor_exposed", [False, True])
def test_ring_dam_uses_an_opening_when_accepting_a_candidate(tmp_path, neighbor_exposed):
    # At the first pad's exit (100.9), a second pad has a 0.06 mm ring gap
    # but a 0.21 mm drill gap. Only its exposed variant can veto that exit.
    layers = '"F.Cu" "F.Mask"' if neighbor_exposed else '"F.Cu"'
    neighbor = f"""(footprint "test:neighbor" (layer "F.Cu") (at 101.76 100)
        (pad "1" smd rect (at 0 0) (size 1 1) (layers {layers}) (net 1 "SIG1")))"""
    pcb = _load(tmp_path, neighbor + _EDGE_VIA.replace("100.65", "100.8"))
    result = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09)
    assert len(result.moved) == 1
    takes_first_exit = pcb.vias[0].position == pytest.approx((100.9, 100))
    assert takes_first_exit != neighbor_exposed


@pytest.mark.parametrize(
    ("version", "board_margin", "footprint_margin", "pad_margin", "expected_x"),
    [
        (20260206, 0.16, None, None, 101.075),
        (20260206, 0.0, 0.16, None, 101.075),
        (20260206, 0.0, None, 0.16, 101.075),
        (20260206, 0.16, 0.03, None, 101.0),
        (20260206, 0.0, 0.16, 0.03, 101.0),
        (20240201, 0.0, 0.16, 0.0, 101.075),
        (20240202, 0.0, 0.16, 0.0, 101.0),
        (20240201, 0.16, 0.0, None, 101.075),
        (20240202, 0.16, 0.0, None, 101.0),
    ],
)
def test_ring_dam_resolves_mask_margin_for_detection_and_move(
    tmp_path, version, board_margin, footprint_margin, pad_margin, expected_x
):
    head = _HEAD.replace("20240108", str(version))
    if footprint_margin is not None:
        head = head.replace("(at 100 100)", f"(at 100 100) (solder_mask_margin {footprint_margin})")
    if pad_margin is not None:
        head = head.replace("(size 1.0 1.0)", f"(size 1.0 1.0) (solder_mask_margin {pad_margin})")
    pcb = _load(tmp_path, _EDGE_VIA.replace("100.65", "101.0"), board_margin, head=head)
    result = relocate_pad_edge_vias(pcb, _RULES, ring_dam_mm=0.09)
    assert len(result.moved) == int(expected_x != 101.0)
    assert not result.unresolvable
    pcb.save(tmp_path / "saved.kicad_pcb")
    saved = PCB.load(tmp_path / "saved.kicad_pcb")
    assert saved.vias[0].position == pytest.approx((expected_x, 100))
    assert not relocate_pad_edge_vias(saved, _RULES, ring_dam_mm=0.09).moved
