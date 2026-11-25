"""A pad branch can look the same on a connected pour, an island or a sole plane bridge."""

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from kicad_tools.cli import runner
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker

RULE = "redundant_copper_pour_stub"


def uid(name):
    return str(uuid5(NAMESPACE_URL, "kct/pour-stub/" + name))


def fixture_board(folder: Path, case: str, *, via: bool = True) -> Path:
    path = folder / "stub.kicad_pcb"
    layers = '(0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal)'

    def fp(ref, x, tht=False, disabled=False):
        spec = (
            'thru_hole circle (drill 0.6) (layers "*.Cu" "*.Mask")'
            if tht
            else 'smd rect (layers "F.Cu" "F.Mask")'
        )
        return f"""(footprint "Test:{ref}" (layer "F.Cu") (at {x} 5) (uuid {uid(ref)})
          (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS"))
          (pad "1" {spec} (at 0 0) (size 1.4 1.4) (net 1 "GND")
            (uuid {uid(ref + "pad")}) {"(zone_connect 0)" if disabled else ""}))"""

    def zone(name, layer, x1, y1, x2, y2, keepout=False):
        behavior = (
            "(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))"
            if keepout
            else "(connect_pads yes (clearance 0.2))"
        )
        net = '(net 0) (net_name "")' if keepout else '(net 1) (net_name "GND")'
        return f"""(zone {net} (layer "{layer}") (uuid {uid(name)}) (hatch edge 0.5)
          {behavior} (min_thickness 0.2) (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
          (polygon (pts (xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2}))))"""

    copper = [zone("front", "F.Cu", 1, 1, 7 if case == "bridge" else 19, 10)]
    if case != "island":
        copper.append(zone("plane", "In1.Cu", 1, 1, 19, 10))
    if case == "island":
        copper.append(zone("gap", "F.Cu", 8, 0.1, 10, 11.9, True))
    if case == "void":
        copper.append(zone("void", "F.Cu", 2, 4, 4, 6, True))
    trace = (
        f'(segment (start 3 5) (end 6 5) (width 0.25) (layer "F.Cu") (net 1) (uuid {uid("track")}))'
    )
    hole = (
        f'(via (at 6 5) (size 0.7) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid {uid("via")}))'
    )
    path.write_text(f"""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6)) (paper "A4")
      (layers {layers} (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "GND")
      (gr_rect (start 0 0) (end 20 12) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
      {fp("U1", 3, disabled=case == "disabled")} {fp("J1", 15, tht=case != "unanchored_plane")}
      {trace} {hole if via else ""} {" ".join(copper)})""")
    path.with_suffix(".kicad_pro").write_text('{"meta":{"filename":"stub.kicad_pro","version":1}}')
    path.with_suffix(".kicad_dru").write_text("(version 1)\n")
    return path


def require_native_refill():
    cli = runner.find_kicad_cli()
    if cli is None or not runner._kicad_drc_supports_refill(cli):
        pytest.skip("requires native KiCad refill and save support")


def check_result(path, prove=True):
    return DRCChecker(
        PCB.load(path), manufacturer="jlcpcb", layers=4, prove_pour_stubs=prove
    ).check_redundant_copper()


def findings(path, prove=True):
    return [v for v in check_result(path, prove).violations if v.rule_id == RULE]


def test_proof_is_opt_in_and_skips_refill_by_default(tmp_path, monkeypatch):
    path = fixture_board(tmp_path, "connected")

    def boom(*_a, **_k):
        raise AssertionError("kicad-cli refill must not run without --prove-pour-stubs")

    monkeypatch.setattr(runner, "run_refill_zones", boom)
    assert check_result(path, prove=False).rules_checked_by_rule[RULE] == 0
    assert findings(path, prove=False) == []


@pytest.mark.parametrize("via", [True, False])
def test_refilled_pour_replaces_branch_without_writing_source(tmp_path, via):
    require_native_refill()
    path = fixture_board(tmp_path, "connected", via=via)
    original = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert check_result(path).rules_checked_by_rule[RULE] == 1
    (hit,) = findings(path)
    assert hit.severity == "warning"
    assert hit.location == pytest.approx((3, 5))
    assert hit.nets == ("GND",)
    assert "U1.1" in hit.items and uid("track") in hit.items
    assert (uid("via") in hit.items) == via
    assert "U1.1" in hit.message and (("via" in hit.message) == via)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == original


@pytest.mark.parametrize("case", ["bridge", "island", "void", "disabled", "unanchored_plane"])
def test_unproven_branches_remain_unreported(tmp_path, case):
    require_native_refill()
    assert not findings(fixture_board(tmp_path, case))


def test_no_native_refill_no_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: None)
    assert not findings(fixture_board(tmp_path, "connected"))


def test_unsaved_refill_never_reuses_cached_copper(tmp_path, monkeypatch):
    require_native_refill()
    path = fixture_board(tmp_path, "connected")
    assert runner.run_refill_zones(path, require_save=True).success
    original = path.read_bytes()
    monkeypatch.setattr(
        runner, "run_refill_zones", lambda *a, **kw: runner.KiCadCLIResult(success=True)
    )
    assert not findings(path)
    assert path.read_bytes() == original


def test_project_rule_disables_pour_connection_in_scratch(tmp_path):
    require_native_refill()
    path = fixture_board(tmp_path, "connected")
    path.with_suffix(".kicad_dru").write_text(
        """(version 1)
        (rule "Isolate U1" (condition "A.Type == 'Pad' && A.memberOfFootprint('U1')")
         (constraint zone_connection none))\n"""
    )
    assert not findings(path)


def test_public_check_and_tapeout_acknowledgement(tmp_path, capsys):
    from kicad_tools.cli import check_cmd, readiness_cmd
    from tests.test_kct_readiness_cmd import _cleanup_options

    require_native_refill()
    path = fixture_board(tmp_path, "connected")
    assert (
        check_cmd.main(
            [
                str(path),
                "--drc-only",
                "--only",
                "redundant_copper",
                "--mfr",
                "jlcpcb",
                "--prove-pour-stubs",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert [v["rule_id"] for v in report["violations"]] == [RULE]
    assert report["summary"]["passed"]
    assert DRCChecker.category_for_rule(RULE) == DRCChecker.CATEGORY_ADVISORY
    options = _cleanup_options(tmp_path)
    assert readiness_cmd._gate_warning_review(options, report)[0].status == readiness_cmd.FAILED
    options.ack_trace_quality = {RULE: "Retain the probe branch for bring-up"}
    assert readiness_cmd._gate_warning_review(options, report)[0].status == readiness_cmd.PASSED


def test_same_group_count_does_not_preserve_reassigned_terminals():
    from kicad_tools.validate.copper_contact import CopperContactGraph
    from kicad_tools.validate.pour_stub import _NetState, _preserves_terminals

    def state(terminals):
        return {"GND": _NetState(CopperContactGraph([]), terminals, {})}

    original = state({"A": 0, "B": 0, "C": 1, "D": 1, "stub-via": 0})
    assert _preserves_terminals(original, state({"A": 8, "B": 8, "C": 9, "D": 9}), "stub-via")
    assert not _preserves_terminals(original, state({"A": 8, "B": 9, "C": 8, "D": 9}), "stub-via")


@pytest.mark.parametrize("case, before_connected", [("bridge", True), ("island", False)])
def test_bridge_and_existing_island_have_distinct_terminal_partitions(
    tmp_path, case, before_connected
):
    from kicad_tools.validate.connectivity import ConnectivityValidator

    require_native_refill()
    path = fixture_board(tmp_path, case)
    assert runner.run_refill_zones(path, require_save=True).success
    together = {frozenset({"J1.1", "U1.1"})}
    apart = {frozenset({"J1.1"}), frozenset({"U1.1"})}
    assert set(ConnectivityValidator(path).extract_pad_partition()) == (
        together if before_connected else apart
    )
    pcb = PCB.load(path)
    assert pcb.remove_segments(pcb.segments) == 1
    assert pcb.remove_vias(pcb.vias) == 1
    pcb.save(path)
    assert runner.run_refill_zones(path, require_save=True).success
    assert set(ConnectivityValidator(path).extract_pad_partition()) == apart


def test_shared_proof_identifies_original_copper_and_surviving_terminals(tmp_path):
    from kicad_tools.validate.pour_stub import verified_pour_stubs

    require_native_refill()
    pcb = PCB.load(fixture_board(tmp_path, "connected"))
    ((branch, evidence),) = verified_pour_stubs(pcb)
    assert branch.segment.uuid == uid("track")
    assert branch.via.uuid == uid("via")
    assert evidence.zone_uuid == uid("front")
    assert evidence.terminal_uuids == {uid("J1pad"), uid("U1pad")}


def test_proof_uses_the_in_memory_board_being_checked(tmp_path):
    from kicad_tools.validate.pour_stub import verified_pour_stubs

    require_native_refill()
    path = fixture_board(tmp_path, "unanchored_plane")
    backup = f'(via (at 12 5) (size 0.7) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid {uid("backup")}))'
    path.write_text(path.read_text().replace("(segment ", backup + "\n(segment ", 1))
    original = path.read_bytes()
    pcb = PCB.load(path)
    assert len(verified_pour_stubs(pcb)) == 1
    assert pcb.remove_vias([v for v in pcb.vias if v.uuid == uid("backup")]) == 1
    assert not verified_pour_stubs(pcb)
    assert pcb.path == path
    assert path.read_bytes() == original
