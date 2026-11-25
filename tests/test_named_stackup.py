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


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_foreign_save_at_silk_write_boundary_still_fails_named_stackup(tmp_path, monkeypatch):
    """An edit landing between the silk writer's write and any re-read (#9990).

    The foreign save is injected inside the writer, right after it published
    its bytes.  The pass must report the hash of the bytes it wrote, not of
    whatever the file holds a moment later, so the exporter refuses to bind
    ordering metadata to the other writer's copper.
    """
    from kicad_tools.core import sexp_file

    path = _silk_defect_stackup_board(tmp_path)
    real_write = sexp_file.atomic_write_text

    def write_then_foreign_save(target, content, **kwargs):
        real_write(target, content, **kwargs)
        # The silk pass writes the exporter's private staged copy (#10091).
        if Path(target).name == path.name:
            Path(target).write_text(Path(target).read_text() + "\n")

    monkeypatch.setattr(sexp_file, "atomic_write_text", write_then_foreign_save)

    result = _stackup_package(path).export(tmp_path / "out")

    assert not result.success
    assert "changed during" in result.errors[-1]
    assert result.stackup_path is None and result.manifest_path is None


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
@pytest.mark.parametrize("when", ["before_read", "before_publish", "during_publish", "open_handle"])
def test_silk_preserves_concurrent_drawing_save(tmp_path, monkeypatch, when):
    from kicad_tools.core import atomic_write
    from kicad_tools.drc import repair_silkscreen

    path = _silk_defect_stackup_board(tmp_path)
    drawing = (
        b'(gr_line (start 1 1) (end 2 2) (stroke (width 0.1) (type solid)) (layer "Dwgs.User"))'
    )
    edited = path.read_bytes().rstrip()[:-1] + drawing + b")\n"

    if when == "before_read":
        real_pass = repair_silkscreen.legalize_silk

        def save_before_read(*args, **kwargs):
            path.write_bytes(edited)
            return real_pass(*args, **kwargs)

        monkeypatch.setattr(repair_silkscreen, "legalize_silk", save_before_read)
    elif when == "before_publish":
        real_rename = Path.rename

        def save_before_rename(source, target):
            if source == path:
                path.write_bytes(edited)
            return real_rename(source, target)

        monkeypatch.setattr(Path, "rename", save_before_rename)
    elif when == "open_handle":
        real_link = atomic_write.os.link

        def save_through_open_handle(source, target, **kwargs):
            # An editor that kept the original file open writes into the
            # displaced inode after it was verified, just before the link.
            if target == path and Path(source).name == "repaired":
                (Path(source).parent / "original").write_bytes(edited)
            return real_link(source, target, **kwargs)

        monkeypatch.setattr(atomic_write.os, "link", save_through_open_handle)
    else:
        real_link = atomic_write.os.link

        def save_before_link(source, target, **kwargs):
            if target == path and not path.exists():
                path.write_bytes(edited)
            return real_link(source, target, **kwargs)

        monkeypatch.setattr(atomic_write.os, "link", save_before_link)

    out = tmp_path / "out"
    result = _stackup_package(path).export(out)
    assert not result.success
    assert "changed during" in result.errors[-1]
    assert path.read_bytes() == edited
    assert not (out / "stackup-ordering.json").exists()
    assert not (out / "manifest.json").exists()


def _break_hard_link_support(monkeypatch):
    """Simulate a filesystem without hard-link support (issue #10051).

    ``_publish_checked`` probes ``os.link`` before displacing the board;
    vfat/exFAT-style volumes answer that probe with an unsupported-operation
    error.  The injection lands on the real probe call only.
    """
    import errno

    from kicad_tools.core import atomic_write

    real_link = atomic_write.os.link

    def linkless(source, target, **kwargs):
        if Path(target).name == "link-test":
            raise OSError(errno.EOPNOTSUPP, "link() not supported on this filesystem")
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(atomic_write.os, "link", linkless)


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_linkless_filesystem_fails_named_stackup_export_before_publishing(tmp_path, monkeypatch):
    """Issue #10051: a named-stackup export must not ship unrepaired silk.

    The filesystem cannot support the checked publication the requested
    repair needs, so the export stops with an error before anything is
    written.  The probe runs before the board is displaced, so the board
    and its directory are left exactly as they were.
    """
    _break_hard_link_support(monkeypatch)
    path = _silk_defect_stackup_board(tmp_path)
    before = path.read_bytes()
    out = tmp_path / "out"
    result = _stackup_package(path).export(out)

    assert not result.success
    assert "Silk legalisation pass could not run" in result.errors[-1]
    assert "--no-silk-fix" in result.errors[-1]
    assert result.stackup_path is None and result.manifest_path is None
    assert not out.exists()
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(f".{path.name}.*"))  # staging cleaned up


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_linkless_filesystem_silk_optout_exports_original_artwork(tmp_path, monkeypatch):
    """Issue #10051: ``--no-silk-fix`` keeps the original artwork exportable.

    With the pass skipped, nothing on this volume needs hard links: the
    export succeeds with zero silk warnings, and the ordering record,
    project ZIP and manifest all describe the untouched board bytes.
    """
    import hashlib
    import zipfile

    from kicad_tools.export.manufacturing import verify_manifest

    _break_hard_link_support(monkeypatch)
    path = _silk_defect_stackup_board(tmp_path)
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    result = _stackup_package(path, silk_fix=False).export(tmp_path / "out")

    assert result.success, result.errors
    assert result.silk_fix is None
    assert not [w for w in result.warnings if "silk" in w.lower()]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    record = json.loads(result.stackup_path.read_text())
    assert record["pcb_sha256"] == before_hash
    with zipfile.ZipFile(result.project_zip_path) as zf:
        assert hashlib.sha256(zf.read(path.name)).hexdigest() == before_hash
    assert verify_manifest(result.manifest_path) == []


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_linkless_refusal_leaves_missing_sidecars_absent(tmp_path, monkeypatch):
    """Issue #10091: the refused repair must not conjure the sidecars.

    The live FAT32 run of the merged refusal left a generated 1,732-byte
    ``.kicad_pro`` and a 2,106-byte ``.kicad_dru`` beside a bare input PCB.
    A sibling that was absent before the export must still be absent after
    the refusal.
    """
    _break_hard_link_support(monkeypatch)
    path = _silk_defect_stackup_board(tmp_path)
    pcb_before = path.read_bytes()
    result = _stackup_package(path).export(tmp_path / "out")

    assert not result.success
    assert "Silk legalisation pass could not run" in result.errors[-1]
    assert path.read_bytes() == pcb_before
    assert not path.with_name(f".{path.name}.zonefill").exists()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []
    assert not path.with_suffix(".kicad_pro").exists()
    assert not path.with_suffix(".kicad_dru").exists()


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_linkless_refusal_restores_authored_sidecars(tmp_path, monkeypatch):
    """Issue #10091: authored project and rules bytes survive the refusal.

    The same live run showed both siblings of an authored project change
    before the refusal fired. Existing files must be preserved byte for
    byte -- here an authored text variable and a named custom rule.
    """
    _break_hard_link_support(monkeypatch)
    path = _silk_defect_stackup_board(tmp_path)
    pro = path.with_suffix(".kicad_pro")
    dru = path.with_suffix(".kicad_dru")
    pro.write_text(
        json.dumps({"meta": {"variables": {"my_text_variable": "authored"}}}, indent=2),
        encoding="utf-8",
    )
    dru.write_text('(rule "my authored rule" (constraint clearance (min 0.5mm)))\n')
    pro_before, dru_before = pro.read_bytes(), dru.read_bytes()

    result = _stackup_package(path).export(tmp_path / "out")

    assert not result.success
    assert "Silk legalisation pass could not run" in result.errors[-1]
    assert pro.read_bytes() == pro_before
    assert dru.read_bytes() == dru_before


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_linkless_refusal_preserves_editor_save_during_export(tmp_path, monkeypatch):
    """Issue #10091: an editor save landing mid-export is not clobbered.

    The rollback may only undo the bytes this export itself wrote: a newer
    save that arrives between the sidecar write and the refusal keeps the
    live name, so restoring a stale pre-export snapshot must not happen.
    """
    from kicad_tools.cli import silk_default_pass

    _break_hard_link_support(monkeypatch)
    path = _silk_defect_stackup_board(tmp_path)
    dru = path.with_suffix(".kicad_dru")
    dru.write_text('(rule "authored" (constraint clearance (min 0.4mm)))\n')
    editor_save = '(rule "editor mid-export save" (constraint clearance (min 0.6mm)))\n'

    real_pass = silk_default_pass.run_default_silk_pass

    def save_then_run_pass(pcb, **kwargs):
        dru.write_text(editor_save)  # the editor saves while export runs
        return real_pass(pcb, **kwargs)

    monkeypatch.setattr(silk_default_pass, "run_default_silk_pass", save_then_run_pass)
    result = _stackup_package(path).export(tmp_path / "out")

    assert not result.success
    assert "Silk legalisation pass could not run" in result.errors[-1]
    assert dru.read_text() == editor_save


@pytest.mark.parametrize(
    "blocked",
    ["board.kicad_pcb", "board.kicad_pro", "board.kicad_dru", ".board.kicad_pcb.zonefill"],
)
@pytest.mark.parametrize("missing_others", [False, True])
def test_publication_refusal_restores_every_source_file(
    tmp_path, monkeypatch, blocked, missing_others
):
    import os

    from kicad_tools.export.manufacturing import _StagedSource

    board = tmp_path / "board.kicad_pcb"
    paths = [
        board,
        board.with_suffix(".kicad_pro"),
        board.with_suffix(".kicad_dru"),
        tmp_path / ".board.kicad_pcb.zonefill",
    ]
    for path in paths:
        if not missing_others or path == board or path.name == blocked:
            path.write_bytes(f"authored {path.name}\n".encode())
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    staged = _StagedSource(board)
    for path in paths:
        (staged.dir / path.name).write_bytes(f"produced {path.name}\n".encode())

    def immutable_at_boundary(operation):
        def wrapped(src, dst, *args, **kwargs):
            if tmp_path / blocked in (Path(src), Path(dst)):
                raise PermissionError("immutable authored source")
            return operation(src, dst, *args, **kwargs)

        return wrapped

    for name in ("rename", "replace"):
        monkeypatch.setattr(os, name, immutable_at_boundary(getattr(os, name)))
    try:
        with pytest.raises(PermissionError, match="immutable"):
            staged.publish()
    finally:
        staged.close()
    assert {p.name for p in tmp_path.iterdir()} == {
        p.name for p, data in before.items() if data is not None
    }
    assert all(p.read_bytes() == data for p, data in before.items() if data is not None)


@pytest.mark.parametrize(
    "name", ["board.kicad_pro", "board.kicad_dru", ".board.kicad_pcb.zonefill"]
)
def test_publication_preserves_an_editor_save_at_the_final_rename(tmp_path, monkeypatch, name):
    import os

    from kicad_tools.core.atomic_write import SourceChangedError
    from kicad_tools.export.manufacturing import _StagedSource

    board = tmp_path / "board.kicad_pcb"
    paths = [
        board,
        board.with_suffix(".kicad_pro"),
        board.with_suffix(".kicad_dru"),
        tmp_path / ".board.kicad_pcb.zonefill",
    ]
    for path in paths:
        path.write_bytes(b"authored\n")
    staged = _StagedSource(board)
    for path in paths:
        (staged.dir / path.name).write_bytes(b"generated\n")
    target = tmp_path / name
    replace = os.replace
    edited = False

    def save_at_boundary(operation):
        def wrapped(src, dst, *args, **kwargs):
            nonlocal edited
            if not edited and target in (Path(src), Path(dst)):
                edited = True
                file = tmp_path / "editor-save"
                file.write_bytes(b"editor\n")
                replace(file, target)
            return operation(src, dst, *args, **kwargs)

        return wrapped

    for method in ("rename", "replace"):
        monkeypatch.setattr(os, method, save_at_boundary(getattr(os, method)))
    try:
        with pytest.raises(SourceChangedError, match="changed"):
            staged.publish()
    finally:
        staged.close()
    assert edited
    assert target.read_bytes() == b"editor\n"
    assert all(p.read_bytes() == b"authored\n" for p in paths if p != target)


@pytest.mark.parametrize(
    "editor_target", ["original-handle", "published-project", "published-board"]
)
def test_editor_save_during_board_commit_rolls_back_the_group(tmp_path, monkeypatch, editor_target):
    import os

    from kicad_tools.core.atomic_write import SourceChangedError
    from kicad_tools.export.manufacturing import _StagedSource

    board = tmp_path / "board.kicad_pcb"
    project = board.with_suffix(".kicad_pro")
    rules = board.with_suffix(".kicad_dru")
    for path in (board, project, rules):
        path.write_bytes(b"authored")
    staged = _StagedSource(board)
    for path in (board, project, rules):
        (staged.dir / path.name).write_bytes(b"generated")

    link = os.link
    with project.open("r+b", buffering=0) as old_project:

        def edit_while_board_is_provisional(src, dst, **kwargs):
            result = link(src, dst, **kwargs)
            if Path(dst) == board and Path(src).name == "repaired":
                if editor_target == "original-handle":
                    old_project.seek(0)
                    old_project.write(b"editor")
                    old_project.truncate()
                else:
                    path = project if editor_target == "published-project" else board
                    saved = tmp_path / "editor-save"
                    saved.write_bytes(b"editor")
                    os.replace(saved, path)
            return result

        monkeypatch.setattr(os, "link", edit_while_board_is_provisional)
        try:
            with pytest.raises(SourceChangedError, match="changed"):
                staged.publish()
        finally:
            staged.close()
    assert board.read_bytes() == (b"editor" if editor_target == "published-board" else b"authored")
    assert project.read_bytes() == (
        b"authored" if editor_target == "published-board" else b"editor"
    )
    assert rules.read_bytes() == b"authored"


@pytest.mark.parametrize("fault", ["none", "editor", "two-editors", "short-write"])
def test_linkless_sidecar_publication_for_an_unchanged_board(tmp_path, monkeypatch, fault):
    import os

    from kicad_tools.core.atomic_write import SourceChangedError
    from kicad_tools.export import manufacturing

    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"unchanged")
    project, rules = board.with_suffix(".kicad_pro"), board.with_suffix(".kicad_dru")
    project.write_bytes(b"authored")
    stage = manufacturing._StagedSource(board)
    stage.pcb.with_suffix(".kicad_pro").write_bytes(b"new project")
    stage.pcb.with_suffix(".kicad_dru").write_bytes(b"new rules")
    # macOS FAT32 supports exclusive creation but neither hard links nor
    # exclusive rename. Exercise that last publication method on every CI host.
    monkeypatch.setattr(manufacturing, "_install_unoccupied", manufacturing._copy_unoccupied)
    opened = os.open
    write = os.write
    target_fd = None

    def reserve(path, flags, *args, **kwargs):
        nonlocal target_fd
        fd = opened(path, flags, *args, **kwargs)
        if target_fd is None and Path(path) == rules and flags & os.O_EXCL:
            target_fd = fd
            if fault in ("editor", "two-editors"):
                rules.write_bytes(b"editor")
        return fd

    def write_part(fd, data):
        if fault == "short-write" and fd == target_fd and data == b"new rules":
            return write(fd, data[:2])
        return write(fd, data)

    rename = os.rename

    def save_again(src, dst, *args, **kwargs):
        result = rename(src, dst, *args, **kwargs)
        if fault == "two-editors" and Path(src) == rules and Path(dst).name == "captured":
            rules.write_bytes(b"second save")
        return result

    monkeypatch.setattr(os, "open", reserve)
    monkeypatch.setattr(os, "write", write_part)
    monkeypatch.setattr(os, "rename", save_again)
    try:
        if fault == "none":
            stage.publish()
        else:
            with pytest.raises(SourceChangedError if fault == "editor" else OSError):
                stage.publish()
    finally:
        stage.close()
    assert board.read_bytes() == b"unchanged"
    assert project.read_bytes() == (b"new project" if fault == "none" else b"authored")
    if fault == "two-editors":
        assert rules.read_bytes() == b"second save"
        assert [p.read_bytes() for p in stage.dir.rglob("editor")] == [b"editor"]
    elif fault == "short-write":
        assert not rules.exists()
    else:
        assert rules.read_bytes() == (b"editor" if fault == "editor" else b"new rules")


def test_strict_preflight_failure_still_emits_drc_constraints(tmp_path, monkeypatch):
    """Issue #10091 regression guard: only the silk refusal undoes the writes.

    Step 0a writes the sidecars before preflight on purpose -- a
    strict-preflight failure still leaves the emitted rules beside the
    board -- so the refusal rollback must not leak into that path.
    """
    from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
    from kicad_tools.export.preflight import PreflightConfig, PreflightResult

    path = authored_pcb(tmp_path, "JLC04161H-3313")
    pkg = ManufacturingPackage(
        path,
        manufacturer="jlcpcb",
        config=ManufacturingConfig(
            stackup_id="JLC04161H-3313",
            bom_source="pcb",
            include_bom=False,
            include_pnp=False,
            include_gerbers=False,
            include_report=False,
            include_project_zip=True,
            preflight=PreflightConfig(),
            strict_preflight=True,
            silk_fix=False,
        ),
    )

    def failing_preflight():
        return [PreflightResult(name="drc", status="FAIL", message="simulated failure")]

    monkeypatch.setattr(pkg, "_run_preflight", failing_preflight)
    result = pkg.export(tmp_path / "out")

    assert not result.success
    assert any("Preflight FAIL [drc]" in e for e in result.errors)
    assert result.drc_constraint_paths
    assert path.with_suffix(".kicad_pro").exists()
    assert path.with_suffix(".kicad_dru").exists()


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
def test_unrunnable_silk_pass_plain_export_keeps_warning(tmp_path, monkeypatch):
    """Issue #10051 boundary: only the named-stackup export escalates.

    Without ``stackup_id`` a pass that cannot run stays a warning and the
    export still ships (#9799's never-fatal default pass).
    """
    import errno

    from kicad_tools.drc import repair_silkscreen
    from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
    from kicad_tools.export.preflight import PreflightConfig

    def unavailable(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "link() not supported on this filesystem")

    monkeypatch.setattr(repair_silkscreen, "legalize_silk", unavailable)

    path = _silk_defect_stackup_board(tmp_path)
    config = ManufacturingConfig(
        bom_source="pcb",
        include_bom=False,
        include_pnp=False,
        include_gerbers=False,
        include_report=False,
        include_project_zip=False,
        preflight=PreflightConfig(skip_all=True),
    )
    result = ManufacturingPackage(path, manufacturer="jlcpcb", config=config).export(
        tmp_path / "out"
    )

    assert result.success, result.errors
    assert any("Silk legalisation pass could not run" in w for w in result.warnings)


@pytest.mark.skipif(not has_shapely(), reason="silk pass needs shapely")
@pytest.mark.parametrize("failure", ["hard-link", "layer-detection"])
def test_export_cli_named_stackup_refuses_unrepairable_silk(tmp_path, capsys, monkeypatch, failure):
    from kicad_tools.cli.export_cmd import main

    if failure == "hard-link":
        _break_hard_link_support(monkeypatch)
    else:

        def unreadable_layers(_board_text):
            raise OSError("cannot detect the board's copper layers")

        monkeypatch.setattr("kicad_tools.router.io.detect_layer_stack", unreadable_layers)

    path = _silk_defect_stackup_board(tmp_path)
    before = path.read_bytes()
    output = tmp_path / "must-not-exist"
    assert (
        main(
            [
                str(path),
                "--stackup-id",
                "JLC04161H-3313",
                "-o",
                str(output),
                "--bom-source",
                "pcb",
                "--skip-preflight",
                "--no-auto-lcsc",
            ]
        )
        == 1
    )
    assert path.read_bytes() == before
    assert not output.exists()
    err = capsys.readouterr().err.lower()
    assert "silk legalisation pass could not run" in err
    assert "--no-silk-fix" in err


def test_save_pcb_returns_hash_of_published_bytes(tmp_path):
    import hashlib

    from kicad_tools.core.sexp_file import save_pcb
    from kicad_tools.sexp import parse_string

    target = tmp_path / "board.kicad_pcb"
    digest = save_pcb(parse_string("(kicad_pcb (version 20240108))"), target)
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
