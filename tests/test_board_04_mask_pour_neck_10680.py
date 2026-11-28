"""Board 04's declared-solid VDD pad is not necked by the recipe's final pour (Issue #10680).

U2/9 (LQFP-48 VDD) is declared solid on +3.3V (``zone_connect 2``).  On a fresh
regen the public ``mask_to_copper`` check reported it as a violation shaped like
a thermal relief the pad never declared: exposed 0.401942049 mm², 0.0549111147
mm² of copper under mask beside the opening.

The cause is the recipe's fill path, not the routed copper.  ``kct zones fill``
follows KiCad's fill with a pure-Python foreign-pad carve (Issue #3711) that
keeps every foreign pad ``connect_pads clearance + min_thickness / 2`` away --
0.1 + 0.125 = 0.225 mm for this +3.3V zone.  U2/9 sits at 0.5 mm pitch between
U2/8 (GND) and U2/10 (no net), so the carve cuts the pour back to a 0.25 mm
channel at the pad's west rim and a dead-end stub at its east rim.  The checker
reads that as a spoke-only join (``_own_pour_solid`` condition 6) and declines
the solid class.

KiCad's own fill honours the board's ``.kicad_dru``: inside U2's courtyard the
attach-zone rule spaces the pour 0.127 mm from U2/8, so the pour wraps the pad's
rim and the join reads ``own_pour_solid``.  ``kct fix-drc --only mask-copper``
(the #10664 repair) refills with exactly that engine, which is why the
intermediate repair logs ``12 -> 0`` and why the committed artifact is clean.
The recipe then re-poured with the carved fill *after* the repair, and the
finished file carried the finding again.

These tests pin the recipe's final pour to KiCad's authoritative refill and
prove the carve / refill pair on an isolated copy of the committed artifact, so
the control does not depend on the host's route.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD = REPO_ROOT / "boards" / "04-stm32-devboard"
RECIPE = BOARD / "generate_design.py"
ROUTED = BOARD / "output" / "stm32_devboard_routed.kicad_pcb"

#: What the issue measured on the necked pour at U2/9.
NECKED_EXPOSED_MM2 = 0.401942049
NECKED_BESIDE_OWNER_MM2 = 0.0549111147
#: What the whole pour reads once KiCad's own fill reaches the pad (the
#: committed artifact carries the same figures).
WHOLE_EXPOSED_MM2 = 0.437003


def _recipe():
    """Import the board-04 ``generate_design.py`` recipe module."""
    spec = importlib.util.spec_from_file_location("board04_generate_design_10680", RECIPE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mask_errors(path: Path) -> list:
    """The ``mask_to_copper`` error measurements on ``path`` under the tier-1 policy."""
    from kicad_tools.validate.mask_copper import (
        check_mask_to_copper,
        default_request_for_manufacturer,
    )

    request = default_request_for_manufacturer("jlcpcb-tier1")
    assessment = check_mask_to_copper(path, policy=request.policy)
    if assessment.coverage != "complete":
        pytest.skip(f"the mask assessment did not complete: {assessment.reasons[:1]}")
    return [m for m in assessment.measurements if m.disposition == "violation"]


def test_the_recipe_ends_on_the_authoritative_refill_not_the_carved_fill():
    """Source pin: the pour shipped after the mask repair is KiCad's own fill.

    The #10664 repair leaves the authoritative refill on the board; a carved
    ``kct zones fill`` after it re-necks the declared-solid pad (Issue #10680).
    Pins the step function, its call site after ``repair_mask_copper`` and
    before ``run_drc``, and that no carved fill runs in between.
    """
    source = RECIPE.read_text()
    assert "def refill_pour_authoritative(" in source, (
        "board-04 recipe no longer defines refill_pour_authoritative -- the "
        "authoritative KiCad refill that leaves a declared-solid fine-pitch pad "
        "whole. Without it the final pour is the carved kct zones fill, which "
        "necks U2/9 (issue #10680)."
    )
    repair = source.index("repair_mask_copper(routed_path, log=print)")
    refill = source.index("refill_pour_authoritative(routed_path)")
    drc = source.index("run_drc(routed_path)")
    assert repair < refill < drc, (
        "refill_pour_authoritative must run after repair_mask_copper and before "
        "run_drc, so the pour the gates measure is the one the repair verified "
        "(issue #10680)."
    )
    assert "fill_zones(routed_path)" not in source[repair:drc], (
        "a carved kct zones fill runs after the mask repair again; it re-necks "
        "the declared-solid pad the repair's own refill had made whole (issue "
        "#10680)."
    )
    start = source.index("def refill_pour_authoritative(")
    body = source[start : source.index("\n\n\n", start)]
    assert "require_save=True" in body, (
        "refill_pour_authoritative must persist the refill (require_save=True); "
        "a read-only DRC refill would leave the carved pour on disk."
    )
    assert "run_fill_zones" not in body and '"zones"' not in body, (
        "refill_pour_authoritative must not use the carved fill path (issue #10680)."
    )


def test_authoritative_refill_saves_the_pour_and_keeps_authored_ids(tmp_path, monkeypatch):
    """Contract: the step persists a native refill, fails closed, and canonicalizes new ids."""
    import kicad_tools.cli.runner as runner

    board = tmp_path / "board.kicad_pcb"
    board.write_text('(pcb "x"\n\t(uuid "aaaaaaaa-1111-2222-3333-444444444444")\n)\n')
    calls = []

    def fake_refill(path, kicad_cli=None, *, require_save=False):
        calls.append((Path(path), require_save))
        path.write_text(
            path.read_text() + '\t(segment (uuid "bbbbbbbb-1111-2222-3333-444444444444"))\n'
        )
        return runner.KiCadCLIResult(success=True, output_path=Path(path))

    monkeypatch.setattr(runner, "run_refill_zones", fake_refill)
    canonicalized = []
    module = _recipe()
    monkeypatch.setattr(
        module,
        "canonicalize_pcb_file_uuids",
        lambda path, keep: canonicalized.append((Path(path), set(keep))) or 0,
    )
    assert module.refill_pour_authoritative(board) is True
    assert calls == [(board, True)]
    # The authored id is preserved as the keep-set; only the id the refill added is new.
    assert canonicalized and canonicalized[0][0] == board
    assert "aaaaaaaa-1111-2222-3333-444444444444" in canonicalized[0][1]
    assert "bbbbbbbb-1111-2222-3333-444444444444" not in canonicalized[0][1]

    monkeypatch.setattr(
        runner,
        "run_refill_zones",
        lambda path, kicad_cli=None, *, require_save=False: runner.KiCadCLIResult(
            success=False, stderr="no report produced"
        ),
    )
    assert module.refill_pour_authoritative(board) is False


def test_control_the_carved_fill_necks_u2_9_and_the_authoritative_refill_clears_it(tmp_path):
    """The issue's exact finding, reproduced from the committed artifact, then cleared.

    The committed routed PCB already carries KiCad's whole pour at U2/9.  The
    carved fill ``kct zones fill`` applies re-creates the neck with the figures
    the issue measured; the recipe's authoritative refill restores the whole
    pour.  Native DRC and connectivity are re-checked on the refilled copy.
    """
    from kicad_tools.cli.runner import find_kicad_cli, run_fill_zones
    from kicad_tools.drc.geometric import run_geometric_drc

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        pytest.skip("kicad-cli not found")
    staged = tmp_path / ROUTED.name
    shutil.copyfile(ROUTED, staged)
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(ROUTED.with_suffix(suffix), staged.with_suffix(suffix))

    def at_u2_9(errors):
        return [m for m in errors if m.owner_ref == "U2/9"]

    assert at_u2_9(_mask_errors(staged)) == []

    carved = run_fill_zones(staged, None, kicad_cli=kicad_cli)
    assert carved.success, carved.stderr
    errors = _mask_errors(staged)
    assert [m.owner_ref for m in errors] == ["U2/9"], [
        (m.owner_ref, m.exposed_area_mm2) for m in errors
    ]
    necked = at_u2_9(errors)[0]
    assert necked.exposed_area_mm2 == pytest.approx(NECKED_EXPOSED_MM2, abs=1e-6)
    assert necked.beside_owner_excess_mm2 == pytest.approx(NECKED_BESIDE_OWNER_MM2, abs=1e-7)
    assert necked.owner_net == "+3.3V" and necked.conductor_kind == "zone"

    assert _recipe().refill_pour_authoritative(staged) is True
    assert _mask_errors(staged) == []

    from kicad_tools.validate.mask_copper import (
        check_mask_to_copper,
        default_request_for_manufacturer,
    )

    assessment = check_mask_to_copper(
        staged, policy=default_request_for_manufacturer("jlcpcb-tier1").policy
    )
    whole = [m for m in assessment.measurements if m.owner_ref == "U2/9"]
    assert [m.disposition for m in whole] == ["own_pour_solid"]
    assert whole[0].exposed_area_mm2 == pytest.approx(WHOLE_EXPOSED_MM2, abs=1e-5)

    native = run_geometric_drc(staged, kicad_cli=kicad_cli)
    assert native.ran, native.note
    assert native.error_count == 0, native.all_by_type
    assert native.unconnected_items == []
