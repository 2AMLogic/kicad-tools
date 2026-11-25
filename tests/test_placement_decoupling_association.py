"""Schematic cap -> pin association in the placement optimizer (issue #10142).

Covers the designated-pin pairing in ``assign_caps_to_pins``, attaching a
schematic's associations to decoupling groups, and the snap pass's same-side
and smallest-closest rules.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    CostMode,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    evaluate_placement,
)
from kicad_tools.placement.decoupling import (
    DecouplingCap,
    DecouplingGroup,
    SchematicCapAssociation,
    SupplyPin,
    apply_cap_associations,
    assign_caps_to_pins,
    identify_decoupling_groups,
    parse_capacitance_farads,
    snap_decoupling_caps,
)
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    PlacementVector,
    decode,
    encode,
)
from kicad_tools.placement.wirelength import build_pad_position_map

_BOARD = BoardOutline(0.0, 0.0, 100.0, 100.0)


def _assoc(cap: str, pin: SupplyPin | None, source: str = "derived") -> SchematicCapAssociation:
    return SchematicCapAssociation(DecouplingCap(cap, "1", "2"), "+3V3", "/", pin, source, "")


class TestAssignWithAssociation:
    def _group(self, associated=()):
        return DecouplingGroup(
            net="+3V3",
            caps=(DecouplingCap("C1", "1", "2"), DecouplingCap("C2", "1", "2")),
            pins=(SupplyPin("U1", "1"), SupplyPin("U2", "1")),
            associated=tuple(associated),
        )

    # Both caps are nearer U1.1 than U2.1.
    POSITIONS = {
        ("U1", "1"): (0.0, 0.0),
        ("U2", "1"): (40.0, 0.0),
        ("C1", "1"): (2.0, 5.0),
        ("C2", "1"): (3.0, 5.0),
    }

    def test_no_association_is_the_greedy_pairing(self):
        pairs = {c.reference: p for c, p, _ in assign_caps_to_pins(self._group(), self.POSITIONS)}
        # Greedy spreads: C1 takes the nearest pin, C2 is pushed to the other.
        assert pairs == {"C1": SupplyPin("U1", "1"), "C2": SupplyPin("U2", "1")}

    def test_associated_caps_take_their_designated_pin(self):
        group = self._group([("C1", SupplyPin("U2", "1")), ("C2", SupplyPin("U1", "1"))])
        pairs = {c.reference: (p, d) for c, p, d in assign_caps_to_pins(group, self.POSITIONS)}
        assert pairs["C1"][0] == SupplyPin("U2", "1")
        assert pairs["C1"][1] == pytest.approx(math.hypot(38.0, 5.0))
        assert pairs["C2"][0] == SupplyPin("U1", "1")

    def test_only_unassociated_caps_go_greedy(self):
        # C1 is designated to U1.1; C2 is free and goes to the unloaded pin.
        group = self._group([("C1", SupplyPin("U1", "1"))])
        pairs = {c.reference: p for c, p, _ in assign_caps_to_pins(group, self.POSITIONS)}
        assert pairs == {"C1": SupplyPin("U1", "1"), "C2": SupplyPin("U2", "1")}

    def test_more_caps_than_pins(self):
        group = DecouplingGroup(
            net="+3V3",
            caps=tuple(DecouplingCap(f"C{i}", "1", "2") for i in (1, 2, 3)),
            pins=(SupplyPin("U1", "1"),),
            associated=(("C3", SupplyPin("U1", "1")),),
        )
        positions = {("U1", "1"): (0.0, 0.0)} | {(f"C{i}", "1"): (float(i), 0.0) for i in (1, 2, 3)}
        pairs = assign_caps_to_pins(group, positions)
        assert {c.reference for c, _, _ in pairs} == {"C1", "C2", "C3"}
        assert all(p == SupplyPin("U1", "1") for _, p, _ in pairs)

    def test_designated_pin_without_position_falls_back(self):
        group = self._group([("C1", SupplyPin("U2", "1"))])
        positions = {k: v for k, v in self.POSITIONS.items() if k != ("U2", "1")}
        pairs = {c.reference: p for c, p, _ in assign_caps_to_pins(group, positions)}
        # U2.1 has no position, so both caps pair with the pin that does.
        assert pairs == {"C1": SupplyPin("U1", "1"), "C2": SupplyPin("U1", "1")}


class TestApplyCapAssociations:
    def _groups(self):
        nets = [
            Net("+3V3", [("U1", "1"), ("U2", "1"), ("C1", "1"), ("C2", "1")]),
            Net("GND", [("U1", "2"), ("C1", "2"), ("C2", "2")]),
        ]
        return identify_decoupling_groups(nets)

    def test_attaches_named_pins(self):
        out = apply_cap_associations(
            self._groups(), [_assoc("C1", SupplyPin("U2", "1")), _assoc("C2", SupplyPin("U1", "1"))]
        )
        assert dict(out[0].associated) == {"C1": SupplyPin("U2", "1"), "C2": SupplyPin("U1", "1")}

    def test_unassociated_and_foreign_pins_are_ignored(self):
        out = apply_cap_associations(
            self._groups(),
            [_assoc("C1", None, "unassociated"), _assoc("C2", SupplyPin("U9", "4"))],
        )
        assert out[0].associated == ()

    def test_no_associations_leaves_groups_unchanged(self):
        groups = self._groups()
        assert apply_cap_associations(groups, []) == groups


def test_parse_capacitance_farads():
    assert parse_capacitance_farads("100nF") == pytest.approx(100e-9)
    assert parse_capacitance_farads("10uF") == pytest.approx(10e-6)
    assert parse_capacitance_farads("4u7") == pytest.approx(4.7e-6)
    assert parse_capacitance_farads("10k") is None


def _ic(ref: str) -> ComponentDef:
    return ComponentDef(
        ref,
        pads=(PadDef("1", 0.0, 0.0, 0.5, 0.5), PadDef("2", 4.0, 0.0, 0.5, 0.5)),
        width=5.0,
        height=1.0,
    )


def _cap(ref: str) -> ComponentDef:
    return ComponentDef(
        ref,
        pads=(PadDef("1", -1.0, 0.0, 1.0, 1.2), PadDef("2", 1.0, 0.0, 1.0, 1.2)),
        width=3.0,
        height=1.2,
    )


def _snap_setup(placed, nets, groups):
    components = [(_ic if p.reference.startswith("U") else _cap)(p.reference) for p in placed]
    sizes = {c.reference: (c.width, c.height) for c in components}
    config = PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC)

    def score_fn(vec: PlacementVector):
        dec = decode(vec, components)
        return evaluate_placement(
            [ComponentPlacement(p.reference, p.x, p.y, p.rotation) for p in dec],
            nets,
            DesignRuleSet(),
            _BOARD,
            config,
            sizes,
            decoupling_groups=groups,
            decoupling_pad_positions=build_pad_position_map(dec),
        )

    return components, encode(placed), config, score_fn


def _pad_dist(vector, components, cap, ic):
    pads = build_pad_position_map(decode(vector, components))
    (cx, cy), (ix, iy) = pads[(cap, "1")], pads[(ic, "1")]
    return math.hypot(cx - ix, cy - iy)


class TestSnapUsesAssociation:
    NETS = [
        Net("+3V3", [("U1", "1"), ("U2", "1"), ("C1", "1"), ("C2", "1")]),
        Net("GND", [("U1", "2"), ("U2", "2"), ("C1", "2"), ("C2", "2")]),
    ]

    def _run(self, associations):
        groups = identify_decoupling_groups(self.NETS)
        if associations:
            groups = apply_cap_associations(groups, associations)
        # Both caps start midway; C1 is a little nearer U1 and C2 a little
        # nearer U2, which is the nearest-pin pairing.
        placed = [
            PlacedComponent("U1", 20.0, 20.0, 0.0, 0),
            PlacedComponent("U2", 70.0, 20.0, 0.0, 0),
            PlacedComponent("C1", 44.0, 40.0, 0.0, 0),
            PlacedComponent("C2", 46.0, 40.0, 0.0, 0),
        ]
        components, vector, config, score_fn = _snap_setup(placed, self.NETS, groups)
        new, moves = snap_decoupling_caps(vector, components, groups, _BOARD, score_fn, config)
        return components, new, moves

    def test_two_ic_shared_rail_each_cap_ends_at_its_own_ic_pin(self):
        # The schematic crosses the pairing: C1 serves U2, C2 serves U1.
        components, new, moves = self._run(
            [_assoc("C1", SupplyPin("U2", "1")), _assoc("C2", SupplyPin("U1", "1"))]
        )
        assert {m.cap: m.pin for m in moves} == {"C1": "U2.1", "C2": "U1.1"}
        assert _pad_dist(new, components, "C1", "U2") <= 3.0
        assert _pad_dist(new, components, "C2", "U1") <= 3.0

    def test_without_association_nearest_pin_pairing_is_unchanged(self):
        components, new, moves = self._run(None)
        assert {m.cap: m.pin for m in moves} == {"C1": "U1.1", "C2": "U2.1"}
        assert _pad_dist(new, components, "C1", "U1") <= 3.0
        assert _pad_dist(new, components, "C2", "U2") <= 3.0


class TestSnapSideAndValueOrder:
    NETS = [
        Net("+3V3", [("U1", "1"), ("C1", "1"), ("C2", "1")]),
        Net("GND", [("U1", "2"), ("C1", "2"), ("C2", "2")]),
    ]

    def test_back_side_cap_ends_on_the_ic_side(self):
        nets = self.NETS[:1] + [Net("GND", [("U1", "2"), ("C1", "2")])]
        nets[0] = Net("+3V3", [("U1", "1"), ("C1", "1")])
        groups = identify_decoupling_groups(nets)
        placed = [
            PlacedComponent("U1", 30.0, 30.0, 0.0, 0),
            PlacedComponent("C1", 30.0, 60.0, 0.0, 1),
        ]
        components, vector, config, score_fn = _snap_setup(placed, nets, groups)
        new, moves = snap_decoupling_caps(
            vector, components, groups, _BOARD, score_fn, config, allow_flip=True
        )
        assert [m.cap for m in moves] == ["C1"]
        assert int(new.data[7]) == 0  # C1 now on U1's side
        assert _pad_dist(new, components, "C1", "U1") <= 3.0

    def test_without_flip_caps_keep_their_side(self):
        nets = [
            Net("+3V3", [("U1", "1"), ("C1", "1")]),
            Net("GND", [("U1", "2"), ("C1", "2")]),
        ]
        groups = identify_decoupling_groups(nets)
        placed = [
            PlacedComponent("U1", 30.0, 30.0, 0.0, 0),
            PlacedComponent("C1", 30.0, 60.0, 0.0, 1),
        ]
        components, vector, config, score_fn = _snap_setup(placed, nets, groups)
        new, _ = snap_decoupling_caps(vector, components, groups, _BOARD, score_fn, config)
        assert int(new.data[7]) == 1

    def _two_cap_run(self, cap_values):
        groups = identify_decoupling_groups(self.NETS)
        # The 10 uF cap starts nearer the pin, so distance order would snap it first.
        placed = [
            PlacedComponent("U1", 30.0, 30.0, 0.0, 0),
            PlacedComponent("C1", 30.0, 44.0, 0.0, 0),  # farther out
            PlacedComponent("C2", 30.0, 38.0, 0.0, 0),  # nearer, so snapped first by distance
        ]
        components, vector, config, score_fn = _snap_setup(placed, self.NETS, groups)
        new, _ = snap_decoupling_caps(
            vector, components, groups, _BOARD, score_fn, config, cap_values=cap_values
        )
        return components, new

    def test_smallest_value_ends_closest_to_the_pin(self):
        components, new = self._two_cap_run({"C1": 100e-9, "C2": 10e-6})
        small = _pad_dist(new, components, "C1", "U1")
        large = _pad_dist(new, components, "C2", "U1")
        assert small < large

    def test_larger_cap_never_nearer_than_smaller_one(self):
        components, new = self._two_cap_run({"C1": 10e-6, "C2": 100e-9})
        # Values swapped: now C2 is the small cap and must be the closer one.
        assert _pad_dist(new, components, "C2", "U1") <= _pad_dist(new, components, "C1", "U1")


class TestSchematicDiscovery:
    def test_no_schematic_leaves_groups_unchanged(self, tmp_path: Path):
        from kicad_tools.cli.optimize_placement_cmd import _associate_from_schematic

        groups = [
            DecouplingGroup("+3V3", (DecouplingCap("C1", "1", "2"),), (SupplyPin("U1", "1"),))
        ]
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("")
        assert _associate_from_schematic(str(pcb), groups, quiet=True) is groups

    def test_unreadable_schematic_falls_back(self, tmp_path: Path):
        from kicad_tools.cli.optimize_placement_cmd import _associate_from_schematic

        groups = [
            DecouplingGroup("+3V3", (DecouplingCap("C1", "1", "2"),), (SupplyPin("U1", "1"),))
        ]
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("")
        (tmp_path / "board.kicad_sch").write_text("not a schematic")
        assert _associate_from_schematic(str(pcb), groups, quiet=True) is groups
