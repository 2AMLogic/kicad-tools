"""Footprint-to-symbol instance link check for native KiCad projects (Issue #10109).

A board can be electrically perfect -- nets, references and pad connectivity
all agreeing with its schematic -- while every footprint's ``(path ...)``
link back to the hierarchical symbol instance that placed it is missing,
duplicated or stale.  KiCad's editors use that token to cross-probe a
footprint to its symbol and to update footprints from the schematic, so a
board with broken links cannot be handed back to Eeschema/PCBeditors for
editing even though ``kct sync`` and DRC stay green.

This module compares the populated footprints of a board against the symbol
instance paths of a schematic hierarchy and classifies each eligible
footprint's link:

* ``missing_path``   -- the footprint carries no ``(path ...)`` token at all.
* ``duplicate_path`` -- two or more footprints share one path token.
* ``stale_path``     -- the path matches no instance of *this* reference
  (it belongs to another reference, or to nothing at all).
* ``nickname_missing`` / ``lib_id_mismatch`` -- the footprint's library
  identifier disagrees with the schematic's footprint assignment, reported
  separately from the path findings via the shared nickname-aware
  comparison (:func:`kicad_tools.footprints.library_path.compare_footprint_lib_ids`).

The check is read-only: it never rewrites a board.  Board-only footprints
(``board_only`` attr), power references and footprints whose reference has no
schematic symbol (reference-level drift, owned by the sync report) are
respected as exclusions, multi-unit symbols accept any of their unit's
instance paths, and schematic symbols excluded from the board
(``on_board no``) do not expect links.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.footprints.library_path import compare_footprint_lib_ids

logger = logging.getLogger(__name__)

#: Finding categories, in report order.  The first three are symbol-path
#: findings; the last two are library-identity findings, kept separate
#: because a link can be perfectly intact while the footprint's library
#: nickname drifted (and vice versa).
PATH_CATEGORIES = ("missing_path", "duplicate_path", "stale_path")
LIBRARY_CATEGORIES = ("nickname_missing", "lib_id_mismatch")
ALL_CATEGORIES = PATH_CATEGORIES + LIBRARY_CATEGORIES

#: Cap on per-finding lines folded into one-line warning surfaces such as
#: ``ManufacturingResult.warnings``.  The full list always lives in the
#: structured report (and the package README section); only the condensed
# warning surface is bounded so an 80-footprint mass failure stays readable.
MAX_WARNING_FINDING_LINES = 20


@dataclass
class FootprintLinkFinding:
    """One footprint's link (or library identity) defect."""

    reference: str
    category: str
    detail: str
    path: str = ""

    def to_dict(self) -> dict:
        d = {
            "reference": self.reference,
            "category": self.category,
            "detail": self.detail,
        }
        if self.path:
            d["path"] = self.path
        return d


@dataclass
class SymbolInstanceIndex:
    """Schematic-side symbol instances keyed for footprint-link comparison.

    Built by :func:`collect_symbol_instances` from a schematic hierarchy.
    Power symbols, symbols excluded from the board (``on_board no``) and
    unannotated references (``R?``) contribute no expected links; a
    do-not-populate symbol still occupies a footprint, so ``dnp`` symbols
    *are* indexed.
    """

    #: reference -> every instance path of that reference, one per placed
    #: unit -- a multi-unit symbol contributes several paths and any one of
    #: them is a valid footprint link.
    paths_by_reference: dict[str, set[str]] = field(default_factory=dict)
    #: instance path -> the reference that owns it (for stale-link detail).
    reference_by_path: dict[str, str] = field(default_factory=dict)
    #: reference -> the schematic's footprint assignment for that reference.
    footprint_by_reference: dict[str, str] = field(default_factory=dict)
    #: References whose every symbol is excluded from the board
    #: (``on_board no``): a footprint matching one of these is skipped
    #: separately from a plain unknown reference.
    excluded_references: set[str] = field(default_factory=set)
    #: Number of sheet placements walked (root plus every child placement).
    sheet_placements: int = 0

    @property
    def reference_count(self) -> int:
        return len(self.paths_by_reference)


def _symbol_is_power(sym) -> bool:
    """Power symbols carry no footprint and never cross-probe."""
    lib_id = getattr(sym, "lib_id", "") or ""
    reference = sym.reference or ""
    return lib_id.startswith("power:") or reference.startswith("#")


def collect_symbol_instances(schematic_path: str | Path) -> SymbolInstanceIndex:
    """Index every board-expected symbol instance of a schematic hierarchy.

    Walks the hierarchy with :func:`kicad_tools.schema.hierarchy.build_hierarchy`
    (one node per sheet *placement*, each carrying its KiCad instance path)
    and records ``"/<root uuid>[/<sheet uuid>...]/<symbol uuid>"`` per placed
    symbol -- the same token KiCad writes into a footprint's ``(path ...)``.
    Per-placement designators come from the symbol's ``(instances ...)``
    block when the sheet file is placed more than once (issue #5815).
    """
    from kicad_tools.schema.hierarchy import build_hierarchy
    from kicad_tools.schema.schematic import Schematic

    index = SymbolInstanceIndex()
    root = build_hierarchy(str(schematic_path))
    for node in root.all_nodes():
        index.sheet_placements += 1
        if not node.instance_path:
            # A missing UUID anywhere on the chain leaves no real KiCad
            # path to compare against; symbols on that placement cannot be
            # linked and are not indexed.
            logger.warning(
                "Sheet %r (%s) has no complete instance path; its symbols are not indexed",
                node.name,
                node.path,
            )
            continue
        try:
            sch = Schematic.load(node.path)
        except Exception as exc:  # unparseable/unloadable sheet placement
            logger.warning("Could not load sheet %s for symbol links: %s", node.path, exc)
            continue
        for sym in sch.symbols:
            if _symbol_is_power(sym):
                continue
            reference = sym.instance_reference(node.instance_path) or sym.reference
            if not reference or reference.endswith("?"):
                continue
            if not sym.on_board:
                index.excluded_references.add(reference)
                continue
            instance_path = f"{node.instance_path.rstrip('/')}/{sym.uuid}"
            index.paths_by_reference.setdefault(reference, set()).add(instance_path)
            index.reference_by_path[instance_path] = reference
            # Every unit of a multi-unit part shares the footprint field;
            # first writer wins and they are expected to agree.
            index.footprint_by_reference.setdefault(reference, sym.footprint)
    return index


@dataclass
class InstanceLinkReport:
    """Result of :func:`check_instance_links`."""

    #: Footprints compared against the schematic (the eligible set).
    checked: int = 0
    #: Footprints whose path resolves to one of their reference's instances.
    linked: int = 0
    #: Findings by reference, path findings first.
    findings: list[FootprintLinkFinding] = field(default_factory=list)
    #: Board-only footprints (``board_only`` attr) -- excluded by design.
    skipped_board_only: list[str] = field(default_factory=list)
    #: Footprints whose reference has no schematic symbol at all.  That is
    #: reference-level drift owned by the sync report, not a link defect.
    skipped_no_symbol: list[str] = field(default_factory=list)
    #: Footprints whose reference only matches symbols excluded from the
    #: board (``on_board no``).
    skipped_excluded_symbol: list[str] = field(default_factory=list)
    #: Schematic-side coverage: sheet placements walked and distinct
    #: board-expected references.
    sheet_placements: int = 0
    symbol_references: int = 0

    def count(self, category: str) -> int:
        return sum(1 for f in self.findings if f.category == category)

    @property
    def is_clean(self) -> bool:
        return not self.findings

    def summary_line(self) -> str:
        parts = [
            f"footprint-to-symbol instance links: {self.checked} checked, {self.linked} linked"
        ]
        if self.findings:
            by_cat = ", ".join(
                f"{self.count(cat)} {cat}" for cat in ALL_CATEGORIES if self.count(cat)
            )
            parts.append(f"{len(self.findings)} finding(s) ({by_cat})")
        return "; ".join(parts) + "."

    def finding_lines(self, limit: int | None = None) -> list[str]:
        """One line per finding, path findings first, bounded by ``limit``."""
        ordered = sorted(
            self.findings, key=lambda f: (ALL_CATEGORIES.index(f.category), f.reference)
        )
        lines = [f"{f.reference}: {f.category.replace('_', ' ')} -- {f.detail}" for f in ordered]
        if limit is not None and len(lines) > limit:
            lines = lines[:limit]
            lines.append(f"... and {len(ordered) - limit} more (see the report data)")
        return lines

    def warning_lines(self) -> list[str]:
        """Condensed warning block for one-line surfaces (export warnings)."""
        if self.is_clean:
            return []
        lines = [self.summary_line()]
        lines.extend(self.finding_lines(limit=MAX_WARNING_FINDING_LINES))
        return lines

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "linked": self.linked,
            "findings": [f.to_dict() for f in self.findings],
            "skipped_board_only": sorted(self.skipped_board_only),
            "skipped_no_symbol": sorted(self.skipped_no_symbol),
            "skipped_excluded_symbol": sorted(self.skipped_excluded_symbol),
            "sheet_placements": self.sheet_placements,
            "symbol_references": self.symbol_references,
        }


def check_instance_links(schematic_path: str | Path, pcb_path: str | Path) -> InstanceLinkReport:
    """Compare a board's footprint symbol-links against its schematic.

    Args:
        schematic_path: Root ``.kicad_sch`` of the schematic hierarchy the
            board belongs to.
        pcb_path: The ``.kicad_pcb`` board.

    Returns:
        :class:`InstanceLinkReport` with per-reference findings.  Never
        raises on a defective board -- every defect is a finding -- but the
        inputs must exist and parse.
    """
    from kicad_tools.schema.pcb import PCB

    index = collect_symbol_instances(schematic_path)
    pcb = PCB.load(pcb_path)

    report = InstanceLinkReport(
        sheet_placements=index.sheet_placements,
        symbol_references=index.reference_count,
    )

    eligible = []
    for fp in pcb.footprints:
        reference = fp.reference or ""
        if not reference or reference.startswith("#"):
            continue
        if "board_only" in fp.attr_flags:
            report.skipped_board_only.append(reference)
            continue
        if reference in index.excluded_references and reference not in index.paths_by_reference:
            report.skipped_excluded_symbol.append(reference)
            continue
        if reference not in index.paths_by_reference:
            report.skipped_no_symbol.append(reference)
            continue
        eligible.append(fp)

    # Duplicate links: one path token carried by more than one footprint.
    # Every carrier is reported (the shared token cross-probes ambiguously
    # whichever footprint it is read from).
    by_path: dict[str, list[str]] = {}
    for fp in eligible:
        if fp.path:
            by_path.setdefault(fp.path, []).append(fp.reference)
    duplicate_refs: dict[str, list[str]] = {}
    for path, refs in by_path.items():
        if len(refs) > 1:
            for ref in refs:
                others = [r for r in refs if r != ref]
                duplicate_refs.setdefault(ref, []).extend(others)

    for fp in sorted(eligible, key=lambda f: f.reference):
        reference = fp.reference
        path = fp.path or ""

        if not path:
            report.findings.append(
                FootprintLinkFinding(
                    reference=reference,
                    category="missing_path",
                    detail="footprint has no symbol-instance path; KiCad cannot cross-probe "
                    "or update it from the schematic",
                )
            )
        elif path in index.paths_by_reference.get(reference, set()):
            report.linked += 1
        else:
            owner = index.reference_by_path.get(path)
            if owner:
                detail = f"path points at {owner}'s symbol instance, not {reference}'s"
            else:
                detail = "path matches no symbol instance in the schematic hierarchy"
            report.findings.append(
                FootprintLinkFinding(
                    reference=reference, category="stale_path", detail=detail, path=path
                )
            )

        if reference in duplicate_refs:
            others = sorted(set(duplicate_refs[reference]))
            report.findings.append(
                FootprintLinkFinding(
                    reference=reference,
                    category="duplicate_path",
                    detail="path shared with " + ", ".join(others),
                    path=path,
                )
            )

        # Library identity, independently of the link: a footprint whose
        # link is intact can still carry a nickname-less or inconsistent
        # library identifier (the observed package had 78 of them).
        sch_fp = index.footprint_by_reference.get(reference, "")
        pcb_fp = fp.name or ""
        if sch_fp and pcb_fp:
            verdict = compare_footprint_lib_ids(sch_fp, pcb_fp)
            if verdict == "mismatch":
                report.findings.append(
                    FootprintLinkFinding(
                        reference=reference,
                        category="lib_id_mismatch",
                        detail=f"PCB footprint {pcb_fp!r} does not match schematic {sch_fp!r}",
                    )
                )
            elif verdict == "nickname_missing":
                report.findings.append(
                    FootprintLinkFinding(
                        reference=reference,
                        category="nickname_missing",
                        detail=f"footprint library nickname missing on one side "
                        f"(schematic {sch_fp!r}, PCB {pcb_fp!r})",
                    )
                )

    report.checked = len(eligible)
    return report
