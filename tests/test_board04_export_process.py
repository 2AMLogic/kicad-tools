"""Board04 export must retain its reviewed paid-drill contract in the package."""

import importlib.util
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from kicad_tools.export.manufacturing import (
    ManufacturingPackage,
    ManufacturingResult,
    _create_project_zip,
)

BOARD = Path(__file__).resolve().parents[1] / "boards/04-stm32-devboard"


@pytest.mark.parametrize(("ref", "pin"), [("C16", "2"), ("U2", "25"), ("U2", "5")])
def test_published_front_mask_clears_the_rerouted_conductors(ref, pin):
    """Check the actual fabrication aperture against the copper in the shipped PCB."""
    from shapely.geometry import LineString, Point

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.gerber_geometry import parse_gerber_geometry
    from kicad_tools.validate.mask_copper import default_request_for_manufacturer

    output = BOARD / "output"
    pcb = PCB.load(output / "stm32_devboard_routed.kicad_pcb")
    with zipfile.ZipFile(output / "manufacturing.zip") as bundle:
        with zipfile.ZipFile(io.BytesIO(bundle.read("kicad_project.zip"))) as project:
            assert project.read(pcb.path.name) == pcb.path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(bundle.read("gerbers/gerbers.zip"))) as artwork:
            mask = artwork.read("stm32_devboard_routed-F_Mask.gts").decode()
    openings = parse_gerber_geometry(mask).geometry
    ox, oy = pcb.board_origin

    def absolute(pos):
        return pos[0] + ox, pos[1] + oy

    pad = next(p for p in pcb.get_footprint(ref).pads if p.number == pin)
    centre = Point(absolute(pcb.get_pad_position(ref, pin)))
    opening = next(p for p in openings.geoms if p.covers(centre))
    floor = default_request_for_manufacturer("jlcpcb-tier1").policy.clearance_mm
    for via in pcb.vias:
        gap = opening.distance(Point(absolute(via.position))) - via.size / 2
        assert gap >= floor, f"{ref}.{pin} to {via.net_name} via {via.uuid}: {gap:.6f} mm"
    for track in pcb.segments:
        if track.layer != "F.Cu":
            continue
        ends = [Point(absolute(p)) for p in (track.start, track.end)]
        if track.net_name == pad.net_name and any(opening.covers(p) for p in ends):
            continue  # The track terminates on its own land.
        gap = opening.distance(LineString(ends)) - track.width / 2
        assert gap >= floor, f"{ref}.{pin} to {track.net_name} track {track.uuid}: {gap:.6f} mm"


def load_recipe():
    spec = importlib.util.spec_from_file_location(
        "board04_export_recipe", BOARD / "generate_design.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def board(tmp_path):
    for name in (
        "stm32_devboard_routed.kicad_pcb",
        "stm32_devboard_routed.kicad_pro",
        "stm32_devboard_routed.kicad_dru",
        "manufacturing-requirements.json",
    ):
        shutil.copy2(BOARD / "output" / name, tmp_path / name)
    return tmp_path / "stm32_devboard_routed.kicad_pcb"


def test_export_preserves_reviewed_floors_in_project_zip(monkeypatch, board):
    recipe = load_recipe()
    schematic = board.parent / "stm32_devboard.kicad_sch"
    shutil.copy2(BOARD / "output/stm32_devboard.kicad_sch", schematic)
    recipe.write_portable_symbols(schematic)

    def export(package, output_dir):
        # Exercise the export's actual constraint writer if enabled, then its
        # actual ZIP writer. Rendering/assembly are independent of this bug.
        result = ManufacturingResult(output_dir=output_dir)
        if package.config.emit_drc_constraints:
            package._write_drc_constraints(result)
        output_dir.mkdir()
        _create_project_zip(package.pcb_path, output_dir)
        return result

    monkeypatch.setattr(ManufacturingPackage, "export", export)
    assert recipe.generate_manufacturing(board, board.parent)
    with zipfile.ZipFile(board.parent / "manufacturing/kicad_project.zip") as archive:
        # The routed project is delivered under the root sheet's basename, the
        # one project KiCad opens with schematic and board together (#10108).
        project = json.loads(archive.read("stm32_devboard.kicad_pro"))
        assert project["meta"]["filename"] == "stm32_devboard.kicad_pro"
        assert archive.read("stm32_devboard.kicad_pcb") == board.read_bytes()
        assert "stm32_devboard_routed.kicad_pro" not in archive.namelist()
        for name in ("board04_symbols.kicad_sym", "kicad_tools_pwr.kicad_sym", "sym-lib-table"):
            assert archive.read(name) == (board.parent / name).read_bytes()
    rules = project["board"]["design_settings"]["rules"]
    assert rules["min_via_hole"] == 0.15
    assert rules["min_via_diameter"] == 0.30
    assert rules["min_via_annular_width"] == 0.075


def test_export_rejects_unselected_paid_option(monkeypatch, board):
    (board.parent / "manufacturing-requirements.json").write_text("{}")

    def unexpected_export(*args, **kwargs):
        pytest.fail("Export must not run without the reviewed option")

    monkeypatch.setattr(ManufacturingPackage, "export", unexpected_export)
    with pytest.raises(ValueError, match="Select the reviewed paid"):
        load_recipe().generate_manufacturing(board, board.parent)


def test_paid_gate_reports_failure_from_json_when_stderr_empty(monkeypatch, tmp_path):
    script = BOARD.parents[1] / "scripts/ci/check_routed_drc_internal.py"
    spec = importlib.util.spec_from_file_location("board04_paid_gate", script)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    def check(command, **kwargs):
        Path(command[-1]).write_text(
            json.dumps({"meta_checks": {"manifest": {"status": "FAILED", "detail": "STALE"}}})
        )
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="")

    monkeypatch.setattr(gate.subprocess, "run", check)
    with pytest.raises(RuntimeError, match="STALE"):
        gate._run_paid_drill_check(tmp_path / "board.kicad_pcb", None)


def test_portable_capacitor_preserves_cached_symbol_and_instance_fields(tmp_path):
    from kicad_tools.sexp import parse_file, serialize_sexp

    recipe = load_recipe()
    schematic = tmp_path / "stm32_devboard.kicad_sch"
    shutil.copy2(BOARD / "output/stm32_devboard.kicad_sch", schematic)
    before = parse_file(schematic)
    # Exercise a stock binding even once the committed artifact is portable.
    for symbol in before.find_child("lib_symbols").find_children("symbol"):
        if symbol.get_string(0).endswith(":C_Small"):
            symbol.set_atom(0, "Device:C_Small")
    for symbol in before.find_children("symbol"):
        if symbol.find_child("lib_id").get_string(0).endswith(":C_Small"):
            symbol.find_child("lib_id").set_atom(0, "Device:C_Small")
    schematic.write_text(serialize_sexp(before))
    recipe.write_portable_symbols(schematic)
    after = parse_file(schematic)
    for symbol in after.find_child("lib_symbols").find_children("symbol"):
        if symbol.get_string(0) == "board04_symbols:C_Small":
            cached = serialize_sexp(symbol)
            symbol.set_atom(0, "Device:C_Small")
    for symbol in after.find_children("symbol"):
        if symbol.find_child("lib_id").get_string(0) == "board04_symbols:C_Small":
            symbol.find_child("lib_id").set_atom(0, "Device:C_Small")
    assert serialize_sexp(after) == serialize_sexp(before)
    local = parse_file(tmp_path / "board04_symbols.kicad_sym").find_child("symbol")
    local.set_atom(0, "board04_symbols:C_Small")
    assert serialize_sexp(local) == cached
    assert "${KIPRJMOD}/board04_symbols.kicad_sym" in (tmp_path / "sym-lib-table").read_text()
    content = schematic.read_bytes()
    recipe.write_portable_symbols(schematic)
    assert schematic.read_bytes() == content
