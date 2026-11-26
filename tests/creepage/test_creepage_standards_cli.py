"""CLI tests for the phase-2 standard-derived creepage flags (Issue #4332).

Covers the new ``--standard`` / ``--pollution-degree`` / ``--working-voltage``
/ ``--material-group`` flags: derived-requirement census output (no ``--min``),
both-flags precedence, structured JSON provenance, loud errors for
over-range / undocumented lookups, the "provide --standard or --min" guard,
and phase-1 backward compatibility (``--min``-only output unchanged).
"""

from __future__ import annotations

import json

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.cli.commands.creepage import run_creepage_command
from kicad_tools.cli.parser import create_parser

from .fixtures import (
    board_close_hv_source,
    board_cross_face_edge_wrap_source,
    board_no_hv_source,
    board_same_footprint_only_source,
    board_selected_pairs_source,
    board_source,
    board_two_pad_gap_source,
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


# ---------------------------------------------------------------------------
# Derived-requirement census (no --min)
# ---------------------------------------------------------------------------


def test_standard_derives_requirement_without_min(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec62368",
            "--working-voltage",
            "170",
            "--pollution-degree",
            "2",
            "--insulation",
            "basic",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0  # 18 mm gaps clear the ~2 mm derived requirement
    assert "IEC 62368-1" in out
    assert "Derived required creepage" in out
    # The 170 V RMS step-up lands on the 200 V row -> 2.0 mm (in EE envelope).
    assert "2.000 mm" in out


@pytest.mark.parametrize("voltage_flag", ["--working-voltage", "--voltage-map"])
def test_ordered_thickness_keeps_opposite_face_clearance(tmp_path, capsys, voltage_flag):
    pcb = _write(tmp_path, board_cross_face_edge_wrap_source())
    voltage = "50"
    if voltage_flag == "--voltage-map":
        voltage_file = tmp_path / "voltage.json"
        voltage_file.write_text(json.dumps({"L_MAINS": 50}))
        voltage = str(voltage_file)
    argv = [
        "creepage",
        str(pcb),
        "--net-class-map",
        str(_hv_map_file(tmp_path)),
        "--standard",
        "iec60664",
        "--pollution-degree",
        "2",
        "--insulation",
        "basic",
        "--format",
        "json",
        voltage_flag,
        voltage,
    ]
    assert _run(argv) == 0
    assert json.loads(capsys.readouterr().out)["gate_passed"]

    code = _run([*argv, "--order-thickness-mm", "1.60"])
    report = json.loads(capsys.readouterr().out)
    pair = next(row for row in report["pairs"] if row["kind"] == "conductor")
    assert pair["creepage_mm"] == pytest.approx(4.6)
    assert pair["clearance_mm"] == pytest.approx(1.6)
    assert pair["required_clearance_mm"] == pytest.approx(0.2)
    assert pair["clearance_pass"] and report["gate_passed"]
    assert code == 0


def test_standard_json_carries_provenance(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "170",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
            "--insulation",
            "basic",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["standard"] == "iec60664"
    assert payload["insulation"] == "basic"
    assert payload["pollution_degree"] == 2
    assert payload["material_group"] == "IIIa"
    assert payload["required_creepage_mm"] == 2.0
    # 170 V RMS -> Table F.1 300 V row, OVC II (default) -> 2500 V impulse ->
    # Table F.2 1.5 mm (issue #7057; was 0.2 mm when F.2 was keyed on the
    # 240 V working-voltage peak).
    assert payload["required_clearance_mm"] == 1.5
    clp = payload["clearance_provenance"]
    assert clp["overvoltage_category"] == "II"
    assert clp["impulse_withstand_voltage_v"] == 2500.0
    assert clp["governing_component"] == "impulse"
    cp = payload["creepage_provenance"]
    assert cp["table_id"] == "Table F.5"
    assert cp["voltage_row_used_v"] == 200.0
    assert "disclaimer" in cp
    # Every pair carries the derived requirement + governing bound.
    for pair in payload["pairs"]:
        assert pair["required_creepage_mm"] == 2.0
        assert pair["governing_bound"] == "derived"
        assert pair["required_clearance_mm"] == 1.5
        assert "clearance_pass" in pair
        assert "provenance" in pair
        # Phase-2 drift-guard (#4403): ``relationship`` is always emitted; the
        # single-pad fixture has no same-footprint pairs, and none are waived.
        assert pair["relationship"] == "board"
        assert "waived" not in pair


def test_standard_fail_when_requirement_not_met(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    # 1000 V PD3 IIIa -> 16 mm required creepage; the 18 mm pad gap clears it
    # but the board-edge pair (~11 mm) does not.
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "1000",
            "--pollution-degree",
            "3",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


# ---------------------------------------------------------------------------
# Precedence: both --min and --standard supplied
# ---------------------------------------------------------------------------


def test_precedence_min_stricter_than_derived(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec62368",
            "--working-voltage",
            "170",
            "--pollution-degree",
            "2",
            "--min",
            "100",  # far stricter than the 2 mm derived value
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1  # 100 mm cannot be met by an 18 mm gap
    for pair in payload["pairs"]:
        assert pair["governing_bound"] == "manual (--min)"


def test_precedence_derived_stricter_than_min(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec62368",
            "--working-voltage",
            "170",
            "--pollution-degree",
            "2",
            "--min",
            "0.5",  # weaker than the 2 mm derived value
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    for pair in payload["pairs"]:
        assert pair["governing_bound"] == "derived"


# ---------------------------------------------------------------------------
# Loud errors
# ---------------------------------------------------------------------------


def test_neither_standard_nor_min_errors(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "provide either --standard" in err


def test_standard_without_voltage_errors(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--standard", "iec60664"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "requires both --working-voltage and --pollution-degree" in err


def test_over_range_voltage_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "5000",
            "--pollution-degree",
            "2",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "standard-table lookup failed" in err
    assert "exceeds the highest tabulated row" in err


def test_undocumented_combo_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "200",
            "--pollution-degree",
            "3",
            "--material-group",
            "IIIb",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "standard-table lookup failed" in err


# ---------------------------------------------------------------------------
# Backward compatibility: --min-only output unchanged (phase-1 schema)
# ---------------------------------------------------------------------------


def test_min_only_json_schema_unchanged(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    # Exactly the phase-1 top-level keys -- no phase-2 fields leak in.
    # (``gate_passed`` / ``waived_count`` are always-on additive verdict keys,
    # #4687 -- the exit-code gate and waiver tally, present in both schemas.)
    assert set(payload.keys()) == {
        "board",
        "net_class",
        "min_mm",
        "hv_nets",
        "pair_count",
        "pairs",
        "passed",
        "gate_passed",
        "waived_count",
        "netless_items",  # additive floating-island coverage keys (#8548)
        "netless_treatment",
    }
    for pair in payload["pairs"]:
        # Drift-guard: ``relationship`` is now an always-on additive key (#4403);
        # ``waived`` appears only on waived rows (none here).
        assert set(pair.keys()) == {
            "net_a",
            "net_b",
            "kind",
            "layer",
            "clearance_mm",
            "creepage_mm",
            "margin_mm",
            "relationship",
            "witness",  # always-on additive key (#8301)
            "pass",
        }
        assert pair["relationship"] == "board"


# ---------------------------------------------------------------------------
# Per-net voltage map + pairwise |dV| requirement (Issue #4371)
# ---------------------------------------------------------------------------


def _vmap_file(tmp_path, data, name="voltage_map.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _run_map(tmp_path, capsys, vmap_data, *, fmt="json", pd="2", extra=None):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    vmap = _vmap_file(tmp_path, vmap_data)
    argv = [
        "creepage",
        str(pcb),
        "--net-class-map",
        str(ncm),
        "--standard",
        "iec60664",
        "--pollution-degree",
        pd,
        "--voltage-map",
        str(vmap),
        "--format",
        fmt,
    ]
    if extra:
        argv.extend(extra)
    rc = run_creepage_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr()


def test_voltage_map_same_potential_passes(tmp_path, capsys):
    # L_MAINS and GND both at 90 V -> that pair requires ~0 and PASSes.
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 90, "GND": 90})
    assert rc == 0
    payload = json.loads(cap.out)
    cond = next(p for p in payload["pairs"] if p["kind"] == "conductor")
    assert cond["required_creepage_mm"] == 0.0
    assert cond["pass"] is True


def test_voltage_map_cross_domain_uses_real_delta(tmp_path, capsys):
    # L_MAINS 150 V vs GND (unmapped -> 0 V): the ~1 mm gap FAILs the 150 V req.
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 150})
    payload = json.loads(cap.out)
    cond = next(p for p in payload["pairs"] if p["kind"] == "conductor")
    assert cond["required_creepage_mm"] > 1.0
    assert cond["provenance"]["voltage"]["delta_v_v"] == 150.0


def test_voltage_map_json_report_fields(tmp_path, capsys):
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 150, "_edge_voltage": 5})
    payload = json.loads(cap.out)
    # Report-level scalar requirement / working voltage are null in map mode.
    assert payload["working_voltage_v"] is None
    assert payload["required_creepage_mm"] is None
    assert payload["voltage_source"] == "per-pair |dV| (voltage-map)"
    assert payload["voltage_map"] == {"L_MAINS": 150.0}
    assert payload["edge_voltage_v"] == 5.0


def test_voltage_map_table_header_mentions_per_pair(tmp_path, capsys):
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 150}, fmt="table")
    assert "per-pair |dV| (voltage-map" in cap.out


def test_voltage_map_without_working_voltage_is_allowed(tmp_path, capsys):
    # --working-voltage is NOT required when --voltage-map is supplied.
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 90, "GND": 90})
    assert rc == 0
    assert "requires both --working-voltage" not in cap.err


def test_voltage_map_requires_standard(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    vmap = _vmap_file(tmp_path, {"L_MAINS": 150})
    rc = _run(["creepage", str(pcb), "--voltage-map", str(vmap), "--min", "1.5"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "--voltage-map requires --standard" in err


def test_voltage_map_requires_pollution_degree(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    vmap = _vmap_file(tmp_path, {"L_MAINS": 150})
    rc = _run(["creepage", str(pcb), "--standard", "iec60664", "--voltage-map", str(vmap)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "requires --pollution-degree" in err


def test_voltage_map_missing_file_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--voltage-map",
            str(tmp_path / "does_not_exist.json"),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "voltage-map file not found" in err


def test_voltage_map_malformed_json_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--voltage-map",
            str(bad),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "parsing voltage-map JSON" in err


def test_voltage_map_non_numeric_value_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    vmap = _vmap_file(tmp_path, {"L_MAINS": "high"})
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--voltage-map",
            str(vmap),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid voltage-map structure" in err


def test_voltage_map_over_range_delta_fails_loud(tmp_path, capsys):
    # A per-pair |ΔV| beyond the highest tabulated row must fail loud (never
    # silently pass a safety-critical audit).
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 1_000_000})
    assert rc == 1
    assert "standard-table lookup failed" in cap.err


def test_single_voltage_json_does_not_leak_map_keys(tmp_path, capsys):
    # Backward compat: the single --working-voltage (no map) phase-2 JSON must
    # NOT gain any of the #4371 map-mode keys.
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "150",
            "--pollution-degree",
            "2",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["working_voltage_v"] == 150
    assert payload["required_creepage_mm"] is not None
    for leaked in ("voltage_source", "voltage_map", "edge_voltage_v"):
        assert leaked not in payload


# ---------------------------------------------------------------------------
# Issue #7057: clearance keyed on the rated impulse voltage (Table F.1 -> F.2)
# ---------------------------------------------------------------------------


def _gap_hv_map_file(tmp_path):
    p = tmp_path / "net_class_map.json"
    p.write_text(json.dumps({"HV_LINE": {"name": "HV"}}))
    return p


def _repro_argv(pcb, ncm, *extra):
    return [
        "creepage",
        str(pcb),
        "--net-class-map",
        str(ncm),
        "--standard",
        "iec60664",
        "--working-voltage",
        "230",
        "--pollution-degree",
        "2",
        "--material-group",
        "I",
        *extra,
    ]


def test_230v_mains_1p3mm_gap_fails_clearance(tmp_path, capsys):
    """Reporter's repro: 1.3 mm HV_LINE<->SELV gap at 230 V must FAIL (1.5 mm)."""
    pcb = _write(tmp_path, board_two_pad_gap_source(1.3))
    ncm = _gap_hv_map_file(tmp_path)
    rc = _run(_repro_argv(pcb, ncm, "--insulation", "basic"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Derived required clearance: 1.500 mm" in out
    assert "rated impulse 2500 V" in out
    selv_rows = [ln for ln in out.splitlines() if ln.startswith("HV_LINE") and "SELV" in ln]
    assert selv_rows, out
    assert "1.500" in selv_rows[0]
    assert "FAIL" in selv_rows[0]


def test_230v_mains_1p3mm_gap_json(tmp_path, capsys):
    pcb = _write(tmp_path, board_two_pad_gap_source(1.3))
    ncm = _gap_hv_map_file(tmp_path)
    rc = _run(_repro_argv(pcb, ncm, "--insulation", "basic", "--format", "json"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["required_clearance_mm"] == 1.5
    assert payload["required_creepage_mm"] == 1.25  # Table F.5, 250 V row, group I
    pair = next(p for p in payload["pairs"] if p["net_b"] == "SELV")
    assert pair["clearance_mm"] == pytest.approx(1.3)
    assert pair["required_clearance_mm"] == 1.5
    assert pair["clearance_pass"] is False
    assert pair["pass"] is False


def test_230v_mains_ovc_i_passes_1p3mm_gap(tmp_path, capsys):
    """OVC I (1500 V impulse -> 0.5 mm) clears the 1.3 mm gap; creepage 1.25 too."""
    pcb = _write(tmp_path, board_two_pad_gap_source(1.3))
    ncm = _gap_hv_map_file(tmp_path)
    rc = _run(_repro_argv(pcb, ncm, "--overvoltage-category", "I", "--insulation", "basic"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Derived required clearance: 0.500 mm" in out


def test_overvoltage_category_rejects_invalid_value(capsys):
    with pytest.raises(SystemExit):
        create_parser().parse_args(
            ["creepage", "x.kicad_pcb", "--standard", "iec60664", "--overvoltage-category", "V"]
        )


def test_overvoltage_category_defaults_to_ii():
    args = create_parser().parse_args(["creepage", "x.kicad_pcb"])
    assert args.overvoltage_category == "II"


# ---------------------------------------------------------------------------
# Issue #7082: insulation-type input (default basic)
# ---------------------------------------------------------------------------


def test_insulation_defaults_to_basic():
    args = create_parser().parse_args(["creepage", "x.kicad_pcb"])
    assert args.insulation == "basic"


def test_insulation_rejects_invalid_value():
    with pytest.raises(SystemExit):
        create_parser().parse_args(
            ["creepage", "x.kicad_pcb", "--standard", "iec60664", "--insulation", "double"]
        )


def test_230v_default_basic_requires_1p5mm_clearance(tmp_path, capsys):
    """Default (basic) at 230 V OVC II equals the pre-#7082 output: 1.5 mm."""
    pcb = _write(tmp_path, board_two_pad_gap_source(1.3))
    ncm = _gap_hv_map_file(tmp_path)
    rc = _run(_repro_argv(pcb, ncm))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Insulation: basic" in out
    assert "Derived required clearance: 1.500 mm" in out
    assert "rated impulse 2500 V" in out
    assert "reinforced step-up" not in out
    assert "Derived required creepage: 1.250 mm" in out


def test_230v_reinforced_opt_in_requires_3mm_clearance(tmp_path, capsys):
    """Explicit --insulation reinforced: 2500 -> 4000 V impulse -> 3.0 mm."""
    pcb = _write(tmp_path, board_two_pad_gap_source(1.3))
    ncm = _gap_hv_map_file(tmp_path)
    rc = _run(_repro_argv(pcb, ncm, "--insulation", "reinforced"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Insulation: reinforced" in out
    assert "Derived required clearance: 3.000 mm" in out
    assert "reinforced step-up -> 4000 V" in out
    assert "Derived required creepage: 2.500 mm" in out


def test_230v_reinforced_json_provenance(tmp_path, capsys):
    pcb = _write(tmp_path, board_two_pad_gap_source(1.3))
    ncm = _gap_hv_map_file(tmp_path)
    rc = _run(_repro_argv(pcb, ncm, "--insulation", "reinforced", "--format", "json"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["insulation"] == "reinforced"
    assert payload["required_clearance_mm"] == 3.0
    assert payload["required_creepage_mm"] == 2.5
    clp = payload["clearance_provenance"]
    assert clp["insulation"] == "reinforced"
    assert clp["impulse_withstand_voltage_v"] == 2500.0
    assert clp["required_impulse_voltage_v"] == 4000.0
    assert "UNVERIFIED" in clp["insulation_unverified"]
    cp = payload["creepage_provenance"]
    assert cp["insulation"] == "reinforced"
    assert cp["basic_value_mm"] == 1.25
    assert cp["insulation_factor"] == 2.0


def test_reinforced_3mm_gap_passes_clearance(tmp_path, capsys):
    """A 3.2 mm gap clears the 3.0 mm reinforced clearance (and 2.5 mm creepage)."""
    pcb = _write(tmp_path, board_two_pad_gap_source(3.2))
    ncm = _gap_hv_map_file(tmp_path)
    rc = _run(_repro_argv(pcb, ncm, "--insulation", "reinforced", "--format", "json"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    pair = next(p for p in payload["pairs"] if p["net_b"] == "SELV")
    assert pair["required_clearance_mm"] == 3.0
    assert pair["clearance_pass"] is True


def test_voltage_map_mode_honours_insulation(tmp_path, capsys):
    """The per-pair (voltage-map) engine path threads the insulation type."""
    pcb = _write(tmp_path, board_two_pad_gap_source(1.3))
    vmap = tmp_path / "vmap.json"
    vmap.write_text(json.dumps({"HV_LINE": 230, "SELV": 0}))
    results = {}
    for ins in ("basic", "reinforced"):
        _run(
            [
                "creepage",
                str(pcb),
                "--standard",
                "iec60664",
                "--voltage-map",
                str(vmap),
                "--pollution-degree",
                "2",
                "--material-group",
                "I",
                "--insulation",
                ins,
                "--format",
                "json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["insulation"] == ins
        pair = next(p for p in payload["pairs"] if {p["net_a"], p["net_b"]} == {"HV_LINE", "SELV"})
        results[ins] = pair["required_clearance_mm"]
    assert results == {"basic": 1.5, "reinforced": 3.0}


# ---------------------------------------------------------------------------
# Selected-pair diagnostic (--pair, #10342)
# ---------------------------------------------------------------------------

_STD = ["--standard", "iec60664", "--pollution-degree", "2"]


def _state(tmp_path, potentials, name="state.json"):
    p = tmp_path / name
    p.write_text(json.dumps(potentials))
    return str(p)


def _json_run(argv, capsys):
    rc = _run(argv)
    return rc, json.loads(capsys.readouterr().out)


def _row(report, net_a, net_b):
    return next(r for r in report["pairs"] if (r["net_a"], r["net_b"]) == (net_a, net_b))


def test_pair_matches_full_row_and_is_marked_limited(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=True))
    state = _state(tmp_path, {"L_MAINS": 307, "GND": 0})
    base = ["creepage", str(pcb), *_STD, "--voltage-map", state, "--format", "json"]

    full_rc, full = _json_run(base, capsys)
    assert full_rc == 0 and any(r["kind"] == "edge" for r in full["pairs"])
    assert "diagnostic" not in full and "unresolved_pairs" not in full  # schema unchanged

    rc, sel = _json_run([*base, "--pair", "L_MAINS", "GND"], capsys)
    assert rc == 0
    assert sel["pair_count"] == 1 and sel["pairs"][0]["kind"] == "conductor"
    assert sel["diagnostic"]["limited"] is True
    assert sel["diagnostic"]["complete_census"] is False
    assert sel["diagnostic"]["requested_pairs"] == [["L_MAINS", "GND"]]
    assert sel["unresolved_pairs"] == []

    row = dict(sel["pairs"][0])
    assert row.pop("governing_voltage_v") == pytest.approx(307.0)
    assert row == _row(full, "L_MAINS", "GND")  # distances, layer, witness, rule provenance
    assert row["layer"] == "F.Cu"
    assert row["clearance_mm"] == pytest.approx(18.0)
    assert row["creepage_mm"] == pytest.approx(21.8472, abs=1e-3)
    assert row["witness"]["item_a"] == "pad" and row["witness"]["item_b"] == "pad"


def test_pair_table_marks_scope_and_witness(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=True))
    state = _state(tmp_path, {"L_MAINS": 307, "GND": 0})
    argv = ["creepage", str(pcb), *_STD, "--voltage-map", state, "--pair", "L_MAINS", "GND"]
    assert _run([*argv, "--witness"]) == 0
    out = capsys.readouterr().out
    assert "LIMITED DIAGNOSTIC" in out and "NOT a full-board result" in out
    assert "<board edge>" not in out
    assert "Witness" in out and "pad -> pad" in out
    assert "rule ->" in out


def test_pairs_only_requested_contribute_and_state_drives_requirement(tmp_path, capsys):
    pcb = _write(tmp_path, board_selected_pairs_source())
    # SIG is unrelated to the requested pairs and carries an out-of-range potential.
    bad = {"L_MAINS": 307, "GND": 0, "AUX": 50, "SIG": 1_000_000}
    state = _state(tmp_path, bad)
    base = ["creepage", str(pcb), *_STD, "--voltage-map", state, "--format", "json"]
    # A whole-census lookup (then filtering) would have to evaluate SIG and fail loudly.
    assert _run([*base, "--net-class-map", str(_hv_map_file(tmp_path))]) == 1
    capsys.readouterr()

    pairs = ["--pair", "L_MAINS", "GND", "--pair", "AUX", "GND", "--pair", "GND", "AUX"]
    rc, rep = _json_run([*base, *pairs], capsys)
    assert rc == 0
    assert rep["pair_count"] == 2  # reversed duplicate measured once
    assert {(r["net_a"], r["net_b"]) for r in rep["pairs"]} == {("L_MAINS", "GND"), ("AUX", "GND")}
    assert not any("SIG" in (r["net_a"], r["net_b"]) for r in rep["pairs"])
    first = _row(rep, "AUX", "GND")
    assert first["governing_voltage_v"] == pytest.approx(50.0)

    # Second operating state with an interval: requirement follows it, geometry does not.
    state2 = _state(
        tmp_path, {"L_MAINS": 307, "GND": 0, "AUX": {"min": -100, "max": 50}}, "s2.json"
    )
    argv2 = ["creepage", str(pcb), *_STD, "--voltage-map", state2, "--format", "json", *pairs]
    rc2, rep2 = _json_run(argv2, capsys)
    assert rc2 == 0
    second = _row(rep2, "AUX", "GND")
    assert second["governing_voltage_v"] == pytest.approx(100.0)
    assert second["required_creepage_mm"] > first["required_creepage_mm"]
    assert second["provenance"]["voltage"]["net_a_endpoint"] == "lo"
    for key in ("clearance_mm", "creepage_mm", "layer", "witness"):
        assert second[key] == first[key]


def test_pair_low_voltage_without_hv_and_explicit_zero(tmp_path, capsys):
    pcb = _write(tmp_path, board_no_hv_source())
    state = _state(tmp_path, {"SIG": 3.3, "GND": 0})
    base = ["creepage", str(pcb), *_STD, "--voltage-map", state, "--format", "json"]
    rc, census = _json_run(base, capsys)  # the whole-board default sees nothing
    assert census["hv_nets"] == [] and census["pair_count"] == 0

    rc, rep = _json_run([*base, "--pair", "SIG", "GND"], capsys)
    assert rc == 0 and rep["pair_count"] == 1 and rep["unresolved_pairs"] == []
    assert rep["pairs"][0]["governing_voltage_v"] == pytest.approx(3.3)
    assert rep["pairs"][0]["clearance_mm"] == pytest.approx(18.0)

    zero = _state(tmp_path, {"SIG": 0, "GND": 0}, "zero.json")
    argv = ["creepage", str(pcb), *_STD, "--voltage-map", zero, "--format", "json"]
    rc, rep = _json_run([*argv, "--pair", "SIG", "GND"], capsys)
    assert rc == 0 and rep["pair_count"] == 1  # an explicit 0 V is a valid potential
    assert rep["pairs"][0]["provenance"]["voltage"]["same_potential"] is True


@pytest.mark.parametrize(
    "pair, potentials, reason",
    [
        (("SIG", "GND"), {"SIG": 3.3}, "no_potential"),  # omitted, never an implicit 0 V
        (("SIG", "GDN"), {"SIG": 3.3, "GND": 0}, "unknown_net"),
        (("SIG", "NOCU"), {"SIG": 3.3, "NOCU": 0}, "no_copper"),
    ],
)
def test_pair_unmeasurable_is_named_and_fails(tmp_path, capsys, pair, potentials, reason):
    pcb = _write(tmp_path, board_selected_pairs_source())
    state = _state(tmp_path, potentials)
    argv = ["creepage", str(pcb), *_STD, "--voltage-map", state, "--pair", *pair]
    rc, rep = _json_run([*argv, "--format", "json"], capsys)
    assert rc == 1
    assert rep["pair_count"] == 0 and not rep["passed"] and not rep["gate_passed"]
    assert [u["reason"] for u in rep["unresolved_pairs"]] == [reason]
    assert rep["diagnostic"]["limited"] is True

    assert _run(argv) == 1
    out = capsys.readouterr().out
    assert "UNMEASURED: " in out and reason in out and "LIMITED DIAGNOSTIC" in out


def test_pair_measured_violation_exits_nonzero(tmp_path, capsys):
    pcb = _write(tmp_path, board_close_hv_source())
    state = _state(tmp_path, {"L_MAINS": 307, "GND": 0})
    argv = ["creepage", str(pcb), *_STD, "--voltage-map", state, "--pair", "L_MAINS", "GND"]
    rc, rep = _json_run([*argv, "--format", "json"], capsys)
    assert rc == 1 and rep["unresolved_pairs"] == []
    assert rep["pairs"][0]["pass"] is False and rep["diagnostic"]["limited"] is True


def test_pair_passing_opposite_face_matches_full_engine(tmp_path, capsys):
    pcb = _write(tmp_path, board_cross_face_edge_wrap_source())
    state = _state(tmp_path, {"L_MAINS": 50, "GND": 0})
    base = [
        "creepage",
        str(pcb),
        *_STD,
        "--voltage-map",
        state,
        "--format",
        "json",
        "--order-thickness-mm",
        "1.60",
    ]
    _, full = _json_run([*base, "--net-class-map", str(_hv_map_file(tmp_path))], capsys)
    rc, sel = _json_run([*base, "--pair", "L_MAINS", "GND"], capsys)
    assert rc == 0 and sel["pair_count"] == 1
    row = dict(sel["pairs"][0])
    assert row["layer"] == "F.Cu/B.Cu" and row["pass"] is True
    assert row["creepage_mm"] == pytest.approx(4.6)
    row.pop("governing_voltage_v")
    assert row == _row(full, "L_MAINS", "GND")  # witness + distances agree
    assert sel["board_thickness_mm"] == full["board_thickness_mm"]

    # Without an explicit order the board's own thickness still measures it.
    rc, rep = _json_run([*base[:-2], "--pair", "L_MAINS", "GND"], capsys)
    assert rc == 0 and rep["pair_count"] == 1 and rep["pairs"][0]["layer"] == "F.Cu/B.Cu"


def test_pair_works_with_manual_minimum(tmp_path, capsys):
    pcb = _write(tmp_path, board_selected_pairs_source())
    argv = ["creepage", str(pcb), "--min", "5", "--format", "json"]
    rc, rep = _json_run([*argv, "--pair", "SIG", "GND", "--pair", "AUX", "GND"], capsys)
    assert rc == 1  # AUX-GND is 3 mm
    assert _row(rep, "SIG", "GND")["pass"] and not _row(rep, "AUX", "GND")["pass"]
    assert "governing_voltage_v" not in _row(rep, "SIG", "GND")


@pytest.mark.parametrize("missing", [None, "/AUX", "AUX"])
def test_pair_distinct_slash_nets(tmp_path, capsys, missing):
    pcb = _write(tmp_path, board_selected_pairs_source().replace('"SIG"', '"/AUX"'))
    potentials = {"/AUX": 307, "AUX": 0}
    if missing:
        del potentials[missing]
    argv = [
        "creepage",
        str(pcb),
        *_STD,
        "--voltage-map",
        _state(tmp_path, potentials),
        "--format",
        "json",
        "--pair",
        "/AUX",
        "AUX",
    ]
    rc, rep = _json_run(argv, capsys)
    assert rc == 1
    if missing:
        assert rep["unresolved_pairs"][0]["reason"] == "no_potential"
        assert f"net(s) {missing} absent" in rep["unresolved_pairs"][0]["detail"]
    else:
        assert _row(rep, "/AUX", "AUX")["governing_voltage_v"] == 307


def test_pair_bound_resolves_board_names(tmp_path, capsys):
    pcb = _write(tmp_path, board_selected_pairs_source().replace('"SIG"', '"/AUX"'))
    state = _state(
        tmp_path,
        {
            "/AUX": 307,
            "AUX": 300,
            "/GND": 0,
            "_pair_bounds": [{"nets": ["AUX", "/GND"], "max_dv": 5, "why": "device rating"}],
        },
    )
    rc, rep = _json_run(
        [
            "creepage",
            str(pcb),
            *_STD,
            "--voltage-map",
            state,
            "--format",
            "json",
            "--pair",
            "/AUX",
            "GND",
            "--pair",
            "AUX",
            "GND",
        ],
        capsys,
    )
    assert rc == 0
    assert _row(rep, "/AUX", "GND")["governing_voltage_v"] == 307
    assert _row(rep, "AUX", "GND")["governing_voltage_v"] == 5


@pytest.mark.parametrize("problem", ["standard_lookup", "board_thickness"])
def test_pair_failure_preserves_independent_row(tmp_path, capsys, problem):
    if problem == "standard_lookup":
        pcb = _write(tmp_path, board_selected_pairs_source())
        state = _state(tmp_path, {"L_MAINS": 0, "GND": 1_000_000, "SIG": 5})
        threshold = [*_STD, "--voltage-map", state]
    else:
        source = (
            board_cross_face_edge_wrap_source()
            .replace(
                "(setup (pad_to_mask_clearance 0))",
                '(setup (stackup (layer "dielectric 1" (type "core") (thickness 0.8))))',
            )
            .replace('(net 2 "GND")\n', '(net 2 "GND")\n  (net 3 "SIG")\n', 1)
        )
        source = source.rstrip()[:-1] + (
            '(segment (start 120 110) (end 126 110) (width 1) (layer "F.Cu") (net 3 "SIG")))'
        )
        pcb = _write(tmp_path, source)
        threshold = ["--min", "1"]
    argv = [
        "creepage",
        str(pcb),
        *threshold,
        "--pair",
        "L_MAINS",
        "GND",
        "--pair",
        "L_MAINS",
        "SIG",
    ]
    rc, rep = _json_run([*argv, "--format", "json"], capsys)
    assert rc == 1 and not rep["gate_passed"] and rep["diagnostic"]["limited"]
    assert _row(rep, "L_MAINS", "SIG")["pass"]
    assert [(p["nets"], p["reason"]) for p in rep["unresolved_pairs"]] == [
        (["L_MAINS", "GND"], problem)
    ]
    assert _run(argv) == 1
    table = capsys.readouterr().out
    assert "UNMEASURED: L_MAINS -- GND" in table and "1 measured row(s) clear" in table


@pytest.mark.parametrize("extra,rc", [([], 0), (["--pair", "missing", "SRC_NEG"], 1)])
def test_pair_waiver_summary(tmp_path, capsys, extra, rc):
    pcb = _write(tmp_path, board_same_footprint_only_source())
    assert (
        _run(
            [
                "creepage",
                str(pcb),
                "--pair",
                "L_MAINS",
                "SRC_NEG",
                "--min",
                "1",
                "--waive-same-footprint",
                *extra,
            ]
        )
        == rc
    )
    assert "1 measured row(s) clear their bound, 1 failing row(s) waived" in capsys.readouterr().out
