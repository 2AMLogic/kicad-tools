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


@pytest.mark.parametrize("radius", [6.0, 0.25])
def test_optimize_writes_only_a_feasible_designated_cap(tmp_path, capsys, radius):
    """Read the placement and pads back from the output of the public command."""
    import json

    from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement
    from kicad_tools.schema.pcb import PCB
    from tests.test_decoupling_association import make_sheet
    from tests.test_placement_decoupling import _silk_board

    source = tmp_path / "design.kicad_pcb"
    board = _silk_board()
    start = board.index('(footprint "C_0805"')
    end = board.index("(footprint", start + 1)
    source.write_text(board[:start] + board[start:end].replace('"F.', '"B.') + board[end:])
    make_sheet(
        source.with_suffix(".kicad_sch"),
        [("U1", 50.8, 50.8, "+3V3", ""), ("C1", 55.88, 50.8, "+3V3", "")],
        [(50.8, 50.8, 55.88, 50.8)],
    )
    original = source.read_bytes()
    output = tmp_path / "result.kicad_pcb"
    output.write_bytes(original)

    rc = run_optimize_placement(
        str(source),
        output_path=str(output),
        seed_method="current",
        max_iterations=0,
        decoupling_radius_mm=radius,
        as_json=True,
        silk_fix=False,
    )
    report = json.loads(capsys.readouterr().out)
    assert source.read_bytes() == original
    if radius == 0.25:
        assert rc == 1
        assert not report["feasible"] and not report["saved"] and not report["success"]
        assert "C1" in report["infeasible_detail"] and "U1.1" in report["infeasible_detail"]
        assert "0.25" in report["infeasible_detail"]
        assert output.read_bytes() == original
    else:
        assert rc == 0 and report["saved"] and report["feasible"]
        written = PCB.load(str(output))
        cap = written.get_footprint("C1")
        assert cap.layer == written.get_footprint("U1").layer == "F.Cu"
        assert all("F.Cu" in pad.layers for pad in cap.pads)
        distance = math.dist(
            written.get_pad_position("C1", "1"), written.get_pad_position("U1", "1")
        )
        row = next(item for item in report["decoupling"] if item["cap"] == "C1")
        assert distance <= radius
        assert row["distance_mm"] == pytest.approx(distance, abs=0.001)


@pytest.fixture
def swapped_board(tmp_path):
    from tests.test_bypass_distance_rule import _board

    def make(*, c1=61.5, c2=21.5, c1_layer="B.Cu", **kwargs):
        path = _board(tmp_path, c1=c1, c2=c2, c1_layer=c1_layer, **kwargs)
        board = path.read_text().rstrip()[:-1]
        # Keep U1 locked at a non-cardinal angle and U2 explicitly fixed.
        board = board.replace("(at 20 10)", "(locked yes) (at 20 10 17)")
        board = board.replace("(at 60 10)", "(at 60 10 -23)")
        marker = board.index('(property "Reference" "C2")')
        board = board[:marker] + board[marker:].replace('"100nF"', '"10uF"')
        # A resistor at the board edge gives the routing-aware fixer real work.
        path.write_text(
            board
            + """
          (footprint "R" (layer "F.Cu") (at 0.85 15)
            (property "Reference" "R1") (property "Value" "10k")
            (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))
          (gr_rect (start 0 0) (end 80 30) (stroke (width 0.05) (type default))
            (fill none) (layer "Edge.Cuts")))
        """
        )
        return path

    return make


@pytest.fixture(params=[False, True], ids=["ordinary", "routing-aware"])
def placement_mode(request, monkeypatch):
    from types import SimpleNamespace

    from kicad_tools.optim.place_route import PlaceRouteOptimizer

    if request.param:
        # Exercise the real placement/fixer iteration while controlling routing
        # and DRC outcomes, which are independent of the CLI save contract.
        def route(self):
            print("Routing progress")
            return [], []

        monkeypatch.setattr(PlaceRouteOptimizer, "_run_routing_phase", route)
        monkeypatch.setattr(
            PlaceRouteOptimizer, "_run_drc_phase", lambda self: SimpleNamespace(passed=True)
        )
        return ["--routing-aware"]
    return []


def _placement_result(capsys, source, mode, *options, fixed="U2"):
    import json

    from kicad_tools.cli import main

    rc = main(
        [
            "placement",
            "optimize",
            str(source),
            "--fixed",
            fixed,
            "--iterations",
            "1",
            "--format",
            "json",
            *mode,
            *options,
        ]
    )
    return rc, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("override", [None, "U2.1"])
def test_placement_optimize_saves_designated_pads(
    swapped_board, placement_mode, tmp_path, capsys, override
):
    from kicad_tools.schema.pcb import PCB

    source = swapped_board(override=override)
    original = source.read_bytes()
    before = PCB.load(str(source))
    output = tmp_path / "placed.kicad_pcb"
    radius = 4.0 if override else 6.0
    options = ["--decoupling-radius", str(radius)] if override else []
    rc, result = _placement_result(capsys, source, placement_mode, "-o", str(output), *options)
    assert rc == 0 and result["success"], result
    assert source.read_bytes() == original
    placed = PCB.load(str(output))
    distances = []
    for cap, ic in [("C1", "U2" if override else "U1"), ("C2", "U2")]:
        fp = placed.get_footprint(cap)
        assert fp.layer == placed.get_footprint(ic).layer == "F.Cu"
        assert "F.Cu" in next(p for p in fp.pads if p.number == "1").layers
        distance = math.dist(placed.get_pad_position(cap, "1"), placed.get_pad_position(ic, "1"))
        assert distance <= radius
        distances.append(distance)
    if override:
        assert distances[0] < distances[1]
    for ref in ("U1", "U2"):
        old, new = before.get_footprint(ref), placed.get_footprint(ref)
        assert (new.position, new.rotation, new.layer) == (old.position, old.rotation, old.layer)
    if placement_mode:
        assert placed.get_footprint("R1").position != before.get_footprint("R1").position


@pytest.mark.parametrize("in_place,dry_run", [(False, False), (True, False), (False, True)])
def test_placement_optimize_infeasible_preserves_files(
    swapped_board, placement_mode, tmp_path, capsys, in_place, dry_run
):
    source = swapped_board()
    original = source.read_bytes()
    output = tmp_path / "previous.kicad_pcb"
    output.write_bytes(b"previous destination")
    options = [] if in_place else ["-o", str(output)]
    if dry_run:
        options.append("--dry-run")
    rc, result = _placement_result(capsys, source, placement_mode, *options, fixed="C1,U2")
    assert rc == 1 and not result["success"]
    assert any("C1" in error and "U1.1" in error for error in result["constraint_violations"])
    assert result["output_path"] is None
    assert source.read_bytes() == original
    assert output.read_bytes() == b"previous destination"


def test_placement_optimize_feasible_dry_run(swapped_board, placement_mode, tmp_path, capsys):
    source = swapped_board()
    original = source.read_bytes()
    output = tmp_path / "absent.kicad_pcb"
    rc, result = _placement_result(
        capsys,
        source,
        placement_mode,
        "--dry-run",
        "-o",
        str(output),
        "--decoupling-radius",
        "1e300",
    )
    assert rc == 0 and result["success"]
    assert source.read_bytes() == original and not output.exists()


def test_placement_optimize_no_designations(swapped_board, placement_mode, tmp_path, capsys):
    from kicad_tools.schema.pcb import PCB

    source = swapped_board(with_schematic=False)
    output = tmp_path / "placed.kicad_pcb"
    rc, result = _placement_result(
        capsys, source, placement_mode, "-o", str(output), "--decoupling-radius", "0"
    )
    assert rc == 0 and result["success"]
    placed = PCB.load(str(output))
    assert placed.get_footprint("C1").layer == "B.Cu"
    assert math.dist(placed.get_pad_position("C1", "1"), placed.get_pad_position("U1", "1")) > 6


def test_placement_optimize_larger_cap_clears_fixed_smaller_cap(
    swapped_board, placement_mode, tmp_path, capsys
):
    from kicad_tools.schema.pcb import PCB

    # The 100 nF cap is fixed 3 mm from U2.1. The 10 uF cap must move outward.
    source = swapped_board(override="U2.1", c1=63.5, c2=61.5, c1_layer="F.Cu")
    output = tmp_path / "placed.kicad_pcb"
    rc, result = _placement_result(capsys, source, placement_mode, "-o", str(output), fixed="C1,U2")
    assert rc == 0 and result["success"], result
    placed = PCB.load(str(output))
    pin = placed.get_pad_position("U2", "1")
    assert math.dist(placed.get_pad_position("C1", "1"), pin) == pytest.approx(3.0)
    assert 3 < math.dist(placed.get_pad_position("C2", "1"), pin) <= 6


@pytest.mark.parametrize("radius", ["nan", "inf", "-1"])
def test_placement_optimize_rejects_invalid_radius(swapped_board, capsys, radius):
    source = swapped_board()
    original = source.read_bytes()
    rc, result = _placement_result(capsys, source, [], "--decoupling-radius", radius)
    assert rc == 1 and not result["success"]
    assert "finite nonnegative" in result["message"]
    assert source.read_bytes() == original


@pytest.mark.parametrize("problem", ["radius", "missing-pad", "pad-layer"])
def test_placement_optimize_reports_unusable_supply_pad(
    swapped_board, placement_mode, capsys, problem
):
    source = swapped_board(c1=23, c1_layer="F.Cu")
    # Keep C1 in place to isolate the candidate-board check.
    text = source.read_text()
    start = text.index('(property "Reference" "C1")')
    end = text.index("(footprint", start)
    cap = text[start:end]
    if problem == "missing-pad":
        cap = cap.replace('(pad "1"', '(pad "9"')
    elif problem == "pad-layer":
        cap = cap.replace('(layers "F.Cu")', '(layers "B.Cu")', 1)
    source.write_text(text[:start] + cap + text[end:])
    original = source.read_bytes()
    options = ["--decoupling-radius", "0"] if problem == "radius" else []
    rc, result = _placement_result(capsys, source, placement_mode, *options, fixed="C1,U2")
    assert rc == 1 and not result["success"]
    assert any("C1" in error and "U1.1" in error for error in result["constraint_violations"])
    assert source.read_bytes() == original
