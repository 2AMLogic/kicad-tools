"""CLI tests for ``kct creepage`` (Issue #4327, phase 1 MVP).

Covers exit codes (0 when all pairs clear ``--min``, non-zero when one is
below), the ``--format json`` census schema (all pairs present, per-pair pass
flag, distinct numeric clearance/creepage/margin), and the "no HV nets found"
path (clean exit 0, no crash).
"""

from __future__ import annotations

import json

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.cli.commands.creepage import run_creepage_command
from kicad_tools.cli.parser import create_parser

from .fixtures import (
    board_attach_zone_source,
    board_benign_suspect_names_source,
    board_mains_named_source,
    board_no_hv_source,
    board_other_layer_board_gap_source,
    board_same_footprint_fail_source,
    board_same_footprint_hides_board_source,
    board_same_footprint_only_source,
    board_scoped_exception_source,
    board_scoped_exception_unaudited_source,
    board_source,
    board_two_sharing_footprints_source,
    scoped_bore_source,
    scoped_dual_face_source,
)

pytestmark = pytest.mark.skipif(not has_shapely(), reason="creepage requires shapely")


def _write(tmp_path, source, name="board.kicad_pcb"):
    p = tmp_path / name
    p.write_text(source)
    return p


def _hv_map_file(tmp_path):
    p = tmp_path / "net_class_map.json"
    p.write_text(json.dumps({"L_MAINS": {"name": "HV"}}))
    return p


def _run(argv):
    args = create_parser().parse_args(argv)
    return run_creepage_command(args)


def test_exit_zero_when_all_pairs_clear(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert "L_MAINS" in out
    # Distinction is explicit in the human output.
    assert "Clearance" in out and "Creepage" in out


def test_exit_nonzero_when_a_pair_below_min(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    # Require 100 mm creepage -- the 18 mm pad gap cannot satisfy it.
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "100"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


def test_json_census_schema(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=True))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        ["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0

    assert payload["net_class"] == "HV"
    assert payload["min_mm"] == 1.5
    assert payload["hv_nets"] == ["L_MAINS"]
    assert payload["pair_count"] == len(payload["pairs"])
    assert payload["pair_count"] >= 2  # HV-vs-GND and HV-vs-edge
    assert payload["passed"] is True
    # #4687: the exit-code verdict + waived count are always serialized; with no
    # waiver active gate_passed is identical to the raw passed.
    assert payload["gate_passed"] is True
    assert payload["waived_count"] == 0

    kinds = {p["kind"] for p in payload["pairs"]}
    assert {"conductor", "edge"} <= kinds

    for pair in payload["pairs"]:
        # Full census: every pair carries numeric fields + a pass flag.
        assert isinstance(pair["clearance_mm"], (int, float))
        assert isinstance(pair["creepage_mm"], (int, float))
        assert isinstance(pair["margin_mm"], (int, float))
        assert isinstance(pair["pass"], bool)
        assert pair["creepage_mm"] >= pair["clearance_mm"] - 1e-6

    gnd = next(p for p in payload["pairs"] if p["kind"] == "conductor" and p["net_b"] == "GND")
    # The slot makes clearance and creepage genuinely different values.
    assert gnd["creepage_mm"] > gnd["clearance_mm"] + 2.0


def test_json_census_no_slot_equals(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    gnd = next(p for p in payload["pairs"] if p["kind"] == "conductor" and p["net_b"] == "GND")
    assert gnd["creepage_mm"] == pytest.approx(gnd["clearance_mm"], abs=1e-4)


def test_no_hv_nets_message_and_exit_zero(tmp_path, capsys):
    pcb = _write(tmp_path, board_no_hv_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No 'HV' nets found" in out


def test_no_hv_nets_json_empty_census(tmp_path, capsys):
    pcb = _write(tmp_path, board_no_hv_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        ["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["pairs"] == []
    assert payload["pair_count"] == 0
    assert payload["passed"] is True
    assert payload["gate_passed"] is True  # vacuously true, like passed (#4687)
    assert payload["waived_count"] == 0


# ---------------------------------------------------------------------------
# #4354 vacuity guard: a mains-looking board must never silently exit 0.
# ---------------------------------------------------------------------------


def test_mains_named_board_no_map_exits_nonzero(tmp_path, capsys):
    # Realistic invocation #1 from the report: NO net-class-map.  The broadened
    # HV name fallback classifies AC_LINE/AC_NEUTRAL/FUSED_LINE, the ~1 mm
    # AC_LINE<->GND gap fails the 250 V IIIa/PD2 requirement -> exit 1 (a real
    # measured failure), NOT a silent exit 0.
    pcb = _write(tmp_path, board_mains_named_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "250",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
        ]
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "Nothing to audit -- exit 0" not in captured.out
    # The mains nets were actually classified and audited.
    assert "AC_LINE" in captured.out


def test_generic_map_hiding_mains_fires_vacuity_guard(tmp_path, capsys):
    # Realistic invocation #2 from the report: a GENERIC net-class-map that
    # names Power/Digital but never HV.  The mains nets are mapped away from HV,
    # so 0 HV nets resolve -- but the board is obviously mains, so the vacuity
    # guard fires with the distinct exit code 2 (NOT a silent 0).
    pcb = _write(tmp_path, board_mains_named_source())
    gmap = tmp_path / "generic_map.json"
    gmap.write_text(
        json.dumps(
            {
                "AC_LINE": {"name": "Power"},
                "AC_NEUTRAL": {"name": "Power"},
                "FUSED_LINE": {"name": "Power"},
            }
        )
    )
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(gmap),
            "--standard",
            "iec60664",
            "--working-voltage",
            "250",
            "--pollution-degree",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2  # distinct "HV unclassified" code, not 0 and not 1
    assert "Nothing to audit -- exit 0" not in captured.out
    assert "WARNING" in captured.err
    assert "AC_LINE" in captured.err
    assert "--net-class-map" in captured.err


def test_high_working_voltage_without_mains_names_fires_guard(tmp_path, capsys):
    # The repro board's real HV nets (TRK_POS/V_RSV_POS) do NOT match mains
    # name patterns.  A mains-level --working-voltage with 0 resolved HV nets
    # must still fire the guard (exit 2), never silent 0.
    pcb = _write(tmp_path, board_no_hv_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "250",
            "--pollution-degree",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "WARNING" in captured.err
    assert "250" in captured.err
    assert "Nothing to audit -- exit 0" not in captured.out


def test_low_voltage_non_hv_board_still_exits_zero(tmp_path, capsys):
    # Negative control: a genuinely low-voltage board (no mains names, working
    # voltage below the 50 V SELV boundary) must still exit 0 with the inert
    # "Nothing to audit" message -- no false alarms.
    pcb = _write(tmp_path, board_no_hv_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "24",
            "--pollution-degree",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "Nothing to audit -- exit 0" in captured.out
    assert "WARNING" not in captured.err


def test_benign_suspect_named_board_exits_zero(tmp_path, capsys):
    # Issue #4365: a board whose only suspect-shaped nets are benign
    # (SPI_LINE / HOT_SWAP / PRIMARY_CLK) with no mains-level working voltage
    # must NOT trip the #4354 vacuity guard -- the tightened MAINS_NAME_RE no
    # longer flags bare LINE / HOT / PRIMARY tokens, so this exits 0.
    pcb = _write(tmp_path, board_benign_suspect_names_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "24",
            "--pollution-degree",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "Nothing to audit -- exit 0" in captured.out
    assert "WARNING" not in captured.err


# ---------------------------------------------------------------------------
# #4403 same-footprint waiver: classify + gate on --waive-same-footprint.
# ---------------------------------------------------------------------------


def test_same_footprint_tagged_in_json(tmp_path, capsys):
    pcb = _write(tmp_path, board_same_footprint_only_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        ["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.0", "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)
    src = next(p for p in payload["pairs"] if p["net_b"] == "SRC_NEG")
    gnd = next(p for p in payload["pairs"] if p["net_b"] == "GND")
    assert src["relationship"] == "same_footprint"
    assert gnd["relationship"] == "board"
    # Without the waiver flag nothing is marked waived.
    assert "waived" not in src
    # #4687: no waiver -> gate_passed mirrors the raw passed and both agree with
    # the exit code.
    assert rc == 1
    assert payload["passed"] is False
    assert payload["gate_passed"] is False
    assert payload["waived_count"] == 0


def test_waiver_off_same_footprint_fail_exits_nonzero(tmp_path, capsys):
    # Default (flag absent): a same-footprint fail still fails the gate (exit 1),
    # exactly as before -- the waiver is opt-in.
    pcb = _write(tmp_path, board_same_footprint_only_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.0"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    # The relationship split is surfaced to prompt the operator toward the flag.
    assert "same-footprint" in out


def test_waiver_on_only_same_footprint_fail_exits_zero(tmp_path, capsys):
    # With --waive-same-footprint and the ONLY fail being same-footprint, the
    # gate passes (exit 0) but the pair is still listed (annotated WAIVED).
    pcb = _write(tmp_path, board_same_footprint_only_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--min",
            "1.0",
            "--waive-same-footprint",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "WAIVED" in out
    assert "SRC_NEG" in out  # still listed, never dropped
    assert "PASS (gate)" in out


def test_waiver_on_board_fail_still_exits_nonzero(tmp_path, capsys):
    # A board-level fail must still fail the gate even when same-footprint pairs
    # are waived.
    pcb = _write(tmp_path, board_same_footprint_fail_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--min",
            "1.0",
            "--waive-same-footprint",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL (gate)" in out
    assert "WAIVED" in out  # the same-footprint pair is annotated but excluded


def test_waiver_on_json_marks_waived_true(tmp_path, capsys):
    pcb = _write(tmp_path, board_same_footprint_only_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--min",
            "1.0",
            "--waive-same-footprint",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    src = next(p for p in payload["pairs"] if p["net_b"] == "SRC_NEG")
    gnd = next(p for p in payload["pairs"] if p["net_b"] == "GND")
    assert src["waived"] is True
    assert "waived" not in gnd  # board pairs are never waived
    # The report-level passed stays False (raw) -- only the exit code (gate) flips.
    assert payload["passed"] is False
    # #4687: the JSON now also carries the verdict the exit code was computed
    # from, so a CI consumer keying on gate_passed agrees with the process.
    assert rc == 0
    assert payload["gate_passed"] is True
    assert payload["waived_count"] == sum(1 for p in payload["pairs"] if p.get("waived"))
    assert payload["waived_count"] >= 1


def test_waiver_on_board_fail_json_gate_passed_false(tmp_path, capsys):
    # A board-level fail keeps gate_passed False even with waived pairs (#4687).
    pcb = _write(tmp_path, board_same_footprint_fail_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--min",
            "1.0",
            "--waive-same-footprint",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["passed"] is False
    assert payload["gate_passed"] is False
    assert payload["waived_count"] == sum(1 for p in payload["pairs"] if p.get("waived"))
    assert payload["waived_count"] >= 1


def test_missing_pcb_file_errors(tmp_path, capsys):
    rc = _run(["creepage", str(tmp_path / "nope.kicad_pcb"), "--min", "1.5"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err


def test_missing_net_class_map_errors(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    rc = _run(
        ["creepage", str(pcb), "--net-class-map", str(tmp_path / "absent.json"), "--min", "1.5"]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "net-class-map file not found" in err


# ---------------------------------------------------------------------------
# #4401 voltage-derived census membership: a high-|V| net whose routing class
# is not HV must still enter the census when a --voltage-map is supplied.
# ---------------------------------------------------------------------------


def _vmap_file(tmp_path, mapping, name="vmap.json"):
    p = tmp_path / name
    p.write_text(json.dumps(mapping))
    return p


def test_census_threshold_pulls_high_v_non_hv_net_into_census(tmp_path, capsys):
    # board_no_hv_source ships SIG + GND -- neither is mains-named nor class-HV,
    # so without a voltage map the census is empty (see
    # test_high_working_voltage_without_mains_names_fires_guard).  Mapping SIG at
    # 150 V with the default 30 V --census-threshold pulls it into hv_nets and
    # produces audited pair rows (against GND and the board edge).
    pcb = _write(tmp_path, board_no_hv_source())
    vmap = _vmap_file(tmp_path, {"SIG": 150.0})
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--voltage-map",
            str(vmap),
            "--pollution-degree",
            "2",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0  # 18 mm gap clears the 150 V requirement
    assert "SIG" in payload["hv_nets"]
    assert payload["pair_count"] >= 2  # SIG-vs-GND and SIG-vs-edge
    kinds = {p["kind"] for p in payload["pairs"]}
    assert {"conductor", "edge"} <= kinds


def test_census_threshold_above_voltage_excludes_net(tmp_path, capsys):
    # A --census-threshold above the mapped potential leaves the net out: SIG at
    # 150 V with a 200 V threshold resolves an empty census (no mains names, no
    # working voltage -> no vacuity guard) -> inert exit 0.
    pcb = _write(tmp_path, board_no_hv_source())
    vmap = _vmap_file(tmp_path, {"SIG": 150.0})
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--voltage-map",
            str(vmap),
            "--pollution-degree",
            "2",
            "--census-threshold",
            "200",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["hv_nets"] == []
    assert payload["pairs"] == []


def test_census_edge_voltage_reference_honored_via_cli(tmp_path, capsys):
    # The reserved _edge_voltage key shifts the reference: with _edge_voltage=140
    # a 150 V net is only 10 V above it -> below the default 30 V threshold ->
    # not a census member.
    pcb = _write(tmp_path, board_no_hv_source())
    vmap = _vmap_file(tmp_path, {"SIG": 150.0, "_edge_voltage": 140.0})
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--voltage-map",
            str(vmap),
            "--pollution-degree",
            "2",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["hv_nets"] == []


def test_census_threshold_inert_without_voltage_map(tmp_path, capsys):
    # Without --voltage-map the flag has no effect: the class/name selection
    # alone governs.  A mains-named board still resolves its HV nets exactly as
    # before, regardless of --census-threshold.
    pcb = _write(tmp_path, board_mains_named_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "250",
            "--pollution-degree",
            "2",
            "--census-threshold",
            "5",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    # Mains nets still classified via the #4354 name fallback (not the voltage
    # union), the ~1 mm AC_LINE<->GND gap fails -> exit 1.
    assert rc == 1
    assert "AC_LINE" in payload["hv_nets"]


def test_census_threshold_defaults_to_thirty(tmp_path):
    # The flag default matches optimize-placement --hv-threshold (30.0).
    args = create_parser().parse_args(["creepage", "board.kicad_pcb", "--min", "1.5"])
    assert args.census_threshold == 30.0


# ---------------------------------------------------------------------------
# #8028 attach-zone waiver: census agrees with the router's #4506 exemption.
# ---------------------------------------------------------------------------


def _attach_argv(pcb, ncm, *extra):
    return [
        "creepage",
        str(pcb),
        "--net-class-map",
        str(ncm),
        "--min",
        "1.0",
        "--format",
        "json",
        *extra,
    ]


def _gnd_row(payload):
    return next(r for r in payload["pairs"] if r["net_b"] == "GND" and r["kind"] == "conductor")


def test_attach_zone_off_by_default_stays_board_fail(tmp_path, capsys):
    pcb = _write(tmp_path, board_attach_zone_source(outside_shortfall=False))
    rc = _run(_attach_argv(pcb, _hv_map_file(tmp_path)))
    payload = json.loads(capsys.readouterr().out)
    row = _gnd_row(payload)
    assert rc != 0
    assert row["relationship"] == "board"
    assert "residual_pass" not in row
    assert payload["waived_count"] == 0


def test_attach_zone_waived_when_residual_passes(tmp_path, capsys):
    pcb = _write(tmp_path, board_attach_zone_source(outside_shortfall=False))
    rc = _run(_attach_argv(pcb, _hv_map_file(tmp_path), "--waive-attach-zone"))
    payload = json.loads(capsys.readouterr().out)
    row = _gnd_row(payload)
    assert row["relationship"] == "attach_zone"
    assert row["waived"] is True
    assert row["residual_pass"] is True
    assert payload["waived_count"] == 1
    # raw verdict stays waiver-blind (kct audit); only gate_passed/exit honour it.
    assert payload["passed"] is False
    assert payload["gate_passed"] is True
    assert rc == 0


@pytest.mark.parametrize("split_x", [None, 111.0, 112.0])
def test_attach_zone_verdict_independent_of_trace_segmentation(tmp_path, capsys, split_x):
    """#8303: a straight GND run split into collinear fragments judges as whole."""
    pcb = _write(
        tmp_path, board_attach_zone_source(outside_shortfall=False, tail=True, split_x=split_x)
    )
    rc = _run(_attach_argv(pcb, _hv_map_file(tmp_path), "--waive-attach-zone"))
    row = _gnd_row(json.loads(capsys.readouterr().out))
    assert row["relationship"] == "attach_zone"
    assert row["waived"] is True
    assert row["residual_pass"] is True
    assert rc == 0


def test_attach_zone_does_not_mask_shortfall_outside_zone(tmp_path, capsys):
    pcb = _write(tmp_path, board_attach_zone_source(outside_shortfall=True))
    rc = _run(_attach_argv(pcb, _hv_map_file(tmp_path), "--waive-attach-zone"))
    payload = json.loads(capsys.readouterr().out)
    row = _gnd_row(payload)
    assert row["relationship"] == "attach_zone"
    assert row["residual_pass"] is False
    assert "waived" not in row
    assert row["residual_creepage_mm"] == pytest.approx(0.8, abs=0.01)
    assert row["residual_witness_mm"][0] > 25  # board-relative: at P3/P4, not Q1 (x~10)
    assert payload["gate_passed"] is False
    assert rc != 0


def test_attach_zone_cross_term_catches_out_of_zone_via(tmp_path, capsys):
    """#8292: out-of-zone foreign copper vs an in-zone pad gates the pair."""
    pcb = _write(tmp_path, board_attach_zone_source(outside_shortfall=False, edge_via=True))
    rc = _run(_attach_argv(pcb, _hv_map_file(tmp_path), "--waive-attach-zone"))
    payload = json.loads(capsys.readouterr().out)
    row = _gnd_row(payload)
    assert row["relationship"] == "attach_zone"
    assert row["residual_pass"] is False
    assert "waived" not in row
    assert row["residual_creepage_mm"] == pytest.approx(0.7, abs=0.02)
    assert row["residual_witness_mm"][0] == pytest.approx(
        10.65, abs=0.05
    )  # pad-edge/via-edge midpoint
    assert payload["gate_passed"] is False
    assert rc != 0


# ---------------------------------------------------------------------------
# #8232: a same_footprint minimum must not hide a board-level approach.
# ---------------------------------------------------------------------------


def _hidden_board_run(tmp_path, capsys, board_gap_mm, *extra):
    pcb = _write(tmp_path, board_same_footprint_hides_board_source(board_gap_mm=board_gap_mm))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--min",
            "1.0",
            "--format",
            "json",
            *extra,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    rows = [p for p in payload["pairs"] if p["net_b"] == "DIV_MID"]
    return rc, payload, rows


def test_same_footprint_pair_also_reports_board_row(tmp_path, capsys):
    _, _, rows = _hidden_board_run(tmp_path, capsys, 1.2)
    assert sorted(r["relationship"] for r in rows) == ["board", "same_footprint"]
    board = next(r for r in rows if r["relationship"] == "board")
    sfp = next(r for r in rows if r["relationship"] == "same_footprint")
    assert board["creepage_mm"] > sfp["creepage_mm"]
    assert abs(board["creepage_mm"] - 1.2) < 0.05


def test_waiver_does_not_hide_failing_board_row(tmp_path, capsys):
    rc, payload, rows = _hidden_board_run(tmp_path, capsys, 0.8, "--waive-same-footprint")
    board = next(r for r in rows if r["relationship"] == "board")
    assert board["pass"] is False
    assert not board.get("waived")
    assert payload["gate_passed"] is False
    assert rc == 1


def test_waiver_passes_when_board_row_clears(tmp_path, capsys):
    rc, payload, rows = _hidden_board_run(tmp_path, capsys, 1.2, "--waive-same-footprint")
    sfp = next(r for r in rows if r["relationship"] == "same_footprint")
    assert sfp["waived"] is True
    assert payload["gate_passed"] is True
    assert rc == 0


def _waived_run(tmp_path, capsys, source):
    pcb = _write(tmp_path, source)
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--min",
            "1.0",
            "--format",
            "json",
            "--waive-same-footprint",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    return rc, payload, [p for p in payload["pairs"] if p["net_b"] == "DIV_MID"]


def test_two_sharing_footprints_cross_approach_reported(tmp_path, capsys):
    rc, payload, rows = _waived_run(tmp_path, capsys, board_two_sharing_footprints_source())
    board = next(r for r in rows if r["relationship"] == "board")
    assert abs(board["creepage_mm"] - 0.8) < 0.05
    assert board["pass"] is False
    assert payload["gate_passed"] is False
    assert rc == 1


def test_board_gap_on_layer_without_sharing_footprint_reported(tmp_path, capsys):
    rc, payload, rows = _waived_run(tmp_path, capsys, board_other_layer_board_gap_source())
    board = next(r for r in rows if r["relationship"] == "board")
    assert board["layer"] == "B.Cu"
    assert abs(board["creepage_mm"] - 0.8) < 0.05
    assert payload["gate_passed"] is False
    assert rc == 1


# ---------------------------------------------------------------------------
# --witness (Issue #8301)
# ---------------------------------------------------------------------------


def test_json_rows_all_carry_witness(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["pairs"]
    for row in payload["pairs"]:
        assert set(row["witness"]) == {"point_a_mm", "point_b_mm", "item_a", "item_b"}


def test_witness_column_only_with_flag(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    base = ["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5"]
    _run(base)
    plain = capsys.readouterr().out
    _run([*base, "--witness"])
    with_w = capsys.readouterr().out
    assert "Witness" not in plain
    assert "Edge.Cuts" not in plain
    assert "Witness" in with_w
    assert "11.000," in with_w
    assert "Edge.Cuts" in with_w


# ---------------------------------------------------------------------------
# #10317: scoped per-pad-pair exceptions alongside the broad same-footprint
# waiver.
# ---------------------------------------------------------------------------

# Phase-2 defaults whose derived bounds the fixture gaps are tuned against:
# 170 V RMS -> required creepage 2.0 mm, required clearance 1.5 mm.
_STD_ARGS = ["--standard", "iec60664", "--working-voltage", "170", "--pollution-degree", "2"]
_EXC_REASON = "Controller review 2026-11-26: staggered footprint clears the case"


def _exc_entry(ref="R1", pads=(1, 2), metric="creepage", reason=_EXC_REASON):
    return {"footprint": ref, "pads": list(pads), "metric": metric, "reason": reason}


def _exception_file(tmp_path, entries, name="exceptions.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"exceptions": entries}))
    return p


def _scoped_run(tmp_path, capsys, source, entries=None, *extra, std=True):
    """Run the census on ``source`` (JSON), optionally with an exception file.

    Returns ``(rc, payload, DIV_MID rows)``.
    """
    pcb = _write(tmp_path, source)
    ncm = _hv_map_file(tmp_path)
    argv = [
        "creepage",
        str(pcb),
        "--net-class-map",
        str(ncm),
        "--format",
        "json",
        *(_STD_ARGS if std else ["--min", "1.0"]),
    ]
    if entries is not None:
        argv += ["--exceptions", str(_exception_file(tmp_path, entries))]
    argv += list(extra)
    rc = _run(argv)
    payload = json.loads(capsys.readouterr().out)
    return rc, payload, [p for p in payload["pairs"] if p["net_b"] == "DIV_MID"]


@pytest.mark.parametrize(
    ("r1_gap", "r2_gap"),
    [(1.6, 1.2), (1.2, 1.4), (1.6, 1.6)],
    ids=["r2-closest", "r1-closest", "tie"],
)
def test_scoped_exception_leaves_second_package_failing(tmp_path, capsys, r1_gap, r2_gap):
    # Waiving R1's creepage must leave R2's pad pair a visible, failing row no
    # matter which gap is the collapsed minimum (or a tie).
    rc, payload, rows = _scoped_run(
        tmp_path,
        capsys,
        board_scoped_exception_source(r1_gap_mm=r1_gap, r2_gap_mm=r2_gap),
        [_exc_entry()],
    )
    assert rc == 1
    assert payload["gate_passed"] is False
    r1 = next(r for r in rows if r.get("footprint") == "R1" and r.get("pads") == ["1", "2"])
    assert r1["exception"] == {"creepage": _EXC_REASON}
    assert r1["pass"] is False  # raw verdict preserved
    assert r1["gate_pass"] is (r1_gap >= 1.5)  # clearance still gates unless it clears
    r2 = next(r for r in rows if r.get("footprint") == "R2" and r.get("pads") == ["1", "2"])
    assert "exception" not in r2
    assert r2["pass"] is False
    assert r2["creepage_mm"] == pytest.approx(r2_gap, abs=0.05)
    assert r2["clearance_mm"] == pytest.approx(r2_gap, abs=0.05)
    # Raw measurements, bounds and per-metric verdicts are retained.
    assert r2["required_creepage_mm"] == pytest.approx(2.0)
    assert r2["required_clearance_mm"] == pytest.approx(1.5)
    assert r2["clearance_pass"] is (r2_gap >= 1.5)
    # The board row on the same nets gates independently.
    board = next(r for r in rows if r["relationship"] == "board")
    assert "exception" not in board


def test_scoped_exception_metric_independence(tmp_path, capsys):
    # Both metrics fail on the reviewed pad pair: waiving either one leaves
    # the other producing exit 1.
    source = board_scoped_exception_source(r1_gap_mm=0.4, r2_gap_mm=2.5)
    rc, payload, rows = _scoped_run(tmp_path, capsys, source, [_exc_entry(metric="creepage")])
    assert rc == 1
    r1 = next(r for r in rows if r.get("footprint") == "R1")
    assert r1["exception"] == {"creepage": _EXC_REASON}
    assert r1["gate_pass"] is False  # air clearance still fails

    rc, payload, rows = _scoped_run(tmp_path, capsys, source, [_exc_entry(metric="clearance")])
    assert rc == 1
    r1 = next(r for r in rows if r.get("footprint") == "R1")
    assert r1["exception"] == {"clearance": _EXC_REASON}
    assert r1["gate_pass"] is False  # surface creepage still fails

    # Covering every failing metric: raw failure preserved, gate passes.
    rc, payload, rows = _scoped_run(
        tmp_path,
        capsys,
        source,
        [_exc_entry(metric="creepage"), _exc_entry(metric="clearance")],
    )
    assert rc == 0
    assert payload["passed"] is False
    assert payload["gate_passed"] is True
    r1 = next(r for r in rows if r.get("footprint") == "R1")
    assert r1["pass"] is False
    assert r1["gate_pass"] is True


def test_scoped_exception_covering_only_failing_metric(tmp_path, capsys):
    # R1 fails creepage only (1.6 < 2.0, clearance 1.6 >= 1.5); covering it
    # produces exit 0 with the raw failure preserved in JSON.
    rc, payload, rows = _scoped_run(
        tmp_path,
        capsys,
        board_scoped_exception_source(r1_gap_mm=1.6, r2_gap_mm=2.5),
        [_exc_entry()],
    )
    assert rc == 0
    assert payload["passed"] is False
    assert payload["gate_passed"] is True
    r1 = next(r for r in rows if r.get("footprint") == "R1")
    assert r1["creepage_mm"] == pytest.approx(1.6, abs=0.05)
    assert r1["required_creepage_mm"] == pytest.approx(2.0)
    assert r1["clearance_pass"] is True
    assert r1["pass"] is False
    assert r1["gate_pass"] is True


@pytest.mark.parametrize("missing_pair", [False, True])
def test_selected_pair_with_scoped_exception(tmp_path, capsys, missing_pair):
    extra = ["--pair", "L_MAINS", "DIV_MID"]
    if missing_pair:
        extra += ["--pair", "L_MAINS", "MISSING_NET"]
    rc, payload, _ = _scoped_run(
        tmp_path, capsys, board_scoped_exception_source(), [_exc_entry()], *extra
    )
    reviewed = next(row for row in payload["pairs"] if row.get("footprint") == "R1")
    assert reviewed["exception"] == {"creepage": _EXC_REASON}
    assert reviewed["gate_pass"] is True
    assert reviewed["governing_voltage_v"] == 170
    assert bool(payload["unresolved_pairs"]) == missing_pair
    assert payload["gate_passed"] == (not missing_pair)
    assert rc == int(missing_pair)


def test_scoped_exception_does_not_waive_board_track(tmp_path, capsys):
    # All package failures covered (R1) or clear (R2); a failing DIV_MID track
    # under R1's L_MAINS pad keeps a board finding and exit 1.
    rc, payload, rows = _scoped_run(
        tmp_path,
        capsys,
        board_scoped_exception_source(r1_gap_mm=1.6, r2_gap_mm=2.5, track_gap_mm=0.5),
        [_exc_entry()],
    )
    assert rc == 1
    assert payload["gate_passed"] is False
    board = next(r for r in rows if r["relationship"] == "board")
    assert board["pass"] is False
    assert board["creepage_mm"] == pytest.approx(0.5, abs=0.05)
    assert "exception" not in board
    r1 = next(r for r in rows if r.get("footprint") == "R1" and r.get("pads") == ["1", "2"])
    assert r1["gate_pass"] is True


def test_scoped_exception_different_pad_pair_still_fails(tmp_path, capsys):
    # A different pad pair (3/4) on the reviewed footprint is a separate row
    # the (1/2) exception cannot waive.
    rc, payload, rows = _scoped_run(
        tmp_path,
        capsys,
        board_scoped_exception_source(r1_gap_mm=1.6, r2_gap_mm=2.5, r1_second_pair_gap_mm=0.4),
        [_exc_entry(pads=(1, 2))],
    )
    assert rc == 1
    pair34 = next(r for r in rows if r.get("footprint") == "R1" and r.get("pads") == ["3", "4"])
    assert pair34["pass"] is False
    assert "exception" not in pair34
    pair12 = next(r for r in rows if r.get("footprint") == "R1" and r.get("pads") == ["1", "2"])
    assert pair12["gate_pass"] is True


def test_scoped_exception_pad_order_irrelevant(tmp_path, capsys):
    source = board_scoped_exception_source(r1_gap_mm=1.6, r2_gap_mm=1.2)
    _, _, rows1 = _scoped_run(tmp_path, capsys, source, [_exc_entry(pads=(1, 2))])
    _, _, rows2 = _scoped_run(tmp_path, capsys, source, [_exc_entry(pads=(2, 1))])
    assert rows1 == rows2


def test_scoped_exception_reproduction_two_sharing_footprints(tmp_path, capsys):
    # The issue's reproduction (phase-1 --min): both packages 0.4 mm apart
    # internally.  Waiving R1 must leave R2 visible and failing; under the
    # collapsed pair row R2 was absent from the report entirely.
    rc, payload, rows = _scoped_run(
        tmp_path, capsys, board_two_sharing_footprints_source(), [_exc_entry()], std=False
    )
    assert rc == 1
    r2 = next(r for r in rows if r.get("footprint") == "R2" and r.get("pads") == ["1", "2"])
    assert r2["pass"] is False
    assert r2["creepage_mm"] == pytest.approx(0.4, abs=0.05)
    board = next(r for r in rows if r["relationship"] == "board")
    assert board["pass"] is False  # 0.8 mm cross-footprint gap < 1.0 mm min
    r1 = next(r for r in rows if r.get("footprint") == "R1" and r.get("pads") == ["1", "2"])
    assert r1["exception"] == {"creepage": _EXC_REASON}


def test_default_census_without_exceptions_is_unchanged(tmp_path, capsys):
    # No --exceptions: the net pair still collapses to the binding row plus
    # the #8232 board row, with no per-pad-pair identity in the JSON.
    rc, payload, rows = _scoped_run(
        tmp_path,
        capsys,
        board_scoped_exception_source(r1_gap_mm=1.6, r2_gap_mm=1.2),
        None,
    )
    assert rc == 1
    assert sorted(r["relationship"] for r in rows) == ["board", "same_footprint"]
    assert all("footprint" not in r and "pads" not in r and "exception" not in r for r in rows)
    sfp = next(r for r in rows if r["relationship"] == "same_footprint")
    assert sfp["creepage_mm"] == pytest.approx(1.2, abs=0.05)  # the collapsed minimum


def test_scoped_exception_composes_with_broad_waiver(tmp_path, capsys):
    # --waive-same-footprint keeps waiving the remaining package rows when a
    # scoped exception file is also present; the board row still gates.
    rc, payload, rows = _scoped_run(
        tmp_path,
        capsys,
        board_scoped_exception_source(r1_gap_mm=0.4, r2_gap_mm=0.4),
        [_exc_entry()],
        "--waive-same-footprint",
    )
    assert rc == 0
    r2 = next(r for r in rows if r.get("footprint") == "R2" and r.get("pads") == ["1", "2"])
    assert r2["waived"] is True
    assert r2["pass"] is False
    board = next(r for r in rows if r["relationship"] == "board")
    assert board["pass"] is True  # cross approaches >= 17 mm


def test_scoped_exception_table_shows_pads_metric_and_reason(tmp_path, capsys):
    pcb = _write(tmp_path, board_scoped_exception_source(r1_gap_mm=1.6, r2_gap_mm=1.2))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            *_STD_ARGS,
            "--exceptions",
            str(_exception_file(tmp_path, [_exc_entry()])),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "R1.1-2" in out
    assert "R2.1-2" in out
    assert "WAIVED" in out
    assert "FAIL" in out
    assert _EXC_REASON in out
    assert "creepage" in out


def test_selected_pair_table_identifies_tied_failing_packages(tmp_path, capsys):
    pcb = _write(tmp_path, board_scoped_exception_source(r1_gap_mm=0.4, r2_gap_mm=0.4))
    rc = _run(
        [
            "creepage",
            str(pcb),
            *_STD_ARGS,
            "--pair",
            "L_MAINS",
            "DIV_MID",
            "--exceptions",
            str(_exception_file(tmp_path, [_exc_entry()])),
        ]
    )
    assert rc == 1
    failures = [
        line.split() for line in capsys.readouterr().out.splitlines() if line.endswith("FAIL")
    ]
    assert sorted(row[-2] for row in failures) == ["R1.1-2", "R2.1-2"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("key", "first", "second", "template"),
    [
        (
            "footprint",
            '"R1"',
            '"R2"',
            '{"exceptions":[{DUP,"metric":"creepage","pads":[1,2],"reason":"x"}]}',
        ),
        (
            "metric",
            '"creepage"',
            '"clearance"',
            '{"exceptions":[{DUP,"footprint":"R1","pads":[1,2],"reason":"x"}]}',
        ),
        ("exceptions", "[]", json.dumps([_exc_entry()]), "{DUP}"),
    ],
)
def test_exception_file_rejects_duplicate_keys(
    tmp_path, capsys, key, first, second, template, reverse
):
    pcb = _write(tmp_path, board_scoped_exception_source(r1_gap_mm=1.6, r2_gap_mm=2.5))
    if reverse:
        first, second = second, first
    exc = tmp_path / "duplicate.json"
    exc.write_text(template.replace("DUP", f'"{key}":{first},"{key}":{second}'))
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(_hv_map_file(tmp_path)),
            *_STD_ARGS,
            "--exceptions",
            str(exc),
        ]
    )
    assert rc == 1
    assert f"duplicate key {key!r}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "entries",
    [
        [{"footprint": "R1", "pads": [1, 2], "metric": "both", "reason": "x"}],
        [{"footprint": "R1", "pads": [1], "metric": "creepage", "reason": "x"}],
        [{"footprint": "R1", "pads": [1, 2], "metric": "creepage"}],
        [{"footprint": "R1", "pads": [1, 2], "metric": "creepage", "reason": "  "}],
        {"exceptions": "nope"},
    ],
    ids=["bad-metric", "one-pad", "no-reason", "blank-reason", "not-a-list"],
)
def test_malformed_exception_file_errors(tmp_path, capsys, entries):
    pcb = _write(tmp_path, board_scoped_exception_source())
    ncm = _hv_map_file(tmp_path)
    exc = tmp_path / "bad.json"
    exc.write_text(json.dumps(entries if isinstance(entries, dict) else {"exceptions": entries}))
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--min",
            "1.0",
            "--exceptions",
            str(exc),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "Error" in captured.err


@pytest.mark.parametrize(
    "entry",
    [
        {"footprint": "NOPE", "pads": [1, 2], "metric": "creepage", "reason": "x"},
        {"footprint": "R1", "pads": [1, 9], "metric": "creepage", "reason": "x"},
    ],
    ids=["unknown-footprint", "unknown-pad"],
)
def test_invalid_exception_selector_errors(tmp_path, capsys, entry):
    pcb = _write(tmp_path, board_scoped_exception_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--min",
            "1.0",
            "--exceptions",
            str(_exception_file(tmp_path, [entry])),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "Error" in captured.err


def test_exception_on_unaudited_net_pair_warns(tmp_path, capsys):
    # An exception whose two nets are never a census pair matches no row: it
    # cannot waive anything and is reported as unused.
    pcb = _write(tmp_path, board_scoped_exception_unaudited_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            *_STD_ARGS,
            "--exceptions",
            str(_exception_file(tmp_path, [_exc_entry(ref="X1", pads=(1, 2))])),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["gate_passed"] is True
    assert payload["exceptions_unused"] == ["X1 pads 1-2 (creepage)"]
    assert "Warning" in captured.err
    assert "X1 pads 1-2" in captured.err


@pytest.mark.parametrize(
    ("gap", "exit_code", "raw_pass", "waived_count", "summary"),
    [
        (0.4, 1, False, 0, "1/4 board pair(s) fail"),
        (1.6, 0, False, 1, "3/4 pair(s) clear"),
        (2.5, 0, True, 0, "4/4 pair(s) clear"),
    ],
)
def test_scoped_exception_summary_counts_the_row_verdict(
    tmp_path, capsys, gap, exit_code, raw_pass, waived_count, summary
):
    board = _write(tmp_path, board_scoped_exception_source(r1_gap_mm=gap, r2_gap_mm=2.5))
    args = [
        "creepage",
        str(board),
        "--net-class-map",
        str(_hv_map_file(tmp_path)),
        *_STD_ARGS,
        "--exceptions",
        str(_exception_file(tmp_path, [_exc_entry()])),
    ]
    assert _run(args) == exit_code
    output = capsys.readouterr().out
    assert _EXC_REASON in output
    assert summary in output.splitlines()[-1]
    assert f"{waived_count} pair(s) WAIVED" in output.splitlines()[-1]

    assert _run([*args, "--format", "json"]) == exit_code
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is raw_pass
    assert report["gate_passed"] is (exit_code == 0)
    assert report["waived_count"] == 0  # No whole-row waiver was requested.
    assert report["gate_waived_count"] == waived_count


def test_empty_hv_selection_reports_every_unused_metric(tmp_path, capsys):
    board = _write(tmp_path, board_scoped_exception_source().replace("L_MAINS", "SIGNAL_A"))
    entries = [_exc_entry(metric="clearance"), _exc_entry(metric="creepage")]
    args = [
        "creepage",
        str(board),
        "--min",
        "2",
        "--format",
        "json",
        "--exceptions",
        str(_exception_file(tmp_path, entries)),
    ]
    assert _run(args) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["pair_count"] == 0
    assert report["exceptions_unused"] == ["R1 pads 1-2 (clearance)", "R1 pads 1-2 (creepage)"]
    assert report["gate_waived_count"] == 0
    assert all(description in captured.err for description in report["exceptions_unused"])


def test_scoped_exception_keeps_cross_face_bore(tmp_path, capsys):
    rc, payload, _ = _scoped_run(
        tmp_path, capsys, scoped_bore_source(), [_exc_entry()], "--min", "4", std=False
    )
    assert rc == 1
    bore = next(r for r in payload["pairs"] if r.get("via_bore") == "MH1.1")
    assert bore["relationship"] == "board"
    assert bore["creepage_mm"] == pytest.approx(3.75, abs=0.01)
    assert bore["pass"] is False


def test_scoped_exception_separates_floating_conductor(tmp_path, capsys):
    source = board_scoped_exception_source(r1_gap_mm=2.6, r2_gap_mm=4.0)
    source = (
        source.rstrip()[:-1]
        + ' (segment (start 110 110.7) (end 110 112.5) (width 0.2) (layer "F.Cu") (net 0)))'
    )
    rc, _, rows = _scoped_run(tmp_path, capsys, source, [_exc_entry()], "--min", "1.5", std=False)
    assert rc == 1
    board = next(r for r in rows if r.get("via_floating") == "segment")
    assert board["relationship"] == "board"
    assert board["creepage_mm"] == pytest.approx(0.6)
    package = next(r for r in rows if r.get("footprint") == "R1")
    assert package["creepage_mm"] == pytest.approx(2.6)
    assert "via_floating" not in package


@pytest.mark.parametrize("repeated_pads", [True, False])
def test_scoped_exception_retains_limiting_clearance(tmp_path, capsys, repeated_pads):
    rc, _, rows = _scoped_run(
        tmp_path,
        capsys,
        scoped_dual_face_source(repeated_pads=repeated_pads),
        [_exc_entry()],
        "--pair",
        "L_MAINS",
        "DIV_MID",
    )
    assert rc == 1
    row = next(r for r in rows if r["layer"] == "B.Cu" and r["clearance_pass"] is False)
    assert row["relationship"] == ("same_footprint" if repeated_pads else "board")
    assert row["clearance_mm"] == pytest.approx(1.2)
    assert row["creepage_mm"] > 2
    assert row["witness"]["item_a"] == ("R1.1" if repeated_pads else "segment")


def test_scoped_exception_witness_preserves_net_order(tmp_path, capsys):
    source = (
        board_scoped_exception_source()
        .replace('(pad "1"', '(pad "swap"')
        .replace('(pad "2"', '(pad "1"')
        .replace('(pad "swap"', '(pad "2"')
    )
    _, _, rows = _scoped_run(tmp_path, capsys, source, [_exc_entry()])
    row = next(r for r in rows if r.get("footprint") == "R1")
    assert row["pads"] == ["1", "2"]
    assert row["net_a"] == "L_MAINS"
    assert row["witness"]["item_a"] == "R1.2"
    assert row["witness"]["item_b"] == "R1.1"
