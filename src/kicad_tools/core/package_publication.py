"""Publish a verified set of files while preserving concurrent edits and prior outputs."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    """Return the SHA256 hex digest of *path*'s bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Name each path that was added, removed or modified between two inventories."""
    changes = [f"added {name}" for name in sorted(after.keys() - before.keys())]
    changes.extend(f"removed {name}" for name in sorted(before.keys() - after.keys()))
    changes.extend(
        f"modified {name}"
        for name in sorted(before.keys() & after.keys())
        if after[name] != before[name]
    )
    return changes


def with_changes(message: str, changes: list[str]) -> str:
    """Append the named changes to ``message``; an empty list leaves it unchanged."""
    return f"{message}: {', '.join(changes)}" if changes else message


def _publication_identity(path: Path) -> tuple[Any, ...] | None:
    """Identify the actual file, including same-content inode replacements."""
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Publication target is not a regular file: {path}")
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_mode,
        stat.st_size,
        stat.st_mtime_ns,
        sha256_file(path),
    )


class PublicationBusy(RuntimeError):
    """Another process is installing readiness results at this destination."""


@contextmanager
def publication_lock(lock: Path) -> Iterator[None]:
    """Serialize writers. An abandoned lock requires the owner to clear it."""
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise PublicationBusy(f"Another readiness publisher holds the release lock {lock}") from exc
    try:
        yield
    finally:
        lock.rmdir()


def publish_directory_locked(
    candidate: Path,
    destination: Path,
    before: dict[str, str],
    inventory: Callable[[Path], dict[str, str]],
    *,
    publish_last: str | None = None,
    destination_path: Callable[[str], Path] | None = None,
) -> None:
    """Install a complete candidate and roll back our own writes if publication fails."""
    target_for = destination_path if destination_path is not None else destination.joinpath
    current = inventory(destination)
    if current != before:
        raise RuntimeError(
            with_changes(
                "Release changed during verification; refusing to overwrite concurrent work",
                inventory_changes(before, current),
            )
        )
    after = inventory(candidate)
    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    if publish_last is not None and publish_last in changed:
        changed.remove(publish_last)
        changed.append(publish_last)
    expected = {name: _publication_identity(target_for(name)) for name in changed}
    for name, identity in expected.items():
        digest = identity[-1] if identity is not None else None
        if digest != before.get(name):
            raise RuntimeError(f"Concurrent edit while identifying publication target: {name}")
    with tempfile.TemporaryDirectory(prefix="kct-release-backup-") as temporary:
        backup = Path(temporary)
        installed: dict[str, tuple[Any, ...] | None] = {}
        try:
            for name in changed:
                target = target_for(name)
                if _publication_identity(target) != expected[name]:
                    raise RuntimeError(f"Concurrent edit before publication: {name}")
                if name in before:
                    copy = backup / name
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, copy)
                if name not in after:
                    if _publication_identity(target) != expected[name]:
                        raise RuntimeError(f"Concurrent edit before deletion: {name}")
                    target.unlink()
                    installed[name] = None
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, staged_name = tempfile.mkstemp(prefix=".readiness-", dir=target.parent)
                os.close(fd)
                staged = Path(staged_name)
                try:
                    shutil.copy2(candidate / name, staged)
                    identity = _publication_identity(staged)
                    if _publication_identity(target) != expected[name]:
                        raise RuntimeError(f"Concurrent edit before replacement: {name}")
                    os.replace(staged, target)
                    # Capture our staged identity, not whatever an editor may
                    # have written to the destination immediately afterwards.
                    installed[name] = identity
                finally:
                    staged.unlink(missing_ok=True)
            current = inventory(destination)
            if current != after:
                raise RuntimeError(
                    with_changes(
                        "Concurrent edit during publication",
                        inventory_changes(after, current),
                    )
                )
        except Exception as exc:
            conflicts = []
            for name, identity in reversed(installed.items()):
                target = target_for(name)
                try:
                    if _publication_identity(target) != identity:
                        conflicts.append(name)
                        continue
                    if name in before:
                        fd, staged_name = tempfile.mkstemp(prefix=".readiness-", dir=target.parent)
                        os.close(fd)
                        staged = Path(staged_name)
                        try:
                            shutil.copy2(backup / name, staged)
                            if _publication_identity(target) != identity:
                                conflicts.append(name)
                                continue
                            os.replace(staged, target)
                        finally:
                            staged.unlink(missing_ok=True)
                    else:
                        target.unlink(missing_ok=True)
                except Exception:
                    conflicts.append(name)
            if conflicts:
                raise RuntimeError(
                    f"{exc}; rollback preserved concurrent edits or could not restore: "
                    + ", ".join(conflicts)
                ) from exc
            raise
