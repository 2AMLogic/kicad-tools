"""Observed escape cells must move their own copper on both KiCad schemas."""

import hashlib
import json
import runpy
from pathlib import Path

import pytest

from kicad_tools.sexp import parse_string

REPAIR = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "boards/02-charlieplex-led/finalize_routing.py")
)["_relocate_escapes"]


def test_release_copper_clears_the_d9_via_slit_and_c1_mask_opening():
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap

    output = Path(__file__).resolve().parents[1] / "boards/02-charlieplex-led/output"
    path = output / "charlieplex_3x3_routed.kicad_pcb"
    pcb = PCB.load(path)
    moves = json.loads((output / "readiness/drill-relocations.json").read_text())
    release_moves = {(row["net"], tuple(row["absolute_from"])) for row in moves}
    assert {("NODE_D", (156.8, 105.0)), ("VCC", (152.8, 117.4))} <= release_moves

    # The seed-42 D9 returning trace left a 0.117537 mm slit against this floor.
    gaps = check_physical_copper_gap(pcb, 0.127)
    assert not [v for v in gaps.violations if v.severity == "error"]

    # A refused release keeps the previous package and records the new
    # board's native measurements in the producer's readiness-attempt report.
    board_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    report = json.loads((output / "manufacturing/kct-check.json").read_text())
    if report["pcb_sha256"] != board_sha:
        report = json.loads((output / "readiness-attempt/kct-check.json").read_text())
    assert report["pcb_sha256"] == board_sha
    (mask,) = report["mask_copper_assessments"]
    assert mask["binding"]["source_sha256"] == board_sha
    assert mask["geometry_provenance"]["export"]["source_sha256"] == board_sha
    assert mask["coverage"] == "complete" and mask["passed"]
    c1 = [sample for sample in mask["measurements"] if sample["owner_ref"] == "C1/1"]
    assert any(sample["own_pad_entry"] and sample["conductor_net"] == "VCC" for sample in c1)


@pytest.mark.parametrize("named_nets", [False, True])
@pytest.mark.parametrize(
    "name,old,new",
    [
        ("GND", (155.1, 117.9), (154.6, 118.4)),
        ("VCC", (129.9, 119.5), (128.6, 119.5)),
        ("GND", (155.1, 117.5), (156.8, 117.5)),
        ("VCC", (139.7, 117.3), (139.2, 115.5)),
        ("VCC", (140.665, 117.3), (139.2, 115.5)),
        ("VCC", (140.665, 117.2), (139.2, 115.5)),
        ("RESET", (141.1, 118.1), (140.9, 118.5)),
        ("RESET", (141.3, 118.3), (140.9, 118.5)),
        ("NODE_B", (148.1, 105.5), (148.4, 105.5)),
        ("NODE_B", (158.9, 105.5), (158.6, 105.5)),
        ("NODE_C", (138.1, 106.0), (138.4, 106.0)),
        ("NODE_D", (156.8, 105.0), (156.625, 105.5)),
        ("VCC", (152.8, 117.4), (152.35, 116.3)),
        ("NODE_A", (148.0, 84.9), (148.0, 84.5)),
        ("NODE_D", (157.9, 106.0), (158.3, 106.0)),
        ("GND", (155.2, 117.4), (156.5, 117.4)),
    ],
)
def test_escape_variant_moves_via_and_both_layers_only_on_its_net(named_nets, name, old, new):
    own = f'"{name}"' if named_nets else "8"
    other = '"OTHER"' if named_nets else "9"
    root_nets = "" if named_nets else f'(net 8 "{name}")(net 9 "OTHER")'
    x, y = old
    doc = parse_string(f"""(kicad_pcb {root_nets}
      (via (at {x} {y}) (net {own}))
      (segment (start {x} {y}) (end 139 115) (layer "F.Cu") (net {own}))
      (segment (start 139 115) (end {x} {y}) (layer "B.Cu") (net {own}))
      (via (at {x} {y}) (net {other}))
      (segment (start {x} {y}) (end 142 120) (net {other})))""")
    REPAIR(doc)

    def point(item, key):
        return tuple(item.find(key).get_float(i) for i in range(2))

    vias = doc.find_all("via")
    tracks = doc.find_all("segment")
    assert point(vias[0], "at") == new
    assert point(tracks[0], "start") == point(tracks[1], "end") == new
    assert point(vias[1], "at") == point(tracks[2], "start") == old
    assert point(tracks[0], "end") == point(tracks[1], "start") == (139, 115)
    REPAIR(doc)
    assert point(vias[0], "at") == new  # Stable on rerun.


def test_quantized_waypoint_without_escape_via_is_not_relocated():
    doc = parse_string("""(kicad_pcb
      (segment (start 163.9 112.7) (end 157.1 105.9) (net "NODE_D"))
      (segment (start 157.1 105.9) (end 156.7 105.9) (net "NODE_D"))
      (via (at 156.7 105.9) (net "NODE_D")))""")
    from kicad_tools.sexp import serialize_sexp

    before = serialize_sexp(doc)
    REPAIR(doc)
    assert serialize_sexp(doc) == before


def test_ci_reset_position_uses_actual_board_origin():
    """Translate the reported normalized finding before selecting its escape."""
    from kicad_tools.schema.pcb import PCB

    board = (
        Path(__file__).resolve().parents[1]
        / "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb"
    )
    origin = PCB.load(board).board_origin
    assert origin == (123.5, 77.5)
    reported = (17.8, 40.8)
    raw = tuple(round(a + b, 3) for a, b in zip(origin, reported, strict=True))
    assert raw == (141.3, 118.3)
    doc = parse_string(f"""(kicad_pcb
      (via (at {raw[0]} {raw[1]}) (net "RESET")))""")
    REPAIR(doc)
    at = doc.find_all("via")[0].find("at")
    assert (at.get_float(0), at.get_float(1)) == (140.9, 118.5)
