"""Controls for the physical Board 07 matched-route cleanup record."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.sexp import parse_string

ENGINEERING = (
    Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design/engineering"
)


@pytest.fixture
def cleanup(monkeypatch):
    monkeypatch.syspath_prepend(str(ENGINEERING))
    return importlib.import_module("cleanup_8603")


def track(uid, start, end, *, net="D0", layer="In2.Cu"):
    return SimpleNamespace(uuid=uid, net_name=net, layer=layer, width=0.18, start=start, end=end)


def test_fragment_replacement_uses_the_real_terminals_and_respects_source_ownership(cleanup):
    points = [(0.0, 0.0), (0.04, 0.0), (0.08, 0.02), (0.12, 0.02)]
    board = SimpleNamespace(
        board_origin=(100.0, 50.0),
        segments=[
            track("a", points[1], points[0]),
            track("b", points[1], points[2]),
            track("c", points[3], points[2]),
        ],
        vias=[],
    )
    run = [["D0", "In2.Cu", "a", [100.0, 50.0], [100.12, 50.02], 3, 1]]
    assert cleanup.fragment_edits(board, set(), run) == {
        "a": ((100.0, 50.0), (100.12, 50.02)),
        "b": None,
        "c": None,
    }
    with pytest.raises(ValueError, match="invalid fragment path"):
        cleanup.fragment_edits(board, {"c"}, run)
    board.vias = [SimpleNamespace(net_name="D0", position=points[1])]
    with pytest.raises(ValueError, match="branch or via"):
        cleanup.fragment_edits(board, set(), run)
    board.vias = []
    board.segments.append(track("branch", points[1], (0.04, 0.04)))
    with pytest.raises(ValueError, match="branch or via"):
        cleanup.fragment_edits(board, set(), run)


def test_covered_track_requires_its_entire_extent_on_the_recorded_host(cleanup):
    board = SimpleNamespace(
        board_origin=(0.0, 0.0),
        segments=[track("host", (0.0, 0.0), (3.0, 0.0)), track("short", (0.4, 0.0), (0.9, 0.0))],
    )
    group = {"net": "D0", "layer": "In2.Cu", "width": 0.18, "covered_by": [["short", "host"]]}
    edits, _ = cleanup.copper_edits(board, [group])
    assert edits == {"short": None}
    board.segments[1].end = (0.9, 0.003)
    with pytest.raises(ValueError, match="fully covered"):
        cleanup.copper_edits(board, [group])
    board.segments[1].layer = "B.Cu"
    with pytest.raises(ValueError, match="is not on"):
        cleanup.copper_edits(board, [group])


def test_record_splice_preserves_sibling_tracks_and_inherits_the_bridge_class(cleanup):
    template = '(segment (start 1 1) (end 2 2) (width 0.18) (layer "In2.Cu") (net "D0") (uuid "a"))'
    sibling = template.replace("In2.Cu", "B.Cu").replace('"a"', '"b"')
    text = f"(kicad_pcb\n{template}\n{sibling}\n)"
    new = cleanup.rewrite(
        text,
        "segment",
        {"a": [(1.0, 1.0), (1.6, 1.6)]},
        {"a": [("bridge", "In2.Cu", (1.6, 1.6), (1.6, 2.0))]},
    )
    assert sibling in new
    segments = parse_string(new).find_children("segment")
    assert [s.find_child("uuid").get_string() for s in segments] == ["a", "bridge", "b"]
    assert segments[1].find_child("width").get_float() == 0.18
    assert segments[1].find_child("layer").get_string() == "In2.Cu"
    assert segments[1].find_child("net").get_string() == "D0"
    with pytest.raises(ValueError, match="missing segment"):
        cleanup.rewrite(text, "segment", {"c": None})


def test_staged_artwork_changes_only_reference_and_silk_on_existing_land(cleanup):
    finish = importlib.import_module("cleanup_artwork_8603")
    # The first UUID in print order is a nested property, as in a stock fiducial.
    original = """(kicad_pcb (title_block (title "Board") (rev "1.0"))
      (footprint "FP:R" (property "Reference" "R1" (layer "F.SilkS") (uuid "text"))
        (uuid "fp") (at 10 20) (pad "1" smd rect (at 0 0) (size 1 2) (net "D0"))
        (fp_line (start 0 0) (end 0 1) (layer "F.Fab"))))"""
    moved = parse_string('(property "Reference" "R1" (at 1 2) (layer "F.SilkS") (uuid "text"))')
    mark = '(footprint "Fiducial:stock" (uuid "mark") (attr board_only exclude_from_bom))'
    result = parse_string(finish.finish_board(original, {"fp": [moved]}, {"FID1": mark}))
    assert result.find_child("title_block").find_child("rev").get_string() == "B"
    fp, fid = result.find_children("footprint")
    before = parse_string(original).find_child("footprint")
    for name in ("uuid", "at", "pad", "fp_line"):
        assert fp.find_child(name).to_string() == before.find_child(name).to_string()
    assert fp.find_child("property").to_string() == moved.to_string()
    assert fid.find_child("uuid").get_string() == "mark"


def test_refuses_unreviewed_candidate_before_publishing_a_partial_directory(tmp_path, cleanup):
    original = tmp_path / "retuned"
    original.mkdir()
    (original / "candidate.kicad_pcb").write_text("(kicad_pcb)")
    out = tmp_path / "cleaned"
    with pytest.raises(ValueError, match="SHA-256"):
        cleanup.prepare(original, out)
    assert not out.exists()


def test_new_via_requires_two_real_ends_on_the_matching_layer_pair(tmp_path, cleanup, monkeypatch):
    pcb = tmp_path / "transition.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n")
    board = SimpleNamespace(
        board_origin=(100.0, 50.0),
        segments=[
            track("inner", (1.0, 1.0), (4.0, 1.0)),
            track("outer", (4.0, 1.0), (4.0, 4.0), layer="F.Cu"),
        ],
        vias=[],
    )
    monkeypatch.setattr(cleanup.PCB, "load", lambda path: board)
    via = ["handoff", "D0", [104.0, 51.0]]
    cleanup.add_layer_handoffs(pcb, [via])
    new = parse_string(pcb.read_text()).find_child("via")
    assert new.find_child("net").get_string() == "D0"
    assert new.find_child("at").get_float(0) == 104.0
    assert new.find_child("drill").get_float() == 0.2
    original = pcb.read_bytes()
    for bad in (["wrong-net", "D1", via[2]], ["stub", "D0", [101.0, 51.0]]):
        with pytest.raises(ValueError, match="layer transition"):
            cleanup.add_layer_handoffs(pcb, [bad])
    with pytest.raises(ValueError, match="layer transition"):
        cleanup.add_layer_handoffs(pcb, [via, ["duplicate", "D0", via[2]]])
    board.segments[1].layer = "B.Cu"
    with pytest.raises(ValueError, match="layer transition"):
        cleanup.add_layer_handoffs(pcb, [via])
    assert pcb.read_bytes() == original


def test_failing_design_cannot_produce_proposed_release_hashes(tmp_path, cleanup):
    import json

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "validation.json").write_text(json.dumps({"status": "blocked", "errors": ["DRC"]}))
    proposed = tmp_path / "proposed"
    with pytest.raises(ValueError, match="fully passing design"):
        cleanup.stage_passing_pair(tmp_path, tmp_path, evidence, proposed)
    assert not proposed.exists()
