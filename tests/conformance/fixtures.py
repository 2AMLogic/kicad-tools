"""The four hand-authored named fixtures, and how to regenerate them.

Each fixture reproduces a **specific, already-observed** disagreement between
``kicad-cli pcb drc`` and one of today's in-tree clearance consumers.  They are
not random cases that happened to fail: they are the evidence from #5398,
#5410, the search-vs-commit asymmetry found while curating Epic #5509, and the
router's rectangle-bounded pad model.

Every fixture is committed under ``tests/fixtures/conformance/`` as
``<name>-seed<N>.kicad_pcb`` + ``.kicad_pro``, so the boards under test are
reviewable artefacts rather than something regenerated at test time.  Regenerate
with::

    uv run python -m tests.conformance.fixtures --write

The seeds are the issue numbers the cases come from (5398, 5410, 5425, 5229)
rather than an arbitrary counter -- the filename then says where the case came
from without a lookup table.

Consumer expectations are recorded in each case's ``notes`` and asserted (as
report-only ``xfail``) by the adapter PR.  This module and
``test_named_fixtures.py`` assert **only** kicad-cli's verdict.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from tests.conformance.board import FIXTURES_DIR, write_case
from tests.conformance.generator import (
    CaseRules,
    CopperCase,
    PadSpec,
    PairIntent,
    PairKind,
    SegmentSpec,
    ViaSpec,
    pad_shape,
)

__all__ = [
    "DOMAIN_MATRIX_FIXTURES",
    "DOMAIN_MATRIX_FIXTURE_BUILDERS",
    "FIXTURE_BUILDERS",
    "FIXTURE_RULES",
    "HV_COUPON_NAME",
    "HV_COUPON_PAIRS",
    "HV_COUPON_VOLTAGES",
    "NAMED_FIXTURES",
    "HvCouponPair",
    "build_hv_pair_matrix_coupon",
    "build_named_fixture",
    "domain_matrix_fixture_path",
    "fixture_pcb_path",
    "hv_coupon_rules_text",
    "roundrect_corner_support",
    "write_named_fixtures",
]

# ---------------------------------------------------------------------------
# Rule values, pinned
# ---------------------------------------------------------------------------
#
# These constants are the fixtures' whole point: a later "fix" that silences a
# disagreement by editing the fixture's clearance instead of the consumer must
# fail loudly.  ``test_named_fixtures.py`` asserts each fixture still carries
# exactly these numbers.

TRACE_CLEARANCE_MM = 0.15
"""Router-side trace clearance -- ``DEFAULT_ROUTE_CLEARANCE_MM`` (#5398)."""

VIA_CLEARANCE_MM = 0.20
"""Router-side via clearance -- the ``DesignRules`` default the CLI never sets."""

PROJECT_CLEARANCE_MM = 0.20
"""The ``.kicad_pro`` ``Default`` netclass clearance kicad-cli actually applies."""

HOLE_TO_HOLE_MM = 0.50
"""Drill-to-drill floor (#5410's 0.513 mm measurement is against this)."""

GRID_RESOLUTION_MM = 0.127
"""Routing grid pitch that produced #5410's six-cell via halo."""

FIXTURE_RULES = CaseRules(
    project_clearance=PROJECT_CLEARANCE_MM,
    trace_clearance=TRACE_CLEARANCE_MM,
    via_clearance=VIA_CLEARANCE_MM,
    min_hole_to_hole=HOLE_TO_HOLE_MM,
    min_drill_clearance=HOLE_TO_HOLE_MM,
    grid_resolution=GRID_RESOLUTION_MM,
)

_TRACE_WIDTH_MM = 0.15
_VIA_DIAMETER_MM = 0.6
_VIA_DRILL_MM = 0.3


# ---------------------------------------------------------------------------
# Fixture 1 -- #5398: segment vs. via at 0.18 mm, order-dependent verdict
# ---------------------------------------------------------------------------


def build_issue5398_seg_via_order() -> CopperCase:
    """0.18 mm copper gap between a 0.15 mm track and a 0.6/0.3 via.

    The gap sits in the window ``trace_clearance (0.15) < 0.18 <
    via_clearance (0.20) == project Default (0.20)``, which makes the answer
    depend on *insertion order* inside a single backend:

    * via committed first, then the segment -- ``validate_segment_clearance``
      compares against ``trace_clearance`` and **accepts**
      (``grid.py:4041-4055``; C++ twin ``grid.cpp:1394``);
    * segment first, then the via -- ``validate_via_clearance`` compares
      against ``via_clearance`` and **rejects** (``grid.py:4083-4084``;
      ``grid.cpp:1481``).

    kicad-cli has no such notion of order: the project's ``Default`` class is
    0.20 mm, so it reports one clearance violation either way.  The board is
    therefore order-free; the adapters replay both orders against it.

    **Resolved by Issue #6272.**  Both insertion orders now resolve the
    trace/via floor ``max(trace_clearance, via_clearance)`` = 0.20 through
    :func:`kicad_tools.router.clearance_resolver.trace_via_clearance_mm`
    (native twin ``router::trace_via_clearance``), so the commit gates and
    the search-time refinement all REJECT the pair -- agreeing with kicad-cli.
    """
    gap = 0.18
    seg_y = 15.0
    via_y = seg_y + _TRACE_WIDTH_MM / 2.0 + gap + _VIA_DIAMETER_MM / 2.0

    segment = SegmentSpec(
        net="SENSE_TRACK",
        start=(10.0, seg_y),
        end=(30.0, seg_y),
        width=_TRACE_WIDTH_MM,
        layer="F.Cu",
    )
    via = ViaSpec(
        net="SENSE_VIA",
        x=20.0,
        y=round(via_y, 6),
        diameter=_VIA_DIAMETER_MM,
        drill=_VIA_DRILL_MM,
    )
    return CopperCase(
        seed=5398,
        name="issue5398-seg-via-0p18-order",
        width=40.0,
        height=30.0,
        layers=2,
        rules=FIXTURE_RULES,
        segments=(segment,),
        vias=(via,),
        pairs=(
            PairIntent(
                kind=PairKind.SEG_VIA,
                net_a=segment.net,
                net_b=via.net,
                target_gap_mm=gap,
                required_mm=PROJECT_CLEARANCE_MM,
                expect_violation=True,
            ),
        ),
        notes=(
            "#5398: 0.18 mm gap between a 0.15 mm track and a 0.6/0.3 via. "
            "kicad-cli flags it against the project Default class (0.20 mm). "
            "Before #6272 the Python/C++ grid commit gates ACCEPTED when the "
            "via was inserted first and REJECTED when the segment was. "
            "Expected today: every trace/via gate resolves max(trace, via) = "
            "0.20 in both orders and REJECTS, agreeing with kicad-cli."
        ),
    )


# ---------------------------------------------------------------------------
# Fixture 2 -- #5410: a legal via the grid halo refused
# ---------------------------------------------------------------------------


def build_issue5410_dqs_n_halo() -> CopperCase:
    """The DQ3 via that board-07's A* refused next to a DQS_N via.

    Coordinates are #5410's verbatim: DQS_N at ``(143.777, 123.800)``, the
    candidate DQ3 via at ``(143.142, 123.292)``.  That is a 0.213 mm copper
    gap against a 0.20 mm requirement and a 0.513 mm drill gap against a
    0.50 mm floor -- **legal on both counts**, and kicad-cli says so.

    The grid marked DQS_N's halo as a Chebyshev *square* of
    ``int((0.3 + 0.2 + 0.075) / 0.127) + 1 + 1 = 6`` cells
    (``grid.py:_mark_via``), which swallows this candidate.  A* burned
    1,000,000 expansions and reported the net unroutable.  The board is sized
    160x140 mm and written with ``center=False`` so these coordinates can be
    quoted exactly as the issue recorded them.
    """
    a = ViaSpec(
        net="DQS_N",
        x=143.777,
        y=123.800,
        diameter=_VIA_DIAMETER_MM,
        drill=_VIA_DRILL_MM,
    )
    b = ViaSpec(
        net="DQ3",
        x=143.142,
        y=123.292,
        diameter=_VIA_DIAMETER_MM,
        drill=_VIA_DRILL_MM,
    )
    centre_distance = math.dist((a.x, a.y), (b.x, b.y))
    copper_gap = centre_distance - _VIA_DIAMETER_MM
    return CopperCase(
        seed=5410,
        name="issue5410-dqs-n-halo-vs-legal-via",
        width=160.0,
        height=140.0,
        layers=4,
        rules=FIXTURE_RULES,
        vias=(a, b),
        pairs=(
            PairIntent(
                kind=PairKind.VIA_VIA,
                net_a=a.net,
                net_b=b.net,
                target_gap_mm=round(copper_gap, 6),
                required_mm=PROJECT_CLEARANCE_MM,
                expect_violation=False,
            ),
        ),
        notes=(
            "#5410: DQ3 via 0.213 mm copper / 0.513 mm drill from DQS_N, "
            "against 0.20 / 0.50 requirements -- legal, and kicad-cli agrees. "
            "Expected today: the grid-occupancy consumer REJECTS it (six-cell "
            "Chebyshev square halo on a 0.127 mm grid) while the commit "
            "validators and the halo-geometry refinement accept it."
        ),
    )


# ---------------------------------------------------------------------------
# Fixture 3 -- search rejects what commit accepts, same backend, same pair
# ---------------------------------------------------------------------------


def build_search_vs_commit_seg_via_max() -> CopperCase:
    """A 0.18 mm segment/via gap that search refused and commit accepted.

    Here the project's ``Default`` class is **0.15 mm**, so kicad-cli is
    clean.  The router used to disagree with itself:

    * ``RouteHaloGeometry.clear`` raised the requirement to
      ``max(required, rules.via_clearance)`` for a trace-vs-via pair (C++ twin
      ``Grid3D::route_trace_geometry_clear``), i.e. 0.20 -- so A* **refused**
      this candidate;
    * the commit validators compare the same pair against ``trace_clearance``
      (0.15) and **accept** it.

    Same run, same backend, same two objects, opposite answers -- which is why
    a net could look unroutable and still pass validation once routed.

    **Resolved by Epic #5509 Phase 3b (#5661)**, then re-resolved the other
    way by **Issue #6272**.  #5661 made search agree with commit by dropping
    the widening; #6272 found the commit gates themselves order-dependent
    (via-first held the trace to ``trace_clearance``, trace-first held the via
    to ``via_clearance``) and made every trace/via gate -- commit and search,
    both backends, both orders -- resolve the one floor
    ``max(trace_clearance, via_clearance)``.  So search and commit still give
    one answer, and that answer is now REJECT at the router's own rule values
    (``via_clearance`` 0.20).  Against this fixture's 0.15 mm project class
    that is an over-rejection of the router's own stricter ``via_clearance``,
    not a kicad-cli disagreement about the pair's geometry: driven at the
    project's own values (``verdicts_at_project_rules``) every migrated row
    accepts and agrees with kicad-cli.
    """
    rules = CaseRules(
        project_clearance=TRACE_CLEARANCE_MM,  # 0.15 -- deliberately below the gap
        trace_clearance=TRACE_CLEARANCE_MM,
        via_clearance=VIA_CLEARANCE_MM,
        min_hole_to_hole=HOLE_TO_HOLE_MM,
        min_drill_clearance=HOLE_TO_HOLE_MM,
        grid_resolution=GRID_RESOLUTION_MM,
    )
    gap = 0.18
    seg_y = 15.0
    via_y = seg_y + _TRACE_WIDTH_MM / 2.0 + gap + _VIA_DIAMETER_MM / 2.0
    segment = SegmentSpec(
        net="SEARCH_TRACK",
        start=(10.0, seg_y),
        end=(30.0, seg_y),
        width=_TRACE_WIDTH_MM,
        layer="F.Cu",
    )
    via = ViaSpec(
        net="COMMIT_VIA",
        x=20.0,
        y=round(via_y, 6),
        diameter=_VIA_DIAMETER_MM,
        drill=_VIA_DRILL_MM,
    )
    return CopperCase(
        seed=5425,
        name="search-vs-commit-seg-via-max",
        width=40.0,
        height=30.0,
        layers=2,
        rules=rules,
        segments=(segment,),
        vias=(via,),
        pairs=(
            PairIntent(
                kind=PairKind.SEG_VIA,
                net_a=segment.net,
                net_b=via.net,
                target_gap_mm=gap,
                required_mm=TRACE_CLEARANCE_MM,
                expect_violation=False,
            ),
        ),
        notes=(
            "Search-vs-commit asymmetry: 0.18 mm gap with the project Default "
            "class at 0.15 mm, so kicad-cli is clean. Before Epic #5509 Phase "
            "3b (#5661) the route-halo geometry consumers REJECTED "
            "(max(required, via_clearance) = 0.20) while the Python and C++ "
            "commit gates ACCEPTED (trace_clearance = 0.15). #5661 made both "
            "ACCEPT; #6272 then made every trace/via gate resolve "
            "max(trace, via) = 0.20 in either insertion order. Expected "
            "today: search and commit both REJECT at the router's own rules "
            "(and both ACCEPT at the project's 0.15 mm, agreeing with "
            "kicad-cli)."
        ),
    )


# ---------------------------------------------------------------------------
# Fixture 4 -- roundrect corner: exact outline vs. rectangle bounds
# ---------------------------------------------------------------------------


def roundrect_corner_support(width: float, height: float, rratio: float) -> float:
    """Distance from a roundrect's centre to its outline along the diagonal.

    The corner radius is ``rratio * min(width, height)``.  The arc's centre
    sits at ``(w/2 - r, h/2 - r)`` in the pad frame, so the outline point on
    the corner diagonal is ``hypot(w/2 - r, h/2 - r) + r`` from the pad
    centre -- strictly less than the bare rectangle's ``hypot(w/2, h/2)``.
    That difference is the entire fixture.
    """
    r = rratio * min(width, height)
    return math.hypot(width / 2.0 - r, height / 2.0 - r) + r


def build_roundrect_corner_gap() -> CopperCase:
    """A track at a rotated roundrect's corner: legal outline, illegal bbox.

    A 1.0x1.0 mm roundrect pad (``rratio`` 0.25, so a 0.25 mm corner radius)
    rotated 45 degrees.  Its outline reaches 0.6036 mm along the corner
    diagonal; the *rectangle* the router models it as reaches 0.7071 mm.  The
    probe track is placed to leave a 0.22 mm gap to the true outline:

    * exact polygon model (``kct check``'s ``_pad_polygon``, and kicad-cli):
      0.22 mm >= 0.20 mm -- **clean**;
    * rectangle-bounded model (``grid.cpp:537 pad_rect_distance``; "Oval and
      roundrect pads retain conservative rectangle bounds",
      ``grid.cpp:1221-1223``): 0.1164 mm < 0.20 mm -- **rejected**.

    Because the pad is square and rotated 45 degrees, one corner diagonal
    points along the board's ``+X`` axis under either rotation sign, so the
    construction does not depend on the pad-rotation convention.
    """
    shape = pad_shape("Pad_RoundRect")
    assert shape.roundrect_rratio is not None
    pad_w, pad_h = shape.size
    support = roundrect_corner_support(pad_w, pad_h, shape.roundrect_rratio)
    exact_gap = 0.22

    pad = PadSpec(
        net="PAD_ROUNDRECT",
        reference="P1",
        footprint=shape.name,
        x=20.0,
        y=15.0,
        rotation=45.0,
        layer="F.Cu",
    )
    track_x = pad.x + support + exact_gap + _TRACE_WIDTH_MM / 2.0
    segment = SegmentSpec(
        net="CORNER_TRACK",
        start=(round(track_x, 6), 13.0),
        end=(round(track_x, 6), 17.0),
        width=_TRACE_WIDTH_MM,
        layer="F.Cu",
    )
    return CopperCase(
        seed=5229,
        name="roundrect-corner-gap",
        width=40.0,
        height=30.0,
        layers=2,
        rules=FIXTURE_RULES,
        segments=(segment,),
        pads=(pad,),
        pairs=(
            PairIntent(
                kind=PairKind.PAD_SEG,
                net_a=pad.net,
                net_b=segment.net,
                target_gap_mm=exact_gap,
                required_mm=PROJECT_CLEARANCE_MM,
                expect_violation=False,
            ),
        ),
        notes=(
            "Roundrect corner: 0.22 mm to the exact outline (clean for "
            "kicad-cli and for kct check's polygon pad model) but 0.1164 mm "
            "to the rectangle bounds the router uses. Expected today: the C++ "
            "grid REJECTS, the kct check ClearanceRule ACCEPTS."
        ),
    )


# ---------------------------------------------------------------------------
# Fixture 5 -- #10048 / #5509 P2c: the eight-pair high-voltage track coupon
# ---------------------------------------------------------------------------
#
# Unlike the four fixtures above this one is *rule-driven*: the requirement a
# pair must meet comes from authored ``.kicad_dru`` rules and a voltage map,
# not from the project's scalar clearance.  It therefore carries a sidecar
# rule file and a voltage map, and it lives in its own registry
# (:data:`DOMAIN_MATRIX_FIXTURE_BUILDERS`) rather than in
# :data:`FIXTURE_BUILDERS`: every test parametrized over ``NAMED_FIXTURES``
# assumes one scalar-rule pair per board and would mis-score eight pairs, two
# of them governed by a rule the scalar consumers cannot see.

HV_COUPON_NAME = "hv-pair-matrix-coupon"
HV_COUPON_SEED = 10048
HV_COUPON_TRACK_WIDTH_MM = 0.2
"""Both tracks of every pair; equal to the project floor and the router's ``dru``."""

HV_COUPON_PROJECT_CLEARANCE_MM = 0.20
"""Project floor and the router's scalar ``trace_clearance`` (the 0 V / 0 V rule)."""

_HV_COUPON_TRACK_X = (10.0, 30.0)
_HV_COUPON_ROW_PITCH_MM = 10.0
"""Row pitch: far above the widest cross-pair requirement (3.2 mm), so a
cross-pair finding can only mean a harness bug, never a coupon pair."""


@dataclass(frozen=True)
class HvCouponPair:
    """One track pair of the coupon: voltages, authored requirement, edge gap."""

    label: str
    net_a: str
    net_b: str
    volts_a: float
    volts_b: float
    required_mm: float
    gap_mm: float
    rule_name: str
    """The authored ``.kicad_dru`` rule that carries this pair's requirement."""

    @property
    def violates(self) -> bool:
        """Whether the edge gap is below the authored requirement."""
        return self.gap_mm < self.required_mm


# Edge-to-edge gaps and requirements are the issue's measured table (#5509
# P2c): the requirements are the IEC 60664-1 pollution-degree-2, material
# group IIIa values the existing pairwise parity tests already pin
# (``tests/router/test_pairwise_cpp_parity.py``): 150 V -> 1.6 mm, 300 V ->
# 3.2 mm; 150 V / 150 V and 0 V / 0 V fall back to the 0.2 mm floor.
HV_COUPON_PAIRS: tuple[HvCouponPair, ...] = (
    HvCouponPair("P1", "P1A", "P1B", 150.0, 0.0, 1.6, 0.700, "hv_150v_to_0v"),
    HvCouponPair("P2", "P2A", "P2B", 150.0, 0.0, 1.6, 1.800, "hv_150v_to_0v"),
    HvCouponPair("P3", "P3A", "P3B", 150.0, -150.0, 3.2, 3.000, "hv_plus150v_to_minus150v"),
    HvCouponPair("P4", "P4A", "P4B", 150.0, -150.0, 3.2, 3.400, "hv_plus150v_to_minus150v"),
    HvCouponPair("P5", "P5A", "P5B", 150.0, 150.0, 0.2, 0.150, "hv_150v_to_150v"),
    HvCouponPair("P6", "P6A", "P6B", 150.0, 150.0, 0.2, 0.700, "hv_150v_to_150v"),
    HvCouponPair("P7", "P7A", "P7B", 0.0, 0.0, 0.2, 0.100, "hv_0v_to_0v"),
    HvCouponPair("P8", "P8A", "P8B", 0.0, 0.0, 0.2, 0.300, "hv_0v_to_0v"),
)

HV_COUPON_VOLTAGES: dict[str, float] = {
    net: volts
    for pair in HV_COUPON_PAIRS
    for net, volts in ((pair.net_a, pair.volts_a), (pair.net_b, pair.volts_b))
}
"""The voltage map the coupon's matrix is built from (signed, as supplied)."""


def build_hv_pair_matrix_coupon() -> CopperCase:
    """Eight pairs of parallel 0.2 mm F.Cu tracks, one per row.

    Rows are :data:`_HV_COUPON_ROW_PITCH_MM` apart; within a row track ``A``
    sits on the row line and track ``B`` is offset by ``width + gap``, so the
    *edge* gap is the authored one.  Four pairs sit below their authored
    requirement (P1, P3, P5, P7) and four are the clear controls (P2, P4, P6,
    P8) -- the controls prove a rule is not simply flagging everything.

    The 0.2 mm requirement of P5-P8 equals the project floor, so
    only P1-P4 are *widened* by the voltage map; P5 and P7 fail the scalar
    floor in every reading, P1 and P3 only when the rule (or the matrix) is
    present.
    """
    segments: list[SegmentSpec] = []
    intents: list[PairIntent] = []
    for row, pair in enumerate(HV_COUPON_PAIRS):
        y_a = 5.0 + row * _HV_COUPON_ROW_PITCH_MM
        y_b = round(y_a + HV_COUPON_TRACK_WIDTH_MM + pair.gap_mm, 6)
        for net, y in ((pair.net_a, y_a), (pair.net_b, y_b)):
            segments.append(
                SegmentSpec(
                    net=net,
                    start=_hv_point(_HV_COUPON_TRACK_X[0], y),
                    end=_hv_point(_HV_COUPON_TRACK_X[1], y),
                    width=HV_COUPON_TRACK_WIDTH_MM,
                    layer="F.Cu",
                )
            )
        intents.append(
            PairIntent(
                kind=PairKind.SEG_SEG,
                net_a=pair.net_a,
                net_b=pair.net_b,
                target_gap_mm=pair.gap_mm,
                required_mm=pair.required_mm,
                expect_violation=pair.violates,
            )
        )

    return CopperCase(
        seed=HV_COUPON_SEED,
        name=HV_COUPON_NAME,
        width=_HV_COUPON_TRACK_X[1] + 10.0,
        height=5.0 + len(HV_COUPON_PAIRS) * _HV_COUPON_ROW_PITCH_MM,
        layers=2,
        rules=CaseRules(
            project_clearance=HV_COUPON_PROJECT_CLEARANCE_MM,
            trace_clearance=HV_COUPON_PROJECT_CLEARANCE_MM,
            via_clearance=VIA_CLEARANCE_MM,
            min_hole_to_hole=HOLE_TO_HOLE_MM,
            min_drill_clearance=HOLE_TO_HOLE_MM,
            grid_resolution=GRID_RESOLUTION_MM,
        ),
        segments=tuple(segments),
        pairs=tuple(intents),
        notes=(
            "Eight track pairs, project floor 0.20 mm, four authored symmetric "
            "pair rules in the sibling .kicad_dru (#10048, acceptance item P2c "
            "of #5509). P1/P3 fail only against the authored HV requirement; "
            "P5/P7 fail the scalar floor; P2/P4/P6/P8 are clear controls."
        ),
    )


def _hv_point(x: float, y: float) -> tuple[float, float]:
    return (x, round(y, 6))


def hv_coupon_rules_text(pairs: tuple[HvCouponPair, ...] = HV_COUPON_PAIRS) -> str:
    """The coupon's ``.kicad_dru`` text: one symmetric rule per potential class.

    Each rule matches both orderings of every net pair in its class
    (``A.NetName`` / ``B.NetName`` swapped), so the requirement does not
    depend on which side KiCad calls ``A``.  The text is deterministic.
    """
    by_rule: dict[str, list[HvCouponPair]] = {}
    for pair in pairs:
        by_rule.setdefault(pair.rule_name, []).append(pair)

    lines = ["(version 1)"]
    for rule_name, members in by_rule.items():
        required = members[0].required_mm
        clauses = []
        for pair in members:
            clauses.append(f"(A.NetName == '{pair.net_a}' && B.NetName == '{pair.net_b}')")
            clauses.append(f"(A.NetName == '{pair.net_b}' && B.NetName == '{pair.net_a}')")
        condition = " || ".join(clauses)
        lines.append(f'(rule "{rule_name}"')
        lines.append(f'  (condition "{condition}")')
        lines.append(f"  (constraint clearance (min {required:.3f}mm)))")
    return "\n".join(lines) + "\n"


FIXTURE_BUILDERS = {
    "issue5398-seg-via-0p18-order": build_issue5398_seg_via_order,
    "issue5410-dqs-n-halo-vs-legal-via": build_issue5410_dqs_n_halo,
    "search-vs-commit-seg-via-max": build_search_vs_commit_seg_via_max,
    "roundrect-corner-gap": build_roundrect_corner_gap,
}

NAMED_FIXTURES = tuple(FIXTURE_BUILDERS)

DOMAIN_MATRIX_FIXTURE_BUILDERS = {
    HV_COUPON_NAME: build_hv_pair_matrix_coupon,
}
"""Rule-driven fixtures with a ``.kicad_dru`` sidecar and a voltage map.

Kept apart from :data:`FIXTURE_BUILDERS` on purpose -- see the Fixture 5
comment above.  Committed and regenerated by the same ``--write`` entry point.
"""

DOMAIN_MATRIX_FIXTURES = tuple(DOMAIN_MATRIX_FIXTURE_BUILDERS)


def build_named_fixture(name: str) -> CopperCase:
    """Build one named fixture's :class:`CopperCase` by name."""
    try:
        builder = FIXTURE_BUILDERS[name]
    except KeyError:
        raise KeyError(
            f"Unknown conformance fixture {name!r}; known: {sorted(FIXTURE_BUILDERS)}"
        ) from None
    return builder()


def fixture_stem(case: CopperCase) -> str:
    """``<name>-seed<N>`` -- the committed filename stem."""
    return f"{case.name}-seed{case.seed}"


def fixture_pcb_path(name: str, directory: Path | None = None) -> Path:
    """Path of a committed fixture board."""
    case = build_named_fixture(name)
    return (directory or FIXTURES_DIR) / f"{fixture_stem(case)}.kicad_pcb"


def domain_matrix_fixture_path(name: str, directory: Path | None = None) -> Path:
    """Path of a committed rule-driven fixture board (its ``.kicad_dru`` is a sibling)."""
    case = DOMAIN_MATRIX_FIXTURE_BUILDERS[name]()
    return (directory or FIXTURES_DIR) / f"{fixture_stem(case)}.kicad_pcb"


def write_named_fixtures(directory: Path | None = None) -> list[Path]:
    """Write every named fixture; returns the ``.kicad_pcb`` paths written."""
    target = Path(directory) if directory is not None else FIXTURES_DIR
    written: list[Path] = []
    for name in NAMED_FIXTURES:
        case = build_named_fixture(name)
        board = write_case(case, target, stem=fixture_stem(case))
        written.append(board.pcb_path)
    for name, builder in DOMAIN_MATRIX_FIXTURE_BUILDERS.items():
        case = builder()
        board = write_case(case, target, stem=fixture_stem(case))
        # The authored pair rules live beside the board: kicad-cli (and the
        # oracle's scratch staging) load the same-stem ``.kicad_dru``.
        board.pcb_path.with_suffix(".kicad_dru").write_text(hv_coupon_rules_text())
        written.append(board.pcb_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the committed conformance fixture boards."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the boards (without it, only the destination is printed).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Destination directory (default: {FIXTURES_DIR}).",
    )
    args = parser.parse_args(argv)

    target = args.out or FIXTURES_DIR
    if not args.write:
        print(
            f"Would write {len(NAMED_FIXTURES) + len(DOMAIN_MATRIX_FIXTURES)} fixtures to {target}"
        )
        for name in NAMED_FIXTURES:
            print(f"  {fixture_stem(build_named_fixture(name))}")
        for builder in DOMAIN_MATRIX_FIXTURE_BUILDERS.values():
            print(f"  {fixture_stem(builder())}")
        return 0

    for path in write_named_fixtures(target):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
