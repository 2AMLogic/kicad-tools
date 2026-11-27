"""Regression tests for design report revision headers (Issue #10594).

Verifies that both ``kct report generate`` and ``kct export`` use the PCB's
authored ``(title_block (rev ...))`` value when no explicit snapshot override
is supplied, while preserving:
- explicit ``revision`` overrides in ``--data-dir`` ``metadata.json``,
- fallback to ``"1"`` when the PCB has no authored revision, and
- independent ``v1`` / ``v2`` output directory numbering across runs.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pdfplumber
import pytest
import yaml

from kicad_tools.cli import main as cli_main
from kicad_tools.report.renderers import _weasyprint_available
from kicad_tools.schema.pcb import PCB
from kicad_tools.schema.schematic import Schematic

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD05_OUTPUT = REPO_ROOT / "boards" / "05-bldc-motor-controller" / "output"
BOARD05_PCB = BOARD05_OUTPUT / "bldc_controller_routed.kicad_pcb"
BOARD05_SCH = BOARD05_OUTPUT / "bldc_controller.kicad_sch"

_STAGING_IGNORE = shutil.ignore_patterns(
    "manufacturing", "manufacturing.zip", "renders", "readiness"
)


def _write_one_pad_board(directory: Path, stem: str = "control", *, rev: str | None = None) -> Path:
    """Write a minimal valid one-pad 2-layer KiCad PCB with optional title-block rev."""
    directory.mkdir(parents=True, exist_ok=True)
    if rev is None:
        title_block = ""
    else:
        escaped = rev.replace("\\", "\\\\").replace('"', '\\"')
        title_block = f'  (title_block\n    (title "Control Board")\n    (rev "{escaped}")\n  )\n'

    pcb_text = (
        "(kicad_pcb\n"
        "  (version 20240108)\n"
        '  (generator "kicad-tools-test")\n'
        "  (general (thickness 1.6))\n"
        '  (paper "A4")\n'
        f"{title_block}"
        "  (layers\n"
        '    (0 "F.Cu" signal)\n'
        '    (31 "B.Cu" signal)\n'
        '    (44 "Edge.Cuts" user)\n'
        "  )\n"
        '  (net 0 "")\n'
        '  (net 1 "SIG")\n'
        '  (gr_rect (start 100 100) (end 120 115) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts"))\n'
        '  (footprint "TestPoint:TestPoint_Pad_1.0x1.0mm"\n'
        '    (layer "F.Cu")\n'
        "    (at 110 107.5)\n"
        '    (property "Reference" "TP1" (at 0 -1.5 0) (layer "F.SilkS"))\n'
        '    (property "Value" "TP" (at 0 1.5 0) (layer "F.Fab"))\n'
        "    (attr smd)\n"
        '    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG"))\n'
        "  )\n"
        ")\n"
    )
    pcb_path = directory / f"{stem}.kicad_pcb"
    pcb_path.write_text(pcb_text, encoding="utf-8")
    return pcb_path


def _extract_pdf_page1_text(pdf_path: Path) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        assert len(pdf.pages) >= 1
        return pdf.pages[0].extract_text() or ""


class TestResolveReportRevisionUnit:
    """Unit tests for the shared revision precedence helper."""

    def test_uses_authored_pcb_revision_when_no_override(self, tmp_path: Path) -> None:
        from kicad_tools.report.utils import resolve_report_revision

        pcb_b = _write_one_pad_board(tmp_path / "b", rev="B")
        pcb_c2 = _write_one_pad_board(tmp_path / "c2", rev="C.2")

        assert resolve_report_revision(pcb_b) == "B"
        assert resolve_report_revision(pcb_c2) == "C.2"

    def test_explicit_override_beats_pcb_revision(self, tmp_path: Path) -> None:
        from kicad_tools.report.utils import resolve_report_revision

        pcb_b = _write_one_pad_board(tmp_path, rev="B")
        assert resolve_report_revision(pcb_b, explicit_revision="LAB-8") == "LAB-8"

    def test_empty_or_missing_pcb_revision_falls_back_to_default(self, tmp_path: Path) -> None:
        from kicad_tools.report.utils import resolve_report_revision

        pcb_none = _write_one_pad_board(tmp_path / "none", rev=None)
        pcb_empty = _write_one_pad_board(tmp_path / "empty", rev="   ")

        assert resolve_report_revision(pcb_none) == "1"
        assert resolve_report_revision(pcb_empty) == "1"
        assert resolve_report_revision(tmp_path / "missing.kicad_pcb") == "1"


@pytest.mark.timeout(180)
class TestReportRevisionCLIAndExport:
    """Control-board matrix across ``kct report generate`` and ``kct export``."""

    @pytest.mark.parametrize(
        ("pcb_rev", "expected_rev"),
        [
            ("B", "B"),
            ("C.2", "C.2"),
            ('C.2 "bench" & B', 'C.2 "bench" & B'),
            (None, "1"),
        ],
    )
    def test_report_generate_uses_pcb_revision_or_fallback(
        self,
        tmp_path: Path,
        pcb_rev: str | None,
        expected_rev: str,
    ) -> None:
        pcb_path = _write_one_pad_board(tmp_path / "board", rev=pcb_rev)
        reports_dir = tmp_path / "reports"

        rc = cli_main(
            [
                "report",
                "generate",
                str(pcb_path),
                "--mfr",
                "jlcpcb",
                "--no-figures",
                "--skip-erc",
                "-o",
                str(reports_dir),
            ]
        )
        assert rc == 0

        md_path = reports_dir / "v1" / "report.md"
        assert md_path.is_file()
        md_text = md_path.read_text(encoding="utf-8")
        header = yaml.safe_load(md_text.split("---", 2)[1])
        assert header["date"].startswith(f"Rev {expected_rev} | ")

        if _weasyprint_available():
            pdf_path = reports_dir / "v1" / "report.pdf"
            assert pdf_path.is_file()
            page1 = _extract_pdf_page1_text(pdf_path)
            assert f"Rev {expected_rev} |" in page1
            if expected_rev != "1":
                assert "Rev 1 |" not in page1

    def test_report_generate_preserves_explicit_data_dir_override(self, tmp_path: Path) -> None:
        pcb_path = _write_one_pad_board(tmp_path / "board", rev="B")
        data_dir = tmp_path / "snapshot_data"
        data_dir.mkdir()
        (data_dir / "metadata.json").write_text(
            json.dumps({"revision": "LAB-8", "date": "2026-11-27"}),
            encoding="utf-8",
        )
        reports_dir = tmp_path / "reports"

        rc = cli_main(
            [
                "report",
                "generate",
                str(pcb_path),
                "--mfr",
                "jlcpcb",
                "--data-dir",
                str(data_dir),
                "--no-figures",
                "-o",
                str(reports_dir),
            ]
        )
        assert rc == 0

        md_text = (reports_dir / "v1" / "report.md").read_text(encoding="utf-8")
        assert 'date: "Rev LAB-8 | 2026-11-27 | jlcpcb"' in md_text
        assert "Rev B |" not in md_text
        assert "Rev 1 |" not in md_text

    def test_report_generate_data_dir_without_revision_uses_pcb_revision(
        self, tmp_path: Path
    ) -> None:
        pcb_path = _write_one_pad_board(tmp_path / "board", rev="B")
        data_dir = tmp_path / "snapshot_data"
        data_dir.mkdir()
        (data_dir / "metadata.json").write_text(
            json.dumps({"date": "2026-11-27"}),
            encoding="utf-8",
        )
        reports_dir = tmp_path / "reports"

        rc = cli_main(
            [
                "report",
                "generate",
                str(pcb_path),
                "--mfr",
                "jlcpcb",
                "--data-dir",
                str(data_dir),
                "--no-figures",
                "-o",
                str(reports_dir),
            ]
        )
        assert rc == 0

        md_text = (reports_dir / "v1" / "report.md").read_text(encoding="utf-8")
        assert 'date: "Rev B | 2026-11-27 | jlcpcb"' in md_text
        assert "Rev 1 |" not in md_text

    def test_report_generate_kicad_pro_input_uses_pcb_revision(self, tmp_path: Path) -> None:
        pcb_path = _write_one_pad_board(tmp_path / "board", rev="B")
        pro_path = pcb_path.with_suffix(".kicad_pro")
        pro_path.write_text("{}", encoding="utf-8")
        reports_dir = tmp_path / "reports"

        rc = cli_main(
            [
                "report",
                "generate",
                str(pro_path),
                "--mfr",
                "jlcpcb",
                "--no-figures",
                "--skip-collect",
                "-o",
                str(reports_dir),
            ]
        )
        assert rc == 0

        md_text = (reports_dir / "v1" / "report.md").read_text(encoding="utf-8")
        assert 'date: "Rev B | ' in md_text
        assert 'date: "Rev 1 | ' not in md_text

    def test_successive_report_runs_keep_distinct_version_dirs(self, tmp_path: Path) -> None:
        pcb_path = _write_one_pad_board(tmp_path / "board", rev="B")
        reports_dir = tmp_path / "reports"

        for expected_dir in ("v1", "v2"):
            rc = cli_main(
                [
                    "report",
                    "generate",
                    str(pcb_path),
                    "--mfr",
                    "jlcpcb",
                    "--no-figures",
                    "--skip-collect",
                    "-o",
                    str(reports_dir),
                ]
            )
            assert rc == 0
            md_path = reports_dir / expected_dir / "report.md"
            assert md_path.is_file()
            md_text = md_path.read_text(encoding="utf-8")
            assert 'date: "Rev B | ' in md_text
            assert 'date: "Rev 1 | ' not in md_text

    @pytest.mark.parametrize(
        ("pcb_rev", "expected_rev"),
        [
            ("C.2", "C.2"),
            (None, "1"),
        ],
    )
    def test_export_uses_pcb_revision_for_nonnumeric_and_unversioned_boards(
        self,
        tmp_path: Path,
        pcb_rev: str | None,
        expected_rev: str,
    ) -> None:
        pcb_path = _write_one_pad_board(tmp_path / "board", rev=pcb_rev)
        out_dir = tmp_path / "mfg"

        with patch(
            "kicad_tools.export.manufacturing.ManufacturingPackage._generate_figures",
            return_value=None,
        ):
            rc = cli_main(
                [
                    "export",
                    str(pcb_path),
                    "--mfr",
                    "jlcpcb",
                    "--skip-preflight",
                    "--no-gerbers",
                    "--no-bom",
                    "--no-cpl",
                    "--no-project-zip",
                    "--no-silk-fix",
                    "-o",
                    str(out_dir),
                ]
            )
        assert rc == 0

        md_path = out_dir / "report.md"
        assert md_path.is_file()
        md_text = md_path.read_text(encoding="utf-8")
        assert f'date: "Rev {expected_rev} | ' in md_text
        if expected_rev != "1":
            assert 'date: "Rev 1 | ' not in md_text

        if _weasyprint_available():
            pdf_path = out_dir / "report.pdf"
            assert pdf_path.is_file()
            page1 = _extract_pdf_page1_text(pdf_path)
            assert f"Rev {expected_rev} |" in page1
            if expected_rev != "1":
                assert "Rev 1 |" not in page1


@pytest.mark.timeout(180)
class TestBoard05ReportRevision:
    """Exercise Board 05 Markdown and PDF across standalone report and export."""

    @pytest.fixture
    def staged_board05(self, tmp_path: Path) -> tuple[Path, Path]:
        if not BOARD05_PCB.is_file() or not BOARD05_SCH.is_file():
            pytest.skip("Board 05 output artifacts not present")
        staged = tmp_path / "board05_output"
        shutil.copytree(BOARD05_OUTPUT, staged, ignore=_STAGING_IGNORE)
        return staged / BOARD05_PCB.name, staged / BOARD05_SCH.name

    def test_board05_report_generate_and_export_show_rev_b_in_md_and_pdf(
        self,
        staged_board05: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        staged_pcb, staged_sch = staged_board05
        assert PCB.load(staged_pcb).revision == "B"
        assert Schematic.load(staged_sch).title_block.rev == "B"

        # 1. Standalone `kct report generate`
        reports_dir = tmp_path / "standalone_reports"
        rc_report = cli_main(
            [
                "report",
                "generate",
                str(staged_pcb),
                "--mfr",
                "jlcpcb",
                "--no-figures",
                "--skip-erc",
                "-o",
                str(reports_dir),
            ]
        )
        assert rc_report == 0

        standalone_md = (reports_dir / "v1" / "report.md").read_text(encoding="utf-8")
        assert 'date: "Rev B | ' in standalone_md
        assert 'date: "Rev 1 | ' not in standalone_md

        if _weasyprint_available():
            standalone_pdf = reports_dir / "v1" / "report.pdf"
            assert standalone_pdf.is_file()
            page1_standalone = _extract_pdf_page1_text(standalone_pdf)
            assert "Rev B |" in page1_standalone
            assert "Rev 1 |" not in page1_standalone

        # 2. Manufacturing `kct export`
        mfg_dir = tmp_path / "mfg_export"
        with patch(
            "kicad_tools.export.manufacturing.ManufacturingPackage._generate_figures",
            return_value=None,
        ):
            rc_export = cli_main(
                [
                    "export",
                    str(staged_pcb),
                    "--mfr",
                    "jlcpcb",
                    "--skip-preflight",
                    "--no-gerbers",
                    "--no-bom",
                    "--no-cpl",
                    "--no-project-zip",
                    "--no-silk-fix",
                    "-o",
                    str(mfg_dir),
                ]
            )
        assert rc_export == 0

        export_md = (mfg_dir / "report.md").read_text(encoding="utf-8")
        assert 'date: "Rev B | ' in export_md
        assert 'date: "Rev 1 | ' not in export_md

        if _weasyprint_available():
            export_pdf = mfg_dir / "report.pdf"
            assert export_pdf.is_file()
            page1_export = _extract_pdf_page1_text(export_pdf)
            assert "Rev B |" in page1_export
            assert "Rev 1 |" not in page1_export
