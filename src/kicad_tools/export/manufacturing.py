"""
Manufacturing package generator.

Creates complete manufacturing packages including BOM, CPL, Gerbers,
KiCad project ZIP, and a manifest with SHA256 checksums.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import kicad_tools
from kicad_tools.core.atomic_write import SourceChangedError, checked_write_text
from kicad_tools.core.package_publication import (
    PublicationRegion,
    is_publication_hold,
    publish_regions,
)
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import KiCadToolsError
from kicad_tools.parts.lcsc import LCSCDependencyMissingError
from kicad_tools.sexp import parse_file

from .assembly import AssemblyConfig, AssemblyPackage, AssemblyPackageResult
from .preflight import (
    PreflightChecker,
    PreflightConfig,
    PreflightResult,
    report_source_mismatch,
)
from .rotation_overrides import placement_preview_instructions

if TYPE_CHECKING:
    from pydantic_core import ErrorDetails

    from ..spec.schema import PurchasingEntry

logger = logging.getLogger(__name__)

#: Name of the supplementary purchasing list inside a bundle (Issue #10431).
#: Reserved rather than configurable: the submission handoff finds the list by
#: this name (``submission_plan.PURCHASING_FILENAME``), so the two must agree.
PURCHASING_FILENAME = "purchasing.json"


@dataclass
class ManufacturingConfig(AssemblyConfig):
    """Configuration for manufacturing package generation.

    ``via_land_overlap`` opts the export into the ``via_land_overlap`` preflight
    measurement of the shipped archive (``kct export --via-land-overlap``, Issue
    #10357); it is off by default.
    """

    # Report settings
    include_report: bool = True

    # KiCad project ZIP settings
    include_project_zip: bool = True
    project_zip_name: str = "kicad_project.zip"

    # Manifest settings
    stackup_id: str | None = None
    include_manifest: bool = True
    manifest_name: str = "manifest.json"

    # Pre-flight validation settings
    preflight: PreflightConfig | None = None

    # When True, preflight FAIL results block export (early return, no files).
    # When False (default), preflight failures are recorded as warnings but
    # export proceeds to generate output files.
    strict_preflight: bool = False

    # When True (default), the export refuses to proceed when the BOM
    # contains references with no matching PCB footprint -- the package
    # would be unbuildable because the fab cannot place components that
    # are not on the board.  This gates only the ``bom_pcb_match``
    # preflight check independently of ``strict_preflight``; users who
    # explicitly want to ship a partial package can opt out via
    # ``--allow-unbuildable-bom`` (which sets this to False).
    block_on_unbuildable_bom: bool = True

    # When True (default), flatten the latest vN/ report directory into a
    # ``report/`` subdirectory and remove all vN/ directories from the output.
    # When False, versioned directories are preserved as-is.
    latest_report_only: bool = True

    # When True, preserve intermediate report build files (markdown, figures,
    # JSON data snapshots) in a .build/report/ subdirectory.  Default False
    # means only report.pdf (or report.md) is kept at the package root.
    keep_build_artifacts: bool = False

    # When True (default), generate a README.txt explaining the output files.
    include_readme: bool = True

    # When True (default), emit a sibling ``<board>.kicad_pro`` (and
    # ``.kicad_dru``) next to the source PCB populated from the target
    # manufacturer profile, so ``kicad-cli pcb drc`` loads the fab's
    # relaxed built-in minimums instead of KiCad's stricter defaults.
    emit_drc_constraints: bool = True

    # When True (default), the emitted ``.kicad_dru`` carries per-net ampacity
    # ``track_width`` floors from the net-class-map sidecar.  False
    # (``--no-ampacity-rules``) emits none (#8612).
    emit_ampacity_rules: bool = True

    # Reviewed design rules the report's DRC/compatibility sections audit
    # under, instead of the manufacturer profile's stock floors (Issue #9536).
    # A board whose reviewed fabrication process the generic tier cannot
    # model (board 04's paid 0.15 mm drill, #6269) reports READY/NOT_READY
    # against the process it will actually be ordered with -- the same
    # override its own gating checker applies.  ``None`` (default) keeps the
    # profile rules.
    report_design_rules: Any | None = None

    # When True (default), run the shared silk legalisation pass
    # (``legalize_silk``, #9795 phase 1) on the board before preflight and
    # plotting judge silk, so checks, gerbers and hashes all read the same
    # repaired bytes (Issue #9799).  ``False`` (``--no-silk-fix``) leaves
    # authored silk byte-identical.
    silk_fix: bool = True
    # Readiness resolves the source boundary before it stages the editable project.
    project_root: Path | None = None

    via_land_overlap: bool = False


@dataclass
class ManufacturingResult:
    """Result of manufacturing package generation."""

    output_dir: Path
    assembly_result: AssemblyPackageResult | None = None
    report_path: Path | None = None
    report_md_path: Path | None = None
    project_zip_path: Path | None = None
    stackup_path: Path | None = None
    manifest_path: Path | None = None
    readme_path: Path | None = None
    image_paths: list[Path] = field(default_factory=list)
    # Sibling DRC-constraint sources (.kicad_pro / .kicad_dru) written next
    # to the source board so kicad-cli loads the manufacturer profile.
    drc_constraint_paths: list[Path] = field(default_factory=list)
    preflight_results: list[PreflightResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Issue #5009: the selected manufacturer profile's declared
    # via-in-pad ``FabricationProcess`` (see
    # ``kicad_tools.manufacturers.fabrication_process.describe_selection``),
    # resolved for this board's actual layer/copper configuration.  ``None``
    # when the profile declares no eligible process for this configuration
    # (the common case -- most manufacturer profiles never set
    # ``via_in_pad_process_id``).  Bound into ``manifest.json`` and
    # ``README.txt`` so the machine-readable requirements and their
    # human-readable ordering instructions travel with the bundle rather
    # than requiring per-board hand-typed prose.
    fabrication_process: dict | None = None
    # Issue #9799: the default silk legalisation pass outcome (or ``None``
    # when it did not run / no profile resolved).
    silk_fix: dict | None = None
    # Issue #7209: outer/inner copper weight the order requests, derived from the
    # board stackup (see ``preflight.resolve_ordered_copper``).
    ordered_copper: dict | None = None
    # Issue #10109: footprint-to-symbol instance link report for the
    # editable-project handoff (``to_dict()`` of
    # :class:`kicad_tools.sync.instance_links.InstanceLinkReport`), or
    # ``None`` when no project ZIP was produced or no schematic could be
    # compared against.  Read-only: broken links are surfaced as warnings,
    # never repaired here.
    instance_links: dict | None = None
    # Issue #10468: externally produced simulation evidence for the declared
    # cases (``SimulationEvidence.to_dict()``), or ``None`` when the board
    # declares none.  Reported independently of native DRC.
    simulation: dict | None = None
    simulation_paths: list[Path] = field(default_factory=list)
    simulation_evidence: Any | None = field(default=None, repr=False)
    # Issue #10422: nominal reinforcement-clearance measurements of the
    # shipped Gerber archive (``to_dict()`` of
    # :class:`~kicad_tools.export.reinforcement_clearance.ReinforcementClearanceReport`),
    # or ``None`` when the board has no reinforcement sidecar.
    reinforcement_clearance: dict | None = None
    # Issue #10431: the supplementary per-board purchasing list staged with
    # the bundle, or ``None`` when the project declares none.  It addresses
    # the buyer, never the fabricator: nothing in it becomes a BOM or CPL
    # row, a placement, a factory match or a stock request.
    purchasing_path: Path | None = None
    # Descriptions of the purchasing rows whose part is not selected yet.
    purchasing_pending: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Check if generation was successful (no errors)."""
        return len(self.errors) == 0

    def bundle_relpath(self, path: Path) -> str:
        """Return ``path`` relative to the bundle root, for display.

        The Gerber archive lives at ``gerbers/gerbers.zip``; printing only
        ``path.name`` told the reader to look for a top-level
        ``gerbers.zip`` that does not exist (issue #7153).  Falls back to
        the bare file name for a path outside :attr:`output_dir`.
        """
        try:
            return Path(path).resolve().relative_to(Path(self.output_dir).resolve()).as_posix()
        except ValueError:
            return Path(path).name

    @property
    def all_files(self) -> list[Path]:
        """Return list of all generated files."""
        files: list[Path] = []
        if self.assembly_result:
            if self.assembly_result.bom_path:
                files.append(self.assembly_result.bom_path)
            if self.assembly_result.pnp_path:
                files.append(self.assembly_result.pnp_path)
            if self.assembly_result.gerber_path:
                files.append(self.assembly_result.gerber_path)
        if self.purchasing_path:
            files.append(self.purchasing_path)
        if self.report_path:
            files.append(self.report_path)
        if self.report_md_path:
            files.append(self.report_md_path)
        files.extend(self.image_paths)
        files.extend(self.simulation_paths)
        if self.project_zip_path:
            files.append(self.project_zip_path)
        if self.readme_path:
            files.append(self.readme_path)
        if self.stackup_path:
            files.append(self.stackup_path)
        if self.manifest_path:
            files.append(self.manifest_path)
        return files

    def __str__(self) -> str:
        lines = [f"Manufacturing Package: {self.output_dir}"]
        if self.assembly_result:
            if self.assembly_result.bom_path:
                lines.append(f"  BOM: {self.assembly_result.bom_path.name}")
            if self.assembly_result.pnp_path:
                lines.append(f"  CPL: {self.assembly_result.pnp_path.name}")
            if self.assembly_result.gerber_path:
                lines.append(f"  Gerbers: {self.bundle_relpath(self.assembly_result.gerber_path)}")
        if self.purchasing_path:
            lines.append(f"  Purchasing: {self.bundle_relpath(self.purchasing_path)}")
            if self.purchasing_pending:
                lines.append(
                    f"    {len(self.purchasing_pending)} selection(s) pending: "
                    + "; ".join(self.purchasing_pending)
                )
        if self.report_path:
            lines.append(f"  Report: {self.report_path.name}")
        if self.image_paths:
            lines.append(f"  Images: {len(self.image_paths)} file(s)")
        if self.project_zip_path:
            lines.append(f"  Project ZIP: {self.project_zip_path.name}")
        if self.readme_path:
            lines.append(f"  README: {self.readme_path.name}")
        if self.manifest_path:
            lines.append(f"  Manifest: {self.manifest_path.name}")
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
            for err in self.errors:
                lines.append(f"    - {err}")
        return "\n".join(lines)


class _PackageOutputs:
    """Map this export's generated files between a private candidate and their public paths."""

    bundle_key = Path("package")

    def __init__(
        self, out_dir: Path, config: ManufacturingConfig, manufacturer: str, *, purchasing: bool
    ) -> None:
        from kicad_tools.manufacturers import get_fab_family

        config = copy(config)
        family = get_fab_family(manufacturer)
        bom = config.bom_filename.format(manufacturer=family)
        configured = {}
        if config.include_project_zip:
            configured["project_zip_name"] = config.project_zip_name
        if config.include_bom:
            configured["bom_filename"] = bom
        if config.include_pnp:
            configured["pnp_filename"] = config.pnp_filename.format(manufacturer=family)
        if config.include_gerbers:
            configured["gerbers_subdir"] = config.gerbers_subdir
        if config.include_manifest:
            configured["manifest_name"] = config.manifest_name
        # CLI paths live in the bundle. The Python API also allows a member on
        # another volume. Give each root a directory in the private candidate.
        bundle_key = self.bundle_key
        live_bundle = out_dir.resolve()
        roots = {bundle_key.name: live_bundle}
        targets = {}
        for field_name, name in configured.items():
            path = live_bundle / name
            targets[field_name] = path.parent.resolve() / path.name
        if purchasing:
            for field_name, target in targets.items():
                resolved = target.resolve()
                # Bundle member names travel between case-sensitive and
                # case-insensitive filesystems, including the buyer's machine.
                if resolved.is_relative_to(live_bundle):
                    parts = resolved.relative_to(live_bundle).parts
                elif config.include_manifest and field_name in {
                    "bom_filename",
                    "pnp_filename",
                    "project_zip_name",
                }:
                    # The manifest records external files by basename.
                    parts = (target.name,)
                else:
                    continue
                if parts and parts[0].casefold() == PURCHASING_FILENAME:
                    raise ValueError(
                        f"{field_name} conflicts with the purchasing list at {live_bundle / PURCHASING_FILENAME}"
                    )
        # Nested external outputs share one staging subtree, just as they do
        # on disk. Package paths always retain their bundle-relative names.
        parents = sorted(
            {p.parent for p in targets.values() if not p.is_relative_to(live_bundle)},
            key=lambda p: (len(p.parts), str(p)),
        )
        for parent in parents:
            if not any(parent.is_relative_to(previous) for previous in roots.values()):
                roots[f"external-{len(roots)}"] = parent
        members: set[Path] = set()
        for field_name, target in targets.items():
            root_name, parent = next(
                (n, path) for n, path in roots.items() if target.is_relative_to(path)
            )
            key = Path(root_name) / target.relative_to(parent)
            members.add(key)
            # Keep ordinary templates and the existing bundle-relative path spelling.
            relative = os.path.relpath(key, bundle_key)
            if Path(relative) != Path(configured[field_name]):
                setattr(config, field_name, relative)
        if purchasing:
            members.add(bundle_key / PURCHASING_FILENAME)
        if config.include_readme:
            members.add(bundle_key / "README.txt")
        members.add(bundle_key / "simulation")
        if config.stackup_id:
            members.add(bundle_key / "stackup-ordering.json")
        if config.include_report:
            members.update(
                bundle_key / name
                for name in ("report.pdf", "report.md", "report", "images", ".build/report")
            )

        self.out_dir = out_dir
        self.members = members
        self.roots = roots
        # A previous BOM supplies the user's LCSC assignments. Its real path
        # is also recorded in the README and in the returned merge report.
        if config.merge_lcsc and config.merge_lcsc_from is None and (out_dir / bom).is_file():
            config.merge_lcsc_from = out_dir / bom
        self.config = config

    def live_path(self, key: str | Path) -> Path:
        top, *rest = Path(key).parts
        root = self.out_dir if top == self.bundle_key.name else self.roots[top]
        return root.joinpath(*rest)

    def paths(self, root: Path) -> set[Path]:
        entries: set[Path] = set()
        bundle = self.out_dir if root == self.out_dir else root / self.bundle_key
        versions = {
            self.bundle_key / p.name
            for p in bundle.glob("v[0-9]*")
            if self.config.include_report and p.is_dir() and p.name[1:].isdigit()
        }
        for name in self.members | versions:
            path = self.live_path(name) if root == self.out_dir else root / name
            if path.is_dir() and path.is_symlink():
                raise OSError(f"Cannot stage package directory symlink: {path}")
            if path.exists():
                entries.add(name)
            if path.is_dir():
                for item in path.rglob("*"):
                    if is_publication_hold(item.relative_to(path)):
                        continue
                    if item.is_dir() and item.is_symlink():
                        raise OSError(f"Cannot stage package directory symlink: {item}")
                    entries.add(name / item.relative_to(path))
        return entries

    def inventory(self, root: Path, held_dirs: set[Path] | None = None) -> dict[str, str]:
        snapshot = {}
        for key in self.paths(root):
            path = self.live_path(key) if root == self.out_dir else root / key
            if (not held_dirs or path.parent not in held_dirs) and path.is_file():
                snapshot[key.as_posix()] = _sha256_file(path)
        return snapshot

    def seed(self, candidate: Path) -> tuple[set[Path], dict[str, str]]:
        """Copy existing export inputs and report versions without following directory links."""
        for member in self.members:
            if self.live_path(member).parent.is_dir():
                (candidate / member).parent.mkdir(parents=True, exist_ok=True)
        existing = self.paths(self.out_dir)
        before = self.inventory(self.out_dir)
        for name in existing:
            source, target = self.live_path(name), candidate / name
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        if self.inventory(candidate) != before:
            raise RuntimeError("Package changed while preparing the export candidate")

        return existing, before

    def publish(self, candidate: Path, existing: set[Path], before: dict[str, str]) -> None:
        manifest = os.path.normpath(self.bundle_key / self.config.manifest_name)
        obsolete = sorted(existing - self.paths(candidate), key=lambda p: -len(p.parts))
        publish_regions(
            [PublicationRegion(candidate, self.out_dir, before, self.live_path)],
            self.inventory,
            publish_last=self.live_path(manifest),
            remove_empty_directories=[
                self.live_path(name) for name in obsolete if self.live_path(name).is_dir()
            ],
        )


def _now_utc() -> datetime:
    """Current UTC time, anchored to ``SOURCE_DATE_EPOCH`` when set (Issue #9536).

    The manifest ``generated_at``, the README ``Generated at`` stamp and the
    report date all flow through here, so a repeated export with the same
    inputs and the same ``SOURCE_DATE_EPOCH`` -- the reproducible-builds
    convention weasyprint already honours for PDF metadata -- writes
    byte-identical evidence instead of wall-clock-drifting bundles.
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if epoch.isdigit():
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest(manifest_path: str | Path) -> list[str]:
    """Verify a manufacturing bundle's manifest.json against files on disk.

    Recomputes the SHA256 checksum and size of every file listed in the
    manifest and compares them to the recorded values.  Files are looked
    up as ``bundle_dir / key``, which resolves every entry of a manifest
    written by a current kicad-tools (keys are bundle-relative POSIX
    paths, e.g. ``gerbers/gerbers.zip``).  Entries that aren't found
    there fall back to a recursive search, purely for backward
    compatibility with **legacy** manifests that recorded bare filenames
    even for artifacts living in subdirectories (issue #4590).

    The manifest itself is excluded from verification (its hash can't
    contain itself).

    Args:
        manifest_path: Path to a ``manifest.json`` file.

    Returns:
        A list of human-readable mismatch descriptions.  Empty list
        means every listed file exists and matches its recorded
        checksum and size.

    This is the integrity check behind issue #3529: ``kct export`` used
    to write BOM/CPL CSVs with CRLF line endings and hash that content,
    while git normalization stored LF -- so a fresh checkout failed
    ``sha256sum`` verification against the manifest.
    """
    manifest_path = Path(manifest_path)
    bundle_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())

    problems: list[str] = []
    for name, info in manifest.get("files", {}).items():
        if name == manifest_path.name:
            continue
        fpath = bundle_dir / name
        if not fpath.exists():
            # LEGACY-ONLY fallback (issue #4590): manifests written
            # before the bundle-relative key fix stored bare filenames
            # for artifacts that live in subdirectories of the bundle
            # (e.g. "gerbers.zip" for gerbers/gerbers.zip).  Current
            # manifests always resolve via bundle_dir / name above; keep
            # this path so already-shipped bundles still verify without
            # being regenerated.
            candidates = sorted(bundle_dir.rglob(name))
            if not candidates:
                problems.append(f"{name}: listed in manifest but not found on disk")
                continue
            fpath = candidates[0]

        actual_sha = _sha256_file(fpath)
        actual_size = fpath.stat().st_size
        expected_sha = info.get("sha256")
        expected_size = info.get("size")
        if expected_sha is not None and actual_sha != expected_sha:
            problems.append(f"{name}: sha256 mismatch (manifest {expected_sha}, disk {actual_sha})")
        if expected_size is not None and actual_size != expected_size:
            problems.append(f"{name}: size mismatch (manifest {expected_size}, disk {actual_size})")

    return problems


def _zone_fill_rule_state(pcb_path: Path) -> str:
    """Fingerprint of the DRC rule sources KiCad's zone filler honours.

    KiCad resolves every gap of a pour against the sibling ``.kicad_dru``
    and the project's clearance-bearing settings -- ``board.design_settings.rules``
    and ``net_settings`` -- so a change to any of them invalidates a fill that
    was saved earlier.  The rest of ``.kicad_pro`` (``meta`` stamps, rule
    severities, schematic settings, formatting) leaves the fill alone and is
    left out, so re-emitting an unchanged rule set reads as unchanged.

    An absent or unparsable sidecar contributes ``None``.
    """
    try:
        dru: str | None = pcb_path.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    except OSError:
        dru = None
    rules: object = None
    net_settings: object = None
    try:
        project = json.loads(pcb_path.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        project = None
    if isinstance(project, dict):
        board = project.get("board")
        settings = board.get("design_settings") if isinstance(board, dict) else None
        if isinstance(settings, dict):
            rules = settings.get("rules")
        net_settings = project.get("net_settings")
    return json.dumps({"dru": dru, "rules": rules, "net_settings": net_settings}, sort_keys=True)


def _saved_zone_fills(pcb_path: Path) -> dict[str, list[tuple]]:
    """Each zone's saved ``filled_polygon`` geometry, keyed by zone uuid.

    Coordinates are rounded to 1 nm so KiCad's own reserialization of an
    identical fill compares equal.  Raises ``OSError``/``ValueError`` when the
    board cannot be read or parsed.
    """
    from ..sexp import parse_string

    root = parse_string(pcb_path.read_text(encoding="utf-8"))
    fills: dict[str, list[tuple]] = {}
    for index, zone in enumerate(root.find_all("zone")):
        uuid_node = zone.find_child("uuid")
        key = str(uuid_node.get_value(0)) if uuid_node is not None else f"#{index}"
        polygons = []
        for poly in zone.find_children("filled_polygon"):
            layer_node = poly.find_child("layer")
            layer = str(layer_node.get_value(0)) if layer_node is not None else ""
            points = tuple(
                (round(float(xy.get_float(0) or 0), 6), round(float(xy.get_float(1) or 0), 6))
                for pts in poly.find_children("pts")
                for xy in pts.find_children("xy")
            )
            polygons.append((layer, points))
        fills[key] = sorted(polygons)
    return fills


@contextmanager
def _zone_fill_copy(pcb_path: Path, board_bytes: bytes | None = None) -> Iterator[Path]:
    """Stage a saved PCB under its original name alongside its native fill rules."""
    with tempfile.TemporaryDirectory(prefix="kct-export-zone-refill-") as tmp:
        scratch = Path(tmp) / pcb_path.name
        if board_bytes is None:
            shutil.copy2(pcb_path, scratch)
        else:
            scratch.write_bytes(board_bytes)
        for suffix in (".kicad_dru", ".kicad_pro"):
            sidecar = pcb_path.with_suffix(suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, scratch.with_suffix(suffix))
        yield scratch


def _saved_zone_fill_matches_native_refill(pcb_path: Path, kicad_cli: Path) -> bool:
    """Whether the saved pours equal what KiCad fills under the rules now present.

    A clean saved-board DRC cannot prove this: copper left farther from foreign
    pads than a stricter rule would fill passes every clearance check yet plots
    differently. The only authoritative comparison is a native fill, run on a
    scratch copy of the board and its rule sources so ``pcb_path`` is never
    touched. Any failure reads as "does not match".

    Two native pipelines produce shipped fills: ``kct export``'s own
    ``--refill-zones`` and the ``run_fill_zones`` pass ``kct route`` finishes
    with, which also remediates starved thermals and so saves different copper
    under the same rules. A saved fill equal to either is current; one equal
    to neither was filled under other rules (or other copper) and is stale.
    """
    from ..cli.runner import run_fill_zones, run_refill_zones

    def native(fill) -> dict[str, list[tuple]] | None:
        with _zone_fill_copy(pcb_path) as scratch:
            if not fill(scratch).success:
                return None
            return _saved_zone_fills(scratch)

    try:
        saved = _saved_zone_fills(pcb_path)
        if not saved:
            return False
        refilled = native(lambda p: run_refill_zones(p, kicad_cli, require_save=True))
        if refilled is None:
            return False
        if saved == refilled:
            return True
        filled = native(lambda p: run_fill_zones(p, kicad_cli=kicad_cli))
        return filled is not None and saved == filled
    except (OSError, ValueError):
        return False


def _restore_board(pcb_path: Path, original: bytes) -> None:
    """Put ``original`` back when a failed zone refill left the board different.

    Issue #6724: a kicad-cli killed mid ``--save-board`` can leave a
    truncated board behind.  An untouched board is not rewritten, so its
    mtime still says when it was last saved.
    """
    try:
        if pcb_path.read_bytes() == original:
            return
    except OSError:
        pass
    try:
        pcb_path.write_bytes(original)
    except OSError as exc:
        logger.error("Could not restore %s after a failed zone refill: %s", pcb_path, exc)


def _project_symbol_files(project_dir: Path) -> list[Path]:
    """Collect portable symbol-table dependencies before writing an archive.

    Keep lexical paths for archive names, but resolve symlinks when checking
    containment. Never expand host environment variables or copy global libraries.
    """
    table_path = project_dir / "sym-lib-table"
    if not table_path.exists() and not table_path.is_symlink():
        return []
    root = project_dir.resolve()
    if not table_path.resolve().is_relative_to(root):
        raise ValueError("Nonportable sym-lib-table: table resolves outside the project")
    try:
        table = parse_file(table_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read sym-lib-table: {exc}") from exc
    if table.name != "sym_lib_table":
        raise ValueError("Malformed sym-lib-table: expected sym_lib_table root")

    files = [table_path]
    for lib in table.find_all("lib"):
        name_node = lib.get("name")
        name = name_node.get_first_atom() if name_node else "<unnamed>"
        uri_node = lib.get("uri")
        uri = uri_node.get_first_atom() if uri_node else None
        type_node = lib.get("type")
        if type_node is None or type_node.get_first_atom() != "KiCad":
            raise ValueError(f"Nonportable symbol library {name!r}: expected KiCad library type")
        if not isinstance(uri, str) or not uri:
            raise ValueError(f"Malformed sym-lib-table: library {name!r} has no URI")
        relative_uri = uri.removeprefix("${KIPRJMOD}/")
        relative = Path(relative_uri)
        if (
            "$" in relative_uri
            or "\\" in relative_uri
            or ":" in relative_uri
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix != ".kicad_sym"
        ):
            raise ValueError(f"Nonportable symbol library {name!r}: {uri!r}")
        target = project_dir / relative
        if not target.resolve().is_relative_to(root):
            raise ValueError(
                f"Nonportable symbol library {name!r}: {uri!r} resolves outside project"
            )
        if not target.is_file():
            raise FileNotFoundError(f"Missing project symbol library {name!r}: {uri!r}")
        files.append(target)
    return files


def project_library_members(directory: Path, boundary: Path) -> list[Path]:
    """Files under a library directory that stay inside *boundary* (#8763).

    Shared by the project-zip exporter and ``kct readiness`` so both agree on
    which library members ship.  A member (or a symlinked directory holding it)
    whose real path leaves *boundary* is excluded with a warning naming it;
    in-boundary symlinks are kept.  Broken links and symlink loops are skipped.
    """
    boundary = boundary.resolve()
    members: list[Path] = []
    for member in sorted(directory.rglob("*")):
        try:
            if not member.is_file():
                continue
            inside = member.resolve().is_relative_to(boundary)
        except (OSError, RuntimeError):  # symlink loop / unreadable
            logger.warning("Excluding unresolvable library member from project zip: %s", member)
            continue
        if inside:
            members.append(member)
        else:
            logger.warning(
                "Excluding library member resolving outside the project: %s -> %s",
                member,
                member.resolve(),
            )
    return members


def _project_footprint_files(project_dir: Path) -> list[Path]:
    """Project-local footprint library inventory (#8727).

    ``fp-lib-table``, ``*.pretty`` / ``footprints`` / ``symbols`` directories and
    the in-project ``${KIPRJMOD}`` (or relative) libraries the table references.
    This mirrors the footprint half of ``kct readiness``'s dependency inventory
    so ``kct export`` writes every file readiness requires of
    ``kicad_project.zip``.  References leaving *project_dir* are skipped here
    (archive names are project-relative); readiness collects those itself in
    ``readiness_cmd._normalize_project_archive`` (Issue #8752).
    """
    root = project_dir.resolve()
    files: set[Path] = set()
    table = project_dir / "fp-lib-table"
    if table.is_file():
        files.add(table)
    for directory in project_dir.iterdir():
        if directory.is_dir() and (
            directory.name in {"footprints", "symbols"} or directory.suffix == ".pretty"
        ):
            files.update(project_library_members(directory, root))
    if table.is_file():
        for uri in re.findall(r'\(uri\s+"([^"\n]+)"\)', table.read_text(errors="replace")):
            if "${KIPRJMOD}" in uri:
                dependency = Path(uri.replace("${KIPRJMOD}", str(root)))
            elif not Path(uri).is_absolute() and "$" not in uri:
                dependency = root / uri
            else:
                continue  # installed KiCad libraries are not project files
            dependency = Path(os.path.normpath(dependency))
            if not dependency.exists() or not dependency.resolve().is_relative_to(root):
                continue
            if dependency.is_dir():
                files.update(project_library_members(dependency, root))
            else:
                files.add(dependency)
    return sorted(files)


def _create_project_zip(
    pcb_path: Path,
    output_dir: Path,
    zip_name: str = "kicad_project.zip",
    schematic_path: Path | None = None,
    *,
    project_root: Path | None = None,
) -> Path:
    """Create a ZIP containing the KiCad project files.

    Project inventory matches ``kct readiness`` (Issue #8752): only the
    ``.kicad_pro`` named like the PCB and the root schematic's sub-sheet
    hierarchy; sibling boards' project files and ``*_backup_*`` are excluded.
    Libraries outside the PCB directory (``${KIPRJMOD}/../``) are not
    collected here: ``readiness_cmd._normalize_project_archive`` owns them.

    Includes only the specific PCB being exported and its sibling .kicad_dru,
    plus .kicad_sch and .kicad_pro files and project-local symbol-table dependencies.
    Other .kicad_pcb variants (intermediate builds,
    working copies, etc.) and backup files are excluded to keep the
    manufacturing package clean.

    The board, its rules, the project file and the root sheet are delivered
    under one basename -- the one :mod:`kicad_tools.export.project_identity`
    picks -- so KiCad opens them as one project (Issue #10108). The ``VERSION.md``
    kept beside the board travels too, and one describing any renaming is
    written when the checked names differ.
    """
    from kicad_tools.export.project_identity import (
        ProjectIdentityError,
        archive_layout,
        project_identity,
    )

    # Resolve once: table-derived members are built from the resolved root, so
    # every member (and its archive name) must share that spelling.  An
    # unresolved/relative/symlinked project_dir (e.g. macOS /var -> /private/var
    # TMPDIR) otherwise breaks ``relative_to`` below.
    project_dir = pcb_path.parent.resolve()
    boundary = project_root.resolve() if project_root is not None else project_dir
    if not project_dir.is_relative_to(boundary):
        raise ProjectIdentityError(
            f"Board {pcb_path} is outside the selected project root {boundary}"
        )
    pcb_path = project_dir / pcb_path.name
    zip_path = output_dir / zip_name

    # Always include the specific PCB being exported
    project_files: list[Path] = []
    if pcb_path.exists():
        project_files.append(pcb_path)
        # Keep the rules KiCad loads for this exact (possibly renamed) board.
        # Their bytes are bound by the project ZIP's manufacturing manifest hash;
        # source-side paths must not become nonportable manifest entries.
        dru_path = pcb_path.with_suffix(".kicad_dru")
        if dru_path.is_file():
            project_files.append(dru_path)

    # Project file of this board only (sibling variants are not dependencies)
    project_files.extend(
        f
        for f in project_dir.iterdir()
        if f.is_file() and f.suffix == ".kicad_pro" and f.stem == pcb_path.stem
    )

    # Schematics: the root's hierarchy.  An explicit root wins; an absent or
    # ambiguous root contributes no schematics (never "zip everything").
    from kicad_tools.report.utils import find_schematic, walk_schematic_hierarchy

    root_sch = Path(schematic_path) if schematic_path else find_schematic(pcb_path)
    walked_sheets: list[Path] = []
    if root_sch is not None and root_sch.is_file():
        walked = walk_schematic_hierarchy(root_sch, lambda sheet: sheet.is_relative_to(boundary))
        project_files.extend(walked.sheets)
        walked_sheets = walked.sheets
        if schematic_path is not None and root_sch.resolve() not in walked_sheets:
            raise ProjectIdentityError(
                f"Cannot pair {root_sch} with {pcb_path}: put the root sheet beside the board "
                "and preserve its sheet references, or run kct readiness with --sch and "
                "--project-root pointing to their common project directory"
            )
        incomplete = [*walked.outside, *walked.missing]
        if schematic_path is not None and incomplete:
            # A ZIP missing part of the selected hierarchy opens in KiCad with
            # an empty/partial BOM and parity gaps, so never report it as a
            # successful export (Issue #10108).  An auto-discovered root keeps
            # the Issue #8752 behaviour: skip with a warning below.
            listed = ", ".join(str(sheet) for sheet in incomplete)
            raise ProjectIdentityError(
                f"Cannot bundle the full schematic hierarchy of {root_sch} with {pcb_path}: "
                f"sheet(s) outside the project boundary or missing: {listed}. Keep every "
                "sheet beside the board, or run kct readiness with --sch and --project-root "
                "pointing to their common project directory"
            )
        for sheet in walked.outside:
            logger.warning("Sub-sheet outside the project directory not bundled: %s", sheet)
    else:
        logger.warning(
            "No unambiguous root schematic for %s; project ZIP contains no schematics "
            "(pass --sch to include them)",
            pcb_path.name,
        )

    if not project_files:
        raise FileNotFoundError(f"No KiCad project files found in {project_dir}")

    project_files.extend(_project_symbol_files(project_dir))
    project_files.extend(_project_footprint_files(project_dir))
    project_files = sorted(set(project_files))

    project_file = pcb_path.with_suffix(".kicad_pro")
    archived_root = root_sch.resolve() if root_sch is not None else None
    identity = project_identity(
        pcb_path,
        project_file if project_file in project_files else None,
        archived_root if archived_root in walked_sheets else None,
    )
    archive_base = (
        project_dir
        if all(source.is_relative_to(project_dir) for source in project_files)
        else boundary
    )
    layout = archive_layout(
        identity, project_files, lambda source: source.parent.relative_to(archive_base).as_posix()
    )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for source in project_files:
            real = source.resolve()
            member = layout.names[real]
            if real == identity.project:
                zf.writestr(member, layout.member_bytes(real, real.read_bytes()))
            else:
                zf.write(source, member)
        if layout.record is not None:
            zf.writestr(layout.record_name, layout.record)

    logger.info(f"Created project ZIP: {zip_path} ({len(project_files)} files)")
    return zip_path


def _build_manifest(
    result: ManufacturingResult,
    pcb_path: Path,
    manufacturer: str,
) -> dict:
    """Build the manifest dictionary with SHA256 checksums.

    Files are keyed by their **bundle-relative POSIX path** (relative to
    ``result.output_dir``), so a consumer can resolve every entry with a
    plain ``bundle_dir / key`` -- including artifacts that live in
    subdirectories such as ``gerbers/gerbers.zip`` and ``images/*.png``
    (issue #4590).  ``as_posix()`` keeps the key platform-independent so
    the manifest is byte-identical across hosts (cf. #3529).
    """
    files_info: dict[str, dict] = {}
    bundle_root = result.output_dir.resolve()
    for fpath in result.all_files:
        if fpath.exists():
            try:
                key = fpath.resolve().relative_to(bundle_root).as_posix()
            except ValueError:
                # Artifact outside the bundle (shouldn't happen for
                # all_files) -- degrade to the basename rather than
                # dropping the entry.
                key = fpath.name
            files_info[key] = {
                "sha256": _sha256_file(fpath),
                "size": fpath.stat().st_size,
            }

    manifest = {
        "version": "1.0",
        "kicad_tools_version": kicad_tools.__version__,
        "generated_at": _now_utc().isoformat(),
        "manufacturer": manufacturer,
        "files": files_info,
        "board": {
            "name": pcb_path.stem,
            "pcb_file": pcb_path.name,
        },
    }

    # Include preflight results if available
    if result.preflight_results:
        manifest["preflight"] = [pr.to_dict() for pr in result.preflight_results]

    # Issue #5009: bind the selected via-in-pad fabrication process (if
    # any) into the manifest as a machine-readable artifact, generalizing
    # the ad hoc ``fabrication_overrides`` blocks boards 03/04 previously
    # hand-wrote into their own reports.
    if result.fabrication_process is not None:
        manifest["fabrication_process"] = result.fabrication_process

    if result.ordered_copper is not None:
        manifest["ordered_copper"] = result.ordered_copper

    # Issue #10468: simulation verdicts live in their own field, never inside
    # ``preflight``; the evidence files are hashed in ``files`` above.
    if result.simulation is not None:
        manifest["simulation"] = result.simulation

    return manifest


def _purchasing_document(entries: list[PurchasingEntry]) -> dict[str, Any]:
    """Freeze validated purchasing rows as the per-board list (Issue #10431).

    The submission handoff parses this exact shape (``submission_plan``'s
    ``_purchasing``); the two change together.  A pending row carries its
    note and no part number, so nothing invents one for it.
    """
    from .submission_plan import purchasing_export_digest

    items: list[dict[str, Any]] = []
    for entry in entries:
        row: dict[str, Any] = {
            "description": entry.description,
            "per_board": entry.quantity,
            "refs": list(entry.refs or []),
            "status": "pending" if entry.pending is not None else "selected",
        }
        if entry.pending is not None:
            row["pending"] = entry.pending
        else:
            row["manufacturer"] = entry.manufacturer
            row["mpn"] = entry.mpn
        items.append(row)
    document = {"schema_version": 1, "items": items}
    return {**document, "kct_export_sha256": purchasing_export_digest(document)}


def _previous_purchasing_digest(out_dir: Path, manifest_name: str) -> str | None:
    """Identify the prior export before replacing or withdrawing its list."""
    from .submission_plan import SubmissionError, parse_purchasing_list

    path = out_dir / PURCHASING_FILENAME
    if not path.is_file() or path.is_symlink():
        return None
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    try:
        document = json.loads(data)
    except ValueError:
        document = None
    marked = isinstance(document, dict) and "kct_export_sha256" in document
    entry = None
    if not marked:
        try:
            manifest = json.loads((out_dir / manifest_name).read_bytes())
        except (FileNotFoundError, ValueError):
            manifest = None
        if isinstance(manifest, dict):
            # Release sign-off can reissue the export manifest as a tapeout receipt.
            tapeout = manifest.get("tapeout")
            if manifest.get("kicad_tools_version") or (
                isinstance(tapeout, dict)
                and "board_sha256" in tapeout
                and "readiness_sha256" in tapeout
            ):
                files = manifest.get("files")
                entry = files.get(PURCHASING_FILENAME) if isinstance(files, dict) else None
                if isinstance(entry, dict) and (
                    entry.get("sha256") != digest or entry.get("size") != len(data)
                ):
                    raise ValueError(f"{path} differs from the previous export manifest")
    try:
        rows = parse_purchasing_list(data)
    except SubmissionError as exc:
        if marked:
            raise ValueError(
                f"{path} differs from the previously exported purchasing list"
            ) from exc
        # An older export could use this filename for a BOM or another artifact.
        return None
    if marked:
        # The marker binds the rows. Also preserve byte-level buyer edits,
        # including reformatting, when there is no matching manifest.
        original = {
            "schema_version": 1,
            "items": rows,
            "kct_export_sha256": document["kct_export_sha256"],
        }
        encoded = (json.dumps(original, indent=2, ensure_ascii=False) + "\n").encode()
        if data not in (encoded, encoded.replace(b"\n", b"\r\n")):
            raise ValueError(f"{path} differs from the previously exported purchasing list")
    if not marked and not isinstance(entry, dict):
        raise ValueError(f"Cannot establish ownership of {path}. Move it aside before exporting.")
    return digest


def _spec_fault(error: ErrorDetails, data: Any) -> str:
    """Name one spec validation error the way its author sees the file.

    Pydantic locates a row as ``purchasing.1.quantity``; a person editing
    YAML counts the ``- description:`` entries from one and recognises the
    row by what it says, so a purchasing fault is reported as
    ``purchasing row 2 (<description>) quantity: ...``.
    """
    location = error["loc"]
    name = ".".join(map(str, location))
    if location[:1] == ("purchasing",) and len(location) >= 2 and isinstance(location[1], int):
        index = location[1]
        rows = data.get("purchasing") if isinstance(data, dict) else None
        row = rows[index] if isinstance(rows, list) and index < len(rows) else None
        description = row.get("description") if isinstance(row, dict) else None
        name = f"purchasing row {index + 1}"
        if isinstance(description, str) and description.strip():
            name += f" ({description.strip()})"
        if len(location) > 2:
            name += " " + ".".join(map(str, location[2:]))
    return f"{name}: {error['msg'].removeprefix('Value error, ')}"


class _SidecarRecoveryError(OSError):
    """The filesystem refused rollback; the staging directory must be retained."""


def _withdraw_copy(
    destination: Path, recovery: Path, fd: int, data: bytes, start: int, written: int
) -> None:
    # Inspect the entry actually withdrawn; an intervening save may have
    # replaced the name or written to our inode before our append.
    captured = recovery / "captured"
    try:
        destination.rename(captured)
    except FileNotFoundError:
        return
    restore = captured
    if os.path.samestat(os.fstat(fd), captured.lstat()):
        raw = captured.read_bytes()
        end = start + written
        if raw[start:end] == data[:written]:
            raw = raw[:start] + raw[end:]
        if not raw:
            return
        restore = recovery / "editor"
        restore.write_bytes(raw)
    # If another save already reclaimed the name, leave that version live
    # and keep the captured version in the recovery directory.
    _install_unoccupied(restore, destination)


def _copy_unoccupied(source: Path, destination: Path) -> os.stat_result:
    """Reserve a name on linkless FAT and never overwrite an intervening save."""
    data = source.read_bytes()
    recovery = Path(tempfile.mkdtemp(prefix="copy-rollback-", dir=source.parent))
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(destination, flags, source.stat().st_mode & 0o777)
    with os.fdopen(fd, "r+b", buffering=0) as stream:
        start = written = 0
        try:
            # One append to the exclusively created file. If an editor got to
            # this new inode first, its bytes precede ours and remain recoverable.
            written = os.write(fd, data)
            start = stream.tell() - written
            if start:
                raise SourceChangedError(f"{destination.name} changed during manufacturing export")
            if written != len(data):
                raise OSError(errno.ENOSPC, "Incomplete sidecar write", destination)
            os.fsync(fd)
        except BaseException:
            try:
                _withdraw_copy(destination, recovery, fd, data, start, written)
            except (OSError, SourceChangedError) as exc:
                raise _SidecarRecoveryError(
                    f"Could not restore {destination.name}; recovery files retained at {recovery}"
                ) from exc
            raise
        # FAT32 assigns the permanent identity only after the first data cluster.
        return os.fstat(fd)


def _install_unoccupied(source: Path, destination: Path) -> os.stat_result:
    """Publish only into a vacant name, preferring whole-file operations."""
    identity = source.lstat()
    try:
        os.link(source, destination, follow_symlinks=False)
        return identity
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.ENOSYS, errno.EOPNOTSUPP):
            raise
        if os.name == "nt":  # Windows rename already refuses an occupied destination.
            source.rename(destination)
            return identity
        # Linkless filesystems can still publish sidecars for a no-op silk pass.
        # Use the platform's exclusive rename. A check followed by replace would
        # discard a save between those two calls.
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        src, dst = os.fsencode(source), os.fsencode(destination)
        if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
            result = libc.renamex_np(src, dst, 0x4)  # RENAME_EXCL
        elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
            result = libc.renameat2(-100, src, -100, dst, 1)  # AT_FDCWD, RENAME_NOREPLACE
        else:
            result = -1
            ctypes.set_errno(errno.ENOTSUP)
        if result == 0:
            return identity
        code = ctypes.get_errno()
        if code not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise OSError(code, os.strerror(code), destination)
        if source.is_symlink():
            destination.symlink_to(source.readlink())
            return destination.lstat()
        return _copy_unoccupied(source, destination)


class _SidecarWrite:
    """Hold the displaced entry until the entire source publication commits."""

    def __init__(self, live: Path, staged: Path, before: bytes | None, data: bytes) -> None:
        self.live = live
        self.staged = staged
        self.before = before
        self.data = data
        self.original = staged.with_name(staged.name + ".original")
        self.identity = staged.stat()
        self.displaced = False
        self.published = False

    def _read_displaced(self, path: Path) -> bytes:
        # A relative symlink still describes a target relative to the live directory.
        if path.is_symlink():
            path = self.live.parent / path.readlink()
        return path.read_bytes()

    def _changed(self) -> SourceChangedError:
        return SourceChangedError(f"{self.live.name} changed during manufacturing export")

    def _owns(self, path: Path) -> bool:
        try:
            return os.path.samestat(self.identity, path.lstat())
        except FileNotFoundError:
            return False

    def publish(self) -> None:
        if self.before is not None:
            try:
                self.live.rename(self.original)
            except FileNotFoundError as exc:
                raise self._changed() from exc
            finally:
                self.displaced = os.path.lexists(self.original)
            if self._read_displaced(self.original) != self.before:
                raise self._changed()
        try:
            self.identity = _install_unoccupied(self.staged, self.live)
        except FileExistsError as exc:
            raise self._changed() from exc
        finally:
            # Some filesystems can finish an operation before reporting its failure.
            self.published = self._owns(self.live)

    def verify(self) -> None:
        try:
            if (
                (self.displaced and self._read_displaced(self.original) != self.before)
                or not self._owns(self.live)
                or self.live.read_bytes() != self.data
            ):
                raise self._changed()
        except FileNotFoundError as exc:
            raise self._changed() from exc

    def rollback(self) -> bool:
        """Restore the latest live entry. Retain backups if two saves compete for it."""
        restore = self.original if self.displaced else None
        if self.published:
            withdrawn = self.staged.with_name(self.staged.name + ".withdrawn")
            try:
                self.live.rename(withdrawn)
            except FileNotFoundError:
                # A later removal wins. Retain any independent save to the old inode.
                return self.displaced and self._read_displaced(self.original) != self.before
            if not self._owns(withdrawn) or self._read_displaced(withdrawn) != self.data:
                restore = withdrawn  # The live name or its bytes already belong to the editor.
        if restore is not None:
            try:
                _install_unoccupied(restore, self.live)
            except FileExistsError:
                return restore != self.original or self._read_displaced(restore) != self.before
        return bool(
            self.displaced
            and restore != self.original
            and self._read_displaced(self.original) != self.before
        )


class _StagedSource:
    """Scratch copy of the board and its DRC sidecars (issue #10091).

    Maintainer decision, 2026-11-24: a named-stackup export that cannot
    perform the requested silkscreen repair must leave the input PCB, the
    project (``.kicad_pro``), the custom rules (``.kicad_dru``) and any
    zone-fill marker as it found them -- and must not disturb an editor save
    that lands meanwhile.  Rule emission, the zone refill and the silk pass
    therefore run on this copy, in a hidden directory beside the board (same
    filesystem, so the silk pass meets the same link/permission limits), and
    :meth:`publish` writes the results back only after every one of them
    succeeded. Publication retains displaced entries until all live writes
    and concurrent-save checks finish.
    """

    _SUFFIXES = (".kicad_pro", ".kicad_dru")

    def __init__(self, live_pcb: Path) -> None:
        self.live = live_pcb
        self.dir = Path(tempfile.mkdtemp(prefix=f".{live_pcb.name}.export-", dir=live_pcb.parent))
        self._preserve_backups = False
        try:
            self.pcb = self.dir / live_pcb.name
            self.pcb_bytes = live_pcb.read_bytes()
            self.pcb.write_bytes(self.pcb_bytes)
            self.sidecars_before: dict[str, bytes | None] = {}
            for suffix in self._SUFFIXES:
                data = self._read(live_pcb.with_suffix(suffix))
                self.sidecars_before[suffix] = data
                if data is not None:
                    self.pcb.with_suffix(suffix).write_bytes(data)
            marker = self._marker(live_pcb)
            self.marker_before = self._read(marker)
            if self.marker_before is not None:
                self._marker(self.pcb).write_bytes(self.marker_before)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _marker(pcb: Path) -> Path:
        return pcb.with_name(f".{pcb.name}.zonefill")

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def close(self) -> None:
        if not self._preserve_backups:
            shutil.rmtree(self.dir, ignore_errors=True)

    def publish(self) -> None:
        """Write the staged results to the live names.

        Raises ``SourceChangedError`` if the board or a sidecar no longer
        holds the bytes the export started from. Any failure rolls back the
        provisional writes, preserving intervening editor saves.
        """
        if self._read(self.live) != self.pcb_bytes:
            raise SourceChangedError("PCB changed during manufacturing export")
        inputs = {
            self.live.with_suffix(suffix): data for suffix, data in self.sidecars_before.items()
        }
        inputs[self._marker(self.live)] = self.marker_before
        writes = []
        unchanged = {}
        for live, before in inputs.items():
            if self._read(live) != before:
                raise SourceChangedError(f"{live.name} changed during manufacturing export")
            staged = self.dir / live.name
            data = self._read(staged)
            if data is None or data == before:
                unchanged[live] = before
            else:
                with staged.open("r+b") as stream:
                    os.fsync(stream.fileno())
                writes.append(_SidecarWrite(live, staged, before, data))
        if writes:
            # Check the volume's no-overwrite operation before displacing a live file.
            probe = self.dir / "publication-test"
            probe.write_bytes(b"")
            _install_unoccupied(probe, self.dir / "publication-test-link")
        staged_pcb = self.pcb.read_bytes()
        board_write = (
            checked_write_text(
                self.live,
                staged_pcb.decode("utf-8"),
                hashlib.sha256(self.pcb_bytes).hexdigest(),
                newline="",
            )
            if staged_pcb != self.pcb_bytes
            else nullcontext()
        )
        unchanged[self.live] = staged_pcb
        try:
            for write in writes:
                write.publish()
            # Finish verification while the board writer can still roll back its inode.
            with board_write:
                for write in writes:
                    write.verify()
                for live, before in unchanged.items():
                    if self._read(live) != before:
                        raise SourceChangedError(f"{live.name} changed during manufacturing export")
        except BaseException as failure:
            self._preserve_backups = isinstance(failure, _SidecarRecoveryError)
            rollback_errors = []
            for write in reversed(writes):
                try:
                    if write.rollback():
                        self._preserve_backups = True
                        logger.warning("Concurrent editor saves retained at %s", self.dir)
                except (OSError, SourceChangedError) as exc:
                    rollback_errors.append(f"{write.live.name}: {exc}")
            if rollback_errors:
                self._preserve_backups = True
                raise OSError(
                    f"Could not restore {', '.join(rollback_errors)}; originals retained at {self.dir}"
                )
            raise


class ManufacturingPackage:
    """
    Generate a complete manufacturing package for PCB production.

    Orchestrates BOM, CPL, Gerber, report, KiCad project ZIP, and
    manifest generation into a single output directory.

    Example::

        pkg = ManufacturingPackage(
            pcb_path="board.kicad_pcb",
            manufacturer="jlcpcb",
        )
        result = pkg.export("manufacturing/")
        print(result)
    """

    def __init__(
        self,
        pcb_path: str | Path,
        schematic_path: str | Path | None = None,
        manufacturer: str = "jlcpcb",
        config: ManufacturingConfig | None = None,
    ):
        self.pcb_path = Path(pcb_path)
        # Live board path while ``pcb_path`` points at a staged copy (#10091).
        self._live_pcb_path: Path | None = None
        self._pending_mutation_notices: list[tuple[int, str]] = []
        self.schematic_path = Path(schematic_path) if schematic_path else None
        self.manufacturer = manufacturer.lower()
        self.config = config or ManufacturingConfig()
        # Issue #10431: the project's supplementary purchasing rows, resolved
        # once per export by :meth:`_load_purchasing`.
        self._purchasing: list[PurchasingEntry] | None = None
        self._prior_purchasing_digest: str | None = None

    @property
    def _lookup_pcb_path(self) -> Path:
        """Board path for sibling-sidecar discovery (the live one when staged)."""
        return self._live_pcb_path or self.pcb_path

    def export(
        self,
        output_dir: str | Path | None = None,
        *,
        dry_run: bool = False,
    ) -> ManufacturingResult:
        """Generate the full manufacturing package.

        Args:
            output_dir: Output directory (overrides config).
            dry_run: If True, report what would be generated without
                     actually writing files.

        Returns:
            ManufacturingResult with paths and any errors.
        """
        out_dir = Path(output_dir) if output_dir else self.config.output_dir
        result = ManufacturingResult(output_dir=out_dir)

        selected_stackup_source_hash: str | None = None
        if self.config.stackup_id:
            selected_stackup_source_hash = self._stackup_source_hash(self.config.stackup_id, result)
            if selected_stackup_source_hash is None:
                return result

        # Issue #10431: read the project's supplementary purchasing rows
        # before anything is written, so a row that cannot be trusted refuses
        # the export instead of shipping a bundle with no list or a stale one.
        try:
            self._prior_purchasing_digest = _previous_purchasing_digest(
                out_dir, self.config.manifest_name
            )
            self._load_purchasing(result)
            manage_purchasing = bool(self._purchasing or self._prior_purchasing_digest)
            layout = (
                _PackageOutputs(
                    out_dir, self.config, self.manufacturer, purchasing=manage_purchasing
                )
                if self.config.include_project_zip or manage_purchasing
                else None
            )
            purchasing_path = out_dir / PURCHASING_FILENAME
            if (
                self._purchasing
                and self._prior_purchasing_digest is None
                and os.path.lexists(purchasing_path)
            ):
                raise ValueError(
                    f"Purchasing list conflicts with an existing file at {purchasing_path}"
                )
        except (OSError, ValueError) as exc:
            result.errors.append(str(exc))
        if not result.success:
            return result

        if dry_run:
            return self._dry_run(out_dir, result)

        # Step 0-floor: Connectivity safety floor.  Read any pre-existing DRC
        # report for hard shorts / connectivity errors *regardless* of
        # skip_preflight (skip_all).  Shipping a shorted board is never safe,
        # so this hard gate fires even when the rest of preflight is skipped
        # -- and it runs before any output files are written.  It performs no
        # kicad-cli invocation (it only parses a report already on disk), so
        # it adds no dependency and cannot slow a clean board's export.
        self._check_drc_safety_floor(result)
        if not result.success:
            return result

        # Pre-construct the assembly package so we surface fatal input
        # errors (e.g. a schematic-sourced BOM with no discoverable
        # .kicad_sch) *before* preflight runs or we create any output
        # files.  This guarantees that a schematic-missing failure
        # leaves nothing on disk -- avoiding the "5/8 files written,
        # exit code 1, partial package" gap.
        try:
            AssemblyPackage(
                pcb_path=self.pcb_path,
                schematic_path=self.schematic_path,
                manufacturer=self.manufacturer,
                config=self.config,
            )
        except KiCadFileNotFoundError as e:
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")
            return result

        # Steps 0a-0c.  A named-stackup export with the default silk repair
        # runs them on a scratch copy and publishes only on success, so a
        # refused repair leaves the board and its siblings untouched (#10091).
        staged: _StagedSource | None = None
        self._pending_mutation_notices = []
        live_pcb = self.pcb_path
        if (
            self.config.emit_drc_constraints
            and self.config.stackup_id
            and self.config.silk_fix
            and self.manufacturer != "generic"
        ):
            try:
                staged = _StagedSource(live_pcb)
            except OSError as exc:
                result.errors.append(f"Could not stage {live_pcb.name} for export: {exc}")
                return result
            self._live_pcb_path = live_pcb
            self.pcb_path = staged.pcb
        try:
            proceed, selected_stackup_source_hash = self._prepare_source(
                result, selected_stackup_source_hash
            )
        except BaseException:
            if staged is not None:
                staged.close()
            raise
        finally:
            self.pcb_path = live_pcb
            self._live_pcb_path = None
        if staged is not None:
            try:
                if proceed:
                    staged_dir = staged.dir
                    try:
                        staged.publish()
                    except SourceChangedError as exc:
                        result.errors.append(f"Stackup ordering validation failed: {exc}")
                        return result
                    except OSError as exc:
                        result.errors.append(
                            f"Silk legalisation pass could not run: {exc} (a named-stackup "
                            "export requires the requested silk repair; pass --no-silk-fix "
                            "to ship the original artwork)"
                        )
                        return result
                    result.drc_constraint_paths = [
                        live_pcb.with_name(Path(os.path.relpath(q, staged_dir)).name)
                        for q in result.drc_constraint_paths
                    ]
                    # The staged changes are now the live files: report them.
                    for level, message in self._pending_mutation_notices:
                        logger.log(level, message)
                        result.warnings.append(message)
            finally:
                staged.close()
        if not proceed:
            return result

        # Step 0: Pre-flight validation
        preflight_cfg = self.config.preflight
        if preflight_cfg is None or not preflight_cfg.skip_all:
            preflight_results = self._run_preflight()
            result.preflight_results = preflight_results

            # The bom_pcb_match check returns FAIL when the BOM references
            # parts with no matching PCB footprint -- an unbuildable
            # package.  This is a targeted block that fires even when
            # ``strict_preflight`` is False, because it's the only check
            # whose failure mode is "the fab literally cannot make this".
            unbuildable_bom_failure: PreflightResult | None = None
            if self.config.block_on_unbuildable_bom:
                for pr in preflight_results:
                    if pr.name == "bom_pcb_match" and pr.status == "FAIL":
                        # Only treat as unbuildable if the schematic-only
                        # axis is involved (refs in BOM not on PCB).  PCB
                        # orphans are only a WARN, so we won't get here for
                        # them.
                        unbuildable_bom_failure = pr
                        break

            if PreflightChecker.has_failures(preflight_results):
                # Collect failure messages
                for pr in preflight_results:
                    if pr.status == "FAIL":
                        msg = f"Preflight FAIL [{pr.name}]: {pr.message}"
                        if pr.details:
                            msg += f" ({pr.details})"
                        # bom_pcb_match failures escalate to errors when
                        # block_on_unbuildable_bom is set; other preflight
                        # failures behave per strict_preflight.
                        is_unbuildable = (
                            unbuildable_bom_failure is not None and pr is unbuildable_bom_failure
                        )
                        if self.config.strict_preflight or is_unbuildable:
                            result.errors.append(msg)
                        else:
                            result.warnings.append(msg)

                # Block export on strict mode OR on unbuildable BOM
                if self.config.strict_preflight or unbuildable_bom_failure is not None:
                    return result

        out_dir.mkdir(parents=True, exist_ok=True)

        if layout is not None:
            return self._publish_package(out_dir, result, selected_stackup_source_hash, layout)
        return self._write_package(out_dir, result, selected_stackup_source_hash)

    def _publish_package(
        self,
        out_dir: Path,
        result: ManufacturingResult,
        source_hash: str | None,
        layout: _PackageOutputs,
    ) -> ManufacturingResult:
        """Generate the editable project and its manufacturing files as one candidate."""
        try:
            with tempfile.TemporaryDirectory(prefix="kct-export-") as tmp:
                candidate = Path(tmp)
                work_bundle = candidate / layout.bundle_key
                work_bundle.mkdir()
                existing, before = layout.seed(candidate)
                writer = copy(self)
                writer.config = layout.config
                result.output_dir = work_bundle
                writer._write_package(work_bundle, result, source_hash)
                if result.success:
                    layout.publish(candidate, existing, before)
        except (OSError, RuntimeError) as exc:
            result.errors.append(f"Manufacturing package publication failed: {exc}")

        if result.output_dir != out_dir:
            self._relocate_package_result(result, out_dir, layout.live_path)
        return result

    @staticmethod
    def _relocate_package_result(
        result: ManufacturingResult, out_dir: Path, live_path: Callable[[str | Path], Path]
    ) -> None:
        """Expose published artifact paths, keeping source-side diagnostics on a refusal."""
        staging = result.output_dir.parent

        def published(path: Path | None) -> Path | None:
            return (
                live_path(Path(os.path.abspath(path)).relative_to(staging))
                if result.success and path is not None
                else None
            )

        result.output_dir = out_dir
        result.project_zip_path = published(result.project_zip_path)
        result.stackup_path = published(result.stackup_path)
        result.manifest_path = published(result.manifest_path)
        result.readme_path = published(result.readme_path)
        result.purchasing_path = published(result.purchasing_path)
        result.report_path = published(result.report_path)
        result.report_md_path = published(result.report_md_path)
        result.image_paths = (
            [live_path(Path(os.path.abspath(p)).relative_to(staging)) for p in result.image_paths]
            if result.success
            else []
        )
        result.simulation_paths = (
            [
                live_path(Path(os.path.abspath(p)).relative_to(staging))
                for p in result.simulation_paths
            ]
            if result.success
            else []
        )
        if (assembly := result.assembly_result) is not None:
            assembly.output_dir = out_dir
            assembly.bom_path = published(assembly.bom_path)
            assembly.pnp_path = published(assembly.pnp_path)
            assembly.gerber_path = published(assembly.gerber_path)

    def _write_package(
        self, out_dir: Path, result: ManufacturingResult, selected_stackup_source_hash: str | None
    ) -> ManufacturingResult:
        # Step 1: BOM + CPL + Gerbers via AssemblyPackage.  When this
        # returns False the stage aborted before writing any artefacts
        # (e.g. schematic missing for a schematic-sourced BOM), so we
        # skip the rest of the pipeline to avoid producing a partial
        # package on disk.
        assembly_ok = self._generate_assembly(out_dir, result)
        if not assembly_ok:
            return result

        # Step 1.5: Reconcile the recorded ``bom_fields`` preflight result
        # against the LCSC enrichment that only just ran (Step 1).  Preflight
        # inspects a raw BOM before any network/cache lookup, so its
        # "missing LCSC part number" clause is a prediction that
        # ``--auto-lcsc`` may have already invalidated (issue #4591).
        self._reconcile_preflight_lcsc(result)

        # Step 1.6: measure the Gerber archive that will ship (Issue #10422),
        # before the report, README and manifest are written from it.
        self._measure_reinforcement_clearance(result)
        # Supplementary purchasing list (Issue #10431).  Staged, or
        # an earlier one withdrawn, without touching the fabricator files.
        self._write_purchasing(out_dir, result)

        if self.config.via_land_overlap:
            self._measure_via_land_overlap(result)

        # Step 2: Report (optional)
        if self.config.include_report:
            self._generate_report(out_dir, result)

        # Step 2.5: Flatten report into report/ when latest_report_only is set
        if self.config.latest_report_only:
            self._flatten_latest_report(out_dir, result)

        # Step 3: KiCad project ZIP (optional)
        if self.config.include_project_zip:
            self._generate_project_zip(out_dir, result)
            if result.project_zip_path is None:
                return result

        # Step 3.4: Optional simulation evidence (Issue #10468)
        self._collect_simulation_evidence(out_dir, result)

        # Step 3.5: README.txt (optional)
        if self.config.include_readme:
            self._generate_readme(out_dir, result)

        # Record the actual source again after generation, before hashing the bundle.
        if self.config.stackup_id:
            from .stackup import stackup_ordering_record

            try:
                record = stackup_ordering_record(self.pcb_path, self.config.stackup_id)
                if record["pcb_sha256"] != selected_stackup_source_hash:
                    raise ValueError("PCB changed during manufacturing export")
            except (OSError, ValueError, KiCadToolsError) as exc:
                result.errors.append(f"Stackup ordering validation failed: {exc}")
                return result
            result.stackup_path = out_dir / "stackup-ordering.json"
            result.stackup_path.write_text(json.dumps(record, indent=2) + "\n")

        # Step 4: Manifest (always last -- needs checksums of other files)
        if self.config.include_manifest:
            self._generate_manifest(out_dir, result)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prepare_source(
        self, result: ManufacturingResult, selected_stackup_source_hash: str | None
    ) -> tuple[bool, str | None]:
        """Steps 0a-0c: rules, zone refill, fabrication process, silk pass.

        Returns ``(proceed, source_hash)``; errors are recorded on *result*.
        """
        # Step 0a: Emit DRC-constraint sources next to the source board so
        # that kicad-cli pcb drc (run here in preflight, in CI, or by the
        # gallery render pipeline) loads the manufacturer profile's relaxed
        # built-in minimums instead of KiCad's stricter hard-coded defaults.
        # Written before preflight so the constraints exist even when a
        # strict-preflight failure aborts the rest of the pipeline.
        if self.config.emit_drc_constraints:
            self._write_drc_constraints(result)
            # KiCad plots the saved copper. Check it under the rules that will
            # ship before plotting, archiving or binding a named stackup (#9987).
            refill_digest = self._refresh_zone_fill(result, selected_stackup_source_hash)
            if not result.success:
                return False, selected_stackup_source_hash
            if self.config.stackup_id and refill_digest is not None:
                selected_stackup_source_hash = refill_digest

        # Step 0b (issue #5009): resolve the selected manufacturer profile's
        # declared via-in-pad fabrication process (if any) for this board's
        # actual layer/copper configuration, so the manifest and README
        # steps below can bind it into the bundle.
        self._resolve_fabrication_process(result)
        self._resolve_ordered_copper(result)

        # Step 0c (issue #9799): the default silk legalisation pass.  Runs
        # BEFORE the first check that judges silk, and the checks, plots and
        # hashes below all read these same repaired bytes (the pass edits
        # the board in place; a zero-edit board is not re-saved, so an
        # already-legal board is untouched and idempotent across the
        # route -> export pipeline).  ``--no-silk-fix`` skips it entirely.
        # The ``generic`` pseudo-tier has no profile, so it skips too.
        if self.config.silk_fix and self.manufacturer != "generic":
            from kicad_tools.cli.silk_default_pass import run_default_silk_pass
            from kicad_tools.core.atomic_write import SourceChangedError

            try:
                result.silk_fix = run_default_silk_pass(
                    self.pcb_path,
                    mfr=self.manufacturer,
                    quiet=True,
                    expected_sha256=selected_stackup_source_hash,
                )
            except SourceChangedError as exc:
                result.errors.append(f"Stackup ordering validation failed: {exc}")
                return False, selected_stackup_source_hash
            if self.config.stackup_id and result.silk_fix is None:
                result.silk_fix = {
                    "applied": False,
                    "manufacturer": self.manufacturer,
                    "error": "could not resolve the manufacturer's silk rules for this board",
                }
            if result.silk_fix is not None and not result.silk_fix.get("applied"):
                message = "Silk legalisation pass could not run: " + str(
                    result.silk_fix.get("error", "unknown reason")
                )
                if self.config.stackup_id:
                    # Issue #10051 (maintainer decision, 2026-11-24): a
                    # named-stackup export must not publish unrepaired silk.
                    # The requested repair could not run -- e.g. the
                    # filesystem lacks the hard-link support the checked
                    # publication path probes for -- so stop here, before
                    # any output is written.  ``--no-silk-fix`` is the
                    # explicit way to ship the original artwork instead.
                    result.errors.append(
                        message + " (a named-stackup export requires the requested silk "
                        "repair; pass --no-silk-fix to ship the original artwork)"
                    )
                    return False, selected_stackup_source_hash
                result.warnings.append(message)
            # Issue #9990: the pass is export's own edit of the source board.
            # Re-bind the named-stackup provenance to the bytes the pass
            # itself wrote (its report carries their sha256), through the
            # same validation used at the start of export() -- so the
            # ordering record describes the bytes that ship, while an edit
            # other than the pass's own still fails the final change guard.
            written = result.silk_fix.get("output_sha256") if result.silk_fix else None
            if self.config.stackup_id and written:
                rebound = self._stackup_hash_after_own_edit(result, written)
                if rebound is None:
                    return False, selected_stackup_source_hash
                selected_stackup_source_hash = rebound

        return True, selected_stackup_source_hash

    def _stackup_hash_after_own_edit(
        self, result: ManufacturingResult, written_sha256: str
    ) -> str | None:
        """Re-bind named-stackup provenance to export's own edit (#9990).

        Runs the same ``stackup_ordering_record`` validation as the start of
        :meth:`export` on the live board and returns its sha256 only when it
        equals *written_sha256*, the digest of the bytes the silk pass wrote.
        The equality pins the rebound provenance to the exporter's own
        transformed bytes, so an external edit during or after that stage
        still fails the final source-change guard.  On any failure the error
        is recorded on *result* and ``None`` is returned.
        """
        from .stackup import stackup_ordering_record

        stackup_id = self.config.stackup_id
        if not stackup_id:
            return None
        try:
            record = stackup_ordering_record(self.pcb_path, stackup_id)
            rebound_hash = str(record["pcb_sha256"])
            if rebound_hash != written_sha256:
                raise ValueError("PCB changed during manufacturing export")
        except (OSError, ValueError, KiCadToolsError) as exc:
            result.errors.append(f"Stackup ordering validation failed: {exc}")
            return None
        return rebound_hash

    # Substring that identifies the LCSC clause emitted by
    # ``PreflightChecker._check_bom_fields``.
    _LCSC_CLAUSE_MARKER = "missing LCSC part number"

    # Substrings that identify the non-LCSC problem clauses.  Anything in
    # ``details`` that matches neither these nor the LCSC marker is an
    # informational note and is preserved verbatim.
    _BOM_FIELD_ISSUE_MARKERS = ("missing footprint", "missing value")

    def _stackup_source_hash(self, stackup_id: str, result: ManufacturingResult) -> str | None:
        """Validate the named stackup; the board's current content hash, or ``None``.

        ``None`` appends the validation error to ``result``: the caller
        stops with nothing written.
        """
        from .stackup import stackup_ordering_record

        try:
            if self.manufacturer not in {"jlcpcb", "jlcpcb-tier1"}:
                raise ValueError("JLCPCB construction requires a JLCPCB manufacturer")
            record = stackup_ordering_record(self.pcb_path, stackup_id)
            return str(record["pcb_sha256"])
        except (OSError, ValueError, KiCadToolsError) as exc:
            result.errors.append(f"Stackup ordering validation failed: {exc}")
            return None

    def _refresh_zone_fill(
        self,
        result: ManufacturingResult,
        expected_sha256: str | None = None,
    ) -> str | None:
        """Return the digest of a newly saved refill. Record blocking failures on result.

        A native refill of a scratch copy can certify the existing pours
        under the final rules without KiCad reserializing the source file.
        Otherwise a native refill must save the board before it can ship.
        Reuse that evidence only for the exact PCB bytes and rule sources.
        """
        from ..cli.runner import _board_unchanged, find_kicad_cli, run_refill_zones
        from ..core.atomic_write import atomic_write_text
        from .gerber import _pcb_has_filled_zones
        from .stackup import stackup_ordering_record

        if not (self.config.include_gerbers or self.config.include_project_zip):
            return None
        if not _pcb_has_filled_zones(self.pcb_path):
            return None

        try:
            original = self.pcb_path.read_bytes()
            original_mtime_ns = self.pcb_path.stat().st_mtime_ns
        except OSError as exc:
            msg = f"Could not read {self.pcb_path.name} before its zone refill: {exc}"
            result.errors.append(msg)
            return None
        board_sha = hashlib.sha256(original).hexdigest()
        if expected_sha256 is not None and board_sha != expected_sha256:
            result.errors.append("PCB changed during manufacturing export")
            return None

        rule_sha = hashlib.sha256(_zone_fill_rule_state(self.pcb_path).encode()).hexdigest()
        marker = self.pcb_path.with_name(f".{self.pcb_path.name}.zonefill")
        try:
            if marker.read_text(encoding="ascii").strip() == f"{rule_sha} {board_sha}":
                return None
        except (OSError, UnicodeError):
            pass

        def cannot_refresh(why: str) -> None:
            msg = (
                f"The saved zone fill in {self.pcb_path.name} could not be refreshed to match "
                f"the generated DRC rules ({why}). Nothing was exported. "
                "Refill the zones in KiCad and export again."
            )
            result.errors.append(msg)
            logger.error(msg)

        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            cannot_refresh("kicad-cli was not found")
            return None
        current = _saved_zone_fill_matches_native_refill(self.pcb_path, kicad_cli)
        if not _board_unchanged(self.pcb_path, original, original_mtime_ns):
            result.errors.append("PCB changed during manufacturing export")
            return None

        if not current:
            stackup_id = self.config.stackup_id
            target_context = (
                _zone_fill_copy(self.pcb_path, original)
                if stackup_id
                else nullcontext(self.pcb_path)
            )
            try:
                with target_context as target:
                    mtime_ns = target.stat().st_mtime_ns
                    refill = run_refill_zones(target, kicad_cli, require_save=True)
                    if not refill.success or _board_unchanged(target, original, mtime_ns):
                        raise ValueError(
                            refill.stderr.strip()
                            or "kicad-cli wrote a DRC report but did not save the board"
                        )
                    if stackup_id:
                        # Validate the construction before replacing the source. Both the
                        # provenance digest and the checked write use the private native output.
                        record = stackup_ordering_record(target, stackup_id)
                        native_output = target.read_bytes()
                        native_digest = hashlib.sha256(native_output).hexdigest()
                        if native_digest != record["pcb_sha256"]:
                            raise ValueError("Refilled PCB differs from the validated stackup")
                        atomic_write_text(
                            self.pcb_path,
                            native_output.decode("utf-8"),
                            encoding="utf-8",
                            newline="",
                            expected_sha256=board_sha,
                        )
                        board_sha = native_digest
                    else:
                        board_sha = _sha256_file(target)
            except (OSError, ValueError, KiCadToolsError) as exc:
                if not stackup_id:
                    _restore_board(self.pcb_path, original)
                cannot_refresh(str(exc))
                return None
            note = f"Export refilled the zone fills in {self.pcb_path.name} to match the generated DRC rules."
            result.warnings.append(note)
            logger.info(note)

        try:
            marker.write_text(f"{rule_sha} {board_sha}\n", encoding="ascii")
        except OSError as exc:
            logger.warning("Could not record the zone-fill state: %s", exc)
        return None if current else board_sha

    def _reconcile_preflight_lcsc(self, result: ManufacturingResult) -> None:
        """Update the recorded ``bom_fields`` result with post-enrichment LCSC state.

        ``PreflightChecker`` runs before any file is written (Step 0), which
        is deliberate: ``strict_preflight`` and ``block_on_unbuildable_bom``
        must be able to abort with nothing on disk.  The consequence is that
        ``_check_bom_fields`` sees a *raw* BOM: for the ``--auto-lcsc`` path
        it cannot know which items :func:`enrich_bom_lcsc` will resolve,
        because that requires parts-API / cache I/O which preflight must not
        perform.  (The spec-overlay path is predicted statically by
        ``PreflightChecker._spec_lcsc_refs`` -- issues #3926 / #3932.)

        So the LCSC clause is reconciled here, after Step 1 has actually run
        the enrichment, and before the manifest is serialized.  Both the
        manifest (``_build_manifest`` reads ``result.preflight_results``) and
        the CLI summary render the updated entry with no changes at either
        consumer.

        Safety rules:

        * A ``FAIL`` is **never** downgraded -- a ``bom_fields`` FAIL comes
          from missing footprints, which enrichment cannot fix.
        * Non-LCSC clauses (missing footprint / missing value) and
          informational notes are preserved verbatim.
        * No enrichment report (``--no-auto-lcsc``, non-JLCPCB family,
          enrichment error) or no preflight results (``--skip-preflight``)
          makes this a no-op.
        """
        if not result.preflight_results:
            return
        if result.assembly_result is None:
            return

        report = result.assembly_result.lcsc_enrichment
        if report is None:
            return

        entry = next((pr for pr in result.preflight_results if pr.name == "bom_fields"), None)
        if entry is None or entry.status == "FAIL":
            return

        clauses = [c.strip() for c in entry.details.split(";")] if entry.details else []
        lcsc_clauses = [c for c in clauses if self._LCSC_CLAUSE_MARKER in c]
        if not lcsc_clauses:
            # Nothing predicted about LCSC -- leave the entry untouched.
            return

        other_clauses = [c for c in clauses if self._LCSC_CLAUSE_MARKER not in c]
        remaining_issues = [
            c for c in other_clauses if any(marker in c for marker in self._BOM_FIELD_ISSUE_MARKERS)
        ]
        notes = [c for c in other_clauses if c not in remaining_issues]

        unmatched_entries = report.unmatched_entries
        spec_unresolved_entries = report.spec_unresolved_entries
        if unmatched_entries:
            refs = [ref for e in unmatched_entries for ref in e.references]
            shown = ", ".join(sorted(refs)[:10])
            suffix = f" (and {len(refs) - 10} more)" if len(refs) > 10 else ""
            remaining_issues.append(
                f"{len(unmatched_entries)} component group(s) still missing LCSC "
                f"part number after enrichment: {shown}{suffix}"
            )
        if spec_unresolved_entries:
            # Explicit MPN/supplier sourcing (issue #4995): never auto-matched
            # in the first place, so this is a deliberate "manually sourced"
            # gap, not a search failure -- surfaced with its own wording so a
            # reviewer can tell the two apart at a glance.
            refs = [ref for e in spec_unresolved_entries for ref in e.references]
            shown = ", ".join(sorted(refs)[:10])
            suffix = f" (and {len(refs) - 10} more)" if len(refs) > 10 else ""
            remaining_issues.append(
                f"{len(spec_unresolved_entries)} component group(s) explicitly "
                f"sourced outside LCSC (spec/CSV MPN set, no LCSC assigned) -- "
                f"verify supplier manually: {shown}{suffix}"
            )
        if not unmatched_entries and not spec_unresolved_entries:
            resolved = report.auto_matched + report.cache_matched
            if resolved:
                notes.append(f"{resolved} group(s) received LCSC from enrichment")

        if remaining_issues:
            entry.status = "WARN"
            entry.message = f"BOM field issues: {len(remaining_issues)} problem(s)"
        else:
            entry.status = "OK"
            entry.message = "All BOM items have required fields (LCSC resolved by enrichment)"

        entry.details = "; ".join(remaining_issues + notes)

    def _gerber_plot_origin(self) -> tuple[float, float]:
        """Origin of the shipped Gerbers in the sheet frame, as kicad-cli plotted them (Issue #10422).

        ``--use-drill-file-origin`` (every preset but OSH Park's, and the
        default ``GerberConfig``) moves the artwork's origin to the board's aux
        origin while the sidecar's coordinates stay sheet-absolute. The value
        resolves exactly as ``AssemblyPackage._generate_gerbers`` resolves the
        Gerber options, and it is a claim: the measurement corroborates it
        against the plotted copper before trusting any anchor.
        """
        from ..manufacturers import get_fab_family
        from .gerber import MANUFACTURER_PRESETS, GerberConfig

        family = get_fab_family(self.manufacturer)
        if family in MANUFACTURER_PRESETS:
            use_aux = MANUFACTURER_PRESETS[family].config.use_aux_origin
        else:
            use_aux = (self.config.gerber_config or GerberConfig()).use_aux_origin
        if not use_aux:
            return (0.0, 0.0)
        try:
            setup = parse_file(self.pcb_path).find_child("setup")
            origin = setup.find_child("aux_axis_origin") if setup is not None else None
            values = origin.get_atoms() if origin is not None else []
        except (OSError, ValueError, KiCadToolsError):
            return (0.0, 0.0)
        try:
            return (float(values[0]), float(values[1]))
        except (IndexError, TypeError, ValueError):
            return (0.0, 0.0)

    def _measure_reinforcement_clearance(self, result: ManufacturingResult) -> None:
        """Step 1.6: measure the finished Gerber archive against the sidecar (Issue #10422).

        Only a board with a ``<board>.reinforcement.json`` sidecar is measured;
        without one nothing is recorded and the package is byte-for-byte what
        it was. The measurements are nominal geometry with no verdict: they
        become one preflight entry, whose status says only whether every
        measurement could be made, so they never turn into an export error or
        into a fabrication verdict.
        """
        from ..pcb.reinforcement_sidecar import load_wires_if_present
        from .reinforcement_clearance import measure_reinforcement_clearance

        wires = load_wires_if_present(self._lookup_pcb_path)
        if not wires:
            return
        try:
            assembly = result.assembly_result
            report = measure_reinforcement_clearance(
                assembly.gerber_path if assembly is not None else None,
                wires,
                plot_origin_mm=self._gerber_plot_origin(),
            )
        except Exception as e:  # noqa: BLE001 - a nominal measurement must never fail an export
            logger.warning("Reinforcement clearance measurement skipped: %s", e)
            return
        result.reinforcement_clearance = report.to_dict()
        result.preflight_results.append(report.preflight_result())

    def _measure_via_land_overlap(self, result: ManufacturingResult) -> None:
        """Measure the shipped archive's plated via drills against same-face SMD lands.

        Opt-in through ``ManufacturingConfig.via_land_overlap`` (Issue #10357).
        Runs once the archive exists and before the report and the manifest
        read ``preflight_results``. A missing or unreadable archive, or a
        failure of the measurement itself, is recorded as partial coverage and
        never blocks the export.
        """
        from ..manufacturers import get_fab_family
        from ..schema.pcb import PCB
        from .assembly import plot_use_aux_origin
        from .via_land_overlap import measure_via_land_overlap, unmeasured_via_land_overlap

        assembly = result.assembly_result
        archive = Path(assembly.gerber_path) if assembly and assembly.gerber_path else None
        name = result.bundle_relpath(archive) if archive is not None else None
        use_aux_origin = plot_use_aux_origin(get_fab_family(self.manufacturer), self.config)
        sha256: str | None = None

        def unmeasured(reason: str) -> PreflightResult:
            return unmeasured_via_land_overlap(
                [reason], archive=name, archive_sha256=sha256, use_aux_origin=use_aux_origin
            )

        if archive is None or not archive.is_file():
            result.preflight_results.append(unmeasured("archive_missing"))
            return
        try:
            sha256 = _sha256_file(archive)
            with zipfile.ZipFile(archive) as opened:
                measurement = measure_via_land_overlap(
                    PCB.load(str(self.pcb_path)),
                    opened,
                    use_aux_origin=use_aux_origin,
                    fabrication_process=result.fabrication_process,
                    archive_name=name,
                    archive_sha256=sha256,
                )
        except (zipfile.BadZipFile, OSError) as exc:
            logger.warning("Via land overlap: archive %s is unreadable: %s", archive, exc)
            measurement = unmeasured("archive_unreadable")
        except Exception as exc:
            logger.exception("Via land overlap measurement failed")
            measurement = unmeasured(f"measurement_failed:{type(exc).__name__}")
        result.preflight_results.append(measurement)

    def _check_drc_safety_floor(self, result: ManufacturingResult) -> None:
        """Hard-abort export on net shorts / connectivity errors.

        Reads any pre-existing DRC report next to the source PCB (or the
        explicit ``drc_report_path`` from the preflight config) and appends
        an error to ``result`` when it contains error-severity net shorts
        (SHORTING_ITEMS) or unconnected items (opens, read through
        ``DRCReport.connectivity_items()``; issue #7091).  A report recorded
        for a different PCB is treated like a missing report (warning only),
        except under ``--strict-preflight`` where it blocks export.  Because
        ``ManufacturingResult.success`` is ``len(errors) == 0``, appending an error causes ``export()`` to
        return a non-success result and skip writing output files.

        This gate runs even when ``preflight.skip_all`` (``--skip-preflight``)
        is True -- that flag skips BOM/ERC/LCSC/cosmetic checks but must not
        suppress the hard safety floor.  The only way to bypass it is the
        explicit ``skip_drc_floor`` escape hatch (``--skip-drc-floor``), for
        known-safe workarounds.

        No kicad-cli is invoked; if no report exists, or it cannot be parsed,
        the floor does nothing (prefer a false-negative over breaking boards
        that never ran kicad-cli DRC).
        """
        preflight_cfg = self.config.preflight
        if preflight_cfg is not None and preflight_cfg.skip_drc_floor:
            msg = (
                "DRC connectivity safety floor DISABLED via --skip-drc-floor; "
                "export will proceed even if the board has net shorts. "
                "This is unsafe unless you have independently verified the board."
            )
            logger.warning(msg)
            result.warnings.append(msg)
            return

        # Resolve the report path: explicit config path wins, else look for a
        # report sitting next to the source PCB (JSON preferred over text).
        report_path: Path | None = None
        explicit = preflight_cfg.drc_report_path if preflight_cfg else None
        if explicit is not None:
            candidate = Path(explicit)
            if candidate.exists():
                report_path = candidate
        else:
            for name in ("drc_report.json", "drc_report.txt"):
                candidate = self.pcb_path.parent / name
                if candidate.exists():
                    report_path = candidate
                    break

        if report_path is None:
            return

        try:
            if report_path.stat().st_size == 0:
                # DRC was attempted but wrote nothing -- most likely kicad-cli
                # was killed (e.g. OOM) before it produced a report. An empty
                # file parses as 0 violations, so it must not read as clean.
                logger.warning(
                    f"DRC report {report_path.name} is empty (0 bytes); kicad-cli "
                    "was likely killed before writing it. The safety floor cannot "
                    "verify connectivity -- re-run DRC."
                )
                return
        except OSError:
            pass

        try:
            from ..drc.report import DRCReport
            from ..drc.violation import ViolationType

            report = DRCReport.load(report_path)
            # Shorts arrive through the geometric ``violations`` collection.
            shorts = [v for v in report.errors if v.type == ViolationType.SHORTING_ITEMS]
            # Opens: since #4498 KiCad's ``unconnected_items`` live in a
            # separate collection that ``report.errors`` never contains, so
            # read them through the connectivity accessor (issue #7091).
            # Only error-severity (non-waived) items block.
            opens = report.connectivity_errors()
        except Exception as e:
            # If we can't read the report, don't block -- prefer a
            # false-negative over breaking boards that never ran DRC.
            logger.warning(f"DRC safety floor could not read {report_path}: {e}")
            return

        # Issue #7091: a report generated for a different PCB (e.g. the
        # ``*_routed`` sibling's report sitting next to the unrouted board)
        # is no evidence for this board.  Treat it like a missing report:
        # warn, but do not block (same false-negative policy as above).
        other = report_source_mismatch(report.pcb_name, self.pcb_path)
        if other is not None:
            msg = (
                f"DRC report {report_path.name} is for {other}, not "
                f"{self.pcb_path.name}; the safety floor cannot verify this "
                "board's connectivity -- re-run DRC on it."
            )
            if self.config.strict_preflight:
                msg = f"Safety floor (strict): {msg}"
                result.errors.append(msg)
                logger.error(msg)
            else:
                logger.warning(msg)
                result.warnings.append(msg)
            return

        # Issue #7190: note a report that is not bound to the PCB's current
        # content (mtimes are meaningless after a git checkout, so this uses
        # the report's recorded pcb_sha256 / readiness co-binding instead).
        # Log only: the floor still blocks on a stale FAIL (staleness in the
        # blocking direction is the safe side); this explains a potentially
        # spurious block.  Preflight's ``_check_drc`` keeps an unbound report
        # from ever *passing* the board.
        if explicit is None:
            from ..drc.report_binding import BindingStatus, binding_status

            binding = binding_status(report_path, self.pcb_path, report.pcb_sha256)
            if binding is not BindingStatus.BOUND:
                logger.warning(
                    f"DRC report {report_path.name} is not bound to the PCB's current "
                    f"content ({binding.value}); safety-floor verdict may be stale."
                )

        if shorts or opens:
            total = len(shorts) + len(opens)
            msg = (
                f"Safety floor: {total} connectivity error(s) "
                f"({len(shorts)} net short(s), {len(opens)} unconnected item(s)) "
                f"in DRC report ({report_path.name}); export blocked. Fix the "
                "shorts/opens, or pass --skip-drc-floor to override (only for "
                "known-safe workarounds)."
            )
            result.errors.append(msg)
            logger.error(msg)

    def _run_preflight(self) -> list[PreflightResult]:
        """Run pre-flight validation checks."""
        preflight_cfg = self.config.preflight or PreflightConfig()
        if self.config.strict_preflight and not preflight_cfg.strict_report_source:
            preflight_cfg = dataclasses.replace(preflight_cfg, strict_report_source=True)

        # Pass explicit THT exclusion setting when the user provided a PnP config
        exclude_tht = None
        if self.config.pnp_config is not None:
            exclude_tht = self.config.pnp_config.exclude_tht

        checker = PreflightChecker(
            pcb_path=self.pcb_path,
            schematic_path=self.schematic_path,
            manufacturer=self.manufacturer,
            output_dir=self.config.output_dir,
            config=preflight_cfg,
            spec_path=self.config.spec_path,
            no_spec=self.config.no_spec,
            exclude_tht=exclude_tht,
            bom_source=self.config.bom_source,
            merge_lcsc=self.config.merge_lcsc,
            merge_lcsc_from=self.config.merge_lcsc_from,
        )
        return checker.run_all()

    def _detect_layer_config(self) -> tuple[int, float]:
        """Determine (copper-layer count, copper weight) for the board.

        The layer count is read from the PCB so the DRC rules selected
        from the manufacturer profile match the board (a 4-layer board
        uses finer via/drill minimums than a 2-layer board).  The copper
        weight is the outer copper declared in the board's stackup -- the
        same rule ``kct check`` uses to pick its preset (#8836), so the
        clearance/width floors written to the ``.kicad_dru`` match the ones
        ``kct check`` enforces (Issue #9422).  A board with no declared
        stackup copper keeps the historical 1oz.

        Returns:
            ``(layers, copper_oz)`` -- falls back to ``(2, 1.0)`` when the
            board cannot be parsed.
        """
        copper_oz = 1.0
        try:
            from ..schema.pcb import PCB

            pcb = PCB.load(str(self.pcb_path))
            layers = len(pcb.copper_layers)
            if layers < 1:
                layers = 2
        except Exception as e:
            logger.debug("Could not detect layer count from %s: %s", self.pcb_path, e)
            return 2, copper_oz
        try:
            from ..physics.stackup import Stackup

            derived = Stackup.from_pcb(pcb).outer_inner_copper_oz()
            if derived is not None and derived[0]:
                copper_oz = float(derived[0])
        except Exception as e:  # noqa: BLE001 - undeclared/odd stackup keeps 1oz
            logger.debug("Could not read stackup copper from %s: %s", self.pcb_path, e)
        return layers, copper_oz

    def _write_drc_constraints(self, result: ManufacturingResult, *, dry_run: bool = False) -> None:
        """Emit sibling .kicad_pro / .kicad_dru from the manufacturer profile.

        Populates the project file's ``board.design_settings.rules`` block
        (and a companion .kicad_dru) from the target manufacturer profile
        so that ``kicad-cli pcb drc`` checks the board against the fab's
        actual capabilities rather than KiCad's stricter built-in defaults.

        The sidecars are written next to the *input* board
        (``self.pcb_path``), **not** into ``--output``.  This is deliberate:
        ``kicad-cli pcb drc`` only loads a ``.kicad_dru`` / ``.kicad_pro``
        that sits beside the ``.kicad_pcb`` it checks, and the callers that
        need these rules (preflight DRC here, CI, the gallery render
        pipeline, a user re-running DRC in KiCad) all operate on the source
        board in place.  Consequence (issue #5746): ``kct export`` mutates
        the input directory, so tests that export a *committed* board must
        stage a scratch copy first (see ``tests/test_board_05_export.py``)
        or the #3580 committed-artifact guard will fire whenever the
        generated rules change.

        With ``dry_run`` the merge runs against scratch copies of the
        sidecars, so a refusal (``RuleLooseningError``) surfaces exactly as
        in the real run while nothing is written next to the board (#9422).
        """
        try:
            from ..manufacturers import (
                get_profile,
                resolve_pcb_fabrication_overrides,
                write_drc_constraints,
            )

            profile = get_profile(self.manufacturer)
        except Exception as e:
            logger.warning(
                "Skipping DRC-constraint emission (profile '%s' unavailable): %s",
                self.manufacturer,
                e,
            )
            result.warnings.append(
                f"Could not emit DRC constraints for manufacturer '{self.manufacturer}': {e}"
            )
            return

        layers, copper_oz = self._detect_layer_config()
        rules = profile.get_design_rules(layers=layers, copper_oz=copper_oz)

        # Issue #5006: retain (or reject) a validated, cited per-board
        # fabrication-floor override the same way `kct check
        # --emit-drc-constraints` does, so the packaged sidecars agree with
        # a reviewed project floor instead of silently reverting it to the
        # profile's conservative default.
        rules, fab_override_msg = resolve_pcb_fabrication_overrides(
            self._lookup_pcb_path, rules, manufacturer_id=profile.id
        )
        if fab_override_msg is not None:
            if fab_override_msg.startswith("ignoring"):
                logger.warning(fab_override_msg)
                result.warnings.append(fab_override_msg)
            else:
                logger.info(fab_override_msg)

        rules = self._apply_stackup_copper(rules)

        unreadable = object()

        def existing_bytes(path: Path):
            # Bytes, not text: read_text() normalizes line endings and would
            # hide a rewrite that only changed them. A file that exists but
            # cannot be read is not "absent": the writer regenerates it.
            try:
                return path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError:
                return unreadable if path.exists() else None

        def stat_sig(path: Path):
            try:
                st = path.stat()
            except OSError:
                return None
            return (st.st_mtime_ns, st.st_size)

        dru_path = Path(self.pcb_path).with_suffix(".kicad_dru")
        pro_path = Path(self.pcb_path).with_suffix(".kicad_pro")
        dru_bytes_before = existing_bytes(dru_path)
        pro_bytes_before = existing_bytes(pro_path)
        sig_before = {dru_path: stat_sig(dru_path), pro_path: stat_sig(pro_path)}
        dru_before = self._ampacity_block(dru_path)

        max_width_mm: float | None = None
        net_classes = None
        if not self.config.emit_ampacity_rules:
            logger.info("Ampacity rules disabled (--no-ampacity-rules)")
        else:
            from ..manufacturers.attach_zone_dru import has_designer_clearance_rule

            if has_designer_clearance_rule(
                dru_bytes_before.decode("utf-8", errors="replace")
                if isinstance(dru_bytes_before, bytes)
                else None
            ):
                result.warnings.append(
                    f"Skipped ampacity rules: {dru_path.name} carries the designer's own "
                    "clearance rules and was not extended with track_width floors."
                )
            else:
                net_classes = self._load_net_classes(result)
                net_classes = self._drop_waived_ampacity_nets(net_classes, result)
                max_width_mm = self._largest_board_dimension()
                if net_classes and max_width_mm is not None:
                    self._warn_oversize_ampacity(net_classes, rules, max_width_mm, result)

        if dry_run:
            self._dry_run_write_drc_constraints(
                write_drc_constraints,
                rules,
                profile_id=profile.id,
                layers=layers,
                copper_oz=copper_oz,
                net_classes=net_classes,
                max_width_mm=max_width_mm,
            )
            return

        # #8612: the designer owns these files; say so whenever export rewrote
        # one. Runs on the write-error path too: the project is saved before
        # the DRU, so a failed DRU write can still have changed the project.
        # Notices from a scratch copy (named-stackup staging) are held back
        # until the staged files are published to the live ones.
        def note(text: str, level: int = logging.INFO) -> None:
            if self._live_pcb_path is not None:
                self._pending_mutation_notices.append((level, text))
            else:
                logger.log(level, text)
                result.warnings.append(text)

        def report_rewrites() -> None:
            for path, before, reason in (
                (dru_path, dru_bytes_before, "generated DRC rules"),
                (pro_path, pro_bytes_before, "project DRC settings"),
            ):
                after = existing_bytes(path)
                if before is unreadable and stat_sig(path) not in (None, sig_before[path]):
                    msg = (
                        f"Export replaced {path.name} ({reason}); its prior contents "
                        "could not be read, so the changes could not be compared."
                    )
                elif isinstance(before, bytes) and isinstance(after, bytes) and before != after:
                    msg = f"Export modified {path.name} ({reason})."
                else:
                    continue
                if path == pro_path:
                    msg += (
                        f" Re-run `kicad-cli pcb drc` on {Path(self.pcb_path).name} "
                        "with the packaged project settings."
                    )
                note(msg)

        try:
            written = write_drc_constraints(
                self.pcb_path,
                rules,
                manufacturer_id=profile.id,
                layers=layers,
                copper_oz=copper_oz,
                net_classes=net_classes,
                max_ampacity_width_mm=max_width_mm,
            )
        except OSError as e:
            logger.warning("Could not write DRC-constraint files: %s", e)
            result.warnings.append(f"Could not write DRC constraints: {e}")
            report_rewrites()
            return

        result.drc_constraint_paths = written

        # #8527: the sign-off DRC ran against the committed DRU; flag a changed
        # ampacity block so nobody ships a DRU that was never DRC'd.
        dru_after = self._ampacity_block(dru_path)
        if dru_before != dru_after:
            msg = (
                f"Export changed the ampacity rules in {dru_path.name}; the "
                "sign-off DRC did not run against them. Re-run "
                "`kicad-cli pcb drc` against the shipped .kicad_dru."
            )
            note(msg, logging.WARNING)

        report_rewrites()

    def _dry_run_write_drc_constraints(
        self,
        write_drc_constraints,
        rules,
        *,
        profile_id: str,
        layers: int,
        copper_oz: float,
        net_classes,
        max_width_mm: float | None,
    ) -> None:
        """Run the sidecar merge on scratch copies; discard the output (#9422)."""
        import shutil
        import tempfile

        pcb_path = Path(self.pcb_path)
        with tempfile.TemporaryDirectory(prefix="kct-export-dry-run-") as tmp:
            staged = Path(tmp) / pcb_path.name
            for suffix in (".kicad_pro", ".kicad_dru"):
                source = pcb_path.with_suffix(suffix)
                if source.is_file():
                    shutil.copyfile(source, staged.with_suffix(suffix))
            try:
                write_drc_constraints(
                    staged,
                    rules,
                    manufacturer_id=profile_id,
                    layers=layers,
                    copper_oz=copper_oz,
                    net_classes=net_classes,
                    max_ampacity_width_mm=max_width_mm,
                    _board_path=pcb_path,
                    _report_dru_path=pcb_path.with_suffix(".kicad_dru"),
                )
            except OSError as e:
                logger.debug("Dry-run DRC-constraint merge skipped: %s", e)

    def _largest_board_dimension(self) -> float | None:
        """Largest Edge.Cuts extent in mm, or None when it cannot be determined."""
        try:
            from ..core.board_outline import board_outline_bounds
            from ..schema.pcb import PCB

            bounds = board_outline_bounds(PCB.load(str(self.pcb_path))._sexp)
        except Exception as e:  # noqa: BLE001 - best effort
            logger.debug("Could not read board outline for DRU: %s", e)
            return None
        if bounds is None:
            return None
        x0, y0, x1, y1 = bounds
        return max(x1 - x0, y1 - y0)

    @staticmethod
    def _warn_oversize_ampacity(net_classes, rules, max_width_mm, result) -> None:
        """Warn for each ampacity minimum wider than the board (skipped by generate_dru)."""
        from ..physics.ampacity import width_for_current

        for nc in net_classes:
            if nc.target_ampacity is None:
                continue
            layers = [("external", rules.outer_copper_oz)]
            if rules.inner_copper_oz > 0:
                layers.append(("internal", rules.inner_copper_oz))
            for layer, oz in layers:
                width = width_for_current(nc.target_ampacity, copper_weight_oz=oz, layer=layer)
                if width > max_width_mm:
                    msg = (
                        f"Skipped ampacity rule '{nc.name} {nc.target_ampacity:g}A, {layer}': "
                        f"minimum {width:.1f} mm exceeds the board's largest dimension "
                        f"({max_width_mm:.1f} mm) and can never be met."
                    )
                    logger.warning(msg)
                    result.warnings.append(msg)

    def _drop_waived_ampacity_nets(self, net_classes, result: ManufacturingResult):
        """Drop nets whose ampacity is waived or current-path reinforced (#8612).

        A malformed sidecar is warned about and the floors are kept.
        """
        if not net_classes:
            return net_classes
        from dataclasses import replace

        exempt: set[str] = set()
        pcb_path = Path(self._lookup_pcb_path)
        try:
            from ..validate.rules.waivers import discover_waivers_sidecar, load_waivers

            wpath = discover_waivers_sidecar(pcb_path)
            if wpath is not None:
                for entry in load_waivers(wpath).entries:
                    if entry.rule not in ("ampacity", "path_ampacity"):
                        continue
                    nets = set(entry.nets)
                    if entry.key:
                        parts = entry.key.split("|")
                        if len(parts) > 2:
                            nets.update(n for n in parts[2].split(",") if n)
                    exempt |= nets
        except Exception as e:  # noqa: BLE001
            msg = f"ignoring unreadable waivers sidecar for ampacity rules: {e}"
            logger.warning(msg)
            result.warnings.append(msg)
        try:
            from ..router.current_paths import (
                discover_current_paths_sidecar,
                load_current_path_specs,
            )

            cpath = discover_current_paths_sidecar(pcb_path)
            if cpath is not None:
                exempt |= {spec.net_name for spec in load_current_path_specs(cpath)}
        except Exception as e:  # noqa: BLE001
            msg = f"ignoring unreadable current-paths sidecar for ampacity rules: {e}"
            logger.warning(msg)
            result.warnings.append(msg)
        if not exempt:
            return net_classes

        kept = []
        for nc in net_classes:
            scope = tuple(getattr(nc, "net_scope", ()) or ())
            if not scope:
                kept.append(nc)
                continue
            remaining = tuple(n for n in scope if n not in exempt)
            if remaining:
                kept.append(replace(nc, net_scope=remaining))
        return kept

    @staticmethod
    def _ampacity_block(dru_path: Path) -> list[str]:
        """Return the ampacity rule blocks of a ``.kicad_dru`` (empty if none).

        The regex assumes each rule spans exactly three lines (name, condition,
        constraint), matching ``generate_dru`` in ``dru_generator.py``; change
        them together.
        """
        try:
            text = dru_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return ManufacturingPackage._ampacity_block_text(text)

    @staticmethod
    def _ampacity_block_text(text: str) -> list[str]:
        return re.findall(r'\(rule "Ampacity Min Width[^"]*"[^\n]*\n[^\n]*\n[^\n]*', text)

    def _apply_stackup_copper(self, rules):
        """Use the board's declared stackup copper weights for ampacity rules (#8527).

        The profile lookup keys on a hard-coded 1 oz, so a declared 2 oz outer
        stackup would otherwise produce 1 oz ampacity widths.  Only overrides
        what the board declares; undeclared stays at the profile value.
        """
        try:
            from dataclasses import replace as _replace

            from ..physics.stackup import Stackup
            from ..schema.pcb import PCB

            # Read the declared stackup directly (as ``kct check`` does): the
            # fab-profile-matched lookup returns nothing for layer counts the
            # profile has no copper option for, silently reverting to 1 oz.
            derived = Stackup.from_pcb(PCB.load(str(self.pcb_path))).outer_inner_copper_oz()
            outer, inner = derived if derived is not None else (None, None)
            updates = {}
            if outer:
                updates["outer_copper_oz"] = float(outer)
            if inner and rules.inner_copper_oz > 0:
                updates["inner_copper_oz"] = float(inner)
            return _replace(rules, **updates) if updates else rules
        except Exception as e:  # noqa: BLE001 - best effort; keep profile weights
            logger.debug("Could not read stackup copper for DRU: %s", e)
            return rules

    def _load_net_classes(self, result: ManufacturingResult) -> list | None:
        """Load the board's net-class-map sidecar as deduped classes (#8393).

        Mirrors ``kct check --emit-dru`` (same probe order, same loader,
        same dedup) so the exported ``.kicad_dru`` keeps the ampacity
        ``track_width`` floors that build-time DRC checked.  Returns
        ``None`` when there is no sidecar or it is malformed (warned).
        """
        from ..cli.check_cmd import (
            _discover_net_class_map_sidecar,
            resolve_net_class_map_for_board,
        )
        from ..manufacturers.project_generator import dedupe_net_classes

        ncm_path = _discover_net_class_map_sidecar(Path(self._lookup_pcb_path))
        if ncm_path is None:
            return None
        try:
            from ..router.rules import net_class_map_from_path
            from ..schema.pcb import PCB

            net_class_map = net_class_map_from_path(ncm_path, pcb_path=self._lookup_pcb_path)
            net_class_map, _ = resolve_net_class_map_for_board(
                net_class_map, PCB.load(str(self.pcb_path))
            )
        except Exception as e:
            msg = f"ignoring malformed net-class-map sidecar {ncm_path}: {e}"
            logger.warning(msg)
            result.warnings.append(msg)
            return None
        return dedupe_net_classes(net_class_map)

    def _resolve_ordered_copper(self, result: ManufacturingResult) -> None:
        """Record the outer/inner copper weight the order requests (#7209).

        Taken from the board stackup (matched against the fab profile), not a
        constant.  Best-effort: failures leave ``result.ordered_copper`` unset.
        """
        try:
            from ..schema.pcb import PCB
            from .preflight import resolve_ordered_copper

            info = resolve_ordered_copper(PCB.load(str(self.pcb_path)), self.manufacturer)
            if info["ordered_outer_oz"] is not None:
                if self.manufacturer.startswith("jlcpcb"):
                    info["source"] = "https://jlcpcb.com/help/article/jlcpcb-copper-weight"
                    info["source_verified"] = "2026-10-18"
                result.ordered_copper = info
        except Exception as e:
            logger.debug("Could not resolve ordered copper: %s", e)

    def _resolve_fabrication_process(self, result: ManufacturingResult) -> None:
        """Resolve the profile's declared via-in-pad process for this board (#5009).

        Mirrors :meth:`_write_drc_constraints`'s profile/layer-config
        resolution, but reads ``DesignRules.via_in_pad_process_id`` and
        looks it up in the shared
        :data:`~kicad_tools.manufacturers.fabrication_process.FABRICATION_PROCESSES`
        registry via ``describe_selection``.  Sets ``result.fabrication_process``
        to the resolved machine-readable dict (with derived
        ``ordering_instructions``), or leaves it ``None`` when the profile
        declares no eligible process for this layer/copper configuration --
        the common case, since most manufacturer profiles never set
        ``via_in_pad_process_id``.  Failures are swallowed (debug-logged
        only): this is an additive, best-effort enrichment step that must
        never block or degrade the rest of export.
        """
        try:
            from ..manufacturers import get_profile
            from ..manufacturers.fabrication_process import describe_selection

            profile = get_profile(self.manufacturer)
            layers, copper_oz = self._detect_layer_config()
            rules = profile.get_design_rules(layers=layers, copper_oz=copper_oz)
            result.fabrication_process = describe_selection(rules)
        except Exception as e:
            logger.debug(
                "Could not resolve fabrication process for manufacturer '%s': %s",
                self.manufacturer,
                e,
            )

    def _dry_run(self, out_dir: Path, result: ManufacturingResult) -> ManufacturingResult:
        """Populate result with what *would* be generated."""
        # Issue #9422: a real run refuses (RuleLooseningError) when the DRU
        # merge would loosen an authored rule; surface the same refusal here,
        # without writing the sidecars.
        if self.config.emit_drc_constraints:
            self._write_drc_constraints(result, dry_run=True)
        bom_name = self.config.bom_filename.format(manufacturer=self.manufacturer)
        pnp_name = self.config.pnp_filename.format(manufacturer=self.manufacturer)

        result.assembly_result = AssemblyPackageResult(
            output_dir=out_dir,
            bom_path=out_dir / bom_name if self.config.include_bom else None,
            pnp_path=out_dir / pnp_name if self.config.include_pnp else None,
            gerber_path=(
                out_dir / self.config.gerbers_subdir / "gerbers.zip"
                if self.config.include_gerbers
                else None
            ),
        )
        if self.config.include_report:
            try:
                from ..report.renderers import pdf_renderer_available

                if pdf_renderer_available() is not None:
                    result.report_path = out_dir / "report.pdf"
                else:
                    result.report_path = out_dir / "report.md"
            except ImportError:
                result.report_path = out_dir / "report.md"
        if self.config.include_project_zip:
            result.project_zip_path = out_dir / self.config.project_zip_name
        if self.config.include_manifest:
            result.manifest_path = out_dir / self.config.manifest_name
        if self._purchasing:
            result.purchasing_path = out_dir / PURCHASING_FILENAME
        return result

    def _generate_assembly(self, out_dir: Path, result: ManufacturingResult) -> bool:
        """Run BOM + CPL + Gerber generation.

        Returns:
            ``True`` when the assembly stage was attempted successfully
            (whether or not its individual artefacts succeeded), ``False``
            when the stage aborted before producing any files -- in which
            case the caller should skip subsequent stages (Report, Project
            ZIP, Manifest) so we don't leave a partial package on disk.
        """
        try:
            assembly = AssemblyPackage(
                pcb_path=self.pcb_path,
                schematic_path=self.schematic_path,
                manufacturer=self.manufacturer,
                config=self.config,  # ManufacturingConfig extends AssemblyConfig
            )
        except KiCadFileNotFoundError as e:
            # Construction failed (e.g. schematic missing for a
            # schematic-sourced BOM).  No artefacts have been written.
            # Bail out hard so we don't produce a partial package.
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")
            return False
        except Exception as e:
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")
            return False

        try:
            result.assembly_result = assembly.export(out_dir)
            if result.assembly_result.errors:
                result.errors.extend(result.assembly_result.errors)
        except LCSCDependencyMissingError as e:
            # The optional ``parts`` extra is missing, so --auto-lcsc could
            # not populate any LCSC part numbers.  This is raised out of BOM
            # generation BEFORE the BOM CSV (the first artefact) is written,
            # so nothing is on disk yet.  Abort the whole pipeline so we
            # never finalize a manifest/bundle whose BOM has an empty
            # ``LCSC Part #`` column; ``kct export`` then exits non-zero with
            # this actionable install hint (issue #4104).
            result.errors.append(str(e))
            logger.error("LCSC auto-matching requires the 'parts' extra: %s", e)
            return False
        except Exception as e:
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")
            return True  # files may already have been written

        return True

    def _load_purchasing(self, result: ManufacturingResult) -> None:
        """Resolve the project's supplementary purchasing rows (Issue #10431).

        The rows come from the ``.kct`` spec the BOM overlay reads.  A row
        that fails validation refuses the export: a list that cannot be
        trusted must neither ship nor quietly disappear.  A project that
        declares no rows is untouched by this feature, even when its spec is
        invalid elsewhere -- that is the BOM overlay's warning to raise.
        """
        self._purchasing = None
        if self.config.no_spec:
            return
        from pydantic import ValidationError

        from ..spec.parser import load_spec_data
        from .bom_spec_overlay import find_spec_file, load_assembly_spec

        try:
            spec_path = self.config.spec_path or find_spec_file(self.pcb_path.parent)
        except OSError as exc:
            if self._prior_purchasing_digest is not None:
                result.errors.append(
                    f"Cannot read the spec for the previous purchasing list: {exc}"
                )
            logger.warning("Purchasing list not exported: spec lookup failed: %s", exc)
            return
        if spec_path is None:
            return
        try:
            spec, _ = load_assembly_spec(spec_path)
        except ValidationError as exc:
            try:
                declared = load_spec_data(spec_path)
            except (OSError, ValueError):
                declared = {}
            errors = exc.errors()
            if (
                self._prior_purchasing_digest is None
                and not declared.get("purchasing")
                and not any(error["loc"][:1] == ("purchasing",) for error in errors)
            ):
                return
            faults = "; ".join(_spec_fault(error, declared) for error in errors)
            result.errors.append(
                f"Invalid project spec; the purchasing list cannot be read from {spec_path}: {faults}"
            )
            logger.error("Purchasing list could not be read from %s: %s", spec_path, faults)
            return
        except (OSError, ValueError) as exc:
            if self._prior_purchasing_digest is not None:
                result.errors.append(
                    f"Cannot read {spec_path} for the previous purchasing list: {exc}"
                )
            logger.warning(
                "Purchasing list not exported: project spec could not be read from %s: %s",
                spec_path,
                exc,
            )
            return
        rows = spec.purchasing or []
        self._purchasing = rows or None
        result.purchasing_pending = [row.description for row in rows if row.pending is not None]

    def _write_purchasing(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Stage the per-board purchasing list, or withdraw an earlier one."""
        if self._purchasing is None and self._prior_purchasing_digest is None:
            return
        path = out_dir / PURCHASING_FILENAME
        try:
            staged_digest = _sha256_file(path) if path.is_file() else None
            if staged_digest != self._prior_purchasing_digest:
                raise OSError(f"{path.name} changed while preparing the export")
            if self._purchasing is None:
                path.unlink()
                return
            document = _purchasing_document(self._purchasing)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            result.errors.append(f"Purchasing list generation failed: {exc}")
            logger.error("Purchasing list generation failed: %s", exc)
            return
        result.purchasing_path = path
        logger.info(f"Generated purchasing list: {path}")

    def _generate_report(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Generate a Markdown design report."""
        try:
            from ..report.collector import ReportDataCollector
            from ..report.generator import ReportGenerator
            from ..report.models import ReportData
            from ..report.net_class_map import resolve_committed_net_class_map
            from ..report.utils import find_schematic
        except ImportError as exc:
            # The optional ``report`` extra is missing.  Record a hard error
            # (not just a log line) so ``kct export`` exits non-zero instead
            # of silently shipping a bundle without report.md / images/
            # (issue #8203, mirrors #4104).  The rest of the bundle is still
            # produced; ``--no-report`` skips this step entirely.
            missing = exc.name or str(exc)
            msg = (
                f"Report generation failed: required dependency '{missing}' "
                "is not installed. Install it with: pip install "
                "'kicad-tools[report]' (or pass --no-report to skip the report)."
            )
            result.errors.append(msg)
            logger.error(msg)
            return

        try:
            # Pre-determine the version directory so collected data and report
            # land in the same vN/ directory.
            version_dir = ReportGenerator.next_version_dir(out_dir)
            data_dir = version_dir / "data"

            # Resolve a committed net_class_map.json sidecar (Part B of
            # #4008). Without it the report's DRC section runs the three
            # sidecar-gated rule families (diffpair_length_skew,
            # diffpair_routing_continuity, match_group_length_skew) as no-ops
            # and would print "Errors 0 / PASS" on boards that actually have
            # blocking diff-pair / match-group errors (e.g. board 07 has 9).
            # Boards without a committed sidecar resolve to None and keep the
            # documented graceful no-op behavior.
            net_class_map_path = resolve_committed_net_class_map(self.pcb_path)

            # Collect design data snapshots
            collector = ReportDataCollector(
                pcb_path=self.pcb_path,
                manufacturer=self.manufacturer,
                net_class_map_path=net_class_map_path,
                schematic_path=self.schematic_path or find_schematic(self.pcb_path),
                design_rules=self.config.report_design_rules,
            )
            collector.collect_all(data_dir)

            # Load collected JSON snapshots into ReportData kwargs
            data_kwargs = self._load_report_data_dir(data_dir)

            # Generate figures (PCB renders + schematic screenshots)
            figures_data = self._generate_figures(version_dir, result)
            if figures_data:
                data_kwargs.update(figures_data)

            # Hand-solder (THT) set excluded from the SMT CPL (issue #3539):
            # surface the exact refs the assembler must hand-place.
            tht_components = self._tht_component_groups(result)
            if tht_components:
                data_kwargs["tht_components"] = tht_components

            project_name = self.pcb_path.stem
            data = ReportData(
                project_name=project_name,
                revision=data_kwargs.pop("revision", "1"),
                date=data_kwargs.pop(
                    "date",
                    _now_utc().strftime("%Y-%m-%d"),
                ),
                manufacturer=self.manufacturer,
                **data_kwargs,
            )

            generator = ReportGenerator()
            report_path = generator.generate(data, out_dir, version_dir=version_dir)
            result.report_path = report_path
            logger.info(f"Generated report: {result.report_path}")

            # Attempt to render Markdown to HTML then PDF
            self._render_report_pdf(report_path, version_dir, result)
        except Exception as e:
            result.errors.append(f"Report generation failed: {e}")
            logger.error(f"Report generation failed: {e}")

    @staticmethod
    def _warn_figures_skipped(result: ManufacturingResult, reason: str) -> None:
        """Record a *loud* warning that the bundle will ship without figures.

        Figure generation degrading silently is how committed bundles ended
        up without per-layer renders / assembly views (issue #3583): the
        skip reason only went to the module logger, which `kct export`
        does not surface.  Recording it on ``result.warnings`` makes the
        CLI print it and JSON output include it.
        """
        msg = (
            f"Report figures skipped: {reason}. "
            "The bundle will NOT include images/ (per-copper-layer renders, "
            "front/back assembly views, schematic sheets). "
            "Figure generation needs kicad-cli (KiCad 8+) and cairosvg "
            "(pip install 'kicad-tools[screenshot]', or `uv sync` in a dev checkout)."
        )
        result.warnings.append(msg)
        logger.warning(msg)

    def _generate_figures(self, version_dir: Path, result: ManufacturingResult) -> dict | None:
        """Generate PCB and schematic figures for the report.

        Returns a dict with ``pcb_figures`` and/or ``schematic_sheets``
        keys suitable for merging into ReportData kwargs, or ``None``
        if figure generation is unavailable or fails.

        Every skip path records a warning on *result* (issue #3583) so
        missing figures are visible in the export output instead of the
        bundle silently shipping without its images/ directory.
        """
        try:
            from ..report.figures import ReportFigureGenerator
            from ..report.utils import find_schematic
        except ImportError as exc:
            self._warn_figures_skipped(result, f"dependency not available ({exc})")
            return None

        sch_path = self.schematic_path
        if sch_path is None:
            sch_path = find_schematic(self.pcb_path)
        if sch_path is None:
            self._warn_figures_skipped(
                result,
                f"no schematic found next to {self.pcb_path.name} (use --sch to specify one)",
            )
            return None

        figures_dir = version_dir / "figures"
        try:
            fig_gen = ReportFigureGenerator()
            entries = fig_gen.generate_all(self.pcb_path, sch_path, figures_dir)
        except (RuntimeError, OSError) as exc:
            self._warn_figures_skipped(result, str(exc))
            return None

        if not entries:
            self._warn_figures_skipped(result, "figure generator produced no images")
            return None

        # Convert FigureEntry list to ReportData-compatible dicts
        type_to_key = {
            "pcb_front": "front",
            "pcb_back": "back",
            "pcb_copper": "copper",
            "assembly": "assembly",
        }
        result: dict = {}

        pcb_figs: dict[str, str] = {}
        for entry in entries:
            key = type_to_key.get(entry.figure_type)
            if key is not None:
                pcb_figs[key] = f"figures/{entry.filename}"
        if pcb_figs:
            result["pcb_figures"] = pcb_figs

        # Per-copper-layer renders (issue #3497): one image per copper
        # layer, in stackup order.
        layer_figs = [
            {
                "name": entry.caption.removeprefix("Copper Layer "),
                "figure_path": f"figures/{entry.filename}",
            }
            for entry in entries
            if entry.figure_type == "pcb_layer"
        ]
        if layer_figs:
            result["pcb_layer_figures"] = layer_figs

        sch_sheets = [
            {"name": entry.caption, "figure_path": f"figures/{entry.filename}"}
            for entry in entries
            if entry.figure_type == "schematic"
        ]
        if sch_sheets:
            result["schematic_sheets"] = sch_sheets

        logger.info(
            f"Generated {len(entries)} figure(s): {len(pcb_figs)} PCB, "
            f"{len(layer_figs)} per-layer, {len(sch_sheets)} schematic"
        )
        return result or None

    @staticmethod
    def _render_report_pdf(
        report_path: Path,
        version_dir: Path,
        result: ManufacturingResult,
    ) -> None:
        """Render the Markdown report to PDF.

        Tries weasyprint (HTML→PDF) first, then pandoc+TeX as fallback.
        Degrades gracefully: if neither is available, a warning is logged
        and only the ``.md`` file remains.
        """
        try:
            from ..report.renderers import pdf_renderer_available
        except (ImportError, OSError):
            # OSError can surface here on hosts where the renderers module
            # eagerly probes a native library (libgobject/pango/cairo) at
            # import time. Treat it the same as a missing package: degrade
            # to Markdown-only and keep the manufacturing bundle successful.
            logger.warning(
                "PDF report rendering skipped: install 'kicad-tools[report]' for PDF output"
            )
            return

        renderer = pdf_renderer_available()
        if renderer is None:
            logger.warning(
                "PDF report rendering skipped: install weasyprint or pandoc+TeX for PDF output"
            )
            return

        pdf_path = report_path.with_suffix(".pdf")
        figures_dir = version_dir / "figures"

        try:
            if renderer == "weasyprint":
                from ..report.renderers import render_html, render_pdf

                md_content = report_path.read_text(encoding="utf-8")
                html_content = render_html(
                    md_content,
                    figures_dir=figures_dir if figures_dir.is_dir() else None,
                )
                render_pdf(html_content, pdf_path)
            else:
                from ..report.renderers import render_pdf_pandoc

                render_pdf_pandoc(report_path, pdf_path)

            result.report_path = pdf_path
            logger.info(f"Generated PDF report via {renderer}: {pdf_path}")
        except Exception as exc:
            logger.warning(f"PDF report rendering via {renderer} failed: {exc}")
            return

        # Bundle-integrity check (issue #4023): when the report references
        # figures and those figures exist on disk, the generated PDF must
        # embed at least one image object. A figure-less PDF here means the
        # renderer silently dropped every image (e.g. pandoc resolving
        # relative paths against the wrong cwd) — surface it loudly instead
        # of shipping a broken bundle. This guard runs for BOTH the
        # weasyprint and pandoc code paths.
        ManufacturingPackage._verify_report_pdf_images(pdf_path, report_path, figures_dir)

    @staticmethod
    def _verify_report_pdf_images(
        pdf_path: Path,
        report_path: Path,
        figures_dir: Path,
    ) -> None:
        """Warn loudly if a figure-referencing report produced a figure-less PDF.

        Only fires when the Markdown references images AND the figures
        directory is non-empty; a report that legitimately has no figures
        (empty/absent figures dir, or no ``![...]`` references) is not a
        violation.
        """
        from ..report.renderers import count_pdf_images

        # No figures on disk → nothing to embed → not a violation.
        if not figures_dir.is_dir():
            return
        if not any(figures_dir.glob("*.png")):
            return

        # No image references in the Markdown → not a violation.
        try:
            md_content = report_path.read_text(encoding="utf-8")
        except OSError:
            return
        if "![" not in md_content:
            return

        image_count = count_pdf_images(pdf_path)
        if image_count < 1:
            logger.warning(
                "PDF report integrity check FAILED: %s embeds 0 images despite "
                "%d figure(s) in %s and image references in the report. Figures "
                "were silently dropped during rendering — the bundle is missing "
                "its visuals.",
                pdf_path,
                len(list(figures_dir.glob("*.png"))),
                figures_dir,
            )

    @staticmethod
    def _load_report_data_dir(data_dir: Path) -> dict:
        """Load JSON snapshot files from *data_dir* into ReportData kwargs.

        Mirrors the logic in ``cli/report_cmd.py:_load_data_dir`` but is
        self-contained so the manufacturing exporter has no dependency on the
        CLI layer.
        """
        import json as _json

        mappings = {
            "board_summary.json": "board_stats",
            "bom.json": "bom_groups",
            "drc_summary.json": "drc",
            "erc_summary.json": "erc",
            "audit.json": "audit",
            "net_status.json": "net_status",
            "cost.json": "cost",
            "analog_components.json": "analog_components",
            "narrative.json": "_narrative",
            "stackup.json": "stackup",
            "off_board.json": "off_board",
        }

        result: dict = {}
        for filename, field_name in mappings.items():
            json_path = data_dir / filename
            if json_path.exists():
                with open(json_path, encoding="utf-8") as f:
                    raw = _json.load(f)
                # Unwrap the envelope written by ReportDataCollector
                data = raw.get("data") if isinstance(raw, dict) else raw
                if data is None:
                    continue
                result[field_name] = data

        # BOM: collector nests group list under ``groups`` key;
        # ReportData.bom_groups expects a plain list[dict].
        if "bom_groups" in result and isinstance(result["bom_groups"], dict):
            result["bom_groups"] = result["bom_groups"].get("groups", [])

        # Analog components: collector nests list under ``components``;
        # ReportData.analog_components expects a plain list[dict].
        if "analog_components" in result and isinstance(result["analog_components"], dict):
            result["analog_components"] = result["analog_components"].get("components", [])

        # Narrative: the collector writes a single dict with sub-keys;
        # unpack into individual ReportData fields.
        if "_narrative" in result and isinstance(result["_narrative"], dict):
            narrative = result.pop("_narrative")
            for key in (
                "design_narrative",
                "functional_blocks",
                "interfaces",
                "power_architecture",
                "assembly_notes",
            ):
                val = narrative.get(key)
                if val is not None:
                    result[key] = val
        else:
            result.pop("_narrative", None)

        return result

    def _flatten_latest_report(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Promote the latest report to the package root and clean up.

        When ``config.latest_report_only`` is ``True``, this post-processing
        step copies the latest ``vN/`` contents into a temporary staging area,
        promotes ``report.pdf`` (or ``report.md`` if PDF is unavailable) to
        the package root, and removes all ``vN/`` directories.

        Report figures (per-layer renders, assembly view, schematic
        sheets) are promoted to an ``images/`` subdirectory so the
        package ships the visuals alongside ``report.md`` (issue #3497),
        and the markdown's relative ``figures/`` references are rewritten
        to ``images/`` so they resolve inside the package.

        If ``config.keep_build_artifacts`` is ``True``, intermediate files
        (markdown source, figures, data, metadata) are preserved in a
        ``.build/report/`` subdirectory.  Otherwise they are discarded.
        """
        try:
            from ..report.generator import ReportGenerator
        except ImportError:
            return

        latest = ReportGenerator.latest_version_dir(out_dir)
        if latest is None:
            # No versioned directories (e.g. --no-report was also set)
            return

        # Stage the latest version contents in a temporary directory
        report_staging = out_dir / "report"
        if report_staging.exists():
            shutil.rmtree(report_staging)
        shutil.copytree(latest, report_staging)

        # Remove all vN/ directories from the output
        for child in list(out_dir.iterdir()):
            if child.is_dir() and re.fullmatch(r"v\d+", child.name):
                shutil.rmtree(child)

        # Promote the report file (PDF preferred, MD fallback) to package root
        staged_pdf = report_staging / "report.pdf"
        staged_md = report_staging / "report.md"

        if staged_pdf.exists():
            promoted = out_dir / "report.pdf"
            shutil.copy2(staged_pdf, promoted)
            result.report_path = promoted
            # Also preserve the markdown source alongside the PDF
            if staged_md.exists():
                promoted_md = out_dir / "report.md"
                shutil.copy2(staged_md, promoted_md)
                result.report_md_path = promoted_md
        elif staged_md.exists():
            promoted = out_dir / "report.md"
            shutil.copy2(staged_md, promoted)
            result.report_path = promoted

        # Promote report figures to images/ at the package root so the
        # bundle ships per-layer + assembly renders (issue #3497).
        self._promote_report_images(out_dir, report_staging, result)

        # Handle build artifacts
        if self.config.keep_build_artifacts:
            build_dir = out_dir / ".build" / "report"
            if build_dir.exists():
                shutil.rmtree(build_dir)
            build_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(report_staging), str(build_dir))
            logger.info(f"Preserved build artifacts in {build_dir}")
        else:
            shutil.rmtree(report_staging)

        logger.info(f"Promoted report to {result.report_path}")

    @staticmethod
    def _promote_report_images(
        out_dir: Path,
        report_staging: Path,
        result: ManufacturingResult,
    ) -> None:
        """Copy staged report figures into ``<out_dir>/images/``.

        The report generator writes figures (per-copper-layer renders,
        front/back/assembly views, schematic sheets) into the versioned
        report's ``figures/`` subdirectory.  This step promotes those PNGs
        to a package-root ``images/`` directory, rewrites the promoted
        ``report.md``'s relative ``figures/`` image references to
        ``images/``, and records the image paths on *result* so the
        manifest includes their checksums (issue #3497).

        When the staged report has no figures (e.g. figure generation
        was skipped because kicad-cli/cairosvg are missing), any
        pre-existing ``images/`` directory from an earlier export is
        REMOVED rather than left behind: stale renders that no longer
        match the gerbers are worse than absent ones, and silently
        keeping them is how the softstart bundle shipped pre-repair
        visuals (issue #3583).
        """
        images_dir = out_dir / "images"
        staged_figures = report_staging / "figures"
        pngs = (
            sorted(p for p in staged_figures.iterdir() if p.suffix.lower() == ".png")
            if staged_figures.is_dir()
            else []
        )
        if not pngs:
            if images_dir.is_dir():
                shutil.rmtree(images_dir)
                msg = (
                    "Removed stale images/ from a previous export: this run "
                    "generated no report figures, and stale renders must not "
                    "ship alongside regenerated gerbers"
                )
                result.warnings.append(msg)
                logger.warning(msg)
            return

        if images_dir.exists():
            shutil.rmtree(images_dir)
        images_dir.mkdir(parents=True)

        for png in pngs:
            dest = images_dir / png.name
            shutil.copy2(png, dest)
            result.image_paths.append(dest)

        # Rewrite relative figure references in the promoted markdown so
        # they resolve against the package layout (figures/ -> images/).
        promoted_md = out_dir / "report.md"
        if promoted_md.exists():
            text = promoted_md.read_text(encoding="utf-8")
            rewritten = text.replace("](figures/", "](images/")
            if rewritten != text:
                promoted_md.write_text(rewritten, encoding="utf-8")

        logger.info(f"Promoted {len(pngs)} report image(s) to {images_dir}")

    @staticmethod
    def _tht_component_groups(result: ManufacturingResult) -> list[dict]:
        """Group the CPL's excluded THT components for the report.

        Returns BOM-style rows ``[{value, footprint, qty, refs}]`` built
        from ``assembly_result.tht_excluded`` (issue #3539), or an empty
        list when no THT parts were excluded (SMD-only board, or
        ``exclude_tht=False``).
        """
        if result.assembly_result is None or not result.assembly_result.tht_excluded:
            return []

        from .pnp import group_tht_exclusions

        return group_tht_exclusions(result.assembly_result.tht_excluded)

    def _generate_readme(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Generate a README.txt describing the manufacturing package contents."""
        lines = [
            f"Manufacturing Package for {self.pcb_path.stem}",
            "=" * 50,
            "",
            f"Manufacturer: {self.manufacturer}",
            f"Generated by: kicad-tools {kicad_tools.__version__}",
            f"Generated at: {_now_utc().strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "Contents:",
            "---------",
        ]

        file_descriptions = {
            "bom": ("BOM (Bill of Materials)", "Component list for PCB assembly ordering"),
            "pnp": (
                "CPL (Component Placement List)",
                "Pick-and-place coordinates for SMT/THT assembly",
            ),
            "gerber": (
                "Gerber files",
                (
                    "PCB fabrication data (copper layers, silkscreen, solder mask,"
                    " solder paste (stencil) where the preset plots it, drill)"
                ),
            ),
            "report": ("Design report", "Manufacturing readiness report with DRC/ERC results"),
            "images": (
                "Report images",
                "Board renders: per-copper-layer, front/back, assembly view, schematic sheets",
            ),
            "project_zip": (
                "KiCad project archive",
                "Source KiCad project files (.kicad_pcb, .kicad_sch, .kicad_pro)",
            ),
            "manifest": ("Manifest", "SHA256 checksums for all output files"),
            "purchasing": (
                "Purchasing list (supplementary)",
                "Accessories and off-board parts to buy per board; not placed or supplied by the factory",
            ),
        }

        if result.assembly_result:
            if result.assembly_result.bom_path:
                desc = file_descriptions["bom"]
                lines.append(f"  {result.assembly_result.bom_path.name}")
                lines.append(f"    {desc[0]}: {desc[1]}")
                lines.append("")
            if result.assembly_result.pnp_path:
                desc = file_descriptions["pnp"]
                lines.append(f"  {result.assembly_result.pnp_path.name}")
                lines.append(f"    {desc[0]}: {desc[1]}")
                lines.extend(
                    f"    {line}" for line in placement_preview_instructions(self.manufacturer)
                )
                tht_excluded = result.assembly_result.tht_excluded
                if tht_excluded:
                    refs = ", ".join(p.reference for p in tht_excluded)
                    lines.append(
                        f"    Note: {len(tht_excluded)} through-hole component(s) are"
                        " excluded from this file and must be hand-soldered"
                        f" after SMT assembly: {refs}"
                    )
                lines.append("")
            if result.assembly_result.gerber_path:
                desc = file_descriptions["gerber"]
                lines.append(f"  {result.bundle_relpath(result.assembly_result.gerber_path)}")
                lines.append(f"    {desc[0]}: {desc[1]}")
                lines.append("")

        if result.purchasing_path:
            desc = file_descriptions["purchasing"]
            lines.append(f"  {result.bundle_relpath(result.purchasing_path)}")
            lines.append(f"    {desc[0]}: {desc[1]}")
            if result.purchasing_pending:
                lines.append(
                    f"    {len(result.purchasing_pending)} selection(s) still pending: "
                    + "; ".join(result.purchasing_pending)
                )
            lines.append("")

        if result.report_path:
            desc = file_descriptions["report"]
            lines.append(f"  {result.report_path.name}")
            lines.append(f"    {desc[0]}: {desc[1]}")
            lines.append("")

        if result.image_paths:
            desc = file_descriptions["images"]
            lines.append(f"  images/ ({len(result.image_paths)} files)")
            lines.append(f"    {desc[0]}: {desc[1]}")
            lines.append("")

        if result.project_zip_path:
            desc = file_descriptions["project_zip"]
            lines.append(f"  {result.project_zip_path.name}")
            lines.append(f"    {desc[0]}: {desc[1]}")
            lines.append("")

        desc = file_descriptions["manifest"]
        lines.append("  manifest.json")
        lines.append(f"    {desc[0]}: {desc[1]}")
        lines.append("")

        # Issue #5009: surface machine-derived factory ordering instructions
        # when this board's manufacturer profile declares a via-in-pad
        # process eligible for its layer/copper configuration -- generalizes
        # the hand-typed README prose boards/03-usb-joystick previously
        # wrote for its four-layer POFV contract into text derived from the
        # shared FabricationProcess registry.
        if result.fabrication_process:
            lines.append("Fabrication process requirements:")
            lines.append("-" * 34)
            instructions = result.fabrication_process.get("ordering_instructions")
            if instructions:
                lines.append(f"  {instructions}")
            else:
                lines.append(f"  Process: {result.fabrication_process.get('name', '(unnamed)')}")
            lines.append("")

        if result.ordered_copper:
            oc = result.ordered_copper
            lines.append("Copper weight to order:")
            lines.append("-" * 23)
            lines.append(
                f"  Outer {oc['ordered_outer_oz']:g} oz / inner {oc['ordered_inner_oz']:g} oz "
                f"({oc['layers']} layers; board stackup: {oc['status']})"
            )
            if oc.get("source"):
                lines.append(f"  Source: {oc['source']} (verified {oc['source_verified']})")
            lines.append("")

        # Issue #10109: the editable-project handoff contract.  Only written
        # when the footprint-to-symbol instance link check found defects --
        # a clean board's README stays byte-identical.
        if result.instance_links and result.instance_links.get("findings"):
            links = result.instance_links
            lines.append("Editing handoff -- footprint to symbol links:")
            lines.append("-" * 43)
            lines.append("  This board's kicad_project.zip cannot be fully cross-probed or")
            lines.append("  updated from its schematic: some footprints' symbol-instance")
            lines.append("  links are missing, duplicated or stale.")
            lines.append("")
            checked = links.get("checked", 0)
            linked = links.get("linked", 0)
            lines.append(f"  Footprints checked: {checked}, intact links: {linked}")
            lines.append("")
            findings = links.get("findings", [])
            for finding in findings:
                lines.append(f"  {finding.get('reference', '?')}: {finding.get('category', '?')}")
                lines.append(f"    {finding.get('detail', '')}")
            skipped = links.get("skipped_board_only") or []
            if skipped:
                lines.append("")
                lines.append(
                    "  Board-only footprints (excluded from the check): " + ", ".join(skipped)
                )
            lines.append("")

        if result.simulation_evidence is not None:
            from .simulation_evidence import readme_lines

            drc = next((pr for pr in result.preflight_results if pr.name == "drc"), None)
            lines.extend(
                readme_lines(
                    result.simulation_evidence, f"{drc.status} ({drc.message})" if drc else None
                )
            )

        if result.reinforcement_clearance:
            from .reinforcement_clearance import summary_lines

            clearance = result.reinforcement_clearance
            title = "Reinforcement clearance (nominal, measured on the shipped Gerbers):"
            lines.append(title)
            lines.append("-" * len(title))
            lines.append(f"  Gerber archive: {clearance['archive']['member']}")
            lines.extend(summary_lines(clearance))
            lines.append(
                "  Nominal geometry for engineering review, with no spacing verdict: the "
                "board's electrical spacing decision and the fabrication checks are separate. "
                "Distances and artwork digests: manifest.json, preflight entry "
                "reinforcement_clearance."
            )
            lines.append("")

        readme_path = out_dir / "README.txt"
        readme_path.write_text("\n".join(lines), encoding="utf-8")
        result.readme_path = readme_path
        logger.info(f"Generated README: {readme_path}")

    def _generate_project_zip(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Create ZIP of KiCad project files."""
        try:
            result.project_zip_path = _create_project_zip(
                self.pcb_path,
                out_dir,
                self.config.project_zip_name,
                schematic_path=self.schematic_path,
                project_root=self.config.project_root,
            )
        except Exception as e:
            result.errors.append(f"Project ZIP creation failed: {e}")
            logger.error(f"Project ZIP creation failed: {e}")
            return
        # The archive above is the editable-project handoff: it carries the
        # board and its schematics back to KiCad's editors.  Check the
        # footprint-to-symbol instance links of exactly that pair so a
        # user returning to the editors knows whether cross-probing and
        # "update footprints from schematic" will work (Issue #10109).
        self._check_symbol_links(result)

    def _check_symbol_links(self, result: ManufacturingResult) -> None:
        """Run the instance-link check for the packaged board (Issue #10109).

        Uses the same root schematic the project ZIP bundles (an explicit
        ``--sch`` wins, otherwise the sibling auto-discovery), so the report
        always describes the archive that ships.  Read-only: findings land
        in ``result.instance_links`` and ``result.warnings``; nothing is
        rewritten and no finding blocks the export.
        """
        from kicad_tools.report.utils import find_schematic
        from kicad_tools.sync.instance_links import check_instance_links

        root_sch = self.schematic_path or find_schematic(self.pcb_path)
        if root_sch is None or not Path(root_sch).is_file():
            logger.info(
                "No schematic resolved for %s; skipping footprint symbol-link check",
                self.pcb_path,
            )
            return
        try:
            report = check_instance_links(root_sch, self.pcb_path)
        except Exception as e:
            logger.warning("Footprint symbol-link check failed for %s: %s", self.pcb_path, e)
            return
        result.instance_links = report.to_dict()
        for line in report.warning_lines():
            result.warnings.append(line)
        if report.is_clean:
            logger.info("Footprint symbol-link check: %s", report.summary_line())

    def _collect_simulation_evidence(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Package and evaluate declared simulation cases (Issue #10468).

        Reads only the declaration and the files it names, inside the project
        root; nothing is executed.  Verdicts are recorded in ``result`` and
        reported as warnings -- they never change the export outcome, and a
        board without a declaration is untouched.
        """
        from .simulation_evidence import EVIDENCE_DIR, collect_simulation_evidence

        # Drop evidence left by an earlier export (or seeded into the candidate)
        # so it can never outlive its declaration.
        target = out_dir / EVIDENCE_DIR
        if target.exists():
            shutil.rmtree(target)
        evidence = collect_simulation_evidence(self._lookup_pcb_path, self.config.project_root)
        if evidence is None:
            return
        try:
            for rel, data in evidence.evidence_files().items():
                path = out_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                result.simulation_paths.append(path)
        except OSError as e:
            result.errors.append(f"Simulation evidence packaging failed: {e}")
            return
        result.simulation = evidence.to_dict()
        result.simulation_evidence = evidence
        result.warnings.extend(evidence.warning_lines())

    def _generate_manifest(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Write manifest.json with SHA256 checksums."""
        try:
            manifest = _build_manifest(result, self.pcb_path, self.manufacturer)
            manifest_path = out_dir / self.config.manifest_name
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            result.manifest_path = manifest_path
            logger.info(f"Generated manifest: {manifest_path}")
        except Exception as e:
            result.errors.append(f"Manifest generation failed: {e}")
            logger.error(f"Manifest generation failed: {e}")
