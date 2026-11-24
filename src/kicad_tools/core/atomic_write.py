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

import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class SourceChangedError(ValueError):
    """The board no longer contains the bytes validated for export."""


def _recovery_prefix(name: str) -> str:
    """Staging-directory prefix that marks it as a retained recovery copy."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f".{name}.recovery-{stamp}-"


def recovery_copies(path: str | Path) -> list[Path]:
    """Retention directories left beside ``path`` by checked rewrites (#10056).

    Each successful checked rewrite keeps the displaced previous file as a
    recoverable copy in a private sibling directory named
    ``.<name>.recovery-<utc-stamp>-<random>/original``; an interrupted
    publication leaves the same layout. Returns the directories, oldest
    first, so callers can inspect the retained versions in rewrite order.
    """
    path = Path(path)
    prefix = f".{path.name}.recovery-"
    return sorted(
        (
            entry
            for entry in path.parent.iterdir()
            if not entry.is_symlink()
            and entry.is_dir()
            and entry.name.startswith(prefix)
            and (entry / "original").is_file()
        ),
        key=lambda entry: entry.name,
    )


def clear_recovery_copies(path: str | Path) -> list[Path]:
    """Delete every retained recovery copy beside ``path`` (#10056).

    The live file is never touched. Returns the removed directories, oldest
    first, so callers can report what was reclaimed.
    """
    removed = []
    for copy_dir in recovery_copies(path):
        shutil.rmtree(copy_dir)
        removed.append(copy_dir)
    return removed


def _publish_checked(
    path: Path, content: str, expected_sha256: str, encoding: str | None, newline: str | None
) -> None:
    # A digest check followed by os.replace() can still erase a save between
    # the two calls. Move the current entry aside, verify that exact file,
    # then link into the vacant name. link() never overwrites a racing save.
    staging = Path(tempfile.mkdtemp(prefix=_recovery_prefix(path.name), dir=path.parent))
    original = staging / "original"
    repaired = staging / "repaired"
    try:
        with repaired.open("w", encoding=encoding, newline=newline) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Verify hard links are supported here before displacing the board.
        probe = staging / "link-test"
        os.link(repaired, probe)
        probe.unlink()
        try:
            path.rename(original)
        except FileNotFoundError as exc:
            raise SourceChangedError("PCB changed during manufacturing export") from exc
        try:
            # A displaced relative symlink still refers to the board's
            # original directory. Resolve its bytes in that context.
            source = path.parent / original.readlink() if original.is_symlink() else original
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
                raise SourceChangedError("PCB changed during manufacturing export")
            os.link(repaired, path)
            # A writer holding the original file open (not re-opening the
            # pathname) writes into the displaced inode, which the link above
            # does not see. Re-verify it now: if it changed, that save is
            # the newer drawing, so give it its name back instead of deleting.
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
                if os.path.samestat(os.lstat(path), os.lstat(repaired)):
                    path.unlink()
                raise SourceChangedError("PCB changed during manufacturing export")
        except BaseException as exc:
            try:
                os.link(original, path, follow_symlinks=False)
            except FileExistsError:
                # Another save owns the live name. Keep the displaced version
                # available too, including when it was itself an editor save.
                raise SourceChangedError(
                    f"PCB changed during manufacturing export (previous file at {original})"
                ) from exc
            original.unlink()
            raise
        # Success: retain the displaced previous file as a recoverable copy
        # (#10056). Its inode keeps this last link, so a save landing through
        # an editor's still-open handle after the re-check above stays
        # recoverable in the recovery directory instead of being lost.
    finally:
        repaired.unlink(missing_ok=True)
        if not os.path.lexists(original):
            staging.rmdir()


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
    there instead of being lost. Each rewrite adds one such directory;
    ``recovery_copies()`` lists them, ``clear_recovery_copies()`` (or
    ``kct clean``) removes them. An interruption also leaves the displaced
    file in that directory for recovery.

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
        _publish_checked(path, content, expected_sha256, encoding, newline)
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
