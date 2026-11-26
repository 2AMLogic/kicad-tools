"""Operating-state verification on the saved geometry of a mask repair."""

import json

import pytest

from kicad_tools.cli import fix_drc_cmd
from tests.test_repair_mask_copper import PASS, _board, _end, _pt_finding, _report, _run


def _layout(tmp_path, y):
    # Nudging the N1 pass-through away from its own pad brings it
    # closer to a second footprint. Both positions clear ordinary DRC.
    other = f"""
    (footprint "TP" (layer "F.Cu") (at 97 {y})
      (property "Reference" "TP2" (at 0 0) (layer "F.SilkS"))
      (pad "1" smd rect (at 0 0) (size 0.2 0.2)
        (layers "F.Cu" "F.Mask") (net 2 "N2")))
    (gr_rect (start 80 80) (end 120 120) (stroke (width 0.05) (type default))
      (fill none) (layer "Edge.Cuts"))
    """
    return _board(tmp_path, PASS + other)


def _maps(tmp_path):
    idle = tmp_path / "idle.json"
    peak = tmp_path / "second.json"
    idle.write_text(json.dumps({"N1": 50, "N2": 0}))
    peak.write_text(json.dumps({"N1": 150, "N2": 0}))
    return ["--voltage-map", str(idle), "--voltage-map", f"peak={peak}"]


@pytest.fixture
def native_ok(monkeypatch):
    # Synthetic UUIDs. Exercise the actual repair, alignment, rollback and
    # creepage census, with the native DRC and mask reports supplied separately.
    monkeypatch.setattr(
        fix_drc_cmd, "_mask_native_state", lambda board: fix_drc_cmd._MaskNativeState()
    )


def test_only_peak_rejects_move(tmp_path, monkeypatch, capsys, native_ok):
    pcb = _layout(tmp_path, 102.45)
    original = pcb.read_bytes()
    rc = _run(
        monkeypatch,
        pcb,
        [_report(_pt_finding("trk-p")), _report()],
        ["--format", "json", *_maps(tmp_path)],
    )
    data = json.loads(capsys.readouterr().out)
    assert rc != 0 and pcb.read_bytes() == original
    assert data["mask_copper"]["unrepaired"][0]["reason"] == "creepage_worse"
    (row,) = data["creepage"]["findings"]
    assert row["map"] == "peak" and {row["net_a"], row["net_b"]} == {"N1", "N2"}
    assert row["layer"] == "F.Cu"
    assert row["margin_mm_before"] >= 0 > row["margin_mm_after"]
    assert row["creepage_mm_before"] > row["creepage_mm_after"] > 0.2


def test_room_for_both_states(tmp_path, monkeypatch, native_ok):
    pcb = _layout(tmp_path, 104)
    assert _run(monkeypatch, pcb, [_report(_pt_finding("trk-p")), _report()], _maps(tmp_path)) == 0
    assert _end(pcb, "trk-p")[1][1] > 100.62


def test_unrelated_move_survives_existing_shortfall(tmp_path, monkeypatch, capsys, native_ok):
    from tests.test_repair_mask_copper import _seg

    pcb = _layout(tmp_path, 102.35)  # Already short under peak.
    extra = """
    (net 3 "N3")
    (footprint "TP" (layer "F.Cu") (at 100 110)
      (property "Reference" "R3" (at 0 0) (layer "F.SilkS"))
      (pad "1" smd rect (at 0 0) (size 2 1)
        (layers "F.Cu" "F.Mask") (net 3 "N3") (uuid "pad-r3")))
    """
    pcb.write_text(
        pcb.read_text().rstrip()[:-1]
        + extra
        + _seg("other", (95, 110.62), (105, 110.62), net=3)
        + ")"
    )
    bad = _pt_finding("trk-p")
    good = _pt_finding("other", pad="pad-r3", location=(100, 110.52))
    rc = _run(
        monkeypatch,
        pcb,
        [_report(bad, good), _report(), _report(bad), _report(bad)],
        ["--format", "json", *_maps(tmp_path)],
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 2 and data["total_repaired"] == 1
    assert _end(pcb, "trk-p")[1][1] == 100.62
    assert _end(pcb, "other")[1][1] > 110.62
    (row,) = data["creepage"]["findings"]
    assert row["margin_mm_after"] < row["margin_mm_before"] < 0


@pytest.mark.parametrize("separate_output", [True, False])
def test_final_gate_preserves_files(tmp_path, monkeypatch, capsys, native_ok, separate_output):
    pcb = _layout(tmp_path, 104)
    pcb.write_bytes(pcb.read_bytes().replace(b"\n", b"\r\n"))
    original = pcb.read_bytes()
    output = tmp_path / "previous.kicad_pcb"
    output.write_bytes(b"previous output")
    run_mask = fix_drc_cmd._run_mask_pass

    def change_after_mask_check(**kwargs):
        # Simulate a later geometry change after individual actions were graded.
        result = run_mask(**kwargs)
        candidate = kwargs["output_path"]
        candidate.write_text(candidate.read_text().replace("(at 97 104)", "(at 97 101.9)"))
        return result

    monkeypatch.setattr(fix_drc_cmd, "_run_mask_pass", change_after_mask_check)
    rc = _run(
        monkeypatch,
        pcb,
        [_report(_pt_finding("trk-p")), _report()],
        [*_maps(tmp_path), "--format", "json", *(["-o", str(output)] if separate_output else [])],
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 1 and data["total_repaired"] == 0
    assert [(r["map"], r["stage"]) for r in data["creepage"]["findings"]] == [
        ("idle", "final"),
        ("peak", "final"),
    ]
    assert pcb.read_bytes() == original and output.read_bytes() == b"previous output"


def test_candidate_census_unavailable_discards_run(tmp_path, monkeypatch, capsys, native_ok):
    from kicad_tools.creepage.output_audit import CreepageAudit

    pcb = _layout(tmp_path, 104)
    original = pcb.read_bytes()
    census = CreepageAudit._run

    def unavailable_after_move(self, text):
        if text != original.decode() and self.voltage_map.endswith("second.json"):
            return None
        return census(self, text)

    monkeypatch.setattr(CreepageAudit, "_run", unavailable_after_move)
    rc = _run(
        monkeypatch,
        pcb,
        [_report(_pt_finding("trk-p")), _report()],
        [*_maps(tmp_path), "--format", "json"],
    )
    assert rc == 1 and pcb.read_bytes() == original
    assert json.loads(capsys.readouterr().out)["creepage"]["findings"] == [
        {"map": "peak", "reason": "unverifiable", "stage": "pass 1"}
    ]


def test_public_cli_audits_preview(tmp_path, monkeypatch, capsys, native_ok):
    from kicad_tools.cli import main

    pcb = _layout(tmp_path, 102.45)
    original = pcb.read_bytes()
    reports = iter([_report(_pt_finding("trk-p")), _report(), _report()])
    monkeypatch.setattr(fix_drc_cmd, "_run_python_drc", lambda *a, **kw: next(reports))
    rc = main(
        [
            "fix-drc",
            str(pcb),
            "--only",
            "mask-copper",
            "--dry-run",
            "--no-connectivity-check",
            "--format",
            "json",
            *_maps(tmp_path),
            "--creepage-standard",
            "iec60664",
            "--material-group",
            "IIIa",
            "--pollution-degree",
            "2",
            "--hv-threshold",
            "200",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["dry_run"] and data["total_repaired"] == 1
    assert data["creepage"] == {"maps": ["idle", "peak"], "findings": []}
    assert pcb.read_bytes() == original


def test_rejected_preview_explains_distances(tmp_path, monkeypatch, capsys, native_ok):
    pcb = _layout(tmp_path, 102.45)
    original = pcb.read_bytes()
    rc = _run(
        monkeypatch,
        pcb,
        [_report(_pt_finding("trk-p")), _report()],
        [*_maps(tmp_path), "--dry-run"],
    )
    text = capsys.readouterr().out
    assert rc == 1 and pcb.read_bytes() == original
    assert "peak" in text and "N1 / N2 on F.Cu" in text and "margin" in text


def test_reject_duplicate_map_names(tmp_path, capsys):
    pcb = _layout(tmp_path, 104)
    with pytest.raises(SystemExit) as error:
        fix_drc_cmd.main(
            [str(pcb), "--voltage-map", "peak.json", "--voltage-map", "peak=other.json"]
        )
    assert error.value.code == 2 and "duplicate" in capsys.readouterr().err


def test_move_is_graded_after_zone_refill(tmp_path, monkeypatch, capsys, native_ok):
    from kicad_tools.cli import runner
    from tests.test_repair_mask_copper import _seg

    pcb = _layout(tmp_path, 104)
    pcb.write_text(
        pcb.read_text().rstrip()[:-1]
        + '(zone (net 2) (net_name "N2") (layer "F.Cu") (uuid "zone")))'
    )
    original = pcb.read_bytes()

    # Stand in for a refiller which adds close N2 copper. The census must see
    # the saved conductor before attributing the regression to the N1 nudge.
    def refill(candidate, *, require_save):
        assert require_save
        candidate.write_text(
            candidate.read_text().rstrip()[:-1]
            + _seg("refilled-copper", (96, 102.42), (98, 102.42), net=2)
            + ")"
        )
        return runner.KiCadCLIResult(success=True)

    monkeypatch.setattr(runner, "run_refill_zones", refill)
    rc = _run(
        monkeypatch,
        pcb,
        [_report(_pt_finding("trk-p")), _report()],
        ["--format", "json", *_maps(tmp_path)],
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 1 and pcb.read_bytes() == original
    assert data["mask_copper"]["unrepaired"][0]["reason"] == "creepage_worse"
    assert data["creepage"]["findings"][0]["stage"] == "pass 1"


def test_missing_map_with_nothing_to_repair(tmp_path, monkeypatch, capsys):
    pcb = _layout(tmp_path, 104)
    original = pcb.read_bytes()
    rc = _run(
        monkeypatch,
        pcb,
        [_report()],
        ["--voltage-map", str(tmp_path / "missing.json"), "--format", "json"],
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 1 and pcb.read_bytes() == original
    assert data["creepage"]["findings"] == [
        {"map": "missing", "stage": "input", "reason": "unverifiable"}
    ]
