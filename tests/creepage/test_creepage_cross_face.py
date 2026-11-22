"""Cross-face surface paths through NPTH bores, slots and the board edge (#9704).

The census historically paired conductors per copper layer, so a surface path
that crosses from one board face to the other -- down a non-plated bore, along
a milled slot wall, or around the board outline -- was never measured.  Each
path is ``d(conductor on face A, rim) + board thickness + d(rim, conductor on
face B)`` and is folded into the pair's creepage as a minimum, so results can
only get stricter, never looser.
"""

from __future__ import annotations

import json

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.creepage.engine import (
    compute_creepage_census,
    resolve_hv_nets,
    voltage_map_from_dict,
)
from kicad_tools.creepage.standards import DEFAULT_INSULATION, get_standard

from .fixtures import (
    board_cross_face_bore_source,
    board_cross_face_edge_wrap_source,
    board_cross_face_slot_source,
    board_cross_face_two_bore_both_faces_source,
    board_same_face_with_bore_source,
    board_source,
)

pytestmark = pytest.mark.skipif(not has_shapely(), reason="creepage requires shapely")


def _load(tmp_path, source):
    from kicad_tools.schema.pcb import PCB

    p = tmp_path / "board.kicad_pcb"
    p.write_text(source)
    return PCB.load(p)


def _hv_map():
    from kicad_tools.router.rules import net_class_map_from_dict

    return net_class_map_from_dict({"L_MAINS": {"name": "HV"}})


def _census(pcb, hv, **kw):
    return compute_creepage_census(pcb, hv, min_mm=6.3, board="board.kicad_pcb", **kw)


def _conductor_pair(report, net_b):
    for pair in report.pairs:
        if pair.kind == "conductor" and pair.net_b == net_b:
            return pair
    raise AssertionError(f"no conductor pair against {net_b!r} in census")


def _census_map(pcb, hv, vmap, *, insulation=DEFAULT_INSULATION):
    std = get_standard("iec60664")
    parsed, _edge = voltage_map_from_dict(vmap)
    return compute_creepage_census(
        pcb,
        hv,
        None,
        standard="iec60664",
        standard_edition=std.edition,
        pollution_degree=2,
        material_group="IIIa",
        voltage_map=parsed,
        standard_obj=std,
        insulation=insulation,
    )


# ---------------------------------------------------------------------------
# The Evidence geometry (issue #9704): bore path ~3.75 mm vs a 6.3 mm bound
# ---------------------------------------------------------------------------


def test_cross_face_bore_path_reported_failing(tmp_path):
    pcb = _load(tmp_path, board_cross_face_bore_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    # 1.4 (B.Cu run -> bore rim) + 1.6 (board thickness) + 0.75 (rim -> F.Cu
    # track) = 3.75 mm, well under the 6.3 mm requirement.
    assert pair.creepage_mm == pytest.approx(3.75, abs=0.01)
    assert not pair.passed
    # The binding row names the bore (as the island hop names its island).
    assert pair.via_bore == "MH1.1"
    assert pair.to_dict()["via_bore"] == "MH1.1"
    # Cross-face-only pair: the clearance is the in-plan projection (0 here,
    # the F.Cu track crosses the B.Cu run in plan), never above the creepage.
    assert pair.clearance_mm <= pair.creepage_mm
    assert pair.witness is not None


def test_cross_face_bore_path_map_mode_600v_basic(tmp_path):
    # The Evidence voltage model: 600 V vs 0 V, basic insulation -> 6.3 mm.
    pcb = _load(tmp_path, board_cross_face_bore_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census_map(pcb, hv, {"L_MAINS": 600.0}, insulation="basic")

    pair = _conductor_pair(report, "GND")
    assert pair.required_creepage_mm == pytest.approx(6.3)
    assert pair.creepage_mm == pytest.approx(3.75, abs=0.01)
    assert not pair.passed


def test_far_bore_creates_no_cross_face_row(tmp_path):
    # Negative: bore far from either conductor -> the bore path is huge, and a
    # passing cross-face pair stays absent exactly as before #9704.
    pcb = _load(tmp_path, board_cross_face_bore_source(hole_x=135.0, hole_y=115.0))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    assert not any(p.kind == "conductor" and p.net_b == "GND" for p in report.pairs)
    # The board-edge row (clearance 1.0 mm) still passes a 1.0 mm bound.
    report10 = compute_creepage_census(pcb, hv, min_mm=1.0, board="board.kicad_pcb")
    assert report10.passed


def test_cross_face_bore_binds_below_same_face_row(tmp_path):
    # The pair also shares F.Cu (a far-away L_MAINS stub, ~25 mm same-face
    # gap); the 3.75 mm bore path must bind by min(), not replace the row.
    pcb = _load(tmp_path, board_cross_face_bore_source(with_hv_fcu_stub=True))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    assert pair.creepage_mm == pytest.approx(3.75, abs=0.01)
    assert pair.via_bore == "MH1.1"
    assert not pair.passed
    # Same-face results unchanged: the binding same-face layer is still F.Cu
    # and the clearance is still the same-face straight-line gap.
    assert pair.layer == "F.Cu"


# ---------------------------------------------------------------------------
# Board outline wrap and milled-slot wall paths
# ---------------------------------------------------------------------------


def test_cross_face_board_edge_wrap(tmp_path):
    pcb = _load(tmp_path, board_cross_face_edge_wrap_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    # 1.5 (F.Cu -> left wall) + 1.6 (thickness) + 1.5 (wall -> B.Cu) = 4.6 mm.
    assert pair.creepage_mm == pytest.approx(4.6, abs=0.01)
    assert pair.via_bore == "Edge.Cuts"
    assert not pair.passed


def test_cross_face_slot_wall_path(tmp_path):
    pcb = _load(tmp_path, board_cross_face_slot_source())
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    # The slot wall is crossed at ONE rim point: 1.0 (F.Cu pad -> near wall)
    # + 1.6 (thickness) + 3.0 (wall -> B.Cu pad across the 2 mm slot).
    assert pair.creepage_mm == pytest.approx(5.6, abs=0.05)
    assert pair.via_bore is not None and "slot" in pair.via_bore
    assert not pair.passed


# ---------------------------------------------------------------------------
# AC3: same-face results do not change
# ---------------------------------------------------------------------------


def test_same_face_row_unchanged_by_off_path_bore(tmp_path):
    baseline = _load(tmp_path, board_source(with_slot=False))
    bored = _load(tmp_path, board_same_face_with_bore_source())
    hv_a = resolve_hv_nets(baseline, "HV", _hv_map())
    hv_b = resolve_hv_nets(bored, "HV", _hv_map())

    ra = compute_creepage_census(baseline, hv_a, min_mm=1.5)
    rb = compute_creepage_census(bored, hv_b, min_mm=1.5)
    pa, pb = _conductor_pair(ra, "GND"), _conductor_pair(rb, "GND")
    assert pb.clearance_mm == pytest.approx(pa.clearance_mm, abs=1e-6)
    assert pb.creepage_mm == pytest.approx(pa.creepage_mm, abs=1e-6)
    assert pb.layer == pa.layer == "F.Cu"
    assert pb.passed == pa.passed


def test_same_face_pair_with_bore_between_unchanged(tmp_path):
    # #8548's fixture: both pads on F.Cu with an NPTH (bare and annular)
    # between them.  Same-face results must not change: the bore only ever
    # joins two DIFFERENT faces.  The annular variant binds through the
    # floating ring (the pre-existing #8548 island hop), not through the bore.
    from .fixtures import board_net0_npth_source

    pcb = _load(tmp_path, board_net0_npth_source(copper_ring=False))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = compute_creepage_census(pcb, hv, min_mm=1.0)
    pair = _conductor_pair(report, "GND")
    # Straight-line 6 mm edge-to-edge gap, no same-face detour, no bore hop.
    assert pair.clearance_mm == pytest.approx(6.0, abs=1e-3)
    assert pair.creepage_mm == pytest.approx(6.0, abs=1e-3)
    assert pair.via_bore is None

    ringed = _load(tmp_path, board_net0_npth_source(copper_ring=True))
    hv_r = resolve_hv_nets(ringed, "HV", _hv_map())
    report_r = compute_creepage_census(ringed, hv_r, min_mm=1.0)
    pair_r = _conductor_pair(report_r, "GND")
    assert pair_r.creepage_mm == pytest.approx(1.6, abs=1e-3)  # #8548 island hop
    assert pair_r.via_floating is not None
    assert pair_r.via_bore is None


# ---------------------------------------------------------------------------
# Board thickness source
# ---------------------------------------------------------------------------


_GENERAL_1_6 = "(general (thickness 1.6))"


def test_thickness_defaults_to_1_6_without_stackup(tmp_path):
    # Neither a stackup nor a ``(general (thickness X))`` header: the 1.6 mm
    # default is the only source left (#9746 made the header a source).
    source = board_cross_face_bore_source()
    assert _GENERAL_1_6 in source
    source = source.replace(_GENERAL_1_6 + "\n", "")
    pcb = _load(tmp_path, source)
    assert pcb.general_thickness is None
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    assert report.board_thickness_mm == pytest.approx(1.6)
    assert report.board_thickness_source == "default"
    d = report.to_dict()
    assert d["board_thickness_mm"] == pytest.approx(1.6)
    assert d["board_thickness_source"] == "default"


def _with_stackup(source, core_mm):
    stackup = (
        "(setup (pad_to_mask_clearance 0)\n  (stackup\n"
        '    (layer "F.Cu" (type "copper") (thickness 0.035))\n'
        f'    (layer "dielectric 1" (type "core") (thickness {core_mm}))\n'
        '    (layer "B.Cu" (type "copper") (thickness 0.035))\n  )\n)\n'
    )
    return source.replace("(setup (pad_to_mask_clearance 0))\n", stackup)


def test_thickness_from_stackup_when_present(tmp_path):
    source = _with_stackup(board_cross_face_bore_source().replace(_GENERAL_1_6, ""), 1.5)
    pcb = _load(tmp_path, source)
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    assert report.board_thickness_mm == pytest.approx(1.57)
    assert report.board_thickness_source == "stackup"
    # 1.4 + 1.57 + 0.75 = 3.72 mm through the bore.
    pair = _conductor_pair(report, "GND")
    assert pair.creepage_mm == pytest.approx(3.72, abs=0.01)


def test_thickness_from_general_when_no_stackup(tmp_path):
    # #9746: a 0.8 mm board with no stackup must use its header thickness,
    # not the 1.6 mm default -- the default made the bore path 0.8 mm longer
    # than the real one (the optimistic direction).
    source = board_cross_face_bore_source().replace(_GENERAL_1_6, "(general (thickness 0.8))")
    pcb = _load(tmp_path, source)
    assert pcb.general_thickness == pytest.approx(0.8)
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    assert report.board_thickness_mm == pytest.approx(0.8)
    assert report.board_thickness_source == "general"
    d = report.to_dict()
    assert d["board_thickness_mm"] == pytest.approx(0.8)
    assert d["board_thickness_source"] == "general"
    # 1.4 + 0.8 + 0.75 = 2.95 mm through the bore (was 3.75 on the default).
    pair = _conductor_pair(report, "GND")
    assert pair.creepage_mm == pytest.approx(2.95, abs=0.01)
    assert pair.via_bore == "MH1.1"
    assert not pair.passed

    # A bound between the true (2.95) and the old default (3.75) path must
    # FAIL now -- it read as passing before #9746.
    report3 = compute_creepage_census(pcb, hv, min_mm=3.3, board="board.kicad_pcb")
    assert not report3.passed


def _run_thickness_cli(tmp_path, capsys, source, *flags, fmt="json"):
    from kicad_tools.cli.commands.creepage import run_creepage_command
    from kicad_tools.cli.parser import create_parser

    board = tmp_path / "board.kicad_pcb"
    board.write_text(source)
    net_map = tmp_path / "net-class-map.json"
    net_map.write_text(json.dumps({"L_MAINS": {"name": "HV"}}))
    argv = [
        "creepage",
        str(board),
        "--net-class-map",
        str(net_map),
        "--min",
        "3.30",
        "--format",
        fmt,
    ]
    code = run_creepage_command(create_parser().parse_args(argv + list(flags)))
    out = capsys.readouterr()
    return code, json.loads(out.out) if fmt == "json" else out.out + out.err


@pytest.mark.parametrize(("order", "distance", "code"), [(0.80, 2.95, 1), (1.60, 3.75, 0)])
def test_ordered_thickness_governs_cross_face_bore(tmp_path, capsys, order, distance, code):
    source = _with_stackup(
        board_cross_face_bore_source().replace(_GENERAL_1_6, "(general (thickness 0.8))"), 1.5
    )
    # Keep the bore and copper fixed, moving the unrelated left board edge out
    # so its 1 mm margin cannot mask the gate transition for the conductor pair.
    source = source.replace("100 100)", "95 100)").replace("100 120)", "95 120)")
    rc, report = _run_thickness_cli(tmp_path, capsys, source, "--order-thickness-mm", str(order))
    assert rc == code
    assert report["board_thickness_mm"] == order
    assert report["board_thickness_source"] == "order"
    assert report["board_thickness_declarations_mm"] == {"general": 0.8, "stackup": 1.57}
    assert report["gate_passed"] is (code == 0)
    row = next(pair for pair in report["pairs"] if pair["net_b"] == "GND")
    assert row["creepage_mm"] == pytest.approx(distance, abs=0.01)
    assert row["pass"] is (code == 0)
    table_rc, table = _run_thickness_cli(
        tmp_path, capsys, source, "--order-thickness-mm", str(order), fmt="table"
    )
    assert table_rc == code
    assert f"{order:g} mm (order)" in table
    assert "header 0.8 mm, stackup 1.57 mm" in table


def test_order_keeps_independent_edge_failure(tmp_path, capsys):
    source = _with_stackup(board_cross_face_bore_source(), 0.73)
    rc, report = _run_thickness_cli(tmp_path, capsys, source, "--order-thickness-mm", "1.6")
    pairs = {row["kind"]: row for row in report["pairs"]}
    assert rc == 1 and report["gate_passed"] is False
    assert pairs["conductor"]["pass"] and pairs["conductor"]["creepage_mm"] == pytest.approx(3.75)
    assert not pairs["edge"]["pass"] and pairs["edge"]["creepage_mm"] == pytest.approx(1.0)


@pytest.mark.parametrize(("header", "core"), [(0.8, 1.5), (1.6, 0.73)])
def test_unresolved_cross_face_conflict_requests_order(tmp_path, capsys, header, core):
    source = _with_stackup(
        board_cross_face_bore_source().replace(_GENERAL_1_6, f"(general (thickness {header}))"),
        core,
    )
    rc, report = _run_thickness_cli(tmp_path, capsys, source)
    assert rc != 0 and report["gate_passed"] is False
    assert report["board_thickness_declarations_mm"] == {
        "general": header,
        "stackup": pytest.approx(core + 0.07),
    }
    assert "--order-thickness-mm" in report["error"]
    rc, table = _run_thickness_cli(tmp_path, capsys, source, fmt="table")
    assert rc != 0 and "--order-thickness-mm" in table


def test_same_face_skips_conflicting_thickness(tmp_path, capsys):
    same = _with_stackup(board_same_face_with_bore_source(), 0.73)
    expected_rc, expected = _run_thickness_cli(tmp_path, capsys, board_same_face_with_bore_source())
    rc, same_report = _run_thickness_cli(tmp_path, capsys, same)
    assert (rc, same_report) == (expected_rc, expected)


def test_agreeing_cross_face_uses_cad(tmp_path):
    agreeing = _with_stackup(board_cross_face_bore_source(), 1.53)
    pcb = _load(tmp_path, agreeing)
    report = _census(pcb, resolve_hv_nets(pcb, "HV", _hv_map()))
    assert report.board_thickness_mm == pytest.approx(1.6)
    assert report.board_thickness_source == "stackup"
    assert "board_thickness_declarations_mm" not in report.to_dict()


@pytest.mark.parametrize("order", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_invalid_order_rejected_even_on_same_face(tmp_path, capsys, order):
    pcb = _load(tmp_path, board_same_face_with_bore_source())
    with pytest.raises(ValueError, match="positive finite"):
        _census(pcb, resolve_hv_nets(pcb, "HV", _hv_map()), order_thickness_mm=order)
    rc, result = _run_thickness_cli(
        tmp_path, capsys, board_same_face_with_bore_source(), f"--order-thickness-mm={order}"
    )
    assert rc != 0 and "positive finite" in result["error"]


@pytest.mark.parametrize("short_first", [True, False])
def test_both_faces_two_bores_shorter_binds_regardless_of_order(tmp_path, short_first):
    # Both nets carry copper on both faces; the 2.6 mm bore must bind whether
    # the face ordering that finds it runs first or last (it used to be
    # overwritten by the later ordering's 5.0 mm path).
    pcb = _load(tmp_path, board_cross_face_two_bore_both_faces_source(short_first=short_first))
    hv = resolve_hv_nets(pcb, "HV", _hv_map())
    report = _census(pcb, hv)

    pair = _conductor_pair(report, "GND")
    assert pair.creepage_mm == pytest.approx(2.6, abs=0.01)
    assert pair.via_bore == "MHA.1"
    assert not pair.passed

    # A bound between the two paths must still FAIL (no false pass on 5.0 mm).
    report3 = compute_creepage_census(pcb, hv, min_mm=3.0, board="board.kicad_pcb")
    assert not report3.passed
