"""Refill saved zones, re-plot and re-pin a board's hash-bound bundle (Issue #6404).

#6390 declared each demo board's router net classes in its routed sidecars,
and #6396 moved board 07's copper.  Both left the boards' saved zone fills
computed under the old rules (native DRC *without* ``--refill-zones`` reports
via/pad-to-zone clearance findings), and left each manufacturing bundle bound
to the old PCB.  ``refresh_readiness.py`` re-hashes evidence but cannot fix
either: its DRC gate refills a throwaway copy, and it carries the old
Gerbers forward.  Rebuilding only ``kicad_project.zip`` would make ``kct
check``'s manifest meta-check pass while the Gerbers, drawings and renders
still showed the old copper, so this driver re-plots them.

It works on a staged copy of the board and publishes nothing unless every
gate passes.  Stages, in order:

1. **Board evidence** (board 07): run ``real_design/build.py``, publish the
   routed PCB it validated, and copy its ``validation/`` evidence into
   ``output/readiness/`` and ``real_design/reviewed-routing/evidence/``.  The
   build must have filled and checked the board under the published routed
   sidecars (byte-identical ``.kicad_pro`` / ``.kicad_dru``); otherwise the
   evidence would describe different rules.  Evidence is copied only when it
   describes a different board than the committed evidence, so a rerun keeps
   the committed (date-stamped) reports.  The public summaries
   (``output/build.json`` / ``output/status.json``) are always rewritten from
   the build's routing-stage summary to describe the gated release (Issue
   #9955): the staging references do not resolve from ``output/`` and its
   pending-export flags describe the design stage.
2. **Refill**: refill the routed PCB's zones under its own sidecars with
   ``declare_router_netclasses_6390.refill_zones_in_place`` (a plain native
   refill, transplanting only the ``(zone ...)`` blocks).  Refuses if anything
   outside the zone blocks changes.  When the refilled zones are
   token-identical to the saved ones the PCB bytes are left alone.
3. **Saved-fill DRC gate**: ``kicad-cli pcb drc --severity-all`` *without*
   ``--refill-zones`` must report 0 errors, 0 zone clearance findings and 0
   unconnected items.  The existing gates only check refilled copper, which
   is how stale fills slipped through.
4. **Re-plot** when the bundle needs rebuilding or ``--force-replot`` is set.
   A fresh ``kct export`` supplies
   ``gerbers/gerbers.zip`` (re-zipped deterministically), ``images/*`` and the
   report; ``kicad-cli`` re-plots the assembly PDFs from the routed PCB and
   the schematic PDF from the published schematic (Issue #7675: a title-block
   edit left a stale ``schematic.pdf`` in the bundle, because only copper
   artifacts were re-plotted); ``kct render`` the board renders.  Also re-written: the saved-fill native DRC report,
   ``fill-consistency.json``, ``refill-stability.json`` and the ``kct check``
   report.  The fresh export's CPL must equal the committed one, or differ only in
   ``Mid X``/``Mid Y`` by matching the routed PCB's Gerber/drill frame (#7059,
   #7199): placement is not this driver's business.
   Board 04 (Issue #7357) re-plots only the ``gerber_members`` of its committed
   ``gerbers.zip`` (copper and F.Silkscreen; every other member keeps its bytes)
   instead of a whole ``kct export``. Its recipe writes ``output/drc_report.json``
   for the final copper before export and checks it again after re-hashing the
   manifest. If only the manifest's DRC entry is stale, the driver refreshes
   that entry from the bound report. The board-local ``check_manufacturing.py``
   supplies the check report. Its assembly PDFs are re-plotted from the
   published routed PCB, and its whole
   ``output/manufacturing`` bundle (report, README, manifest) is regenerated
   through the recipe's ``generate_manufacturing`` under the reviewed paid
   process with ``SOURCE_DATE_EPOCH`` anchored to ``--checked-at``, so the
   drawings follow the copper and two publishes write identical evidence
   (Issue #9536).
5. ``refresh_readiness.py``'s pipeline: native ERC, deterministic archive
   rebuilds, manifest re-hash (only ``files`` may change), its gates
   (refilled native DRC, ``kct check``, ``net-status``, ``validate --sync``),
   metric comparison, and the ``readiness.json`` re-pin validated by
   ``read_readiness()``.  A board registered with ``tier_check`` is also
   judged by the public verifier's generic check, and what it measures is the
   receipt published: ``blocked`` names the unaccepted findings and exits 1
   (Issue #10662).

Usage (repository root, KiCad 10 ``kicad-cli`` on PATH or ``KICAD_CLI_DIR``)::

    uv run --with shapely python scripts/boards/refresh_bundles_6404.py \\
        boards/07-matchgroup-test --checked-at 2026-10-13T00:00:00+00:00 \\
        --save-erc /tmp/erc

A rerun with the same ``--checked-at`` and ``--reuse-erc`` publishes 0 files.
``--build-dir DIR`` keeps board 07's ``build.py`` output in DIR, and replays
it when DIR already holds one.  ``--dry-run`` runs every gate and publishes
nothing.  ``--check`` (Issue #10692) is a ``--dry-run`` that anchors each board
to the ``checked_at`` its committed ``readiness.json`` already carries (so
``--checked-at`` is optional) and exits 1 when any bundle would publish a
file: a committed bundle can be self-consistent and still disagree with what
the current rules emit, and only a fresh run on the pinned toolchain shows it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _cpl_frame as cpl_frame  # noqa: E402
import declare_router_netclasses_6390 as netclasses  # noqa: E402
import refresh_copper_gerbers_6404 as copper_gerbers  # noqa: E402
import refresh_readiness as rr  # noqa: E402

GateFailure = rr.GateFailure
log = rr.log
sha256 = rr.sha256

REPO_ROOT = rr.REPO_ROOT


#: ``gerbers.zip`` members plotted from the saved copper fills / the front silk.
COPPER_MEMBER = copper_gerbers.COPPER_MEMBER
SILK_MEMBER = re.compile(r"-F_Silkscreen\.[A-Za-z0-9]+$")


@dataclass(frozen=True)
class BundleConfig:
    """Where one board keeps its plotted artifacts and evidence copies."""

    manufacturer: str  # ``kct export --mfr``
    #: Bundle-relative members taken from a fresh ``kct export``.
    export_members: tuple[str, ...] = (
        "gerbers/gerbers.zip",
        "images/*.png",
        "report.md",
        "report.pdf",
    )
    #: Bundle-relative assembly drawings and their plotted layers.
    assembly_pdfs: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("assembly-front.pdf", ("F.Cu", "F.SilkS", "Edge.Cuts")),
        ("assembly-back.pdf", ("B.Cu", "B.SilkS", "Edge.Cuts")),
    )
    #: Bundle-relative schematic PDFs re-plotted from ``BoardConfig.schematic``
    #: (Issue #7675).  A bundle that ships a ``schematic.pdf`` must list it here,
    #: or a schematic edit re-pins the old drawing as ready.
    schematic_pdfs: tuple[str, ...] = ("schematic.pdf",)
    #: Board-relative render directory re-plotted by ``kct render`` (or None).
    renders: str | None = "output/renders"
    #: Board-relative copies of the saved-fill native DRC report.  Never a path in
    #: ``BoardConfig.drc_targets``: those hold the refilled report, written by
    #: ``refresh_readiness.repin_staged`` (Issue #7353).
    saved_drc_reports: tuple[str, ...] = ()
    #: Board-relative copies of the saved-vs-refilled area deltas.
    fill_consistency: tuple[str, ...] = ("output/manufacturing/fill-consistency.json",)
    #: Board-relative copies of the refill-stability record.
    refill_stability: tuple[str, ...] = ()
    #: Board-relative copies of the ``kct check`` report on the published PCB.
    check_reports: tuple[str, ...] = ("output/manufacturing/check-report.json",)
    #: Board-relative copies of the schematic-parity native DRC report on the
    #: saved fills (board 04): must be finding-free; rewritten only when its
    #: findings change, so a rerun keeps the committed ``date``.
    parity_drc_reports: tuple[str, ...] = ()
    #: When set, the re-plot swaps just these ``gerbers/gerbers.zip`` members for
    #: a fresh plot, instead of a whole ``kct export`` (board 04, Issue #7357).
    gerber_members: tuple[re.Pattern[str], ...] = ()
    #: Board recipe hook (module:function) to finalize artwork before refill and DRC.
    finalize_artwork: str | None = None
    #: Board-relative recipe whose ``run_drc(routed_pcb)`` writes
    #: ``output/drc_report.json`` against the final bundle (board 04).
    recipe_drc: str | None = None
    #: Board-relative recipe whose ``generate_manufacturing(routed_pcb,
    #: output_dir)`` regenerates the whole ``output/manufacturing`` bundle
    #: from the published routed PCB (board 04, Issue #9536: the report,
    #: README and manifest must follow the copper, under the board's
    #: reviewed paid process).
    recipe_manufacturing: str | None = None
    #: Board-specific evidence stage (a key of ``EVIDENCE_STAGES``).
    evidence: str | None = None
    #: Board-relative evidence PCB that must equal the published routed PCB.
    evidence_pcb: str | None = None


BUNDLES: dict[str, BundleConfig] = {
    # Board 04: ``check_manufacturing.py`` replaces ``kct check`` (its paid
    # 0.15 mm drill process), the committed DRC is the schematic-parity run, and
    # only the copper and F.Silkscreen Gerbers follow the saved fills (#7357).
    "04-stm32-devboard": BundleConfig(
        manufacturer="jlcpcb-tier1",
        export_members=(),
        # Issue #9536: the assembly drawings are plotted from the published
        # routed PCB like every other bundle member -- skipping them re-pinned
        # the pre-#9455 copper drawings against the new manifest hashes.
        assembly_pdfs=(
            ("assembly-front.pdf", ("F.Cu", "F.SilkS", "Edge.Cuts")),
            ("assembly-back.pdf", ("B.Cu", "B.SilkS", "Edge.Cuts")),
        ),
        schematic_pdfs=(),
        renders=None,
        parity_drc_reports=(
            "output/readiness/native-drc.json",
            "output/manufacturing/native-drc.json",
        ),
        fill_consistency=(
            "output/readiness/fill-consistency.json",
            "output/manufacturing/fill-consistency.json",
        ),
        check_reports=(
            "output/readiness/kct-check.json",
            "output/manufacturing/check-report.json",
        ),
        gerber_members=(COPPER_MEMBER, SILK_MEMBER),
        finalize_artwork="generate_design.py:finalize_silkscreen",
        recipe_drc="generate_design.py",
        recipe_manufacturing="generate_design.py",
    ),
    # Board 06 (Issue #8404): the routed board is the one
    # ``assembled-demo/build.py`` emits; the bundle follows it via a plain
    # ``kct export`` at the board's tier.
    "06-diffpair-test": BundleConfig(
        manufacturer="jlcpcb-tier1",
        fill_consistency=(
            "output/readiness/fill-consistency.json",
            "output/manufacturing/fill-consistency.json",
        ),
        check_reports=(
            "output/readiness/kct-check.json",
            "output/manufacturing/check-report.json",
        ),
    ),
    "07-matchgroup-test": BundleConfig(
        manufacturer="jlcpcb",
        export_members=(
            "gerbers/gerbers.zip",
            "bom_jlcpcb.csv",
            "cpl_jlcpcb.csv",
            "images/*.png",
            "report.md",
            "report.pdf",
        ),
        fill_consistency=(
            "output/readiness/fill-consistency.json",
            "output/manufacturing/fill-consistency.json",
        ),
        refill_stability=(
            "output/readiness/refill-stability.json",
            "real_design/reviewed-routing/evidence/refill-stability.json",
        ),
        check_reports=(
            "output/readiness/kct-check.json",
            "output/manufacturing/check-report.json",
        ),
        evidence="board07",
        evidence_pcb="output/readiness/checked.kicad_pcb",
    ),
}


# --------------------------------------------------------------------------- helpers
def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return rr.run(cmd, **kwargs)


def _tokens(text: str) -> str:
    """Whitespace-insensitive form of an s-expression (formatting-only edits vanish)."""
    return re.sub(r"\s+", " ", re.sub(r"\s*([()])\s*", r"\1", text)).strip()


def zone_blocks(text: str) -> list[str]:
    return [text[start:end] for start, end in netclasses._top_level_zones(text)]


def without_zones(text: str) -> str:
    """``text`` with every top-level ``(zone ...)`` block cut out."""
    pieces: list[str] = []
    cursor = 0
    for start, end in netclasses._top_level_zones(text):
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def archive_is_current(
    archive: Path,
    search: list[Path],
    *,
    derived: dict[str, bytes] | None = None,
    replaces: frozenset[str] = frozenset(),
    pinned: dict[str, bytes] | None = None,
) -> bool:
    """Compare the delivered project or its older, source-named layout.

    *pinned* members -- the selected net-class map a legacy archive keeps
    beside its checked-basename PCB (Issue #10637) -- must be present with
    exactly those bytes; they are not looked up under *search*.
    """
    pinned = pinned or {}
    with zipfile.ZipFile(archive) as existing:
        names = existing.namelist()
        if any(name not in names or existing.read(name) != data for name, data in pinned.items()):
            return False
        if replaces.isdisjoint(names) and derived:
            if any(
                name not in names or existing.read(name) != data for name, data in derived.items()
            ):
                return False
        else:
            derived = {}
        for name in names:
            if name in derived or name in pinned or name.endswith("/"):
                continue
            for base in search:
                candidate = base / name
                if candidate.is_file():
                    if candidate.read_bytes() != existing.read(name):
                        return False
                    break
            else:
                return False
    return True


def _write_targets(staged: Path, targets: tuple[str, ...], data: bytes) -> None:
    for target in targets:
        path = staged / target
        if not path.is_file():
            raise GateFailure(f"{target}: not part of the committed board; refusing to add it")
        path.write_bytes(data)


def _json_bytes(data: object) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


#: Board-relative public release summaries (Issue #9955).  Both carry the same
#: derived document: the staging builder writes them as identical routing-stage
#: copies next to the artifacts it validated, and the publisher rewrites them
#: to describe the gated release shipped in ``output/``.
BOARD07_SUMMARY_TARGETS: tuple[str, ...] = ("output/build.json", "output/status.json")


def _published_board07_summary(stage_summary: dict, cfg: rr.BoardConfig) -> bytes:
    """Derive board 07's public summaries from the staging build's own (Issue #9955).

    ``real_design/build.py`` records the routing stage: its ``pcb`` /
    ``evidence`` references are relative to the staging build directory and its
    completion flags describe that design stage, where no manufacturing
    package exists yet.  The published ``output/`` copies keep the recorded
    routing facts and describe the gated release instead: references resolve
    from ``output/`` (the published routed PCB and the shipped design
    evidence), the readiness receipt and the exported package are named, and
    the completion flags describe the verified package.
    """
    summary = dict(stage_summary)
    summary.update(
        status="released",
        pcb=Path(cfg.routed_pcb).name,
        evidence="readiness/validation.json",
        readiness="readiness.json",
        manufacturing_package="manufacturing.zip",
        manufacturing_complete=True,
        manufacturing_export_required=False,
    )
    return _json_bytes(summary)


# --------------------------------------------------------------------------- stage 1
#: ``validation/`` file -> board-relative copies (board 07).
BOARD07_EVIDENCE: dict[str, tuple[str, ...]] = {
    "check.json": (
        "output/readiness/check.json",
        "real_design/reviewed-routing/evidence/check.json",
    ),
    "check.log": ("output/readiness/check.log",),
    "checked.kicad_dru": ("output/readiness/checked.kicad_dru",),
    "checked.kicad_pcb": ("output/readiness/checked.kicad_pcb",),
    "checked.kicad_pro": ("output/readiness/checked.kicad_pro",),
    "checked.kicad_sch": ("output/readiness/checked.kicad_sch",),
    "clock-spacing.json": (
        "output/readiness/clock-spacing.json",
        "real_design/reviewed-routing/evidence/clock-spacing.json",
    ),
    "clock-spacing.log": ("output/readiness/clock-spacing.log",),
    "erc.json": (
        "output/readiness/erc.json",
        "real_design/reviewed-routing/evidence/erc.json",
    ),
    "lvs.json": (
        "output/lvs.json",
        "output/readiness/lvs.json",
        "output/manufacturing/lvs.json",
        "real_design/reviewed-routing/evidence/lvs.json",
    ),
    "native-drc-resolved.json": (
        "output/readiness/native-drc-resolved.json",
        "real_design/reviewed-routing/evidence/native-drc-resolved.json",
    ),
    "native-drc.json": ("real_design/reviewed-routing/evidence/native-drc.json",),
    "validation.json": (
        "output/readiness/validation.json",
        "real_design/reviewed-routing/evidence/validation.json",
    ),
}


def _board07_build(target: Path) -> Path:
    """``build.py`` output in ``target``: replayed if already built, else built fresh."""
    script = REPO_ROOT / "boards/07-matchgroup-test/real_design/build.py"
    if (target / "build.json").is_file():
        log(f"  board 07 build: replaying {target}")
        return target
    log(f"  board 07 build: running real_design/build.py -> {target}")
    proc = run([sys.executable, str(script), str(target)], cwd=REPO_ROOT)
    if proc.returncode != 0:
        raise GateFailure(f"board 07 build.py failed: {proc.stdout[-2000:]}{proc.stderr[-2000:]}")
    return target


def stage_board07_evidence(
    staged: Path, cfg: rr.BoardConfig, build_dir: Path | None = None
) -> list[str]:
    """Publish the routed PCB ``build.py`` validated, plus its evidence.

    ``build_dir`` keeps (or replays) the build; by default it is a temporary
    directory removed afterwards.  Returns the board-relative files rewritten
    (empty when the committed evidence already describes the build's board).
    The public release summaries are always rewritten in ``staged`` from the
    build's summary (Issue #9955), whether or not the evidence itself is
    current; the staging build's own files are only ever read.
    """
    if build_dir is None:
        with tempfile.TemporaryDirectory(prefix="refresh-bundles-07-build-") as tmp:
            return _publish_board07_evidence(staged, cfg, _board07_build(Path(tmp) / "build"))
    return _publish_board07_evidence(staged, cfg, _board07_build(build_dir))


def _publish_board07_evidence(staged: Path, cfg: rr.BoardConfig, build: Path) -> list[str]:
    validation = build / "validation"
    report = json.loads((validation / "validation.json").read_text())
    if report.get("status") != "pass":
        raise GateFailure(f"board 07 validation is {report.get('status')}: {report.get('errors')}")
    routed = staged / cfg.routed_pcb
    candidate = build / "sdram_demo_routed.kicad_pcb"
    if candidate.read_bytes() != (validation / "checked.kicad_pcb").read_bytes():
        raise GateFailure("build.py published a PCB other than the one it validated")
    for suffix in (".kicad_pro", ".kicad_dru"):
        # The routed sidecars are #6390's; the evidence must be filled and
        # checked under exactly them, never the other way round.
        if candidate.with_suffix(suffix).read_bytes() != routed.with_suffix(suffix).read_bytes():
            raise GateFailure(
                f"build.py checked the board under a different {suffix} than the published "
                "routed sidecar; its evidence would describe other rules"
            )
    # Issue #9955: the public summaries must describe the gated release, not
    # the staging build whose ``build.json`` still carries staging-relative
    # references and the design stage's pending-export flags.  Only ``staged``
    # is written here: a refused publication (mismatched rules here, or any
    # later gate in ``rr.repin_staged``) never copies to the board, and a
    # rerun over an already-repaired bundle proposes zero changes.
    summary = _published_board07_summary(json.loads((build / "build.json").read_text()), cfg)
    _write_targets(staged, BOARD07_SUMMARY_TARGETS, summary)
    current = routed.read_bytes() == candidate.read_bytes() and all(
        (staged / targets[0]).read_bytes() == (validation / name).read_bytes()
        for name, targets in BOARD07_EVIDENCE.items()
        if name.startswith("checked.")
    )
    if current:
        log("  board 07 evidence: committed evidence already describes this build; kept")
        return []
    if without_zones(routed.read_text()) != without_zones(candidate.read_text()):
        log("  board 07 evidence: routed copper changed outside zone fills (reviewed routing)")
    written = [cfg.routed_pcb]
    shutil.copyfile(candidate, routed)
    for name, targets in BOARD07_EVIDENCE.items():
        data = (validation / name).read_bytes()
        if name == "check.log":
            # Keep the checked sidecar search paths relative to the build.
            data = data.replace(os.fsencode(build.resolve()) + b"/", b"")
        _write_targets(staged, targets, data)
        written.extend(targets)
    written.extend(BOARD07_SUMMARY_TARGETS)
    log(f"  board 07 evidence: published routed PCB and {len(written) - 1} evidence copies")
    return written


EVIDENCE_STAGES: dict[str, Callable[..., list[str]]] = {"board07": stage_board07_evidence}


# --------------------------------------------------------------------------- stage 2
def _fill_context(pcb: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        source = pcb.with_suffix(suffix)
        if source.is_file():
            shutil.copyfile(source, (destination / pcb.name).with_suffix(suffix))
    return destination / pcb.name


def fill_areas(pcb: Path) -> dict[str, float]:
    """Per-copper-layer saved fill area (mm^2), as ``kct readiness`` measures it."""
    from kicad_tools.cli.readiness_cmd import _fill_areas

    return _fill_areas(pcb)


def stage_refill(staged: Path, cfg: rr.BoardConfig) -> dict:
    """Refill the routed PCB's zones under its own sidecars; zone blocks only.

    Returns the refill record (areas, hashes, whether anything changed).
    """
    routed = staged / cfg.routed_pcb
    original = routed.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-refill-") as tmp:
        work = _fill_context(routed, Path(tmp))
        netclasses.refill_zones_in_place(work)
        refilled = work.read_text(encoding="utf-8")
        saved_areas = fill_areas(routed)
        refilled_areas = fill_areas(work)
    if without_zones(refilled) != without_zones(original):
        raise GateFailure("zone refill changed the PCB outside its (zone ...) blocks")
    old_zones, new_zones = zone_blocks(original), zone_blocks(refilled)
    if len(old_zones) != len(new_zones):
        raise GateFailure("zone refill changed the number of zones")
    identical = all(_tokens(a) == _tokens(b) for a, b in zip(old_zones, new_zones, strict=True))
    before = sha256(routed)
    if not identical:
        routed.write_text(refilled, encoding="utf-8")
    log(
        f"  refill: {len(old_zones)} zone(s); saved fills "
        f"{'already current (PCB untouched)' if identical else 'replaced'}"
    )
    layers = sorted(set(saved_areas) | set(refilled_areas))
    return {
        "changed": not identical,
        "before_sha256": before,
        "after_sha256": sha256(routed),
        "fill_identical": identical,
        "deltas_mm2": {
            layer: round(abs(saved_areas.get(layer, 0.0) - refilled_areas.get(layer, 0.0)), 6)
            for layer in layers
        },
    }


# --------------------------------------------------------------------------- stage 3
def _is_zone_clearance(violation: dict) -> bool:
    return violation.get("type") == "clearance" and any(
        item.get("description", "").startswith("Zone ") for item in violation.get("items", [])
    )


def saved_fill_findings(report: dict) -> dict[str, int]:
    """Error, zone-clearance and unconnected counts of a no-refill DRC report."""
    violations = report.get("violations", [])
    return {
        "errors": sum(1 for v in violations if v.get("severity") == "error"),
        "zone_clearance": sum(1 for v in violations if _is_zone_clearance(v)),
        "unconnected": len(report.get("unconnected_items", [])),
    }


def gate_saved_fill_drc(staged: Path, cfg: rr.BoardConfig) -> bytes:
    """Native DRC on the *saved* fills (no ``--refill-zones``): must be clean."""
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-drc-") as tmp:
        work = _fill_context(staged / cfg.routed_pcb, Path(tmp) / "board")
        report = Path(tmp) / "native-drc.json"
        proc = run(
            [
                "kicad-cli",
                "pcb",
                "drc",
                str(work),
                "--severity-all",
                "--format",
                "json",
                "--output",
                str(report),
            ]
        )
        if not report.is_file():
            raise GateFailure(f"kicad-cli DRC (saved fills) did not run: {proc.stderr}")
        data = report.read_bytes()
    counts = saved_fill_findings(json.loads(data))
    log(
        f"  gate kicad-cli DRC (saved fills, no refill): {counts['errors']} error(s), "
        f"{counts['zone_clearance']} zone clearance, {counts['unconnected']} unconnected"
    )
    if any(counts.values()):
        raise GateFailure(
            f"saved zone fills fail native DRC without --refill-zones ({counts}); refusing to "
            "publish a bundle plotted from stale fills"
        )
    return data


def parity_drc_findings(report: dict) -> dict[str, int]:
    """Violation, unconnected and schematic-parity counts of a parity DRC report."""
    return {
        "violations": len(report.get("violations", [])),
        "unconnected": len(report.get("unconnected_items", [])),
        "parity": len(report.get("schematic_parity", [])),
    }


def gate_parity_drc(staged: Path, cfg: rr.BoardConfig, bundle: BundleConfig) -> None:
    """Schematic-parity native DRC on the saved fills: must report nothing (board 04).

    Writes ``bundle.parity_drc_reports`` only when the findings differ from the
    committed copy (kicad-cli stamps a wall-clock ``date``).
    """
    from kicad_tools.cli.kicad_toolchain import finalize_native_drc_report

    with tempfile.TemporaryDirectory(prefix="refresh-bundles-parity-") as tmp:
        work = _fill_context(staged / cfg.routed_pcb, Path(tmp) / "board")
        report = Path(tmp) / "native-drc.json"
        run(
            [
                "kicad-cli",
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-error",
                "--severity-warning",
                "--schematic-parity",
                "--units",
                "mm",
                "--output",
                str(report),
                str(work),
            ]
        )
        if not report.is_file():
            raise GateFailure("kicad-cli DRC (schematic parity) did not run")
        data = finalize_native_drc_report(report)  # Issue #6962: refuses lib findings
        raw = report.read_bytes()
    counts = parity_drc_findings(data)
    log(
        f"  gate kicad-cli DRC (schematic parity): {counts['violations']} violation(s), "
        f"{counts['unconnected']} unconnected, {counts['parity']} parity"
    )
    if any(counts.values()):
        raise GateFailure(f"native DRC (schematic parity) is not clean ({counts})")
    for target in bundle.parity_drc_reports:
        path = staged / target
        if not path.is_file():
            raise GateFailure(f"{target}: not part of the committed board; refusing to add it")
        committed = json.loads(path.read_text())
        if {k: v for k, v in committed.items() if k != "date"} != {
            k: v for k, v in data.items() if k != "date"
        }:
            path.write_bytes(raw)


# --------------------------------------------------------------------------- stage 4
def _project_archive(cfg: rr.BoardConfig) -> tuple[str, tuple[str, ...]]:
    for archive, search in cfg.archives:
        if archive.endswith("kicad_project.zip"):
            return archive, search
    raise GateFailure("board configuration has no kicad_project.zip archive")


def _legacy_project_map(staged: Path, cfg: rr.BoardConfig) -> dict[str, bytes]:
    """The selected map under the member the archived checked-basename PCB selects.

    Empty for a delivered-layout archive (its map rides in ``derived``) and for a
    board that selects no map (Issue #10637).
    """
    archive, _ = _project_archive(cfg)
    with zipfile.ZipFile(staged / archive) as existing:
        return rr.legacy_net_class_map_member(
            existing.namelist(), staged / cfg.routed_pcb, staged / cfg.schematic, add=True
        )


def bundle_is_stale(staged: Path, cfg: rr.BoardConfig) -> bool:
    archive, search = _project_archive(cfg)
    derived, replaces = rr.delivered_project_members(
        staged / cfg.routed_pcb, staged / cfg.schematic
    )
    return not archive_is_current(
        staged / archive,
        [staged / s for s in search],
        derived=derived,
        replaces=replaces,
        pinned=_legacy_project_map(staged, cfg),
    )


def stage_project_map(staged: Path, cfg: rr.BoardConfig) -> list[str]:
    """Deliver the selected net-class map inside a legacy ``kicad_project.zip`` (Issue #10637).

    ``kct readiness --verify`` resolves the map an extracted board loads, and a
    legacy archive keeps the checked PCB basename, so the map is filed under the
    name that PCB probes.  Every existing member keeps its bytes.  The plain
    receipt refresh deliberately never adds members to a legacy archive, so the
    producer that owns the bundle adds it here.
    """
    archive, search = _project_archive(cfg)
    pinned = _legacy_project_map(staged, cfg)
    if not pinned:
        return []
    changed = rr.rebuild_archive(
        staged / archive,
        [staged / s for s in search],
        additional_members=tuple(pinned),
        pinned=pinned,
    )
    if changed:
        log(f"  {archive}: net-class map delivered {changed}")
    return changed


def check_recipe_drc_preflight(staged: Path, cfg: rr.BoardConfig, *, refresh: bool = False) -> bool:
    """Check the exporter's DRC result, optionally refreshing that entry in a current bundle."""
    from kicad_tools.export.preflight import PreflightChecker

    routed = staged / cfg.routed_pcb
    report = routed.parent / "drc_report.json"
    manifest = routed.parent / "manufacturing" / "manifest.json"
    if not report.is_file() or not manifest.is_file():
        return False
    data = json.loads(report.read_bytes())
    if (
        data.get("pcb_sha256") != sha256(routed)
        or data.get("manufacturer") != cfg.tier
        or Path(data.get("file", "")).name != routed.name
        or data.get("meta_checks", {}).get("manifest", {}).get("status") != "PASSED"
    ):
        return False
    published = json.loads(manifest.read_bytes())
    entries = published.get("preflight", [])
    matches = [i for i, entry in enumerate(entries) if entry["name"] == "drc"]
    if len(matches) != 1:
        return False
    expected = PreflightChecker(routed, manufacturer=cfg.tier)._check_drc().to_dict()
    index = matches[0]
    if entries[index] != expected:
        if not refresh:
            return False
        # The PCB, report and project archive still bind. Keep the artwork and
        # other preflight observations when only this recorded result is stale.
        entries[index] = expected
        manifest.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
        log("  manufacturing manifest: refreshed DRC preflight from the bound report")
    return True


def check_fresh_cpl(committed: bytes, fresh: bytes, routed_pcb: Path) -> None:
    """Refuse a fresh CPL that moves a part; accept a pure coordinate-frame refresh (#7199).

    Byte-identical is always fine.  A change is accepted only when every column
    but ``Mid X``/``Mid Y`` is unchanged *and* the fresh coordinates equal the
    routed PCB's own Gerber/drill frame, i.e. the placement did not move.
    """
    if fresh == committed:
        return
    if not cpl_frame.only_coordinates_differ(committed, fresh):
        raise GateFailure("fresh CPL differs from the committed one: placement changed")
    problems = cpl_frame.coordinate_mismatches(cpl_frame.read_cpl(fresh), routed_pcb)
    if problems:
        raise GateFailure(
            "fresh CPL differs from the committed one and does not match the routed PCB's "
            "drill frame: placement changed: " + "; ".join(problems[:3])
        )
    log("  CPL: coordinate-frame refresh only (columns other than Mid X/Mid Y unchanged)")


def _normalize_pdf_timestamps(pdf: Path) -> None:
    """Anchor a plotted PDF's ``/CreationDate`` to ``SOURCE_DATE_EPOCH`` (#9536).

    kicad-cli stamps the wall clock into the document info dictionary at
    second resolution; left alone, two publishes of the same PCB write
    different bytes.  With ``SOURCE_DATE_EPOCH`` anchored the stamp becomes a
    pure function of the environment; without it the PDF is untouched.
    """

    epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if not epoch.isdigit():
        return
    stamp = datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y:%m:%d:%H:%M:%S")
    data = pdf.read_bytes()
    anchored = re.sub(
        rb"/(CreationDate|ModDate) \(D:\d{4}:\d{2}:\d{2}:\d{2}:\d{2}:\d{2}[^)]*\)",
        rf"/\1 (D:{stamp})".encode(),
        data,
    )
    if anchored != data:
        pdf.write_bytes(anchored)


def stage_replot(
    staged: Path, cfg: rr.BoardConfig, bundle: BundleConfig, *, saved_drc: bytes | None = None
) -> list[str]:
    """Re-plot every artifact drawn from the routed PCB's copper or the schematic."""
    from kicad_tools.cli.readiness_cmd import _export_assembly_pdf, _export_schematic_pdf
    from kicad_tools.cli.render_cmd import _render_pcb
    from kicad_tools.cli.runner import find_kicad_cli

    routed = staged / cfg.routed_pcb
    mfg = staged / "output" / "manufacturing"
    written: list[str] = []
    if bundle.gerber_members:
        archive = mfg / "gerbers" / "gerbers.zip"
        for pattern in bundle.gerber_members:
            written.extend(
                f"output/manufacturing/gerbers/gerbers.zip:{name}"
                for name in copper_gerbers.refresh_copper_gerbers(routed, archive, pattern)
            )
        log(f"  re-plot: {len(written)} Gerber member(s) from the published routed PCB")
        # Issue #9536: the assembly drawings are drawn from the routed PCB's
        # copper like the Gerbers; a bundle that skips them re-pins the
        # previous copper's drawings under the new manifest hashes.
        if bundle.assembly_pdfs:
            from kicad_tools.cli.readiness_cmd import _export_assembly_pdf

            for name, layers in bundle.assembly_pdfs:
                if not (mfg / name).is_file():
                    raise GateFailure(
                        f"{name}: not part of the committed bundle; refusing to add it"
                    )
                result = _export_assembly_pdf(routed, layers, mfg / name)
                if not result.ok:
                    raise GateFailure(f"{name}: {result.detail}")
                _normalize_pdf_timestamps(mfg / name)
                written.append(f"output/manufacturing/{name}")
            log(f"  re-plot: {len(bundle.assembly_pdfs)} assembly drawing(s) re-plotted")
        return written
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-plot-") as tmp:
        project = Path(tmp) / "output"
        shutil.copytree(
            routed.parent,
            project,
            ignore=shutil.ignore_patterns("manufacturing", "manufacturing.zip", "renders"),
        )
        # ``kct export`` resolves LCSC numbers from the board-root spec overlay
        # (board 06, Issue #8404); without it every BOM row is unmatched.
        if (routed.parent.parent / "project.kct").is_file():
            shutil.copy2(routed.parent.parent / "project.kct", Path(tmp) / "project.kct")
        export = Path(tmp) / "export"
        plot_board = project / routed.name
        command = rr.kct(
            "export", str(plot_board), "--mfr", bundle.manufacturer, "--output", str(export)
        )
        if bundle.evidence_pcb is not None:
            # The artwork is already reviewed. Export must plot those bytes
            # and preflight the reports for this board, including on the first
            # run when the previous manufacturing receipt is stale.
            from kicad_tools.drc.report_binding import stamp_report_hash

            if saved_drc is None:
                raise GateFailure("export of the reviewed board requires its saved-fill DRC")
            drc, erc = project / "drc_report.json", project / "erc_report.json"
            drc.write_bytes(saved_drc)
            erc.write_bytes(rr.native_erc(staged, cfg.erc[0], None, None))
            if not stamp_report_hash(drc, plot_board, "pcb_sha256") or not stamp_report_hash(
                erc, project / Path(cfg.schematic).name, "schematic_sha256"
            ):
                raise GateFailure("could not bind the export reports to the reviewed design")
            command.extend(("--no-silk-fix", "--strict-preflight", "--no-auto-lcsc"))
        proc = run(command)
        if proc.returncode != 0:
            raise GateFailure(f"kct export failed: {proc.stdout[-2000:]}{proc.stderr[-2000:]}")
        if plot_board.read_bytes() != routed.read_bytes():
            raise GateFailure("kct export modified the routed PCB it plotted")
        if bundle.evidence_pcb is not None:
            for suffix in (".kicad_pro", ".kicad_dru"):
                if (
                    plot_board.with_suffix(suffix).read_bytes()
                    != routed.with_suffix(suffix).read_bytes()
                ):
                    raise GateFailure(f"kct export changed the reviewed {suffix} rules")
            # Keep the assembly sign-off mode and take the measurements and
            # production settings from the new export. repin_staged hashes the
            # full gallery payload after adding the board-specific evidence.
            manifest_path = mfg / "manifest.json"
            fresh_manifest = json.loads((export / "manifest.json").read_bytes())
            fresh_manifest["mode"] = json.loads(manifest_path.read_bytes())["mode"]
            manifest_path.write_text(json.dumps(fresh_manifest, indent=2) + "\n")
        if (export / "cpl_jlcpcb.csv").is_file() and (mfg / "cpl_jlcpcb.csv").is_file():
            check_fresh_cpl(
                (mfg / "cpl_jlcpcb.csv").read_bytes(),
                (export / "cpl_jlcpcb.csv").read_bytes(),
                routed,
            )
        for pattern in bundle.export_members:
            committed = sorted(p.relative_to(mfg).as_posix() for p in mfg.glob(pattern))
            fresh = sorted(p.relative_to(export).as_posix() for p in export.glob(pattern))
            if committed != fresh:
                raise GateFailure(f"{pattern}: export produced {fresh}, bundle has {committed}")
            for name in fresh:
                source = export / name
                if name.endswith(".zip"):
                    with zipfile.ZipFile(source) as z:
                        members = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
                    rr.write_deterministic_zip(mfg / name, members)
                else:
                    shutil.copyfile(source, mfg / name)
                written.append(f"output/manufacturing/{name}")
        for name, layers in bundle.assembly_pdfs:
            result = _export_assembly_pdf(routed, layers, mfg / name)
            if not result.ok:
                raise GateFailure(f"{name}: {result.detail}")
            written.append(f"output/manufacturing/{name}")
        for name in bundle.schematic_pdfs:
            if not (mfg / name).is_file():
                raise GateFailure(f"{name}: not part of the committed bundle; refusing to add it")
            result = _export_schematic_pdf(staged / cfg.schematic, mfg / name)
            if not result.ok:
                raise GateFailure(f"{name}: {result.detail}")
            written.append(f"output/manufacturing/{name}")
        if bundle.renders is not None:
            renders = Path(tmp) / "renders"
            status = _render_pcb(
                routed,
                renders,
                name=staged.name,
                do_3d=True,
                kicad_cli=find_kicad_cli(),
                quiet=True,
            )
            if status["status"] != "ok":
                raise GateFailure(f"kct render failed: {status['errors']}")
            for path in sorted((staged / bundle.renders).iterdir()):
                fresh_render = renders / path.name
                if not fresh_render.is_file():
                    raise GateFailure(f"kct render produced no {path.name}")
                shutil.copyfile(fresh_render, path)
                written.append(f"{bundle.renders}/{path.name}")
    log(f"  re-plot: {len(written)} artifact(s) from the published routed PCB")
    return written


def run_check_report(staged: Path, cfg: rr.BoardConfig) -> bytes:
    """Run the bundle's ``kct check`` (board-relative path, so reproducible); return the report.

    A board with a ``check_script`` (board 04) runs that instead, as its gate does.
    """
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-check-") as tmp:
        report = Path(tmp) / "check.json"
        if cfg.check_script is not None:
            command = [
                sys.executable,
                str(staged / cfg.check_script),
                str(staged / cfg.routed_pcb),
                str(report),
            ]
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        else:
            command = rr.kct(
                "check",
                cfg.routed_pcb,
                "--mfr",
                cfg.tier,
                "--format",
                "json",
                "--output",
                str(report),
            )
            env = None
        run(command, cwd=staged, env=env)
        if not report.is_file():
            raise GateFailure("the check did not produce a report")
        return report.read_bytes()


def stage_check_report(staged: Path, cfg: rr.BoardConfig, bundle: BundleConfig) -> None:
    """Write the bundle's ``kct check`` report into every committed target."""
    _write_targets(staged, bundle.check_reports, run_check_report(staged, cfg))


def rule_counts(report: bytes) -> dict[str, int] | None:
    """Per-rule violation counts of a check report; ``None`` when it is not a report."""
    try:
        data = json.loads(report)
        violations = data.get("violations", [])
        counts: dict[str, int] = {}
        for violation in violations:
            counts[violation["rule_id"]] = counts.get(violation["rule_id"], 0) + 1
    except (ValueError, AttributeError, KeyError, TypeError):
        return None
    return counts


def stale_check_reports(
    board: Path, staged: Path, cfg: rr.BoardConfig, bundle: BundleConfig
) -> list[str]:
    """Committed check reports whose per-rule counts differ from the current checker (Issue #10692).

    The refresh only re-runs the checker when the archive is stale, so a bundle
    that is self-consistent but was checked under older rules would otherwise
    compare clean.  Only per-rule counts are compared: the report's bytes also
    carry archive-level noise that is not checker drift.
    """
    committed_targets = [t for t in bundle.check_reports if (board / t).is_file()]
    if not committed_targets:
        return []
    fresh = rule_counts(run_check_report(staged, cfg))
    stale = []
    for target in committed_targets:
        committed = board / target
        if fresh is None or rule_counts(committed.read_bytes()) != fresh:
            stale.append(target)
    return stale


def stage_recipe_drc(
    staged: Path, cfg: rr.BoardConfig, bundle: BundleConfig, *, before_export: bool = False
) -> None:
    """Capture the recipe's ``run_drc`` report as ``output/drc_report.json`` (board 04).

    Export needs the new copper's DRC. Its manifest meta-check is authoritative
    only on the second run, once the new bundle has been built and re-hashed.
    """
    assert bundle.recipe_drc is not None
    routed = staged / cfg.routed_pcb
    report = routed.parent / "drc_report.json"
    report.unlink(missing_ok=True)
    snippet = (
        "import runpy, sys; from pathlib import Path; "
        "recipe = runpy.run_path(sys.argv[1], run_name='refresh_bundles_recipe'); "
        "print('run_drc:', recipe['run_drc'](Path(sys.argv[2])))"
    )
    proc = run(
        [
            sys.executable,
            "-c",
            snippet,
            str(staged / bundle.recipe_drc),
            str(routed),
        ],
        cwd=staged,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(staged)},
    )
    if proc.returncode != 0 or not report.is_file():
        raise GateFailure(f"{bundle.recipe_drc} run_drc wrote no report: {proc.stderr[-2000:]}")
    data = json.loads(report.read_bytes())
    if data.get("pcb_sha256") != sha256(routed):
        raise GateFailure("output/drc_report.json does not bind the final routed PCB")
    status = data.get("meta_checks", {}).get("manifest", {}).get("status")
    if not before_export and status != "PASSED":
        raise GateFailure(
            f"output/drc_report.json: manifest meta-check is {status!r}, not 'PASSED'"
        )
    log(f"  recipe {proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else 'run_drc'}")


def stage_recipe_manufacturing(staged: Path, cfg: rr.BoardConfig, bundle: BundleConfig) -> None:
    """Regenerate ``output/manufacturing`` through the recipe (board 04, #9536).

    The report, README and manifest are drawn from the routed PCB under the
    board's reviewed paid process (the recipe passes its own
    ``process_rules()`` to the exporter), so a copper change re-generates them
    instead of re-hashing stale artifacts.  Runs before the re-plot so the
    Gerber-member and assembly-drawing refresh lands on the final bundle.
    """
    assert bundle.recipe_manufacturing is not None
    snippet = (
        "import runpy, sys; from pathlib import Path; "
        "recipe = runpy.run_path(sys.argv[1], run_name='refresh_bundles_recipe'); "
        "ok = recipe['generate_manufacturing'](Path(sys.argv[2]), Path(sys.argv[3])); "
        "sys.exit(0 if ok else 1)"
    )
    proc = run(
        [
            sys.executable,
            "-c",
            snippet,
            str(staged / bundle.recipe_manufacturing),
            str(staged / cfg.routed_pcb),
            str(staged / "output"),
        ],
        cwd=staged,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(staged)},
    )
    if proc.returncode != 0:
        raise GateFailure(
            f"{bundle.recipe_manufacturing} generate_manufacturing failed: "
            f"{proc.stdout[-2000:]}{proc.stderr[-2000:]}"
        )
    log("  recipe generate_manufacturing: bundle regenerated under the reviewed process")


# --------------------------------------------------------------------------- driver
def refresh_bundle(
    board: Path,
    checked_at: str,
    *,
    reuse_erc: Path | None = None,
    save_erc: Path | None = None,
    build_dir: Path | None = None,
    force_replot: bool = False,
    dry_run: bool = False,
    known_issue: list[str] | None = None,
    resolve_known_issue: list[str] | None = None,
    on_verdict: Callable[[str], None] | None = None,
    compare_checks: bool = False,
) -> list[str]:
    cfg = rr.BOARDS[board.name]
    bundle = BUNDLES[board.name]
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-") as tmp:
        staged = Path(tmp) / board.name
        shutil.copytree(board, staged, ignore=shutil.ignore_patterns("__pycache__"))

        if bundle.evidence is not None:
            EVIDENCE_STAGES[bundle.evidence](staged, cfg, build_dir)
        if bundle.finalize_artwork:
            finalize = rr.load_refresher(staged, bundle.finalize_artwork)
            if not finalize(staged / cfg.routed_pcb):
                raise GateFailure("Could not finalize the routed PCB artwork")
        refill = stage_refill(staged, cfg)
        if bundle.evidence_pcb is not None and (
            (staged / bundle.evidence_pcb).read_bytes() != (staged / cfg.routed_pcb).read_bytes()
        ):
            raise GateFailure(
                f"{bundle.evidence_pcb} is not the published routed PCB: the board's evidence "
                "was filled under other rules than its sidecars"
            )
        saved_drc = gate_saved_fill_drc(staged, cfg)
        if bundle.parity_drc_reports:
            gate_parity_drc(staged, cfg, bundle)

        stale = (
            force_replot
            or bundle_is_stale(staged, cfg)
            or (
                bundle.recipe_drc is not None
                and not check_recipe_drc_preflight(staged, cfg, refresh=True)
            )
        )
        if stale:
            if bundle.recipe_drc is not None:
                stage_recipe_drc(staged, cfg, bundle, before_export=True)
            if bundle.recipe_manufacturing is not None:
                stage_recipe_manufacturing(staged, cfg, bundle)
            stage_replot(staged, cfg, bundle, saved_drc=saved_drc)
            stage_project_map(staged, cfg)
            _write_targets(staged, bundle.saved_drc_reports, saved_drc)
            _write_targets(staged, bundle.fill_consistency, _json_bytes(refill["deltas_mm2"]))
            routed = staged / cfg.routed_pcb
            stability = {
                "before_sha256": refill["before_sha256"],
                "after_sha256": refill["after_sha256"],
                "byte_identical": refill["before_sha256"] == refill["after_sha256"],
                "fill_identical": refill["fill_identical"],
                "native_violations": len(json.loads(saved_drc)["violations"]),
                "native_unconnected_items": len(json.loads(saved_drc)["unconnected_items"]),
                "native_rule_sha256": {
                    suffix: sha256(routed.with_suffix(suffix))
                    for suffix in (".kicad_pro", ".kicad_dru")
                },
                "post_check_rule_sources_identical_to_refill_proof": True,
            }
            if bundle.refill_stability:
                _write_targets(staged, bundle.refill_stability, _json_bytes(stability))
        else:
            log("  re-plot: bundle already binds the current routed project files; kept")

        # Issue #7353: the ERC, DRC, archive, manifest, gate and re-pin tail is
        # refresh_readiness's, not a copy of it.  The check report records the
        # manifest meta-check, so it is built against the re-hashed bundle and
        # hashed in by the second re-hash.
        def after_manifest() -> None:
            stage_check_report(staged, cfg, bundle)
            if bundle.recipe_drc is not None:
                # run_drc's manifest meta-check needs the check report hashed in.
                rr.load_refresher(staged, cfg.manifest_refresher)(
                    staged / "output" / "manufacturing"
                )
                stage_recipe_drc(staged, cfg, bundle)
                if not check_recipe_drc_preflight(staged, cfg):
                    raise GateFailure(
                        "published DRC preflight disagrees with output/drc_report.json"
                    )

        would_publish = rr.repin_staged(
            board,
            staged,
            cfg,
            checked_at,
            reuse_erc=reuse_erc,
            save_erc=save_erc,
            dry_run=dry_run,
            known_issue=known_issue,
            resolve_known_issue=resolve_known_issue,
            after_manifest=after_manifest if stale else None,
            on_verdict=on_verdict,
        )
        if compare_checks and not stale:
            # Issue #10692: a current archive skips the checker above, so compare
            # the committed per-rule counts to a fresh run here.
            for target in stale_check_reports(board, staged, cfg, bundle):
                if target not in would_publish:
                    would_publish.append(target)
        return would_publish


def committed_checked_at(board: Path) -> str:
    """The ``checked_at`` a board's committed ``readiness.json`` was pinned at (Issue #10692)."""
    report = json.loads((board / "output" / "readiness.json").read_text())
    return report["checked_at"]


def _source_date_epoch(checked_at: str) -> str:
    """``SOURCE_DATE_EPOCH`` for an ISO ``checked_at``; the ``Z`` suffix parses on 3.10 too."""
    return str(int(datetime.fromisoformat(checked_at.replace("Z", "+00:00")).timestamp()))


def drift_exit_status(
    drift: dict[str, list[str]],
    refused: list[str] | None = None,
    blocked: list[str] | None = None,
) -> int:
    """The ``--check`` verdict (Issue #10692).

    1 when any board would publish, was refused by a gate, or does not verify
    ready (a ``blocked`` receipt, Issue #10662 -- the nightly stays red for
    such a board until its defect is fixed); else 0.  The "clean" line is only
    printed when none of those happened, so a red run never reads as clean.
    """
    refused, blocked = refused or [], blocked or []
    if not (drift or refused or blocked):
        log("== --check: every bundle reproduces on the pinned toolchain; nothing to publish")
        return 0
    for name, files in drift.items():
        log(f"== --check DRIFT {name}: a fresh run would publish {len(files)} file(s)")
        for path in files:
            log(f"    {path}")
    if drift:
        log(
            "  --check: the committed bundle disagrees with a fresh run. Refresh it with this "
            "driver (without --check) on the pinned toolchain and commit the result."
        )
    for name in refused:
        log(f"== --check REFUSED {name}: a gate refused the fresh run; drift is unknown")
    for name in blocked:
        log(f"== --check BLOCKED {name}: the board does not verify ready (not drift)")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("boards", nargs="+", type=Path, help="board directories")
    parser.add_argument(
        "--checked-at",
        help="ISO timestamp for readiness.json (with --check: defaults to each board's committed value)",
    )
    parser.add_argument("--reuse-erc", type=Path, help="replay ERC reports saved by --save-erc")
    parser.add_argument("--save-erc", type=Path, help="keep the native ERC reports here")
    parser.add_argument(
        "--build-dir",
        type=Path,
        help="keep the board's build output here; replay it if the directory already has one",
    )
    parser.add_argument(
        "--force-replot", action="store_true", help="re-plot even if the bundle is current"
    )
    parser.add_argument("--dry-run", action="store_true", help="run every gate, publish nothing")
    parser.add_argument(
        "--check",
        action="store_true",
        help="publish nothing; exit 1 when any bundle would publish files (Issue #10692)",
    )
    parser.add_argument(
        "--known-issue",
        action="append",
        default=[],
        metavar="N:SUMMARY[:KIND[:!rule@x/y,...]]",
        help="record a known issue (repeatable; same syntax as `kct readiness`, Issue #7323)",
    )
    parser.add_argument(
        "--resolve-known-issue",
        action="append",
        default=[],
        metavar="N",
        help="drop a recorded known issue by number (repeatable, Issue #7323)",
    )
    args = parser.parse_args(argv)
    if not args.checked_at and not args.check:
        parser.error("--checked-at is required unless --check reads each board's committed value")
    # Issue #9536: anchor every exporter wall-clock stamp (manifest
    # ``generated_at``, README ``Generated at``, report date -- and weasyprint
    # PDF metadata) to the board's ``checked_at`` via the reproducible-builds
    # ``SOURCE_DATE_EPOCH`` convention, so a repeated publish with the same
    # inputs writes byte-identical evidence.  It is set per board, because
    # ``--check`` takes each board's own committed anchor; a value already in
    # the environment is an explicit caller override and is kept.
    ambient_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    status = 0
    drift: dict[str, list[str]] = {}
    refused: list[str] = []
    blocked: list[str] = []
    for board in args.boards:
        board = board.resolve()
        if board.name not in BUNDLES:
            parser.error(f"{board.name}: no bundle configuration (known: {sorted(BUNDLES)})")
        log(f"== {board.name}")
        verdict = rr.Verdict()
        if args.checked_at:
            checked_at, origin = args.checked_at, "--checked-at"
        else:
            origin = f"{board.name}: committed checked_at"
            try:
                checked_at = committed_checked_at(board)
            except (OSError, KeyError, ValueError):
                parser.error(f"{origin} is unreadable in output/readiness.json")
        try:
            anchored = _source_date_epoch(checked_at)
        except ValueError:
            parser.error(f"{origin} is not an ISO timestamp: {checked_at}")
        if ambient_epoch is None:
            os.environ["SOURCE_DATE_EPOCH"] = anchored
        try:
            would_publish = refresh_bundle(
                board,
                checked_at,
                reuse_erc=args.reuse_erc,
                save_erc=args.save_erc,
                build_dir=args.build_dir.resolve() if args.build_dir else None,
                force_replot=args.force_replot,
                dry_run=args.dry_run or args.check,
                compare_checks=args.check,
                known_issue=args.known_issue,
                resolve_known_issue=args.resolve_known_issue,
                on_verdict=verdict.publish,
            )
            for name in would_publish:
                log(f"    {name}")
            if would_publish:
                drift[board.name] = would_publish
        except GateFailure as exc:
            log(f"  REFUSED: {exc} -- nothing published for {board.name}")
            status = 1
            refused.append(board.name)
            continue
        if verdict.status != "ready":
            log(f"  BLOCKED: {board.name} does not verify ready (exit 1)")
            status = 1
            blocked.append(board.name)
    if args.check:
        status = drift_exit_status(drift, refused=refused, blocked=blocked) or status
    return status


if __name__ == "__main__":
    os.environ["PATH"] = os.pathsep.join(
        [os.environ.get("KICAD_CLI_DIR", "/Applications/KiCad/KiCad.app/Contents/MacOS")]
        + os.environ.get("PATH", "").split(os.pathsep)
    )
    from kicad_tools.cli.kicad_toolchain import run_on_evidence_toolchain

    raise SystemExit(run_on_evidence_toolchain(main, [REPO_ROOT]))
