"""Readiness-generated ``gerbers.zip`` is reproducible (Issue #10174).

kicad-cli stamps wall-clock creation dates into every Gerber and drill file, so
two ``kct readiness --generate`` runs on an unchanged board uploaded different
ZIP bytes.  These tests drive the real preset export, normalisation and archive
writer; only the ``kicad-cli`` plot/drill steps are replaced by writers that
stamp the *current* time, like kicad-cli does.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.export import gerber as gerber_mod
from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

PCB_TEXT = '(kicad_pcb (version 20240108) (generator "test"))\n'


def _fake_plot(stamp_clock: list[int]):
    """Replacement ``_export_gerbers``/``_export_drill`` stamping a fresh time."""

    def _stamp() -> datetime:
        stamp_clock[0] += 7  # each plot lands at a different "wall-clock" time
        return datetime.fromtimestamp(1_760_000_000 + stamp_clock[0], tz=timezone.utc)

    def export_gerbers(self, config, output_dir: Path) -> None:
        t = _stamp()
        iso = t.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        flat = t.strftime("%Y-%m-%d %H:%M:%S")
        (output_dir / "board-F_Cu.gtl").write_text(
            f"G04 Created by KiCad (PCBNEW 10.0.6) date {flat}*\n"
            f"%TF.CreationDate,{iso}*%\n{self._copper}\nM02*\n",
            encoding="ascii",
        )
        (output_dir / "board-job.gbrjob").write_text(
            f'{{"GeneralSpecs": {{"CreationDate": "{iso}"}}}}\n', encoding="ascii"
        )

    def export_drill(self, config, output_dir: Path) -> None:
        t = _stamp()
        (output_dir / "board.drl").write_text(
            f"M48\n; DRILL file KiCad 10.0.6 date {t.strftime('%Y-%m-%dT%H:%M:%S')}\n"
            f"; #@! TF.CreationDate,{t.strftime('%Y-%m-%dT%H:%M:%S+00:00')}\nM30\n",
            encoding="ascii",
        )

    return export_gerbers, export_drill


@pytest.fixture
def generate(tmp_path, monkeypatch):
    clock = [0]
    plot, drill = _fake_plot(clock)
    monkeypatch.setattr(gerber_mod.GerberExporter, "_export_gerbers", plot)
    monkeypatch.setattr(gerber_mod.GerberExporter, "_export_drill", drill)
    monkeypatch.setattr(gerber_mod.GerberExporter, "_copper", "(gr_line A)", raising=False)
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    counter = [0]

    def _generate(copper: str = "(gr_line A)") -> Path:
        counter[0] += 1
        gerber_mod.GerberExporter._copper = copper
        out = tmp_path / f"out{counter[0]}"
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_TEXT)
        config = AssemblyConfig(
            include_bom=False,
            include_pnp=False,
            gerber_source_date_epoch=readiness_cmd._readiness_gerber_epoch(),
        )
        pkg = AssemblyPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        return pkg._generate_gerbers(out)

    return _generate


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_unset_epoch_two_generations_are_byte_identical(generate):
    first = generate()
    time.sleep(1.1)  # the fake plotters stamp a different time each call
    second = generate()
    assert _sha(first) == _sha(second)
    with zipfile.ZipFile(first) as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert any(n.endswith(".gtl") for n in names)
        assert any(n.endswith(".drl") for n in names)
        assert any(n.endswith(".gbrjob") for n in names)
        stamp = datetime.fromtimestamp(readiness_cmd.READINESS_GERBER_EPOCH, tz=timezone.utc)
        assert stamp.strftime("%Y-%m-%dT%H:%M:%S+00:00") in zf.read("board-F_Cu.gtl").decode()


def test_explicit_epoch_controls_creation_date_and_is_repeatable(generate, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    first = generate()
    time.sleep(1.1)
    second = generate()
    assert _sha(first) == _sha(second)
    with zipfile.ZipFile(first) as zf:
        assert "2023-11-14T22:13:20+00:00" in zf.read("board-F_Cu.gtl").decode()


def test_explicit_epoch_differs_from_default(generate, monkeypatch):
    default = _sha(generate())
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert _sha(generate()) != default


def test_invalid_epoch_falls_back_to_default(generate, monkeypatch):
    default = _sha(generate())
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")
    assert _sha(generate()) == default


def test_copper_change_changes_zip_hash(generate):
    assert _sha(generate("(gr_line A)")) != _sha(generate("(gr_line B)"))


def test_run_export_passes_gerber_epoch(tmp_path, monkeypatch):
    seen = {}

    class Stub:
        def __init__(self, pcb_path, manufacturer, config, schematic_path=None):
            seen["config"] = config

        def export(self, output_dir):
            raise RuntimeError("stop after capturing config")

    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    monkeypatch.setattr("kicad_tools.export.manufacturing.ManufacturingPackage", Stub)
    run = readiness_cmd._run_export(tmp_path / "b.kicad_pcb", "jlcpcb", tmp_path / "o", True)
    assert not run.ok
    assert seen["config"].gerber_source_date_epoch == datetime.fromtimestamp(
        readiness_cmd.READINESS_GERBER_EPOCH, tz=timezone.utc
    )
