"""Board 01's reviewed Power copper survives its release packager (Issue #10421).

``boards/01-voltage-divider/generate_design.py`` used to emit a project with
only the ``Default`` net class. ``kct route`` then declared the router's
``Power`` class as a clone of ``Default``, whose ``track_width`` is the JLCPCB
floor (0.127 mm), and the release packager's default cleanup -- ``kct
optimize-traces --drc-aware``, which honours project class widths since
#10189 -- narrowed both VIN tracks and all seven VOUT tracks to 0.127 mm,
below the 0.30 mm ``min_trace`` the board's ``project.kct`` declares.

The recipe now authors the ``Power`` class it reviewed (0.50 mm tracks on
VIN, VOUT and GND). Authored intent wins over the router-declared clone, so
the same cleanup leaves the nine segments untouched: two VIN segments and
seven VOUT segments, each at 0.500 mm, where ``project.kct`` declares a
0.30 mm ``min_trace``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "01-voltage-divider"
OUTPUT = BOARD_DIR / "output"
RECIPE_PROJECT = OUTPUT / "voltage_divider.kicad_pro"
ROUTED_PCB = OUTPUT / "voltage_divider_routed.kicad_pcb"
ROUTED_PROJECT = OUTPUT / "voltage_divider_routed.kicad_pro"

POWER_NETCLASS = "Power"
POWER_NETS = ("GND", "VIN", "VOUT")
POWER_TRACK_WIDTH_MM = 0.5
POWER_CLEARANCE_MM = 0.2
EXPECTED_SEGMENTS = {"VIN": 2, "VOUT": 7}
SPEC_MIN_TRACE_MM = 0.3
WIDTH_FINDINGS = frozenset(
    {"spec_conformance_min_trace", "netclass_sidecar_mismatch", "netclass_track_width_mismatch"}
)

ROUTED_SIDECARS = (
    "voltage_divider_routed.kicad_pro",
    "voltage_divider_routed.kicad_dru",
    "voltage_divider_routed.net_class_map.json",
)


def _load_recipe():
    recipe = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board01_generate_design_10421", recipe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _power_class(project: dict) -> dict:
    return next(cls for cls in project["net_settings"]["classes"] if cls["name"] == POWER_NETCLASS)


def _assigned_classes(project: dict) -> dict[str, list[str]]:
    """``{net: [class, ...]}`` each Power net resolves to, schema 3 or 4/5."""
    assignments = project["net_settings"]["netclass_assignments"]
    resolved = {}
    for net in POWER_NETS:
        value = assignments[net]
        resolved[net] = [value] if isinstance(value, str) else list(value)
    return resolved


def _track_widths(pcb_path: Path) -> dict[str, list[float]]:
    """``{net: [width, ...]}`` of every routed copper segment, in file order."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    assert not pcb.arcs, "board 01 routes straight segments only"
    nets = {num: net.name for num, net in pcb.nets.items()}
    widths: dict[str, list[float]] = {net: [] for net in EXPECTED_SEGMENTS}
    for segment in pcb.segments:
        name = nets[segment.net_number]
        assert name in widths, f"unexpected routed net {name!r}"
        widths[name].append(round(segment.width, 4))
    return widths


def _assert_reviewed_power_copper(pcb_path: Path) -> None:
    widths = _track_widths(pcb_path)
    assert {net: len(found) for net, found in widths.items()} == EXPECTED_SEGMENTS, widths
    narrow = {net: [w for w in found if w != POWER_TRACK_WIDTH_MM] for net, found in widths.items()}
    assert not any(narrow.values()), f"Power copper left the reviewed width: {widths}"
    assert POWER_TRACK_WIDTH_MM >= SPEC_MIN_TRACE_MM


def _stage_routed_board(tmp_path: Path) -> Path:
    """The committed routed board and its sidecars, as the packager stages them."""
    for name in ("voltage_divider_routed.kicad_pcb", *ROUTED_SIDECARS):
        (tmp_path / name).write_bytes((OUTPUT / name).read_bytes())
    return tmp_path / "voltage_divider_routed.kicad_pcb"


def test_recipe_authors_the_power_netclass(tmp_path: Path) -> None:
    """The recipe's project declares Power at the reviewed width and assigns its nets."""
    project_path = _load_recipe().create_project(tmp_path, "voltage_divider")
    project = _load_json(project_path)

    power = _power_class(project)
    assert power["track_width"] == POWER_TRACK_WIDTH_MM
    assert power["clearance"] == POWER_CLEARANCE_MM
    assert _assigned_classes(project) == {net: [POWER_NETCLASS] for net in POWER_NETS}

    patterns = {
        row["pattern"]: row["netclass"] for row in project["net_settings"]["netclass_patterns"]
    }
    assert {net: patterns[net] for net in POWER_NETS} == dict.fromkeys(POWER_NETS, POWER_NETCLASS)

    from kicad_tools.core.project_clearance import authored_netclass_widths

    resolved = authored_netclass_widths(project, list(POWER_NETS))
    assert {net: resolved[net].width for net in POWER_NETS} == dict.fromkeys(
        POWER_NETS, POWER_TRACK_WIDTH_MM
    )
    assert {net: resolved[net].source_class for net in POWER_NETS} == dict.fromkeys(
        POWER_NETS, POWER_NETCLASS
    )


def test_committed_recipe_project_is_what_the_recipe_emits(tmp_path: Path) -> None:
    """``output/voltage_divider.kicad_pro`` is the recipe's own output."""
    project_path = _load_recipe().create_project(tmp_path, "voltage_divider")
    assert project_path.read_bytes() == RECIPE_PROJECT.read_bytes(), (
        "the committed recipe project is stale; regenerate it with "
        "boards/01-voltage-divider/generate_design.py"
    )


def test_committed_routed_project_declares_the_reviewed_power_width() -> None:
    """The shipped routed project carries the authored class through ``kct route``."""
    project = _load_json(ROUTED_PROJECT)
    power = _power_class(project)
    assert power["track_width"] == POWER_TRACK_WIDTH_MM, (
        f"routed project narrows Power to {power['track_width']} mm"
    )
    assert power["clearance"] == POWER_CLEARANCE_MM
    assert _assigned_classes(project) == {net: [POWER_NETCLASS] for net in POWER_NETS}

    from kicad_tools.core.project_clearance import authored_netclass_widths

    resolved = authored_netclass_widths(project, list(POWER_NETS))
    assert {net: resolved[net].width for net in POWER_NETS} == dict.fromkeys(
        POWER_NETS, POWER_TRACK_WIDTH_MM
    )


def test_committed_routed_copper_is_the_reviewed_power_width() -> None:
    _assert_reviewed_power_copper(ROUTED_PCB)


def test_default_cleanup_keeps_the_nine_power_tracks(tmp_path: Path) -> None:
    """The packager's ``dedupe -> prune -> optimize-traces`` leaves the copper alone."""
    from kicad_tools.cli import readiness_cmd
    from kicad_tools.cli.readiness_cmd import ReadinessOptions

    staged = _stage_routed_board(tmp_path)
    before = staged.read_bytes()
    _assert_reviewed_power_copper(staged)

    options = ReadinessOptions(
        board_dir=tmp_path,
        pcb=staged,
        schematic=None,
        project=tmp_path / "voltage_divider_routed.kicad_pro",
        manufacturer="jlcpcb",
        mode="assembly",
        operation="generate",
        output_dir=tmp_path / "manufacturing",
        evidence_dir=tmp_path / "readiness",
        skip_cleanup=False,
    )
    record = readiness_cmd._copper_cleanup(options)

    assert record["copper_cleanup"] == "applied", record
    steps = record["steps"]
    assert steps["dedupe"]["status"] == "applied", record
    assert steps["prune"]["status"] == "applied", record
    assert steps["optimize_traces"]["status"] == "applied", record

    _assert_reviewed_power_copper(staged)
    assert staged.read_bytes() == before, "default cleanup rewrote the reviewed copper"


def test_committed_release_evidence_carries_no_width_finding() -> None:
    """The shipped receipt was produced with cleanup on and flags no width drift."""
    from kicad_tools.cli.board_readiness import read_readiness

    receipt = read_readiness(BOARD_DIR)
    assert receipt["status"] != "unverified", receipt["blockers"]

    readiness = _load_json(OUTPUT / "readiness.json")
    cleanup = readiness["metrics"]["copper_cleanup"]
    assert cleanup["copper_cleanup"] == "applied", "evidence must come from a default-cleanup run"
    assert cleanup["steps"]["optimize_traces"]["status"] == "applied"

    kct_check = next(c for c in readiness["checks"] if c["name"] == "kct_check")
    assert kct_check["status"] == "passed", kct_check

    report = _load_json(OUTPUT / "readiness" / "kct-check.json")
    drifted = sorted(v["rule_id"] for v in report["violations"] if v["rule_id"] in WIDTH_FINDINGS)
    assert drifted == [], f"width findings in the shipped kct check: {drifted}"
