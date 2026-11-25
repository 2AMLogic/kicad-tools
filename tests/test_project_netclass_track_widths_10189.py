"""Issue #10189: ``kct route`` and ``kct optimize-traces`` honour project netclass track widths.

Tests verify:
- Editing one class's ``track_width`` in ``.kicad_pro`` and re-running ``kct route``
  updates only that class's copper (both widening and narrowing) while leaving
  other classes' copper unchanged.
- The same before/after contract holds for ``kct optimize-traces`` (including
  already-straight single-segment runs as well as multi-segment paths).
- Edge cases: ``netclass_patterns`` vs ``netclass_assignments``, ``Default``
  class (board-wide default keeps winning per ``authored_netclass_widths``),
  explicit ``--trace-width`` override on ``kct route``, locked tracks, and
  collision-safe widening in ``optimize-traces``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import optimize_cmd, route_cmd
from kicad_tools.core.project_clearance import ProjectClearanceError
from kicad_tools.core.project_file import default_netclass_definition
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.optimizer.pcb import parse_segments
from kicad_tools.router.rules import DesignRules


def _two_pad_pair_footprints(
    pairs: list[tuple[str, str, int, str, float]],
) -> str:
    """Build horizontal SMD pad pairs well separated in Y inside a 25x25 board."""
    blocks: list[str] = []
    for ref_a, ref_b, net_id, net_name, y in pairs:
        for ref, x in ((ref_a, 4.0), (ref_b, 20.0)):
            blocks.append(
                f"""  (footprint "Resistor_SMD:R_0805"
    (layer "F.Cu")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net {net_id} "{net_name}"))
  )"""
            )
    return "\n".join(blocks)


def _write_unrouted_board(
    tmp_path: Path,
    nets: list[tuple[str, float]],
    *,
    name: str = "board",
) -> Path:
    """Write a clean 2-layer PCB with one horizontal two-pad net per row."""
    net_decls = "\n  ".join(
        ['(net 0 "")', *[f'(net {idx} "{net_name}")' for idx, (net_name, _) in enumerate(nets, 1)]]
    )
    pairs = [
        (f"R{2 * idx - 1}", f"R{2 * idx}", idx, net_name, y)
        for idx, (net_name, y) in enumerate(nets, 1)
    ]
    footprints = _two_pad_pair_footprints(pairs)
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(
        f"""(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  {net_decls}
  (gr_rect (start 0 0) (end 25 25) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{footprints}
)
""",
        encoding="utf-8",
    )
    return pcb


def _base_project(
    *,
    classes: list[dict] | None = None,
    assignments: dict | None = None,
    patterns: list[dict] | None = None,
    default_width: float = 0.25,
) -> dict:
    default_cls = default_netclass_definition()
    default_cls["track_width"] = default_width
    default_cls["clearance"] = 0.15
    return {
        "board": {"design_settings": {}},
        "meta": {"version": 1},
        "net_settings": {
            "meta": {"version": 3},
            "classes": [default_cls, *(classes or [])],
            "net_colors": None,
            "netclass_assignments": assignments,
            "netclass_patterns": patterns or [],
        },
    }


def _widths_by_net(pcb_path: Path) -> dict[str, set[float]]:
    segs_by_net = parse_segments(pcb_path.read_text(encoding="utf-8"))
    return {net: {round(seg.width, 4) for seg in segs} for net, segs in segs_by_net.items()}


def _geometry_signature(pcb_path: Path, net: str) -> list[tuple]:
    segs = parse_segments(pcb_path.read_text(encoding="utf-8")).get(net, [])
    return sorted(
        (
            seg.layer,
            round(seg.width, 4),
            tuple(
                sorted(((round(seg.x1, 4), round(seg.y1, 4)), (round(seg.x2, 4), round(seg.y2, 4))))
            ),
        )
        for seg in segs
    )


def _route_board(pcb_path: Path, extra_args: list[str] | None = None) -> None:
    rc = route_cmd.main(
        [
            str(pcb_path),
            "-o",
            str(pcb_path),
            "--layers",
            "2",
            "--grid",
            "0.1",
            "--no-auto-layers",
            "--no-auto-pour",
            "--no-cache",
            "--quiet",
            *(extra_args or []),
        ]
    )
    assert rc == 0, f"kct route failed with exit code {rc}"


class TestRouteHonoursProjectNetclassWidths:
    """AC1: ``kct route`` honours edited project netclass ``track_width`` values."""

    def test_reroute_after_editing_one_class_track_width_changes_only_that_class(
        self, tmp_path: Path
    ) -> None:
        pcb = _write_unrouted_board(tmp_path, [("+5V", 6.0), ("SDA", 18.0)])
        pro = pcb.with_suffix(".kicad_pro")
        pro.write_text(json.dumps(_base_project(), indent=2), encoding="utf-8")

        # Initial route: router classifies +5V -> Power (0.5mm), SDA -> Digital (0.2mm)
        # and writes both classes into board.kicad_pro (#10133).
        _route_board(pcb)
        assert _widths_by_net(pcb) == {"+5V": {0.5}, "SDA": {0.2}}
        power_before = _geometry_signature(pcb, "+5V")

        # Edit ONLY Digital's track_width (widen 0.2 -> 0.35) in .kicad_pro and re-route.
        project = json.loads(pro.read_text(encoding="utf-8"))
        for cls in project["net_settings"]["classes"]:
            if cls["name"] == "Digital":
                cls["track_width"] = 0.35
        pro.write_text(json.dumps(project, indent=2), encoding="utf-8")

        _route_board(pcb)
        assert _widths_by_net(pcb) == {"+5V": {0.5}, "SDA": {0.35}}
        assert _geometry_signature(pcb, "+5V") == power_before
        sda_after_widen = _geometry_signature(pcb, "SDA")

        # Edit ONLY Power's track_width (narrow 0.5 -> 0.30, below router default!) and re-route.
        project = json.loads(pro.read_text(encoding="utf-8"))
        for cls in project["net_settings"]["classes"]:
            if cls["name"] == "Power":
                cls["track_width"] = 0.30
        pro.write_text(json.dumps(project, indent=2), encoding="utf-8")

        _route_board(pcb)
        assert _widths_by_net(pcb) == {"+5V": {0.3}, "SDA": {0.35}}
        assert _geometry_signature(pcb, "SDA") == sda_after_widen

    def test_route_pattern_vs_assignment_and_default_class_keeps_board_wide_default(
        self, tmp_path: Path
    ) -> None:
        pcb = _write_unrouted_board(
            tmp_path,
            [("ASSIGN_NET", 5.0), ("PAT_1", 12.0), ("PLAIN_NET", 19.0)],
        )
        pro = pcb.with_suffix(".kicad_pro")
        pro.write_text(
            json.dumps(
                _base_project(
                    default_width=0.45,  # Default.track_width ignored per authored_netclass_widths
                    classes=[
                        {"name": "ClassA", "track_width": 0.40, "clearance": 0.15},
                        {"name": "ClassB", "track_width": 0.28, "clearance": 0.15},
                    ],
                    assignments={"ASSIGN_NET": "ClassA"},
                    patterns=[{"netclass": "ClassB", "pattern": "PAT_*"}],
                ),
                indent=2,
            ),
            encoding="utf-8",
        )

        _route_board(pcb, extra_args=["--skip-drc"])
        assert _widths_by_net(pcb) == {
            "ASSIGN_NET": {0.4},
            "PAT_1": {0.28},
            "PLAIN_NET": {0.2},
        }

    def test_explicit_cli_trace_width_overrides_project_class_widths(self, tmp_path: Path) -> None:
        pcb = _write_unrouted_board(tmp_path, [("ASSIGN_NET", 6.0), ("PAT_1", 18.0), ("+5V", 12.0)])
        pro = pcb.with_suffix(".kicad_pro")
        pro.write_text(
            json.dumps(
                _base_project(
                    classes=[
                        {"name": "ClassA", "track_width": 0.40, "clearance": 0.15},
                        {"name": "ClassB", "track_width": 0.28, "clearance": 0.15},
                    ],
                    assignments={"ASSIGN_NET": "ClassA"},
                    patterns=[{"netclass": "ClassB", "pattern": "PAT_*"}],
                ),
                indent=2,
            ),
            encoding="utf-8",
        )

        _route_board(pcb, extra_args=["--trace-width", "0.18", "--skip-drc"])
        assert _widths_by_net(pcb) == {
            "ASSIGN_NET": {0.18},
            "PAT_1": {0.18},
            "+5V": {0.5},
        }

    def test_explicit_width_preserves_connected_name_classified_copper(
        self, tmp_path: Path
    ) -> None:
        pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
        pcb.write_text(pcb.read_text().replace("NET_A", "+5V"))
        project.write_text(json.dumps(_base_project()))
        before = _geometry_signature(pcb, "+5V")
        assert len(before) == 2
        _route_board(pcb, ["--trace-width", "0.18", "--skip-drc"])
        assert _geometry_signature(pcb, "+5V") == before

    @pytest.mark.parametrize("command", ["route", "optimize-traces"])
    @pytest.mark.parametrize("lock", ["via", "group-track", "nested-group-via"])
    def test_reroute_preserves_locked_copper_on_stale_net(
        self, tmp_path: Path, lock: str, command: str
    ) -> None:
        from kicad_tools.core.pcb_item_locks import BoardItemLocks
        from kicad_tools.schema.pcb import PCB

        pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
        track_id = "ff956807-5f30-48b2-959c-da35080ca721"
        via_id = "eb4a7d68-0b42-40a5-aa3b-af4eb43c3897"
        inner_id = "fbef539f-bba5-45d6-9d82-95f0c876e0a0"
        group_id = "ae05d1f5-c27e-41a4-9461-faca98a1347d"
        text = (
            pcb.read_text()
            .replace('(uuid "a1")', f'(uuid "{track_id}")')
            .rstrip()
            .removesuffix(")")
        )
        lock_flag = "(locked yes)" if lock == "via" else ""
        text += (
            f" (via {lock_flag} (at 12 6) (size 0.60) (drill 0.30)"
            f' (layers "F.Cu" "B.Cu") (net 1) (uuid "{via_id}"))'
        )
        if lock != "via":
            member = track_id
            if lock == "nested-group-via":
                text += f' (group "inner" (uuid "{inner_id}") (members "{via_id}"))'
                member = inner_id
            text += f' (group "protected" (uuid "{group_id}") (locked yes) (members "{member}"))'
        pcb.write_text(text + ")")
        settings = json.loads(project.read_text())
        for cls in settings["net_settings"]["classes"]:
            if cls["name"] in {"ClassA", "ClassB"}:
                cls["track_width"] = 0.37
        project.write_text(json.dumps(settings))
        original = _geometry_signature(pcb, "NET_A")

        if command == "route":
            _route_board(pcb, ["--skip-drc"])
        else:
            assert optimize_cmd.main([str(pcb), "--format", "json"]) == 0

        saved = PCB.load(pcb)
        assert _geometry_signature(pcb, "NET_A") == original
        via = next(v for v in saved.vias if v.uuid == via_id)
        assert (via.position, via.size, via.drill) == ((12, 6), 0.6, 0.3)
        kind, member_id = ("segment", track_id) if lock == "group-track" else ("via", via_id)
        locks = BoardItemLocks(saved._sexp)
        assert member_id in {
            item["uuid"].get_string(0)
            for item in saved._sexp.find_children(kind)
            if locks.is_locked(item)
        }
        assert _widths_by_net(pcb)["NET_B"] == {0.37}

    @pytest.mark.parametrize("sidecar_key", ["NET_A", "/analog/NET_A"])
    def test_reroute_resolves_explicit_width_for_connected_sheet_net(
        self, tmp_path: Path, sidecar_key: str
    ) -> None:
        pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
        for file in (pcb, project):
            file.write_text(file.read_text().replace('"NET_A"', '"/analog/NET_A"'))
        sidecar = tmp_path / "widths.json"
        sidecar.write_text(json.dumps({sidecar_key: {"name": "ClassA", "trace_width": 0.37}}))
        other = _geometry_signature(pcb, "NET_B")

        _route_board(pcb, ["--net-class-map", str(sidecar), "--skip-drc"])

        assert _widths_by_net(pcb)["/analog/NET_A"] == {0.37}
        assert _geometry_signature(pcb, "NET_B") == other

    @pytest.mark.parametrize("both_connected", [True, False])
    def test_ambiguous_sidecar_width_does_not_rip_up_connected_nets(
        self, tmp_path: Path, both_connected: bool
    ) -> None:
        pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
        for file in (pcb, project):
            file.write_text(
                file.read_text()
                .replace('"NET_A"', '"/left/LINE"')
                .replace('"NET_B"', '"/right/LINE"')
            )
        if not both_connected:
            pcb.write_text(
                "\n".join(
                    line for line in pcb.read_text().splitlines() if '(uuid "b1")' not in line
                )
            )
        sidecar = tmp_path / "widths.json"
        sidecar.write_text(json.dumps({"LINE": {"name": "Override", "trace_width": 0.37}}))
        left = _geometry_signature(pcb, "/left/LINE")
        right = _geometry_signature(pcb, "/right/LINE")

        _route_board(pcb, ["--net-class-map", str(sidecar), "--skip-drc"])

        assert _geometry_signature(pcb, "/left/LINE") == left
        if both_connected:
            assert _geometry_signature(pcb, "/right/LINE") == right
        else:
            assert _widths_by_net(pcb)["/right/LINE"] == {0.37}

    def test_project_width_below_manufacturer_floor_warns(self, tmp_path: Path, capsys) -> None:
        pcb = _write_unrouted_board(tmp_path, [("BUS", 10.0)])
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps(
                _base_project(
                    classes=[{"name": "Thin", "track_width": 0.09, "clearance": 0.15}],
                    assignments={"BUS": "Thin"},
                )
            )
        )
        _route_board(pcb, ["--manufacturer", "jlcpcb", "--skip-drc"])
        err = capsys.readouterr().err
        assert "Project net class Thin uses 0.09mm" in err
        assert "dimension_trace_width" in err
        assert _widths_by_net(pcb) == {"BUS": {0.09}}

    def test_load_pcb_for_routing_invalid_project_track_width_raises(self, tmp_path: Path) -> None:
        pcb = _write_unrouted_board(tmp_path, [("SIG", 10.0)])
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps(
                _base_project(
                    classes=[{"name": "BadWidth", "track_width": -0.1, "clearance": 0.15}],
                    assignments={"SIG": "BadWidth"},
                )
            ),
            encoding="utf-8",
        )
        with pytest.raises(ProjectClearanceError, match="Invalid track_width"):
            load_pcb_for_routing(
                str(pcb),
                rules=DesignRules(grid_resolution=0.1),
                force_python=True,
                validate_drc=False,
            )


def _write_routed_two_class_board_for_optimize(tmp_path: Path) -> tuple[Path, Path]:
    """Write a two-class routed board: NET_A (collinear pair, 0.25mm) and NET_B (single straight, 0.40mm)."""
    pcb = tmp_path / "opt.kicad_pcb"
    footprints = _two_pad_pair_footprints(
        [
            ("R1", "R2", 1, "NET_A", 6.0),
            ("R3", "R4", 2, "NET_B", 18.0),
        ]
    )
    pcb.write_text(
        f"""(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET_A")
  (net 2 "NET_B")
  (gr_rect (start 0 0) (end 25 25) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{footprints}
  (segment (start 4 6) (end 12 6) (width 0.25) (layer "F.Cu") (net 1) (uuid "a1"))
  (segment (start 12 6) (end 20 6) (width 0.25) (layer "F.Cu") (net 1) (uuid "a2"))
  (segment (start 4 18) (end 20 18) (width 0.4) (layer "F.Cu") (net 2) (uuid "b1"))
)
""",
        encoding="utf-8",
    )
    pro = pcb.with_suffix(".kicad_pro")
    pro.write_text(
        json.dumps(
            _base_project(
                classes=[
                    {"name": "ClassA", "track_width": 0.25, "clearance": 0.15},
                    {"name": "ClassB", "track_width": 0.40, "clearance": 0.15},
                ],
                assignments={"NET_A": "ClassA", "NET_B": "ClassB"},
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    return pcb, pro


class TestOptimizeTracesHonoursProjectNetclassWidths:
    """AC2: ``kct optimize-traces`` honours edited project netclass ``track_width`` values."""

    def test_optimize_traces_after_editing_one_class_track_width_changes_only_that_class(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pcb, pro = _write_routed_two_class_board_for_optimize(tmp_path)

        # Initial optimize-traces run: NET_A merges at 0.25mm, NET_B stays 0.40mm.
        assert optimize_cmd.main([str(pcb), "--format", "json"]) == 0
        first_doc = json.loads(capsys.readouterr().out)
        assert first_doc["stats"]["nets_touched"] == ["NET_A"]
        assert _widths_by_net(pcb) == {"NET_A": {0.25}, "NET_B": {0.4}}
        net_b_before = _geometry_signature(pcb, "NET_B")

        # Edit ONLY ClassA's track_width (widen 0.25 -> 0.38) in .kicad_pro and re-run.
        # Note: NET_A is now a single straight segment with no bends left to simplify,
        # yet its copper width must update to 0.38mm while NET_B stays untouched.
        project = json.loads(pro.read_text(encoding="utf-8"))
        for cls in project["net_settings"]["classes"]:
            if cls["name"] == "ClassA":
                cls["track_width"] = 0.38
        pro.write_text(json.dumps(project, indent=2), encoding="utf-8")

        assert optimize_cmd.main([str(pcb), "--format", "json"]) == 0
        second_doc = json.loads(capsys.readouterr().out)
        assert second_doc["stats"]["nets_touched"] == ["NET_A"]
        assert _widths_by_net(pcb) == {"NET_A": {0.38}, "NET_B": {0.4}}
        assert _geometry_signature(pcb, "NET_B") == net_b_before
        net_a_after = _geometry_signature(pcb, "NET_A")

        # Edit ONLY ClassB's track_width (narrow 0.40 -> 0.22) in .kicad_pro and re-run.
        project = json.loads(pro.read_text(encoding="utf-8"))
        for cls in project["net_settings"]["classes"]:
            if cls["name"] == "ClassB":
                cls["track_width"] = 0.22
        pro.write_text(json.dumps(project, indent=2), encoding="utf-8")

        assert optimize_cmd.main([str(pcb), "--format", "json"]) == 0
        third_doc = json.loads(capsys.readouterr().out)
        assert third_doc["stats"]["nets_touched"] == ["NET_B"]
        assert _widths_by_net(pcb) == {"NET_A": {0.38}, "NET_B": {0.22}}
        assert _geometry_signature(pcb, "NET_A") == net_a_after

    def test_optimize_traces_pattern_vs_assignment_and_default_class_untouched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pcb = tmp_path / "three.kicad_pcb"
        footprints = _two_pad_pair_footprints(
            [
                ("R1", "R2", 1, "ASSIGN_NET", 5.0),
                ("R3", "R4", 2, "PAT_1", 12.0),
                ("R5", "R6", 3, "PLAIN_NET", 19.0),
            ]
        )
        pcb.write_text(
            f"""(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "ASSIGN_NET")
  (net 2 "PAT_1")
  (net 3 "PLAIN_NET")
  (gr_rect (start 0 0) (end 25 25) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{footprints}
  (segment (start 4 5) (end 20 5) (width 0.2) (layer "F.Cu") (net 1) (uuid "s1"))
  (segment (start 4 12) (end 20 12) (width 0.2) (layer "F.Cu") (net 2) (uuid "s2"))
  (segment (start 4 19) (end 20 19) (width 0.2) (layer "F.Cu") (net 3) (uuid "s3"))
)
""",
            encoding="utf-8",
        )
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps(
                _base_project(
                    default_width=0.50,  # Default.track_width excluded per authored_netclass_widths
                    classes=[
                        {"name": "ClassA", "track_width": 0.36, "clearance": 0.15},
                        {"name": "ClassB", "track_width": 0.30, "clearance": 0.15},
                    ],
                    assignments={"ASSIGN_NET": "ClassA"},
                    patterns=[{"netclass": "ClassB", "pattern": "PAT_*"}],
                ),
                indent=2,
            ),
            encoding="utf-8",
        )

        assert optimize_cmd.main([str(pcb), "--format", "json"]) == 0
        doc = json.loads(capsys.readouterr().out)
        assert sorted(doc["stats"]["nets_touched"]) == ["ASSIGN_NET", "PAT_1"]
        assert _widths_by_net(pcb) == {
            "ASSIGN_NET": {0.36},
            "PAT_1": {0.3},
            "PLAIN_NET": {0.2},
        }

    def test_optimize_traces_respects_locked_tracks_and_collision_clearance(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pcb = tmp_path / "guarded.kicad_pcb"
        # LOCKED_NET has (locked yes) -> stays at 0.20mm despite Wide=0.50mm.
        # TIGHT_NET sits 0.26mm edge-to-edge from an OBSTACLE pad at (12, 12.66)
        # (centre y=12 vs pad centre y=12.66 with pad size 0.6 -> half 0.3;
        # at 0.20mm width: gap = 0.66 - 0.3 - 0.10 = 0.26mm >= 0.15mm clear;
        # at 0.80mm width: gap = 0.66 - 0.3 - 0.40 = -0.04mm overlap!).
        pcb.write_text(
            """(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "LOCKED_NET")
  (net 2 "TIGHT_NET")
  (net 3 "OBSTACLE")
  (gr_rect (start 0 0) (end 25 25) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 4 5)
    (fp_text reference "R1" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net 1 "LOCKED_NET")))
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 20 5)
    (fp_text reference "R2" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net 1 "LOCKED_NET")))
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 4 12)
    (fp_text reference "R3" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net 2 "TIGHT_NET")))
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 20 12)
    (fp_text reference "R4" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net 2 "TIGHT_NET")))
  (footprint "Resistor_SMD:R_0805" (layer "F.Cu") (at 12 12.66)
    (fp_text reference "R5" (at 0 -2) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "OBSTACLE")))
  (segment (start 4 5) (end 20 5) (width 0.2) (locked yes) (layer "F.Cu") (net 1) (uuid "lk1"))
  (segment (start 4 12) (end 20 12) (width 0.2) (layer "F.Cu") (net 2) (uuid "tg1"))
)
""",
            encoding="utf-8",
        )
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps(
                _base_project(
                    classes=[{"name": "Huge", "track_width": 0.80, "clearance": 0.15}],
                    assignments={"LOCKED_NET": "Huge", "TIGHT_NET": "Huge"},
                ),
                indent=2,
            ),
            encoding="utf-8",
        )

        # Disable detour shortening so TIGHT_NET tests in-place widening collision check directly.
        assert optimize_cmd.main([str(pcb), "--no-detour", "--format", "json"]) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["locked_nets_skipped"] == ["LOCKED_NET"]
        assert _widths_by_net(pcb) == {
            "LOCKED_NET": {0.2},
            "TIGHT_NET": {0.2},
        }

    def test_optimize_traces_honours_explicit_project_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pcb, default_pro = _write_routed_two_class_board_for_optimize(tmp_path)
        alt_dir = tmp_path / "alt_project"
        alt_dir.mkdir()
        alt_pro = alt_dir / "custom.kicad_pro"
        project = json.loads(default_pro.read_text(encoding="utf-8"))
        for cls in project["net_settings"]["classes"]:
            if cls["name"] == "ClassB":
                cls["track_width"] = 0.32
        alt_pro.write_text(json.dumps(project, indent=2), encoding="utf-8")

        assert optimize_cmd.main([str(pcb), "--project", str(alt_pro), "--format", "json"]) == 0
        doc = json.loads(capsys.readouterr().out)
        assert sorted(doc["stats"]["nets_touched"]) == ["NET_A", "NET_B"]
        assert _widths_by_net(pcb) == {"NET_A": {0.25}, "NET_B": {0.32}}


def test_default_auto_layers_reroute_updates_edited_class_only(tmp_path: Path) -> None:
    """Two default re-routes retain the authored class names, widths and assignments."""
    pcb = _write_unrouted_board(tmp_path, [("VIN", 6.0), ("VOUT", 18.0)])
    pro = pcb.with_suffix(".kicad_pro")
    pro.write_text(json.dumps(_base_project(), indent=2), encoding="utf-8")

    # Both names classify as Power without the user's original map.
    user_map = tmp_path / "classes.json"
    user_map.write_text(
        json.dumps(
            {
                "VIN": {"name": "Power", "trace_width": 0.5, "clearance": 0.2},
                "VOUT": {"name": "Sense", "trace_width": 0.25, "clearance": 0.2},
            }
        )
    )
    args = [str(pcb), "-o", str(pcb), "--no-auto-pour", "--no-cache", "--quiet"]
    assert route_cmd.main([*args, "--net-class-map", str(user_map)]) == 0
    assert _widths_by_net(pcb) == {"VIN": {0.5}, "VOUT": {0.25}}
    sense_before = _geometry_signature(pcb, "VOUT")

    project = json.loads(pro.read_text(encoding="utf-8"))
    for cls in project["net_settings"]["classes"]:
        if cls["name"] == "Power":
            cls["track_width"] = 0.34
    pro.write_text(json.dumps(project, indent=2), encoding="utf-8")

    for _ in range(2):
        assert route_cmd.main(args) == 0
        assert _widths_by_net(pcb) == {"VIN": {0.34}, "VOUT": {0.25}}
        assert _geometry_signature(pcb, "VOUT") == sense_before
        settings = json.loads(pro.read_text())["net_settings"]
        assert settings["netclass_assignments"] == {"VIN": "Power", "VOUT": "Sense"}
        widths = {c["name"]: c["track_width"] for c in settings["classes"]}
        assert widths["Power"] == 0.34 and widths["Sense"] == 0.25
        sidecar = json.loads(pcb.with_suffix(".net_class_map.json").read_text())
        assert sidecar["VOUT"]["name"] == "Sense"


def _arc_fixture_ids(text: str) -> str:
    for short in ("a1", "a2", "b1"):
        text = text.replace(f'(uuid "{short}")', f'(uuid "00000000-0000-4000-8000-{short:0>12}")')
    return text


def _arc_geometry(arc):
    return arc.net_name, arc.uuid, arc.start, arc.mid, arc.end, arc.width, arc.layer


@pytest.mark.parametrize("command", ["route", "optimize-traces"])
@pytest.mark.parametrize("dialect", ["numeric", "name-only", "name-with-no-net"])
def test_class_edit_on_arcs_preserves_other_copper(tmp_path, command, dialect):
    from kicad_tools.schema.pcb import PCB

    pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
    text = _arc_fixture_ids(pcb.read_text())
    text = text.replace("(segment (start 4 6)", "(arc (start 4 6) (mid 8 6.2)")
    text = text.replace("(segment (start 4 18)", "(arc (start 4 18) (mid 12 18.2)")
    if dialect != "numeric":
        if dialect == "name-only":
            text = text.replace('  (net 0 "")\n', "")
        for number, name in [(1, "NET_A"), (2, "NET_B")]:
            text = text.replace(f'  (net {number} "{name}")\n', "").replace(
                f"(net {number})", f'(net "{name}")'
            )
            text = text.replace(f'(net {number} "{name}")', f'(net "{name}")')
    pcb.write_text(text)
    before = {arc.net_name: arc for arc in PCB.load(pcb).arcs}
    settings = json.loads(project.read_text())
    next(c for c in settings["net_settings"]["classes"] if c["name"] == "ClassA")["track_width"] = (
        0.37
    )
    project.write_text(json.dumps(settings))
    if command == "route":
        _route_board(pcb, ["--skip-drc"])
    else:
        assert optimize_cmd.main([str(pcb), "--format", "json"]) == 0
    board = PCB.load(pcb)
    assert {a.width for a in [*board.arcs, *board.segments] if a.net_name == "NET_A"} == {0.37}
    assert [_arc_geometry(a) for a in board.arcs if a.net_name == "NET_B"] == [
        _arc_geometry(before["NET_B"])
    ]
    if command == "optimize-traces":
        (arc,) = (a for a in board.arcs if a.net_name == "NET_A")
        assert (arc.uuid, arc.start, arc.mid, arc.end) == (
            before["NET_A"].uuid,
            before["NET_A"].start,
            before["NET_A"].mid,
            before["NET_A"].end,
        )


@pytest.mark.parametrize("obstacle", ["pad", "arc"])
def test_curved_width_uses_swept_copper_and_filter(tmp_path, obstacle):
    from kicad_tools.schema.pcb import PCB

    pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
    text = _arc_fixture_ids(pcb.read_text()).replace(
        "(segment (start 4 6)", "(arc (start 4 6) (mid 8 8)"
    )
    text = text.replace("(segment (start 4 18)", "(arc (start 4 18) (mid 12 18.2)")
    if obstacle == "pad":
        extra = (
            '(footprint "test" (layer "F.Cu") (at 8 8.45) (fp_text reference "U5" (at 0 0)'
            ' (layer "F.SilkS")) (pad "1" smd rect (at 0 0) (size 0.2 0.2)'
            ' (layers "F.Cu") (net 2 "NET_B")))'
        )
    else:
        # At the apex the gap is legal at .25 mm and too narrow at .8 mm.
        extra = (
            "(arc (start 7 8.65) (mid 8 8.45) (end 9 8.65) (width 0.2)"
            ' (layer "F.Cu") (net 2) (uuid "00000000-0000-4000-8000-0000000000b2"))'
        )
    pcb.write_text(text.rstrip().removesuffix(")") + extra + ")")
    settings = json.loads(project.read_text())
    for cls in settings["net_settings"]["classes"]:
        if cls["name"] in {"ClassA", "ClassB"}:
            cls["track_width"] = 0.8
    project.write_text(json.dumps(settings))
    before = {_arc_geometry(arc) for arc in PCB.load(pcb).arcs}
    assert optimize_cmd.main([str(pcb), "--net", "NET_A", "--format", "json"]) == 0
    after = {_arc_geometry(arc) for arc in PCB.load(pcb).arcs}
    assert after == before


def test_arc_only_width_edit_obeys_dry_run_and_output_verifier(tmp_path, capsys):
    from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer
    from kicad_tools.router.optimizer.arc_widths import ArcWidthEdit

    pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
    board = _arc_fixture_ids(pcb.read_text()).replace(
        "(segment (start 4 18)", "(arc (start 4 18) (mid 12 18.2)"
    )
    pcb.write_text(board)
    settings = json.loads(project.read_text())
    next(c for c in settings["net_settings"]["classes"] if c["name"] == "ClassB")["track_width"] = (
        0.55
    )
    project.write_text(json.dumps(settings))
    assert optimize_cmd.main([str(pcb), "--net", "NET_B", "--dry-run", "--format", "json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["stats"]["nets_touched"] == ["NET_B"]
    assert pcb.read_text() == board

    def plan_width(text, locked_nets):
        start = text.index("0.4", text.index("(arc "))
        return {"NET_B": [ArcWidthEdit(start, start + 3, 0.55)]}

    config = OptimizationConfig(plan_arc_widths=plan_width)
    seen = []

    def count_findings(text):
        widened = "(width 0.55)" in text
        seen.append(widened)
        return {"clearance": int(widened)}

    config.output_verifiers["drc"] = count_findings
    stats = TraceOptimizer(config).optimize_pcb(str(pcb), net_filter="NET_B")
    assert True in seen and False in seen
    assert stats.output_verify_rolled_back["drc"] == ["NET_B"]
    assert pcb.read_text() == board


@pytest.mark.parametrize("command", ["route", "optimize-traces"])
@pytest.mark.parametrize("lock", ["direct", "group"])
def test_width_edit_preserves_net_with_only_a_locked_arc(tmp_path, capsys, command, lock):
    from kicad_tools.schema.pcb import PCB

    pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
    board = _arc_fixture_ids(pcb.read_text()).replace(
        "(segment (start 4 18)", "(arc (start 4 18) (mid 12 18.2)"
    )
    if lock == "direct":
        board = board.replace("(arc (start 4 18)", "(arc (locked yes) (start 4 18)")
    else:
        board = board.rstrip().removesuffix(")") + (
            '(group "tuned" (uuid "00000000-0000-4000-8000-0000000000c1") (locked yes)'
            ' (members "00000000-0000-4000-8000-0000000000b1")))'
        )
    pcb.write_text(board)
    settings = json.loads(project.read_text())
    next(c for c in settings["net_settings"]["classes"] if c["name"] == "ClassB")["track_width"] = (
        0.55
    )
    project.write_text(json.dumps(settings))
    before = _arc_geometry(PCB.load(pcb).arcs[0])
    if command == "route":
        _route_board(pcb, ["--skip-drc"])
    else:
        assert optimize_cmd.main([str(pcb), "--net", "NET_B", "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["locked_nets_skipped"] == ["NET_B"]
    assert [_arc_geometry(arc) for arc in PCB.load(pcb).arcs] == [before]


@pytest.mark.parametrize("detached", [True, False])
def test_reroute_recovers_old_detached_arc_but_keeps_connected_neckdown(tmp_path, detached):
    from kicad_tools.schema.pcb import PCB

    pcb, project = _write_routed_two_class_board_for_optimize(tmp_path)
    board = _arc_fixture_ids(pcb.read_text()).replace("(width 0.25)", "(width 0.37)")
    if detached:
        board = board.rstrip().removesuffix(")") + (
            '(arc (start 8 9) (mid 10 9.1) (end 12 9) (width 0.25) (layer "F.Cu")'
            ' (net 1) (uuid "00000000-0000-4000-8000-0000000000a3")))'
        )
    else:
        board = board.replace(
            "(segment (start 4 6) (end 12 6) (width 0.37)",
            "(arc (start 4 6) (mid 8 6.2) (end 12 6) (width 0.25)",
        )
    pcb.write_text(board)
    settings = json.loads(project.read_text())
    next(c for c in settings["net_settings"]["classes"] if c["name"] == "ClassA")["track_width"] = (
        0.37
    )
    project.write_text(json.dumps(settings))
    original = _arc_geometry(PCB.load(pcb).arcs[0])
    _route_board(pcb, ["--skip-drc"])
    result = PCB.load(pcb)
    assert {s.width for s in result.segments if s.net_name == "NET_A"} == {0.37}
    assert [_arc_geometry(arc) for arc in result.arcs] == ([] if detached else [original])
