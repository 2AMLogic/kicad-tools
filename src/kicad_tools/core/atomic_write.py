"""Shared atomic-write primitive for KiCad file writers.

Issue #4898 (Part of #4880's Konnect-ideas audit, item 3): every low-level
``save_*`` writer in this codebase used a plain ``Path.write_text(...)`` --
no tmp file, no ``fsync``, no ``os.replace`` -- so a SIGKILL/OOM/power-loss
between "bytes handed to the OS" and "final flush" could leave a *torn*
(truncated or partially-written) ``.kicad_pcb`` / ``.kicad_sch`` /
``.kicad_pro`` / ``.kicad_mod`` / ``.kicad_dru`` file on disk.

``route_cmd.py``'s ``_write_routed_pcb()`` already solved this for the
terminal routed-PCB save path (Issue #2808): write to a sibling ``.tmp``
file, ``fsync`` it, then ``os.replace()`` it onto the final path -- an
atomic rename on POSIX filesystems, so a crash mid-write leaves either the
old content or the new content, never a torn mix of both. This module
extracts that same pattern into a single, reusable helper so every writer
(not just the router's own output path) gets the same guarantee.

Not a port of Konnect's ``transaction.rs`` (AGPL-3.0) -- this is the
minimal write -> fsync -> rename primitive, written from scratch, with no
WAL journal, no cross-file transaction, and no lock detection. See the
PR/issue discussion for the audit of which additional pieces of Konnect's
design were adopted vs. declined for this codebase.
"""

from __future__ import annotations

import errno
import hashlib
import io
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path


class SourceChangedError(ValueError):
    """The board no longer contains the bytes validated for export."""


def _recovery_prefix(name: str) -> str:
    """Staging-directory prefix that marks it as a retained recovery copy."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f".{name}.recovery-{stamp}-"


_RECOVERY_NAME = re.compile(r"^\.(?P<board>.+)\.recovery-\d{8}T\d{6}Z-[^/]+$")


def recovery_link_target(directory: Path) -> Path | None:
    """Source named by a displaced board alias, relative to its original parent."""
    for link in (directory / "alias", directory / "original"):
        if link.is_symlink():
            try:
                return directory.parent / link.readlink()
            except FileNotFoundError:
                continue
    return None


def retained_copy_board(directory: Path) -> Path | None:
    """Return the live board for a completed, cleanup-eligible copy."""
    match = _RECOVERY_NAME.fullmatch(directory.name)
    if match is None or directory.is_symlink():
        return None
    board = directory.parent / match["board"]
    if (
        (directory / "complete").is_file()
        and ((directory / "original").is_file() or (directory / "original").is_symlink())
        and board.is_file()
        and not os.path.lexists(directory / "repaired")
    ):
        return board
    return None


def _recovery_boards(path: Path) -> tuple[Path, ...]:
    if path.is_symlink() and path.is_file():
        target = path.resolve()
        parent = path.parent.resolve()
        if target.is_relative_to(parent):
            return path, path.parent / target.relative_to(parent)
    return (path,)


def remove_retained_copy(directory: Path, *, selected_board: Path | None = None) -> bool:
    """Recheck eligibility when applying a cleanup preview."""
    board = retained_copy_board(directory)
    if board is None or (
        selected_board is not None and board not in _recovery_boards(selected_board)
    ):
        return False
    shutil.rmtree(directory)
    return True


def recovery_copies(path: str | Path) -> list[Path]:
    """Retention directories left beside ``path`` by checked rewrites (#10056).

    Each successful checked rewrite keeps the displaced previous file as a
    recoverable copy in a private sibling directory named
    ``.<name>.recovery-<utc-stamp>-<random>/original``. A rollback after
    publication also retains the briefly public inode as ``provisional``
    and any live entry displaced during rollback as ``withdrawn``. An interrupted
    publication's working directory is reserved for manual recovery.
    A live alias also finds records beside its target inside the board's
    directory tree. Select an external source directly to inspect its records.
    Sort by the completion marker's timestamp, then name for tied timestamps.
    """
    path = Path(path)
    copies = []
    for board in _recovery_boards(path):
        for entry in board.parent.iterdir():
            if retained_copy_board(entry) == board:
                try:
                    copies.append(((entry / "complete").stat().st_mtime_ns, entry.name, entry))
                except FileNotFoundError:
                    continue  # Explicit cleanup completed during this listing.
    return [entry for _, _, entry in sorted(copies)]


def clear_recovery_copies(path: str | Path) -> list[Path]:
    """Delete the completed copies returned by ``recovery_copies(path)`` (#10056).

    The live file is never touched. Returns the removed directories in listing order.
    """
    removed = []
    for copy_dir in recovery_copies(path):
        if remove_retained_copy(copy_dir, selected_board=Path(path)):
            removed.append(copy_dir)
    return removed


def _withdraw_published(
    path: Path, repaired: Path, content: str, encoding: str | None, newline: str | None
) -> None:
    """Capture the live entry and restore it if it already contains another save."""
    withdrawn = repaired.with_name("withdrawn")
    try:
        path.rename(withdrawn)
    except FileNotFoundError:
        return
    # Test the entry we actually moved. Checking the live name before unlinking
    # it could delete a different writer's save between those two operations.
    try:
        matches_repair = os.path.samestat(withdrawn.lstat(), repaired.lstat())
    except OSError:
        matches_repair = False
    if matches_repair:
        # An editor can also save in place on our inode. Recreate the exact
        # text encoding and newline translation only on this rollback path.
        expected = io.BytesIO()
        with io.TextIOWrapper(expected, encoding=encoding, newline=newline) as stream:
            stream.write(content)
            stream.flush()
            try:
                matches_repair = withdrawn.read_bytes() == expected.getvalue()
            except OSError:
                matches_repair = False
    # This comparison selects the live board only. Retain the captured link:
    # a handle can still change its bytes after read_bytes() returns.
    if not matches_repair:
        # A newer save that already took the live name stays there.
        with suppress(FileExistsError):
            os.link(withdrawn, path, follow_symlinks=False)


@contextmanager
def checked_write_text(
    path: Path,
    content: str,
    expected_sha256: str,
    *,
    encoding: str | None = "utf-8",
    newline: str | None = None,
) -> Iterator[None]:
    """Keep the previous board available for rollback until the caller finishes validation."""
    # A digest check followed by os.replace() can still erase a save between
    # the two calls. Move the current entry aside, verify that exact file,
    # then link into the vacant name. link() never overwrites a racing save.
    staging = Path(tempfile.mkdtemp(prefix=_recovery_prefix(path.name), dir=path.parent))
    original = staging / "original"
    displaced = original
    repaired = staging / "repaired"
    complete = staging / "complete"
    linked = False
    try:
        with repaired.open("w", encoding=encoding, newline=newline) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Verify hard links are supported here before displacing the board.
        probe = staging / "link-test"
        os.link(repaired, probe)
        probe.unlink()
        # Reserve recovery metadata while the live board is still untouched.
        # The repaired link keeps cleanup out until the final unlink commits.
        complete.touch()
        try:
            path.rename(original)
        except FileNotFoundError as exc:
            raise SourceChangedError("PCB changed during manufacturing export") from exc
        try:
            source = original
            if original.is_symlink():
                target = original.readlink()
                source = path.parent / target
                # Keep the exact directory entry for rollback and a readable
                # version of the target at the documented recovery path.
                alias = staging / "alias"
                try:
                    original.rename(alias)
                finally:
                    if os.path.lexists(alias):
                        displaced = alias
                try:
                    os.link(source.resolve(strict=True), original)
                except OSError as error:
                    if error.errno not in {
                        errno.EXDEV,
                        errno.EPERM,
                        errno.EACCES,
                        errno.ENOTSUP,
                        errno.EMLINK,
                    }:
                        raise
                    # The destination can be on a different filesystem.
                    # The direct writer leaves the original target in place.
                    original.symlink_to(Path("..") / target)
            if hashlib.sha256(original.read_bytes()).hexdigest() != expected_sha256:
                raise SourceChangedError("PCB changed during manufacturing export")
            os.link(repaired, path)
            linked = True
            yield
            # An editor holding the displaced inode can still save to it.
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
                raise SourceChangedError("PCB changed during manufacturing export")
            try:
                repaired.unlink()
            except OSError:
                # A filesystem may complete the removal before returning an
                # error. Only an absent link confirms that it committed.
                try:
                    repaired.lstat()
                except FileNotFoundError:
                    return
                raise
        except BaseException as exc:
            if linked:
                _withdraw_published(path, repaired, content, encoding, newline)
            try:
                os.link(displaced, path, follow_symlinks=False)
            except FileExistsError:
                restored_original = False
            else:
                restored_original = True
            if linked:
                # Both inodes were public. Preserve every open handle across
                # rollback, even if the public name had been replaced before
                # withdrawal. Renaming the working link also makes this record
                # eligible for explicit cleanup after the live name is settled.
                try:
                    repaired.rename(staging / "provisional")
                except OSError as recovery_error:
                    # The working link still preserves the inode for manual
                    # recovery. Propagate the publication failure below so
                    # callers still distinguish a concurrent save from an I/O error.
                    logging.getLogger(__name__).warning(
                        "Could not finalize rollback recovery at %s: %s", staging, recovery_error
                    )
            if not restored_original:
                # Another save owns the live name. Keep the displaced version
                # available too, including when it was itself an editor save.
                raise SourceChangedError(
                    f"PCB changed during manufacturing export (previous file at {original})"
                ) from exc
            if not linked:
                displaced.unlink()
                if displaced != original:
                    original.unlink(missing_ok=True)
            raise
    except BaseException:
        # A competing save can leave the displaced board here for manual
        # recovery. Keep its working link so cleanup cannot take it.
        if not linked and not os.path.lexists(displaced):
            repaired.unlink(missing_ok=True)
            complete.unlink(missing_ok=True)
            staging.rmdir()
        raise


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    encoding: str | None = "utf-8",
    newline: str | None = None,
    expected_sha256: str | None = None,
) -> None:
    """Publish ``content`` to ``path``.

    With ``expected_sha256``, reserve the destination by moving it into a
    private sibling directory, check the displaced bytes, and publish with
    a hard link that fails if another writer takes the name. The path is
    briefly vacant. After linking, the displaced file is re-verified: a
    save through a handle that was already open on it fails the call and
    gets the live name back.

    A successful checked rewrite retains the displaced previous file as a
    recoverable copy in ``.<filename>.recovery-<utc-stamp>-<random>/original``
    beside the destination (#10056): a write landing after that re-check,
    through a handle still open on the previous file, stays recoverable
    there instead of being lost. If the new file was briefly published and
    then rolled back, its inode remains available as ``provisional`` in the
    same directory. Each rewrite adds one such directory;
    ``recovery_copies()`` lists them, ``clear_recovery_copies()`` (or
    ``kct clean``) removes them. An interruption also leaves the displaced
    file in that directory for recovery.

    A checked write to a symlink replaces the named entry and leaves its
    target in place. The record keeps the displaced link as ``alias`` and
    links ``original`` to the target inode. When the target filesystem or
    permissions prevent the hard link, ``original`` refers to the target
    by its path, which must remain available for recovery.

    Without a digest, use the ordinary atomic replacement described below.

    Writes to a sibling ``<path>.tmp`` file (same directory, so the final
    ``os.replace`` is guaranteed to be a same-filesystem rename -- atomic
    on POSIX), ``fsync``s it so the bytes are durable before the rename,
    then atomically replaces ``path`` with the tmp file's contents.

    If the process is killed (or the filesystem errors) at any point
    before ``os.replace`` completes, ``path`` is left with whatever
    content it had before this call -- never a truncated or partially
    written file. The ``.tmp`` file is intentionally left on disk on
    failure (not cleaned up) so the caller/operator can inspect what was
    being written; callers that need cleanup-on-failure should do so
    explicitly.

    Args:
        path: Destination file path.
        content: Text content to write.
        encoding: Encoding to use, or ``None`` to fall back to
            ``Path.write_text``'s platform-default encoding (matches the
            historical, unparameterized ``path.write_text(content)`` some
            callers relied on before this helper existed).

        newline: Optional newline policy. ``""`` disables platform newline
            translation for writers preserving exact source fragments.

    Raises:
        OSError: If the write, fsync, or replace fails. On failure the
            original ``path`` (if it existed) is left untouched.
    """
    path = Path(path)
    if expected_sha256 is not None:
        with checked_write_text(path, content, expected_sha256, encoding=encoding, newline=newline):
            pass
        return
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    if newline is not None:
        tmp_path.write_text(content, encoding=encoding, newline=newline)
    elif encoding is None:
        tmp_path.write_text(content)
    else:
        tmp_path.write_text(content, encoding=encoding)

    # Issue #7478: fsync needs a handle with write access. On Windows
    # ``os.fsync`` is ``_commit()`` -> ``FlushFileBuffers``, which rejects a
    # read-only ("rb") handle with EBADF. "r+b" opens for writing without
    # truncating the bytes written above.
    with open(tmp_path, "r+b") as f:
        os.fsync(f.fileno())

    os.replace(tmp_path, path)
