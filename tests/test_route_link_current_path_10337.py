"""Branch-specific sizing and commit gates for route-link."""

from __future__ import annotations

import contextlib
import json
from collections import Counter

import pytest

from kicad_tools.cli import kicad_toolchain, main, route_link_cmd
from kicad_tools.physics.ampacity import required_widths_for, via_barrel_current_capacity
from kicad_tools.schema.pcb import PCB


def _pad(ref, x, y, *, through=False, net="PWR"):
    shape = (
        'thru_hole circle (size 2.4 2.4) (drill 1) (layers "*.Cu" "*.Mask")'
        if through
        else 'smd rect (size 1 1) (layers "F.Cu" "F.Mask")'
    )
    return f'''(footprint "" (layer "F.Cu") (at {x} {y})
       (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS"))
       (pad "1" {shape} (at 0 0) (net 1 "{net}")))'''


@pytest.fixture
def board(tmp_path):
    p = tmp_path / "branches.kicad_pcb"
    p.write_text(f"""(kicad_pcb (version 20240108) (generator "test")
        (general (thickness 1.6))
        (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
                (31 "B.Cu" signal) (44 "Edge.Cuts" user))
        (setup (stackup (layer "F.Cu" (type "copper") (thickness 0.070))
                        (layer "In1.Cu" (type "copper") (thickness 0.0175))
                        (layer "In2.Cu" (type "copper") (thickness 0.0175))
                        (layer "B.Cu" (type "copper") (thickness 0.070))))
        (net 0 "") (net 1 "PWR")
        (gr_rect (start 100 100) (end 130 125) (stroke (width 0.1) (type default))
                 (fill none) (layer "Edge.Cuts"))
        {_pad("J1", 110, 110)} {_pad("J2", 119, 110, through=True)} {_pad("J3", 119, 116)}
    )""")
    paths = [
        {
            "name": name,
            "net": "PWR",
            "source": {"ref": "J1" if name == "charge" else "J2", "pad": "1"},
            "sink": {"ref": sink, "pad": "1"},
            "continuous_a": amps,
        }
        for name, sink, amps in [("charge", "J2", 1.32), ("sense", "J3", 0.01)]
    ]
    (tmp_path / "current_paths.json").write_text(json.dumps({"paths": paths}))
    return p


CHARGE = [
    "--link",
    "PWR J1.1 J2.1@In1.Cu",
    "--current-path",
    "charge",
    "--via-at",
    "112,110",
    "--link-layers",
    "F.Cu,In1.Cu",
]
SENSE = ["--link", "PWR J2.1 J3.1", "--current-path", "sense", "--link-layers", "F.Cu"]


def run(board, capsys, *args):
    rc = main(["route-link", str(board), "--format", "json", *args])
    return rc, json.loads(capsys.readouterr().out)


def test_branch_widths_on_written_board(board, capsys, monkeypatch):
    monkeypatch.setattr(
        kicad_toolchain, "evidence_toolchain", lambda **kw: contextlib.nullcontext()
    )
    monkeypatch.setattr(route_link_cmd, "_gate_state", lambda *args, **kwargs: (Counter(), None))
    rc, doc = run(board, capsys, *CHARGE, *SENSE)
    assert rc == 0, doc
    pcb = PCB.load(str(board))
    widths = required_widths_for(pcb, 1.32)
    assert {s.layer for s in pcb.segments} == {"In1.Cu", "F.Cu"}
    assert all(
        widths["In1.Cu"] <= s.width < widths["In1.Cu"] + 0.01
        for s in pcb.segments
        if s.layer == "In1.Cu"
    )
    outer = {s.width for s in pcb.segments if s.layer == "F.Cu"}
    assert 0.2 in outer and widths["F.Cu"] <= max(outer) < widths["F.Cu"] + 0.01
    assert len(pcb.vias) == 1 and via_barrel_current_capacity(pcb.vias[0].drill) >= 1.32


@pytest.mark.parametrize(
    ("name", "net", "error"),
    [
        ("unknown", "PWR", "unknown current path"),
        ("charge", "another", "is on 'PWR'"),
    ],
)
def test_selection_errors_precede_writes(board, capsys, name, net, error):
    before = board.read_bytes()
    rc, result = run(board, capsys, *SENSE, "--link", f"{net} J1.1 J2.1", "--current-path", name)
    assert rc == 1 and error in result["error"]
    assert board.read_bytes() == before


def test_explicit_sidecar_and_copper_without_stackup(board, capsys):
    import re

    from kicad_tools.physics.ampacity import width_for_current

    sidecar = board.with_suffix(".json")
    board.with_name("current_paths.json").rename(sidecar)
    board.write_text(
        re.sub(
            r"\(setup \(stackup .*?\n        \(net 0",
            "(setup)\n        (net 0",
            board.read_text(),
            flags=re.S,
        )
    )
    rc, doc = run(
        board,
        capsys,
        *CHARGE,
        "--probe",
        "--current-paths",
        str(sidecar),
        "--copper",
        "outer=2,inner=1",
    )
    assert rc == 0, doc
    width = doc["links"][0]["widths_mm"]["In1.Cu"]
    need = width_for_current(1.32, 1.0, layer="internal")
    assert need <= width < need + 0.01


def test_via_grows_with_current_and_keeps_explicit_dimensions(board, capsys):
    sidecar = board.with_name("current_paths.json")
    paths = json.loads(sidecar.read_text())
    paths["paths"][0]["continuous_a"] = 3.0
    sidecar.write_text(json.dumps(paths))
    rc, doc = run(board, capsys, *CHARGE, "--probe")
    assert rc == 0, doc
    auto = doc["links"][0]
    assert auto["via_drill_mm"] > 0.3
    assert via_barrel_current_capacity(auto["via_drill_mm"]) >= 3.0
    assert auto["via_diameter_mm"] - auto["via_drill_mm"] >= 0.3 - 1e-9
    rc, doc = run(board, capsys, *CHARGE, "--probe", "--via-diameter", "1.6", "--via-drill", "0.8")
    assert rc == 0 and (doc["links"][0]["via_diameter_mm"], doc["links"][0]["via_drill_mm"]) == (
        1.6,
        0.8,
    )


def test_inner_corridor_reports_width_left_after_clearance(board, capsys):
    # Constrain the entrance to J2 on the inner layer; other layers cannot
    # reach the layer-qualified destination pad.
    rails = "".join(
        f'(segment (start 105 {y}) (end 125 {y}) (width 0.2) (layer "In1.Cu") (net 2))'
        for y in (109, 111, 111.3)
    )
    board.write_text(
        board.read_text().replace('(net 0 "")', '(net 2 "OTHER") (net 0 "")').rstrip()[:-1]
        + rails
        + ")"
    )
    rc, doc = run(board, capsys, *CHARGE, "--probe")
    assert rc == 1, doc
    failure = doc["links"][0]
    assert failure["corridor"]["available_width_mm"] == pytest.approx(1.4)
    assert all(token in failure["reason"] for token in ("2.300", "In1.Cu", "1.400"))
    sidecar = board.with_name("current_paths.json")
    paths = json.loads(sidecar.read_text())
    paths["paths"][0]["continuous_a"] = 0.83
    sidecar.write_text(json.dumps(paths))
    rc, doc = run(board, capsys, *CHARGE, "--probe")
    assert rc == 0, doc
    assert doc["links"][0]["widths_mm"]["In1.Cu"] == 1.21


@pytest.mark.parametrize("gauge", [16, 12])
def test_commit_checks_reinforcement_from_input_project(tmp_path, capsys, monkeypatch, gauge):
    from dataclasses import replace

    from kicad_tools.pcb.reinforcement_sidecar import write_sidecar
    from kicad_tools.router.current_paths import PathEndpoint
    from tests.test_path_ampacity_check import _force_spec, _wired_trunk

    pcb, wires = _wired_trunk(trace_width=2.0, end_width=14.0)
    spec = replace(_force_spec(), sink=PathEndpoint("J3", "1"))
    board = tmp_path / "input.kicad_pcb"
    pcb.save(board)
    # The wired-trunk helper's connector pads exist only in memory.
    pads = "".join(
        _pad(ref, x, 50, through=True, net=spec.net_name)
        for ref, x in (("J1", 20), ("J2", 120), ("J3", 137))
    )
    board.write_text(board.read_text().rstrip()[:-1] + pads + ")")
    # A declared 12 AWG wire cannot fit the existing 16 AWG anchor holes.
    write_sidecar(board, [dict(wire, wire_gauge_awg=gauge) for wire in wires])
    board.with_name("current_paths.json").write_text(json.dumps({"paths": [spec.to_dict()]}))
    output = tmp_path / "out" / "renamed.kicad_pcb"
    output.parent.mkdir()
    monkeypatch.setattr(
        kicad_toolchain, "evidence_toolchain", lambda **kw: contextlib.nullcontext()
    )
    monkeypatch.setattr(route_link_cmd, "_gate_state", lambda *a, **kw: (Counter(), None))
    rc, report = run(
        board,
        capsys,
        "--link",
        "FUSED_LINE J2.1 J3.1",
        "--current-path",
        "FORCE",
        "--link-layers",
        "F.Cu",
        "-o",
        str(output),
    )
    if gauge == 16:
        assert rc == 0, report
        assert any(s.width == 12.59 for s in PCB.load(str(output)).segments)
    else:
        assert rc == 1 and report["links"][0]["failed_check"] == "path_ampacity", report
        assert not output.exists()


def test_ampacity_gate_rolls_back_unresolved_declaration(board, capsys, monkeypatch):
    sidecar = board.with_name("current_paths.json")
    paths = json.loads(sidecar.read_text())
    paths["paths"][0]["sink"]["ref"] = "absent"
    sidecar.write_text(json.dumps(paths))
    monkeypatch.setattr(
        kicad_toolchain, "evidence_toolchain", lambda **kw: contextlib.nullcontext()
    )
    monkeypatch.setattr(route_link_cmd, "_gate_state", lambda *a, **kw: (Counter(), None))
    before = board.read_bytes()
    output = board.with_name("previous.kicad_pcb")
    output.write_bytes(b"previous output")
    rc, doc = run(board, capsys, *CHARGE, "--output", str(output))
    assert output.read_bytes() == b"previous output"
    assert rc == 1 and doc["links"][0]["failed_check"] == "path_ampacity"
    assert board.read_bytes() == before


def test_refilled_native_open_rolls_back(board, capsys, monkeypatch):
    from types import SimpleNamespace

    from kicad_tools.cli import runner

    def still_open(_pcb, report, **kwargs):
        assert kwargs["refill_zones"]
        item = {"description": "Pad 1 [PWR] of J1", "uuid": "test", "pos": {"x": 110, "y": 110}}
        report.write_text(
            json.dumps(
                {
                    "violations": [],
                    "unconnected_items": [
                        {
                            "type": "unconnected_items",
                            "description": "Missing connection",
                            "severity": "error",
                            "items": [item, {**item, "description": "Pad 1 [PWR] of J2"}],
                        }
                    ],
                }
            )
        )
        return SimpleNamespace(success=True)

    monkeypatch.setattr(
        kicad_toolchain, "evidence_toolchain", lambda **kw: contextlib.nullcontext()
    )
    monkeypatch.setattr(runner, "run_drc", still_open)
    before = board.read_bytes()
    rc, doc = run(board, capsys, *CHARGE)
    assert rc == 1 and doc["links"][0]["failed_check"] == "connectivity", doc
    assert board.read_bytes() == before
