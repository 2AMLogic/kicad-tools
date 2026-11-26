"""Compare explicit, per-instance circuit expectations with the two design sources."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from kicad_tools.lvs.board_lvs import (
    _is_placeholder_net,
    _iter_instance_symbols,
    _schematic_pin_to_net,
)
from kicad_tools.operations.netlist import Netlist, export_netlist
from kicad_tools.schema.pcb import PCB

Name = Annotated[str, StringConstraints(min_length=1)]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Role(_Record):
    value: Name
    pins: dict[Name, Name] = Field(min_length=1)
    fields: dict[Name, str] = Field(default_factory=dict)


class Instance(_Record):
    references: dict[Name, Name]
    nets: dict[Name, Name]


class ChannelIntent(_Record):
    roles: dict[Name, Role] = Field(min_length=1)
    private_nets: list[Name] = Field(default_factory=list)
    instances: dict[Name, Instance] = Field(min_length=1)

    @classmethod
    def load(cls, path: Path) -> ChannelIntent:
        def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate declaration key {key!r}")
                result[key] = value
            return result

        return cls.model_validate(
            json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
        )

    @model_validator(mode="after")
    def check_bindings(self) -> ChannelIntent:
        aliases = {alias for role in self.roles.values() for alias in role.pins.values()}
        private = set(self.private_nets)
        if private - aliases:
            raise ValueError(f"Unknown private nets: {sorted(private - aliases)}")
        seen_refs: set[str] = set()
        used_nets: set[str] = set()
        private_names: set[str] = set()
        for role_name, role in self.roles.items():
            if {"Value", "Reference"} & role.fields.keys():
                raise ValueError(f"{role_name}: Value and Reference have dedicated bindings")
        for name, instance in self.instances.items():
            for kind, expected, actual in [
                ("references", self.roles.keys(), instance.references.keys()),
                ("nets", aliases, instance.nets.keys()),
            ]:
                if expected != actual:
                    raise ValueError(
                        f"{name}.{kind}: missing {sorted(expected - actual)}, unknown {sorted(actual - expected)}"
                    )
            for ref in instance.references.values():
                if ref in seen_refs:
                    raise ValueError(f"{name}: reference {ref!r} is bound more than once")
                seen_refs.add(ref)
            for alias, net in instance.nets.items():
                if net in private_names or (alias in private and net in used_nets):
                    raise ValueError(f"{name}.{alias}: private net {net!r} has another binding")
                used_nets.add(net)
                if alias in private:
                    private_names.add(net)
        return self


@dataclass
class _Source:
    # A multi-unit symbol can supply several field sets for one reference.
    fields: dict[str, list[dict[str, str]]] = field(default_factory=lambda: defaultdict(list))
    # Stacked pads with the same number must all agree with the declared net.
    pins: dict[tuple[str, str], set[str | None]] = field(default_factory=lambda: defaultdict(set))

    def add_component(self, ref: str, value: str, properties: dict[str, str]) -> None:
        fields = properties.copy()
        if value:
            fields["Value"] = value
        self.fields[ref].append(fields)

    @classmethod
    def from_schematic(cls, path: Path, intent: ChannelIntent) -> _Source:
        source = cls()
        if not path.is_file():
            raise FileNotFoundError(path)
        for ref, symbol, _ in _iter_instance_symbols(path, placement_refs=True):
            source.add_component(ref, symbol.value, symbol.properties)
        pin_names = _schematic_pin_to_net(path, placement_refs=True)
        # The LVS graph identifies an unlabelled connection by an arbitrary
        # representative pin. Only KiCad's actual name can be compared literally.
        unnamed = {
            (ref, pin)
            for instance in intent.instances.values()
            for role, ref in instance.references.items()
            for pin in intent.roles[role].pins
            if _is_placeholder_net(pin_names.get((ref, pin)))
        }
        for key, name in pin_names.items():
            if key not in unnamed:
                source.pins[key].add(name)
        if unnamed:
            with TemporaryDirectory() as tmp:
                native = export_netlist(path, Path(tmp) / "channel.net", fallback=False)
            for net in native.nets:
                for node in net.nodes:
                    key = (node.reference, node.pin)
                    if key in unnamed:
                        source.pins[key].add(net.name or None)
        return source

    @classmethod
    def from_netlist(cls, path: Path) -> _Source:
        source = cls()
        netlist = Netlist.load(path)
        for part in netlist.components:
            source.add_component(part.reference, part.value, part.properties)
        for net in netlist.nets:
            for node in net.nodes:
                source.pins[node.reference, node.pin].add(net.name or None)
        return source

    @classmethod
    def from_pcb(cls, path: Path) -> _Source:
        source = cls()
        for fp in PCB.load(path).footprints:
            source.add_component(fp.reference, fp.value, fp.properties)
            for pad in fp.pads:
                source.pins[fp.reference, pad.number].add(pad.net_name or None)
        return source


@dataclass(frozen=True)
class Finding:
    instance: str
    reference: str
    source: str
    pin: str | None
    field: str | None
    expected: str
    observed: str | None


def _compare(intent: ChannelIntent, source: _Source, label: str) -> list[Finding]:
    findings = []
    for name, instance in intent.instances.items():
        for role_name, role in intent.roles.items():
            ref = instance.references[role_name]
            if ref not in source.fields:
                findings.append(Finding(name, ref, label, None, "Reference", ref, None))
                continue
            for field_name, expected in {"Value": role.value, **role.fields}.items():
                values = {fields.get(field_name) for fields in source.fields[ref]}
                for observed in sorted(values - {expected}, key=repr):
                    findings.append(Finding(name, ref, label, None, field_name, expected, observed))
            for pin, alias in role.pins.items():
                expected = instance.nets[alias]
                nets = source.pins.get((ref, pin), {None})
                for observed in sorted(nets - {expected}, key=repr):
                    findings.append(Finding(name, ref, label, pin, None, expected, observed))
    return findings


def check_channel_intent(declaration: Path, design: Path, pcb: Path) -> list[Finding]:
    """Check declared bindings independently and return all source-specific mismatches."""
    intent = ChannelIntent.load(declaration)
    if design.suffix == ".kicad_sch":
        findings = _compare(intent, _Source.from_schematic(design, intent), "schematic")
    else:
        findings = _compare(intent, _Source.from_netlist(design), "netlist")
    findings.extend(_compare(intent, _Source.from_pcb(pcb), "pcb"))
    return findings
