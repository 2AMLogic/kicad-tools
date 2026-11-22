#!/usr/bin/env python3
"""Reproduce the six-layer Board 07 SDRAM tuning candidate from issue #8603.

Run in the project environment with a new output directory. The recipe builds
the release input, optimizes with the board's six-layer rules, retunes the
candidate, and returns the unchanged release validator's exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from shapely.geometry import LineString, MultiLineString, Point

from kicad_tools.cli.kicad_toolchain import evidence_toolchain
from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.mask_geometry import pad_mask_shapes

RECORD = Path(__file__).with_name("retune-8603.toml")
OPTIMIZED_SHA256 = "95e6b2297888ed6aacdd26a4f071ad29bba0163b7cc61165de0cfccb5b182289"
AUTHORED_SHA256 = "689abbbde3a373bf18d49ac471c34b7402599ea5f5270c2d829cb469918afe94"
SOURCE_FILES = (
    "circuit.json",
    "net_class_map.json",
    "sdram_constraints.json",
    "sdram_demo.kicad_pro",
    "sdram_demo.kicad_sch",
)


def pinned_text(path: Path, expected: str) -> str:
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(f"{path} has SHA-256 {actual}; expected {expected}")
    return raw.decode("utf-8")


def retune_tracks(text: str, routes: list[dict], owned=frozenset()) -> str:
    """Replace exactly one 0.18 mm host per edge, preserving all other PCB text."""
    replacements = {}
    for row in routes:
        before, after = (list(map(tuple, row[key])) for key in ("before", "after"))
        if (before[0], before[-1]) != (after[0], after[-1]):
            raise ValueError(f"{row['net']} path terminals differ")
        # A translated clock corridor moves the junctions of three existing
        # tracks. A trombone replaces its one straight host with a series path.
        if len(before) == 2:
            paths = [after]
        elif len(after) == len(before):
            paths = list(pairwise(after))
        else:
            raise ValueError(f"{row['net']} has ambiguous track correspondence")
        for (u, v), path in zip(pairwise(before), paths, strict=True):
            key = (row["net"], row["layer"], frozenset((u, v)))
            if key in replacements:
                raise ValueError(f"duplicate host in repair record: {key}")
            replacements[key] = (u, path)

    pieces, seen, cursor = [], set(), 0
    for lo, hi, block in _extract_balanced_blocks(text, "segment"):
        track = parse_string(block)
        if track.find("width").get_float() != 0.18:
            continue
        start, end = (
            tuple(field.get_float(i) for i in (0, 1))
            for field in (track.find("start"), track.find("end"))
        )
        key = (
            track.find("net").get_string(),
            track.find("layer").get_string(),
            frozenset((start, end)),
        )
        if key not in replacements:
            continue
        if key in seen:
            raise ValueError(f"duplicate host on PCB: {key}")
        seen.add(key)
        u, path = replacements[key]
        ordered = path if start == u else path[::-1]
        ident = uuid.UUID(track.find("uuid").get_string())
        if str(ident) in owned:
            raise ValueError(f"tuning host overlaps authored copper: {key}")
        pieces.append(text[cursor:lo])
        for index, (p, q) in enumerate(pairwise(ordered)):
            if index:
                pieces.append("\n\t")
                track.find("uuid").set_value(0, str(uuid.uuid5(ident, f"8603/{index}")))
            for field, point in ((track.find("start"), p), (track.find("end"), q)):
                for i, value in enumerate(point):
                    field.set_value(i, value)
            pieces.append(track.to_string())
        cursor = hi
    if missing := replacements.keys() - seen:
        raise ValueError(f"missing host on PCB: {missing}")
    return "".join([*pieces, text[cursor:]])


def point_key(p):
    """KiCad stores track and via coordinates on the 1 nm grid."""
    return tuple(round(v, 6) for v in p)


def escape_path(board: PCB, pin: str, via_uuid: str):
    """Follow one unbranched 0.18 mm front-side escape from its via to its pad."""
    vias = [v for v in board.vias if v.uuid == via_uuid]
    if len(vias) != 1:
        raise ValueError(f"{pin}: expected one physical via with UUID {via_uuid}")
    via = vias[0]
    if set(via.layers) != {"F.Cu", "B.Cu"}:
        raise ValueError(f"{pin}: escape via is not through")
    adj = defaultdict(list)
    for seg in board.segments:
        if seg.layer == "F.Cu" and seg.net_name == via.net_name and seg.width == 0.18:
            for p in (seg.start, seg.end):
                adj[point_key(p)].append(seg)
    current = point_key(via.position)
    if len(adj[current]) != 1:
        raise ValueError(f"{pin}: expected one F.Cu track at via {via_uuid}")
    points, visited = [current], set()
    while following := [seg for seg in adj[current] if seg.uuid not in visited]:
        if len(following) != 1:
            raise ValueError(f"{pin}: branched escape at {current}")
        seg = following[0]
        visited.add(seg.uuid)
        a, b = point_key(seg.start), point_key(seg.end)
        current = b if a == current else a
        points.append(current)
    terminal = Point(current)
    pins = [
        (f"{fp.reference}.{pad.number}", pad.uuid)
        for fp in board.footprints
        for pad in fp.pads
        if pad.net_name == via.net_name
        and ("F.Cu" in pad.layers or "*.Cu" in pad.layers)
        and pad_mask_shapes(pad, fp, 0)[0].covers(terminal)
    ]
    if len(pins) != 1 or pins[0][0] != pin or current == points[0]:
        raise ValueError(f"{pin}: escape terminates at {current} on {pins}")
    identity = (via.net_name, via.size, via.drill, pins[0][1])
    return LineString(points), visited, identity


def authored_ids(authored: PCB, optimized: PCB, escapes: dict[str, str]) -> set[str]:
    """Recover original copper coverage plus the 14 pad/via anchored front escapes."""
    by_class = defaultdict(list)
    for seg in authored.segments:
        by_class[(seg.net_name, seg.layer, seg.width)].append((seg.start, seg.end))
    # Tolerance is 0.1 um for collinear coverage, as in the #8602 prune.
    support = {
        k: MultiLineString(lines).buffer(0.0001, cap_style="square")
        for k, lines in by_class.items()
    }
    owned = {
        s.uuid
        for s in optimized.segments
        if (area := support.get((s.net_name, s.layer, s.width))) is not None
        and area.covers(LineString([s.start, s.end]))
    }
    for pin, via_uuid in escapes.items():
        old, _, original_identity = escape_path(authored, pin, via_uuid)
        new, ids, identity = escape_path(optimized, pin, via_uuid)
        if (
            identity != original_identity
            or old.coords[0] != new.coords[0]
            or old.coords[-1] != new.coords[-1]
            or not math.isclose(old.length, new.length, abs_tol=0.0001)
            or old.hausdorff_distance(new) > 0.531
        ):
            raise ValueError(f"{pin}: changed pad/via terminals or escape geometry")
        owned.update(ids)
    return owned


def rederive_authored(authored: str, optimized: str, owned: set[str]) -> str:
    """Keep the authored PCB except for its collection of source track records."""
    selected = []
    for _, _, block in _extract_balanced_blocks(optimized, "segment"):
        if parse_string(block).find("uuid").get_string() in owned:
            selected.append(block)
    if len(selected) != len(owned):
        raise ValueError("missing or duplicated owned copper")
    spans = _extract_balanced_blocks(authored, "segment")
    chunks = [authored[: spans[0][0]], "\n\t".join(selected)]
    for (_, end, _), (start, _, _) in pairwise(spans):
        if authored[end:start].strip():
            chunks.append(authored[end:start])
    return "".join([*chunks, authored[spans[-1][1] :]])


def prepare(optimized: Path, release: Path, output: Path) -> None:
    optimized_text = pinned_text(optimized, OPTIMIZED_SHA256)
    source_path = release / "sdram_demo.kicad_pcb"
    authored_text = pinned_text(source_path, AUTHORED_SHA256)
    if output.exists():
        raise FileExistsError(f"choose a new candidate directory: {output}")
    with RECORD.open("rb") as f:
        record = tomllib.load(f)
    owned = authored_ids(PCB.load(source_path), PCB.load(optimized), record["escape"])
    tuned = retune_tracks(optimized_text, record["path"], owned)
    new_authored = rederive_authored(authored_text, optimized_text, owned)
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        staged = Path(scratch) / "candidate"
        source = staged / "source"
        source.mkdir(parents=True)
        for name in SOURCE_FILES:
            shutil.copyfile(release / name, source / name)
        (source / source_path.name).write_text(new_authored, encoding="utf-8")
        (staged / "candidate.kicad_pcb").write_text(tuned, encoding="utf-8")
        staged.rename(output)


def reproduce(output: Path, *, starting_design: Path | None = None) -> int:
    design = RECORD.parent.parent
    repo = design.parents[2]
    release, optimized = output / "release", output / "optimized.kicad_pcb"
    tuned = output / "retuned"
    output.mkdir(parents=True, exist_ok=False)
    with evidence_toolchain([repo, output], pin_host_footprints=True):
        build = subprocess.run(
            [sys.executable, (starting_design or design) / "build.py", release], cwd=repo
        )
        if build.returncode:
            # The reviewed starting board already fails its full design check.
            # A different build failure invalidates the starting evidence.
            report = release / "validation/validation.json"
            if not report.exists() or json.loads(report.read_text())["errors"] != [
                "Full kct design checks failed (exit 2)"
            ]:
                return build.returncode
        project = release / "validation/checked.kicad_pro"
        if not project.with_suffix(".kicad_dru").is_file():
            raise FileNotFoundError(project.with_suffix(".kicad_dru"))
        # Native probes create their own trial boards. A failed probe can
        # consume a different number of UUIDs even for identical final copper.
        # Preserve the machine report and require both checks before retuning.
        command = [
            "kct",
            "optimize-traces",
            release / "sdram_demo_routed.kicad_pcb",
            "-o",
            optimized,
            "--project",
            project,
            "--drc-aware",
            "--mfr",
            "jlcpcb",
            "--layers",
            "6",
            "--format",
            "json",
        ]
        for attempt in range(2):
            result = subprocess.run(
                command, cwd=repo, check=True, stdout=subprocess.PIPE, text=True
            )
            report = json.loads(result.stdout)
            (output / f"optimizer-{attempt + 1}.json").write_text(result.stdout)
            checks = report["output_checks"]
            if all(checks.get(name) == "ran" for name in ("drc", "opens")):
                break
        else:
            raise RuntimeError(f"Native optimizer checks did not complete: {checks}")
        prepare(optimized, release, tuned)
        return subprocess.run(
            [
                sys.executable,
                design / "validate.py",
                tuned / "candidate.kicad_pcb",
                tuned / "source",
                output / "evidence",
            ],
            cwd=repo,
        ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    return reproduce(args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
