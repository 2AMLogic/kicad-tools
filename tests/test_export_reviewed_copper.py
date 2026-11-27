"""Issue #10492: ``kct export --verify-reviewed-copper`` publishes only reviewed copper.

``kct export`` writes the manufacturer ``.kicad_dru`` and refills the zones so
the shipped copper obeys the rules that ship (#9987). A team that signed off
the saved copper had no way to say "publish only if the final copper equals
what I reviewed": the refill could shrink two inner planes by 96 and 124 mm²
and, with no ``--stackup-id``, the export rewrote the checked-out board.

The opt-in mode plots the saved board natively before anything is prepared,
prepares rules, refill, silk and artwork on a private working copy, and
compares the copper of the staged Gerber archive with that reference layer by
layer as geometry before anything is published. These tests run it through the
public ``kct export`` entry point on a four-layer fixture, so argument
forwarding is exercised, and pin the comparison itself on synthetic Gerber.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from kicad_tools.cli import main as cli_main
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.export.gerber import JLCPCB_PRESET, GerberExporter
from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
from kicad_tools.export.preflight import PreflightConfig
from kicad_tools.export.reviewed_copper import (
    REVIEWED_COPPER_TOLERANCE_MM2,
    ReviewedCopperExport,
    compare_copper,
    load_copper,
    preflight_result,
    refusal_message,
    reviewed_copper_record,
    summary_lines,
    unreadable_record,
)
from kicad_tools.validate.gerber_geometry import parse_gerber_geometry
from tests.test_export_zone_fill_rule_sync_9987 import _stale_fill_board
from tests.test_named_stackup import authored_pcb

needs_kicad_cli = pytest.mark.skipif(
    find_kicad_cli() is None, reason="Native KiCad CLI is not installed"
)

#: The issue's reproduction command, minus the board, the output directory and
#: the opt-in every test here adds.
_EXPORT_ARGS = (
    "--mfr",
    "jlcpcb",
    "--skip-preflight",
    "--bom-source",
    "pcb",
    "--no-report",
    "--no-auto-lcsc",
)

_LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
_MEMBERS = ("board-F_Cu.gtl", "board-In1_Cu.g1", "board-In2_Cu.g2", "board-B_Cu.gbl")
_FUNCTIONS = ("Copper,L1,Top", "Copper,L2,Inr", "Copper,L3,Inr", "Copper,L4,Bot")
_PCB = {"name": "board.kicad_pcb", "sha256": "0" * 64}

# A 1 mm square, in the 4.6 Gerber format (1 nm units).
_SQUARE = [(0, 0), (1_000_000, 0), (1_000_000, 1_000_000), (0, 1_000_000), (0, 0)]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _four_layer_board(path: Path, *, inner_zone: bool = False) -> Path:
    """A 20 x 20 mm four-layer board with two THT pads and a track on each face.

    With ``inner_zone`` the board also outlines a GND pour on ``In1.Cu`` that
    was never filled, so an export that fills it ships copper the saved board
    does not have.
    """
    zone = (
        '  (zone (net 1) (net_name "GND") (layer "In1.Cu") (hatch edge 0.5)\n'
        "    (priority 0)\n"
        "    (min_thickness 0.25)\n"
        "    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))\n"
        "    (polygon (pts (xy 2 2) (xy 18 2) (xy 18 18) (xy 2 18))))\n"
        if inner_zone
        else ""
    )
    path.write_text(
        """(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
    (36 "B.SilkS" user) (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user)
    (34 "B.Paste" user) (35 "F.Paste" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "GND") (net 2 "A")
  (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (footprint "TG" (layer "F.Cu") (at 4 4)
    (fp_text reference "U1" (at 0 -2.5) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
      (layers "*.Cu" "*.Mask") (net 1 "GND")))
  (footprint "TA" (layer "F.Cu") (at 10 10)
    (fp_text reference "U2" (at 0 -2.5) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
      (layers "*.Cu" "*.Mask") (net 2 "A")))
  (segment (start 10 10) (end 16 10) (width 0.3) (layer "F.Cu") (net 2) (uuid "s1"))
  (segment (start 10 10) (end 10 16) (width 0.3) (layer "B.Cu") (net 2) (uuid "s2"))
"""
        + zone
        + ")\n"
    )
    return path


def _tree(root: Path, *skip: Path) -> dict[str, tuple[str, int]]:
    """Every file under ``root`` outside ``skip``: relative name -> (sha256, mtime_ns)."""
    skipped = [path.resolve() for path in skip]
    state: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(path.resolve().is_relative_to(s) for s in skipped):
            continue
        stat = path.stat()
        state[path.relative_to(root).as_posix()] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.st_mtime_ns,
        )
    return state


def _export(board: Path, out: Path, *extra: str, preflight: bool = False) -> int:
    """``kct export --verify-reviewed-copper`` through the public entry point."""
    args = [arg for arg in _EXPORT_ARGS if not (preflight and arg == "--skip-preflight")]
    return cli_main(
        ["export", str(board), *args, "--verify-reviewed-copper", "-o", str(out), *extra]
    )


def _diagnostics(out: Path) -> Path:
    """The one attempt directory a refused export left beside the package."""
    attempts = sorted(
        p for p in (out.parent / f"{out.name}-reviewed-copper").iterdir() if p.is_dir()
    )
    assert len(attempts) == 1, attempts
    return attempts[0]


def _report(attempt: Path) -> dict:
    return json.loads((attempt / "reviewed-copper.json").read_text())


def _layers(report: dict) -> dict[str, dict]:
    return {record["layer"]: record for record in report["layers"]}


def _geometry(source: Path | zipfile.ZipFile, member: str):
    data = (
        source.read(member)
        if isinstance(source, zipfile.ZipFile)
        else (source / member).read_bytes()
    )
    return parse_gerber_geometry(data.decode()).geometry


def _ring_area() -> float:
    """Both 1.7 mm THT annular rings: all the copper the inner layers of the fixture save."""
    return 2 * math.pi * 0.85**2


# ---------------------------------------------------------------------------
# Synthetic Gerber: the comparison itself (no kicad-cli needed)
# ---------------------------------------------------------------------------


def _gerber(function: str, regions: list[list[tuple[int, int]]], **head: str) -> str:
    lines = [
        "%TF.GenerationSoftware,KiCad,Pcbnew,10.0.8*%",
        f"%TF.CreationDate,{head.get('date', '2026-01-01T00:00:00+00:00')}*%",
        f"%TF.FileFunction,{function}*%",
        "%TF.FilePolarity,Positive*%",
        "%FSLAX46Y46*%",
        "%MOMM*%",
        "G01*",
        "%ADD10C,0.1*%",
        "D10*",
    ]
    for points in regions:
        lines.append("G36*")
        x, y = points[0]
        lines.append(f"X{x}Y{y}D02*")
        for x, y in points[1:]:
            lines.append(f"X{x}Y{y}D01*")
        lines.append("G37*")
    lines.append("M02*")
    return "".join(line + "\n" for line in lines)


def _plot(directory: Path, members: dict[str, str]) -> Path:
    """A directory of loose plots, the way the reference is read."""
    directory.mkdir(parents=True)
    for name, text in members.items():
        (directory / name).write_text(text)
    return directory


def _archive(path: Path, members: dict[str, str]) -> Path:
    """A Gerber archive, the way the candidate is read."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


def _baseline() -> dict[str, str]:
    return {name: _gerber(_FUNCTIONS[index], [_SQUARE]) for index, name in enumerate(_MEMBERS)}


def _record(tmp_path: Path, reference: dict[str, str], candidate: dict[str, str], *, zipped=True):
    """Compare two plots of the fixture's four copper layers."""
    layers = load_copper(_plot(tmp_path / "reference", reference), _LAYERS)
    target = tmp_path / "candidate"
    other = load_copper(
        _archive(target, candidate) if zipped else _plot(target, candidate), _LAYERS
    )
    return reviewed_copper_record(reviewed_pcb=_PCB, records=compare_copper(layers, other))


def _square_moved(dx: int = 0, dy: int = 0, *, height: int = 1_000_000) -> list[tuple[int, int]]:
    return [
        (dx, dy),
        (1_000_000 + dx, dy),
        (1_000_000 + dx, height + dy),
        (dx, height + dy),
        (dx, dy),
    ]


def test_copper_that_moved_with_its_area_intact_changes_both_directions(tmp_path):
    moved = dict(_baseline())
    moved["board-In2_Cu.g2"] = _gerber("Copper,L3,Inr", [_square_moved(dx=1_000_000)])

    record = _record(tmp_path, _baseline(), moved)

    assert record["status"] == "changed"
    assert record["changed_layers"] == ["In2.Cu"]
    assert record["unreadable_layers"] == []
    assert record["tolerance_mm2"] == REVIEWED_COPPER_TOLERANCE_MM2
    assert record["reviewed_pcb"] == _PCB
    layers = _layers(record)
    assert [entry["layer"] for entry in record["layers"]] == list(_LAYERS)
    assert layers["In2.Cu"]["added_mm2"] == pytest.approx(1.0)
    assert layers["In2.Cu"]["removed_mm2"] == pytest.approx(1.0)
    for layer in ("F.Cu", "In1.Cu", "B.Cu"):
        assert layers[layer]["status"] == "identical"
        assert (layers[layer]["added_mm2"], layers[layer]["removed_mm2"]) == (0.0, 0.0)
    assert layers["In2.Cu"]["reference"] == {
        "member": "board-In2_Cu.g2",
        "sha256": hashlib.sha256(_baseline()["board-In2_Cu.g2"].encode()).hexdigest(),
    }

    message = refusal_message(
        record, {"proposed_pcb": "/keep/proposed/board.kicad_pcb", "report": "/keep/r.json"}
    )
    assert "In2.Cu +1.0000 / -1.0000 mm²" in message
    assert "package was withheld" in message
    assert "/keep/proposed/board.kicad_pcb" in message and "/keep/r.json" in message
    assert preflight_result(record).status == "FAIL"
    assert (
        summary_lines(record)[3].strip()
        == "In2.Cu   changed   +1.0000 / -1.0000 mm² added / removed"
    )


@pytest.mark.parametrize("variant", ["metadata", "start-vertex", "loose-directory"])
def test_metadata_and_starting_vertex_do_not_read_as_changed_copper(tmp_path, variant):
    reference = _baseline()
    candidate = dict(reference)
    if variant == "metadata":
        # Another plot time, other object attributes and comments.
        candidate["board-In1_Cu.g1"] = _gerber(
            "Copper,L2,Inr", [_SQUARE], date="2027-05-06T07:08:09+00:00"
        ).replace("G36*", '%TO.N,"VCC"*%\n%TD*%\nG04 #@!TF.MD,changed*\nG36*', 1)
    elif variant == "start-vertex":
        x, y = _SQUARE[1]
        candidate["board-In1_Cu.g1"] = _gerber("Copper,L2,Inr", [_SQUARE[1:] + [(x, y)]])
    record = _record(tmp_path, reference, candidate, zipped=variant != "loose-directory")

    assert record["status"] == "identical"
    assert record["changed_layers"] == []
    for entry in record["layers"]:
        assert entry["status"] == "identical", entry
        assert (entry["added_mm2"], entry["removed_mm2"]) == (0.0, 0.0)
    assert preflight_result(record).status == "OK"


@pytest.mark.parametrize(
    ("fault", "status"),
    [("missing", "missing"), ("duplicate", "ambiguous"), ("mislabelled", "unreadable")],
)
def test_a_layer_that_cannot_be_read_fails_closed(tmp_path, fault, status):
    candidate = _baseline()
    if fault == "missing":
        del candidate["board-In2_Cu.g2"]
    elif fault == "duplicate":
        candidate["copy-In2_Cu.gbr"] = candidate["board-In2_Cu.g2"]
    else:
        candidate["board-In2_Cu.g2"] = _gerber("Soldermask,L3,Top", [_SQUARE])

    record = _record(tmp_path, _baseline(), candidate)

    assert record["status"] == "unreadable"
    assert record["unreadable_layers"] == ["In2.Cu"]
    assert record["changed_layers"] == []
    layer = _layers(record)["In2.Cu"]
    assert layer["status"] == status
    assert layer["added_mm2"] is None and layer["removed_mm2"] is None
    assert layer["detail"]
    result = preflight_result(record)
    assert (result.status, result.name) == ("FAIL", "reviewed_copper")
    assert layer["detail"] in result.details
    assert "could not be compared" in refusal_message(record, None)


def test_unsupported_gerber_constructs_prevent_publication(tmp_path):
    candidate = dict(_baseline())
    candidate["board-F_Cu.gtl"] = candidate["board-F_Cu.gtl"].replace(
        "%ADD10C,0.1*%", "%ABroundrect*%"
    )

    record = _record(tmp_path, _baseline(), candidate)

    assert record["status"] == "unreadable"
    assert record["unreadable_layers"] == ["F.Cu"]
    assert "Unsupported extended command" in _layers(record)["F.Cu"]["detail"]


def test_layers_are_matched_by_file_function_even_when_renamed(tmp_path):
    """KiCad names a plot after the layer's user-facing name, which copper layers can change."""
    reference = _baseline()
    candidate = {
        name.replace("In1_Cu", "Ground plane").replace("B_Cu", "Bottom copper"): text
        for name, text in reference.items()
    }

    record = _record(tmp_path, reference, candidate)

    assert record["status"] == "identical"
    assert _layers(record)["In1.Cu"]["candidate"]["member"] == "board-Ground plane.g1"
    assert _layers(record)["B.Cu"]["candidate"]["member"] == "board-Bottom copper.gbl"


def test_untagged_artwork_falls_back_to_the_layer_file_name(tmp_path):
    reference = {
        name: re.sub(r"^%TF\..*", "", text, flags=re.M) for name, text in _baseline().items()
    }

    record = _record(tmp_path, reference, reference)

    assert record["status"] == "identical"
    assert all(
        entry["candidate"]["member"] == name
        for entry, name in zip(record["layers"], _MEMBERS, strict=True)
    )


def test_differences_at_or_below_the_tolerance_are_the_same_copper(tmp_path):
    candidate = dict(_baseline())
    candidate["board-B_Cu.gbl"] = _gerber("Copper,L4,Bot", [_square_moved(height=1_000_001)])

    sliver = _record(tmp_path, _baseline(), candidate)
    assert sliver["status"] == "identical"
    assert _layers(sliver)["B.Cu"]["added_mm2"] == pytest.approx(REVIEWED_COPPER_TOLERANCE_MM2)

    candidate["board-B_Cu.gbl"] = _gerber("Copper,L4,Bot", [_square_moved(height=1_000_010)])
    measurable = _record(tmp_path / "wider", _baseline(), candidate)
    assert measurable["status"] == "changed"
    assert _layers(measurable)["B.Cu"]["removed_mm2"] == 0.0


def test_an_unopenable_plot_is_unreadable_on_every_layer():
    record = unreadable_record(
        reviewed_pcb=_PCB, layers=_LAYERS, side="candidate archive", reason="not a zip"
    )

    assert record["status"] == "unreadable"
    assert record["unreadable_layers"] == list(_LAYERS)
    assert all(
        entry["detail"] == "candidate archive unreadable: not a zip" for entry in record["layers"]
    )


# ---------------------------------------------------------------------------
# The working copy (no kicad-cli needed)
# ---------------------------------------------------------------------------


def test_working_copy_mirrors_the_project_and_keeps_its_writable_files_private(tmp_path):
    project = tmp_path / "proj"
    board_dir = project / "board"
    board_dir.mkdir(parents=True)
    board = board_dir / "board.kicad_pcb"
    saved = b"(kicad_pcb (version 1))\n"
    board.write_bytes(saved)
    (board_dir / "board.kicad_pro").write_text("{}")
    (board_dir / "board.kicad_dru").write_text("(version 1)")
    (board_dir / "board.kicad_prl").write_text('{"board": {}}')
    (board_dir / "board.kicad_sch").write_text("(kicad_sch (version 1))")
    libs = board_dir / "libs"
    libs.mkdir()
    (libs / "lib.kicad_sym").write_text("(kicad_symbol_lib)")
    (project / "project.kct").write_text("title: x\n")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    stale = project / ".board.kicad_pcb.export-abc"
    stale.mkdir()
    (stale / "board.kicad_pcb").write_bytes(b"stale")
    out = project / "mfg"
    out.mkdir()
    (out / "manifest.json").write_text("{}")
    workspace = ReviewedCopperExport(board, project_root=project, output_dir=out)
    try:
        working = workspace.pcb
        assert working == workspace.dir / "board" / "board.kicad_pcb"
        assert workspace.snapshot.read_bytes() == saved
        assert working.read_bytes() == saved
        assert (working.parent / "board.kicad_dru").read_text() == "(version 1)"

        # What KiCad rewrites beside a board is private; a nested library shares its inode.
        for name in (
            "board.kicad_pcb",
            "board.kicad_pro",
            "board.kicad_dru",
            "board.kicad_prl",
            "board.kicad_sch",
        ):
            assert not (working.parent / name).samefile(board_dir / name), name
        assert (working.parent / "libs" / "lib.kicad_sym").samefile(libs / "lib.kicad_sym")
        assert (workspace.dir / "project.kct").read_text() == "title: x\n"
        assert not (workspace.dir / ".git").exists()
        assert not (workspace.dir / stale.name).exists()

        # The package, and the evidence of an earlier refusal, are not inputs.
        assert not (workspace.dir / "mfg").exists()
        assert not (workspace.dir / workspace.diagnostics_root.name).exists()

        # Preparing the copy never reaches the checkout.
        working.write_bytes(b"(kicad_pcb (version 2))\n")
        (working.parent / "board.kicad_prl").write_text('{"board": {"active_layer": 4}}')
        assert board.read_bytes() == saved
        assert (board_dir / "board.kicad_prl").read_text() == '{"board": {}}'

        # Explicit inputs inside the project resolve to their working copies.
        assert workspace.rebase(board_dir / "board.kicad_sch") == working.parent / "board.kicad_sch"
        elsewhere = tmp_path / "elsewhere.kicad_sch"
        assert workspace.rebase(elsewhere) == elsewhere
        assert workspace.rebase(None) is None
        assert workspace.identity == {
            "name": "board.kicad_pcb",
            "sha256": hashlib.sha256(saved).hexdigest(),
        }
        assert workspace.source_unchanged() is True
        board.write_bytes(b"(kicad_pcb (version 3))\n")
        assert workspace.source_unchanged() is False
        assert workspace.diagnostics_root == project / "mfg-reviewed-copper"
    finally:
        workspace.close()
    assert not workspace.dir.exists()
    assert not workspace.snapshot_dir.exists()


def test_working_copy_keeps_project_links_readable(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "sheet.kicad_sch").write_text("(kicad_sch)")
    project = tmp_path / "proj"
    project.mkdir()
    board = project / "board.kicad_pcb"
    board.write_bytes(b"(kicad_pcb)")
    (project / "root.kicad_sch").symlink_to(shared / "sheet.kicad_sch")
    inner = project / "inner"
    inner.mkdir()
    (inner / "lib.kicad_mod").write_text("(footprint)")
    (project / "lib.kicad_mod").symlink_to(inner / "lib.kicad_mod")
    (project / "library").symlink_to(inner, target_is_directory=True)
    workspace = ReviewedCopperExport(board, project_root=None, output_dir=tmp_path / "mfg")
    try:
        outside = workspace.dir / "root.kicad_sch"
        assert outside.is_symlink()
        assert outside.read_text() == "(kicad_sch)"
        assert outside.resolve() == (shared / "sheet.kicad_sch").resolve()
        inside = workspace.dir / "lib.kicad_mod"
        assert inside.read_text() == "(footprint)"
        assert inside.resolve().is_relative_to(workspace.dir)
        assert (workspace.dir / "library" / "lib.kicad_mod").read_text() == "(footprint)"
        assert (workspace.dir / "library").resolve().is_relative_to(workspace.dir)
    finally:
        workspace.close()


# ---------------------------------------------------------------------------
# Through ``kct export``, on the real kicad-cli
# ---------------------------------------------------------------------------


@needs_kicad_cli
def test_unfilled_inner_zone_that_gains_copper_withholds_the_package(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    board = _four_layer_board(tmp_path / "probe.kicad_pcb", inner_zone=True)
    saved = board.read_bytes()
    # The baseline, plotted by kicad-cli alone from a copy of the saved fixture: no refill.
    independent = tmp_path / "independent"
    independent.mkdir()
    shutil.copyfile(board, independent / board.name)
    subprocess.run(
        [
            str(find_kicad_cli()),
            "pcb",
            "export",
            "gerbers",
            str(independent / board.name),
            "--output",
            str(independent / "plot") + "/",
            "--layers",
            ",".join(_LAYERS),
            "--use-drill-file-origin",
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    before = _tree(tmp_path)
    out = tmp_path / "mfg"

    assert _export(board, out) == 1

    # Nothing was published, and the checked-out board and its directory are as they were.
    assert not (out / "manifest.json").exists()
    assert not (out / "kicad_project.zip").exists()
    assert not (out / "gerbers").exists()
    assert _tree(tmp_path, out, out.parent / f"{out.name}-reviewed-copper") == before

    # The diagnostic survives the scratch copies, and names the proposed geometry.
    attempt = _diagnostics(out)
    report = _report(attempt)
    assert report["schema"] == "kct.reviewed-copper.v1"
    assert report["status"] == "changed"
    assert report["reviewed_pcb"] == {
        "name": "probe.kicad_pcb",
        "sha256": hashlib.sha256(saved).hexdigest(),
    }
    assert report["tolerance_mm2"] == REVIEWED_COPPER_TOLERANCE_MM2
    layers = _layers(report)
    assert [entry["layer"] for entry in report["layers"]] == list(_LAYERS)
    assert report["changed_layers"] == ["In1.Cu"]
    assert layers["In1.Cu"]["added_mm2"] > 0
    assert layers["In1.Cu"]["removed_mm2"] == 0.0
    for layer in ("F.Cu", "In2.Cu", "B.Cu"):
        assert layers[layer]["status"] == "identical"

    # The reference is the saved copper; the figure is candidate minus reference.
    member = "probe-In1_Cu.g1"
    export_reference = _geometry(attempt / "reference", member)
    native_reference = _geometry(independent / "plot", member)
    assert export_reference.equals(native_reference)
    assert native_reference.area == pytest.approx(_ring_area(), rel=1e-3)
    with zipfile.ZipFile(attempt / "gerbers.zip") as archive:
        candidate = _geometry(archive, member)
    assert candidate.area > native_reference.area
    assert layers["In1.Cu"]["added_mm2"] == pytest.approx(
        candidate.area - native_reference.area, abs=REVIEWED_COPPER_TOLERANCE_MM2
    )
    assert (
        layers["In1.Cu"]["reference"]["sha256"]
        == hashlib.sha256((attempt / "reference" / member).read_bytes()).hexdigest()
    )

    proposed = Path(report["diagnostics"]["proposed_pcb"])
    assert proposed.is_file()
    assert b"filled_polygon" in proposed.read_bytes()
    rules = proposed.with_suffix(".kicad_dru")
    assert "kct fab floors" in rules.read_text()
    assert (attempt / "reviewed-copper.txt").read_text().count("In1.Cu") == 1
    error = capsys.readouterr().err
    assert "In1.Cu" in error and "withheld" in error
    assert str(proposed) in error and report["diagnostics"]["report"] in error


@needs_kicad_cli
def test_board_without_zones_publishes_when_every_copper_layer_matches(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    board = _four_layer_board(tmp_path / "probe.kicad_pcb")
    saved = board.read_bytes()
    before = _tree(tmp_path)
    out = tmp_path / "mfg"

    # With preflight on: the opt-in keeps the ordinary fabrication checks.
    assert _export(board, out, preflight=True) == 0

    for name in ("manifest.json", "kicad_project.zip", "gerbers/gerbers.zip", "bom_jlcpcb.csv"):
        assert (out / name).is_file(), name
    assert _tree(tmp_path, out) == before

    manifest = json.loads((out / "manifest.json").read_text())
    reviewed = manifest["reviewed_copper"]
    assert reviewed["status"] == "identical"
    assert reviewed["reviewed_pcb"] == {
        "name": "probe.kicad_pcb",
        "sha256": hashlib.sha256(saved).hexdigest(),
    }
    assert [entry["layer"] for entry in reviewed["layers"]] == list(_LAYERS)
    assert all(entry["status"] == "identical" for entry in reviewed["layers"])
    names = {entry["name"] for entry in manifest["preflight"]}
    assert {"reviewed_copper", "board_outline"} <= names
    assert (
        "Reviewed copper (verified before this package was published):"
        in (out / "README.txt").read_text()
    )

    # The manufacturer rules still ship, generated in the working copy.
    with zipfile.ZipFile(out / "kicad_project.zip") as archive:
        assert "kct fab floors" in archive.read("probe.kicad_dru").decode()
        assert "board" in json.loads(archive.read("probe.kicad_pro"))
    assert not board.with_suffix(".kicad_dru").exists()
    assert not board.with_suffix(".kicad_pro").exists()

    # The recorded digests are of a native plot of the saved board: under
    # SOURCE_DATE_EPOCH an independent plot reproduces them byte for byte.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    shutil.copyfile(board, scratch / board.name)
    GerberExporter(scratch / board.name).plot_saved(JLCPCB_PRESET.config, scratch / "plot")
    for entry in reviewed["layers"]:
        stem = entry["layer"].replace(".", "_")
        plotted = next(path for path in (scratch / "plot").iterdir() if f"-{stem}." in path.name)
        assert entry["reference"]["sha256"] == hashlib.sha256(plotted.read_bytes()).hexdigest()
        assert entry["candidate"]["sha256"] == entry["reference"]["sha256"]
    assert (
        "[ok] Reviewed copper: all 4 layer(s) equal the saved probe.kicad_pcb"
        in capsys.readouterr().out
    )


def _archive_with_moved_inner_copper(monkeypatch, suffix: str = "-In2_Cu.", shift: int = 2_000_000):
    """After plotting, move one inner layer's copper sideways inside the staged archive.

    The shift clears the 1.7 mm pads it moves, so no copper overlaps where it was.
    """
    coordinate = re.compile(r"^X(-?\d+)(Y-?\d+(?:I-?\d+J-?\d+)?D0[123]\*)$", re.M)
    plotted = GerberExporter._create_zip

    def create_zip(self, source_dir, zip_path, reproducible=None):
        plotted(self, source_dir, zip_path, reproducible)
        with zipfile.ZipFile(zip_path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        name = next(name for name in members if suffix in name)
        moved = coordinate.sub(lambda m: f"X{int(m[1]) + shift}{m[2]}", members[name].decode())
        assert moved != members[name].decode()
        members[name] = moved.encode()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member, data in members.items():
                archive.writestr(member, data)

    monkeypatch.setattr(GerberExporter, "_create_zip", create_zip)


@pytest.mark.parametrize("project_zip", [True, False], ids=["project-zip", "no-project-zip"])
@needs_kicad_cli
def test_refusal_leaves_the_previous_package_untouched(tmp_path, monkeypatch, project_zip):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    board = _four_layer_board(tmp_path / "probe.kicad_pcb")
    out = tmp_path / "mfg"
    extra = [] if project_zip else ["--no-project-zip"]

    assert _export(board, out, *extra) == 0
    published = _tree(out)
    assert ("kicad_project.zip" in published) is project_zip

    _archive_with_moved_inner_copper(monkeypatch)
    assert _export(board, out, *extra) == 1

    # Every file of the earlier package survives by name and by content.
    assert _tree(out) == published
    attempt = _diagnostics(out)
    report = _report(attempt)
    assert report["status"] == "changed"
    assert report["changed_layers"] == ["In2.Cu"]
    layer = _layers(report)["In2.Cu"]
    assert layer["added_mm2"] == pytest.approx(_ring_area(), rel=1e-3)
    assert layer["removed_mm2"] == pytest.approx(layer["added_mm2"], rel=1e-9)


@needs_kicad_cli
def test_stale_saved_pour_is_withheld_and_the_authored_rules_are_kept(tmp_path):
    """The reporter's case: the export's refill would ship less copper than was saved."""
    board = _stale_fill_board(tmp_path)
    before = _tree(tmp_path)
    out = tmp_path / "mfg"

    assert _export(board, out) == 1

    assert _tree(tmp_path, out, out.parent / f"{out.name}-reviewed-copper") == before
    assert "Zone clearance" not in board.with_suffix(".kicad_dru").read_text()
    report = _report(_diagnostics(out))
    assert report["changed_layers"] == ["F.Cu"]
    front = _layers(report)["F.Cu"]
    assert front["removed_mm2"] > 0
    assert front["added_mm2"] < front["removed_mm2"]
    assert _layers(report)["B.Cu"]["status"] == "identical"


@needs_kicad_cli
def test_named_stackup_export_keeps_its_prepared_sources_in_staging(tmp_path):
    """The named-stackup path otherwise writes prepared sources back to the checkout."""
    (tmp_path / "reviewed").mkdir()
    board = authored_pcb(tmp_path / "reviewed", "JLC04161H-3313")
    saved = board.read_bytes()
    before = _tree(tmp_path / "reviewed")
    out = tmp_path / "reviewed" / "mfg"

    assert _export(board, out, "--stackup-id", "JLC04161H-3313", "--no-silk-fix") == 0

    assert (out / "stackup-ordering.json").is_file()
    assert (
        json.loads((out / "stackup-ordering.json").read_text())["pcb_sha256"]
        == hashlib.sha256(saved).hexdigest()
    )
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["reviewed_copper"]["status"] == "identical"
    assert (
        manifest["reviewed_copper"]["reviewed_pcb"]["sha256"] == hashlib.sha256(saved).hexdigest()
    )
    # No rule file, no marker and no scratch directory beside the checked-out board.
    assert _tree(tmp_path / "reviewed", out) == before

    # The same export without the opt-in does write its prepared rules to the checkout.
    (tmp_path / "plain").mkdir()
    plain = authored_pcb(tmp_path / "plain", "JLC04161H-3313")
    assert (
        cli_main(
            [
                "export",
                str(plain),
                *_EXPORT_ARGS,
                "--stackup-id",
                "JLC04161H-3313",
                "--no-silk-fix",
                "-o",
                str(tmp_path / "plain" / "mfg"),
            ]
        )
        == 0
    )
    assert plain.with_suffix(".kicad_dru").is_file()


def test_no_gerbers_with_reviewed_copper_is_refused_before_any_output(tmp_path, capsys):
    board = _four_layer_board(tmp_path / "probe.kicad_pcb")
    before = _tree(tmp_path)
    out = tmp_path / "mfg"

    assert (
        cli_main(["export", str(board), "--no-gerbers", "--verify-reviewed-copper", "-o", str(out)])
        == 1
    )

    error = capsys.readouterr().err
    assert "--verify-reviewed-copper" in error and "--no-gerbers" in error
    assert not out.exists()
    assert _tree(tmp_path) == before


def test_library_refuses_requests_that_could_verify_nothing(tmp_path):
    board = _four_layer_board(tmp_path / "probe.kicad_pcb")
    before = _tree(tmp_path)
    common = {
        "verify_reviewed_copper": True,
        "preflight": PreflightConfig(skip_all=True),
        "output_dir": tmp_path / "mfg",
    }

    without_gerbers = ManufacturingPackage(
        board, manufacturer="jlcpcb", config=ManufacturingConfig(include_gerbers=False, **common)
    ).export()
    without_rules = ManufacturingPackage(
        board,
        manufacturer="jlcpcb",
        config=ManufacturingConfig(emit_drc_constraints=False, **common),
    ).export()

    assert not without_gerbers.success and "--no-gerbers" in without_gerbers.errors[0]
    assert not without_rules.success and "emit_drc_constraints" in without_rules.errors[0]
    assert not (tmp_path / "mfg").exists()
    assert _tree(tmp_path) == before


def test_missing_native_plotter_prevents_publication(tmp_path, monkeypatch, capsys):
    board = _four_layer_board(tmp_path / "probe.kicad_pcb")
    before = _tree(tmp_path)
    monkeypatch.setattr("kicad_tools.export.gerber.find_kicad_cli", lambda: None)
    out = tmp_path / "mfg"

    assert _export(board, out) == 1

    assert not out.exists()
    assert _tree(tmp_path) == before
    assert "could not be plotted for review" in capsys.readouterr().err


@needs_kicad_cli
def test_an_unopenable_candidate_archive_withholds_the_package(tmp_path, monkeypatch):
    board = _four_layer_board(tmp_path / "probe.kicad_pcb")
    out = tmp_path / "mfg"

    def broken_zip(self, source_dir, zip_path, reproducible=None):
        zip_path.write_bytes(b"PK\x03\x04 not an archive")

    monkeypatch.setattr(GerberExporter, "_create_zip", broken_zip)

    assert _export(board, out) == 1

    report = _report(_diagnostics(out))
    assert report["status"] == "unreadable"
    assert report["unreadable_layers"] == list(_LAYERS)
    assert "candidate archive unreadable" in report["layers"][0]["detail"]
    assert not (out / "manifest.json").exists()


@needs_kicad_cli
def test_a_board_saved_during_the_export_is_not_published(tmp_path, monkeypatch, capsys):
    board = _four_layer_board(tmp_path / "probe.kicad_pcb")
    out = tmp_path / "mfg"
    monkeypatch.setattr(ReviewedCopperExport, "source_unchanged", lambda self: False)

    assert _export(board, out) == 1

    assert not (out / "manifest.json").exists()
    assert "changed during manufacturing export" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Review round 3: complete layer table, copper inventory, mirrored layout, links
# ---------------------------------------------------------------------------


@needs_kicad_cli
def test_unquoted_inner_layer_names_are_still_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    board = _four_layer_board(tmp_path / "probe.kicad_pcb")
    board.write_text(
        board.read_text()
        .replace('(1 "In1.Cu" signal)', "(1 In1.Cu signal)")
        .replace('(2 "In2.Cu" signal)', "(2 In2.Cu signal)")
    )
    out = tmp_path / "mfg"

    assert _export(board, out, preflight=True) == 0

    reviewed = json.loads((out / "manifest.json").read_text())["reviewed_copper"]
    assert [entry["layer"] for entry in reviewed["layers"]] == list(_LAYERS)
    with zipfile.ZipFile(out / "gerbers" / "gerbers.zip") as archive:
        coppers = [n for n in archive.namelist() if re.search(r"-(F|B|In\d)_Cu\.", n)]
    assert len(coppers) == 4


def test_copper_outside_the_board_layers_is_not_identical(tmp_path):
    candidate = _baseline()
    candidate["old-In3_Cu.g3"] = _gerber("Copper,L5,Inr", [_SQUARE])
    record = _record(tmp_path, _baseline(), candidate, zipped=False)
    assert record["status"] == "unreadable"
    assert any(entry["layer"].startswith("unexpected copper:") for entry in record["layers"])


@pytest.mark.parametrize("function", ["Copper,L9,Inr", "Copper"])
def test_misdeclared_copper_metadata_is_refused_not_raised(tmp_path, function):
    candidate = _baseline()
    candidate[_MEMBERS[2]] = _gerber(function, [_SQUARE])
    record = _record(tmp_path, _baseline(), candidate)
    assert record["status"] == "unreadable"
    assert {e["layer"]: e["status"] for e in record["layers"]}["In2.Cu"] != "identical"


@needs_kicad_cli
def test_split_project_layout_keeps_archive_relative_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    (tmp_path / "pcb").mkdir()
    (tmp_path / "schematics").mkdir()
    board = _four_layer_board(tmp_path / "pcb" / "probe.kicad_pcb")
    (tmp_path / "schematics" / "probe.kicad_sch").write_text(
        '(kicad_sch (version 20240108) (generator eeschema) (paper "A4"))\n'
    )
    args = (
        "--sch",
        str(tmp_path / "schematics" / "probe.kicad_sch"),
        "--project-root",
        str(tmp_path),
    )
    plain = tmp_path / "plain"
    reviewed = tmp_path / "reviewed"
    assert cli_main(["export", str(board), *_EXPORT_ARGS, "-o", str(plain), *args]) == 0
    assert _export(board, reviewed, *args) == 0

    def members(out: Path) -> set[str]:
        with zipfile.ZipFile(out / "kicad_project.zip") as archive:
            return set(archive.namelist())

    assert members(reviewed) == members(plain)
    assert not any(".reviewed-" in name for name in members(reviewed))


def test_working_copy_resolves_external_relative_links(tmp_path):
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "power.kicad_sch").write_text("(kicad_sch)")
    project = tmp_path / "pcb"
    project.mkdir()
    board = project / "board.kicad_pcb"
    board.write_bytes(b"(kicad_pcb)")
    (project / "probe.kicad_sch").symlink_to("../shared/power.kicad_sch")
    workspace = ReviewedCopperExport(board, project_root=None, output_dir=tmp_path / "mfg")
    try:
        assert (workspace.dir / "probe.kicad_sch").read_text() == "(kicad_sch)"
    finally:
        workspace.close()


def test_destination_that_is_the_board_directory_keeps_the_project_inputs(tmp_path):
    project = tmp_path / "project"
    board_dir = project / "pcb"
    board_dir.mkdir(parents=True)
    board = board_dir / "board.kicad_pcb"
    board.write_bytes(b"(kicad_pcb (version 1))\n")
    (board_dir / "board.kicad_sch").write_text("(kicad_sch)")
    (board_dir / "sym-lib-table").write_text("(sym_lib_table)")
    (project / "VERSION.md").write_text("v1\n")
    workspace = ReviewedCopperExport(board, project_root=project, output_dir=board_dir)
    try:
        assert (workspace.dir / "pcb" / "board.kicad_sch").is_file()
        assert (workspace.dir / "pcb" / "sym-lib-table").is_file()
        assert (workspace.dir / "VERSION.md").is_file()
        assert not (workspace.dir / "pcb" / workspace.dir.name).exists()
    finally:
        workspace.close()


@needs_kicad_cli
@pytest.mark.parametrize("bom_source", ["pcb", "schematic"])
def test_destination_that_is_the_board_directory_keeps_archive_inputs(
    tmp_path, monkeypatch, bom_source
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    project = tmp_path / "project"
    (project / "pcb").mkdir(parents=True)
    board = _four_layer_board(project / "pcb" / "probe.kicad_pcb")
    sch = project / "pcb" / "probe.kicad_sch"
    sch.write_text('(kicad_sch (version 20240108) (generator eeschema) (paper "A4"))\n')
    (project / "VERSION.md").write_text("v1\n")
    args = ["--bom-source", bom_source, "--sch", str(sch), "--project-root", str(project)]
    base = [arg for arg in _EXPORT_ARGS if arg not in ("--bom-source", "pcb")]
    out = project / "pcb"
    assert cli_main(["export", str(board), *base, "-o", str(tmp_path / "plain"), *args]) == 0
    code = cli_main(
        ["export", str(board), *base, "--verify-reviewed-copper", "-o", str(out), *args]
    )
    assert code == 0

    def members(path: Path) -> set[str]:
        with zipfile.ZipFile(path / "kicad_project.zip") as archive:
            return set(archive.namelist())

    assert "probe.kicad_sch" in members(out) or any(
        n.endswith("probe.kicad_sch") for n in members(out)
    )
    assert members(out) >= members(tmp_path / "plain")
