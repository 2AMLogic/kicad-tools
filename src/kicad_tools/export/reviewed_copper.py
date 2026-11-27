"""Reviewed-copper verification for ``kct export --verify-reviewed-copper`` (Issue #10492).

``kct export`` writes the manufacturer DRC rules and refills the zones so the
shipped copper obeys the rules that ship (#9987). That can plot copper the
designer never reviewed: an inner pour that shrinks under a new clearance, or
an outlined zone with no saved fill that gains copper during export. A team
that signed off the saved copper opts in to publish the package only when every
copper layer of the staged Gerber archive equals a native plot of the saved
board, taken before any rule was written or any zone refilled.

The comparison is geometric. Each layer is reconstructed with the strict
Gerber reader, then the two directional differences are measured: added area is
candidate minus reference and removed area is reference minus candidate, both
in mm-squared. Timestamps, X2 metadata and a polygon's starting vertex leave
the image unchanged, while copper that moved with its total area intact shows
in both directions. A layer whose artwork is missing, ambiguous or unreadable
fails closed.

:class:`ReviewedCopperExport` holds the scratch an export of this kind works
in. The checked-out board is never written: rules, refill, silk and plots are
prepared on a private working copy, and the reference is plotted from a byte
snapshot of the saved board.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..validate.gerber_geometry import parse_gerber_geometry
from .preflight import PreflightResult

logger = logging.getLogger(__name__)

SCHEMA = "kct.reviewed-copper.v1"
CHECK_NAME = "reviewed_copper"

#: Added and removed area, each, at or below this (mm-squared) is the same
#: copper. Two plots of one board overlay to exactly zero, and a rotated start
#: vertex or reordered metadata leaves the image unchanged, so the tolerance
#: only absorbs the floating-point residue of reconstructing one outline
#: twice: the area of a 1 nm sliver one millimetre long, below anything a
#: plot can express.
REVIEWED_COPPER_TOLERANCE_MM2 = 1e-6

_FILE_FUNCTION_RE = re.compile(r"%TF\.FileFunction,([^*%\s]+)\*%")
# What reading a member of an archive can raise: a truncated or encrypted
# entry, an unsupported compression, a broken container.
_UNREADABLE = (
    OSError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    zipfile.BadZipFile,
    zlib.error,
)


@dataclass
class CopperLayer:
    """One expected copper layer of one plot, read into geometry."""

    layer: str
    member: str | None = None
    sha256: str | None = None
    geometry: Any = None
    #: ``read`` once the image is reconstructed, else which check it failed.
    status: str = "read"
    detail: str | None = None

    def identity(self) -> dict[str, str | None]:
        return {"member": self.member, "sha256": self.sha256}


_UNEXPECTED = "unexpected copper:"
_COPPER_NAME_RE = re.compile(r"-(?:F|B|In\d+)_Cu\.[A-Za-z0-9]+$")


def _layer_member_re(layer: str) -> re.Pattern[str]:
    """Match kicad-cli's member name for ``layer``, e.g. ``board-In1_Cu.g1``."""
    return re.compile(rf"-{re.escape(layer.replace('.', '_'))}\.[A-Za-z0-9]+$")


def _copper_position(index: int, count: int) -> str:
    """The ``%TF.FileFunction,Copper,L<n>,<position>`` token of one copper layer."""
    if index == 0:
        return "top"
    return "bot" if index == count - 1 else "inr"


def _select_and_read(
    names: Sequence[str],
    head: Callable[[str], bytes],
    read: Callable[[str], bytes],
    layers: Sequence[str],
) -> dict[str, CopperLayer]:
    """Name each expected layer's member before reading any payload."""
    functions: dict[str, tuple[str, list[str]] | None] = {}
    for name in names:
        try:
            text = head(name).decode("utf-8", errors="replace")
        except _UNREADABLE:
            functions[name] = None
            continue
        match = _FILE_FUNCTION_RE.search(text)
        if match is None:
            functions[name] = None
        else:
            functions[name] = (match[1], [part.strip().lower() for part in match[1].split(",")])

    # KiCad names a plot after the layer's user-facing name and copper layers
    # can be renamed, so the X2 function's ordinal is authoritative. The file
    # name only decides for artwork that carries no X2 attributes at all.
    tagged = any(functions.values())
    entries: dict[str, CopperLayer] = {}
    considered: set[str] = set()
    for index, layer in enumerate(layers):
        entry = entries[layer] = CopperLayer(layer)
        member_re = _layer_member_re(layer)
        by_name = [name for name in names if member_re.search(Path(name).name)]
        by_function = [
            name
            for name in names
            if (function := functions[name])
            and function[1][0] == "copper"
            and len(function[1]) > 1
            and function[1][1] == f"l{index + 1}"
        ]
        matches = by_function if tagged else by_name
        if tagged and set(by_name) - set(by_function):
            matches = sorted(set(by_function) | set(by_name))
        considered.update(matches)
        if not matches:
            entry.status, entry.detail = "missing", "no archive member for this layer"
            continue
        if len(matches) > 1:
            entry.status = "ambiguous"
            entry.detail = "ambiguous archive members: " + ", ".join(matches)
            continue
        entry.member = matches[0]
        function = functions[entry.member]
        if function is not None and (
            function[1][0] != "copper"
            or len(function[1]) < 3
            or function[1][1] != f"l{index + 1}"
            or function[1][2] != _copper_position(index, len(layers))
        ):
            entry.status = "unreadable"
            entry.detail = f"FileFunction is {function[0]}, not the copper layer it was taken for"
            continue
        try:
            data = read(entry.member)
        except _UNREADABLE as exc:
            entry.status, entry.detail = "unreadable", f"archive member unreadable: {exc}"
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            entry.status, entry.detail = "unreadable", "member is not text"
            continue
        entry.sha256 = hashlib.sha256(data).hexdigest()
        try:
            entry.geometry = parse_gerber_geometry(text).geometry
        except Exception as exc:  # noqa: BLE001 - unrenderable artwork fails closed, never the export
            entry.status, entry.detail = "unreadable", f"unreadable Gerber: {exc}"

    # Every copper member of the plot must belong to a board layer: copper the
    # comparison never looks at would ship unreviewed.
    for name in names:
        if name in considered:
            continue
        function = functions[name]
        tagged_copper = function is not None and function[1][0] == "copper"
        if tagged_copper or _COPPER_NAME_RE.search(Path(name).name):
            entries[f"{_UNEXPECTED} {name}"] = CopperLayer(
                f"{_UNEXPECTED} {name}",
                member=name,
                status="unexpected",
                detail="copper archive member matches no copper layer of the saved board",
            )
    return entries


def load_copper(source: Path, layers: Sequence[str]) -> dict[str, CopperLayer]:
    """Read each copper layer of a Gerber ZIP or a directory of loose plots.

    Raises ``OSError``, ``RuntimeError`` or ``zipfile.BadZipFile`` when *source*
    itself cannot be opened; a member that cannot be read is recorded on its layer.
    """
    if source.is_dir():
        names = sorted(p.name for p in source.iterdir() if p.is_file())

        def read(name: str) -> bytes:
            return (source / name).read_bytes()

        return _select_and_read(names, read, read, layers)
    with zipfile.ZipFile(source) as archive:
        names = sorted(name for name in archive.namelist() if not name.endswith("/"))
        return _select_and_read(
            names,
            lambda name: archive.open(name).read(4096),
            archive.read,
            layers,
        )


def compare_copper(
    reference: dict[str, CopperLayer],
    candidate: dict[str, CopperLayer],
    *,
    tolerance: float = REVIEWED_COPPER_TOLERANCE_MM2,
) -> list[dict[str, Any]]:
    """One record per layer, in plot order: its members, digests and two differences."""
    records: list[dict[str, Any]] = []
    for layer in dict.fromkeys([*reference, *candidate]):
        ref = reference.get(layer) or CopperLayer(
            layer, status="unexpected", detail="copper in the reference plot matches no board layer"
        )
        cand = candidate.get(layer) or CopperLayer(
            layer, status="missing", detail="no archive member for this layer"
        )
        record: dict[str, Any] = {
            "layer": layer,
            "reference": ref.identity(),
            "candidate": cand.identity(),
            "added_mm2": None,
            "removed_mm2": None,
        }
        unreadable = [entry for entry in (ref, cand) if entry.status != "read"]
        if unreadable:
            record["status"] = unreadable[0].status
            record["detail"] = "; ".join(
                dict.fromkeys(entry.detail for entry in unreadable if entry.detail)
            )
        else:
            if cand.geometry.equals(ref.geometry):
                added = removed = 0.0
            else:
                added = float(cand.geometry.difference(ref.geometry).area)
                removed = float(ref.geometry.difference(cand.geometry).area)
            record.update(
                added_mm2=added,
                removed_mm2=removed,
                status="changed" if max(added, removed) > tolerance else "identical",
                detail=None,
            )
        records.append(record)
    return records


def reviewed_copper_record(
    *,
    reviewed_pcb: dict[str, str],
    records: list[dict[str, Any]],
    tolerance: float = REVIEWED_COPPER_TOLERANCE_MM2,
) -> dict[str, Any]:
    """The whole comparison: one record per layer, and what it means for publication."""
    changed = [record["layer"] for record in records if record["status"] == "changed"]
    unreadable = [
        record["layer"] for record in records if record["status"] not in {"identical", "changed"}
    ]
    status = "unreadable" if unreadable else "changed" if changed else "identical"
    return {
        "schema": SCHEMA,
        "status": status,
        "reviewed_pcb": dict(reviewed_pcb),
        "tolerance_mm2": tolerance,
        "layers": records,
        "changed_layers": changed,
        "unreadable_layers": unreadable,
    }


def unreadable_record(
    *, reviewed_pcb: dict[str, str], layers: Sequence[str], side: str, reason: str
) -> dict[str, Any]:
    """Every layer unreadable on one side, for a plot that could not be opened at all."""
    records = [
        {
            "layer": layer,
            "reference": {"member": None, "sha256": None},
            "candidate": {"member": None, "sha256": None},
            "added_mm2": None,
            "removed_mm2": None,
            "status": "unreadable",
            "detail": f"{side} unreadable: {reason}",
        }
        for layer in layers
    ]
    return reviewed_copper_record(reviewed_pcb=reviewed_pcb, records=records)


def _describe(record: dict[str, Any]) -> str:
    if record["status"] in {"identical", "changed"}:
        return f"{record['layer']} +{record['added_mm2']:.4f} / -{record['removed_mm2']:.4f} mm²"
    return f"{record['layer']}: {record['detail']}"


def summary_lines(record: dict[str, Any]) -> list[str]:
    """The comparison as a person reads it, one line per layer."""
    reviewed = record["reviewed_pcb"]
    lines = [f"Reviewed copper -- saved board {reviewed['name']} (sha256 {reviewed['sha256']}):"]
    for layer in record["layers"]:
        if layer["status"] in {"identical", "changed"}:
            lines.append(
                f"  {layer['layer']:<8} {layer['status']:<9} "
                f"+{layer['added_mm2']:.4f} / -{layer['removed_mm2']:.4f} mm² added / removed"
            )
        else:
            lines.append(f"  {layer['layer']:<8} {layer['status']:<11} {layer['detail']}")
    return lines


def preflight_result(record: dict[str, Any]) -> PreflightResult:
    """The comparison as one entry of the package's preflight report."""
    count = len(record["layers"])
    status: Literal["OK", "FAIL"]
    if record["status"] == "identical":
        status = "OK"
        message = f"Reviewed copper: all {count} layer(s) equal the saved board"
    elif record["status"] == "changed":
        status = "FAIL"
        message = (
            f"Reviewed copper: {len(record['changed_layers'])} of {count} layer(s) "
            "differ from the saved board"
        )
    else:
        status = "FAIL"
        message = (
            f"Reviewed copper: the {count} layer(s) could not all be compared with the saved board"
        )
    return PreflightResult(
        name=CHECK_NAME,
        status=status,
        message=message,
        details="; ".join(
            _describe(layer) for layer in record["layers"] if layer["status"] != "identical"
        ),
        data=record,
    )


def refusal_message(record: dict[str, Any], diagnostics: dict[str, str] | None) -> str:
    """Why nothing was published, and where the proposed geometry can be inspected."""
    reviewed = record["reviewed_pcb"]
    findings = "; ".join(
        _describe(layer) for layer in record["layers"] if layer["status"] != "identical"
    )
    if record["status"] == "changed":
        head = (
            f"The copper this export would ship differs from the saved board "
            f"{reviewed['name']} on {len(record['changed_layers'])} layer(s), so the "
            "package was withheld and the checked-out sources were not modified: "
        )
    else:
        head = (
            "The copper this export would ship could not be compared with the saved "
            f"board {reviewed['name']} on every layer, so the package was withheld and "
            "the checked-out sources were not modified: "
        )
    text = head + findings + f" (tolerance {record['tolerance_mm2']:g} mm² per direction)"
    if diagnostics is not None:
        text += (
            f". Proposed geometry to inspect and adopt: {diagnostics['proposed_pcb']}; "
            f"diagnostic report: {diagnostics['report']}"
        )
    return text


# Hidden staging directories ``kct export`` keeps beside a board while it runs.
_STAGE_DIR_RE = re.compile(r"^\..+\.(?:export|reviewed)-")

#: KiCad rewrites project-local state in place beside a board (its
#: ``.kicad_prl``), so the board's own directory is private copies. Larger
#: neighbours there -- archives, models, earlier exports -- are linked: an
#: export never rewrites them and copying them would cost the user disk.
_PRIVATE_COPY_LIMIT_BYTES = 16 * 1024 * 1024


def _replicate(source: Path, target: Path, *, private: bool) -> None:
    if private and source.stat().st_size <= _PRIVATE_COPY_LIMIT_BYTES:
        shutil.copy2(source, target)
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


class ReviewedCopperExport:
    """The scratch a reviewed-copper export works in.

    ``live`` is the checked-out board and ``pcb`` its working copy, where the
    export writes rules, refills zones and plots the candidate. The project
    around the board is mirrored into one hidden directory beside it, with the
    same relative layout, so schematic, spec and library discovery resolve
    exactly as they do for the checked-out board. ``snapshot`` holds the saved
    bytes the reference is plotted from, under the board's own name.

    Nothing here is written back to the checkout. :meth:`close` removes all of
    it, so anything a designer must keep is copied out first
    (:meth:`write_diagnostics`).
    """

    def __init__(self, live: Path, *, project_root: Path | None, output_dir: Path) -> None:
        self.live = live
        self.output_dir = output_dir
        self.pcb_bytes = live.read_bytes()
        self.sha256 = hashlib.sha256(self.pcb_bytes).hexdigest()
        self.board_dir = live.parent.resolve()
        self.root = (project_root or self.board_dir).resolve()
        try:
            self.relative = (self.board_dir / live.name).relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Board {live} is outside the project root {self.root}") from exc
        self.dir = Path(tempfile.mkdtemp(prefix=f".{live.name}.reviewed-", dir=self.board_dir))
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        try:
            self._scratch = tempfile.TemporaryDirectory(prefix="kct-export-reviewed-")
            self.snapshot_dir = Path(self._scratch.name)
            self.snapshot = self.snapshot_dir / live.name
            self.reference_dir = self.snapshot_dir / "reference"
            self.snapshot.write_bytes(self.pcb_bytes)
            self._copy_authored(self.snapshot)
            self._mirror_project()
        except BaseException:
            self.close()
            raise
        self.pcb = self.dir / self.relative

    @property
    def identity(self) -> dict[str, str]:
        """The reviewed input as the manifest names it."""
        return {"name": self.live.name, "sha256": self.sha256}

    def _copy_authored(self, board: Path) -> None:
        """Bring the authored project and rule siblings of ``self.live`` beside ``board``."""
        for suffix in (".kicad_pro", ".kicad_dru"):
            authored = self.live.with_suffix(suffix)
            if authored.is_file():
                shutil.copy2(authored, board.with_suffix(suffix))

    def _mirror_project(self) -> None:
        board = self.dir / self.relative
        prepared = {
            board.name,
            board.with_suffix(".kicad_pro").name,
            board.with_suffix(".kicad_dru").name,
            f".{board.name}.zonefill",
        }
        excluded = set()
        for path in (self.output_dir, self.diagnostics_root):
            with contextlib.suppress(OSError, RuntimeError):
                excluded.add(path.resolve())

        def skip(path: Path) -> bool:
            if (
                path == self.dir
                or _STAGE_DIR_RE.match(path.name)
                or path.name == ".git"
                or path.name.startswith("~")
            ):
                return True
            if path.parent == self.board_dir and path.name in prepared:
                return True
            try:
                return path.resolve() in excluded
            except (OSError, RuntimeError):
                return False

        self._mirror(self.root, self.dir, skip=skip)
        board.parent.mkdir(parents=True, exist_ok=True)
        # The sources the export prepares start as private bytes of the saved ones.
        board.write_bytes(self.pcb_bytes)
        self._copy_authored(board)
        marker = self.live.with_name(f".{self.live.name}.zonefill")
        if marker.is_file():
            shutil.copy2(marker, board.with_name(marker.name))

    def _mirror(self, source: Path, target: Path, *, skip: Callable[[Path], bool]) -> None:
        for entry in sorted(source.iterdir()):
            if skip(entry):
                continue
            copy = target / entry.name
            try:
                if entry.is_symlink():
                    self._mirror_link(entry, copy, private=source == self.board_dir, skip=skip)
                elif entry.is_dir():
                    copy.mkdir()
                    self._mirror(entry, copy, skip=skip)
                elif entry.is_file():
                    _replicate(entry, copy, private=source == self.board_dir)
            except FileNotFoundError:
                # A neighbour that left between listing and copying (an editor's
                # atomic save, a released lock) was never a stable input.
                continue

    def _mirror_link(
        self,
        link: Path,
        target: Path,
        *,
        private: bool,
        skip: Callable[[Path], bool],
    ) -> None:
        """Keep a symlink pointing at its mirrored target when that lies in the project."""
        try:
            resolved = link.resolve()
        except (OSError, RuntimeError):
            resolved = None
        if (
            resolved is not None
            and resolved != self.root
            and resolved.is_relative_to(self.root)
            and not skip(resolved)
        ):
            if resolved.is_dir():
                os.symlink(self.dir / resolved.relative_to(self.root), target)
            elif resolved.is_file():
                _replicate(resolved, target, private=private)
            else:
                os.symlink(os.readlink(link), target)
        else:
            # Outside the project, or dangling. A relative target resolves from
            # the deeper working directory, so point at the resolved target
            # when it exists; a dangling link reads the same either way.
            literal = os.readlink(link)
            if resolved is not None and not os.path.isabs(literal) and resolved.exists():
                literal = str(resolved)
            os.symlink(literal, target)

    def rebase(self, path: Path | None) -> Path | None:
        """The working-copy path of a live file inside the project, else the path itself."""
        if path is None:
            return None
        try:
            return self.dir / path.resolve().relative_to(self.root)
        except (OSError, RuntimeError, ValueError):
            return path

    def source_unchanged(self) -> bool:
        """Whether the checked-out board still holds the bytes that were reviewed."""
        try:
            return self.live.read_bytes() == self.pcb_bytes
        except OSError:
            return False

    @property
    def diagnostics_root(self) -> Path:
        """Where refused exports keep their evidence: beside the package, never in it."""
        output = self.output_dir
        return output.parent / f"{output.name or 'kicad-export'}-reviewed-copper"

    def write_diagnostics(self, record: dict[str, Any], candidate_zip: Path) -> dict[str, str]:
        """Copy a refusal's evidence out of scratch before the scratch is removed.

        Each attempt gets its own directory: nothing of an earlier refusal is
        replaced, and nothing of the package is touched.
        """
        root = self.diagnostics_root
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        attempt = Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=root))
        proposed = attempt / "proposed"
        proposed.mkdir()
        board = self.pcb
        # The candidate was plotted from a temporary fill of any outlined zone
        # that has none saved, so the board offered for adoption is given that
        # same fill: otherwise adopting it would not reproduce the candidate.
        from ..cli.runner import run_fill_zones
        from .gerber import _pcb_has_unfilled_zones

        if _pcb_has_unfilled_zones(board):
            with tempfile.TemporaryDirectory(prefix="kct-export-reviewed-fill-") as scratch:
                filled = Path(scratch) / board.name
                result = run_fill_zones(board, output_path=filled)
                if result.success and filled.is_file():
                    shutil.copy2(filled, proposed / board.name)
                else:
                    logger.warning(
                        "Reviewed copper: the proposed %s keeps its unfilled zones (%s)",
                        board.name,
                        result.stderr.strip() or "the fill did not save a board",
                    )
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            source = board.with_suffix(suffix)
            if source.is_file() and not (proposed / source.name).exists():
                shutil.copy2(source, proposed / source.name)
        candidate = attempt / candidate_zip.name
        shutil.copy2(candidate_zip, candidate)
        reference = attempt / "reference"
        shutil.copytree(self.reference_dir, reference)
        paths = {
            "directory": str(attempt),
            "report": str(attempt / "reviewed-copper.json"),
            "text": str(attempt / "reviewed-copper.txt"),
            "proposed_pcb": str(proposed / self.pcb.name),
            "candidate_gerbers": str(candidate),
            "reference_gerbers": str(reference),
        }
        (attempt / "reviewed-copper.json").write_text(
            json.dumps({**record, "diagnostics": paths}, indent=2) + "\n", encoding="utf-8"
        )
        (attempt / "reviewed-copper.txt").write_text(
            "\n".join(summary_lines(record)) + "\n", encoding="utf-8"
        )
        return paths

    def close(self) -> None:
        """Remove the working copy and the snapshot; safe to call more than once."""
        shutil.rmtree(self.dir, ignore_errors=True)
        if self._scratch is not None:
            self._scratch.cleanup()
