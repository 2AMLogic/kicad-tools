"""Tests for the report renderers module.

Verifies Markdown-to-HTML conversion, base64 image embedding,
CSS integration, PDF rendering guards, and edge cases.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

QUOTE_SHAPES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_markdown() -> str:
    """A minimal Markdown document for testing."""
    return (
        "# Test Report\n\n"
        "This is a test report.\n\n"
        "## DRC Results\n\n"
        "| Rule | Status |\n"
        "|------|--------|\n"
        "| Clearance | Pass |\n"
        "| Width | Fail |\n\n"
        "```python\nprint('hello')\n```\n"
    )


@pytest.fixture
def markdown_with_image() -> str:
    """Markdown containing an image reference."""
    return "# Board Layout\n\n![Front Copper](front_copper.png)\n\nSome description.\n"


@pytest.fixture
def markdown_with_multiple_images() -> str:
    """Markdown containing multiple image references."""
    return "# Layers\n\n![Front](front.png)\n\n![Back](back.png)\n\n![Missing](missing.png)\n\n"


@pytest.fixture
def figures_dir(tmp_path: Path) -> Path:
    """Create a temporary figures directory with a test PNG file."""
    figs = tmp_path / "figures"
    figs.mkdir()
    # Minimal valid PNG: 1x1 transparent pixel
    # This is the smallest valid PNG file (67 bytes)
    png_header = (
        b"\x89PNG\r\n\x1a\n"  # PNG signature
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01"  # width = 1
        b"\x00\x00\x00\x01"  # height = 1
        b"\x08\x02"  # bit depth 8, color type 2 (RGB)
        b"\x00\x00\x00"  # compression, filter, interlace
        b"\x90wS\xde"  # CRC
        b"\x00\x00\x00\x0cIDATx"
        b"\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
        b"\x18\xd8N"  # CRC
        b"\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"  # CRC
    )
    (figs / "front_copper.png").write_bytes(png_header)
    (figs / "front.png").write_bytes(png_header)
    (figs / "back.png").write_bytes(png_header)
    return figs


# ---------------------------------------------------------------------------
# render_html tests
# ---------------------------------------------------------------------------


class TestRenderHtml:
    """Tests for the render_html function."""

    def test_produces_valid_html(self, simple_markdown: str) -> None:
        """Output contains required HTML5 structural elements."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        assert "<!DOCTYPE html>" in result
        assert "<html" in result
        assert "<head>" in result
        assert "<body>" in result
        assert "</html>" in result

    def test_contains_embedded_css(self, simple_markdown: str) -> None:
        """CSS is embedded inline in a <style> tag, not as a link."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        assert "<style>" in result
        # Check some characteristic CSS content
        assert ".drc-error" in result
        assert ".drc-warning" in result
        assert ".drc-ok" in result

    def test_markdown_converted_to_html(self, simple_markdown: str) -> None:
        """Markdown headings, tables, and code blocks are converted to HTML."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        assert "<h1>" in result or "<h1 " in result
        assert "<h2>" in result or "<h2 " in result
        assert "<table>" in result
        assert "<code>" in result or "<code " in result

    def test_tables_wrapped_for_responsive_layout(self, simple_markdown: str) -> None:
        """Tables are wrapped in a scrollable container div."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        assert '<div class="table-wrapper">' in result

    def test_embeds_figures_as_base64(
        self,
        markdown_with_image: str,
        figures_dir: Path,
    ) -> None:
        """PNG images from figures_dir are embedded as base64 data URIs."""
        from kicad_tools.report.renderers import render_html

        result = render_html(markdown_with_image, figures_dir=figures_dir)
        assert "data:image/png;base64," in result
        # Original filename should NOT appear as a src attribute
        assert 'src="front_copper.png"' not in result

    def test_embeds_multiple_figures(
        self,
        markdown_with_multiple_images: str,
        figures_dir: Path,
    ) -> None:
        """Multiple existing PNGs are embedded; missing ones are left unchanged."""
        from kicad_tools.report.renderers import render_html

        result = render_html(markdown_with_multiple_images, figures_dir=figures_dir)
        # Two images should be embedded
        base64_count = result.count("data:image/png;base64,")
        assert base64_count == 2
        # Missing image reference should be preserved unchanged
        assert 'src="missing.png"' in result

    def test_no_figures_dir_preserves_paths(self, markdown_with_image: str) -> None:
        """When figures_dir is None, image src attributes are left as-is."""
        from kicad_tools.report.renderers import render_html

        result = render_html(markdown_with_image, figures_dir=None)
        assert "data:image/png;base64," not in result

    def test_empty_markdown(self) -> None:
        """Empty Markdown input does not raise an error."""
        from kicad_tools.report.renderers import render_html

        result = render_html("")
        assert "<!DOCTYPE html>" in result
        assert "<body>" in result

    def test_markdown_no_images(self, simple_markdown: str, figures_dir: Path) -> None:
        """Markdown without image references renders without error even with a figures_dir."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown, figures_dir=figures_dir)
        assert "data:image/png;base64," not in result
        assert "<!DOCTYPE html>" in result

    def test_empty_figures_dir(self, markdown_with_image: str, tmp_path: Path) -> None:
        """Empty figures directory does not crash; images are left as relative paths."""
        from kicad_tools.report.renderers import render_html

        empty_dir = tmp_path / "empty_figs"
        empty_dir.mkdir()

        result = render_html(markdown_with_image, figures_dir=empty_dir)
        assert "data:image/png;base64," not in result
        assert "<!DOCTYPE html>" in result

    def test_non_png_files_not_embedded(self, tmp_path: Path) -> None:
        """Non-PNG files in figures directory are not embedded."""
        from kicad_tools.report.renderers import render_html

        figs = tmp_path / "figs"
        figs.mkdir()
        (figs / "diagram.svg").write_text("<svg></svg>")

        md = "![Diagram](diagram.svg)\n"
        result = render_html(md, figures_dir=figs)
        assert "data:image/png;base64," not in result

    def test_self_contained_single_file(self, simple_markdown: str) -> None:
        """Output is a self-contained document with no external references."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        # Should not contain <link rel="stylesheet"> or external references
        assert "<link " not in result
        assert 'rel="stylesheet"' not in result

    def test_figures_dir_as_string(
        self,
        markdown_with_image: str,
        figures_dir: Path,
    ) -> None:
        """render_html accepts figures_dir as a plain str without raising."""
        from kicad_tools.report.renderers import render_html

        result = render_html(markdown_with_image, figures_dir=str(figures_dir))
        assert "data:image/png;base64," in result
        assert 'src="front_copper.png"' not in result

    def test_embeds_figures_with_prefixed_src(self, figures_dir: Path) -> None:
        """Images with a 'figures/' prefix in src are embedded correctly."""
        from kicad_tools.report.renderers import render_html

        # This mirrors the real template output where src="figures/front.png"
        md = "# Board\n\n![Front](figures/front.png)\n\nDescription.\n"
        result = render_html(md, figures_dir=figures_dir)
        assert "data:image/png;base64," in result
        assert 'src="figures/front.png"' not in result

    def test_cover_block_in_html_output(self) -> None:
        """Cover block div passes through Markdown-to-HTML conversion."""
        from kicad_tools.report.renderers import render_html

        md = (
            '<div class="cover-block">\n\n'
            "# MyProject\n\n"
            '<p class="cover-meta">Design Report</p>\n'
            "</div>\n\n"
            "## Section\n\nContent.\n"
        )
        result = render_html(md)
        assert 'class="cover-block"' in result
        assert "MyProject" in result


# ---------------------------------------------------------------------------
# render_pdf tests
# ---------------------------------------------------------------------------


class TestRenderPdf:
    """Tests for the render_pdf function."""

    def test_missing_weasyprint_raises_import_error(self, tmp_path: Path) -> None:
        """render_pdf raises ImportError with install hint when weasyprint is absent."""
        from kicad_tools.report.renderers import render_pdf

        with patch(
            "kicad_tools.report.renderers._weasyprint_available",
            return_value=False,
        ):
            with pytest.raises(ImportError, match="weasyprint"):
                render_pdf("<html></html>", tmp_path / "report.pdf")

    def test_import_error_contains_install_instructions(self, tmp_path: Path) -> None:
        """The ImportError message tells the user how to install weasyprint."""
        from kicad_tools.report.renderers import render_pdf

        with patch(
            "kicad_tools.report.renderers._weasyprint_available",
            return_value=False,
        ):
            with pytest.raises(ImportError, match=r"pip install.*kicad-tools\[report\]"):
                render_pdf("<html></html>", tmp_path / "report.pdf")

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """render_pdf creates parent directories if they do not exist."""
        import sys

        from kicad_tools.report.renderers import render_pdf

        output = tmp_path / "deep" / "nested" / "report.pdf"

        # Create a fake weasyprint module with a mock HTML class
        mock_html_cls = type(
            "MockHTML",
            (),
            {
                "__init__": lambda self, string: None,
                "write_pdf": lambda self, path: Path(path).write_bytes(b"%PDF-mock"),
            },
        )

        fake_weasyprint = type(sys)("weasyprint")
        fake_weasyprint.HTML = mock_html_cls
        sys.modules["weasyprint"] = fake_weasyprint

        try:
            with patch(
                "kicad_tools.report.renderers._weasyprint_available",
                return_value=True,
            ):
                render_pdf("<html></html>", output)
                assert output.parent.exists()
                assert output.exists()
        finally:
            del sys.modules["weasyprint"]

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        """render_pdf accepts a plain str path without raising AttributeError."""
        import sys

        from kicad_tools.report.renderers import render_pdf

        output = tmp_path / "str_test" / "report.pdf"
        # Pass the path as a plain string, not a Path object
        output_str = str(output)

        # Create a fake weasyprint module with a mock HTML class
        mock_html_cls = type(
            "MockHTML",
            (),
            {
                "__init__": lambda self, string: None,
                "write_pdf": lambda self, path: Path(path).write_bytes(b"%PDF-mock"),
            },
        )

        fake_weasyprint = type(sys)("weasyprint")
        fake_weasyprint.HTML = mock_html_cls
        sys.modules["weasyprint"] = fake_weasyprint

        try:
            with patch(
                "kicad_tools.report.renderers._weasyprint_available",
                return_value=True,
            ):
                render_pdf("<html></html>", output_str)
                assert output.parent.exists()
                assert output.exists()
        finally:
            del sys.modules["weasyprint"]


# ---------------------------------------------------------------------------
# render_pdf_pandoc tests (issue #4023: cwd-independent image resolution +
# loud detection of dropped resources)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    """Stand-in for subprocess.CompletedProcess in pandoc mocks."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRenderPdfPandoc:
    """Tests for render_pdf_pandoc's resource resolution and error surfacing."""

    def test_passes_resource_path_of_markdown_dir(self, tmp_path: Path) -> None:
        """The pandoc command includes --resource-path=<markdown dir>.

        This is the core cwd-independence fix (issue #4023): relative image
        references must resolve against the Markdown's own directory, not the
        process cwd.
        """
        from kicad_tools.report import renderers

        report_dir = tmp_path / "manufacturing"
        report_dir.mkdir()
        md_path = report_dir / "report.md"
        md_path.write_text("# Report\n\n![Fig](images/x.png)\n")
        out_pdf = report_dir / "report.pdf"

        captured: dict = {}

        def fake_run(cmd, capture_output, text):  # noqa: ANN001, ANN202
            captured["cmd"] = cmd
            return _FakeCompletedProcess(returncode=0, stderr="")

        with patch.object(renderers.subprocess, "run", side_effect=fake_run):
            renderers.render_pdf_pandoc(md_path, out_pdf)

        resource_args = [a for a in captured["cmd"] if a.startswith("--resource-path=")]
        assert resource_args == [f"--resource-path={report_dir}"]

    def test_resource_path_independent_of_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--resource-path points at the markdown dir regardless of the cwd.

        Simulates ``kct export`` being invoked from the repo root while the
        report lives in a nested directory.
        """
        from kicad_tools.report import renderers

        report_dir = tmp_path / "boards" / "05" / "manufacturing"
        report_dir.mkdir(parents=True)
        md_path = report_dir / "report.md"
        md_path.write_text("# Report\n\n![Fig](images/x.png)\n")

        # Change cwd to somewhere unrelated (the "repo root" analogue).
        other_dir = tmp_path / "repo_root"
        other_dir.mkdir()
        monkeypatch.chdir(other_dir)

        captured: dict = {}

        def fake_run(cmd, capture_output, text):  # noqa: ANN001, ANN202
            captured["cmd"] = cmd
            return _FakeCompletedProcess(returncode=0, stderr="")

        with patch.object(renderers.subprocess, "run", side_effect=fake_run):
            # Pass a path relative to cwd would break; we pass absolute here,
            # matching how the exporter calls it.
            renderers.render_pdf_pandoc(md_path, report_dir / "report.pdf")

        assert f"--resource-path={report_dir}" in captured["cmd"]
        # The resource path must be the markdown's dir, never the cwd.
        assert f"--resource-path={other_dir}" not in captured["cmd"]

    def test_dropped_resource_raises(self, tmp_path: Path) -> None:
        """A pandoc 'Could not fetch resource' warning (exit 0) raises loudly.

        This is the silent-failure guard: pandoc exits 0 but drops figures.
        The function must not let that pass as success.
        """
        from kicad_tools.report import renderers

        md_path = tmp_path / "report.md"
        md_path.write_text("# Report\n\n![Fig](images/pcb_front.png)\n")
        out_pdf = tmp_path / "report.pdf"

        stderr = (
            "[WARNING] Could not fetch resource images/pcb_front.png: "
            "replacing image with description\n"
            "[WARNING] Could not fetch resource images/pcb_back.png: "
            "replacing image with description\n"
        )

        with patch.object(
            renderers.subprocess,
            "run",
            return_value=_FakeCompletedProcess(returncode=0, stderr=stderr),
        ):
            with pytest.raises(RuntimeError, match="could not fetch"):
                renderers.render_pdf_pandoc(md_path, out_pdf)

    def test_dropped_resource_names_missing_files(self, tmp_path: Path) -> None:
        """The raised error names each dropped resource so it is diagnosable."""
        from kicad_tools.report import renderers

        md_path = tmp_path / "report.md"
        md_path.write_text("# Report\n")
        out_pdf = tmp_path / "report.pdf"

        stderr = (
            "[WARNING] Could not fetch resource images/pcb_front.png: replacing\n"
            "[WARNING] Could not fetch resource images/pcb_back.png: replacing\n"
        )

        with patch.object(
            renderers.subprocess,
            "run",
            return_value=_FakeCompletedProcess(returncode=0, stderr=stderr),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                renderers.render_pdf_pandoc(md_path, out_pdf)

        msg = str(excinfo.value)
        assert "images/pcb_front.png" in msg
        assert "images/pcb_back.png" in msg

    def test_clean_render_does_not_raise(self, tmp_path: Path) -> None:
        """Exit 0 with no dropped-resource warnings succeeds silently."""
        from kicad_tools.report import renderers

        md_path = tmp_path / "report.md"
        md_path.write_text("# Report\n\n![Fig](images/x.png)\n")
        out_pdf = tmp_path / "report.pdf"

        with patch.object(
            renderers.subprocess,
            "run",
            return_value=_FakeCompletedProcess(returncode=0, stderr="[INFO] fine\n"),
        ):
            renderers.render_pdf_pandoc(md_path, out_pdf)  # must not raise

    def test_nonzero_exit_still_raises(self, tmp_path: Path) -> None:
        """A non-zero pandoc exit continues to raise RuntimeError."""
        from kicad_tools.report import renderers

        md_path = tmp_path / "report.md"
        md_path.write_text("# Report\n")
        out_pdf = tmp_path / "report.pdf"

        with patch.object(
            renderers.subprocess,
            "run",
            return_value=_FakeCompletedProcess(returncode=1, stderr="fatal: boom"),
        ):
            with pytest.raises(RuntimeError, match="pandoc failed"):
                renderers.render_pdf_pandoc(md_path, out_pdf)

    def _rendered_report(self, directory: Path, project_name: str, revision: str = "B") -> Path:
        """Write a report through the real generator; return its Markdown path.

        The name is supplied to the generator directly, so no filesystem
        naming rule is involved (Issue #10630).
        """
        from kicad_tools.report.generator import ReportGenerator
        from kicad_tools.report.models import ReportData

        data = ReportData(
            project_name=project_name,
            revision=revision,
            date="2026-11-27",
            manufacturer="jlcpcb",
        )
        return ReportGenerator().generate(data, directory / "reports")

    def _pandoc_cmd(self, markdown: str, tmp_path: Path) -> list[str]:
        """Run render_pdf_pandoc on *markdown* against a mocked subprocess."""
        from kicad_tools.report import renderers

        md_path = tmp_path / "report.md"
        md_path.write_text(markdown, encoding="utf-8")
        captured: dict = {}

        def fake_run(cmd, capture_output, text):  # noqa: ANN001, ANN202
            captured["cmd"] = cmd
            return _FakeCompletedProcess(returncode=0, stderr="")

        with patch.object(renderers.subprocess, "run", side_effect=fake_run):
            renderers.render_pdf_pandoc(md_path, tmp_path / "report.pdf")
        return captured["cmd"]

    def _latex_output(self, md_path: Path, tmp_path: Path) -> str:
        """Return the LaTeX render_pdf_pandoc has pandoc write for *md_path*.

        pandoc infers the output format from the file extension, so a
        ``.tex`` destination runs the real command without a TeX install.
        Escapes pandoc terminates with ``{}`` (``\\textbackslash{}``,
        ``\\textbar{}``) have that suffix dropped, so they read the same as
        the bare form pandoc writes before a space.
        """
        from kicad_tools.report import renderers

        tex_path = tmp_path / "report.tex"
        renderers.render_pdf_pandoc(md_path, tex_path)
        tex = tex_path.read_text(encoding="utf-8")
        for escape in ("\\textbackslash", "\\textbar"):
            tex = tex.replace(escape + "{}", escape)
        return tex

    def test_cover_scalars_passed_as_literal_metadata(self, tmp_path: Path) -> None:
        """JSON-quoted cover scalars are decoded and re-supplied as --metadata.

        Pandoc parses YAML metadata strings as Markdown, so a title such as
        ``Panel A\\B`` reaches the TeX writer as a raw control sequence and
        XeLaTeX exits 43 (Issue #10630). Command-line ``--metadata`` values
        are literal strings that override the front matter block.
        """
        cmd = self._pandoc_cmd(
            "---\n"
            'title: "Panel A\\\\B"\n'
            'subtitle: "Design Report"\n'
            'author: "kicad-tools 0.11.0"\n'
            'date: "Rev B | 2026-11-27 | jlcpcb"\n'
            'geometry: "margin=1in"\n'
            "---\n\n# Body\n",
            tmp_path,
        )

        metadata_args = [a for a in cmd if a.startswith("--metadata=")]
        assert metadata_args == [
            "--metadata=title:Panel A\\B",
            "--metadata=subtitle:Design Report",
            "--metadata=author:kicad-tools 0.11.0",
            "--metadata=date:Rev B | 2026-11-27 | jlcpcb",
        ]

    def test_boolean_spelled_cover_scalar_keeps_front_matter_value(self, tmp_path: Path) -> None:
        """pandoc reads ``--metadata=title:False`` as the boolean false.

        A false boolean suppresses the title entirely, so YAML boolean
        spellings are left to the front matter block, where the template
        already quotes them.
        """
        cmd = self._pandoc_cmd(
            "---\n"
            'title: "False"\n'
            'subtitle: "Design Report"\n'
            'date: "Rev B | 2026-11-27 | jlcpcb"\n'
            "---\n\n# Body\n",
            tmp_path,
        )

        metadata_args = [a for a in cmd if a.startswith("--metadata=")]
        assert "--metadata=title:False" not in metadata_args
        assert metadata_args == [
            "--metadata=subtitle:Design Report",
            "--metadata=date:Rev B | 2026-11-27 | jlcpcb",
        ]

    def test_unquoted_cover_scalar_is_left_to_pandoc(self, tmp_path: Path) -> None:
        """Older or custom templates may emit plain YAML scalars; nothing is overridden."""
        cmd = self._pandoc_cmd(
            "---\ntitle: Panel A\\B\nsubtitle: Design Report\n---\n\n# Body\n",
            tmp_path,
        )

        assert not [a for a in cmd if a.startswith("--metadata=")]

    @pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is unavailable")
    @pytest.mark.parametrize(
        ("project_name", "latex_title"),
        [
            ("Board 05", "Board 05"),
            ('ctl "bench"', 'ctl "bench"'),
            ('Capteur "été"', 'Capteur "été"'),
            ("Panel A\\B", "Panel A\\textbackslash B"),
            ("double\\\\slash", "double\\textbackslash\\textbackslash slash"),
            ("False", "False"),
        ],
    )
    def test_cover_title_reaches_latex_literally(
        self, tmp_path: Path, project_name: str, latex_title: str
    ) -> None:
        """The generator's title and revision survive as literal LaTeX text.

        Only what pandoc hands to TeX is checked here; the report's Markdown
        is unchanged by Issue #10630.
        """
        md_path = self._rendered_report(tmp_path, project_name)

        tex = self._latex_output(md_path, tmp_path)

        assert f"\\title{{{latex_title}}}" in tex
        assert "\\date{Rev B \\textbar 2026-11-27 \\textbar jlcpcb}" in tex

    @pytest.mark.skipif(shutil.which("pandoc") is None, reason="Pandoc is unavailable")
    def test_cover_revision_reaches_latex_literally(self, tmp_path: Path) -> None:
        """A literal backslash in the revision is guarded by the same mechanism."""
        md_path = self._rendered_report(tmp_path, "Board 05", revision="A\\B")

        tex = self._latex_output(md_path, tmp_path)

        assert "\\date{Rev A\\textbackslash B \\textbar 2026-11-27 \\textbar jlcpcb}" in tex

    @pytest.mark.skipif(
        shutil.which("pandoc") is None or shutil.which("xelatex") is None,
        reason="Pandoc or XeLaTeX is unavailable",
    )
    @pytest.mark.parametrize(
        "project_name",
        ["Board 05", 'ctl "bench"', 'Capteur "été"', "Panel A\\B", "double\\\\slash"],
    )
    def test_pdf_text_preserves_literal_title_and_revision(
        self, tmp_path: Path, project_name: str
    ) -> None:
        """A real XeLaTeX run writes a PDF whose text keeps title and revision.

        TeX's default font draws a straight double quote as a typographic one,
        so only the shape of a quote may differ from the literal name.
        """
        import pdfplumber

        from kicad_tools.report import renderers

        md_path = self._rendered_report(tmp_path, project_name)
        pdf_path = tmp_path / "report.pdf"

        renderers.render_pdf_pandoc(md_path, pdf_path)

        assert pdf_path.is_file()
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        first_line = text.lstrip().splitlines()[0]
        assert first_line.translate(QUOTE_SHAPES) == project_name
        assert "Rev B | 2026-11-27 | jlcpcb" in text


# ---------------------------------------------------------------------------
# count_pdf_images tests (issue #4023 bundle-integrity check)
# ---------------------------------------------------------------------------


class TestCountPdfImages:
    """Tests for the count_pdf_images integrity helper."""

    def test_counts_image_xobjects(self, tmp_path: Path) -> None:
        """Counts /Subtype /Image XObject declarations in the raw PDF bytes."""
        from kicad_tools.report.renderers import count_pdf_images

        pdf = tmp_path / "with_images.pdf"
        pdf.write_bytes(
            b"%PDF-1.5\n"
            b"<< /Type /XObject /Subtype /Image >>\n"
            b"<< /Type /XObject /Subtype/Image >>\n"  # no space variant
            b"%%EOF\n"
        )
        assert count_pdf_images(pdf) == 2

    def test_zero_for_imageless_pdf(self, tmp_path: Path) -> None:
        """A PDF with no image XObjects returns 0 (the broken-bundle signature)."""
        from kicad_tools.report.renderers import count_pdf_images

        pdf = tmp_path / "no_images.pdf"
        pdf.write_bytes(b"%PDF-1.5\n<< /Type /Page >>\n%%EOF\n")
        assert count_pdf_images(pdf) == 0

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        """A non-existent path returns 0 rather than raising."""
        from kicad_tools.report.renderers import count_pdf_images

        assert count_pdf_images(tmp_path / "does_not_exist.pdf") == 0

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        """count_pdf_images accepts a plain str path."""
        from kicad_tools.report.renderers import count_pdf_images

        pdf = tmp_path / "s.pdf"
        pdf.write_bytes(b"<< /Subtype /Image >>")
        assert count_pdf_images(str(pdf)) == 1


# ---------------------------------------------------------------------------
# CSS tests
# ---------------------------------------------------------------------------


class TestCss:
    """Tests for the CSS template."""

    def test_css_file_exists(self) -> None:
        """The report.css template file exists on disk."""
        from kicad_tools.report.renderers import _TEMPLATES_DIR

        css_path = _TEMPLATES_DIR / "report.css"
        assert css_path.exists()

    def test_css_classes_present(self) -> None:
        """CSS contains the required DRC badge classes."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert ".drc-error" in css
        assert ".drc-warning" in css
        assert ".drc-ok" in css

    def test_css_has_print_media_query(self) -> None:
        """CSS contains @media print rules for PDF layout."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert "@media print" in css

    def test_css_has_table_styles(self) -> None:
        """CSS contains table styling rules."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert "table" in css
        assert "table-layout" in css

    def test_css_has_image_constraints(self) -> None:
        """CSS constrains images with max-width."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert "max-width" in css

    def test_css_has_page_rule(self) -> None:
        """CSS contains @page rules with size, margins, and page numbers."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert "@page" in css
        assert "size: letter" in css
        assert "counter(page)" in css
        assert "@bottom-center" in css

    def test_css_has_cover_block_class(self) -> None:
        """CSS defines the .cover-block class with blue accent styling."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert ".cover-block" in css
        assert "#2563EB" in css

    def test_css_has_pcb_grid_class(self) -> None:
        """CSS defines the .pcb-grid class for side-by-side PCB layout."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert ".pcb-grid" in css
        assert "grid-template-columns" in css

    def test_css_no_h2_page_break_always(self) -> None:
        """CSS no longer forces page-break-before: always on every h2."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        # The old rule was "h2 { page-break-before: always; }" inside @media print.
        # Now h2 uses page-break-before: auto. Verify "always" is not applied to h2.
        import re

        # Find the h2 rule inside @media print and ensure it says "auto" not "always"
        # A simple check: "page-break-before: always" should NOT appear in h2 context
        h2_break_matches = re.findall(r"h2\s*\{[^}]*page-break-before:\s*always", css)
        assert len(h2_break_matches) == 0, "h2 should not have page-break-before: always"

    def test_css_has_bom_refs_class(self) -> None:
        """CSS defines the .bom-refs class for overflow control."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert ".bom-refs" in css
        assert "max-height" in css


# ---------------------------------------------------------------------------
# Internal helper tests
# ---------------------------------------------------------------------------


class TestEmbedImages:
    """Tests for the _embed_images helper."""

    def test_replaces_existing_png(self, figures_dir: Path) -> None:
        """Existing PNG src is replaced with a data URI."""
        from kicad_tools.report.renderers import _embed_images

        html = '<img src="front_copper.png" alt="test">'
        result = _embed_images(html, figures_dir)
        assert result.startswith('<img src="data:image/png;base64,')
        assert 'alt="test">' in result

    def test_preserves_missing_images(self, figures_dir: Path) -> None:
        """References to non-existent files are left unchanged."""
        from kicad_tools.report.renderers import _embed_images

        html = '<img src="nonexistent.png" alt="gone">'
        result = _embed_images(html, figures_dir)
        assert result == html

    def test_replaces_png_with_alt_before_src(self, figures_dir: Path) -> None:
        """Handles markdown-library output where alt appears before src."""
        from kicad_tools.report.renderers import _embed_images

        # This is the format Python-Markdown produces
        html = '<img alt="Front Copper" src="front_copper.png" />'
        result = _embed_images(html, figures_dir)
        assert "data:image/png;base64," in result
        assert 'src="front_copper.png"' not in result
        assert 'alt="Front Copper"' in result

    def test_handles_no_img_tags(self, figures_dir: Path) -> None:
        """HTML without img tags passes through unchanged."""
        from kicad_tools.report.renderers import _embed_images

        html = "<p>No images here</p>"
        result = _embed_images(html, figures_dir)
        assert result == html

    def test_strips_figures_prefix_from_src(self, figures_dir: Path) -> None:
        """src with a 'figures/' prefix does not double-nest the path."""
        from kicad_tools.report.renderers import _embed_images

        html = '<img alt="Front" src="figures/front.png" />'
        result = _embed_images(html, figures_dir)
        assert "data:image/png;base64," in result
        assert 'src="figures/front.png"' not in result

    def test_deeply_nested_prefix_left_unchanged(self, figures_dir: Path) -> None:
        """A deeply nested prefix like 'assets/figures/file.png' uses only the filename."""
        from kicad_tools.report.renderers import _embed_images

        # front.png exists in figures_dir, so using .name extracts it correctly
        html = '<img alt="Front" src="assets/figures/front.png" />'
        result = _embed_images(html, figures_dir)
        assert "data:image/png;base64," in result

    def test_non_png_with_figures_prefix_not_embedded(self, figures_dir: Path) -> None:
        """Non-PNG files with a figures/ prefix are left unchanged."""
        from kicad_tools.report.renderers import _embed_images

        html = '<img alt="Diagram" src="figures/diagram.svg" />'
        result = _embed_images(html, figures_dir)
        assert result == html


class TestWrapTables:
    """Tests for the _wrap_tables helper."""

    def test_wraps_table_in_div(self) -> None:
        """Tables are wrapped in a div with class table-wrapper."""
        from kicad_tools.report.renderers import _wrap_tables

        html = "<table><tr><td>cell</td></tr></table>"
        result = _wrap_tables(html)
        assert '<div class="table-wrapper"><table>' in result
        assert "</table></div>" in result

    def test_wraps_multiple_tables(self) -> None:
        """Multiple tables in the document are each wrapped."""
        from kicad_tools.report.renderers import _wrap_tables

        html = "<table><tr><td>1</td></tr></table><p>gap</p><table><tr><td>2</td></tr></table>"
        result = _wrap_tables(html)
        assert result.count('<div class="table-wrapper">') == 2
        assert result.count("</table></div>") == 2

    def test_no_table_passes_through(self) -> None:
        """HTML without tables passes through unchanged."""
        from kicad_tools.report.renderers import _wrap_tables

        html = "<p>No tables</p>"
        result = _wrap_tables(html)
        assert result == html


class TestWrapHtml:
    """Tests for the _wrap_html helper."""

    def test_produces_complete_document(self) -> None:
        """Output is a complete HTML5 document."""
        from kicad_tools.report.renderers import _wrap_html

        result = _wrap_html("<p>body</p>", "body { color: red; }")
        assert "<!DOCTYPE html>" in result
        assert "<html lang=" in result
        assert "<meta charset=" in result
        assert "<p>body</p>" in result
        assert "body { color: red; }" in result

    def test_css_in_style_tag(self) -> None:
        """CSS is embedded in a <style> tag, not a <link>."""
        from kicad_tools.report.renderers import _wrap_html

        css = ".test { color: blue; }"
        result = _wrap_html("<p>test</p>", css)
        assert "<style>" in result
        assert css in result
        assert "<link" not in result


class TestWeasprintAvailable:
    """Tests for the _weasyprint_available guard."""

    def test_returns_false_when_not_installed(self) -> None:
        """Returns False when weasyprint cannot be imported."""
        import sys

        # Temporarily hide weasyprint if it exists
        saved = sys.modules.get("weasyprint")
        sys.modules["weasyprint"] = None  # type: ignore[assignment]
        try:
            # Need to reload to reset the check
            from kicad_tools.report.renderers import _weasyprint_available

            # Mock the import to raise ImportError
            with patch.dict(sys.modules, {"weasyprint": None}):
                # _weasyprint_available tries 'import weasyprint', which will
                # raise ImportError when module is None in sys.modules
                result = _weasyprint_available()
                assert result is False
        finally:
            if saved is not None:
                sys.modules["weasyprint"] = saved
            else:
                sys.modules.pop("weasyprint", None)

    def test_weasyprint_oserror_treated_as_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """libgobject dlopen failure (OSError) must degrade like ImportError.

        Regression test for issue #3205: on macOS / minimal Linux hosts where
        libgobject-2.0-0 is missing, ``import weasyprint`` raises ``OSError``
        from ``ctypes.CDLL`` rather than ``ImportError``. The guard in
        ``_weasyprint_available()`` must catch both so the whole export
        pipeline degrades gracefully instead of bubbling an unhandled
        exception that taints ``result.errors``.
        """
        import builtins
        import sys

        import kicad_tools.report.renderers as renderers_mod

        # Reset the one-shot hint guard so this test always exercises the warn path.
        monkeypatch.setattr(renderers_mod, "_LIBGOBJECT_HINT_LOGGED", False)

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "weasyprint":
                raise OSError(
                    "cannot load library 'libgobject-2.0-0': "
                    "dlopen(libgobject-2.0.dylib, 0x0002): tried: ..."
                )
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Ensure the import is re-attempted (not served from sys.modules cache)
        saved = sys.modules.pop("weasyprint", None)
        try:
            from kicad_tools.report.renderers import (
                _weasyprint_available,
                pdf_renderer_available,
            )

            # Must not raise — the OSError must be caught and converted to False.
            assert _weasyprint_available() is False

            # And pdf_renderer_available must not raise either: it falls
            # through to pandoc (if installed) or returns None.
            renderer = pdf_renderer_available()
            assert renderer in (None, "pandoc")
        finally:
            if saved is not None:
                sys.modules["weasyprint"] = saved

    def test_libgobject_hint_logged_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The libgobject install hint is emitted once per process, not on every call."""
        import builtins
        import logging
        import sys

        import kicad_tools.report.renderers as renderers_mod

        monkeypatch.setattr(renderers_mod, "_LIBGOBJECT_HINT_LOGGED", False)

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "weasyprint":
                raise OSError("cannot load library 'libgobject-2.0-0'")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        sys.modules.pop("weasyprint", None)

        from kicad_tools.report.renderers import _weasyprint_available

        with caplog.at_level(logging.WARNING, logger="kicad_tools.report.renderers"):
            _weasyprint_available()
            _weasyprint_available()
            _weasyprint_available()

        libgobject_warnings = [r for r in caplog.records if "libgobject" in r.getMessage()]
        assert len(libgobject_warnings) == 1, (
            f"Expected exactly one libgobject hint, got {len(libgobject_warnings)}: "
            f"{[r.getMessage() for r in libgobject_warnings]}"
        )
