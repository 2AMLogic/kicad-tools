"""Board-backed native DRC review of two B.Cu buttress wires (Issue #10364)."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli import drc_cmd
from kicad_tools.drc import DRCReport
from kicad_tools.pcb.reinforce import reinforce_net
from kicad_tools.pcb.reinforcement_sidecar import (
    adopt_wire_records,
    build_wire_records,
    sidecar_path,
)
from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint, dump_current_path_specs
from kicad_tools.schema.pcb import PCB
from tests.test_pcb_reinforce import _write_single_pad_footprint_lib


def _wire_board(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    pcb = PCB.create(width=200, height=100, layers=4)
    mod = _write_single_pad_footprint_lib(tmp_path)
    specs, wires = [], []
    for i, (net, y) in enumerate((("FUSED_LINE", 50.000022), ("PGND", 70.0))):
        source, sink = f"J{2 * i + 1}", f"J{2 * i + 2}"
        for ref, x in ((source, 20.0), (sink, 120.0)):
            pcb.add_footprint_from_file(mod, ref, x, y)
            pcb.assign_net_to_footprint_pad(ref, "1", net)
        pcb.add_trace((20, y), (120, y), width=2.0, layer="B.Cu", net=net)
        spec = CurrentPathSpec(
            continuous_a=10.0,
            reinforcement_eligible=True,
            net_name=net,
            name=net,
            source=PathEndpoint(source, "1"),
            sink=PathEndpoint(sink, "1"),
        )
        specs.append(spec)
        result = reinforce_net(pcb, net, current_paths=[spec])
        wires.extend(build_wire_records(result, *pcb.board_origin))
    x, y = pcb.vias[0].position
    pcb.add_via(x, y + 2, size=0.6, drill=0.3, net="FUSED_LINE")
    pcb.save(board)
    (tmp_path / "current_paths.json").write_text(json.dumps(dump_current_path_specs(specs)))
    sidecar_path(board).write_text(json.dumps({"version": 1, "wires": wires}))
    return board


@pytest.fixture
def native_review(tmp_path, monkeypatch):
    board = _wire_board(tmp_path)
    # Native violations captured on the pinned KiCad 10.0.6 toolchain.
    capture = Path(__file__).parent / "fixtures/drc/kicad10_wire_ends_10364.json"
    report = tmp_path / "native.json"
    report.write_bytes(capture.read_bytes())
    raw = json.loads(report.read_bytes())["violations"]
    monkeypatch.setattr(drc_cmd, "run_drc_on_pcb", lambda *a, **kw: DRCReport.load(report))
    return board, report, raw


def test_wire_ends_keep_native_warnings_and_strict_gate(native_review, capsys):
    board, report, raw = native_review
    assert drc_cmd.main([str(board), "--format", "json", "--strict", "--allow-incomplete"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["warnings"] == 5
    for index, (finding, original) in enumerate(zip(data["violations"], raw, strict=True)):
        item = original["items"][0]
        assert finding["message"] == original["description"]
        assert finding["items"] == [item["description"]]
        assert finding["severity"] == original["severity"]
        if index == 4:
            assert "wire_end" not in finding
            continue
        evidence = finding["wire_end"]
        assert evidence["via_uuid"] == item["uuid"]
        assert evidence["wire_index"] == index // 2
        assert evidence["net"] == ("FUSED_LINE", "PGND")[index // 2]
        assert evidence["layer"] == "B.Cu"
        assert evidence["endpoint"] == ("start", "end")[index % 2]
        assert [evidence["x_mm"], evidence["y_mm"]] == pytest.approx(
            list(item["pos"].values()), abs=1e-7
        )
    assert drc_cmd.main([str(board), "--allow-incomplete"]) == 0
    table = capsys.readouterr().out
    assert table.count("Intentional wire-end hole") == 4
    assert table.count(raw[0]["description"]) == 5
    assert drc_cmd.main([str(report), "--format", "json", "--allow-incomplete"]) == 0
    assert all("wire_end" not in v for v in json.loads(capsys.readouterr().out)["violations"])


def test_manufacturer_review_preserves_native_warning_gate(native_review, capsys):
    board, _, _ = native_review
    args = [str(board), "--mfr", "jlcpcb", "--allow-incomplete", "--strict"]
    assert drc_cmd.main(args) == 2
    assert capsys.readouterr().out.count("Intentional wire-end hole") == 4
    assert drc_cmd.main([*args, "--format", "json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["not_checked_warnings"] == 5
    assert sum("wire_end" in c for c in data["checks"]) == 4


@pytest.mark.parametrize(
    "change",
    [
        "stale",
        "stale_run",
        "duplicate",
        "shared_end",
        "missing",
        "drill",
        "gauge",
        "copper",
        "narrow_track_gap",
        "wrong_uuid",
        "no_paths",
        "no_wires",
    ],
)
def test_incomplete_evidence_leaves_warnings_visible(change, native_review, capsys):
    board, report, raw = native_review
    path = sidecar_path(board)
    data = json.loads(path.read_text())
    wire = data["wires"][0]
    if change == "stale":
        wire["anchors"][0]["x"] += 0.0001
    elif change == "stale_run":
        wire["leg_end"]["y"] += 0.0001
    elif change == "gauge":
        wire["wire_gauge_awg"] = 14
    elif change == "duplicate":
        data["wires"].append(wire)
    elif change == "shared_end":
        data["wires"].append({**wire, "anchors": [wire["anchors"][0], {"x": 100, "y": 100}]})
    elif change in ("drill", "missing", "copper", "narrow_track_gap"):
        from kicad_tools.sexp import parse_file, parse_string

        tree = parse_file(board)
        if change == "copper":
            tree.find("segment").find("layer").set_value(0, "F.Cu")
        elif change == "narrow_track_gap":
            # The leg still passes the completeness search (2 mm max width).
            # Its offset start via misses the narrow local track by 0.072 mm.
            track = tree.find("segment")
            far = parse_string(track.to_string())
            far.find("uuid").set_value(0, "d45bef3b-c474-4442-9932-a2be4cd66aad")
            far.find("start").set_value(0, 100)
            tree.append(far)
            track.find("end").set_value(0, 100)
            track.find("width").set_value(0, 0.1)
            tree.find("via").find("at").set_value(1, 106.080022)
            wire["anchors"][0]["y"] = 106.08
            raw[0]["items"][0]["pos"]["y"] = 106.080022
            report.write_text(json.dumps({"source": board.name, "violations": raw}))
        else:
            via = tree.find("via")
            if change == "missing":
                tree.remove(via)
            else:
                via.find("drill").set_value(0, 0.3)
        board.write_text(tree.to_string())
    elif change == "wrong_uuid":
        raw[0]["items"][0]["uuid"] = raw[-1]["items"][0]["uuid"]
        report.write_text(json.dumps({"source": board.name, "violations": raw}))
    elif change == "no_paths":
        (board.parent / "current_paths.json").unlink()
    path.write_text(json.dumps(data))
    if change == "no_wires":
        path.unlink()
    assert drc_cmd.main([str(board), "--format", "json", "--strict"]) == 2
    findings = json.loads(capsys.readouterr().out)["violations"]
    assert len(findings) == 5 and all(v["severity"] == "warning" for v in findings)
    expected = [] if change.startswith("no_") else [2, 3]
    if change == "wrong_uuid":
        expected.insert(0, 1)
    assert [i for i, v in enumerate(findings) if "wire_end" in v] == expected


def test_sidecar_endpoint_order_and_adopted_strip_ends(native_review, capsys):
    board, _, _ = native_review
    path = sidecar_path(board)
    data = json.loads(path.read_text())
    wire = data["wires"][0]
    wire["anchors"].reverse()
    wire["leg_start"], wire["leg_end"] = wire["leg_end"], wire["leg_start"]
    (data["wires"][1],) = adopt_wire_records(PCB.load(board), "PGND").records
    path.write_text(json.dumps(data))
    assert drc_cmd.main([str(board), "--format", "json", "--allow-incomplete"]) == 0
    findings = json.loads(capsys.readouterr().out)["violations"]
    assert [v["wire_end"]["endpoint"] for v in findings[:4]] == ["end", "start", "start", "end"]
