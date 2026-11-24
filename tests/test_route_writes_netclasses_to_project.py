"""Issue #10133: ``kct route`` writes the classes it routed with into the project.

The router classifies every net and serializes the result only to the
``<stem>.net_class_map.json`` sidecar; the sibling ``.kicad_pro``
``net_settings`` kept a flat ``Default`` with one width, so KiCad's Board
Setup and native DRC never saw the widths the copper actually uses.  These
tests pin the route-step write (``_write_netclasses_to_project``): one
``net_settings.classes`` entry per used router class with the router's
width / clearance / via size, one exact-name ``netclass_patterns`` entry per
routed net, GND and power rails in dedicated classes, hand-authored content
preserved, and the non-fatal degradation paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli import route_cmd
from kicad_tools.core.project_file import default_netclass_definition
from kicad_tools.router.rules import NetClassRouting

GROUND = NetClassRouting(name="Ground", priority=1, trace_width=0.5, clearance=0.2, via_size=0.8)
POWER = NetClassRouting(name="Power", priority=1, trace_width=0.5, clearance=0.2, via_size=0.8)
DIGITAL = NetClassRouting(name="Digital", priority=5, trace_width=0.2, clearance=0.2, via_size=0.6)


def _write_pcb(tmp_path: Path, nets: list[str], name: str = "board") -> Path:
    """A minimal ``.kicad_pcb`` declaring the given net names."""
    decls = "".join(f'(net {i} "{n}")' for i, n in enumerate(nets))
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(f"(kicad_pcb (version 20241229) {decls})", encoding="utf-8")
    return pcb


def _write_project(tmp_path: Path, project: dict, name: str = "board") -> Path:
    path = tmp_path / f"{name}.kicad_pro"
    path.write_text(json.dumps(project, indent=2), encoding="utf-8")
    return path


def _minimal_project() -> dict:
    return {
        "board": {"design_settings": {}},
        "meta": {"version": 1},
        "net_settings": {
            "classes": [default_netclass_definition()],
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
    }


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _classes(project: dict) -> dict[str, dict]:
    return {c["name"]: c for c in project["net_settings"]["classes"] if isinstance(c, dict)}


def _patterns(project: dict) -> list[dict]:
    return project["net_settings"]["netclass_patterns"]


class TestClassesAndPatternsWritten:
    def test_classes_with_router_values_and_exact_name_patterns(self, tmp_path: Path):
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
        # AC1: one exact-name pattern per routed net.
        assert {"netclass": "Ground", "pattern": "GND"} in _patterns(project)
        assert {"netclass": "Power", "pattern": "+3V3"} in _patterns(project)
        assert {"netclass": "Digital", "pattern": "SDA"} in _patterns(project)
        # The Default class is untouched.
        assert classes["Default"]["track_width"] == 0.25

    def test_gnd_and_power_nets_not_in_default(self, tmp_path: Path):
        """AC3: GND and power rails land in dedicated classes, not Default."""
        pcb = _write_pcb(tmp_path, ["GND", "+3V3", "SDA"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(
            pcb, {"GND": GROUND, "+3V3": POWER, "SDA": DIGITAL}, quiet=True
        )

        project = _load(pro)
        by_pattern = {p["pattern"]: p["netclass"] for p in _patterns(project)}
        assert by_pattern["GND"] == "Ground"
        assert by_pattern["+3V3"] == "Power"
        assert by_pattern["GND"] != "Default"
        assert by_pattern["+3V3"] != "Default"

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
        assert "GHOST" not in {p["pattern"] for p in _patterns(project)}
        assert "Ground" not in _classes(project)


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
        pcb = _write_pcb(tmp_path, ["AUX1", "GND"])
        project = _minimal_project()
        project["net_settings"]["classes"].append(
            {"name": "Audio", "track_width": 0.25, "clearance": 0.2}
        )
        project["net_settings"]["netclass_patterns"].append(
            {"netclass": "Audio", "pattern": "AUX*"}
        )
        pro = _write_project(tmp_path, project)

        route_cmd._write_netclasses_to_project(pcb, {"AUX1": DIGITAL, "GND": GROUND}, quiet=True)

        patterns = _patterns(_load(pro))
        # The authored wildcard pattern is untouched and is the only
        # classification of AUX1 (no competing exact-name pattern).
        assert {"netclass": "Audio", "pattern": "AUX*"} in patterns
        assert "AUX1" not in {p["pattern"] for p in patterns}
        # The un-classified net still gets its pattern.
        assert {"netclass": "Ground", "pattern": "GND"} in patterns

    def test_kct_recorded_6390_assignments_do_not_block_patterns(self, tmp_path: Path):
        """The #6390 assignments kct itself wrote moments earlier (recorded
        under ``meta.kct_router_netclasses``) are not authored intent: every
        routed net is still re-stated as an exact-name pattern."""
        pcb = _write_pcb(tmp_path, ["GND"])
        project = _minimal_project()
        project["net_settings"]["classes"].append(
            {"name": "Ground", "track_width": 0.25, "clearance": 0.2}
        )
        project["net_settings"]["netclass_assignments"] = {"GND": "Ground"}
        project["meta"]["kct_router_netclasses"] = {
            "classes": ["Ground"],
            "assignments": ["GND"],
        }
        pro = _write_project(tmp_path, project)

        route_cmd._write_netclasses_to_project(pcb, {"GND": GROUND}, quiet=True)

        assert {"netclass": "Ground", "pattern": "GND"} in _patterns(_load(pro))


class TestIdempotency:
    def test_rerun_is_byte_identical(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["GND", "+3V3", "SDA"])
        pro = _write_project(tmp_path, _minimal_project())
        ncm = {"GND": GROUND, "+3V3": POWER, "SDA": DIGITAL}

        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True)
        first = pro.read_text(encoding="utf-8")
        route_cmd._write_netclasses_to_project(pcb, ncm, quiet=True)

        assert pro.read_text(encoding="utf-8") == first
        # No duplicate patterns after the re-run.
        patterns = _patterns(_load(pro))
        assert len(patterns) == len({(p["netclass"], p["pattern"]) for p in patterns})

    def test_changed_classification_replaces_the_stale_pattern(self, tmp_path: Path):
        pcb = _write_pcb(tmp_path, ["SDA"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(pcb, {"SDA": DIGITAL}, quiet=True)
        clock = NetClassRouting(name="Clock", trace_width=0.15, clearance=0.2)
        route_cmd._write_netclasses_to_project(pcb, {"SDA": clock}, quiet=True)

        project = _load(pro)
        by_pattern = {p["pattern"]: p["netclass"] for p in _patterns(project)}
        assert by_pattern == {"SDA": "Clock"}
        assert project["meta"][route_cmd.KCT_NETCLASS_PATTERNS_META_KEY] == ["SDA"]


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
        not be guessed at (#6390 rule): no patterns are added."""
        pcb = _write_pcb(tmp_path, ["SDA"])
        project = _minimal_project()
        project["net_settings"]["meta"] = {"version": 99}  # unsupported schema
        pro = _write_project(tmp_path, project)

        route_cmd._write_netclasses_to_project(pcb, {"SDA": DIGITAL}, quiet=False)

        out = capsys.readouterr().out
        assert "cannot be evaluated safely" in out
        assert _patterns(_load(pro)) == []

    def test_wildcard_named_nets_skipped_from_patterns(self, tmp_path: Path, capsys):
        """A net whose name contains a wildcard character cannot be assigned
        through an exact-name pattern (#10122 hazard); its class is still
        declared for its sibling nets."""
        pcb = _write_pcb(tmp_path, ["USB*", "GND"])
        pro = _write_project(tmp_path, _minimal_project())

        route_cmd._write_netclasses_to_project(pcb, {"USB*": DIGITAL, "GND": GROUND}, quiet=False)

        out = capsys.readouterr().out
        assert "wildcard" in out
        project = _load(pro)
        assert "USB*" not in {p["pattern"] for p in _patterns(project)}
        assert "Digital" not in _classes(project)  # no assignable member
        assert {"netclass": "Ground", "pattern": "GND"} in _patterns(project)


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
        pcb = _write_pcb(tmp_path, ["GND", "+3V3", "SDA"])

        # The DRC verification itself is skipped for time (#6273): the
        # sidecars (including this write) still ship with the board.
        errors, warnings = route_cmd.run_post_route_drc(
            output_path=pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map={"GND": GROUND, "+3V3": POWER, "SDA": DIGITAL},
            time_left=20.0,
            drc_cost_estimate=40.0,
        )
        assert (errors, warnings) == route_cmd.DRC_SKIPPED_FOR_TIME

        project = _load(tmp_path / "board.kicad_pro")
        classes = _classes(project)
        # The constraint step created the project; this write declared the
        # router's classes in it.
        assert classes["Ground"]["track_width"] == 0.5
        assert classes["Ground"]["via_diameter"] == 0.8
        assert classes["Power"]["track_width"] == 0.5
        assert classes["Digital"]["track_width"] == 0.2
        by_pattern = {p["pattern"]: p["netclass"] for p in _patterns(project)}
        assert by_pattern["GND"] == "Ground"
        assert by_pattern["+3V3"] == "Power"
        assert by_pattern["SDA"] == "Digital"
        out = capsys.readouterr().out
        assert "Net classes written to project" in out
