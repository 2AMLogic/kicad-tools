"""Board 05's U2/U3 escapes: the pour-slot recipe state and its physical criterion (Issue #9921).

The ``pour_slot`` advisory originally reported 20 witnesses -- nineteen U2
lead-escape pairs and the U3 GND/RESET pair -- from adjacent pad-row escapes
running parallel with 0.49 mm (0.16 mm tracks) or 0.25 mm (0.4 mm tracks)
gaps inside the ``2 x clearance + min_thickness`` (0.7 mm) band.

The recipe repairs eleven of them using horizontal, vertical and 45-degree
copper only (no off-angle exemption: the fleet 45-degree criterion stays
intact).  Five signal escapes are widened/repacked so their copper-to-copper
gap drops below the 0.400 mm lower edge of the band the front GND pour cannot
fill, and the two redundant GND escapes the attaching pour replaces are
removed.  **Nine advisories remain by design**: seven involve the
power/GND_SENSE bank or its IREF neighbour, and two (IREF/SENSE_IN,
EN_A/PWM_A) lie wholly within the stock U2 lead length, where the pad edges
set the gap and need a ground-plane or pad-edge criterion before they count as
repaired.  These tests pin that state -- exactly those nine pairs, and the
physical criterion on the two witness windows -- rather than a quiet
detector, which an angle change alone could produce without any fill.
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


#: The nine pour-slot advisories that remain open by design (module docstring):
#: sorted (net, net) pairs.  A change here is a recipe change -- update the
#: docstring and the #9921 evidence with it.
BOARD05_OPEN_POUR_SLOT_PAIRS = sorted(
    [
        ("EN_A", "PWM_A"),
        ("PHASE_A", "VM"),
        ("GND_SENSE", "PHASE_A"),
        ("GND_SENSE", "PHASE_B"),
        ("GND_SENSE", "PHASE_C"),
        ("PHASE_B", "PHASE_C"),
        ("GND_SENSE", "VM"),
        ("IREF", "SENSE_IN"),
        ("IREF", "VM"),
    ]
)


def _segment_gap(a, b):
    """Edge-to-edge distance (mm) between two straight tracks."""
    import math

    steps = 400
    best = float("inf")
    (cx, cy), (ex, ey) = b.start, b.end
    dx, dy = ex - cx, ey - cy
    length2 = dx * dx + dy * dy
    for i in range(steps + 1):
        t = i / steps
        x = a.start[0] + (a.end[0] - a.start[0]) * t
        y = a.start[1] + (a.end[1] - a.start[1]) * t
        u = max(0.0, min(1.0, ((x - cx) * dx + (y - cy) * dy) / length2)) if length2 else 0.0
        best = min(best, math.hypot(x - (cx + u * dx), y - (cy + u * dy)))
    return best - a.width / 2 - b.width / 2


def test_replayed_copper_pins_the_open_pour_slot_pairs(tmp_path):
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.pour_slot import iter_pour_slot_pairs

    pairs = list(iter_pour_slot_pairs(PCB.load(_replayed_board(tmp_path))))
    found = sorted(tuple(sorted((p.first.net_name, p.second.net_name))) for p in pairs)
    assert found == BOARD05_OPEN_POUR_SLOT_PAIRS, (
        "Board 05's pour-slot advisory set changed: expected the nine open"
        " pairs documented for #9921, got " + repr(found)
    )


def test_witness_channels_leave_no_pour_void_band(tmp_path):
    """The two #9921 witness channels must not sit in the unfillable band.

    The board owner's native-Gerber measurement found an empty 0.400-0.700 mm
    section (no plotted GND fill) between PWM_C/EN_C and PWM_B/EN_B in the
    earlier recipe, where the nominal gaps were 0.53-0.65 mm.  The repacked
    escapes keep every track-to-track gap under the 0.400 mm band floor (native
    measurement 0.244 / 0.264 mm; this track-only check reads about 0.23 /
    0.26 mm), so no channel is left as a void the pour cannot reach.  This pins
    the physical criterion, not the advisory's angle gate.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(_replayed_board(tmp_path))
    for net_a, net_b in (("PWM_C", "EN_C"), ("PWM_B", "EN_B")):
        a = [s for s in pcb.segments if s.net_name == net_a and s.layer == "F.Cu"]
        b = [s for s in pcb.segments if s.net_name == net_b and s.layer == "F.Cu"]
        assert a and b, (net_a, net_b)
        gap = min(_segment_gap(s, t) for s in a for t in b)
        assert gap < 0.400, f"{net_a}/{net_b} escape gap {gap:.3f} mm is in the void band"


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
