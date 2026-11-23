"""Report concurrent changes to the original release and its checked dependencies."""

import shutil

import pytest

from kicad_tools.cli import readiness_cmd
from tests.test_kct_readiness_cmd import FakeEngines
from tests.test_readiness_project_root_5813 import attempt_report, go, make_project


@pytest.mark.parametrize("edit_board_files", [True, False])
def test_release_change_blocker_names_each_modified_input(tmp_path, monkeypatch, edit_board_files):
    project = make_project(tmp_path)
    board = project / "pcb"
    output = board / "output"
    pcb = output / "board.kicad_pcb"
    original_pcb = pcb.read_bytes()
    (output / "old-note").write_text("original note")
    (output / "retired-note").write_text("retired note")
    schematic = project / "schematics/board.kicad_sch"
    symbol = project / "symbols/custom.kicad_sym"
    refill = FakeEngines.refill

    def edit_during_first_refill(self, candidate_pcb):
        if "refill" not in self.calls:
            assert not candidate_pcb.is_relative_to(board)
            schematic.write_text("(kicad_sch edited)\n")
            symbol.unlink()
            if edit_board_files:
                (output / "new-note").write_text("new note")
                (output / "old-note").write_text("edited note")
                (output / "retired-note").unlink()
        return refill(self, candidate_pcb)

    monkeypatch.setattr(FakeEngines, "refill", edit_during_first_refill)
    rc, _ = go(project, "--project-root", str(project))
    assert rc == 1
    [blocker] = attempt_report(board)["blockers"]
    assert "Release changed while checks were running" in blocker
    assert "modified external input ../schematics/board.kicad_sch" in blocker
    assert "removed external input ../symbols/custom.kicad_sym" in blocker
    assert ("added output/new-note" in blocker) == edit_board_files
    assert ("modified output/old-note" in blocker) == edit_board_files
    assert ("removed output/retired-note" in blocker) == edit_board_files
    assert pcb.name not in blocker
    assert pcb.read_bytes() == original_pcb
    assert schematic.read_text() == "(kicad_sch edited)\n"
    assert not symbol.exists()
    assert not (output / "manufacturing.zip").exists()


def test_inventory_changes_names_added_removed_and_modified():
    before = {"keep": "1", "gone": "2", "edit": "3"}
    after = {"keep": "1", "edit": "4", "new": "5"}
    assert readiness_cmd._inventory_changes(before, after) == [
        "added new",
        "removed gone",
        "modified edit",
    ]
    assert readiness_cmd._inventory_changes(before, dict(before)) == []


def test_with_changes_appends_only_when_there_are_changes():
    assert readiness_cmd._with_changes("Boom", []) == "Boom"
    assert readiness_cmd._with_changes("Boom", ["added a", "modified b"]) == (
        "Boom: added a, modified b"
    )


def _release_dirs(tmp_path):
    destination = tmp_path / "release"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")
    (destination / "edit.txt").write_text("original")
    before = readiness_cmd._snapshot_inventory(destination)
    candidate = tmp_path / "candidate"
    shutil.copytree(destination, candidate)
    (candidate / "edit.txt").write_text("candidate")
    return destination, candidate, before


def test_publish_names_paths_edited_during_verification(tmp_path):
    destination, candidate, before = _release_dirs(tmp_path)
    (destination / "edit.txt").write_text("concurrent")
    (destination / "stray.txt").write_text("stray")
    with pytest.raises(RuntimeError) as exc:
        readiness_cmd._publish_candidate(candidate, destination, before)
    assert str(exc.value) == (
        "Release changed during verification; refusing to overwrite concurrent work: "
        "added stray.txt, modified edit.txt"
    )
    assert (destination / "edit.txt").read_text() == "concurrent"


def test_publish_names_paths_edited_during_publication(tmp_path, monkeypatch):
    destination, candidate, before = _release_dirs(tmp_path)
    replace = readiness_cmd.os.replace

    def replace_then_edit(src, dst):
        replace(src, dst)
        (destination / "late.txt").write_text("late")

    monkeypatch.setattr(readiness_cmd.os, "replace", replace_then_edit)
    with pytest.raises(RuntimeError) as exc:
        readiness_cmd._publish_candidate(candidate, destination, before)
    assert str(exc.value) == "Concurrent edit during publication: added late.txt"


def test_candidate_snapshot_blocker_names_changed_source(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    board = project / "pcb"
    note = board / "output" / "note"
    note.write_text("original")
    copytree = shutil.copytree

    def edit_then_copy(src, dst, *args, **kwargs):
        if src == board:
            note.write_text("edited while copying")
        return copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", edit_then_copy)
    rc, _ = go(project, "--project-root", str(project))
    assert rc == 1
    [blocker] = attempt_report(board)["blockers"]
    assert "Source changed while creating candidate snapshot: " in blocker
    assert "modified output/note" in blocker
