"""Real revision-B component pinout, procurement and copper provenance guards."""

import importlib.util
import sys
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

SOURCE = Path(__file__).resolve().parents[1] / "boards/05-bldc-motor-controller/redesign"


def load(name):
    spec = importlib.util.spec_from_file_location(f"board05_revb_{name}", SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hardware():
    return load("hardware")


def test_real_driver_current_comparator_and_power(hardware):
    parts = {part.ref: part for part in hardware.parts()}
    assert parts["U2"].mpn == "DRV8313PWPR"
    assert parts["U2"].nets["12"] == "IREF"  # COMPP
    assert parts["U2"].nets["13"] == "SENSE_IN"  # COMPN: low output on overcurrent
    assert parts["U2"].nets["19"] == parts["U1"].nets["32"] == "nTRIP"
    assert parts["U2"].nets["1"] == "CPL"
    assert parts["U2"].nets["2"] == "CPH"
    assert parts["U2"].nets["3"] == "VCP"
    assert parts["U3"].nets["3"] == parts["U1"].nets["29"] == "RESET"
    assert parts["U2"].nets["29"] == parts["U3"].nets["9"] == "GND"


def test_all_parts_have_exact_procurement_and_physical_pins(hardware, tmp_path):
    parts = hardware.parts()
    assert len(parts) == 42
    assert len({part.ref for part in parts}) == 42
    assert all(part.mpn and part.lcsc[0] == "C" and part.lcsc[1:].isdigit() for part in parts)
    pcb = hardware.build_pcb()
    path = tmp_path / "board.kicad_pcb"
    pcb.save(path)
    pcb = PCB.load(path)
    for part in parts:
        fp = pcb.get_footprint(part.ref)
        assert fp.name == part.footprint, part.ref
        actual = {pad.number: pad.net_name for pad in fp.pads if pad.number in part.nets}
        assert actual == part.nets, part.ref
    assert pcb.get_footprint("U1").name.endswith("TQFP-32_7x7mm_P0.8mm")
    assert pcb.get_footprint("C1").name.endswith("CP_Elec_8x10.5_PolarityMark")


#: Issue #9921 pour-slot de-slant: the sixteen pad-escape stubs that leave
#: their U2/U3 lead rows at a shallow angle (1.2-16.7 degrees off horizontal)
#: so no two adjacent escapes stay parallel and the front GND pour no longer
#: slots between them (the ``pour_slot`` advisory's 20 witnesses).  A 45-only
#: shape is geometrically impossible here: within the pad field (pads 1.575 x
#: 0.4 mm on a 0.65 mm pitch) every escape is forced horizontal for its first
#: ~0.79 mm -- pads block any diagonal and the 45-degree set blocks any
#: shallow angle -- so adjacent-row stubs always overlapped in parallel.  The
#: sloped stubs have no exposed acute copper junctions (each lands on its own
#: pad, a via centre, or a junction buried under via copper; the acid-trap
#: rule passes), and the fleet census exemption mechanism this mirrors is
#: ``DOCUMENTED_OFF_ANGLE`` in ``tests/test_fleet_45_census.py``.  Exit
#: clause: remove an entry when its net is re-routed off this row (e.g. by
#: the U2/U3 land-pattern decisions pending in issue #9921).
BOARD05_DOCUMENTED_OFF_ANGLE = frozenset(
    {
        "cad743e3-9687-4dd7-9203-01ff7a0609d0",  # EN_A escape
        "43a3b551-724f-4ab3-8887-9d5841cb4e4d",  # EN_B escape
        "a9441ecb-92fa-4399-82c6-1c8978ca8305",  # EN_C escape
        "68d05a5e-bba3-4ce9-b69c-686cbdcfd5f6",  # nFAULT escape
        "d3667215-43af-44a0-a0fe-b5a4ce212e71",  # DRIVER_SLEEP escape
        "c3f3a1b7-3e5f-4b5c-8897-a61280adf527",  # VCP escape
        "90c765ee-89b3-4235-83f9-785f1f49d299",  # PHASE_A escape
        "5428a16a-39d7-49f1-b29c-b9f884e75e59",  # GND_SENSE escape (pad 6)
        "b10e96b9-e806-4de8-9007-793a2a340ad8",  # GND_SENSE escape (pad 7)
        "03771fb9-4984-47e2-a4aa-1a89b02dd10d",  # PHASE_B escape
        "3f6dc586-7165-40af-8f1b-2dc8734c8df6",  # PHASE_C escape
        "7a0d798a-db37-4b9b-ace4-7961acce1255",  # GND_SENSE escape (pad 10)
        "8fec79fa-8dfc-583d-bb27-49171496e95a",  # VM escape (pad 11)
        "3daddaa4-4301-4d6b-a301-acc8f222a0ae",  # IREF escape
        "0f51d425-761e-5514-9aef-580a524a79fc",  # SENSE_IN escape
    }
)


def test_reference_copper_rejects_a_changed_real_net(tmp_path, hardware):
    routing = load("routing")
    path = tmp_path / "board.kicad_pcb"
    hardware.build_pcb().save(path)
    original = routing.geometry_fingerprint(path)
    routing.apply_routing(path)
    assert routing.geometry_fingerprint(path) == original
    assert PCB.load(path).segments
    from kicad_tools.router.quantize import segment_angle_census

    off_angle = [
        seg
        for seg in segment_angle_census(path)[1]
        if str(seg.get("uuid")) not in BOARD05_DOCUMENTED_OFF_ANGLE
    ]
    assert not off_angle, (
        "Reviewed copper must preserve the fleet 45-degree policy outside the"
        " pinned #9921 de-slant escapes: " + repr(off_angle)
    )
    # Ratchet: every pinned uuid must still be an off-angle segment in the
    # recipe, so a future re-route that retires one fails here instead of
    # silently masking it.
    present = {str(seg.get("uuid")) for seg in segment_angle_census(path)[1]}
    assert present >= BOARD05_DOCUMENTED_OFF_ANGLE, "Stale #9921 de-slant pins: " + repr(
        sorted(BOARD05_DOCUMENTED_OFF_ANGLE - present)
    )
    hardware.build_pcb().save(path)
    changed = PCB.load(path)
    changed.assign_net_to_footprint_pad("U2", "12", "GND")
    changed.save(path)
    with pytest.raises(ValueError, match="geometry changed"):
        routing.apply_routing(path)


def test_thermal_vias_keep_real_smt_ics_in_cpl(hardware):
    from kicad_tools.export.pnp import is_through_hole_footprint

    pcb = hardware.build_pcb()
    assert all(not is_through_hole_footprint(pcb.get_footprint(ref)) for ref in ["U1", "U2", "U3"])
    assert all(
        is_through_hole_footprint(pcb.get_footprint(ref))
        for ref in ["J1", "J2", "J3", "J4", "J5", "RV1"]
    )


def test_unrouted_board_carries_silk_dfm_corrections(hardware):
    """Silk width floor and reference offsets belong to generation, not the replay (#7848)."""
    pcb = hardware.build_pcb()
    for fp in pcb._sexp.find_all("footprint"):
        props = {p.get_string(0): p for p in fp.find_all("property")}
        ref = props["Reference"].get_string(1)
        if ref in hardware.REFERENCE_OFFSETS:
            at = props["Reference"].find("at")
            assert (at.get_float(0), at.get_float(1)) == hardware.REFERENCE_OFFSETS[ref]
        for line in fp.children:
            if (line.name or "").startswith("fp_") and line.find("layer") is not None:
                if line.find("layer").get_string(0) in {"F.SilkS", "B.SilkS"}:
                    stroke = line.find("stroke")
                    if stroke is not None:
                        assert stroke.find("width").get_float(0) >= 0.15


def test_routed_board_reports_zero_silk_hole_overlaps(tmp_path, hardware):
    """Reviewed copper's vias must not cross any silkscreen ink (issue #9071).

    The 13 findings on the strict gate were fixed at the source: reference
    fields moved via ``REFERENCE_OFFSETS`` and crossing ``fp_line`` strokes
    dropped/trimmed via ``SILK_STROKE_EDITS``, both applied in
    ``build_pcb()`` so every rebuild path ships clean.  Replays the real
    routing (the vias live in ``routing.json``, not ``hardware.py``) and
    re-runs the checker's rule on the routed copper.
    """
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap

    routing = load("routing")
    path = tmp_path / "board.kicad_pcb"
    hardware.build_pcb().save(path)
    routing.apply_routing(path)
    pcb = PCB.load(path)
    rules = DesignRules(
        min_trace_width_mm=0.15,
        min_clearance_mm=0.15,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.3,
    )
    results = check_silk_hole_overlap(pcb, rules)
    assert not results.violations, [v.message for v in results.violations]
