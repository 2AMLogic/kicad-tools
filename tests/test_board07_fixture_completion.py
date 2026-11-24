"""Physical pad-exit and completed-bundle controls for the four-layer Board 07 fixture."""

from __future__ import annotations

import json
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from kicad_tools.schema.pcb import PCB


@pytest.fixture(scope="module")
def generate_design_mod():
    from scripts.ci.net_class_map_resolver import load_board_recipe_module

    recipe = load_board_recipe_module(
        Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test"
    )
    assert recipe is not None
    return recipe


def _id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, name))


def _a7_board(path, case):
    points = [
        (10, 10),
        (10.4, 10),
        (10.4, 10.4927),
        (10.8, 10.4927),
        (10.8, 10),
        (11.2, 10),
        (11.2, 10.4927),
        (11.6, 10.4927),
        (11.6, 10),
        (14, 10),
    ]

    def track(start, end, uid, layer="B.Cu", net=1):
        return (
            f"(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) "
            f'(width 0.2) (layer "{layer}") (net {net}) (uuid "{uid}"))'
        )

    def pad(ref, x):
        return (
            f'(footprint "Header" (layer "F.Cu") (at {x} 10) '
            f'(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")) '
            '(pad "9" thru_hole circle (at 0 0) (size 1.60 1.60) (drill 0.8) '
            '(layers "*.Cu" "*.Mask") (net 1 "A7")))'
        )

    items = [pad("J3", 10), pad("U5", 14)]
    if case == "different-route":
        points[2] = (10.4, 10.6)
    items.extend(track(a, b, _id(f"a7/{i}")) for i, (a, b) in enumerate(pairwise(points)))
    if case == "group-lock":
        items.append(
            f'(group "hold" (uuid "{_id("group")}") (locked yes) (members "{_id("a7/0")}"))'
        )
    elif case == "branch":
        items.append(track(points[2], (10.4, 12), _id("tap")))
    elif case in ("obstruction", "other-side"):
        layer = "F.Cu" if case == "other-side" else "B.Cu"
        items.append(track((11.0, 11.0), (11.8, 11.0), _id("foreign"), layer=layer, net=2))
    elif case == "same-net-contact":
        # An offset end touches the old left bump partway along its length.
        items.append(track((10.25, 10.4), (9.5, 11.15), _id("tap")))
    elif case == "tangent-via":
        # The old bump overlaps this barrel. The new straight only touches its outer rim.
        items.append(
            f'(via (at 10.6 10.35) (size 0.5) (drill 0.2) (layers "F.Cu" "B.Cu") '
            f'(net 1) (uuid "{_id("tap-via")}"))'
        )
    elif case == "keepout":
        items.append(
            '(zone (net 0) (net_name "") (layers "F&B.Cu") (hatch edge 0.5) '
            "(keepout (tracks not_allowed)) (polygon (pts (xy 11.1 10.71) "
            "(xy 11.5 10.71) (xy 11.5 10.8) (xy 11.1 10.8))))"
        )
    path.write_text(
        "(kicad_pcb (version 20240108) (generator test) (general (thickness 1.6)) "
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (setup (pad_to_mask_clearance 0)) '
        '(net 0 "") (net 1 "A7") (net 2 "A1")\n' + "\n".join(items) + ")\n"
    )


@pytest.mark.parametrize(
    "case",
    [
        "clear",
        "other-side",
        "group-lock",
        "branch",
        "different-route",
        "obstruction",
        "same-net-contact",
        "tangent-via",
        "keepout",
    ],
)
def test_address_escape_splices_only_its_unobstructed_unbranched_copper(
    tmp_path, monkeypatch, case
):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test")
    )
    from ddr_pad_escapes import clear_fixture_pad_escapes

    path = tmp_path / "board.kicad_pcb"
    _a7_board(path, case)
    source = path.read_bytes()
    expected = case in ("clear", "other-side")
    assert clear_fixture_pad_escapes(path) == int(expected)
    if expected:
        routed = PCB.load(path)
        by_net = [s for s in routed.segments if s.net_name == "A7"]
        assert len(by_net) == 5
        assert any(
            s.start == pytest.approx((11.2, 10.74)) and s.end == pytest.approx((11.6, 10.74))
            for s in by_net
        )
        assert any(s.uuid == _id("a7/8") and s.start == pytest.approx((11.6, 10)) for s in by_net)
        saved = path.read_bytes()
        assert clear_fixture_pad_escapes(path) == 0 and path.read_bytes() == saved
    else:
        assert path.read_bytes() == source


@pytest.mark.parametrize(
    ("exit_code", "changes", "complete"),
    [
        (0, {"verdict": "success"}, True),
        (0, {"verdict": "success", "nets_routed": 30}, False),
        (3, {"failure_reason": "post_route_drc"}, True),
        (3, {"failure_reason": "post_route_drc", "nets_partial": 1}, False),
        (3, {"failure_reason": "post_route_drc", "has_disconnected_islands": True}, False),
        (3, {}, False),
        (4, {"failure_reason": "post_route_drc"}, False),
        (10, {"verdict": "unverified"}, False),
    ],
)
def test_complete_signal_route_keeps_unresolved_failures(
    generate_design_mod, exit_code, changes, complete
):
    summary = {
        "nets_requested": 31,
        "nets_routed": 31,
        "nets_partial": 0,
        "nets_unrouted": 0,
        "has_disconnected_islands": False,
        "verdict": "failed",
    }
    summary.update(changes)
    report = subprocess.CompletedProcess([], exit_code, json.dumps({"summary": summary}))
    assert generate_design_mod._route_completed(report) is complete
    assert not generate_design_mod._route_completed(subprocess.CompletedProcess([], exit_code, ""))


@pytest.mark.parametrize("bundle_written", [False, True])
def test_full_gate_rechecks_the_exported_bundle(
    generate_design_mod, monkeypatch, tmp_path, bundle_written
):
    from kicad_tools.drc.geometric import GeometricDRCResult
    from kicad_tools.recipes.gate import evaluate_pipeline_gate

    pcb = tmp_path / "fixture.kicad_pcb"
    monkeypatch.setattr(generate_design_mod, "write_lvs_report", lambda *a, **kw: (True, True))
    calls = []

    def fake_check(path, *, drc_only=False):
        assert path == pcb
        calls.append(drc_only)
        # An absent or invalid manifest cannot pass the completed bundle's check.
        return drc_only

    monkeypatch.setattr(generate_design_mod, "run_drc", fake_check)
    monkeypatch.setattr(
        generate_design_mod,
        "evaluate_pipeline_gate",
        lambda *a, **kw: evaluate_pipeline_gate(*a, **kw, _drc_result=GeometricDRCResult(ran=True)),
    )
    gate = generate_design_mod.check_finished_board(
        pcb.with_suffix(".kicad_sch"), pcb, True, bundle_written=bundle_written
    )
    assert calls == [not bundle_written]
    assert gate.passed is not bundle_written
    assert gate.export_ok is (False if bundle_written else None)


@pytest.mark.timeout(180)
@pytest.mark.parametrize("rollback", [False, True])
def test_recipe_distinguishes_post_route_drc_from_a_failed_repair(
    generate_design_mod, tmp_path, capsys, monkeypatch, rollback
):
    """A fully routed board with a failed repair and one with only DRC both exit 3."""
    from kicad_tools.cli import main, route_cmd

    pcb = tmp_path / "input.kicad_pcb"
    shutil.copyfile(Path(__file__).parent / "fixtures/projects/test_project.kicad_pcb", pcb)
    monkeypatch.setattr(route_cmd, "run_post_route_drc", lambda *a, **kw: (1, 0))
    monkeypatch.setattr(route_cmd, "_run_auto_fix", lambda *a, **kw: 3)
    args = [
        "route",
        str(pcb),
        "-o",
        str(tmp_path / "out.kicad_pcb"),
        "--no-optimize",
        "--no-sync-check",
        "--no-auto-pour",
        "--no-placement-feedback",
        "--oracle-rounds",
        "0",
        "--no-current-paths",
        "--no-auto-layers",
        "--format",
        "json",
    ]
    if rollback:
        args.append("--auto-fix")
    rc = main(args)
    stdout = capsys.readouterr().out
    assert rc == 3
    summary = json.loads(stdout)["summary"]
    assert summary["verdict"] == "failed"
    assert summary.get("failure_reason") == (None if rollback else "post_route_drc")
    assert (
        generate_design_mod._route_completed(subprocess.CompletedProcess(args, rc, stdout))
        is not rollback
    )
