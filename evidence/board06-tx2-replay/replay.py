"""Replay Board 06 TX2 relief on the captured iteration-two copper and grid."""

import argparse
import gzip
import json
import random
import shutil
import time
from collections import Counter
from pathlib import Path

import numpy as np

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.core.types import CopperLayer
from kicad_tools.router.access_ripup import RipupCycleGuard
from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder
from kicad_tools.router.io import load_pcb_for_routing, merge_routes_into_pcb
from kicad_tools.router.plane_via_sites import reserve_plane_via_sites
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting, ZoneRules
from kicad_tools.schema.pcb import PCB

HERE = Path(__file__).resolve().parent
BOARD = HERE / "input.kicad_pcb"
PAIR = ("USB3_TX2+", "USB3_TX2-")
POWER = ["GND", "VBUS_USB", "+3V3", "+1V8", "+1V2"]
TYPES = {c.__name__: c for c in (DesignRules, NetClassRouting, ZoneRules, Route, Segment, Via)}


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "layer" in value:
        return CopperLayer(value["layer"])
    if "tuple" in value:
        return tuple(decode(item) for item in value["tuple"])
    if "pairs" in value:
        return {decode(k): decode(v) for k, v in value["pairs"]}
    cls = TYPES[value["type"]]
    return cls(**{k: decode(v) for k, v in value["values"].items()})


def restore(backend):
    payload = json.loads(gzip.decompress((HERE / "checkpoint.json.gz").read_bytes()))
    snapshot = {k: decode(v) for k, v in payload.items()}
    random.seed(42)
    router, ids = load_pcb_for_routing(
        str(BOARD),
        rules=snapshot["rules"],
        skip_nets=POWER,
        force_python=True,
        max_search_iterations=12_000_000,
        per_net_iterations=1_000_000,
    )
    reserve_plane_via_sites(router.grid, ["VBUS_USB"])
    router.net_class_map.update(snapshot["classes"])
    committed = snapshot["routes"]
    for records in committed.values():
        for route in records:
            assert router._mark_route(route), route.net_name
            router.grid.mark_route_usage(route)
            router.routes.append(route)
    with np.load(HERE / "grid.npz", allow_pickle=False) as cells:
        for field, recorded in cells.items():
            current = getattr(router.grid, field, None)
            if current is None or current.shape != recorded.shape:
                setattr(router.grid, field, recorded.copy())
            else:
                current[...] = recorded
    if backend == "cpp":
        mirror = CppGrid.from_routing_grid(router.grid)
        router.router = CppPathfinder(
            mirror,
            router.rules,
            diagonal_routing=True,
            net_class_map=router.net_class_map,
            max_search_iterations=12_000_000,
            per_net_iterations=1_000_000,
        )
        for field, data in (
            ("usage_count", router.grid._usage_count),
            ("history_cost", router.grid._history_cost),
        ):
            for layer, y, x in zip(*np.nonzero(data), strict=True):
                cell = mirror._impl.at(int(x), int(y), int(layer))
                setattr(cell, field, data[layer, y, x].item())
    router.router.set_net_name_to_id({name: number for number, name in router.net_names.items()})
    return router, ids, committed, snapshot["cost"]


def save_state(router, output, *, all_nets=False):
    output.write_text(merge_routes_into_pcb(BOARD.read_text(), router.to_sexp(skip_cleanup=True)))
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(HERE / ("routed" + suffix), output.with_suffix(suffix))
    pcb = PCB.load(output)
    tracked = Counter()
    for s in pcb.segments:
        tracked[
            ("segment", pcb.nets[s.net_number].name, s.layer, s.width, *sorted((s.start, s.end)))
        ] += 1
    for v in pcb.vias:
        tracked[
            ("via", pcb.nets[v.net_number].name, tuple(v.layers), v.position, v.size, v.drill)
        ] += 1
    nets = NetStatusAnalyzer(output, strict=True).analyze().nets
    return tracked, {s.net_name: s.island_count for s in nets if all_nets or s.net_name in PAIR}


def run(backend, order, dest):
    dest.mkdir(parents=True, exist_ok=False)
    router, ids, committed, cost = restore(backend)
    pads = {k: [router.pads[p] for p in pins] for k, pins in router.nets.items() if k}
    previous, prior_islands = save_state(router, dest / "before.kicad_pcb", all_nets=True)
    outcomes = []
    searches = []
    for index, target in enumerate(order, start=1):
        net = ids[target]
        router._update_router_via_foreign_context(net)
        router._update_router_segment_foreign_context(net)
        pathfinder = router.router
        original = pathfinder.route
        entry_nets = {n for n, routes in committed.items() if routes}
        displaced = set()

        def observed(start, end, *args, **kwargs):
            began = time.monotonic()
            result = original(start, end, *args, **kwargs)
            event = {
                "outer": target,
                "from": [start.ref, start.pin],
                "to": [end.ref, end.pin],
                "protected_partner_drills": list(getattr(pathfinder, "relief_partner_drills", ())),
                "candidate_drills": [(v.x, v.y, v.drill) for v in result.vias] if result else [],
                "exit": "reached" if result else router._relief_search_exit_reason(),
                "search": router.net_names[start.net],
                "relief": getattr(
                    pathfinder, "relief_mode", getattr(pathfinder, "_relief_mode", None)
                ),
                "seconds": round(time.monotonic() - began, 3),
                "timeout": kwargs.get("per_net_timeout"),
                "reached": result is not None,
                "segments": len(result.segments) if result else 0,
                "vias": len(result.vias) if result else 0,
                "deadline_hit": getattr(pathfinder, "last_route_deadline_hit", None),
                "deadline_iterations": getattr(pathfinder, "last_route_deadline_iterations", None),
                "failure_info": pathfinder.get_last_failure_info(),
            }
            searches.append(event)
            print(json.dumps(event), flush=True)
            return result

        pathfinder.route = observed
        try:
            worker = NegotiatedRouter(router.grid, pathfinder, router.rules, router.net_class_map)
            rip = worker.rip_up_nets

            def observed_rip(nets, net_routes, routes):
                displaced.update(n for n in nets if n in entry_nets and net_routes.get(n))
                return rip(nets, net_routes, routes)

            worker.rip_up_nets = observed_rip
            accepted = router._relief_rescue(
                net,
                worker,
                committed,
                pads,
                cost,
                None,
                print,
                lambda: "captured TX2 replay",
                deterministic_rescue=True,
                guard=RipupCycleGuard(),
            )
        finally:
            pathfinder.route = original
        copper, islands = save_state(router, dest / f"after-{index}.kicad_pcb", all_nets=True)
        added, removed = sum((copper - previous).values()), sum((previous - copper).values())
        victims = {}
        for victim in sorted(displaced - {net}):
            name = router.net_names[victim]
            before, after = prior_islands[name], islands[name]
            disposition = "re-landed" if accepted else "rolled back"
            victims[name] = {
                "before_islands": before,
                "after_islands": after,
                "disposition": "lost" if after > before else disposition,
            }
        outcome = {
            "outer": target,
            "accepted": accepted,
            "islands": {name: islands[name] for name in PAIR},
            "added": added,
            "removed": removed,
            "displaced_victims": victims,
        }
        if not accepted:
            assert copper == previous and islands == prior_islands, outcome
        assert all(v["disposition"] != "lost" for v in victims.values()), outcome
        previous, prior_islands = copper, islands
        outcomes.append(outcome)
        print(json.dumps(outcome), flush=True)
    (dest / "results.json").write_text(
        json.dumps({"outcomes": outcomes, "searches": searches}, indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=("cpp", "python"))
    parser.add_argument(
        "out", type=Path, help="new output directory for the two saved boards and search log"
    )
    parser.add_argument("--negative-first", action="store_true")
    args = parser.parse_args()
    run(args.backend, tuple(reversed(PAIR)) if args.negative_first else PAIR, args.out)
