"""``DesignRules`` enforces ``min_inner_clearance_mm <= min_clearance_mm`` (Issue #9113)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kicad_tools.manufacturers import generate_dru, get_profile
from kicad_tools.manufacturers.base import (
    _DATA_DIR,
    DesignRules,
    load_design_rules_from_yaml,
)

_SHIPPED_YAML_IDS = sorted(p.stem for p in _DATA_DIR.glob("*.yaml"))

_BASE = {
    "min_trace_width_mm": 0.1,
    "min_clearance_mm": 0.10,
    "min_via_drill_mm": 0.2,
    "min_via_diameter_mm": 0.4,
    "min_annular_ring_mm": 0.1,
}


def test_inner_stricter_than_outer_raises_naming_both_values():
    with pytest.raises(ValueError) as exc:
        DesignRules(**{**_BASE, "min_inner_clearance_mm": 0.20})
    assert str(exc.value) == (
        "min_inner_clearance_mm (0.2) must not exceed min_clearance_mm (0.1): "
        "the inner-layer floor is the looser one (lighter inner copper)"
    )


@pytest.mark.parametrize("inner", [None, 0.10, 0.0762])
def test_none_equal_or_lower_inner_constructs(inner):
    rules = DesignRules(**{**_BASE, "min_inner_clearance_mm": inner})
    assert rules.min_inner_clearance_mm == inner


def test_dataclasses_replace_cannot_invert_floors():
    rules = DesignRules(**{**_BASE, "min_inner_clearance_mm": 0.0762})
    with pytest.raises(ValueError):
        replace(rules, min_inner_clearance_mm=0.25)
    with pytest.raises(ValueError):
        replace(rules, min_clearance_mm=0.05)


@pytest.mark.parametrize(("copper_oz", "argument_count"), [(2, 28), (1, 38)])
def test_existing_positional_profiles_keep_their_rules(copper_oz, argument_count):
    # Public constructor order before the THT/SMD spacing bands were added.
    legacy_order = (
        "min_trace_width_mm",
        "min_clearance_mm",
        "min_via_drill_mm",
        "min_via_diameter_mm",
        "min_annular_ring_mm",
        "min_hole_diameter_mm",
        "max_hole_diameter_mm",
        "min_copper_to_edge_mm",
        "min_hole_to_edge_mm",
        "min_copper_to_vscore_mm",
        "min_hole_to_hole_mm",
        "min_silkscreen_width_mm",
        "min_silkscreen_height_mm",
        "declared_reference_height_mm",
        "min_solder_mask_dam_mm",
        "min_solder_mask_clearance_mm",
        "min_silk_to_pad_clearance_mm",
        "min_silk_to_hole_clearance_mm",
        "silk_to_pad_danger_mm",
        "silk_to_pad_warning_mm",
        "silk_exposed_copper_is_error",
        "silk_minimums_are_error",
        "silk_under_body_is_error",
        "min_smd_pad_clearance_mm",
        "min_pth_hole_to_track_mm",
        "min_inner_pth_hole_to_copper_mm",
        "min_inner_clearance_mm",
        "min_pad_size_mm",
        "board_thickness_mm",
        "outer_copper_oz",
        "inner_copper_oz",
        "max_board_width_mm",
        "max_board_height_mm",
        "via_in_pad_supported",
        "via_in_pad_process_id",
        "blind_buried_vias_supported",
        "micro_vias_supported",
        "min_pth_annular_ring_mm",
    )
    profile = get_profile("jlcpcb").get_design_rules(4, copper_oz)
    arguments = {name: getattr(profile, name) for name in legacy_order[:argument_count]}
    positional = DesignRules(*arguments.values())
    named = DesignRules(**arguments)

    assert positional == named
    assert generate_dru(positional, "custom") == generate_dru(named, "custom")


@pytest.mark.parametrize("manufacturer_id", _SHIPPED_YAML_IDS)
def test_every_shipped_profile_loads(manufacturer_id):
    configs = load_design_rules_from_yaml(manufacturer_id)
    for rules in configs.values():
        if rules.min_inner_clearance_mm is not None:
            assert rules.min_inner_clearance_mm <= rules.min_clearance_mm
