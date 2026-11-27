"""The editable manufacturing project retains the input to its managed rules."""

import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest

from kicad_tools.cli import export_cmd, readiness_cmd
from kicad_tools.export.assembly import AssemblyPackage
from kicad_tools.sidecars import net_class_map_sidecar_candidates
from tests.test_kct_readiness_cmd import FakeEngines
from tests.test_project_archive_identity_10108 import (
    archive_members,
    generate,
    options_for,
    reseal,
    staged_design,
)

BOARD02 = Path(__file__).resolve().parents[1] / "boards/02-charlieplex-led/output"
BARE = "net_class_map.json"


@pytest.mark.parametrize("spelling", ["bare", "stem", "both"])
def test_board02_reexport_keeps_vcc_rule_and_map(tmp_path, spelling):
    design = tmp_path / "released"
    design.mkdir()
    pcb = design / "charlieplex_3x3_routed.kicad_pcb"
    schematic = design / "charlieplex_3x3.kicad_sch"
    for source in (pcb, schematic, pcb.with_suffix(".kicad_pro"), pcb.with_suffix(".kicad_dru")):
        shutil.copyfile(BOARD02 / source.name, source)
    rules = pcb.with_suffix(".kicad_dru").read_bytes()
    assert b"VCC" in rules and b"(constraint track_width (min 0.0125mm))" in rules
    selected = (BOARD02 / BARE).read_bytes()
    if spelling == "both":
        (design / BARE).write_bytes(b"{}")
    name = BARE if spelling == "bare" else f"{pcb.stem}.{BARE}"
    (design / name).write_bytes(selected)

    for generation in range(2):
        bundle = tmp_path / f"bundle-{generation}"
        assert (
            export_cmd.main(
                [
                    str(pcb),
                    "--sch",
                    str(schematic),
                    "--mfr",
                    "jlcpcb",
                    "-o",
                    str(bundle),
                    "--no-gerbers",
                    "--no-report",
                    "--no-auto-lcsc",
                    "--skip-preflight",
                ]
            )
            == 0
        )
        extracted = tmp_path / f"recipient-{generation}"
        with zipfile.ZipFile(bundle / "kicad_project.zip") as zf:
            zf.extractall(extracted)
        pcb = extracted / schematic.with_suffix(".kicad_pcb").name
        schematic = extracted / schematic.name
        found = [path for path in net_class_map_sidecar_candidates(pcb) if path.is_file()]
        assert len(found) == 1
        assert found[0].read_bytes() == selected
        assert pcb.with_suffix(".kicad_dru").read_bytes() == rules


@pytest.mark.parametrize("explicit", [False, True])
def test_readiness_binds_packaged_map_to_selected_input(tmp_path, explicit, monkeypatch):
    board = staged_design(tmp_path)
    design = board / "output"
    (design / BARE).write_bytes(b"{}")
    keyed = design / f"board_v07.{BARE}"
    keyed.write_bytes(b'{ "VCC": {"name": "power"} }\n')
    selected = keyed
    args = ["--sch", str(design / "board.kicad_sch")]
    if explicit:
        selected = board / "reviewed-classes.json"
        selected.write_bytes(b'{ "VCC": {"name": "reviewed"} }\n')
        args.extend(["--net-class-map", str(selected)])

    # Exercise the real generic exporter and the release receipt together.
    # The Gerber plot is unrelated to the editable archive and needs native KiCad.
    def plot_gerbers(self, out_dir):
        archive = out_dir / "gerbers" / "gerbers.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("board.gbr", "G04*")
            zf.writestr("board.drl", "M48")
        return archive

    monkeypatch.setattr(AssemblyPackage, "_generate_gerbers", plot_gerbers)
    engines = FakeEngines(board)
    monkeypatch.setattr(engines, "refill", lambda pcb: readiness_cmd.EngineRun(ok=True))
    monkeypatch.setattr(engines, "export", readiness_cmd._run_export)
    report = generate(board, *args, engines=engines)
    members = archive_members(design / "manufacturing")
    maps = [name for name in members if name.endswith(BARE)]
    assert len(maps) == 1
    member = maps[0]
    assert member in (f"board.{BARE}", BARE)
    assert members[member] == selected.read_bytes()
    assert (
        report["inputs"][selected.relative_to(board).as_posix()]
        == hashlib.sha256(members[member]).hexdigest()
    )
    if explicit:
        assert report["selected_net_class_map"] == "reviewed-classes.json"
    else:
        assert "selected_net_class_map" not in report
    options = options_for(board, *args)
    assert report["engine"]["rules_digest"] == readiness_cmd._rules_digest(
        [options.project, selected, *design.glob("*.kicad_dru")]
    )
    reseal(options, lambda contents: contents.update({member: b"{}"}))
    assert any(member in p for p in readiness_cmd._verify_project_provenance(options))

    for path in {selected, keyed, design / BARE}:
        path.unlink()
    options = options_for(board, "--sch", str(design / "board.kicad_sch"))
    assert any("no checked source" in p for p in readiness_cmd._verify_project_provenance(options))
    reseal(options, lambda contents: contents.pop(member))
    assert readiness_cmd._verify_project_provenance(options) == []


def test_export_checks_map_boundary_before_packaging(tmp_path):
    from kicad_tools.export.manufacturing import (
        ManufacturingConfig,
        ManufacturingPackage,
        ManufacturingResult,
    )

    board = tmp_path / "board"
    board.mkdir()
    pcb = board / "board.kicad_pcb"
    pcb.touch()
    sidecar = board / BARE
    target = tmp_path / "selected.json"
    target.write_bytes(b"{}")
    sidecar.symlink_to(target)
    package = ManufacturingPackage(pcb)
    result = ManufacturingResult(output_dir=board)
    package._generate_project_zip(board, result)
    assert any("resolves outside" in error for error in result.errors)
    assert not (board / "kicad_project.zip").exists()

    config = ManufacturingConfig(project_root=tmp_path)
    assert ManufacturingPackage(pcb, config=config)._net_class_map_input == (target, b"{}")


def test_export_finds_map_in_sibling_output(tmp_path):
    from kicad_tools.export.manufacturing import ManufacturingPackage

    design = tmp_path / "design"
    design.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    selected = output / BARE
    selected.write_bytes(b"{}")
    assert ManufacturingPackage(design / "board.kicad_pcb")._net_class_map_input == (
        selected,
        b"{}",
    )
