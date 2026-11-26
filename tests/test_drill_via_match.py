"""Round drill hits, in the coordinate frame shipped to the fabricator."""

import pytest

from kicad_tools.export.drill_via_match import match_drill_vias, parse_excellon
from kicad_tools.schema.pcb import PCB


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "sample.kicad_pcb"
    path.write_text("""(kicad_pcb (version 20240108) (generator "test")
      (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))
      (setup (aux_axis_origin 100 50))
      (via (at 101.2504 53) (size .65) (drill .3) (layers "F.Cu" "B.Cu")
           (uuid "11111111-1111-4111-8111-111111111111"))
      (via blind (at 102 54) (size .55) (drill .2) (layers "In1.Cu" "In2.Cu"))
      (footprint "Test:Holes" (layer "F.Cu") (at 104 55 90)
         (pad "" thru_hole circle (at 1 0) (size .8 .8) (drill .3) (layers "*.Cu"))
         (pad "" np_thru_hole circle (at 2 0) (size .8 .8) (drill .3) (layers "*.Cu"))))
    """)
    return PCB.load(path)


def drill(body, *, header="METRIC\nT1C0.305", function="Plated,1,4,PTH"):
    attr = f"; #@! TF.FileFunction,{function}\n" if function else ""
    return f"M48\n{attr}{header}\n%\nG90\nG05\n{body}\nM30\n"


INNER = drill("T1\nX2.0Y-4.0", header="METRIC\nT1C0.200", function="Plated,2,3,Buried")
DECIMAL = drill("T1\nX1.25Y-3.0")


@pytest.mark.parametrize(
    ("zeros", "x", "y"),
    [("TZ", "1250", "-3000"), ("LZ", "00125", "-003"), ("", "001250", "-003000")],
)
def test_decimal_and_fixed_point_give_same_verified_matches(board, zeros, x, y):
    header = ";FILE_FORMAT=3:3\nMETRIC" + ("," + zeros if zeros else "") + "\nT1C0.305"
    fixed = drill(f"T1\nX{x}Y{y}", header=header)

    def match(content):
        return match_drill_vias(
            board, {"top-PTH.drl": content, "top-in1-in2.drl": INNER}, use_aux_origin=True
        )

    result = match(fixed)
    assert result == match(DECIMAL)
    assert (result.expected, result.matched, result.reasons) == (2, 2, ())
    outer, buried = result.matches
    assert outer.via_uuid == board.vias[0].uuid
    assert outer.center_mm == (1.25, -3.0)
    assert outer.diameter_mm == 0.305
    assert outer.faces == ("F.Cu", "B.Cu")
    assert buried.via_index == 1
    assert buried.faces == ()


def test_inch_and_kicad_fixed_point_header():
    parsed = parse_excellon(
        "board.drl",
        drill(
            "T02\nX005Y-01",
            header=(
                "; FORMAT={2:4/ absolute / inch / suppress trailing zeros}\n"
                "INCH,LZ\n; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT02C0.0120"
            ),
            function="MixedPlating,1,2",
        ),
    )
    (hit,) = parsed.hits
    assert parsed.units == "inch"
    assert hit.plated is True
    assert hit.center_mm == pytest.approx((12.7, -25.4))
    assert hit.diameter_mm == pytest.approx(0.3048)
    assert hit.tolerance_mm == pytest.approx((0.00005 * 25.4,) * 2)


def test_component_holes_and_unplated_tools_are_excluded(board):
    # Each test via coincides with a different physical pad occurrence (same pin number).
    board.vias[0].position = (104, 54)
    board.vias[1].position = (104, 53)
    board.vias[1].layers = ["F.Cu", "B.Cu"]
    content = drill("T1\nX4.0Y-4.0\nX4.0Y-3.0", function=None)
    result = match_drill_vias(board, {"board-PTH.drl": content}, use_aux_origin=True)
    assert result.matched == 0
    assert "unmatched_via" in result.reasons
    board.vias[0].position = (101.25, 53)
    header = (
        "METRIC\n; #@! TA.AperFunction,NonPlated,NPTH,ComponentDrill\nT1C0.305\n"
        "; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT2C0.305"
    )
    for tool, matched in [(1, 0), (2, 1)]:
        merged = drill(f"T{tool}\nX1.25Y-3.0", header=header, function="MixedPlating,1,4")
        assert (
            match_drill_vias(board, {"board.drl": merged}, use_aux_origin=True).matched == matched
        )
    assert (
        match_drill_vias(
            board, {"board-NPTH.drl": drill("T1\nX1.25Y-3.0", function=None)}, use_aux_origin=True
        ).matched
        == 0
    )


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (DECIMAL.replace("M30", "X1.250Y-3.000\nM30"), "ambiguous_hit"),
        (DECIMAL.replace("1.25", "1.251"), "unmatched_via"),
        (DECIMAL.replace("X1.25Y-3.0", "X1250Y-3000"), "format_unknown"),
        (drill("T1\nX1.25Y-3.0", function=None), "plating_unknown"),
        (DECIMAL.replace("G90", "G91"), "registration_unknown"),
        ("%TF.FileFunction,Plated,1,4,PTH*%", "format_unknown"),
    ],
)
def test_incomplete_file_preserves_independent_verified_via(board, content, reason):
    result = match_drill_vias(board, {"top.drl": content, "inner.drl": INNER}, use_aux_origin=True)
    assert (result.expected, result.matched) == (2, 1)
    assert result.matches[0].via_index == 1
    assert reason in result.reasons


def test_do_not_register_by_searching_for_an_offset(board):
    files = {"board-PTH.drl": DECIMAL}
    assert "registration_unknown" in match_drill_vias(board, files).reasons
    assert match_drill_vias(board, files, use_aux_origin=False).matched == 0
    absolute = DECIMAL.replace("X1.25Y-3.0", "X101.25Y-53.0")
    assert match_drill_vias(board, {"board-PTH.drl": absolute}, use_aux_origin=False).matched == 1


def test_one_hit_cannot_verify_two_vias_or_the_wrong_span(board):
    board.vias[1].position = board.vias[0].position
    files = {"board-PTH.drl": DECIMAL}
    assert match_drill_vias(board, files, use_aux_origin=True).matched == 1
    board.vias[1].layers = board.vias[0].layers
    result = match_drill_vias(board, files, use_aux_origin=True)
    assert result.matched == 0
    assert "ambiguous_hit" in result.reasons


def test_slot_endpoints_are_not_round_hits(board):
    slots = "T1\nG00X1.25Y-3.0\nM15\nG01X1.50Y-3.0\nM16\nG05\nX1.25Y-3.0G85X1.50Y-3.0\nG05"
    result = match_drill_vias(board, {"board-PTH.drl": drill(slots)}, use_aux_origin=True)
    assert result.matched == 0


def test_each_axis_retains_its_own_resolution(board):
    board.vias[0].position = (101.25004, 53.0004)
    content = drill("T1\nX1.2500Y-3.0")
    assert match_drill_vias(board, {"board.drl": content}, use_aux_origin=True).matched == 1
    board.vias[0].position = (101.25006, 53.0004)
    assert match_drill_vias(board, {"board.drl": content}, use_aux_origin=True).matched == 0


def test_conflicting_format_evidence_does_not_verify_a_hit(board):
    header = "; FORMAT={3:3/ absolute / metric / suppress trailing zeros}\nMETRIC,TZ\nT1C0.305"
    content = drill("T1\nX1250Y-3000", header=header)
    result = match_drill_vias(board, {"board.drl": content}, use_aux_origin=True)
    assert result.matched == 0
    assert "format_unknown" in result.reasons


@pytest.mark.parametrize(
    ("layer", "filename", "face"),
    [("F.Cu", "board-front-in1.drl", "F.Cu"), ("B.Cu", "board-in1-back.drl", "B.Cu")],
)
def test_zip_members_and_minimal_blind_span(board, tmp_path, layer, filename, face):
    from zipfile import ZipFile

    board.vias[1].layers = ["In1.Cu", layer]
    path = tmp_path / "fab.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("copper.gbr", "not drill data")
        archive.writestr("dir/board-PTH.drl", DECIMAL)
        archive.writestr("dir/" + filename, drill("T1\nX2.0Y-4.0", function=None))
    with ZipFile(path) as archive:
        result = match_drill_vias(board, archive, use_aux_origin=True)
    assert result.reasons == ()
    assert result.matched == result.expected == 2
    assert result.matches[1].faces == (face,)


def test_missing_or_conflicting_span_leaves_other_hits_verified(board):
    for function, name in [("Plated", "renamed.drl"), ("Plated,1,4,PTH", "board-in1-in2.drl")]:
        unknown = drill("T1\nX2.0Y-4.0", function=function)
        result = match_drill_vias(
            board, {"board-PTH.drl": DECIMAL, name: unknown}, use_aux_origin=True
        )
        assert result.matched == 1
        assert result.matches[0].via_index == 0
        assert "span_unknown" in result.reasons


def test_contradictory_kicad_units_are_unknown(board):
    header = "; FORMAT={-:-/ absolute / inch / decimal}\nMETRIC\nT1C0.305"
    content = drill("T1\nX1.25Y-3.0", header=header)
    result = match_drill_vias(board, {"board.drl": content}, use_aux_origin=True)
    assert result.matched == 0
    assert "format_unknown" in result.reasons


def _outline_board(tmp_path, name="sample"):
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text("""(kicad_pcb (version 20240108) (generator "test")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (setup (aux_axis_origin 100 50))
      (gr_rect (start 100 100) (end 140 130) (layer "Edge.Cuts") (width 0.1))
      (via (at 140 125) (size .65) (drill .3) (layers "F.Cu" "B.Cu")
           (uuid "11111111-1111-4111-8111-111111111111"))
      (via (at 145 131) (size .65) (drill .5) (layers "F.Cu" "B.Cu")
           (uuid "22222222-2222-4222-8222-222222222222")))
    """)
    return PCB.load(path)


@pytest.mark.parametrize("use_aux_origin", [True, False])
def test_translated_edge_cuts_origin_is_subtracted_before_lookup(tmp_path, use_aux_origin):
    board = _outline_board(tmp_path)
    assert board.board_origin == (100, 100)
    # Sheet position -> drill frame: minus the aux origin (or none), Y negated.
    ox, oy = (100, 50) if use_aux_origin else (0, 0)
    hit = f"X{140 - ox}.0Y{-(125 - oy)}.0"
    content = drill(f"T1\n{hit}", header="METRIC\nT1C0.300", function="Plated,1,2,PTH")
    result = match_drill_vias(board, {"board.drl": content}, use_aux_origin=use_aux_origin)
    assert [m.via_index for m in result.matches] == [0]
    assert result.matches[0].diameter_mm == 0.3
    assert result.matches[0].center_mm == (140 - ox, -(125 - oy))


@pytest.mark.parametrize("name", ["board-PTH", "board-NPTH", "board-front-in1"])
def test_board_name_that_looks_like_a_drill_suffix_is_not_evidence(tmp_path, name):
    board = _outline_board(tmp_path, name)
    header = "METRIC\n; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300"
    content = drill("T1\nX40.0Y-75.0", header=header, function="MixedPlating,1,2")
    result = match_drill_vias(board, {f"{name}.drl": content}, use_aux_origin=True)
    assert (result.matched, result.reasons) == (1, ("unmatched_via",))
    # A genuinely conflicting split-file suffix after the board name is still rejected.
    conflicting = drill("T1\nX40.0Y-75.0", header=header, function="NonPlated,1,2,NPTH")
    result = match_drill_vias(board, {f"{name}-PTH.drl": conflicting}, use_aux_origin=True)
    assert result.matched == 0 and "plating_unknown" in result.reasons
