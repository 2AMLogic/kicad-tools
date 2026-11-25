"""Composite cost function aggregator for placement scoring.

Aggregates individual placement metrics (wirelength, overlap, DRC violations,
boundary violations, area) into a single scalar score for the optimizer.

Supports two scoring modes:
- Weighted-sum: single weighted sum of all components
- Lexicographic: compare by (overlap, drc, boundary) first, then weighted sum
  of (wirelength, area) — ensures feasible placements always beat infeasible ones

Usage:
    config = PlacementCostConfig()
    score = evaluate_placement(placements, nets, rules, board, config)
    print(score.total, score.is_feasible)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from .decoupling import DecouplingGroup, compute_decoupling_distance


class CostMode(Enum):
    """Scoring mode for the composite cost function."""

    WEIGHTED_SUM = "weighted_sum"
    LEXICOGRAPHIC = "lexicographic"


# Score offset added to any infeasible placement when running in
# :class:`CostMode.LEXICOGRAPHIC` mode. Used as a feasibility sentinel:
# any total score below this value indicates a feasible placement; any
# total score >= this value indicates an infeasible placement.
#
# Consumers like the CMA-ES convergence check can use this to decide
# whether the optimizer is still in the infeasible region.
INFEASIBILITY_OFFSET: float = 1e12


@dataclass(frozen=True)
class PlacementCostConfig:
    """Configuration for the composite cost function.

    Attributes:
        overlap_weight: Weight for component overlap penalty.
        drc_weight: Weight for DRC violation penalty.
        boundary_weight: Weight for board boundary violation penalty.
        wirelength_weight: Weight for total wirelength cost.
        area_weight: Weight for bounding-box area cost.
        block_boundary_weight: Weight for block boundary violation penalty.
        inter_block_spacing: Minimum spacing between block bounding boxes (mm).
        creepage_weight: Weight for the HV creepage-keepout penalty. Applied to
            :func:`compute_creepage_violation`. Like ``overlap``/``drc``/
            ``boundary`` it is treated as a hard-feasibility term: a non-zero
            creepage shortfall makes the placement infeasible.
        cohesion_weight: Weight for the same-domain clustering (cohesion)
            penalty. Applied to :func:`compute_domain_cohesion`. Unlike the
            creepage term this is a **soft preference** -- it packs footprints
            that share a voltage domain into a compact zone but never gates
            feasibility. Kept small relative to the hard-constraint weights so
            it only shapes the layout once a placement is already feasible.
        decoupling_weight: Weight for the decoupling-capacitor affinity term
            (issue #6020), the summed distance from each decoupling cap to the
            IC supply pin it is assigned (see
            :mod:`kicad_tools.placement.decoupling`). Soft, like cohesion: it
            pulls caps onto their pins but never gates feasibility. Only fires
            when decoupling groups are passed to :func:`evaluate_placement`;
            ``0.0`` disables it.
        group_cohesion_weight: Weight for the schematic-sheet cohesion term
            (issue #10146): per-sheet spread around the centroid, reusing the
            :func:`compute_domain_cohesion` formulation. Soft, like cohesion:
            it packs the footprints a schematic draws on one sheet into a
            compact zone but never gates feasibility. Only fires when a
            ``ref -> sheet_path`` map with more than one distinct sheet is
            passed to :func:`evaluate_placement` (sheets with fewer than three
            members are skipped); ``0.0`` disables it. Default ``1.0``, kept
            small relative to the hard-constraint weights like
            ``cohesion_weight``.
        group_order_weight: Weight for the schematic-order term (issue
            #10146): within a sheet, same-prefix parts are penalised when
            their order along the group's dominant axis disagrees with their
            schematic (reference/annotation) order. Soft like
            ``group_cohesion``; same map/no-op contract. Default ``1.0``.
        mode: Scoring mode (weighted_sum or lexicographic).
    """

    overlap_weight: float = 1e6
    drc_weight: float = 1e4
    boundary_weight: float = 1e5
    wirelength_weight: float = 1.0
    area_weight: float = 0.1
    block_boundary_weight: float = 1e5
    inter_block_spacing: float = 1.0
    creepage_weight: float = 1e5
    cohesion_weight: float = 1.0
    decoupling_weight: float = 2.0
    group_cohesion_weight: float = 1.0
    group_order_weight: float = 1.0
    mode: CostMode = CostMode.WEIGHTED_SUM


@dataclass(frozen=True)
class BlockRegion:
    """Axis-aligned region assigned to a block.

    Attributes:
        block_id: Identifier matching a :class:`BlockGroupDef`.
        min_x: Left edge in mm.
        min_y: Top edge in mm.
        max_x: Right edge in mm.
        max_y: Bottom edge in mm.
    """

    block_id: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float


@dataclass(frozen=True)
class CostBreakdown:
    """Per-component cost breakdown for debugging.

    Attributes:
        wirelength: Raw wirelength estimate (mm).
        overlap: Raw overlap penalty (sum of overlap areas, mm^2).
        boundary: Raw boundary violation penalty (sum of violation depths, mm).
        drc: Raw DRC violation count.
        area: Raw bounding-box area of all placements (mm^2).
        block_boundary: Raw block boundary violation penalty (mm).
        inter_block: Raw inter-block spacing violation penalty (mm).
        creepage: Raw HV creepage-keepout shortfall penalty (mm) -- the sum of
            required-minus-actual gaps across cross-domain footprint pairs that
            are closer than their required creepage.
        cohesion: Raw same-domain clustering penalty (mm) -- the sum, across
            every voltage domain with two or more members, of each member's
            distance to its domain centroid (a radius-of-gyration-style spread
            penalty). Lower means each domain is packed more tightly.
        decoupling: Raw decoupling-affinity cost (mm) -- the summed distance
            from each decoupling cap's supply pad to its assigned IC supply
            pin (issue #6020). Zero when no decoupling groups were supplied.
        group_cohesion: Raw schematic-sheet cohesion penalty (mm) -- the sum,
            across every sheet with three or more placed members, of each
            member's distance to its sheet centroid (issue #10146). Zero when
            no ``ref -> sheet_path`` map was supplied or the design has a
            single sheet.
        group_order: Raw schematic-order penalty (mm) -- the summed depth of
            the order inversions between same-prefix parts of a sheet along
            the group's dominant axis, relative to their schematic
            (reference/annotation) order (issue #10146). Zero when no map was
            supplied or the design has a single sheet.
    """

    wirelength: float = 0.0
    overlap: float = 0.0
    boundary: float = 0.0
    drc: float = 0.0
    area: float = 0.0
    block_boundary: float = 0.0
    inter_block: float = 0.0
    creepage: float = 0.0
    cohesion: float = 0.0
    decoupling: float = 0.0
    group_cohesion: float = 0.0
    group_order: float = 0.0


@dataclass(frozen=True)
class PlacementScore:
    """Result of evaluating a placement configuration.

    Attributes:
        total: Scalar score (lower is better).
        breakdown: Per-component raw costs before weighting.
        is_feasible: True if overlap=0, drc=0, boundary=0, block_boundary=0,
            and creepage=0 (HV keepout satisfied).
    """

    total: float
    breakdown: CostBreakdown
    is_feasible: bool

    def __lt__(self, other: PlacementScore) -> bool:
        return self.total < other.total

    def __le__(self, other: PlacementScore) -> bool:
        return self.total <= other.total


@dataclass(frozen=True)
class ComponentPlacement:
    """Position of a single component.

    Attributes:
        reference: Component reference designator (e.g. "U1", "R3").
        x: X position in mm.
        y: Y position in mm.
        rotation: Rotation in degrees.
    """

    reference: str
    x: float
    y: float
    rotation: float = 0.0


@dataclass(frozen=True)
class Net:
    """A net connecting component pins.

    Attributes:
        name: Net name.
        pins: Sequence of (reference, pin_name) pairs in this net.
        weight: Per-net multiplier for wirelength cost contributions.
            Defaults to 1.0 (uniform weighting). Values >1.0 prioritise
            keeping this net short (useful when one or more pins are
            anchored to a fixed perimeter footprint and the optimizer
            could otherwise stretch the channel). A weight of 0.0
            removes the net from the wirelength sum entirely.
    """

    name: str
    pins: Sequence[tuple[str, str]]
    weight: float = 1.0


@dataclass(frozen=True)
class DesignRuleSet:
    """Design rules for DRC checking.

    Attributes:
        min_clearance: Minimum copper-to-copper clearance in mm.
        min_hole_to_hole: Minimum hole-to-hole distance in mm.
        min_edge_clearance: Minimum component-to-board-edge clearance in mm.
    """

    min_clearance: float = 0.2
    min_hole_to_hole: float = 0.5
    min_edge_clearance: float = 0.3


@dataclass(frozen=True)
class BoardOutline:
    """Board boundary definition.

    Attributes:
        min_x: Left edge in mm.
        min_y: Top edge in mm.
        max_x: Right edge in mm.
        max_y: Bottom edge in mm.
    """

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y


def _oriented_size(
    placement: ComponentPlacement,
    footprint_sizes: dict[str, tuple[float, float]] | None,
    default_size: tuple[float, float],
) -> tuple[float, float]:
    """Footprint ``(width, height)`` as placed: swapped at 90/270 degrees.

    *footprint_sizes* holds the unrotated extent. Every bounding-box term
    used to read it as-is, so a cap turned 90 degrees was checked for
    overlap as if it still lay flat -- the opposite of
    :func:`~kicad_tools.placement.slide_off.slide_off_overlaps`, which swaps
    the half-sizes. The two then disagreed about which rotated parts collide,
    and the post-pass slide-off could turn a feasible optimum infeasible
    (issue #6020).
    """
    w, h = (footprint_sizes or {}).get(placement.reference, default_size)
    if int(round(placement.rotation / 90.0)) % 2:
        return h, w
    return w, h


def compute_wirelength(
    placements: Sequence[ComponentPlacement],
    nets: Sequence[Net],
    pad_positions: Mapping[tuple[str, str], tuple[float, float]] | None = None,
) -> float:
    """Compute total half-perimeter wirelength (HPWL) estimate.

    For each net, computes the half-perimeter of the bounding box of all
    connected pins. This is the standard HPWL estimator used in placement
    optimization.

    Each net's HPWL contribution is multiplied by ``net.weight`` (default
    ``1.0``). Setting ``weight > 1.0`` prioritises keeping that net short
    (used by the anchor-aware path in ``optimize-placement``); setting
    ``weight = 0.0`` excludes the net from the wirelength sum entirely.

    **Pad anchoring (issue #4831 M1).** By default each pin is measured at
    its component's *centre*, which makes the estimate blind to where a pad
    actually sits on the footprint -- and blind to rotation entirely, since
    rotating a part leaves ``p.x``/``p.y`` unchanged. Passing *pad_positions*
    (see :func:`kicad_tools.placement.wirelength.build_pad_position_map`)
    measures each pin at its own pad instead, which is the anchoring
    pcbplace reports as its single highest-leverage placement finding
    (``docs/placement-pad-anchoring-audit.md``). Resolution is per pin: a
    pin absent from *pad_positions* silently falls back to its component
    centre, so a partially-populated map degrades gracefully rather than
    dropping the net.

    Args:
        placements: Current component positions.
        nets: Net connectivity information.
        pad_positions: Optional map from ``(reference, pad_name)`` to the
            absolute ``(x, y)`` of that pad in mm. ``None`` (the default)
            preserves the historical centre-anchored behaviour exactly.

    Returns:
        Total weighted HPWL across all nets (mm).
    """
    if not nets:
        return 0.0

    pos_map: dict[str, tuple[float, float]] = {p.reference: (p.x, p.y) for p in placements}

    total = 0.0
    for net in nets:
        xs: list[float] = []
        ys: list[float] = []
        for ref, pin_name in net.pins:
            pos: tuple[float, float] | None = None
            if pad_positions is not None:
                pos = pad_positions.get((ref, pin_name))
            if pos is None:
                pos = pos_map.get(ref)
            if pos is not None:
                xs.append(pos[0])
                ys.append(pos[1])
        if len(xs) >= 2:
            total += net.weight * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
    return total


def compute_overlap(
    placements: Sequence[ComponentPlacement],
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute total pairwise overlap area between component bounding boxes.

    Args:
        placements: Current component positions.
        footprint_sizes: Map from reference to (width, height) in mm.
            If None, uses a default 1x1mm bounding box per component.

    Returns:
        Sum of pairwise overlap areas (mm^2). Zero means no overlaps.
    """
    default_size = (1.0, 1.0)

    boxes: list[tuple[float, float, float, float]] = []
    for p in placements:
        w, h = _oriented_size(p, footprint_sizes, default_size)
        half_w, half_h = w / 2, h / 2
        boxes.append((p.x - half_w, p.y - half_h, p.x + half_w, p.y + half_h))

    total_overlap = 0.0
    n = len(boxes)
    for i in range(n):
        for j in range(i + 1, n):
            x_overlap = max(0.0, min(boxes[i][2], boxes[j][2]) - max(boxes[i][0], boxes[j][0]))
            y_overlap = max(0.0, min(boxes[i][3], boxes[j][3]) - max(boxes[i][1], boxes[j][1]))
            total_overlap += x_overlap * y_overlap
    return total_overlap


def compute_boundary_violation(
    placements: Sequence[ComponentPlacement],
    board: BoardOutline,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute total boundary violation depth.

    For each component that extends beyond the board outline, sums the
    depth of violation on each edge.

    Args:
        placements: Current component positions.
        board: Board outline.
        footprint_sizes: Map from reference to (width, height) in mm.
            If None, uses a default 1x1mm bounding box per component.

    Returns:
        Sum of boundary violation depths across all components (mm).
        Zero means all components are within bounds.
    """
    default_size = (1.0, 1.0)
    total = 0.0

    for p in placements:
        w, h = _oriented_size(p, footprint_sizes, default_size)
        half_w, half_h = w / 2, h / 2

        left = p.x - half_w
        right = p.x + half_w
        top = p.y - half_h
        bottom = p.y + half_h

        # Violation on each edge (positive means outside board)
        total += max(0.0, board.min_x - left)
        total += max(0.0, right - board.max_x)
        total += max(0.0, board.min_y - top)
        total += max(0.0, bottom - board.max_y)

    return total


def compute_drc_violations(
    placements: Sequence[ComponentPlacement],
    rules: DesignRuleSet,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute count of DRC clearance violations.

    Checks pairwise clearance between component bounding boxes against
    the minimum clearance rule.

    Args:
        placements: Current component positions.
        rules: Design rules with clearance constraints.
        footprint_sizes: Map from reference to (width, height) in mm.
            If None, uses a default 1x1mm bounding box per component.

    Returns:
        Number of pairwise clearance violations (float for consistency
        with other cost components).
    """
    default_size = (1.0, 1.0)
    min_gap = rules.min_clearance

    boxes: list[tuple[float, float, float, float]] = []
    for p in placements:
        w, h = _oriented_size(p, footprint_sizes, default_size)
        half_w, half_h = w / 2, h / 2
        boxes.append((p.x - half_w, p.y - half_h, p.x + half_w, p.y + half_h))

    violations = 0.0
    n = len(boxes)
    for i in range(n):
        for j in range(i + 1, n):
            # Compute edge-to-edge gap (negative means overlap)
            gap_x = max(boxes[i][0], boxes[j][0]) - min(boxes[i][2], boxes[j][2])
            gap_y = max(boxes[i][1], boxes[j][1]) - min(boxes[i][3], boxes[j][3])

            # If boxes overlap on both axes, gap is negative on both
            # If separated, the gap is the distance on the separating axis
            if gap_x <= 0 and gap_y <= 0:
                # Overlapping — clearance is 0 (definitely a violation)
                gap = 0.0
            elif gap_x > 0 and gap_y > 0:
                # Separated on both axes — corner-to-corner distance
                gap = (gap_x**2 + gap_y**2) ** 0.5
            else:
                # Separated on one axis — edge-to-edge distance
                gap = max(gap_x, gap_y)

            if gap < min_gap:
                violations += 1.0
    return violations


def compute_area(
    placements: Sequence[ComponentPlacement],
) -> float:
    """Compute bounding-box area enclosing all component centers.

    This is a simple proxy for placement compactness.

    Args:
        placements: Current component positions.

    Returns:
        Area of the bounding box of all component centers (mm^2).
    """
    if not placements:
        return 0.0

    xs = [p.x for p in placements]
    ys = [p.y for p in placements]

    width = max(xs) - min(xs)
    height = max(ys) - min(ys)

    return width * height


def compute_block_boundary_violation(
    placements: Sequence[ComponentPlacement],
    block_regions: Sequence[BlockRegion],
    block_membership: dict[str, str],
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute total boundary violation for block member components.

    For each component that belongs to a block, penalizes any portion of
    its bounding box that extends outside the assigned block region. Uses
    the same depth-based penalty approach as :func:`compute_boundary_violation`.

    Args:
        placements: Current component positions.
        block_regions: Block boundary regions.
        block_membership: Map from component reference to block_id.
        footprint_sizes: Map from reference to (width, height) in mm.

    Returns:
        Sum of boundary violation depths across all block members (mm).
        Zero means all block members are within their assigned regions.
    """
    if not block_regions or not block_membership:
        return 0.0

    default_size = (1.0, 1.0)
    region_map = {r.block_id: r for r in block_regions}
    total = 0.0

    for p in placements:
        bid = block_membership.get(p.reference)
        if bid is None:
            continue
        region = region_map.get(bid)
        if region is None:
            continue

        w, h = _oriented_size(p, footprint_sizes, default_size)
        half_w, half_h = w / 2, h / 2

        left = p.x - half_w
        right = p.x + half_w
        top = p.y - half_h
        bottom = p.y + half_h

        total += max(0.0, region.min_x - left)
        total += max(0.0, right - region.max_x)
        total += max(0.0, region.min_y - top)
        total += max(0.0, bottom - region.max_y)

    return total


def compute_inter_block_spacing_violation(
    placements: Sequence[ComponentPlacement],
    block_membership: dict[str, str],
    min_spacing: float,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute penalty for blocks placed closer than minimum spacing.

    Computes the bounding box of each block from its member positions, then
    checks pairwise edge-to-edge distance. If any pair is closer than
    *min_spacing*, the shortfall is added to the penalty.

    Args:
        placements: Current component positions.
        block_membership: Map from component reference to block_id.
        min_spacing: Minimum spacing between block bounding boxes (mm).
        footprint_sizes: Map from reference to (width, height) in mm.

    Returns:
        Sum of spacing shortfalls across all block pairs (mm).
        Zero means all blocks are sufficiently spaced.
    """
    if not block_membership or min_spacing <= 0:
        return 0.0

    default_size = (1.0, 1.0)

    # Compute bounding box per block
    block_boxes: dict[str, list[float]] = {}  # block_id -> [min_x, min_y, max_x, max_y]
    for p in placements:
        bid = block_membership.get(p.reference)
        if bid is None:
            continue
        w, h = _oriented_size(p, footprint_sizes, default_size)
        half_w, half_h = w / 2, h / 2
        left = p.x - half_w
        right = p.x + half_w
        top = p.y - half_h
        bottom = p.y + half_h

        if bid not in block_boxes:
            block_boxes[bid] = [left, top, right, bottom]
        else:
            bb = block_boxes[bid]
            bb[0] = min(bb[0], left)
            bb[1] = min(bb[1], top)
            bb[2] = max(bb[2], right)
            bb[3] = max(bb[3], bottom)

    # Pairwise spacing check
    block_ids = list(block_boxes.keys())
    total = 0.0
    for i in range(len(block_ids)):
        for j in range(i + 1, len(block_ids)):
            a = block_boxes[block_ids[i]]
            b = block_boxes[block_ids[j]]
            # Edge-to-edge gap on each axis
            gap_x = max(a[0], b[0]) - min(a[2], b[2])
            gap_y = max(a[1], b[1]) - min(a[3], b[3])

            if gap_x <= 0 and gap_y <= 0:
                # Overlapping -- gap is 0
                gap = 0.0
            elif gap_x > 0 and gap_y > 0:
                gap = (gap_x**2 + gap_y**2) ** 0.5
            else:
                gap = max(gap_x, gap_y)

            if gap < min_spacing:
                total += min_spacing - gap

    return total


def _domain_pair_key(a: str, b: str) -> tuple[str, str]:
    """Return an order-independent key for a pair of domain ids."""
    return (a, b) if a <= b else (b, a)


def compute_creepage_violation(
    placements: Sequence[ComponentPlacement],
    ref_domains: dict[str, str],
    required_mm_by_domain_pair: dict[tuple[str, str], float],
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
    exempt_pairs: set[frozenset[str]] | None = None,
) -> float:
    """Compute the HV creepage-keepout shortfall between cross-domain footprints.

    This is the voltage-aware placement term (issue #4373). Each component is
    assigned an HV *domain* (e.g. ``"mains"``, ``"signal"``) via *ref_domains*.
    For every pair of footprints that belong to **different** domains, the
    edge-to-edge gap between their bounding boxes is compared against the
    required creepage for that domain pair (looked up in
    *required_mm_by_domain_pair*, keyed by the order-independent
    ``(min, max)`` domain-id tuple). When the gap is short of the requirement,
    the shortfall ``required - gap`` is accumulated.

    Same-domain pairs, pairs whose domain combination has no tabulated
    requirement, and pairs listed in *exempt_pairs* (guarded sense taps that are
    intentionally close to their parent HV net, issue #4373 Phase 3) contribute
    zero. Components absent from *ref_domains* are treated as domain-less and
    skipped.

    The required distances are supplied by the caller -- typically derived from
    ``kicad_tools.creepage.standards`` at the cross-domain ``|ΔV|`` -- so this
    function stays a pure numeric penalty with no standards dependency.

    Args:
        placements: Current component positions.
        ref_domains: Map from component reference to its HV domain id.
        required_mm_by_domain_pair: Map from an order-independent
            ``(domain_a, domain_b)`` tuple to the required creepage in mm.
        footprint_sizes: Map from reference to (width, height) in mm.
        exempt_pairs: Optional set of ``frozenset({ref_a, ref_b})`` pairs to
            exclude from the keepout (guarded sense taps).

    Returns:
        Sum of creepage shortfalls (mm) across all constrained cross-domain
        footprint pairs. Zero means every cross-domain pair meets its required
        creepage.
    """
    if not ref_domains or not required_mm_by_domain_pair:
        return 0.0

    default_size = (1.0, 1.0)
    exempt = exempt_pairs or set()

    boxes: list[tuple[str, str, float, float, float, float]] = []
    for p in placements:
        domain = ref_domains.get(p.reference)
        if domain is None:
            continue
        w, h = _oriented_size(p, footprint_sizes, default_size)
        half_w, half_h = w / 2, h / 2
        boxes.append(
            (
                p.reference,
                domain,
                p.x - half_w,
                p.y - half_h,
                p.x + half_w,
                p.y + half_h,
            )
        )

    total = 0.0
    n = len(boxes)
    for i in range(n):
        ref_i, dom_i, imin_x, imin_y, imax_x, imax_y = boxes[i]
        for j in range(i + 1, n):
            ref_j, dom_j, jmin_x, jmin_y, jmax_x, jmax_y = boxes[j]
            if dom_i == dom_j:
                continue
            required = required_mm_by_domain_pair.get(_domain_pair_key(dom_i, dom_j))
            if required is None or required <= 0.0:
                continue
            if frozenset((ref_i, ref_j)) in exempt:
                continue

            gap_x = max(imin_x, jmin_x) - min(imax_x, jmax_x)
            gap_y = max(imin_y, jmin_y) - min(imax_y, jmax_y)
            if gap_x <= 0 and gap_y <= 0:
                gap = 0.0
            elif gap_x > 0 and gap_y > 0:
                gap = (gap_x**2 + gap_y**2) ** 0.5
            else:
                gap = max(gap_x, gap_y)

            if gap < required:
                total += required - gap

    return total


def _centroid_spread(positions: Sequence[tuple[float, float]], start: float = 0.0) -> float:
    """Sum of member distances to the centroid of *positions* (mm), added to *start*.

    *start* lets a caller keep one running accumulator across several groups,
    preserving the original float summation order.

    The radius-of-gyration-style spread shared by the domain-cohesion and
    sheet-cohesion terms (issue #10146). Fewer than two members yield ``0.0``.
    """
    n = len(positions)
    if n < 2:
        return start
    cx = sum(x for x, _ in positions) / n
    cy = sum(y for _, y in positions) / n
    return float(sum((((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x, y in positions), start))


def compute_domain_cohesion(
    placements: Sequence[ComponentPlacement],
    ref_domains: dict[str, str] | None,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute the same-domain clustering (cohesion) penalty (issue #4373).

    This is the voltage-aware *attraction* term. It complements
    :func:`compute_creepage_violation` (which pushes **different** domains
    apart): cohesion actively packs footprints that share a voltage *domain*
    into a compact zone so a domain forms a coherent region rather than relying
    on wirelength to incidentally pull it together.

    For each domain (from *ref_domains*) with two or more member footprints, the
    centroid of the member positions is computed and each member's Euclidean
    distance to that centroid is summed; the totals across all domains are
    added. This is a radius-of-gyration-style spread penalty whose minimum is a
    tight cluster per domain. It is smooth, needs no voltages (only the domain
    partition), and depends on nothing but footprint positions.

    Singleton domains (a single member), and components absent from
    *ref_domains* (domain-less), contribute zero. An empty or ``None``
    *ref_domains* yields ``0.0`` -- the term is dormant on the no-HV-input path,
    keeping the voltage-blind objective byte-identical.

    .. note::
        This is a **soft** preference, not a feasibility gate. It is
        deliberately excluded from the ``is_feasible`` conjunction in
        :func:`evaluate_placement` and from the infeasible branch of the
        lexicographic score -- a spread-out domain is suboptimal, never
        infeasible.

    Args:
        placements: Current component positions.
        ref_domains: Map from component reference to its HV domain id. ``None``
            or empty disables the term.
        footprint_sizes: Accepted for signature parity with the other cost
            terms; unused (cohesion is centroid-of-positions based).

    Returns:
        Sum of per-domain spread penalties (mm). Zero means every domain is a
        single point (or has fewer than two members).
    """
    del footprint_sizes  # unused: cohesion is measured between footprint centres
    if not ref_domains:
        return 0.0

    by_domain: dict[str, list[tuple[float, float]]] = {}
    for p in placements:
        domain = ref_domains.get(p.reference)
        if domain is None:
            continue
        by_domain.setdefault(domain, []).append((p.x, p.y))

    total = 0.0
    for positions in by_domain.values():
        total = _centroid_spread(positions, total)
    return total


# Minimum members for a schematic sheet to count as a group (issue #10146):
# a two-part "group" carries no layout information, and a whole-board group
# (single-sheet design) must leave the objective untouched.
_SHEET_GROUP_MIN_MEMBERS = 3

_REF_SPLIT_RE = re.compile(r"^([A-Za-z]+)(\d*)(.*)$")


def _ref_prefix(reference: str) -> str:
    """The reference's leading alpha prefix (``"R10"`` -> ``"R"``)."""
    match = _REF_SPLIT_RE.match(reference)
    return match.group(1) if match else reference


def _ref_order_key(reference: str) -> tuple[str, int, str]:
    """Natural (schematic/annotation) order key for a reference.

    ``R2`` sorts before ``R10`` (numeric suffix compared as an integer); a
    missing suffix sorts first; the trailing remainder (e.g. gate letters)
    breaks any remaining tie.
    """
    match = _REF_SPLIT_RE.match(reference)
    if not match:
        return (reference, -1, "")
    prefix, digits, rest = match.groups()
    return (prefix, int(digits) if digits else -1, rest)


def _sheet_members(
    placements: Sequence[ComponentPlacement],
    ref_sheets: dict[str, str] | None,
) -> dict[str, list[ComponentPlacement]] | None:
    """Group *placements* by sheet, or ``None`` when the term must not fire.

    Returns ``None`` when *ref_sheets* is empty or the map spans fewer than
    two distinct sheets (a single-sheet design is one whole-board group and
    carries no ordering/clustering information). Components absent from the
    map are domain-less analogues: skipped.
    """
    if not ref_sheets:
        return None
    if len(set(ref_sheets.values())) < 2:
        return None
    by_sheet: dict[str, list[ComponentPlacement]] = {}
    for p in placements:
        sheet = ref_sheets.get(p.reference)
        if sheet is None:
            continue
        by_sheet.setdefault(sheet, []).append(p)
    return by_sheet


def compute_sheet_cohesion(
    placements: Sequence[ComponentPlacement],
    ref_sheets: dict[str, str] | None,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute the schematic-sheet cohesion penalty (issue #10146).

    The schematic-group *attraction* term. A group is the set of footprints
    whose symbols sit on the same schematic sheet (*ref_sheets*, from the
    netlist's ``sheetpath`` node). For each sheet with
    ``_SHEET_GROUP_MIN_MEMBERS`` or more placed members, the centroid of the
    member positions is computed and each member's Euclidean distance to that
    centroid is summed -- the same :func:`_centroid_spread` formulation as
    :func:`compute_domain_cohesion`, partitioned by sheet instead of voltage
    domain. Minimizing it packs each sheet's circuit into a compact zone.

    Single-sheet designs (one whole-board group) are a no-op, and so are an
    empty or ``None`` map -- keeping the sheet-blind objective byte-identical
    (regression-safe opt-in, mirroring the HV terms). Sheets with fewer than
    three members and components absent from the map contribute zero.

    .. note::
        This is a **soft** preference, not a feasibility gate. Like cohesion
        it is deliberately excluded from the ``is_feasible`` conjunction in
        :func:`evaluate_placement` and from the infeasible branch of the
        lexicographic score.

    Args:
        placements: Current component positions.
        ref_sheets: Map from component reference to its schematic sheet path.
            ``None`` or empty disables the term.
        footprint_sizes: Accepted for signature parity with the other cost
            terms; unused (cohesion is centroid-of-positions based).

    Returns:
        Sum of per-sheet spread penalties (mm). Zero when the term is dormant.
    """
    del footprint_sizes  # unused: cohesion is measured between footprint centres
    by_sheet = _sheet_members(placements, ref_sheets)
    if by_sheet is None:
        return 0.0

    return sum(
        _centroid_spread([(p.x, p.y) for p in members])
        for members in by_sheet.values()
        if len(members) >= _SHEET_GROUP_MIN_MEMBERS
    )


def compute_sheet_order(
    placements: Sequence[ComponentPlacement],
    ref_sheets: dict[str, str] | None,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute the schematic-order penalty within sheets (issue #10146).

    Within each sheet (see :func:`compute_sheet_cohesion` for the group
    definition), parts sharing a reference prefix are ordered by their
    schematic (reference/annotation) order -- ``R2`` before ``R10`` -- and
    their positions are read along that prefix group's **dominant axis** (the
    axis of the *prefix group's* larger bounding-box extent, not the sheet's). Every pair placed in the
    opposite order along that axis contributes the depth of the inversion
    (mm): a piecewise-linear penalty (continuous for a fixed axis; the axis pick
    flips when a prefix group's x-extent crosses its y-extent, so it is not
    continuous overall -- tolerable for derivative-free CMA-ES) that is zero for a correctly
    ordered group however far apart it is spread, and grows with how badly
    the placement disagrees with the schematic's reading order.

    The same no-op contract as :func:`compute_sheet_cohesion`: ``None``/empty
    map, a single sheet, sheets with fewer than three members, and components
    absent from the map all contribute zero. Prefix groups with a single
    member carry no order and contribute nothing.

    .. note::
        Soft preference only: never part of ``is_feasible`` nor of the
        infeasible branch of the lexicographic score.

    Args:
        placements: Current component positions.
        ref_sheets: Map from component reference to its schematic sheet path.
            ``None`` or empty disables the term.
        footprint_sizes: Accepted for signature parity with the other cost
            terms; unused (order is read between footprint centres).

    Returns:
        Sum of pairwise inversion depths (mm). Zero when the term is dormant.
    """
    del footprint_sizes  # unused: order is read between footprint centres
    by_sheet = _sheet_members(placements, ref_sheets)
    if by_sheet is None:
        return 0.0

    total = 0.0
    for members in by_sheet.values():
        if len(members) < _SHEET_GROUP_MIN_MEMBERS:
            continue
        by_prefix: dict[str, list[tuple[tuple[str, int, str], float, float]]] = {}
        for p in members:
            by_prefix.setdefault(_ref_prefix(p.reference), []).append(
                (_ref_order_key(p.reference), p.x, p.y)
            )
        for group in by_prefix.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda m: m[0])
            xs = [x for _, x, _ in group]
            ys = [y for _, _, y in group]
            axis_is_x = (max(xs) - min(xs)) >= (max(ys) - min(ys))
            coords = [x if axis_is_x else y for _, x, y in group]
            for i in range(len(coords)):
                for j in range(i + 1, len(coords)):
                    inversion = coords[i] - coords[j]
                    if inversion > 0.0:
                        total += inversion
    return total


def evaluate_placement(
    placements: Sequence[ComponentPlacement],
    nets: Sequence[Net],
    rules: DesignRuleSet,
    board: BoardOutline,
    config: PlacementCostConfig | None = None,
    footprint_sizes: dict[str, tuple[float, float]] | None = None,
    block_regions: Sequence[BlockRegion] | None = None,
    block_membership: dict[str, str] | None = None,
    ref_domains: dict[str, str] | None = None,
    required_mm_by_domain_pair: dict[tuple[str, str], float] | None = None,
    exempt_pairs: set[frozenset[str]] | None = None,
    pad_positions: Mapping[tuple[str, str], tuple[float, float]] | None = None,
    decoupling_groups: Sequence[DecouplingGroup] | None = None,
    decoupling_pad_positions: Mapping[tuple[str, str], tuple[float, float]] | None = None,
    ref_sheets: dict[str, str] | None = None,
) -> PlacementScore:
    """Evaluate a placement configuration and return a composite score.

    This is a pure function with no side effects, safe to call from
    multiple threads for parallel evaluation.

    .. note::
        This is the **optimizer objective**. Overlap is measured as raw
        axis-aligned bounding-box overlap *area* (mm^2, see
        :func:`compute_overlap`) and DRC as a bbox-clearance *count* (see
        :func:`compute_drc_violations`) -- with **no courtyard margin** and
        **no KiCad-layer awareness**. This intentionally differs from
        ``kct placement check``
        (:class:`kicad_tools.placement.analyzer.PlacementAnalyzer`), which
        expands each footprint by a ``courtyard_margin`` and reports real
        KiCad DRC violations. The two metrics serve different audiences
        (optimizer search vs. user-facing diagnostics) and can disagree by
        the courtyard margin for touching footprints. See
        ``docs/placement-scoring.md`` for the full comparison (issue #3940).

    Args:
        placements: Current component positions.
        nets: Net connectivity information for wirelength estimation.
        rules: Design rules for DRC checking.
        board: Board outline for boundary checking.
        config: Cost function configuration. Uses defaults if None.
        footprint_sizes: Optional map from reference to (width, height) in mm.
            Used by overlap, boundary, and DRC sub-functions.
        block_regions: Optional block boundary regions for block-aware scoring.
        block_membership: Optional map from component reference to block_id.
        ref_domains: Optional map from component reference to its HV domain id.
            Enables the voltage-aware creepage-keepout term (issue #4373).
        required_mm_by_domain_pair: Optional map from an order-independent
            ``(domain_a, domain_b)`` tuple to the required creepage in mm.
            Required alongside *ref_domains* for the creepage term to fire.
        exempt_pairs: Optional set of ``frozenset({ref_a, ref_b})`` pairs
            exempted from the creepage keepout (guarded sense taps).
        pad_positions: Optional map from ``(reference, pad_name)`` to that
            pad's absolute ``(x, y)`` in mm, as built by
            :func:`kicad_tools.placement.wirelength.build_pad_position_map`.
            When supplied, the **wirelength term only** is measured
            pad-to-pad instead of centre-to-centre (issue #4831 M1); every
            other term is unchanged. ``None`` (the default) keeps the
            objective byte-identical to the historical centre-anchored
            score. Pins missing from the map fall back to their component
            centre.
        decoupling_groups: Optional decoupling structure from
            :func:`kicad_tools.placement.decoupling.identify_decoupling_groups`.
            Enables the soft decoupling-affinity term (issue #6020), weighted by
            ``config.decoupling_weight``. ``None`` (the default) keeps the
            objective unchanged.
        decoupling_pad_positions: Pad map the decoupling term measures with.
            Kept separate from *pad_positions* so the decoupling term can be
            pad-accurate while wirelength stays centre-anchored. Falls back to
            *pad_positions*, then to component centres.
        ref_sheets: Optional map from component reference to its schematic
            sheet path (from the netlist's ``sheetpath`` node). Enables the
            soft schematic-group terms (issue #10146): per-sheet cohesion
            (:func:`compute_sheet_cohesion`) and within-sheet schematic order
            (:func:`compute_sheet_order`), weighted by
            ``config.group_cohesion_weight`` / ``config.group_order_weight``.
            ``None`` (the default), an empty map, and a single-sheet design
            all keep the objective byte-identical to today.

    Returns:
        PlacementScore with total score, per-component breakdown, and
        feasibility flag.
    """
    if config is None:
        config = PlacementCostConfig()

    # Compute individual cost components
    wirelength = compute_wirelength(placements, nets, pad_positions)
    overlap = compute_overlap(placements, footprint_sizes)
    boundary = compute_boundary_violation(placements, board, footprint_sizes)
    drc = compute_drc_violations(placements, rules, footprint_sizes)
    area = compute_area(placements)

    # Block-aware cost components
    block_boundary = 0.0
    inter_block = 0.0
    if block_regions and block_membership:
        block_boundary = compute_block_boundary_violation(
            placements, block_regions, block_membership, footprint_sizes
        )
        inter_block = compute_inter_block_spacing_violation(
            placements, block_membership, config.inter_block_spacing, footprint_sizes
        )

    # HV creepage-keepout term (issue #4373). Only fires when both a domain
    # map and a required-distance table are supplied -- keeps the default
    # (voltage-blind) objective byte-identical.
    creepage = 0.0
    if ref_domains and required_mm_by_domain_pair:
        creepage = compute_creepage_violation(
            placements,
            ref_domains,
            required_mm_by_domain_pair,
            footprint_sizes,
            exempt_pairs,
        )

    # Same-domain clustering (issue #4373 Phase 1). Fires whenever a domain
    # partition is supplied -- independent of the required-creepage table, since
    # clustering needs only the partition, not per-pair voltages. It is a SOFT
    # preference: computed here and added to the score, but deliberately absent
    # from the is_feasible gate below.
    cohesion = 0.0
    if ref_domains:
        cohesion = compute_domain_cohesion(placements, ref_domains, footprint_sizes)

    # Decoupling-cap affinity (issue #6020). Soft preference like cohesion:
    # added to the score, never to the is_feasible gate below.
    decoupling = 0.0
    if decoupling_groups and config.decoupling_weight:
        decoupling = compute_decoupling_distance(
            decoupling_groups,
            placements,
            decoupling_pad_positions if decoupling_pad_positions is not None else pad_positions,
        )

    # Schematic-sheet grouping (issue #10146): keep the parts a sheet draws
    # together and in schematic order where constraints allow. Both terms are
    # SOFT preferences like cohesion -- computed here and added to the score,
    # but deliberately absent from the is_feasible gate below -- and both are
    # dormant without a multi-sheet ref -> sheet_path map, keeping the
    # sheet-blind objective byte-identical.
    group_cohesion = 0.0
    group_order = 0.0
    if ref_sheets:
        group_cohesion = compute_sheet_cohesion(placements, ref_sheets)
        group_order = compute_sheet_order(placements, ref_sheets)

    breakdown = CostBreakdown(
        wirelength=wirelength,
        overlap=overlap,
        boundary=boundary,
        drc=drc,
        area=area,
        block_boundary=block_boundary,
        inter_block=inter_block,
        creepage=creepage,
        cohesion=cohesion,
        decoupling=decoupling,
        group_cohesion=group_cohesion,
        group_order=group_order,
    )

    # NOTE: cohesion, decoupling and the schematic-group terms are
    # intentionally NOT part of this conjunction. They are optimization
    # preferences (pack same-domain refs / sheet members), not feasibility
    # constraints -- a spread-out group is suboptimal, never infeasible.
    is_feasible = (
        overlap == 0.0
        and drc == 0.0
        and boundary == 0.0
        and block_boundary == 0.0
        and creepage == 0.0
    )

    if config.mode == CostMode.LEXICOGRAPHIC:
        total = _lexicographic_score(breakdown, config, is_feasible)
    else:
        total = _weighted_sum_score(breakdown, config)

    return PlacementScore(
        total=total,
        breakdown=breakdown,
        is_feasible=is_feasible,
    )


def _weighted_sum_score(breakdown: CostBreakdown, config: PlacementCostConfig) -> float:
    """Compute weighted sum of all cost components."""
    return (
        config.overlap_weight * breakdown.overlap
        + config.drc_weight * breakdown.drc
        + config.boundary_weight * breakdown.boundary
        + config.wirelength_weight * breakdown.wirelength
        + config.area_weight * breakdown.area
        + config.block_boundary_weight * breakdown.block_boundary
        + config.block_boundary_weight * breakdown.inter_block
        + config.creepage_weight * breakdown.creepage
        + config.cohesion_weight * breakdown.cohesion
        + config.decoupling_weight * breakdown.decoupling
        + config.group_cohesion_weight * breakdown.group_cohesion
        + config.group_order_weight * breakdown.group_order
    )


def _lexicographic_score(
    breakdown: CostBreakdown,
    config: PlacementCostConfig,
    is_feasible: bool,
) -> float:
    """Compute lexicographic score.

    Infeasible placements get a large penalty base ensuring they always
    score worse than any feasible placement. The penalty is the weighted
    sum of the infeasibility components (overlap, DRC, boundary) plus a
    large constant offset.

    Feasible placements are scored by the weighted sum of wirelength, area,
    and the soft same-domain cohesion, decoupling-affinity and
    schematic-group (cohesion/order, issue #10146) terms. Those shape the
    layout only once a placement is already feasible -- they are absent from
    the infeasible offset branch so they can never make one infeasible
    placement outrank another on preference alone.
    """
    if not is_feasible:
        # Large offset ensures any infeasible score > any feasible score.
        # The infeasibility components are added so the optimizer can still
        # differentiate between "slightly infeasible" and "very infeasible".
        return INFEASIBILITY_OFFSET + (
            config.overlap_weight * breakdown.overlap
            + config.drc_weight * breakdown.drc
            + config.boundary_weight * breakdown.boundary
            + config.creepage_weight * breakdown.creepage
        )
    else:
        return (
            config.wirelength_weight * breakdown.wirelength
            + config.area_weight * breakdown.area
            + config.cohesion_weight * breakdown.cohesion
            + config.decoupling_weight * breakdown.decoupling
            + config.group_cohesion_weight * breakdown.group_cohesion
            + config.group_order_weight * breakdown.group_order
        )
