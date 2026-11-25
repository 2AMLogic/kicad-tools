"""A project-declared output root resolves readiness destinations (Issue #10106).

A hand-edited board may live at ``kicad/board.kicad_pcb`` while its generated
release belongs in a sibling ``output/``.  A project-local ``[readiness]
output_root`` TOML declaration authorizes that sibling: the bundle, outer ZIP,
generated receipt, evidence and diagnostics publish under it, the receipt keys
its inputs against the declaring project, and every unsafe path refuses before
anything is read or written.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


def _live_files(project):
    return {
        p.relative_to(project): p.read_bytes()
        for p in project.rglob("*")
        if p.is_file() and "readiness-attempt" not in p.relative_to(project).parts
    }


def _changed_candidate(project):
    fake = FakeEngines(project / "kicad")
    refill = fake.refill

    def revised_refill(pcb):
        result = refill(pcb)
        pcb.write_text("(kicad_pcb refilled candidate revision)\n")
        return result

    fake.refill = revised_refill
    return fake


@pytest.mark.parametrize("prior", [False, True], ids=["first", "prior"])
@pytest.mark.parametrize("lock_region", ["kicad", "output"])
def test_both_publication_locks_precede_any_install(tmp_path, prior, lock_region):
    project = make_project(tmp_path)
    if prior:
        assert run(project, "--generate", "--checked-at", "2026-11-23T00:00:00Z")[0] == 0
    before = _live_files(project)
    with readiness_cmd._publication_lock(project / f".{lock_region}.readiness-publish.lock"):
        assert run(project, "--generate", "--checked-at", "2026-11-24T00:00:00Z")[0] == 1
    assert _live_files(project) == before
    attempt = json.loads((project / "output/readiness-attempt/readiness.json").read_bytes())
    assert attempt["candidate_not_published"] is True
    if prior:
        assert read_readiness(project / "kicad", layout=layout_for(project))["status"] == "ready"
    else:
        assert sorted(p.name for p in (project / "output").iterdir()) == ["readiness-attempt"]


@pytest.mark.parametrize("prior", [False, True], ids=["first", "prior"])
@pytest.mark.parametrize(
    "fault", ["board-write", "receipt-write", "editor", "published-artifact", "external-input"]
)
def test_group_publication_failure_preserves_release_and_editor(
    tmp_path, monkeypatch, prior, fault
):
    import os

    project = make_project(tmp_path)
    if prior:
        assert run(project, "--generate", "--checked-at", "2026-11-23T00:00:00Z")[0] == 0
    before = _live_files(project)
    board = project / "kicad" / "board.kicad_pcb"
    receipt = project / "output" / "readiness.json"
    edited = {
        "editor": board,
        "published-artifact": project / "output/manufacturing/README.txt",
        "external-input": project / "schematics/board.kicad_sch",
    }.get(fault)
    fake = _changed_candidate(project)
    real_replace = os.replace
    triggered = False

    real_link = os.link

    def interrupt_install(src, dst, _real=None):
        nonlocal triggered
        (_real or real_replace)(src, dst)
        target = board if fault == "board-write" else receipt
        if triggered or Path(dst) != target:
            return
        triggered = True
        if edited is not None:
            editor_tmp = edited.with_name("editor-save")
            editor_tmp.write_bytes(b"independent editor revision\n")
            real_replace(editor_tmp, edited)
        else:
            # The file has genuinely been installed before the error is reported.
            raise OSError("late installation failure")

    monkeypatch.setattr(readiness_cmd.os, "replace", interrupt_install)
    monkeypatch.setattr(
        readiness_cmd.os, "link", lambda src, dst: interrupt_install(src, dst, real_link)
    )
    code, _ = run(project, "--generate", "--checked-at", "2026-11-24T00:00:00Z", fake=fake)
    assert triggered
    assert code == 1
    if edited is not None:
        before[edited.relative_to(project)] = b"independent editor revision\n"
    assert _live_files(project) == before
    attempt = json.loads((project / "output/readiness-attempt/readiness.json").read_bytes())
    assert attempt["candidate_not_published"] is True
    if prior and edited is None:
        assert read_readiness(board.parent, layout=layout_for(project))["status"] == "ready"
    if not prior:
        expected_directories = {"readiness-attempt"}
        if fault == "published-artifact":
            expected_directories.add("manufacturing")
        assert {p.name for p in (project / "output").iterdir()} == expected_directories


@pytest.mark.parametrize("prior", [False, True], ids=["first", "prior"])
@pytest.mark.parametrize("target_name", ["board", "readme"])
@pytest.mark.parametrize("moment", ["before-claim", "in-gap"])
def test_same_target_editor_save_survives_publication(
    tmp_path, monkeypatch, prior, target_name, moment
):
    """An editor save that lands at the exact file being replaced is never overwritten."""
    import os

    project = make_project(tmp_path)
    if prior:
        assert run(project, "--generate", "--checked-at", "2026-11-23T00:00:00Z")[0] == 0
    target = (
        project / "kicad" / "board.kicad_pcb"
        if target_name == "board"
        else project / "output/manufacturing/README.txt"
    )
    if target_name == "readme" and not prior:
        pytest.skip("no released README exists before the first publication")
    before = _live_files(project)
    fake = _changed_candidate(project)
    real_replace, real_link = os.replace, os.link
    saved = False

    def editor_save():
        nonlocal saved
        saved = True
        tmp = target.with_name("editor-save")
        tmp.write_bytes(b"independent editor revision\n")
        real_replace(tmp, target)

    def claim_hook(src, dst):
        # The claim moves the live file to a hidden hold name.
        if not saved and Path(src) == target and ".readiness-hold-" in Path(dst).name:
            if moment == "before-claim":
                editor_save()
        real_replace(src, dst)

    def link_hook(src, dst):
        if not saved and Path(dst) == target and moment == "in-gap":
            editor_save()
        real_link(src, dst)

    monkeypatch.setattr(readiness_cmd.os, "replace", claim_hook)
    monkeypatch.setattr(readiness_cmd.os, "link", link_hook)
    code, _ = run(project, "--generate", "--checked-at", "2026-11-24T00:00:00Z", fake=fake)
    assert saved
    assert code == 1
    before[target.relative_to(project)] = b"independent editor revision\n"
    assert _live_files(project) == before
    assert not list(project.rglob(".readiness-hold-*"))


@pytest.fixture
def linkless_volume(monkeypatch):
    def unsupported(source, destination):
        raise OSError(errno.ENOTSUP, "Hard links are unavailable", destination)

    monkeypatch.setattr(os, "link", unsupported)


def test_linkless_volume_generates_and_verifies_first_and_revised_package(
    tmp_path, linkless_volume
):
    project = make_project(tmp_path)
    pcb = project / "kicad/board.kicad_pcb"
    archive = project / "output/manufacturing.zip"
    for fake in (FakeEngines(pcb.parent), _changed_candidate(project)):
        old_board = pcb.read_bytes()
        old_zip = archive.read_bytes() if archive.exists() else None
        assert run(project, "--generate", fake=fake)[0] == 0
        assert pcb.read_bytes() != old_board
        assert archive.read_bytes() != old_zip
        assert read_readiness(pcb.parent, layout=layout_for(project))["status"] == "ready"
        assert run(project, "--verify")[0] == 0
        assert not list(project.rglob(".readiness-*"))


@pytest.mark.parametrize("prior", [False, True], ids=["first", "prior"])
@pytest.mark.parametrize("name", ["kicad/board.kicad_pcb", "output/manufacturing.zip"])
@pytest.mark.parametrize(
    "fault", ["denied", "short-write", "editor-before-open", "editor-after-open"]
)
def test_linkless_refusal_retains_public_files(
    tmp_path, monkeypatch, linkless_volume, prior, name, fault
):
    project = make_project(tmp_path)
    board_dir = project / "kicad"
    if prior:
        assert run(project, "--generate")[0] == 0
        assert read_readiness(board_dir, layout=layout_for(project))["status"] == "ready"
    before = _live_files(project)
    target = project / name
    editor_bytes = b"independent editor revision\n"
    real_open, real_write = os.open, os.write
    target_fd = None
    triggered = False

    def save():
        replacement = project / "editor-save"
        replacement.write_bytes(editor_bytes)
        os.replace(replacement, target)

    def open_target(path, flags, *args, **kwargs):
        nonlocal triggered, target_fd
        if path != target or not flags & os.O_RDWR:
            return real_open(path, flags, *args, **kwargs)
        if fault == "denied":
            triggered = True
            raise PermissionError(errno.EACCES, "Refusing all writes to target", path)
        if not triggered and fault == "editor-before-open":
            triggered = True
            save()
        target_fd = real_open(path, flags, *args, **kwargs)
        if not triggered and fault == "editor-after-open":
            triggered = True
            save()
        return target_fd

    def write_target(fd, data):
        nonlocal triggered
        if fd == target_fd and not triggered and fault == "short-write":
            triggered = True
            return real_write(fd, data[:7])
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", open_target)
    monkeypatch.setattr(os, "write", write_target)
    assert run(project, "--generate", fake=_changed_candidate(project))[0] == 1
    assert triggered
    if fault.startswith("editor"):
        before[Path(name)] = editor_bytes
    assert _live_files(project) == before
    attempt = json.loads((project / "output/readiness-attempt/readiness.json").read_bytes())
    assert attempt["candidate_not_published"] is True
    assert not list(project.rglob(".readiness-*"))
    if prior and not fault.startswith("editor"):
        assert read_readiness(board_dir, layout=layout_for(project))["status"] == "ready"


@pytest.mark.parametrize("use_links", [True, False], ids=["hard-links", "exclusive-copy"])
@pytest.mark.parametrize("name", ["kicad/board.kicad_pcb", "output/manufacturing.zip"])
@pytest.mark.parametrize("moment", ["before-move", "install", "after-receipt"])
def test_publication_preserves_save_through_preopened_descriptor(
    tmp_path, monkeypatch, use_links, name, moment
):
    project = make_project(tmp_path)
    board_dir = project / "kicad"
    target = project / name
    if use_links and not readiness_cmd._supports_publication_links(board_dir):
        pytest.skip("this volume cannot exercise the hard-link installation")
    if not use_links:
        monkeypatch.setattr(readiness_cmd, "_supports_publication_links", lambda directory: False)
    assert run(project, "--generate")[0] == 0
    assert read_readiness(board_dir, layout=layout_for(project))["status"] == "ready"
    before = _live_files(project)
    revision = b"saved through the descriptor opened before generation\n"
    # This editor never reopens the board to save. Only its timing is controlled.
    script = """import os, sys
with open(sys.argv[1], 'r+b', buffering=0) as board:
    print('opened', flush=True)
    data = sys.stdin.buffer.readline()
    board.seek(0)
    assert board.write(data) == len(data)
    board.truncate()
    os.fsync(board.fileno())
"""
    editor = subprocess.Popen(
        [sys.executable, "-c", script, str(target)], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    saved = False

    def save():
        nonlocal saved
        assert editor.stdin is not None
        editor.stdin.write(revision)
        editor.stdin.flush()
        assert editor.wait(timeout=10) == 0
        saved = True

    real_replace, real_link, real_open = os.replace, os.link, os.open
    real_inventory = readiness_cmd._snapshot_inventory

    def replace(source, destination):
        if not saved and moment == "before-move" and source == target:
            save()
            assert target.read_bytes() == revision
        return real_replace(source, destination)

    def link(source, destination):
        if not saved and moment == "install" and destination == target:
            save()
        return real_link(source, destination)

    def open_target(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if not saved and moment == "install" and path == target and flags & os.O_EXCL:
            save()
        return fd

    def inventory(path, held_dirs=None):
        if not saved and moment == "after-receipt" and held_dirs and path == board_dir:
            save()
        return real_inventory(path, held_dirs)

    try:
        assert editor.stdout is not None and editor.stdout.readline() == b"opened\n"
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "link", link)
        monkeypatch.setattr(os, "open", open_target)
        monkeypatch.setattr(readiness_cmd, "_snapshot_inventory", inventory)
        code, _ = run(project, "--generate", fake=_changed_candidate(project))
        # Some filesystems already refuse to rename a file that is open for writing.
        if not saved and os.name == "nt":
            assert code == 1 and target.read_bytes() == before[target.relative_to(project)]
            save()
    finally:
        if editor.poll() is None:
            editor.kill()
        editor.communicate()
    assert saved and code == 1
    before[target.relative_to(project)] = revision
    assert _live_files(project) == before
    assert read_readiness(board_dir, layout=layout_for(project))["status"] == "unverified"
    attempt = json.loads((project / "output/readiness-attempt/readiness.json").read_bytes())
    assert attempt["candidate_not_published"] is True
    assert not list(project.rglob(".readiness-*"))


def test_linkless_rollback_restores_removed_empty_file(tmp_path, monkeypatch, linkless_volume):
    live, candidate = tmp_path / "live", tmp_path / "candidate"
    live.mkdir()
    candidate.mkdir()
    (live / "a").touch()
    (live / "b").write_bytes(b"original")
    (candidate / "b").write_bytes(b"revised")
    real_open = os.open

    def refuse_second(path, flags, *args, **kwargs):
        if path == live / "b" and flags & os.O_RDWR:
            raise PermissionError(errno.EACCES, "Write refused", path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_second)
    with pytest.raises(PermissionError):
        readiness_cmd._publish_candidate(candidate, live, readiness_cmd._snapshot_inventory(live))
    assert (live / "a").read_bytes() == b""
    assert (live / "b").read_bytes() == b"original"
    assert not list(live.rglob(".readiness-*"))


@pytest.mark.parametrize("fault", ["late-rename-error", "target-link-refused"])
def test_failed_claim_restores_captured_file(tmp_path, monkeypatch, fault):
    live, candidate = tmp_path / "live", tmp_path / "candidate"
    live.mkdir()
    candidate.mkdir()
    if not readiness_cmd._supports_publication_links(live):
        pytest.skip("the move-aside installation path requires a hard-link-capable volume")
    target = live / "board.kicad_pcb"
    target.write_bytes(b"original")
    (candidate / target.name).write_bytes(b"revised")
    real_replace, real_link = os.replace, os.link

    def replace(source, destination):
        real_replace(source, destination)
        if source == target and fault == "late-rename-error":
            raise OSError(errno.EIO, "Rename reported an error after moving the file")

    def link(source, destination):
        if destination == target and fault == "target-link-refused":
            raise OSError(errno.ENOTSUP, "Links refused at the live name")
        return real_link(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "link", link)
    with pytest.raises(OSError):
        readiness_cmd._publish_candidate(candidate, live, readiness_cmd._snapshot_inventory(live))
    assert target.read_bytes() == b"original"
    assert not list(live.rglob(".readiness-*"))


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
