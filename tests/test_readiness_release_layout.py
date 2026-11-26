"""Exercise declared output roots through the consumers of a generated assembly package."""

from __future__ import annotations

import json
import shutil

import pytest

from kicad_tools.cli import readiness_cmd, submit_jlcpcb_cmd
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
        from kicad_tools.export.manufacturing import _create_project_zip

        result = super().export(pcb, mfr, output_dir, *args, **kwargs)
        _create_project_zip(
            pcb,
            output_dir,
            schematic_path=kwargs.get("schematic"),
            project_root=kwargs.get("project_root"),
        )
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


def _submission_preview(bundle, pcb, schematic, destination):
    return submit_jlcpcb_cmd.main(
        [
            str(bundle),
            "--board",
            str(pcb),
            "--schematic",
            str(schematic),
            "--quantity",
            "5",
            "--dry-run",
            "-o",
            str(destination),
        ]
    )


@pytest.mark.parametrize("selection", ["explicit", "explicit-with-sibling", "spec", "paired"])
def test_electrical_check_uses_the_exported_schematic(tmp_path, selection):
    from kicad_tools.cli.check_cmd import _resolve_check_schematic_with_source, portable_report_path

    project = make_project(tmp_path)
    pcb = project / "kicad/board.kicad_pcb"
    selected = (project / "schematics/board.kicad_sch").rename(
        project / "schematics/rev_b.kicad_sch"
    )
    extra = []
    if selection in {"explicit-with-sibling", "spec"}:
        pcb.with_suffix(".kicad_sch").write_text("(kicad_sch other)")
    if selection == "spec":
        (project / "project.kct").write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            "  name: board\n"
            "  artifacts:\n"
            "    schematic: schematics/rev_b.kicad_sch\n"
            "    pcb: kicad/board.kicad_pcb\n"
        )
    elif selection == "paired":
        selected = selected.rename(pcb.with_suffix(".kicad_sch"))
        pcb = pcb.rename(pcb.with_name("board_v07.kicad_pcb"))
        pcb.with_name("auxiliary.kicad_sch").write_text("(kicad_sch other)")
    else:
        extra = ["--sch", str(selected)]

    class ElectricalEngines(HandoffEngines):
        def kct_check(self, pcb, mfr, report_path, extra):
            result = super().kct_check(pcb, mfr, report_path, extra)
            explicit = extra[extra.index("--schematic") + 1] if "--schematic" in extra else None
            self.checked, source = _resolve_check_schematic_with_source(explicit, pcb)
            assert self.checked == pcb.parent.parent / selected.relative_to(project)
            assert self.checked != selected  # Gate 1 must check the staged copy.
            report = json.loads(report_path.read_bytes())
            report["meta_checks"]["schematic"] = {
                "source": source,
                "path": portable_report_path(self.checked),
            }
            report_path.write_text(json.dumps(report))
            return result

    engines = ElectricalEngines(project / "kicad")
    args = [str(pcb), "--mfr", "jlcpcb", "--generate", *extra]
    assert readiness_cmd.main(args, engines=engines.bundle()) == 0
    assert engines.export_schematic == engines.checked
    receipt = json.loads((project / "output/readiness.json").read_bytes())
    assert receipt["schematic_root"] == selected.relative_to(project).as_posix()
    report = json.loads((project / "output/readiness/kct-check.json").read_bytes())
    assert report["meta_checks"]["schematic"] == {
        "source": "--schematic",
        "path": receipt["schematic_root"],
    }


def test_ambiguous_schematics_require_selection_before_generation(tmp_path, capsys):
    project = make_project(tmp_path)
    pcb = project / "kicad/board.kicad_pcb"
    for stem in ("rev_a", "rev_b"):
        pcb.with_name(f"{stem}.kicad_sch").write_text("(kicad_sch)")
    engines = HandoffEngines(pcb.parent)
    args = [str(pcb), "--mfr", "jlcpcb", "--generate"]
    assert readiness_cmd.main(args, engines=engines.bundle()) == 2
    assert "--sch" in capsys.readouterr().err
    assert not engines.calls
    assert not (project / "output").exists()


@pytest.mark.parametrize("location", ["readiness.json", "readiness-verification/readiness.json"])
def test_relocated_declared_release_requires_its_declaration(project, location, capsys):
    pcb = project / "kicad/board.kicad_pcb"
    output = project / "fabrication"
    bundle = output / "manufacturing"
    sign_off_bundle(bundle, pcb, "local test", readiness=output / location)
    relocated = project.rename(project.with_name("signed-release"))
    (relocated / ".kicad-tools.toml").unlink()
    pcb = relocated / pcb.relative_to(project)
    substitute = pcb.with_suffix(".kicad_sch")
    substitute.write_text("(kicad_sch other)")
    preview = relocated / "preview"
    assert (
        _submission_preview(relocated / bundle.relative_to(project), pcb, substitute, preview) == 2
    )
    assert "requires its project output declaration" in capsys.readouterr().err
    assert not preview.exists()


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
    assert _submission_preview(bundle, pcb, project / "schematics/board.kicad_sch", review) == 0
    assert (review / "review-page/index.md").is_file()
    plan = json.loads((review / "handoff/plan.json").read_text())
    assert {role: row["path"] for role, row in plan["sources"].items()} == {
        "pcb": "kicad/board.kicad_pcb",
        "schematic": "schematics/board.kicad_sch",
    }


@pytest.mark.parametrize("location", ["readiness.json", "readiness-verification/readiness.json"])
def test_submission_requires_the_signed_schematic_path_and_digest(project, location, monkeypatch):
    pcb = project / "kicad/board.kicad_pcb"
    schematic = project / "schematics/board.kicad_sch"
    output = project / "fabrication"
    bundle = output / "manufacturing"
    sign_off_bundle(bundle, pcb, "local test", readiness=output / location)
    artifacts = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
    _no_browser(monkeypatch)
    _stock(monkeypatch, {"C17630": 100})

    historical = project / "history/board.kicad_sch"
    shutil.copyfile(schematic, historical)
    unrelated = project / "history/other.kicad_sch"
    unrelated.write_text("(kicad_sch (version 20250114))\n")
    destination = project.parent / "refused-preview"
    for selected in (
        historical,
        unrelated,
        project / "kicad/board.kicad_pro",
        project / "history/missing.kicad_sch",
    ):
        assert _submission_preview(bundle, pcb, selected, destination) == 2
        assert not destination.exists()

    schematic.write_bytes(schematic.read_bytes() + b"\n")
    assert _submission_preview(bundle, pcb, schematic, destination) == 2
    assert not destination.exists()
    assert {path: path.read_bytes() for path in output.rglob("*") if path.is_file()} == artifacts


def test_submission_uses_only_the_receipt_named_by_the_release_digest(project, monkeypatch, capsys):
    output = project / "fabrication"
    pcb = project / "kicad/board.kicad_pcb"
    bundle = output / "manufacturing"
    signed = output / "readiness-verification/readiness.json"
    sign_off_bundle(bundle, pcb, "local test", readiness=signed)
    _stock(monkeypatch, {"C17630": 100})
    signed.write_bytes(signed.read_bytes() + b"\n")

    destination = project.parent / "refused-preview"
    assert (
        _submission_preview(bundle, pcb, project / "schematics/board.kicad_sch", destination) == 2
    )
    assert "used at release sign-off is missing or changed" in capsys.readouterr().err
    assert not destination.exists()


@pytest.fixture(params=["enclosing", "narrower"])
def nested_roots(tmp_path, request):
    checkout = tmp_path / "checkout"
    project = make_project(checkout, '[readiness]\noutput_root = "fabrication"\n')
    if request.param == "enclosing":
        return project, project, checkout
    (project / ".kicad-tools.toml").unlink()
    (checkout / ".kicad-tools.toml").write_text(
        '[readiness]\noutput_root = "project/fabrication"\n'
    )
    return project, checkout, project


def test_explicit_root_keeps_the_declared_receipt_base(tmp_path, nested_roots):
    project, declared_root, requested_root = nested_roots
    pcb = project / "kicad/board.kicad_pcb"
    pcb.write_text(PCB)
    engines = HandoffEngines(
        pcb.parent, part_numbers={"R1": "C17630"}, tht_refs=(), cpl_refs=("R1",)
    )
    flags = ["--project-root", str(requested_root)]
    assert run(project, "--generate", *flags, fake=engines)[0] == 0
    assert run(project, "--verify", fake=engines)[0] == 0
    assert run(project, "--verify", *flags, fake=engines)[0] == 0

    output = project / "fabrication"
    board_key = pcb.relative_to(declared_root).as_posix()
    output_key = output.relative_to(declared_root).as_posix()
    relocated = declared_root.rename(tmp_path / "relocated")
    pcb = relocated / board_key
    output = relocated / output_key
    assert read_readiness(pcb)["status"] == "ready"
    for location in ("readiness.json", "readiness-verification/readiness.json"):
        receipt = output / location
        record = json.loads(receipt.read_text())
        assert record["output_root"] == output_key
        assert board_key in record["inputs"]
        sign_off_bundle(output / "manufacturing", pcb, "local test", readiness=receipt)
        assert read_readiness(pcb)["status"] == "ready"


def test_explicit_root_and_declaration_both_bound_schematic(nested_roots):
    project, _, requested_root = nested_roots
    schematic = project.parent / "other-board.kicad_sch"
    schematic.write_text("(kicad_sch other)")
    for operation in ("--generate", "--verify"):
        code, engines = run(
            project, operation, "--project-root", str(requested_root), "--sch", str(schematic)
        )
        assert code == 2
        assert engines.calls == []
        assert not (project / "fabrication").exists()


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


@pytest.fixture
def hierarchy(project):
    root = project / "schematics/board.kicad_sch"
    child = root.with_name("notes.kicad_sch")
    child.write_text("(kicad_sch)\n")
    root.write_text('(kicad_sch (sheet (property "Sheetfile" "notes.kicad_sch")))\n')
    engines = HandoffEngines(
        project / "kicad", part_numbers={"R1": "C17630"}, tht_refs=(), cpl_refs=("R1",)
    )
    assert run(project, "--generate", fake=engines)[0] == 0
    assert run(project, "--verify", fake=engines)[0] == 0
    return project, root, child


def test_generic_project_archive_carries_the_selected_sibling_root(hierarchy, tmp_path):
    from zipfile import ZipFile

    from kicad_tools.export.manufacturing import _create_project_zip

    project, root, child = hierarchy
    board = project / "kicad/board.kicad_pcb"
    archive = _create_project_zip(board, tmp_path, schematic_path=root, project_root=project)
    with ZipFile(archive) as zf:
        assert zf.read("kicad/board.kicad_sch") == root.read_bytes()
        assert zf.read("kicad/notes.kicad_sch") == child.read_bytes()
        assert zf.read("kicad/board.kicad_pcb") == board.read_bytes()


@pytest.mark.parametrize("location", ["readiness.json", "readiness-verification/readiness.json"])
@pytest.mark.parametrize("legacy", [False, True], ids=["recorded-root", "legacy"])
def test_release_uses_one_complete_schematic_hierarchy(hierarchy, location, legacy, monkeypatch):
    project, root, child = hierarchy
    board = project / "kicad/board.kicad_pcb"
    bundle = project / "fabrication/manufacturing"
    receipt = project / "fabrication" / location
    data = json.loads(receipt.read_bytes())
    if legacy:
        data.pop("schematic_root", None)
        receipt.write_text(json.dumps(data))
    else:
        assert data["schematic_root"] == root.relative_to(project).as_posix()

    _no_browser(monkeypatch)
    _stock(monkeypatch, {"C17630": 100})
    sign_off_bundle(bundle, board, "local test", readiness=receipt)
    assert _submission_preview(bundle, board, root, project.parent / "complete-preview") == 0
    refused = project.parent / "refused-preview"
    assert _submission_preview(bundle, board, child, refused) == 2
    child.unlink()
    assert _submission_preview(bundle, board, root, refused) == 2
    assert not refused.exists()
    assert read_readiness(board)["status"] == "unverified"
    with pytest.raises(WorkflowError):
        sign_off_bundle(bundle, board, "local test", readiness=receipt)


def test_legacy_receipt_with_only_the_parent_requires_fresh_verification(hierarchy):
    project, root, child = hierarchy
    board = project / "kicad/board.kicad_pcb"
    output = project / "fabrication"
    receipt = output / "readiness.json"
    data = json.loads(receipt.read_bytes())
    data.pop("schematic_root", None)
    del data["inputs"][child.relative_to(project).as_posix()]
    receipt.write_text(json.dumps(data))
    for missing in (False, True):
        if missing:
            child.unlink()
        assert read_readiness(board)["status"] == "unverified"
        with pytest.raises(WorkflowError):
            sign_off_bundle(output / "manufacturing", board, "local test", readiness=receipt)


@pytest.mark.parametrize("legacy", [False, True])
def test_snapshots_are_evidence_only(tmp_path, monkeypatch, legacy):
    project = make_project(tmp_path, '[readiness]\noutput_root = "fabrication"\n')
    nested = project / "boards/controller"
    nested.mkdir(parents=True)
    pcb = (project / "kicad").rename(nested / "kicad") / "board.kicad_pcb"
    pcb.write_text(PCB)
    sch = project / "schematics/board.kicad_sch"
    engines = HandoffEngines(
        pcb.parent, part_numbers={"R1": "C17630"}, tht_refs=(), cpl_refs=("R1",)
    )
    for mode in ("--generate", "--verify"):
        args = [str(pcb), "--mfr", "jlcpcb", "--sch", str(sch), mode]
        assert readiness_cmd.main(args, engines=engines.bundle()) == 0
    output = project / "fabrication"
    bundle = output / "manufacturing"
    _no_browser(monkeypatch)
    _stock(monkeypatch, {"C17630": 100})
    for receipt in (output / "readiness.json", output / "readiness-verification/readiness.json"):
        data = json.loads(receipt.read_bytes())
        assert data["source_pcb"] == pcb.relative_to(project).as_posix()
        if legacy:
            del data["source_pcb"]
            receipt.write_text(json.dumps(data))
        sign_off_bundle(bundle, pcb, "local test", readiness=receipt)
        assert read_readiness(pcb)["status"] == "ready"
        manifest = bundle / "manifest.json"
        before = manifest.read_bytes()
        snapshots = [
            project / key
            for key in data["inputs"]
            if key.endswith(".kicad_pcb") and project / key != pcb
        ]
        assert len(snapshots) == 2
        for snapshot in snapshots:
            assert snapshot.read_bytes() == pcb.read_bytes()
            assert read_readiness(snapshot)["status"] == "unverified"
            with pytest.raises(WorkflowError, match="not produced for this --board"):
                sign_off_bundle(bundle, snapshot, "local test", readiness=receipt)
            assert _submission_preview(bundle, snapshot, sch, tmp_path / "preview") == 2
            assert not (tmp_path / "preview").exists()
            assert manifest.read_bytes() == before


def test_parent_schematic_declaration_is_freshness_evidence(tmp_path):
    project = make_project(tmp_path)
    pcb = project / "kicad/board.kicad_pcb"
    pcb.write_text(PCB)
    spec = project / "project.kct"
    spec.write_text(
        'kct_version: "1.0"\nproject:\n  name: board\n  artifacts:\n'
        "    schematic: schematics/board.kicad_sch\n    pcb: kicad/board.kicad_pcb\n"
    )
    (project / "schematics/rev_b.kicad_sch").write_text("(kicad_sch rev_b)")
    engines = HandoffEngines(
        pcb.parent, part_numbers={"R1": "C17630"}, tht_refs=(), cpl_refs=("R1",)
    )
    for mode in ("--generate", "--verify"):
        assert (
            readiness_cmd.main([str(pcb), "--mfr", "jlcpcb", mode], engines=engines.bundle()) == 0
        )
    output = project / "output"
    assert read_readiness(pcb)["status"] == "ready"
    spec.write_text(spec.read_text().replace("schematics/board", "schematics/rev_b"))
    result = read_readiness(pcb)
    assert result["status"] == "unverified"
    assert "project.kct" in str(result["blockers"])
    manifest = output / "manufacturing/manifest.json"
    before = manifest.read_bytes()
    for receipt in (output / "readiness.json", output / "readiness-verification/readiness.json"):
        assert "project.kct" in json.loads(receipt.read_bytes())["inputs"]
        with pytest.raises(WorkflowError, match="project.kct"):
            sign_off_bundle(manifest.parent, pcb, "local test", readiness=receipt)
        assert manifest.read_bytes() == before


def test_ambiguous_legacy_source_requires_fresh_verification(project):
    output = project / "fabrication"
    receipt = output / "readiness.json"
    data = json.loads(receipt.read_bytes())
    board = project / data.pop("source_pcb")
    history = project / "history/board.kicad_pcb"
    shutil.copyfile(board, history)
    data["inputs"]["history/board.kicad_pcb"] = data["inputs"]["kicad/board.kicad_pcb"]
    receipt.write_text(json.dumps(data))
    for selected in (board, history):
        assert read_readiness(selected)["status"] == "unverified"
        with pytest.raises(WorkflowError, match="not produced for this --board"):
            sign_off_bundle(output / "manufacturing", selected, "local test", readiness=receipt)


def test_implicit_declaration_must_fit_explicit_source_boundary(tmp_path, capsys):
    project = make_project(tmp_path, '[readiness]\noutput_root = "kicad/fabrication"\n')
    pcb = project / "kicad/board.kicad_pcb"
    sch = pcb.with_suffix(".kicad_sch")
    sch.write_text("(kicad_sch)")
    (project / "project.kct").write_text(
        'kct_version: "1.0"\nproject:\n  name: board\n  artifacts:\n'
        "    schematic: kicad/board.kicad_sch\n    pcb: kicad/board.kicad_pcb\n"
    )
    args = [str(pcb), "--mfr", "jlcpcb", "--generate", "--project-root", str(pcb.parent)]
    engines = HandoffEngines(pcb.parent)
    assert readiness_cmd.main(args, engines=engines.bundle()) == 2
    assert "schematic declaration" in capsys.readouterr().err
    assert not (pcb.parent / "fabrication").exists()
    assert readiness_cmd.main([*args, "--sch", str(sch)], engines=engines.bundle()) == 0
