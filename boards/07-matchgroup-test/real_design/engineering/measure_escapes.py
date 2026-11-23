"""Measure front-layer SDRAM data/control exposure on the completed Board 07 routing."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from itertools import pairwise, product
from pathlib import Path

from shapely.geometry import LineString, MultiLineString, MultiPoint
from shapely.ops import linemerge

from kicad_tools.physics.crosstalk import CrosstalkAnalyzer
from kicad_tools.physics.stackup import Stackup
from kicad_tools.schema.pcb import PCB

ROOT = Path(__file__).resolve().parents[1]
SHARED_LAYER = "F.Cu"
SEPARATION_MM = 5.0
RISE_TIMES_NS = (1.0, 0.1, 0.05)


def straight_runs(lines: MultiLineString) -> list[LineString]:
    """Join contiguous collinear track fragments before estimating coupled length."""
    merged = linemerge(lines)
    paths = [merged] if isinstance(merged, LineString) else merged.geoms
    return [
        LineString(sorted((start, end)))
        for path in paths
        for start, end in pairwise(path.simplify(1e-8).coords)
    ]


def parallel_overlap(first: LineString, second: LineString) -> float:
    """Length of common projection of two parallel track centerlines."""
    (ax, ay), (bx, by) = first.coords
    (cx, cy), (dx, dy) = second.coords
    ux, uy = bx - ax, by - ay
    vx, vy = dx - cx, dy - cy
    length = first.length
    if not length or not second.length or abs(ux * vy - uy * vx) > 1e-7 * length * second.length:
        return 0.0
    start, end = sorted(((cx - ax) * ux + (cy - ay) * uy, (dx - ax) * ux + (dy - ay) * uy))
    return max(0.0, min(length, end / length) - max(0.0, start / length))


def measure(pcb: Path, net_map: Path) -> tuple[dict, dict]:
    board = PCB.load(pcb)
    rules = json.loads(net_map.read_bytes())
    groups = {
        net: rule["length_match_group"]
        for net, rule in rules.items()
        if rule.get("length_match_group")
    }
    data = sorted(net for net, group in groups.items() if group in {"SDRAM_BYTE0", "SDRAM_BYTE1"})
    control = sorted(
        net for net, group in groups.items() if group in {"SDRAM_ADDRESS", "SDRAM_COMMAND"}
    )

    tracks = defaultdict(list)
    vias = defaultdict(list)
    widths, diameters = set(), set()
    for segment in board.segments:
        if segment.layer == SHARED_LAYER and segment.net_name in groups:
            tracks[segment.net_name].append(LineString([segment.start, segment.end]))
            widths.add(segment.width)
    for via in board.vias:
        if SHARED_LAYER in via.layers and via.net_name in groups:
            vias[via.net_name].append(via.position)
            diameters.add(via.size)
    if (
        not data
        or not control
        or len(data) + len(control) != len(groups)
        or len(widths) != 1
        or len(diameters) != 1
    ):
        raise ValueError("The Board 07 census requires both bus groups and uniform trace/via sizes")
    width, diameter = widths.pop(), diameters.pop()
    lines = {net: MultiLineString(tracks[net]) for net in groups}
    runs = {net: straight_runs(lines[net]) for net in groups}
    centers = {net: MultiPoint(vias[net]) for net in groups}
    close_pairs, spans = [], []
    for byte, command in product(data, control):
        envelopes = (
            ("track/track", lines[byte], lines[command], width),
            ("track/via", lines[byte], centers[command], (width + diameter) / 2),
            ("via/track", centers[byte], lines[command], (width + diameter) / 2),
            ("via/via", centers[byte], centers[command], diameter),
        )
        distances = {
            kind: left.distance(right) - radii
            for kind, left, right, radii in envelopes
            if not left.is_empty and not right.is_empty
        }
        if not distances or min(distances.values()) >= SEPARATION_MM:
            continue
        close_pairs.append(
            {
                "data": byte,
                "control": command,
                "edge_gaps_mm": {kind: round(gap, 6) for kind, gap in distances.items()},
            }
        )
        if command == "SDCLK" or distances.get("track/track", math.inf) >= SEPARATION_MM:
            continue
        for a, b in product(runs[byte], runs[command]):
            gap = a.distance(b) - width
            if gap >= SEPARATION_MM:
                continue
            overlap = parallel_overlap(a, b)
            if overlap > 1e-7:
                spans.append(
                    {
                        "data": byte,
                        "control": command,
                        "data_track": [[round(x, 6), round(y, 6)] for x, y in a.coords],
                        "control_track": [[round(x, 6), round(y, 6)] for x, y in b.coords],
                        "gap_mm": round(gap, 6),
                        "parallel_mm": round(overlap, 6),
                    }
                )

    spans.sort(
        key=lambda span: (span["data"], span["control"], span["data_track"], span["control_track"])
    )
    digest = hashlib.sha256(pcb.read_bytes()).hexdigest()
    spacing = {
        "pcb_sha256": digest,
        "scope": "All data/control track and via pairs on F.Cu within 5 mm. Clock-to-other-copper spacing on all signal layers is checked separately by clock_spacing.py.",
        "trace_width_mm": width,
        "via_diameter_mm": diameter,
        "layer": SHARED_LAYER,
        "pairs_within_5_mm": close_pairs,
        "nonclock_parallel_track_spans": spans,
    }
    model = CrosstalkAnalyzer(Stackup.from_pcb(board))
    scenarios = []
    for rise in RISE_TIMES_NS:
        totals = defaultdict(lambda: [0.0, 0.0])
        cases = []
        for span in spans:
            estimate = model.analyze(
                width, width, span["gap_mm"], span["parallel_mm"], SHARED_LAYER, rise
            )
            key = (span["data"], span["control"])
            totals[key][0] += estimate.next_percent
            totals[key][1] += estimate.fext_percent
            cases.append((max(estimate.next_percent, estimate.fext_percent), span, estimate))
        if not cases:
            scenarios.append({"assumed_rise_time_ns": rise, "worst_span": None, "worst_pair": None})
            continue
        _, span, worst = max(cases, key=lambda row: row[0])
        pair, (near, far) = max(totals.items(), key=lambda row: max(row[1]))
        scenarios.append(
            {
                "assumed_rise_time_ns": rise,
                "worst_span": {
                    **span,
                    "next_percent": round(worst.next_percent, 6),
                    "fext_percent": round(worst.fext_percent, 6),
                    "severity": worst.severity,
                },
                "worst_pair": {
                    "data": pair[0],
                    "control": pair[1],
                    "sum_next_percent": round(near, 6),
                    "sum_fext_percent": round(far, 6),
                },
            }
        )
    coupling = {
        "pcb_sha256": digest,
        "scope": "Illustrative single-aggressor estimates on the board stack for parallel non-clock data/control trace spans on F.Cu within 5 mm. Pair totals add modeled magnitudes. Vias, bends, pads, other layers, package parasitics and simultaneous aggressors are outside this estimate. Rise times are assumptions.",
        "scenarios": scenarios,
    }
    return spacing, coupling


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcb", type=Path, default=ROOT / "reviewed-routing/sdram_demo.kicad_pcb")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    reports = measure(args.pcb, ROOT / "authored-source/net_class_map.json")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, report in zip(
        ("package-escape-spacing.json", "escape-coupling-scenarios.json"), reports, strict=True
    ):
        (args.output_dir / name).write_text(json.dumps(report, indent=2) + "\n")
