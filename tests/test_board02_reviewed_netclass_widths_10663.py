"""Board 02's reviewed copper survives its release publisher (Issue #10663).

``boards/02-charlieplex-led/generate_design.py`` left every net class to
``kct route``, which declared Audio, Clock, Debug, Digital and Power as clones
of ``Default`` at the JLCPCB floor -- ``track_width`` 0.127 mm. The publisher's
ordinary cleanup, ``kct optimize-traces --drc-aware``, has honoured project
class widths since #10189, so it rewrote all 144 reviewed segments (99 signal
segments at 0.20 mm, 45 power segments at 0.50 mm) to 0.127 mm, and the
render provenance then refused the changed PCB.

The recipe now authors the five classes it reviewed, the way Board 01 authors
its Power class (#10421). Authored intent wins over the router-declared
clone, so the routed project carries the reviewed widths and the same
cleanup leaves the copper untouched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "02-charlieplex-led"
OUTPUT = BOARD_DIR / "output"
RECIPE_PROJECT = OUTPUT / "charlieplex_3x3.kicad_pro"
ROUTED_PCB = OUTPUT / "charlieplex_3x3_routed.kicad_pcb"
ROUTED_PROJECT = OUTPUT / "charlieplex_3x3_routed.kicad_pro"

SIGNAL_TRACK_WIDTH_MM = 0.2
POWER_TRACK_WIDTH_MM = 0.5
#: class -> (reviewed track width, the router's own clearance, the nets it covers)
REVIEWED_CLASSES = {
    "Audio": (SIGNAL_TRACK_WIDTH_MM, 0.15, ("LINE_A", "LINE_B", "LINE_C", "LINE_D")),
    "Clock": (SIGNAL_TRACK_WIDTH_MM, 0.2, ("ISP_SCK",)),
    "Debug": (SIGNAL_TRACK_WIDTH_MM, 0.15, ("RESET",)),
    "Digital": (SIGNAL_TRACK_WIDTH_MM, 0.15, ("NODE_A", "NODE_B", "NODE_C", "NODE_D")),
    "Power": (POWER_TRACK_WIDTH_MM, 0.2, ("GND", "VCC")),
}
#: net -> reviewed width, i.e. what the reviewed PCB and net_class_map.json agree on
REVIEWED_WIDTHS = {
    net: width for width, _clearance, nets in REVIEWED_CLASSES.values() for net in nets
}
EXPECTED_SEGMENTS = {
    "GND": 18,
    "VCC": 27,
    "ISP_SCK": 2,
    "LINE_A": 7,
    "LINE_B": 4,
    "LINE_C": 9,
    "LINE_D": 6,
    "NODE_A": 16,
    "NODE_B": 20,
    "NODE_C": 18,
    "NODE_D": 9,
    "RESET": 8,
}
ROUTED_SIDECARS = (
    "charlieplex_3x3_routed.kicad_pro",
    "charlieplex_3x3_routed.kicad_dru",
)

#: The five through-hole-to-SMD spacing pairs of the existing Board 02 warning
#: review: (sorted item ids, reported location, distance rounded to 1 um, floor).
REVIEWED_THT_SMD_FINDINGS = {
    (
        ("952137cb-6367-589a-bf72-41ce1a9f8f41", "c4ebcd42-772c-5e63-88bf-c3fccec88356"),
        (129.35, 121.438),
        2.425,
        3.05,
    ),
    (
        ("92fcdf7c-5c83-5053-8cae-03107bf2bd19", "952137cb-6367-589a-bf72-41ce1a9f8f41"),
        (130.206, 121.416),
        3.0024,
        3.05,
    ),
    (
        ("8ca2c009-383b-5834-8c6e-c153aab144fd", "bb59d3e0-70ca-58e3-85e6-8e82e12a2cb4"),
        (141.813, 118.95),
        2.5295,
        3.05,
    ),
    (
        ("1ab7b4f8-809c-51c6-be33-98d2a47234cb", "90c61153-9acf-58ca-8451-6aff8d392c1c"),
        (152.384, 119.025),
        2.2863,
        3.05,
    ),
    (
        ("2a762dac-9e64-504e-967e-5e0c65cff1af", "568f8156-8e82-52a7-a1b9-8b18a464a6a1"),
        (164.325, 127.54),
        2.6512,
        3.05,
    ),
}


def _load_recipe():
    sys.path.insert(0, str(BOARD_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "board02_generate_design_10663", BOARD_DIR / "generate_design.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(BOARD_DIR))


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assigned_classes(project: dict) -> dict[str, list[str]]:
    """``{net: [class, ...]}`` each reviewed net resolves to, schema 3 or 4/5."""
    assignments = project["net_settings"]["netclass_assignments"]
    resolved = {}
    for net in REVIEWED_WIDTHS:
        value = assignments[net]
        resolved[net] = [value] if isinstance(value, str) else list(value)
    return resolved


def _assert_reviewed_netclasses(project: dict) -> None:
    classes = {cls["name"]: cls for cls in project["net_settings"]["classes"]}
    patterns = {
        row["pattern"]: row["netclass"] for row in project["net_settings"]["netclass_patterns"]
    }
    from kicad_tools.core.project_clearance import authored_netclass_widths

    resolved = authored_netclass_widths(project, list(REVIEWED_WIDTHS))
    for name, (width, clearance, nets) in REVIEWED_CLASSES.items():
        assert classes[name]["track_width"] == width, f"{name} declares {classes[name]}"
        assert classes[name]["clearance"] == clearance, f"{name} declares {classes[name]}"
        assert {net: resolved[net].width for net in nets} == dict.fromkeys(nets, width), name
        assert {net: resolved[net].source_class for net in nets} == dict.fromkeys(nets, name)
        assert {net: patterns[net] for net in nets} == dict.fromkeys(nets, name), name
    assert _assigned_classes(project) == {
        net: [name] for name, (_w, _c, nets) in REVIEWED_CLASSES.items() for net in nets
    }


def _track_widths(pcb_path: Path) -> dict[str, list[float]]:
    """``{net: [width, ...]}`` of every routed copper segment, in file order."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    assert not pcb.arcs, "board 02 routes straight segments only"
    nets = {num: net.name for num, net in pcb.nets.items()}
    widths: dict[str, list[float]] = {net: [] for net in EXPECTED_SEGMENTS}
    for segment in pcb.segments:
        name = nets[segment.net_number]
        assert name in widths, f"unexpected routed net {name!r}"
        widths[name].append(round(segment.width, 4))
    return widths


def _assert_reviewed_copper(pcb_path: Path) -> None:
    widths = _track_widths(pcb_path)
    assert {net: len(found) for net, found in widths.items()} == EXPECTED_SEGMENTS, widths
    off_review = {
        net: [w for w in found if w != REVIEWED_WIDTHS[net]] for net, found in widths.items()
    }
    assert not any(off_review.values()), f"copper left its reviewed width: {widths}"


def _stage_routed_board(tmp_path: Path) -> Path:
    """The committed routed board and its sidecars, as the publisher stages them."""
    for name in ("charlieplex_3x3_routed.kicad_pcb", *ROUTED_SIDECARS):
        (tmp_path / name).write_bytes((OUTPUT / name).read_bytes())
    return tmp_path / "charlieplex_3x3_routed.kicad_pcb"


def test_recipe_authors_the_reviewed_netclasses(tmp_path: Path) -> None:
    """The recipe's project declares the five classes it reviewed and assigns their nets."""
    project_path = _load_recipe().create_project(tmp_path, "charlieplex_3x3")
    _assert_reviewed_netclasses(_load_json(project_path))


def test_committed_recipe_project_is_what_the_recipe_emits(tmp_path: Path) -> None:
    """``output/charlieplex_3x3.kicad_pro`` is the recipe's own output."""
    project_path = _load_recipe().create_project(tmp_path, "charlieplex_3x3")
    assert project_path.read_bytes() == RECIPE_PROJECT.read_bytes(), (
        "the committed recipe project is stale; regenerate it with "
        "boards/02-charlieplex-led/generate_design.py"
    )


def test_committed_routed_project_declares_the_reviewed_widths() -> None:
    """The shipped routed project carries the authored classes through ``kct route``."""
    _assert_reviewed_netclasses(_load_json(ROUTED_PROJECT))


def test_committed_routed_copper_is_the_reviewed_width() -> None:
    _assert_reviewed_copper(ROUTED_PCB)


def test_optimizer_reads_the_width_the_committed_copper_uses() -> None:
    """The widths ``optimize-traces`` takes from the project match every segment."""
    from kicad_tools.cli.optimize_cmd import _project_netclass_widths

    project_widths = _project_netclass_widths(
        ROUTED_PCB, str(ROUTED_PROJECT), list(REVIEWED_WIDTHS)
    )
    assert project_widths == REVIEWED_WIDTHS


def test_default_cleanup_preserves_the_reviewed_copper(tmp_path: Path) -> None:
    """The publisher's ``dedupe -> prune -> optimize-traces`` leaves the copper alone."""
    from kicad_tools.cli import readiness_cmd
    from kicad_tools.cli.readiness_cmd import ReadinessOptions

    staged = _stage_routed_board(tmp_path)
    before = staged.read_bytes()
    _assert_reviewed_copper(staged)

    options = ReadinessOptions(
        board_dir=tmp_path,
        pcb=staged,
        schematic=None,
        project=tmp_path / "charlieplex_3x3_routed.kicad_pro",
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
    assert steps["optimize_traces"]["segments_before"] == sum(EXPECTED_SEGMENTS.values())
    assert steps["optimize_traces"]["segments_after"] == sum(EXPECTED_SEGMENTS.values())

    _assert_reviewed_copper(staged)
    assert staged.read_bytes() == before, "default cleanup rewrote the reviewed copper"


def test_committed_receipt_keeps_the_five_reviewed_spacing_findings_traceable() -> None:
    """Each reviewed through-hole-to-SMD pair keeps its items, place and distance."""
    report = _load_json(OUTPUT / "readiness" / "kct-check.json")
    found = {
        (
            tuple(sorted(item["items"])),
            tuple(item["location"]),
            round(item["actual_value"], 4),
            item["required_value"],
        )
        for item in report["violations"]
        if item["type"] == "tht_smd_spacing"
    }
    assert found == REVIEWED_THT_SMD_FINDINGS, found ^ REVIEWED_THT_SMD_FINDINGS


def test_committed_release_evidence_is_the_reviewed_copper() -> None:
    """The shipped receipt was produced with cleanup on and retained every segment."""
    from kicad_tools.cli.board_readiness import read_readiness

    receipt = read_readiness(BOARD_DIR)
    assert receipt["status"] != "unverified", receipt["blockers"]
    cleanup = receipt["metrics"]["copper_cleanup"]
    assert cleanup["copper_cleanup"] == "applied", cleanup
    optimize = cleanup["steps"]["optimize_traces"]
    assert optimize["segments_after"] == sum(EXPECTED_SEGMENTS.values()), cleanup
