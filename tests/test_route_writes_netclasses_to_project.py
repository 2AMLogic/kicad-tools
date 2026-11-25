"""Issue #10133: ``kct route`` writes the classes it routed with into the project.

The router classifies every net and serializes the result only to the
``<stem>.net_class_map.json`` sidecar; the sibling ``.kicad_pro``
``net_settings`` kept a flat ``Default`` with one width, so KiCad's Board
Setup and native DRC never saw the widths the copper actually uses.  These
tests pin the route-step write (``_write_netclasses_to_project``): one
``net_settings.classes`` entry per used router class with the router's
width / clearance (floored at the project's minimum clearance) / via size, one
PCB assignment and one schematic pattern per routed net (no pattern
metacharacter spillover across ``.``, ``+``, ``()``, ``*``, ``?``), GND and
power rails in dedicated classes, hand-authored content and edited assignments
preserved, and the non-fatal degradation paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.core.netclass_diagnostics import diagnose_netclasses
from kicad_tools.core.project_clearance import (
    authored_netclass_clearances,
    resolve_project_clearances,
)
from kicad_tools.core.project_file import create_netclass_definition, default_netclass_definition
from kicad_tools.manufacturers import get_profile
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.validate.rules.netclass_floor import check_netclass_floor

GROUND = NetClassRouting(name="Ground", priority=1, trace_width=0.5, clearance=0.2, via_size=0.8)
POWER = NetClassRouting(name="Power", priority=1, trace_width=0.5, clearance=0.2, via_size=0.8)
DIGITAL = NetClassRouting(name="Digital", priority=5, trace_width=0.2, clearance=0.2, via_size=0.6)
PROBE = NetClassRouting(name="Probe", priority=1, trace_width=0.3, clearance=0.5, via_size=0.7)


def _write_pcb(tmp_path: Path, nets: list[str], name: str = "board") -> Path:
    """A minimal ``.kicad_pcb`` declaring the given net names."""
    decls = "".join(f'(net {i} "{n}")' for i, n in enumerate(["", *nets]))
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(f"(kicad_pcb (version 20241229) {decls})", encoding="utf-8")
    return pcb


def _write_project(tmp_path: Path, project: dict, name: str = "board") -> Path:
    path = tmp_path / f"{name}.kicad_pro"
    path.write_text(json.dumps(project, indent=2), encoding="utf-8")
    return path


def _minimal_project(*, schema_version: int | None = 3) -> dict:
    net_settings: dict = {
        "classes": [default_netclass_definition()],
        "net_colors": None,
        "netclass_assignments": None,
        "netclass_patterns": [],
    }
    if schema_version is not None:
        net_settings["meta"] = {"version": schema_version}
        if schema_version >= 4:
            net_settings["classes"][0]["priority"] = 2**31 - 1
    else:
        net_settings["classes"][0]["priority"] = 2**31 - 1
    return {
        "board": {"design_settings": {}},
        "meta": {"version": 1},
        "net_settings": net_settings,
    }


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _classes(project: dict) -> dict[str, dict]:
    return {c["name"]: c for c in project["net_settings"]["classes"] if isinstance(c, dict)}


def _assignments(project: dict) -> dict | None:
    return project["net_settings"].get("netclass_assignments")


def _patterns(project: dict) -> list[dict]:
    return project["net_settings"].get("netclass_patterns") or []


class TestClassesAndAssignmentsWritten:
    def test_classes_with_router_values_and_exact_name_assignments(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["GND", "+3V3", "SDA"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(
            pcb,
            {"GND": GROUND, "+3V3": POWER, "SDA": DIGITAL},
            quiet=True,
        )

        project = _load(pro)
        classes = _classes(project)
        assert classes["Ground"]["track_width"] == 0.5
        assert classes["Ground"]["clearance"] == 0.2
        assert classes["Ground"]["via_diameter"] == 0.8
        assert classes["Power"]["track_width"] == 0.5
        assert classes["Digital"]["track_width"] == 0.2
        # Both editor representations are present, including schema 3 PCB strings.
        assert _assignments(project) == {"+3V3": "Power", "GND": "Ground", "SDA": "Digital"}
        assert len(_patterns(project)) == len(_assignments(project))
        # The Default class is untouched.
        assert classes["Default"]["track_width"] == 0.25

    @pytest.mark.parametrize("schema_version", [5, None])
    def test_v5_and_unversioned_projects_get_array_assignments(
        self, tmp_path: Path, schema_version: int | None
    ):
        pcb = _write_pcb(tmp_path, ["GND", "+3V3", "SDA"])
        pro = _write_project(tmp_path, _minimal_project(schema_version=schema_version))

        route_cmd._write_netclasses_to_project(
            pcb,
            {"GND": GROUND, "+3V3": POWER, "SDA": DIGITAL},
            quiet=True,
        )

        project = _load(pro)
        assert _assignments(project) == {
            "+3V3": ["Power"],
            "GND": ["Ground"],
            "SDA": ["Digital"],
        }
        report = diagnose_netclasses(project, ["GND", "+3V3", "SDA"])
        assert report["diagnostics"] == []
        assert {(r["net"], r["target"]) for r in report["assignments"]} == {
            ("+3V3", "Power"),
            ("GND", "Ground"),
            ("SDA", "Digital"),
        }

    def test_gnd_and_power_nets_not_in_default(self, tmp_path: Path):
        """AC3: GND and power rails land in dedicated classes, not Default."""
        pcb = _write_pcb(tmp_path, ["GND", "+3V3", "SDA"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(
            pcb, {"GND": GROUND, "+3V3": POWER, "SDA": DIGITAL}, quiet=True
        )

        project = _load(pro)
        resolved = resolve_project_clearances(project, ["GND", "+3V3", "SDA"])
        assert resolved["GND"].source_class == "Ground"
        assert resolved["+3V3"].source_class == "Power"
        assert resolved["GND"].source_class != "Default"
        assert resolved["+3V3"].source_class != "Default"

    def test_via_drill_threaded_from_router_rules(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["SDA"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(pcb, {"SDA": DIGITAL}, quiet=True, via_drill=0.35)

        assert _classes(_load(pro))["Digital"]["via_drill"] == 0.35

    def test_divergent_member_widths_keep_the_stricter(self, tmp_path: Path):
        """Two nets of one class name with different widths -> the larger
        (#6390's clearance rule, applied to every floor this write owns)."""
        pcb = _write_pcb(tmp_path, ["MOTOR_A", "MOTOR_B"])
        pro = _write_project(tmp_path, _minimal_project())
        wide = NetClassRouting(name="HighCurrent", trace_width=0.35, via_size=0.9)

        route_cmd._write_netclasses_to_project(
            pcb,
            {
                "MOTOR_A": NetClassRouting(name="HighCurrent", trace_width=0.2, via_size=0.6),
                "MOTOR_B": wide,
            },
            quiet=True,
        )

        cls = _classes(_load(pro))["HighCurrent"]
        assert cls["track_width"] == 0.35
        assert cls["via_diameter"] == 0.9

    def test_nets_missing_from_the_board_are_ignored(self, tmp_path: Path):
        """A loaded user map may name nets that are not on this board."""
        pcb = _write_pcb(tmp_path, ["SDA"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(pcb, {"SDA": DIGITAL, "GHOST": GROUND}, quiet=True)

        project = _load(pro)
        assert "GHOST" not in (_assignments(project) or {})
        assert "Ground" not in _classes(project)

    def test_sub_floor_class_clearance_floored_at_project_min_clearance(self, tmp_path: Path):
        """Board 07 regression: a NetClassRouting clearance below the project's
        min_clearance (0.10 mm vs JLCPCB 4-layer 0.1016 mm) is floored at
        min_clearance so check_netclass_floor reports zero errors."""
        rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)
        assert rules.min_clearance_mm == pytest.approx(0.1016)

        pcb = _write_pcb(tmp_path, ["DQ0", "DQS_P", "MIPI_CLK_P", "TMDS_D0_P"])
        project = _minimal_project()
        project["board"]["design_settings"]["rules"] = {"min_clearance": rules.min_clearance_mm}
        project["net_settings"]["classes"][0]["clearance"] = rules.min_clearance_mm
        project["net_settings"]["classes"][0]["via_drill"] = rules.min_via_drill_mm
        pro = _write_project(tmp_path, project)

        ncm = {
            "DQ0": NetClassRouting(name="DDR_DATA_BYTE_0", trace_width=0.15, clearance=0.10),
            "DQS_P": NetClassRouting(name="DDR_DQS", trace_width=0.225, clearance=0.10),
            "MIPI_CLK_P": NetClassRouting(name="MIPI_CSI_LANES", trace_width=0.225, clearance=0.10),
            "TMDS_D0_P": NetClassRouting(name="HDMI_TMDS_LANES", trace_width=0.225, clearance=0.10),
        }
        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True, via_drill=0.2)

        saved = _load(pro)
        for cls_name in ("DDR_DATA_BYTE_0", "DDR_DQS", "MIPI_CSI_LANES", "HDMI_TMDS_LANES"):
            assert _classes(saved)[cls_name]["clearance"] == pytest.approx(0.1016)
        floor_results = check_netclass_floor(pcb, rules, copper_layers=4)
        assert floor_results.errors == []


class TestMetacharacterNameControls:
    """Blocker 1 (#10166): exact-name assignments never spill onto lookalike nets."""

    @pytest.mark.parametrize(
        ("label", "lookalike"),
        [
            ("AC_LINE", "AC_LINE_N"),
            ("OC_TRIP_N", "OC_TRIP"),
            ("A.B", "AXB"),
            ("+3.3V", "+3x3V"),
            ("C+P", "CCP"),
            ("Net-(U5-CAPP)", "Net-U5-CAPP"),
            ("USB*", "USB1"),
            ("D?", "D1"),
        ],
    )
    @pytest.mark.parametrize("schema", [3, 5])
    def test_native_schematic_export_after_routing(
        self, tmp_path: Path, label: str, lookalike: str, schema: int
    ):
        import subprocess
        import xml.etree.ElementTree as ET

        from kicad_tools.cli.runner import find_kicad_cli

        cli = find_kicad_cli()
        if cli is None:
            pytest.skip("kicad-cli not installed")
        fixture = Path(__file__).parent / "fixtures" / "simple_rc.kicad_sch"
        schematic = tmp_path / "board.kicad_sch"
        schematic.write_text(
            fixture.read_text()
            .replace('(label "VIN"', f'(label "{label}"')
            .replace('(label "GND"', f'(label "{lookalike}"')
        )
        # Local labels acquire the root sheet prefix in both editors.
        net, other = f"/{label}", f"/{lookalike}"
        pcb = _write_pcb(tmp_path, [net, other])
        # Upgrade a project produced by the assignment-only route writer.
        router_class = NetClassRouting(name="HV") if label == "AC_LINE" else DIGITAL
        project = _minimal_project(schema_version=schema)
        project["net_settings"]["classes"].append(create_netclass_definition(router_class.name))
        project["net_settings"]["netclass_assignments"] = {
            net: router_class.name if schema == 3 else [router_class.name]
        }
        project["meta"][route_cmd.KCT_NETCLASS_ASSIGNMENTS_META_KEY] = {net: router_class.name}
        _write_project(tmp_path, project)
        route_cmd._write_netclasses_to_project(pcb, {net: router_class}, quiet=True)

        xml = tmp_path / "board.xml"
        subprocess.run(
            [
                str(cli),
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "-o",
                str(xml),
                str(schematic),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        classes = {n.attrib["name"]: n.attrib["class"] for n in ET.parse(xml).findall("./nets/net")}
        assert classes == {net: router_class.name, other: "Default"}

    @pytest.mark.parametrize(
        ("target_net", "lookalike_net"),
        [
            ("A.B", "AXB"),
            ("A.B", "A B"),
            ("C+P", "CCP"),
            ("D+", "DD"),
            ("Net-(U5-CAPP)", "Net-U5-CAPP"),
            ("USB*", "USB1"),
            ("D?", "D1"),
            ("+3.3V", "+3x3V"),
            ("SDA", "SDA1"),
            ("SPI CLK", "SPI_CLK"),
        ],
    )
    def test_metacharacter_net_assigns_only_itself_and_never_lookalike(
        self, tmp_path: Path, target_net: str, lookalike_net: str
    ):
        board_nets = [target_net, lookalike_net]
        pcb = _write_pcb(tmp_path, board_nets)
        pro = _write_project(tmp_path, _minimal_project(schema_version=5))

        route_cmd._write_netclasses_to_project(pcb, {target_net: PROBE}, quiet=True)

        project = _load(pro)
        # 1. diagnose_netclasses verifies structural/syntax support & membership:
        #    both representations classify exactly the target.
        report = diagnose_netclasses(project, board_nets)
        assert report["diagnostics"] == []
        assert [(row["target"], row["matches"]) for row in report["patterns"]] == [
            ("Probe", [target_net])
        ]
        assert [
            (row["net"], row["target"], row["status"], row["net_present"])
            for row in report["assignments"]
        ] == [(target_net, "Probe", "supported", True)]

        # 2. Effective membership via resolve_project_clearances &
        #    authored_netclass_clearances: target_net gets Probe (0.5 mm) while
        #    lookalike_net stays on Default (0.15 mm).
        resolved = resolve_project_clearances(project, board_nets)
        assert resolved[target_net].source_class == "Probe"
        assert resolved[target_net].clearance == pytest.approx(0.5)
        assert resolved[lookalike_net].source_class == "Default"
        assert resolved[lookalike_net].clearance == pytest.approx(0.15)
        assert set(authored_netclass_clearances(project, board_nets)) == {target_net}

    @pytest.mark.parametrize(
        ("target_net", "lookalike_net"),
        [
            ("A.B", "AXB"),
            ("C+P", "CCP"),
            ("Net-(U5-CAPP)", "Net-U5-CAPP"),
        ],
    )
    def test_native_kicad_cli_no_clearance_spillover_onto_lookalike(
        self, tmp_path: Path, target_net: str, lookalike_net: str
    ):
        import subprocess

        from kicad_tools.cli.runner import find_kicad_cli

        cli = find_kicad_cli()
        if cli is None:
            pytest.skip("kicad-cli not installed")

        # target_net (Probe, 0.50 mm) is isolated at y=15; lookalike_net sits
        # 0.25 mm from GND (y=5 vs y=5.45 with 0.2 mm tracks -> 0.25 mm gap):
        # legal under Default (0.15 mm) and illegal if Probe (0.50 mm) spills over.
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(
            f"""(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "{target_net}") (net 2 "{lookalike_net}") (net 3 "GND")
  (gr_rect (start 0 0) (end 25 25) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (segment (start 5 15) (end 10 15) (width 0.3) (layer "F.Cu") (net 1))
  (segment (start 5 5) (end 10 5) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 5 5.45) (end 10 5.45) (width 0.2) (layer "F.Cu") (net 3)))
""",
            encoding="utf-8",
        )
        _write_project(tmp_path, _minimal_project())
        route_cmd._write_netclasses_to_project(pcb, {target_net: PROBE}, quiet=True)

        report_path = pcb.with_suffix(".drc.json")
        proc = subprocess.run(
            [
                str(cli),
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-error",
                "-o",
                str(report_path),
                str(pcb),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        drc_data = json.loads(report_path.read_text(encoding="utf-8"))
        clearance_violations = [v for v in drc_data["violations"] if v["type"] == "clearance"]
        assert clearance_violations == []


class TestHandAuthoredContentPreserved:
    def test_authored_class_merged_not_replaced(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["SDA"])
        project = _minimal_project()
        project["net_settings"]["classes"].append(
            {
                "name": "Digital",
                "track_width": 0.3,  # stricter than the router's 0.2
                "clearance": 0.2,
                "wire_width": 42,  # an authored key this write never mentions
            }
        )
        pro = _write_project(tmp_path, project)

        route_cmd._write_netclasses_to_project(pcb, {"SDA": DIGITAL}, quiet=True)

        cls = _classes(_load(pro))["Digital"]
        # Stricter-wins floors (#7385): the authored 0.3 beats 0.2.
        assert cls["track_width"] == 0.3
        # Authored keys survive verbatim.
        assert cls["wire_width"] == 42

    def test_net_already_classified_by_authored_pattern_keeps_its_class(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["AUX1", "AUX2", "GND"])
        project = _minimal_project()
        project["net_settings"]["classes"].append(
            {"name": "Audio", "track_width": 0.25, "clearance": 0.25}
        )
        project["net_settings"]["netclass_patterns"].append(
            {"netclass": "Audio", "pattern": "AUX*"}
        )
        pro = _write_project(tmp_path, project)

        route_cmd._write_netclasses_to_project(
            pcb, {"AUX1": DIGITAL, "AUX2": DIGITAL, "GND": GROUND}, quiet=True
        )

        saved = _load(pro)
        # Authored wildcard pattern retains its KiCad behaviour across AUX1/AUX2,
        # with no competing netclass_assignments overriding it.
        assert _patterns(saved) == [
            {"netclass": "Audio", "pattern": "AUX*"},
            {"netclass": "Ground", "pattern": "GND"},
        ]
        assert _assignments(saved) == {"GND": "Ground"}
        resolved = resolve_project_clearances(saved, ["AUX1", "AUX2", "GND"])
        assert resolved["AUX1"].source_class == "Audio"
        assert resolved["AUX2"].source_class == "Audio"
        assert resolved["GND"].source_class == "Ground"

    def test_kct_recorded_6390_assignments_upgrade_class_definition(self, tmp_path: Path):
        """The #6390 assignments kct itself wrote moments earlier (recorded
        under ``meta.kct_router_netclasses``) are not authored intent: the
        class still receives the router's full track_width and via_diameter."""
        pcb = _write_pcb(tmp_path, ["GND"])
        project = _minimal_project()
        project["net_settings"]["classes"].append(
            {"name": "Ground", "track_width": 0.25, "clearance": 0.2, "via_diameter": 0.6}
        )
        project["net_settings"]["netclass_assignments"] = {"GND": "Ground"}
        project["meta"]["kct_router_netclasses"] = {
            "classes": ["Ground"],
            "assignments": ["GND"],
        }
        pro = _write_project(tmp_path, project)

        route_cmd._write_netclasses_to_project(pcb, {"GND": GROUND}, quiet=True)

        saved = _load(pro)
        assert _classes(saved)["Ground"]["track_width"] == 0.5
        assert _classes(saved)["Ground"]["via_diameter"] == 0.8
        assert _assignments(saved) == {"GND": "Ground"}

    def test_legacy_ownership_does_not_claim_an_identical_authored_pattern(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["SDA"])
        project = _minimal_project()
        project["net_settings"]["classes"].append({"name": "Digital", "clearance": 0.2})
        project["net_settings"]["netclass_assignments"] = {"SDA": "Digital"}
        project["meta"][route_cmd.KCT_NETCLASS_ASSIGNMENTS_META_KEY] = {"SDA": "Digital"}
        authored = {"netclass": "Digital", "pattern": "SDA"}
        project["net_settings"]["netclass_patterns"] = [authored]
        pro = _write_project(tmp_path, project)

        route_cmd._write_netclasses_to_project(pcb, {"SDA": PROBE}, quiet=True)
        assert _patterns(_load(pro)) == [authored]
        assert _assignments(_load(pro)) == {}

    def test_hand_edited_kct_assignment_survives_rerun(self, tmp_path: Path):
        """Blocker 2 (#10166): if the user re-points a kct-written assignment to
        another class in KiCad (SDA: Probe -> EditorSignal), re-running the
        writer preserves EditorSignal instead of reverting to Probe."""
        pcb = _write_pcb(tmp_path, ["SDA", "GND"])
        pro = _write_project(tmp_path, _minimal_project())
        ncm = {"SDA": PROBE, "GND": GROUND}

        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True)

        # User edits SDA -> EditorSignal in KiCad.
        edited = _load(pro)
        edited["net_settings"]["classes"].append(
            {"name": "EditorSignal", "track_width": 0.25, "clearance": 0.18, "via_diameter": 0.6}
        )
        edited["net_settings"]["netclass_assignments"]["SDA"] = "EditorSignal"
        _write_project(tmp_path, edited)

        # Re-run the production writer with the same router map.
        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True)

        saved = _load(pro)
        assert _assignments(saved) == {"GND": "Ground", "SDA": "EditorSignal"}
        assert saved["meta"][route_cmd.KCT_NETCLASS_ASSIGNMENTS_META_KEY] == {"GND": "Ground"}
        resolved = resolve_project_clearances(saved, ["SDA", "GND"])
        assert resolved["SDA"].source_class == "EditorSignal"
        assert resolved["SDA"].clearance == pytest.approx(0.18)
        assert resolved["GND"].source_class == "Ground"

    def test_hand_edited_assignment_replaced_with_pattern_survives_rerun(self, tmp_path: Path):
        """If the user removes kct's assignment and classifies the net via a
        pattern in KiCad, re-running preserves that pattern's effective class."""
        pcb = _write_pcb(tmp_path, ["SDA", "GND"])
        pro = _write_project(tmp_path, _minimal_project())
        ncm = {"SDA": PROBE, "GND": GROUND}

        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True)

        edited = _load(pro)
        edited["net_settings"]["classes"].append(
            {"name": "EditorSignal", "track_width": 0.25, "clearance": 0.18, "via_diameter": 0.6}
        )
        edited["net_settings"]["netclass_assignments"].pop("SDA")
        edited["net_settings"]["netclass_patterns"].append(
            {"netclass": "EditorSignal", "pattern": "SDA"}
        )
        _write_project(tmp_path, edited)

        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True)

        saved = _load(pro)
        assert _assignments(saved) == {"GND": "Ground"}
        assert _patterns(saved) == [
            {"netclass": "EditorSignal", "pattern": "SDA"},
            {"netclass": "Ground", "pattern": "GND"},
        ]
        resolved = resolve_project_clearances(saved, ["SDA", "GND"])
        assert resolved["SDA"].source_class == "EditorSignal"


class TestIdempotency:
    def test_rerun_is_byte_identical(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["GND", "+3V3", "SDA"])
        pro = _write_project(tmp_path, _minimal_project())
        ncm = {"GND": GROUND, "+3V3": POWER, "SDA": DIGITAL}

        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True)
        first = pro.read_text(encoding="utf-8")
        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True)

        assert pro.read_text(encoding="utf-8") == first

    def test_changed_classification_replaces_the_stale_assignment(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["SDA"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(pcb, {"SDA": DIGITAL}, quiet=True)
        clock = NetClassRouting(name="Clock", trace_width=0.15, clearance=0.2)
        route_cmd._write_netclasses_to_project(pcb, {"SDA": clock}, quiet=True)

        project = _load(pro)
        assert _patterns(project) == [{"netclass": "Clock", "pattern": "SDA"}]
        assert _assignments(project) == {"SDA": "Clock"}
        assert project["meta"][route_cmd.KCT_NETCLASS_ASSIGNMENTS_META_KEY] == {"SDA": "Clock"}

    def test_net_reclassified_to_default_drops_stale_assignment(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["SDA"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(pcb, {"SDA": DIGITAL}, quiet=True)
        route_cmd._write_netclasses_to_project(
            pcb, {"SDA": NetClassRouting(name="Default")}, quiet=True
        )

        project = _load(pro)
        assert _patterns(project) == []
        assert _assignments(project) == {}
        assert route_cmd.KCT_NETCLASS_ASSIGNMENTS_META_KEY not in project.get("meta", {})


class TestNonFatalDegradation:
    def test_missing_project_is_a_warning(self, tmp_path: Path, capsys):
        pcb = _write_pcb(tmp_path, ["GND"])
        # No .kicad_pro beside the board.

        route_cmd._write_netclasses_to_project(pcb, {"GND": GROUND}, quiet=False)

        out = capsys.readouterr().out
        assert "not written to the project" in out
        assert not (tmp_path / "board.kicad_pro").exists()

    def test_empty_map_touches_nothing(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["GND"])
        pro = _write_project(tmp_path, _minimal_project())
        before = pro.read_text(encoding="utf-8")

        route_cmd._write_netclasses_to_project(pcb, {}, quiet=True)
        route_cmd._write_netclasses_to_project(pcb, None, quiet=True)

        assert pro.read_text(encoding="utf-8") == before

    def test_readonly_dir_is_non_fatal(self, tmp_path: Path, capsys):
        pcb = _write_pcb(tmp_path, ["GND"])
        pro = _write_project(tmp_path, _minimal_project())
        before = pro.read_text(encoding="utf-8")

        import os

        os.chmod(tmp_path, 0o500)
        try:
            # Must not raise.
            route_cmd._write_netclasses_to_project(pcb, {"GND": GROUND}, quiet=False)
        finally:
            os.chmod(tmp_path, 0o700)
        captured = capsys.readouterr().out
        # Either the write was blocked (warning emitted) or the platform
        # ignored the mode bits (running as root) and the classes landed.
        assert (
            "could not write net classes" in captured
            or _classes(_load(pro)).get("Ground", {}).get("track_width") == 0.5
        )
        if "could not write net classes" in captured:
            assert pro.read_text(encoding="utf-8") == before

    def test_unevaluable_authored_netclasses_skip_assignment_with_warning(
        self, tmp_path: Path, capsys
    ):
        """An authored netclass block that cannot be evaluated safely must
        not be guessed at (#6390 rule): no assignments are added."""
        pcb = _write_pcb(tmp_path, ["SDA"])
        project = _minimal_project()
        project["net_settings"]["meta"] = {"version": 99}  # unsupported schema
        pro = _write_project(tmp_path, project)
        before = pro.read_text(encoding="utf-8")

        route_cmd._write_netclasses_to_project(pcb, {"SDA": DIGITAL}, quiet=False)

        out = capsys.readouterr().out
        assert "cannot be evaluated safely" in out
        assert pro.read_text(encoding="utf-8") == before


class TestKctStampInterop:
    def test_stamped_project_stays_kct_owned(self, tmp_path: Path):
        """#6390 AC5: a kct-stamped project must stay recognisable across
        this write, or the next renamed-output route's conflict check
        refuses the destination."""
        from kicad_tools.manufacturers.project_generator import (
            is_kct_owned_project,
            stamp_kct_project,
        )

        pcb = _write_pcb(tmp_path, ["GND"])
        project = _minimal_project()
        stamp_kct_project(project)
        pro = _write_project(tmp_path, project)

        route_cmd._write_netclasses_to_project(pcb, {"GND": GROUND}, quiet=True)

        assert is_kct_owned_project(_load(pro))

    def test_hand_authored_project_gains_no_stamp(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["GND"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(pcb, {"GND": GROUND}, quiet=True)

        assert "kct_sidecar_sha256" not in _load(pro).get("meta", {})


class TestRunPostRouteDrcIntegration:
    def test_project_netclasses_written_after_constraint_sidecars(self, tmp_path: Path, capsys):
        """The shared post-route DRC entry (all three route flows) declares
        the classes in the project, after -- and into the file produced by
        -- the DRC-constraint sidecar write."""
        pcb = _write_pcb(tmp_path, ["GND", "+3V3", "SDA", "A.B", "AXB"])

        # The DRC verification itself is skipped for time (#6273): the
        # sidecars (including this write) still ship with the board.
        errors, warnings = route_cmd.run_post_route_drc(
            output_path=pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map={"GND": GROUND, "+3V3": POWER, "SDA": DIGITAL, "A.B": PROBE},
            time_left=20.0,
            drc_cost_estimate=40.0,
        )
        assert (errors, warnings) == route_cmd.DRC_SKIPPED_FOR_TIME

        project = _load(tmp_path / "board.kicad_pro")
        classes = _classes(project)
        assert classes["Ground"]["track_width"] == 0.5
        assert classes["Ground"]["via_diameter"] == 0.8
        assert classes["Power"]["track_width"] == 0.5
        assert classes["Digital"]["track_width"] == 0.2
        assert classes["Probe"]["clearance"] == 0.5
        assert _assignments(project) == {
            "+3V3": "Power",
            "A.B": "Probe",
            "GND": "Ground",
            "SDA": "Digital",
        }
        assert len(_patterns(project)) == len(_assignments(project))
        resolved = resolve_project_clearances(project, ["GND", "+3V3", "SDA", "A.B", "AXB"])
        assert resolved["A.B"].source_class == "Probe"
        assert resolved["AXB"].source_class == "Default"
        out = capsys.readouterr().out
        assert "Net classes written to project" in out

    def test_post_route_drc_rerun_preserves_hand_edited_stricter_class_assignment(
        self, tmp_path: Path
    ):
        """Full post-route pipeline rerun (constraint sidecars + project
        netclass writer) preserves an edited SDA: Probe -> EditorSignal
        assignment even though Probe > Default was also in #6390 provenance."""
        pcb = _write_pcb(tmp_path, ["SDA", "GND"])
        ncm = {"SDA": PROBE, "GND": GROUND}

        route_cmd.run_post_route_drc(
            output_path=pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=ncm,
            time_left=20.0,
            drc_cost_estimate=40.0,
            quiet=True,
        )

        pro = tmp_path / "board.kicad_pro"
        edited = _load(pro)
        edited["net_settings"]["classes"].append(
            {"name": "EditorSignal", "track_width": 0.25, "clearance": 0.18, "via_diameter": 0.6}
        )
        edited["net_settings"]["netclass_assignments"]["SDA"] = "EditorSignal"
        _write_project(tmp_path, edited)

        route_cmd.run_post_route_drc(
            output_path=pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=ncm,
            time_left=20.0,
            drc_cost_estimate=40.0,
            quiet=True,
        )

        saved = _load(pro)
        assert _assignments(saved) == {"GND": "Ground", "SDA": "EditorSignal"}
        resolved = resolve_project_clearances(saved, ["SDA", "GND"])
        assert resolved["SDA"].source_class == "EditorSignal"
        assert resolved["SDA"].clearance == pytest.approx(0.18)
        assert resolved["GND"].source_class == "Ground"

    def test_reroute_preserves_edited_pattern_and_its_class(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["SDA", "SDA1", "AUX"])
        pro = pcb.with_suffix(".kicad_pro")

        def route():
            route_cmd.run_post_route_drc(
                output_path=pcb,
                manufacturer="jlcpcb",
                layers=2,
                net_class_map={"SDA": PROBE, "AUX": NetClassRouting(name="Aux", clearance=0.1)},
                time_left=20.0,
                drc_cost_estimate=40.0,
                quiet=True,
            )

        route()
        before = pro.read_bytes()
        route()
        assert pro.read_bytes() == before

        project = _load(pro)
        authored = {"netclass": "Probe", "pattern": "SDA*", "comment": "user edit"}
        project["net_settings"]["netclass_patterns"][-1] = authored
        _write_project(tmp_path, project)
        route()
        saved = _load(pro)
        assert _patterns(saved) == [authored, {"netclass": "Aux", "pattern": "AUX"}]
        assert _assignments(saved) == {"AUX": "Aux"}
        assert resolve_project_clearances(saved, ["SDA1"])["SDA1"].source_class == "Probe"
