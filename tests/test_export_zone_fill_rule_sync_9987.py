"""Issue #9987: ``kct export`` ships a saved zone fill that obeys the rules it writes.

``kct export`` writes the manufacturer ``.kicad_dru`` next to the source board
before preflight and plotting, and since #8937 that block carries one
zone-scoped clearance rule per pour.  KiCad plots the copper saved in the
board, so when those rules are new -- the Board 06 recipe fills its pours
before export adds five 0.30 mm rules -- the Gerbers, the archived PCB and
the ``.kicad_dru`` in the project ZIP disagree: 499 native clearance errors
on the saved board, none after a refill.  The saved-fill guard in
``export/gerber.py`` cannot see that, because a stale pour is still
connected.

The exporter now persists a native refill at the point the final rules
become effective, and publishes nothing when a required fill cannot be
produced.  The 20 x 20 mm two-pad board from
``tests/test_zone_clearance_floor_8937.py`` reproduces the defect through the
public exporter without the Board 06 recipe.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from kicad_tools.cli import runner
from kicad_tools.cli.export_cmd import main as export_main
from kicad_tools.cli.runner import KiCadCLIResult, find_kicad_cli, run_refill_zones
from kicad_tools.core.atomic_write import SourceChangedError
from kicad_tools.export import manufacturing
from kicad_tools.export.gerber import GerberExporter, _pcb_has_filled_zones
from kicad_tools.export.manufacturing import (
    ManufacturingConfig,
    ManufacturingPackage,
    _zone_fill_rule_state,
)
from kicad_tools.export.preflight import PreflightConfig
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from tests.test_named_stackup import authored_pcb
from tests.test_zone_clearance_floor_8937 import _fixture, _pour_to_pad_gap, _project

#: The issue's reproduction command, minus the board and the output directory.
_EXPORT_ARGS = (
    "--mfr",
    "jlcpcb",
    "--skip-preflight",
    "--bom-source",
    "pcb",
    "--no-report",
    "--no-auto-lcsc",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _zoned_board(directory: Path) -> Path:
    """The 8937 two-pad board with its 0.30 mm GND pour, in ``directory``."""
    return _fixture(directory / "probe.kicad_pcb", zone_clearance=0.3)


def _write_sidecars(board: Path) -> None:
    """The project and DRU ``kct export`` itself writes for this board."""
    rules = get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1.0)
    board.with_suffix(".kicad_pro").write_text(json.dumps(_project(default_clearance=0.1)))
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb", layers=2, copper_oz=1.0)


def _persist_native_fill(board: Path) -> None:
    result = run_refill_zones(board, find_kicad_cli(), require_save=True)
    assert result.success, result.stderr


def _stale_rules_board(directory: Path) -> Path:
    """Rules first, then the zone: the ``.kicad_dru`` predates the pour.

    The sidecars are written while the board has no zone, so the DRU carries
    no ``Zone clearance`` rule and the board has no saved fill yet.
    """
    board = _zoned_board(directory)
    zoned = board.read_text()
    board.write_text(zoned[: zoned.index("  (zone (net 1)")] + ")\n")
    _write_sidecars(board)
    assert "Zone clearance" not in board.with_suffix(".kicad_dru").read_text()
    board.write_text(zoned)
    return board


def _stale_fill_board(directory: Path) -> Path:
    """``_stale_rules_board`` after KiCad filled the pour under those rules."""
    board = _stale_rules_board(directory)
    _persist_native_fill(board)
    assert _pour_to_pad_gap(board) < 0.2
    return board


def _with_saved_fill_text(board: Path) -> Path:
    """A text-only saved pour, for the tests that must not need kicad-cli."""
    text = board.read_text().rstrip()
    assert text.endswith(")")
    board.write_text(
        text[:-1]
        + '  (zone (net 1) (net_name "GND") (layer "F.Cu") (min_thickness 0.25)\n'
        + '    (filled_polygon (layer "F.Cu") (pts (xy 3 3) (xy 4 3) (xy 4 4))))\n'
        + ")\n"
    )
    assert _pcb_has_filled_zones(board)
    return board


def _config(out: Path, **overrides) -> ManufacturingConfig:
    """A KiCad-free bundle: project ZIP only, no preflight, no silk pass."""
    options: dict = {
        "output_dir": out,
        "bom_source": "pcb",
        "include_bom": False,
        "include_pnp": False,
        "include_gerbers": False,
        "include_report": False,
        "include_project_zip": True,
        "silk_fix": False,
        "preflight": PreflightConfig(skip_all=True),
    }
    options.update(overrides)
    return ManufacturingConfig(**options)


def _is_scratch_refill(pcb_path) -> bool:
    """The exporter's comparison refill runs on a scratch copy, never the board."""
    return "kct-export-zone-refill-" in str(pcb_path)


def _spy_on_refill(monkeypatch) -> list[tuple[Path, bool]]:
    """Record each native refill, passing it through to the real runner."""
    calls: list[tuple[Path, bool]] = []
    real = runner.run_refill_zones

    def spy(pcb_path, kicad_cli=None, *, require_save=False):
        if not _is_scratch_refill(pcb_path):
            calls.append((Path(pcb_path), require_save))
        return real(pcb_path, kicad_cli, require_save=require_save)

    monkeypatch.setattr(runner, "run_refill_zones", spy)
    return calls


def _forbid_refill(monkeypatch) -> list[tuple[Path, bool]]:
    """Record a native refill and fail: these tests must not reach kicad-cli."""
    calls: list[tuple[Path, bool]] = []
    real = runner.run_refill_zones

    def spy(pcb_path, kicad_cli=None, *, require_save=False):
        if _is_scratch_refill(pcb_path):
            return real(pcb_path, kicad_cli, require_save=require_save)
        calls.append((Path(pcb_path), require_save))
        pytest.fail("export attempted a native zone refill")

    monkeypatch.setattr(runner, "run_refill_zones", spy)
    return calls


def _native_drc(board: Path, *, refill: bool) -> dict:
    """``kicad-cli pcb drc`` on ``board``; a refill is in memory only."""
    report = board.with_name(f"{board.stem}.{'refilled' if refill else 'saved'}.drc.json")
    extra = ["--refill-zones"] if refill else []
    proc = subprocess.run(
        [
            str(find_kicad_cli()),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            *extra,
            str(board),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode in (0, 1, 5), proc.stderr
    return json.loads(report.read_text())


def _zone_clearance_violations(report: dict) -> list[dict]:
    return [v for v in report["violations"] if v["type"] == "clearance"]


def _copper_members(zip_path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(zip_path) as zf:
        return {name: zf.read(name) for name in zf.namelist() if name.endswith((".gtl", ".gbl"))}


# ---------------------------------------------------------------------------
# The defect, through the public exporter (real kicad-cli)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
def test_export_persists_the_fill_its_new_zone_rules_require(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    board = _stale_fill_board(tmp_path)
    refills = _spy_on_refill(monkeypatch)
    # The stale board as it stood before export, for an independent refill
    # under the delivered rules once export has written them.
    independent = tmp_path / "independent"
    independent.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copyfile(board.with_suffix(suffix), independent / board.with_suffix(suffix).name)
    out = tmp_path / "mfg"

    assert export_main([str(board), *_EXPORT_ARGS, "-o", str(out)]) == 0

    # The rule export wrote is in force on the copper it shipped.
    dru = board.with_suffix(".kicad_dru")
    assert "Zone clearance - GND (F.Cu)" in dru.read_text()
    assert _pour_to_pad_gap(board) >= 0.3 - 0.005
    assert [(path.resolve(), save) for path, save in refills] == [(board.resolve(), True)]

    # Saved and refilled native DRC agree on the shipped bytes: no zone
    # clearance error and no open on either view.
    for refill in (False, True):
        report = _native_drc(board, refill=refill)
        assert _zone_clearance_violations(report) == []
        assert report["unconnected_items"] == []

    # The archived board is the shipped board, filed with its own rules.
    shipped = board.read_bytes()
    archive = out / "kicad_project.zip"
    with zipfile.ZipFile(archive) as zf:
        assert zf.read(board.name) == shipped
        assert zf.read(dru.name) == dru.read_bytes()
    archived = json.loads((out / "manifest.json").read_text())["files"]["kicad_project.zip"]
    assert archived["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert archived["size"] == archive.stat().st_size

    # Copper plots equal the plots of an independent refill under the delivered rules.
    for suffix in (".kicad_pro", ".kicad_dru"):
        shutil.copyfile(board.with_suffix(suffix), independent / board.with_suffix(suffix).name)
    _persist_native_fill(independent / board.name)
    assert _pour_to_pad_gap(independent / board.name) == pytest.approx(
        _pour_to_pad_gap(board), abs=1e-6
    )
    expected = tmp_path / "expected"
    GerberExporter(independent / board.name).export_for_manufacturer("jlcpcb", expected)
    delivered = _copper_members(out / "gerbers" / "gerbers.zip")
    assert any(name.endswith("-F_Cu.gtl") for name in delivered)
    assert delivered == _copper_members(expected / "gerbers.zip")


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
def test_export_refills_a_clean_fill_left_wider_than_the_delivered_rules(tmp_path, monkeypatch):
    """A fill made under a stricter rule passes saved DRC but is not what the rules plot."""
    board = _fixture(tmp_path / "probe.kicad_pcb", zone_clearance=0.6)
    _write_sidecars(board)
    _persist_native_fill(board)
    wide_gap = _pour_to_pad_gap(board)
    assert wide_gap >= 0.55
    # The pour is edited down to 0.30 mm; the saved fill and its DRU predate it.
    text = board.read_text()
    assert "(clearance 0.6)" in text
    board.write_text(text.replace("(clearance 0.6)", "(clearance 0.3)"))
    board.with_suffix(".kicad_dru").unlink()
    assert _pour_to_pad_gap(board) == pytest.approx(wide_gap)
    refills = _spy_on_refill(monkeypatch)

    assert export_main([str(board), *_EXPORT_ARGS, "-o", str(tmp_path / "mfg")]) == 0

    assert [(path.resolve(), save) for path, save in refills] == [(board.resolve(), True)]
    assert _pour_to_pad_gap(board) == pytest.approx(0.3, abs=0.02)


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
def test_export_reuses_the_same_pcb_but_refills_a_restored_pcb(tmp_path, monkeypatch):
    board = _stale_fill_board(tmp_path)
    stale = board.read_bytes()
    refills = _spy_on_refill(monkeypatch)
    assert export_main([str(board), *_EXPORT_ARGS, "-o", str(tmp_path / "first")]) == 0
    assert len(refills) == 1

    shipped = board.read_bytes()
    shipped_mtime_ns = board.stat().st_mtime_ns
    dru = board.with_suffix(".kicad_dru").read_bytes()

    assert export_main([str(board), *_EXPORT_ARGS, "-o", str(tmp_path / "second")]) == 0

    assert len(refills) == 1
    assert board.read_bytes() == shipped
    assert board.stat().st_mtime_ns == shipped_mtime_ns
    assert board.with_suffix(".kicad_dru").read_bytes() == dru
    with zipfile.ZipFile(tmp_path / "second" / "kicad_project.zip") as zf:
        assert zf.read(board.name) == shipped

    # A git restore replaces only the tracked PCB. The generated rules and
    # marker from the successful export still describe the previous bytes.
    board.write_bytes(stale)
    assert export_main([str(board), *_EXPORT_ARGS, "-o", str(tmp_path / "restored")]) == 0
    assert len(refills) == 2
    assert _pour_to_pad_gap(board) >= 0.3 - 0.005
    assert board.with_suffix(".kicad_dru").read_bytes() == dru
    with zipfile.ZipFile(tmp_path / "restored" / "kicad_project.zip") as zf:
        assert zf.read(board.name) == board.read_bytes()


@pytest.mark.skipif(find_kicad_cli() is None, reason="Native KiCad CLI is not installed")
def test_current_fill_without_a_marker_preserves_board_bytes(tmp_path, monkeypatch):
    """Native saved-board DRC can qualify copper with no prior export record."""
    board = _zoned_board(tmp_path)
    _write_sidecars(board)
    _persist_native_fill(board)
    dru = board.with_suffix(".kicad_dru").read_bytes()
    original = board.read_bytes()
    mtime_ns = board.stat().st_mtime_ns
    refills = _forbid_refill(monkeypatch)

    for output in ("first", "again"):
        assert export_main([str(board), *_EXPORT_ARGS, "-o", str(tmp_path / output)]) == 0
        assert board.read_bytes() == original
        assert board.stat().st_mtime_ns == mtime_ns
        assert board.with_suffix(".kicad_dru").read_bytes() == dru
    assert refills == []


def _fake_refill(monkeypatch, *, fail_first: bool = False) -> list[Path]:
    """A refill that appends a newline to the board; optionally fails once."""
    calls: list[Path] = []

    def refilling(pcb_path, kicad_cli=None, *, require_save=False):
        assert require_save
        calls.append(Path(pcb_path))
        if fail_first and len(calls) == 1:
            return KiCadCLIResult(success=False, stderr="boom")
        Path(pcb_path).write_bytes(Path(pcb_path).read_bytes() + b"\n")
        return KiCadCLIResult(success=True, output_path=Path(pcb_path))

    monkeypatch.setattr(manufacturing, "_saved_zone_fill_matches_native_refill", lambda *_: False)
    monkeypatch.setattr(runner, "run_refill_zones", refilling)
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    return calls


def test_failed_refill_then_repeat_export_still_refills(tmp_path, monkeypatch):
    board = _with_saved_fill_text(_stale_rules_board(tmp_path))
    calls = _fake_refill(monkeypatch, fail_first=True)

    first = ManufacturingPackage(
        board, manufacturer="jlcpcb", config=_config(tmp_path / "one")
    ).export()
    assert not first.success
    # The rules are now on disk beside the old copper.
    assert "Zone clearance - GND (F.Cu)" in board.with_suffix(".kicad_dru").read_text()

    second = ManufacturingPackage(
        board, manufacturer="jlcpcb", config=_config(tmp_path / "two")
    ).export()
    assert second.success, second.errors
    assert len(calls) == 2
    with zipfile.ZipFile(tmp_path / "two" / "kicad_project.zip") as zf:
        assert zf.read(board.name) == board.read_bytes()

    # A previously successful, unchanged export does not refill again.
    third = ManufacturingPackage(
        board, manufacturer="jlcpcb", config=_config(tmp_path / "three")
    ).export()
    assert third.success, third.errors
    assert len(calls) == 2


def test_parts_only_export_then_full_export_refills(tmp_path, monkeypatch):
    board = _with_saved_fill_text(_stale_rules_board(tmp_path))
    calls = _fake_refill(monkeypatch)

    parts = ManufacturingPackage(
        board,
        manufacturer="jlcpcb",
        config=_config(tmp_path / "parts", include_project_zip=False, include_gerbers=False),
    ).export()
    assert parts.success, parts.errors
    assert calls == []

    full = ManufacturingPackage(
        board, manufacturer="jlcpcb", config=_config(tmp_path / "full")
    ).export()
    assert full.success, full.errors
    assert len(calls) == 1


def test_named_stackup_without_a_refill_keeps_its_original_binding(tmp_path, monkeypatch):
    """A board edit during rule emission must not be certified (no refill ran)."""
    board = authored_pcb(tmp_path, "JLC04161H-3313")
    original_hash = hashlib.sha256(board.read_bytes()).hexdigest()
    calls = _fake_refill(monkeypatch)
    real_write = ManufacturingPackage._write_drc_constraints

    def racing(self, result, **kwargs):
        real_write(self, result, **kwargs)
        board.write_bytes(board.read_bytes() + b"\n")

    monkeypatch.setattr(ManufacturingPackage, "_write_drc_constraints", racing)
    config = _config(tmp_path / "mfg", stackup_id="JLC04161H-3313")
    result = ManufacturingPackage(board, manufacturer="jlcpcb", config=config).export()

    assert calls == []
    if result.stackup_path is not None and result.stackup_path.exists():
        record = json.loads(result.stackup_path.read_text())
        assert record["pcb_sha256"] == original_hash


def test_named_stackup_refill_refuses_a_board_that_changed_before_it(tmp_path, monkeypatch):
    board = _with_saved_fill_text(authored_pcb(tmp_path, "JLC04161H-3313"))
    calls = _fake_refill(monkeypatch)
    real_write = ManufacturingPackage._write_drc_constraints

    def racing(self, result, **kwargs):
        real_write(self, result, **kwargs)
        board.write_bytes(board.read_bytes() + b"\n")

    monkeypatch.setattr(ManufacturingPackage, "_write_drc_constraints", racing)
    config = _config(tmp_path / "mfg", stackup_id="JLC04161H-3313")
    result = ManufacturingPackage(board, manufacturer="jlcpcb", config=config).export()

    assert not result.success
    assert any("PCB changed during manufacturing export" in e for e in result.errors)
    assert calls == []


def test_named_stackup_refuses_a_pcb_edited_during_saved_fill_check(tmp_path, monkeypatch):
    board = _with_saved_fill_text(authored_pcb(tmp_path, "JLC04161H-3313"))
    refills = _fake_refill(monkeypatch)
    edited = board.read_bytes() + b"\n"

    def edit_pcb(pcb_path, kicad_cli):
        pcb_path.write_bytes(edited)
        return True

    monkeypatch.setattr(manufacturing, "_saved_zone_fill_matches_native_refill", edit_pcb)
    out = tmp_path / "mfg"
    config = _config(out, stackup_id="JLC04161H-3313")
    result = ManufacturingPackage(board, manufacturer="jlcpcb", config=config).export()
    assert not result.success
    assert "PCB changed during manufacturing export" in result.errors
    assert refills == []
    assert board.read_bytes() == edited
    assert not out.exists()


# ---------------------------------------------------------------------------
# Nothing is published without a required fill (no kicad-cli needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stackup_id", [None, "JLC04161H-3313"])
def test_failed_required_fill_publishes_nothing_and_restores_the_board(
    tmp_path, monkeypatch, stackup_id
):
    source = authored_pcb(tmp_path, stackup_id) if stackup_id else _stale_rules_board(tmp_path)
    board = _with_saved_fill_text(source)
    original = board.read_bytes()
    before_mtime = board.stat().st_mtime_ns
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    monkeypatch.setattr(manufacturing, "_saved_zone_fill_matches_native_refill", lambda *_: False)

    def dying_refill(pcb_path, kicad_cli=None, *, require_save=False):
        # kicad-cli killed mid --save-board: a truncated board on disk.
        Path(pcb_path).write_bytes(b"(kicad_pcb (version 20240108) (generator")
        return KiCadCLIResult(success=False, stderr="kicad-cli was killed")

    monkeypatch.setattr(runner, "run_refill_zones", dying_refill)
    out = tmp_path / "mfg"
    result = ManufacturingPackage(
        board, manufacturer="jlcpcb", config=_config(out, stackup_id=stackup_id)
    ).export()

    assert not result.success
    assert any("zone fill" in error and "kicad-cli was killed" in error for error in result.errors)
    assert not out.exists()
    assert board.read_bytes() == original
    if stackup_id:
        assert board.stat().st_mtime_ns == before_mtime
        assert not board.with_name(f".{board.name}.zonefill").exists()


def test_a_refill_that_saves_nothing_is_a_failed_required_fill(tmp_path, monkeypatch):
    """A DRC report over an unchanged board is not a saved fill (#9938)."""
    board = _with_saved_fill_text(_stale_rules_board(tmp_path))
    original = board.read_bytes()
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    monkeypatch.setattr(manufacturing, "_saved_zone_fill_matches_native_refill", lambda *_: False)
    monkeypatch.setattr(
        runner,
        "run_refill_zones",
        lambda pcb_path, kicad_cli=None, *, require_save=False: KiCadCLIResult(
            success=True, output_path=Path(pcb_path)
        ),
    )
    out = tmp_path / "mfg"
    result = ManufacturingPackage(board, manufacturer="jlcpcb", config=_config(out)).export()

    assert not result.success
    assert any("did not save" in error for error in result.errors)
    assert not out.exists()
    assert board.read_bytes() == original


def test_missing_kicad_cli_blocks_a_required_fill(tmp_path, monkeypatch):
    board = _with_saved_fill_text(_stale_rules_board(tmp_path))
    original = board.read_bytes()
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: None)
    out = tmp_path / "mfg"
    result = ManufacturingPackage(board, manufacturer="jlcpcb", config=_config(out)).export()

    assert not result.success
    assert any("kicad-cli" in error for error in result.errors)
    assert not out.exists()
    assert board.read_bytes() == original


def test_dry_run_refills_and_writes_nothing(tmp_path, monkeypatch):
    board = _with_saved_fill_text(_stale_rules_board(tmp_path))
    original = board.read_bytes()
    original_mtime_ns = board.stat().st_mtime_ns
    dru = board.with_suffix(".kicad_dru").read_bytes()
    refills = _forbid_refill(monkeypatch)
    out = tmp_path / "mfg"

    assert export_main([str(board), *_EXPORT_ARGS, "--dry-run", "-o", str(out)]) == 0

    assert refills == []
    assert not out.exists()
    assert board.read_bytes() == original
    assert board.stat().st_mtime_ns == original_mtime_ns
    assert board.with_suffix(".kicad_dru").read_bytes() == dru


# ---------------------------------------------------------------------------
# When no fill is required
# ---------------------------------------------------------------------------


def test_rules_change_without_a_saved_fill_needs_no_refill(tmp_path, monkeypatch):
    """Zones defined but never filled have no stale copper to bring in line."""
    board = _stale_rules_board(tmp_path)
    assert not _pcb_has_filled_zones(board)
    refills = _forbid_refill(monkeypatch)
    out = tmp_path / "mfg"

    result = ManufacturingPackage(board, manufacturer="jlcpcb", config=_config(out)).export()

    assert result.success, result.errors
    assert "Zone clearance - GND (F.Cu)" in board.with_suffix(".kicad_dru").read_text()
    assert refills == []


def test_bundle_without_copper_needs_no_fill(tmp_path, monkeypatch):
    """A BOM-only bundle ships neither the saved copper nor a plot of it."""
    board = _with_saved_fill_text(_stale_rules_board(tmp_path))
    original = board.read_bytes()
    refills = _forbid_refill(monkeypatch)
    out = tmp_path / "mfg"

    result = ManufacturingPackage(
        board,
        manufacturer="jlcpcb",
        config=_config(out, include_bom=True, include_project_zip=False),
    ).export()

    assert result.success, result.errors
    assert (out / "bom_jlcpcb.csv").is_file()
    assert refills == []
    assert board.read_bytes() == original


def test_unchanged_rules_needs_no_refill(tmp_path, monkeypatch):
    """``emit_drc_constraints=False`` writes no rule, so none becomes effective."""
    board = _with_saved_fill_text(_stale_rules_board(tmp_path))
    original = board.read_bytes()
    dru = board.with_suffix(".kicad_dru").read_bytes()
    refills = _forbid_refill(monkeypatch)

    result = ManufacturingPackage(
        board, manufacturer="jlcpcb", config=_config(tmp_path / "mfg", emit_drc_constraints=False)
    ).export()

    assert result.success, result.errors
    assert refills == []
    assert board.with_suffix(".kicad_dru").read_bytes() == dru
    assert board.read_bytes() == original


# ---------------------------------------------------------------------------
# A named stackup binds to the bytes that ship
# ---------------------------------------------------------------------------


def test_named_stackup_export_binds_its_record_to_the_refilled_board(tmp_path, monkeypatch):
    board = _with_saved_fill_text(authored_pcb(tmp_path, "JLC04161H-3313"))
    original = board.read_bytes()
    native_output = original + b"\r\n"
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    monkeypatch.setattr(manufacturing, "_saved_zone_fill_matches_native_refill", lambda *_: False)

    def refill(staged, kicad_cli=None, *, require_save=False):
        assert require_save
        assert staged.name == board.name
        assert staged.parent != board.parent
        assert staged.read_bytes() == original
        for suffix in (".kicad_pro", ".kicad_dru"):
            assert staged.with_suffix(suffix).read_bytes() == board.with_suffix(suffix).read_bytes()
        staged.write_bytes(native_output)
        return KiCadCLIResult(success=True, output_path=staged)

    monkeypatch.setattr(runner, "run_refill_zones", refill)
    out = tmp_path / "mfg"
    config = _config(out, stackup_id="JLC04161H-3313")
    result = ManufacturingPackage(board, manufacturer="jlcpcb", config=config).export()

    assert result.success, result.errors
    assert board.read_bytes() == native_output
    digest = hashlib.sha256(native_output).hexdigest()
    assert json.loads(result.stackup_path.read_text())["pcb_sha256"] == digest
    assert board.with_name(f".{board.name}.zonefill").read_text().split()[1] == digest
    with zipfile.ZipFile(out / "kicad_project.zip") as archive:
        assert archive.read(board.name) == native_output


@pytest.mark.parametrize(
    "error_type", [SourceChangedError, OSError], ids=["stale-source", "filesystem"]
)
def test_named_refill_requires_checked_publication(tmp_path, monkeypatch, error_type):
    from kicad_tools.core import atomic_write

    board = _with_saved_fill_text(authored_pcb(tmp_path, "JLC04161H-3313"))
    original = board.read_bytes()
    _fake_refill(monkeypatch)

    def refuse(path, content, *, expected_sha256, encoding, newline):
        assert path == board
        assert expected_sha256 == hashlib.sha256(original).hexdigest()
        assert content.encode(encoding) == original + b"\n"
        assert newline == ""
        raise error_type("checked publication refused")

    monkeypatch.setattr(atomic_write, "atomic_write_text", refuse)
    out = tmp_path / "mfg"
    config = _config(out, stackup_id="JLC04161H-3313")
    result = ManufacturingPackage(board, manufacturer="jlcpcb", config=config).export()

    assert any("checked publication refused" in error for error in result.errors)
    assert board.read_bytes() == original
    assert not out.exists()
    assert not board.with_name(f".{board.name}.zonefill").exists()


# ---------------------------------------------------------------------------
# The rule fingerprint
# ---------------------------------------------------------------------------


def test_zone_fill_rule_state_tracks_only_what_the_filler_honours(tmp_path):
    board = _stale_rules_board(tmp_path)
    before = _zone_fill_rule_state(board)

    # A rule the filler resolves gaps against.
    pro = board.with_suffix(".kicad_pro")
    project = json.loads(pro.read_text())
    project["net_settings"]["classes"][0]["clearance"] = 0.15
    pro.write_text(json.dumps(project))
    assert _zone_fill_rule_state(board) != before
    netclass_change = _zone_fill_rule_state(board)
    project["board"]["design_settings"]["rules"]["min_clearance"] = 0.2
    pro.write_text(json.dumps(project))
    assert _zone_fill_rule_state(board) != netclass_change

    # Custom rules.
    dru = board.with_suffix(".kicad_dru")
    dru.write_text(dru.read_text() + '(rule "x" (constraint clearance (min 0.2mm)))\n')
    dru_change = _zone_fill_rule_state(board)

    # Formatting and bookkeeping that leave the fill alone.
    project["meta"]["manufacturer"] = "other"
    project["board"]["design_settings"]["rule_severities"] = {"silk_over_copper": "ignore"}
    pro.write_text("\n".join(json.dumps(project, indent=4).splitlines()) + "\n\n")
    assert _zone_fill_rule_state(board) == dru_change
