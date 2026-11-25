"""Regression tests for evolutionary placement result reporting (issue #10228)."""

import json
import math
from pathlib import Path

from kicad_tools.cli import main
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
