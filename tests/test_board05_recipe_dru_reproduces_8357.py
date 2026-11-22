"""Board 05's committed ``.kicad_dru`` is what its recipe emits (Issue #8357).

``design.py`` prepares the routed board and declares its sidecars, including
the per-zone clearances and the #8048 attach-zone exemptions. Its pre-refill
``kct check --emit-dru`` refreshes the manufacturer's rule block. Finally
the manufacturing export writes the sidecars from the bare profile. The last write
is what the recipe leaves on disk.  Before #8357 the committed file lacked the
attach-zone block the recipe emits, so every honest regeneration had to
hand-restore it and refill the zones against the hand-restored file.  The
operator ruled (2026-11-04) to commit what the recipe emits.

This test runs the recipe's own routing preparation on the committed unrouted
board in a scratch directory, starting with its original project and no DRU,
and asserts the result is byte-identical to the committed file.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult
from kicad_tools.manufacturers.attach_zone_dru import (
    DRU_ATTACH_ZONE_BLOCK_END,
    has_attach_zone_block,
)
from kicad_tools.manufacturers.zone_clearance_dru import zone_clearance_rule, zone_clearance_targets

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


def _recipe_dru(tmp_path: Path) -> bytes:
    for suffix in (".kicad_pcb", ".kicad_pro"):
        name = f"bldc_controller{suffix}"
        shutil.copy2(OUTPUT / name, tmp_path / name)
    shutil.copy2(OUTPUT / "net_class_map.json", tmp_path / "net_class_map.json")
    routed = tmp_path / f"{STEM}.kicad_pcb"
    assert not routed.with_suffix(".kicad_dru").exists()

    prepared, _ = _load_build().prepare_routed_board(tmp_path)
    assert prepared == routed
    # The last matching rule wins in KiCad. Check the actual refill input
    # as well as the result of export, which runs the sidecar writer again.
    before_refill = routed.with_suffix(".kicad_dru").read_text()
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
    assert (tmp_path / "readiness" / "before-refill.json").is_file()
    # ``kct export`` (redesign/package.py) writes the sidecars once more,
    # next to the input board, before anything else it does.
    result = ManufacturingResult(output_dir=tmp_path / "manufacturing")
    ManufacturingPackage(routed, manufacturer="jlcpcb")._write_drc_constraints(result)
    assert routed.with_suffix(".kicad_dru") in result.drc_constraint_paths
    return routed.with_suffix(".kicad_dru").read_bytes()


def test_committed_dru_carries_the_attach_zone_block():
    assert has_attach_zone_block((OUTPUT / f"{STEM}.kicad_dru").read_text(encoding="utf-8"))


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


# Default-on fab view (#8843) makes `kct check --mfr jlcpcb` on this dense board
# several times slower (~8 s on main -> ~40 s, measured on an 8-core host, one
# run). The global 60 s cap is too tight for the 2-vCPU CI runner, so this gets
# explicit headroom; follow-up profiling should cut the cost further.
@pytest.mark.timeout(180)
def test_recipe_emits_the_committed_dru_byte_for_byte(tmp_path):
    committed = (OUTPUT / f"{STEM}.kicad_dru").read_bytes()
    assert _recipe_dru(tmp_path) == committed, (
        "board 05's recipe no longer emits the committed .kicad_dru; regenerate "
        "the board (design.py) on the pinned toolchain and re-pin its evidence"
    )
