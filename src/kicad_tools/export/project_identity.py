"""Name the editable KiCad project ``kicad_project.zip`` delivers (Issue #10108).

KiCad opens a project through one basename: ``<name>.kicad_pro`` pairs with
``<name>.kicad_sch`` and ``<name>.kicad_pcb`` in the same directory. A checked
design often arrives under other names -- a revisioned ``board_v07.kicad_pcb``
beside ``board.kicad_sch``, or ``kct route``'s ``board_routed.kicad_pcb`` --
and an archive that keeps them is not a project KiCad can open.

:func:`project_identity` picks the one basename the packaged project file, root
sheet and board share. :func:`archive_layout` maps every checked source to its
member name and raises :class:`ProjectIdentityError` when two would share one.
Renaming happens in the archive only: the checked sources are never modified,
every other member keeps its bytes, and a ``VERSION.md`` member records the
delivered and checked names so the revision stays recoverable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

VERSION_FILE = "VERSION.md"

_REVISION_SUFFIX = re.compile(r"_v(?P<number>[0-9]+)$")
_FILENAME_VALUE = re.compile(r'("filename"\s*:\s*)("(?:[^"\\]|\\.)*")')


class ProjectIdentityError(ValueError):
    """The checked files cannot be packaged as one project KiCad can open."""


@dataclass(frozen=True)
class ProjectIdentity:
    """The one basename the packaged project file, root sheet and board share.

    :attr:`revision` is the ``vNN`` a revisioned checked stem carries, when
    there is one; :attr:`renamed` pairs every checked source that is delivered
    under another name with that name.
    """

    name: str
    pcb: Path
    project: Path | None = None
    root: Path | None = None
    revision: str | None = None
    renamed: tuple[tuple[Path, str], ...] = ()

    @property
    def project_file(self) -> str:
        """The delivered project file name."""
        return f"{self.name}.kicad_pro"

    @property
    def version_source(self) -> Path | None:
        """The ``VERSION.md`` kept beside the checked board, when there is one."""
        carried = self.pcb.parent / VERSION_FILE
        return carried if carried.is_file() else None

    def file_name(self, source: Path) -> str:
        """The file name *source* is delivered under."""
        for checked, delivered in self.renamed:
            if checked == source:
                return delivered
        return source.name


@dataclass(frozen=True)
class ArchiveLayout:
    """Member names of one ``kicad_project.zip``."""

    identity: ProjectIdentity
    directories: dict[Path, str]
    names: dict[Path, str]
    record: bytes | None
    record_name: str

    def checked(self, source: Path) -> str:
        """The member name *source* has when it is archived under its own name."""
        return str(PurePosixPath(self.directories[source]) / source.name)

    def member_bytes(self, source: Path, data: bytes) -> bytes:
        """The member for *source* given its bytes, with the packaging transforms applied."""
        return archived_member(source, data, self.identity, self.record)


def project_identity(
    pcb: Path,
    project: Path | None = None,
    root_schematic: Path | None = None,
) -> ProjectIdentity:
    """Choose the basename the packaged project, root sheet and board share.

    Files that already share one keep it. Otherwise the project takes the root
    sheet's own basename: KiCad opens a project's root sheet by that name, and
    the instance records a schematic saved with its project carry it. The
    board, its rules and the project file follow. Without a root sheet the
    board's stem is the identity.
    """
    pcb = Path(pcb).resolve()
    project = Path(project).resolve() if project is not None else None
    root = Path(root_schematic).resolve() if root_schematic is not None else None
    stems = {path.stem for path in (pcb, project, root) if path is not None}
    name = root.stem if len(stems) > 1 and root is not None else pcb.stem
    rules = pcb.with_suffix(".kicad_dru")
    renamed = [
        (source, source.with_stem(name).name)
        for source in (pcb, project, root, rules if rules.is_file() else None)
        if source is not None and source.stem != name
    ]
    revision = None
    for stem in (pcb.stem, project.stem if project is not None else ""):
        found = _REVISION_SUFFIX.search(stem)
        if found:
            revision = found.group(0).removeprefix("_")
            break
    return ProjectIdentity(name, pcb, project, root, revision, tuple(renamed))


def with_project_filename(data: bytes, filename: str) -> bytes:
    """Return *data* with ``meta.filename`` set to *filename* and nothing else changed.

    Only the string literal moves, so a packaged ``.kicad_pro`` still differs
    from its checked source in that one value. *data* comes back unchanged when
    it already agrees, carries no string ``meta.filename``, or is not JSON.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    try:
        document = json.loads(text)
    except ValueError:
        return data
    meta = document.get("meta") if isinstance(document, dict) else None
    if not isinstance(meta, dict) or not isinstance(meta.get("filename"), str):
        return data
    if meta["filename"] == filename:
        return data
    expected = {**document, "meta": {**meta, "filename": filename}}
    replacement = json.dumps(filename, ensure_ascii=False)
    for match in reversed(list(_FILENAME_VALUE.finditer(text))):
        candidate = text[: match.start(2)] + replacement + text[match.end(2) :]
        try:
            agrees = json.loads(candidate) == expected
        except ValueError:
            agrees = False
        if agrees:
            return candidate.encode("utf-8")
    body = json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
    return body.encode("utf-8")


def archived_member(
    source: Path, data: bytes, identity: ProjectIdentity, record: bytes | None
) -> bytes:
    """The archive member for *source*, given the bytes being archived as *data*."""
    if source == identity.project:
        return with_project_filename(data, identity.project_file)
    if record is not None and source == identity.version_source:
        return record
    return data


def version_record(identity: ProjectIdentity) -> bytes | None:
    """The ``VERSION.md`` member: the record kept beside the board, or one describing the renaming."""
    carried = identity.version_source
    kept = carried.read_bytes() if carried is not None else None
    if not identity.renamed:
        return kept
    rules = identity.pcb.with_suffix(".kicad_dru")
    members = [
        source
        for source in (
            identity.pcb,
            rules if rules.is_file() else None,
            identity.project,
            identity.root,
        )
        if source is not None
    ]
    lines = [
        f"# {identity.name}: project revision record",
        "",
        f"This archive delivers the editable KiCad project `{identity.name}`: "
        f"`{identity.name}.kicad_pro`, `{identity.name}.kicad_sch` and "
        f"`{identity.name}.kicad_pcb` share one basename, so KiCad opens the root "
        "sheet and the board together. The files were renamed while packaging; "
        "the checked sources were not modified.",
        "",
    ]
    if identity.revision:
        lines += [f"Working revision: `{identity.revision}`, read from the checked file names.", ""]
    else:
        lines += ["Working revision: none is encoded in the checked file names.", ""]
    lines += ["| File in this archive | Checked source |", "| --- | --- |"]
    lines += [f"| `{identity.file_name(source)}` | `{source.name}` |" for source in members]
    if kept is not None:
        lines += ["", "## Record kept beside the checked board", ""]
    return ("\n".join(lines) + "\n").encode("utf-8") + (kept or b"")


def _relocated_sheets(
    identity: ProjectIdentity, sources: set[Path], board_directory: PurePosixPath
) -> dict[Path, str]:
    """Move a separated root and its hierarchy together, retaining relative sheet paths."""
    from kicad_tools.report.utils import walk_schematic_hierarchy

    root = identity.root
    if root is None or root.parent == identity.pcb.parent:
        return {}
    hierarchy = walk_schematic_hierarchy(root, sources.__contains__)
    unavailable = hierarchy.outside + hierarchy.missing
    moved = {}
    for sheet in hierarchy.sheets:
        if not sheet.is_relative_to(root.parent):
            unavailable.append(sheet)
        else:
            moved[sheet] = (board_directory / sheet.relative_to(root.parent)).as_posix()
    if unavailable:
        raise ProjectIdentityError(
            f"Cannot pair {root} with {identity.pcb}: {unavailable[0]} cannot be moved "
            "with the selected schematic hierarchy. Put the root sheet beside the board "
            "and keep its relative sheet references valid before exporting"
        )
    return moved


def archive_layout(
    identity: ProjectIdentity,
    sources: Iterable[Path],
    directory_of: Callable[[Path], str],
    aliases: Iterable[Path] = (),
) -> ArchiveLayout:
    """Map every checked source to its archive member name.

    *aliases* are in-project symlinked library members.  They are keyed by
    their own (unresolved) path, so each keeps the library-relative name it was
    discovered under instead of collapsing onto its target.

    *directory_of* gives the archive directory a source is delivered in, as a
    POSIX path. The ``VERSION.md`` record lands beside the delivered project
    file.
    """
    directories: dict[Path, str] = {}
    names: dict[Path, str] = {}
    holders: dict[str, Path] = {}
    checked = [Path(source).resolve() for source in sources]
    board_directory = PurePosixPath(directory_of(identity.pcb))
    sheets = _relocated_sheets(identity, set(checked), board_directory)
    for real in checked:
        directory = directory_of(real)
        member = sheets.get(real, str(PurePosixPath(directory) / identity.file_name(real)))
        previous = holders.setdefault(member, real)
        if previous != real:
            raise ProjectIdentityError(
                f"kicad_project.zip cannot hold one openable project: {previous.name} and "
                f"{real.name} would both be archived as {member}. KiCad opens a project only "
                "when the project file, root sheet and board share one basename: rename one of "
                "the two files"
            )
        directories[real] = directory
        names[real] = member
    for alias in sorted({Path(a) for a in aliases}):
        directory = directory_of(alias)
        member = str(PurePosixPath(directory) / alias.name)
        previous = holders.setdefault(member, alias)
        if previous != alias:
            raise ProjectIdentityError(
                f"kicad_project.zip cannot hold {previous.name} and {alias.name}: "
                f"both would be archived as {member}"
            )
        directories[alias] = directory
        names[alias] = member
    return ArchiveLayout(
        identity,
        directories,
        names,
        version_record(identity),
        str(board_directory / VERSION_FILE),
    )


def archived_pcb_member(names: Iterable[str], pcb: Path) -> str | None:
    """The member holding the selected board in ``kicad_project.zip``.

    It is archived under its checked name, or under the delivered project's
    basename beside the project file of the same stem. ``None`` when neither
    is there exactly once.
    """
    members = list(names)
    exact = [name for name in members if Path(name).name == pcb.name]
    if exact:
        return pcb.name if exact == [pcb.name] else None
    boards = [name for name in members if name.endswith(".kicad_pcb")]
    if len(boards) == 1 and boards[0][: -len(".kicad_pcb")] + ".kicad_pro" in members:
        return boards[0]
    return None
