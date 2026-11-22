"""Board 03 recipe repairs mask_to_copper copper via fix-drc (Issue #8900).

The recipe's Step 5.55 runs ``kct fix-drc --only mask-copper`` on the routed
board -- after the Step 5.48 fiducials (new mask openings) and after the
Step 5.5 fill (the repairer's native gate needs the filled state; it refills
internally after saving) -- so a fresh regen carries legal copper instead of
the 72 same-net ``mask_to_copper`` errors the committed board measures on
the pinned toolchain.  These tests pin the wiring (order + command shape)
and the invocation loop's stop/error semantics; the repairer itself is
covered by ``tests/test_fix_drc_cmd.py``.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

BOARD = Path(__file__).resolve().parent.parent / "boards" / "03-usb-joystick"
RECIPE = BOARD / "generate_design.py"


def _recipe():
    spec = importlib.util.spec_from_file_location("board03_generate_design_8900", RECIPE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repair_step_runs_after_the_fill_and_before_drc() -> None:
    """Step 5.55 is wired after Step 5.5's fill and before Step 6's DRC gate.

    The fill must come first: the repairer's native before/after gate on the
    unfilled board is pour-served-opens noise (62 -> 0 on a fresh regen).
    """
    source = inspect.getsource(_recipe().main)
    fill = source.find("fill_zones_in_routed_pcb(routed_path)")
    repair = source.find("repair_mask_copper(routed_path)")
    drc = source.find("drc_success = run_drc(routed_path)")
    assert 0 < fill < repair < drc, (
        "repair_mask_copper must run after the zone fill and before the DRC "
        f"gate (indices {fill}/{repair}/{drc})"
    )


def test_repair_command_targets_mask_copper_on_jlcpcb_tier1() -> None:
    """The command shape matches the measured repair invocation (#9905)."""
    source = inspect.getsource(_recipe().repair_mask_copper)
    for token in ('"fix-drc"', '"--only"', '"mask-copper"', '"--mfr"', '"jlcpcb-tier1"'):
        assert token in source, f"repair command lost {token}"


class _FakeResult:
    def __init__(self, stdout: str, returncode: int = 2) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


@pytest.fixture()
def board_file(tmp_path: Path):
    path = tmp_path / "routed.kicad_pcb"
    path.write_bytes(b"(kicad_pcb v0)")
    return path


def _run_loop(monkeypatch, board_file: Path, replies, file_bytes):
    """Drive repair_mask_copper with faked fix-drc invocations.

    ``replies`` maps invocation number to (stdout, returncode); each fake run
    rewrites the board file from ``file_bytes`` (simulating a repair that
    changed, or did not change, the bytes).
    """
    recipe = _recipe()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        invocation = len(calls)
        stdout, returncode = replies[invocation]
        board_file.write_bytes(file_bytes[invocation])
        return _FakeResult(stdout, returncode)

    monkeypatch.setattr(recipe.subprocess, "run", fake_run)
    kept = recipe.repair_mask_copper(board_file)
    return kept, calls


def test_loop_stops_when_a_pass_leaves_the_board_unchanged(monkeypatch, board_file) -> None:
    """Invocations repeat only while the bytes keep changing (72->43->41)."""
    replies = {
        1: ("Repaired 25/72 violations", 2),
        2: ("Repaired 2/43 violations", 2),
        # A hypothetical fourth call must never happen.
        4: ("Repaired 99/99 violations", 2),
    }
    kept, calls = _run_loop(
        monkeypatch,
        board_file,
        replies,
        file_bytes={1: b"(kicad_pcb a)", 2: b"(kicad_pcb a)", 4: b"(kicad_pcb z)"},
    )
    assert kept == 27
    assert len(calls) == 2


def test_loop_stops_clean_when_no_findings_remain(monkeypatch, board_file) -> None:
    """A converged board (0 findings) exits the loop without error."""
    kept, calls = _run_loop(
        monkeypatch,
        board_file,
        replies={1: ("Repaired 0/0 violations", 0)},
        file_bytes={1: b"(kicad_pcb v0)"},
    )
    assert kept == 0
    assert len(calls) == 1


def test_converged_residual_stops_the_loop_cleanly(monkeypatch, board_file) -> None:
    """0-progress on real findings is convergence (the tracked tool gap), not an error.

    Step 6 ``run_drc`` remains the gate that fails the recipe while any
    finding remains, so the transport step must not double-gate it.
    """
    kept, calls = _run_loop(
        monkeypatch,
        board_file,
        replies={
            1: ("Repaired 27/72 violations", 2),
            2: ("Repaired 0/41 violations", 2),
            # A hypothetical third call must never happen.
            3: ("Repaired 99/99 violations", 2),
        },
        file_bytes={
            1: b"(kicad_pcb repaired)",
            2: b"(kicad_pcb repaired)",
            3: b"(kicad_pcb z)",
        },
    )
    assert kept == 27
    assert len(calls) == 2


def test_connectivity_rollback_is_a_hard_error(monkeypatch, board_file) -> None:
    recipe = _recipe()

    def fake_run(cmd, **kwargs):
        return _FakeResult("Repaired 0/72 violations", 3)

    monkeypatch.setattr(recipe.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="connectivity"):
        recipe.repair_mask_copper(board_file)


def test_unparseable_summary_is_a_hard_error(monkeypatch, board_file) -> None:
    """The loop must never mistake an unreadable run for convergence."""
    recipe = _recipe()

    def fake_run(cmd, **kwargs):
        return _FakeResult("kct fix-drc: something unexpected", 2)

    monkeypatch.setattr(recipe.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="summary"):
        recipe.repair_mask_copper(board_file)
