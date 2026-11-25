"""JLCPCB versioned DFM threshold table (Issue #8839)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli.check_cmd import _apply_dfm_table
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.base import DFM_THRESHOLD_NAMES


@pytest.fixture(scope="module")
def table():
    t = get_profile("jlcpcb").dfm_thresholds
    assert t is not None
    return t


def test_table_has_version(table):
    assert table.version >= 1


def test_every_threshold_has_provenance(table):
    for entry in table.entries:
        assert set(entry.thresholds) == set(DFM_THRESHOLD_NAMES)
        for name, th in entry.thresholds.items():
            assert th.source_url.startswith("http"), (entry.key, name)
            assert len(th.retrieved) == 10 and th.retrieved[4] == "-", (entry.key, name)
            assert th.basis in {"capability_page", "observed_jlcdfm"}


def test_every_design_rule_stackup_is_covered(table):
    p = get_profile("jlcpcb")
    for key, rules in p.design_rules.items():
        layers = int(key.split("layer_")[0])
        assert p.get_dfm_entry(layers, rules.outer_copper_oz, rules.inner_copper_oz), key


def test_pth_ring_varies_by_copper_weight():
    p = get_profile("jlcpcb")
    one = p.get_dfm_entry(2, 1.0, 0.0).thresholds["annular_ring_pth"].danger_mm
    two = p.get_dfm_entry(4, 2.0, 0.5).thresholds["annular_ring_pth"].danger_mm
    assert one == pytest.approx(0.18)
    assert two == pytest.approx(0.254)


def test_observed_2oz_trace_spacing_recorded():
    ts = get_profile("jlcpcb").get_dfm_entry(4, 2.0, 0.5).thresholds["trace_spacing"]
    assert (ts.danger_mm, ts.warning_mm) == (0.10, 0.15)
    assert ts.basis == "observed_jlcdfm"


@pytest.mark.parametrize("stackup", [(4, 3.0, 0.5), (2, 3.0, 0.0), (4, 2.0, 1.0)])
def test_missing_stackup_has_no_fallback(stackup):
    assert get_profile("jlcpcb").get_dfm_entry(*stackup) is None


def _checker(n_layers=4):
    return SimpleNamespace(
        pcb=SimpleNamespace(copper_layers=list(range(n_layers))),
        design_rules=get_profile("jlcpcb").get_design_rules(n_layers, 2.0),
    )


def test_apply_table_missing_stackup_is_incomplete(capsys):
    cw: dict = {}
    assert _apply_dfm_table(_checker(), "jlcpcb", 4, 3.0, 0.5, 1.0, cw) is True
    assert cw["dfm_table"]["entry"] is None
    assert "INCOMPLETE" in capsys.readouterr().err


def test_apply_table_2oz_floors():
    cw: dict = {}
    c = _checker()
    assert _apply_dfm_table(c, "jlcpcb", 4, 2.0, 0.5, 1.0, cw) is False
    assert c.design_rules.min_pth_annular_ring_mm == pytest.approx(0.254)
    assert c.design_rules.min_solder_mask_dam_mm == pytest.approx(0.20)
    assert cw["dfm_table"]["entry"] == "4layer_2oz"


def test_apply_table_none_profile_fields_use_table_floor():
    """Optional profile fields left as None must not crash max() (#8839)."""
    from dataclasses import replace

    c = _checker()
    c.design_rules = replace(
        c.design_rules, min_smd_pad_clearance_mm=None, min_pth_annular_ring_mm=None
    )
    assert _apply_dfm_table(c, "jlcpcb", 4, 2.0, 0.5, 1.0, {}) is False
    assert c.design_rules.min_pth_annular_ring_mm == pytest.approx(0.254)
    assert c.design_rules.min_smd_pad_clearance_mm is not None


def test_verdict_incomplete_when_dfm_missing(tmp_path):
    from kicad_tools.cli.check_cmd import evaluate_check_verdict

    results = SimpleNamespace(violations=[], mask_copper_assessments=[], rules_checked=1)
    v = evaluate_check_verdict(
        tmp_path / "x.kicad_pcb", results, drc_only=True, dfm_incomplete=True
    )
    assert v.verdict == "incomplete" and v.exit_code != 0
    ok = evaluate_check_verdict(
        tmp_path / "x.kicad_pcb", results, drc_only=True, dfm_incomplete=True, allow_incomplete=True
    )
    assert ok.verdict == "incomplete" and ok.exit_code == 0
    clean = evaluate_check_verdict(tmp_path / "x.kicad_pcb", results, drc_only=True)
    assert clean.verdict == "pass"


_REPO = Path(__file__).resolve().parent.parent
_BOARD_4L = _REPO / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"


@pytest.mark.parametrize("inner", [0.5, 0.434286, 0.45])
def test_lookup_snaps_stock_inner_copper(inner):
    """KiCad's stock 0.0152 mm inner copper is the fab's 0.5 oz (review item 1)."""
    assert get_profile("jlcpcb").get_dfm_entry(4, 1.0, inner).key == "4layer_1oz"


@pytest.mark.parametrize("stackup", [(4, 2.5, 0.5), (4, 1.0, 0.25), (6, 1.0, 2.0)])
def test_lookup_snap_does_not_reach_unoffered_weights(stackup):
    assert get_profile("jlcpcb").get_dfm_entry(*stackup) is None


# Each main() is a full `kct check` (~13s locally); allow headroom on loaded CI.
@pytest.mark.timeout(300)
def test_real_4layer_board_is_not_incomplete(capsys):
    """Regression: a committed 4L board with a stock stackup must not go incomplete."""
    from kicad_tools.cli.check_cmd import main

    main([str(_BOARD_4L), "--mfr", "jlcpcb", "--skip", "mask_to_copper", "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    # The overall verdict can also be "incomplete" for an unrelated reason
    # (fab DFM parity, #8845), so assert on this table's own lookup.
    dfm = data["copper_weights"]["dfm_table"]
    assert dfm["entry"] is not None and dfm["entry"].startswith("4layer_")


# Each main() is a full `kct check` (~13s locally); allow headroom on loaded CI.
@pytest.mark.timeout(300)
def test_real_board_3oz_is_incomplete_and_allow_incomplete(capsys):
    from kicad_tools.cli.check_cmd import main

    args = [str(_BOARD_4L), "--mfr", "jlcpcb", "--skip", "mask_to_copper", "--copper", "3"]
    assert main(args) != 0
    assert main([*args, "--allow-incomplete"]) == 0


def test_table_floors_not_looser_than_profile_outer_floor():
    """Table danger floors may only tighten the profile's outer floors."""
    p = get_profile("jlcpcb")
    for key, rules in p.design_rules.items():
        layers = int(key.split("layer_")[0])
        e = p.get_dfm_entry(layers, rules.outer_copper_oz, rules.inner_copper_oz)
        assert e.thresholds["annular_ring_pth"].danger_mm >= (rules.min_pth_annular_ring_mm or 0)
        assert e.thresholds["mask_bridge"].danger_mm >= rules.min_solder_mask_dam_mm - 1e-9


def test_observed_warning_basis_recorded():
    e = get_profile("jlcpcb").get_dfm_entry(4, 2.0, 0.5)
    assert e.thresholds["annular_ring_via"].warning_basis == "observed_jlcdfm"
    assert e.thresholds["pad_spacing"].warning_mm == pytest.approx(0.13)


# Bump ``version`` in jlcpcb.yaml AND this digest together when any number,
# ``source_url`` or ``retrieved`` date changes (the digest covers provenance,
# not just the millimetre values).
_PINNED = (3, "46378785efc16673")


def _digest(table) -> str:
    rows = [
        (
            e.key,
            e.layers,
            e.outer_oz,
            e.inner_oz,
            n,
            t.danger_mm,
            t.warning_mm,
            t.basis,
            t.warning_basis,
            t.source_url,
            t.retrieved,
        )
        for e in table.entries
        for n, t in sorted(e.thresholds.items())
    ]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def test_version_pinned_to_content_digest(table):
    assert (table.version, _digest(table)) == _PINNED, (
        f"dfm_thresholds changed: bump version and set _PINNED = ({table.version + 1}, "
        f"{_digest(table)!r})"
    )


def test_drc_checker_default_fails_closed_on_3oz_jlcpcb():
    """Non-`kct check` DRCChecker callers keep the #8836 fail-closed raise.

    Only ``kct check`` opts out (``strict_copper=False``) because it applies the
    table's ``incomplete`` verdict; export/MCP/fix-drc/audit paths must not
    silently fall back to 1 oz floors.
    """
    from kicad_tools.manufacturers.base import UnsupportedCopperWeightError
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.checker import DRCChecker

    pcb = PCB.load(str(_BOARD_4L))
    with pytest.raises(UnsupportedCopperWeightError):
        DRCChecker(pcb, manufacturer="jlcpcb", layers=4, copper_oz=3.0)
    # The explicit opt-out used by `kct check` does not raise.
    DRCChecker(pcb, manufacturer="jlcpcb", layers=4, copper_oz=3.0, strict_copper=False)


# --- Issue #8857: one effective-rules path for check / apply-rules / .kicad_dru

_FLOOR_FIELDS = {
    "trace_spacing": "min_clearance_mm",
    "pad_spacing": "min_smd_pad_clearance_mm",
    "annular_ring_via": "min_annular_ring_mm",
    "annular_ring_pth": "min_pth_annular_ring_mm",
    "mask_bridge": "min_solder_mask_dam_mm",
    "tht_smd_spacing": "tht_smd_danger_mm",
}


def _table_stackups():
    t = get_profile("jlcpcb").dfm_thresholds
    return [(e.layers, e.outer_oz, e.inner_oz) for e in t.entries]


@pytest.mark.parametrize("stackup", _table_stackups())
def test_effective_rules_meet_table_floors_and_match_check(stackup):
    layers, outer, inner = stackup
    p = get_profile("jlcpcb")
    entry = p.get_dfm_entry(*stackup)
    eff = p.get_design_rules(layers, outer)
    for name, field_name in _FLOOR_FIELDS.items():
        assert getattr(eff, field_name) >= entry.thresholds[name].danger_mm - 1e-9, (
            entry.key,
            field_name,
        )
    assert eff.tht_smd_warning_mm == pytest.approx(entry.thresholds["tht_smd_spacing"].warning_mm)
    # kct check applies the same floors: its result equals the profile's.
    c = SimpleNamespace(
        pcb=SimpleNamespace(copper_layers=list(range(layers))),
        design_rules=p._base_design_rules(layers, outer),
    )
    assert _apply_dfm_table(c, "jlcpcb", layers, outer, inner, outer, {}) is False
    assert c.design_rules == eff


def test_tht_smd_spacing_thresholds_recorded_on_all_stackups(table):
    """Issue #10172: JLCPCB profile carries calibrated THT-to-SMD bands with provenance."""
    for entry in table.entries:
        th = entry.thresholds["tht_smd_spacing"]
        assert th.danger_mm == pytest.approx(2.032)
        assert th.warning_mm == pytest.approx(3.05)
        assert th.basis == "observed_jlcdfm"
        assert th.source_url == "https://github.com/2AMLogic/kicad-tools/issues/9421"
        assert th.retrieved == "2026-11-18"


def test_checker_tht_smd_spacing_verdict_follows_loaded_profile_threshold(monkeypatch):
    """Issue #10172: varying the loaded JLCPCB profile threshold shifts the checker verdict."""
    from dataclasses import replace

    from kicad_tools.manufacturers.base import DfmEntry, DfmThresholdTable
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import parse_string
    from kicad_tools.validate.checker import DRCChecker

    # THT 1.6 mm round pad at x=0 (edge x=0.8); SMD 1 mm square at x = 0.8 + 2.2 + 0.5 = 3.5.
    # Copper-edge gap is 2.20 mm: warning under the default 2.032 / 3.05 mm profile bands.
    pcb = PCB(
        parse_string(
            """(kicad_pcb (version 20240108) (generator pcbnew)
              (general (thickness 1.6))
              (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
              (net 0 "") (net 1 "A") (net 2 "B")
              (footprint "T" (layer "F.Cu") (at 0 0) (property "Reference" "J1")
                (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.9)
                  (layers "*.Cu" "*.Mask") (net 1 "A") (uuid "tht")))
              (footprint "S" (layer "F.Cu") (at 3.5 0) (property "Reference" "R1")
                (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "B") (uuid "smd"))))"""
        )
    )
    profile = get_profile("jlcpcb")
    base_table = profile.dfm_thresholds
    assert base_table is not None

    def _with_tht_band(danger_mm: float, warning_mm: float) -> DfmThresholdTable:
        entries = []
        for e in base_table.entries:
            new_th = replace(
                e.thresholds["tht_smd_spacing"],
                danger_mm=danger_mm,
                warning_mm=warning_mm,
            )
            entries.append(
                DfmEntry(
                    key=e.key,
                    layers=e.layers,
                    outer_oz=e.outer_oz,
                    inner_oz=e.inner_oz,
                    thresholds={**e.thresholds, "tht_smd_spacing": new_th},
                )
            )
        return DfmThresholdTable(version=base_table.version, entries=tuple(entries))

    # Default profile: 2.20 mm sits between 2.032 and 3.05 -> warning.
    default_found = (
        DRCChecker(pcb, manufacturer="jlcpcb", layers=2, fab_view=True)
        .check_tht_smd_spacing()
        .violations
    )
    assert [v.severity for v in default_found] == ["warning"]

    # Tighter profile (danger=2.50, warning=3.50): 2.20 mm becomes error.
    monkeypatch.setattr(profile, "dfm_thresholds", _with_tht_band(2.50, 3.50))
    tight_found = (
        DRCChecker(pcb, manufacturer="jlcpcb", layers=2, fab_view=True)
        .check_tht_smd_spacing()
        .violations
    )
    assert [v.severity for v in tight_found] == ["error"]
    assert tight_found[0].required_value == pytest.approx(2.50)

    # Looser profile (danger=1.50, warning=2.10): 2.20 mm becomes clean.
    monkeypatch.setattr(profile, "dfm_thresholds", _with_tht_band(1.50, 2.10))
    loose_found = (
        DRCChecker(pcb, manufacturer="jlcpcb", layers=2, fab_view=True)
        .check_tht_smd_spacing()
        .violations
    )
    assert not loose_found


def test_4layer_2oz_mask_dam_is_table_floor_everywhere():
    """The divergence behind #8857: profile said 0.1, table (and check) 0.2."""
    from kicad_tools.manufacturers.dru_generator import generate_dru

    eff = get_profile("jlcpcb").get_design_rules(4, 2.0)
    assert eff.min_solder_mask_dam_mm == pytest.approx(0.20)
    assert "0.2" in generate_dru(eff, "JLCPCB")


@pytest.mark.parametrize("stackup", [s for s in _table_stackups() if s[0] > 2])
def test_inner_and_outer_spacing_stay_separate(stackup):
    layers, outer, _ = stackup
    p = get_profile("jlcpcb")
    base = p._base_design_rules(layers, outer)
    eff = p.get_design_rules(layers, outer)
    assert eff.min_inner_clearance_mm == base.min_inner_clearance_mm


def test_3oz_main_is_incomplete_not_error(tmp_path):
    from kicad_tools.cli.check_cmd import evaluate_check_verdict

    c = SimpleNamespace(
        pcb=SimpleNamespace(copper_layers=list(range(4))),
        design_rules=get_profile("jlcpcb").get_design_rules(4, 1.0),
    )
    assert _apply_dfm_table(c, "jlcpcb", 4, 3.0, 0.5, 1.0, {}) is True
    res = SimpleNamespace(violations=[], mask_copper_assessments=[], rules_checked=1)
    v = evaluate_check_verdict(tmp_path / "x.kicad_pcb", res, drc_only=True, dfm_incomplete=True)
    assert v.verdict == "incomplete"
