"""Symlinked library members must not escape the project zip (#8763)."""

from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.export.manufacturing import _create_project_zip, _project_footprint_files


@pytest.fixture
def project(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    (outside / "ext.pretty").mkdir(parents=True)
    (outside / "ext.pretty" / "ext.kicad_mod").write_text("(footprint ext)")
    (outside / "secret.kicad_mod").write_text("(footprint secret)")
    (outside / "inner_target").mkdir()

    p = tmp_path / "p"
    (p / "local.pretty").mkdir(parents=True)
    (p / "local.pretty" / "ok.kicad_mod").write_text("(footprint ok)")
    (p / "local.pretty" / "leak.kicad_mod").symlink_to(outside / "secret.kicad_mod")
    (p / "local.pretty" / "broken.kicad_mod").symlink_to(tmp_path / "nope")
    (p / "ext.pretty").symlink_to(outside / "ext.pretty")
    (p / "real").mkdir()
    (p / "real" / "inside.kicad_mod").write_text("(footprint inside)")
    (p / "local.pretty" / "alias.kicad_mod").symlink_to(p / "real" / "inside.kicad_mod")
    (p / "b.kicad_pcb").write_text("(kicad_pcb)")
    return p


def _names(files, root: Path) -> set[str]:
    return {Path(f).relative_to(root).as_posix() for f in files}


def test_outside_members_excluded_and_inside_symlink_kept(project, caplog):
    root = project.resolve()
    with caplog.at_level(logging.WARNING):
        files = _project_footprint_files(project)
    names = _names(files, root)
    assert "local.pretty/ok.kicad_mod" in names
    assert "local.pretty/alias.kicad_mod" in names
    assert "local.pretty/leak.kicad_mod" not in names
    assert not any(n.startswith("ext.pretty/") for n in names)
    assert "leak.kicad_mod" in caplog.text and "ext.kicad_mod" in caplog.text


def test_readiness_and_exporter_agree(project):
    root = project.resolve()
    pcb = root / "b.kicad_pcb"
    ready = {p for p in readiness_cmd._project_dependencies(pcb, root) if p.suffix == ".kicad_mod"}
    export = {p for p in _project_footprint_files(project) if p.suffix == ".kicad_mod"}
    assert _names(ready, root) == _names(export, root)
    assert not any("leak" in str(p) or "ext.kicad_mod" in str(p) for p in ready)


def test_symlink_loop_is_skipped(project):
    os.symlink(project / "local.pretty", project / "local.pretty" / "loop")
    names = _names(_project_footprint_files(project), project.resolve())
    assert "local.pretty/ok.kicad_mod" in names


def test_local_3d_library_ships_with_license_and_is_verified(project, tmp_path):
    shapes = project / "Local.3dshapes"
    shapes.mkdir()
    model = shapes / "formed.step"
    model.write_bytes(b"project-local STEP model")
    notice = shapes / "LICENSE.md"
    notice.write_text("Local model license")
    outside = tmp_path / "outside-model.step"
    outside.write_bytes(b"external model")
    (shapes / "external.step").symlink_to(outside)

    pcb = project / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (footprint "Local:F" (model "${KIPRJMOD}/Local.3dshapes/formed.step")))'
    )
    out = tmp_path / "export"
    out.mkdir()
    dependencies = readiness_cmd._project_dependencies(pcb, project)
    with zipfile.ZipFile(_create_project_zip(pcb, out)) as archive:
        for member in (model, notice):
            relative = member.relative_to(project).as_posix()
            assert member in dependencies
            assert readiness_cmd._is_project_local_library_member(relative)
            assert archive.read(relative) == member.read_bytes()
        assert "Local.3dshapes/external.step" not in archive.namelist()
    assert shapes / "external.step" not in dependencies
