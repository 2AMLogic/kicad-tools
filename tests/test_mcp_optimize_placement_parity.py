"""MCP ``optimize_placement`` matches ``kct optimize-placement`` (issue #6253).

PR #6251 (#6020) gave the CLI a decoupling-cap affinity term and a post-optimize
snap pass. The MCP tool must run the same code, with the same opt-out, so an
agent placing parts through MCP gets the same board as one using the CLI.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

import pytest

from kicad_tools.mcp.server import create_server
from kicad_tools.mcp.tools.registry import get_tool
from kicad_tools.native_concurrency import ENV_SLOT_DIR, _slot_directory
from kicad_tools.schema.pcb import PCB
from tests.test_placement_decoupling import _DECAP_PCB
from tests.test_placement_decoupling_association import make_designated_back_side_board

pytest.importorskip("cmaes", reason="cmaes not installed")

ITERATIONS = 15


@pytest.fixture
def decap_pcb(tmp_path: Path) -> Path:
    path = tmp_path / "decap.kicad_pcb"
    path.write_text(_DECAP_PCB)
    return path


def _positions(path: Path) -> dict[str, tuple[float, float, float]]:
    return {
        fp.reference: (round(fp.position[0], 4), round(fp.position[1], 4), fp.rotation)
        for fp in PCB.load(str(path)).footprints
    }


def _run_cli(pcb: Path, out: Path, weights: dict | None, capsys, **kwargs) -> dict:
    from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement

    run_optimize_placement(
        str(pcb),
        output_path=str(out),
        max_iterations=ITERATIONS,
        weights_json=json.dumps(weights) if weights is not None else None,
        allow_infeasible=True,
        as_json=True,
        **kwargs,
    )
    return json.loads(capsys.readouterr().out)


def _run_mcp(pcb: Path, out: Path, weights: dict | None) -> dict:
    from kicad_tools.mcp.tools.optimize_placement import optimize_placement

    result = optimize_placement(
        str(pcb),
        max_iterations=ITERATIONS,
        weights=weights,
        output_path=str(out),
    )
    assert result["success"], result
    return result


@pytest.mark.parametrize("weights", [None, {"decoupling": 0}], ids=["default", "decoupling-off"])
def test_mcp_and_cli_place_identically(decap_pcb, tmp_path, capsys, weights):
    cli_out = tmp_path / "cli.kicad_pcb"
    mcp_out = tmp_path / "mcp.kicad_pcb"
    cli_doc = _run_cli(decap_pcb, cli_out, weights, capsys)
    mcp_doc = _run_mcp(decap_pcb, mcp_out, weights)

    assert _positions(cli_out) == _positions(mcp_out)
    assert mcp_doc["decoupling"] == cli_doc["decoupling"]
    assert mcp_doc["final_score"]["total"] == pytest.approx(
        cli_doc["scores"]["final"]["total"], abs=1e-3
    )


def test_mcp_applies_decoupling_by_default(decap_pcb, tmp_path):
    on = _run_mcp(decap_pcb, tmp_path / "on.kicad_pcb", None)
    off = _run_mcp(decap_pcb, tmp_path / "off.kicad_pcb", {"decoupling": 0})

    assert [e["cap"] for e in on["decoupling"]] == ["C1"]
    assert off["decoupling"] == []
    assert off["final_score"]["breakdown"]["decoupling"] == 0
    assert on["decoupling"][0]["distance_mm"] <= 3.0


def test_mcp_resolves_sheet_map_with_cli_weight_gate(monkeypatch):
    """Issue #10146: group_cohesion/group_order are not inert on the MCP path."""
    from kicad_tools.mcp.tools import optimize_placement as mcp_mod
    from kicad_tools.placement.cost import PlacementCostConfig

    calls = []
    monkeypatch.setattr(
        mcp_mod, "_build_sheet_map", lambda p, quiet=False: calls.append(p) or {"R1": "/a/"}
    )

    off = PlacementCostConfig(group_cohesion_weight=0.0, group_order_weight=0.0)
    assert mcp_mod._resolve_sheet_map("b.kicad_pcb", off) is None
    assert calls == []  # zeroed weights skip the netlist export, like the CLI

    assert mcp_mod._resolve_sheet_map("b.kicad_pcb", PlacementCostConfig()) == {"R1": "/a/"}
    assert calls == ["b.kicad_pcb"]


@pytest.mark.parametrize(
    ("radius", "locked", "expected_reason"),
    [
        (None, False, None),
        (0.25, False, "limit 0.25 mm"),
        (0.0, False, "limit 0 mm"),
        (6.0, True, "C1 is on B.Cu"),
    ],
)
def test_designated_cap_preview_and_save_match_cli(
    tmp_path, capsys, monkeypatch, radius, locked, expected_reason
):
    pcb = make_designated_back_side_board(tmp_path)
    if locked:
        pcb.write_text(
            pcb.read_text().replace('(footprint "C_0805"', '(footprint "C_0805" (locked yes)')
        )
    original = pcb.read_bytes()
    options = {} if radius is None else {"decoupling_radius_mm": radius}
    server = create_server()
    params = {"pcb_path": str(pcb), "max_iterations": ITERATIONS, **options}
    scratch = tmp_path / "preview"
    scratch.mkdir()
    with monkeypatch.context() as patch:
        # Keep using the workers' shared lock directory while observing preview files.
        patch.setenv(ENV_SLOT_DIR, str(_slot_directory(os.environ)))
        patch.setattr(tempfile, "tempdir", str(scratch))
        preview = server.call_tool("optimize_placement", params)
    assert not list(scratch.iterdir())
    assert "output_path" not in preview

    output = tmp_path / "mcp.kicad_pcb"
    saved = server.call_tool("optimize_placement", {**params, "output_path": str(output)})
    cli_out = tmp_path / "cli.kicad_pcb"
    cli = _run_cli(pcb, cli_out, None, capsys, **options)
    assert preview["success"] and saved["success"] and cli["saved"]
    assert saved["output_path"] == str(output)
    assert pcb.read_bytes() == original
    assert preview["feasible"] == saved["feasible"] == cli["feasible"] == (expected_reason is None)
    assert (
        preview.get("infeasible_detail")
        == saved.get("infeasible_detail")
        == cli["infeasible_detail"]
    )
    assert preview["decoupling"] == saved["decoupling"] == cli["decoupling"]
    assert saved["final_score"]["total"] == pytest.approx(cli["scores"]["final"]["total"], abs=1e-3)
    if expected_reason:
        assert expected_reason in saved["infeasible_detail"]
        assert "U1.1" in saved["infeasible_detail"]

    written = PCB.load(str(output))
    cap = written.get_footprint("C1")
    ic = written.get_footprint("U1")
    assert cap.layer == PCB.load(str(cli_out)).get_footprint("C1").layer
    for footprint in (cap, ic):
        assert all(footprint.layer in pad.layers for pad in footprint.pads)
    distance = math.dist(written.get_pad_position("C1", "1"), written.get_pad_position("U1", "1"))
    assert saved["decoupling"][0]["distance_mm"] == pytest.approx(distance, abs=0.001)
    if locked:
        assert cap.locked and cap.layer == "B.Cu"
        assert _positions(pcb)["C1"] == _positions(output)["C1"]
    if expected_reason is None:
        assert cap.layer == ic.layer == "F.Cu"
        assert distance <= 6.0


@pytest.mark.parametrize("accept_snap", [True, False])
@pytest.mark.parametrize("budget", [None, 1234])
def test_designated_cap_checks_route_selected_board(tmp_path, monkeypatch, accept_snap, budget):
    from kicad_tools.placement import route_check
    from tests.test_optimize_placement_route_check import _outcome

    pcb = make_designated_back_side_board(tmp_path)
    original = pcb.read_bytes()
    output = tmp_path / "result.kicad_pcb"
    routed_layers = []
    expected_budget = route_check.DEFAULT_ROUTE_CHECK_BUDGET if budget is None else budget

    def measure(candidate, *, budget, **kwargs):
        assert budget == expected_budget
        routed_layers.append(PCB.load(str(candidate)).get_footprint("C1").layer)
        return _outcome(3 if accept_snap or len(routed_layers) == 1 else 2, requested=3)

    monkeypatch.setattr(route_check, "bounded_route", measure)
    params = {"pcb_path": str(pcb), "max_iterations": ITERATIONS, "route_check": True}
    if budget is not None:
        params["route_check_budget"] = budget
    server = create_server()
    saved = server.call_tool("optimize_placement", {**params, "output_path": str(output)})
    assert saved["success"] and saved["output_path"] == str(output)
    assert routed_layers == ["B.Cu", "B.Cu", "F.Cu"]
    assert saved["feasible"] == accept_snap
    if accept_snap:
        assert saved["route_check"]["accepted_stage"] == "decoupling_snap"
        assert PCB.load(str(output)).get_footprint("C1").layer == "F.Cu"
    else:
        assert saved["route_check"]["accepted_stage"] == "incumbent"
        assert output.read_bytes() == original
        assert "C1 is on B.Cu but its designated pin U1.1 is on F.Cu" in saved["infeasible_detail"]
    routed_layers.clear()
    preview = server.call_tool("optimize_placement", params)
    assert preview["success"] and "output_path" not in preview
    assert preview["feasible"] == saved["feasible"]
    assert preview.get("infeasible_detail") == saved.get("infeasible_detail")
    assert pcb.read_bytes() == original


@pytest.mark.parametrize(
    ("small_cap_x", "ordering_violation"),
    [(24, True), (23, True), (22, False)],
    ids=["farther", "same-distance", "closer"],
)
def test_designated_cap_value_order_and_overlap_diagnostics(
    tmp_path, small_cap_x, ordering_violation
):
    from tests.test_decoupling_association import make_sheet

    # Both caps are locked and overlap in every case. The larger cap's supply
    # pad is 4 mm from U1.1; the smaller cap's pad is 5, 4 or 3 mm away.
    board = _DECAP_PCB.replace('(layer "F.Cu")', '(locked yes) (layer "F.Cu")')
    start = board.index('  (footprint "C_0805"')
    end = board.index("  (gr_line", start)
    template = (
        board[start:end]
        .replace("(at 26.0 26.0 0)", "(at {x} 15 0)")
        .replace(
            '(property "Reference" "C1")',
            '(property "Reference" "{ref}") (property "Value" "{value}")',
        )
    )
    caps = template.format(x=small_cap_x, ref="C1", value="100nF") + template.format(
        x=23, ref="C2", value="1uF"
    )
    pcb = tmp_path / "ordered.kicad_pcb"
    pcb.write_text(board[:start] + caps + board[end:])
    make_sheet(
        pcb.with_suffix(".kicad_sch"),
        [
            ("U1", 50.8, 50.8, "+3V3", ""),
            ("C1", 55.88, 50.8, "+3V3", "U1.1"),
            ("C2", 60.96, 50.8, "+3V3", "U1.1"),
        ],
    )
    result = get_tool("optimize_placement").handler({"pcb_path": str(pcb), "max_iterations": 1})
    assert result["success"] and not result["feasible"]
    expected_detail = (
        "C1 has a smaller capacitance than C2 but is not closer to U1.1"
        if ordering_violation
        else None
    )
    assert result.get("infeasible_detail") == expected_detail
    assert any(
        {pair["ref1"], pair["ref2"]} == {"C1", "C2"} for pair in result["unresolved_overlaps"]
    )
