"""Read-only centerline inspection through the public analyze command."""

import json
import math

import pytest

from kicad_tools.cli.commands.analyze import run_analyze_command
from kicad_tools.cli.parser import create_parser


def zone(net, layer, rings):
    outline = "(pts (xy -1 -1) (xy 12 -1) (xy 12 8) (xy -1 8))"
    fills = "".join(
        f'(filled_polygon (layer "{layer}") (pts '
        + " ".join(f"(xy {x} {y})" for x, y in ring)
        + "))"
        for ring in rings
    )
    return f'(zone (net {net}) (layer "{layer}") (polygon {outline}) {fills})'


def rect(left, right):
    return [(left, -1), (right, -1), (right, 8), (left, 8)]


def track(net, layer, x1, x2, y, width=0.25):
    return (
        f'(segment (start {x1} {y}) (end {x2} {y}) (width {width}) (layer "{layer}") (net {net}))'
    )


def board(tmp_path, *, reverse=False, missing=False, extra=""):
    order = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    if reverse:
        order[1:3] = reversed(order[1:3])
    dielectric = '(layer "core" (type "core") (thickness 0.2) (epsilon_r 4.2))'
    stack = dielectric.join(f'(layer "{name}" (type "copper") (thickness 0.035))' for name in order)
    # The outline encloses both midpoints. The saved GND fill has a bridged hole
    # between x=4 and 7 crossing LONG (y=2), but not SHORT (y=5).
    shell = [
        (-1, -1),
        (8, -1),
        (8, 2),
        (7, 2),
        (7, 1),
        (4, 1),
        (4, 3),
        (7, 3),
        (7, 2),
        (8, 2),
        (8, 8),
        (-1, 8),
    ]
    resistors = "".join(
        f'(footprint "R" (layer "F.Cu") (at {x} {y}) '
        f'(property "Reference" "R{i}") (property "Value" "100k") '
        f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {net}))'
        f'(pad "2" smd rect (at 0 1.5) (size 1 1) (layers "F.Cu") (net {3 if i % 2 == 0 else 4})))'
        for i, (x, y, net) in enumerate([(0, 2, 1), (10, 2, 1), (0, 5, 2), (3, 5, 2)], 1)
    )
    path = tmp_path / "divider.kicad_pcb"
    path.write_text(f"""(kicad_pcb (version 20250210)
    (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" mixed))
    (setup (stackup {stack}))
    (net 1 "LONG") (net 2 "SHORT") (net 3 "GND") (net 4 "PWR") (net 5 "EMPTY")
    {resistors}
    {track(1, "F.Cu", 10, 2, 2)} {track(1, "F.Cu", 0, 2, 2, 0.2)} {track(2, "F.Cu", 0, 3, 5)}
    {zone(3, "In1.Cu", [] if missing else [shell, rect(0, 2)])}
    {zone(4, "In1.Cu", [rect(8, 11)])}
    {zone(3, "In2.Cu", [rect(11, 12)])}
    {extra})""")
    return path


def inspect(path, *options):
    args = create_parser().parse_args(["analyze", "reference-coverage", str(path), *options])
    return run_analyze_command(args)


def test_pair_read_only_and_supported_spans(tmp_path, capsys):
    path = board(tmp_path)
    original = path.read_bytes()
    options = ["--pair", "SHORT", "LONG", "--format", "json"]
    assert inspect(path, *options) == 0
    output = capsys.readouterr().out
    assert inspect(path, *options) == 0
    assert capsys.readouterr().out == output
    assert path.read_bytes() == original
    report = json.loads(output)
    layer = report["nets"]["LONG"]["layers"]["F.Cu"]
    assert layer["widths_mm"] == [0.2, 0.25]
    ref = layer["references"]["In1.Cu"]
    assert ref["by_net_mm"] == {"GND": 5, "PWR": 2}
    assert ref["void_spans"] == [{"path": 0, "start_mm": 4, "end_mm": 7}]
    assert ref["covered_mm"] == 7
    assert ref["uncovered_mm"] == 3
    short = report["nets"]["SHORT"]["layers"]["F.Cu"]
    assert short["references"]["In1.Cu"]["coverage"] == "complete"
    assert report["pair"]["delta"]["length_mm"] == 7
    assert report["pair"]["delta"]["layers"]["F.Cu"]["widths_added_mm"] == [0.2]
    delta = report["pair"]["delta"]["layers"]["F.Cu"]["references"]["In1.Cu"]
    assert delta["uncovered_mm"] == 3
    assert delta["by_net_mm"] == {"GND": 2, "PWR": 2}
    spans = layer["paths"][0]["spans"]
    voids = [s for s in spans if s["reference_nets"]["In1.Cu"] == []]
    assert [(s["start_mm"], s["end_mm"], s["estimate"]) for s in voids] == [(4, 7, None)]
    for span in spans:
        if span not in voids:
            assert span["estimate"]["z0_ohm"] > 0
            assert span["estimate"]["delay_ps_per_mm"] > 0
    assert set(report["unmodeled"]) >= {
        "incomplete coverage",
        "vias",
        "pads",
        "fringing",
        "coupling to switching copper",
    }
    assert "capacitance" not in output
    assert inspect(path, "--net", "LONG") == 0
    text = capsys.readouterr().out
    assert "In1.Cu" in text and "void" in text and "GND" in text


@pytest.mark.parametrize("missing,reverse", [(True, False), (False, True)])
def test_missing_fill_is_unknown_and_reordered_stack_changes_reference(
    tmp_path, capsys, missing, reverse
):
    assert (
        inspect(board(tmp_path, missing=missing, reverse=reverse), "--net", "LONG", "-f", "json")
        == 0
    )
    layer = json.loads(capsys.readouterr().out)["nets"]["LONG"]["layers"]["F.Cu"]
    name = "In2.Cu" if reverse else "In1.Cu"
    ref = layer["references"][name]
    assert ref["coverage"] == ("unknown" if missing else "none")
    assert ref["uncovered_mm"] == (None if missing else 10)
    assert all(s["estimate"] is None for s in layer["paths"][0]["spans"])


def test_inner_arc_via_empty_and_unknown_net(tmp_path, capsys):
    extra = """(via blind (at 10 2) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1))
    (arc (start 10 2) (mid 11 3) (end 10 4) (width 0.25) (layer "In1.Cu") (net 1))"""
    path = board(tmp_path, extra=extra)
    assert inspect(path, "--net", "LONG", "--net", "EMPTY", "-f", "json") == 0
    nets = json.loads(capsys.readouterr().out)["nets"]
    assert nets["EMPTY"]["layers"] == {}
    assert nets["LONG"]["via_count"] == 1
    inner = nets["LONG"]["layers"]["In1.Cu"]
    assert nets["LONG"]["layers"].keys() == {"F.Cu", "In1.Cu"}
    assert inner["via_count"] == 1
    assert inner["length_mm"] == pytest.approx(math.pi, abs=1e-6)
    assert set(inner["references"]) == {"F.Cu", "In2.Cu"}
    assert all(s["estimate"] is None for s in inner["paths"][0]["spans"])
    assert inspect(path, "--net", "TYPO") != 0
    assert "TYPO" in capsys.readouterr().err


def test_stripline_estimate_only_where_both_sides_overlap(tmp_path, capsys):
    extra = track(1, "In1.Cu", 0, 10, 0)
    extra += zone(3, "F.Cu", [rect(0, 6)]) + zone(4, "In2.Cu", [rect(4, 10)])
    assert inspect(board(tmp_path, extra=extra), "--net", "LONG", "-f", "json") == 0
    layer = json.loads(capsys.readouterr().out)["nets"]["LONG"]["layers"]["In1.Cu"]
    spans = layer["paths"][0]["spans"]
    assert [(s["start_mm"], s["end_mm"]) for s in spans if s["estimate"]] == [(4, 6)]
    assert spans[1]["estimate"]["model"] == "stripline"
    assert spans[1]["estimate"]["z0_ohm"] > 0


def test_no_explicit_stackup_preserves_coverage_without_preset_estimate(tmp_path, capsys):
    path = board(tmp_path)
    path.write_text(
        "\n".join(line for line in path.read_text().splitlines() if "(stackup" not in line)
    )
    assert inspect(path, "--net", "SHORT", "-f", "json") == 0
    layer = json.loads(capsys.readouterr().out)["nets"]["SHORT"]["layers"]["F.Cu"]
    assert layer["references"]["In1.Cu"]["covered_mm"] == 3
    assert all(span["estimate"] is None for span in layer["paths"][0]["spans"])
