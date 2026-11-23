"""Factory construction identity must describe the physical impedance model."""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.physics import Stackup, TransmissionLine


@pytest.mark.parametrize(
    "identifier,height,er,core",
    [
        ("JLC04161H-3313", 0.0994, 4.1, 1.265),
        ("JLC04161H-7628", 0.2104, 4.4, 1.065),
    ],
)
def test_named_construction(identifier, height, er, core):
    stack = Stackup.jlcpcb_named(identifier)
    assert stack.get_dielectric_height("F.Cu") == pytest.approx(height)
    assert stack.layers[1].epsilon_r == er
    assert stack.layers[3].thickness_mm == core
    assert stack.layers[2].thickness_mm == 0.0152
    assert stack.summary()["construction"]["factory_id"] == identifier
    assert stack.summary()["construction"]["source_url"] == "https://jlcpcb.com/impedance"
    width = TransmissionLine(stack).width_for_impedance(50, "F.Cu")
    assert TransmissionLine(stack).microstrip(width_mm=width, layer="F.Cu").z0 == pytest.approx(
        50, abs=0.5
    )


def test_legacy_keeps_numbers_without_factory_identity():
    legacy = Stackup.jlcpcb_4layer()
    assert legacy.layers[1].thickness_mm == 0.2104
    assert legacy.layers[1].epsilon_r == 4.05
    assert legacy.layers[2].thickness_mm == 0.0175
    assert legacy.summary()["construction"]["factory_id"] is None
    assert legacy.summary()["construction"]["id"] == "jlcpcb-4-legacy"
    assert Stackup.jlcpcb_4layer_legacy().summary() == legacy.summary()
    assert TransmissionLine(Stackup.jlcpcb_named("JLC04161H-3313")).width_for_impedance(
        50, "F.Cu"
    ) < TransmissionLine(legacy).width_for_impedance(50, "F.Cu")


def authored_pcb(tmp_path, identifier):
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.create(width=20, height=20, layers=4)
    stack = Stackup.jlcpcb_named(identifier)
    nodes = [
        n
        for n in pcb._sexp.get("setup").get("stackup").find_children("layer")
        if n.get("type") and n.get("type").get_atoms()[0] in {"copper", "core", "prepreg"}
    ]
    assert len(nodes) == 7
    for node, layer in zip(nodes, stack.layers, strict=True):
        node.get("thickness").set_value(0, layer.thickness_mm)
        if layer.is_dielectric:
            node.get("epsilon_r").set_value(0, layer.epsilon_r)
    path = tmp_path / "board.kicad_pcb"
    pcb.save(path)
    return path


def test_ordering_record_uses_actual_source_and_rejects_mismatch(tmp_path):
    import hashlib

    from kicad_tools.export.stackup import stackup_ordering_record

    path = authored_pcb(tmp_path, "JLC04161H-3313")
    before = path.read_bytes()
    record = stackup_ordering_record(path, "JLC04161H-3313")
    assert record["pcb_sha256"] == hashlib.sha256(before).hexdigest()
    assert record["construction"]["factory_id"] == "JLC04161H-3313"
    with pytest.raises(ValueError, match="mismatch"):
        stackup_ordering_record(path, "JLC04161H-7628")
    assert path.read_bytes() == before


def test_named_cli_and_explicit_pcb_priority(tmp_path):
    from kicad_tools.cli.commands.impedance import _get_stackup

    stack = _get_stackup(Namespace(pcb=None, impedance_preset="jlcpcb-3313"))
    assert stack.layers[1].thickness_mm == 0.0994
    pcb = authored_pcb(tmp_path, "JLC04161H-3313")
    selected = _get_stackup(Namespace(pcb=str(pcb), impedance_preset="jlcpcb-7628"))
    assert selected.has_explicit_data
    assert selected.get_dielectric_height("F.Cu") == pytest.approx(0.0994)


def test_manufacturing_metadata_is_manifest_bound_and_mismatch_writes_nothing(tmp_path):
    from kicad_tools.export.manufacturing import (
        ManufacturingConfig,
        ManufacturingPackage,
        verify_manifest,
    )
    from kicad_tools.export.preflight import PreflightConfig

    path = authored_pcb(tmp_path, "JLC04161H-3313")

    def package(identifier):
        return ManufacturingPackage(
            path,
            config=ManufacturingConfig(
                stackup_id=identifier,
                bom_source="pcb",
                include_bom=False,
                include_pnp=False,
                include_gerbers=False,
                include_report=False,
                include_project_zip=False,
                preflight=PreflightConfig(skip_all=True),
            ),
        )

    bad = tmp_path / "bad"
    result = package("JLC04161H-7628").export(bad)
    assert not result.success and "mismatch" in result.errors[0]
    assert not bad.exists()
    before = path.read_bytes()
    result = package("JLC04161H-3313").export(tmp_path / "good")
    assert result.success, result.errors
    assert result.stackup_path in result.all_files
    manifest = json.loads(result.manifest_path.read_text())
    assert "stackup-ordering.json" in manifest["files"]
    assert verify_manifest(result.manifest_path) == []
    assert path.read_bytes() == before


def test_no_explicit_stack_cannot_get_factory_order_identity(tmp_path):
    from kicad_tools.export.stackup import stackup_ordering_record
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / "implicit.kicad_pcb"
    pcb = PCB.create(layers=4)
    pcb._sexp.get("setup").children.remove(pcb._sexp.get("setup").get("stackup"))
    pcb.save(path)
    with pytest.raises(ValueError, match="no explicit"):
        stackup_ordering_record(path, "JLC04161H-3313")


def test_cli_parser_accepts_named_preset(capsys):
    from kicad_tools.cli import main

    assert main(["impedance", "stackup", "--preset", "jlcpcb-7628", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["construction"]["factory_id"] == "JLC04161H-7628"


@pytest.mark.parametrize("mutation", ["epsilon", "copper", "nominal"])
def test_factory_match_rejects_changed_physics(tmp_path, mutation):
    from kicad_tools.export.stackup import stackup_ordering_record
    from kicad_tools.schema.pcb import PCB

    path = authored_pcb(tmp_path, "JLC04161H-3313")
    pcb = PCB.load(path)
    if mutation == "nominal":
        pcb._sexp.get("general").get("thickness").set_value(0, 2.0)
    else:
        for node in pcb._sexp.get("setup").get("stackup").find_children("layer"):
            if mutation == "epsilon" and node.get_atoms()[0] == "dielectric 1":
                node.get("epsilon_r").set_value(0, 4.4)
            elif mutation == "copper" and node.get_atoms()[0] == "In1.Cu":
                node.get("thickness").set_value(0, 0.0175)
    pcb.save(path)
    with pytest.raises(ValueError, match="mismatch"):
        stackup_ordering_record(path, "JLC04161H-3313")


def test_export_cli_rejects_mismatch_before_output(tmp_path, capsys):
    from kicad_tools.cli.export_cmd import main

    path = authored_pcb(tmp_path, "JLC04161H-3313")
    output = tmp_path / "must-not-exist"
    assert (
        main([str(path), "--stackup-id", "JLC04161H-7628", "-o", str(output), "--skip-preflight"])
        == 1
    )
    assert not output.exists()
    assert "mismatch" in capsys.readouterr().err.lower()


def test_export_cannot_rebind_metadata_after_source_changed(tmp_path, monkeypatch):
    from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
    from kicad_tools.export.preflight import PreflightConfig

    path = authored_pcb(tmp_path, "JLC04161H-3313")
    config = ManufacturingConfig(
        stackup_id="JLC04161H-3313",
        bom_source="pcb",
        include_bom=False,
        include_pnp=False,
        include_gerbers=False,
        include_report=False,
        include_project_zip=False,
        preflight=PreflightConfig(skip_all=True),
    )
    pkg = ManufacturingPackage(path, config=config)

    def change_source(*args):
        path.write_text(path.read_text() + "\n")
        return True

    monkeypatch.setattr(pkg, "_generate_assembly", change_source)
    result = pkg.export(tmp_path / "output")
    assert not result.success
    assert "changed during" in result.errors[-1]
    assert result.manifest_path is None and result.stackup_path is None


def _silk_defect_stackup_board(tmp_path, identifier="JLC04161H-3313"):
    """The named-construction board plus authored silk defects the pass repairs.

    Same construction as :func:`authored_pcb`; the appended defects (the
    wiring suite's ``DEFECTS``) give the default silk pass one repair to
    apply, so Step 0c re-saves the board in place (#9990).
    """
    from tests.test_silk_default_pass_wiring import DEFECTS

    path = authored_pcb(tmp_path, identifier)
    text = path.read_text().rstrip()
    path.write_text(text[:-1] + DEFECTS + ")\n")
    return path


def _stackup_package(path, **overrides):
    from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
    from kicad_tools.export.preflight import PreflightConfig

    config = ManufacturingConfig(
        stackup_id="JLC04161H-3313",
        bom_source="pcb",
        include_bom=False,
        include_pnp=False,
        include_gerbers=False,
        include_report=False,
        include_project_zip=True,
        preflight=PreflightConfig(skip_all=True),
        **overrides,
    )
    return ManufacturingPackage(path, manufacturer="jlcpcb", config=config)


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_silk_repair_rebinds_stackup_provenance_to_shipped_bytes(tmp_path):
    import hashlib
    import zipfile

    from kicad_tools.export.manufacturing import verify_manifest

    path = _silk_defect_stackup_board(tmp_path)
    result = _stackup_package(path).export(tmp_path / "out")

    assert result.silk_fix is not None and result.silk_fix["applied"] is True
    assert result.silk_fix["total_fixed"] >= 1  # the pass really re-saved the board
    assert result.success, result.errors

    shipped = hashlib.sha256(path.read_bytes()).hexdigest()
    record = json.loads(result.stackup_path.read_text())
    assert record["pcb_sha256"] == shipped  # ordering record names the repaired PCB
    with zipfile.ZipFile(result.project_zip_path) as zf:
        assert hashlib.sha256(zf.read(path.name)).hexdigest() == shipped
    assert verify_manifest(result.manifest_path) == []


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_silk_pass_zero_edit_stackup_export_keeps_source_bytes(tmp_path):
    import hashlib

    path = authored_pcb(tmp_path, "JLC04161H-3313")
    before = path.read_bytes()
    result = _stackup_package(path).export(tmp_path / "out")

    assert result.success, result.errors
    assert result.silk_fix is not None and result.silk_fix["total_fixed"] == 0
    assert path.read_bytes() == before  # a zero-edit board is not re-saved
    record = json.loads(result.stackup_path.read_text())
    assert record["pcb_sha256"] == hashlib.sha256(before).hexdigest()


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_foreign_edit_during_silk_stage_still_fails_named_stackup(tmp_path, monkeypatch):
    from kicad_tools.cli import silk_default_pass

    path = _silk_defect_stackup_board(tmp_path)
    real_pass = silk_default_pass.run_default_silk_pass

    def pass_then_foreign_edit(board, **kwargs):
        document = real_pass(board, **kwargs)
        if document and document.get("applied"):
            # An independent drawing edit landing right after the pass wrote
            # its result must not be adopted as the exporter's own bytes.
            Path(board).write_text(Path(board).read_text() + "\n")
        return document

    monkeypatch.setattr(silk_default_pass, "run_default_silk_pass", pass_then_foreign_edit)

    result = _stackup_package(path).export(tmp_path / "out")

    assert not result.success
    assert "changed during" in result.errors[-1]
    assert result.stackup_path is None and result.manifest_path is None
