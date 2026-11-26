"""Validate saved manufacturing evidence against the current board files."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .known_issues import KNOWN_ISSUES_FILENAME, defects, validate_known_issues

MANUFACTURING_MANIFEST = "output/manufacturing/manifest.json"


@dataclass(frozen=True)
class ReadinessLayout:
    """Trusted receipt and input-base locations for one readiness record.

    A project-declared output root (Issue #10106) moves the receipt out of
    ``<board>/output/readiness.json`` into the declared root and keys its
    ``inputs`` against the declaring project instead of the board directory.
    The producer passes its resolved layout. Directory-only readers still
    use the board-local location unless they explicitly supply a layout.
    """

    #: The ``readiness.json`` record to read.
    report: Path
    #: Directory the record's ``inputs`` paths resolve against.
    input_base: Path

    def validate_declaration(self, data: dict, board_dir: Path) -> None:
        """Check that the receipt describes the project and output selected by the caller."""
        base = self.input_base.resolve()
        expected_project = Path(os.path.relpath(base, board_dir.resolve())).as_posix()
        recorded_project = data.get("project_root")
        if (
            data.get("output_root") != self.report.parent.resolve().relative_to(base).as_posix()
            or not isinstance(recorded_project, str)
            # Older Windows producers used native separators for this field.
            or recorded_project.replace("\\", "/") != expected_project
            or "external_inputs" in data
        ):
            raise ValueError(
                "Readiness receipt does not match the declared project and output root"
            )

    def checked_schematic_root(self, data: dict) -> Path:
        """Verify the released sheet hierarchy and identify the root used for its BOM."""
        from kicad_tools.report.utils import walk_schematic_hierarchy

        inputs = data.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("The readiness receipt has no schematic input hashes")
        sheet_names = {name for name in inputs if name.endswith(".kicad_sch")}
        recorded = data.get("schematic_root")
        if "schematic_root" in data and (
            not isinstance(recorded, str) or recorded not in sheet_names
        ):
            raise ValueError("The selected schematic root is missing from readiness inputs")
        base = self.input_base.resolve()
        roots: list[Path] = []
        # Older receipts recorded file hashes without recording which sheet was
        # the root. Accept one complete hierarchy that accounts for every sheet.
        for name in [recorded] if recorded is not None else sorted(sheet_names):
            root = (base / name).resolve()
            if not root.is_relative_to(base) or root.relative_to(base).as_posix() != name:
                raise ValueError(f"Invalid schematic path in readiness inputs: {name}")
            walked = walk_schematic_hierarchy(root, lambda sheet: sheet.is_relative_to(base))
            unavailable = walked.missing + walked.outside
            if unavailable:
                raise ValueError(f"Schematic hierarchy is incomplete at {unavailable[0]}")
            reachable = set()
            for sheet in walked.sheets:
                relative = sheet.relative_to(base).as_posix()
                if inputs.get(relative) != hashlib.sha256(sheet.read_bytes()).hexdigest():
                    raise ValueError(
                        f"Readiness receipt does not cover schematic sheet {relative} at its current sha256"
                    )
                reachable.add(relative)
            if recorded is not None or reachable == sheet_names:
                roots.append(root)
        if len(roots) != 1:
            raise ValueError("The readiness receipt does not identify one complete schematic root")
        if data.get("schematic_selection") == "implicit":
            from kicad_tools.sync.discover import resolve_schematic_for_pcb

            source = data.get("source_pcb")
            if not isinstance(source, str) or source not in inputs:
                raise ValueError("Implicit schematic selection requires the recorded source PCB")
            pcb = (base / source).resolve()
            if not pcb.is_relative_to(base):
                raise ValueError("Recorded source PCB leaves the project")
            current = resolve_schematic_for_pcb(pcb)
            if current is None or current.resolve() != roots[0]:
                raise ValueError("Implicit schematic selection changed; re-verify readiness")
        return roots[0]


def validate_source_pcb(data: dict, pcb: Path, base: Path, output: Path) -> None:
    """Match the selected board to the editable PCB identified by the producer."""
    inputs = data["inputs"]
    selected = pcb.relative_to(base).as_posix()
    if "source_pcb" in data:
        source = data["source_pcb"]
    else:
        # Older receipts list the two diagnostic copies alongside the design.
        # Native DRC provenance can distinguish remaining routed and unrouted inputs.
        snapshots = [
            output / evidence / kind
            for evidence in ("readiness", "readiness-verification")
            for kind in ("fill-saved", "fill-refilled")
        ]
        candidates = [
            name
            for name in inputs
            if name.endswith(".kicad_pcb")
            and not any((base / name).is_relative_to(tree) for tree in snapshots)
        ]
        if len(candidates) > 1:
            candidates = _legacy_report_candidates(data, base, output, candidates)
        source = candidates[0] if len(candidates) == 1 else None
    if source != selected or selected not in inputs:
        raise ValueError("Readiness receipt was not produced for this --board")


def _legacy_report_candidates(
    data: dict, base: Path, output: Path, candidates: list[str]
) -> list[str]:
    """Use the receipt's hash-bound native DRC reports to identify the checked board."""
    inputs = data["inputs"]
    sources = data.get("report_sources")
    if not isinstance(sources, dict):
        return []
    matches = set(candidates)
    for name, digest in sources.items():
        path = (base / name).resolve()
        if path.name not in {"native-drc.json", "native-drc-raw.json", "drc_report.json"}:
            continue
        if name not in inputs or not path.is_relative_to(output):
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != inputs[name]:
            return []
        report = json.loads(raw)
        if not isinstance(report, dict):
            return []
        bound = [candidate for candidate in candidates if inputs[candidate] == digest]
        if report.get("source"):
            bound = [candidate for candidate in bound if Path(candidate).name == report["source"]]
        matches.intersection_update(bound)
    return sorted(matches)


def board_directory_for_pcb(pcb: Path) -> Path:
    """Locate a board's directory for either an editable PCB or a board under output/."""
    return pcb.parent.parent if pcb.parent.name == "output" else pcb.parent


def declared_readiness_layout(pcb: Path, board_dir: Path) -> ReadinessLayout | None:
    """Resolve the selected PCB's local output declaration with the producer's path rules."""
    from kicad_tools.config import ConfigError, find_readiness_output_root

    try:
        declaration = find_readiness_output_root(pcb.parent)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    if declaration is None:
        return None
    output_root, declared_by = declaration
    project_dir = declared_by.parent.resolve()
    if output_root == project_dir or not output_root.is_relative_to(project_dir):
        raise ValueError(
            f"[readiness] output_root in {declared_by.name} must resolve to a directory "
            f"inside the project {project_dir}; got {output_root}"
        )
    if output_root == board_dir.resolve():
        raise ValueError(
            f"[readiness] output_root in {declared_by.name} must not be the board directory "
            f"itself ({output_root}); the release must not mix with the editable design"
        )
    return ReadinessLayout(report=output_root / "readiness.json", input_base=project_dir)


def is_manufacturing_manifest(rel_name: str) -> bool:
    """True for the verified bundle's ``manifest.json`` input (Issue #8592).

    The bundle is ``<board>/output/manufacturing/`` or, when the PCB sits
    directly in its board directory, ``<board>/manufacturing/`` (readiness's own
    ``--output`` default).  Hashing and the Ready gate share this one predicate.
    """
    path = Path(rel_name)
    return path.name == "manifest.json" and path.parent.name == "manufacturing"


def _canonical_manifest_bytes(raw: bytes) -> bytes | None:
    """Manifest bytes minus the top-level ``tapeout`` sign-off key (Issue #8580).

    ``kct tapeout-sign-off`` adds ``tapeout`` to ``manifest.json``; that must not
    stale a readiness receipt that attests the evidence, not the sign-off.
    Returns ``None`` when the manifest is not a JSON object.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    data.pop("tapeout", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def readiness_input_hash(rel_name: str, raw: bytes) -> str:
    """Hash a readiness input; the manufacturing manifest uses its canonical form.

    Every other input (and an unparseable manifest) keeps the raw-bytes hash.
    """
    if is_manufacturing_manifest(rel_name):
        canonical = _canonical_manifest_bytes(raw)
        if canonical is not None:
            return hashlib.sha256(canonical).hexdigest()
    return hashlib.sha256(raw).hexdigest()


def _input_hash_matches(name: str, raw: bytes, expected: object) -> bool:
    if readiness_input_hash(name, raw) == expected:
        return True
    # Receipts written before #8580 pinned the raw manifest bytes; they stay
    # valid until the manifest changes (a signed one needs `kct readiness`).
    return is_manufacturing_manifest(name) and hashlib.sha256(raw).hexdigest() == expected


def read_readiness(board_dir: Path, *, layout: ReadinessLayout | None = None) -> dict:
    """Load the v1 sidecar, failing closed on missing or changed evidence.

    A selected PCB discovers its project declaration and must itself be a
    hashed input. Directory callers keep the board-local receipt location
    unless the producer supplies its trusted layout.
    """
    unknown = {"status": "unverified", "blockers": ["Readiness has not been verified."]}
    try:
        pcb = None
        if board_dir.suffix == ".kicad_pcb" and not board_dir.is_dir():
            pcb = board_dir.resolve()
            board_dir = board_directory_for_pcb(pcb)
            if layout is None:
                layout = declared_readiness_layout(pcb, board_dir)
        report = layout.report if layout is not None else board_dir / "output/readiness.json"
        if layout is not None and report.resolve() != report.parent.resolve() / report.name:
            raise ValueError("Readiness receipt leaves its declared location")
        data = json.loads(report.read_text())
        if not isinstance(data, dict):
            raise ValueError("report must be an object")
        if layout is not None:
            layout.validate_declaration(data, board_dir)
        elif pcb is not None and "output_root" in data:
            raise ValueError("Readiness receipt needs its project output declaration")
        if (
            data.get("schema_version") != 1
            or data.get("mode") not in {"assembly", "pcb_only"}
            or data.get("status") not in {"ready", "blocked", "unverified"}
        ):
            raise ValueError("unsupported readiness report")
        if not isinstance(data.get("checked_at"), str):
            raise ValueError("invalid verification timestamp")
        datetime.fromisoformat(data["checked_at"].replace("Z", "+00:00"))
        if not isinstance(data.get("blockers"), list) or not all(
            isinstance(item, str) for item in data["blockers"]
        ):
            raise ValueError("invalid blockers")
        checks = data.get("checks", [])
        metrics = data.get("metrics", {})
        if not isinstance(metrics, dict):
            raise ValueError("invalid current metrics")
        for name in ("drc_violations", "lvs_mismatches", "nets_routed_pct"):
            if name in metrics:
                value = metrics[name]
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                    or (name != "nets_routed_pct" and value != int(value))
                    or (name == "nets_routed_pct" and value > 100)
                ):
                    raise ValueError(f"invalid current metric: {name}")
        if "lvs_clean" in metrics and not isinstance(metrics["lvs_clean"], bool):
            raise ValueError("invalid current metric: lvs_clean")
        if not isinstance(checks, list) or any(
            not isinstance(c, dict)
            or not isinstance(c.get("name"), str)
            or c.get("status") not in {"passed", "failed", "not_run"}
            or ("detail" in c and not isinstance(c["detail"], str))
            for c in checks
        ):
            raise ValueError("invalid checks")
        inputs = data.get("inputs")
        if not isinstance(inputs, dict) or not inputs:
            raise ValueError("missing input hashes")
        root = layout.input_base.resolve() if layout is not None else board_dir.resolve()
        if pcb is not None:
            validate_source_pcb(data, pcb, root, report.parent)
        for name, expected in inputs.items():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("input path must be board-relative")
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise ValueError("input path leaves board directory")
            if not _input_hash_matches(name, target.read_bytes(), expected):
                raise ValueError(f"Evidence is stale: {name} changed")
        external = data.get("external_inputs")
        if external is not None:
            declared = data.get("project_root")
            if not isinstance(external, dict) or not isinstance(declared, str):
                raise ValueError("invalid external inputs")
            project_root = (root / declared).resolve()
            if project_root == root or not root.is_relative_to(project_root):
                raise ValueError("project root must be an ancestor of the board directory")
            for name, expected in external.items():
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name:
                    raise ValueError("external input path must be project-root-relative")
                target = (project_root / path).resolve()
                if not target.is_relative_to(project_root):
                    raise ValueError("external input path leaves project root")
                if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Evidence is stale: {name} changed")
        output = board_dir / "output"
        required = (
            {
                p.resolve().relative_to(root).as_posix()
                for p in output.iterdir()
                if p.is_file()
                and (
                    p.suffix in {".kicad_pro", ".kicad_dru"}
                    or p.name.endswith("net_class_map.json")
                    or p.name.endswith("fab_profile.json")
                )
            }
            if output.is_dir()
            else set()
        )
        # Issue #9006: a waivers sidecar the check gate auto-loads for a
        # hashed PCB is evidence like the rule files, so require it here.
        from kicad_tools.validate.rules.waivers import discover_waivers_sidecar

        for name in inputs:
            if not name.endswith(".kicad_pcb"):
                continue
            sidecar = discover_waivers_sidecar(root / name)
            if sidecar is None:
                continue
            sidecar_resolved = sidecar.resolve()
            if sidecar_resolved.is_relative_to(root):
                required.add(sidecar_resolved.relative_to(root).as_posix())
        # Issue #9322: the CPL rotation sidecar shapes the shipped CPL.
        from kicad_tools.export.rotation_overrides import discover_rotation_overrides

        for name in inputs:
            if not name.endswith(".kicad_pcb"):
                continue
            rotations = discover_rotation_overrides(root / name)
            if rotations is not None and rotations.resolve().is_relative_to(root):
                required.add(rotations.resolve().relative_to(root).as_posix())
        # Issue #10107: every sheet reachable from a hashed schematic shapes the
        # released BOM and drawings, so a child edit must not read as ready.
        from kicad_tools.report.utils import walk_schematic_hierarchy

        if layout is not None and data["status"] == "ready":
            layout.checked_schematic_root(data)
        else:
            for name in list(inputs):
                if not name.endswith(".kicad_sch") or not (root / name).is_file():
                    continue
                walked = walk_schematic_hierarchy(
                    (root / name).resolve(), lambda sheet: sheet.is_relative_to(root)
                )
                required.update(
                    sheet.resolve().relative_to(root).as_posix()
                    for sheet in walked.sheets
                    if sheet.resolve().is_relative_to(root)
                )
        if not required.issubset(inputs):
            raise ValueError("Evidence omits current project or rule files")
        if "manufacturer" in data and (
            not isinstance(data["manufacturer"], str) or not data["manufacturer"]
        ):
            raise ValueError("invalid manufacturer")
        output_prefix = (
            layout.report.parent.resolve().relative_to(root) if layout else Path("output")
        )
        known_issues = _bound_known_issues(data, inputs, root, output_prefix)
        if data["status"] == "ready" and defects(known_issues):
            numbers = ", ".join(f"#{entry['issue']}" for entry in defects(known_issues))
            raise ValueError(f"Ready conflicts with known defect {numbers}")
        if data["status"] == "ready":
            if not {"kct_check", "native_drc", "artifacts", "bom"}.issubset(
                c["name"] for c in checks
            ):
                raise ValueError("Ready requires kct_check, native_drc, artifacts and bom checks")
            if (
                not any(n.endswith(".kicad_pcb") for n in inputs)
                or not any(n.endswith(".kicad_sch") for n in (*inputs, *(external or ())))
                or not any(is_manufacturing_manifest(n) for n in inputs)
            ):
                raise ValueError("Ready requires PCB, schematic and manufacturing evidence")
            if data["blockers"] or any(c["status"] != "passed" for c in checks):
                raise ValueError("Ready conflicts with incomplete or failed checks")
        if data["status"] == "unverified":
            data.pop("metrics", None)
        return data
    except FileNotFoundError:
        return {**unknown, "blockers": ["Readiness report or checked files are missing."]}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**unknown, "blockers": [f"Readiness needs verification: {exc}"]}


def _bound_known_issues(data: dict, inputs: dict, root: Path, output_prefix: Path) -> list[dict]:
    """Validate ``known_issues`` and require it to match its hashed evidence (#7261).

    The report itself is not hashed, so the list is also written to a
    ``known-issues.json`` evidence file listed in ``inputs`` (whose hash was
    checked above).  A list missing from that file, or differing from it, is
    an edited or stale record.  A ``known-issues.json`` left in the evidence
    folder but absent from ``inputs`` is an unbound-and-dropped record (#7323).
    Only the record's own evidence folder(s) are scanned: ``--verify`` writes
    to ``output/readiness-verification/`` while the committed evidence stays in
    ``output/readiness/``, and neither folder orphans the other (#7337).
    """
    known_issues = validate_known_issues(data.get("known_issues"))
    orphans = sorted(
        rel
        for folder in _evidence_folders(data, inputs, output_prefix)
        for path in (root / folder).rglob(KNOWN_ISSUES_FILENAME)
        if path.is_file() and (rel := path.relative_to(root).as_posix()) not in inputs
    )
    if orphans:
        raise ValueError(f"{orphans[0]} is not listed in inputs (orphaned known-issues evidence)")
    bound = [name for name in inputs if Path(name).name == KNOWN_ISSUES_FILENAME]
    if len(bound) > 1:
        raise ValueError("known_issues is bound to more than one evidence file")
    if not bound:
        if known_issues:
            raise ValueError("known_issues is not bound to hashed evidence")
        return known_issues
    recorded = json.loads((root / bound[0]).read_text())
    if (
        not isinstance(recorded, dict)
        or validate_known_issues(recorded.get("known_issues")) != known_issues
    ):
        raise ValueError("known_issues differs from its hashed evidence")
    return known_issues


def _evidence_folders(data: dict, inputs: dict, output_prefix: Path) -> set[str]:
    """Board-relative evidence folders the record itself names (#7337).

    The folder of an evidence path is its first directory below the output root
    (``output/readiness/refilled-native/x.json`` -> ``output/readiness``), or
    its first directory when the board has no ``output/`` folder.  Folders come
    from the ``evidence`` map and from any bound ``known-issues.json``; a
    record naming neither falls back to the default ``output/readiness``.
    """
    evidence = data.get("evidence")
    names = (
        [v for v in evidence.values() if isinstance(v, str)] if isinstance(evidence, dict) else []
    )
    names += [n for n in inputs if Path(n).name == KNOWN_ISSUES_FILENAME]
    folders = set()
    for name in names:
        parts = Path(name).parts
        if ".." in parts or Path(name).is_absolute():
            continue
        depth = len(output_prefix.parts) + 1 if Path(name).is_relative_to(output_prefix) else 1
        if len(parts) > depth:
            folders.add(Path(*parts[:depth]).as_posix())
    return folders or {(output_prefix / "readiness").as_posix()}
