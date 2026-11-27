"""Stage Board 03's two reviewed USB widths on a scratch project (#10575).

For the #8900 reissue, run this step after route_pcb writes the net-class
map and fabrication overrides, before the first project rule writer. For a
saved-plan build the project belongs to the output PCB. An exploratory route
stages its project from the input PCB.

    uv run python boards/03-usb-joystick/engineering/usb_project_rules.py PCB

The normal pre-fill writer or `kct check --emit-drc-constraints` then
assigns each net to its prepared class using the emitted net-class map.
"""

import argparse
import json
from pathlib import Path

from kicad_tools.core.atomic_write import atomic_write_text
from kicad_tools.core.project_file import load_project, save_project
from kicad_tools.core.rule_loosening import NETCLASS_MINIMUM_KEYS, NETCLASS_TARGET_KEYS
from kicad_tools.manufacturers import get_profile, resolve_pcb_fabrication_overrides
from kicad_tools.manufacturers.project_generator import (
    build_default_netclass,
    build_project_data,
    is_kct_owned_project,
    stamp_kct_project,
)

# The two pairs use different widths for both single-track and differential routing.
USB_PAIRS = (("USB_D", "USBConnector", 0.20), ("USB_MCU_D", "USBMCU", 0.25))


def stage_usb_classes(pcb_path: Path, *, net_class_map_path: Path | None = None) -> None:
    """Name the two pairs in the router map and declare their widths in the project."""
    map_path = net_class_map_path or pcb_path.with_name("net_class_map.json")
    mapping = json.loads(map_path.read_bytes())
    profile = get_profile("jlcpcb-tier1")
    rules, _ = resolve_pcb_fabrication_overrides(
        map_path.with_name(pcb_path.name),
        profile.get_design_rules(layers=4, copper_oz=1.0),
        manufacturer_id=profile.id,
    )
    path = pcb_path.with_suffix(".kicad_pro")
    project = (
        load_project(path)
        if path.exists()
        else build_project_data(
            rules, path.stem, manufacturer_id=profile.id, layers=4, copper_oz=1.0
        )
    )
    owned = is_kct_owned_project(project)
    classes = project["net_settings"]["classes"]
    existing = {row["name"]: row for row in classes}
    template = existing.get("HighSpeed") or build_default_netclass(rules)
    additions = []
    renamed = False
    for prefix, name, width in USB_PAIRS:
        pair = [mapping[prefix + polarity] for polarity in ("+", "-")]
        clearance = pair[0]["clearance"]
        for net in pair:
            if net["trace_width"] != width or net["clearance"] != clearance:
                raise ValueError(
                    f"Router map disagrees with the reviewed widths or clearance for {prefix}"
                )
            if net["name"] not in ("HighSpeed", name):
                raise ValueError(f"Unexpected router class for {prefix}: {net['name']}")
            if net["name"] != name:
                net["name"] = name
                renamed = True
        definition = {
            **template,
            "name": name,
            "clearance": clearance,
            "track_width": width,
            "diff_pair_width": width,
        }
        if name in existing:
            for field in NETCLASS_MINIMUM_KEYS + NETCLASS_TARGET_KEYS:
                if existing[name].get(field) != definition.get(field):
                    raise ValueError(
                        f"Project class {name} conflicts with the reviewed USB rules for {field}"
                    )
        else:
            additions.append(definition)

    if additions:
        classes.extend(additions)
        # A verified incoming stamp distinguishes generated classes from
        # user-authored rules. Keep that distinction after our own edit.
        if owned:
            stamp_kct_project(project)
        save_project(project, path)
    if renamed:
        # Install definitions first. A retry after an interrupted map write
        # can safely reuse the definitions and finish assigning the pairs.
        atomic_write_text(map_path, json.dumps(mapping, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcb", type=Path)
    stage_usb_classes(parser.parse_args().pcb)
