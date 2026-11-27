"""The reviewed physical fixture must agree with independent circuit generation."""

import importlib.util
from pathlib import Path


def test_reviewed_physical_source_matches_fresh_circuit(tmp_path):
    root = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"
    spec = importlib.util.spec_from_file_location("board07_source", root / "build_source.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.build(tmp_path)
    assert result["electrical_identity_verified"]
    assert (result["parts"], result["bound_pads"]) == (43, 216)
    assert not result["manufacturing_ready"]
    import json

    rules = json.loads((tmp_path / "net_class_map.json").read_text())
    for name, rule in rules.items():
        assert 1 in rule["avoid_layers"] and 4 in rule["avoid_layers"]
        if rule.get("length_match_group"):
            assert rule["trace_width"] == 0.18
            # Issue #7423: the published map tunes the SDRAM groups at route time.
            assert rule["length_critical"] is True
    assert (
        json.loads((tmp_path / "sdram_constraints.json").read_text())["stackup"]
        == "JLC06161H-2116A"
    )


def _routing_fixture(end):
    return f"""(kicad_pcb
  (segment
    (start 0 0)
    (end {end})
    (width 0.18)
    (layer "F.Cu")
    (net 1)
    (uuid "11111111-1111-1111-1111-111111111111")
  )
)
"""


def test_reviewed_build_rejects_hash_bound_off_angle_copper(tmp_path, monkeypatch):
    """A matching manifest cannot bypass either source's angle policy."""
    import hashlib
    import json
    from types import SimpleNamespace

    import pytest

    root = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"
    spec = importlib.util.spec_from_file_location("board07_build_angles", root / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "authored-source"
    reviewed = tmp_path / "reviewed-routing"
    source.mkdir()
    reviewed.mkdir()
    (source / "manifest.json").write_text(json.dumps({"sha256": {}}))
    monkeypatch.setattr(module, "ROOT", tmp_path)

    def unexpected_validation(*args):
        pytest.fail("Off-angle copper reached electrical validation")

    for bad_source in ("authored", "routed"):
        candidate = _routing_fixture("1 2" if bad_source == "routed" else "1 1")
        (reviewed / "sdram_demo.kicad_pcb").write_text(candidate)
        (reviewed / "manifest.json").write_text(
            json.dumps(
                {
                    "authored_source_sha256": {},
                    "sha256": {
                        "sdram_demo.kicad_pcb": hashlib.sha256(candidate.encode()).hexdigest()
                    },
                }
            )
        )

        def build_source(output):
            output.mkdir()
            (output / "sdram_demo.kicad_pcb").write_text(
                _routing_fixture("1 2" if bad_source == "authored" else "1 1")
            )

        monkeypatch.setattr(
            module,
            "load_module",
            lambda name: (
                SimpleNamespace(build=build_source)
                if name == "build_source"
                else SimpleNamespace(check=unexpected_validation)
            ),
        )
        with pytest.raises(RuntimeError, match="off-angle copper"):
            module.build(tmp_path / f"output-{bad_source}")


ROOT07 = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"

# The #8603 adoption re-optimized both physical boards, so the historical
# repair records are verified against the last pre-adoption copper instead of
# the live files. The squash merge keeps this commit reachable as an ancestor
# of main, and the recorded digests keep the pin self-verifying.
PRE_ADOPTION_8603 = "8148a4d88"
PRE_ADOPTION_SHA256_8603 = {
    "authored-source/sdram_demo.kicad_pcb": "51663e0156b541c9602bef2bf55266bbfde02545fb5264a2909fdd4cf4b7163a",
    "reviewed-routing/sdram_demo.kicad_pcb": "cb1a2322eba616ebee1c1f66231cf05f898c7213d5e380deb19060bbdd900011",
}


def _pre_adoption_board(name: str, tmp_path) -> Path:
    import hashlib
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    blob = subprocess.check_output(
        ["git", "show", f"{PRE_ADOPTION_8603}:boards/07-matchgroup-test/real_design/{name}"],
        cwd=repo,
    )
    assert hashlib.sha256(blob).hexdigest() == PRE_ADOPTION_SHA256_8603[name]
    target = tmp_path / name.replace("/", "_")
    target.write_bytes(blob)
    return target


def _load_record(name, root=ROOT07):
    spec = importlib.util.spec_from_file_location(f"board07_{name}", root / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_front_layer_spacing_and_coupling_follow_the_reviewed_route():
    import json

    engineering = ROOT07 / "engineering"
    census = _load_record("measure_escapes", engineering)
    spacing, coupling = census.measure(
        ROOT07 / "reviewed-routing/sdram_demo.kicad_pcb",
        ROOT07 / "authored-source/net_class_map.json",
    )
    assert spacing == json.loads((engineering / "package-escape-spacing.json").read_bytes())
    assert coupling == json.loads((engineering / "escape-coupling-scenarios.json").read_bytes())


def test_collinear_fragments_count_as_a_single_coupled_run():
    from shapely.geometry import LineString, MultiLineString

    census = _load_record("measure_escapes", ROOT07 / "engineering")
    parts = MultiLineString([[(0, 0), (2, 0)], [(4, 0), (2, 0)], [(4, 0), (4, 1)]])
    victim = LineString([(3, 1), (1, 1)])
    assert sorted(census.parallel_overlap(run, victim) for run in census.straight_runs(parts)) == [
        0.0,
        2.0,
    ]


def test_native_refill_keeps_reviewed_ids_and_stabilizes_added_fiducial_fields(tmp_path):
    from uuid import UUID, uuid4

    from kicad_tools.core.canonical_uuids import canonicalize_pcb_file_uuids, uuids_in_file

    reviewed = uuids_in_file(ROOT07 / "reviewed-routing/sdram_demo.kicad_pcb")
    released = ROOT07.parent / "output/matchgroup_test_routed.kicad_pcb"
    published = uuids_in_file(released)
    assert reviewed <= published
    added = published - reviewed
    assert len(added) == 12
    assert all(UUID(value).version == 5 for value in added)

    # Simulate another first KiCad save of the two new properties on each FID.
    # KiCad and the canonicalizer order copper differently. Compare identities.
    text = released.read_text()
    for value in added:
        text = text.replace(value, str(uuid4()))
    native_save = tmp_path / "refilled.kicad_pcb"
    native_save.write_text(text)
    canonicalize_pcb_file_uuids(native_save, keep=reviewed)
    assert uuids_in_file(native_save) == published


def test_declared_fiducials_require_an_unconnected_unnumbered_board_only_land(tmp_path):
    import shutil
    import subprocess
    from types import SimpleNamespace

    import pytest

    # A caller may stage inside another checkout. Apply only to that scratch
    # pair, keeping the production recipes byte-identical to their release.
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    proposed = tmp_path / "proposed"
    fixture = _load_record("cleanup_fixture_8603", ROOT07 / "engineering")
    fixture.stage_design(ROOT07, proposed)
    # The rehearsal also accepts a source tree that already has the fiducial changes.
    candidate_patch = Path("engineering/assembly-fiducials.patch")
    shutil.copyfile(ROOT07 / candidate_patch, proposed / candidate_patch)
    adopted = tmp_path / "adopted"
    fixture.stage_design(proposed, adopted)
    # Re-staging the adopted fiducial support must reproduce the production recipes.
    for name in ("build.py", "build_source.py"):
        assert (adopted / name).read_bytes() == (proposed / name).read_bytes()
        assert (ROOT07 / name).read_bytes() == (adopted / name).read_bytes()
    release = _load_record("build_source", proposed)
    pad = SimpleNamespace(
        number="", net_name="", net_number=0, size=(1.0, 1.0), drill=0.0, layers=["F.Cu", "F.Mask"]
    )
    mark = SimpleNamespace(
        reference="FID1",
        name=release.FIDUCIAL_FOOTPRINT,
        value="Fiducial",
        layer="F.Cu",
        _attr_unknown_tokens=["board_only"],
        exclude_from_bom=True,
        exclude_from_pos_files=True,
        pads=[pad],
    )
    board = SimpleNamespace(footprints=[mark])
    release.verify_fiducials(board, {"FID1": "F.Cu"})
    for obj, field, invalid in (
        (mark, "name", "Resistor_SMD:R_0603"),
        (mark, "_attr_unknown_tokens", []),
        (mark, "exclude_from_pos_files", False),
        (mark, "exclude_from_bom", False),
        (mark, "layer", "B.Cu"),
        (mark, "pads", [pad, pad]),
        (pad, "number", "1"),
        (pad, "net_name", "SDCLK"),
        (pad, "net_number", 2),
        (pad, "drill", 0.3),
        (pad, "size", (1.0, 2.0)),
        (pad, "layers", ["F.Cu", "B.Cu"]),
    ):
        initial = getattr(obj, field)
        setattr(obj, field, invalid)
        with pytest.raises(ValueError, match="Invalid board-only fiducial"):
            release.verify_fiducials(board, {"FID1": "F.Cu"})
        setattr(obj, field, initial)
    with pytest.raises(ValueError, match="Missing declared fiducials"):
        release.verify_fiducials(board, {"FID1": "F.Cu", "FID2": "B.Cu"})


def _vias(path):
    import re

    return {
        (net, (float(x), float(y)))
        for x, y, net in re.findall(
            r'\(via\s+\(at ([-\d.]+) ([-\d.]+)\).*?\(net "([^"]*)"\)', path.read_text(), re.S
        )
    }


def test_class_clearance_repair_is_applied_and_bounded(tmp_path):
    """#6396: every listed via sits at its repaired position, moved <= 50 um.

    #9091 later moved two of these vias again; a via counts as in place when it
    sits where the later record put it. Verified on the pinned pre-adoption
    copper: the #8603 re-optimization replaced those vias wholesale.
    """
    import math

    module = _load_record("repair_class_clearance_6396")
    later = {
        (net, old): new for net, old, new, _ in _load_record("repair_copper_gap_9091").VIA_MOVES
    }
    boards = {
        "authored": _pre_adoption_board("authored-source/sdram_demo.kicad_pcb", tmp_path),
        "reviewed": _pre_adoption_board("reviewed-routing/sdram_demo.kicad_pcb", tmp_path),
    }
    for path in (boards["authored"], boards["reviewed"]):
        vias = _vias(path)
        for net, old, new, authored in module.VIA_MOVES:
            if path == boards["authored"] and not authored:
                continue
            assert math.dist(old, new) <= 0.05
            final = later.get((net, new), new)
            assert (net, final) in vias and (net, old) not in vias


def test_copper_gap_repair_is_applied_and_bounded(tmp_path):
    """#9091: both sources carry the repair, and it stays a small, on-grid edit.

    Verified on the pinned pre-adoption copper; the #8603 re-optimization
    replaced that geometry wholesale.
    """
    import hashlib
    import math

    module = _load_record("repair_copper_gap_9091")
    boards = {
        "authored": (
            _pre_adoption_board("authored-source/sdram_demo.kicad_pcb", tmp_path),
            module.PRE_REPAIR_SHA256[module.AUTHORED],
        ),
        "reviewed": (
            _pre_adoption_board("reviewed-routing/sdram_demo.kicad_pcb", tmp_path),
            module.PRE_REPAIR_SHA256[module.REVIEWED],
        ),
    }
    for path, pre_repair in boards.values():
        vias = _vias(path)
        for net, old, new, authored in module.VIA_MOVES:
            if path == boards["authored"][0] and not authored:
                continue
            assert math.dist(old, new) <= 0.09
            assert (net, new) in vias and (net, old) not in vias
        # Re-running the record on its own output must refuse, never re-apply.
        assert hashlib.sha256(path.read_bytes()).hexdigest() != pre_repair
    for _net, _layer, points, _ in module.TROMBONES:
        for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
            dx, dy = round(abs(x2 - x1), 4), round(abs(y2 - y1), 4)
            assert dx == 0 or dy == 0 or dx == dy


def test_copper_gap_repair_keeps_byte1_lengths(tmp_path):
    """#9091: the trombones restore exactly the loop length they replace."""
    from kicad_tools.analysis.trace_length import TraceLengthAnalyzer
    from kicad_tools.schema.pcb import PCB

    # Copper lengths of the reviewed artifact before the repair (main 89c2ee058),
    # vias excluded: the repair moves no via on these nets. Read from the
    # pinned pre-adoption board: the #8603 repair re-tuned byte 1 afterwards.
    reviewed = {"DQ13": 73.6572, "DQ15": 73.6558, "UDQM": 73.6411}
    board = PCB.load(str(_pre_adoption_board("reviewed-routing/sdram_demo.kicad_pcb", tmp_path)))
    analyzer = TraceLengthAnalyzer()
    for net, before in reviewed.items():
        assert abs(analyzer.analyze_net(board, net).total_length_mm - before) < 0.002, net
