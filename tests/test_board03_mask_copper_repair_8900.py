"""Board 03's staged mask_to_copper repair (Issue #8900).

``boards/03-usb-joystick/engineering/mask_repair_8900.py`` wraps ``kct fix-drc
--only mask-copper`` for a scratch copy of the routed, filled board.  It is
scratch-only: the live recipe must stay byte-identical to the published
``design-source`` archive (operator ruling on #8900: no expected-stale entry),
so the recipe gains the step only with the producer-generated release.  These
tests pin the live recipe's non-use, the command shape and the invocation
loop's stop/error semantics; the repairer itself is covered by
``tests/test_fix_drc_cmd.py``.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

BOARD = Path(__file__).resolve().parent.parent / "boards" / "03-usb-joystick"
RECIPE = BOARD / "engineering" / "mask_repair_8900.py"
LIVE_RECIPE = BOARD / "generate_design.py"


def _recipe():
    spec = importlib.util.spec_from_file_location("board03_mask_repair_8900", RECIPE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_recipe_does_not_wire_the_staged_repair() -> None:
    """The live recipe stays untouched until the consistent release lands.

    Wiring it now would diverge the recipe from the published archive (the
    parity test in ``test_design_source_archive_parity.py`` guards the bytes).
    """
    source = LIVE_RECIPE.read_text()
    assert "repair_mask_copper" not in source
    assert "mask_repair_8900" not in source


def test_missing_board_is_a_hard_error(tmp_path: Path) -> None:
    """A router that reports success without writing the board must fail loudly."""
    with pytest.raises(FileNotFoundError):
        _recipe().repair_mask_copper(tmp_path / "absent.kicad_pcb")


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
    monkeypatch.setattr(recipe, "_mask_assessment_complete", lambda path: True)

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


NOTHING = "No targeted violations found. Nothing to repair."
NON_REPAIRABLE = (
    "No repairable violations found, but 3 non-repairable violation(s) "
    "detected (edge clearance, dimension, silkscreen, etc.)."
)


@pytest.mark.parametrize("prose,code", [(NOTHING, 0), (NON_REPAIRABLE, 2)])
def test_already_clean_input_is_not_an_error(monkeypatch, board_file, prose, code) -> None:
    """The real zero-target prose (no 'Repaired N/M') means converged (#9940)."""
    kept, calls = _run_loop(
        monkeypatch,
        board_file,
        replies={1: (prose, code)},
        file_bytes={1: b"(kicad_pcb v0)"},
    )
    assert kept == 0
    assert len(calls) == 1


def test_repair_then_second_invocation_converges(monkeypatch, board_file) -> None:
    """A real repair followed by the production nothing-to-repair run succeeds."""
    kept, calls = _run_loop(
        monkeypatch,
        board_file,
        replies={1: ("Repaired 1/1 violations", 0), 2: (NOTHING, 0)},
        file_bytes={1: b"(kicad_pcb fixed)", 2: b"(kicad_pcb fixed)"},
    )
    assert kept == 1
    assert len(calls) == 2


def test_zero_target_with_incomplete_assessment_is_a_hard_error(monkeypatch, board_file) -> None:
    """An unqualified KiCad reports zero targets; that is not convergence."""
    recipe = _recipe()
    monkeypatch.setattr(recipe, "_mask_assessment_complete", lambda path: False)
    monkeypatch.setattr(recipe.subprocess, "run", lambda cmd, **kw: _FakeResult(NOTHING, 0))
    with pytest.raises(RuntimeError, match="incomplete"):
        recipe.repair_mask_copper(board_file)
