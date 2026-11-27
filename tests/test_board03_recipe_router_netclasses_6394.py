"""Board 03's recipe declares the router's net classes itself (Issue #6394).

``generate_design.py`` replays ``routing-plan.json`` instead of calling
``kct route``, so before #6394 a fresh regen wrote sidecars that declared only
``Default``; the classes reached the committed sidecars only through
``scripts/boards/declare_router_netclasses_6390.py``.  The recipe's
pre-fill ``apply_manufacturing_profile`` now passes the map ``kct route``
routed under as ``router_net_classes=``.  These tests strip the committed
``.kicad_pro`` back to ``Default`` and check that the recipe alone restores
the same declaration -- including the classifier's ``Digital``/``Clock``
classes for nets the sidecar map does not name, so no class is loosened.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "03-usb-joystick"
OUTPUT = BOARD_DIR / "output"
STEM = "usb_joystick_routed"


def _load_recipe(filename: str = "generate_design.py"):
    if str(BOARD_DIR) not in sys.path:
        sys.path.insert(0, str(BOARD_DIR))
    spec = importlib.util.spec_from_file_location(
        f"board03_{Path(filename).stem}_6394", BOARD_DIR / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(tmp_path: Path, *, with_map: bool = True) -> Path:
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy(OUTPUT / f"{STEM}{suffix}", tmp_path / f"{STEM}{suffix}")
    shutil.copy(OUTPUT / "fabrication_overrides.json", tmp_path / "fabrication_overrides.json")
    if with_map:
        shutil.copy(OUTPUT / "net_class_map.json", tmp_path / "net_class_map.json")
    pro = tmp_path / f"{STEM}.kicad_pro"
    data = json.loads(pro.read_text())
    settings = data["net_settings"]
    settings["classes"] = [c for c in settings["classes"] if c["name"] == "Default"]
    settings["netclass_assignments"] = {}
    data.get("meta", {}).pop("kct_router_netclasses", None)
    pro.write_text(json.dumps(data, indent=2))
    return tmp_path / f"{STEM}.kicad_pcb"


def _net_settings(pcb: Path) -> dict:
    return json.loads(pcb.with_suffix(".kicad_pro").read_text())["net_settings"]


def test_fresh_regen_declares_router_classes(tmp_path: Path) -> None:
    pcb = _stage(tmp_path)
    _load_recipe().apply_manufacturing_profile(pcb)

    settings = _net_settings(pcb)
    classes = {c["name"]: c for c in settings["classes"]}
    assignments = settings["netclass_assignments"]
    assert "Power" in classes
    assert classes["Power"]["clearance"] == pytest.approx(0.2)
    for net in ("GND", "VCC", "VBUS"):
        assert assignments.get(net) == "Power", net
    for net in ("USB_D+", "USB_D-", "USB_MCU_D+", "USB_MCU_D-"):
        assert assignments.get(net) == "HighSpeed", net
    dru = pcb.with_suffix(".kicad_dru").read_text()
    assert 'rule "Reviewed clearance - Power"' in dru


def test_fresh_regen_matches_the_committed_declaration(tmp_path: Path) -> None:
    """Classifier classes survive: nothing the committed sidecars hold is loosened."""
    pcb = _stage(tmp_path)
    _load_recipe().apply_manufacturing_profile(pcb)

    committed = json.loads((OUTPUT / f"{STEM}.kicad_pro").read_text())["net_settings"]
    fresh = _net_settings(pcb)
    assert fresh["netclass_assignments"] == committed["netclass_assignments"]
    assert {c["name"]: c["clearance"] for c in fresh["classes"]} == {
        c["name"]: c["clearance"] for c in committed["classes"]
    }
    # A net the sidecar map does not name keeps its classifier class.
    assert fresh["netclass_assignments"]["BTN1"] == "Digital"


def test_no_sidecar_map_declares_nothing(tmp_path: Path) -> None:
    recipe = _load_recipe()
    pcb = _stage(tmp_path, with_map=False)
    assert recipe.router_net_class_map(pcb) is None


@pytest.mark.parametrize("existing_routed_project", [False, True])
def test_usb_widths_survive_both_recipe_rule_writers(tmp_path: Path, existing_routed_project: bool):
    from kicad_tools.cli.check_cmd import main as check
    from kicad_tools.core.project_clearance import authored_netclass_widths

    recipe = _load_recipe()
    stage_usb_classes = _load_recipe("engineering/usb_project_rules.py").stage_usb_classes

    source = tmp_path / "usb_joystick.kicad_pcb"
    shutil.copyfile(OUTPUT / source.name, source)
    routed = tmp_path / f"{STEM}.kicad_pcb"
    project = routed.with_suffix(".kicad_pro")
    if existing_routed_project:
        shutil.copyfile(OUTPUT / project.name, project)
    assert recipe.route_pcb(source, routed)
    copper = routed.read_bytes()
    map_path = tmp_path / "net_class_map.json"
    stage_usb_classes(routed)
    router_map = json.loads(map_path.read_bytes())
    reviewed_map = json.loads((OUTPUT / map_path.name).read_bytes())
    expected = {"USB_D+": 0.20, "USB_D-": 0.20, "USB_MCU_D+": 0.25, "USB_MCU_D-": 0.25}
    for net in expected:
        assert router_map[net] == {**reviewed_map[net], "name": router_map[net]["name"]}
    assert router_map["USB_D+"]["name"] != router_map["USB_MCU_D+"]["name"]
    original = json.loads((OUTPUT / project.name).read_bytes())["net_settings"]
    original_usb = next(row for row in original["classes"] if row["name"] == "HighSpeed")

    def check_widths():
        data = json.loads(project.read_bytes())
        widths = authored_netclass_widths(data, expected)
        assert {net: width.width for net, width in widths.items()} == expected
        settings = data["net_settings"]
        classes = {row["name"]: row for row in settings["classes"]}
        for net, width in expected.items():
            name = router_map[net]["name"]
            assert widths[net].source_class == name
            assert classes[name] == {**original_usb, "name": name, "track_width": width}
        for net in ("USB_CC1", "USB_CC2", "BTN1", "XTAL1", "VCC"):
            name = original["netclass_assignments"][net]
            assert settings["netclass_assignments"][net] == name
            assert classes[name] == next(row for row in original["classes"] if row["name"] == name)
        assert routed.read_bytes() == copper

    cycles = []
    for _ in range(2):
        stage_usb_classes(routed)
        recipe.apply_manufacturing_profile(routed)
        check_widths()
        assert (
            check(
                [
                    str(routed),
                    "--mfr",
                    "jlcpcb-tier1",
                    "--emit-drc-constraints",
                    "--net-class-map",
                    str(map_path),
                    "--only",
                    "netclass_agreement",
                    "--drc-only",
                ]
            )
            == 0
        )
        check_widths()
        cycles.append((project.read_bytes(), routed.with_suffix(".kicad_dru").read_bytes()))
    assert cycles[0] == cycles[1]


@pytest.mark.parametrize("conflict", ["router width", "existing class"])
def test_staging_keeps_both_sidecars_on_conflict(tmp_path: Path, conflict: str) -> None:
    stage_usb_classes = _load_recipe("engineering/usb_project_rules.py").stage_usb_classes
    pcb = _stage(tmp_path)
    project = pcb.with_suffix(".kicad_pro")
    sidecar = tmp_path / "net_class_map.json"
    if conflict == "router width":
        mapping = json.loads(sidecar.read_bytes())
        mapping["USB_MCU_D-"]["trace_width"] = 0.20
        sidecar.write_text(json.dumps(mapping))
    else:
        data = json.loads(project.read_bytes())
        data["net_settings"]["classes"].append(
            {"name": "USBMCU", "track_width": 0.18, "clearance": 0.15}
        )
        project.write_text(json.dumps(data))
    original = (project.read_bytes(), sidecar.read_bytes(), pcb.read_bytes())
    with pytest.raises(ValueError, match="reviewed"):
        stage_usb_classes(pcb)
    assert (project.read_bytes(), sidecar.read_bytes(), pcb.read_bytes()) == original
