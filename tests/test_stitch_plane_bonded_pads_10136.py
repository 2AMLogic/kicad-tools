"""Power-plane stitching skips SMD pads already bonded to the plane by fill (#10136).

A four-layer board with the GNDA plane on In1.Cu and a surface GNDA pour on F.Cu.
C1.1 is a bare SMD pad on the pour.  Whether it needs a stitch depends on whether
the CURRENT F.Cu fill is physically in the same copper-contact component as the
In1.Cu plane (through the two existing vias at x=120.8 and x=130.8), not on the
mere presence of a zone outline or of any fill.  Every case is checked against a
native KiCad refill; the cases skip when KiCad 10 is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.cli.stitch_cmd import run_stitch

FIXTURE = Path(__file__).parent / "fixtures" / "stitch-5388" / "continuous.kicad_pcb"
SRC = Path(__file__).resolve().parents[1] / "src"

_BARE_SEGMENT = re.compile(
    r"\t\(segment\n\t\t\(start 110 110\)\n\t\t\(end 112 110\)\n.*?\n\t\)\n", re.DOTALL
)


@pytest.fixture
def native_cli():
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("find_kicad_cli() found no kicad-cli install; KiCad 10 is needed to refill")
    cli = str(cli)
    version = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=20)
    if version.returncode or not version.stdout.startswith("10."):
        pytest.skip("These cases require native KiCad 10")
    return cli


def _surface_zone(right_edge: float, connect: str) -> str:
    return f"""\t(zone
\t\t(net "GNDA")
\t\t(layer "F.Cu")
\t\t(uuid "11111111-2222-5333-8444-555555555555")
\t\t(hatch edge 0.5)
\t\t({connect}
\t\t\t(clearance 0.2)
\t\t)
\t\t(min_thickness 0.25)
\t\t(fill yes
\t\t\t(thermal_gap 0.5)
\t\t\t(thermal_bridge_width 0.5)
\t\t\t(island_removal_mode 0)
\t\t)
\t\t(polygon
\t\t\t(pts
\t\t\t\t(xy 105 105) (xy {right_edge} 105) (xy {right_edge} 115) (xy 105 115)
\t\t\t)
\t\t)
\t)
"""


def make_board(
    tmp_path: Path, *, right_edge: float = 135, connect: str = "connect_pads yes"
) -> Path:
    """Four-layer variant of the #5388 fixture with C1.1 bare on a surface pour."""
    text = FIXTURE.read_text().replace(
        '(2 "B.Cu" signal)', '(4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal)'
    )
    zone_start = text.index("\n\t(zone")
    text = text[:zone_start] + text[zone_start:].replace('(layer "B.Cu")', '(layer "In1.Cu")')
    text, removed = _BARE_SEGMENT.subn("", text)
    assert removed == 1
    text = text.rstrip()[:-1].rstrip() + "\n" + _surface_zone(right_edge, connect) + ")\n"
    board = tmp_path / "board.kicad_pcb"
    board.write_text(text)
    return board


def run_phase(name: str, command: list[str], **kwargs) -> None:
    started = time.monotonic()
    print(f"Starting {name} ({shlex.join(command)})", flush=True)
    subprocess.run(command, capture_output=True, text=True, check=True, **kwargs)
    print(f"Completed {name} in {time.monotonic() - started:.3f} s", flush=True)


def refill(board: Path, cli: str) -> dict:
    report = board.parent / "native.json"
    run_phase(
        f"native refill of {board.name}",
        [cli, "pcb", "drc", str(board), "--refill-zones", "--save-board", "--format", "json"]
        + ["-o", str(report)],
        timeout=180,
    )
    return json.loads(report.read_text())


def edit_zone(board: Path, layer: str, old: str, new: str) -> None:
    """Edit the zone on one layer, leaving its stored fill untouched.

    Native refill rewrites the file and may reorder zones, so find the block.
    """
    marker = "\t(zone\n"
    head, *zones = board.read_text().split(marker)
    (index,) = [i for i, z in enumerate(zones) if f'(layer "{layer}")' in z.split("(polygon")[0]]
    assert old in zones[index]
    zones[index] = zones[index].replace(old, new, 1)
    board.write_text(head + "".join(marker + z for z in zones))


def stitch(board: Path, **kwargs):
    return run_stitch(
        pcb_path=board,
        net_names=["GNDA"],
        via_size=0.6,
        drill=0.3,
        clearance=0.2,
        dry_run=True,
        **kwargs,
    )


def stitched_pads(result) -> set[str]:
    return {f"{v.pad.reference}.{v.pad.pad_number}" for v in result.vias_added}


def bonded_pads(result) -> dict[str, str]:
    return {f"{p.reference}.{p.pad_number}": why for p, why in result.pour_bonded}


@pytest.mark.parametrize(
    "connect", ["connect_pads yes", "connect_pads"], ids=["solid", "thermal-spokes"]
)
def test_pour_bond_reaching_the_plane_needs_no_stitch(tmp_path, native_cli, connect):
    board = make_board(tmp_path, connect=connect)
    assert refill(board, native_cli)["unconnected_items"] == []
    result = stitch(board)
    assert "C1.1" not in stitched_pads(result)
    assert "F.Cu" in bonded_pads(result)["C1.1"]
    assert "plane" in bonded_pads(result)["C1.1"]
    assert not result.vias_added
    assert not result.traces_added
    assert not [p for p, _ in result.pads_skipped]


def test_island_pour_still_gets_a_legal_stitch(tmp_path, native_cli):
    board = make_board(tmp_path, right_edge=115)
    assert len(refill(board, native_cli)["unconnected_items"]) == 1
    result = stitch(board)
    assert stitched_pads(result) == {"C1.1"}
    assert "C1.1" not in bonded_pads(result)


def test_forced_native_open_repair_outranks_the_bond(tmp_path, native_cli):
    board = make_board(tmp_path)
    refill(board, native_cli)
    result = stitch(board, force_pads=frozenset({"C1.1"}))
    assert stitched_pads(result) == {"C1.1"}
    assert "C1.1" not in bonded_pads(result)


def test_outline_without_fill_earns_no_skip(tmp_path, native_cli):
    board = make_board(tmp_path)  # never refilled: the zone is outline only
    assert "filled_polygon" not in board.read_text().split("(zone", 3)[-1]
    result = stitch(board)
    assert "C1.1" not in bonded_pads(result)


def test_stale_fill_after_outline_edit_earns_no_skip(tmp_path, native_cli):
    board = make_board(tmp_path)
    refill(board, native_cli)
    assert "C1.1" in bonded_pads(stitch(board))
    # The positive outline now ends before the vias but the old fill polygons remain.
    edit_zone(board, "F.Cu", "(xy 135 105) (xy 135 115)", "(xy 115 105) (xy 115 115)")
    result = stitch(board)
    assert "C1.1" not in bonded_pads(result)
    assert stitched_pads(result) == {"C1.1"}


def test_fill_overlapping_foreign_copper_is_stale(tmp_path, native_cli):
    board = make_board(tmp_path)
    refill(board, native_cli)
    text = board.read_text().rstrip()[:-1]
    text += '(segment (start 124 112) (end 126 112) (width 0.3) (layer "F.Cu") (net "SIG")))'
    board.write_text(text)  # routed after the refill: the fill still covers it
    result = stitch(board)
    assert "C1.1" not in bonded_pads(result)


def test_disabled_pad_connection_earns_no_skip(tmp_path, native_cli):
    board = make_board(tmp_path, connect="connect_pads no")
    assert len(refill(board, native_cli)["unconnected_items"]) == 1
    result = stitch(board)
    assert stitched_pads(result) == {"C1.1"}
    assert "C1.1" not in bonded_pads(result)


def test_stale_fill_with_pad_connection_disabled_earns_no_skip(tmp_path, native_cli):
    board = make_board(tmp_path)
    refill(board, native_cli)
    edit_zone(board, "F.Cu", "connect_pads yes", "connect_pads no")  # fill left as written
    result = stitch(board)
    assert "C1.1" not in bonded_pads(result)
    assert stitched_pads(result) == {"C1.1"}


def route_and_refill(board: Path, native_cli: str) -> tuple[int, int]:
    routed = board.with_name("routed.kicad_pcb")
    run_phase(
        "power-plane routing",
        [sys.executable, "-m", "kicad_tools.cli", "route", str(board)]
        + ["--layers", "4", "--stitch-power-planes", "--via-diameter", "0.6"]
        + ["--via-drill", "0.3", "--clearance", "0.2", "-o", str(routed)],
        timeout=240,
        cwd=board.parent,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    probe = board.with_name("after.kicad_pcb")
    shutil.copy2(routed, probe)
    after = refill(probe, native_cli)
    assert after["unconnected_items"] == []
    text = routed.read_text()
    segments = len(re.findall(r'\(segment\b[^"]*?\(layer "[^"]+"\)\s*\(net "GNDA"\)', text, re.S))
    vias = len(re.findall(r'\(via\b.*?\(net "GNDA"\)', text, re.S))
    return segments, vias


@pytest.mark.parametrize("island", [False, True], ids=["bonded", "island"])
def test_public_route_command_and_native_refill(tmp_path, native_cli, island):
    board = make_board(tmp_path, right_edge=115 if island else 135)
    before = refill(board, native_cli)
    assert len(before["unconnected_items"]) == (1 if island else 0)
    assert route_and_refill(board, native_cli) == ((3, 3) if island else (2, 2))


@pytest.mark.parametrize("refill_first", [False, True], ids=["stale", "refilled"])
def test_board_cutout_cannot_hide_a_required_stitch(tmp_path, native_cli, refill_first):
    board = make_board(tmp_path)
    # Widen only the inner plane so it runs around both ends of the new slot.
    edit_zone(
        board,
        "In1.Cu",
        "(xy 105 105) (xy 135 105) (xy 135 115) (xy 105 115)",
        "(xy 103 102) (xy 137 102) (xy 137 118) (xy 103 118)",
    )
    assert not refill(board, native_cli)["unconnected_items"]
    slot = (
        "(gr_rect (start 114 104) (end 116 116) (stroke (width 0.05) (type default)) "
        '(fill none) (layer "Edge.Cuts") (uuid "390472d5-0042-402b-b0a0-f9b2816164f2"))'
    )
    board.write_text(board.read_text().rstrip()[:-1] + slot + ")\n")
    probe = board.with_name("before.kicad_pcb")
    shutil.copyfile(board, probe)
    assert len(refill(probe, native_cli)["unconnected_items"]) == 1
    if refill_first:
        shutil.copyfile(probe, board)
    original = board.read_bytes()
    assert route_and_refill(board, native_cli) == (3, 3)
    assert board.read_bytes() == original


def test_failed_refill_uses_the_project_copy_and_withholds_the_bond(
    tmp_path, native_cli, monkeypatch
):
    from kicad_tools.cli import runner

    board = make_board(tmp_path)
    refill(board, native_cli)
    project = board.with_suffix(".kicad_pro")
    rules = board.with_suffix(".kicad_dru")
    project.write_text('{"board": {}}')
    rules.write_text("(version 1)\n")
    original = {p: p.read_bytes() for p in (board, project, rules)}
    attempted = []

    def fail_on_copy(copy, *, require_save):
        assert require_save and copy.parent != board.parent and copy.name == board.name
        for path, content in original.items():
            assert copy.with_suffix(path.suffix).read_bytes() == content
        attempted.append(copy)
        copy.write_text("partial native write")
        return runner.KiCadCLIResult(success=False, stderr="injected refill failure")

    monkeypatch.setattr(runner, "run_refill_zones", fail_on_copy)
    result = stitch(board)
    assert attempted and not attempted[0].exists()
    assert "C1.1" not in bonded_pads(result)
    assert stitched_pads(result) == {"C1.1"}
    assert all(path.read_bytes() == content for path, content in original.items())


_KEEPOUT = """\t(zone
\t\t(net 0)
\t\t(net_name "")
\t\t(layer "F.Cu")
\t\t(uuid "99999999-2222-5333-8444-555555555555")
\t\t(hatch edge 0.5)
\t\t(connect_pads (clearance 0))
\t\t(min_thickness 0.25)
\t\t(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
\t\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
\t\t(polygon (pts (xy 114 104) (xy 116 104) (xy 116 116) (xy 114 116)))
\t)
"""


def test_pour_keepout_added_after_refill_invalidates_the_saved_bond(tmp_path, native_cli):
    board = make_board(tmp_path)
    refill(board, native_cli)
    assert "C1.1" in bonded_pads(stitch(board))
    text = board.read_text().rstrip()[:-1].rstrip() + "\n" + _KEEPOUT + ")\n"
    board.write_text(text)  # carves the surface path on the next refill; fill left stale
    result = stitch(board, target_layer="In1.Cu")
    assert "C1.1" not in bonded_pads(result)


def test_isolated_surface_island_is_not_its_own_plane_regardless_of_zone_order(
    tmp_path, native_cli
):
    board = make_board(tmp_path, right_edge=115)
    refill(board, native_cli)
    text = board.read_text()
    marker = "\t(zone\n"
    head, *zones = text.split(marker)
    tail = ")\n"
    zones[-1] = zones[-1].rstrip()[:-1].rstrip() + "\n"  # drop the board's closing paren
    for ordering in (zones, zones[::-1]):
        board.write_text(head + "".join(marker + z for z in ordering) + tail)
        assert "C1.1" not in bonded_pads(stitch(board))
