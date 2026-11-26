"""Explain native dangling vias that belong to verified reinforcement wires."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kicad_tools.core.geometry import point_to_segment_distance
from kicad_tools.exceptions import KiCadToolsError
from kicad_tools.pcb.reinforcement_sidecar import adopt_wire_records, load_wires_if_present
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    discover_current_paths_sidecar,
    load_current_path_specs,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.reinforcement_incomplete import (
    ReinforcementIncompleteRule,
    WiredLeg,
)

from .violation import DRCViolation, WireEnd


def _is_through_hole(via: Any) -> bool:
    # A buttress wire passes through the board: blind/buried/micro vias (or any
    # via not spanning both outer layers) are not wire anchors.
    return via.via_type is None and "F.Cu" in via.layers and "B.Cu" in via.layers


def _has_wire_side_copper(pcb: PCB, leg: WiredLeg) -> bool:
    # Completeness uses the widest segment to locate candidate holes. Check
    # actual copper contact using the local track width at each accepted hole.
    net = pcb.get_net_by_name(leg.net)
    return net is not None and all(
        (a.via is None or _is_through_hole(a.via))
        and any(
            s.net_number == net.number
            and s.layer == leg.layer
            and point_to_segment_distance(*a.pos, *s.start, *s.end) <= a.radius + s.width / 2
            for s in pcb.segments
        )
        for a in leg.anchors
    )


def _wire_candidates(
    pcb: PCB,
    specs: Sequence[CurrentPathSpec],
    wires: dict[int, dict[str, Any]],
    rule_wires: dict[int, dict[str, Any]] | None = None,
) -> list[list[WireEnd]]:
    # ``wires`` drive anchor-claim ambiguity; ``rule_wires`` (default: all of
    # them) are the records the completeness rule may evaluate.
    ox, oy = pcb.board_origin or (0.0, 0.0)

    def recorded(pos: tuple[float, float]) -> dict[str, float]:
        return {"x": round(pos[0] + ox, 4), "y": round(pos[1] + oy, 4)}

    rule = ReinforcementIncompleteRule(specs=specs, wires=list((rule_wires or wires).values()))
    candidates: list[list[WireEnd]] = []
    adopted = None
    for leg in rule.fully_wired_legs(pcb):
        if not _has_wire_side_copper(pcb, leg):
            continue
        anchors = [recorded(a.pos) for a in leg.anchors]
        ends = [recorded(leg.points[0]), recorded(leg.points[-1])]
        # Either recorded endpoint can claim a hole. A second claim makes the
        # wire ambiguous even when that record's *other* endpoint is stale.
        matches = [
            (index, wire)
            for index, wire in wires.items()
            if wire.get("layer") == leg.layer
            and wire.get("strategy", "wire") == "wire"
            and isinstance(wire.get("anchors"), list)
            and any(point in anchors for point in wire["anchors"])
        ]
        if len(matches) != 1:
            continue
        index, wire = matches[0]
        if wire.get("wire_gauge_awg") != leg.gauge or wire.get("status") not in ("full", "adopted"):
            continue
        reverse = wire.get("anchors") == anchors[::-1]
        if not reverse and wire.get("anchors") != anchors:
            continue
        leg_ends = [wire.get("leg_start"), wire.get("leg_end")]
        if wire.get("status") == "adopted":
            # Adopted leg ends describe the mask strip, not the track ends.
            if adopted is None:
                adopted = adopt_wire_records(pcb, leg.net).records
            if not any(
                w["layer"] == leg.layer
                and w["anchors"] == wire["anchors"]
                and [w["leg_start"], w["leg_end"]] == leg_ends
                for w in adopted
            ):
                continue
        elif leg_ends != (ends[::-1] if reverse else ends):
            continue
        ordered = leg.anchors[::-1] if reverse else leg.anchors
        candidates.append(
            [
                WireEnd(
                    index,
                    leg.net,
                    leg.layer,
                    end,
                    a.pos[0] + ox,
                    a.pos[1] + oy,
                    a.via.uuid,
                    leg.gauge,
                    a.via.drill,
                )
                for end, a in zip(("start", "end"), ordered, strict=True)
                if a.via is not None
            ]
        )
    return candidates


_BAD_RECORD = (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError)


def _record_usable(pcb: PCB, specs: Sequence[CurrentPathSpec], wire: dict[str, Any]) -> bool:
    try:
        list(ReinforcementIncompleteRule(specs=specs, wires=[wire]).fully_wired_legs(pcb))
    except _BAD_RECORD:
        return False
    return True


def explain_wire_ends(board: Path, findings: list[DRCViolation]) -> None:
    """Attach evidence to uniquely identified native vias on complete recorded wires."""
    wires = load_wires_if_present(board)
    if not wires:
        return
    paths_file = discover_current_paths_sidecar(board)
    if paths_file is None:
        return
    try:
        specs = load_current_path_specs(paths_file)
        if not any(s.reinforcement_eligible for s in specs):
            return
        pcb = PCB.load(board)
    except (KiCadToolsError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return

    dangling = [v for v in findings if v.type_str == "via_dangling" and len(v.item_uuids) == 1]
    candidates: list[list[WireEnd]] = []
    for net in {name for v in dangling for name in v.nets}:
        records = {i: w for i, w in enumerate(wires) if isinstance(w, dict) and w.get("net") == net}
        if not records:
            continue
        net_specs = [s for s in specs if s.net_name == net]
        try:
            candidates.extend(_wire_candidates(pcb, net_specs, records))
        except _BAD_RECORD:
            # Isolate unusable record data to its own wire; the bad record still
            # counts for anchor-claim ambiguity but is never evaluated.
            valid = {i: w for i, w in records.items() if _record_usable(pcb, net_specs, w)}
            try:
                candidates.extend(_wire_candidates(pcb, net_specs, records, valid))
            except _BAD_RECORD:
                continue

    # Ambiguity at either end invalidates both ends of the affected wire.
    counts = Counter(v.uuid for v in pcb.vias)
    uses = Counter(end.via_uuid for pair in candidates for end in pair)
    by_uuid = {
        end.via_uuid: end
        for pair in candidates
        if all(e.via_uuid and counts[e.via_uuid] == uses[e.via_uuid] == 1 for e in pair)
        for end in pair
    }
    for v in dangling:
        end = by_uuid.get(v.item_uuids[0])
        pos = v.primary_location
        if (
            end
            and v.nets == [end.net]
            and pos
            and (round(pos.x_mm, 6), round(pos.y_mm, 6)) == (round(end.x_mm, 6), round(end.y_mm, 6))
        ):
            v.wire_end = end
