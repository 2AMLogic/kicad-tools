"""``kct route-link`` honours ``.kicad_pro`` netclass clearances (Issue #8650)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli.route_link_cmd import (  # noqa: E402
    LinkSpecError,
    _rules_for_link,
    _rules_from_args,
    _stage_project_files,
)
from kicad_tools.schema.pcb import PCB  # noqa: E402
from tests.test_route_link_hv_8546 import _pad, _parse, _seg, _write  # noqa: E402

BODY = _pad(1, 105, 110, 1, "SIG") + _pad(2, 125, 110, 1, "SIG") + _seg(1, 108, 111, 122, 111, 2)


def _project(path: Path, clearance: float = 0.4) -> None:
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "meta": {"version": 3},
                "net_settings": {
                    "meta": {"version": 3},
                    "classes": [
                        {"name": "Default", "clearance": 0.2},
                        {"name": "HVC", "clearance": clearance},
                    ],
                    "netclass_assignments": {"HV": "HVC"},
                },
            }
        )
    )


def _floor(tmp_path, *extra, clearance=0.4):
    p = _write(tmp_path, BODY)
    _project(p, clearance)
    pcb = PCB.load(str(p))
    a = _parse(str(p), "--link", "SIG R1.1 R2.1", *extra)
    return _rules_for_link(a, pcb, _rules_from_args(a, pcb), a.links[0])


def test_authored_neighbour_floor_applied_by_default(tmp_path):
    assert _floor(tmp_path).clearance_for(2) == pytest.approx(0.4)


def test_explicit_net_clearance_cannot_lower_authored(tmp_path):
    assert _floor(tmp_path, "--net-clearance", "HV=0.25").clearance_for(2) == pytest.approx(0.4)
    assert _floor(tmp_path, "--net-clearance", "HV=1").clearance_for(2) == pytest.approx(1.0)


def test_missing_project_adds_no_floor(tmp_path):
    p = _write(tmp_path, BODY)
    pcb = PCB.load(str(p))
    a = _parse(str(p), "--link", "SIG R1.1 R2.1")
    base = _rules_from_args(a, pcb)
    assert _rules_for_link(a, pcb, base, a.links[0]) is base


def test_malformed_project_is_a_refusal(tmp_path):
    p = _write(tmp_path, BODY)
    p.with_suffix(".kicad_pro").write_text("{not json")
    pcb = PCB.load(str(p))
    a = _parse(str(p), "--link", "SIG R1.1 R2.1")
    with pytest.raises(LinkSpecError):
        _rules_for_link(a, pcb, _rules_from_args(a, pcb), a.links[0])


def _probe_len(tmp_path, capsys, clearance):
    from kicad_tools.cli import main

    p = _write(tmp_path, BODY)
    if clearance:
        _project(p, clearance)
    main(["route-link", "--format", "json", "--probe", str(p), "--link", "SIG R1.1 R2.1",
          "--link-layers", "F.Cu"])  # fmt: skip
    link = json.loads(capsys.readouterr().out)["links"][0]
    return link["length_mm"] if link["routable"] else float("inf")


def test_probe_detours_for_authored_floor(tmp_path, capsys):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    free = _probe_len(tmp_path / "a", capsys, 0)
    bent = _probe_len(tmp_path / "b", capsys, 2.0)
    assert bent > free


@pytest.mark.parametrize("with_clearance_rule", [False, True])
def test_malformed_project_exits_nonzero(tmp_path, capsys, with_clearance_rule):
    from kicad_tools.cli import main

    p = _write(tmp_path, BODY)
    p.with_suffix(".kicad_pro").write_text("{not json")
    if with_clearance_rule:
        p.with_suffix(".kicad_dru").write_text(
            '(version 1)\n(rule "unused" (condition "A.NetName == \'NEVER\'") '
            "(constraint clearance (min 2mm)))"
        )
    rc = main(["route-link", "--format", "json", "--probe", str(p), "--link", "SIG R1.1 R2.1"])
    assert rc == 1


@pytest.mark.parametrize("field", ["classes", "netclass_patterns"])
@pytest.mark.parametrize("with_clearance_rule", [False, True])
def test_malformed_net_settings_array_preserves_json_error(
    tmp_path, capsys, field, with_clearance_rule
):
    from kicad_tools.cli import main

    p = _write(tmp_path, BODY)
    _project(p, 0.4)
    pro_path = p.with_suffix(".kicad_pro")
    data = json.loads(pro_path.read_text())
    data["net_settings"][field] = 1
    pro_path.write_text(json.dumps(data))
    if with_clearance_rule:
        p.with_suffix(".kicad_dru").write_text(
            '(version 1)\n(rule "front pair" (layer "F.Cu") '
            "(condition \"A.NetName == 'HV' && B.NetName == 'SIG'\") "
            "(constraint clearance (min 2mm)))\n"
        )
    rc = main(
        [
            "route-link",
            "--format",
            "json",
            "--probe",
            str(p),
            "--link",
            "SIG R1.1 R2.1",
            "--link-layers",
            "F.Cu",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert f"net_settings.{field}" in payload["error"]
    assert "array" in payload["error"].lower()


def test_gate_stages_project_files_next_to_output(tmp_path):
    src = tmp_path / "in" / "b.kicad_pcb"
    out = tmp_path / "out" / "o.kicad_pcb"
    src.parent.mkdir()
    out.parent.mkdir()
    src.write_text("x")
    src.with_suffix(".kicad_pro").write_text("{}")
    src.with_suffix(".kicad_dru").write_text("(version 1)")
    staged = _stage_project_files(src, out)
    assert {f.suffix for f in staged} == {".kicad_pro", ".kicad_dru"}
    assert all(f.stem == "o" and f.is_file() for f in staged)
    assert _stage_project_files(src, src) == []


def test_custom_pair_clearance_detours_only_on_authored_layer(tmp_path, capsys):
    from shapely.geometry import Point

    from kicad_tools.cli import main

    p = _write(tmp_path, BODY)
    _project(p, 0.2)
    pcb = PCB.load(str(p))
    args = _parse(str(p), "--link", "SIG R1.1 R2.1")
    p.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "front pair" (layer "F.Cu") '
        "(condition \"A.NetName == 'HV' && B.NetName == 'SIG'\") "
        "(constraint clearance (min 2mm)))\n"
    )
    rules = _rules_for_link(args, pcb, _rules_from_args(args, pcb), args.links[0])
    point = Point(0, 0)
    assert rules.clearance_parts("SIG", 2, "F.Cu", point)[0][1] == 2.0
    assert rules.clearance_parts("SIG", 2, "B.Cu", point)[0][1] == 0.2
    assert rules.clearance_parts("SIG", 3, "F.Cu", point)[0][1] == 0.2
    argv = [
        "route-link",
        "--format",
        "json",
        "--probe",
        str(p),
        "--link",
        "SIG R1.1 R2.1",
        "--link-layers",
        "F.Cu",
    ]
    main(argv)
    routed = json.loads(capsys.readouterr().out)["links"][0]
    p.with_suffix(".kicad_dru").unlink()
    main(argv)
    direct = json.loads(capsys.readouterr().out)["links"][0]
    assert routed["routable"] and direct["routable"]
    assert routed["length_mm"] > direct["length_mm"]


def test_attach_zone_keeps_custom_clearance(tmp_path):
    from shapely.geometry import Point

    from kicad_tools.cli.route_cmd import _preload_voltage_map
    from tests.test_route_link_hv_8546 import _two_net_pad

    p = _write(tmp_path, _two_net_pad(1, 105, 110) + _pad(2, 125, 110, 1, "SIG"))
    vm = tmp_path / "map.json"
    vm.write_text(json.dumps({"SIG": 0, "HV": 300}))
    p.with_suffix(".kicad_dru").write_text(
        "(version 1)\n(rule \"pair\" (condition \"A.NetName == 'SIG' && B.NetName == 'HV'\") "
        "(constraint clearance (min 1mm)))\n"
    )
    pcb = PCB.load(str(p))
    args = _parse(str(p), "--voltage-map", str(vm), "--link", "SIG Q1.1 R2.1")
    assert _preload_voltage_map(args) == 0
    rules = _rules_for_link(args, pcb, _rules_from_args(args, pcb), args.links[0])
    zone = rules.attach_zones[0]
    inside = Point((zone.min_x + zone.max_x) / 2, (zone.min_y + zone.max_y) / 2)
    assert rules.clearance_for(2) > 1.0
    assert rules.clearance_parts("SIG", 2, "F.Cu", inside)[0][1] == 1.0


def test_invalid_dru_refuses_before_routing(tmp_path):
    p = _write(tmp_path, BODY)
    p.with_suffix(".kicad_dru").write_text("(version 1) (rule")
    args = _parse(str(p), "--link", "SIG R1.1 R2.1")
    with pytest.raises(LinkSpecError):
        _rules_from_args(args, PCB.load(str(p)))


def test_unreadable_dru_refuses_before_routing(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import main

    p = _write(tmp_path, BODY)
    _project(p, 0.2)
    dru = p.with_suffix(".kicad_dru")
    dru.write_text(
        '(version 1)\n(rule "front pair" (layer "F.Cu") '
        "(condition \"A.NetName == 'HV' && B.NetName == 'SIG'\") "
        "(constraint clearance (min 2mm)))\n"
    )
    orig_read_text = Path.read_text

    def fail_on_dru(self, *args, **kwargs):
        if self == dru:
            raise PermissionError(13, "Permission denied", str(self))
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_on_dru)
    rc = main(
        [
            "route-link",
            "--format",
            "json",
            "--probe",
            str(p),
            "--link",
            "SIG R1.1 R2.1",
            "--link-layers",
            "F.Cu",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "links" not in payload
    assert dru.name in payload["error"]
    assert "cannot be read" in payload["error"]


def _pair_rules(tmp_path, dru, clearance=0.2):
    p = _write(tmp_path, BODY)
    _project(p, clearance)
    p.with_suffix(".kicad_dru").write_text("(version 1)\n" + dru)
    pcb = PCB.load(str(p))
    args = _parse(str(p), "--link", "SIG R1.1 R2.1")
    return _rules_for_link(args, pcb, _rules_from_args(args, pcb), args.links[0])


def test_pair_exception_below_netclass_wins(tmp_path):
    from shapely.geometry import Point

    rules = _pair_rules(
        tmp_path,
        '(rule "p" (layer "F.Cu") (condition "A.NetName == \'SIG\' && B.NetName == \'HV\'") '
        "(constraint clearance (min 0.3mm)))\n",
        clearance=2.0,
    )
    assert rules.clearance_parts("SIG", 2, "F.Cu", Point(0, 0))[0][1] == pytest.approx(0.3)


def test_via_rule_applies_to_via_mask_only(tmp_path):
    from shapely.geometry import Point

    rules = _pair_rules(
        tmp_path,
        "(rule \"v\" (condition \"A.Type == 'Via' && A.NetName == 'SIG' && B.NetName == 'HV'\") "
        "(constraint clearance (min 6mm)))\n",
    )
    pt = Point(0, 0)
    assert rules.clearance_parts("SIG", 2, "F.Cu", pt, None, "Track")[0][1] == pytest.approx(0.2)
    assert rules.clearance_parts("SIG", 2, "F.Cu", pt, None, "Via")[0][1] == pytest.approx(6.0)


def test_pad_reference_reaches_resolver(tmp_path):
    from shapely.geometry import Point

    from kicad_tools.router.link_router import _Item

    rules = _pair_rules(
        tmp_path,
        "(rule \"r\" (condition \"A.NetName == 'SIG' && B.Type == 'Pad' && B.Reference == 'R9'\") "
        "(constraint clearance (min 6mm)))\n",
    )
    pt = Point(0, 0)
    r9 = _Item(2, {"F.Cu": pt}, True, 0.0, "R9", "")
    r3 = _Item(2, {"F.Cu": pt}, True, 0.0, "R3", "")
    assert rules.clearance_parts("SIG", 2, "F.Cu", pt, r9)[0][1] == pytest.approx(6.0)
    assert rules.clearance_parts("SIG", 2, "F.Cu", pt, r3)[0][1] == pytest.approx(0.2)


def test_copper_beyond_window_padding_is_still_avoided(tmp_path, capsys):
    from kicad_tools.cli import main

    body = (
        _pad(1, 105, 110, 1, "SIG") + _pad(2, 125, 110, 1, "SIG") + _seg(1, 111, 116, 119, 116, 2)
    )
    p = _write(tmp_path, body)
    _project(p, 0.2)
    argv = ["route-link", "--format", "json", "--probe", str(p), "--link", "SIG R1.1 R2.1"]
    main(argv)
    plain = json.loads(capsys.readouterr().out)["links"][0]
    p.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "far" (layer "F.Cu") '
        "(condition \"A.NetName == 'SIG' && B.NetName == 'HV'\") "
        "(constraint clearance (min 7mm)))\n"
    )
    main(argv)
    ruled = json.loads(capsys.readouterr().out)["links"][0]
    assert plain["routable"]
    assert not ruled["routable"] or ruled["length_mm"] > plain["length_mm"]


@pytest.mark.parametrize(
    "predicate", ["B.memberOfFootprint('R9')", "B.Type == 'Pad' && B.Layer == 'B.Cu'"]
)
def test_probe_resolves_foreign_footprint_properties(tmp_path, capsys, predicate):
    from kicad_tools.cli import main

    p = _write(tmp_path, BODY + _pad(3, 110, 111, 2, "HV") + _pad(9, 128, 119, 2, "HV"))
    _project(p, 0.2)
    argv = [
        "route-link",
        "--probe",
        "--format",
        "json",
        str(p),
        "--link",
        "SIG R1.1 R2.1",
        "--link-layers",
        "F.Cu",
    ]
    assert main(argv) == 0
    direct = json.loads(capsys.readouterr().out)["links"][0]
    p.with_suffix(".kicad_dru").write_text(
        f"(version 1)\n(rule \"select pad\" (condition \"A.NetName == 'SIG' && B.NetName == 'HV' && {predicate}\") "
        "(constraint clearance (min 6mm)))"
    )
    assert main(argv) == 0
    routed = json.loads(capsys.readouterr().out)["links"][0]
    assert routed["routable"] and direct["routable"]
    assert routed["length_mm"] == direct["length_mm"]

    # A through-hole R9 on a back-side footprint has copper on F.Cu and
    # matches either predicate. Put it near the terminal to force a blocker.
    blocker_pad = (
        '(footprint "R" (layer "B.Cu") (at 106 111) '
        '(fp_text reference "R9" (at 0 -1.5) (layer "B.SilkS")) '
        '(pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) '
        '(layers "*.Cu" "*.Mask") (net 2 "HV")))'
    )
    _write(tmp_path, BODY + blocker_pad)
    assert main(argv) == 1
    blocked = json.loads(capsys.readouterr().out)["links"][0]
    assert not blocked["routable"]
    assert blocked["blocker"]["kind"] == "pad"
    assert blocked["blocker"]["required_mm"] == pytest.approx(6.1)
