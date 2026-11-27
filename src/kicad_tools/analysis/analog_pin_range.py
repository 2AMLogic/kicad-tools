"""Simulated analog pin ranges across a driven waveform and supply states.

A reviewed assertion sidecar binds device pins to schematic nets and to
externally generated voltage traces. Every sample inside a named window is then
compared with the operating limits that apply to one supply state. An ERC or
connectivity pass cannot supply this evidence. Nothing here runs a simulator,
looks up a rating, predicts clamp current or approves a release.

Run as a module with a flat schematic and the sidecar::

    python -m kicad_tools.analysis.analog_pin_range board.kicad_sch pin-ranges.json
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeGuard

STATES = ("powered", "unpowered")
LABELS = {"omitted": "unclamped_demand", "modeled": "simulated_with_input_protection"}
MAX_SAMPLE_BYTES = 128 << 20
SCOPE = (
    "A pass applies only to the named waveform, supply state, model and window; it is not a "
    "device rating, a clamp-current prediction or release approval"
)
_SIDECAR_FIELDS = frozenset({"schema_version", "schematic_sha256", "samples", "cases"})
_CASE_FIELDS = frozenset(
    {"id", "waveform", "state", "window", "reference_net", "rails", "samples", "model", "pins"}
)
_WINDOW_FIELDS = frozenset({"start_s", "end_s"})
_RAIL_FIELDS = frozenset({"net", "voltage_v", "source"})
_MODEL_FIELDS = frozenset({"input_protection", "assumptions"})
_PIN_FIELDS = frozenset({"reference", "pin", "net", "trace", "limits"})
_LIMIT_FIELDS = frozenset({"state", "lower_v", "upper_v", "source"})
_ENTRY_FIELDS = frozenset({"path", "sha256"})
_SAMPLE_FIELDS = frozenset({"schema_version", "source", "time_s", "traces"})


@dataclass
class _Scope:
    """The validated fields that every pin assertion of one case shares."""

    id: str
    waveform: str | None = None
    state: str | None = None
    window: tuple[float, float] | None = None
    reference_net: str | None = None
    rails: dict[str, Any] | None = None
    rail_voltages: dict[str, set[float]] = field(default_factory=dict)
    samples: str | None = None
    model: dict[str, Any] | None = None
    label: str | None = None


@dataclass
class _Samples:
    """A digest-bound sample file: one shared time axis and named traces."""

    time_s: list[float]
    traces: dict[str, Any]
    source: str | None


@dataclass
class _Binding:
    """Schematic connectivity plus the sidecar's sample files."""

    references: set[str]
    nets: set[str]
    pin_nets: dict[tuple[str, str], set[str]]
    supply_nets: dict[str, set[str]]
    base: Path
    entries: dict[str, Any]
    loaded: dict[str, tuple[_Samples | None, list[str], str | None]] = field(default_factory=dict)

    def samples(self, key: str) -> tuple[_Samples | None, list[str], str | None]:
        if key not in self.loaded:
            entry = self.entries.get(key)
            if entry is None:
                return None, [f"samples {key!r} are not declared in the sidecar"], None
            self.loaded[key] = _load_samples(entry, f"samples {key!r}", self.base)
        return self.loaded[key]


def _text(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _fields(record: Any, allowed: frozenset[str], name: str, problems: list[str]) -> bool:
    """Reject a record that is not an object or that carries unknown fields."""
    if not isinstance(record, dict):
        problems.append(f"{name}: object required")
        return False
    for key in sorted(set(record) - allowed):
        problems.append(f"{name}: unknown field {key!r}")
    return True


class _DuplicateKeyError(ValueError):
    """A JSON object repeated a key, so its meaning is ambiguous."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or None if absent, non-numeric or unrepresentable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _number(value: Any, name: str, problems: list[str]) -> float | None:
    number = _finite(value)
    if number is None:
        problems.append(f"{name}: finite number required")
    return number


def _series(value: Any, name: str, problems: list[str]) -> list[float] | None:
    if not isinstance(value, list):
        problems.append(f"{name}: array of numbers required")
        return None
    result: list[float] = []
    for index, item in enumerate(value):
        number = _finite(item)
        if number is None:
            problems.append(f"{name}: sample {index} is not a finite number")
            return None
        result.append(number)
    return result


def _load_samples(
    entry: Any, name: str, base: Path
) -> tuple[_Samples | None, list[str], str | None]:
    """Read one sample file, check its digest and return its shared time axis."""
    problems: list[str] = []
    digest: str | None = None

    def fail(message: str) -> tuple[None, list[str], str | None]:
        problems.append(message)
        return None, problems, digest

    if not _fields(entry, _ENTRY_FIELDS, name, problems):
        return None, problems, digest
    path_text, expected = entry.get("path"), entry.get("sha256")
    if not _text(path_text):
        return fail(f"{name}: path absent")
    path = Path(path_text)
    if not path.is_absolute():
        path = base / path
    try:
        if path.stat().st_size > MAX_SAMPLE_BYTES:
            return fail(f"{name}: {path_text} exceeds the {MAX_SAMPLE_BYTES} byte limit")
        data = path.read_bytes()
    except OSError as exc:
        return fail(f"{name}: cannot read {path_text}: {exc.strerror}")
    digest = hashlib.sha256(data).hexdigest()
    if expected != digest:
        problems.append(f"{name}: digest mismatch for {path_text}")
    try:
        parsed = json.loads(data, object_pairs_hook=_no_duplicates)
    except _DuplicateKeyError as exc:
        return fail(f"{name}: {path_text} is ambiguous: {exc}")
    except ValueError:
        return fail(f"{name}: {path_text} is not valid JSON")
    label = f"{name}: {path_text}"
    if not _fields(parsed, _SAMPLE_FIELDS, label, problems):
        return None, problems, digest
    if parsed.get("schema_version") != 1:
        return fail(f"{label}: unsupported sample schema")
    time_s = _series(parsed.get("time_s"), f"{label}: time_s", problems)
    if time_s is None:
        return None, problems, digest
    if len(time_s) < 2:
        return fail(f"{label}: time_s needs at least two samples")
    if any(later <= earlier for earlier, later in zip(time_s, time_s[1:], strict=False)):
        return fail(f"{label}: time_s must be strictly increasing")
    traces = parsed.get("traces")
    if not isinstance(traces, dict) or not traces or not all(map(_text, traces)):
        return fail(f"{label}: traces must be a nonempty object of named arrays")
    source = parsed.get("source")
    return _Samples(time_s, traces, source if _text(source) else None), problems, digest


def _window(window: Any, name: str, problems: list[str]) -> tuple[float, float] | None:
    if not _fields(window, _WINDOW_FIELDS, f"{name}: window", problems):
        return None
    start = _number(window.get("start_s"), f"{name}: window start_s", problems)
    end = _number(window.get("end_s"), f"{name}: window end_s", problems)
    if start is None or end is None:
        return None
    if not end > start:
        problems.append(f"{name}: window end_s must exceed start_s")
        return None
    return start, end


def _rails(
    rails: Any, state: str | None, binding: _Binding, name: str, problems: list[str]
) -> dict[str, Any] | None:
    if not isinstance(rails, dict) or not rails:
        problems.append(f"{name}: rail evidence absent for the {state or 'declared'} state")
        return None
    echo: dict[str, Any] = {}
    valid = True
    for rail_name, rail in rails.items():
        label = f"{name}: rail {rail_name}"
        if not _fields(rail, _RAIL_FIELDS, label, problems):
            valid = False
            continue
        net, source = rail.get("net"), rail.get("source")
        voltage = _number(rail.get("voltage_v"), f"{label} voltage_v", problems)
        if not _text(net):
            problems.append(f"{label}: net absent")
        elif net not in binding.nets:
            problems.append(f"{label}: net {net!r} is not in the schematic")
        if not _text(source):
            problems.append(f"{label}: source absent")
        valid = valid and voltage is not None and _text(net) and _text(source)
        echo[rail_name] = {"net": net, "voltage_v": voltage, "source": source}
    return echo if valid else None


def _model(model: Any, name: str, problems: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    if not _fields(model, _MODEL_FIELDS, f"{name}: model", problems):
        return None, None
    protection, assumptions = model.get("input_protection"), model.get("assumptions")
    declared = isinstance(protection, str) and protection in LABELS
    if not declared:
        problems.append(f"{name}: model must declare input_protection as 'omitted' or 'modeled'")
    if not isinstance(assumptions, list) or not assumptions or not all(map(_text, assumptions)):
        problems.append(f"{name}: model assumptions absent")
        assumptions = None
    echo = None
    if assumptions is not None and isinstance(protection, str):
        echo = {"input_protection": protection, "assumptions": assumptions}
    return echo, LABELS.get(protection) if declared else None


def _scope(case: dict[str, Any], binding: _Binding) -> tuple[_Scope, list[str]]:
    problems: list[str] = []
    declared_id = case.get("id")
    scope = _Scope(id=declared_id if _text(declared_id) else "<unnamed>")
    name = scope.id
    if not _text(declared_id):
        problems.append(f"{name}: case id absent")
    for key in sorted(set(case) - _CASE_FIELDS):
        problems.append(f"{name}: unknown case field {key!r}")
    waveform = case.get("waveform")
    if _text(waveform):
        scope.waveform = waveform
    else:
        problems.append(f"{name}: waveform name absent")
    state = case.get("state")
    if state in STATES:
        scope.state = state
    else:
        problems.append(f"{name}: supply state must be 'powered' or 'unpowered'")
    scope.window = _window(case.get("window"), name, problems)
    net = case.get("reference_net")
    if not _text(net):
        problems.append(f"{name}: voltage reference net absent")
    elif net not in binding.nets:
        problems.append(f"{name}: voltage reference net {net!r} is not in the schematic")
    else:
        scope.reference_net = net
    scope.rails = _rails(case.get("rails"), scope.state, binding, name, problems)
    if scope.rails:
        for rail in scope.rails.values():
            scope.rail_voltages.setdefault(rail["net"], set()).add(rail["voltage_v"])
    sample_set = case.get("samples")
    if _text(sample_set):
        scope.samples = sample_set
    else:
        problems.append(f"{name}: samples are not named")
    scope.model, scope.label = _model(case.get("model"), name, problems)
    return scope, problems


def _bound(
    reference: Any, pin: Any, net: Any, scope: _Scope, binding: _Binding, problems: list[str]
) -> bool:
    """Check the declared reference, pin and net against the schematic netlist."""
    who = f"{scope.id}: {reference}.{pin}"
    if not _text(reference) or not _text(pin):
        problems.append(f"{scope.id}: pin reference and number are required")
    elif reference not in binding.references:
        problems.append(f"{scope.id}: {reference}: reference is not in the schematic")
    elif (reference, pin) not in binding.pin_nets:
        problems.append(f"{who}: pin is not connected in the schematic")
    else:
        actual = sorted(binding.pin_nets[reference, pin])
        if actual == [net]:
            return True
        problems.append(f"{who}: expected net {net!r}, schematic has {', '.join(actual)!r}")
    return False


def _supply(
    scope: _Scope, reference: Any, binding: _Binding, who: str, problems: list[str]
) -> None:
    """Require evidence for the device's own supply, not only the shared reference."""
    if scope.rails is None or scope.state is None:
        return
    supply_nets = binding.supply_nets.get(reference, set())
    required = {net for net in supply_nets if net != scope.reference_net}
    missing = required.difference(scope.rail_voltages)
    if not required or missing:
        detail = f" ({', '.join(sorted(missing))})" if missing else ""
        problems.append(
            f"{who}: no rail evidence for a supply net of {reference}{detail} other than the "
            f"reference net, so the {scope.state} state is not established"
        )
    relevant = required.copy()
    if scope.reference_net is not None:
        relevant.add(scope.reference_net)
    conflicts = sorted(net for net in relevant if len(scope.rail_voltages.get(net, ())) > 1)
    for net in conflicts:
        values = ", ".join(f"{v} V" for v in sorted(scope.rail_voltages[net]))
        problems.append(f"{who}: conflicting rail voltages for {net}: {values}")
    if not required or missing or conflicts:
        return
    volts = [v for net in required for v in scope.rail_voltages[net]]
    if scope.state == "powered" and not any(v != 0.0 for v in volts):
        problems.append(f"{who}: every supply rail of {reference} is 0 V in a powered case")
    elif scope.state == "unpowered" and any(v != 0.0 for v in volts):
        problems.append(f"{who}: a supply rail of {reference} is nonzero in an unpowered case")


def _trace(
    scope: _Scope, trace: Any, who: str, binding: _Binding, row: dict[str, Any], problems: list[str]
) -> tuple[_Samples | None, list[float] | None]:
    """Resolve the named trace inside the case's digest-bound sample file."""
    if scope.samples is None:
        return None, None
    samples, sample_problems, digest = binding.samples(scope.samples)
    problems.extend(sample_problems)
    row["samples"] = {
        "name": scope.samples,
        "sha256": digest,
        "source": samples.source if samples is not None else None,
    }
    if samples is None or sample_problems:
        return None, None
    if not _text(trace):
        problems.append(f"{who}: trace name absent")
    elif trace not in samples.traces:
        problems.append(f"{who}: trace {trace!r} is absent from samples {scope.samples!r}")
    else:
        series = _series(samples.traces[trace], f"{who}: trace {trace!r}", problems)
        if series is not None and len(series) != len(samples.time_s):
            problems.append(
                f"{who}: trace {trace!r} has {len(series)} samples for {len(samples.time_s)} times"
            )
        else:
            return samples, series
    return samples, None


def _limits(
    limits: Any, scope: _Scope, who: str, row: dict[str, Any], problems: list[str]
) -> tuple[float | None, float | None]:
    """Read the limits that explicitly apply to the case's supply state."""
    limits = limits if isinstance(limits, dict) else {}
    declared = limits.get("state")
    if scope.state is None or declared != scope.state:
        problems.append(
            f"{who}: limits applicable to the {scope.state or 'case'} state are absent; "
            f"declared state is {declared!r}"
        )
        return None, None
    for key in sorted(set(limits) - _LIMIT_FIELDS):
        problems.append(f"{who}: limits: unknown field {key!r}")
    lower = _number(limits.get("lower_v"), f"{who}: limits lower_v", problems)
    upper = _number(limits.get("upper_v"), f"{who}: limits upper_v", problems)
    if lower is not None and upper is not None and lower > upper:
        problems.append(f"{who}: limits lower_v exceeds upper_v")
        lower = upper = None
    source = limits.get("source")
    if not _text(source):
        problems.append(f"{who}: limits source absent")
    elif lower is not None and upper is not None:
        row["limits"] = {"state": declared, "lower_v": lower, "upper_v": upper, "source": source}
    return lower, upper


def _extrema(
    samples: _Samples | None,
    series: list[float] | None,
    scope: _Scope,
    who: str,
    row: dict[str, Any],
    problems: list[str],
) -> tuple[float | None, float | None]:
    """Take the signed extrema over every sample inside the window."""
    if samples is None or series is None or scope.window is None:
        return None, None
    times = samples.time_s
    start, end = scope.window
    low, high = bisect.bisect_left(times, start), bisect.bisect_right(times, end)
    row["sample_count"] = high - low
    if times[0] > start or times[-1] < end:
        span = f"{times[0]:.6g}..{times[-1]:.6g} s"
        problems.append(
            f"{who}: samples span {span} and do not cover the window {start:.6g}..{end:.6g} s"
        )
    elif high - low < 2:
        problems.append(f"{who}: window holds {high - low} sample; at least two are required")
    else:
        minimum, maximum = min(series[low:high]), max(series[low:high])
        row["min_v"], row["max_v"] = minimum, maximum
        return minimum, maximum
    return None, None


def _violations(minimum: float, maximum: float, lower: float, upper: float) -> list[dict[str, Any]]:
    """One entry per violated side; a sample equal to a limit does not violate it."""
    violations: list[dict[str, Any]] = []
    if minimum < lower:
        violations.append(
            {
                "side": "lower",
                "limit_v": lower,
                "extreme_v": minimum,
                "excursion_v": lower - minimum,
            }
        )
    if maximum > upper:
        violations.append(
            {
                "side": "upper",
                "limit_v": upper,
                "extreme_v": maximum,
                "excursion_v": maximum - upper,
            }
        )
    return violations


def _pin_row(
    assertion: Any, scope: _Scope, inherited: list[str], binding: _Binding
) -> dict[str, Any]:
    problems = list(inherited)
    row: dict[str, Any] = {
        "case": scope.id,
        "waveform": scope.waveform,
        "state": scope.state,
        "window": None,
        "reference_net": scope.reference_net,
        "reference": None,
        "pin": None,
        "net": None,
        "trace": None,
        "samples": None,
        "rails": scope.rails,
        "model": scope.model,
        "label": scope.label,
        "limits": None,
        "sample_count": 0,
        "min_v": None,
        "max_v": None,
        "violations": [],
        "outcome": "unassessed",
        "reasons": problems,
    }
    if scope.window:
        row["window"] = {"start_s": scope.window[0], "end_s": scope.window[1]}
    if not _fields(assertion, _PIN_FIELDS, f"{scope.id}: pin", problems):
        return row
    reference, pin = assertion.get("reference"), assertion.get("pin")
    if isinstance(pin, int) and not isinstance(pin, bool):
        pin = str(pin)
    net, trace = assertion.get("net"), assertion.get("trace")
    for key, value in (("reference", reference), ("pin", pin), ("net", net), ("trace", trace)):
        row[key] = value if isinstance(value, str) else None
    who = f"{scope.id}: {reference}.{pin}"
    bound = _bound(reference, pin, net, scope, binding, problems)
    traceable = bound and scope.reference_net is not None
    if bound:
        _supply(scope, reference, binding, who, problems)
    samples, series = _trace(scope, trace, who, binding, row, problems)
    lower, upper = _limits(assertion.get("limits"), scope, who, row, problems)
    minimum, maximum = (
        _extrema(samples, series, scope, who, row, problems) if traceable else (None, None)
    )
    if problems or lower is None or upper is None or minimum is None or maximum is None:
        return row
    violations = _violations(minimum, maximum, lower, upper)
    if not all(math.isfinite(item["excursion_v"]) for item in violations):
        problems.append(f"{who}: excursion is not representable as a finite number")
        return row
    row["violations"] = violations
    row["outcome"] = "fail" if violations else "pass"
    row["reasons"] = []
    return row


def analyze_pin_ranges(schematic: str | Path, sidecar: str | Path) -> dict[str, Any]:
    """Compare supplied traces with the limits of each bound pin and case."""
    from kicad_tools.operations.netlist import build_netlist_from_schematic
    from kicad_tools.schema.schematic import Schematic

    path, assertions = Path(schematic), Path(sidecar)
    source = path.read_bytes()
    raw = assertions.read_bytes()
    report: dict[str, Any] = {
        "schema_version": 1,
        "schematic_sha256": hashlib.sha256(source).hexdigest(),
        "sidecar_sha256": hashlib.sha256(raw).hexdigest(),
        "status": "unassessed",
        "release_eligible": False,
        "scope": SCOPE,
        "pins": [],
        "issues": [],
    }
    issues: list[str] = report["issues"]
    try:
        policy = json.loads(raw, object_pairs_hook=_no_duplicates)
    except _DuplicateKeyError as exc:
        issues.append(f"Sidecar is ambiguous: {exc}")
        return report
    if not _fields(policy, _SIDECAR_FIELDS, "sidecar", issues):
        return report
    if policy.get("schema_version") != 1:
        issues.append("Unsupported sidecar schema")
        return report
    if policy.get("schematic_sha256") != report["schematic_sha256"]:
        issues.append("Stale schematic binding: source hash changed")
        return report
    if Schematic.load(path).sheets:
        issues.append("Hierarchical schematic binding is not supported; no partial hierarchy pass")
        return report
    netlist = build_netlist_from_schematic(path, include_symbol_pins=True)
    supply_pins = {
        (component.reference, pin.number)
        for component in netlist.components
        for pin in component.pins
        if pin.pin_type == "power_in"
    }
    supply_nets: dict[str, set[str]] = {}
    pin_nets: dict[tuple[str, str], set[str]] = {}
    for net in netlist.nets:
        for node in net.nodes:
            key = (node.reference, node.pin)
            pin_nets.setdefault(key, set()).add(net.name)
            if key in supply_pins:
                supply_nets.setdefault(node.reference, set()).add(net.name)
    entries = policy.get("samples")
    if not isinstance(entries, dict):
        issues.append("Sample sets must be an object")
        entries = {}
    binding = _Binding(
        references={component.reference for component in netlist.components},
        nets={net.name for net in netlist.nets},
        pin_nets=pin_nets,
        supply_nets=supply_nets,
        base=assertions.parent,
        entries=entries,
    )
    cases = policy.get("cases")
    if not isinstance(cases, list) or not cases:
        issues.append("No cases declared")
    seen: set[str] = set()
    asserted: set[tuple[str, str, str]] = set()
    for item in cases if isinstance(cases, list) else []:
        if not isinstance(item, dict):
            issues.append("Each case must be an object")
            continue
        scope, problems = _scope(item, binding)
        if scope.id in seen:
            issues.append(f"Duplicate case id: {scope.id}")
        seen.add(scope.id)
        pins = item.get("pins")
        if not isinstance(pins, list) or not pins:
            issues.append(f"{scope.id}: no pin assertions")
        for assertion in pins if isinstance(pins, list) else []:
            if isinstance(assertion, dict):
                identity = (scope.id, str(assertion.get("reference")), str(assertion.get("pin")))
                if identity in asserted:
                    issues.append(
                        f"{scope.id}: duplicate assertion for {identity[1]}.{identity[2]}"
                    )
                asserted.add(identity)
            report["pins"].append(_pin_row(assertion, scope, problems, binding))
    if path.read_bytes() != source:
        issues.append("Schematic changed while calculating evidence")
    outcomes = {entry["outcome"] for entry in report["pins"]}
    report["status"] = (
        "failed"
        if "fail" in outcomes
        else "unassessed"
        if issues or "unassessed" in outcomes
        else "passed"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path)
    parser.add_argument("sidecar", type=Path)
    args = parser.parse_args(argv)
    try:
        report = analyze_pin_ranges(args.schematic, args.sidecar)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        report = {
            "schema_version": 1,
            "status": "unassessed",
            "release_eligible": False,
            "pins": [],
            "issues": [str(exc)],
        }
    print(json.dumps(report, indent=2, allow_nan=False))
    return {"failed": 1, "passed": 0}.get(report["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
