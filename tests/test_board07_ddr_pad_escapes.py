"""Physical controls for Board 07's single-ended DDR pad exits (issue #9960)."""

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from shapely.geometry import LineString, Point, box


def test_mask_clear_escape_respects_neighboring_via_and_preserves_its_landing(monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test")
    )
    from ddr_pad_escapes import choose_escape

    # U2/5 (DM0) in pad-relative coordinates, including the DQ4 and DQS_P vias.
    path = [(0.0, 0.0), (-0.2, 0.2), (-1.0, 0.2), (-1.8, 1.0)]
    opening = box(-0.35, -0.15, 0.35, 0.15)
    vias = [Point(-0.8, y).buffer(0.3, quad_segs=32) for y in (-0.4, 0.8)]
    width = 0.15
    plain = choose_escape(path, width, opening, [])
    guarded = choose_escape(path, width, opening, [(via, 0.15) for via in vias])
    assert plain and guarded
    assert min(LineString(plain).distance(via) for via in vias) - width / 2 < 0.15
    assert min(LineString(guarded).distance(via) for via in vias) - width / 2 >= 0.15
    assert guarded[0] == path[0] and guarded[-1] == path[-1]
    assert LineString(guarded[1:]).distance(opening) - width / 2 >= 0.15
    assert LineString(guarded).length > LineString(path).length


def test_two_leg_escape_keeps_length_at_a_via_landing(monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test")
    )
    from ddr_pad_escapes import choose_escape

    # U2/11 (DQ7) terminates on a via after only two tracks.
    path = [(0.0, 0.0), (-0.2, 0.2), (-1.0, 0.2)]
    revised = choose_escape(path, 0.15, box(-0.35, -0.15, 0.35, 0.15), [])
    assert revised and revised[-1] == path[-1]
    assert abs(LineString(revised).length - LineString(path).length) < 1e-8


@pytest.mark.parametrize("locked", [False, True], ids=["tunable", "locked-bump"])
def test_pad_repair_and_tuning_bump_are_written_together(tmp_path, monkeypatch, locked):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test")
    )
    from ddr_pad_escapes import clear_ddr_pad_escapes

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.connectivity import ConnectivityValidator

    # DM0 leaves U2/5 between the two foreign vias and reaches the far pad
    # through the rectangular tuning bump. Both bends of the bump are untapped.
    points = [
        (10, 10),
        (9.8, 10.2),
        (9, 10.2),
        (8.2, 11),
        (7.4, 11),
        (6.4, 11),
        (6.4, 12),
        (6, 12),
        (6, 11),
        (5, 11),
    ]
    segments = []
    for i, ((x, y), (xx, yy)) in enumerate(zip(points, points[1:], strict=False)):
        lock = "(locked yes)" if locked and i == 6 else ""
        segments.append(
            f"(segment (start {x} {y}) (end {xx} {yy}) (width 0.15) "
            f'(layer "F.Cu") (net 1) {lock} (uuid "{uuid5(NAMESPACE_URL, f"seg/{i}")}"))'
        )
    pads = [
        f'(footprint "T" (layer "F.Cu") (at {x} {y}) '
        f'(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")) '
        '(pad "5" smd rect (at 0 0) (size 0.7 0.3) (layers "F.Cu" "F.Mask") (net 1 "DM0")))'
        for ref, x, y in (("U2", 10, 10), ("R1", 5, 11))
    ]
    vias = [
        f'(via (at 9.2 {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        f'(net {i}) (uuid "{uuid5(NAMESPACE_URL, f"via/{i}")}"))'
        for i, y in ((2, 9.6), (3, 10.8))
    ]
    board = tmp_path / "ddr.kicad_pcb"
    board.write_text(
        "(kicad_pcb (version 20240108) (generator test) (general (thickness 1.6)) "
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (setup (pad_to_mask_clearance 0)) '
        '(net 0 "") (net 1 "DM0") (net 2 "DQ4") (net 3 "DQS_P")\n'
        + "\n".join(pads + vias + segments)
        + ")\n"
    )
    original = board.read_bytes()
    original_length = LineString(points).length
    assert clear_ddr_pad_escapes(board, ["DM0"]) == int(not locked)
    if locked:
        assert board.read_bytes() == original
    else:
        assert board.read_bytes() != original
        pcb = PCB.load(board)
        assert (
            abs(sum(LineString([s.start, s.end]).length for s in pcb.segments) - original_length)
            < 2e-6
        )
        assert not ConnectivityValidator(board).validate().issues
        assert len(pcb.vias) == 2
    first_result = board.read_bytes()
    assert clear_ddr_pad_escapes(board, ["DM0"]) == 0
    assert board.read_bytes() == first_result
