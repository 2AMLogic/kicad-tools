"""``kct route-link`` keeps tracks out of non-plated mounting holes (Issue #10393).

Issue #10504: the clearance kept from the drill is the board's own
``hole_clearance`` rule, not a fixed 0.25 mm.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli import main  # noqa: E402
from kicad_tools.cli.route_link_cmd import _rules_for_link, _rules_from_args  # noqa: E402
from kicad_tools.router.link_router import (  # noqa: E402
    LinkRoute,
    LinkRouteRules,
    _build_model,
    append_link_route,
    route_clears_floors,
    route_link,
    terminal_for_pad,
)
from kicad_tools.schema.pcb import PCB  # noqa: E402
from tests.test_route_link_hv_8546 import _pad, _parse, _write  # noqa: E402

SIG = _pad(1, 105, 110, 1, "SIG") + _pad(2, 125, 110, 1, "SIG")


def _npth_rule(mm: float) -> str:
    """A ``hole_clearance`` rule only a non-plated pad matches (Issue #10504)."""
    return (
        f'(rule "npth drill" (constraint hole_clearance (min {mm}mm)) '
        "(condition \"B.Pad_Type == 'NPTH, mechanical'\"))\n"
    )


def _pth_rule(mm: float) -> str:
    """The same rule keyed on a plated pad: a bare NPTH drill does not match it."""
    return (
        f'(rule "pth drill" (constraint hole_clearance (min {mm}mm)) '
        "(condition \"B.Pad_Type == 'Through-hole'\"))\n"
    )


def _hv_rule(mm: float) -> str:
    """A copper ``clearance`` rule, which attaches ``board_rules``."""
    return (
        f'(rule "hv" (constraint clearance (min {mm}mm)) '
        "(condition \"A.NetName == 'SIG' && B.NetName == 'HV'\"))\n"
    )


def _dru(board: Path, *rules: str) -> None:
    board.with_suffix(".kicad_dru").write_text("(version 1)\n" + "".join(rules))


def _npth(x: float, y: float, size: float, drill: float = 0.6) -> str:
    """Unnumbered ``np_thru_hole`` pad on ``J9`` (the issue's mounting hole)."""
    return (
        f'  (footprint "MH" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000099") '
        f"(at {x} {y})\n"
        '    (fp_text reference "J9" (at 0 -1.5) (layer "F.SilkS"))\n'
        f'    (pad "" np_thru_hole circle (at 0 0) (size {size} {size}) (drill {drill}) '
        '(layers "*.Cu" "*.Mask")))\n'
    )


def _probe(board, capsys):
    rc = main(
        [
            "route-link",
            "--probe",
            "--format",
            "json",
            str(board),
            "--link",
            "SIG R1.1 R2.1",
            "--link-layers",
            "F.Cu",
        ]
    )
    return rc, json.loads(capsys.readouterr().out)["links"][0]


def _route(board, rules=None, **rule_kwargs):
    pcb = PCB.load(str(board))
    nets = {n.name: num for num, n in pcb.nets.items() if n.name}
    rules = rules or LinkRouteRules(**rule_kwargs)
    model = _build_model(pcb)
    route = route_link(
        pcb,
        nets["SIG"],
        "SIG",
        terminal_for_pad(pcb, "R1", "1"),
        terminal_for_pad(pcb, "R2", "1"),
        rules,
        model=model,
        allowed_layers={"F.Cu"},
    )
    return route, model, rules


# Model coordinates are relative to the board origin (100, 100), so the hole
# placed at (113, 110) in the file sits at (13, 10) here.
def _min_gap_to_hole(route, hx, hy, radius, half_w):
    from shapely.geometry import LineString, Point

    hole = Point(hx, hy)
    return min(
        LineString([(x1, y1), (x2, y2)]).distance(hole) - radius - half_w
        for x1, y1, x2, y2, _layer in route.segments
    )


def test_adjacent_hole_keeps_straight_route(tmp_path, capsys):
    board = _write(tmp_path, SIG + _npth(106, 111, 0.6))
    rc, link = _probe(board, capsys)
    assert rc == 0
    assert link["routable"]
    assert link["length_mm"] == pytest.approx(19.3, abs=0.01)


@pytest.mark.parametrize("size", [0.6, 0.8], ids=["drill-only", "under-0.8mm-pad"])
def test_centered_hole_is_detoured(tmp_path, capsys, size):
    board = _write(tmp_path, SIG + _npth(113, 110, size))
    rc, link = _probe(board, capsys)
    assert rc == 0
    assert link["routable"]
    # The straight 19.30 mm line crosses the drill; any legal path is longer.
    assert link["length_mm"] > 19.3 + 0.1


@pytest.mark.parametrize("size", [0.6, 0.8], ids=["drill-only", "under-0.8mm-pad"])
def test_routed_geometry_clears_hole_and_pad_copper(tmp_path, size):
    board = _write(tmp_path, SIG + _npth(113, 110, size))
    route, model, rules = _route(board)
    assert route is not None
    half_w = route.width_for("F.Cu") / 2
    # Track edge to hole edge keeps KiCad's default 0.25 mm hole clearance.
    assert _min_gap_to_hole(route, 13, 10, 0.3, half_w) >= rules.hole_clearance - 1e-3
    # The written route re-checks clean against every obstacle (incl. the hole).
    assert route_clears_floors(route, model, rules)


@pytest.mark.parametrize("with_hole", [False, True], ids=["no-hole", "mounting-hole"])
def test_existing_positional_layer_width_is_written_to_copper(tmp_path, with_hole):
    board = _write(tmp_path, SIG + (_npth(113, 111.8, 0.6) if with_hole else ""))
    # Existing callers can supply both width_by_layer and pad_entry_gap positionally.
    rules = LinkRouteRules(
        0.2,
        0.2,
        0.6,
        0.3,
        0.5,
        0.25,
        0.5,
        0.05,
        4.0,
        2.0,
        4.0,
        1_500_000,
        0.0,
        None,
        (),
        frozenset(),
        None,
        None,
        {"F.Cu": 0.45},
        0.1,
    )
    assert rules.pad_entry_gap == 0.1
    route, _, _ = _route(board, rules)
    assert route is not None
    append_link_route(board, route, PCB.load(str(board)).board_origin)
    assert {segment.width for segment in PCB.load(str(board)).segments} == {0.45}


def test_hole_clearance_is_a_rule_knob(tmp_path):
    """A wider ``hole_clearance`` pushes the same route further from the drill."""
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    route, _model, rules = _route(board, hole_clearance=1.5)
    assert route is not None
    half_w = route.width_for("F.Cu") / 2
    assert _min_gap_to_hole(route, 13, 10, 0.3, half_w) >= 1.5 - 1e-3


def test_hole_also_blocks_via_site(tmp_path):
    """The drill stays a via blocker (hole-to-hole) next to the new track blocker."""
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    model = _build_model(PCB.load(str(board)))
    assert any(abs(x - 13) < 1e-6 and abs(y - 10) < 1e-6 for x, y, _d in model.drills)


# ---------------------------------------------------------------------------
# Issue #10504: the drill floor is the board's own ``hole_clearance`` rule.
# ---------------------------------------------------------------------------


def _project(board: Path, hole_clearance: float | None = None, hvc: float | None = None) -> None:
    """A ``.kicad_pro`` declaring only ``min_hole_clearance`` and/or an ``HVC`` class."""
    rules = {} if hole_clearance is None else {"min_hole_clearance": hole_clearance}
    project: dict = {"meta": {"version": 3}, "board": {"design_settings": {"rules": rules}}}
    if hvc is not None:
        project["net_settings"] = {
            "meta": {"version": 3},
            "classes": [{"name": "Default", "clearance": 0.2}, {"name": "HVC", "clearance": hvc}],
            "netclass_assignments": {"HV": "HVC"},
        }
    board.with_suffix(".kicad_pro").write_text(json.dumps(project))


def _cli_rules(board: Path):
    """The rules ``kct route-link`` builds for ``SIG R1.1 R2.1`` on ``F.Cu``."""
    pcb = PCB.load(str(board))
    a = _parse(str(board), "--link", "SIG R1.1 R2.1", "--link-layers", "F.Cu")
    return _rules_for_link(a, pcb, _rules_from_args(a, pcb), a.links[0])


def _drill_floor(board: Path, rules, moving: str = "Track") -> float:
    """Clearance ``rules`` demands between the link's ``moving`` copper and the drill."""
    drill = next(it for it in _build_model(PCB.load(str(board))).items if it.hole)
    parts = rules.clearance_parts("SIG", drill.net, "F.Cu", drill.layers["F.Cu"], drill, moving)
    return parts[0][1]


def _routed_gap(board: Path, rules) -> float:
    """Minimum track-edge to drill-edge gap over every routed segment."""
    route = _route(board, rules=rules)[0]
    assert route is not None
    return _min_gap_to_hole(route, 13, 10, 0.3, route.width_for("F.Cu") / 2)


def _declare_hole_rule(board: Path, how: str) -> None:
    """Declare a 0.60 mm ``hole_clearance`` for NPTH drills the way ``how`` names."""
    if how == "project":
        _project(board, 0.6)
    elif how == "dru":
        _dru(board, _npth_rule(0.6))
    elif how == "dru-over-project":
        _project(board, 0.25)  # the custom rule overrides a lower min_hole_clearance
        _dru(board, _npth_rule(0.6))
    else:
        _dru(board, _hv_rule(0.3), _npth_rule(0.6))


@pytest.mark.parametrize(
    ("how", "copper_rules_attached"),
    [
        ("project", False),
        ("dru", False),
        ("dru-over-project", False),
        ("dru-beside-copper-rule", True),
    ],
)
def test_board_hole_rule_sets_the_drill_floor(tmp_path, how, copper_rules_attached):
    """0.60 mm declared by the project or a hole-only DRU is kept from the drill."""
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    _declare_hole_rule(board, how)
    rules = _cli_rules(board)
    assert (rules.board_rules is not None) is copper_rules_attached
    assert _drill_floor(board, rules) == pytest.approx(0.6)
    assert _routed_gap(board, rules) >= 0.6 - 1e-3
    route, model, _rules = _route(board, rules=rules)
    assert route_clears_floors(route, model, rules)


def test_project_hole_rule_reaches_the_cli_probe(tmp_path, capsys):
    """``kct route-link --probe`` itself detours wider around the drill."""
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    _project(board, 0.6)
    rc, link = _probe(board, capsys)
    assert rc == 0 and link["routable"]
    wide = link["length_mm"]
    board.with_suffix(".kicad_pro").unlink()
    rc, link = _probe(board, capsys)
    assert rc == 0 and link["routable"]
    assert wide > link["length_mm"]


def test_plated_only_rule_leaves_the_drill_at_its_fallback_floor(tmp_path):
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    _dru(board, _pth_rule(0.6))
    rules = _cli_rules(board)
    assert _drill_floor(board, rules) == pytest.approx(0.25)
    assert 0.25 - 1e-3 <= _routed_gap(board, rules) < 0.6 - 1e-3


def test_without_sidecars_the_drill_floor_is_the_default(tmp_path):
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    rules = _cli_rules(board)
    assert rules.board_rules is None and rules.hole_rules is None
    assert _drill_floor(board, rules) == pytest.approx(0.25)


def test_larger_explicit_and_per_net_floors_still_win(tmp_path):
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    _project(board, 0.6)
    rules = _cli_rules(board)
    explicit = replace(rules, hole_clearance=1.5)
    assert _drill_floor(board, explicit) == pytest.approx(1.5)
    assert _routed_gap(board, explicit) >= 1.5 - 1e-3
    assert _drill_floor(board, replace(rules, clearance_by_net={0: 2.0})) == pytest.approx(2.0)


@pytest.mark.parametrize("how", ["project", "dru"])
def test_hole_rule_keeps_the_authored_netclass_floors(tmp_path, how):
    """No copper rule, so ``board_rules`` stays unset and the #8650 floors still apply."""
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    _project(board, 0.6 if how == "project" else None, hvc=0.4)
    if how == "dru":
        _dru(board, _npth_rule(0.6))
    rules = _cli_rules(board)
    assert rules.board_rules is None
    assert rules.clearance_for(2) == pytest.approx(0.4)
    assert _drill_floor(board, rules) == pytest.approx(0.6)


def test_route_clears_floors_judges_the_drill_by_the_resolved_floor(tmp_path):
    """A finished track 0.45 mm from the drill edge: legal at 0.25 mm, not at 0.60 mm."""
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    model = _build_model(PCB.load(str(board)))
    candidate = LinkRoute(1, "SIG", 0.2, 0.6, 0.3, segments=[(3, 10.85, 23, 10.85, "F.Cu")])
    assert _min_gap_to_hole(candidate, 13, 10, 0.3, 0.1) == pytest.approx(0.45)
    assert route_clears_floors(candidate, model, LinkRouteRules())
    _project(board, 0.6)
    assert not route_clears_floors(candidate, model, _cli_rules(board))


@pytest.mark.parametrize(
    "predicate",
    ["B.Pad_Type == 'NPTH, mechanical'", "!B.isPlated()", "B.memberOfFootprint('J9')"],
)
def test_drill_keeps_its_npth_pad_identity_for_rule_conditions(tmp_path, predicate):
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    _dru(board, f'(rule "r" (constraint hole_clearance (min 0.6mm)) (condition "{predicate}"))\n')
    assert _drill_floor(board, _cli_rules(board)) == pytest.approx(0.6)


def test_via_only_hole_rule_reaches_the_via_mask_alone(tmp_path):
    board = _write(tmp_path, SIG + _npth(113, 110, 0.6))
    _dru(
        board,
        '(rule "via to drill" (constraint hole_clearance (min 1mm)) '
        "(condition \"A.Type == 'Via'\"))\n",
    )
    rules = _cli_rules(board)
    assert _drill_floor(board, rules, moving="Track") == pytest.approx(0.25)
    assert _drill_floor(board, rules, moving="Via") == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("blocker", "rule", "kind"),
    [
        (_npth(106.5, 110, 0.6), _npth_rule(6), "hole"),
        (_pad(3, 106.5, 110, 2, "HV"), _hv_rule(6), "pad"),
    ],
    ids=["drill", "pad"],
)
def test_probe_blocker_kind_distinguishes_drill_from_pad(tmp_path, capsys, blocker, rule, kind):
    """A drill that seals the terminal is reported as ``hole``; a pad stays ``pad``."""
    board = _write(tmp_path, SIG + blocker)
    _dru(board, rule)
    rc, link = _probe(board, capsys)
    assert rc == 1 and not link["routable"]
    assert link["blocker"]["kind"] == kind
    assert link["blocker"]["required_mm"] == pytest.approx(6.1)
