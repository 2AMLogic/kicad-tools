"""Revision-bound simulation limits and fault-case evidence for a manufacturing export.

Copper DRC cannot see circuit operating limits (an optocoupler driven past its
datasheet RMS current, a gate-feed resistor over its power rating).  A project
may therefore *declare* simulation cases beside its board, and an external
producer (ngspice, a lab script, ...) supplies a result record for each.  The
exporter only **reads** those files: it never runs a simulator or a project
command, parses no waveforms and converts no units.

Declaration -- ``<board stem>.simulations.json`` next to the PCB::

    {"cases": [{
        "name": "fault-installed-ballast",
        "deck": "sim/fault.cir",
        "result": "sim/fault.result.json",
        "quantity": "optotriac_irms",
        "unit": "mA",
        "upper": 100,
        "inputs": ["sim/models/opto.lib"]
    }]}

Each case needs ``name``, project-relative ``deck`` and ``result`` paths,
``quantity``, ``unit`` and an inclusive ``upper`` and/or ``lower`` bound.
``inputs`` optionally lists further project-relative files the deck needs.

Result record, written by the producer::

    {"case": "fault-installed-ballast", "quantity": "optotriac_irms",
     "unit": "mA", "value": 85.6, "deck_sha256": "<sha256 of the deck bytes>",
     "inputs": {"sim/models/opto.lib": "<sha256>"}}

The producer's digests are compared with the *current* bytes before the limit
is evaluated, so an obsolete measurement can never pass.  Verdicts, kept
separate from native DRC:

``pass``          measured value within the bound, tested inputs current
``out_of_range``  measured value violates the bound
``stale``         a tested input digest differs from the current bytes
``missing``       the declared deck or result file does not exist
``invalid``       declaration or record is malformed, mismatched or non-finite
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PASS = "pass"
OUT_OF_RANGE = "out_of_range"
STALE = "stale"
MISSING = "missing"
INVALID = "invalid"

#: Bundle sub-directory holding the packaged evidence.
EVIDENCE_DIR = "simulation"
DECLARATION_SUFFIX = ".simulations.json"

_MAX_JSON_BYTES = 1 << 20
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def declaration_path(pcb_path: Path) -> Path:
    """Where a board's optional simulation declaration lives."""
    return pcb_path.with_name(pcb_path.stem + DECLARATION_SUFFIX)


@dataclass
class CaseVerdict:
    """The evaluated outcome of one declared case."""

    name: str
    status: str
    quantity: str = ""
    unit: str = ""
    upper: float | None = None
    lower: float | None = None
    measured: float | None = None
    diagnostic: str = ""
    tested_sha256: dict[str, str] = field(default_factory=dict)
    current_sha256: dict[str, str] = field(default_factory=dict)
    #: bundle-relative path -> bytes to package for this case
    evidence: dict[str, bytes] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.quantity:
            d["quantity"] = self.quantity
            d["unit"] = self.unit
        if self.upper is not None:
            d["upper"] = self.upper
        if self.lower is not None:
            d["lower"] = self.lower
        if self.measured is not None:
            d["measured"] = self.measured
        if self.diagnostic:
            d["diagnostic"] = self.diagnostic
        if self.tested_sha256:
            d["tested_sha256"] = dict(self.tested_sha256)
        if self.current_sha256:
            d["current_sha256"] = dict(self.current_sha256)
        d["evidence"] = sorted(self.evidence)
        return d

    def describe(self) -> str:
        """One human-readable review row."""
        label = self.status.upper().replace("_", " ")
        if self.measured is not None and self.quantity:
            bound = " and ".join(
                part
                for part in (
                    f">= {self.lower:g} {self.unit}" if self.lower is not None else "",
                    f"<= {self.upper:g} {self.unit}" if self.upper is not None else "",
                )
                if part
            )
            line = (
                f"[{label}] {self.name}: {self.quantity} = {self.measured:g} {self.unit}"
                f" (limit {bound})"
            )
        else:
            line = f"[{label}] {self.name}"
            if self.quantity:
                line += f": {self.quantity} ({self.unit})"
        if self.diagnostic:
            line += f" -- {self.diagnostic}"
        return line


@dataclass
class SimulationEvidence:
    """All declared cases of one export plus the declaration bytes."""

    declaration_name: str
    declaration_bytes: bytes | None = None
    cases: list[CaseVerdict] = field(default_factory=list)
    diagnostic: str = ""

    @property
    def counts(self) -> dict[str, int]:
        out = {PASS: 0, OUT_OF_RANGE: 0, STALE: 0, MISSING: 0, INVALID: 0}
        for case in self.cases:
            out[case.status] += 1
        return out

    @property
    def all_passed(self) -> bool:
        return (
            not self.diagnostic and bool(self.cases) and all(c.status == PASS for c in self.cases)
        )

    def evidence_files(self) -> dict[str, bytes]:
        """Everything to package, keyed by bundle-relative POSIX path."""
        files: dict[str, bytes] = {}
        if self.declaration_bytes is not None:
            files[f"{EVIDENCE_DIR}/declaration.json"] = self.declaration_bytes
        for case in self.cases:
            files.update(case.evidence)
        return files

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "declaration": f"{EVIDENCE_DIR}/declaration.json"
            if self.declaration_bytes is not None
            else None,
            "summary": self.counts,
            "cases": [c.to_dict() for c in self.cases],
        }
        if self.diagnostic:
            d["diagnostic"] = self.diagnostic
        return d

    def warning_lines(self) -> list[str]:
        lines = []
        if self.diagnostic:
            lines.append(f"Simulation evidence: {self.diagnostic}")
        lines.extend(f"Simulation evidence {c.describe()}" for c in self.cases if c.status != PASS)
        return lines


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _project_file(root: Path, rel: Any) -> tuple[str, Path]:
    """Validate a project-relative reference; return ``(posix relpath, resolved path)``.

    Raises ``ValueError`` for anything that is not a plain relative path that
    stays inside the project root after symlinks are resolved.
    """
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        raise ValueError(f"path {rel!r} is not a project-relative path")
    posix = Path(rel)
    if posix.is_absolute() or ".." in posix.parts:
        raise ValueError(f"path {rel!r} is outside the project root")
    resolved = (root / posix).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path {rel!r} is outside the project root")
    return posix.as_posix(), resolved


def _read(path: Path, limit: int | None = None) -> bytes | None:
    if not path.is_file():
        return None
    if limit is not None and path.stat().st_size > limit:
        raise ValueError(f"{path.name} is larger than {limit} bytes")
    return path.read_bytes()


def _load_json(data: bytes, what: str) -> dict:
    try:
        parsed = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{what} must be a JSON object")
    return parsed


def _reject_constant(token: str) -> float:
    raise ValueError(f"non-finite value {token}")


def _bound(case: dict, key: str) -> float | None:
    if key not in case:
        return None
    value = case[key]
    if not _finite(value):
        raise ValueError(f"{key} must be a finite number")
    return float(value)


def _evaluate_case(root: Path, case: Any, index: int) -> CaseVerdict:
    name = case.get("name") if isinstance(case, dict) else None
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return CaseVerdict(
            name=f"case[{index}]",
            status=INVALID,
            diagnostic="case needs a 'name' of letters, digits, '.', '_' or '-'",
        )
    verdict = CaseVerdict(name=name, status=INVALID)
    try:
        quantity, unit = case.get("quantity"), case.get("unit")
        if not isinstance(quantity, str) or not quantity or not isinstance(unit, str) or not unit:
            raise ValueError("'quantity' and 'unit' must be non-empty strings")
        verdict.quantity, verdict.unit = quantity, unit
        verdict.upper, verdict.lower = _bound(case, "upper"), _bound(case, "lower")
        if verdict.upper is None and verdict.lower is None:
            raise ValueError("declare an inclusive 'upper' and/or 'lower' bound")
        if (
            verdict.upper is not None
            and verdict.lower is not None
            and verdict.lower > verdict.upper
        ):
            raise ValueError("'lower' bound exceeds 'upper' bound")
        extras = case.get("inputs", [])
        if not isinstance(extras, list):
            raise ValueError("'inputs' must be a list of project-relative paths")
        deck_rel, deck_path = _project_file(root, case.get("deck"))
        result_rel, result_path = _project_file(root, case.get("result"))
        input_refs = [_project_file(root, ref) for ref in extras]
    except ValueError as exc:
        verdict.diagnostic = str(exc)
        return verdict

    try:
        deck = _read(deck_path)
        record_bytes = _read(result_path, _MAX_JSON_BYTES)
        inputs = {rel: _read(path) for rel, path in input_refs}
    except (OSError, ValueError) as exc:
        verdict.diagnostic = f"cannot read declared file: {exc}"
        return verdict

    def package(rel: str, data: bytes | None) -> None:
        if data is not None:
            verdict.evidence[f"{EVIDENCE_DIR}/{name}/{rel}"] = data

    package(deck_rel, deck)
    package(result_rel, record_bytes)
    for rel, data in inputs.items():
        package(rel, data)
    current = {deck_rel: _sha256(deck)} if deck is not None else {}
    current.update({rel: _sha256(data) for rel, data in inputs.items() if data is not None})
    verdict.current_sha256 = current

    absent = [
        rel
        for rel, data in [(deck_rel, deck), (result_rel, record_bytes), *inputs.items()]
        if data is None
    ]
    if absent:
        verdict.status = MISSING
        verdict.diagnostic = "declared file not found: " + ", ".join(absent)
        return verdict

    try:
        assert record_bytes is not None
        record = _load_json(record_bytes, f"result {result_rel}")
        if record.get("case") != name:
            raise ValueError(f"result is for case {record.get('case')!r}, not {name!r}")
        if record.get("quantity") != verdict.quantity:
            raise ValueError(
                f"result quantity {record.get('quantity')!r} != declared {verdict.quantity!r}"
            )
        if record.get("unit") != verdict.unit:
            raise ValueError(f"result unit {record.get('unit')!r} != declared {verdict.unit!r}")
        if not _finite(record.get("value")):
            raise ValueError("result 'value' must be a finite number")
        verdict.measured = float(record["value"])
        tested = {deck_rel: record.get("deck_sha256")}
        recorded_inputs = record.get("inputs", {})
        if not isinstance(recorded_inputs, dict):
            raise ValueError("result 'inputs' must map path to sha256")
        for rel, _ in input_refs:
            tested[rel] = recorded_inputs.get(rel)
        for rel, digest in tested.items():
            if not isinstance(digest, str) or not _SHA_RE.match(digest):
                raise ValueError(f"result lacks a sha256 digest for tested input {rel}")
    except ValueError as exc:
        verdict.diagnostic = str(exc)
        return verdict

    verdict.tested_sha256 = tested  # type: ignore[assignment]
    changed = [rel for rel, digest in tested.items() if digest != current[rel]]
    if changed:
        verdict.status = STALE
        verdict.diagnostic = "input changed since the result was produced: " + ", ".join(changed)
        return verdict

    measured = verdict.measured
    if (verdict.upper is not None and measured > verdict.upper) or (
        verdict.lower is not None and measured < verdict.lower
    ):
        verdict.status = OUT_OF_RANGE
    else:
        verdict.status = PASS
    return verdict


def collect_simulation_evidence(
    pcb_path: Path, project_root: Path | None = None
) -> SimulationEvidence | None:
    """Evaluate a board's declared simulation cases, or ``None`` when it has none."""
    declaration = declaration_path(pcb_path)
    if not declaration.is_file():
        return None
    root = (project_root or pcb_path.parent).resolve()
    evidence = SimulationEvidence(declaration_name=declaration.name)
    try:
        evidence.declaration_bytes = _read(declaration, _MAX_JSON_BYTES)
        assert evidence.declaration_bytes is not None
        parsed = _load_json(evidence.declaration_bytes, declaration.name)
        cases = parsed.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError("'cases' must be a non-empty list")
    except (OSError, ValueError) as exc:
        evidence.diagnostic = f"{declaration.name}: {exc}"
        return evidence

    seen: set[str] = set()
    for index, case in enumerate(cases):
        verdict = _evaluate_case(root, case, index)
        if verdict.name in seen:
            verdict = CaseVerdict(
                name=f"{verdict.name}#{index}",
                status=INVALID,
                diagnostic=f"duplicate case name {verdict.name!r}",
            )
        seen.add(verdict.name)
        evidence.cases.append(verdict)
    return evidence


def readme_lines(evidence: SimulationEvidence, native_drc: str | None) -> list[str]:
    """README section: one row per case, native DRC reported on its own line."""
    title = "Simulation evidence (supplied by an external producer; not native DRC):"
    lines = [title, "-" * len(title)]
    lines.append(f"  Native DRC: {native_drc or 'not run'}  (independent of the rows below)")
    if evidence.diagnostic:
        lines.append(f"  INVALID declaration: {evidence.diagnostic}")
    else:
        counts = evidence.counts
        lines.append(
            "  Summary: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items() if n)
        )
    lines.extend(f"  {case.describe()}" for case in evidence.cases)
    lines.append(f"  Evidence files: {EVIDENCE_DIR}/ (hashed in manifest.json)")
    lines.append("")
    return lines
