"""Board 06 keeps the RX2 barrels and repairs both connector exits together."""

import json
import shutil
from itertools import pairwise
from pathlib import Path
from uuid import NAMESPACE_OID, uuid5

import pytest
from shapely.geometry import LineString, Point

from kicad_tools.router.rules import net_class_map_to_dict
from kicad_tools.schema.pcb import PCB

BOARD06 = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"
NETS = ("USB3_RX2-", "USB3_RX2+")
OLD = (
    ((10, 10), (10, 10.15), (10.1936, 10.15), (10.95, 10.9064), (10.95, 11.1)),
    ((11, 10), (11, 10.15), (11, 10.3436), (11.7564, 11.1), (11.95, 11.1)),
)
NEW = (
    ((10, 10), (10.58, 10.58), (10.71, 10.58), (10.95, 10.82), (10.95, 11.1)),
    ((11, 10), (11.60, 10.60), (11.60, 11.10), (11.95, 11.10)),
)


@pytest.fixture
def board06(monkeypatch):
    monkeypatch.syspath_prepend(str(BOARD06))
    from generate_design import _clear_pad_departures

    return _clear_pad_departures


def _write_board(path, control=None):
    from generate_design import usb3_net_class

    shutil.copyfile(
        BOARD06 / "regression-fixture/diffpair_test_routed.kicad_pro",
        path.with_suffix(".kicad_pro"),
    )
    if control != "missing constraints":
        classes = dict.fromkeys(NETS, usb3_net_class())
        path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(classes)))
    mask = 0.2 if control == "expanded mask" else 0
    parts = [
        "(kicad_pcb (version 20240108) (generator test) (general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (2 "In1.Cu" power) (4 "In2.Cu" power) (31 "B.Cu" signal)'
        ' (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))',
        f"(setup (pad_to_mask_clearance {mask}))",
        '(net 1 "USB3_RX2-") (net 2 "USB3_RX2+") (net 3 "VBUS_USB")',
        '(footprint "Connector_USB:USB_C_Receptacle_USB2.0" (layer "F.Cu") (at 0 0)',
        '(property "Reference" "J1" (at 0 0) (layer "F.SilkS"))',
    ]
    for idx, (net, run) in enumerate(zip(NETS, OLD, strict=True), 1):
        x, y = run[0]
        lock = "(locked yes)" if idx == 2 and control == "locked pad" else ""
        parts.append(
            f'(pad "A{9 + idx}" smd rect (at {x} {y}) (size .3 .6) '
            f'(layers "F.Cu" "F.Mask") (net {idx} "{net}") {lock})'
        )
    parts.append(")")
    for idx, (net, run) in enumerate(zip(NETS, OLD, strict=True), 1):
        for piece, (a, b) in enumerate(pairwise(run)):
            uid = uuid5(NAMESPACE_OID, f"{net}/{piece}")
            lock = "(locked yes)" if (idx, piece, control) == (2, 1, "locked track") else ""
            width = 0.15 if (idx, piece, control) == (2, 1, "narrow track") else 0.275
            parts.append(
                f"(segment (start {b[0]} {b[1]}) (end {a[0]} {a[1]}) (width {width})"
                f' (layer "F.Cu") (net {idx}) {lock} (uuid "{uid}"))'
            )
        x, y = run[-1]
        lock = "(locked yes)" if idx == 2 and control == "locked barrel" else ""
        drill = 0.3 if idx == 2 and control == "different drill" else 0.25
        parts.append(
            f'(via (at {x} {y}) (size .6) (drill {drill}) (layers "F.Cu" "B.Cu") (net {idx}) {lock})'
        )
        tail = 18 if control == "skew" and idx == 2 else 15
        parts.append(
            f'(segment (start {x} {y}) (end {x} {tail}) (width .275) (layer "B.Cu") (net {idx}))'
        )
        parts.append(
            f'(footprint "Test:Land" (layer "B.Cu") (at {x} {tail}) '
            f'(property "Reference" "T{idx}" (at 0 0) (layer "B.SilkS")) '
            f'(pad "1" smd rect (at 0 0) (size .4 .4) (layers "B.Cu" "B.Mask") (net {idx} "{net}")))'
        )
    if control == "group":
        parts.append(f'(group "tuned" (members "{uuid5(NAMESPACE_OID, NETS[1] + "/2")}"))')
    if control == "foreign via":
        parts.append('(via (at 11.94 10.38) (size .45) (drill .2) (layers "F.Cu" "B.Cu") (net 3))')
    if control == "branch":
        parts.append(
            '(segment (start 11 10.3436) (end 10.6 10.5) (width .15) (layer "F.Cu") (net 2))'
        )
    path.write_text("\n".join([*parts, ")"]))


def test_connector_keeps_barrels_and_rebuilds_both_full_width_rails(tmp_path, board06):
    path = tmp_path / "rx2.kicad_pcb"
    _write_board(path)
    assert board06(path) == 2
    pcb = PCB.load(path)
    for net, points in zip(NETS, NEW, strict=True):
        tracks = [s for s in pcb.segments if s.net_name == net and s.layer == "F.Cu"]
        assert {frozenset((s.start, s.end)) for s in tracks} == {
            frozenset(edge) for edge in pairwise(points)
        }
        assert all(s.width == 0.275 for s in tracks)
        (via,) = [v for v in pcb.vias if v.net_name == net]
        assert (via.position, via.size, via.drill) == (points[-1], 0.6, 0.25)
    once = path.read_bytes()
    assert board06(path) == 0
    assert path.read_bytes() == once


@pytest.mark.parametrize(
    "control",
    [
        "group",
        "locked track",
        "locked pad",
        "locked barrel",
        "different drill",
        "narrow track",
        "branch",
        "skew",
        "missing constraints",
        "expanded mask",
    ],
)
def test_unsafe_or_altered_partner_leaves_the_file_intact(tmp_path, board06, control):
    path = tmp_path / "rx2.kicad_pcb"
    _write_board(path, control)
    before = path.read_bytes()
    assert board06(path) == 0
    assert path.read_bytes() == before


def test_vbus_barrel_clear_of_the_old_escape_refuses_the_replacement(tmp_path, board06):
    path = tmp_path / "rx2.kicad_pcb"
    _write_board(path, "foreign via")
    (via,) = [v for v in PCB.load(path).vias if v.net_name == "VBUS_USB"]
    center = Point(via.position)
    radius = (via.size + 0.275) / 2
    assert LineString(OLD[1]).distance(center) - radius == pytest.approx(0.27644, abs=1e-5)
    assert LineString(NEW[1]).distance(center) - radius == pytest.approx(0.04247, abs=1e-5)
    before = path.read_bytes()
    assert board06(path) == 0
    assert path.read_bytes() == before
