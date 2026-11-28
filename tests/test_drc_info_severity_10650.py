"""Informational findings are neither errors nor warnings (Issue #10650).

``DRCReport.warning_count``, export's DRC preflight and the ``kct drc``
counts all derived "warning" from ``not is_error``, so an ``info``-severity
finding landed in the warning bucket: an info-only report exited 2 under
``--strict`` and a manufacturing preflight reported its informational rows
as DRC warnings.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from kicad_tools.cli import drc_cmd, stitch_cmd
from kicad_tools.cli.runner import KiCadCLIResult
from kicad_tools.drc.report import parse_json_report
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig

INFO = {
    "rule_id": "model_coverage",
    "severity": "info",
    "message": "Model coverage was assessed.",
}
WARNING = {
    "rule_id": "silk_over_copper",
    "severity": "warning",
    "message": "Silkscreen clipped by solder mask",
}
ERROR = {"rule_id": "courtyards_overlap", "severity": "error", "message": "Courtyards overlap"}

# (findings, errors, warnings, infos, preflight status, preflight message,
#  ``kct drc --strict --allow-incomplete`` exit code) -- the issue's table.
CASES = [
    pytest.param([], 0, 0, 0, "OK", "DRC: 0 errors, 0 warnings", 0, id="empty"),
    pytest.param([INFO], 0, 0, 1, "OK", "DRC: 0 errors, 0 warnings", 0, id="one-info"),
    pytest.param([WARNING], 0, 1, 0, "WARN", "DRC passed with 1 warning(s)", 2, id="one-warning"),
    pytest.param(
        [WARNING, INFO], 0, 1, 1, "WARN", "DRC passed with 1 warning(s)", 2, id="warning-and-info"
    ),
    pytest.param([ERROR, INFO], 1, 0, 1, "FAIL", "DRC has 1 error(s)", 1, id="error-and-info"),
]


def _kct_report(violations: list[dict], file: str = "control.kicad_pcb") -> dict:
    """The kct-check JSON shape ``kct check --output`` writes."""
    return {
        "file": file,
        "manufacturer": "jlcpcb-tier1",
        "summary": {"errors": 0, "warnings": 0, "passed": True},
        "violations": violations,
    }


def _kicad_cli_report(violations: list[dict], source: str = "control.kicad_pcb") -> dict:
    """The same findings in kicad-cli's JSON shape."""
    return {
        "source": source,
        "date": "2026-11-27T00:00:00+00:00",
        "coordinate_units": "mm",
        "violations": [
            {"type": v["rule_id"], "severity": v["severity"], "description": v["message"]}
            for v in violations
        ],
        "unconnected_items": [],
        "schematic_parity": [],
    }


def _write(tmp_path: Path, data: dict, name: str = "drc_report.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def _minimal_pcb(tmp_path: Path, name: str = "board.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text('(kicad_pcb (version 20231014) (generator "pcbnew"))\n')
    return pcb


def _preflight(pcb: Path, report: Path | None):
    checker = PreflightChecker(
        pcb_path=pcb,
        config=PreflightConfig(drc_report_path=str(report) if report else None, skip_erc=True),
    )
    return checker._check_drc()


def _courtyard_error() -> dict:
    """A waivable error: two whole footprints, as in ``test_cli_drc_waivers``."""
    return {
        "type": "courtyards_overlap",
        "severity": "error",
        "description": "Courtyards overlap",
        "items": [{"description": "Footprint C52"}, {"description": "Footprint U10"}],
    }


_COURTYARD_WAIVER = {
    "version": 2,
    "waivers": [
        {
            "rule": "courtyards_overlap",
            "items": ["C52", "U10"],
            "reason": "EE-mandated tight decoupling",
            "issue": "chorus#18",
        }
    ],
}


def _waivable_report() -> dict:
    """One waivable courtyard error plus one info finding, in kicad-cli's shape."""
    data = _kicad_cli_report([INFO])
    data["violations"].insert(0, _courtyard_error())
    return data


# ---------------------------------------------------------------------------
# Parsed report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("findings", "errors", "warnings", "infos", "_s", "_m", "_x"), CASES)
def test_report_counts_only_warning_severity(findings, errors, warnings, infos, _s, _m, _x):
    report = parse_json_report(json.dumps(_kct_report(findings)))
    assert (report.error_count, report.warning_count, report.info_count) == (
        errors,
        warnings,
        infos,
    )
    assert len(report.warnings) == warnings
    assert [v.severity.value for v in report.infos] == ["info"] * infos
    assert report.summary()["infos"] == infos
    assert report.to_dict()["info_count"] == infos


@pytest.mark.parametrize(("findings", "errors", "warnings", "infos", "_s", "_m", "_x"), CASES)
def test_kicad_cli_format_counts_the_same(findings, errors, warnings, infos, _s, _m, _x):
    """The parser keeps ``info`` in both report shapes; so do the counts."""
    report = parse_json_report(json.dumps(_kicad_cli_report(findings)))
    assert (report.error_count, report.warning_count, report.info_count) == (
        errors,
        warnings,
        infos,
    )


def test_info_findings_stay_available_for_inspection():
    report = parse_json_report(json.dumps(_kct_report([WARNING, INFO])))
    assert [v.type_str for v in report.violations] == ["silk_over_copper", "model_coverage"]
    info = report.infos[0]
    assert not info.is_error
    assert not info.is_warning
    assert info.is_info


def test_waived_findings_are_not_warnings_in_the_report(tmp_path):
    """The report agrees with the CLI: a waived finding is neither error nor warning."""
    from kicad_tools.drc.waivers import apply_waivers_to_report
    from kicad_tools.validate.rules.waivers import load_waivers

    report = parse_json_report(json.dumps(_waivable_report()))
    assert (report.error_count, report.warning_count) == (1, 0)
    apply_waivers_to_report(report, load_waivers(_write(tmp_path, _COURTYARD_WAIVER, "w.json")))
    assert report.warnings == []
    assert (report.error_count, report.warning_count, report.info_count) == (0, 0, 1)


# ---------------------------------------------------------------------------
# kct drc
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("findings", "errors", "warnings", "infos", "_s", "_m", "exit_code"), CASES
)
def test_strict_exit_counts_only_warnings(
    tmp_path, capsys, findings, errors, warnings, infos, _s, _m, exit_code
):
    report = _write(tmp_path, _kct_report(findings))
    rc = drc_cmd.main([str(report), "--strict", "--allow-incomplete", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == exit_code == data["exit_code"]
    assert (data["summary"]["errors"], data["summary"]["warnings"]) == (errors, warnings)
    # OMIT-when-absent, like ``waived``: info-free reports keep their old shape.
    assert data["summary"].get("infos", 0) == infos
    assert ("infos" in data["summary"]) is (infos > 0)
    assert len(data["violations"]) == len(findings)


@pytest.mark.parametrize(("findings", "errors", "warnings", "infos", "_s", "_m", "_x"), CASES)
def test_table_summary_counts_only_warnings(
    tmp_path, capsys, findings, errors, warnings, infos, _s, _m, _x
):
    report = _write(tmp_path, _kct_report(findings))
    drc_cmd.main([str(report), "--allow-incomplete"])
    out = capsys.readouterr().out
    assert f"Errors:     {errors}" in out
    assert f"Warnings:   {warnings}" in out
    assert (f"Infos:      {infos}" in out) is (infos > 0)


def test_table_lists_info_findings_without_calling_them_warnings(tmp_path, capsys):
    report = _write(tmp_path, _kct_report([WARNING, INFO]))
    drc_cmd.main([str(report), "--allow-incomplete"])
    out = capsys.readouterr().out
    assert "silk_over_copper: 1 warning" in out
    assert "model_coverage: 1 info" in out
    assert "model_coverage: 1 warning" not in out
    assert "WARNINGS (review recommended):" in out
    assert "INFO (informational, non-gating):" in out
    assert "[i] model_coverage" in out
    assert "DRC INCOMPLETE - Schematic parity NOT RUN; review warnings" in out


def test_info_only_table_has_no_warning_section(tmp_path, capsys):
    report = _write(tmp_path, _kct_report([INFO]))
    drc_cmd.main([str(report), "--allow-incomplete"])
    out = capsys.readouterr().out
    assert "WARNINGS (review recommended):" not in out
    assert "INFO (informational, non-gating):" in out
    # --allow-incomplete only changes the exit code; the verdict stays honest.
    assert "DRC INCOMPLETE - Schematic parity NOT RUN\n" in out
    assert "review warnings" not in out


def test_summary_format_has_an_infos_column(tmp_path, capsys):
    report = _write(tmp_path, _kct_report([WARNING, INFO]))
    drc_cmd.main([str(report), "--allow-incomplete", "--format", "summary"])
    out = capsys.readouterr().out
    assert re.search(r"Type\s+Errors\s+Warnings\s+Infos", out)
    assert re.search(r"^silk_over_copper\s+0\s+1\s+0\s*$", out, re.M)
    assert re.search(r"^model_coverage\s+0\s+0\s+1\s*$", out, re.M)
    assert re.search(r"^TOTAL\s+0\s+1\s+1\s*$", out, re.M)


def test_info_only_report_passes_strict_when_other_gates_pass(tmp_path, monkeypatch, capsys):
    """Parity ran and found nothing: an info-only report is a clean pass."""
    from kicad_tools.drc.report import DRCReport

    report = DRCReport.load(_write(tmp_path, _kct_report([INFO])))
    report.schematic_parity_ran = True
    pcb = tmp_path / "control.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    monkeypatch.setattr(drc_cmd, "run_drc_on_pcb", lambda *a, **k: report)

    rc = drc_cmd.main([str(pcb), "--strict", "--format", "json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    rc = drc_cmd.main([str(pcb), "--strict"])
    assert rc == 0
    assert "DRC PASSED" in capsys.readouterr().out


def test_hidden_disclosure_covers_only_what_the_verdict_counts(tmp_path, capsys):
    """An info finding hidden by ``--errors-only`` is not a hidden warning."""
    report = _write(tmp_path, _kct_report([INFO]))
    rc = drc_cmd.main(
        [str(report), "--strict", "--allow-incomplete", "--errors-only", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["violations"] == []
    assert "hidden_by_view_filter" not in data["summary"]

    drc_cmd.main([str(report), "--allow-incomplete", "--errors-only"])
    out = capsys.readouterr().out
    assert "Hidden:" not in out
    assert "hidden by view filter" not in out


def test_hidden_real_warning_is_still_disclosed(tmp_path, capsys):
    report = _write(tmp_path, _kct_report([WARNING, INFO]))
    rc = drc_cmd.main([str(report), "--allow-incomplete", "--errors-only", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0  # warnings only fail under --strict
    assert data["summary"]["hidden_by_view_filter"] == {"errors": 0, "warnings": 1}


@pytest.mark.parametrize(
    ("severities", "exit_code", "errors", "warnings"),
    [
        (["error"], 1, 1, 0),
        (["warning"], 2, 0, 1),
        (["info"], 0, 0, 0),
    ],
)
def test_unconnected_items_gate_by_their_own_severity(
    tmp_path, capsys, severities, exit_code, errors, warnings
):
    """Connectivity keeps its gate: error opens fail, warning opens are warnings."""
    report = _write(
        tmp_path,
        {
            "source": "control.kicad_pcb",
            "violations": [],
            "unconnected_items": [
                {
                    "type": "unconnected_items",
                    "severity": s,
                    "description": "Missing connection between items",
                    "items": [{"description": "Pad 1 [VCC] of C1"}],
                }
                for s in severities
            ],
        },
    )
    rc = drc_cmd.main([str(report), "--strict", "--allow-incomplete", "--format", "json"])
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert rc == exit_code
    assert (summary["errors"], summary["warnings"]) == (errors, warnings)
    assert summary["unconnected"] == len(severities)


def test_waived_error_plus_info_exits_zero_under_strict(tmp_path, capsys):
    """Waived findings keep their own bucket; the info finding does not gate."""
    waiver = _write(tmp_path, _COURTYARD_WAIVER, "w.json")
    report = _write(tmp_path, _waivable_report())
    rc = drc_cmd.main(
        [
            str(report),
            "--waivers",
            str(waiver),
            "--strict",
            "--allow-incomplete",
            "--format",
            "json",
        ]
    )
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert rc == 0
    assert (summary["errors"], summary["warnings"], summary["infos"], summary["waived"]) == (
        0,
        0,
        1,
        1,
    )


# ---------------------------------------------------------------------------
# kct drc --mfr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("finding", "exit_code", "errors", "warnings", "infos"),
    [
        (ERROR, 1, 1, 0, 0),
        (WARNING, 2, 0, 1, 0),
        (INFO, 0, 0, 0, 1),
    ],
)
def test_mfr_not_checked_keeps_kicad_cli_severity(
    tmp_path, capsys, finding, exit_code, errors, warnings, infos
):
    """None of these types has a fab-limit check, so kicad-cli's severity stands."""
    report = _write(tmp_path, _kct_report([finding]))
    rc = drc_cmd.main(
        [str(report), "--mfr", "jlcpcb", "--strict", "--allow-incomplete", "--format", "json"]
    )
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert rc == exit_code
    assert summary["not_checked"] == 1
    assert (summary["not_checked_errors"], summary["not_checked_warnings"]) == (errors, warnings)
    assert summary.get("not_checked_infos", 0) == infos
    assert ("not_checked_infos" in summary) is (infos > 0)


def test_mfr_table_not_checked_line_counts_info_as_neither(tmp_path, capsys):
    report = _write(tmp_path, _kct_report([INFO]))
    rc = drc_cmd.main([str(report), "--mfr", "jlcpcb", "--strict", "--allow-incomplete"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT CHECKED against JLCPCB limits: 1 (errors 0, warnings 0, 1 info)" in out


def test_mfr_table_not_checked_line_is_unchanged_without_info(tmp_path, capsys):
    report = _write(tmp_path, _kct_report([WARNING]))
    drc_cmd.main([str(report), "--mfr", "jlcpcb", "--allow-incomplete"])
    out = capsys.readouterr().out
    assert re.search(r"NOT CHECKED against JLCPCB limits: 1 \(errors 0, warnings 1\)$", out, re.M)


# ---------------------------------------------------------------------------
# Export DRC preflight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("findings", "_e", "_w", "_i", "status", "message", "_x"), CASES)
def test_preflight_counts_only_warnings(tmp_path, findings, _e, _w, _i, status, message, _x):
    pcb = _minimal_pcb(tmp_path)
    # kicad-cli shape: no recorded manufacturer, so no rules mismatch to speak of.
    report = _write(tmp_path, _kicad_cli_report(findings, source="board.kicad_pcb"))
    result = _preflight(pcb, report)
    assert (result.status, result.message) == (status, message)


def _sha_bound_report(pcb: Path, violations: list[dict]) -> dict:
    data = _kicad_cli_report(violations, source="board.kicad_pcb")
    data["pcb_sha256"] = hashlib.sha256(pcb.read_bytes()).hexdigest()
    return data


def test_auto_discovered_sha_bound_info_only_report_is_ok(tmp_path):
    """The published-board path: ``drc_report.json`` beside the board it is bound to."""
    pcb = _minimal_pcb(tmp_path)
    _write(tmp_path, _sha_bound_report(pcb, [INFO, INFO]))  # auto-discovered, no --drc-report
    result = _preflight(pcb, None)
    assert (result.status, result.message) == ("OK", "DRC: 0 errors, 0 warnings")


def test_auto_discovered_sha_bound_warning_report_warns(tmp_path):
    pcb = _minimal_pcb(tmp_path)
    _write(tmp_path, _sha_bound_report(pcb, [WARNING, INFO]))
    result = _preflight(pcb, None)
    assert (result.status, result.message) == ("WARN", "DRC passed with 1 warning(s)")


# ---------------------------------------------------------------------------
# kct stitch post-stitch DRC (reads the same report counts)
# ---------------------------------------------------------------------------


def _post_stitch_drc(tmp_path: Path, violations: list[dict]) -> dict:
    report_path = _write(tmp_path, _kicad_cli_report(violations))
    return stitch_cmd._finish_post_stitch_drc(
        KiCadCLIResult(success=True, output_path=report_path, return_code=0),
        tmp_path / "control.kicad_pcb",
    )


def test_post_stitch_drc_does_not_report_info_as_warnings(tmp_path, capsys):
    """``silk_over_copper`` is a warning for a pad and an info for a via."""
    via_info = {**WARNING, "severity": "info", "message": "Silkscreen clipped by via mask"}
    result = _post_stitch_drc(tmp_path, [WARNING, via_info])
    out = capsys.readouterr().out
    assert result["warning_count"] == 1
    assert "Warnings: 1" in out
    assert "silk_over_copper: 1 warning, 1 info" in out
    assert "silk_over_copper: 2 warnings" not in out


def test_post_stitch_drc_passes_an_info_only_report(tmp_path, capsys):
    result = _post_stitch_drc(tmp_path, [{**WARNING, "severity": "info"}])
    out = capsys.readouterr().out
    assert result["passed"] is True
    assert "Warnings: 0" in out
    assert "DRC PASSED" in out


def test_post_stitch_drc_pass_reports_info_count_and_type(tmp_path, capsys):
    """A passing post-stitch check must not hide its informational findings."""
    result = _post_stitch_drc(tmp_path, [{**WARNING, "severity": "info"}])
    out = capsys.readouterr().out
    assert result["passed"] is True
    assert result["info_count"] == 1
    assert result["info_by_type"] == {"silk_over_copper": 1}
    assert "Infos:    1" in out
    assert "DRC PASSED - No errors or warnings (1 informational finding remain)" in out
    assert "silk_over_copper: 1 info" in out
    assert "No violations found" not in out


def test_post_stitch_drc_clean_report_keeps_plain_pass_message(tmp_path, capsys):
    result = _post_stitch_drc(tmp_path, [])
    out = capsys.readouterr().out
    assert result["info_count"] == 0
    assert "DRC PASSED - No violations found" in out
    assert "Infos:" not in out


def test_table_info_only_pass_message_is_accurate(tmp_path, capsys):
    report = parse_json_report(json.dumps(_kct_report([{**WARNING, "severity": "info"}])))
    report.schematic_parity_ran = True
    drc_cmd.output_table(list(report.violations), report)
    out = capsys.readouterr().out
    assert "Infos:      1" in out
    assert "DRC PASSED - No unwaived errors or warnings" in out
    assert "No unwaived violations" not in out


# ---------------------------------------------------------------------------
# Damaged / unrecognized severities never fail open (review of #10673)
# ---------------------------------------------------------------------------

BAD_SEVERITIES = ["warnng", "", "   ", None, 3]


def _kct_with_severity(sev) -> dict:
    return _kct_report([{**WARNING, "severity": sev}])


def _kicad_with_severity(sev) -> dict:
    data = _kicad_cli_report([{**WARNING, "severity": sev}])
    return data


@pytest.mark.parametrize("sev", BAD_SEVERITIES)
@pytest.mark.parametrize("shape", [_kct_with_severity, _kicad_with_severity])
def test_unrecognized_severity_is_a_warning_not_info(tmp_path, shape, sev):
    report = parse_json_report(json.dumps(shape(sev)))
    (v,) = report.violations
    assert v.is_warning and not v.is_info
    assert "unrecognized severity" in v.message


@pytest.mark.parametrize("sev", BAD_SEVERITIES)
@pytest.mark.parametrize("shape", [_kct_with_severity, _kicad_with_severity])
def test_unrecognized_severity_fails_strict_cli(tmp_path, monkeypatch, capsys, shape, sev):
    path = _write(tmp_path, shape(sev))
    code = drc_cmd.main([str(path), "--strict", "--allow-incomplete", "--format", "json"])
    assert code == 2


@pytest.mark.parametrize("sev", BAD_SEVERITIES)
@pytest.mark.parametrize("shape", [_kct_with_severity, _kicad_with_severity])
def test_unrecognized_severity_cannot_yield_ok_preflight(tmp_path, shape, sev):
    pcb = _minimal_pcb(tmp_path)
    data = shape(sev)
    data["pcb_sha256"] = hashlib.sha256(pcb.read_bytes()).hexdigest()
    _write(tmp_path, data)
    result = _preflight(pcb, None)
    assert result.status == "WARN"


@pytest.mark.parametrize("shape", [_kct_report, _kicad_cli_report])
def test_explicit_info_stays_non_gating(tmp_path, shape):
    report = parse_json_report(json.dumps(shape([{**WARNING, "severity": "info"}])))
    (v,) = report.violations
    assert v.is_info and "unrecognized" not in v.message
