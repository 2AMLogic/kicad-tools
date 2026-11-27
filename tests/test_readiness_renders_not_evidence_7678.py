"""Renders are presentation artifacts, not readiness evidence (Issue #7678).

``scripts/deploy-site.sh`` re-renders ``output/renders`` and
``output/manufacturing/renders``; fingerprinting them made every deploy
invalidate the committed readiness record.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.readiness_cmd import (
    EngineFingerprint,
    ReadinessOptions,
    _hashable_inputs,
    _is_render_artifact,
    build_report,
)

BOARDS_DIR = Path(__file__).resolve().parent.parent / "boards"


def _options(board: Path) -> ReadinessOptions:
    return ReadinessOptions(
        board_dir=board,
        pcb=board / "output" / "b.kicad_pcb",
        schematic=None,
        project=None,
        manufacturer="jlcpcb",
        mode="verify",
        output_dir=board / "output" / "manufacturing",
        evidence_dir=board / "output" / "readiness",
    )


def test_render_files_are_not_hashable_inputs(tmp_path: Path) -> None:
    out = tmp_path / "output"
    (out / "renders").mkdir(parents=True)
    (out / "manufacturing" / "renders").mkdir(parents=True)
    (out / "b.kicad_pcb").write_text("(kicad_pcb)")
    (out / "renders" / "3d-front.png").write_bytes(b"png")
    (out / "manufacturing" / "renders" / "pcb-front.svg").write_text("<svg/>")
    (out / "manufacturing" / "report.md").write_text("report")

    names = {
        p.relative_to(tmp_path.resolve()).as_posix() for p in _hashable_inputs(_options(tmp_path))
    }

    assert "output/manufacturing/report.md" in names
    assert not [n for n in names if "/renders/" in n], names


def test_board_recipe_cannot_add_renders_as_evidence(tmp_path: Path) -> None:
    render = tmp_path / "output" / "renders" / "3d-front.png"
    render.parent.mkdir(parents=True)
    render.write_bytes(b"image")
    recipe = tmp_path / "generate_design.py"
    recipe.write_text("# board generator\n")

    report = build_report(
        _options(tmp_path),
        [],
        {},
        EngineFingerprint(kicad_tools_version="test"),
        extra_inputs=[render, recipe],
    )

    assert "generate_design.py" in report["inputs"]
    assert "output/renders/3d-front.png" not in report["inputs"]


def test_committed_readiness_does_not_fingerprint_renders() -> None:
    offenders = []
    for report in sorted(BOARDS_DIR.glob("*/output/readiness.json")):
        inputs = json.loads(report.read_text()).get("inputs", {})
        offenders += [f"{report.parent.parent.name}: {n}" for n in inputs if "/renders/" in n]
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# A project-declared output root may carry any name (Issues #10106, #10163)
# ---------------------------------------------------------------------------


def _declared_options(project: Path, root: str, output_dir: Path | None = None) -> ReadinessOptions:
    board = project / "kicad"
    output_root = project / root
    return ReadinessOptions(
        board_dir=board,
        pcb=board / "b.kicad_pcb",
        schematic=None,
        project=None,
        manufacturer="jlcpcb",
        mode="verify",
        output_dir=output_dir or output_root / "manufacturing",
        evidence_dir=output_root / "readiness",
        project_root=project,
        output_root=output_root,
    )


@pytest.mark.parametrize("root", ["release", "out", "build/release", "output"])
def test_declared_root_renders_are_not_hashable_inputs(tmp_path: Path, root: str) -> None:
    project = tmp_path
    (project / "kicad").mkdir()
    (project / "kicad" / "b.kicad_pcb").write_text("(kicad_pcb)")
    (project / "kicad" / "renders_helper.kicad_sch").write_text("(kicad_sch)")
    release = project / root
    (release / "renders").mkdir(parents=True)
    (release / "manufacturing" / "renders").mkdir(parents=True)
    (release / "renders" / "3d-front.png").write_bytes(b"png")
    (release / "manufacturing" / "renders" / "pcb-front.svg").write_text("<svg/>")
    (release / "manufacturing" / "report.md").write_text("report")
    (project / "kicad" / "output" / "renders").mkdir(parents=True)
    (project / "kicad" / "output" / "renders" / "top.png").write_bytes(b"png")

    names = {
        p.relative_to(project.resolve()).as_posix()
        for p in _hashable_inputs(_declared_options(project, root))
    }

    assert f"{root}/manufacturing/report.md" in names
    assert "kicad/b.kicad_pcb" in names
    assert not [n for n in names if "/renders/" in n], names


def test_declared_root_extra_input_render_is_not_evidence(tmp_path: Path) -> None:
    (tmp_path / "kicad").mkdir()
    (tmp_path / "kicad" / "b.kicad_pcb").write_text("(kicad_pcb)")
    render = tmp_path / "build" / "release" / "renders" / "3d-front.png"
    render.parent.mkdir(parents=True)
    render.write_bytes(b"image")
    recipe = tmp_path / "generate_design.py"
    recipe.write_text("# board generator\n")

    report = build_report(
        _declared_options(tmp_path, "build/release"),
        [],
        {},
        EngineFingerprint(kicad_tools_version="test"),
        extra_inputs=[render, recipe],
    )

    assert "generate_design.py" in report["inputs"]
    assert "build/release/renders/3d-front.png" not in report["inputs"]


def test_nested_output_dir_renders_are_not_evidence(tmp_path: Path) -> None:
    (tmp_path / "kicad").mkdir()
    (tmp_path / "kicad" / "b.kicad_pcb").write_text("(kicad_pcb)")
    nested = tmp_path / "release" / "sub" / "manufacturing"
    (nested / "renders").mkdir(parents=True)
    (nested / "renders" / "top.png").write_bytes(b"png")
    (nested / "report.md").write_text("report")

    names = {
        p.relative_to(tmp_path.resolve()).as_posix()
        for p in _hashable_inputs(_declared_options(tmp_path, "release", output_dir=nested))
    }

    assert "release/sub/manufacturing/report.md" in names
    assert not [n for n in names if "/renders/" in n], names


def test_render_exclusion_does_not_swallow_lookalikes(tmp_path: Path) -> None:
    options = _declared_options(tmp_path, "release")
    for kept in (
        "kicad/renders_helper.kicad_sch",
        "release/manufacturing/report.md",
        "release/renders",  # the directory name itself, no file beneath it
        "release/rendersX/top.png",
        "other/renders/top.png",  # not under the declared root
    ):
        assert not _is_render_artifact(Path(kept), options), kept
    for dropped in (
        "release/renders/top.png",
        "release/manufacturing/renders/top.png",
        "release/renders/deep/er/top.png",
        "kicad/output/renders/top.png",
    ):
        assert _is_render_artifact(Path(dropped), options), dropped
    # Without a declaration only the board-local forms match (unchanged).
    assert not _is_render_artifact(Path("release/renders/top.png"))
    assert _is_render_artifact(Path("output/renders/top.png"))


def test_rerendering_under_declared_root_keeps_receipt_valid(tmp_path: Path) -> None:
    from kicad_tools.cli.board_readiness import read_readiness
    from tests.test_readiness_project_output_root_10106 import (
        layout_for,
        make_project,
        run,
    )

    project = make_project(tmp_path, declaration='[readiness]\noutput_root = "release"\n')
    renders = project / "release" / "renders"
    renders.mkdir(parents=True)
    (renders / "3d-front.png").write_bytes(b"first render")

    code, _ = run(project, "--generate")
    assert code == 0, project / "release" / "readiness-attempt" / "readiness.json"

    receipt = json.loads((project / "release" / "readiness.json").read_text())
    assert not [n for n in receipt["inputs"] if "/renders/" in n], receipt["inputs"]

    layout = layout_for(project)
    layout = type(layout)(report=project / "release" / "readiness.json", input_base=project)
    assert read_readiness(project / "kicad", layout=layout)["status"] == "ready"

    # An external re-render (what ``kct render`` / deploy scripts do).
    (renders / "3d-front.png").write_bytes(b"second render")
    (renders / "3d-back.png").write_bytes(b"new render")
    assert read_readiness(project / "kicad", layout=layout)["status"] == "ready"

    code, _ = run(project, "--verify")
    assert code == 0
    verified = json.loads(
        (project / "release" / "readiness-verification" / "readiness.json").read_text()
    )
    assert not [n for n in verified["inputs"] if "/renders/" in n], verified["inputs"]
