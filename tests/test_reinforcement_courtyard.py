"""Tests for the ``wire_run_courtyard_overlap`` DRC rule (Issue #8820)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.pcb.reinforce import reinforce_net
from kicad_tools.pcb.reinforcement_sidecar import (
    build_wire_records,
    load_wires_if_present,
    write_sidecar,
)
from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint
from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic, Pad
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.reinforcement_courtyard import ReinforcementCourtyardRule
from tests.test_pcb_reinforce import _add_th_pad_footprint, _straight_run_pcb

NET = "FUSED_LINE"


def _rules() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
    )


def _spec(eligible: bool = True) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="FORCE",
        net_name=NET,
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=10.0,
        reinforcement_eligible=eligible,
    )


def _board() -> PCB:
    pcb = _straight_run_pcb(length=100.0)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=NET, drill=1.0, size=2.0)
    reinforce_net(pcb, NET)
    return pcb


def _body(
    pcb: PCB,
    ref: str,
    x: float,
    y: float,
    *,
    side: str = "F",
    half: float = 3.0,
    courtyard: bool = True,
    pads: list[Pad] | None = None,
) -> Footprint:
    suffix = "Cu"
    layer = f"{side}.{suffix}"
    gl = [
        FootprintGraphic(
            graphic_type="rect",
            layer=f"{side}.CrtYd" if courtyard else f"{side}.Fab",
            stroke_width=0.05,
            start=(-half, -half),
            end=(half, half),
        )
    ]
    fp = Footprint(
        name="X:Body",
        layer=layer,
        position=(x, y),
        rotation=0.0,
        reference=ref,
        value="V",
        pads=pads or [],
        graphics=gl,
    )
    pcb._footprints.append(fp)
    return fp


def _run(pcb: PCB, eligible: bool = True):
    return ReinforcementCourtyardRule(specs=[_spec(eligible)]).check(pcb, _rules())


def _msgs(results) -> list[str]:
    return [v.message for v in results]


def test_baseline_clean():
    assert not list(_run(_board()))


def test_run_under_same_side_courtyard_errors():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0)
    res = _run(pcb)
    runs = [v for v in res if "[run]" in v.message]
    assert runs and all(v.severity == "error" for v in runs)
    assert "R1" in runs[0].message


def test_run_on_empty_side_is_clean():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0, side="B")
    assert not [v for v in _run(pcb) if "[run]" in v.message]


def test_no_courtyard_falls_back_to_fab_bbox():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0, courtyard=False)
    runs = [v for v in _run(pcb) if "[run]" in v.message]
    assert runs and "R1" in runs[0].message


def test_opposite_side_tht_foreign_pin_errors_own_pin_clean():
    def tht(net_name: str) -> Pad:
        pcb_net = pcb.add_net(net_name)
        return Pad(
            number="1",
            type="thru_hole",
            shape="circle",
            position=(0.0, 0.0),
            size=(2.0, 2.0),
            layers=["*.Cu"],
            net_number=pcb_net.number,
            net_name=net_name,
            drill=1.0,
        )

    pcb = _board()
    _body(pcb, "Q1", 70.0, 51.0, side="B", courtyard=True, pads=[tht("OTHER")])
    # THT part placed on the back; run is on F.Cu (opposite side): only pins count.
    pcb._footprints[-1].graphics = []
    assert [v for v in _run(pcb) if "[run]" in v.message and "Q1" in v.message]

    pcb = _board()
    _body(pcb, "Q1", 70.0, 51.3, side="B", courtyard=False, pads=[tht(NET)])
    pcb._footprints[-1].graphics = []
    assert not [v for v in _run(pcb) if "Q1" in v.message]


def test_same_net_component_drill_is_clear_of_the_fitted_wire():
    pcb = _board()
    from kicad_tools.sexp import parse_string

    # KiCad 10.0.8 exports X70.0Y-51.2 for this offset land, just as for
    # a plain 1.70 mm drill. The wire runs at y=50.
    pad = Pad.from_sexp(
        parse_string(
            f'(pad "1" thru_hole circle (at 0 0) (size 2.8 2.8) '
            f'(drill 1.7 (offset 0 0.6)) (layers "*.Cu") (net {pcb.add_net(NET).number} "{NET}"))'
        )
    )
    fp = _body(pcb, "R6", 70, 51.20, side="B", pads=[pad])
    matches = [v for v in _run(pcb) if "component drill" in v.message and "R6.1" in v.message]
    assert len(matches) == 1
    assert round(matches[0].actual_value, 3) == 0.350
    assert "wire diameter 1.2910 mm" in matches[0].message
    assert "0.2955 mm interference" in matches[0].message
    assert matches[0].required_value == 0.6455
    fp.position = (70, 50)
    crossing = [v for v in _run(pcb) if "component drill" in v.message and "R6.1" in v.message]
    assert len(crossing) == 1 and "at least 0.6455 mm" in crossing[0].message
    fp.position = (70, 52.50)
    assert not [v for v in _run(pcb) if "R6.1" in v.message]
    # The end terminal is connected to an unwired copper tail beyond the anchor.
    fp.position = (20.5, 50)
    assert not [v for v in _run(pcb) if "R6.1" in v.message]


def test_anchor_sized_component_bore_between_verified_end_vias():
    pcb = _board()
    _add_th_pad_footprint(pcb, ref="R6", x=70, y=50, net=NET, drill=1.40, size=2.8)
    pcb.get_footprint("R6").layer = "B.Cu"
    findings = [v for v in _run(pcb) if "component drill" in v.message]
    assert len(findings) == 1
    assert findings[0].items == (NET, "R6", "1")


def test_component_bore_projecting_onto_end_via_is_still_checked():
    pcb = _board()
    x, y = pcb.vias[0].position
    _add_th_pad_footprint(pcb, ref="R6", x=x, y=y + 1.1, net=NET, drill=1.40, size=2.8)
    pcb.get_footprint("R6").layer = "B.Cu"
    assert any("component drill" in v.message and "R6.1" in v.message for v in _run(pcb))


def test_same_net_plated_pad_can_serve_as_a_verified_wire_anchor():
    pcb = _board()
    anchor = pcb.vias[0]
    pcb.remove_vias([anchor])
    x, y = anchor.position
    _add_th_pad_footprint(pcb, ref="W1", x=x, y=y, net=NET, drill=anchor.drill, size=anchor.size)
    pcb.get_footprint("W1").layer = "B.Cu"
    rule = ReinforcementCourtyardRule(specs=[_spec()])
    assert len(rule.fully_wired_legs(pcb, _rules())) == 1
    assert not [v for v in rule.check(pcb, _rules()) if "W1" in v.message]


@pytest.mark.parametrize("pad_ends", [(False, True), (True, False), (True, True), (False, False)])
@pytest.mark.parametrize("drill", [1.70, 1.40])
def test_component_bore_near_mixed_end_anchors_is_still_checked(pad_ends, drill):
    """Each end resolves on its own, so a decoy lead hole is never a third anchor."""
    pcb = _board()
    vias = list(pcb.vias)
    for idx, as_pad in enumerate(pad_ends):
        if not as_pad:
            continue
        via = vias[idx]
        pcb.remove_vias([via])
        x, y = via.position
        ref = f"W{idx + 1}"
        _add_th_pad_footprint(pcb, ref=ref, x=x, y=y, net=NET, drill=via.drill, size=via.size)
        pcb.get_footprint(ref).layer = "B.Cu"
    x, y = vias[0].position
    _add_th_pad_footprint(pcb, ref="R6", x=x + 5.0, y=y + 0.5, net=NET, drill=drill, size=2.8)
    pcb.get_footprint("R6").layer = "B.Cu"
    rule = ReinforcementCourtyardRule(specs=[_spec()])
    assert len(rule.fully_wired_legs(pcb, _rules())) == 1
    bores = [v for v in rule.check(pcb, _rules()) if "component drill" in v.message]
    assert [v.items for v in bores] == [(NET, "R6", "1")]
    assert not [v for v in bores if "W1" in v.message or "W2" in v.message]


def test_end_hole_under_opposite_side_body_errors():
    pcb = _board()
    # Anchor holes sit near the run ends; put a back-side body on the first one.
    anchors = [tuple(v.position) for v in pcb.vias]
    assert anchors
    x, y = anchors[0]
    _body(pcb, "U9", x, y, side="B", half=0.5)
    res = [v for v in _run(pcb) if "[end hole]" in v.message and "U9" in v.message]
    assert res and "back" in res[0].message


def test_end_hole_clear_is_clean():
    assert not [v for v in _run(_board()) if "[end hole]" in v.message]


def test_no_eligible_path_is_noop():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0)
    assert not list(_run(pcb, eligible=False))


def test_registered():
    from kicad_tools.cli.check_categories import CHECK_CATEGORIES
    from kicad_tools.validate.checker import DRCChecker
    from kicad_tools.validate.coverage import BLOCKING_CATEGORIES

    assert "wire_run_courtyard_overlap" in CHECK_CATEGORIES
    assert "wire_run_courtyard_overlap" in BLOCKING_CATEGORIES
    assert "check_wire_run_courtyard_overlap" in DRCChecker.CHECK_ALL_METHODS


# --- reinforce consults the same overlap geometry (Issue #8821) -------------


def _bare_board() -> PCB:
    pcb = _straight_run_pcb(length=100.0)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=NET, drill=1.0, size=2.0)
    return pcb


def test_reinforce_refuses_run_under_same_side_courtyard():
    pcb = _bare_board()
    _body(pcb, "R1", 70.0, 50.0)
    before = len(pcb.vias)
    res = reinforce_net(pcb, NET, design_rules=_rules())
    assert res.placed_count == 0
    assert len(pcb.vias) == before
    leg = res.legs[0]
    assert leg.status == "unanchored"
    assert "R1" in leg.reason and "courtyard" in leg.reason


def test_reinforce_dry_run_reports_footprint():
    pcb = _bare_board()
    _body(pcb, "R1", 70.0, 50.0)
    res = reinforce_net(pcb, NET, design_rules=_rules(), dry_run=True)
    assert "R1" in res.legs[0].reason


def test_reinforce_clean_when_courtyard_on_other_side():
    pcb = _bare_board()
    _body(pcb, "R1", 70.0, 50.0, side="B")
    res = reinforce_net(pcb, NET, design_rules=_rules())
    assert res.legs[0].status == "full"
    assert not [v for v in _run(pcb) if "[run]" in v.message]


def test_reinforce_prefers_unobstructed_side():
    pcb = _bare_board()
    # Longer copper on F.Cu (blocked) plus a shorter run on B.Cu (clear).
    for x0 in (30.0, 50.0):
        pcb.add_trace((x0, 80.0), (x0 + 10.0, 80.0), width=2.0, layer="B.Cu", net=NET)
    _body(pcb, "R1", 70.0, 50.0)
    res = reinforce_net(pcb, NET, design_rules=_rules(), all_runs=True)
    assert res.layer == "B.Cu"


def test_reinforce_both_sides_blocked_refused_with_reason():
    pcb = _bare_board()
    pcb.add_trace((30.0, 80.0), (60.0, 80.0), width=2.0, layer="B.Cu", net=NET)
    _body(pcb, "R1", 70.0, 50.0)
    _body(pcb, "R2", 45.0, 80.0, side="B")
    res = reinforce_net(pcb, NET, design_rules=_rules(), all_runs=True, layer="B.Cu")
    assert res.placed_count == 0
    assert any("R2" in leg.reason for leg in res.legs if leg.layer == "B.Cu")


# --- finding location and doubled copper (Issue #8866) ----------------------


def test_run_finding_is_located_on_the_run_not_origin():
    pcb = _board()
    _body(pcb, "R1", 70.0, 50.0)
    runs = [v for v in _run(pcb) if "[run]" in v.message]
    assert runs
    for v in runs:
        x, y = v.location
        assert (x, y) != (0.0, 0.0)
        assert abs(y - 50.0) < 1e-6  # on the straight F.Cu run at y=50
        assert 20.0 <= x <= 120.0
        assert abs(x - 70.0) <= 3.0 + 1.0 + 1.0 + 1.5  # near the courtyard


def test_pin_finding_is_located_on_the_run():
    pcb = _board()
    pcb.add_net("OTHER")
    pad = Pad(
        number="1",
        type="thru_hole",
        shape="circle",
        position=(0.0, 0.0),
        size=(2.0, 2.0),
        layers=["*.Cu"],
        net_number=pcb.get_net_by_name("OTHER").number,
        net_name="OTHER",
        drill=1.0,
    )
    _body(pcb, "Q1", 70.0, 51.0, side="B", pads=[pad])
    pcb._footprints[-1].graphics = []
    pins = [v for v in _run(pcb) if "pin 1" in v.message]
    assert pins
    assert abs(pins[0].location[1] - 50.0) < 1e-6
    assert abs(pins[0].location[0] - 70.0) < 1e-6


def test_doubled_copper_routed_around_body_is_clean():
    """Stitched F.Cu/B.Cu copper that detours around a body: the run follows
    the copper, never the endpoint chord, so no overlap is reported."""
    pcb = PCB.create(width=200, height=100, center=False)
    detour = [(20.0, 50.0), (60.0, 50.0), (60.0, 70.0), (80.0, 70.0), (80.0, 50.0), (120.0, 50.0)]
    for layer in ("F.Cu", "B.Cu"):
        for a, b in zip(detour, detour[1:], strict=False):
            pcb.add_trace(a, b, width=2.6, layer=layer, net=NET)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=NET, drill=1.0, size=2.0)
    # Body sits on the chord between the ends, inside the detour.
    _body(pcb, "R1", 70.0, 50.0, half=3.0)
    assert not [v for v in _run(pcb) if "[run]" in v.message and "R1" in v.message]


def test_ambiguous_net_wire_across_a_courtyard_is_reported():
    """Issue #8886: a wire on a net with a loop-closing tap is still checked."""
    from tests.test_reinforcement_incomplete import _ambiguous_wire_board, _resolution_status

    pcb = _ambiguous_wire_board()
    assert _resolution_status(pcb) == "ambiguous"
    assert not list(_run(pcb))
    _body(pcb, "R1", 80.0, 50.0, side="B")
    runs = [v for v in _run(pcb) if "[run]" in v.message]
    assert runs and "R1" in runs[0].message


# --- reinforce vets end holes against courtyards (Issue #9815) --------------


def _natural_start() -> tuple[float, float]:
    """Where reinforce puts the start anchor on the unobstructed board."""
    res = reinforce_net(_bare_board(), NET, design_rules=_rules(), dry_run=True)
    a = res.legs[0].anchors[0]
    return (a.x, a.y)


def test_hole_courtyard_overlaps_is_the_checks_test():
    from kicad_tools.validate.rules.reinforcement_courtyard import hole_courtyard_overlaps

    pcb = _bare_board()
    fp = _body(pcb, "U9", 40.0, 50.0, side="B", half=0.5)
    net = pcb.get_net_by_name(NET).number
    assert hole_courtyard_overlaps((40.0, 50.0), 0.6, pcb.footprints, net) == [(fp, "B")]
    # Envelope = radius + HOLE_FILLET_MM: 0.6 + 0.75 reaches 1.35 mm past the hole.
    assert hole_courtyard_overlaps((41.8, 50.0), 0.6, pcb.footprints, net) == [(fp, "B")]
    assert not hole_courtyard_overlaps((41.9, 50.0), 0.6, pcb.footprints, net)


def test_hole_courtyard_overlaps_keeps_terminating_pad_exemption():
    from kicad_tools.validate.rules.reinforcement_courtyard import hole_courtyard_overlaps

    pcb = _bare_board()
    other = pcb.add_net("OTHER")

    def pad(net_number: int, net_name: str) -> Pad:
        return Pad(
            number="1",
            type="thru_hole",
            shape="circle",
            position=(0.0, 0.0),
            size=(2.0, 2.0),
            layers=["*.Cu"],
            net_number=net_number,
            net_name=net_name,
            drill=1.0,
        )

    net = pcb.get_net_by_name(NET).number
    _body(pcb, "J5", 40.0, 30.0, half=2.0, pads=[pad(net, NET)])
    assert not hole_courtyard_overlaps((40.0, 30.0), 0.6, pcb.footprints, net)
    fp = _body(pcb, "J6", 60.0, 30.0, half=2.0, pads=[pad(other.number, "OTHER")])
    assert hole_courtyard_overlaps((60.0, 30.0), 0.6, pcb.footprints, net) == [(fp, "F")]


def test_reinforce_slides_end_anchor_out_of_opposite_side_courtyard():
    x, y = _natural_start()
    pcb = _bare_board()
    _body(pcb, "U9", x, y, side="B", half=0.5)
    dry = reinforce_net(pcb, NET, design_rules=_rules(), dry_run=True)
    leg = dry.legs[0]
    assert leg.status == "full"
    start = leg.anchors[0]
    assert start.shift_mm > 0
    # First clear position: just past the courtyard edge + hole envelope.
    assert start.x > x + 0.5
    assert "U9" in leg.reason and "back-side courtyard" in leg.reason

    res = reinforce_net(pcb, NET, design_rules=_rules())
    assert res.legs[0].status == "full"
    assert not [v for v in _run(pcb) if "[end hole]" in v.message]
    assert not [v for v in _run(pcb) if v.rule_id == "wire_run_courtyard_overlap"]


def test_reinforce_courtyard_over_whole_shift_leaves_leg_partial():
    pcb = _bare_board()
    # Back-side body covering x = 20..50: beyond the 25 mm end-shift bound.
    _body(pcb, "U9", 35.0, 50.0, side="B", half=15.0)
    res = reinforce_net(pcb, NET, design_rules=_rules(), dry_run=True)
    leg = res.legs[0]
    assert leg.status == "partial"
    assert len(leg.anchors) == 1 and leg.anchors[0].x > 50.0
    assert res.refused
    r = res.refused[0]
    assert r.rule == "anchor_courtyard"
    assert r.obstacle_footprint == "U9" and r.obstacle_side == "B"
    assert "U9" in r.reason and "courtyard" in r.reason

    res = reinforce_net(pcb, NET, design_rules=_rules())
    assert not [v for v in _run(pcb) if "[end hole]" in v.message]


def test_reinforce_cli_dry_run_lists_courtyard_refusal(tmp_path, capsys, monkeypatch):
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_pcb_reinforce import _reinforce_args

    def board() -> PCB:
        pcb = _bare_board()
        _body(pcb, "U9", 35.0, 50.0, side="B", half=15.0)
        return pcb

    path = tmp_path / "b.kicad_pcb"
    _bare_board().save(path)
    # In-memory test footprints do not serialize: hand the CLI the board itself.
    monkeypatch.setattr(PCB, "load", classmethod(lambda cls, *a, **k: board()))

    assert run_pcb_command(_reinforce_args(path, dry_run=True, format="json")) == 0
    out = json.loads(capsys.readouterr().out)
    refused = [r for r in out["refused"] if r["rule"] == "anchor_courtyard"]
    assert refused and refused[0]["obstacle_footprint"] == "U9"
    assert refused[0]["obstacle_side"] == "B"
    assert "U9" in refused[0]["reason"]

    assert run_pcb_command(_reinforce_args(path, dry_run=True)) == 0
    text = capsys.readouterr().out
    assert "refused at" in text and "courtyard of U9" in text and "in U9" in text


def test_mixed_wire_assembly_checks_actual_wire_and_anchor_bodies():
    """An eligible path continues from the wired front side on bare back copper."""
    pcb = PCB.create(width=200, height=100, center=True)
    assert pcb.board_origin != (0, 0)
    for ref, (x, y) in (("J1", (20, 50)), ("J2", (120, 50)), ("J3", (99, 70))):
        _add_th_pad_footprint(pcb, ref=ref, x=x, y=y, net=NET, drill=1, size=2)
    pcb.add_trace((20, 50), (120, 50), width=2, layer="F.Cu", net=NET)
    pcb.add_trace((120, 50), (99, 70), width=5.5, layer="B.Cu", net=NET)
    spec = replace(_spec(), sink=PathEndpoint("J3", "1"))
    plan = reinforce_net(pcb, NET, design_rules=_rules(), layer="F.Cu", current_paths=[spec])
    # A drilled center may lie off the trace centerline, within its copper.
    front_leg = next(leg for leg in plan.legs if leg.layer == "F.Cu")
    for via, anchor in zip(pcb.vias, front_leg.anchors, strict=True):
        via.position = (via.position[0], via.position[1] + 0.3)
        anchor.y += 0.3
    records = build_wire_records(plan, *pcb.board_origin)
    assert sorted(r["status"] for r in records) == ["full", "unhandled"]
    _body(pcb, "COPPER_ONLY", 109.5, 60, side="B", half=0.5)
    _body(pcb, "OVER_WIRE", 70, 50)
    _body(pcb, "OVER_TAIL", *pcb.vias[-1].position, side="B", half=0.5)
    # The declared path returns to the fitted layer on a separate bare leg.
    _add_th_pad_footprint(pcb, ref="J4", x=70, y=70, net=NET, drill=1, size=2)
    pcb.add_trace((99, 70), (70, 70), width=5.5, layer="F.Cu", net=NET)
    spec = replace(spec, sink=PathEndpoint("J4", "1"))
    _body(pcb, "BARE_FRONT", 85, 70, half=0.5)

    def messages(records):
        checker = DRCChecker(pcb, current_path_specs=[spec], reinforcement_wires=records)
        return _msgs(checker.check_wire_run_courtyard_overlap())

    fitted = messages(records)
    assert all("COPPER_ONLY" not in m and "BARE_FRONT" not in m for m in fitted)
    assert any("[run]" in m and "F.Cu" in m and "OVER_WIRE" in m for m in fitted)
    assert any("[end hole]" in m and "OVER_TAIL" in m and "opposite" in m for m in fitted)
    assert not any("[run]" in m and "OVER_TAIL" in m for m in fitted)
    assert any("COPPER_ONLY" in m for m in messages([]))
    for record in records:
        for point in record["anchors"]:
            point["y"] -= 0.3
    assert any("COPPER_ONLY" in m for m in messages(records))


@pytest.mark.parametrize("wire", ["absent", "current", "stale", "unrecorded", "mask_gap"])
def test_parallel_copper_checks_only_a_fitted_wire(wire):
    pcb = _bare_board()
    spec = _spec()
    wires = []
    if wire != "absent":
        wire_plan = reinforce_net(pcb, NET, current_paths=[spec], strategy="wire")
        wires = build_wire_records(wire_plan, *pcb.board_origin)
        assert [w["status"] for w in wires] == ["full"]
    plan = reinforce_net(pcb, NET, current_paths=[spec], strategy="parallel")
    records = build_wire_records(plan, *pcb.board_origin)
    assert any(r["copy_status"] == "placed" for r in records)
    for side in ("F", "B"):
        _body(pcb, f"OVER_{side}", 70, 50, side=side)
    if wire != "absent":
        anchor = wires[0]["anchors"][0]
        _body(pcb, "FILLET", anchor["x"], anchor["y"], side="B", half=0.5)
    if wire == "stale":
        wires[0]["anchors"][0]["y"] += 0.5
    elif wire == "unrecorded":
        wires.clear()
    elif wire == "mask_gap":
        strip = next(g for g in pcb.graphic_lines if g.layer == "F.Mask")
        strip.end = (strip.end[0] - 1, strip.end[1])
    checker = DRCChecker(pcb, current_path_specs=[spec], reinforcement_wires=records + wires)
    messages = _msgs(checker.check_wire_run_courtyard_overlap())
    if wire != "absent":
        assert any("[run]" in m and "OVER_F" in m for m in messages)
        assert not any("OVER_B" in m for m in messages)
        assert any("[end hole]" in m and "FILLET" in m and "opposite" in m for m in messages)
    else:
        assert messages == []
    incomplete = _msgs(checker.check_reinforcement_incomplete())
    if wire == "mask_gap":
        assert any("[b]" in m for m in incomplete)
    else:
        assert incomplete == []


def test_recorded_wire_leaves_bare_copper_between_hole_and_terminal():
    pcb = _bare_board()
    result = reinforce_net(pcb, NET, design_rules=_rules(), end_offset_mm=12)
    assert result.placed_count == 2
    ends = [{"x": a.x, "y": a.y} for a in result.legs[0].anchors]
    assert min(p["x"] for p in ends) > 29
    _body(pcb, "BARE_STUB", 24, 50, half=0.5)
    _body(pcb, "OVER_WIRE", 60, 50)
    rule = ReinforcementCourtyardRule(
        specs=[_spec()],
        wires=[{"net": NET, "layer": "F.Cu", "status": "full", "anchors": ends}],
        end_offset_mm=12,
    )
    messages = _msgs(rule.check(pcb, _rules()))
    assert not any("BARE_STUB" in m for m in messages)
    assert any("OVER_WIRE" in m for m in messages)


def test_fitted_wire_follows_bend_and_clears_its_chord():
    pcb = PCB.create(width=130, height=80, center=False)
    for ref, x, y in [("J1", 20, 20), ("J2", 100, 60)]:
        _add_th_pad_footprint(pcb, ref=ref, x=x, y=y, net=NET, drill=1, size=2)
    pcb.add_trace((20, 20), (100, 20), width=2, layer="B.Cu", net=NET)
    pcb.add_trace((100, 20), (100, 60), width=2, layer="B.Cu", net=NET)
    plan = reinforce_net(pcb, NET, current_paths=[_spec()])
    records = build_wire_records(plan, *pcb.board_origin)
    assert len(records) == 1 and records[0]["status"] == "full"
    _body(pcb, "BEND", 100, 20, side="B")
    _body(pcb, "CHORD", 60, 40, side="B")
    _add_th_pad_footprint(pcb, ref="PIN", x=100, y=40, net="OTHER", drill=1, size=2)
    checker = DRCChecker(pcb, current_path_specs=[_spec()], reinforcement_wires=records)
    messages = _msgs(checker.check_wire_run_courtyard_overlap())
    assert any("[run]" in m and "BEND" in m for m in messages)
    assert any("[run]" in m and "foreign-net pin 1 of PIN" in m for m in messages)
    assert not any("CHORD" in m for m in messages)


def test_adopted_wire_sidecar_checks_same_span_as_planned_wire(tmp_path):
    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_pcb_reinforce import _reinforce_args

    pcb = _bare_board()
    pcb.add_trace((120, 50), (120, 80), width=5.5, layer="B.Cu", net=NET)
    path = tmp_path / "planned.kicad_pcb"
    plan = reinforce_net(pcb, NET, layer="F.Cu")
    pcb.save(path)
    write_sidecar(path, build_wire_records(plan, *pcb.board_origin))
    adopted = tmp_path / "adopted.kicad_pcb"
    adopted.write_bytes(path.read_bytes())
    assert run_pcb_command(_reinforce_args(adopted, adopt=True)) == 0
    assert adopted.read_bytes() == path.read_bytes()

    pcb = PCB.load(path)
    # The test terminal footprints are in-memory fixtures.
    for ref, x, y in [("J1", 20, 50), ("J2", 120, 50), ("J3", 120, 80)]:
        _add_th_pad_footprint(pcb, ref=ref, x=x, y=y, net=NET, drill=1, size=2)
    spec = replace(_spec(), sink=PathEndpoint("J3", "1"))
    _body(pcb, "WIRE", 70, 50)
    _body(pcb, "BARE", 120, 65, side="B")
    for board, status in [(path, "full"), (adopted, "adopted")]:
        records = load_wires_if_present(board)
        assert any(r["status"] == status for r in records)
        checker = DRCChecker(pcb, current_path_specs=[spec], reinforcement_wires=records)
        messages = _msgs(checker.check_wire_run_courtyard_overlap())
        assert any("[run]" in m and "WIRE" in m for m in messages)
        assert not any("BARE" in m for m in messages)


def test_recorded_anchor_also_serves_as_layer_change():
    pcb = _bare_board()
    plan = reinforce_net(pcb, NET, layer="F.Cu")
    records = build_wire_records(plan, *pcb.board_origin)
    x, y = pcb.vias[-1].position
    _add_th_pad_footprint(pcb, ref="J3", x=x, y=80, net=NET, drill=1, size=2)
    pcb.add_trace((x, y), (x, 80), width=5.5, layer="B.Cu", net=NET)
    spec = replace(_spec(), sink=PathEndpoint("J3", "1"))
    _body(pcb, "WIRE", 70, 50)
    _body(pcb, "FILLET", x, y, side="B", half=0.5)
    _body(pcb, "BARE", x, 65, side="B", half=0.5)
    checker = DRCChecker(pcb, current_path_specs=[spec], reinforcement_wires=records)
    messages = _msgs(checker.check_wire_run_courtyard_overlap())
    assert any("[run]" in m and "F.Cu" in m and "WIRE" in m for m in messages)
    assert any("[end hole]" in m and "opposite" in m and "FILLET" in m for m in messages)
    assert not any("[run]" in m and "B.Cu" in m for m in messages)
