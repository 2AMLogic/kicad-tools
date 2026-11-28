"""Pour completion necks a pad escape the class width cannot leave (Issue #9424).

PR #10631 (Issue #10627) welds pour pads at the full class width.  A 0.50 mm
stub cannot leave a 0.40 mm pad on 0.65 mm pitch with 0.20 mm clearance -- the
geometry of board 05's U2 pins 16 (+5V) and 20 (GND) -- so those welds found
no placement and the unattended route regressed to 44/46 nets.  A stub no
strategy can place at the class width is retried as a short pad escape only
when the class declares a narrower ``neck_trace_width``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("shapely")

from shapely.geometry import box  # noqa: E402

from kicad_tools.router.oracle_completion import (  # noqa: E402
    ClosureAttempt,
    OracleEndpoint,
    OracleLink,
    PourLinkCloser,
)
from kicad_tools.router.rules import NetClassRouting  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.netclass_agreement import (  # noqa: E402
    RULE_TRACK_WIDTH_MISMATCH,
    check_netclass_agreement,
    pad_escape_lands,
)

WELD_FIXTURE = Path(__file__).parent / "fixtures" / "stitch-5388" / "continuous.kicad_pcb"

GND = {"name": "Ground plane", "trace_width": 0.5, "neck_trace_width": 0.15, "clearance": 0.2}


def _closer(classes: dict | None, **kw) -> PourLinkCloser:
    return PourLinkCloser(
        via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2, net_class_map=classes, **kw
    )


def _segs(path: Path) -> list[tuple]:
    return [(s.start, s.end, round(s.width, 6)) for s in PCB.load(str(path)).segments]


def _new_segs(path: Path, before: list[tuple]) -> list[tuple]:
    old = list(before)
    out = []
    for seg in _segs(path):
        if seg in old:
            old.remove(seg)
        else:
            out.append(seg)
    return out


def _fine_pitch_board(tmp_path: Path, *, thermal: bool = False) -> Path:
    """U1.2 (GND, 0.40 mm tall on 0.65 mm pitch) can only be left along its axis.

    Its neighbours are 0.50 mm tall, so a 0.50 mm stub beside them keeps
    0.15 mm (0.20 mm needed).  By default the body-side blocker rules out -x
    and the GND plane is on B.Cu, so the pad is welded, not routed.  With
    ``thermal`` the blocker is U1.29, a GND thermal pad 0.525 mm to the left
    on F.Cu and there is no plane: the pad is joined to it on its own layer.
    """

    def pad(num: str, net: int, name: str, y: float, w: float = 1.575, h: float = 0.5) -> str:
        return (
            f'    (pad "{num}" smd rect (at 0 {y:g}) (size {w:g} {h:g}) '
            f'(layers "F.Cu") (net {net} "{name}"))\n'
        )

    text = (
        '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (net 0 "")\n  (net 1 "GND")\n  (net 2 "V3P3")\n  (net 3 "SIG")\n'
        '  (footprint "Test:FinePitch" (layer "F.Cu") (at 120 110)\n'
        '    (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))\n'
        + pad("1", 2, "V3P3", -0.65)
        + pad("2", 1, "GND", 0.0, h=0.4)
        + pad("3", 3, "SIG", 0.65)
        + (
            pad("29", 1, "GND", 0.0, w=1.0, h=4.0).replace("(at 0 0)", "(at -1.8125 0)")
            if thermal
            else pad("4", 3, "SIG", 0.0, w=1.2, h=4.0).replace("(at 0 0)", "(at -1.6 0)")
        )
        + "  )\n"
    )
    if not thermal:
        text += (
            '  (zone (net 1) (net_name "GND") (layer "B.Cu")\n'
            "    (polygon (pts (xy 110 100) (xy 130 100) (xy 130 120) (xy 110 120)))\n"
            '    (filled_polygon (layer "B.Cu")\n'
            "      (pts (xy 110 100) (xy 130 100) (xy 130 120) (xy 110 120))))\n"
        )
    text += '  (gr_rect (start 100 90) (end 140 130) (layer "Edge.Cuts") (width 0.1))\n)\n'
    path = tmp_path / "fine.kicad_pcb"
    path.write_text(text)
    _write_ground_class(path, 0.5)
    return path


def _write_ground_class(path: Path, width: float) -> None:
    project = {
        "board": {},
        "meta": {"version": 1},
        "net_settings": {
            "meta": {"version": 3},
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.2,
                    "track_width": 0.2,
                    "priority": 2**31 - 1,
                },
                {"name": "Ground plane", "clearance": 0.2, "track_width": width, "priority": 1},
            ],
            "netclass_assignments": {"GND": "Ground plane"},
            "netclass_patterns": [],
        },
    }
    path.with_suffix(".kicad_pro").write_text(json.dumps(project))


@pytest.fixture
def fine_pitch_route_input(tmp_path):
    from kicad_tools.cli.kicad_toolchain import PINNED_KICAD_VERSION, version_number
    from kicad_tools.cli.runner import find_kicad_cli, get_kicad_version, run_refill_zones
    from kicad_tools.pcb.editor import PCBEditor

    cli = find_kicad_cli()
    if cli is None or version_number(get_kicad_version(cli) or "") != PINNED_KICAD_VERSION:
        pytest.skip(f"native pour-completion test requires KiCad {PINNED_KICAD_VERSION}")

    # A back-side plane joins the through-hole header. Two signal nets beside
    # U1.2 exercise the negotiated route before the ground-pad escape runs.
    board = tmp_path / "stock.kicad_pcb"
    pcb = PCB.create(width=37, height=36, center=False)
    pcb.add_footprint("Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm", "U1", 17, 18)
    pcb.add_footprint("Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical", "J1", 27, 26)
    nets = {
        "GND": [("U1", "2"), ("J1", "1")],
        "GUARD": [("U1", "1"), ("U1", "3")],
        "SIG": [("U1", "13"), ("U1", "15")],
    }
    for net, pads in nets.items():
        for ref, pad in pads:
            assert pcb.assign_net_to_footprint_pad(ref, pad, net)
    pcb.save(board)
    editor = PCBEditor(str(board))
    editor.add_zone(
        "GND", "B.Cu", [(1, 1), (36, 1), (36, 35), (1, 35)], clearance=0.2, min_thickness=0.15
    )
    editor.save()
    _write_ground_class(board, 0.75)
    assert run_refill_zones(board, require_save=True).success
    return board


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize(("neck", "route_exit", "opens"), [(0.75, 3, 1), (0.2, 0, 0)])
def test_public_route_and_audit_respect_the_declared_escape(
    fine_pitch_route_input, tmp_path, neck, route_exit, opens
):
    from kicad_tools.cli import main
    from kicad_tools.cli.runner import run_drc

    board = fine_pitch_route_input
    original = board.read_bytes()
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"GND": {**GND, "trace_width": 0.75, "neck_trace_width": neck}}))
    routed = tmp_path / "routed.kicad_pcb"
    assert (
        main(
            [
                "route",
                str(board),
                "-o",
                str(routed),
                "--net-class-map",
                str(rules),
                "--no-optimize",
                "--no-auto-pour",
                "--no-auto-layers",
                "--no-sync-check",
                "--no-current-paths",
                "--no-placement-feedback",
                "--oracle-rounds",
                "2",
                "--format",
                "json",
            ]
        )
        == route_exit
    )
    assert board.read_bytes() == original
    saved = PCB.load(routed)
    ground = saved.get_net_by_name("GND").number
    widths = [segment.width for segment in saved.segments if segment.net_number == ground]
    assert widths == ([] if opens else [pytest.approx(neck)])
    native = tmp_path / "native.json"
    assert run_drc(routed, native, schematic_parity=False, refill_zones=True).success
    assert len(json.loads(native.read_text())["unconnected_items"]) == opens

    audit = tmp_path / "audit.json"
    main(
        [
            "check",
            str(routed),
            "--mfr",
            "jlcpcb",
            "--only",
            "netclass_agreement",
            "--format",
            "json",
            "--output",
            str(audit),
        ]
    )
    report = json.loads(audit.read_text())
    assert report["summary"]["rules_checked_by_rule"][RULE_TRACK_WIDTH_MISMATCH]
    assert not any(row["rule_id"] == RULE_TRACK_WIDTH_MISMATCH for row in report["findings"])


class TestNeckWidth:
    def test_declared_neck_below_the_class_width(self):
        closer = _closer({"GND": NetClassRouting(**GND)})
        assert closer.neck_width("GND") == pytest.approx(0.15)

    def test_neck_is_relative_to_the_effective_class_width(self):
        narrow = NetClassRouting(name="S", trace_width=0.15, neck_trace_width=0.1)
        assert _closer({"SIG": narrow}).neck_width("SIG") == pytest.approx(0.1)

    @pytest.mark.parametrize("neck", [None, 0.5, 0.6, 0.0, -0.1, True])
    def test_escape_requires_a_positive_declared_width_below_class(self, neck):
        cls = NetClassRouting(name="Ground plane", trace_width=0.5, neck_trace_width=neck)
        closer = _closer({"GND": cls})
        assert closer.neck_width("GND") is None

    def test_none_when_nothing_is_narrower_than_the_class(self):
        same = _closer({"SIG": NetClassRouting(name="D", trace_width=0.2)})
        assert same.neck_width("SIG") is None  # class width == board width
        assert _closer({"GND": NetClassRouting(**GND)}).neck_width("VM") is None
        assert _closer(None).neck_width("GND") is None


class TestFinePitchPadTakesTheNeck:
    def _weld(self, tmp_path: Path, classes: dict | None):
        board = _fine_pitch_board(tmp_path)
        before = _segs(board)
        closer = _closer(classes)
        attempt = ClosureAttempt()
        closer._weld_pads(board, {"GND": {"U1.2"}}, attempt)
        return board, before, closer, attempt

    def test_class_width_cannot_leave_the_pad(self, tmp_path):
        """Control: nothing narrower than the class exists, and its stub is impossible.

        With the board width equal to the class width there is no pad-escape
        neck to fall back to, so the pad stays open exactly as before.
        """
        board = _fine_pitch_board(tmp_path)
        before = _segs(board)
        closer = PourLinkCloser(
            via_size=0.6,
            via_drill=0.3,
            clearance=0.2,
            trace_width=0.5,
            net_class_map={"GND": NetClassRouting(name="Ground plane", trace_width=0.5)},
        )
        attempt = ClosureAttempt()
        closer._weld_pads(board, {"GND": {"U1.2"}}, attempt)
        assert attempt.applied == 0
        assert any("no valid via location" in n for n in attempt.notes), attempt.notes
        assert _segs(board) == before
        assert closer.exhausted_stitch_pads == {"U1.2"}

    @pytest.mark.parametrize("neck", [None, 0.5, 0.6])
    def test_weld_preserves_class_width_when_no_narrower_escape_is_permitted(self, tmp_path, neck):
        classes = {"GND": NetClassRouting(**{**GND, "neck_trace_width": neck})}
        board, before, closer, attempt = self._weld(tmp_path, classes)
        assert attempt.applied == 0
        assert _segs(board) == before
        assert not PCB.load(str(board)).vias
        assert closer.exhausted_stitch_pads == {"U1.2"}

    def test_necked_stub_is_written_and_the_audit_excuses_it(self, tmp_path):
        classes = {"GND": NetClassRouting(**GND)}
        board, before, closer, attempt = self._weld(tmp_path, classes)
        new = _new_segs(board, before)
        assert attempt.applied >= 1, attempt.notes
        assert new, "no stub written"
        assert all(w == pytest.approx(0.15) for _, _, w in new), new
        assert len(PCB.load(str(board)).vias) == 1
        assert closer.exhausted_stitch_pads == set()
        assert any("pad-escape neck 0.15" in n for n in attempt.notes), attempt.notes
        results = check_netclass_agreement(board, PCB.load(str(board)), classes)
        assert results.violations == []

    def test_the_audit_neck_is_what_excuses_the_stub(self, tmp_path):
        """Not vacuous: the same stub below a class without a declared neck warns."""
        classes = {"GND": NetClassRouting(**GND)}
        board, before, closer, attempt = self._weld(tmp_path, classes)
        assert attempt.applied >= 1, attempt.notes
        strict = {"GND": NetClassRouting(name="Ground plane", trace_width=0.5, clearance=0.2)}
        (finding,) = check_netclass_agreement(board, PCB.load(str(board)), strict).violations
        assert finding.rule_id == RULE_TRACK_WIDTH_MISMATCH
        assert finding.actual_value == pytest.approx(0.15)

    def test_necked_stub_is_a_straight_pad_escape(self, tmp_path):
        board, before, closer, attempt = self._weld(tmp_path, {"GND": NetClassRouting(**GND)})
        assert attempt.applied >= 1, attempt.notes
        new = _new_segs(board, before)
        assert len(new) == 1, new
        (sx, sy), (ex, ey), _ = new[0]
        pcb = PCB.load(str(board))
        ox, oy = pcb.board_origin  # the loader reports board-relative coordinates
        assert (sx, sy) == pytest.approx((120.0 - ox, 110.0 - oy))  # the pad centre
        assert sy == pytest.approx(ey) and ex > sx  # along the pad axis, out of the pad


class TestPadEscapeLands:
    START = (120.0, 110.0)

    def test_straight_leg_within_reach(self):
        assert pad_escape_lands([self.START, (121.3, 110.0)], 2.0)
        assert pad_escape_lands([self.START, (121.0, 111.0)], 2.0)

    def test_straight_leg_beyond_reach(self):
        assert not pad_escape_lands([self.START, (123.0, 110.0)], 2.0)

    def test_45_degree_dogleg_within_reach(self):
        assert pad_escape_lands([self.START, (121.0, 110.0), (121.5, 110.5)], 2.0)
        assert pad_escape_lands([self.START, (120.5, 110.5), (121.5, 110.5)], 2.0)

    def test_dogleg_whose_landing_is_beyond_reach(self):
        assert not pad_escape_lands([self.START, (121.0, 110.0), (122.5, 110.5)], 2.0)

    def test_right_angle_corner_is_not_a_pad_dogleg(self):
        assert not pad_escape_lands([self.START, (121.0, 110.0), (121.0, 111.0)], 2.0)

    def test_more_than_two_legs(self):
        path = [self.START, (120.5, 110.5), (121.0, 110.5), (121.5, 111.0)]
        assert not pad_escape_lands(path, 4.0)
        assert not pad_escape_lands([self.START], 2.0)


class TestNeckedWeldDispatch:
    @pytest.fixture
    def rig(self, monkeypatch):
        from kicad_tools.cli import stitch_cmd

        calls: list[dict] = []

        def make(*, full_skipped=(), neck_welded=(), neck_skipped=()):
            refs = {"GNDA": "C1", "GND": "C1", "VM": "R1", "SIG": "C1"}

            def fake(pcb_path, nets, **kw):
                calls.append({"nets": list(nets), **kw})
                width = kw["trace_width"]
                necked = bool(kw.get("pad_escape_class"))
                welded = neck_welded if necked else ()
                skipped = neck_skipped if necked else full_skipped
                placed, skips = [], []
                for net in nets:
                    pad = SimpleNamespace(
                        reference=refs[net], pad_number="1", net_name=net, width=1.0, height=1.0
                    )
                    if width in welded:
                        placed.append(
                            SimpleNamespace(pad=pad, via_x=120.8, via_y=110.0, size=0.6, drill=0.3)
                        )
                    elif width in skipped:
                        skips.append((pad, "no site"))
                return SimpleNamespace(
                    vias_added=placed,
                    already_connected=0,
                    pads_skipped=skips,
                    skip_details=skips,
                    traces_added=[],
                )

            monkeypatch.setattr(stitch_cmd, "run_stitch", fake)
            return calls

        return make

    def _board(self, tmp_path: Path) -> Path:
        board = tmp_path / "b.kicad_pcb"
        shutil.copy(WELD_FIXTURE, board)
        return board

    def test_skipped_pad_is_retried_at_its_class_neck(self, tmp_path, rig):
        calls = rig(full_skipped={0.5}, neck_welded={0.15})
        closer = _closer({"GNDA": NetClassRouting(**{**GND, "name": "P"})})
        attempt = ClosureAttempt()
        closer._weld_pads(self._board(tmp_path), {"GNDA": {"C1.1"}}, attempt)
        full, neck = calls
        assert full["trace_width"] == 0.5 and full["pad_escape_class"] is None
        assert neck["trace_width"] == pytest.approx(0.15)
        assert neck["only_pads"] == frozenset({"C1.1"}) and neck["nets"] == ["GNDA"]
        assert neck["pad_escape_class"] == (pytest.approx(0.5), pytest.approx(0.2))
        assert neck["foreign_fills_yield"] is True
        assert attempt.applied == 1 and closer.exhausted_stitch_pads == set()
        assert any("pad-escape neck 0.15" in n for n in attempt.notes), attempt.notes

    def test_undeclared_neck_keeps_the_class_width_skip(self, tmp_path, rig):
        calls = rig(full_skipped={0.5})
        closer = _closer({"GNDA": NetClassRouting(name="P", trace_width=0.5, clearance=0.2)})
        attempt = ClosureAttempt()
        closer._weld_pads(self._board(tmp_path), {"GNDA": {"C1.1"}}, attempt)
        (full,) = calls
        assert full["trace_width"] == 0.5 and full["pad_escape_class"] is None
        assert closer.exhausted_stitch_pads == {"C1.1"}
        assert attempt.notes == ["GNDA C1.1: no site"]

    def test_a_welded_pad_is_not_retried(self, tmp_path, rig):
        calls = rig()
        closer = _closer({"GNDA": NetClassRouting(**{**GND, "name": "P"})})
        closer._weld_pads(self._board(tmp_path), {"GNDA": {"C1.1"}}, ClosureAttempt())
        assert [c["trace_width"] for c in calls] == [0.5]

    def test_necks_are_grouped_per_width(self, tmp_path, rig):
        calls = rig(full_skipped={0.5}, neck_welded={0.15, 0.3})
        closer = _closer(
            {
                "GND": NetClassRouting(**{**GND, "name": "P"}),
                "VM": NetClassRouting(name="M", trace_width=0.5, neck_trace_width=0.3),
            }
        )
        closer._weld_pads(
            self._board(tmp_path), {"GND": {"C1.1"}, "VM": {"R1.1"}}, ClosureAttempt()
        )
        necks = {
            (round(c["trace_width"], 6), tuple(c["nets"])) for c in calls if c["pad_escape_class"]
        }
        assert necks == {(0.15, ("GND",)), (0.3, ("VM",))}
        by_neck = {c["trace_width"]: c for c in calls if c["pad_escape_class"]}
        assert by_neck[0.15]["only_pads"] == frozenset({"C1.1"})
        assert by_neck[0.15]["pad_escape_class"] == (pytest.approx(0.5), pytest.approx(0.2))
        assert by_neck[0.3]["pad_escape_class"] == (pytest.approx(0.5), pytest.approx(0.2))

    def test_neck_is_not_consulted_at_the_board_width(self, tmp_path, rig):
        """Only the wide (class > board) groups #10627 made stricter change."""
        calls = rig(full_skipped={0.2})
        closer = _closer({"SIG": NetClassRouting(name="D", trace_width=0.2, neck_trace_width=0.15)})
        closer._weld_pads(self._board(tmp_path), {"SIG": {"C1.1"}}, ClosureAttempt())
        assert [c["trace_width"] for c in calls] == [0.2]

    def test_necked_failure_is_annotated(self, tmp_path, rig):
        rig(full_skipped={0.5}, neck_skipped={0.15})
        closer = _closer({"GNDA": NetClassRouting(**{**GND, "name": "P"})})
        attempt = ClosureAttempt()
        closer._weld_pads(self._board(tmp_path), {"GNDA": {"C1.1"}}, attempt)
        assert closer.exhausted_stitch_pads == {"C1.1"}
        (note,) = attempt.notes
        assert note.startswith("GNDA C1.1: no site")
        assert "pad-escape neck 0.15" in note


def _gnd_zone_link() -> OracleLink:
    return OracleLink(
        net="GND",
        a=OracleEndpoint(
            description="Pad 2 [GND] of U1",
            kind="pad",
            net="GND",
            x=120.0,
            y=110.0,
            layer="F.Cu",
            ref="U1",
            pad="2",
        ),
        b=OracleEndpoint(description="Zone [GND]", kind="zone", net="GND", x=120.0, y=110.0),
    )


class TestNeckedLink:
    """U2.20's shape: the class width cannot cross to same-net copper on the pad's own layer."""

    def _route(self, tmp_path: Path, classes: dict | None, board_width: float = 0.2):
        board = _fine_pitch_board(tmp_path, thermal=True)
        before = board.read_bytes()
        closer = PourLinkCloser(
            via_size=0.6,
            via_drill=0.3,
            clearance=0.2,
            trace_width=board_width,
            net_class_map=classes,
        )
        attempt = ClosureAttempt()
        closer._route_links(board, [_gnd_zone_link()], attempt)
        return board, before, closer, attempt

    def test_control_without_a_narrower_width_the_link_stays_open(self, tmp_path):
        classes = {"GND": NetClassRouting(name="Ground plane", trace_width=0.5)}
        board, before, closer, attempt = self._route(tmp_path, classes, board_width=0.5)
        assert attempt.applied == 0
        assert any("no clearance-legal route" in n for n in attempt.notes), attempt.notes
        assert board.read_bytes() == before  # no undersized copper

    def test_declared_neck_joins_the_pad_and_the_audit_excuses_it(self, tmp_path):
        classes = {"GND": NetClassRouting(**GND)}
        board, _, closer, attempt = self._route(tmp_path, classes)
        new = [(s.start, s.end, s.width) for s in PCB.load(str(board)).segments]
        assert attempt.applied == 1, attempt.notes
        assert len(new) == 1 and new[0][2] == pytest.approx(0.15), new
        (sx, sy), (ex, ey), _ = new[0]
        pcb = PCB.load(str(board))
        ox, oy = pcb.board_origin  # the loader reports board-relative coordinates
        assert (sx, sy) == pytest.approx((120.0 - ox, 110.0 - oy))  # the pad centre
        assert ex < 119.2125 - ox and sy == pytest.approx(ey)  # one straight leg into U1.29
        assert any(
            "welded at the class pad-escape neck 0.15 mm by the link router" in n
            for n in attempt.notes
        ), attempt.notes
        assert check_netclass_agreement(board, pcb, classes).violations == []

    @pytest.mark.parametrize("neck", [None, 0.5, 0.6])
    def test_link_stays_open_when_no_narrower_escape_is_permitted(self, tmp_path, neck):
        classes = {"GND": NetClassRouting(**{**GND, "neck_trace_width": neck})}
        board, before, _, attempt = self._route(tmp_path, classes)
        assert attempt.applied == 0
        assert any("no clearance-legal route" in n for n in attempt.notes), attempt.notes
        assert board.read_bytes() == before

    def test_neck_is_not_consulted_at_the_board_width(self, tmp_path):
        """A class at the board width has nothing to neck down to: unchanged behaviour."""
        classes = {
            "GND": NetClassRouting(name="Ground plane", trace_width=0.2, neck_trace_width=0.15)
        }
        board, before, closer, attempt = self._route(tmp_path, classes)
        assert attempt.applied == 1, attempt.notes  # the plain 0.20 mm route closes it
        widths = [s.width for s in PCB.load(str(board)).segments]
        assert widths and all(w == pytest.approx(0.2) for w in widths), widths
        assert not any("link router" in n for n in attempt.notes), attempt.notes


class TestEscapePolyline:
    LAND = box(119.2, 109.8, 120.8, 110.2)  # U1.2 in absolute coordinates

    def _route(self, segments, vias=()):
        return SimpleNamespace(segments=list(segments), vias=[tuple(v) for v in vias])

    def test_one_leg_from_the_pad(self):
        from kicad_tools.router.oracle_completion import _escape_polyline

        r = self._route([(119.5, 110.0, 121.3, 110.0, "F.Cu")])
        assert _escape_polyline(r, self.LAND, "F.Cu") == [(119.5, 110.0), (121.3, 110.0)]

    def test_leg_order_and_orientation_do_not_matter(self):
        from kicad_tools.router.oracle_completion import _escape_polyline

        r = self._route(
            [(121.0, 110.5, 121.5, 110.5, "F.Cu"), (119.6, 110.0, 121.0, 110.5, "F.Cu")]
        )
        assert _escape_polyline(r, self.LAND, "F.Cu") == [
            (119.6, 110.0),
            (121.0, 110.5),
            (121.5, 110.5),
        ]

    def test_the_path_must_end_at_the_via(self):
        from kicad_tools.router.oracle_completion import _escape_polyline

        r = self._route([(119.5, 110.0, 121.3, 110.0, "F.Cu")], vias=[(121.3, 110.0)])
        assert _escape_polyline(r, self.LAND, "F.Cu")[-1] == (121.3, 110.0)
        r = self._route([(119.5, 110.0, 121.3, 110.0, "F.Cu")], vias=[(121.9, 110.0)])
        assert _escape_polyline(r, self.LAND, "F.Cu") is None
        r = self._route(
            [(119.5, 110.0, 121.3, 110.0, "F.Cu")], vias=[(121.3, 110.0), (122.0, 110.0)]
        )
        assert _escape_polyline(r, self.LAND, "F.Cu") is None

    def test_other_layers_unchained_or_pad_less_copper_is_refused(self):
        from kicad_tools.router.oracle_completion import _escape_polyline

        other_layer = self._route(
            [(119.5, 110.0, 121.3, 110.0, "F.Cu"), (121.3, 110.0, 121.3, 112.0, "B.Cu")]
        )
        assert _escape_polyline(other_layer, self.LAND, "F.Cu") is None
        unchained = self._route(
            [(119.5, 110.0, 121.3, 110.0, "F.Cu"), (122.5, 111.0, 123.0, 111.0, "F.Cu")]
        )
        assert _escape_polyline(unchained, self.LAND, "F.Cu") is None
        padless = self._route([(121.0, 110.0, 121.3, 110.0, "F.Cu")])
        assert _escape_polyline(padless, self.LAND, "F.Cu") is None
        assert _escape_polyline(self._route([]), self.LAND, "F.Cu") is None
