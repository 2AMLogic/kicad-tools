"""Report concurrent changes to the original release and its checked dependencies."""

import pytest

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
