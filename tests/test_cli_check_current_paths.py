"""CLI tests for ``kct check --current-paths <path>`` (Issue #4980/#5124).

Verifies the ``--current-paths`` flag is wired into ``check_cmd.py`` and
threaded into ``DRCChecker`` (via the new ``check_path_ampacity`` method) so
``PathAmpacityRule`` is reachable from ``kct check`` -- the second deferred
item from PR #5125 (Issue #5124's Acceptance Criterion 2).

Coverage mirrors ``tests/test_cli_check_net_class_map.py`` /
``tests/test_cli_check_ampacity_net_class_map.py``:

1. **Graceful degradation**: without ``--current-paths`` (and no sidecar on
   disk), ``path_ampacity`` reports zero violations.
2. **Explicit flag wiring**: an adequate trunk + a declared low-current
   sense spur passes; narrowing the trunk below its own declared-current
   IPC-2221 floor fails -- the issue's core T-network acceptance test,
   exercised through the CLI rather than the rule directly.
3. **Sidecar auto-discovery**: a bare ``current_paths.json`` next to the
   board is picked up with no flag, mirroring ``--net-class-map``'s
   auto-discovery contract (Issue #5124 AC2).
4. **Endpoint fail-closed**: a spec whose declared pad no longer resolves
   reports an error, not a silent pass.
5. **Error paths**: missing explicit file, malformed JSON, and the
   ``--current-paths``/``--no-current-paths`` mutual-exclusivity guard.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint, dump_current_path_specs

# IPC-2221 external 1oz floor for the trunk's declared 15A is ~12.585mm
# (matches tests/test_cli_check_ampacity_net_class_map.py's own 1oz-external
# reference value) and for the sense spec's declared 0.01A is ~0.0005mm --
# negligible next to any physically routable trace.
ADEQUATE_TRUNK_WIDTH_MM = 13.0
NARROW_TRUNK_WIDTH_MM = 2.0
SENSE_WIDTH_MM = 0.2

# 15A J1->J2 trunk plus a low-current J1->U3 sense spur, all on one net.
# Raw-text fixture (mirrors tests/test_router_io.py's footprint/pad style)
# rather than the Python PCB-building API: footprints appended via the
# private ``_footprints`` list are not round-tripped by ``PCB.save()``, and
# the CLI under test always loads the board fresh from disk. Geometry
# mirrors tests/test_path_ampacity_check.py::_t_network_pcb -- a single
# T-junction at (60, 50) keeps the net's copper a tree (no cycle), so both
# declared paths resolve cleanly.
_T_NETWORK_PCB_TEMPLATE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (gr_rect (start 0 0) (end 200 120)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "TH:Pad"
    (layer "F.Cu")
    (at 20 50)
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
  )
  (footprint "TH:Pad"
    (layer "F.Cu")
    (at 120 50)
    (fp_text reference "J2" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
  )
  (footprint "TH:Pad"
    (layer "F.Cu")
    (at 60 70)
    (fp_text reference "U3" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
  )
  (segment (start 20 50) (end 60 50) (width {trunk_width}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000001"))
  (segment (start 60 50) (end 120 50) (width {trunk_width}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000002"))
  (segment (start 60 50) (end 60 70) (width {sense_width}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000003"))
)
"""


def _write_t_network_pcb(
    path: Path, *, trunk_width: float, sense_width: float = SENSE_WIDTH_MM
) -> Path:
    path.write_text(
        _T_NETWORK_PCB_TEMPLATE.format(trunk_width=trunk_width, sense_width=sense_width)
    )
    return path


def _trunk_spec(current_a: float = 15.0) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="TRUNK",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def _sense_spec(current_a: float = 0.01) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="SENSE",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("U3", "1"),
        continuous_a=current_a,
        reinforcement_eligible=False,
    )


def _write_sidecar(path: Path, specs: list[CurrentPathSpec]) -> Path:
    path.write_text(json.dumps(dump_current_path_specs(specs)))
    return path


def _path_ampacity_violations(report_path: Path) -> list[dict]:
    data = json.loads(report_path.read_text())
    return [v for v in data["violations"] if v.get("rule_id") == "path_ampacity"]


def _run_check(pcb: Path, report: Path, *, extra: list[str] | None = None) -> int:
    from kicad_tools.cli.check_cmd import main

    argv = [
        str(pcb),
        "--mfr",
        "jlcpcb",
        "--only",
        "path_ampacity",
        "--output",
        str(report),
        "--format",
        "json",
        "--allow-incomplete",
    ]
    if extra:
        argv.extend(extra)
    return main(argv)


@pytest.fixture
def adequate_pcb(tmp_path: Path) -> Path:
    return _write_t_network_pcb(tmp_path / "board.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM)


@pytest.fixture
def narrow_trunk_pcb(tmp_path: Path) -> Path:
    return _write_t_network_pcb(tmp_path / "board.kicad_pcb", trunk_width=NARROW_TRUNK_WIDTH_MM)


class TestGracefulDegradation:
    def test_no_flag_no_sidecar_is_clean_noop(self, adequate_pcb: Path, tmp_path: Path):
        """Without --current-paths (and nothing on disk), path_ampacity is silent."""
        report = tmp_path / "report.json"
        result = _run_check(adequate_pcb, report)
        assert result == 0
        assert _path_ampacity_violations(report) == []


class TestExplicitFlagWiring:
    """The issue's core T-network acceptance test, through the CLI."""

    def test_adequate_trunk_and_narrow_sense_passes(self, adequate_pcb: Path, tmp_path: Path):
        sidecar = _write_sidecar(tmp_path / "cp.json", [_trunk_spec(15.0), _sense_spec(0.01)])
        report = tmp_path / "report.json"

        result = _run_check(adequate_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 0
        assert _path_ampacity_violations(report) == []

    def test_narrowing_trunk_fails(self, narrow_trunk_pcb: Path, tmp_path: Path):
        sidecar = _write_sidecar(tmp_path / "cp.json", [_trunk_spec(15.0), _sense_spec(0.01)])
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 2  # errors present -> non-zero exit
        violations = _path_ampacity_violations(report)
        trunk_errors = [
            v for v in violations if "TRUNK" in v.get("items", []) and v["severity"] == "error"
        ]
        assert len(trunk_errors) == 2  # both trunk segments under-width
        # The sense spur's own tiny declared current is unaffected.
        assert not any(
            "SENSE" in v.get("items", []) for v in violations if v["severity"] == "error"
        )


class TestSidecarAutoDiscovery:
    def test_bare_sidecar_next_to_board_is_auto_loaded(
        self, narrow_trunk_pcb: Path, tmp_path: Path
    ):
        """A bare current_paths.json next to the board engages the rule
        with no flag -- mirrors --net-class-map's auto-discovery (#5124 AC2)."""
        _write_sidecar(tmp_path / "current_paths.json", [_trunk_spec(15.0)])
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report)

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert len(errors) == 2

    def test_no_current_paths_suppresses_auto_discovery(
        self, narrow_trunk_pcb: Path, tmp_path: Path
    ):
        _write_sidecar(tmp_path / "current_paths.json", [_trunk_spec(15.0)])
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--no-current-paths"])

        assert result == 0
        assert _path_ampacity_violations(report) == []


class TestEndpointFailClosed:
    @pytest.mark.parametrize("reverse", [False, True], ids=["sink", "source"])
    @pytest.mark.parametrize(
        ("shape", "angle", "chamfer", "start", "end", "connected"),
        [
            pytest.param("rect", 0, None, "17 18", "12.90 14.90", False, id="disconnected"),
            pytest.param("rect", 90, None, "6 8", "10.80 12.80", True, id="offset-land"),
            pytest.param("circle", 0, None, "17 18", "12.86 14.00", False, id="offset-circle"),
            pytest.param("oval", 0, None, "6 8", "10.79 13.70", True, id="offset-oval"),
            pytest.param(
                "roundrect", 0, ("bottom_right", 0.0, 0.14), "17 18", "12.67 14.67", False, id="cut"
            ),
            pytest.param(
                "roundrect", 0, ("top_left", 0.0, 0.14), "17 18", "12.67 14.67", True, id="intact"
            ),
            pytest.param(
                "roundrect",
                -90,
                ("top_right", 0.0, 0.14),
                "17 18",
                "12.67 14.67",
                False,
                id="rotated-cut",
            ),
            pytest.param(
                "roundrect",
                90,
                ("bottom_left", 0.0, 0.14),
                "6 8",
                "10.90 12.90",
                True,
                id="rotated-land",
            ),
            pytest.param(
                "roundrect",
                0,
                ("bottom_right", 0.5, 0.05),
                "17 18",
                "12.60 14.60",
                True,
                id="chamfer-replaces-round",
            ),
            pytest.param(
                "roundrect",
                0,
                ("top_left", 0.5, 0.05),
                "17 18",
                "12.60 14.60",
                False,
                id="uncut-corner-keeps-radius",
            ),
            pytest.param(
                "roundrect",
                0,
                ("bottom_right", 0.0, None),
                "17 18",
                "12.60 14.60",
                False,
                id="missing-chamfer-ratio",
            ),
            pytest.param(
                "roundrect",
                0,
                ("top_left", 0.0, None),
                "17 18",
                "12.60 14.60",
                True,
                id="missing-chamfer-ratio-intact",
            ),
            pytest.param(
                "roundrect",
                0,
                ("bottom_right", 0.0, None),
                "17 18",
                "12.50 14.50",
                True,
                id="missing-chamfer-ratio-inward",
            ),
            pytest.param(
                "roundrect",
                0,
                ("bottom_right", 0.0, 0.0),
                "17 18",
                "12.60 14.60",
                True,
                id="zero-chamfer-ratio",
            ),
            pytest.param(
                "roundrect",
                0,
                ("bottom_right", 0.0, 0.14),
                "17 18",
                "13.20 13.70",
                False,
                id="chamfer-off-side",
            ),
        ],
    )
    def test_contact_with_offset_plated_pad(
        self, tmp_path: Path, shape, angle, chamfer, start, end, connected, reverse
    ):
        # The plotted copper stays centred at (11.7, 13.7) in board coordinates.
        # At -90 degrees the local top-right cut moves to the board's bottom right.
        offset = {0: "-0.3 -0.3", 90: "0.3 -0.3", -90: "-0.3 0.3"}[angle]
        height = 1.0 if shape == "oval" else 2.0
        corner_tokens = ""
        if chamfer:
            corner, radius_ratio, cut_ratio = chamfer
            ratio_token = "" if cut_ratio is None else f" (chamfer_ratio {cut_ratio})"
            corner_tokens = f"(roundrect_rratio {radius_ratio}){ratio_token} (chamfer {corner})"
        pcb = tmp_path / "offset.kicad_pcb"
        pcb.write_text(f"""(kicad_pcb (version 20240108) (generator "test")
            (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)
            (44 "Edge.Cuts" user) (49 "F.Fab" user)) (setup) (net 0 "") (net 1 "SENSE")
            (gr_rect (start 1 1) (end 23 23) (stroke (width 0.1) (type default))
             (fill none) (layer "Edge.Cuts"))
            (footprint "P" (layer "F.Cu") (at {start}) (fp_text reference "P3" (at 0 0) (layer "F.Fab") hide)
             (pad "1" thru_hole rect (at 0 0) (size 2 2) (drill 0.4) (layers "*.Cu") (net 1 "SENSE")))
            (footprint "P" (layer "F.Cu") (at 12 14 {angle}) (fp_text reference "P2" (at 0 0) (layer "F.Fab") hide)
             (pad "1" thru_hole {shape} (at 0 0 {angle}) (size 2 {height}) {corner_tokens}
              (drill 0.4 (offset {offset})) (layers "*.Cu") (net 1 "SENSE")))
            (segment (start {start}) (end {end}) (width 0.20) (layer "F.Cu") (net 1)))""")
        source, sink = ("P2", "P3") if reverse else ("P3", "P2")
        spec = CurrentPathSpec(
            "branch", "SENSE", PathEndpoint(source, "1"), PathEndpoint(sink, "1"), 0.10
        )
        sidecar = _write_sidecar(tmp_path / "current_paths.json", [spec])
        report = tmp_path / "report.json"
        result = _run_check(pcb, report, extra=["--current-paths", str(sidecar), "--drc-only"])

        assert json.loads(report.read_text())["coverage"]["path_ampacity"]["status"] == "checked"
        assert result == (0 if connected else 2)
        violations = _path_ampacity_violations(report)
        if connected:
            assert violations == []
        else:
            errors = [v for v in violations if v["severity"] == "error"]
            assert len(errors) == 1
            assert "P2.1 has no routed copper touching it" in errors[0]["message"]

    @pytest.mark.parametrize(
        ("shape", "pad_tokens"),
        [
            pytest.param(
                "roundrect",
                "(roundrect_rratio 0.0) (chamfer_ratio -0.1) (chamfer bottom_right)",
                id="negative-ratio",
            ),
            pytest.param(
                "roundrect",
                "(roundrect_rratio 0.0) (chamfer_ratio 0.8) (chamfer bottom_right)",
                id="ratio-above-half",
            ),
            pytest.param(
                "roundrect",
                "(roundrect_rratio 0.0) (chamfer_ratio nan) (chamfer bottom_right)",
                id="nan-ratio",
            ),
            pytest.param(
                "roundrect",
                "(roundrect_rratio 0.0) (chamfer_ratio banana) (chamfer bottom_right)",
                id="non-numeric-ratio",
            ),
            pytest.param(
                "roundrect",
                "(roundrect_rratio 0.2) (chamfer_ratio 0.2) (chamfer bogus)",
                id="unknown-corner",
            ),
            pytest.param(
                "roundrect",
                "(roundrect_rratio banana)",
                id="non-numeric-rratio",
            ),
            pytest.param(
                "rect",
                "(chamfer_ratio 0.2) (chamfer bottom_right)",
                id="chamfer-on-rect",
            ),
        ],
    )
    def test_unusable_chamfer_or_ratio_fails_closed(
        self, tmp_path: Path, shape: str, pad_tokens: str
    ):
        pcb = tmp_path / "malformed.kicad_pcb"
        pcb.write_text(f"""(kicad_pcb (version 20240108) (generator "test")
            (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal)
            (44 "Edge.Cuts" user) (49 "F.Fab" user)) (setup) (net 0 "") (net 1 "SENSE")
            (gr_rect (start 1 1) (end 23 23) (stroke (width 0.1) (type default))
             (fill none) (layer "Edge.Cuts"))
            (footprint "P" (layer "F.Cu") (at 17 18) (fp_text reference "P3" (at 0 0) (layer "F.Fab") hide)
             (pad "1" thru_hole rect (at 0 0) (size 2 2) (drill 0.4) (layers "*.Cu") (net 1 "SENSE")))
            (footprint "P" (layer "F.Cu") (at 12 14) (fp_text reference "P2" (at 0 0) (layer "F.Fab") hide)
             (pad "1" thru_hole {shape} (at 0 0) (size 2 2) {pad_tokens}
              (drill 0.4 (offset -0.3 -0.3)) (layers "*.Cu") (net 1 "SENSE")))
            (segment (start 17 18) (end 11.70 13.70) (width 0.20) (layer "F.Cu") (net 1)))""")
        spec = CurrentPathSpec(
            "branch", "SENSE", PathEndpoint("P3", "1"), PathEndpoint("P2", "1"), 0.10
        )
        sidecar = _write_sidecar(tmp_path / "current_paths.json", [spec])
        report = tmp_path / "report.json"
        result = _run_check(pcb, report, extra=["--current-paths", str(sidecar), "--drc-only"])

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert len(errors) == 1
        assert "P2.1 has no routed copper touching it" in errors[0]["message"]

    def test_moved_pad_reports_error_not_silent_pass(self, adequate_pcb: Path, tmp_path: Path):
        """A declared endpoint that no longer resolves is a visible error --
        the issue's fail-closed acceptance criterion, through the CLI."""
        bogus_spec = CurrentPathSpec(
            name="TRUNK",
            net_name="NET1",
            source=PathEndpoint("J1", "99"),  # pad "99" does not exist
            sink=PathEndpoint("J2", "1"),
            continuous_a=15.0,
        )
        sidecar = _write_sidecar(tmp_path / "cp.json", [bogus_spec])
        report = tmp_path / "report.json"

        result = _run_check(adequate_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert len(errors) == 1
        assert "unresolved" in errors[0]["message"]


class TestErrorPaths:
    def test_missing_explicit_file_returns_1(self, adequate_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        result = main([str(adequate_pcb), "--current-paths", "/does/not/exist/current_paths.json"])
        assert result == 1
        captured = capsys.readouterr()
        assert "current-paths" in captured.err
        assert "not found" in captured.err

    def test_malformed_explicit_json_returns_1(self, adequate_pcb: Path, tmp_path: Path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("not { valid json")
        from kicad_tools.cli.check_cmd import main

        result = main([str(adequate_pcb), "--current-paths", str(bad)])
        assert result == 1
        captured = capsys.readouterr()
        assert "current-paths" in captured.err.lower() or "parsing" in captured.err.lower()

    def test_malformed_autodiscovered_json_degrades_gracefully(
        self, adequate_pcb: Path, tmp_path: Path, capsys
    ):
        (tmp_path / "current_paths.json").write_text("not { valid json")
        report = tmp_path / "report.json"

        result = _run_check(adequate_pcb, report)

        assert result == 0
        captured = capsys.readouterr()
        assert "malformed" in captured.err.lower()
        assert _path_ampacity_violations(report) == []

    def test_flag_and_no_flag_mutually_exclusive(self, adequate_pcb: Path, tmp_path: Path, capsys):
        sidecar = _write_sidecar(tmp_path / "cp.json", [_trunk_spec(15.0)])
        from kicad_tools.cli.check_cmd import main

        result = main(
            [
                str(adequate_pcb),
                "--current-paths",
                str(sidecar),
                "--no-current-paths",
            ]
        )
        assert result == 1
        captured = capsys.readouterr()
        assert "--no-current-paths" in captured.err
        assert "--current-paths" in captured.err


@pytest.mark.parametrize("bridge", [False, True])
def test_cli_cross_layer_path_requires_via(tmp_path, bridge):
    # The bridging via is deliberately oversized (Issue #8745: an undersized
    # via is now a path_ampacity finding; see test_path_ampacity_check.py).
    board = tmp_path / "layers.kicad_pcb"
    copper = _T_NETWORK_PCB_TEMPLATE.format(
        trunk_width=ADEQUATE_TRUNK_WIDTH_MM, sense_width=SENSE_WIDTH_MM
    )
    copper = copper.replace(
        f'(end 120 50) (width {ADEQUATE_TRUNK_WIDTH_MM}) (layer "F.Cu")',
        f'(end 120 50) (width {ADEQUATE_TRUNK_WIDTH_MM}) (layer "B.Cu")',
    )
    assert f'(end 120 50) (width {ADEQUATE_TRUNK_WIDTH_MM}) (layer "B.Cu")' in copper
    if bridge:
        copper = (
            copper.rstrip()[:-1]
            + '(via (at 60 50) (size 8.5) (drill 8.0) (layers "F.Cu" "B.Cu") (net 1))\n)\n'
        )
    board.write_text(copper)
    before = board.read_bytes()
    sidecar = _write_sidecar(tmp_path / "cp.json", [_trunk_spec(), _sense_spec()])
    report = tmp_path / "report.json"
    status = _run_check(board, report, extra=["--current-paths", str(sidecar)])
    violations = _path_ampacity_violations(report)
    if bridge:
        assert status == 0
        assert violations == []
    else:
        assert status == 2
        assert any("unresolved" in v["message"].lower() for v in violations)
    assert board.read_bytes() == before


# --------------------------------------------------------------------------
# Pulsed / duty-cycled declarations through the CLI (Issue #4980)
# --------------------------------------------------------------------------


def _pulsed_trunk_spec(**overrides) -> CurrentPathSpec:
    """A 1A steady trunk with a 15A pulse -- 2% duty, 1ms, by default."""
    kwargs = {
        "name": "TRUNK",
        "net_name": "NET1",
        "source": PathEndpoint("J1", "1"),
        "sink": PathEndpoint("J2", "1"),
        "continuous_a": 1.0,
        "pulsed_a": 15.0,
        "duty_cycle": 0.02,
        "pulse_duration_s": 0.001,
        "reinforcement_eligible": True,
    }
    kwargs.update(overrides)
    return CurrentPathSpec(**kwargs)


class TestPulsedDeclarationsThroughCheck:
    """The sidecar's waveform fields reach the rule and change the verdict.

    The 2.0mm trunk board is the fixture for all three: it is comfortably
    wide for the ~2.34A RMS of a 15A/2%-duty pulse (~0.97mm required at
    1oz external) and far too narrow for 15A of continuous current
    (~12.59mm required).
    """

    def test_duty_cycled_pulse_passes_on_the_narrow_trunk(
        self, narrow_trunk_pcb: Path, tmp_path: Path
    ):
        sidecar = _write_sidecar(tmp_path / "cp.json", [_pulsed_trunk_spec(), _sense_spec(0.01)])
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 0
        assert [v for v in _path_ampacity_violations(report) if v["severity"] == "error"] == []

    def test_same_peak_without_duty_cycle_fails_the_same_board(
        self, narrow_trunk_pcb: Path, tmp_path: Path
    ):
        """Withholding the duty cycle is not a free pass: the peak is sized
        as continuous, and the under-width error names that assumption."""
        sidecar = _write_sidecar(
            tmp_path / "cp.json", [_pulsed_trunk_spec(duty_cycle=None), _sense_spec(0.01)]
        )
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert errors
        assert any("peak treated as continuous" in v["message"] for v in errors)

    def test_fusing_error_surfaces_through_the_cli(self, narrow_trunk_pcb: Path, tmp_path: Path):
        """A 25A pulse held for a full second melts 2.0mm of 1oz copper
        (~20.2A Onderdonk limit) even though its ~3.67A RMS is fine."""
        sidecar = _write_sidecar(
            tmp_path / "cp.json",
            [_pulsed_trunk_spec(pulsed_a=25.0, pulse_duration_s=1.0), _sense_spec(0.01)],
        )
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert any("would fuse" in v["message"] for v in errors)
        assert not any("too narrow" in v["message"] for v in errors)

    def test_unchecked_fusing_mode_is_reported_as_a_warning(
        self, narrow_trunk_pcb: Path, tmp_path: Path
    ):
        sidecar = _write_sidecar(
            tmp_path / "cp.json", [_pulsed_trunk_spec(pulse_duration_s=None), _sense_spec(0.01)]
        )
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 0  # a warning does not fail the gate
        warnings = [v for v in _path_ampacity_violations(report) if v["severity"] == "warning"]
        assert any("fusing survivability was NOT checked" in v["message"] for v in warnings)

    def test_half_declared_waveform_in_an_explicit_sidecar_exits_1(
        self, narrow_trunk_pcb: Path, tmp_path: Path, capsys
    ):
        """A duty cycle with no pulse is a declaration error, caught at load
        time -- the board is never checked against an intent nobody can
        interpret."""
        sidecar = tmp_path / "cp.json"
        sidecar.write_text(
            json.dumps(
                {
                    "paths": [
                        {
                            "name": "TRUNK",
                            "net": "NET1",
                            "source": {"ref": "J1", "pad": "1"},
                            "sink": {"ref": "J2", "pad": "1"},
                            "continuous_a": 1.0,
                            "duty_cycle": 0.02,
                        }
                    ]
                }
            )
        )
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 1
        assert "duty_cycle" in capsys.readouterr().err


class TestInPadBranchIsNotPathEvidence:
    """Issue #9647: a thin branch whose first segment lies wholly inside a
    large endpoint pad was counted as the resolved path's copper, so
    ``path_ampacity`` raised a false "too narrow / would fuse" error on the
    sense tap's neck instead of the trunk's."""

    BOARD = """(kicad_pcb
      (version 20240108)
      (generator "test")
      (generator_version "8.0")
      (general (thickness 1.6) (legacy_teardrops no))
      (paper "A4")
      (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
        (44 "Edge.Cuts" user)
      )
      (setup (pad_to_mask_clearance 0))
      (net 0 "")
      (net 1 "NET1")
      (gr_rect (start 0 0) (end 200 120)
        (stroke (width 0.1) (type default))
        (fill none)
        (layer "Edge.Cuts")
      )
      (footprint "TH:Pad"
        (layer "F.Cu")
        (at 20 50)
        (fp_text reference "J1" (at 0 -3) (layer "F.SilkS"))
        (pad "1" thru_hole circle (at 0 0) (size 5 5) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
      )
      (footprint "TH:Pad"
        (layer "F.Cu")
        (at 120 50)
        (fp_text reference "J2" (at 0 -3) (layer "F.SilkS"))
        (pad "1" thru_hole circle (at 0 0) (size 5 5) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
      )
      (footprint "TH:Pad"
        (layer "F.Cu")
        (at 130 70)
        (fp_text reference "R1" (at 0 -3) (layer "F.SilkS"))
        (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
      )
      (segment (start 20 50) (end 60 50) (width 13) (layer "F.Cu") (net 1)
        (uuid "00000000-0000-0000-0000-000000000001"))
      (segment (start 60 50) (end 120 50) (width 13) (layer "F.Cu") (net 1)
        (uuid "00000000-0000-0000-0000-000000000002"))
      (segment (start 120 50) (end 121 51) (width 0.3) (layer "F.Cu") (net 1)
        (uuid "00000000-0000-0000-0000-000000000003"))
      (segment (start 121 51) (end 121 70) (width 0.3) (layer "F.Cu") (net 1)
        (uuid "00000000-0000-0000-0000-000000000004"))
      (segment (start 121 70) (end 130 70) (width 0.3) (layer "F.Cu") (net 1)
        (uuid "00000000-0000-0000-0000-000000000005"))
    )
    """

    def test_sense_branch_inside_sink_pad_does_not_fuse(self, tmp_path: Path):
        pcb = tmp_path / "branch.kicad_pcb"
        pcb.write_text(self.BOARD)
        sidecar = _write_sidecar(
            tmp_path / "cp.json",
            [
                _trunk_spec(),
                CurrentPathSpec(
                    name="SENSE",
                    net_name="NET1",
                    source=PathEndpoint("J2", "1"),
                    sink=PathEndpoint("R1", "1"),
                    continuous_a=0.01,
                ),
            ],
        )
        report = tmp_path / "report.json"

        result = _run_check(pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 0, report.read_text()
        assert _path_ampacity_violations(report) == []


@pytest.mark.parametrize("interval", [(177.45, 182.7), (178.0, 181.0), (182.7, 169.0)])
def test_covered_l_trunk(tmp_path, interval, capsys):
    from dataclasses import replace

    from kicad_tools.cli import main
    from kicad_tools.router.current_paths import (
        reinforcement_eligible_segment_ids,
        resolve_current_path,
    )
    from kicad_tools.schema.pcb import PCB
    from tests.test_pcb_reinforce import _write_single_pad_footprint_lib

    board = tmp_path / "return.kicad_pcb"
    pcb = PCB.create(width=200, height=200, center=False)
    pad = _write_single_pad_footprint_lib(tmp_path)
    for ref, x, y in [("J1", 93.5, 177.45), ("J2", 108.7, 182.7)]:
        pcb.add_footprint_from_file(pad, ref, x, y)
        pcb.assign_net_to_footprint_pad(ref, "1", "NET1")
    # Put the overlay first to exercise replacement by the wider carrier.
    pcb.add_trace((108.7, interval[0]), (108.7, interval[1]), width=0.2, layer="B.Cu", net="NET1")
    pcb.add_trace((108.7, 177.45), (108.7, 182.7), width=4, layer="B.Cu", net="NET1")
    pcb.add_trace((93.5, 177.45), (108.7, 177.45), width=4, layer="B.Cu", net="NET1")
    pcb.save(board)
    pcb = PCB.load(board)
    spec = _trunk_spec(current_a=5)
    for direction in [spec, replace(spec, source=spec.sink, sink=spec.source)]:
        res = resolve_current_path(pcb, direction)
        assert res.ok, res.reason
        assert res.min_width.width_mm == pytest.approx(4)
        assert res.length_mm == pytest.approx(20.45)
        assert all(seg.width == 4 for seg in res.segments)
        ids = reinforcement_eligible_segment_ids(pcb, [direction])
        assert ids == {id(s) for s in pcb.segments if s.width == 4 or min(interval) >= 177.45}
        _write_sidecar(tmp_path / "current_paths.json", [direction])
        assert (
            main(
                [
                    "pcb",
                    "reinforce",
                    str(board),
                    "--net",
                    "NET1",
                    "--anchor-mode",
                    "ends",
                    "--current-paths",
                    str(tmp_path / "current_paths.json"),
                    "--format",
                    "json",
                    "--dry-run",
                ]
            )
            == 0
        )
        reinforcement = json.loads(capsys.readouterr().out)
        assert reinforcement["anchors_placed"] == 2
        assert reinforcement["run_length_mm"] == pytest.approx(20.45)
    _write_sidecar(tmp_path / "current_paths.json", [spec])
    assert _run_check(board, tmp_path / "check.json") == 0


def _write_pgnd_corner(tmp_path, *, reverse, buried, return_ends, feeder_x=65, split_feeder=False):
    from kicad_tools.schema.pcb import PCB
    from tests.test_pcb_reinforce import _write_single_pad_footprint_lib

    board = tmp_path / "trunk.kicad_pcb"
    pcb = PCB.create(width=200, height=200, center=False)
    pad = _write_single_pad_footprint_lib(tmp_path)
    for ref, x, y in [("J1", feeder_x, 119.6), ("J2", 77.5375, 135)]:
        pcb.add_footprint_from_file(pad, ref, x, y)
        pcb.assign_net_to_footprint_pad(ref, "1", "NET1")
    trunk = [(feeder_x, 119.6), (77.5375, 119.6), (77.5375, 124.5), (77.5375, 135)]
    if split_feeder:
        trunk.insert(1, (72, 119.6))
    tracks = [(a, b, 2.4) for a, b in pairwise(trunk)]
    if buried:
        tracks.extend(
            [
                ((77.9, 119), (77.5375, 119.3625), 0.2),
                ((77.5375, 119.3625), (77.5375, 121), 0.2),
                ((77.5, 119), (77.7, 119.2), 0.2),
                ((77.7, 119.2), (77.7, 120.8375), 0.2),
            ]
        )
    if return_ends is not None:
        # The return can join through track width at one or both stations.
        lower, upper = return_ends
        points = (lower, (84, lower[1]), (84, upper[1]), upper)
        tracks.extend((a, b, 0.2) for a, b in pairwise(points))
    if reverse:
        tracks = [(end, start, width) for start, end, width in reversed(tracks)]
    for start, end, width in tracks:
        pcb.add_trace(start, end, width=width, layer="F.Cu", net="NET1")
    pcb.save(board)
    return board


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "buried,return_ends",
    [
        (True, None),
        (True, ((77.7, 120.8375), (77.9, 128))),
        (False, ((77.7, 120.8375), (77.9, 128))),
        (False, ((77.5375, 120.5), (77.95, 130))),
        (True, ((77.5375, 120.5), (77.95, 130))),
        (False, ((77.8, 120.5), (77.5375, 130))),
        (True, ((77.8, 120.5), (77.5375, 130))),
    ],
)
def test_pgnd_tracks_at_trunk_corner(tmp_path, reverse, buried, return_ends):
    """Issue #10016 case 2, with terminal pads clear of the measured corner."""
    from dataclasses import replace

    from kicad_tools.cli import main
    from kicad_tools.router.current_paths import (
        audit_current_paths,
        reinforcement_eligible_segment_ids,
        resolve_current_path,
    )
    from kicad_tools.schema.pcb import PCB

    board = _write_pgnd_corner(tmp_path, reverse=reverse, buried=buried, return_ends=return_ends)
    pcb = PCB.load(board)
    spec = _trunk_spec(current_a=2.5)
    for direction in (spec, replace(spec, source=spec.sink, sink=spec.source)):
        result = resolve_current_path(pcb, direction)
        if return_ends is not None:
            assert result.status == "ambiguous"
        else:
            assert result.ok, result.reason
            assert result.min_width.width_mm == pytest.approx(2.4)
            assert result.length_mm == pytest.approx(27.9375)
            assert not audit_current_paths(pcb, [direction]).uncovered
            assert reinforcement_eligible_segment_ids(pcb, [direction]) == {
                id(s) for s in pcb.segments if s.width == 2.4
            }
    _write_sidecar(tmp_path / "current_paths.json", [spec])
    assert main(["check", str(board), "--only", "path_ampacity", "--allow-incomplete"]) == (
        2 if return_ends is not None else 0
    )


@pytest.mark.parametrize(
    "return_ends",
    [
        ((77.5375, 120.5), (77.95, 130)),
        ((77.8, 120.5), (77.5375, 130)),
    ],
)
@pytest.mark.parametrize("buried,split_feeder", [(False, False), (True, False), (True, True)])
def test_pgnd_mixed_return_reinforces_only_the_series_feeder(
    tmp_path, capsys, return_ends, buried, split_feeder
):
    from dataclasses import replace

    from kicad_tools.cli import main

    board = _write_pgnd_corner(
        tmp_path,
        reverse=False,
        buried=buried,
        return_ends=return_ends,
        feeder_x=55,
        split_feeder=split_feeder,
    )
    spec = _trunk_spec(current_a=2.5)
    for direction in (spec, replace(spec, source=spec.sink, sink=spec.source)):
        sidecar = _write_sidecar(tmp_path / "current_paths.json", [direction])
        args = ["pcb", "reinforce", str(board), "--net", "NET1", "--current-paths", str(sidecar)]
        for strategy in ("wire", "parallel"):
            assert main([*args, "--strategy", strategy, "--dry-run", "--format", "json"]) == 0
            report = json.loads(capsys.readouterr().out)
            assert report["unresolved_paths"]
            assert report["run_length_mm"] == pytest.approx(22.5375)
            if strategy == "wire":
                (wire,) = report["wires"]
                assert len(wire["anchors"]) == 2
                endpoints = {(p["x"], p["y"]) for p in (wire["leg_start"], wire["leg_end"])}
            else:
                (copy,) = report["copies"]
                assert copy["layer"] == "B.Cu"
                points = [tuple(seg[key]) for seg in copy["segments"] for key in ("start", "end")]
                assert all(y == 119.6 for x, y in points)
                endpoints = {min(points), max(points)}
            assert endpoints == {(55, 119.6), (77.5375, 119.6)}


@pytest.mark.parametrize("offset", [0.0, 0.3])
@pytest.mark.parametrize("has_return", [False, True])
def test_buried_track_spanning_a_split_feeder(tmp_path, capsys, offset, has_return):
    from dataclasses import replace

    from kicad_tools.cli import main
    from kicad_tools.router.current_paths import audit_current_paths, resolve_current_path
    from kicad_tools.schema.pcb import PCB

    board = _write_pgnd_corner(
        tmp_path,
        reverse=False,
        buried=False,
        return_ends=((77.5375, 120.5), (77.95, 130)) if has_return else None,
        feeder_x=55,
        split_feeder=True,
    )
    pcb = PCB.load(board)
    y = 119.6 + offset
    pcb.add_trace((65, y), (75, y), width=0.2, layer="F.Cu", net="NET1")
    pcb.save(board)
    spec = _trunk_spec(current_a=2.5)
    for direction in (spec, replace(spec, source=spec.sink, sink=spec.source)):
        result = resolve_current_path(pcb, direction)
        if has_return:
            assert result.status == "ambiguous"
        else:
            assert result.ok, result.reason
            assert result.min_width.width_mm == pytest.approx(2.4)
            assert result.length_mm == pytest.approx(37.9375)
            assert not audit_current_paths(pcb, [direction]).uncovered
        sidecar = _write_sidecar(tmp_path / "current_paths.json", [direction])
        assert main(["check", str(board), "--only", "path_ampacity", "--allow-incomplete"]) == (
            2 if has_return else 0
        )
        capsys.readouterr()
        args = ["pcb", "reinforce", str(board), "--net", "NET1", "--current-paths", str(sidecar)]
        for strategy in ("wire", "parallel"):
            assert main([*args, "--strategy", strategy, "--dry-run", "--format", "json"]) == 0
            report = json.loads(capsys.readouterr().out)
            assert report["run_length_mm"] == pytest.approx(22.5375 if has_return else 37.9375)


@pytest.mark.parametrize("rejoin", [False, True])
def test_thin_sense_cycle_check(tmp_path, rejoin):
    from kicad_tools.router.current_paths import reinforcement_eligible_segment_ids
    from kicad_tools.schema.pcb import PCB

    board = _write_t_network_pcb(tmp_path / "sense.kicad_pcb", trunk_width=4)
    pcb = PCB.load(board)
    pcb.add_trace((60, 70), (70, 65), width=0.2, layer="F.Cu", net="NET1")
    pcb.add_trace((70, 65), (80 if rejoin else 60, 50), width=0.2, layer="F.Cu", net="NET1")
    pcb.save(board)
    spec = _trunk_spec(current_a=5)
    _write_sidecar(tmp_path / "current_paths.json", [spec])
    report = tmp_path / "check.json"
    rc = _run_check(board, report)
    findings = _path_ampacity_violations(report)
    if rejoin:
        assert rc != 0
        assert any("ambiguous" in f["message"] for f in findings)
    else:
        assert rc == 0
        (loop,) = [f for f in findings if "loop" in f["message"] and f["severity"] == "warning"]
        assert loop["layer"] == "F.Cu" and loop["location"] in ([65, 57.5], [65, 67.5], [60, 60])
        assert reinforcement_eligible_segment_ids(pcb, [spec]) == {
            id(segment) for segment in pcb.segments if segment.width == 4
        }


# --------------------------------------------------------------------------
# Operating cases through the sidecar (Issue #10361)
# --------------------------------------------------------------------------


def _both_loads_sidecar(path: Path, *, cases: list[dict] | None) -> Path:
    payload = dump_current_path_specs([_trunk_spec(2.0), _sense_spec(2.0)])
    if cases is not None:
        payload["cases"] = cases
    path.write_text(json.dumps(payload))
    return path


_BOTH_LOADS = {
    "name": "charge+support",
    "contributions": [
        {"path": "TRUNK", "current_a": 2.0},
        {"path": "SENSE", "current_a": 2.0},
    ],
}
_EACH_ALONE = [
    {"name": "charge", "contributions": [{"path": "TRUNK", "current_a": 2.0}]},
    {"name": "support", "contributions": [{"path": "SENSE", "current_a": 2.0}]},
]


@pytest.fixture
def shared_feed_pcb(tmp_path: Path) -> Path:
    # The CLI judges 1 oz copper: 1.0 mm clears 2 A (0.781 mm) but not 4 A (2.033 mm).
    return _write_t_network_pcb(tmp_path / "board.kicad_pcb", trunk_width=1.0, sense_width=1.0)


class TestOperatingCasesThroughCheck:
    def test_explicit_sidecar_case_fails_only_on_the_common_feed(
        self, shared_feed_pcb: Path, tmp_path: Path
    ):
        sidecar = _both_loads_sidecar(tmp_path / "cp.json", cases=[_BOTH_LOADS, *_EACH_ALONE])
        report = tmp_path / "report.json"

        result = _run_check(shared_feed_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert len(errors) == 1
        (error,) = errors
        assert "charge+support" in error["items"]
        assert "TRUNK +2A" in error["message"] and "SENSE +2A" in error["message"]
        assert "combined 4A" in error["message"]
        assert error["location"] == pytest.approx([40.0, 50.0])

    def test_auto_discovered_sidecar_carries_cases(self, shared_feed_pcb: Path, tmp_path: Path):
        _both_loads_sidecar(tmp_path / "current_paths.json", cases=[_BOTH_LOADS])
        report = tmp_path / "report.json"

        result = _run_check(shared_feed_pcb, report)

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert [e["items"][0] for e in errors] == ["charge+support"]

    def test_route_emits_case_for_the_final_audit(self, shared_feed_pcb, tmp_path, monkeypatch):
        from argparse import Namespace

        from kicad_tools.cli import route_cmd
        from tests.test_cli_route_current_paths import _capture_drc_results, _patch_geometric_absent

        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        source = _both_loads_sidecar(tmp_path / "authored.json", cases=[_BOTH_LOADS])
        args = Namespace(current_paths=str(source), quiet=True)
        assert route_cmd._preload_current_paths(args, shared_feed_pcb) == 0
        route_cmd.run_post_route_drc(
            shared_feed_pcb,
            "jlcpcb",
            2,
            quiet=True,
            current_path_specs=args._loaded_current_paths,
            current_path_cases=args._loaded_current_path_cases,
            current_paths_input_path=args._current_paths_input_path,
        )
        assert any("combined 4A" in v.message for v in captured["results"].errors)
        report = tmp_path / "report.json"
        assert _run_check(shared_feed_pcb, report) == 2
        assert any("combined 4A" in v["message"] for v in _path_ampacity_violations(report))

    def test_cases_with_no_declared_paths(self, shared_feed_pcb, tmp_path):
        source = tmp_path / "empty.json"
        source.write_text(json.dumps({"paths": [], "cases": [_BOTH_LOADS]}))
        report = tmp_path / "report.json"
        assert _run_check(shared_feed_pcb, report, extra=["--current-paths", str(source)]) == 2
        errors = [
            v["message"] for v in _path_ampacity_violations(report) if v["severity"] == "error"
        ]
        assert len(errors) == 2
        for name in ("TRUNK", "SENSE"):
            assert any(f"unknown path {name!r}" in message for message in errors)

    def test_each_load_alone_passes(self, shared_feed_pcb: Path, tmp_path: Path):
        sidecar = _both_loads_sidecar(tmp_path / "cp.json", cases=_EACH_ALONE)
        report = tmp_path / "report.json"

        result = _run_check(shared_feed_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 0
        assert _path_ampacity_violations(report) == []

    @pytest.mark.parametrize("bare_list", [False, True])
    def test_legacy_sidecar_without_cases_keeps_independent_checks(
        self, shared_feed_pcb: Path, tmp_path: Path, bare_list: bool
    ):
        specs = [_trunk_spec(2.0), _sense_spec(2.0)]
        sidecar = tmp_path / "cp.json"
        if bare_list:
            sidecar.write_text(json.dumps([s.to_dict() for s in specs]))
        else:
            _write_sidecar(sidecar, specs)
        report = tmp_path / "report.json"

        result = _run_check(shared_feed_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 0
        assert _path_ampacity_violations(report) == []

    def test_malformed_case_in_explicit_sidecar_exits_1(
        self, shared_feed_pcb: Path, tmp_path: Path, capsys
    ):
        bad = {**_BOTH_LOADS, "pulse_overlap": "sometimes"}
        sidecar = _both_loads_sidecar(tmp_path / "cp.json", cases=[bad])

        result = _run_check(
            shared_feed_pcb, tmp_path / "report.json", extra=["--current-paths", str(sidecar)]
        )

        assert result == 1
        assert "pulse_overlap" in capsys.readouterr().err

    def test_unknown_path_in_a_case_is_an_error_finding(
        self, shared_feed_pcb: Path, tmp_path: Path
    ):
        case = {"name": "typo", "contributions": [{"path": "TRUNKK", "current_a": 2.0}]}
        sidecar = _both_loads_sidecar(tmp_path / "cp.json", cases=[case])
        report = tmp_path / "report.json"

        result = _run_check(shared_feed_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 2
        (error,) = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert "unknown path 'TRUNKK'" in error["message"]


class TestOperatingCaseSidecarModel:
    def test_cases_round_trip_through_dump_and_parse(self):
        from kicad_tools.router.current_paths import (
            CaseContribution,
            OperatingCase,
            parse_current_path_sidecar,
        )

        case = OperatingCase(
            name="n",
            contributions=(CaseContribution("TRUNK", 2.0), CaseContribution("SENSE", -1.0)),
            pulse_overlap="disjoint",
            notes="x",
        )
        payload = dump_current_path_specs([_trunk_spec(2.0), _sense_spec(2.0)], [case])
        parsed = parse_current_path_sidecar(json.loads(json.dumps(payload)))
        assert parsed.cases == [case]
        assert [s.name for s in parsed.specs] == ["TRUNK", "SENSE"]

    def test_spec_loader_still_returns_a_list_and_ignores_cases(self, tmp_path: Path):
        from kicad_tools.router.current_paths import load_current_path_specs

        sidecar = _both_loads_sidecar(tmp_path / "cp.json", cases=[_BOTH_LOADS])
        specs = load_current_path_specs(sidecar)
        assert [s.name for s in specs] == ["TRUNK", "SENSE"]

    @pytest.mark.parametrize(
        "cases",
        [
            "nope",
            [{"contributions": []}],
            [{"name": "c"}],
            [{"name": "c", "contributions": [{"path": "A"}]}],
            [{"name": "c", "contributions": [{"path": "A", "current_a": True}]}],
            [{"name": "c", "contributions": []}, {"name": "c", "contributions": []}],
        ],
    )
    def test_malformed_cases_are_rejected(self, cases):
        from kicad_tools.router.current_paths import parse_current_path_sidecar

        with pytest.raises(ValueError):
            parse_current_path_sidecar({"paths": [], "cases": cases})
