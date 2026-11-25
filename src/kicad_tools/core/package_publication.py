"""Publish verified output regions together while preserving concurrent file edits."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)


def sha256_file(path: Path) -> str:
    """Return the SHA256 hex digest of *path*'s bytes."""
    with path.open("rb") as handle:
        return _sha256_stream(handle)


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(65536), b""):
        digest.update(chunk)
    return digest.hexdigest()


PUBLICATION_HOLD_PREFIX = ".readiness-"


def is_publication_hold(path: Path) -> bool:
    """True inside a retained publication backup (Issue #10106).

    A hold can live in a generated directory when that directory is its own
    mount, so it must never become an input, package member or removal target.
    """
    return any(part.startswith(PUBLICATION_HOLD_PREFIX) for part in path.parts)


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


def _stream_identity(stream: BinaryIO) -> tuple[Any, ...]:
    info = os.fstat(stream.fileno())
    stream.seek(0)
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        _sha256_stream(stream),
    )


def _publication_identity(path: Path) -> tuple[Any, ...] | None:
    """Identify the opened file, including same-content inode replacements."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError(f"Publication target is not a regular file: {path}")
        with path.open("rb") as stream:
            return _stream_identity(stream)
    except FileNotFoundError:
        return None


def _supports_publication_links(directory: Path) -> bool:
    """Probe private names before moving any live file on this filesystem."""
    with tempfile.TemporaryDirectory(prefix=".readiness-probe-", dir=directory) as scratch:
        source = Path(scratch) / "source"
        source.touch()
        try:
            os.link(source, Path(scratch) / "link")
        except OSError as exc:
            if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.ENOSYS):
                return False
            raise
    return True


def _copy_publication_file(
    source: Path, target: Path, record: Callable[[tuple[Any, ...] | None], None] | None = None
) -> None:
    """Copy only into a newly created name on a filesystem without hard links."""
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with source.open("rb") as staged:
        source_info = os.fstat(staged.fileno())
        fd = os.open(target, flags, stat.S_IMODE(source_info.st_mode))
        with os.fdopen(fd, "r+b", buffering=0) as live:
            if os.fstat(fd).st_size:
                raise RuntimeError(f"Concurrent edit before publication: {target.name}")
            digest = hashlib.sha256()
            try:
                for chunk in iter(lambda: staged.read(65536), b""):
                    if os.write(fd, chunk) != len(chunk):
                        raise OSError(errno.ENOSPC, "Incomplete publication write", target)
                    digest.update(chunk)
                os.fsync(fd)
                if os.utime in os.supports_fd:
                    os.utime(fd, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))
            except BaseException:
                # A short write may already have changed our file. Journal the
                # descriptor, never the pathname an atomic save may now own.
                if record is not None:
                    record(_stream_identity(live))
                raise
            identity = _stream_identity(live)
            if identity[-1] != digest.hexdigest():
                raise RuntimeError(f"Concurrent edit during publication: {target.name}")
            if record is not None:
                # FAT may assign the permanent inode only after the first write.
                record(identity)
            if not os.path.samestat(os.fstat(fd), target.lstat()):
                raise RuntimeError(f"Concurrent edit during publication: {target.name}")


def _restore_held(hold: Path, target: Path, links: bool) -> bool:
    """Restore the captured file only if no newer save has taken its name."""
    if links:
        try:
            os.link(hold, target)
            return True
        except FileExistsError:
            return False
        except OSError:
            # Even a filesystem that passed the probe may refuse this link.
            pass
    try:
        _copy_publication_file(hold, target)
    except FileExistsError:
        return False
    return True


@dataclass
class _PublicationHold:
    target: Path
    path: Path
    identity: tuple[Any, ...]
    links: bool

    @property
    def reserve(self) -> Path:
        return self.path.with_name("recovery-space")

    def release_space(self) -> None:
        if self.reserve.exists():
            # Reclaim blocks even when a filesystem watcher has the file open.
            with self.reserve.open("wb"):
                pass
            self.reserve.unlink()

    def discard(self) -> None:
        """Retire a private backup after its contents have been committed or restored."""
        try:
            self.release_space()
            self.path.unlink(missing_ok=True)
            self.path.parent.rmdir()
        except OSError as error:
            # A busy backup or an unexpected file belongs to the operator. Keep
            # it available without changing the outcome of either public region.
            logger.warning("Could not clean up readiness backup %s: %s", self.path.parent, error)

    def restore(self) -> None:
        self.release_space()
        restored = _restore_held(self.path, self.target, self.links)
        if not restored and _publication_identity(self.path) != self.identity:
            raise RuntimeError(
                f"Another save owns {self.target}; the displaced editor's file remains at {self.path}"
            )
        self.discard()


def _claim_target(
    target: Path,
    expected: tuple[Any, ...] | None,
    links: bool,
    region_root: Path,
    *,
    reserve: bool = False,
) -> _PublicationHold | None:
    """Retain the actual displaced file so a writer with it open can finish a save."""
    if expected is None:
        if os.path.lexists(target):
            raise RuntimeError(f"Concurrent edit before publication: {target.name}")
        return None
    # Keep retained backups outside package and evidence subdirectories. A
    # nested mount needs a hold on its own device for the editor-preserving rename.
    parent = target.parent
    if not parent.is_relative_to(region_root):
        region_root = parent
    device = parent.stat().st_dev
    while parent != region_root and parent.parent.stat().st_dev == device:
        parent = parent.parent
    private = Path(tempfile.mkdtemp(prefix=".readiness-", dir=parent))
    hold = _PublicationHold(target, private / ".readiness-hold-original", expected, links)
    try:
        with ExitStack() as stack:
            opened = None
            if not links:
                # Preflight access and enough recovery space before moving anything.
                # Restoration on FAT needs a fresh file at the public name.
                flags = os.O_RDWR if reserve else os.O_RDONLY
                fd = os.open(target, flags | getattr(os, "O_NOFOLLOW", 0))
                opened = stack.enter_context(os.fdopen(fd, "rb"))
                if _stream_identity(opened) != expected:
                    raise RuntimeError(f"Concurrent edit before replacement: {target.name}")
                if reserve:
                    opened.seek(0)
                    with hold.reserve.open("xb") as space:
                        shutil.copyfileobj(opened, space)
                        space.flush()
                        os.fsync(space.fileno())
                if os.name != "posix" or expected[3] != 0:
                    # Windows denies a rename while our own descriptor is open.
                    opened.close()
                    opened = None
            os.replace(target, hold.path)
            if opened is not None:
                # A zero-length FAT file can change inode at rename.
                expected = (expected[0], os.fstat(opened.fileno()).st_ino, *expected[2:])
            observed = _publication_identity(hold.path)
            if observed != expected:
                raise RuntimeError(f"Concurrent edit before replacement: {target.name}")
            hold.identity = expected
    except BaseException:
        if os.path.lexists(hold.path):
            hold.restore()
        else:
            hold.discard()
        raise
    return hold


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
        try:
            lock.rmdir()
        except OSError as error:
            logger.warning("Could not release readiness publication lock %s: %s", lock, error)


@dataclass(frozen=True)
class PublicationRegion:
    """One staged directory and its destination, keyed by the inventory's portable names."""

    candidate: Path
    destination: Path
    before: dict[str, str]
    # A standalone manufacturing export can map a configured output to another volume.
    destination_path: Callable[[str], Path] | None = None


def publish_regions(
    regions: Sequence[PublicationRegion],
    inventory: Callable[[Path, set[Path] | None], dict[str, str]],
    *,
    publish_last: Path | None = None,
    external_changes: Callable[[], list[str]] | None = None,
    remove_empty_directories: Sequence[Path] = (),
) -> None:
    """Hold every lock and rollback journal until all regions and dependencies verify."""
    with ExitStack() as publication:
        # All publishers take locks in the same order, including when only
        # the release root is shared by two different board directories.
        for root in sorted({region.destination.resolve() for region in regions}):
            root.parent.mkdir(parents=True, exist_ok=True)
            publication.enter_context(
                publication_lock(root.parent / f".{root.name}.readiness-publish.lock")
            )

        holds: list[_PublicationHold] = []
        with ExitStack() as installs:
            inventories = []
            for region in regions:
                after = installs.enter_context(
                    _install_candidate(region, inventory, publish_last, holds)
                )
                inventories.append((region.destination, after))
            # Both regions can still roll back when we recheck an earlier file or
            # a project dependency outside them. The consumer receipt was installed last.
            held_dirs = {hold.path.parent for hold in holds}
            for live, after in inventories:
                current = inventory(live, held_dirs)
                if current != after:
                    raise RuntimeError(
                        with_changes(
                            "Concurrent edit during publication", inventory_changes(after, current)
                        )
                    )
            if external_changes is not None:
                edits = external_changes()
                if edits:
                    raise RuntimeError(with_changes("Concurrent edit during publication", edits))
            # An already-open editor can save to a displaced inode while we install
            # either region. Check all originals while both journals can still unwind.
            for hold in holds:
                if _publication_identity(hold.path) != hold.identity:
                    raise RuntimeError(f"Concurrent edit during publication: {hold.target.name}")

        # Both regions have committed. Retire all backups under the locks.
        for original in holds:
            original.discard()
        # A successful standalone re-export can retire a previous report directory.
        for directory in remove_empty_directories:
            with suppress(OSError):
                directory.rmdir()


def _make_publish_directory(directory: Path, created: list[Path]) -> None:
    """Record only directories this installation created, for empty-directory rollback."""
    if directory.is_dir():
        return
    _make_publish_directory(directory.parent, created)
    try:
        directory.mkdir()
    except FileExistsError:
        if not directory.is_dir():
            raise
    else:
        created.append(directory)


@contextmanager
def _install_candidate(
    region: PublicationRegion,
    inventory: Callable[[Path, set[Path] | None], dict[str, str]],
    publish_last: Path | None,
    holds: list[_PublicationHold],
) -> Iterator[dict[str, str]]:
    """Install one region, retaining its rollback journal through the caller's commit."""
    candidate, destination, before = region.candidate, region.destination, region.before
    target_for = region.destination_path or destination.joinpath
    current = inventory(destination, None)
    if current != before:
        raise RuntimeError(
            with_changes(
                "Release changed during verification; refusing to overwrite concurrent work",
                inventory_changes(before, current),
            )
        )
    after = inventory(candidate, None)
    # The readiness receipt becomes visible after the artifacts it describes.
    changed = sorted(
        (name for name in set(before) | set(after) if before.get(name) != after.get(name)),
        key=lambda name: (target_for(name) == publish_last, name),
    )
    expected = {name: _publication_identity(target_for(name)) for name in changed}
    for name, identity in expected.items():
        digest = identity[-1] if identity is not None else None
        if digest != before.get(name):
            raise RuntimeError(f"Concurrent edit while identifying publication target: {name}")
    links_by_device: dict[int, bool] = {}

    originals: dict[str, _PublicationHold] = {}
    installed: dict[str, tuple[Any, ...] | None] = {}
    created: list[Path] = []
    try:
        for name in changed:
            target = target_for(name)
            if _publication_identity(target) != expected[name]:
                raise RuntimeError(f"Concurrent edit before publication: {name}")
            _make_publish_directory(target.parent, created)
            device = target.parent.stat().st_dev
            if device not in links_by_device:
                links_by_device[device] = _supports_publication_links(target.parent)
            links = links_by_device[device]
            with ExitStack() as preparation:
                staged = candidate / name
                if links and name in after:
                    fd, staged_name = tempfile.mkstemp(prefix=".readiness-", dir=target.parent)
                    os.close(fd)
                    staged = Path(staged_name)
                    preparation.callback(staged.unlink, missing_ok=True)
                    shutil.copy2(candidate / name, staged)
                hold = _claim_target(target, expected[name], links, destination, reserve=True)
                if hold is not None:
                    originals[name] = hold
                    holds.append(hold)
                    installed[name] = None
                if name not in after:
                    continue
                record = partial(installed.__setitem__, name)
                if not links:
                    try:
                        _copy_publication_file(staged, target, record)
                    except FileExistsError:
                        raise RuntimeError(f"Concurrent edit before replacement: {name}") from None
                    continue
                identity = _publication_identity(staged)
                try:
                    os.link(staged, target)
                except FileExistsError:
                    raise RuntimeError(f"Concurrent edit before replacement: {name}") from None
                except BaseException:
                    if staged.stat().st_nlink > 1:
                        record(identity)
                    raise
                else:
                    record(identity)
        yield after
    except BaseException as exc:
        conflicts = []
        for name, identity in reversed(installed.items()):
            target = target_for(name)
            original = originals.get(name)
            if original is not None:
                original.release_space()
            links = links_by_device[target.parent.stat().st_dev]
            try:
                withdrawn = _claim_target(target, identity, links, destination)
            except Exception:
                # A newer save (or an editor's deletion) owns the public name.
                if (
                    original is not None
                    and _publication_identity(original.path) == original.identity
                ):
                    original.discard()
                conflicts.append(
                    f"{name} (saved at {original.path})"
                    if original is not None and original.path.exists()
                    else name
                )
                continue
            try:
                if original is not None:
                    # Restore the actual displaced inode's latest bytes, never
                    # a backup copied before the editor completed its save.
                    original.restore()
            except Exception:
                conflicts.append(
                    f"{name} (saved at {original.path})"
                    if original is not None and original.path.exists()
                    else name
                )
                if withdrawn is not None:
                    try:
                        withdrawn.restore()
                    except Exception:
                        conflicts.append(str(withdrawn.path))
            else:
                if withdrawn is not None:
                    withdrawn.discard()
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                    conflicts.append(str(directory))
        if conflicts:
            raise RuntimeError(
                f"{exc}; rollback preserved concurrent edits or could not restore: "
                + ", ".join(conflicts)
            ) from exc
        raise
