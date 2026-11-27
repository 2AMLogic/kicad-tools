"""Verify an inner-layer corridor against two natively refilled boards."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString  # type: ignore[import-untyped]

from kicad_tools.router.link_router import LinkRoute, LinkRouteRules, PlaneCorridor
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.connectivity import ConnectivityValidator
from kicad_tools.validate.copper_contact import FILL, PAD, TRACK, VIA, build_copper_contact_graph


class PlaneCheckError(ValueError):
    """The candidate loses an island, a terminal connection or a clearance floor."""


@dataclass
class _PlaneNet:
    number: int
    # Stable native UUIDs survive a save that reorders or renumbers board items.
    islands: dict[str, list[tuple[Any, int]]] = field(default_factory=dict)
    terminals: dict[str, int] = field(default_factory=dict)


def _plane_nets(pcb: PCB, layer: str) -> dict[str, _PlaneNet]:
    nets = {z.net_number for z in pcb.zones if layer in (z.layers or [z.layer]) and not z.keepout}
    validator = ConnectivityValidator(pcb)
    states = {}
    for net in nets:
        state = _PlaneNet(net)
        graph = build_copper_contact_graph(validator, net)
        for item, group in zip(graph.items, graph.component, strict=True):
            if item.kind == FILL and layer in item.layers:
                zone = pcb.zones[item.zone]
                state.islands.setdefault(zone.uuid, []).append((item.geom, group))
            elif item.kind == PAD:
                fi, pi = item.key
                state.terminals[pcb.footprints[fi].pads[pi].uuid] = group
            elif item.kind == VIA:
                state.terminals[pcb.vias[item.key].uuid] = group
            elif item.kind == TRACK:
                kind, index = item.key
                track = pcb.segments[index] if kind == "segment" else pcb.arcs[index]
                state.terminals[track.uuid] = group
        states[pcb.nets[net].name if net else ""] = state
    return states


def _preserve_islands(
    old: _PlaneNet,
    new: _PlaneNet,
    layer: str,
    net: str,
    retained: dict[str, list[Any]],
) -> None:
    # A previous component must still reach all its islands and terminals,
    # even if another previously disconnected component joined it on refill.
    destinations: dict[int, set[int | None]] = defaultdict(set)
    for key, group in old.terminals.items():
        destinations[group].add(new.terminals.get(key))
    for zone, islands in old.islands.items():
        remaining = new.islands.get(zone, [])
        for shape, group in islands:
            overlaps = [g for geom, g in remaining if shape.intersection(geom).area > 0]
            destinations[group].update(overlaps)
            severed = (
                sum(piece.intersection(shape).area > 0 for piece in retained.get(zone, ())) > 1
            )
            if (
                severed
                or len(overlaps) != 1
                or len(destinations[group]) != 1
                or None in destinations[group]
            ):
                point = shape.representative_point()
                raise PlaneCheckError(
                    f"stranded {net or 'netless'} island on {layer} in zone {zone} "
                    f"near ({point.x:g}, {point.y:g}) board-relative mm"
                )
        if len(remaining) > len(islands):
            raise PlaneCheckError(f"new stranded {net} island on {layer} in zone {zone}")


def check_plane_refill(
    before: PCB,
    after: PCB,
    route: LinkRoute,
    rules: LinkRouteRules,
    corridor: PlaneCorridor,
    with_islands: PCB | None,
) -> float | None:
    """Check saved track extents, island membership and edge-to-edge gap in mm."""
    layer = corridor.layer
    ox, oy = after.board_origin
    # Inspect the serialized copper: both the width and the absolute endpoints
    # can move on the writer's 0.01 mm grid.
    existing = {s.uuid for s in before.segments}
    tracks = []
    for segment in after.segments:
        if segment.uuid in existing or segment.layer != layer:
            continue
        if after.nets[segment.net_number].name != route.net_name:
            raise PlaneCheckError(f"new track on {layer} lost its net on refill")
        half = segment.width / 2
        if not all(
            corridor.contains(x + ox, y + oy, half) for x, y in (segment.start, segment.end)
        ):
            raise PlaneCheckError(f"written track leaves the authorized corridor on {layer}")
        tracks.append((LineString([segment.start, segment.end]), half))

    if not tracks and any(segment[-1] == layer for segment in route.segments):
        raise PlaneCheckError(f"could not identify the new {layer} tracks after refill")

    retained: dict[str, list[Any]] = defaultdict(list)
    if with_islands is not None:
        for zone in with_islands.zones:
            for i, points in enumerate(zone.filled_polygons):
                if zone.filled_polygon_layer(i) == layer:
                    shape = ConnectivityValidator._fill_solid_region(points)
                    if shape is not None:
                        retained[zone.uuid].append(shape)
    previous, current = _plane_nets(before, layer), _plane_nets(after, layer)
    if previous.keys() != current.keys():
        raise PlaneCheckError(f"plane nets on {layer} changed during refill")
    minimum = None
    for net, old in previous.items():
        new = current[net]
        _preserve_islands(old, new, layer, net, retained)
        if net == route.net_name or not tracks:
            continue
        for islands in new.islands.values():
            for shape, _group in islands:
                for part, floor in rules.clearance_parts(route.net_name, old.number, layer, shape):
                    gap = max(0.0, min(line.distance(part) - half for line, half in tracks))
                    if gap + 1e-6 < floor:
                        raise PlaneCheckError(
                            f"{net or 'netless'} plane gap on {layer} is {gap:g} mm, "
                            f"requires {floor:g} mm"
                        )
                    minimum = gap if minimum is None else min(minimum, gap)
    return round(minimum, 6) if minimum is not None else None
