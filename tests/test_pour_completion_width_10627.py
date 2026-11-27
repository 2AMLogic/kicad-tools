"""Pour completion writes its copper at the pour net's effective width (Issue #10627).

Board 05's unattended router closed every ``+5V`` / ``GND`` pour link at the
0.20 mm board width although both nets sit in a 0.50 mm ``Power`` class.  Both
emitters -- the pad-to-via weld (``run_stitch``) and the routed link
(``route_link``) -- must size candidates for, and write, ``pour_width(net)``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("shapely")

from kicad_tools.router.oracle_completion import (  # noqa: E402
    ClosureAttempt,
    OracleEndpoint,
    OracleLink,
    PourLinkCloser,
)
from kicad_tools.router.rules import NetClassRouting  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402

from .test_oracle_completion import TestIslandJoinTargetsPlane  # noqa: E402

WELD_FIXTURE = Path(__file__).parent / "fixtures" / "stitch-5388" / "continuous.kicad_pcb"


def _classes(**widths: float | None) -> dict[str, NetClassRouting]:
    return {
        net: NetClassRouting(name=f"C{net}", trace_width=w) if w is not None else None
        for net, w in widths.items()
    }


def _closer(board_width: float, classes: dict | None, **kw) -> PourLinkCloser:
    ncm = None if classes is None else {k: v for k, v in classes.items() if v is not None}
    return PourLinkCloser(
        via_size=0.6,
        via_drill=0.3,
        clearance=0.2,
        trace_width=board_width,
        net_class_map=ncm,
        **kw,
    )


def _segs(path: Path):
    return [(s.start, s.end, round(s.width, 6)) for s in PCB.load(str(path)).segments]


def _new_widths(path: Path, before: list) -> list[float]:
    old = list(before)
    out = []
    for seg in _segs(path):
        if seg in old:
            old.remove(seg)
        else:
            out.append(seg[2])
    return out


def _weld(tmp_path: Path, board_width: float, classes: dict | None) -> list[float]:
    board = tmp_path / "w.kicad_pcb"
    shutil.copy(WELD_FIXTURE, board)
    before = _segs(board)
    closer = _closer(board_width, classes)
    attempt = ClosureAttempt()
    closer._weld_pads(board, {"GNDA": {"C1.1"}}, attempt)
    new = _new_widths(board, before)
    assert new, f"no copper written: {attempt.notes}"
    return new


def _pad_end() -> OracleEndpoint:
    return OracleEndpoint(
        description="Pad 1 [GND] of R1",
        kind="pad",
        net="GND",
        x=10.0,
        y=10.0,
        layer="F.Cu",
        ref="R1",
        pad="1",
    )


def _link(tmp_path: Path, board_width: float, classes: dict | None) -> list[float]:
    board = TestIslandJoinTargetsPlane()._board(tmp_path)
    before = _segs(board)
    zone = OracleEndpoint(description="Zone [GND]", kind="zone", net="GND", x=10.0, y=10.0)
    attempt = ClosureAttempt()
    _closer(board_width, classes)._route_links(
        board, [OracleLink(net="GND", a=_pad_end(), b=zone)], attempt
    )
    assert attempt.applied == 1, attempt.notes
    new = _new_widths(board, before)
    assert new
    return new


ROWS = [
    # (board width, class width, expected written width)
    (0.20, None, 0.20),
    (0.20, 0.50, 0.50),
    (0.50, 0.50, 0.50),
]


class TestPourWidth:
    def test_resolution(self):
        c = _closer(0.2, _classes(A=0.5, B=0.1))
        assert c.pour_width("A") == 0.5
        assert c.pour_width("B") == 0.1  # narrower than the board is honoured
        assert c.pour_width("MISSING") == 0.2
        assert c.pour_width(None) == 0.2
        assert _closer(0.2, None).pour_width("A") == 0.2

    def test_neck_trace_width_does_not_narrow_or_widen(self):
        ncm = {"A": NetClassRouting(name="P", trace_width=0.5, neck_trace_width=0.3)}
        assert _closer(0.2, ncm).pour_width("A") == 0.5

    def test_link_rules_carry_the_width(self):
        from kicad_tools.router.link_router import LinkRouteRules

        base = LinkRouteRules(trace_width=0.2, clearance=0.2, via_size=0.6, via_drill=0.3)
        pcb = SimpleNamespace(nets={1: SimpleNamespace(name="A")})
        assert _closer(0.2, _classes(A=0.5)).link_rules(base, pcb, "A").trace_width == 0.5
        assert _closer(0.2, None).link_rules(base, pcb, "A") is base


class TestWrittenWidths:
    @pytest.mark.parametrize(("board_w", "class_w", "expected"), ROWS)
    def test_weld_width(self, tmp_path, board_w, class_w, expected):
        widths = _weld(tmp_path, board_w, _classes(GNDA=class_w))
        assert widths and all(w == pytest.approx(expected) for w in widths), widths

    @pytest.mark.parametrize(("board_w", "class_w", "expected"), ROWS)
    def test_link_width(self, tmp_path, board_w, class_w, expected):
        widths = _link(tmp_path, board_w, _classes(GND=class_w))
        assert all(w == pytest.approx(expected) for w in widths), widths

    def test_declared_neck_still_writes_full_class_width(self, tmp_path):
        ncm = {"GND": NetClassRouting(name="P", trace_width=0.5, neck_trace_width=0.3)}
        widths = _link(tmp_path, 0.2, ncm)
        assert all(w == pytest.approx(0.5) for w in widths), widths


class TestWeldDispatch:
    @pytest.fixture
    def calls(self, monkeypatch):
        from kicad_tools.cli import stitch_cmd

        calls: list[dict] = []

        def fake(pcb_path, nets, **kw):
            calls.append({"nets": list(nets), **kw})
            return SimpleNamespace(
                vias_added=[], already_connected=0, pads_skipped=[], traces_added=[]
            )

        monkeypatch.setattr(stitch_cmd, "run_stitch", fake)
        return calls

    def test_mixed_classes_dispatch_one_run_per_width(self, tmp_path, calls):
        board = tmp_path / "b.kicad_pcb"
        shutil.copy(WELD_FIXTURE, board)
        closer = _closer(0.2, _classes(GNDA=0.5, VCC=0.5, SIG=0.3))
        closer._weld_pads(
            board,
            {"VCC": {"U1.1"}, "GNDA": {"C1.1"}, "SIG": {"R1.1"}, "OTHER": {"R2.1"}},
            ClosureAttempt(),
        )
        by_width = {c["trace_width"]: c for c in calls}
        assert by_width[0.5]["nets"] == ["GNDA", "VCC"]
        assert by_width[0.5]["only_pads"] == frozenset({"C1.1", "U1.1"})
        assert by_width[0.3]["nets"] == ["SIG"]
        assert by_width[0.2]["nets"] == ["OTHER"]
        assert [c["trace_width"] for c in calls] == [0.5, 0.2, 0.3]  # sorted first-net order

    def test_classless_input_is_a_single_scalar_run(self, tmp_path, calls):
        board = tmp_path / "b.kicad_pcb"
        shutil.copy(WELD_FIXTURE, board)
        _closer(0.2, None)._weld_pads(board, {"B": {"U1.1"}, "A": {"C1.1"}}, ClosureAttempt())
        assert len(calls) == 1
        assert calls[0]["nets"] == ["A", "B"] and calls[0]["trace_width"] == 0.2

    def test_exhausted_pads_are_kept_per_group(self, tmp_path, monkeypatch):
        from kicad_tools.cli import stitch_cmd

        def fake(pcb_path, nets, **kw):
            pad = SimpleNamespace(reference="C1", pad_number="1", net_name=nets[0])
            skipped = [(pad, "no site")] if nets == ["GNDA"] else []
            return SimpleNamespace(
                vias_added=[],
                already_connected=0,
                pads_skipped=skipped,
                skip_details=skipped,
                traces_added=[],
            )

        monkeypatch.setattr(stitch_cmd, "run_stitch", fake)
        board = tmp_path / "b.kicad_pcb"
        shutil.copy(WELD_FIXTURE, board)
        closer = _closer(0.2, _classes(GNDA=0.5))
        attempt = ClosureAttempt()
        closer._weld_pads(board, {"GNDA": {"C1.1"}, "OTHER": {"R2.1"}}, attempt)
        assert closer.exhausted_stitch_pads == {"C1.1"}
        assert attempt.notes == ["GNDA C1.1: no site"]


class TestWideCopperThatCannotFit:
    def _gap_board(self, tmp_path: Path) -> Path:
        # Two GND pads joined only through a 0.70 mm slot between foreign walls.
        def fp(ref, x, y, net_n, net, w, h):
            return (
                f'  (footprint "T:{ref}" (layer "F.Cu")\n    (at {x:g} {y:g})\n'
                f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
                f'    (pad "1" smd rect (at 0 0) (size {w:g} {h:g}) (layers "F.Cu") '
                f'(net {net_n} "{net}"))\n  )\n'
            )

        text = (
            '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
            '  (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n'
            '  (net 0 "")\n  (net 1 "GND")\n  (net 2 "SIG")\n'
            + fp("R1", 10, 10, 1, "GND", 0.8, 0.8)
            + fp("R2", 14, 10, 1, "GND", 0.8, 0.8)
            # Full-height walls leave only a 0.70 mm slot (y 9.65..10.35) over
            # x in [11, 13]: a 0.20 mm track needs 0.2 + 2*0.2 = 0.6, a
            # 0.50 mm one needs 0.9, and there is no way around.
            + fp("W1", 12, 20.175, 2, "SIG", 2.0, 19.65)
            + fp("W2", 12, 4.825, 2, "SIG", 2.0, 9.65)
            + '  (gr_rect (start 0 0) (end 30 30) (layer "Edge.Cuts") (width 0.1))\n)\n'
        )
        path = tmp_path / "gap.kicad_pcb"
        path.write_text(text)
        return path

    def _run(self, tmp_path, class_w):
        board = self._gap_board(tmp_path)
        before = board.read_bytes()
        link = OracleLink(
            net="GND",
            a=_pad_end(),
            b=OracleEndpoint(
                description="Pad 1 [GND] of R2",
                kind="pad",
                net="GND",
                x=14.0,
                y=10.0,
                layer="F.Cu",
                ref="R2",
                pad="1",
            ),
        )
        attempt = ClosureAttempt()
        _closer(0.2, _classes(GND=class_w))._route_links(board, [link], attempt)
        return board, before, attempt

    def test_narrow_control_closes(self, tmp_path):
        board, _, attempt = self._run(tmp_path, 0.2)
        assert attempt.applied == 1, attempt.notes
        assert all(w == pytest.approx(0.2) for w in _new_widths(board, []))

    def test_wide_class_stays_open_and_writes_nothing(self, tmp_path):
        board, before, attempt = self._run(tmp_path, 0.5)
        assert attempt.applied == 0
        assert any("no clearance-legal route" in n for n in attempt.notes), attempt.notes
        assert board.read_bytes() == before  # no undersized fallback copper


class TestWeldReachUsesPourWidth:
    def test_wide_class_reaches_a_neighbour_the_board_width_misses(self, tmp_path):
        from kicad_tools.cli.stitch_cmd import max_via_reach_mm
        from kicad_tools.router.oracle_completion import WELD_ESCAPE_DISTANCE_MM, WELD_OFFSET_MM

        from .test_pour_link_class_clearance_6374 import _board, _box

        reach = max_via_reach_mm(0.8, 0.8, WELD_OFFSET_MM, WELD_ESCAPE_DISTANCE_MM)
        # OSC_IN edge 0.1 mm beyond the 0.20 mm-width threshold D + 0.3 + 0.5,
        # i.e. inside the 1.0 mm-width threshold D + 0.5 + 0.5.
        x0 = 10.0 + reach + 0.3 + 0.5 + 0.1
        board = _board(tmp_path, _box("Y1", 2, "OSC_IN", x0, 9.0, x0 + 0.4, 11.0))

        def clearance(width: float) -> float:
            ncm = {
                "GND": NetClassRouting(name="P", clearance=0.2, trace_width=width),
                "OSC_IN": NetClassRouting(name="O", clearance=0.5),
            }
            closer = _closer(0.2, None)
            closer.net_class_map = ncm
            return closer.weld_clearance(board, {"GND": {"R1.1"}})

        assert clearance(0.2) == pytest.approx(0.2)
        assert clearance(1.0) == pytest.approx(0.5)
