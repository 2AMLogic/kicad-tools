"""Board 05 recipe rule reproduction (#8357, #10430).

Generate the revision-B board from source and check its reviewed net classes,
widths and final exported rules. Exercise export of the released package
independently until #9921 ships the revised sidecars with their matching copper.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult
from kicad_tools.manufacturers.attach_zone_dru import (
    DRU_ATTACH_ZONE_BLOCK_END,
    has_attach_zone_block,
)
from kicad_tools.manufacturers.zone_clearance_dru import zone_clearance_rule, zone_clearance_targets
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD = REPO_ROOT / "boards" / "05-bldc-motor-controller"
OUTPUT = BOARD / "output"
REDESIGN = BOARD / "redesign"
STEM = "bldc_controller_routed"


def _load_build():
    sys.path.insert(0, str(REDESIGN))
    try:
        spec = importlib.util.spec_from_file_location("board05_build_8357", REDESIGN / "build.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(REDESIGN))


def _assert_u3_pour_clearance(text: str) -> None:
    # KiCad gives the last matching rule precedence. Both item orders must
    # cover exactly the five stock pads while leaving the 42 exemptions intact.
    marker = '(rule "Zone clearance at U3 pads - GND (F.Cu)"'
    assert text.count(marker) == 1
    assert text.count('(rule "kct attach zone - ') == 42
    assert reviewed_clearances(text) == REVIEWED_CLEARANCES
    assert text.count('(rule "Zone clearance - ') == 4
    assert text.count('(rule "Ampacity Min Width') == 2
    assert text.rindex('(rule "kct attach zone - ') < text.index(marker)
    rule = text.split(marker)[1].split(DRU_ATTACH_ZONE_BLOCK_END)[0]
    assert '(layer "F.Cu")' in rule
    assert "(constraint clearance (min 0.2mm))" in rule
    for zone, pad in (("A", "B"), ("B", "A")):
        numbers = " || ".join(f"{pad}.Pad_Number == '{n}'" for n in (1, 3, 5, 7, 8))
        assert (
            f"({zone}.Type == 'Zone' && {zone}.NetName == 'GND' && "
            f"{pad}.Type == 'Pad' && {pad}.Reference == 'U3' && ({numbers}))"
        ) in rule


#: Each net class the staged project declares, and the clearance its
#: ``Reviewed clearance`` DRU rule holds its nets to (#10430).  ``Motor power``
#: keeps the strictest clearance the motor nets already held (VIN and the
#: phases 0.2 mm); FUSED, VM and GND_SENSE were held to 0.15 mm only because the
#: name classifier called them ``Digital`` or ``Audio``.
REVIEWED_CLEARANCES = {
    "Audio": "0.15",
    "Clock": "0.2",
    "Debug": "0.15",
    "Differential": "0.15",
    "Digital": "0.15",
    "Ground plane": "0.2",
    "Motor power": "0.2",
    "Power": "0.2",
}


def reviewed_clearances(dru: str) -> dict[str, str]:
    """``{class: min clearance}`` from a DRU's ``Reviewed clearance`` rules."""
    return dict(
        re.findall(
            r'\(rule "Reviewed clearance - ([^"]+)"\n\s*\(condition[^\n]*\n'
            r"\s*\(constraint clearance \(min ([\d.]+)mm\)\)",
            dru,
        )
    )


# Default-on fab view (#8843) makes `kct check --mfr jlcpcb` on this dense board
# several times slower (~8 s on main -> ~40 s, measured on an 8-core host, one
# run). The global 60 s cap is too tight for the 2-vCPU CI runner, so this gets
# explicit headroom; follow-up profiling should cut the cost further.
@pytest.mark.xdist_group("board05_recipe_staged")
@pytest.mark.timeout(180)
def test_recipe_reproduces_reviewed_dru(staged, tmp_path):
    routed = _copy_stage(staged, tmp_path / "export")
    # The last matching rule wins in KiCad. Check the actual refill input
    # as well as the result of export, which runs the sidecar writer again.
    before_refill = routed.with_suffix(".kicad_dru").read_text()
    _assert_u3_pour_clearance(before_refill)
    assert before_refill.rstrip().endswith(DRU_ATTACH_ZONE_BLOCK_END)
    for ref in ("U2", "U3"):
        assert f'(rule "kct attach zone - {ref}"' in before_refill
    targets = zone_clearance_targets(routed)
    assert {(layer, net) for layer, net, _ in targets} == {
        ("F.Cu", "GND"),
        ("In1.Cu", "GND"),
        ("In2.Cu", "+5V"),
        ("B.Cu", "GND"),
    }
    for layer, net, clearance in targets:
        assert clearance == pytest.approx(0.2)
        assert zone_clearance_rule(layer, net, clearance).rstrip() in before_refill
    assert (staged.parent / "readiness" / "before-refill.json").is_file()
    # ``kct export`` (redesign/package.py) writes the sidecars once more,
    # next to the input board, before anything else it does.
    result = ManufacturingResult(output_dir=tmp_path / "manufacturing")
    ManufacturingPackage(routed, manufacturer="jlcpcb")._write_drc_constraints(result)
    assert routed.with_suffix(".kicad_dru") in result.drc_constraint_paths
    exported = routed.with_suffix(".kicad_dru").read_bytes()
    _assert_u3_pour_clearance(exported.decode())
    # Fingerprint of the reviewed revision-B export. The released DRU is
    # replayed separately below until its matching copper ships (#9921).
    assert hashlib.sha256(exported).hexdigest() == (
        "d7735690d5770e95de757b78f16b45bd9d049c4141efd912befc5274d0e3b2d7"
    )


def test_committed_dru_carries_the_attach_zone_block():
    assert has_attach_zone_block((OUTPUT / f"{STEM}.kicad_dru").read_text(encoding="utf-8"))


def test_export_replays_committed_rules_and_respects_a_designer_clearance(tmp_path):
    board = tmp_path / f"{STEM}.kicad_pcb"
    dru = board.with_suffix(".kicad_dru")
    for path in (board, dru, board.with_suffix(".kicad_pro"), tmp_path / "net_class_map.json"):
        shutil.copy2(OUTPUT / path.name, path)

    def export():
        result = ManufacturingResult(output_dir=tmp_path / "manufacturing")
        ManufacturingPackage(board, manufacturer="jlcpcb")._write_drc_constraints(result)
        assert dru in result.drc_constraint_paths
        return dru.read_bytes()

    assert export() == (OUTPUT / dru.name).read_bytes()
    designer_rule = b'(rule "Designer clearance" (constraint clearance (min 0.3mm)))'
    dru.write_bytes(dru.read_bytes() + designer_rule + b"\n")
    after = export()
    assert designer_rule in after
    assert not has_attach_zone_block(after.decode())
    assert b'(rule "Zone clearance at U3 pads' not in after
    assert b'(rule "Ampacity Min Width' not in after


def test_mask_standoff_preserves_fiducial_geometry(tmp_path, monkeypatch):
    from kicad_tools.cli.pcb_add_fiducials import run_add_fiducials
    from kicad_tools.sexp import parse_file, parse_string, serialize_sexp

    monkeypatch.syspath_prepend(str(REDESIGN))
    from routing import geometry_fingerprint

    build = _load_build()
    routed = tmp_path / f"{STEM}.kicad_pcb"
    shutil.copy2(OUTPUT / routed.name, routed)
    assert run_add_fiducials(routed, sides="populated") == 0
    original = geometry_fingerprint(routed)
    build.clear_fiducial_openings(routed)
    assert geometry_fingerprint(routed) == original

    board = parse_file(routed)
    fiducials = [
        fp
        for fp in board.find_all("footprint")
        if fp.get_string(0) == "Fiducial:Fiducial_1mm_Mask2mm"
    ]
    assert len(fiducials) == 3
    for footprint in fiducials:
        (pad,) = footprint.find_all("pad")
        copper = pad.find("clearance").get_float(0)
        mask = pad.find("solder_mask_margin").get_float(0)
        assert copper - mask == pytest.approx(0.2)

    # Recompute from a larger local opening and preserve a stricter clearance.
    fiducials[0].find("pad").find("solder_mask_margin").set_value(0, 0.65)
    fiducials[1].find("pad").find("clearance").set_value(0, 0.9)
    fiducials[2].find("pad").remove_child("solder_mask_margin")
    fiducials[2].append(parse_string("(solder_mask_margin 0.6)"))
    routed.write_text(serialize_sexp(board))
    build.clear_fiducial_openings(routed)
    cleared = routed.read_bytes()
    actual = [
        fp.find("pad").find("clearance").get_float(0)
        for fp in parse_file(routed).find_all("footprint")
        if fp.get_string(0) == "Fiducial:Fiducial_1mm_Mask2mm"
    ]
    assert actual == pytest.approx([0.85, 0.9, 0.8])
    build.clear_fiducial_openings(routed)
    assert routed.read_bytes() == cleared


@pytest.fixture(scope="module")
def staged(tmp_path_factory) -> Path:
    """Generate fresh hardware, routing and sidecars using the recipe entry point."""
    routed, _ = _load_build().generate_routed_board(tmp_path_factory.mktemp("board05-staged"))
    return routed


def _sidecar(routed: Path) -> dict:
    """The staged ``net_class_map.json``, loaded the way ``kct check`` loads it."""
    from kicad_tools.router.rules import net_class_map_from_path

    return net_class_map_from_path(routed.parent / "net_class_map.json", pcb_path=routed)


def _resolved(routed: Path) -> dict[str, tuple[str, float]]:
    """``{net: (class, width)}`` the staged project resolves each routed net to."""
    from kicad_tools.core.project_clearance import authored_netclass_widths

    project = json.loads(routed.with_suffix(".kicad_pro").read_text())
    names = sorted({net.name for net in PCB.load(str(routed)).nets.values() if net.name})
    return {
        net: (width.source_class, round(width.width, 4))
        for net, width in authored_netclass_widths(project, names).items()
    }


def _at(point, segment) -> bool:
    """True when ``point`` is one of ``segment``'s ends (coordinates are micrometre-rounded)."""
    return math.dist(point, segment.start) < 1e-3 or math.dist(point, segment.end) < 1e-3


def _copy_stage(staged: Path, root: Path) -> Path:
    root.mkdir()
    sources = (
        staged,
        staged.with_suffix(".kicad_pro"),
        staged.with_suffix(".kicad_dru"),
        staged.parent / "net_class_map.json",
    )
    for source in sources:
        shutil.copy2(source, root / source.name)
    return root / staged.name


def _phase_a_leg(pcb: PCB) -> tuple:
    """PHASE_A's pad-escape leg and the U2 pad it leaves, with the pad's centre."""
    from kicad_tools.validate.rules.netclass_agreement import _pad_necks

    phase = [s for s in pcb.segments if s.net_name == "PHASE_A"]
    (neck,) = (phase[k] for k in _pad_necks(pcb, phase, 0.5, 0.2))
    u2 = next(fp for fp in pcb.footprints if fp.reference == "U2")
    pad = next(p for p in u2.pads if p.number == "5")
    return neck, pad, (u2.position[0] + pad.position[0], u2.position[1] + pad.position[1])


def _edit_copper(board: Path, edit) -> None:
    """Rewrite ``board``'s segments through its S-expression tree, as the recipe does.

    ``edit(node, net, layer, start, end, to_file)`` sees board-relative endpoints;
    a coordinate written back must pass through ``to_file`` to reach the file frame.
    """
    from kicad_tools.sexp import parse_file, serialize_sexp

    pcb = PCB.load(str(board))
    ox, oy = pcb.board_origin
    by_number = {num: net.name for num, net in pcb.nets.items()}
    tree = parse_file(board)
    for node in tree.find_all("segment"):
        net = node.find("net").get_string(0)
        start, end = node.find("start"), node.find("end")
        edit(
            node,
            by_number.get(int(net), net) if net.isdigit() else net,
            node.find("layer").get_string(0),
            (start.get_float(0) - ox, start.get_float(1) - oy),
            (end.get_float(0) - ox, end.get_float(1) - oy),
            lambda point: (point[0] + ox, point[1] + oy),
        )
    board.write_text(serialize_sexp(tree))


def _add_segment(board: Path, net: str, layer: str, start, end, width) -> None:
    from kicad_tools.sexp import parse_file, parse_string, serialize_sexp

    pcb = PCB.load(str(board))
    ox, oy = pcb.board_origin
    number = next(num for num, item in pcb.nets.items() if item.name == net)
    tree = parse_file(board)
    tree.append(
        parse_string(
            f"(segment (start {start[0] + ox} {start[1] + oy}) (end {end[0] + ox} {end[1] + oy})"
            f' (width {width}) (layer "{layer}") (net {number}))'
        )
    )
    board.write_text(serialize_sexp(tree))


def _reset_run_width(board: Path, width: float) -> None:
    def edit(node, net, layer, start, end, to_file):
        if net == "RESET" and layer == "In1.Cu" and math.dist(start, (44.6, 15.325)) < 1e-3:
            node.find("width").set_value(0, width)

    _edit_copper(board, edit)


def _extend_phase_a_leg(board: Path, leg) -> None:
    """Lengthen PHASE_A's retained pad-escape leg to 3.6 mm, still at its pad."""

    def edit(node, net, layer, start, end, to_file):
        if net == "PHASE_A" and layer == leg.layer and math.dist(start, leg.start) < 1e-3:
            node.find("end").set_value(0, to_file((18.5, leg.start[1]))[0])

    _edit_copper(board, edit)


@pytest.mark.xdist_group("board05_recipe_staged")
@pytest.mark.timeout(180)
def test_recipe_declares_reviewed_classes_widths_and_necks(staged):
    build = _load_build()
    assert (staged.parent / "net_class_map.json").read_bytes() == (
        REDESIGN / "net_class_map.json"
    ).read_bytes()
    sidecar = _sidecar(staged)
    pcb = PCB.load(str(staged))
    assert set(sidecar) == {net.name for net in pcb.nets.values() if net.name}
    assert tuple(sorted(REVIEWED_CLEARANCES)) == build.ROUTER_NETCLASSES

    resolved = _resolved(staged)
    motor = {
        net: resolved[net]
        for net in ("VIN", "FUSED", "VM", "PHASE_A", "PHASE_B", "PHASE_C", "GND_SENSE")
    }
    assert set(motor.values()) == {("Motor power", 0.5)}
    assert resolved["GND"] == ("Ground plane", 0.5)
    # Formerly below their 0.2 mm class: the 0.15 mm RESET, MOSI, PWM_B and PWM_C runs are now 0.2 mm.
    assert resolved["MOSI"] == ("Digital", 0.2)
    assert resolved["RESET"] == ("Debug", 0.2)
    # Differential is the router's 0.15 mm class; the project used to say 0.2 mm.
    assert resolved["RUN"] == ("Differential", 0.15)
    for net, routing in sidecar.items():
        assert resolved[net] == (routing.name, round(routing.trace_width, 4))

    project = json.loads(staged.with_suffix(".kicad_pro").read_text())
    classes = {c["name"]: c for c in project["net_settings"]["classes"]}
    assert set(classes) == {"Default", *build.ROUTER_NETCLASSES}
    for name, clearance in REVIEWED_CLEARANCES.items():
        assert classes[name]["clearance"] == float(clearance)
    assert classes["Differential"]["track_width"] == pytest.approx(0.15)
    assert classes["Motor power"]["track_width"] == pytest.approx(0.5)
    assert (
        tuple(sorted(project["meta"]["kct_router_netclasses"]["classes"]))
        == build.ROUTER_NETCLASSES
    )

    from kicad_tools.validate.rules.netclass_agreement import check_netclass_agreement

    results = check_netclass_agreement(staged, pcb, sidecar)
    assert results.violations == []

    reset = [s for s in pcb.segments if s.net_name == "RESET"]
    assert 0.15 not in {round(s.width, 3) for s in reset}
    (run,) = [s for s in reset if s.layer == "In1.Cu" and math.dist(s.start, (44.6, 15.325)) < 1e-3]
    assert run.width == pytest.approx(0.2)
    assert math.dist(run.end, (44.6, 32.6375)) < 1e-3


@pytest.mark.xdist_group("board05_recipe_staged")
@pytest.mark.timeout(180)
def test_incomplete_map_refused_before_writing_sidecars(staged, tmp_path):
    board = _copy_stage(staged, tmp_path / "partial-map")
    map_file = board.parent / "net_class_map.json"
    entries = json.loads(map_file.read_bytes())
    del entries["FUSED"]
    map_file.write_text(json.dumps(entries))
    sidecars = [board.with_suffix(suffix) for suffix in (".kicad_pro", ".kicad_dru")]
    original = [path.read_bytes() for path in sidecars]

    with pytest.raises(RuntimeError, match=r"missing.*FUSED"):
        _load_build().declare_router_netclasses(board)
    assert [path.read_bytes() for path in sidecars] == original


@pytest.mark.xdist_group("board05_recipe_staged")
@pytest.mark.timeout(180)
def test_loaded_board_map_preserves_routing_policy(staged):
    from kicad_tools.cli.route_cmd import _apply_net_class_map_sidecar
    from kicad_tools.router.io import load_pcb_for_routing

    router, _ = load_pcb_for_routing(
        str(staged.parent / "bldc_controller.kicad_pcb"), auto_adjust_grid=True
    )
    _apply_net_class_map_sidecar(
        router, SimpleNamespace(_loaded_net_class_map=_sidecar(staged)), quiet=True
    )
    differential = {"DRIVER_SLEEP", "RUN", "VCP", "nTRIP"}
    costs = {"+5V": 0.8, "V3P3": 0.8, "SCK": 0.9, "RESET": 1.2}
    costs.update(dict.fromkeys(differential, 0.85))
    for net in router.net_names.values():
        if net:
            settings = router.net_class_map[net]
            assert settings.cost_multiplier == costs.get(net, 1.0), net
            assert settings.length_critical == (net == "SCK" or net in differential), net
            assert settings.noise_sensitive == (net == "SENSE_IN"), net
            assert not settings.coupled_routing, net


@pytest.mark.xdist_group("board05_recipe_staged")
@pytest.mark.timeout(180)
def test_retained_motor_neck_is_the_u2_pad_escape(staged):
    pcb = PCB.load(str(staged))
    leg, pad, centre = _phase_a_leg(pcb)
    assert pad.net_name == "PHASE_A" and "F.Cu" in pad.layers
    # A 0.5 mm trace cannot leave a 0.4 mm pad on 0.65 mm pitch: the leg leaves
    # pad 5 at the pad's own width and lands on a via into 0.5 mm inner copper.
    assert pad.size == pytest.approx((1.575, 0.4))
    assert leg.layer == "F.Cu" and leg.width == pytest.approx(0.4)
    assert leg.start == pytest.approx(centre, abs=1e-3)
    assert math.dist(leg.start, leg.end) == pytest.approx(1.199, abs=1e-3)
    landing = [
        s for s in pcb.segments if s.net_name == "PHASE_A" and s is not leg and _at(leg.end, s)
    ]
    assert landing and all(s.layer == "In2.Cu" and s.width == pytest.approx(0.5) for s in landing)
    assert any(
        v.position == pytest.approx(leg.end, abs=1e-6) for v in pcb.vias if v.net_name == "PHASE_A"
    )


@pytest.mark.xdist_group("board05_recipe_staged")
@pytest.mark.timeout(180)
def test_narrow_copper_that_is_not_the_pad_escape_still_warns(staged, tmp_path):
    from kicad_tools.validate.rules.netclass_agreement import (
        RULE_TRACK_WIDTH_MISMATCH,
        check_netclass_agreement,
    )

    pcb = PCB.load(str(staged))
    leg, _pad, centre = _phase_a_leg(pcb)
    cases = {
        # 0.4 mm away from both PHASE_A pads is below class, not a neck.
        "away_from_pads": ("In2.Cu", (50.0, 60.0), (52.0, 60.0), 0.4),
        # At the pad and 0.4 mm wide, but inside pad 5 and under the 0.3 mm neck floor.
        "under_neck_floor": ("F.Cu", centre, (centre[0] + 0.5, centre[1]), 0.2),
    }
    for name, (layer, start, end, width) in cases.items():
        board = _copy_stage(staged, tmp_path / name)
        _add_segment(board, "PHASE_A", layer, start, end, width)
        findings = check_netclass_agreement(board, PCB.load(str(board)), _sidecar(board)).violations
        assert [(v.rule_id, v.nets) for v in findings] == [
            (RULE_TRACK_WIDTH_MISMATCH, ("PHASE_A",))
        ]
        assert f"narrowest {width:.3f}mm" in findings[0].message

    # The retained leg itself stops being an escape once it outruns the reach.
    board = _copy_stage(staged, tmp_path / "long_leg")
    _extend_phase_a_leg(board, leg)
    (long_leg,) = [
        s
        for s in PCB.load(str(board)).segments
        if s.net_name == "PHASE_A" and s.width < 0.5 and math.dist(s.start, leg.start) < 1e-3
    ]
    assert math.dist(long_leg.start, long_leg.end) > 3.3
    findings = check_netclass_agreement(board, PCB.load(str(board)), _sidecar(board)).violations
    assert [(v.rule_id, v.nets) for v in findings] == [(RULE_TRACK_WIDTH_MISMATCH, ("PHASE_A",))]
    assert "narrowest 0.400mm" in findings[0].message


@pytest.mark.xdist_group("board05_recipe_staged")
@pytest.mark.timeout(180)
def test_recipe_refuses_copper_or_project_outside_the_reviewed_account(staged, tmp_path):
    build = _load_build()

    board = _copy_stage(staged, tmp_path / "copper")
    _reset_run_width(board, 0.15)
    with pytest.raises(RuntimeError, match=r"netclass_track_width_mismatch RESET: .*0\.150mm"):
        build.declare_router_netclasses(board)

    board = _copy_stage(staged, tmp_path / "project")
    project_path = board.with_suffix(".kicad_pro")
    project = json.loads(project_path.read_text())
    project["net_settings"]["netclass_assignments"]["FUSED"] = ["Digital"]
    for row in project["net_settings"]["netclass_patterns"]:
        if row["pattern"] == "FUSED":
            row["netclass"] = "Digital"
    project_path.write_text(json.dumps(project, indent=2))
    with pytest.raises(RuntimeError, match="Net 'FUSED' does not resolve to its reviewed class"):
        build.verify_router_netclasses(board, build.reviewed_net_class_map(board))
