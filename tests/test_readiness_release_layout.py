"""Exercise declared output roots through the consumers of a generated assembly package."""

from __future__ import annotations

import json
import shutil

import pytest

from kicad_tools.cli import submit_jlcpcb_cmd
from kicad_tools.cli.board_readiness import ReadinessLayout, read_readiness
from kicad_tools.cli.readiness_cmd import EngineRun
from kicad_tools.cli.render_cmd import PROVENANCE_FILE
from kicad_tools.cli.render_cmd import main as render
from kicad_tools.export.submission_workflow import WorkflowError, sign_off_bundle
from tests.test_kct_readiness_cmd import FakeEngines
from tests.test_readiness_project_output_root_10106 import make_project, run
from tests.test_render_cmd import fake_kicad  # noqa: F401
from tests.test_submission_workflow import PCB
from tests.test_submit_jlcpcb_cli import _no_browser, _stock


class HandoffEngines(FakeEngines):
    """Keep the layer table and supply the matching BOM/CPL used by submission preflight."""

    def refill(self, pcb):
        # This fixture has no zones to refill.
        return EngineRun(ok=True)

    def export(self, pcb, mfr, output_dir, *args, **kwargs):
        result = super().export(pcb, mfr, output_dir, *args, **kwargs)
        (output_dir / f"bom_{mfr}.csv").write_text(
            "Comment,Designator,Footprint,LCSC Part #\n1k,R1,Package,C17630\n"
        )
        return result


@pytest.fixture
def project(tmp_path):
    original = make_project(tmp_path, '[readiness]\noutput_root = "fabrication"\n')
    (original / "kicad/board.kicad_pcb").write_text(PCB)
    engines = HandoffEngines(
        original / "kicad", part_numbers={"R1": "C17630"}, tht_refs=(), cpl_refs=("R1",)
    )
    assert (
        run(original, "--generate", "--known-issue", "7220:thermal:advisory", fake=engines)[0] == 0
    )
    assert run(original, "--verify", fake=engines)[0] == 0
    return original.rename(tmp_path / "relocated")


@pytest.mark.usefixtures("fake_kicad")
def test_selected_board_reads_its_receipt_and_renders_to_declared_root(project):
    pcb = project / "kicad/board.kicad_pcb"
    output = project / "fabrication"
    assert read_readiness(pcb)["status"] == "ready"
    assert read_readiness(pcb.parent)["status"] == "unverified"
    assert render([str(pcb), "--no-3d"]) == 0
    provenance = json.loads((output / "renders" / PROVENANCE_FILE).read_text())
    assert set(provenance["outputs"]) == {"pcb-front.svg", "pcb-back.svg"}
    assert not (pcb.parent / "renders").exists()
    assert render([str(pcb), "--no-3d"]) == 0
    assert read_readiness(pcb)["status"] == "ready"


@pytest.mark.parametrize("location", ["readiness.json", "readiness-verification/readiness.json"])
def test_release_signoff_and_dry_run_use_relocated_bundle(project, location, monkeypatch):
    pcb = project / "kicad/board.kicad_pcb"
    output = project / "fabrication"
    bundle = output / "manufacturing"
    sign_off_bundle(bundle, pcb, "local test", readiness=output / location)
    assert read_readiness(pcb)["status"] == "ready"
    _no_browser(monkeypatch)
    _stock(monkeypatch, {"C17630": 100})
    review = project.parent / "submission-preview"
    assert (
        submit_jlcpcb_cmd.main(
            [
                str(bundle),
                "--board",
                str(pcb),
                "--schematic",
                str(project / "schematics/board.kicad_sch"),
                "--quantity",
                "5",
                "--dry-run",
                "-o",
                str(review),
            ]
        )
        == 0
    )
    assert (review / "review-page/index.md").is_file()
    plan = json.loads((review / "handoff/plan.json").read_text())
    assert {role: row["path"] for role, row in plan["sources"].items()} == {
        "pcb": "kicad/board.kicad_pcb",
        "schematic": "schematics/board.kicad_sch",
    }


def test_selected_history_copy_is_not_the_checked_board(project):
    pcb = project / "kicad/board.kicad_pcb"
    history = project / "history/board.kicad_pcb"
    shutil.copyfile(pcb, history)
    output = project / "fabrication"
    bundle = output / "manufacturing"
    manifest = bundle / "manifest.json"
    before = manifest.read_bytes()
    assert read_readiness(history)["status"] == "unverified"
    with pytest.raises(WorkflowError, match="not produced for this --board"):
        sign_off_bundle(bundle, history, "local test", readiness=output / "readiness.json")
    assert manifest.read_bytes() == before


@pytest.mark.parametrize(
    "relative", ["kicad/board.kicad_pcb", "fabrication/manufacturing/gerbers/gerbers.zip"]
)
def test_changed_board_or_bundle_stops_readiness_and_signoff(project, relative):
    pcb = project / "kicad/board.kicad_pcb"
    output = project / "fabrication"
    bundle = output / "manufacturing"
    manifest = bundle / "manifest.json"
    before = manifest.read_bytes()
    changed = project / relative
    changed.write_bytes(changed.read_bytes() + b" changed")
    assert read_readiness(pcb)["status"] == "unverified"
    with pytest.raises(WorkflowError):
        sign_off_bundle(bundle, pcb, "local test", readiness=output / "readiness.json")
    assert manifest.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "changed"), [("project_root", "../.."), ("output_root", "retired")]
)
def test_receipt_cannot_change_the_declared_layout(project, field, changed):
    pcb = project / "kicad/board.kicad_pcb"
    output = project / "fabrication"
    receipt = output / "readiness.json"
    data = json.loads(receipt.read_text())
    data[field] = changed
    receipt.write_text(json.dumps(data))
    manifest = output / "manufacturing/manifest.json"
    before = manifest.read_bytes()
    assert read_readiness(pcb)["status"] == "unverified"
    with pytest.raises(WorkflowError, match="declared project and output root"):
        sign_off_bundle(manifest.parent, pcb, "local test", readiness=receipt)
    assert manifest.read_bytes() == before


def test_nested_board_accepts_a_receipt_created_on_windows(tmp_path):
    layout = ReadinessLayout(tmp_path / "fabrication/readiness.json", tmp_path)
    layout.validate_declaration(
        {"output_root": "fabrication", "project_root": r"..\.."}, tmp_path / "boards/controller"
    )


@pytest.mark.usefixtures("fake_kicad")
def test_render_and_receipt_refuse_an_output_root_outside_the_project(project):
    pcb = project / "kicad/board.kicad_pcb"
    output = project / "fabrication"
    moved = output.rename(project.parent / "retained")
    output.symlink_to(moved, target_is_directory=True)
    before = (moved / "manufacturing/manifest.json").read_bytes()
    assert read_readiness(pcb)["status"] == "unverified"
    with pytest.raises(WorkflowError, match="inside the project"):
        sign_off_bundle(
            output / "manufacturing", pcb, "local test", readiness=output / "readiness.json"
        )
    assert render([str(pcb), "--no-3d"]) == 1
    assert not (moved / "renders").exists()
    assert (moved / "manufacturing/manifest.json").read_bytes() == before
