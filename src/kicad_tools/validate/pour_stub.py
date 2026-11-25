"""Discover simple pad branches and prove their removal on privately refilled copper."""

from __future__ import annotations

import logging
import math
import tempfile
from collections import defaultdict
from copy import copy
from dataclasses import dataclass
from pathlib import Path

from shapely.errors import GEOSException  # type: ignore[import-untyped]
from shapely.geometry import Point, Polygon  # type: ignore[import-untyped]

from kicad_tools.cli import runner
from kicad_tools.exceptions import KiCadToolsError
from kicad_tools.schema.pcb import PCB, Segment, Via
from kicad_tools.sidecars import copy_project_sidecars
from kicad_tools.validate.connectivity import ConnectivityValidator
from kicad_tools.validate.copper_contact import (
    FILL,
    PAD,
    TRACK,
    VIA,
    CopperContactGraph,
    CopperItem,
    build_copper_contact_graph,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PourStub:
    """Objects on the checked board. Each verified branch was tried independently."""

    pad_name: str
    location: tuple[float, float]
    segment: Segment
    via: Via | None

    @property
    def net(self) -> str:
        return self.segment.net_name

    def _signature(self) -> tuple:
        # The first KiCad save can assign UUIDs to legacy boards and reorder items.
        return (
            self.net,
            self.pad_name,
            self.location,
            self.segment.layer,
            self.segment.width,
            tuple(sorted((self.segment.start, self.segment.end))),
            self.via.position if self.via else None,
        )


@dataclass(frozen=True)
class PourStubEvidence:
    """The directly contacted filled zone and surviving pad/via UUIDs on this net."""

    zone_uuid: str
    terminal_uuids: frozenset[str]


class _TrialUnavailable(Exception):
    """The candidate could not be identified, removed or refilled on the trial board."""


def _connection_enabled(pcb: PCB, pad: CopperItem, zone_index: int) -> bool:
    fp_index, pad_index = pad.key
    fp = pcb.footprints[fp_index]
    for override in (fp.pads[pad_index].zone_connect, fp.zone_connect):
        if override is not None and override != -1:
            return override in (1, 2, 3)
    return pcb.zones[zone_index].connect_pads in ("solid", "thermal_reliefs")


def _candidates(pcb: PCB, graph: CopperContactGraph) -> list[PourStub]:
    adjacency: dict[int, list[int]] = defaultdict(list)
    for left, right in graph.bonds:
        if FILL not in (graph.items[left].kind, graph.items[right].kind):
            adjacency[left].append(right)
            adjacency[right].append(left)
    outlines = [
        (i, zone, Polygon(zone.polygon))
        for i, zone in enumerate(pcb.zones)
        if not zone.keepout and len(zone.polygon) >= 3
    ]
    result = []
    for i, item in enumerate(graph.items):
        if item.kind != TRACK or item.key[0] != "segment":
            continue
        segment = pcb.segments[item.key[1]]
        if segment.locked or not segment.net_name:
            continue
        neighbors = [graph.items[j] for j in adjacency[i]]
        pads = [p for p in neighbors if p.kind == PAD]
        vias = [j for j in adjacency[i] if graph.items[j].kind == VIA]
        if len(pads) != 1 or len(vias) > 1 or len(neighbors) != 1 + len(vias):
            continue
        pad = pads[0]
        if not pad.exact or pad.center is None:
            continue
        outer_ends = [p for p in item.endpoints if not pad.geom.covers(Point(p))]
        if len(outer_ends) != 1:
            continue
        via = None
        if vias:
            via = pcb.vias[graph.items[vias[0]].key]
            if len(adjacency[vias[0]]) != 1 or math.dist(via.position, outer_ends[0]) > 1e-6:
                continue
        if not any(
            zone.net_number == segment.net_number
            and segment.layer in (zone.layers or [zone.layer])
            and outline.intersects(pad.geom)
            and _connection_enabled(pcb, pad, z)
            for z, zone, outline in outlines
        ):
            continue
        fp_index, pad_index = pad.key
        fp = pcb.footprints[fp_index]
        result.append(
            PourStub(f"{fp.reference}.{fp.pads[pad_index].number}", pad.center, segment, via)
        )
    return result


def discover_pour_stubs(pcb: PCB) -> list[PourStub]:
    """Return pad-to-free-end or pad-to-via branches. Discovery alone proves no removal."""
    cv = ConnectivityValidator(pcb)
    nets = {z.net_number for z in pcb.zones if z.net_number and not z.keepout}
    return [
        branch
        for net in sorted(nets & {s.net_number for s in pcb.segments})
        for branch in _candidates(pcb, build_copper_contact_graph(cv, net, with_bonds=True))
    ]


@dataclass
class _NetState:
    graph: CopperContactGraph
    # Stable, physical terminals after the first native save. Fill record indices
    # and connected-component labels can both change on a subsequent refill.
    terminals: dict[str, int]
    pads: dict[str, int]


def _states(pcb: PCB) -> dict[str, _NetState]:
    states = {}
    cv = ConnectivityValidator(pcb)
    for net in {z.net_number for z in pcb.zones if z.net_number and not z.keepout}:
        graph = build_copper_contact_graph(cv, net, with_bonds=True)
        terminals = {}
        pads = {}
        for i, item in enumerate(graph.items):
            if item.kind == PAD:
                fi, pi = item.key
                uuid = pcb.footprints[fi].pads[pi].uuid
                pads[uuid] = i
                if not item.exact:
                    raise ValueError("inexact terminal copper")
            elif item.kind == VIA:
                uuid = pcb.vias[item.key].uuid
            else:
                continue
            if not uuid or uuid in terminals:
                raise ValueError("ambiguous saved terminal")
            terminals[uuid] = graph.component[i]
        declared_pads = {
            (fi, pi)
            for fi, fp in enumerate(pcb.footprints)
            for pi, pad in enumerate(fp.pads)
            if pad.net_number == net and any(layer.endswith(".Cu") for layer in pad.layers)
        }
        if not declared_pads <= {graph.items[i].key for i in pads.values()}:
            raise ValueError("incomplete terminal geometry")
        states[pcb.nets[net].name] = _NetState(graph, terminals, pads)
    return states


def _preserves_terminals(
    before: dict[str, _NetState], after: dict[str, _NetState], removed: str
) -> bool:
    if before.keys() != after.keys():
        return False
    for net, old in before.items():
        new = after[net].terminals
        prior = {key: group for key, group in old.terminals.items() if key != removed}
        if prior.keys() != new.keys():
            return False
        destinations: dict[int, int] = {}
        for key, group in prior.items():
            if destinations.setdefault(group, new[key]) != new[key]:
                return False
    return True


def _connected_pour(pcb: PCB, state: _NetState, pad_id: str, layer: str) -> str | None:
    graph = state.graph
    pad_index = state.pads[pad_id]
    group = graph.component[pad_index]
    if any(component != group for component in state.terminals.values()):
        return None
    if len(state.pads) == 1 and not any(
        item.kind == FILL and layer not in item.layers and graph.component[i] == group
        for i, item in enumerate(graph.items)
    ):
        # A local island and its lone pad cannot establish a pour connection
        # to the rest of the board. A connected pour on another layer can.
        return None
    pad = graph.items[pad_index]
    for left, right in graph.bonds:
        if pad_index not in (left, right):
            continue
        item = graph.items[right if left == pad_index else left]
        if item.kind == FILL and layer in item.layers and _connection_enabled(pcb, pad, item.zone):
            return pcb.zones[item.zone].uuid or None
    return None


def _keeps_plane_connections(
    before: PCB, old: _NetState, after: PCB, new: _NetState, pad_id: str
) -> bool:
    """An inner plane can depend on the via even when that plane has no other pads."""
    remaining: dict[tuple, list[tuple[CopperItem, int]]] = defaultdict(list)
    for item, group in zip(new.graph.items, new.graph.component, strict=True):
        if item.kind == FILL:
            remaining[(after.zones[item.zone].uuid, item.layers)].append((item, group))
    for item, group in zip(old.graph.items, old.graph.component, strict=True):
        if item.kind != FILL or group != old.terminals[pad_id]:
            continue
        zone_id = before.zones[item.zone].uuid
        if not zone_id:
            return False
        # A refill may split or merge polygons. Match overlapping copper on the
        # same zone and layer, never assume the nth saved island is still the nth.
        groups = {
            g
            for fill, g in remaining[zone_id, item.layers]
            if fill.geom.intersection(item.geom).area > 0
        }
        if groups != {new.terminals[pad_id]}:
            return False
    return True


def _refill(path: Path, cli: Path) -> bool:
    # A successful DRC report alone does not prove the new fill was saved.
    # Clear stale solids only in the disposable PCB, then require the native save.
    runner._strip_stale_zone_fills(path)
    unfilled, mtime = path.read_bytes(), path.stat().st_mtime_ns
    result = runner.run_refill_zones(path, cli, require_save=True)
    return result.success and not runner._board_unchanged(path, unfilled, mtime)


def _verify_branch(
    candidate: PourStub,
    baseline: PCB,
    previous: dict[str, _NetState],
    path: Path,
    cli: Path,
) -> PourStubEvidence | None:
    prior = previous[candidate.net]
    pad_ids = [
        key
        for key, i in prior.pads.items()
        if prior.graph.items[i].center == candidate.location
        and candidate.segment.layer in prior.graph.items[i].layers
    ]
    if len(pad_ids) != 1:
        raise _TrialUnavailable("pad identity is ambiguous")
    (pad_id,) = pad_ids
    layer = candidate.segment.layer
    if not _connected_pour(baseline, prior, pad_id, layer):
        return None
    trial = PCB.load(path)
    if trial.remove_segments([candidate.segment]) != 1:
        raise _TrialUnavailable("candidate track could not be removed")
    if candidate.via and trial.remove_vias([candidate.via]) != 1:
        raise _TrialUnavailable("candidate via could not be removed")
    trial.save(path)
    if not _refill(path, cli):
        raise _TrialUnavailable("native KiCad did not save the trial refill")
    after = PCB.load(path)
    states = _states(after)
    if not _preserves_terminals(previous, states, candidate.via.uuid if candidate.via else ""):
        return None
    state = states[candidate.net]
    zone_uuid = _connected_pour(after, state, pad_id, layer)
    if zone_uuid and _keeps_plane_connections(baseline, prior, after, state, pad_id):
        return PourStubEvidence(zone_uuid, frozenset(state.terminals))
    return None


def evaluate_pour_stubs(pcb: PCB) -> list[tuple[PourStub, PourStubEvidence]] | None:
    """Return verified branches, or None if no candidate could be evaluated."""
    source = pcb.path
    if source is None or not pcb.zones or not pcb.segments:
        return None
    try:
        branches = discover_pour_stubs(pcb)
        if not branches or (cli := runner.find_kicad_cli()) is None:
            return None
        with tempfile.TemporaryDirectory(prefix="kct-pour-stub-") as scratch:
            path = Path(scratch) / source.name
            original = source.read_bytes()
            # Include pending in-memory edits. PCB.save changes its receiver's
            # path, so save a shallow copy without redirecting the caller.
            copy(pcb).save(path)
            rules = {p.name: p.read_bytes() for p in copy_project_sidecars(source, path)}
            if not _refill(path, cli):
                return None
            frozen = path.read_bytes()
            baseline = PCB.load(path)
            previous = _states(baseline)
            saved = {b._signature(): b for b in discover_pour_stubs(baseline)}
            results = []
            exercised = False
            for branch in branches:
                candidate = saved.get(branch._signature())
                if candidate is None:
                    continue
                with tempfile.TemporaryDirectory(dir=scratch) as trial_dir:
                    trial = Path(trial_dir) / path.name
                    trial.write_bytes(frozen)
                    for name, data in rules.items():
                        (trial.parent / name).write_bytes(data)
                    try:
                        evidence = _verify_branch(candidate, baseline, previous, trial, cli)
                    except _TrialUnavailable as exc:
                        logger.debug("Could not evaluate pour stub at %s: %s", branch.pad_name, exc)
                        continue
                    exercised = True
                    if evidence is not None:
                        results.append((branch, evidence))
            return results if exercised and source.read_bytes() == original else None
    except (OSError, KeyError, ValueError, GEOSException, KiCadToolsError) as exc:
        logger.debug("Could not verify pour-connected pad branches in %s: %s", source, exc)
        return None


def verified_pour_stubs(pcb: PCB) -> list[tuple[PourStub, PourStubEvidence]]:
    """List branches with positive removal evidence for callers that consume them."""
    return evaluate_pour_stubs(pcb) or []
