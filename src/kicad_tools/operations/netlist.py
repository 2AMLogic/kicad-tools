"""
Netlist parsing and export operations.

Provides classes for parsing KiCad netlist files and extracting
connectivity information.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.core.sheet_properties import SHEET_FILE_KEYS, SHEET_NAME_KEYS
from kicad_tools.sexp import SExp, parse_string

logger = logging.getLogger(__name__)


@dataclass
class ComponentPin:
    """A pin on a component."""

    number: str
    name: str
    pin_type: str = ""


@dataclass
class NetlistComponent:
    """A component in the netlist."""

    reference: str
    value: str
    footprint: str
    lib_id: str
    sheet_path: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    pins: list[ComponentPin] = field(default_factory=list)
    symbol_path: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> NetlistComponent:
        """Parse component from netlist S-expression."""
        ref = ""
        value = ""
        footprint = ""
        lib_id = ""
        sheet_path = ""
        symbol_path = ""
        properties: dict[str, str] = {}

        if ref_node := sexp.find("ref"):
            ref = ref_node.get_string(0) or ""

        if value_node := sexp.find("value"):
            value = value_node.get_string(0) or ""

        if fp_node := sexp.find("footprint"):
            footprint = fp_node.get_string(0) or ""

        if lib_node := sexp.find("libsource"):
            if part := lib_node.find("part"):
                lib_id = part.get_string(0) or ""
            elif lib_node.get_string(1):
                lib_id = lib_node.get_string(1) or ""

        if sheet_node := sexp.find_child("sheetpath"):
            if names := sheet_node.find_child("names"):
                sheet_path = names.get_string(0) or ""
            # The native export has already selected this project's annotation.
            # A multi-unit part can supply several symbol timestamps.
            sheet_stamps = sheet_node.find_child("tstamps")
            unit_stamps = sexp.find_child("tstamps")
            if sheet_stamps is not None and unit_stamps is not None:
                prefix = sheet_stamps.get_string(0) or ""
                units = (unit_stamps.get_string(0) or "").split()
                if prefix.startswith("/") and units:
                    symbol_path = f"{prefix.rstrip('/')}/{units[0]}"

        # Parse properties
        for prop in sexp.find_all("property"):
            prop_name = prop.get_string(0)
            prop_value = prop.get_string(1)
            if prop_name and prop_value:
                properties[prop_name] = prop_value

        return cls(
            reference=ref,
            value=value,
            footprint=footprint,
            lib_id=lib_id,
            sheet_path=sheet_path,
            properties=properties,
            symbol_path=symbol_path,
        )


@dataclass
class NetNode:
    """A connection point in a net."""

    reference: str
    pin: str
    pin_function: str = ""
    pin_type: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> NetNode:
        """Parse node from netlist S-expression."""
        ref = ""
        pin = ""
        pin_function = ""
        pin_type = ""

        # Reference is in (ref "...") child node or as first positional atom
        if ref_node := sexp.find("ref"):
            ref = ref_node.get_string(0) or ""
        else:
            ref = sexp.get_string(0) or ""

        if pin_node := sexp.find("pin"):
            pin = pin_node.get_string(0) or ""

        if func_node := sexp.find("pinfunction"):
            pin_function = func_node.get_string(0) or ""

        if type_node := sexp.find("pintype"):
            pin_type = type_node.get_string(0) or ""

        return cls(
            reference=ref,
            pin=pin,
            pin_function=pin_function,
            pin_type=pin_type,
        )


@dataclass
class NetlistNet:
    """A net (electrical connection) in the netlist."""

    code: int
    name: str
    nodes: list[NetNode] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> NetlistNet:
        """Parse net from netlist S-expression."""
        code = 0
        name = ""
        nodes: list[NetNode] = []

        if code_node := sexp.find("code"):
            code = code_node.get_int(0) or 0

        if name_node := sexp.find("name"):
            name = name_node.get_string(0) or ""

        for node in sexp.find_all("node"):
            nodes.append(NetNode.from_sexp(node))

        return cls(code=code, name=name, nodes=nodes)

    @property
    def connection_count(self) -> int:
        """Number of pins connected to this net."""
        return len(self.nodes)


@dataclass
class SheetInfo:
    """Information about a schematic sheet."""

    number: int
    name: str
    path: str
    title: str = ""
    source: str = ""


@dataclass
class Netlist:
    """Parsed netlist data."""

    source_file: str = ""
    tool: str = ""
    date: str = ""
    sheets: list[SheetInfo] = field(default_factory=list)
    components: list[NetlistComponent] = field(default_factory=list)
    nets: list[NetlistNet] = field(default_factory=list)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Netlist:
        """Parse complete netlist from S-expression."""
        netlist = cls()

        if sexp.tag != "export":
            raise ValueError(f"Expected 'export' root, got '{sexp.tag}'")

        # Parse design info
        if design := sexp.find("design"):
            if source := design.find("source"):
                netlist.source_file = source.get_string(0) or ""
            if tool := design.find("tool"):
                netlist.tool = tool.get_string(0) or ""
            if date := design.find("date"):
                netlist.date = date.get_string(0) or ""

            # Parse sheets
            for sheet in design.find_all("sheet"):
                sheet_num = 0
                sheet_name = ""
                sheet_path = ""
                title = ""
                source_str = ""

                if num := sheet.find("number"):
                    sheet_num = num.get_int(0) or 0
                if name := sheet.find("name"):
                    sheet_name = name.get_string(0) or ""
                if tstamps := sheet.find("tstamps"):
                    sheet_path = tstamps.get_string(0) or ""

                if tb := sheet.find("title_block"):
                    if t := tb.find("title"):
                        title = t.get_string(0) or ""
                    if s := tb.find("source"):
                        source_str = s.get_string(0) or ""

                netlist.sheets.append(
                    SheetInfo(
                        number=sheet_num,
                        name=sheet_name,
                        path=sheet_path,
                        title=title,
                        source=source_str,
                    )
                )

        # Parse components
        if components := sexp.find("components"):
            for comp in components.find_all("comp"):
                netlist.components.append(NetlistComponent.from_sexp(comp))

        # Parse nets
        if nets_node := sexp.find("nets"):
            for net in nets_node.find_all("net"):
                netlist.nets.append(NetlistNet.from_sexp(net))

        return netlist

    @classmethod
    def load(cls, path: str | Path) -> Netlist:
        """Load and parse a netlist file."""
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        sexp = parse_string(text)
        return cls.from_sexp(sexp)

    def get_component(self, reference: str) -> NetlistComponent | None:
        """Get component by reference designator."""
        for comp in self.components:
            if comp.reference == reference:
                return comp
        return None

    def get_net(self, name: str) -> NetlistNet | None:
        """Get net by name."""
        for net in self.nets:
            if net.name == name:
                return net
        return None

    def get_component_nets(self, reference: str) -> list[NetlistNet]:
        """Get all nets connected to a component."""
        result = []
        for net in self.nets:
            for node in net.nodes:
                if node.reference == reference:
                    result.append(net)
                    break
        return result

    def get_net_by_pin(self, reference: str, pin: str) -> NetlistNet | None:
        """Get the net connected to a specific pin."""
        for net in self.nets:
            for node in net.nodes:
                if node.reference == reference and node.pin == pin:
                    return net
        return None

    @property
    def power_nets(self) -> list[NetlistNet]:
        """Get power nets (containing power pins)."""
        power_nets = []
        for net in self.nets:
            for node in net.nodes:
                if "power" in node.pin_type.lower():
                    power_nets.append(net)
                    break
        return power_nets

    def find_single_pin_nets(self) -> list[NetlistNet]:
        """Find nets with only one connection (potential issues).

        Single-pin nets often indicate unconnected pins or incomplete
        connectivity in the schematic.

        Returns:
            List of nets with exactly one connection.
        """
        return [net for net in self.nets if net.connection_count == 1]

    def find_floating_pins(self) -> list[tuple[str, str, str]]:
        """Find pins that appear on nets with only one connection.

        These are pins that are connected to a net but have no other
        connections, suggesting they may be unintentionally floating.

        Returns:
            List of tuples (reference, pin, net_name) for floating pins.
        """
        floating = []
        for net in self.nets:
            if len(net.nodes) == 1:
                node = net.nodes[0]
                floating.append((node.reference, node.pin, net.name))
        return floating

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "source": self.source_file,
            "tool": self.tool,
            "date": self.date,
            "sheets": [
                {
                    "number": s.number,
                    "name": s.name,
                    "path": s.path,
                    "title": s.title,
                    "source": s.source,
                }
                for s in self.sheets
            ],
            "components": [
                {
                    "reference": c.reference,
                    "value": c.value,
                    "footprint": c.footprint,
                    "lib_id": c.lib_id,
                    "sheet_path": c.sheet_path,
                    "properties": c.properties,
                }
                for c in self.components
            ],
            "nets": [
                {
                    "code": n.code,
                    "name": n.name,
                    "connections": n.connection_count,
                    "nodes": [
                        {
                            "reference": node.reference,
                            "pin": node.pin,
                            "pin_function": node.pin_function,
                            "pin_type": node.pin_type,
                        }
                        for node in n.nodes
                    ],
                }
                for n in self.nets
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def summary(self) -> dict:
        """Get netlist summary statistics."""
        # Group components by prefix
        by_prefix: dict[str, int] = defaultdict(int)
        for comp in self.components:
            prefix = "".join(c for c in comp.reference if c.isalpha())
            by_prefix[prefix] += 1

        # Categorize nets
        power_names = {"GND", "PGND", "AGND", "VCC", "VDD", "VBUS"}
        power_nets = [n for n in self.nets if n.name.startswith("+") or n.name in power_names]

        return {
            "source_file": self.source_file,
            "tool": self.tool,
            "date": self.date,
            "sheet_count": len(self.sheets),
            "component_count": len(self.components),
            "components_by_type": dict(by_prefix),
            "net_count": len(self.nets),
            "power_net_count": len(power_nets),
            "signal_net_count": len(self.nets) - len(power_nets),
        }


def _count_hierarchy_sheets(sch_path: Path, visited: set[Path] | None = None) -> int:
    """Count the total number of sheets in a schematic hierarchy.

    Recursively traverses all sub-sheet references and returns the total
    count including the root sheet itself.  Circular references are
    detected and skipped.

    Args:
        sch_path: Path to the root .kicad_sch file.
        visited: Set of already-visited resolved paths (internal).

    Returns:
        Total number of sheets (1 for a flat schematic).
    """
    if visited is None:
        visited = set()

    resolved = sch_path.resolve()
    if resolved in visited:
        return 0
    visited.add(resolved)

    if not sch_path.exists():
        return 0

    count = 1  # This sheet
    sub_filenames = _get_sheet_filenames(sch_path)
    for filename in sub_filenames:
        sub_path = sch_path.parent / filename
        count += _count_hierarchy_sheets(sub_path, visited)
    return count


@dataclass
class _SheetEntry:
    """Parsed sheet entry with filename, sheet name, and pin information.

    ``sheetname`` is the ``(property "Sheetname" ...)`` value — the label
    KiCad shows on the sheet symbol and, crucially, the component it uses
    when building a **hierarchical net name**: a local label ``DBG_LED``
    inside a sheet named ``SubMcu`` is named ``/SubMcu/DBG_LED`` in KiCad's
    exported netlist and on the PCB (issue #5809).  It is *not* the
    filename — two sheet instances can share one ``Sheetfile`` under
    different ``Sheetname``s — so consumers that need KiCad-compatible net
    identities must use this field, not :attr:`filename`.  Empty when the
    sheet carries no ``Sheetname`` property.

    ``uuid`` is the sheet *symbol*'s own ``(uuid ...)``.  It is the segment
    KiCad appends to the parent's instance path, and therefore the key that
    tells the two placements of one shared ``Sheetfile`` apart: a symbol in
    the child file records ``(instances (project ... (path
    "/<root-uuid>/<this-uuid>" (reference "R11") ...)))`` once per placement,
    so per-instance reference designators can only be resolved through it
    (issue #5815).  Empty when the sheet symbol carries no ``(uuid ...)``.
    """

    filename: str
    sheetname: str = ""
    pin_names: list[str] = field(default_factory=list)
    pin_positions: list[tuple[float, float]] = field(default_factory=list)
    uuid: str = ""


def _get_sheet_filenames(sch_path: Path) -> list[str]:
    """Extract sub-sheet filenames from a schematic file.

    Parses the raw S-expression tree to find ``(sheet ...)`` entries and
    extracts the ``Sheetfile`` property from each.

    Args:
        sch_path: Path to the .kicad_sch file.

    Returns:
        List of sub-sheet filenames (relative to the schematic directory).
    """
    return [entry.filename for entry in _get_sheet_entries(sch_path)]


def _get_sheet_entries(sch_path: Path) -> list[_SheetEntry]:
    """Extract sub-sheet entries including pin information.

    Parses the raw S-expression tree to find ``(sheet ...)`` entries and
    extracts both the ``Sheetfile`` property and any ``(pin ...)`` children
    (sheet pins) from each.

    Sheet pins represent the interface between a parent sheet and its child
    sheet.  Each pin has a name that corresponds to a ``hierarchical_label``
    in the child schematic.  The pin's position in the parent sheet is used
    to determine which parent net it connects to.

    Args:
        sch_path: Path to the .kicad_sch file.

    Returns:
        List of :class:`_SheetEntry` objects with filename, sheet name,
        and pin data.
    """
    doc = parse_string(sch_path.read_text(encoding="utf-8"))
    entries: list[_SheetEntry] = []
    for child in doc.children:
        if getattr(child, "name", None) == "sheet" or getattr(child, "tag", None) == "sheet":
            filename = ""
            sheetname = ""
            pin_names: list[str] = []
            pin_positions: list[tuple[float, float]] = []

            # The sheet symbol's own UUID -- the instance-path segment that
            # distinguishes two placements of one shared file (issue #5815).
            uuid_node = child.find("uuid")
            sheet_uuid = (uuid_node.get_string(0) or "") if uuid_node else ""

            # Extract sheet position for computing absolute pin positions
            sheet_x, sheet_y = 0.0, 0.0
            at_node = child.find("at")
            if at_node:
                atoms = at_node.get_atoms()
                if len(atoms) >= 2:
                    sheet_x = round(float(atoms[0]), 2)
                    sheet_y = round(float(atoms[1]), 2)

            # Look for (property "Sheetfile" "filename.kicad_sch") and
            # (property "Sheetname" "SubMcu") -- the latter is what KiCad
            # uses to build hierarchical net names (issue #5809).
            for prop in child.find_all("property"):
                prop_name = prop.get_string(0)
                if prop_name in SHEET_FILE_KEYS:
                    fname = prop.get_string(1)
                    if fname:
                        filename = fname
                elif prop_name in SHEET_NAME_KEYS:
                    sname = prop.get_string(1)
                    if sname:
                        sheetname = sname

            # Extract (pin "name" direction (at x y angle) ...) children.
            # A pin's (at x y) is already in absolute page coordinates, like its parent wire.
            for pin_node in child.find_all("pin"):
                pin_name = pin_node.get_string(0)
                if pin_name:
                    pin_names.append(pin_name)
                    pin_at = pin_node.find("at")
                    if pin_at:
                        pin_atoms = pin_at.get_atoms()
                        if len(pin_atoms) >= 2:
                            px = round(float(pin_atoms[0]), 2)
                            py = round(float(pin_atoms[1]), 2)
                            pin_positions.append((px, py))
                        else:
                            pin_positions.append((sheet_x, sheet_y))
                    else:
                        pin_positions.append((sheet_x, sheet_y))

            if not filename:
                logger.warning(
                    "Sheet %r in %s has no file property (Sheetfile / Sheet file); skipping",
                    sheetname,
                    sch_path,
                )
            else:
                entries.append(
                    _SheetEntry(
                        filename=filename,
                        sheetname=sheetname,
                        pin_names=pin_names,
                        pin_positions=pin_positions,
                        uuid=sheet_uuid,
                    )
                )
    return entries


@dataclass(frozen=True, order=True)
class _NetIdentity:
    """Rank and name select the display name. Scope distinguishes sheet-local aliases."""

    priority: int
    name: str
    scope: str


class _ParentNetAliases:
    """Equivalences between parent nets joined through child-sheet wiring."""

    def __init__(self) -> None:
        self.parents: dict[_NetIdentity, _NetIdentity] = {}

    def resolve(self, name: _NetIdentity) -> _NetIdentity:
        while name in self.parents:
            parent = self.parents[name]
            self.parents[name] = self.parents.get(parent, parent)
            name = self.parents[name]
        return name

    def join(self, first: _NetIdentity, second: _NetIdentity) -> None:
        first, second = sorted((self.resolve(first), self.resolve(second)))
        if first != second:
            self.parents[second] = first


def _collect_hierarchy_components(
    sch_path: Path,
    sheet_path: str,
    visited: set[Path] | None = None,
    *,
    include_symbol_pins: bool = False,
) -> tuple[list[NetlistComponent], dict[str, list]]:
    """Resolve names after collecting bindings from every sheet in the hierarchy."""
    aliases = _ParentNetAliases()
    components, nets = _collect_sheet_nets(
        sch_path,
        sheet_path,
        visited,
        include_symbol_pins=include_symbol_pins,
        net_scope=sheet_path,
        parent_nets_by_port={},
        aliases=aliases,
        multiunit_packages={},
    )
    by_identity: dict[_NetIdentity, list] = defaultdict(list)
    for identity, pins in nets.items():
        by_identity[aliases.resolve(identity)].extend(pins)

    # Preserve short names when unique. For a collision, qualify each local
    # label with its sheet instance path and keep the global name bare.
    counts = Counter(identity.name for identity in by_identity)
    names = {
        identity: identity.name
        for identity in by_identity
        if not identity.scope or counts[identity.name] == 1
    }
    used = set(names.values())
    for identity in by_identity:
        if identity in names:
            continue
        qualified = f"{identity.scope}{identity.name}"
        name = qualified
        # A label can itself contain the generated path.
        suffix = 2
        while name in used:
            name = f"{qualified}#{suffix}"
            suffix += 1
        names[identity] = name
        used.add(name)
    return components, {names[identity]: pins for identity, pins in by_identity.items()}


def _placed_symbol_identity(source: SExp | None, sheet_uuids: tuple[str, ...]) -> tuple[str, str]:
    """Read this placement's annotation (if present) and link from the original symbol."""
    if source is None or not all(sheet_uuids):
        return "", ""
    uuid = source.find_child("uuid")
    symbol_uuid = uuid.get_string(0) if uuid is not None else None
    link = "/" + "/".join((*sheet_uuids[1:], symbol_uuid)) if symbol_uuid else ""
    instances = source.find_child("instances")
    if instances is not None:
        # On duplicate placement paths the native exporter uses the last
        # matching annotation, including on renamed projects and repeated sheets.
        wanted = "/" + "/".join(sheet_uuids)
        for project in reversed(instances.find_children("project")):
            for path in reversed(project.find_children("path")):
                if (path.get_string(0) or "").rstrip("/") == wanted:
                    placed = path.find_child("reference")
                    if placed is not None and (ref := placed.get_string(0)):
                        return ref, link
    return "", link


def _collect_sheet_nets(
    sch_path: Path,
    sheet_path: str,
    visited: set[Path] | None,
    *,
    include_symbol_pins: bool,
    net_scope: str,
    parent_nets_by_port: dict[str, _NetIdentity],
    aliases: _ParentNetAliases,
    multiunit_packages: dict[tuple[str, str], NetlistComponent],
    sheet_uuids: tuple[str, ...] = (),
    ancestors: frozenset[Path] = frozenset(),
) -> tuple[list[NetlistComponent], dict[_NetIdentity, list]]:
    """Recursively collect components and nets from a schematic hierarchy.

    Loads the schematic at *sch_path*, collects its components and
    per-sheet netlist, then recurses into any ``(sheet ...)`` sub-sheets.
    Global labels with matching names across sheets are merged into the
    same net.  Hierarchical labels in child sheets are merged with the
    corresponding parent sheet pin's net using the parent's wiring context.
    Circular references (the same file appearing again in the traversal)
    are detected and skipped with a warning.

    Args:
        sch_path: Absolute path to a ``.kicad_sch`` file.
        sheet_path: Hierarchical sheet path string for this level
            (e.g. ``"/"`` for root).
        visited: Set of already-visited *resolved* paths used for
            circular-reference detection.  Callers should pass ``None``;
            the function creates the set internally.
        include_symbol_pins: Retain library pin definitions and include the
            pins of placed power symbols in the connectivity.
        net_scope: Path of sheet instance names used to qualify local nets.
        parent_nets_by_port: Parent net names at this sheet instance's ports.
        aliases: Parent nets joined by ports anywhere in this traversal.

    Returns:
        A ``(components, net_dict)`` tuple where *components* is a flat
        list of :class:`NetlistComponent` from all sheets and *net_dict*
        maps net identities to lists of :class:`PinRef`-like objects (each
        with ``symbol_ref`` and ``pin`` attributes).
    """
    from kicad_tools.schematic.models import Schematic
    from kicad_tools.schematic.models.netlist_mixin import _component_root

    if visited is None:
        visited = set()

    resolved = sch_path.resolve()
    if resolved in ancestors:
        logger.warning("Circular sheet reference detected, skipping: %s", sch_path)
        return [], {}

    if not sch_path.exists():
        logger.warning("Sub-sheet file not found, skipping: %s", sch_path)
        return [], {}

    # Load single sheet
    sch = Schematic.load(str(sch_path))
    if not sheet_uuids:
        sheet_uuids = (sch.sheet_uuid,)
    identities_by_symbol = [
        _placed_symbol_identity(getattr(sym, "_source_node", None), sheet_uuids)
        for sym in sch.symbols
    ]
    # Old shared sheets with no annotations have only one set of references
    # and net bindings. A second annotated placement has its own references.
    if resolved in visited and sch.symbols and not any(ref for ref, _ in identities_by_symbol):
        return [], {}
    visited.add(resolved)

    # Collect components from this sheet
    components: list[NetlistComponent] = []
    for sym, (ref, symbol_path) in zip(sch.symbols, identities_by_symbol, strict=True):
        # Apply the placement's reference to this private, freshly loaded
        # sheet before extracting nets so pad bindings use the same identity.
        if ref:
            sym.reference = ref
        footprint = ""
        if hasattr(sym, "footprint") and sym.footprint:
            footprint = sym.footprint
        elif hasattr(sym, "properties"):
            footprint = sym.properties.get("Footprint", "")

        lib_id = ""
        if sym.symbol_def and hasattr(sym.symbol_def, "lib_id"):
            lib_id = sym.symbol_def.lib_id

        component = NetlistComponent(
            reference=sym.reference,
            value=sym.value,
            footprint=footprint,
            lib_id=lib_id,
            sheet_path=sheet_path,
            symbol_path=symbol_path,
            pins=[ComponentPin(pin.number, pin.name, pin.pin_type) for pin in sym.symbol_def.pins]
            if include_symbol_pins
            else [],
        )
        if sym.symbol_def.unit_count() > 1 and sym.reference and not sym.reference.endswith("?"):
            # Units share a single physical package and library pin definition.
            # Every unit still participates in the connectivity extraction below.
            package = multiunit_packages.setdefault((sym.reference, lib_id), component)
            if package is not component:
                if not package.footprint:
                    package.footprint = footprint
                continue
        components.append(component)

    ranked_names: dict[tuple, list[tuple[int, str]]] = {}
    connectivity, names_by_root, _ = sch._build_connectivity_graph(
        ranked_names_by_root=ranked_names
    )
    identities = {}
    for candidates in ranked_names.values():
        priority, winner = candidates[0]
        identity = _NetIdentity(priority, winner, "" if priority <= 1 else net_scope)
        identities[winner] = identity
        # Every global or power name is a connection across sheets, even if
        # another label won the flat component's display name.
        for rank, name in candidates:
            if rank <= 1:
                # Both power symbols and global labels share this namespace.
                aliases.join(identity, _NetIdentity(1, name, ""))

    def net_at(pos: tuple[float, float]) -> str | None:
        root = _component_root(connectivity, (round(pos[0], 2), round(pos[1], 2)))
        names = names_by_root.get(root)
        return names[0] if names else None

    # A local label can win the flat sheet's name even though the same wire
    # reaches a differently named hierarchical port. Bind the physical
    # component at the port, then carry its parent name through nested sheets.
    parent_names: dict[str, _NetIdentity] = {}
    for label in sch.hier_labels:
        if label.text in parent_nets_by_port:
            local_net = net_at((label.x, label.y))
            if local_net:
                parent_name = parent_nets_by_port[label.text]
                first_parent = parent_names.setdefault(local_net, parent_name)
                aliases.join(first_parent, parent_name)
                identity = identities[local_net]
                if identity.priority <= 1:
                    aliases.join(identity, parent_name)

    net_dict: dict[_NetIdentity, list] = {}
    try:
        for name, pins in sch.extract_netlist(include_power_symbols=include_symbol_pins).items():
            identity = (
                parent_names.get(name) or identities.get(name) or _NetIdentity(4, name, net_scope)
            )
            net_dict.setdefault(identity, []).extend(pins)
    except Exception:
        logger.warning("Failed to extract netlist from %s, skipping connectivity", sch_path)

    for entry in _get_sheet_entries(sch_path):
        child_bindings = {}
        for pin_name, pin_pos in zip(entry.pin_names, entry.pin_positions, strict=False):
            parent_net = net_at(pin_pos)
            if parent_net:
                child_bindings[pin_name] = parent_names.get(parent_net, identities[parent_net])
        sub_components, sub_nets = _collect_sheet_nets(
            sch_path.parent / entry.filename,
            f"{sheet_path}{entry.filename}/",
            visited,
            include_symbol_pins=include_symbol_pins,
            net_scope=f"{net_scope}{entry.sheetname or Path(entry.filename).stem}/",
            parent_nets_by_port=child_bindings,
            aliases=aliases,
            multiunit_packages=multiunit_packages,
            sheet_uuids=(*sheet_uuids, entry.uuid),
            ancestors=ancestors | {resolved},
        )
        components.extend(sub_components)
        for identity, pins in sub_nets.items():
            net_dict.setdefault(identity, []).extend(pins)

    return components, net_dict


def build_netlist_from_schematic(
    sch_path: str | Path, *, include_symbol_pins: bool = False
) -> Netlist:
    """
    Build a Netlist from a schematic using pure Python extraction.

    This provides a fallback when kicad-cli is unavailable or crashes.
    Uses the Schematic.extract_netlist() method to analyze connectivity
    directly from the schematic file.

    For hierarchical schematics the function recursively loads all
    sub-sheets referenced via ``(sheet ...)`` entries and merges their
    components and nets.  Global labels with matching names across
    sheets are unified into the same net.

    Args:
        sch_path: Path to .kicad_sch file
        include_symbol_pins: Populate each component’s library pin definitions
            across all units and include power symbols and PWR_FLAGs in the nets.

    Returns:
        Parsed Netlist object

    Raises:
        FileNotFoundError: If schematic not found
        ValueError: If schematic parsing fails
    """
    sch_path = Path(sch_path)
    if not sch_path.exists():
        raise FileNotFoundError(f"Schematic not found: {sch_path}")

    # Recursively collect components and nets from all sheets
    components, net_dict = _collect_hierarchy_components(
        sch_path, "/", include_symbol_pins=include_symbol_pins
    )

    # Build nets from merged connectivity data
    nets: list[NetlistNet] = []
    for code, (net_name, pins) in enumerate(net_dict.items(), 1):
        nodes = [NetNode(reference=pin.symbol_ref, pin=pin.pin) for pin in pins]
        nets.append(
            NetlistNet(
                code=code,
                name=net_name,
                nodes=nodes,
            )
        )

    return Netlist(
        source_file=str(sch_path),
        tool="kicad-tools (Python fallback)",
        components=components,
        nets=nets,
    )


def build_pin_to_pad_map(
    sch_path: str | Path,
    pcb,
) -> dict[tuple[str, str], str]:
    """Build a mapping from schematic pin numbers to footprint pad numbers.

    For each component present in both the schematic and the PCB, this
    function compares the symbol's pin definitions (from ``lib_symbols``)
    with the footprint's pad definitions.  When a direct pin-number match
    exists (the common case), an identity entry is recorded.  When pin
    numbers do not match pad numbers, a heuristic mapping based on
    positional order or pin-name-to-pad-name matching is used.

    This mapping is consumed by
    :meth:`~kicad_tools.schema.pcb.PCB.assign_nets_from_netlist` to
    translate ``NetNode.pin`` values produced by the pure-Python fallback
    into correct footprint pad numbers.

    Args:
        sch_path: Path to the root ``.kicad_sch`` file.
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB` object.

    Returns:
        Dict mapping ``(reference, schematic_pin_number)`` to
        ``pad_number``.  Missing entries should be treated as identity
        (pin number == pad number).
    """
    from kicad_tools.lvs.board_lvs import _iter_instance_symbols

    sch_path = Path(sch_path)
    pin_pad_map: dict[tuple[str, str], str] = {}

    # Every placed symbol across the hierarchy, under the reference of its
    # own placement: a sheet file placed twice contributes its symbols once
    # per placement (``R1`` and ``R11``), resolved from each symbol's
    # ``(instances ...)`` block (issues #5815 / #6004).
    all_symbols = [(ref, sym) for ref, sym, _visit in _iter_instance_symbols(sch_path)]

    # Build pad lookup for each PCB footprint: ref -> {pad_number: pad}
    pcb_pad_lookup: dict[str, dict[str, object]] = {}
    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue
        pcb_pad_lookup[fp.reference] = {pad.number: pad for pad in fp.pads}

    # For each schematic symbol, build pin->pad mapping
    for ref, sym in all_symbols:
        if not ref or ref.startswith("#"):
            continue
        if ref not in pcb_pad_lookup:
            continue

        pad_numbers = pcb_pad_lookup[ref]

        for pin in sym.symbol_def.pins:
            pin_num = pin.number
            if pin_num in pad_numbers:
                # Direct match: pin number == pad number (common case)
                pin_pad_map[(ref, pin_num)] = pin_num
            else:
                # Pin number doesn't match any pad -- try name-based matching.
                # Some footprints label pads by function name (e.g. "A1")
                # while the symbol uses sequential numbers.
                for pad_num, pad in pad_numbers.items():
                    pad_name = getattr(pad, "name", None) or ""
                    if pad_name and pad_name == pin.name:
                        pin_pad_map[(ref, pin_num)] = pad_num
                        logger.debug(
                            "Pin-to-pad name match: %s pin %s -> pad %s (name=%s)",
                            ref,
                            pin_num,
                            pad_num,
                            pin.name,
                        )
                        break
                else:
                    # No match by name either; record identity as last resort.
                    # assign_net_to_footprint_pad will log missing_pads.
                    pin_pad_map[(ref, pin_num)] = pin_num

    return pin_pad_map


def export_netlist(
    sch_path: str | Path,
    output_path: str | Path | None = None,
    kicad_cli: str | Path | None = None,
    format: str = "kicadsexpr",
    fallback: bool = True,
) -> Netlist:
    """
    Export netlist from schematic using kicad-cli, with optional Python fallback.

    Args:
        sch_path: Path to .kicad_sch file
        output_path: Where kicad-cli writes the exported netlist file.  When
            omitted, defaults to ``<sch_path dir>/<stem>-netlist.kicad_net``
            -- i.e. *beside the schematic*, not a temp location -- and the
            file is left in place after parsing.  Callers that do not want
            that byproduct must pass an explicit path (e.g. inside a
            ``tempfile.TemporaryDirectory``).  Unused by the pure-Python
            fallback, which writes nothing.
        kicad_cli: Path to kicad-cli (auto-detected if not provided)
        format: Netlist format (kicadsexpr, kicadxml)
        fallback: If True, use pure Python extraction when kicad-cli fails

    Returns:
        Parsed Netlist object

    Raises:
        FileNotFoundError: If kicad-cli not found (and fallback=False)
        RuntimeError: If export fails (and fallback=False)
    """
    sch_path = Path(sch_path)
    if not sch_path.exists():
        raise FileNotFoundError(f"Schematic not found: {sch_path}")

    # Try to find kicad-cli
    if kicad_cli is None:
        cli = find_kicad_cli()
        if cli is None:
            if fallback:
                logger.warning("kicad-cli not found, using pure Python netlist extraction")
                return build_netlist_from_schematic(sch_path)
            raise FileNotFoundError("kicad-cli not found. Install KiCad 8.")
        kicad_cli = cli
    else:
        kicad_cli = Path(kicad_cli)

    if output_path is None:
        output_path = sch_path.parent / f"{sch_path.stem}-netlist.kicad_net"
    else:
        output_path = Path(output_path)

    # Delete existing netlist to ensure fresh export (prevents stale cache issues)
    if output_path.exists():
        output_path.unlink()

    cmd = [
        str(kicad_cli),
        "sch",
        "export",
        "netlist",
        "--format",
        format,
        "--output",
        str(output_path),
        str(sch_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Check for kicad-cli crash (SIGSEGV = exit code 139)
        if result.returncode == 139:
            if fallback:
                logger.warning("kicad-cli crashed (SIGSEGV), using pure Python netlist extraction")
                return build_netlist_from_schematic(sch_path)
            raise RuntimeError(
                "kicad-cli crashed (SIGSEGV). This may be caused by a problematic "
                "symbol in the schematic. Try removing recently added symbols or "
                "exporting the netlist manually from KiCad GUI. "
                "See: https://gitlab.com/kicad/code/kicad/-/issues"
            )

        # Check for other non-zero exit codes
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else f"Exit code {result.returncode}"
            if fallback:
                logger.warning(
                    f"kicad-cli failed ({error_msg}), using pure Python netlist extraction"
                )
                return build_netlist_from_schematic(sch_path)
            raise RuntimeError(f"kicad-cli failed: {error_msg}")

        if not output_path.exists():
            if fallback:
                logger.warning("kicad-cli produced no output, using pure Python netlist extraction")
                return build_netlist_from_schematic(sch_path)
            raise RuntimeError(result.stderr or "Netlist export produced no output")

        netlist = Netlist.load(output_path)

        # Validate completeness: kicad-cli sometimes omits sub-sheets
        # from hierarchical schematics.  Compare the sheet count in the
        # exported netlist against the hierarchy declared in the .kicad_sch
        # files.  When the output is incomplete, fall back to the Python
        # extractor which traverses the full hierarchy.
        if fallback:
            expected_sheets = _count_hierarchy_sheets(sch_path)
            exported_sheets = len(netlist.sheets) if netlist.sheets else 1
            if exported_sheets < expected_sheets:
                logger.warning(
                    "kicad-cli netlist is incomplete: exported %d of %d sheets, "
                    "using pure Python netlist extraction",
                    exported_sheets,
                    expected_sheets,
                )
                return build_netlist_from_schematic(sch_path)

            # Also validate component count: kicad-cli may report all sheets
            # but still omit components from some of them.  Use the Python
            # hierarchy traversal to get the expected count.
            py_components, _ = _collect_hierarchy_components(sch_path, "/")
            # Filter out power symbols (references starting with #) which
            # are not included in the kicad-cli netlist component list.
            expected_count = sum(1 for c in py_components if not c.reference.startswith("#"))
            exported_count = len(netlist.components)
            if exported_count < expected_count:
                logger.warning(
                    "kicad-cli netlist is missing components: exported %d of %d, "
                    "using pure Python netlist extraction",
                    exported_count,
                    expected_count,
                )
                return build_netlist_from_schematic(sch_path)

        return netlist

    except subprocess.CalledProcessError as e:
        if fallback:
            logger.warning(
                f"kicad-cli subprocess error ({e}), using pure Python netlist extraction"
            )
            return build_netlist_from_schematic(sch_path)
        raise RuntimeError(f"Netlist export failed: {e}")
