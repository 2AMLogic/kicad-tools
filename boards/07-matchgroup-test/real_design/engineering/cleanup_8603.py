#!/usr/bin/env python3
"""Repair the physical copper of the six-layer Board 07 retuning candidate.

The coordinate record uses absolute KiCad millimetres. UUIDs belong to the
pinned candidate and keep neighbouring tracks on dense bus escapes distinct.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

from cleanup_artwork_8603 import finish
from cleanup_fixture_8603 import stage_design, stage_passing_pair
from retune_8603 import SOURCE_FILES, pinned_text
from retune_8603 import reproduce as reproduce_retuning
from shapely.geometry import LineString, Point

from kicad_tools.cli.kicad_toolchain import evidence_toolchain
from kicad_tools.pcb_prune import COVERED_TOL_MM
from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks
from kicad_tools.router.quantize import segment_angle_census
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string

RECORD = Path(__file__).with_name("cleanup-8603.json")
RETUNED_SHA256 = "c5c1e003bf546a5b4df692e01651a22c2d3d426d5b7e55cb848a05eacece50dc"
AUTHORED_SHA256 = "45f03dc9e5c311cf1ac50ffa6f53e7a93c450c2dc7f30cc6b253a4bfc0ec5ff8"
FRAGMENT_TOLERANCE_MM = 0.042


def absolute(board: PCB, point) -> tuple[float, float]:
    return tuple(round(n + off, 6) for n, off in zip(point, board.board_origin, strict=True))


def fragment_edits(board: PCB, authored: set[str], runs: list) -> dict:
    """Simplify only the recorded unbranched runs of sub-0.1 mm tracks."""
    segments = {s.uuid: s for s in board.segments}
    adjacent = defaultdict(list)
    for segment in segments.values():
        for point in (segment.start, segment.end):
            key = (segment.net_name, segment.layer, absolute(board, point))
            adjacent[key].append(segment)
    vias = {(via.net_name, absolute(board, via.position)) for via in board.vias}
    edits = {}
    for net, layer, first, start, end, size, expected in runs:
        segment = segments[first]
        at = tuple(start)
        chain = []
        points = []
        for i in range(size):
            a, b = segment.start, segment.end
            if absolute(board, b) == at:
                a, b = b, a
            if (
                absolute(board, a) != at
                or (segment.net_name, segment.layer) != (net, layer)
                or segment.width != segments[first].width
                or (segment.uuid in authored) != (first in authored)
                or not 0 < math.dist(a, b) < 0.1
                or segment.uuid in chain
                or segment.uuid in edits
            ):
                raise ValueError(f"invalid fragment path on {net}, {layer} at {at}")
            chain.append(segment.uuid)
            points.append(a)
            at = absolute(board, b)
            if i < size - 1:
                following = [s for s in adjacent[net, layer, at] if s.uuid != segment.uuid]
                if len(following) != 1 or (net, at) in vias:
                    raise ValueError(f"fragment path crosses a branch or via on {net} at {at}")
                segment = following[0]
        points.append(b)
        line = LineString(points)
        replacement = line.simplify(FRAGMENT_TOLERANCE_MM)
        if (
            at != tuple(end)
            or len(replacement.coords) != expected + 1
            or replacement.coords[0] != points[0]
            or replacement.coords[-1] != points[-1]
            or line.hausdorff_distance(replacement) > FRAGMENT_TOLERANCE_MM + 1e-9
        ):
            raise ValueError(f"fragment end or simplified geometry changed for {first}")
        edges = list(pairwise(absolute(board, point) for point in replacement.coords))
        for index, uid in enumerate(chain):
            edits[uid] = edges[index] if index < len(edges) else None
    return edits


def set_point(node, field: str, point) -> None:
    for index, value in enumerate(point):
        node.find_child(field).set_value(index, value)


def rewrite(text: str, kind: str, edits: dict, extra=None) -> str:
    """Splice the addressed records and leave surrounding text byte-identical."""
    chunks, cursor, seen = [], 0, set()
    extra = extra or {}
    for lo, hi, raw in _extract_balanced_blocks(text, kind):
        node = parse_string(raw)
        uid = node.find_child("uuid").get_string()
        if uid not in edits and uid not in extra:
            continue
        if uid in seen:
            raise ValueError(f"duplicate {kind} UUID {uid}")
        seen.add(uid)
        chunks.append(text[cursor:lo])
        cursor = hi
        if uid not in edits:
            chunks.append(raw)
        elif edits[uid] is not None:
            if kind == "segment":
                start, end = edits[uid]
                set_point(node, "start", start)
                set_point(node, "end", end)
            else:
                set_point(node, "at", edits[uid])
            chunks.append(node.to_string())
        for bridge, layer, start, end in extra.get(uid, ()):
            node.find_child("layer").set_value(0, layer)
            node.find_child("uuid").set_value(0, bridge)
            set_point(node, "start", start)
            set_point(node, "end", end)
            chunks.extend(["\n\t", node.to_string()])
    missing = (edits.keys() | extra.keys()) - seen
    if missing:
        raise ValueError(f"missing {kind} records {sorted(missing)}")
    return "".join([*chunks, text[cursor:]])


def copper_edits(board: PCB, groups: list) -> tuple[dict, dict]:
    """Resolve the measured copper geometry against the pinned tracks and their classes."""
    segments = {s.uuid: s for s in board.segments}
    edits, extra = {}, defaultdict(list)
    new_ids = set()
    for group in groups:
        signature = (group["net"], group["layer"], group["width"])

        def host(uid):
            segment = segments[uid]
            if (segment.net_name, segment.layer, segment.width) != signature:
                raise ValueError(f"{uid} is not on {signature}")
            return segment

        for uid, container in group.get("covered_by", ()):
            victim, covering = host(uid), host(container)
            line = LineString([covering.start, covering.end])
            if line.length <= math.dist(victim.start, victim.end) + COVERED_TOL_MM or any(
                line.distance(Point(p)) > COVERED_TOL_MM for p in (victim.start, victim.end)
            ):
                raise ValueError(f"{uid} is not fully covered by {container}")
            edits[uid] = None
        for uid in group.get("remove", ()):
            host(uid)
            edits[uid] = None
        for uid, start, end in group.get("ends", ()):
            segment = host(uid)
            edits[uid] = (
                start or absolute(board, segment.start),
                end or absolute(board, segment.end),
            )
        for uid, template, start, end in group.get("new_tracks", ()):
            parent = segments[template]
            if (parent.net_name, parent.width) != (group["net"], group["width"]):
                raise ValueError(f"new track {uid} has the wrong net or width")
            if uid in segments or uid in new_ids or start == end:
                raise ValueError(f"invalid new track {uid}")
            new_ids.add(uid)
            extra[template].append((uid, group["layer"], start, end))
    return edits, extra


def relocated(board: PCB, record: dict) -> tuple[dict, dict]:
    """Verify each physical via or footprint before relocating its centre."""
    vias = {v.uuid: v for v in board.vias}
    footprints = {f.uuid: f for f in board.footprints}
    via_edits, placement = {}, {}
    for uid, net, old, new in record["vias"]:
        via = vias[uid]
        if via.net_name != net or absolute(board, via.position) != tuple(old):
            raise ValueError(f"via {uid} differs from the {net} repair site")
        via_edits[uid] = new
    for ref, uid, old, new in record["footprints"]:
        fp = footprints[uid]
        if fp.reference != ref or absolute(board, fp.position) != tuple(old):
            raise ValueError(f"footprint {ref} differs from its recorded placement")
        placement[uid] = new
    return via_edits, placement


def owned_edits(edits: dict, ids: set[str]) -> dict:
    return {uid: value for uid, value in edits.items() if uid in ids}


def repair_copper(retuned: Path, staged: Path, record: dict) -> None:
    candidate = retuned / "candidate.kicad_pcb"
    source = retuned / "source/sdram_demo.kicad_pcb"
    texts = [pinned_text(candidate, RETUNED_SHA256), pinned_text(source, AUTHORED_SHA256)]
    board, authored = PCB.load(candidate), PCB.load(source)
    owned = {s.uuid for s in authored.segments}
    fragments = fragment_edits(board, owned, record["fragment_runs"])
    cleaned = staged / "candidate.kicad_pcb"
    cleaned_source = staged / "source/sdram_demo.kicad_pcb"
    cleaned_source.parent.mkdir(parents=True)
    for name in SOURCE_FILES:
        shutil.copyfile(source.parent / name, cleaned_source.parent / name)
    simplified = rewrite(texts[0], "segment", fragments)
    cleaned.write_text(simplified, encoding="utf-8")
    board = PCB.load(cleaned)
    edits, extra = copper_edits(board, record["copper"])
    via_edits, placement = relocated(board, record)
    via_ids = {via.uuid for via in authored.vias}
    source_text = rewrite(texts[1], "segment", owned_edits(fragments, owned))
    for path, text, tracks, bridges, vias in (
        (cleaned, simplified, edits, extra, via_edits),
        (
            cleaned_source,
            source_text,
            owned_edits(edits, owned),
            owned_edits(extra, owned),
            owned_edits(via_edits, via_ids),
        ),
    ):
        text = rewrite(text, "segment", tracks, bridges)
        text = rewrite(text, "via", vias)
        path.write_text(rewrite(text, "footprint", placement), encoding="utf-8")


def add_layer_handoffs(path: Path, through_vias: list) -> None:
    """Require a real change of routing layer at every new plated through-hole."""
    if not through_vias:
        return
    board = PCB.load(path)
    endpoints = defaultdict(set)
    for track in board.segments:
        for endpoint in (track.start, track.end):
            endpoints[track.net_name, absolute(board, endpoint)].add(track.layer)
    occupied = {absolute(board, via.position) for via in board.vias}
    identities = {obj.uuid for obj in [*board.segments, *board.vias]}
    rows = []
    for uid, net, point in through_vias:
        site = tuple(point)
        active = endpoints[net, site]
        if (
            uid in identities
            or site in occupied
            or active not in ({"F.Cu", "In2.Cu"}, {"In3.Cu", "B.Cu"})
        ):
            raise ValueError(f"no distinct matched-route layer transition for {net} at {site}")
        identities.add(uid)
        occupied.add(site)
        rows.append(
            f"(via (at {point[0]} {point[1]}) (size 0.5) (drill 0.2) "
            f'(layers "F.Cu" "B.Cu") (net {json.dumps(net)}) (uuid {json.dumps(uid)}))'
        )
    text = path.read_text(encoding="utf-8")
    end = text.rindex(")")
    path.write_text(text[:end] + "\n\t" + "\n\t".join(rows) + "\n" + text[end:], encoding="utf-8")


def prepare(retuned: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"choose a new candidate directory: {output}")
    record = json.loads(RECORD.read_bytes())
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        staged = Path(scratch) / "candidate"
        repair_copper(retuned, staged, record)
        add_layer_handoffs(staged / "candidate.kicad_pcb", record["through_vias"])
        for pcb in (staged / "candidate.kicad_pcb", staged / "source/sdram_demo.kicad_pcb"):
            total, off_angle = segment_angle_census(pcb)
            if not total or off_angle:
                raise ValueError(f"{pcb.name} has {len(off_angle)} off-angle tracks of {total}")
        finish(staged, Path(scratch))
        staged.rename(output)


def reproduce(output: Path) -> int:
    design = RECORD.parent.parent
    repo = design.parents[2]
    baseline = output / "baseline"
    output.mkdir(parents=True, exist_ok=False)
    # The previous release remains reproducible after either production pin moves.
    original = output / "original-design"
    stage_design(design, original, original=True)
    code = reproduce_retuning(baseline, starting_design=original)
    if code:
        report = baseline / "evidence/validation.json"
        if not report.exists() or json.loads(report.read_bytes())["errors"] != [
            "Full kct design checks failed (exit 2)"
        ]:
            return code
    cleaned = output / "cleaned"
    with evidence_toolchain([repo, output], pin_host_footprints=True):
        prepare(baseline / "retuned", cleaned)
        validation = subprocess.run(
            [
                sys.executable,
                design / "validate.py",
                cleaned / "candidate.kicad_pcb",
                cleaned / "source",
                output / "evidence",
            ],
            cwd=repo,
        )
        if validation.returncode:
            return validation.returncode
        proposed = output / "proposed-design"
        stage_passing_pair(design, cleaned, output / "evidence", proposed)
        return subprocess.run(
            [sys.executable, proposed / "build.py", output / "release"], cwd=repo
        ).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    raise SystemExit(reproduce(parser.parse_args().output.resolve()))
