"""Enumerate the BGA-side vias in the captured TX2 Python search, with the other leg absent."""

import argparse
import json
import math
from itertools import product
from pathlib import Path

from replay import HERE, PAIR, restore, save_state

from kicad_tools.router.core import RELIEF_DIFFPAIR_GOAL_BIAS_WEIGHT
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via


def terminal_sites(name, out):
    excluded = set()
    sites = []
    for index in range(16):
        router, ids, _, cost = restore("python")
        net = ids[name]
        (terminal,) = (router.pads[key] for key in router.nets[net] if router.pads[key].ref == "U2")
        check_via = router.router._check_via_placement_cached

        def remaining_site(x, y, *args, **kwargs):
            return (x, y) not in excluded and check_via(x, y, *args, **kwargs)

        router.router._check_via_placement_cached = remaining_site
        with router.trial_gene_overrides({net: RELIEF_DIFFPAIR_GOAL_BIAS_WEIGHT}):
            routes = router._route_net_negotiated(
                net, cost, per_net_timeout=20, reverse_search=True
            )
        if not routes:
            return {"sites": sites, "exit": router._relief_search_exit_reason()}

        router.routes.extend(route for route in routes if route not in router.routes)
        _, islands = save_state(router, out / f"{index}.kicad_pcb")
        if islands[name] != 1:
            raise ValueError(f"{name} trial {index} has {islands[name]} pad islands")
        vias = [
            v for r in routes for v in r.vias if math.hypot(v.x - terminal.x, v.y - terminal.y) < 2
        ]
        if not vias:
            return {"sites": sites, "exit": "connected without a via within 2 mm of U2"}
        via = min(vias, key=lambda v: math.hypot(v.x - terminal.x, v.y - terminal.y))
        cell = router.grid.world_to_grid(via.x, via.y)
        if cell in excluded:
            raise ValueError(f"{name} reused excluded site {cell}")
        excluded.add(cell)
        sites.append({"x": via.x, "y": via.y, "drill": via.drill, "saved_islands": islands})
    return {"sites": sites, "exit": "16-site enumeration limit"}


def write_physical_controls(out):
    """Apply the recorded pair to the same neighbors and translate one drilled land by one cell."""
    router, ids, _, _ = restore("python")
    records = json.loads((HERE / "partner-candidate.json").read_text())
    for name, record in records.items():
        net = ids[name]
        segments = [
            Segment(x1, y1, x2, y2, width, Layer.from_kicad_name(layer), net=net)
            for x1, y1, x2, y2, layer, width in record["segments"]
        ]
        vias = [
            Via(x, y, drill, size, (Layer.from_kicad_name(a), Layer.from_kicad_name(b)), net=net)
            for x, y, a, b, size, drill in record["vias"]
        ]
        router.routes.append(Route(net=net, net_name=name, segments=segments, vias=vias))
    _, candidate = save_state(router, out / "paired-candidate.kicad_pcb")

    # The native hole_to_hole witness identifies this TX2- drill. Retain
    # every approach and join its old endpoint to the new center on each
    # occupied layer, with the whole short segment inside the new land.
    (leg,) = (rt for rt in router.routes if rt.net == ids[PAIR[1]])
    old = (151.75, 69.70)
    (via,) = (v for v in leg.vias if (v.x, v.y) == old)
    step = router.grid.resolution
    contacts = {(s.layer, s.width) for s in leg.segments if old in (s.start, s.end)}
    assert len(contacts) == 2
    via.y = round(via.y - step, 6)
    for layer, width in sorted(contacts, key=lambda contact: contact[0].value):
        assert step + width / 2 < via.diameter / 2
        leg.segments.append(Segment(*old, via.x, via.y, width, layer, net=leg.net))
    _, translated = save_state(router, out / "one-cell-control.kicad_pcb")
    return {"paired_candidate": candidate, "one_cell_control": translated}


def stitch_reservation():
    """Trace the first F.Cu cell rejecting the one-cell control back to its writer."""
    from unittest.mock import patch

    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.pathfinder import search_radius_cells

    cell = (151.80, 69.20)
    owners = []
    stamp = RoutingGrid._apply_stitch_via_halo

    def trace(grid, pad, *args, **kwargs):
        gx, gy = grid.world_to_grid(*cell)
        before = bool(grid._blocked[0, gy, gx])
        stamp(grid, pad, *args, **kwargs)
        if not before and grid._blocked[0, gy, gx]:
            owners.append([pad.ref, pad.pin, pad.net_name])

    with patch.object(RoutingGrid, "_apply_stitch_via_halo", trace):
        router, ids, _, _ = restore("python")
    net = ids[PAIR[1]]
    router._update_router_via_foreign_context(net)
    router._update_router_segment_foreign_context(net)
    via = router.net_class_map[PAIR[1]].via_size
    radius = search_radius_cells(via / 2, router.rules.via_clearance, router.grid.resolution)

    def permitted(x, y):
        return router.router._check_via_placement_cached(
            *router.grid.world_to_grid(x, y), net, allow_sharing=True, radius=radius
        )

    gx, gy = router.grid.world_to_grid(*cell)
    return {
        "blocked_cell_mm": cell,
        "stitch_halo_writers": owners,
        "is_policy_cell": router.grid._pad_cell_key(0, gy, gx) in router.grid._pad_policy_cells,
        "original_site_permitted": permitted(151.75, 69.70),
        "translated_site_permitted": permitted(151.75, 69.65),
    }


def run(out):
    out.mkdir(parents=True, exist_ok=False)
    terminal = {}
    for name in PAIR:
        target = out / name
        target.mkdir()
        terminal[name] = terminal_sites(name, target)
    settings = json.loads((HERE / "routed.kicad_pro").read_text())["board"]["design_settings"]
    minimum = settings["rules"]["min_hole_to_hole"]
    pairs = []
    for a, b in product(*(terminal[name]["sites"] for name in PAIR)):
        gap = math.hypot(a["x"] - b["x"], a["y"] - b["y"]) - (a["drill"] + b["drill"]) / 2
        pairs.append(
            {
                "positive": [a["x"], a["y"]],
                "negative": [b["x"], b["y"]],
                "hole_gap_mm": gap,
                "meets_project_minimum": gap + 1e-6 >= minimum,
            }
        )
    result = {
        "project_minimum_mm": minimum,
        "legs": terminal,
        "pairs": pairs,
        "control_islands": write_physical_controls(out),
        "translated_site": stitch_reservation(),
    }
    (out / "terminal-sites.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "out", type=Path, help="new directory for candidate boards and measurements"
    )
    print(json.dumps(run(parser.parse_args().out), indent=2))
