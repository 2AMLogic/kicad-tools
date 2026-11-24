"""Checked rewrites retain a recoverable copy of the previous board (#10056).

Maintainer decision (2026-11-24): every successful checked manufacturing
rewrite keeps a discoverable, recoverable copy of the displaced previous
file, so a save landing through an editor's still-open handle after the
post-link re-verification stays recoverable instead of being lost with the
unlinked inode. Retained copies are inspectable and removable through
``recovery_copies`` / ``clear_recovery_copies`` and ``kct clean``.
"""

from __future__ import annotations

import errno
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event

import pytest

from kicad_tools.core.atomic_write import (
    SourceChangedError,
    atomic_write_text,
    clear_recovery_copies,
    recovery_copies,
)


def _checked_rewrite(board: Path, next_content: bytes) -> None:
    atomic_write_text(
        board,
        next_content.decode("utf-8"),
        encoding="utf-8",
        newline="",
        expected_sha256=hashlib.sha256(board.read_bytes()).hexdigest(),
    )


def _save_through_open_handle(fd: int, payload: bytes) -> None:
    """The delayed-save probe from the issue: ftruncate + pwrite + fsync."""
    os.ftruncate(fd, 0)
    os.pwrite(fd, payload, 0)
    os.fsync(fd)


class TestCheckedRewriteRetention:
    def test_successful_checked_rewrite_retains_previous_version(self, tmp_path):
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(b"(kicad_pcb original)")

        _checked_rewrite(board, b"(kicad_pcb repaired)")

        assert board.read_bytes() == b"(kicad_pcb repaired)"
        copies = recovery_copies(board)
        assert len(copies) == 1
        assert copies[0].name.startswith(".board.kicad_pcb.recovery-")
        assert (copies[0] / "original").read_bytes() == b"(kicad_pcb original)"

    def test_delayed_save_through_open_handle_stays_recoverable(self, tmp_path):
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(b"(kicad_pcb original)")
        fd = os.open(board, os.O_RDWR)
        try:
            _checked_rewrite(board, b"(kicad_pcb repaired)")
            # The editor's save lands after publication, past the post-link
            # re-check that _publish_checked performs. Before #10056 the
            # displaced inode's last link was dropped and this save was lost.
            _save_through_open_handle(fd, b"(kicad_pcb editors-later-save)")
        finally:
            os.close(fd)

        assert board.read_bytes() == b"(kicad_pcb repaired)"
        copies = recovery_copies(board)
        assert len(copies) == 1
        assert (copies[0] / "original").read_bytes() == b"(kicad_pcb editors-later-save)"

    def test_save_landing_inside_the_recheck_window_stays_recoverable(self, tmp_path, monkeypatch):
        """A save landing between the post-link re-check and the old unlink."""
        from kicad_tools.core import atomic_write

        board = tmp_path / "board.kicad_pcb"
        original = b"(kicad_pcb original)"
        board.write_bytes(original)
        edited = b"(kicad_pcb editors-window-save)"
        original_digest = hashlib.sha256(original).hexdigest()
        real_sha256 = atomic_write.hashlib.sha256
        seen: list[int] = []

        def sha_after_recheck(data):
            digest = real_sha256(data)
            # The digest of the displaced bytes is computed twice (the
            # displacement check, then the post-link re-check). Simulate the
            # editor writing through its open handle right after the second
            # read begins -- inside the window the unlink used to close.
            if bytes(data) == original:
                seen.append(1)
                if len(seen) == 2:
                    assert board.is_file()
                    assert clear_recovery_copies(board) == []
                    _save_through_open_handle(handle.fileno(), edited)
            return digest

        monkeypatch.setattr(atomic_write.hashlib, "sha256", sha_after_recheck)
        with board.open("r+b") as handle:
            atomic_write_text(
                board,
                "(kicad_pcb repaired)",
                encoding="utf-8",
                newline="",
                expected_sha256=original_digest,
            )

        assert seen == [1, 1]
        assert board.read_bytes() == b"(kicad_pcb repaired)"
        copies = recovery_copies(board)
        assert len(copies) == 1
        assert (copies[0] / "original").read_bytes() == edited

    def test_successive_rewrites_retain_one_copy_each(self, tmp_path):
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(b"(kicad_pcb v1)")
        _checked_rewrite(board, b"(kicad_pcb v2)")
        _checked_rewrite(board, b"(kicad_pcb v3)")

        assert board.read_bytes() == b"(kicad_pcb v3)"
        copies = recovery_copies(board)
        assert len(copies) == 2
        retained = sorted((entry / "original").read_bytes() for entry in copies)
        assert retained == [b"(kicad_pcb v1)", b"(kicad_pcb v2)"]

    def test_failed_checked_publication_leaves_no_recovery_copy(self, tmp_path):
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(b"(kicad_pcb original)")

        with pytest.raises(SourceChangedError):
            atomic_write_text(
                board,
                "(kicad_pcb repaired)",
                encoding="utf-8",
                newline="",
                expected_sha256="0" * 64,
            )

        assert board.read_bytes() == b"(kicad_pcb original)"
        assert recovery_copies(board) == []
        assert [entry for entry in tmp_path.iterdir() if entry.is_dir()] == []

    def test_plain_atomic_write_leaves_no_recovery_dir(self, tmp_path):
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(b"(kicad_pcb old)")

        atomic_write_text(board, "(kicad_pcb new)", encoding="utf-8")

        assert board.read_bytes() == b"(kicad_pcb new)"
        assert recovery_copies(board) == []
        assert [entry for entry in tmp_path.iterdir() if entry.is_dir()] == []


class TestRecoveryHelpers:
    def test_clear_recovery_copies_removes_retained_dirs_only(self, tmp_path):
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(b"(kicad_pcb v1)")
        _checked_rewrite(board, b"(kicad_pcb v2)")
        _checked_rewrite(board, b"(kicad_pcb v3)")

        removed = clear_recovery_copies(board)

        assert len(removed) == 2
        assert recovery_copies(board) == []
        assert board.read_bytes() == b"(kicad_pcb v3)"

    def test_dirs_without_original_are_not_recovery_copies(self, tmp_path):
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(b"(kicad_pcb)")
        decoy = tmp_path / ".board.kicad_pcb.recovery-20261124T160000Z-ab12cd"
        decoy.mkdir()
        (decoy / "notes").write_text("not a retained board")

        assert recovery_copies(board) == []
        assert clear_recovery_copies(board) == []
        assert decoy.exists()

    def test_recovery_copies_do_not_leak_across_boards(self, tmp_path):
        board_a = tmp_path / "a.kicad_pcb"
        board_b = tmp_path / "b.kicad_pcb"
        board_a.write_bytes(b"(a v1)")
        board_b.write_bytes(b"(b v1)")
        _checked_rewrite(board_a, b"(a v2)")

        assert recovery_copies(board_b) == []
        assert len(recovery_copies(board_a)) == 1


MINIMAL_PROJECT = '{\n  "meta": {"filename": "proj.kicad_pro", "version": 1}\n}\n'


class TestCleanRemovesRecoveryCopies:
    def _project_with_retained_copy(self, tmp_path: Path) -> tuple[Path, Path]:
        from kicad_tools.cli.clean_cmd import find_cleanable_files  # noqa: F401

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        project_file = project_dir / "proj.kicad_pro"
        project_file.write_text(MINIMAL_PROJECT)
        board = project_dir / "proj.kicad_pcb"
        board.write_bytes(b"(kicad_pcb v2)")
        _checked_rewrite(board, b"(kicad_pcb v3)")
        return project_file, board

    def test_clean_flags_retained_recovery_copies(self, tmp_path):
        from kicad_tools.cli.clean_cmd import find_cleanable_files

        project_file, board = self._project_with_retained_copy(tmp_path)

        result = find_cleanable_files(project_file)

        flagged = result.by_category("recovery_copy")
        assert len(flagged) == 1
        assert flagged[0].path == recovery_copies(board)[0]
        assert flagged[0].size_bytes == len(b"(kicad_pcb v2)")
        assert board in [kept.path for kept in result.to_keep]

    def test_clean_force_deletes_retained_recovery_copies(self, tmp_path, capsys):
        from kicad_tools.cli.clean_cmd import main as clean_main

        project_file, board = self._project_with_retained_copy(tmp_path)

        assert clean_main(["--force", str(project_file)]) == 0

        assert recovery_copies(board) == []
        assert board.read_bytes() == b"(kicad_pcb v3)"
        out = capsys.readouterr().out
        assert "recovery" in out

    def test_clean_dry_run_keeps_retained_recovery_copies(self, tmp_path, capsys):
        from kicad_tools.cli.clean_cmd import main as clean_main

        project_file, board = self._project_with_retained_copy(tmp_path)

        assert clean_main([str(project_file)]) == 0

        assert len(recovery_copies(board)) == 1


@pytest.mark.parametrize("fail", [False, True])
def test_cleanup_while_writer_has_displaced_board(tmp_path, monkeypatch, capsys, fail):
    from kicad_tools.cli.clean_cmd import main as clean_main

    project = tmp_path / "board.kicad_pro"
    project.write_text(MINIMAL_PROJECT)
    board = project.with_suffix(".kicad_pcb")
    board.write_bytes(b"original board")
    displaced, resume = Event(), Event()
    real_link = os.link

    def pause_publication(source, destination, **kwargs):
        if destination == board and source.name == "repaired":
            displaced.set()
            assert resume.wait(10)
            if fail:
                raise OSError("publication failed")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", pause_publication)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(_checked_rewrite, board, b"new board")
        try:
            assert displaced.wait(10)
            assert not board.exists()
            assert recovery_copies(board) == []
            assert clear_recovery_copies(board) == []
            for flags in ([], ["--force"]):
                assert clean_main([str(project), *flags]) == 0
                assert ".recovery-" not in capsys.readouterr().out
            assert next(tmp_path.glob(".*.recovery-*/original")).read_bytes() == b"original board"
        finally:
            resume.set()
        if fail:
            with pytest.raises(OSError, match="publication failed"):
                writer.result()
            assert board.read_bytes() == b"original board"
            assert recovery_copies(board) == []
        else:
            writer.result()
            (copy,) = recovery_copies(board)
            assert (copy / "original").read_bytes() == b"original board"
            assert clean_main([str(project), "--force"]) == 0
            assert not copy.exists()
            assert board.read_bytes() == b"new board"


def test_cleanup_rechecks_missing_board_after_preview(tmp_path):
    from kicad_tools.cli.clean_cmd import delete_files, find_cleanable_files

    project = tmp_path / "board.kicad_pro"
    project.write_text(MINIMAL_PROJECT)
    board = project.with_suffix(".kicad_pcb")
    board.write_bytes(b"original board")
    _checked_rewrite(board, b"new board")
    preview = find_cleanable_files(project)
    (copy,) = preview.by_category("recovery_copy")
    board.unlink()

    assert delete_files(preview) == (0, 0)
    assert clear_recovery_copies(board) == []
    assert (copy.path / "original").read_bytes() == b"original board"


def test_recovery_marker_allocation_failure_preserves_board(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"original board")
    real_touch = Path.touch

    def fail_marker(path, *args, **kwargs):
        if path.name == "complete":
            raise OSError(errno.ENOSPC, "no space for recovery marker")
        return real_touch(path, *args, **kwargs)

    monkeypatch.setattr(Path, "touch", fail_marker)
    with pytest.raises(OSError, match="no space for recovery marker"):
        _checked_rewrite(board, b"new board")

    assert board.read_bytes() == b"original board"
    assert list(tmp_path.iterdir()) == [board]


@pytest.mark.parametrize("editor_save", [None, "replace", "in-place"])
def test_failed_recovery_finalization_preserves_newest_board(tmp_path, monkeypatch, editor_save):
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"original board")
    real_unlink = Path.unlink
    attempted = False

    def fail_final_unlink(path, *args, **kwargs):
        nonlocal attempted
        if not attempted and path.name == "repaired" and board.is_file() and board.samefile(path):
            attempted = True
            assert recovery_copies(board) == []
            if editor_save == "replace":
                editor = tmp_path / "save.tmp"
                editor.write_bytes(b"editor save")
                editor.replace(board)
            elif editor_save == "in-place":
                board.write_bytes(b"editor save")
            raise OSError(errno.EIO, "cannot finish recovery record")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_final_unlink)
    with pytest.raises((OSError, SourceChangedError)):
        _checked_rewrite(board, b"new board")

    assert attempted
    (record,) = recovery_copies(board)
    assert board.read_bytes() == (b"editor save" if editor_save else b"original board")
    assert (record / "original").read_bytes() == b"original board"
    provisional = b"editor save" if editor_save == "in-place" else b"new board"
    assert (record / "provisional").read_bytes() == provisional
    assert (record / "withdrawn").read_bytes() == (b"editor save" if editor_save else provisional)


@pytest.mark.parametrize("trigger", ["source_change", "final_unlink"])
def test_rollback_keeps_both_editor_handles_after_withdrawal_read(tmp_path, monkeypatch, trigger):
    from kicad_tools.cli.clean_cmd import delete_files, find_cleanable_files

    board = tmp_path / "board.kicad_pcb"
    project = board.with_suffix(".kicad_pro")
    project.write_text(MINIMAL_PROJECT)
    board.write_bytes(b"previous board")
    _checked_rewrite(board, b"original")
    (older,) = recovery_copies(board)
    real_link, real_unlink, real_read = os.link, Path.unlink, Path.read_bytes
    second = None
    saved_after_read = False

    def open_published(source, target, **kwargs):
        nonlocal second
        result = real_link(source, target, **kwargs)
        if source.name == "repaired" and target == board:
            second = handles.enter_context(board.open("r+b"))
            if trigger == "source_change":
                _save_through_open_handle(first.fileno(), b"first editor")
        return result

    def fail_final_unlink(path, *args, **kwargs):
        if trigger == "final_unlink" and path.name == "repaired" and second is not None:
            _save_through_open_handle(first.fileno(), b"first editor")
            raise OSError(errno.EIO, "final unlink failed")
        return real_unlink(path, *args, **kwargs)

    def save_after_read(path):
        nonlocal saved_after_read
        data = real_read(path)
        if path.name == "withdrawn" and not saved_after_read:
            assert second is not None and data == b"provisional"
            _save_through_open_handle(second.fileno(), b"second editor")
            saved_after_read = True
        return data

    with ExitStack() as handles, monkeypatch.context() as patch:
        first = handles.enter_context(board.open("r+b"))
        patch.setattr(os, "link", open_published)
        patch.setattr(Path, "unlink", fail_final_unlink)
        patch.setattr(Path, "read_bytes", save_after_read)
        error = SourceChangedError if trigger == "source_change" else OSError
        with pytest.raises(error):
            _checked_rewrite(board, b"provisional")
        assert saved_after_read and second is not None
        assert board.read_bytes() == b"first editor"
        (record,) = set(recovery_copies(board)) - {older}
        assert os.path.samestat(os.fstat(second.fileno()), (record / "provisional").stat())
        assert (record / "provisional").read_bytes() == b"second editor"
        _save_through_open_handle(second.fileno(), b"save after rollback")

    assert (older / "original").read_bytes() == b"previous board"
    assert (record / "provisional").read_bytes() == b"save after rollback"
    preview = find_cleanable_files(project)
    sizes = {row.path: row.size_bytes for row in preview.by_category("recovery_copy")}
    assert sizes == {older: len(b"previous board"), record: len(b"save after rollback")}
    assert delete_files(preview) == (2, sum(sizes.values()))
    assert not recovery_copies(board) and board.read_bytes() == b"first editor"


@pytest.mark.parametrize("completion", ["concurrent_cleanup", "rename_failure"])
def test_rollback_completion_preserves_the_live_board(tmp_path, monkeypatch, completion):
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"original")
    real_link, real_rename = os.link, Path.rename
    cleaned = []

    def edit_original(source, target, **kwargs):
        real_link(source, target, **kwargs)
        if source.name == "repaired" and target == board:
            (source.parent / "original").write_bytes(b"editor save")

    def clean_completed_rollback(path, target):
        finishing = path.name == "repaired" and target.name == "provisional"
        if finishing and completion == "rename_failure":
            raise OSError(errno.EIO, "cannot complete rollback record")
        result = real_rename(path, target)
        if finishing:
            cleaned.extend(clear_recovery_copies(board))
        return result

    monkeypatch.setattr(os, "link", edit_original)
    monkeypatch.setattr(Path, "rename", clean_completed_rollback)
    error = SourceChangedError if completion == "concurrent_cleanup" else OSError
    with pytest.raises(error):
        _checked_rewrite(board, b"briefly published")
    assert board.read_bytes() == b"editor save"
    assert clear_recovery_copies(board) == []
    if completion == "concurrent_cleanup":
        assert len(cleaned) == 1 and list(tmp_path.iterdir()) == [board]
    else:
        (working,) = tmp_path.glob(".*.recovery-*")
        assert (working / "original").read_bytes() == b"editor save"
        assert (working / "repaired").read_bytes() == b"briefly published"


def test_cleanup_at_commit_cannot_invalidate_publication(tmp_path, monkeypatch):
    board = tmp_path / "board.kicad_pcb"
    board.write_bytes(b"previous")
    real_unlink = Path.unlink
    cleaned = []

    def clean_after_unlink(path, *args, **kwargs):
        if path.name == "repaired" and board.is_file() and board.samefile(path):
            real_unlink(path, *args, **kwargs)
            cleaned.extend(clear_recovery_copies(board))
            raise OSError(errno.EIO, "filesystem returned an error after removing the link")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", clean_after_unlink)
    _checked_rewrite(board, b"new board")
    assert len(cleaned) == 1
    assert board.read_bytes() == b"new board"
    assert list(tmp_path.iterdir()) == [board]
