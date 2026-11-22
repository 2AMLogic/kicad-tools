"""Regression controls for the six-layer board 07 optimizer/retune experiment."""

import importlib.util
import json
from collections import Counter
from contextlib import nullcontext
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from shapely.geometry import LineString, Point

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ENGINEERING = (
    Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design/engineering"
)
RECORD = tomllib.loads((ENGINEERING / "retune-8603.toml").read_text())


@pytest.fixture(scope="module")
def recipe():
    spec = importlib.util.spec_from_file_location("retune_8603", ENGINEERING / "retune_8603.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def segment(net, layer, start, end, *, width=0.18, tag=""):
    ident = uuid5(NAMESPACE_URL, repr((net, layer, start, end, width, tag)))
    return (
        f"(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) "
        f'(width {width}) (layer "{layer}") (uuid "{ident}") (net "{net}"))'
    )


def test_record_restores_four_physical_timing_groups():
    # Saved pad-to-pad lengths with series via travel BEFORE retuning. Use the
    # actual worst witnesses so a straight chord replacing a trombone fails.
    deltas = Counter()
    for row in RECORD["path"]:
        old, new = row["before"], row["after"]
        assert {tuple(old[0]), tuple(old[-1])} == {tuple(new[0]), tuple(new[-1])}
        assert LineString(new).is_simple
        for (x, y), (u, v) in pairwise(new):
            dx, dy = abs(u - x), abs(v - y)
            assert max(dx, dy) > 0
            assert min(dx, dy, abs(dx - dy)) < 0.000002
        deltas[row["net"]] += LineString(new).length - LineString(old).length
    assert 26.598367 + deltas["DQ1"] == pytest.approx(62.75, abs=0.001)
    assert 50.880487 + deltas["DQ0"] == pytest.approx(62.75, abs=0.001)
    assert 76.738064 - (68.739919 + deltas["DQ11"]) < 4.4
    assert 78.736711 - (61.575267 + deltas["A9"]) < 4.24
    assert 72.070339 - (62.531289 + deltas["SDNE1"]) < 3.78
    assert abs(deltas["SDCLK"]) < 1e-6


def test_clock_clears_the_real_ba1_via_and_a1_trace():
    clock, address = (
        next(row for row in RECORD["path"] if row["net"] == net) for net in ("SDCLK", "A1")
    )
    via = Point(148.662, 105.287)

    # BA1 is a 0.5 mm through-via, SDCLK and A1 are both 0.18 mm traces.
    def gaps(stage):
        path = LineString(clock[stage])
        return path.distance(via) - 0.25 - 0.09, path.distance(LineString(address[stage])) - 0.18

    assert gaps("before") == pytest.approx((0.447, 0.470), abs=0.001)
    assert min(gaps("after")) >= 0.54


def test_track_splice_uses_the_physical_net_layer_width_and_preserves_other_bytes(recipe):
    # Replay a reversed host next to an identically positioned track on another
    # layer and a different width. The latter and the via must survive verbatim.
    row = next(row for row in RECORD["path"] if row["net"] == "SDNE1")
    u, v = row["before"]
    host = segment("SDNE1", "B.Cu", v, u)
    stationary = "\n".join(
        [
            segment("SDNE1", "In3.Cu", u, v),
            segment("SDNE1", "B.Cu", u, v, width=0.25),
            segment("SDCLK", "B.Cu", u, v),
            "(via (at 128.75 90.45) (size 0.5) (drill 0.2))",
        ]
    )
    board = f"(kicad_pcb\n{host}\n{stationary}\n)"
    tuned = recipe.retune_tracks(board, [row])
    assert stationary in tuned
    assert tuned == recipe.retune_tracks(board, [row])
    tracks = parse_string(tuned).find_all("segment")[:-3]
    assert len(tracks) == len(row["after"]) - 1
    assert len({s.find("uuid").get_string() for s in tracks}) == len(tracks)
    assert tracks[0].find("uuid").get_string() == parse_string(host).find("uuid").get_string()
    pts = [(s.find("start").get_float(0), s.find("start").get_float(1)) for s in tracks]
    pts.append(tuple(tracks[-1].find("end").get_float(i) for i in (0, 1)))
    assert pts == [tuple(p) for p in reversed(row["after"])]
    for bad in [f"(kicad_pcb {stationary})", f"(kicad_pcb {host} {host})"]:
        with pytest.raises(ValueError, match="host"):
            recipe.retune_tracks(bad, [row])
    with pytest.raises(ValueError, match="authored copper"):
        recipe.retune_tracks(board, [row], {tracks[0].find("uuid").get_string()})


def test_escape_ownership_ends_at_the_recorded_physical_pad_and_via(recipe, tmp_path):
    # The record was measured against the pre-adoption authored copper, whose
    # A9 escape was still the fragmented 30+-segment staircase; the #8603
    # adoption simplified it in place.
    import hashlib
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    blob = subprocess.check_output(
        [
            "git",
            "show",
            "8148a4d88:boards/07-matchgroup-test/real_design/authored-source/sdram_demo.kicad_pcb",
        ],
        cwd=repo,
    )
    assert (
        hashlib.sha256(blob).hexdigest()
        == "51663e0156b541c9602bef2bf55266bbfde02545fb5264a2909fdd4cf4b7163a"
    )
    pinned = tmp_path / "authored-pre-adoption.kicad_pcb"
    pinned.write_bytes(blob)
    board = PCB.load(str(pinned))
    pin = "U1.55"
    via = RECORD["escape"][pin]
    path, ids, identity = recipe.escape_path(board, pin, via)
    assert identity[:3] == ("A9", 0.5, 0.2)
    ox, oy = board.board_origin
    assert path.coords[0] == pytest.approx((133.787 - ox, 118.912 - oy))
    assert path.coords[-1] == pytest.approx((133.75 - ox, 115.662 - oy))
    tracks = [seg for seg in board.segments if seg.uuid in ids]
    assert len(tracks) > 2
    # The other pad on A9 belongs to the same net, so a net-name match is
    # insufficient. Duplicating an edge also invalidates an escape's ownership.
    with pytest.raises(ValueError, match="terminates"):
        recipe.escape_path(board, "U2.34", via)
    branched = SimpleNamespace(
        segments=[*tracks, tracks[0]], vias=board.vias, footprints=board.footprints
    )
    with pytest.raises(ValueError, match="branched|expected one"):
        recipe.escape_path(branched, pin, via)


def test_authored_coverage_does_not_claim_adjacent_front_trunks(recipe):
    def track(ident, start, end, net="A9", layer="F.Cu", width=0.18):
        return SimpleNamespace(
            uuid=ident, start=start, end=end, net_name=net, layer=layer, width=width
        )

    source = SimpleNamespace(
        segments=[
            track("first", (0, 0), (1, 0)),
            track("second", (1, 0), (2, 0)),
        ]
    )
    optimized = SimpleNamespace(
        segments=[
            track("merged", (2, 0), (0, 0)),
            track("past the pad", (0, 0), (2.01, 0)),
            track("near another track", (0, 0.01), (2, 0.01)),
            track("wrong net", (0, 0), (2, 0), net="A8"),
            track("wrong layer", (0, 0), (2, 0), layer="In3.Cu"),
            track("wrong width", (0, 0), (2, 0), width=0.2),
        ]
    )
    assert recipe.authored_ids(source, optimized, {}) == {"merged"}


def test_unrelated_board_is_rejected_before_writing(tmp_path, recipe):
    src = tmp_path / "release"
    src.mkdir()
    pcb = tmp_path / "four-layer.kicad_pcb"
    pcb.write_text("(kicad_pcb (layers (0 F.Cu signal) (31 B.Cu signal)))")
    out = tmp_path / "candidate"
    with pytest.raises(ValueError, match="SHA-256"):
        recipe.prepare(pcb, src, out)
    assert not out.exists()


def test_fresh_recipe_stops_on_an_unrelated_build_failure(tmp_path, monkeypatch, recipe):
    output = tmp_path / "run"
    commands = []

    def failed_build(argv, **kwargs):
        commands.append(argv)
        report = Path(argv[-1]) / "validation/validation.json"
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"errors": ["Schematic/PCB copper or label LVS failed"]}))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(recipe, "evidence_toolchain", lambda *a, **k: nullcontext())
    monkeypatch.setattr(recipe.subprocess, "run", failed_build)
    assert recipe.reproduce(output) == 1
    assert len(commands) == 1 and commands[0][1].name == "build.py"
    assert not (output / "retuned").exists()
    with pytest.raises(FileExistsError):
        recipe.reproduce(output)


@pytest.mark.parametrize("recovers", [False, True])
def test_retuning_requires_both_native_optimizer_probes(tmp_path, monkeypatch, recipe, recovers):
    attempts = []
    accepted = []
    monkeypatch.setattr(recipe, "evidence_toolchain", lambda *a, **k: nullcontext())

    def run(argv, **kwargs):
        if len(argv) > 1 and Path(argv[1]).name == "build.py":
            project = Path(argv[2]) / "validation/checked.kicad_dru"
            project.parent.mkdir(parents=True)
            project.write_text("(version 1)")
        elif argv[0] == "kct":
            attempts.append(argv)
            status = "ran" if len(attempts) == 2 and recovers else "unavailable"
            return SimpleNamespace(
                stdout=json.dumps({"output_checks": {"opens": "ran", "drc": status}})
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(recipe.subprocess, "run", run)
    monkeypatch.setattr(recipe, "prepare", lambda *args: accepted.append(args))
    out = tmp_path / "probe-test"
    if recovers:
        assert recipe.reproduce(out) == 0
        assert len(accepted) == 1
    else:
        with pytest.raises(RuntimeError, match="Native optimizer checks did not complete"):
            recipe.reproduce(out)
        assert not accepted
    assert len(attempts) == 2
    assert (
        json.loads((out / "optimizer-1.json").read_bytes())["output_checks"]["drc"] == "unavailable"
    )
