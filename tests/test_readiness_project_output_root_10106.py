"""A project-declared output root resolves readiness destinations (Issue #10106).

A hand-edited board may live at ``kicad/board.kicad_pcb`` while its generated
release belongs in a sibling ``output/``.  A project-local ``[readiness]
output_root`` TOML declaration authorizes that sibling: the bundle, outer ZIP,
generated receipt, evidence and diagnostics publish under it, the receipt keys
its inputs against the declaring project, and every unsafe path refuses before
anything is read or written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from kicad_tools.cli import readiness_cmd
from kicad_tools.cli.board_readiness import read_readiness, readiness_input_hash
from kicad_tools.cli.readiness_cmd import ReadinessLayout
from tests.test_kct_readiness_cmd import FakeEngines

DECLARATION = '[readiness]\noutput_root = "output"\n'


def make_project(tmp_path: Path, declaration: str = DECLARATION) -> Path:
    """``kicad/`` editable design + sibling ``schematics/`` dependency, no output."""
    project = tmp_path / "project"
    (project / "kicad").mkdir(parents=True)
    (project / "schematics").mkdir()
    (project / "history").mkdir()
    (project / "kicad" / "board.kicad_pcb").write_text("(kicad_pcb original)\n")
    (project / "kicad" / "board.kicad_pro").write_text('{"board": {}}\n')
    (project / "schematics" / "board.kicad_sch").write_text("(kicad_sch top)\n")
    (project / "history" / "board.kicad_pcb").write_text("(kicad_pcb previous revision)\n")
    (project / ".kicad-tools.toml").write_text(declaration)
    return project


def run(project: Path, *extra: str, fake: FakeEngines | None = None) -> tuple[int, FakeEngines]:
    pcb = project / "kicad" / "board.kicad_pcb"
    fake = fake or FakeEngines(project / "kicad")
    args = [
        str(pcb),
        "--mfr",
        "jlcpcb",
        "--sch",
        str(project / "schematics" / "board.kicad_sch"),
        *extra,
    ]
    return readiness_cmd.main(args, engines=fake.bundle()), fake


def layout_for(project: Path) -> ReadinessLayout:
    return ReadinessLayout(
        report=project / "output" / "readiness.json",
        input_base=project,
    )


def digest_for(project: Path, name: str) -> str:
    return readiness_input_hash(name, (project / name).read_bytes())


# ---------------------------------------------------------------------------
# Generate + verify through the declared output root
# ---------------------------------------------------------------------------


def test_declared_root_generates_then_verifies_at_documented_destinations(tmp_path):
    project = make_project(tmp_path)

    code, _ = run(project, "--generate")
    assert code == 0, project / "output" / "readiness-attempt" / "readiness.json"

    for destination in (
        project / "output" / "manufacturing",
        project / "output" / "manufacturing.zip",
        project / "output" / "readiness.json",
        project / "output" / "readiness",
    ):
        assert destination.exists(), destination
    # The receipt validates through the consumer side's trusted layout.
    assert read_readiness(project / "kicad", layout=layout_for(project))["status"] == "ready"

    code, _ = run(project, "--verify")
    assert code == 0
    verification = project / "output" / "readiness-verification" / "readiness.json"
    assert verification.is_file()
    # Both receipt forms identify the same live board, dependencies and
    # generated artifacts on the same portable project-relative base (verify
    # re-measures everything itself, so it hashes its own evidence directory
    # instead of the generated one).
    generated = json.loads((project / "output" / "readiness.json").read_text())
    verified = json.loads(verification.read_text())
    for name in ("kicad/board.kicad_pcb", "kicad/board.kicad_pro", "schematics/board.kicad_sch"):
        assert verified["inputs"][name] == generated["inputs"][name] == digest_for(project, name)
    for name in verified["inputs"]:
        if name.startswith("output/manufacturing"):
            assert verified["inputs"][name] == generated["inputs"][name], name
    # The generated receipt is untouched by verification.
    assert read_readiness(project / "kicad", layout=layout_for(project))["status"] == "ready"


def test_declared_receipt_uses_portable_project_relative_paths(tmp_path):
    project = make_project(tmp_path)
    assert run(project, "--generate")[0] == 0

    report = json.loads((project / "output" / "readiness.json").read_text())
    assert report["project_root"] == ".."
    assert report["output_root"] == "output"
    assert "external_inputs" not in report
    inputs = report["inputs"]
    # The selected live board, its project and the sibling dependency.
    for name in (
        "kicad/board.kicad_pcb",
        "kicad/board.kicad_pro",
        "schematics/board.kicad_sch",
        # Generated artifacts under the declared root.
        "output/manufacturing/manifest.json",
        "output/manufacturing/kicad_project.zip",
        "output/manufacturing/README.txt",
        "output/readiness/kct-check.json",
        "output/readiness/native-drc.json",
        "output/manufacturing.zip",
    ):
        assert name in inputs, name
        assert inputs[name] == digest_for(project, name), name
    # The report never hashes itself, and a prior revision is not an input.
    assert "output/readiness.json" not in inputs
    assert not [name for name in inputs if name.startswith("history/")]


def test_explicit_matching_output_and_project_root_work(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.chdir(project)

    code, fake = run(
        project,
        "--project-root",
        str(project),
        "--output",
        "output/manufacturing",
        "--generate",
    )

    assert code == 0
    assert fake.calls  # gates ran
    assert (project / "output" / "manufacturing" / "manifest.json").is_file()


def test_moved_project_still_verifies_and_design_and_bundle_survive(tmp_path):
    project = make_project(tmp_path)
    assert run(project, "--generate")[0] == 0
    pcb = project / "kicad" / "board.kicad_pcb"
    bundle = sorted(
        p.read_bytes() for p in (project / "output" / "manufacturing").rglob("*") if p.is_file()
    )
    design = pcb.read_bytes()

    moved = tmp_path / "elsewhere" / "project"
    moved.parent.mkdir()
    shutil.move(str(project), str(moved))

    code, _ = run(moved, "--verify")

    assert code == 0
    assert (moved / "kicad" / "board.kicad_pcb").read_bytes() == design
    assert (
        sorted(
            p.read_bytes() for p in (moved / "output" / "manufacturing").rglob("*") if p.is_file()
        )
        == bundle
    )
    report = json.loads(
        (moved / "output" / "readiness-verification" / "readiness.json").read_text()
    )
    assert report["status"] == "ready"


# ---------------------------------------------------------------------------
# Failure and concurrent-change preservation
# ---------------------------------------------------------------------------


def test_failed_generate_preserves_prior_release_and_writes_attempt_diagnostics(tmp_path):
    project = make_project(tmp_path)
    assert run(project, "--generate")[0] == 0
    receipt = (project / "output" / "readiness.json").read_bytes()
    bundle = {
        p.relative_to(project): p.read_bytes()
        for p in (project / "output" / "manufacturing").rglob("*")
        if p.is_file()
    }

    code, _ = run(project, "--generate", fake=FakeEngines(project / "kicad", native_errors=1))

    assert code == 1
    assert (project / "output" / "readiness.json").read_bytes() == receipt
    assert {
        p.relative_to(project): p.read_bytes()
        for p in (project / "output" / "manufacturing").rglob("*")
        if p.is_file()
    } == bundle
    attempt = json.loads((project / "output" / "readiness-attempt" / "readiness.json").read_text())
    assert attempt["status"] == "blocked"
    assert attempt["candidate_not_published"] is True


def test_concurrent_source_edit_refuses_and_preserves_release(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    assert run(project, "--generate")[0] == 0
    receipt = (project / "output" / "readiness.json").read_bytes()
    schematic = project / "schematics" / "board.kicad_sch"
    refill = FakeEngines.refill

    def edit_during_refill(self, candidate_pcb):
        if "refill" not in self.calls:
            schematic.write_text("(kicad_sch edited)\n")
        return refill(self, candidate_pcb)

    monkeypatch.setattr(FakeEngines, "refill", edit_during_refill)
    code, _ = run(project, "--generate")

    assert code == 1
    attempt = json.loads((project / "output" / "readiness-attempt" / "readiness.json").read_text())
    [blocker] = attempt["blockers"]
    assert "Release changed while checks were running" in blocker
    assert "modified external input ../schematics/board.kicad_sch" in blocker
    assert (project / "output" / "readiness.json").read_bytes() == receipt
    assert schematic.read_text() == "(kicad_sch edited)\n"


def test_concurrent_artifact_edit_refuses_and_preserves_release(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    assert run(project, "--generate")[0] == 0
    readme = project / "output" / "manufacturing" / "README.txt"
    refill = FakeEngines.refill

    def edit_during_refill(self, candidate_pcb):
        if "refill" not in self.calls:
            readme.write_text("concurrent edit\n")
        return refill(self, candidate_pcb)

    monkeypatch.setattr(FakeEngines, "refill", edit_during_refill)
    code, _ = run(project, "--generate")

    assert code == 1
    attempt = json.loads((project / "output" / "readiness-attempt" / "readiness.json").read_text())
    [blocker] = attempt["blockers"]
    assert "Release changed while checks were running" in blocker
    assert "modified output/manufacturing/README.txt" in blocker
    assert readme.read_bytes() == b"concurrent edit\n"


# ---------------------------------------------------------------------------
# Declared root inside the board directory
# ---------------------------------------------------------------------------


def test_declared_root_inside_board_dir_publishes_under_it(tmp_path):
    """A declaration may name a board-local root; the copy carries the release."""
    project = make_project(tmp_path, declaration='[readiness]\noutput_root = "kicad/output"\n')

    code, _ = run(project, "--generate")

    assert code == 0, project / "kicad" / "output" / "readiness-attempt" / "readiness.json"
    board = project / "kicad"
    assert (board / "output" / "manufacturing" / "manifest.json").is_file()
    assert (board / "output" / "manufacturing.zip").is_file()
    assert (board / "output" / "readiness.json").is_file()
    assert (board / "output" / "readiness").is_dir()
    assert not (project / "output").exists()
    report = json.loads((board / "output" / "readiness.json").read_text())
    assert report["output_root"] == "kicad/output"
    assert report["project_root"] == ".."


# ---------------------------------------------------------------------------
# Refusals: unsafe declarations and outputs reject before reading or writing
# ---------------------------------------------------------------------------


def test_undeclared_sibling_output_still_refused(tmp_path, capsys):
    project = make_project(tmp_path, declaration="")
    (project / ".kicad-tools.toml").unlink()
    fake = FakeEngines(project / "kicad")

    code, _ = run(
        project,
        "--project-root",
        str(project),
        "--output",
        "output/manufacturing",
        "--generate",
        fake=fake,
    )

    assert code == 2
    assert "--output must be inside the board directory" in capsys.readouterr().err
    assert not (project / "output").exists()
    assert fake.calls == []


def test_declared_root_outside_project_is_refused(tmp_path, capsys):
    project = make_project(tmp_path, declaration='[readiness]\noutput_root = "../escape"\n')
    fake = FakeEngines(project / "kicad")

    code, _ = run(project, "--generate", fake=fake)

    assert code == 2
    assert "must be a relative path inside the project" in capsys.readouterr().err
    assert not (tmp_path / "escape").exists()
    assert fake.calls == []


def test_declared_root_symlink_escape_is_refused(tmp_path, capsys):
    project = make_project(tmp_path, declaration='[readiness]\noutput_root = "out"\n')
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "out").symlink_to(outside, target_is_directory=True)
    fake = FakeEngines(project / "kicad")

    code, _ = run(project, "--generate", fake=fake)

    assert code == 2
    assert "must resolve to a directory inside the project" in capsys.readouterr().err
    assert list(outside.iterdir()) == []
    assert fake.calls == []


def test_output_outside_declared_root_is_refused(tmp_path, capsys):
    project = make_project(tmp_path)
    elsewhere = project / "kicad" / "manufacturing"
    fake = FakeEngines(project / "kicad")

    code, _ = run(project, "--output", str(elsewhere), "--generate", fake=fake)

    assert code == 2
    assert "--output must be inside the declared output root" in capsys.readouterr().err
    assert not elsewhere.exists()
    assert not (project / "output").exists()
    assert fake.calls == []


def test_output_symlink_escaping_declared_root_is_refused(tmp_path, capsys):
    project = make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "output").mkdir()
    (project / "output" / "escape").symlink_to(outside, target_is_directory=True)
    fake = FakeEngines(project / "kicad")

    code, _ = run(
        project,
        "--output",
        str(project / "output" / "escape" / "manufacturing"),
        "--generate",
        fake=fake,
    )

    assert code == 2
    assert "--output must be inside the declared output root" in capsys.readouterr().err
    assert list(outside.iterdir()) == []
    assert fake.calls == []


def test_declared_root_equal_to_board_dir_is_refused(tmp_path, capsys):
    project = make_project(tmp_path, declaration='[readiness]\noutput_root = "kicad"\n')
    fake = FakeEngines(project / "kicad")

    code, _ = run(project, "--generate", fake=fake)

    assert code == 2
    assert "must not be the board directory itself" in capsys.readouterr().err
    assert fake.calls == []


def test_explicit_project_root_not_containing_declared_root_is_refused(tmp_path, capsys):
    project = make_project(tmp_path)
    inner = project / "kicad"
    fake = FakeEngines(project / "kicad")

    code, _ = run(project, "--project-root", str(inner), "--generate", fake=fake)

    assert code == 2
    assert "must contain the declared output root" in capsys.readouterr().err
    assert fake.calls == []


def test_user_wide_config_does_not_authorize_a_sibling(tmp_path, monkeypatch):
    """Only the project-local TOML declares an output root (Issue #10106)."""
    from kicad_tools import config

    project = make_project(tmp_path, declaration="")
    (project / ".kicad-tools.toml").unlink()
    # No sibling dependency: keep the sources board-local for this check.
    (project / "kicad" / "board.kicad_sch").write_text("(kicad_sch top)\n")
    shutil.rmtree(project / "schematics")
    user_config = tmp_path / "user-config.toml"
    user_config.write_text(DECLARATION)
    monkeypatch.setattr(config, "USER_CONFIG_PATH", user_config)
    fake = FakeEngines(project / "kicad")
    args = [str(project / "kicad" / "board.kicad_pcb"), "--mfr", "jlcpcb", "--generate"]

    code = readiness_cmd.main(args, engines=fake.bundle())

    # No project declaration: the historical board-local destinations apply.
    assert code == 0
    assert (project / "kicad" / "manufacturing").is_dir()
    assert (project / "kicad" / "output" / "readiness.json").is_file()
    assert not (project / "output").exists()
