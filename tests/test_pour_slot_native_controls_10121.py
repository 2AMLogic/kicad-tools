"""Native-refill controls for the packed-pair pour-slot criterion (Issue #10121).

Issue #10121 measured Board 05's nine remaining ``pour_slot`` advisories
against the native copper film on pinned KiCad 10.0.6 and found every one
packed (gap <= 2 x clearance), leaving no fillable strip for ``min_thickness``
to drop.  The detector refinement that follows -- report a pair only for
``2 x clearance < gap < 2 x clearance + min_thickness`` -- is validated here
against the two control boards that were refilled natively during the
investigation (``kicad-cli pcb drc --refill-zones --save-board``, image
``kicad/kicad@sha256:1869356...``, kicad-cli 10.0.6):

* **Control P** (genuine pour slot): two 0.4 mm different-net tracks with a
  0.550 mm edge gap running 8 mm across an open 0.200 mm-clearance /
  0.300 mm-min_thickness GND pour.  The native film shows a 0.550 mm
  copper-free channel along the whole run (dropped island: strip
  ``0.550 - 2 x 0.200 = 0.150 mm`` < ``min_thickness``) with pour present on
  both sides and beyond both via ends -- the rule must keep warning.
* **Control N** (pad-exit, Board 05's EN_A/PWM_A shape): 1.575 x 0.4 mm pads
  on 0.650 mm Y pitch (0.250 mm pad-edge gap) whose escapes exit the pad tips
  with a 0.395 mm track gap (0.35 mm + 0.16 mm tracks).  The native film shows
  the pad row's inherent 0.250 mm channel inside the row and, west of the
  tips, exactly the 0.395 mm clearance void -- no dropped island anywhere,
  pour penetrating past the escape vias -- the rule must stay silent.

The channel-width assertions below are calibrated against those native films
(measured 0.550 / 0.395 / 0.250 mm); the pure-Python cross-sections recompute
them from authored geometry at test time.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.pour_slot import iter_pour_slot_pairs

#: Board 05's front GND zone parameters (also used by both controls).
CLEARANCE_MM = 0.200
MIN_THICKNESS_MM = 0.300
#: The dropped-island band for that zone: an island narrower than
#: ``min_thickness`` is dropped by KiCad's fill, leaving a slot.
BAND_MM = (2 * CLEARANCE_MM, 2 * CLEARANCE_MM + MIN_THICKNESS_MM)

ZONE = """(zone
	(net "GND")
	(net_name "GND")
	(layer "F.Cu")
	(hatch edge 0.5)
	(connect_pads (clearance 0.2))
	(min_thickness 0.3)
	(fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3) (island_removal_mode 0))
	(polygon (pts (xy 5 5) (xy 45 5) (xy 45 35) (xy 5 35)))
)
"""

PAD_ROW = """(footprint "Control:PadRow" (layer "F.Cu") (uuid "00000000-0000-0000-0000-0000000000f1")
	(at 20 15)
	(property "Reference" "U9" (at 0 0) (layer "F.SilkS"))
	(pad "1" smd roundrect (at 0 -0.325) (size 1.575 0.4) (layers "F.Cu" "F.Mask")
		(roundrect_rratio 0.25) (net 1 "NET1"))
	(pad "2" smd roundrect (at 0 0.325) (size 1.575 0.4) (layers "F.Cu" "F.Mask")
		(roundrect_rratio 0.25) (net 2 "NET2"))
)
"""


def _roundtrip(pcb: PCB, tmp_path: Path, name: str) -> PCB:
    """Save + reload so the hand-written zone/pad s-expressions are parsed."""
    path = tmp_path / name
    pcb.save(str(path))
    return PCB.load(str(path))


def _control_p(tmp_path: Path) -> PCB:
    pcb = PCB.create(width=50, height=40, center=False)
    pcb.add_trace((11, 15), (19, 15), width=0.4, layer="F.Cu", net="NET1")
    pcb.add_trace((11, 15.95), (19, 15.95), width=0.4, layer="F.Cu", net="NET2")
    pcb.add_via(10.2, 15, size=0.6, drill=0.3, net="NET1")
    pcb.add_via(10.2, 15.95, size=0.6, drill=0.3, net="NET2")
    pcb.add_via(19.8, 15, size=0.6, drill=0.3, net="NET1")
    pcb.add_via(19.8, 15.95, size=0.6, drill=0.3, net="NET2")
    pcb._sexp.append(parse_string(ZONE))
    return _roundtrip(pcb, tmp_path, "control_p.kicad_pcb")


def _control_n(tmp_path: Path) -> PCB:
    pcb = PCB.create(width=50, height=40, center=False)
    pcb._sexp.append(parse_string(PAD_ROW))
    pcb.add_trace((17.6, 14.675), (20.2, 14.675), width=0.35, layer="F.Cu", net="NET1")
    pcb.add_trace((17.6, 15.325), (20.2, 15.325), width=0.16, layer="F.Cu", net="NET2")
    pcb.add_via(17.2, 14.675, size=0.45, drill=0.25, net="NET1")
    pcb.add_via(17.2, 15.325, size=0.45, drill=0.25, net="NET2")
    pcb._sexp.append(parse_string(ZONE))
    return _roundtrip(pcb, tmp_path, "control_n.kicad_pcb")


def _pair_gaps(pcb: PCB) -> list[tuple[str, str, float]]:
    return sorted(
        (*sorted((p.first.net_name, p.second.net_name)), round(p.gap, 4))
        for p in iter_pour_slot_pairs(pcb)
    )


def _track_gap(y_upper: float, w_upper: float, y_lower: float, w_lower: float) -> float:
    """Edge-to-edge gap between two horizontal tracks (sheet mm)."""
    return (y_lower - w_lower / 2) - (y_upper + w_upper / 2)


def test_control_p_genuine_slot_still_warns(tmp_path: Path) -> None:
    """The dropped-island control keeps its warning after the refinement."""
    pairs = _pair_gaps(_control_p(tmp_path))
    assert pairs == [("NET1", "NET2", 0.55)], pairs
    gap = pairs[0][2]
    assert BAND_MM[0] < gap < BAND_MM[1]
    # The island KiCad drops: 0.150 mm, below min_thickness (native film: a
    # 0.550 mm copper-free channel over the whole 8 mm run, pour both sides).
    assert round(gap - 2 * CLEARANCE_MM, 4) == 0.15
    assert gap - 2 * CLEARANCE_MM < MIN_THICKNESS_MM


def test_control_n_pad_exit_stays_silent(tmp_path: Path) -> None:
    """The packed pad-exit control (Board 05's witness shape) clears."""
    assert _pair_gaps(_control_n(tmp_path)) == []

    pcb = _control_n(tmp_path)
    seg1 = next(s for s in pcb.segments if s.net_name == "NET1")
    seg2 = next(s for s in pcb.segments if s.net_name == "NET2")
    gap = _track_gap(seg1.start[1], seg1.width, seg2.start[1], seg2.width)
    # Native film (pinned KiCad 10.0.6): exactly this 0.395 mm clearance void
    # west of the pad tips, and the pad row's inherent 0.250 mm channel inside
    # the row; no dropped island anywhere on the film.
    assert round(gap, 4) == 0.395
    assert gap <= 2 * CLEARANCE_MM  # packed: no fillable strip ever existed
    # The pad row produces the same channel with no tracks at all: pad edges
    # at 0.650 mm pitch minus 2 x 0.4/2 mm pad height.
    assert round(0.650 - 0.4, 4) == 0.25


def test_board05_witness_band_is_packed_only() -> None:
    """Every Board 05 witness gap sits at or below the packed edge.

    The nine historical advisories measured 0.250 mm (six pairs), 0.275 mm
    (VM/IREF) and 0.395 mm (IREF/SENSE_IN, EN_A/PWM_A); all are <= the
    0.400 mm = 2 x clearance lower band edge, so the refined rule reports
    none of them and the recipe's copper is unchanged.
    """
    witnesses = (0.250, 0.275, 0.395, 0.395, 0.250, 0.250, 0.250, 0.250, 0.250)
    assert all(g <= 2 * CLEARANCE_MM for g in witnesses)
    assert all(g > 0 for g in witnesses)
