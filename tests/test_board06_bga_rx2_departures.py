"""Board 06 moves the BGA's two RX2 barrels only as a verified pair."""

import json
import shutil
from itertools import pairwise
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from kicad_tools.router.rules import net_class_map_to_dict
from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"
NETS = ("USB3_RX2+", "USB3_RX2-")


def _id(name):
    return str(uuid5(NAMESPACE_URL, f"board06-bga-rx2/{name}"))


@pytest.fixture
def generation(monkeypatch):
    monkeypatch.syspath_prepend(str(BOARD))
    from generate_design import _clear_pad_departures

    return _clear_pad_departures


def _fixture(path, control="", *, extra_items=()):
    from generate_design import usb3_net_class

    shutil.copyfile(
        BOARD / "regression-fixture/diffpair_test_routed.kicad_pro",
        path.with_suffix(".kicad_pro"),
    )
    classes = dict.fromkeys(NETS, usb3_net_class())
    path.with_name("net_class_map.json").write_text(json.dumps(net_class_map_to_dict(classes)))
    items = [
        '(kicad_pcb (version 20260206) (generator "test") (general (thickness 1.6))',
        '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)'
        ' (1 "F.Mask" user) (3 "B.Mask" user) (25 "Edge.Cuts" user))',
        "(setup (pad_to_mask_clearance 0))",
        '(net 1 "USB3_RX2+") (net 2 "USB3_RX2-") (net 3 "OTHER")',
        f'(footprint "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm" (layer "F.Cu")'
        f' (at 10 10) (uuid "{_id("U2")}")',
        '(property "Reference" "U2" (at 0 0) (layer "F.Fab"))',
    ]
    for number, (pin, x, net) in enumerate(zip(("F5", "F6"), (0, 1.27), NETS, strict=True), 1):
        lock = "(locked yes)" if pin == "F6" and control == "pad lock" else ""
        items.append(
            f'(pad "{pin}" smd rect (at {x} 0) (size .45 .45) (layers "F.Cu" "F.Mask")'
            f' (net {number} "{net}") (uuid "{_id(pin)}") {lock})'
        )
    items.append(")")
    runs = [
        (1, "F.Cu", [(10, 10), (9.97, 10), (9.93, 9.96), (9.63, 9.66), (9.53, 9.66)]),
        (1, "In1.Cu", [(4.605, 9.66), (9.53, 9.66)]),
        (2, "F.Cu", [(11.27, 10), (10.88, 9.61)]),
        (2, "In2.Cu", [(10.88, 9.61), (10.28, 9.01)]),
        (2, "In2.Cu", [(10.28, 9.01), (5.68 if control != "skew" else 2.68, 9.01)]),
    ]
    if control == "inner tap":
        runs.append((2, "In2.Cu", [(10.88, 9.61), (11.3, 9.61)]))
    if control == "foreign inner":
        runs.append((3, "In2.Cu", [(9.1, 10.82), (9.84, 10.82)]))
    for idx, (number, layer, points) in enumerate(runs):
        if control == "changed inner" and idx == 1:
            points = [(4.62, 9.67), points[1]]
        width = 0.20 if control == "changed width" and idx == 0 else 0.275
        for piece, (a, b) in enumerate(pairwise(points)):
            items.append(
                f"(segment (start {b[0]} {b[1]}) (end {a[0]} {a[1]}) (width {width})"
                f' (layer "{layer}") (net {number}) (uuid "{_id(f"track-{idx}-{piece}")}"))'
            )
    for number, x, y in [(1, 9.53, 9.66), (2, 10.88, 9.61)]:
        lock = "(locked yes)" if control == "locked barrel" and number == 2 else ""
        tent = "(tenting (front yes) (back no))" if control == "open barrel" and number == 2 else ""
        items.append(
            f'(via (at {x} {y}) (size .60) (drill .25) (layers "F.Cu" "B.Cu")'
            f' (net {number}) (uuid "{_id(f"via-{number}")}") {lock} {tent})'
        )
    if control == "via group":
        items.append(
            f'(group "protected escape" (uuid "{_id("group")}") (members "{_id("via-2")}"))'
        )
    if control in {"back expanded", "back clear"}:
        margin = 0.18 if control == "back expanded" else 0
        items.append(
            f'(footprint "Test:Back" (layer "B.Cu") (at 9.44 11.21) (uuid "{_id("tp")}")'
            ' (property "Reference" "TP1" (at 0 0) (layer "B.Fab"))'
            f' (pad "1" smd circle (at 0 0) (size .20 .20) (layers "B.Cu" "B.Mask")'
            f' (solder_mask_margin {margin}) (net 3 "OTHER") (uuid "{_id("test point")}")))'
        )
    path.write_text("\n".join([*items, *extra_items, ")"]))


@pytest.mark.parametrize("control", ["", "back clear"])
def test_both_bga_barrels_move_and_repeat_is_byte_identical(tmp_path, generation, control):
    pcb_path = tmp_path / "rx2.kicad_pcb"
    _fixture(pcb_path, control)
    assert generation(pcb_path) == 2
    board = PCB.load(pcb_path)
    assert {
        (v.net_name, tuple(round(a, 4) for a in v.position), v.size, v.drill) for v in board.vias
    } == {("USB3_RX2+", (9.44, 10.56), 0.60, 0.25), ("USB3_RX2-", (10.71, 9.44), 0.60, 0.25)}
    assert all(s.width == 0.275 for s in board.segments)
    assert len([s for s in board.segments if s.layer == "F.Cu"]) == 2
    repaired = pcb_path.read_bytes()
    assert generation(pcb_path) == 0
    assert pcb_path.read_bytes() == repaired


@pytest.mark.parametrize(
    "control",
    [
        "back expanded",
        "open barrel",
        "via group",
        "locked barrel",
        "pad lock",
        "changed inner",
        "changed width",
        "inner tap",
        "foreign inner",
        "skew",
    ],
)
def test_bga_repair_refuses_the_entire_pair(tmp_path, generation, control):
    pcb_path = tmp_path / "rx2.kicad_pcb"
    _fixture(pcb_path, control)
    before = pcb_path.read_bytes()
    assert generation(pcb_path) == 0
    assert pcb_path.read_bytes() == before


def _neighbor_drill(x, y):
    return (
        f'(via (at {x} {y}) (size 0.45) (drill 0.25) (layers "F.Cu" "B.Cu")'
        f' (net 3) (uuid "{_id("neighbor drill")}"))'
    )


@pytest.mark.parametrize(
    ("rule", "item", "clear_minimum"),
    [
        pytest.param("hole_to_hole", _neighbor_drill(9.44, 11.26), 0.4, id="F5-drill"),
        pytest.param("hole_to_hole", _neighbor_drill(11.13, 8.88), 0.4, id="F6-drill"),
        pytest.param(
            "copper_edge_clearance",
            "(gr_rect (start 0 0) (end 20 11.21) (stroke (width 0.05) (type default))"
            ' (fill none) (layer "Edge.Cuts"))',
            0.25,
            id="F5-edge",
        ),
    ],
)
@pytest.mark.parametrize("blocked", [True, False], ids=["blocked", "clear"])
def test_bga_barrels_honor_project_only_clearances(
    tmp_path, generation, rule, item, clear_minimum, blocked
):
    board = tmp_path / "rx2.kicad_pcb"
    _fixture(board, extra_items=[item])
    project = board.with_suffix(".kicad_pro")
    settings = json.loads(project.read_text())
    design = settings["board"]["design_settings"]
    design["rules"][f"min_{rule}"] = 0.5 if blocked else clear_minimum
    design["rule_severities"][rule] = "error"
    project.write_text(json.dumps(settings))
    assert not board.with_suffix(".kicad_dru").exists()

    before = board.read_bytes()
    edits = generation(board)
    if blocked:
        assert edits == 0
        assert board.read_bytes() == before
    else:
        assert edits == 2
