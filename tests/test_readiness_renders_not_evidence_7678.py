"""Renders are presentation artifacts, not readiness evidence (Issue #7678).

``scripts/deploy-site.sh`` re-renders ``output/renders`` and
``output/manufacturing/renders``; fingerprinting them made every deploy
invalidate the committed readiness record.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli.readiness_cmd import (
    EngineFingerprint,
    ReadinessOptions,
    _hashable_inputs,
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
