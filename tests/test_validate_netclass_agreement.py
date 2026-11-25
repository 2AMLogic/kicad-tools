"""Tests for the ``netclass_agreement`` DRC rule (Issue #10139).

``kct route`` writes the classes it routed with to the ``<stem>.net_class_map.json``
sidecar and (since #10133) to the project's ``net_settings``.  The rule reports
when those two disagree for a net, and when a net's copper is narrower than its
resolved project class ``track_width``.  Covered here: both rule ids, exact vs
pattern assignment, arcs, the neck-down floor, severity, determinism, every
silent-pass carve-out, and the ``kct check`` wiring end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import main as check_main
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.netclass_agreement import (
    NETCLASS_AGREEMENT_RULE_IDS,
    RULE_SIDECAR_MISMATCH,
    RULE_TRACK_WIDTH_MISMATCH,
    check_netclass_agreement,
)

_HEADER = """(kicad_pcb
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
  (net 1 "VCC")
  (net 2 "SIG")
  (net 3 "VBUS_A")
"""
_FOOTER = """  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""


def _seg(x1: float, x2: float, width: float, net: int, y: float = 110.0) -> str:
    return (
        f"  (segment (start {x1} {y}) (end {x2} {y}) (width {width}) "
        f'(layer "F.Cu") (net {net}) (uuid "00000000-0000-0000-0000-{net:04d}{int(x1):08d}"))\n'
    )


def _arc(width: float, net: int) -> str:
    return (
        f"  (arc (start 120 120) (mid 125 125) (end 130 120) (width {width}) "
        f'(layer "F.Cu") (net {net}) (uuid "00000000-0000-0000-0000-{net:04d}99999999"))\n'
    )


def _vcc_pad(x: float, y: float, layer: str = "F.Cu") -> str:
    return (
        f'  (footprint "Test:Pad" (layer "{layer}") (at {x} {y})'
        f' (pad "1" smd rect (at 0 0) (size 0.6 0.8) (layers "{layer}") (net 1 "VCC")))\n'
    )


def _write_pcb(tmp_path: Path, body: str = "", name: str = "board") -> Path:
    pcb_path = tmp_path / f"{name}.kicad_pcb"
    pcb_path.write_text(_HEADER + body + _FOOTER, encoding="utf-8")
    return pcb_path


def _project(
    *,
    power_width: float | None = 0.5,
    assignments: dict | None = None,
    patterns: list | None = None,
) -> dict:
    power: dict = {"name": "Power", "clearance": 0.2, "priority": 1}
    if power_width is not None:
        power["track_width"] = power_width
    return {
        "board": {},
        "meta": {"version": 1},
        "net_settings": {
            "meta": {"version": 3},
            "classes": [
                {"name": "Default", "clearance": 0.2, "track_width": 0.2, "priority": 2**31 - 1},
                power,
            ],
            "netclass_assignments": {"VCC": "Power"} if assignments is None else assignments,
            "netclass_patterns": [] if patterns is None else patterns,
        },
    }


def _write_project(pcb_path: Path, project: dict) -> None:
    pcb_path.with_suffix(".kicad_pro").write_text(json.dumps(project), encoding="utf-8")


def _ncm(**overrides) -> dict:
    entry = {"VCC": NetClassRouting(name="Power", trace_width=0.5)}
    entry.update(overrides)
    return entry


def _run(pcb_path: Path, ncm) -> object:
    return check_netclass_agreement(pcb_path, PCB.load(str(pcb_path)), ncm)


def _ids(results) -> list[str]:
    return [v.rule_id for v in results.violations]


class TestAgreement:
    def test_agreeing_board_is_silent(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.5, 1) + _seg(100, 110, 0.2, 2, y=112))
        _write_project(pcb, _project())
        results = _run(pcb, _ncm())
        assert results.violations == []
        assert set(results.rules_checked_by_rule) == set(NETCLASS_AGREEMENT_RULE_IDS)

    def test_sidecar_class_name_differs(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.5, 1))
        _write_project(pcb, _project())
        results = _run(pcb, _ncm(VCC=NetClassRouting(name="Ground", trace_width=0.5)))
        assert _ids(results) == [RULE_SIDECAR_MISMATCH]
        finding = results.violations[0]
        assert finding.severity == "warning"
        assert finding.nets == ("VCC",)
        assert "'Ground'" in finding.message and "'Power'" in finding.message

    def test_sidecar_width_differs_from_project_class(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.5, 1))
        _write_project(pcb, _project(power_width=0.8))
        results = _run(pcb, _ncm())
        # Copper (0.5) is also narrower than the edited class (0.8).
        assert _ids(results) == [RULE_SIDECAR_MISMATCH, RULE_TRACK_WIDTH_MISMATCH]
        side = results.violations[0]
        assert side.actual_value == pytest.approx(0.5)
        assert side.required_value == pytest.approx(0.8)

    def test_width_edit_reports_only_the_edited_class(self, tmp_path: Path):
        """Editing Power's width must not implicate nets in other classes."""
        pcb = _write_pcb(
            tmp_path,
            _seg(100, 110, 0.5, 1) + _seg(100, 110, 0.2, 2, y=112) + _seg(100, 110, 0.5, 3, y=114),
        )
        project = _project(power_width=0.8, assignments={"VCC": "Power", "SIG": "Sig"})
        project["net_settings"]["classes"].append(
            {"name": "Sig", "clearance": 0.2, "track_width": 0.2, "priority": 5}
        )
        _write_project(pcb, project)
        ncm = _ncm(SIG=NetClassRouting(name="Sig", trace_width=0.2))
        results = _run(pcb, ncm)
        assert {n for v in results.violations for n in v.nets} == {"VCC"}

    def test_copper_narrower_than_class_width(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.5, 1) + _seg(110, 120, 0.3, 1))
        _write_project(pcb, _project())
        results = _run(pcb, _ncm())
        assert _ids(results) == [RULE_TRACK_WIDTH_MISMATCH]
        finding = results.violations[0]
        assert finding.severity == "warning"
        assert finding.actual_value == pytest.approx(0.3)
        assert finding.required_value == pytest.approx(0.5)
        assert finding.layer == "F.Cu"
        # Rule locations are board-relative (the CLI closure absolutizes).
        assert finding.location == pytest.approx((10.0, 10.0))
        assert "1 copper segment(s)" in finding.message

    def test_wider_copper_is_fine(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.9, 1))
        _write_project(pcb, _project())
        assert _run(pcb, _ncm()).violations == []

    def test_arc_narrower_than_class_width(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.5, 1) + _arc(0.25, 1))
        _write_project(pcb, _project())
        results = _run(pcb, _ncm())
        assert _ids(results) == [RULE_TRACK_WIDTH_MISMATCH]
        assert results.violations[0].actual_value == pytest.approx(0.25)

    def test_pattern_assignment_resolves(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.3, 3, y=114))
        _write_project(
            pcb,
            _project(assignments={}, patterns=[{"netclass": "Power", "pattern": "VBUS*"}]),
        )
        ncm = {"VBUS_A": NetClassRouting(name="Power", trace_width=0.5)}
        results = _run(pcb, ncm)
        assert _ids(results) == [RULE_TRACK_WIDTH_MISMATCH]
        assert results.violations[0].nets == ("VBUS_A",)

    def test_sidecar_neck_floor_tolerates_pad_escape_necks(self, tmp_path: Path):
        pad = _vcc_pad(112, 110)
        pcb = _write_pcb(tmp_path, pad + _seg(100, 110, 0.5, 1) + _seg(110, 112, 0.3, 1))
        _write_project(pcb, _project())
        ncm = _ncm(VCC=NetClassRouting(name="Power", trace_width=0.5, neck_trace_width=0.3))
        assert _run(pcb, ncm).violations == []
        thinner = _seg(100, 110, 0.5, 1) + _seg(110, 112, 0.2, 1)
        pcb2 = _write_pcb(tmp_path, pad + thinner, name="thin")
        _write_project(pcb2, _project())
        results = _run(pcb2, ncm)
        assert _ids(results) == [RULE_TRACK_WIDTH_MISMATCH]
        assert "neck-down floor" in results.violations[0].message
        assert results.violations[0].required_value == pytest.approx(0.3)

    def test_two_leg_neck_can_reach_full_width_from_a_pad(self, tmp_path: Path):
        corner = (
            '  (segment (start 112 110) (end 111.5 110.5) (width 0.3) (layer "F.Cu") (net 1))\n'
        )
        board = _vcc_pad(112, 110) + corner + _seg(111.5, 111, 0.3, 1, 110.5)
        pcb = _write_pcb(tmp_path, board + _seg(111, 105, 0.5, 1, 110.5))
        _write_project(pcb, _project())
        ncm = _ncm(VCC=NetClassRouting(name="Power", trace_width=0.5, neck_trace_width=0.3))
        assert _run(pcb, ncm).violations == []

    def test_wide_class_escape_uses_the_router_landing_radius(self, tmp_path: Path):
        # The lattice's dense-field control emits a 2.8 mm leg after a short
        # diagonal. The stock 2.6 mm body needs the expanded landing search.
        pad = _vcc_pad(108, 110)
        corner = (
            '  (segment (start 108 110) (end 107.6 109.6) (width 0.2) (layer "F.Cu") (net 1))\n'
        )
        body = pad + corner + _seg(107.6, 104.8, 0.2, 1, 109.6) + _seg(104.8, 103, 2.6, 1, 109.6)
        pcb = _write_pcb(tmp_path, body)
        _write_project(pcb, _project(power_width=2.6))
        ncm = _ncm(
            VCC=NetClassRouting(name="Power", trace_width=2.6, neck_trace_width=0.2, clearance=0.3)
        )
        assert _run(pcb, ncm).violations == []

        # Extending just that leg across the board ceases to be a local escape.
        long_leg = _write_pcb(tmp_path, pad + corner + _seg(107.6, 125, 0.2, 1, 109.6), name="long")
        _write_project(long_leg, _project(power_width=2.6))
        (finding,) = _run(long_leg, ncm).violations
        assert finding.required_value == pytest.approx(2.6)

    def test_net_absent_from_sidecar_still_gets_copper_check(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.3, 1))
        _write_project(pcb, _project())
        ncm = {"SIG": NetClassRouting(name="Digital", trace_width=0.2)}
        assert _ids(_run(pcb, ncm)) == [RULE_TRACK_WIDTH_MISMATCH]

    def test_findings_are_sorted_and_deterministic(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.3, 1) + _seg(100, 110, 0.3, 3, y=114))
        project = _project(assignments={"VCC": "Power", "VBUS_A": "Power"})
        _write_project(pcb, project)
        ncm = _ncm(VBUS_A=NetClassRouting(name="Other", trace_width=0.5))
        first = [(v.rule_id, v.nets) for v in _run(pcb, ncm).violations]
        assert first == [
            (RULE_SIDECAR_MISMATCH, ("VBUS_A",)),
            (RULE_TRACK_WIDTH_MISMATCH, ("VBUS_A",)),
            (RULE_TRACK_WIDTH_MISMATCH, ("VCC",)),
        ]
        assert first == [(v.rule_id, v.nets) for v in _run(pcb, ncm).violations]


class TestSilentPasses:
    """Same carve-outs as ``netclass_floor``; never a crash."""

    def _bad_board(self, tmp_path: Path) -> Path:
        return _write_pcb(tmp_path, _seg(100, 110, 0.2, 1))

    def test_no_pcb_path(self, tmp_path: Path):
        pcb = self._bad_board(tmp_path)
        _write_project(pcb, _project())
        assert check_netclass_agreement(None, PCB.load(str(pcb)), _ncm()).violations == []

    @pytest.mark.parametrize("ncm", [None, {}])
    def test_no_sidecar(self, tmp_path: Path, ncm):
        pcb = self._bad_board(tmp_path)
        _write_project(pcb, _project())
        assert _run(pcb, ncm).violations == []

    def test_no_kicad_pro(self, tmp_path: Path):
        pcb = self._bad_board(tmp_path)
        assert _run(pcb, _ncm()).violations == []

    @pytest.mark.parametrize("text", ["{not json", "[]", "null", ""])
    def test_unparsable_or_non_object_project(self, tmp_path: Path, text: str):
        pcb = self._bad_board(tmp_path)
        pcb.with_suffix(".kicad_pro").write_text(text, encoding="utf-8")
        assert _run(pcb, _ncm()).violations == []

    def test_default_only_project(self, tmp_path: Path):
        pcb = self._bad_board(tmp_path)
        project = _project()
        project["net_settings"]["classes"] = [project["net_settings"]["classes"][0]]
        project["net_settings"]["netclass_assignments"] = {}
        _write_project(pcb, project)
        assert _run(pcb, _ncm()).violations == []

    def test_named_class_without_track_width(self, tmp_path: Path):
        pcb = self._bad_board(tmp_path)
        _write_project(pcb, _project(power_width=None))
        assert _run(pcb, _ncm()).violations == []

    def test_project_without_net_settings(self, tmp_path: Path):
        pcb = self._bad_board(tmp_path)
        _write_project(pcb, {"board": {}, "meta": {"version": 1}})
        assert _run(pcb, _ncm()).violations == []

    def test_malformed_class_fields_never_crash(self, tmp_path: Path):
        pcb = self._bad_board(tmp_path)
        _write_project(pcb, _project(power_width="wide"))  # type: ignore[arg-type]
        assert _run(pcb, _ncm()).violations == []

    def test_net_resolving_to_default_is_not_reported(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.1, 2, y=112))
        _write_project(pcb, _project())
        ncm = {"SIG": NetClassRouting(name="Whatever", trace_width=0.9)}
        assert _run(pcb, ncm).violations == []


class TestWiring:
    @pytest.mark.parametrize("pad_layer", ["F.Cu", "B.Cu"])
    def test_cli_neck_floor_is_local_to_a_same_layer_pad(self, tmp_path: Path, capsys, pad_layer):
        # The same board has a short pad entry and, between two full-width
        # sections of VCC, a second section at exactly the same width.
        copper = (
            _vcc_pad(110, 110, pad_layer)
            + _seg(110, 111, 0.35, 1)
            + _seg(111, 116, 0.5, 1)
            + _seg(116, 117, 0.35, 1)
            + _seg(117, 121, 0.5, 1)
        )
        pcb = _write_pcb(tmp_path, copper)
        _write_project(pcb, _project())
        (tmp_path / "board.net_class_map.json").write_text(
            json.dumps({"VCC": {"name": "Power", "trace_width": 0.5, "neck_trace_width": 0.3}})
        )
        code, report = self._check(pcb, tmp_path, capsys)
        assert code == 0
        (finding,) = report["violations"]
        assert finding["rule_id"] == RULE_TRACK_WIDTH_MISMATCH
        assert finding["required_value"] == pytest.approx(0.5)
        count = 1 if pad_layer == "F.Cu" else 2
        assert f"{count} copper segment(s)" in finding["message"]

    def test_advisory_category_and_registration(self):
        from kicad_tools.cli.check_categories import CHECK_CATEGORIES

        assert "netclass_agreement" in CHECK_CATEGORIES
        for rule_id in NETCLASS_AGREEMENT_RULE_IDS:
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_ADVISORY

    def _routed_board(self, tmp_path: Path, *, power_width: float) -> Path:
        pcb = _write_pcb(tmp_path, _seg(100, 110, 0.5, 1) + _seg(100, 110, 0.2, 2, y=112))
        _write_project(pcb, _project(power_width=power_width))
        ncm = {
            "VCC": {"name": "Power", "trace_width": 0.5},
            "SIG": {"name": "Digital", "trace_width": 0.2},
        }
        (tmp_path / "board.net_class_map.json").write_text(json.dumps(ncm), encoding="utf-8")
        return pcb

    def _check(self, pcb: Path, tmp_path: Path, capsys) -> tuple[int, dict]:
        out = tmp_path / "report.json"
        code = check_main(
            [
                str(pcb),
                "--mfr",
                "jlcpcb",
                "--only",
                "netclass_agreement",
                "--allow-incomplete",
                "--format",
                "json",
                "--output",
                str(out),
            ]
        )
        capsys.readouterr()
        return code, json.loads(out.read_text())

    def test_cli_reports_edited_class_only_and_warning_does_not_fail(self, tmp_path: Path, capsys):
        pcb = self._routed_board(tmp_path, power_width=0.8)
        code, report = self._check(pcb, tmp_path, capsys)
        found = {(v["rule_id"], tuple(v["nets"])) for v in report["violations"]}
        assert found == {
            (RULE_SIDECAR_MISMATCH, ("VCC",)),
            (RULE_TRACK_WIDTH_MISMATCH, ("VCC",)),
        }
        assert {v["severity"] for v in report["violations"]} == {"warning"}
        assert code == 0

    def test_cli_agreeing_board_is_clean(self, tmp_path: Path, capsys):
        pcb = self._routed_board(tmp_path, power_width=0.5)
        code, report = self._check(pcb, tmp_path, capsys)
        assert report["violations"] == []
        assert code == 0
