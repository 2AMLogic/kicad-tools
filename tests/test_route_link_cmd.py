"""``kct route-link``: targeted single-link routing and probe (Issue #8310)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli import main  # noqa: E402
from kicad_tools.cli.route_link_cmd import LinkSpecError, parse_link_spec  # noqa: E402
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
LINK = "SIG R1.1 R2.1"


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


def _file_segments(path: Path) -> list[tuple]:
    """``(start, end, width, layer, uuid)`` per SENSE track, in the file's millimetres."""
    pcb = PCB.load(str(path))
    ox, oy = pcb.board_origin
    return [
        (
            (round(s.start[0] + ox, 2), round(s.start[1] + oy, 2)),
            (round(s.end[0] + ox, 2), round(s.end[1] + oy, 2)),
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

    def test_output_file_leaves_input_alone(self, board, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        before = board.read_bytes()
        rc, _ = _run(capsys, str(board), "--link", LINK, "-o", str(out))
        assert rc == 0 and board.read_bytes() == before and out.exists()

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
        _commit_to_model(model, via, via_net)
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
