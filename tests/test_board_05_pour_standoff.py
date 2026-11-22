"""Board 05's declared pour-standoff pads keep the authored 0.2 mm (Issue #9921).

``restore_pour_standoff`` raises the front/back GND pour back to the four
``Zone clearance`` rules' authored 0.2 mm standoff at the pads whose courtyard
exemption had dropped it onto the 0.15 mm mask-to-copper warning band.  These
guards pin the declaration set, its value and its idempotence, so a stock
footprint regeneration cannot silently drop or widen it.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

from kicad_tools.sexp import parse_file

REPO_ROOT = Path(__file__).resolve().parent.parent
REDESIGN = REPO_ROOT / "boards/05-bldc-motor-controller/redesign"
OUTPUT = REPO_ROOT / "boards/05-bldc-motor-controller/output"


def _load(name):
    sys.path.insert(0, str(REDESIGN))
    try:
        spec = importlib.util.spec_from_file_location(
            f"board05_{name}_9921", REDESIGN / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(REDESIGN))


def _reference(routed):
    """``{ref/pad: clearance}`` for every pad carrying a local clearance."""
    board = parse_file(routed)
    found = {}
    for footprint in board.find_children("footprint"):
        ref = next(
            (
                prop.get_string(1)
                for prop in footprint.find_all("property")
                if prop.get_string(0) == "Reference"
            ),
            None,
        )
        for pad in footprint.find_children("pad"):
            clearance = pad.find_child("clearance")
            if clearance is not None:
                found[f"{ref}/{pad.get_string(0)}"] = clearance.get_float(0)
    return found


def _replayed(tmp_path):
    build = _load("build")
    routing = _load("routing")
    routed = tmp_path / "bldc_controller_routed.kicad_pcb"
    shutil.copy2(OUTPUT / "bldc_controller.kicad_pcb", routed)
    routing.apply_routing(routed)
    return build, routing, routed


def test_declared_pads_carry_the_authored_standoff(tmp_path):
    build, _routing, routed = _replayed(tmp_path)
    build.restore_pour_standoff(routed)
    found = _reference(routed)
    expected = {f"{ref}/{pad}" for ref, pads in build.POUR_STANDOFF_PADS.items() for pad in pads}
    assert set(found) == expected, sorted(set(found) ^ expected)
    assert set(found.values()) == {build.POUR_STANDOFF_MM}
    # Only these numbered U2 leads have a pour-opening finding. The exposed
    # pad, its barrels and the unnumbered thermal aperture retain stock values.
    u2_pads = {key.removeprefix("U2/") for key in found if key.startswith("U2/")}
    assert u2_pads == {str(n) for n in (*range(1, 14), *range(15, 19), *range(22, 28))}
    # A pad-local 0.2 mm minimum conflicts with U3's 0.15 mm lead gaps.
    assert not any(key.startswith("U3/") for key in found)


def test_standoff_is_idempotent_and_preserves_the_fingerprint(tmp_path):
    build, routing, routed = _replayed(tmp_path)
    build.restore_pour_standoff(routed)
    before = routed.read_bytes()
    fingerprint = routing.geometry_fingerprint(routed)
    build.restore_pour_standoff(routed)
    assert routed.read_bytes() == before
    assert routing.geometry_fingerprint(routed) == fingerprint


def test_standoff_fails_closed_on_a_missing_pad(tmp_path, monkeypatch):
    build, _routing, routed = _replayed(tmp_path)
    board = parse_file(routed)
    for footprint in board.find_children("footprint"):
        ref = next(
            (
                prop.get_string(1)
                for prop in footprint.find_all("property")
                if prop.get_string(0) == "Reference"
            ),
            None,
        )
        if ref == "J5":
            board.remove(footprint)
            break
    routed.write_text(build.serialize_sexp(board))
    try:
        build.restore_pour_standoff(routed)
    except RuntimeError as exc:
        assert "J5" in str(exc)
    else:
        raise AssertionError("a missing declared pad must fail the rebuild")
