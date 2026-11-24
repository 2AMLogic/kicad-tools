"""Content-derived controls for independent saved-copper verification."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd as cmd


def options(tmp_path):
    source = tmp_path / "output"
    source.mkdir()
    pcb = source / "demo.kicad_pcb"
    pcb.write_text(json.dumps({"F.Cu": 1.0}))
    project = source / "demo.kicad_pro"
    project.write_text('{"rule": "must survive"}')
    (source / "demo.kicad_dru").write_text("(version 1)")
    return cmd.ReadinessOptions(
        tmp_path,
        pcb,
        None,
        project,
        "jlcpcb",
        "pcb_only",
        source / "manufacturing",
        source / "readiness",
    )


@pytest.mark.parametrize(
    "filled,expected",
    [
        ({"F.Cu": 2.0}, cmd.FAILED),
        ({"F.Cu": 1.0}, cmd.PASSED),
        ({"F.Cu": 1.005}, cmd.PASSED),
        ({"F.Cu": 1.01}, cmd.PASSED),
        ({"F.Cu": 1.0100001}, cmd.FAILED),
        ({"B.Cu": 1.0}, cmd.FAILED),
    ],
)
def test_independent_refill_preserves_saved_source_and_project_context(tmp_path, filled, expected):
    opts = options(tmp_path)
    before = opts.pcb.read_bytes()
    seen = []

    def refill(pcb):
        assert pcb != opts.pcb
        assert pcb.read_bytes() == before
        assert pcb.with_suffix(".kicad_pro").read_bytes() == opts.project.read_bytes()
        assert pcb.with_suffix(".kicad_dru").read_text() == "(version 1)"
        seen.append(pcb)
        pcb.write_text(json.dumps(filled))
        return cmd.EngineRun(True)

    engines = replace(
        cmd.Engines(),
        refill=refill,
        fill_areas=lambda pcb: json.loads(pcb.read_text()),
        kicad_cli_version=lambda: "test engine",
    )
    result = cmd._gate_refill(opts, engines)
    assert result.status == expected
    assert len(seen) == 1
    assert opts.pcb.read_bytes() == before
    evidence = json.loads((opts.evidence_dir / "fill-consistency.json").read_text())
    assert evidence["saved_areas_mm2"] == {"F.Cu": 1.0}
    assert evidence["refilled_areas_mm2"] == filled
    assert evidence["engine"] == "test engine"
    assert evidence["saved_sha256"] == cmd._sha256_file(opts.pcb)
    assert (opts.evidence_dir / "fill-saved" / opts.pcb.name).read_bytes() == before


def test_fill_evidence_follows_the_selected_revision_on_rerun(tmp_path):
    opts = options(tmp_path)
    source = opts.pcb.parent
    other = source / "demo_v2.kicad_pcb"
    other.write_text(opts.pcb.read_text())
    other.with_suffix(".kicad_pro").write_text('{"revision": 2}')
    other.with_suffix(".kicad_dru").write_text("(version 2)")
    shared = {
        "sym-lib-table",
        "fp-lib-table",
        "Board.pretty/pad.kicad_mod",
        "footprints/Local.pretty/chip.kicad_mod",
    }
    for name in shared:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    opts.pcb.with_suffix(".kicad_dru").unlink()
    engines = replace(
        cmd.Engines(),
        refill=lambda pcb: cmd.EngineRun(True),
        fill_areas=lambda pcb: json.loads(pcb.read_text()),
        kicad_cli_version=lambda: "test engine",
    )

    for pcb in (other, opts.pcb):
        project = pcb.with_suffix(".kicad_pro")
        result = cmd._gate_refill(replace(opts, pcb=pcb, project=project), engines)
        assert result.status == cmd.PASSED
        expected = shared | {pcb.name, project.name}
        if pcb == other:
            expected.add("demo_v2.kicad_dru")
        for name in ("fill-saved", "fill-refilled"):
            snapshot = opts.evidence_dir / name
            assert {
                p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file()
            } == expected
            for relative in expected:
                assert (snapshot / relative).read_bytes() == (source / relative).read_bytes()
        evidence = json.loads((opts.evidence_dir / "fill-consistency.json").read_text())
        assert set(evidence["context_sha256"]) == expected


@pytest.mark.parametrize(
    "uri,in_parent",
    [
        ("${KIPRJMOD}/vendor/Shared Parts.pretty", False),
        ("vendor/Shared Parts.pretty", False),
        ("${KIPRJMOD}/../vendor/Shared Parts.pretty", True),
    ],
)
def test_fill_snapshots_keep_the_library_at_its_table_uri(tmp_path, uri, in_parent):
    opts = options(tmp_path)
    library_root = opts.board_dir if in_parent else opts.pcb.parent
    member = Path("vendor/Shared Parts.pretty/J1.kicad_mod")
    library = library_root / member
    library.parent.mkdir(parents=True)
    library.write_text('(footprint "J1")')
    table = opts.pcb.parent / "fp-lib-table"
    table.write_text(f'(fp_lib_table (lib (name "shared")(type "KiCad")(uri "{uri}")))')
    (library_root / "vendor" / "different-board.kicad_pro").write_text("{}")
    if in_parent:
        opts = replace(opts, project_root=library_root)
    engines = replace(
        cmd.Engines(),
        refill=lambda pcb: cmd.EngineRun(True),
        fill_areas=lambda pcb: json.loads(pcb.read_text()),
        kicad_cli_version=lambda: "test engine",
    )

    result = cmd._gate_refill(opts, engines)
    assert result.status == cmd.PASSED
    receipt = json.loads((opts.evidence_dir / "fill-consistency.json").read_text())
    assert receipt["context_sha256"][member.as_posix()] == cmd._sha256_file(library)
    for name in ("saved_pcb", "refilled_pcb"):
        staged = opts.board_dir / receipt[name]
        assert staged.read_bytes() == opts.pcb.read_bytes()
        assert (staged.parent / table.name).read_bytes() == table.read_bytes()
        target = staged.parent / uri.removeprefix("${KIPRJMOD}/") / library.name
        assert target.read_bytes() == library.read_bytes()
        assert not (target.parent.parent / "different-board.kicad_pro").exists()


@pytest.mark.parametrize("routed_project", [False, True])
def test_routed_fill_snapshot_keeps_the_paired_project(tmp_path, routed_project):
    opts = options(tmp_path)
    pcb = opts.pcb.with_stem("demo_routed")
    pcb.write_bytes(opts.pcb.read_bytes())
    pcb.with_suffix(".kicad_dru").write_text("(version 2)")
    project = opts.project
    if routed_project:
        project = pcb.with_suffix(".kicad_pro")
        project.write_text('{"routed": true}')
    saved, sources = cmd._copy_fill_context(pcb, tmp_path / "snapshot")
    snapshot = saved.parent
    assert sources["demo_routed.kicad_pro"] == project
    assert {p.name for p in snapshot.iterdir()} == {
        pcb.name,
        "demo_routed.kicad_dru",
        "demo_routed.kicad_pro",
    }
    assert (snapshot / "demo_routed.kicad_pro").read_bytes() == project.read_bytes()
    assert (snapshot / "demo_routed.kicad_dru").read_text() == "(version 2)"


def test_native_fill_uses_the_selected_project_with_each_supported_name(tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("native KiCad CLI is not installed")
    board00 = Path(__file__).resolve().parents[1] / "boards/00-simple-led/output"
    source = board00 / "simple_led_routed.kicad_pcb"
    default_fill = tmp_path / "default.kicad_pcb"
    shutil.copyfile(source, default_fill)
    engines = cmd.Engines()
    assert engines.refill(default_fill).ok
    project_data = json.loads(source.with_suffix(".kicad_pro").read_bytes())
    project_data["board"]["design_settings"]["rules"]["min_copper_edge_clearance"] = 3.0
    rules = '(version 1)\n(rule "board edge" (constraint edge_clearance (min 3mm)))\n'
    areas = []
    for directory, pcb_stem, project_stem, sibling in (
        ("exact", "board_routed", "board_routed", None),
        ("routed", "board_routed", "board", None),
        ("sole", "shipping_v3", "shared", None),
        ("sibling-first", "shipping_v3", "shared", "a_other"),
        ("sibling-last", "shipping_v3", "shared", "z_other"),
    ):
        target = tmp_path / directory
        target.mkdir()
        pcb = target / f"{pcb_stem}.kicad_pcb"
        shutil.copyfile(default_fill, pcb)
        project = target / f"{project_stem}.kicad_pro"
        project.write_text(json.dumps(project_data))
        project.with_suffix(".kicad_dru").write_text(rules)
        if sibling:
            other = target / f"{sibling}.kicad_pcb"
            shutil.copyfile(source, other)
            other.with_suffix(".kicad_pro").write_text("{}")
            other.with_suffix(".kicad_dru").write_text("(version 1)\n")
        args = cmd.build_parser().parse_args(
            [str(pcb), "--generate", "--mfr", "jlcpcb", "--pcb-only"]
        )
        opts, error = cmd.resolve_options(args)
        assert opts is not None and opts.project == project, error
        result = cmd._gate_refill(opts, engines)
        evidence = json.loads((opts.evidence_dir / "fill-consistency.json").read_bytes())
        assert evidence["refill_returncode"] == 0
        assert result.status == cmd.FAILED, (directory, result.detail)
        assert evidence["deltas_mm2"]["F.Cu"] > 1
        areas.append(evidence["refilled_areas_mm2"])
        for suffix in (".kicad_pro", ".kicad_dru"):
            staged_name = pcb.with_suffix(suffix).name
            source_file = project.with_suffix(suffix)
            assert evidence["context_sha256"][staged_name] == cmd._sha256_file(source_file)
            assert evidence["rule_sources"][staged_name] == cmd._rel(opts, source_file)
        assert pcb.read_bytes() == default_fill.read_bytes()
    assert all(area == areas[0] for area in areas)


@pytest.mark.parametrize("unpaired", [(), ("shared",), ("shared", "alternate")])
def test_project_fallback_requires_one_project_available_to_the_board(tmp_path, unpaired):
    pcb = tmp_path / "shipping_v3.kicad_pcb"
    pcb.touch()
    # The sibling can use its original project through the supported _routed alias.
    (tmp_path / "a_other_routed.kicad_pcb").touch()
    (tmp_path / "a_other.kicad_pro").write_text("{}")
    for name in unpaired:
        (tmp_path / f"{name}.kicad_pro").write_text("{}")
    args = cmd.build_parser().parse_args([str(pcb), "--verify", "--mfr", "jlcpcb", "--pcb-only"])
    opts, error = cmd.resolve_options(args)
    if len(unpaired) == 1:
        assert error is None and opts is not None
        assert opts.project == tmp_path / "shared.kicad_pro"
    else:
        assert opts is None
        assert pcb.name in error and ".kicad_pro" in error


@pytest.mark.parametrize("bad", [{"F.Cu": float("nan")}, {"F.Cu": -1}, [], {"F.Cu": "x"}])
def test_malformed_fill_measurements_cannot_pass(tmp_path, bad):
    opts = options(tmp_path)
    engines = replace(
        cmd.Engines(),
        refill=lambda pcb: cmd.EngineRun(True),
        fill_areas=lambda pcb: bad,
        kicad_cli_version=lambda: "test",
    )
    assert cmd._gate_refill(opts, engines).status == cmd.FAILED


def test_failed_refill_cannot_modify_source(tmp_path):
    opts = options(tmp_path)
    before = opts.pcb.read_bytes()

    def fail(pcb):
        pcb.write_text("corrupt")
        return cmd.EngineRun(False, "refill failed", 2)

    engines = replace(
        cmd.Engines(),
        refill=fail,
        fill_areas=lambda pcb: json.loads(pcb.read_text()),
        kicad_cli_version=lambda: "test",
    )
    assert cmd._gate_refill(opts, engines).status == cmd.NOT_RUN
    assert opts.pcb.read_bytes() == before
