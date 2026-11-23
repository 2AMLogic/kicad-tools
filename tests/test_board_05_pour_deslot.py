"""Board 05's routed escapes leave no parallel pair the pour cannot fill (Issue #9921).

The ``pour_slot`` advisory's 20 witnesses -- nineteen U2 lead-escape pairs and
the U3 GND/RESET pair -- came from adjacent pad-row escapes running parallel
with 0.49 mm (0.16 mm tracks) or 0.25 mm (0.4 mm tracks) gaps, both inside the
``2 x clearance + min_thickness`` (0.7 mm) band where the front GND pour
either drops a thin island or never enters.  The recipe now slopes every other
escape off its row (see ``BOARD05_DOCUMENTED_OFF_ANGLE`` in
``tests/test_board_05_revision_b.py`` for why a 45-only shape is impossible
inside the 0.65 mm pad field) and drops the two GND escapes the attaching pour
replaces.  These guards pin the de-slotted recipe: replaying the reviewed
copper must report zero pour-slot pairs, and the two GND escapes must stay
gone (their vias remain as pour stitches).
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REDESIGN = REPO_ROOT / "boards/05-bldc-motor-controller/redesign"


def load(name):
    spec = importlib.util.spec_from_file_location(f"board05_deslot_{name}", REDESIGN / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _replayed_board(tmp_path):
    hardware = load("hardware")
    routing = load("routing")
    path = tmp_path / "board.kicad_pcb"
    hardware.build_pcb().save(path)
    routing.apply_routing(path)
    return path


def test_replayed_copper_reports_zero_pour_slot_pairs(tmp_path):
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.pour_slot import iter_pour_slot_pairs

    pairs = list(iter_pour_slot_pairs(PCB.load(_replayed_board(tmp_path))))
    assert pairs == [], (
        "Board 05's reviewed copper must leave no parallel escape pair inside the"
        " pour-slot band; a regeneration or recipe edit reintroduced: " + repr(pairs)
    )


def test_pour_replaced_gnd_escapes_stay_gone():
    recipe = (REDESIGN / "routing.json").read_text()
    # U2 pad 20 (right column) and U3 pad 4 (left column) escape stubs: the
    # front GND pour attaches both pads and reaches their stitch vias, so the
    # parallel stubs were removed rather than sloped.
    assert "2aa4f094-cc7c-4416-bfe5-f9277df1eaec" not in recipe
    assert "e5531a28-a0e7-5f32-ab2f-ba3f2c843b86" not in recipe
    assert "908df261-a3db-4df7-87a2-e15ddbd29a2e" not in recipe
    # Their stitch vias remain (the pour still ties the layers there).
    assert "1eeefbf1-a3cd-4bd8-98d0-0e08f226e4db" in recipe
    assert "dbc23801-258b-4620-9555-825f028d7570" in recipe
