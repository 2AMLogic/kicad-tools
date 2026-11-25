"""Regression tests for evolutionary placement result reporting (issue #10228)."""

import json
import math
from pathlib import Path

import pytest

from kicad_tools.cli import main
from kicad_tools.optim.evolutionary import EvolutionaryPlacementOptimizer, Individual
from kicad_tools.optim.workflow import OptimizationWorkflow, WorkflowConfig
from kicad_tools.schema.pcb import PCB

LED_BOARD = Path(__file__).resolve().parents[1] / "boards/00-simple-led/output/simple_led.kicad_pcb"


def test_evolutionary_cli_reports_wire_length(capsys):
    status = main(
        [
            "placement",
            "optimize",
            str(LED_BOARD),
            "--strategy",
            "evolutionary",
            "--generations",
            "1",
            "--population",
            "8",
            "--dry-run",
            "--format",
            "json",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert status == 0, result["message"]
    assert result["success"] is True
    assert math.isfinite(result["wire_length_mm"])
    assert result["wire_length_mm"] > 0


def test_evolutionary_measures_winning_placement():
    pcb = PCB.load(str(LED_BOARD))
    # Any on-board candidate beats this starting placement with kilometre-long wires.
    assert pcb.update_footprint_position("R1", 1_000_000, 0)
    workflow = OptimizationWorkflow(
        pcb, WorkflowConfig(strategy="evolutionary", generations=1, population=2)
    )
    result = workflow.run()
    # Three connections on this 25 x 20 mm board fit within 100 mm in total.
    assert 0 < result.wire_length_mm < 100


def test_rotated_winner_reports_distances_between_saved_pads(tmp_path, monkeypatch):
    pcb = PCB.load(LED_BOARD)
    winner = Individual(
        positions={"D1": (12, 9), "R1": (7, 6)},
        rotations={"D1": 90, "R1": 270},
    )
    monkeypatch.setattr(EvolutionaryPlacementOptimizer, "optimize", lambda *args, **kwargs: winner)
    workflow = OptimizationWorkflow(pcb, WorkflowConfig(strategy="evolutionary"))

    result = workflow.run()
    output = tmp_path / "winner.kicad_pcb"
    workflow.save(output)
    saved = PCB.load(output)

    assert saved.get_footprint("D1").rotation == 90
    assert saved.get_footprint("R1").rotation % 360 == 270
    nets = [(("J1", "1"), ("R1", "1")), (("J1", "2"), ("D1", "1")), (("R1", "2"), ("D1", "2"))]
    expected = sum(
        math.dist(saved.get_pad_position(*a), saved.get_pad_position(*b)) for a, b in nets
    )
    assert result.wire_length_mm == pytest.approx(expected)
