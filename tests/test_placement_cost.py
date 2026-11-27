"""Unit tests for kicad_tools.placement.cost.

Focused on per-net wirelength weighting (issue #2822).  The module ships
with broader cost coverage in test_optimize_placement_cmd.py and the
strategy/seed test files; this module isolates the new ``Net.weight`` knob
introduced for anchor-aware optimisation.
"""

from __future__ import annotations

import pytest

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    CostMode,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    compute_creepage_violation,
    compute_domain_cohesion,
    compute_sheet_cohesion,
    compute_sheet_order,
    compute_wirelength,
    evaluate_placement,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _placements() -> list[ComponentPlacement]:
    """Three components arranged in an L-shape.

    Layout:

        A (0, 0)   B (10, 0)
                          |
                          C (10, 5)

    HPWL of net {A, B, C} is (10-0) + (5-0) = 15 mm.
    HPWL of net {A, B} is 10 mm; HPWL of net {B, C} is 5 mm.
    """
    return [
        ComponentPlacement(reference="A", x=0.0, y=0.0),
        ComponentPlacement(reference="B", x=10.0, y=0.0),
        ComponentPlacement(reference="C", x=10.0, y=5.0),
    ]


# ---------------------------------------------------------------------------
# Default-weight regression coverage
# ---------------------------------------------------------------------------


class TestComputeWirelengthDefaultWeight:
    """Net constructed without an explicit weight must behave as before."""

    def test_compute_wirelength_default_weight_unchanged(self) -> None:
        # Sanity: explicit weight=1.0 and no weight kwarg agree.
        placements = _placements()
        net_default = Net(name="N", pins=[("A", "1"), ("B", "1"), ("C", "1")])
        net_explicit = Net(
            name="N",
            pins=[("A", "1"), ("B", "1"), ("C", "1")],
            weight=1.0,
        )
        assert net_default.weight == 1.0
        assert compute_wirelength(placements, [net_default]) == compute_wirelength(
            placements, [net_explicit]
        )

    def test_compute_wirelength_matches_pre_weight_formula(self) -> None:
        """Aggregate equals the historical sum of HPWLs (pre-#2822)."""
        placements = _placements()
        nets = [
            Net(name="N1", pins=[("A", "1"), ("B", "1")]),  # HPWL = 10
            Net(name="N2", pins=[("B", "1"), ("C", "1")]),  # HPWL =  5
        ]
        assert compute_wirelength(placements, nets) == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Per-net weighting
# ---------------------------------------------------------------------------


class TestComputeWirelengthHonorsPerNetWeight:
    """compute_wirelength must scale each net's HPWL by ``net.weight``."""

    def test_weight_three_triples_contribution(self) -> None:
        placements = _placements()
        net = Net(
            name="HEAVY",
            pins=[("A", "1"), ("B", "1")],
            weight=3.0,
        )
        # HPWL = 10, weight = 3.0 -> 30.
        assert compute_wirelength(placements, [net]) == pytest.approx(30.0)

    def test_mixed_weights_sum_correctly(self) -> None:
        placements = _placements()
        nets = [
            Net(name="A_B", pins=[("A", "1"), ("B", "1")], weight=2.0),  # 10*2 = 20
            Net(name="B_C", pins=[("B", "1"), ("C", "1")], weight=0.5),  #  5*0.5 = 2.5
        ]
        assert compute_wirelength(placements, nets) == pytest.approx(22.5)

    def test_weight_scales_linearly(self) -> None:
        """Doubling the weight doubles the contribution."""
        placements = _placements()
        net1 = Net(name="N", pins=[("A", "1"), ("C", "1")], weight=1.0)
        net2 = Net(name="N", pins=[("A", "1"), ("C", "1")], weight=2.0)
        single = compute_wirelength(placements, [net1])
        doubled = compute_wirelength(placements, [net2])
        assert doubled == pytest.approx(2.0 * single)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestComputeWirelengthZeroWeight:
    """Weight of 0 must drop the net from the sum entirely."""

    def test_zero_weight_excludes_net(self) -> None:
        placements = _placements()
        kept = Net(name="KEEP", pins=[("A", "1"), ("B", "1")])  # HPWL=10
        dropped = Net(
            name="DROP",
            pins=[("B", "1"), ("C", "1")],  # would add 5 if weight==1
            weight=0.0,
        )
        assert compute_wirelength(placements, [kept, dropped]) == pytest.approx(10.0)

    def test_zero_weight_alone_yields_zero(self) -> None:
        placements = _placements()
        net = Net(name="ZERO", pins=[("A", "1"), ("B", "1"), ("C", "1")], weight=0.0)
        assert compute_wirelength(placements, [net]) == 0.0


# ---------------------------------------------------------------------------
# HV creepage-keepout term (issue #4373)
# ---------------------------------------------------------------------------

# Two 2x2 mm footprints; edge-to-edge gap = |dx| - 2 when placed on the X axis.
_HV_SIZES = {"A": (2.0, 2.0), "B": (2.0, 2.0)}
# Reproduces the AC_NEUTRAL <-> REF_1V65 evidence pair: required 1.6 mm.
_REQ = {("mains", "signal"): 1.6}


def _hv_pair(dx: float) -> list[ComponentPlacement]:
    """A at origin, B at (dx, 0). Both 2x2 mm -> gap = dx - 2 for dx >= 2."""
    return [
        ComponentPlacement(reference="A", x=0.0, y=0.0),
        ComponentPlacement(reference="B", x=dx, y=0.0),
    ]


class TestComputeCreepageViolation:
    def test_same_domain_pair_is_zero(self) -> None:
        """Two refs in the same domain never incur a keepout shortfall."""
        placements = _hv_pair(2.35)  # gap 0.35 mm -- but same domain
        domains = {"A": "mains", "B": "mains"}
        assert compute_creepage_violation(placements, domains, _REQ, _HV_SIZES) == 0.0

    def test_cross_domain_below_required_reports_shortfall(self) -> None:
        placements = _hv_pair(2.35)  # gap 0.35 mm, required 1.6 -> shortfall 1.25
        domains = {"A": "mains", "B": "signal"}
        got = compute_creepage_violation(placements, domains, _REQ, _HV_SIZES)
        assert got == pytest.approx(1.25)

    def test_cross_domain_at_or_above_required_is_zero(self) -> None:
        placements = _hv_pair(3.6)  # gap exactly 1.6 mm == required -> no shortfall
        domains = {"A": "mains", "B": "signal"}
        assert compute_creepage_violation(placements, domains, _REQ, _HV_SIZES) == 0.0

    def test_untabulated_domain_pair_is_zero(self) -> None:
        """A cross-domain pair with no required-distance entry is unconstrained."""
        placements = _hv_pair(2.35)
        domains = {"A": "mains", "B": "other"}  # ("mains","other") absent from _REQ
        assert compute_creepage_violation(placements, domains, _REQ, _HV_SIZES) == 0.0

    def test_exempt_pair_is_skipped(self) -> None:
        """Guarded sense taps: an exempt ref pair incurs no shortfall."""
        placements = _hv_pair(2.35)
        domains = {"A": "mains", "B": "signal"}
        exempt = {frozenset(("A", "B"))}
        got = compute_creepage_violation(placements, domains, _REQ, _HV_SIZES, exempt)
        assert got == 0.0

    def test_ref_absent_from_domains_is_skipped(self) -> None:
        placements = _hv_pair(2.35)
        domains = {"A": "mains"}  # B has no domain -> pair skipped
        assert compute_creepage_violation(placements, domains, _REQ, _HV_SIZES) == 0.0

    def test_empty_inputs_are_zero(self) -> None:
        placements = _hv_pair(2.35)
        assert compute_creepage_violation(placements, {}, _REQ, _HV_SIZES) == 0.0
        assert compute_creepage_violation(placements, {"A": "mains"}, {}, _HV_SIZES) == 0.0


class TestEvaluatePlacementCreepageFeasibility:
    """The creepage term gates feasibility in evaluate_placement."""

    _BOARD = BoardOutline(min_x=-10.0, min_y=-10.0, max_x=10.0, max_y=10.0)
    _RULES = DesignRuleSet()

    def test_too_close_is_infeasible(self) -> None:
        placements = _hv_pair(2.35)  # gap 0.35 < 1.6
        domains = {"A": "mains", "B": "signal"}
        score = evaluate_placement(
            placements,
            nets=[],
            rules=self._RULES,
            board=self._BOARD,
            config=PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC),
            footprint_sizes=_HV_SIZES,
            ref_domains=domains,
            required_mm_by_domain_pair=_REQ,
        )
        assert score.breakdown.creepage == pytest.approx(1.25)
        assert score.is_feasible is False
        # Lexicographic: infeasible placements sit above the sentinel offset.
        assert score.total >= 1e12

    def test_separated_is_feasible(self) -> None:
        placements = _hv_pair(3.6)  # gap 1.6 == required
        domains = {"A": "mains", "B": "signal"}
        score = evaluate_placement(
            placements,
            nets=[],
            rules=self._RULES,
            board=self._BOARD,
            config=PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC),
            footprint_sizes=_HV_SIZES,
            ref_domains=domains,
            required_mm_by_domain_pair=_REQ,
        )
        assert score.breakdown.creepage == 0.0
        assert score.is_feasible is True

    def test_no_domain_input_is_byte_identical(self) -> None:
        """Absent a domain/voltage input the creepage term stays dormant."""
        placements = _hv_pair(2.35)
        common = {
            "nets": [],
            "rules": self._RULES,
            "board": self._BOARD,
            "config": PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC),
            "footprint_sizes": _HV_SIZES,
        }
        baseline = evaluate_placement(placements, **common)
        # Passing domains but no required table (and vice versa) must also no-op.
        with_domains_only = evaluate_placement(
            placements, ref_domains={"A": "mains", "B": "signal"}, **common
        )
        assert baseline.breakdown.creepage == 0.0
        assert with_domains_only.breakdown.creepage == 0.0
        assert with_domains_only.total == baseline.total


# ---------------------------------------------------------------------------
# Same-domain clustering / cohesion term (issue #4373 Phase 1)
# ---------------------------------------------------------------------------


class TestComputeDomainCohesion:
    def test_empty_ref_domains_is_zero(self) -> None:
        placements = _hv_pair(10.0)
        assert compute_domain_cohesion(placements, {}) == 0.0
        assert compute_domain_cohesion(placements, None) == 0.0

    def test_single_domain_single_member_is_zero(self) -> None:
        """A domain with one member has zero spread."""
        placements = _hv_pair(10.0)
        assert compute_domain_cohesion(placements, {"A": "mains"}) == 0.0

    def test_all_singleton_domains_is_zero(self) -> None:
        """Every domain a singleton -> nothing to cluster."""
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "signal"}
        assert compute_domain_cohesion(placements, domains) == 0.0

    def test_monotonic_in_spread(self) -> None:
        """Two refs in one domain 10 mm apart cost more than 1 mm apart."""
        domains = {"A": "mains", "B": "mains"}
        far = compute_domain_cohesion(_hv_pair(10.0), domains)
        near = compute_domain_cohesion(_hv_pair(1.0), domains)
        assert far > near
        # Each member sits at half the separation from the centroid:
        # 2 members * (dx / 2) = dx.
        assert far == pytest.approx(10.0)
        assert near == pytest.approx(1.0)

    def test_domainless_refs_contribute_zero(self) -> None:
        """A ref absent from ref_domains does not join any cluster."""
        placements = [
            ComponentPlacement(reference="A", x=0.0, y=0.0),
            ComponentPlacement(reference="B", x=10.0, y=0.0),
            ComponentPlacement(reference="C", x=100.0, y=0.0),
        ]
        # C is domain-less; only A,B (same domain) cluster.
        domains = {"A": "mains", "B": "mains"}
        assert compute_domain_cohesion(placements, domains) == pytest.approx(10.0)

    def test_footprint_sizes_ignored(self) -> None:
        """footprint_sizes is accepted for parity but does not change the value."""
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "mains"}
        with_sizes = compute_domain_cohesion(placements, domains, _HV_SIZES)
        without = compute_domain_cohesion(placements, domains)
        assert with_sizes == without


class TestEvaluatePlacementCohesion:
    """Cohesion is a soft term: it scores but never gates feasibility."""

    _BOARD = BoardOutline(min_x=-100.0, min_y=-100.0, max_x=100.0, max_y=100.0)
    _RULES = DesignRuleSet()

    def _common(self, config: PlacementCostConfig) -> dict:
        return {
            "nets": [],
            "rules": self._RULES,
            "board": self._BOARD,
            "config": config,
            "footprint_sizes": _HV_SIZES,
        }

    def test_cohesion_populated_when_ref_domains_supplied(self) -> None:
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "mains"}
        score = evaluate_placement(
            placements,
            ref_domains=domains,
            **self._common(PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC)),
        )
        assert score.breakdown.cohesion == pytest.approx(10.0)

    def test_high_cohesion_keeps_feasible(self) -> None:
        """A large spread does NOT flip is_feasible (soft-term proof)."""
        placements = _hv_pair(10.0)  # 8 mm gap -> no overlap/drc/creepage
        domains = {"A": "mains", "B": "mains"}  # same domain -> no keepout
        score = evaluate_placement(
            placements,
            ref_domains=domains,
            required_mm_by_domain_pair=_REQ,
            **self._common(PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC)),
        )
        assert score.breakdown.cohesion == pytest.approx(10.0)
        assert score.breakdown.overlap == 0.0
        assert score.breakdown.drc == 0.0
        assert score.breakdown.creepage == 0.0
        assert score.is_feasible is True
        # Feasible: total sits below the infeasibility sentinel.
        assert score.total < 1e12

    def test_cohesion_enters_feasible_branch_only(self) -> None:
        """Feasible total includes the weighted cohesion term."""
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "mains"}
        config = PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC, cohesion_weight=2.0)
        score = evaluate_placement(placements, ref_domains=domains, **self._common(config))
        assert score.is_feasible is True
        # wirelength=0 (no nets), area = bbox area of the two 2x2 footprints.
        expected = config.area_weight * score.breakdown.area + 2.0 * score.breakdown.cohesion
        assert score.total == pytest.approx(expected)

    def test_cohesion_absent_from_infeasible_branch(self) -> None:
        """An infeasible (overlapping) placement's score excludes cohesion."""
        placements = _hv_pair(0.5)  # overlapping 2x2 footprints -> infeasible
        domains = {"A": "mains", "B": "mains"}
        config = PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC, cohesion_weight=1e9)
        score = evaluate_placement(placements, ref_domains=domains, **self._common(config))
        assert score.is_feasible is False
        # Infeasible offset branch = OFFSET + overlap/drc/boundary/creepage only.
        # A huge cohesion_weight must NOT leak into the total.
        expected = 1e12 + (
            config.overlap_weight * score.breakdown.overlap
            + config.drc_weight * score.breakdown.drc
            + config.boundary_weight * score.breakdown.boundary
            + config.creepage_weight * score.breakdown.creepage
        )
        assert score.total == pytest.approx(expected)

    def test_weighted_sum_includes_cohesion(self) -> None:
        placements = _hv_pair(10.0)
        domains = {"A": "mains", "B": "mains"}
        config = PlacementCostConfig(mode=CostMode.WEIGHTED_SUM, cohesion_weight=3.0)
        score = evaluate_placement(placements, ref_domains=domains, **self._common(config))
        # Isolate the cohesion contribution against a zero-weight baseline.
        baseline = evaluate_placement(
            placements,
            ref_domains=domains,
            **self._common(PlacementCostConfig(mode=CostMode.WEIGHTED_SUM, cohesion_weight=0.0)),
        )
        assert score.total - baseline.total == pytest.approx(3.0 * score.breakdown.cohesion)


# ---------------------------------------------------------------------------
# Schematic-sheet grouping (issue #10146)
# ---------------------------------------------------------------------------

_SHEET_A = "/root"
_SHEET_B = "/root/power/"


def _sheet_layout() -> list[ComponentPlacement]:
    """Three parts per sheet, each sheet an L-shaped spread.

    Sheet A: R1 (0, 0), R2 (10, 0), R3 (0, 20).
    Sheet B: U1 (50, 50), U2 (60, 50), U3 (50, 90).
    """
    return [
        ComponentPlacement(reference="R1", x=0.0, y=0.0),
        ComponentPlacement(reference="R2", x=10.0, y=0.0),
        ComponentPlacement(reference="R3", x=0.0, y=20.0),
        ComponentPlacement(reference="U1", x=50.0, y=50.0),
        ComponentPlacement(reference="U2", x=60.0, y=50.0),
        ComponentPlacement(reference="U3", x=50.0, y=90.0),
    ]


_SHEETS = {
    "R1": _SHEET_A,
    "R2": _SHEET_A,
    "R3": _SHEET_A,
    "U1": _SHEET_B,
    "U2": _SHEET_B,
    "U3": _SHEET_B,
}


class TestComputeSheetCohesion:
    def test_spread_matches_domain_formulation(self) -> None:
        """Same partition through the domain term gives the same number.

        Fixture groups are deliberately at/above ``_SHEET_GROUP_MIN_MEMBERS``
        (3), so the two terms' differing member floors are not exercised.
        """
        placements = _sheet_layout()
        assert compute_sheet_cohesion(placements, _SHEETS) == pytest.approx(
            compute_domain_cohesion(placements, _SHEETS)
        )

    def test_tighter_sheet_costs_less(self) -> None:
        spread = compute_sheet_cohesion(_sheet_layout(), _SHEETS)
        tight = [
            ComponentPlacement(reference="R1", x=0.0, y=0.0),
            ComponentPlacement(reference="R2", x=1.0, y=0.0),
            ComponentPlacement(reference="R3", x=0.0, y=1.0),
            ComponentPlacement(reference="U1", x=50.0, y=50.0),
            ComponentPlacement(reference="U2", x=51.0, y=50.0),
            ComponentPlacement(reference="U3", x=50.0, y=51.0),
        ]
        assert compute_sheet_cohesion(tight, _SHEETS) < spread

    def test_sheet_with_fewer_than_three_members_skipped(self) -> None:
        placements = [
            ComponentPlacement(reference="R1", x=0.0, y=0.0),
            ComponentPlacement(reference="R2", x=30.0, y=0.0),  # only 2 on sheet A
            ComponentPlacement(reference="U1", x=50.0, y=50.0),
            ComponentPlacement(reference="U2", x=60.0, y=50.0),
            ComponentPlacement(reference="U3", x=50.0, y=90.0),
        ]
        sheets = {"R1": _SHEET_A, "R2": _SHEET_A, "U1": _SHEET_B, "U2": _SHEET_B, "U3": _SHEET_B}
        only_b = compute_sheet_cohesion(
            [p for p in placements if p.reference != "R1" and p.reference != "R2"], sheets
        )
        assert compute_sheet_cohesion(placements, sheets) == pytest.approx(only_b)

    def test_single_sheet_is_zero(self) -> None:
        placements = _sheet_layout()
        single = dict.fromkeys(_SHEETS, _SHEET_A)
        assert compute_sheet_cohesion(placements, single) == 0.0

    def test_none_map_is_zero(self) -> None:
        assert compute_sheet_cohesion(_sheet_layout(), None) == 0.0

    def test_refs_absent_from_map_skipped(self) -> None:
        placements = _sheet_layout()
        partial = {"U1": _SHEET_B, "U2": _SHEET_B, "U3": _SHEET_B}  # R* unmapped
        only_b = compute_sheet_cohesion(
            [p for p in placements if p.reference.startswith("U")], partial
        )
        assert compute_sheet_cohesion(placements, partial) == pytest.approx(only_b)


class TestComputeSheetOrder:
    def test_schematic_order_along_x_is_zero(self) -> None:
        placements = [
            ComponentPlacement(reference="R1", x=0.0, y=0.0),
            ComponentPlacement(reference="R2", x=10.0, y=0.0),
            ComponentPlacement(reference="R3", x=20.0, y=0.0),
            ComponentPlacement(reference="U1", x=50.0, y=0.0),
            ComponentPlacement(reference="U2", x=60.0, y=0.0),
            ComponentPlacement(reference="U3", x=70.0, y=0.0),
        ]
        assert compute_sheet_order(placements, _SHEETS) == 0.0

    def test_inverted_order_reports_inversion_depth(self) -> None:
        placements = [
            ComponentPlacement(reference="R3", x=0.0, y=0.0),
            ComponentPlacement(reference="R2", x=10.0, y=0.0),
            ComponentPlacement(reference="R1", x=20.0, y=0.0),
            ComponentPlacement(reference="U1", x=50.0, y=0.0),
            ComponentPlacement(reference="U2", x=60.0, y=0.0),
            ComponentPlacement(reference="U3", x=70.0, y=0.0),
        ]
        # Pairs (R1,R2): 10, (R1,R3): 20, (R2,R3): 10 -- 40 mm of inversions
        # over 3 pairs: mean inversion depth 40/3 mm.
        assert compute_sheet_order(placements, _SHEETS) == pytest.approx(40.0 / 3)

    def test_fully_reversed_bucket_is_scale_stable(self) -> None:
        # A fully reversed bucket on a 10 mm pitch has mean inversion depth
        # (k+1)/3 * 10 mm, i.e. linear in k -- not the O(k^2) raw sum.
        def reversed_bucket(k: int) -> float:
            placements = [
                ComponentPlacement(reference=f"R{i + 1}", x=10.0 * (k - i), y=0.0) for i in range(k)
            ]
            placements += [
                ComponentPlacement(reference=f"U{i + 1}", x=1000.0 + 10.0 * i, y=0.0)
                for i in range(3)
            ]
            sheets = {f"R{i + 1}": _SHEET_A for i in range(k)}
            sheets.update({f"U{i + 1}": _SHEET_B for i in range(3)})
            return compute_sheet_order(placements, sheets)

        assert reversed_bucket(40) == pytest.approx(10.0 * 41 / 3)
        assert reversed_bucket(40) / reversed_bucket(10) < 5.0

    def test_dominant_axis_is_y_when_spread_vertically(self) -> None:
        placements = [
            # y is the wider extent of the R-group -> order read on y (ordered: 0).
            ComponentPlacement(reference="R1", x=5.0, y=0.0),
            ComponentPlacement(reference="R2", x=0.0, y=10.0),
            ComponentPlacement(reference="R3", x=1.0, y=20.0),
            ComponentPlacement(reference="U1", x=50.0, y=0.0),
            ComponentPlacement(reference="U2", x=60.0, y=0.0),
            ComponentPlacement(reference="U3", x=70.0, y=0.0),
        ]
        assert compute_sheet_order(placements, _SHEETS) == 0.0

    def test_different_prefixes_do_not_interact(self) -> None:
        placements = [
            ComponentPlacement(reference="R1", x=0.0, y=0.0),
            ComponentPlacement(reference="R2", x=10.0, y=0.0),
            ComponentPlacement(reference="R3", x=20.0, y=0.0),
            # C* inverted among themselves on the other sheet.
            ComponentPlacement(reference="C3", x=50.0, y=0.0),
            ComponentPlacement(reference="C2", x=60.0, y=0.0),
            ComponentPlacement(reference="C1", x=70.0, y=0.0),
        ]
        sheets = {**_SHEETS, "C1": _SHEET_B, "C2": _SHEET_B, "C3": _SHEET_B}
        only_c = (10.0 + 20.0 + 10.0) / 3  # mean inversion depth over 3 pairs
        assert compute_sheet_order(placements, sheets) == pytest.approx(only_c)

    def test_sheet_with_fewer_than_three_members_skipped(self) -> None:
        placements = [
            ComponentPlacement(reference="R1", x=20.0, y=0.0),  # inverted pair...
            ComponentPlacement(reference="R2", x=0.0, y=0.0),  # ...but only 2 on sheet A
            ComponentPlacement(reference="U1", x=50.0, y=0.0),
            ComponentPlacement(reference="U2", x=60.0, y=0.0),
            ComponentPlacement(reference="U3", x=70.0, y=0.0),
        ]
        sheets = {"R1": _SHEET_A, "R2": _SHEET_A, "U1": _SHEET_B, "U2": _SHEET_B, "U3": _SHEET_B}
        assert compute_sheet_order(placements, sheets) == 0.0

    def test_single_sheet_is_zero(self) -> None:
        placements = [
            ComponentPlacement(reference="R3", x=0.0, y=0.0),
            ComponentPlacement(reference="R2", x=10.0, y=0.0),
            ComponentPlacement(reference="R1", x=20.0, y=0.0),
        ]
        single = {"R1": _SHEET_A, "R2": _SHEET_A, "R3": _SHEET_A}
        assert compute_sheet_order(placements, single) == 0.0

    def test_none_map_is_zero(self) -> None:
        assert compute_sheet_order(_sheet_layout(), None) == 0.0


# A large board with three far-apart 1x1 parts: feasible by construction.
_SHEET_BOARD = BoardOutline(min_x=-50.0, min_y=-50.0, max_x=150.0, max_y=150.0)
_SHEET_RULES = DesignRuleSet()
_SHEET_NETS = [Net(name="N1", pins=[("R1", "1"), ("R2", "1")])]

_SHEET_PLACEMENTS = [
    ComponentPlacement(reference="R1", x=0.0, y=0.0),
    ComponentPlacement(reference="R2", x=40.0, y=0.0),
    ComponentPlacement(reference="R3", x=0.0, y=40.0),
    ComponentPlacement(reference="U1", x=80.0, y=80.0),
    ComponentPlacement(reference="U2", x=120.0, y=80.0),
    ComponentPlacement(reference="U3", x=80.0, y=120.0),
]


class TestEvaluatePlacementSheetTerms:
    def test_absent_map_score_identical(self) -> None:
        no_arg = evaluate_placement(_SHEET_PLACEMENTS, _SHEET_NETS, _SHEET_RULES, _SHEET_BOARD)
        with_map = evaluate_placement(
            _SHEET_PLACEMENTS, _SHEET_NETS, _SHEET_RULES, _SHEET_BOARD, ref_sheets=None
        )
        assert with_map.total == no_arg.total
        assert with_map.breakdown.group_cohesion == 0.0
        assert with_map.breakdown.group_order == 0.0

    def test_single_sheet_score_identical(self) -> None:
        single = dict.fromkeys(_SHEETS, _SHEET_A)
        baseline = evaluate_placement(_SHEET_PLACEMENTS, _SHEET_NETS, _SHEET_RULES, _SHEET_BOARD)
        got = evaluate_placement(
            _SHEET_PLACEMENTS, _SHEET_NETS, _SHEET_RULES, _SHEET_BOARD, ref_sheets=single
        )
        assert got.total == baseline.total
        assert got.breakdown.group_cohesion == 0.0
        assert got.breakdown.group_order == 0.0

    def test_multi_sheet_breakdown_populated_and_feasible(self) -> None:
        config = PlacementCostConfig(mode=CostMode.WEIGHTED_SUM)
        score = evaluate_placement(
            _SHEET_PLACEMENTS, _SHEET_NETS, _SHEET_RULES, _SHEET_BOARD, config, ref_sheets=_SHEETS
        )
        assert score.breakdown.group_cohesion > 0.0
        assert score.is_feasible  # soft term never gates feasibility

    def test_group_cohesion_weight_scales_contribution(self) -> None:
        placements = _SHEET_PLACEMENTS
        off = evaluate_placement(
            placements,
            _SHEET_NETS,
            _SHEET_RULES,
            _SHEET_BOARD,
            PlacementCostConfig(mode=CostMode.WEIGHTED_SUM, group_cohesion_weight=0.0),
            ref_sheets=_SHEETS,
        )
        on = evaluate_placement(
            placements,
            _SHEET_NETS,
            _SHEET_RULES,
            _SHEET_BOARD,
            PlacementCostConfig(mode=CostMode.WEIGHTED_SUM, group_cohesion_weight=2.0),
            ref_sheets=_SHEETS,
        )
        assert on.total == pytest.approx(off.total + 2.0 * on.breakdown.group_cohesion)

    def test_group_order_weight_scales_contribution(self) -> None:
        # R3 above R2 below... inverted along x: swap R2/R3 x positions.
        placements = [
            ComponentPlacement(reference="R1", x=40.0, y=0.0),
            ComponentPlacement(reference="R2", x=0.0, y=40.0),
            ComponentPlacement(reference="R3", x=0.0, y=0.0),
            ComponentPlacement(reference="U1", x=80.0, y=80.0),
            ComponentPlacement(reference="U2", x=120.0, y=80.0),
            ComponentPlacement(reference="U3", x=80.0, y=120.0),
        ]
        off = evaluate_placement(
            placements,
            _SHEET_NETS,
            _SHEET_RULES,
            _SHEET_BOARD,
            PlacementCostConfig(mode=CostMode.WEIGHTED_SUM, group_order_weight=0.0),
            ref_sheets=_SHEETS,
        )
        on = evaluate_placement(
            placements,
            _SHEET_NETS,
            _SHEET_RULES,
            _SHEET_BOARD,
            PlacementCostConfig(mode=CostMode.WEIGHTED_SUM, group_order_weight=1.0),
            ref_sheets=_SHEETS,
        )
        assert on.breakdown.group_order > 0.0
        assert on.total == pytest.approx(off.total + on.breakdown.group_order)

    def test_excluded_from_lexicographic_infeasible_branch(self) -> None:
        overlapping = [
            ComponentPlacement(reference="R1", x=0.0, y=0.0),
            ComponentPlacement(reference="R2", x=0.0, y=0.0),
            ComponentPlacement(reference="R3", x=0.0, y=40.0),
            ComponentPlacement(reference="U1", x=80.0, y=80.0),
            ComponentPlacement(reference="U2", x=120.0, y=80.0),
            ComponentPlacement(reference="U3", x=80.0, y=120.0),
        ]
        off = evaluate_placement(
            overlapping,
            _SHEET_NETS,
            _SHEET_RULES,
            _SHEET_BOARD,
            PlacementCostConfig(
                mode=CostMode.LEXICOGRAPHIC, group_cohesion_weight=0.0, group_order_weight=0.0
            ),
            ref_sheets=_SHEETS,
        )
        on = evaluate_placement(
            overlapping,
            _SHEET_NETS,
            _SHEET_RULES,
            _SHEET_BOARD,
            PlacementCostConfig(
                mode=CostMode.LEXICOGRAPHIC, group_cohesion_weight=1e6, group_order_weight=1e6
            ),
            ref_sheets=_SHEETS,
        )
        assert not off.is_feasible
        assert on.total == off.total

    def test_included_in_lexicographic_feasible_branch(self) -> None:
        off = evaluate_placement(
            _SHEET_PLACEMENTS,
            _SHEET_NETS,
            _SHEET_RULES,
            _SHEET_BOARD,
            PlacementCostConfig(
                mode=CostMode.LEXICOGRAPHIC, group_cohesion_weight=0.0, group_order_weight=0.0
            ),
            ref_sheets=_SHEETS,
        )
        on = evaluate_placement(
            _SHEET_PLACEMENTS,
            _SHEET_NETS,
            _SHEET_RULES,
            _SHEET_BOARD,
            PlacementCostConfig(
                mode=CostMode.LEXICOGRAPHIC, group_cohesion_weight=1.0, group_order_weight=1.0
            ),
            ref_sheets=_SHEETS,
        )
        assert off.is_feasible and on.is_feasible
        assert on.total == pytest.approx(
            off.total + on.breakdown.group_cohesion + on.breakdown.group_order
        )
