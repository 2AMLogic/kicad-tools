"""Default silk-legalisation wiring across the pipeline (Issue #9799, #9795 phase 3).

``kct route`` / ``route-auto`` / ``optimize-placement`` / ``export`` run the
shared :func:`~kicad_tools.drc.repair_silkscreen.legalize_silk` pass on the
board they publish, after copper is written and before any gate judges silk.
The command output names the pass's result and every unresolved item; vias
are never clipped, moved or reported.  ``--no-silk-fix`` on each command
leaves authored silk byte-identical, and route-then-export is idempotent.
"""

from __future__ import annotations

import json
import shutil
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from kicad_tools._shapely import has_shapely

pytestmark = pytest.mark.skipif(not has_shapely(), reason="shapely not installed")

from kicad_tools.analysis.net_status import NetStatusAnalyzer  # noqa: E402
from kicad_tools.cli import main as kct_main  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "projects"

#: Authored defects injected into the shared test-project board: a
#: sub-minimum silk line (jlcpcb floor 0.15mm), a via a silk line crosses
#: (vias are never touched, #9795) and an unclippable arc over a PTH drill.
DEFECTS = """
	(gr_line (start 105 55) (end 115 55) (stroke (width 0.08) (type default)) (layer "F.SilkS") (uuid "silk-thin"))
	(gr_line (start 146 57) (end 150 57) (stroke (width 0.15) (type default)) (layer "F.SilkS") (uuid "silk-over-via"))
	(via (at 148 57) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-under-silk"))
	(footprint "Test:PTH" (layer "F.Cu") (uuid "fp-u1-uuid") (at 130 58)
		(property "Reference" "U9" (at 0 -2.2) (layer "F.SilkS") (uuid "ref-u9"))
		(property "Value" "PTH" (at 0 2.2) (layer "F.Fab") (uuid "val-u9"))
		(fp_arc (start -1 0) (mid 0 -1) (end 1 0) (stroke (width 0.15) (type default)) (layer "F.SilkS") (uuid "arc-unfix"))
		(pad "1" thru_hole circle (at 0 -1) (size 1.2 1.2) (drill 0.8) (layers "*.Cu") (net 1 "VIN") (uuid "pad-u9"))
	)
"""


def _defect_board(
    directory: Path, name: str = "board.kicad_pcb", *, extra_copper: str = ""
) -> Path:
    """The shared test project plus the authored silk defects above."""
    directory.mkdir(parents=True, exist_ok=True)
    for suffix in (".kicad_sch", ".kicad_pro"):
        shutil.copy(FIXTURE / f"test_project{suffix}", directory / f"board{suffix}")
    source = (FIXTURE / "test_project.kicad_pcb").read_text()
    closing = source.rstrip().rfind(")")
    pcb = directory / name
    pcb.write_text(source[:closing] + DEFECTS + extra_copper + source[closing:])
    return pcb


def _assert_net1_open(board: Path) -> None:
    """The input board starts with exactly one open NET1 connection."""
    signal = NetStatusAnalyzer(board, strict=True).analyze().get_net("NET1")
    assert signal is not None and signal.open_connections == 1


def _assert_routed_connectivity(out: Path) -> None:
    """The saved board connects all NET1 pads and completes every net."""
    routed = NetStatusAnalyzer(out, strict=True).analyze()
    routed_signal = routed.get_net("NET1")
    assert routed_signal is not None and routed_signal.connected_count == 3
    assert routed.complete_count == routed.total_nets == 3
    assert not routed.stranded


def _silk_lines(path: Path) -> list[tuple[str, float, float, float, float, float]]:
    """``(uuid, width, x1, y1, x2, y2)`` of every board-level F.SilkS line."""
    board = PCB.load(str(path))
    out = []
    for graphic in board.graphics:
        if graphic.layer == "F.SilkS" and graphic.graphic_type == "line":
            out.append(
                (
                    str(graphic.uuid),
                    float(graphic.stroke_width or 0.0),
                    graphic.start[0],
                    graphic.start[1],
                    graphic.end[0],
                    graphic.end[1],
                )
            )
    return out


def _silk_widths(path: Path) -> list[float]:
    """Stroke widths of every board-level F.SilkS line."""
    return [entry[1] for entry in _silk_lines(path)]


def _mock_assembly(monkeypatch, tmp_path: Path) -> None:
    """Replace AssemblyPackage.export so no kicad-cli / parts data is needed."""
    from kicad_tools.export import assembly

    def fake_export(self, output_dir=None):
        od = Path(output_dir) if output_dir else self.config.output_dir
        od.mkdir(parents=True, exist_ok=True)
        bom_path = od / f"bom_{self.manufacturer}.csv"
        bom_path.write_text("Comment,Designator,Footprint,LCSC Part #\n")
        cpl_path = od / f"cpl_{self.manufacturer}.csv"
        cpl_path.write_text("Designator,Val,Package,Mid X,Mid Y,Rotation,Layer\n")
        return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom_path, pnp_path=cpl_path)

    monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)


def _export_json(board: Path, out_dir: Path, capsys, *extra: str) -> dict:
    capsys.readouterr()
    assert (
        kct_main(
            [
                "export",
                str(board),
                "--mfr",
                "jlcpcb",
                "-o",
                str(out_dir),
                "--no-report",
                "--no-project-zip",
                "--skip-preflight",
                "--format",
                "json",
                *extra,
            ]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# kct route
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def routed_silk_witness(tmp_path_factory):
    directory = tmp_path_factory.mktemp("routed-silk")
    # Connect the silk-crossing via to D1.2 so this board needs no GND pour.
    board = _defect_board(
        directory,
        extra_copper='(segment (start 141 50) (end 148 57) (width 0.25) (layer "F.Cu") '
        '(net 2) (uuid "silk-via-gnd-feed"))\n',
    )
    out = directory / "routed.kicad_pcb"
    output = StringIO()
    with redirect_stdout(output):
        rc = kct_main(
            [
                "route",
                str(board),
                "-o",
                str(out),
                "--seed",
                "7",
                "--skip-drc",
                "--no-auto-pour",
                "--no-chain-complete",
            ]
        )

    assert rc == 0
    return board, out, output.getvalue()


def test_route_runs_pass_and_names_unresolved(routed_silk_witness):
    board, out, text = routed_silk_witness
    _assert_net1_open(board)
    assert "--- Silk legalisation (default pass, jlcpcb) ---" in text
    assert "unresolved [silk_over_hole]" in text
    assert "U9 (fp_arc)" in text  # the unfixable item is named

    _assert_routed_connectivity(out)
    widths = _silk_widths(out)
    assert widths  # the authored lines survived the write
    assert all(w >= 0.15 for w in widths)  # ...and meet the jlcpcb floor


@pytest.mark.parametrize(
    "flags",
    [[], ["--no-auto-layers"], ["--adaptive-rules"], ["--adaptive-rules", "--no-auto-layers"]],
    ids=["layer-escalation", "direct", "combined-escalation", "rule-relaxation"],
)
def test_route_json_reports_published_silk(tmp_path, capsys, flags):
    board = _defect_board(tmp_path / "source")
    out = tmp_path / "routed.kicad_pcb"
    rc = kct_main(
        [
            "route",
            str(board),
            "-o",
            str(out),
            "--seed",
            "7",
            "--skip-drc",
            "--format",
            "json",
            "--quiet",
            *flags,
        ]
    )
    assert rc == 0
    repair = json.loads(capsys.readouterr().out)["silk_fix"]
    assert Path(repair["board"]) == out
    assert repair["manufacturer"] == "jlcpcb"
    assert repair["total_fixed"] > 0
    assert any(
        row["kind"] == "silk_over_hole" and "U9" in row["item"] for row in repair["unresolved"]
    )


@pytest.mark.parametrize("flag", ["--no-silk-fix", "--dry-run"])
def test_route_json_skips_silk_when_board_is_untouched(tmp_path, capsys, flag):
    board = _defect_board(tmp_path / "source")
    out = tmp_path / "routed.kicad_pcb"
    assert (
        kct_main(
            [
                "route",
                str(board),
                "-o",
                str(out),
                "--seed",
                "7",
                "--skip-drc",
                "--format",
                "json",
                "--quiet",
                flag,
            ]
        )
        == 0
    )
    assert "silk_fix" not in json.loads(capsys.readouterr().out)
    if flag == "--dry-run":
        assert not out.exists()
    else:
        assert next(line[1] for line in _silk_lines(out) if line[2] == 105.0) == 0.08


@pytest.mark.parametrize(
    "command,flags",
    [("route-auto", ["--net", "NET1"]), ("optimize-placement", ["--max-iterations", "3"])],
)
@pytest.mark.parametrize(
    "source_fab,destination_fab,width", [("jlcpcb", "oshpark", 0.15), ("oshpark", None, 0.127)]
)
def test_output_silk_uses_source_fabricator(
    tmp_path, capsys, command, flags, source_fab, destination_fab, width
):
    board = _defect_board(tmp_path / "source")
    out = tmp_path / "shipping" / "board.kicad_pcb"
    out.parent.mkdir()
    for directory, fab in ((board.parent, source_fab), (out.parent, destination_fab)):
        if fab:
            (directory / "project.kct").write_text(
                'kct_version: "1.0"\nproject:\n  name: silk-test\n'
                f"requirements:\n  manufacturing:\n    target_fab: {fab}\n"
            )
    assert kct_main([command, str(board), "-o", str(out), "--format", "json", *flags]) == 0
    assert json.loads(capsys.readouterr().out)["silk_fix"]["manufacturer"] == source_fab
    assert next(line[1] for line in _silk_lines(out) if line[2] == 105.0) == pytest.approx(width)


def test_route_reports_no_unresolved_for_via_only_crossing(routed_silk_witness):
    _, out, text = routed_silk_witness
    unresolved = [line for line in text.splitlines() if "unresolved [" in line]
    (pth_arc,) = unresolved
    assert "unresolved [silk_over_hole]" in pth_arc and "U9 (fp_arc)" in pth_arc
    assert "via" not in pth_arc
    # Check both physical witnesses on the board the route command wrote.
    routed = PCB.load(out)
    origin_x, origin_y = routed.board_origin
    center = (148.0 - origin_x, 57.0 - origin_y)
    (via,) = [v for v in routed.vias if v.net_name == "GND" and v.position == pytest.approx(center)]
    assert (via.size, via.drill) == pytest.approx((0.6, 0.3))
    (line,) = [entry for entry in _silk_lines(out) if entry[2:4] == pytest.approx((146.0, 57.0))]
    assert line[1:] == pytest.approx((0.15, 146.0, 57.0, 150.0, 57.0))


def test_route_no_silk_fix_leaves_authored_silk(tmp_path: Path, capsys):
    # Connect the silk-crossing via to D1.2 so this board needs no GND pour.
    board = _defect_board(
        tmp_path / "run",
        extra_copper='(segment (start 141 50) (end 148 57) (width 0.25) (layer "F.Cu") '
        '(net 2) (uuid "silk-via-gnd-feed"))\n',
    )
    out = tmp_path / "run" / "routed.kicad_pcb"

    rc = kct_main(
        [
            "route",
            str(board),
            "-o",
            str(out),
            "--seed",
            "7",
            "--skip-drc",
            "--no-auto-pour",
            "--no-chain-complete",
            "--no-silk-fix",
        ]
    )

    assert rc == 0
    assert "Silk legalisation" not in capsys.readouterr().out
    # Real routing still ran: the opt-out arm must connect what the input left open.
    _assert_net1_open(board)
    _assert_routed_connectivity(out)
    # KiCad can assign UUIDs to the fixture's named lines during the route.
    authored = sorted(line[1:] for line in _silk_lines(board))
    assert (0.08, 105.0, 55.0, 115.0, 55.0) in authored
    assert sorted(line[1:] for line in _silk_lines(out)) == authored


# ---------------------------------------------------------------------------
# Idempotence across the pipeline: route, then export
# ---------------------------------------------------------------------------


def test_route_then_export_reports_zero_additional_edits(tmp_path: Path, monkeypatch, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "routed.kicad_pcb"
    assert kct_main(["route", str(board), "-o", str(out), "--seed", "7", "--skip-drc"]) == 0
    routed_silk = sorted(_silk_lines(out))
    before = out.read_bytes()

    _mock_assembly(monkeypatch, tmp_path)
    doc = _export_json(out, tmp_path / "run" / "manufacturing", capsys)

    assert doc["silk_fix"]["applied"] is True
    assert doc["silk_fix"]["total_fixed"] == 0  # zero additional edits
    assert doc["silk_fix"]["unresolved"]  # the arc is still named, never waived
    assert sorted(_silk_lines(out)) == routed_silk  # routed silk preserved
    assert out.read_bytes() == before  # a zero-edit board is not re-saved


def test_export_repairs_authored_defect_in_place(tmp_path: Path, monkeypatch, capsys):
    board = _defect_board(tmp_path / "run")

    _mock_assembly(monkeypatch, tmp_path)
    doc = _export_json(board, tmp_path / "run" / "manufacturing", capsys)

    assert doc["silk_fix"]["applied"] is True
    assert doc["silk_fix"]["total_fixed"] >= 1
    assert doc["silk_fix"]["unresolved"]
    assert all(w >= 0.15 for w in _silk_widths(board))  # checks, plots and
    # hashes below read these same repaired bytes.


def test_export_no_silk_fix_leaves_board_byte_identical(tmp_path: Path, monkeypatch, capsys):
    board = _defect_board(tmp_path / "run")
    before = board.read_bytes()

    _mock_assembly(monkeypatch, tmp_path)
    doc = _export_json(board, tmp_path / "run" / "manufacturing", capsys, "--no-silk-fix")

    assert "silk_fix" not in doc
    assert board.read_bytes() == before  # authored silk byte-identical


def test_export_non_default_profile_min_width(tmp_path: Path, monkeypatch, capsys):
    """A profile whose silk minimum differs widens to ITS floor, not jlcpcb's."""
    board = _defect_board(tmp_path / "run")

    _mock_assembly(monkeypatch, tmp_path)
    capsys.readouterr()
    assert (
        kct_main(
            [
                "export",
                str(board),
                "--mfr",
                "oshpark",
                "-o",
                str(tmp_path / "run" / "mfg"),
                "--no-report",
                "--no-project-zip",
                "--skip-preflight",
                "--format",
                "json",
            ]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["silk_fix"]["applied"] is True

    # oshpark's 2-layer minimum silk width is 0.127mm (jlcpcb's is 0.15).
    (thin,) = [entry for entry in _silk_lines(board) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.127)


# ---------------------------------------------------------------------------
# kct route-auto
# ---------------------------------------------------------------------------


def test_route_auto_legalizes_published_output(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "auto.kicad_pcb"

    capsys.readouterr()
    rc = kct_main(["route-auto", str(board), "--net", "NET1", "-o", str(out), "--format", "json"])

    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["silk_fix"]["applied"] is True
    assert doc["silk_fix"]["total_fixed"] >= 1
    assert [u["kind"] for u in doc["silk_fix"]["unresolved"]] == ["silk_over_hole"]
    assert all(w >= 0.15 for w in _silk_widths(out))


def test_route_auto_no_silk_fix_preserves_authored_silk(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "auto.kicad_pcb"

    capsys.readouterr()
    rc = kct_main(
        [
            "route-auto",
            str(board),
            "--net",
            "NET1",
            "-o",
            str(out),
            "--format",
            "json",
            "--no-silk-fix",
        ]
    )

    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert "silk_fix" not in doc
    (thin,) = [entry for entry in _silk_lines(out) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.08)


def test_route_auto_without_output_leaves_input_alone(tmp_path: Path, capsys):
    """A run with no board to publish must leave the input alone."""
    board = _defect_board(tmp_path / "run")
    before = board.read_bytes()

    capsys.readouterr()
    rc = kct_main(["route-auto", str(board), "--net", "NET1", "--format", "json"])

    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert "silk_fix" not in doc
    assert board.read_bytes() == before


def test_failed_route_auto_preserves_previous_output(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = _defect_board(tmp_path / "previous")
    before = out.read_bytes()

    rc = kct_main(
        ["route-auto", str(board), "--net", "MISSING", "-o", str(out), "--format", "json"]
    )

    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["nets"][0]["success"] is False
    assert "silk_fix" not in doc
    assert out.read_bytes() == before


# ---------------------------------------------------------------------------
# kct optimize-placement
# ---------------------------------------------------------------------------


def test_optimize_placement_legalizes_written_board(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "placed.kicad_pcb"

    rc = kct_main(["optimize-placement", str(board), "-o", str(out), "--max-iterations", "3"])

    assert rc == 0
    text = capsys.readouterr().out
    assert "--- Silk legalisation (default pass" in text
    assert "unresolved [silk_over_hole]" in text
    assert all(w >= 0.15 for w in _silk_widths(out))


def test_optimize_placement_no_silk_fix_bypasses_silk_edits(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "placed.kicad_pcb"

    rc = kct_main(
        [
            "optimize-placement",
            str(board),
            "-o",
            str(out),
            "--max-iterations",
            "3",
            "--no-silk-fix",
        ]
    )

    assert rc == 0
    assert "Silk legalisation" not in capsys.readouterr().out
    # The opt-out bypasses the reference-move repairs too: the authored
    # sub-minimum line survives byte-identical (placement moved, silk not).
    (thin,) = [entry for entry in _silk_lines(out) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.08)


def test_optimize_placement_json_names_the_pass(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "placed.kicad_pcb"

    capsys.readouterr()
    rc = kct_main(
        [
            "optimize-placement",
            str(board),
            "-o",
            str(out),
            "--max-iterations",
            "3",
            "--format",
            "json",
        ]
    )

    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["silk_fix"]["applied"] is True
    assert doc["silk_fix"]["total_fixed"] >= 1
    assert doc["silk_fix"]["unresolved"]


# ---------------------------------------------------------------------------
# kct readiness --generate: the pass runs on the staged candidate BEFORE the
# gates judge silk, so checks, plots and hashes consume the repaired bytes.
# ---------------------------------------------------------------------------


def _readiness_board(tmp_path: Path) -> tuple[Path, Path]:
    """A ready board directory (fake engines) holding the real defect PCB."""
    from tests.test_kct_readiness_cmd import make_board

    board_dir = make_board(tmp_path)
    pcb = board_dir / "output" / "demo_routed.kicad_pcb"
    source = (FIXTURE / "test_project.kicad_pcb").read_text()
    closing = source.rstrip().rfind(")")
    pcb.write_text(source[:closing] + DEFECTS + source[closing:])
    return board_dir, pcb


def _preserving_engines(board_dir: Path):
    """FakeEngines whose refill keeps the candidate bytes (#9799).

    The shared fake's refill replaces the board with a stub line, which is
    fine for gate-order tests but leaves the default silk pass nothing real
    to legalise.  This subclass refills as a no-op and reports the board's
    own (empty) filled areas, so the zone_fill gate still passes.
    """
    from tests.test_kct_readiness_cmd import FakeEngines

    class PreservingEngines(FakeEngines):
        def refill(self, pcb: Path):
            self.calls.append("refill")
            from kicad_tools.cli.readiness_cmd import EngineRun

            return EngineRun(ok=True)

        def fill_areas(self, pcb: Path) -> dict[str, float]:
            return {}

    return PreservingEngines(board_dir)


def test_readiness_generate_legalizes_staged_candidate(tmp_path: Path):
    from tests.test_kct_readiness_cmd import run

    board_dir, pcb = _readiness_board(tmp_path)
    fake = _preserving_engines(board_dir)

    code, report = run(board_dir, fake)

    assert code == 0
    assert report["status"] == "ready"
    assert report["metrics"]["silk_fix"]["applied"] is True
    assert report["metrics"]["silk_fix"]["total_fixed"] >= 1
    assert [u["kind"] for u in report["metrics"]["silk_fix"]["unresolved"]] == ["silk_over_hole"]
    # The published board carries the repaired geometry the checks, plots and
    # hashes consumed.
    assert all(w >= 0.15 for w in _silk_widths(pcb))


def test_readiness_generate_no_silk_fix_keeps_candidate_silk(tmp_path: Path):
    from tests.test_kct_readiness_cmd import run

    board_dir, pcb = _readiness_board(tmp_path)

    code, report = run(board_dir, _preserving_engines(board_dir), "--no-silk-fix")

    assert code == 0
    assert report["status"] == "ready"
    assert report["metrics"]["silk_fix"] is None
    (thin,) = [entry for entry in _silk_lines(pcb) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.08)  # authored silk byte-identical
