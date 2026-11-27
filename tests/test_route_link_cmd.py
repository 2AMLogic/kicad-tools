"""``kct route-link``: targeted single-link routing and probe (Issue #8310)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("shapely")
from shapely.geometry import Point, box  # noqa: E402

from kicad_tools.cli import main  # noqa: E402
from kicad_tools.cli.route_link_cmd import LinkSpecError, parse_link_spec  # noqa: E402
from kicad_tools.native_concurrency import configured_limit, slot_wait_ceiling  # noqa: E402
from kicad_tools.router.link_router import LinkJunction, LinkRoute, append_link_routes  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402

# R1.1 (110,110) and R2.1 (120,110) are on SIG.  A foreign F.Cu+B.Cu-free wall
# (a FOE track on F.Cu, x=115, y 100..120) splits F.Cu; B.Cu is open.
BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "FOE")
  (gr_rect (start 100 100) (end 130 120) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000001") (at 110 110)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG")))
  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000002") (at 120 110)
    (fp_text reference "R2" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG")))
  (segment (start 115 100.6) (end 115 119.4) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000010"))
)
"""
OFFSET_PAD_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SENSE")
  (gr_rect (start 1 1) (end 21 19) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "P" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000001") (at 6 8)
    (fp_text reference "P1" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SENSE")))
  (footprint "P" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000002") (at 12 14)
    (fp_text reference "P2" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SENSE")))
  (footprint "P" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000003") (at 12 9 {angle})
    (fp_text reference "P3" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" thru_hole {shape} (at 0 0 {angle}) (size 2 2) (drill 0.4 (offset 0.6 0)) {rounding} (layers "*.Cu" "*.Mask") (net 1 "SENSE")))
  (segment (start 6 8) (end 12 14) (width 0.25) (layer "F.Cu") (net 1) (uuid "00000000-0000-0000-0000-000000000010"))
)
"""
LINK = "SIG R1.1 R2.1"

# Tests that launch kicad-cli through `_native_plane_link` queue for one of the
# Test job's two native permits.  The job's 60 s default (`--timeout=60`) was
# measured at 57.72 s / 46.42 s for these tests and cannot absorb the queue, so
# the budget is composed like tests/test_route_partial_placement_cli.py: the
# test's own work plus the gate's fail-open queue bound, only when the gate is
# configured (local runs keep the tight historical bound).
_NATIVE_WORK_SECONDS = 60.0
_NATIVE_QUEUE_CEILING_SECONDS = slot_wait_ceiling() if configured_limit() is not None else 0.0
native_launching = pytest.mark.timeout(_NATIVE_WORK_SECONDS + _NATIVE_QUEUE_CEILING_SECONDS)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(BOARD)
    return p


# Issue #10486: P1.1 (5,4), P2.1 (5,14) and P3.1 (12,9) are 1 x 1 mm SMD pads
# on SENSE.  The outline starts at (1,1), so the file's millimetres differ from
# the model's board-relative ones by the board origin.
TEE_PADS = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SENSE")
  (gr_rect (start 1 1) (end 21 19) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "P" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000001") (at 5 4)
    (fp_text reference "P1" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SENSE")))
  (footprint "P" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000002") (at 5 14)
    (fp_text reference "P2" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SENSE")))
  (footprint "P" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000003") (at 12 9)
    (fp_text reference "P3" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SENSE")))
"""
TEE_SMD = '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")'
TEE_THH = '(pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.4) (layers "*.Cu" "*.Mask")'
#: P1.1 -> P2.1: the track a branch from P3.1 can join.
TEE_TRACK = (
    '(segment (start 5 4) (end 5 14) (width 0.25) (layer "F.Cu") (net 1) '
    '(uuid "00000000-0000-0000-0000-000000000010"))'
)
#: Same net and nearer to P3.1, but connected to nothing.
TEE_ISLAND = (
    '(segment (start 9 5) (end 9 7) (width 0.25) (layer "F.Cu") (net 1) '
    '(uuid "00000000-0000-0000-0000-000000000020"))'
)
TEE_TRACK_B = TEE_TRACK.replace('"F.Cu"', '"B.Cu"').replace("000000000010", "000000000030")
TEE_TUNE = ("--trace-width", ".25", "--clearance", ".2")
TEE_OPTS = ("--link-layers", "F.Cu", *TEE_TUNE)
TEE_ARGS = ("--link", "SENSE P3.1 P2.1", *TEE_OPTS)
TEE_HOST_UUID = "00000000-0000-0000-0000-000000000010"
#: A fourth stranded pad, west of the track, for a second link onto the same track.
TEE_PAD4 = """  (footprint "P" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000004") (at 2 12)
    (fp_text reference "P4" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SENSE")))
"""
#: A foreign F.Cu wall between P3.1 and the track, open only past its ends.
TEE_WALL = (
    '(segment (start 8 2) (end 8 16) (width 0.2) (layer "F.Cu") (net 2) '
    '(uuid "00000000-0000-0000-0000-000000000040"))'
)


def _tee(
    tmp_path: Path, *tracks: str, thru_hole: bool = False, extra_pads: str = "", nets: str = ""
) -> Path:
    """The three-pad board plus ``nets``, ``extra_pads`` and ``tracks``.

    ``thru_hole`` swaps THT pads in.
    """
    pads = TEE_PADS.replace(TEE_SMD, TEE_THH) if thru_hole else TEE_PADS
    out = tmp_path / "tee.kicad_pcb"
    body = pads.replace('  (net 1 "SENSE")\n', '  (net 1 "SENSE")\n' + nets)
    out.write_text(body + extra_pads + "".join(f"  {track}\n" for track in tracks) + ")\n")
    return out


@pytest.fixture
def tee(tmp_path: Path) -> Path:
    return _tee(tmp_path, TEE_TRACK)


def _plated_destination(tmp_path: Path, host: str, pad: str, source_y: float = 9) -> Path:
    """Three plated pads with P2's front and back lands specified independently."""
    board = _tee(tmp_path, host, thru_hole=True)
    before, p2 = board.read_text().split('(fp_text reference "P2"')
    board.write_text(
        (before + '(fp_text reference "P2"' + p2.replace(TEE_THH, pad, 1))
        .replace("(at 12 9)", f"(at 12 {source_y})")
        .replace('(31 "B.Cu" signal)', '(1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)')
    )
    return board


def _file_segments(path: Path) -> list[tuple]:
    """``(start, end, width, layer, uuid)`` per SENSE track, in the file's millimetres."""
    pcb = PCB.load(str(path))
    ox, oy = pcb.board_origin
    return [
        (
            (round(s.start[0] + ox, 6), round(s.start[1] + oy, 6)),
            (round(s.end[0] + ox, 6), round(s.end[1] + oy, 6)),
            s.width,
            s.layer,
            s.uuid,
        )
        for s in sorted((s for s in pcb.segments if s.net_number == 1), key=lambda s: s.start)
    ]


def _run(capsys, *argv: str) -> tuple[int, dict]:
    rc = main(["route-link", "--format", "json", *argv])
    out = capsys.readouterr().out
    return rc, json.loads(out)


class TestParse:
    def test_pad_and_xy_with_layers(self):
        s = parse_link_spec("GND U1.3 12.5,7@B.Cu")
        assert (s.net, s.a.ref, s.a.pad) == ("GND", "U1", "3")
        assert s.b.xy == (12.5, 7.0) and s.b.layer == "B.Cu"

    @pytest.mark.parametrize("bad", ["SIG R1.1", "SIG R1 R2.1", "SIG 1,x R2.1", "SIG R1.1@ R2.1"])
    def test_bad_specs(self, bad):
        with pytest.raises(LinkSpecError):
            parse_link_spec(bad)


class TestProbe:
    def test_probe_leaves_board_bytes_unchanged(self, board, capsys):
        before = board.read_bytes()
        rc, doc = _run(capsys, str(board), "--link", LINK, "--probe")
        assert rc == 0 and doc["ok"] and doc["written"] is None
        link = doc["links"][0]
        assert link["routable"] and link["length_mm"] >= 9.0
        assert link["vias"] == 2 and link["layers"] == ["B.Cu", "F.Cu"]
        assert board.read_bytes() == before

    def test_layer_restriction_blocks_and_reports_blocker(self, board, capsys):
        before = board.read_bytes()
        rc, doc = _run(capsys, str(board), "--link", LINK, "--link-layers", "F.Cu", "--probe")
        assert rc == 1 and not doc["ok"]
        link = doc["links"][0]
        assert not link["routable"]
        assert link["blocker"]["net"] == "FOE" and link["blocker"]["layer"] == "F.Cu"
        assert link["blocker"]["distance_mm"] < link["blocker"]["required_mm"]
        assert board.read_bytes() == before

    def test_back_layer_only_has_no_endpoint_copper(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", LINK, "--link-layers", "B.Cu", "--probe")
        assert rc == 1
        assert "no copper on the allowed layers" in doc["links"][0]["reason"]

    def test_endpoint_layer_not_in_link_layers(self, board, capsys):
        rc, doc = _run(
            capsys, str(board), "--link", "SIG R1.1@B.Cu R2.1", "--link-layers", "F.Cu", "--probe"
        )
        assert rc == 1 and "no copper on layer B.Cu" in doc["links"][0]["reason"]

    def test_xy_endpoint_without_copper(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", "SIG 105,105 R2.1", "--probe")
        assert rc == 1 and "no 'SIG' via or track" in doc["links"][0]["reason"]

    def test_wrong_net_pad(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", "FOE R1.1 R2.1", "--probe")
        assert rc == 1 and "is on net" in doc["links"][0]["reason"]

    # --join-track lands on a track electrically connected to the destination
    # pad, outside every pad, instead of entering the pad (Issue #10486).
    def test_join_track_three_probes(self, tee, capsys):
        before = tee.read_bytes()
        _rc, named = _run(capsys, str(tee), *TEE_ARGS, "--probe")
        _rc, manual = _run(
            capsys,
            str(tee),
            "--link",
            "SENSE P3.1 5,9@F.Cu",
            "--link-layers",
            "F.Cu",
            "--trace-width",
            ".25",
            "--clearance",
            ".2",
            "--probe",
        )
        rc, joined = _run(capsys, str(tee), *TEE_ARGS, "--join-track", "--probe")
        pad, point, branch = named["links"][0], manual["links"][0], joined["links"][0]
        assert rc == 0 and joined["written"] is None and tee.read_bytes() == before
        # Without the opt-in both existing forms are unchanged and report no junction.
        assert pad["routable"] and pad["length_mm"] == 7.902 and "junction" not in pad
        assert point["routable"] and point["length_mm"] == 6.5 and "junction" not in point
        assert branch["routable"] and branch["vias"] == 0 and branch["layers"] == ["F.Cu"]
        assert branch["length_mm"] < pad["length_mm"]
        # The foot of the perpendicular from P3.1 (12,9): inside both pads' gap.
        assert branch["junction"] == {"x": 5.0, "y": 9.0, "layer": "F.Cu"}

    def test_join_track_ignores_closer_disconnected_island(self, tmp_path, capsys):
        island = _tee(tmp_path, TEE_TRACK, TEE_ISLAND)
        rc, doc = _run(capsys, str(island), *TEE_ARGS, "--join-track", "--probe")
        link = doc["links"][0]
        assert rc == 0 and link["routable"]
        assert link["junction"] == {"x": 5.0, "y": 9.0, "layer": "F.Cu"}

    def test_join_track_without_eligible_track_keeps_the_pad_route(self, tmp_path, capsys):
        island = _tee(tmp_path, TEE_ISLAND)  # the only SENSE track is an island
        _rc, plain = _run(capsys, str(island), *TEE_ARGS, "--probe")
        rc, doc = _run(capsys, str(island), *TEE_ARGS, "--join-track", "--probe")
        link, baseline = doc["links"][0], plain["links"][0]
        assert rc == 0 and link["routable"]
        assert "junction" not in link and link["join_track_skipped"]
        assert link["length_mm"] == baseline["length_mm"]
        assert link["layers"] == baseline["layers"] and link["vias"] == baseline["vias"]

    def test_join_track_falls_back_when_no_branch_to_the_track_routes(self, tmp_path, capsys):
        wall = _tee(tmp_path, TEE_TRACK, TEE_WALL, nets='  (net 2 "FOE")\n')
        rc, doc = _run(capsys, str(wall), *TEE_ARGS, "--join-track", "--probe")
        link = doc["links"][0]
        assert rc == 0 and link["routable"] and link["layers"] == ["F.Cu"]
        # The pad route still gets around the wall's end; a branch to the track
        # does not, so the pad route is kept and the entry says why.
        assert "junction" not in link
        assert link["join_track_skipped"].startswith("no routable branch ends on a track")

    def test_join_track_does_not_park_a_via_on_the_junction(self, tmp_path, capsys):
        # P1/P2 are through-hole, so the back-layer track is connected to P2.1;
        # P3.1 is SMD, so a branch can only reach that track through a via.
        mixed = tmp_path / "tee.kicad_pcb"
        mixed.write_text(TEE_PADS.replace(TEE_SMD, TEE_THH, 2) + f"  {TEE_TRACK_B}\n)\n")
        rc, doc = _run(
            capsys, str(mixed), "--link", "SENSE P3.1 P2.1", *TEE_TUNE, "--join-track", "--probe"
        )
        link = doc["links"][0]
        assert rc == 0 and link["routable"]
        assert "junction" not in link and link["layers"] == ["F.Cu"] and link["vias"] == 0
        assert link["join_track_skipped"].startswith("no routable branch ends on a track")

    def test_join_track_respects_endpoint_layer_and_allowed_layers(self, tmp_path, capsys):
        back = _tee(tmp_path, TEE_TRACK_B, thru_hole=True)

        def probe(dest: str, *extra: str) -> dict:
            return _run(
                capsys,
                str(back),
                "--link",
                f"SENSE P3.1 {dest}",
                "--trace-width",
                ".25",
                "--clearance",
                ".2",
                "--join-track",
                "--probe",
                *extra,
            )[1]["links"][0]

        on_back = probe("P2.1@B.Cu")
        assert on_back["junction"] == {"x": 5.0, "y": 9.0, "layer": "B.Cu"}
        assert on_back["layers"] == ["B.Cu"] and on_back["vias"] == 0
        # The one connected track is on B.Cu: neither form may borrow it.
        for skipped in (probe("P2.1@F.Cu"), probe("P2.1", "--link-layers", "F.Cu")):
            assert skipped["routable"] and skipped["layers"] == ["F.Cu"]
            assert "junction" not in skipped and skipped["join_track_skipped"]

    def test_join_track_needs_a_pad_destination(self, tee, capsys):
        rc, doc = _run(capsys, str(tee), "--link", "SENSE P3.1 5,9@F.Cu", "--join-track", "--probe")
        assert rc == 1 and "--join-track" in doc["links"][0]["reason"]
        assert not doc["links"][0]["routable"]

    def test_join_track_must_follow_link(self, tee):
        with pytest.raises(SystemExit):
            main(["route-link", str(tee), "--join-track", "--link", "SENSE P3.1 P2.1"])


class TestCommit:
    @pytest.fixture(autouse=True)
    def _stub_gate(self, monkeypatch):
        # The per-link DRC/creepage gate is covered in test_route_link_gating.py.
        import contextlib
        from collections import Counter

        from kicad_tools.cli import kicad_toolchain, route_link_cmd

        monkeypatch.setattr(
            kicad_toolchain, "evidence_toolchain", lambda *a, **k: contextlib.nullcontext()
        )
        monkeypatch.setattr(route_link_cmd, "_gate_state", lambda path, args: (Counter(), None))

    def test_commit_writes_copper(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", LINK)
        assert rc == 0 and doc["written"] == str(board)
        pcb = PCB.load(str(board))
        sig = [s for s in pcb.segments if s.net_number == 1]
        assert sig and len(pcb.vias) == 2
        assert {s.layer for s in sig} == {"F.Cu", "B.Cu"}

    @pytest.mark.parametrize("offset", [False, True], ids=["roundrect", "offset-plated"])
    def test_join_track_starts_on_source_land(self, tmp_path, capsys, offset):
        from shapely.geometry import Point, box

        host = TEE_TRACK.replace("start 5 4", "start 6 8").replace("end 5 14", "end 12 14")
        board = _tee(tmp_path, host)
        prefix, source = board.read_text().rsplit(TEE_SMD, 1)
        if offset:
            pad = '(pad "1" thru_hole rect (at 0 0) (size 2 2) (drill 0.4 (offset 0.6 0)) (layers "*.Cu" "*.Mask")'
            land = box(11.6, 8, 13.6, 10)
        else:
            pad = '(pad "1" smd roundrect (at 0 0) (size 2 2) (roundrect_rratio 0.25) (layers "F.Cu" "F.Mask")'
            land = box(11.5, 8.5, 12.5, 9.5).buffer(0.5)
        board.write_text(
            prefix.replace("(at 5 4)", "(at 6 8)").replace("(at 5 14)", "(at 12 14)") + pad + source
        )
        out = tmp_path / "saved.kicad_pcb"
        probe = _joined(capsys, board, "--join-track", "--link-layers", "F.Cu", "--probe")
        saved = _joined(capsys, board, "--join-track", "--link-layers", "F.Cu", "-o", str(out))
        assert saved["status"] == "committed"
        assert saved["length_mm"] == probe["length_mm"]
        centre_x = 12.6 if offset else 12.0
        # Perpendicular foot on the host line y = x + 2 from the plotted land centre.
        junction_x = (centre_x + 9.0 - 2.0) / 2
        assert (
            saved["junction"]
            == probe["junction"]
            == {"x": junction_x, "y": junction_x + 2.0, "layer": "F.Cu"}
        )
        assert any(land.contains(Point(start)) for start, *_ in _file_segments(out))

    def test_join_track_splits_the_host_and_adds_the_branch(self, tee, capsys):
        rc, doc = _run(capsys, str(tee), *TEE_ARGS, "--join-track")
        assert rc == 0 and doc["written"] == str(tee)
        assert doc["links"][0]["status"] == "committed"
        junction = doc["links"][0]["junction"]
        assert junction == {"x": 5.0, "y": 9.0, "layer": "F.Cu"}
        first, second, branch = _file_segments(tee)
        # Both halves keep the original endpoints, width, net and layer, and
        # meet the branch exactly at the reported junction.
        assert first[:2] == ((5, 4), (5, 9)) and second[:2] == ((5, 9), (5, 14))
        assert branch[0] == (11.55, 9) and branch[1] == (junction["x"], junction["y"])
        assert {(s[2], s[3]) for s in (first, second, branch)} == {(0.25, "F.Cu")}
        assert first[4] == TEE_HOST_UUID  # the half with the original start keeps it
        assert len({s[4] for s in (first, second, branch)}) == 3

    @pytest.mark.parametrize(
        "layer,mode",
        [("B.Cu", "front_inner_back"), ("In1.Cu", "front_inner_back"), ("In1.Cu", "custom")],
    )
    @pytest.mark.parametrize("diameter,connected", [(0.5, False), (1.5, True)])
    def test_join_track_contacts_the_plated_land_on_the_host_layer(
        self, tmp_path, capsys, layer, mode, diameter, connected
    ):
        from shapely.geometry import LineString, Point

        inner = "Inner" if mode == "front_inner_back" else "In1.Cu"
        pad = (
            '(pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.3) (layers "*.Cu" "*.Mask") '
            f'(padstack (mode {mode}) (layer "{inner}" (shape circle) (size {diameter} {diameter})) '
            f'(layer "B.Cu" (shape circle) (size {diameter} {diameter})))'
        )
        host = TEE_TRACK.replace("start 5 4", "start 5.68 14").replace("end 5 14", "end 11 9")
        board = _plated_destination(tmp_path, host.replace("F.Cu", layer), pad)
        output = tmp_path / "saved.kicad_pcb"
        link = _joined(capsys, board, "--join-track", "--link-layers", layer, "-o", str(output))
        assert link["status"] == "committed"
        branch = [
            LineString([a, b]) for a, b, _, _, uid in _file_segments(output) if uid != TEE_HOST_UUID
        ]
        if connected:
            assert link["junction"] == {"x": 11.0, "y": 9.0, "layer": layer}
            assert any(line.distance(Point(11, 9)) == 0 for line in branch)
        else:
            assert "junction" not in link and link["join_track_skipped"]
            assert min(line.distance(Point(5, 14)) for line in branch) <= diameter / 2 + 0.125

    @pytest.mark.parametrize("back_size,source_y", [(1, 13.35), (2, 13.35), (2, 12.65)])
    def test_join_track_clears_the_plated_back_land_and_its_mask(
        self, tmp_path, capsys, back_size, source_y
    ):
        from shapely.geometry import LineString, box

        pad = (
            '(pad "1" thru_hole rect (at 0 0) (size 1 1) (drill 0.3) (layers "*.Cu" "*.Mask") '
            '(padstack (mode front_inner_back) (layer "Inner" (shape rect) (size 1 1)) '
            f'(layer "B.Cu" (shape rect) (size {back_size} {back_size})))'
        )
        board = _plated_destination(tmp_path, TEE_TRACK_B, pad, source_y)
        output = tmp_path / "saved.kicad_pcb"
        link = _joined(capsys, board, "--join-track", "--link-layers", "B.Cu", "-o", str(output))
        assert link["status"] == "committed"
        if back_size == 1 or source_y == 12.65:
            assert link["junction"] == {"x": 5.0, "y": source_y, "layer": "B.Cu"}
        if "junction" in link:
            half = back_size / 2
            opening = box(5 - half, 14 - half, 5 + half, 14 + half)
            host = LineString([(5, 4), (5, 14)])
            branch = [
                LineString([a, b]).buffer(w / 2)
                for a, b, w, _, _ in _file_segments(output)
                if not host.covers(LineString([a, b]))
            ]
            assert branch and all(g.intersection(opening).area < 1e-9 for g in branch)
        else:
            assert link["join_track_skipped"]

    def test_second_join_track_link_splits_the_piece_it_lands_on(self, tmp_path, capsys):
        board = _tee(tmp_path, TEE_TRACK, extra_pads=TEE_PAD4)
        rc, doc = _run(
            capsys,
            str(board),
            *TEE_ARGS,
            "--join-track",
            "--link",
            "SENSE P4.1 P2.1",
            *TEE_OPTS,
            "--join-track",
        )
        assert rc == 0 and [e["status"] for e in doc["links"]] == ["committed"] * 2
        assert [e["junction"]["y"] for e in doc["links"]] == [9.0, 12.0]
        pieces = _file_segments(board)
        # The first link already halved the track: the second must split the
        # half it lands on, leaving three pieces that only touch end to end.
        assert sorted(p[:2] for p in pieces) == sorted(
            [
                ((5, 4), (5, 9)),
                ((5, 9), (5, 12)),
                ((5, 12), (5, 14)),
                ((11.55, 9), (5, 9)),
                ((2.45, 12), (5, 12)),
            ]
        )
        assert len({p[4] for p in pieces}) == 5

    @pytest.mark.parametrize(
        ("angle", "shape", "source_flash"),
        [
            (90, "rect", box(11, 7.4, 13, 9.4)),
            (0, "rect", box(11.6, 8, 13.6, 10)),
            (-90, "rect", box(11, 8.6, 13, 10.6)),
            (90, "circle", Point(12, 8.4).buffer(1)),
            (90, "roundrect", box(11.5, 7.9, 12.5, 8.9).buffer(0.5)),
        ],
    )
    def test_offset_pad_link_lands_on_both_named_pads(
        self, tmp_path, capsys, angle, shape, source_flash
    ):
        board = tmp_path / "offset.kicad_pcb"
        out = tmp_path / "saved.kicad_pcb"
        board.write_text(
            OFFSET_PAD_BOARD.format(
                angle=angle,
                shape=shape,
                rounding="(roundrect_rratio 0.25)" if shape == "roundrect" else "",
            )
        )
        before = board.read_bytes()
        args = (
            str(board),
            "--link",
            "SENSE P3.1 P2.1",
            "--link-layers",
            "F.Cu",
            "--trace-width",
            ".25",
            "--clearance",
            ".2",
        )
        rc, doc = _run(capsys, *args, "--probe")
        assert rc == 0 and doc["links"][0]["routable"]
        assert board.read_bytes() == before
        probe_length = doc["links"][0]["length_mm"]
        rc, doc = _run(capsys, *args, "-o", str(out))
        assert rc == 0 and doc["links"][0]["status"] == "committed"
        assert doc["links"][0]["length_mm"] == probe_length
        assert board.read_bytes() == before

        pcb = PCB.load(str(out))
        ox, oy = pcb.board_origin
        original = PCB.load(str(board)).segments[0]
        added = [s for s in pcb.segments if s.uuid != original.uuid]
        assert added and all(s.layer == "F.Cu" and s.net_number == 1 for s in added)
        assert not pcb.vias
        # Board-file coordinates of the source flash and P2.1. A connection
        # ending on the existing diagonal alone would also clear the open.
        ends = [Point(x + ox, y + oy) for s in added for x, y in (s.start, s.end)]
        assert any(source_flash.covers(p) for p in ends)
        destination_flash = box(11.5, 13.5, 12.5, 14.5)
        assert any(destination_flash.covers(p) for p in ends)

    @pytest.mark.parametrize("width", [0.28, 0.22])
    @pytest.mark.parametrize(
        "pad",
        [
            "smd roundrect (at 0 0) (size {width} 1) (roundrect_rratio 0) "
            "(chamfer_ratio 0.2) (chamfer top_left top_right)",
            "smd trapezoid (at 0 0) (size {width} 1) (rect_delta 0.08 0)",
        ],
        ids=["chamfered", "trapezoid"],
    )
    def test_narrow_destination_between_grid_columns(self, tmp_path, capsys, pad, width):
        # Remove the foreign wall and place R2 half a grid step east of R1.
        board = tmp_path / "off_grid.kicad_pcb"
        two_pads = BOARD.partition("  (segment ")[0] + ")\n"
        before, r2 = two_pads.split("(at 120 110)")
        r2 = r2.replace("smd rect (at 0 0) (size 0.8 0.8)", pad.format(width=width))
        board.write_text(before + "(at 110.025 115)" + r2)
        args = (str(board), "--link", LINK, "--link-layers", "F.Cu", "--trace-width", ".2")
        rc, probe = _run(capsys, *args, "--probe")
        assert rc == 0 and probe["links"][0]["routable"]
        out = tmp_path / "routed.kicad_pcb"
        rc, written = _run(capsys, *args, "-o", str(out))
        assert rc == 0 and written["links"][0]["status"] == "committed"

        ends = [Point(point) for start, end, *_ in _file_segments(out) for point in (start, end)]
        # This central rectangle lies inside both the chamfer and the trapezoid.
        assert any(box(109.935, 114.55, 110.115, 115.45).covers(p) for p in ends)

    def test_output_file_leaves_input_alone(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        before = board.read_bytes()
        rc, _ = _run(capsys, str(board), "--link", LINK, "-o", str(out))
        assert rc == 0 and board.read_bytes() == before and out.exists()

    @pytest.mark.parametrize(
        ("lower_edge", "status"),
        [("109.900001", "unroutable"), ("109.9", "rolled-back")],
    )
    def test_refused_corridor_preserves_existing_output(
        self, plane_board, capsys, monkeypatch, lower_edge, status
    ):
        from kicad_tools.cli import route_link_cmd
        from kicad_tools.router.plane_corridor import PlaneCheckError

        out = plane_board.with_name("output.kicad_pcb")
        original = plane_board.read_bytes() + b"\n"
        out.write_bytes(original)

        def veto(*args):
            raise PlaneCheckError("stranded island")

        monkeypatch.setattr(route_link_cmd, "_native_refill", PCB.load)
        monkeypatch.setattr(route_link_cmd, "_verify_corridor_candidate", veto)
        rc, doc = _run(
            capsys,
            str(plane_board),
            "-o",
            str(out),
            "--link",
            LINK,
            "--link-layers",
            "In1.Cu",
            "--plane-corridor",
            f"In1.Cu=109,{lower_edge},121,110.1",
        )
        assert rc == 1 and doc["written"] is None
        assert doc["links"][0]["status"] == status
        if status == "rolled-back":
            assert doc["links"][0]["reason"] == "stranded island"
        assert out.read_bytes() == original

    def test_via_at_forces_waypoint(self, board, capsys):
        rc, doc = _run(capsys, str(board), "--link", LINK, "--via-at", "112,114", "--probe")
        assert rc == 0, doc
        # forced via plus the two the wall requires
        assert doc["links"][0]["vias"] >= 1
        _rc, _doc = _run(capsys, str(board), "--link", LINK, "--via-at", "112,114")
        pcb = PCB.load(str(board))
        ox, oy = pcb.board_origin
        assert any(
            abs(v.position[0] + ox - 112) < 0.02 and abs(v.position[1] + oy - 114) < 0.02
            for v in pcb.vias
        )

    def test_via_at_illegal_site(self, board, capsys):
        # on top of the foreign wall
        rc, doc = _run(capsys, str(board), "--link", LINK, "--via-at", "115,110", "--probe")
        assert rc == 1 and "via site" in doc["links"][0]["reason"]

    def test_via_at_must_follow_link(self, board):
        with pytest.raises(SystemExit):
            main(["route-link", str(board), "--via-at", "1,1", "--link", LINK])

    def test_no_link_is_error(self, board, capsys):
        rc, doc = _run(capsys, str(board))
        assert rc == 1 and not doc["ok"]


class TestJoinTrackWrite:
    """The split the route writer applies for a junction (Issue #10486)."""

    @staticmethod
    def _write(board: Path, **junction: object) -> str:
        """Write one branch whose junction is ``junction`` over the defaults."""
        host: dict[str, object] = {
            "point": (4.0, 8.0),
            "layer": "F.Cu",
            "start": (4.0, 3.0),
            "end": (4.0, 13.0),
            "width": 0.25,
            "uuid": TEE_HOST_UUID,
        }
        j = LinkJunction(**(host | junction))  # type: ignore[arg-type]
        route = LinkRoute(
            1,
            "SENSE",
            0.25,
            0.6,
            0.3,
            segments=[(10.55, 8.0, j.point[0], j.point[1], "F.Cu")],
            junction=j,
        )
        append_link_routes(board, [route], (1.0, 1.0))
        return board.read_text()

    def test_junction_on_an_existing_endpoint_needs_no_split(self, tmp_path):
        board = _tee(tmp_path, TEE_TRACK)
        out = self._write(board, point=(4.0, 3.0))
        assert out.count("(segment") == 2 and out.count(TEE_HOST_UUID) == 1
        host, branch = _file_segments(board)
        assert host[:2] == ((5, 4), (5, 14)) and branch[1] == (5, 4)

    def test_split_keeps_the_board_s_net_dialect(self, tmp_path):
        board = _tee(tmp_path, TEE_TRACK.replace("(net 1)", '(net "SENSE")'))
        out = self._write(board)
        written = re.split(r"\(segment\b", out)[1:]
        assert len(written) == 3
        assert all('(net "SENSE")' in s and "(net 1)" not in s for s in written)
        uuids = re.findall(r'\(uuid "([^"]+)"\)', "".join(written))
        assert len(set(uuids)) == 3 and TEE_HOST_UUID in uuids


# Issue #10486 review: electrical membership, pad avoidance, chained links.
FOUR_LAYERS = '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))'
_TWO_LAYERS = '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))'


def _via(layers: str, *, at: str = "5 14", kind: str = "") -> str:
    return (
        f"(via {kind} (at {at}) (size 0.6) (drill 0.3) (layers {layers}) (net 1) "
        '(uuid "00000000-0000-0000-0000-0000000000a0"))'
    )


def _p3_through_hole(path: Path) -> None:
    text = path.read_text()
    i = text.index("(at 12 9)")
    j = text.index(TEE_SMD, i)
    path.write_text(text[:j] + TEE_THH + text[j + len(TEE_SMD) :])


def _joined(capsys, board: Path, *extra: str) -> dict:
    _rc, doc = _run(capsys, str(board), "--link", "SENSE P3.1 P2.1", *TEE_TUNE, *extra)
    return doc["links"][0]


class TestJoinTrackReview:
    @pytest.mark.parametrize("diameter,connected", [(0.5, False), (1.5, True)])
    @pytest.mark.parametrize(
        "layer,mode",
        [("B.Cu", "front_inner_back"), ("In1.Cu", "front_inner_back"), ("In1.Cu", "custom")],
    )
    def test_host_contacts_the_via_land_on_its_own_layer(
        self, tmp_path, capsys, diameter, connected, layer, mode
    ):
        inner = "Inner" if mode == "front_inner_back" else "In1.Cu"
        inner_size = diameter if layer == "In1.Cu" else 0.5
        back_size = diameter if layer == "B.Cu" else 0.5
        via = (
            _via('"F.Cu" "B.Cu"')
            .replace("(size 0.6)", "(size 1.5)")
            .replace(
                "(net 1)",
                f'(padstack (mode {mode}) (layer "{inner}" (size {inner_size})) '
                f'(layer "B.Cu" (size {back_size}))) (net 1)',
            )
        )
        host = (
            TEE_TRACK.replace("start 5 4", "start 5.68 14")
            .replace("end 5 14", "end 11 9")
            .replace('"F.Cu"', f'"{layer}"')
        )
        board = _tee(tmp_path, via, host)
        board.write_text(
            board.read_text().replace(
                '(31 "B.Cu" signal)', '(1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)'
            )
        )
        _p3_through_hole(board)
        link = _joined(capsys, board, "--join-track", "--probe")
        assert ("junction" in link) is connected
        if connected:
            assert link["junction"]["layer"] == layer

    @pytest.mark.parametrize("source", ["zero", "pad", "footprint", "board", "offset"])
    def test_join_site_stays_outside_the_effective_pad_opening(self, tmp_path, capsys, source):
        board = _tee(tmp_path, TEE_TRACK)
        text = board.read_text().replace(
            "(at 12 9)", "(at 12 12.65)" if source == "offset" else "(at 12 13.35)"
        )
        text = text.replace(
            '(44 "Edge.Cuts" user)', '(38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user)'
        )
        before, target = text.split('(fp_text reference "P2"')
        if source == "pad":
            target = target.replace("(size 1 1)", "(size 1 1) (solder_mask_margin 0.25)", 1)
        elif source == "footprint":
            before += "(solder_mask_margin 0.25) "
        elif source == "board":
            before = before.replace("pad_to_mask_clearance 0", "pad_to_mask_clearance 0.25")
        elif source == "offset":
            target = target.replace(
                TEE_SMD,
                '(pad "1" thru_hole rect (at 0 0) (size 2 2) (drill 0.4 (offset 0 -0.7)) (layers "*.Cu" "*.Mask")',
                1,
            )
        board.write_text(before + '(fp_text reference "P2"' + target)
        link = _joined(capsys, board, "--join-track", "--probe", "--link-layers", "F.Cu")
        assert link["routable"]
        if source == "zero":
            assert link["junction"]["y"] == 13.35
        elif "junction" in link:
            # The lower edge is 12.3 for the offset copper and 13.25 for the expanded opening.
            lower_edge = 12.3 if source == "offset" else 13.25
            assert link["junction"]["y"] + 0.125 <= lower_edge

    @pytest.mark.parametrize("layer_type", ["signal", "power", "mixed", "jumper"])
    def test_blind_via_does_not_connect_the_far_layer(self, tmp_path, capsys, layer_type):
        board = _tee(
            tmp_path,
            _via('"F.Cu" "In1.Cu"', kind="blind"),
            '(segment (start 5 14) (end 11 9) (width 0.25) (layer "B.Cu") (net 1) '
            '(uuid "00000000-0000-0000-0000-000000000050"))',
        )
        layers = FOUR_LAYERS.replace('"In1.Cu" signal', f'"In1.Cu" {layer_type}')
        board.write_text(board.read_text().replace(_TWO_LAYERS, layers))
        _p3_through_hole(board)
        link = _joined(capsys, board, "--join-track", "--probe")
        assert "junction" not in link and link["join_track_skipped"]

    def test_track_near_a_rounded_corner_is_isolated(self, tmp_path, capsys):
        board = _tee(tmp_path, TEE_TRACK.replace("5 4", "5.96 13.04").replace("5 14", "11 9"))
        text = board.read_text()
        prefix, p2 = text.split('(fp_text reference "P2"')
        p2 = p2.replace("smd rect", "smd roundrect", 1).replace(
            "(size 1 1)", "(size 2 2) (roundrect_rratio 0.5)", 1
        )
        board.write_text(prefix + '(fp_text reference "P2"' + p2)
        link = _joined(capsys, board, "--join-track", "--probe")
        assert link["routable"] and "junction" not in link

    def test_fractional_origin_cannot_move_a_reused_endpoint(self, tmp_path, capsys):
        board = _tee(tmp_path, TEE_TRACK.replace("start 5 4", "start 5.125 9.125"))
        board.write_text(
            board.read_text()
            .replace("start 1 1", "start 1.125 1.125")
            .replace("(at 12 9)", "(at 12 8)")
        )
        link = _joined(capsys, board, "--join-track", "--probe")
        assert link["routable"] and "junction" not in link

    def test_written_branch_clears_destination_at_fractional_origin(self, tmp_path, capsys):
        board = _tee(tmp_path, TEE_TRACK)
        board.write_text(
            board.read_text()
            .replace("start 1 1", "start 1 1.007")
            .replace("(at 5 14)", "(at 5 14.003)")
            .replace("(at 12 9)", "(at 12 13.377)")
        )
        link = _joined(capsys, board, "--join-track", "--probe")
        assert link["routable"] and "junction" not in link

    def test_written_end_cap_clears_an_intermediate_pad(self, tmp_path, capsys):
        board = _tee(tmp_path, TEE_TRACK, extra_pads=TEE_PAD4.replace("(at 2 12)", "(at 5 9)"))
        link = _joined(capsys, board, "--join-track", "--probe")
        junction = link.get("junction")
        assert link["routable"]
        assert junction is None or abs(junction["y"] - 9) >= 0.625

    @pytest.mark.parametrize("via_net,connected", [(1, True), (2, False)])
    def test_only_prior_same_net_vias_connect_fills(self, tmp_path, via_net, connected):
        from shapely.geometry import box

        from kicad_tools.cli.route_link_cmd import _commit_to_model
        from kicad_tools.router.link_router import _build_model, _join_island, terminal_for_pad
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(_tee(tmp_path, TEE_TRACK_B)))
        model = _build_model(pcb)
        ox, oy = pcb.board_origin
        fill = box(3 - ox, 10 - oy, 8 - ox, 15 - oy)
        model.fills = {layer: [(1, fill)] for layer in ("F.Cu", "B.Cu")}
        via = LinkRoute(
            via_net, "SENSE" if connected else "FOE", 0.25, 0.6, 0.3, vias=[(4 - ox, 12 - oy)]
        )
        _commit_to_model(model, via, via_net, (ox, oy))
        island = _join_island(pcb, model, 1, terminal_for_pad(pcb, "P2", "1"))
        assert ("B.Cu" in island) == connected

    def test_arc_is_followed_not_its_chords(self, tmp_path, capsys):
        board = _tee(
            tmp_path,
            '(arc (start 5 14) (mid 10 9) (end 5 4) (width 0.25) (layer "F.Cu") (net 1) '
            '(uuid "00000000-0000-0000-0000-000000000060"))',
            '(segment (start 7.5 11.5) (end 8 11.5) (width 0.25) (layer "F.Cu") (net 1) '
            '(uuid "00000000-0000-0000-0000-000000000061"))',
        )
        link = _joined(capsys, board, "--join-track", "--probe")
        assert "junction" not in link

    def test_branch_through_the_destination_pad_is_refused(self):
        from shapely.geometry import box

        from kicad_tools.router.link_router import LinkTerminal, _branch_clears_pad

        pad = LinkTerminal("P2.1", (5, 13), {"F.Cu": box(4.5, 12.5, 5.5, 13.5)})
        through = LinkRoute(1, "S", 0.25, 0.6, 0.3, segments=[(5, 16, 5, 11, "F.Cu")])
        beside = LinkRoute(1, "S", 0.25, 0.6, 0.3, segments=[(7, 16, 7, 11, "F.Cu")])
        assert not _branch_clears_pad(through, pad) and _branch_clears_pad(beside, pad)

    def test_branch_via_clears_the_destination_on_the_opposite_face(self):
        from shapely.geometry import box

        from kicad_tools.router.link_router import LinkTerminal, _branch_clears_pad

        pad = LinkTerminal("P2.1", (5, 13), {"B.Cu": box(4.5, 12.5, 5.5, 13.5)})
        route = LinkRoute(1, "S", 0.25, 0.6, 0.3, segments=[(5, 16, 5, 13.75, "F.Cu")])
        assert _branch_clears_pad(route, pad)
        route.vias.append((5, 13.75))
        assert not _branch_clears_pad(route, pad)

    def test_link_below_the_pad_never_crosses_it(self, tmp_path, capsys):
        board = _tee(tmp_path, TEE_TRACK)
        board.write_text(board.read_text().replace("(at 12 9)", "(at 5 17)"))
        link = _joined(capsys, board, "--join-track", "--probe", "--link-layers", "F.Cu")
        junction = link.get("junction")
        assert junction is None or junction["y"] < 13.5 - 0.125 or junction["y"] > 14.5

    def test_later_link_joins_an_earlier_route(self, tmp_path, capsys):
        board = _tee(tmp_path)
        rc, doc = _run(
            capsys,
            str(board),
            "--link", "SENSE P1.1 P2.1", *TEE_OPTS, "--join-track",
            "--link", "SENSE P3.1 P2.1", *TEE_OPTS, "--join-track",
            "--probe",
        )  # fmt: skip
        first, second = doc["links"]
        assert "junction" not in first and "junction" in second
        assert "join_track_skipped" not in second

    def test_junction_snaps_to_the_host_endpoint_it_is_next_to(self, tmp_path, capsys):
        board = _tee(
            tmp_path,
            '(segment (start 5.003 9.004) (end 5 14) (width 0.25) (layer "F.Cu") (net 1) '
            f'(uuid "{TEE_HOST_UUID}"))',
            '(segment (start 5 4) (end 5.003 9.004) (width 0.25) (layer "F.Cu") (net 1) '
            '(uuid "00000000-0000-0000-0000-000000000011"))',
        )
        board.write_text(board.read_text().replace("(at 12 9)", "(at 12 8)"))
        link = _joined(capsys, board, "--join-track", "--probe", "--link-layers", "F.Cu")
        j = link.get("junction")
        assert j is None or (j["x"], j["y"]) != (5.0, 9.0)

    @pytest.mark.parametrize("suffix", ["", "@B.Cu"])
    def test_allowed_layers_apply_to_the_junction_not_the_pad(self, tmp_path, capsys, suffix):
        board = _tee(
            tmp_path,
            _via('"F.Cu" "B.Cu"'),
            '(segment (start 5 14) (end 5 4) (width 0.25) (layer "B.Cu") (net 1) '
            '(uuid "00000000-0000-0000-0000-000000000070"))',
        )
        _p3_through_hole(board)
        rc, doc = _run(
            capsys,
            str(board),
            "--link",
            f"SENSE P3.1 P2.1{suffix}",
            *TEE_TUNE,
            "--join-track",
            "--probe",
            "--link-layers",
            "B.Cu",
        )
        assert rc == 0 and doc["links"][0]["junction"]["layer"] == "B.Cu"

    def test_split_of_a_legacy_tstamp_segment_has_unique_ids(self, tmp_path):
        board = _tee(tmp_path, TEE_TRACK.replace("(uuid ", "(tstamp "))
        board.write_text(board.read_text().replace("(version 20240108)", "(version 20221018)"))
        out = TestJoinTrackWrite._write(board)
        ids = re.findall(
            r'\((?:uuid|tstamp) "([^"]+)"\)', "".join(re.split(r"\(segment\b", out)[1:])
        )
        assert len(ids) == 3 and len(set(ids)) == 3


@pytest.fixture
def plane_board(tmp_path):
    """Plated SIG terminals span a partial inner pour bonded to a remote pad."""
    text = BOARD.replace('(31 "B.Cu"', '(1 "In1.Cu" power) (2 "In2.Cu" signal) (31 "B.Cu"')
    text = text.replace("smd rect", "thru_hole circle").replace(
        '(layers "F.Cu" "F.Paste" "F.Mask")', '(drill 0.4) (layers "*.Cu" "*.Mask")'
    )
    # The remote connection sits north of the corridor. The baseline pour's
    # southern edge exactly touches the authorized rectangle.
    pts = "(pts (xy 106 104) (xy 124 104) (xy 124 109.85) (xy 106 109.85))"
    extra = f"""
    (footprint "R" (layer "F.Cu") (at 115 106)
      (fp_text reference "J3" (at 0 -1) (layer "F.SilkS"))
      (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.4)
        (layers "*.Cu" "*.Mask") (net 2 "FOE")))
    (zone (net 2) (net_name "FOE") (layer "In1.Cu")
      (uuid "00000000-0000-0000-0000-000000000011")
      (hatch edge 0.5) (connect_pads yes (clearance 0.5)) (min_thickness 0.1)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon {pts}) (filled_polygon (layer "In1.Cu") {pts}))
    """
    path = tmp_path / "partial.kicad_pcb"
    path.write_text(text.rstrip()[:-1] + extra + ")\n")
    return path


def test_plane_corridor_probe_is_per_link_and_contains_full_track(plane_board, capsys):
    original = plane_board.read_bytes()
    args = ("--link", LINK, "--link-layers", "In1.Cu")
    rc, doc = _run(
        capsys,
        str(plane_board),
        "--probe",
        *args,
        "--plane-corridor",
        "In1.Cu=109,109.9,121,110.1",
        *args,
        "--plane-corridor",
        "In1.Cu=109,109.900001,121,110.1",
        *args,
    )
    assert rc == 1
    ok, narrow, default = doc["links"]
    assert ok["routable"] and ok["layers"] == ["In1.Cu"]
    assert not narrow["routable"] and "corridor" in narrow["reason"]
    assert not default["routable"]
    assert plane_board.read_bytes() == original


def _native_plane_link(capsys, board, *args):
    from kicad_tools.cli.kicad_toolchain import PINNED_KICAD_VERSION, version_number
    from kicad_tools.cli.runner import find_kicad_cli, get_kicad_version

    cli = find_kicad_cli()
    if cli is None or version_number(get_kicad_version(cli) or "") != PINNED_KICAD_VERSION:
        pytest.skip(f"native plane refill requires KiCad {PINNED_KICAD_VERSION}")
    return _run(
        capsys,
        str(board),
        "--link",
        LINK,
        "--link-layers",
        "In1.Cu",
        "--plane-corridor",
        "In1.Cu=109,109.9,121,110.1",
        *args,
    )


def test_native_plane_refill_requires_a_bounded_save(plane_board, monkeypatch):
    from math import isfinite

    from kicad_tools.cli import runner
    from kicad_tools.cli.route_link_cmd import _native_refill

    def timed_out(path, output, cli, *, timeout):
        assert isfinite(timeout) and timeout > 0
        return runner.KiCadCLIResult(success=False, stderr="refill timed out")

    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("kicad-cli"))
    monkeypatch.setattr(runner, "_run_fill_zones_via_drc", timed_out)
    with pytest.raises(RuntimeError, match="refill timed out"):
        _native_refill(plane_board)


@pytest.fixture
def project_plane_board(plane_board):
    plane_board.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "meta": {"version": 3},
                "net_settings": {
                    "meta": {"version": 3},
                    "classes": [{"name": "Default", "clearance": 0.8}],
                },
            }
        )
    )
    return plane_board


@native_launching
def test_plane_commit_reports_gap_to_saved_project_refill(project_plane_board, capsys):
    original = project_plane_board.read_bytes()
    out = project_plane_board.with_name("routed.kicad_pcb")
    rc, doc = _native_plane_link(capsys, project_plane_board, "-o", str(out))
    assert rc == 0, doc
    gap = doc["links"][0]["min_plane_gap_mm"]
    assert 0.8 <= gap < 0.81  # project floor exceeds zone (0.5) and CLI (0.2)
    assert project_plane_board.read_bytes() == original
    pcb = PCB.load(out)
    assert [s.layer for s in pcb.segments if pcb.nets[s.net_number].name == "SIG"] == ["In1.Cu"]
    assert pcb.zones[0].filled_polygons


@native_launching
def test_plane_commit_vetoes_clearance_above_native_setback(project_plane_board, capsys):
    original = project_plane_board.read_bytes()
    rc, doc = _native_plane_link(capsys, project_plane_board, "--net-clearance", "FOE=1.5")
    assert rc == 1 and "requires 1.5 mm" in doc["links"][0]["reason"]
    assert project_plane_board.read_bytes() == original


@native_launching
@pytest.mark.parametrize("second_pad", [True, False])
def test_plane_commit_rolls_back_stranded_island(plane_board, capsys, second_pad):
    # The SIG track now crosses the entire pour. With a pad in the severed
    # half native KiCad keeps both islands. Without it native removes that half.
    text = plane_board.read_text().replace("(xy 106 ", "(xy 112 ").replace("(xy 124 ", "(xy 118 ")
    text = text.replace("109.85)", "116)")
    if second_pad:
        text = (
            text.rstrip()[:-1]
            + """
        (footprint "R" (layer "F.Cu") (at 114 114)
          (fp_text reference "J4" (at 0 -1) (layer "F.SilkS"))
          (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.4)
            (layers "*.Cu" "*.Mask") (net 2 "FOE"))))
        """
        )
    plane_board.write_text(text)
    rc, doc = _native_plane_link(capsys, plane_board)
    assert rc == 1, doc
    link = doc["links"][0]
    assert link["status"] == "rolled-back" and link["failed_check"] == "plane-corridor"
    assert "stranded FOE island on In1.Cu" in link["reason"]
    assert plane_board.read_text() == text


@pytest.mark.parametrize(
    "corridor",
    [
        "F.Cu=1,2,3,4",
        "In1.Cu=nan,2,3,4",
        "In1.Cu=3,2,1,4",
        "In1.Cu=1,2,3",
    ],
)
def test_invalid_plane_corridor_is_rejected(board, corridor):
    with pytest.raises(SystemExit):
        main(["route-link", str(board), "--link", LINK, "--plane-corridor", corridor])


class TestCorridorPublish:
    """Corridor commits keep the destination's mode and the input project's context."""

    @pytest.fixture(autouse=True)
    def _stub_gate(self, monkeypatch):
        import contextlib
        from collections import Counter

        from kicad_tools.cli import kicad_toolchain, route_link_cmd

        monkeypatch.setattr(
            kicad_toolchain, "evidence_toolchain", lambda *a, **k: contextlib.nullcontext()
        )
        monkeypatch.setattr(route_link_cmd, "_gate_state", lambda path, args: (Counter(), None))
        monkeypatch.setattr(route_link_cmd, "_native_refill", PCB.load)
        monkeypatch.setattr(
            route_link_cmd, "_verify_corridor_candidate", lambda path, before, *a: (before, 0.0)
        )

    def _link(self, capsys, board, *extra):
        return _run(
            capsys,
            str(board),
            *extra,
            "--link",
            LINK,
            "--link-layers",
            "In1.Cu",
            "--plane-corridor",
            "In1.Cu=109,109.9,121,110.1",
        )

    def test_read_only_destination_is_refused_untouched(self, plane_board, capsys):
        import os

        if os.geteuid() == 0:
            pytest.skip("root bypasses file modes")
        original = plane_board.read_bytes()
        plane_board.chmod(0o444)
        rc, _ = self._link(capsys, plane_board)
        assert rc == 2
        assert plane_board.read_bytes() == original
        assert plane_board.stat().st_mode & 0o777 == 0o444

    @pytest.mark.parametrize("in_place", [True, False])
    def test_private_mode_survives_publish(self, plane_board, capsys, in_place):
        original = plane_board.read_bytes()
        target = plane_board
        extra: tuple[str, ...] = ()
        if not in_place:
            target = plane_board.with_name("out.kicad_pcb")
            target.write_bytes(original)
            extra = ("-o", str(target))
        target.chmod(0o600)
        rc, doc = self._link(capsys, plane_board, *extra)
        assert rc == 0 and doc["links"][0]["status"] == "committed"
        assert target.read_bytes() != original
        assert target.stat().st_mode & 0o777 == 0o600

    def test_stage_carries_project_libraries_and_input_rules(self, tmp_path):
        from kicad_tools.cli.route_link_cmd import _stage_project_files

        src = tmp_path / "proj"
        src.mkdir()
        board = src / "b.kicad_pcb"
        board.write_text("")
        (src / "b.kicad_dru").write_text("(version 1)\n(rule a)")
        (src / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "P")(type "KiCad")(uri "${KIPRJMOD}/P.pretty")(options "")))'
        )
        private = tmp_path / "private"
        private.mkdir()
        staged = _stage_project_files(board, private / "b.kicad_pcb")
        assert (private / "b.kicad_dru").read_text() == "(version 1)\n(rule a)"
        table = (private / "fp-lib-table").read_text()
        assert f'(uri "{src.resolve()}/P.pretty")' in table and "KIPRJMOD" not in table
        assert len(staged) == 2

    @pytest.mark.parametrize("corridor_second", [True, False])
    def test_second_link_keeps_first_in_separate_output(self, plane_board, capsys, corridor_second):
        text = plane_board.read_text().rstrip()[:-1]
        for ref, x in (("R3", 110), ("R4", 120)):
            text += f"""
  (footprint "R" (layer "F.Cu") (at {x} 114)
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.4)
      (layers "*.Cu" "*.Mask") (net 1 "SIG")))"""
        plane_board.write_text(text + ")\n")
        out = plane_board.with_name("out.kicad_pcb")
        out.write_bytes(plane_board.read_bytes())
        second = ["--link", "SIG R3.1 R4.1"]
        if corridor_second:
            second += ["--link-layers", "In1.Cu", "--plane-corridor", "In1.Cu=109,113.9,121,114.1"]
        rc, doc = self._link(capsys, plane_board, "-o", str(out), *second)
        assert rc == 0, doc
        assert [e["status"] for e in doc["links"]] == ["committed", "committed"]
        ys = {round(s.start[1]) for s in PCB.load(out).segments if s.net_number == 1}
        assert {10, 14} <= ys  # board-origin relative

    def test_stage_handles_bare_uri(self, tmp_path):
        from kicad_tools.cli.route_link_cmd import _stage_project_files

        src = tmp_path / "proj"
        src.mkdir()
        board = src / "b.kicad_pcb"
        board.write_text("")
        (src / "fp-lib-table").write_text(
            "(fp_lib_table (lib (name local)(type KiCad)(uri ${KIPRJMOD}/local.pretty)))"
        )
        private = tmp_path / "private"
        private.mkdir()
        _stage_project_files(board, private / "b.kicad_pcb")
        table = (private / "fp-lib-table").read_text()
        assert f'(uri "{src.resolve()}/local.pretty")' in table and "KIPRJMOD" not in table

    def test_publish_stages_beside_resolved_destination(self, plane_board, capsys, monkeypatch):
        import os

        real = plane_board.parent / "elsewhere"
        real.mkdir()
        dest = real / "target.kicad_pcb"
        dest.write_bytes(plane_board.read_bytes())
        link = plane_board.with_name("out.kicad_pcb")
        link.symlink_to(dest)
        sibling = dest.with_name(f".publish-{dest.name}")
        sibling.write_bytes(b"independent sibling")
        seen = []
        orig = os.replace
        monkeypatch.setattr(
            os,
            "replace",
            lambda a, b: (
                seen.append(Path(a).parent) if Path(b) == dest else None,
                orig(a, b),
            )[1],
        )
        rc, doc = self._link(capsys, plane_board, "-o", str(link))
        assert rc == 0, doc
        assert seen and all(directory.parent == real.resolve() for directory in seen)
        assert sibling.read_bytes() == b"independent sibling"
        assert link.is_symlink()
        assert dest.read_bytes() != plane_board.read_bytes()

    def test_symlink_output_mounts_cover_native_boards(self, plane_board, capsys, monkeypatch):
        from collections import Counter
        from contextlib import contextmanager

        from kicad_tools.cli import kicad_toolchain, route_link_cmd

        link = plane_board.parent / "links" / "out.kicad_pcb"
        dest = plane_board.parent / "targets" / "target.kicad_pcb"
        source = plane_board.parent / "project" / plane_board.name
        for path in (source, link, dest):
            path.parent.mkdir()
        plane_board.rename(source)
        dest.write_bytes(source.read_bytes())
        link.symlink_to(dest)

        active_mounts = []
        boards = []

        @contextmanager
        def toolchain(*, mounts):
            active_mounts.extend(mounts)
            yield

        def gate(path, args):
            boards.append(path)
            return Counter(), None

        monkeypatch.setattr(kicad_toolchain, "evidence_toolchain", toolchain)
        monkeypatch.setattr(route_link_cmd, "_gate_state", gate)
        rc, doc = self._link(capsys, source, "-o", str(link))
        assert rc == 0, doc
        assert len(set(boards)) == 2  # baseline and routed candidate
        assert all(any(path.is_relative_to(root) for root in active_mounts) for path in boards)

    def test_baseline_uses_input_rules_not_output_sidecars(self, plane_board, capsys, monkeypatch):
        from kicad_tools.cli import route_link_cmd

        plane_board.with_suffix(".kicad_dru").write_text("(version 1)\n(rule input)")
        out = plane_board.with_name("out.kicad_pcb")
        out.write_bytes(plane_board.read_bytes())
        out_dru = out.with_suffix(".kicad_dru")
        out_dru.write_text("(version 1)")
        seen = {}

        def capture(*args):
            baseline = args[10]
            seen["dru"] = baseline.with_suffix(".kicad_dru").read_text()
            return None, None

        monkeypatch.setattr(route_link_cmd, "_commit_links_gated", capture)
        self._link(capsys, plane_board, "-o", str(out))
        assert seen["dru"] == "(version 1)\n(rule input)"
        assert out_dru.read_text() == "(version 1)"
