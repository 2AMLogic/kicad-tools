"""Render ``docs/clearance-conformance.md`` -- one row per consumer group.

The table's shape is the deliverable, not just its numbers.  Epic #5509
section 1 inventories **nineteen** implementation groups that carry their own
clearance arithmetic; the acceptance criterion is that every one of them
appears, with groups that have no adapter yet marked ``not measured`` rather
than quietly omitted.  An omitted row reads as "fine"; a ``not measured`` row
reads as "unknown", which is the truth and is what keeps later phases honest
about their own coverage.

:data:`ADAPTERS` wires one adapter per measured group.  Each adapter drives an
**unmodified** consumer, and an adapter that cannot run on this machine (e.g.
``grid_cpp`` without the compiled router extension) is skipped so its group
reads ``not measured`` rather than contributing a confidently-wrong
zero-disagreement row.

A group with no adapter is never silently omitted, and never merely stamped
``not measured`` either: :data:`NOT_MEASURED_REASONS` gives each one a stated
reason, rendered in the table's ``notes`` column.  ``not measured`` with no
reason is indistinguishable from "nobody looked", which is the failure mode
the whole document exists to prevent.

Phase 1c left exactly one such gap -- group 7's ``rail_clear``, an *unexposed*
C++ lambda no binding could reach.  Epic #5509 Phase 3c (#5662) promoted it to
a bound method while migrating it onto the kernel, so :data:`NOT_MEASURED_REASONS`
is now **empty**: all nineteen groups are measured.  The dict and the machinery
around it stay, because they are what forces the *next* gap to justify itself
rather than appear as a bare blank cell.

:data:`NOTES` carries the same column for *measured* rows, where it records
the sub-entry-points a row does **not** cover (the C++ pairwise threshold, the
sub-grid escape internals, the diff-pair variant of the match-group check),
so a measured row cannot imply more coverage than it has.

The Phase 1b **kernel** is measured too, by ``adapters/kernel.py``, but it is
not one of the nineteen groups -- it is the model they are to be unified onto
-- so it renders in its own section under the key
:data:`~tests.conformance.adapters.kernel.KERNEL_GROUP` (``0``).

The rows that are still **advisory** -- neither migrated nor gated -- are
classified as well as measured (Issue #9922).  :data:`ADVISORY_GROUPS` is
their complement of :data:`MIGRATED_GROUPS`; :data:`CLASSIFICATIONS` states
each one's production caller, the two rule values it is compared at and its
disposition, and :func:`measure_corpus` reads every advisory adapter a second
time at kicad-cli's own rule value so the published classification carries
the counts and the named pairs it is based on.

Run as a module (``tests`` is a package and pytest's ``pythonpath`` only adds
``src``, so invoking this file by path cannot import its own package)::

    uv run python -m tests.conformance.report --seeds 0-23 \\
        --out docs/clearance-conformance.md

Without kicad-cli this exits non-zero with ``kicad_cli_absent`` and writes
nothing.  A table regenerated on a machine with no KiCad would show zero
disagreement everywhere -- a confident, wrong answer is worse than no answer.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tests.conformance.adapters import ConsumerAdapter
from tests.conformance.adapters.coupled import CoupledRailAdapter
from tests.conformance.adapters.diffpair import DiffPairAdapter
from tests.conformance.adapters.drc_cpp import DrcCppAdapter
from tests.conformance.adapters.drc_nudge import DrcNudgeAdapter
from tests.conformance.adapters.fixed_copper import FixedCopperAdapter
from tests.conformance.adapters.grid_cpp import GridCppAdapter
from tests.conformance.adapters.grid_cpp_occupancy import (
    CppTraceBlockedAdapter,
    GridCppMarkingAdapter,
)
from tests.conformance.adapters.grid_py import GridPyAdapter
from tests.conformance.adapters.kct_check import KctCheckAdapter
from tests.conformance.adapters.kernel import KERNEL_GROUP, KernelAdapter
from tests.conformance.adapters.lattice import LatticeAdapter
from tests.conformance.adapters.match_group import MatchGroupAdapter
from tests.conformance.adapters.mesh import MeshAdapter
from tests.conformance.adapters.occupancy import OccupancyAdapter
from tests.conformance.adapters.optimizer import OptimizerCollisionAdapter
from tests.conformance.adapters.pairwise import PairwiseAdapter
from tests.conformance.adapters.route_geometry_cpp import RouteGeometryCppAdapter
from tests.conformance.adapters.route_halo import RouteHaloAdapter
from tests.conformance.adapters.via_clearance import ViaClearanceAdapter
from tests.conformance.board import write_case
from tests.conformance.generator import (
    BOUNDARY_BAND_MM,
    CopperCase,
    PairIntent,
    PairKind,
    generate_corpus,
    parse_seed_range,
)
from tests.conformance.oracle import (
    REASON_ABSENT,
    KiCadCliAbsent,
    OracleResult,
    kicad_cli_available,
    run_oracle,
)

__all__ = [
    "ADAPTERS",
    "ADVISORY_GROUPS",
    "ADVISORY_TRACE_SEEDS",
    "CLASSIFICATION_TABLE_BEGIN",
    "CLASSIFICATION_TABLE_END",
    "CLASSIFICATIONS",
    "CONSERVATIVE",
    "DISPOSITIONS",
    "GROUPS",
    "KERNEL_GROUP",
    "MIGRATED_GROUPS",
    "NOTES",
    "NOT_MEASURED",
    "NOT_MEASURED_REASONS",
    "OUT_OF_SCOPE",
    "QUANTISED_GROUPS",
    "REFINEMENT_GATED_GROUPS",
    "RETAINED_GAP",
    "RULE_VALUE",
    "TABLE_BEGIN",
    "TABLE_END",
    "KERNEL_TABLE_BEGIN",
    "KERNEL_TABLE_END",
    "CLASSIFICATION_COLUMNS",
    "TABLE_COLUMNS",
    "AdapterMeasurement",
    "Classification",
    "CorpusStats",
    "Group",
    "SameRuleReading",
    "adapter_for_group",
    "classification_errors",
    "kicad_cli_version",
    "main",
    "measure_corpus",
    "not_measured_reason",
    "render_advisory_table",
    "render_document",
    "render_kernel_table",
    "render_table",
]

NOT_MEASURED = "not measured"

TABLE_BEGIN = "<!-- clearance-conformance:table:begin -->"
TABLE_END = "<!-- clearance-conformance:table:end -->"

KERNEL_TABLE_BEGIN = "<!-- clearance-conformance:kernel:begin -->"
KERNEL_TABLE_END = "<!-- clearance-conformance:kernel:end -->"

CLASSIFICATION_TABLE_BEGIN = "<!-- clearance-conformance:advisory:begin -->"
CLASSIFICATION_TABLE_END = "<!-- clearance-conformance:advisory:end -->"

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "clearance-conformance.md"


@dataclass(frozen=True)
class Group:
    """One Epic #5509 section-1 implementation group.

    Attributes:
        number: The group's number in the epic's inventory (1-19).
        stage: Pipeline stage the group belongs to.
        label: Short human name used in the table.
        where: Primary source location(s), for navigation.
    """

    number: int
    stage: str
    label: str
    where: str


# The nineteen groups, verbatim from Epic #5509 section 1.  Pinned here as a
# constant (rather than parsed from the epic) so the table cannot silently
# lose a row: the renderer asserts it emitted exactly ``len(GROUPS)`` rows.
GROUPS: tuple[Group, ...] = (
    Group(
        1,
        "Search-time occupancy",
        "Python grid halo marking",
        "`router/grid.py` `_mark_segment` / `_mark_via` / `_get_clearance_mask` / `_dilate_blocked`",
    ),
    Group(
        2,
        "Search-time occupancy",
        "C++ grid halo marking",
        "`router/cpp/src/grid.cpp` `Grid3D::mark_segment` / `Grid3D::mark_via`",
    ),
    Group(
        3,
        "Search-time occupancy",
        "C++ A* blocked-cell kernel",
        "`router/cpp/src/pathfinder.cpp` `Pathfinder::is_trace_blocked`",
    ),
    Group(
        4,
        "Search-time refinement",
        "Python route-halo geometry",
        "`router/route_halo_geometry.py` `RouteHaloGeometry.clear`",
    ),
    Group(
        5,
        "Search-time refinement",
        "C++ route geometry",
        "`router/cpp/src/grid.cpp` `route_trace_geometry_clear` / `route_via_geometry_clear` / `trace_stored_vias_clear`",
    ),
    Group(
        6,
        "Search-time refinement",
        "Fixed-copper predicate",
        "`router/fixed_copper.py` `segment_clear` / `via_clear`; `grid.cpp` `Grid3D::fixed_fill_clear`",
    ),
    Group(
        7,
        "Search-time refinement",
        "C++ coupled rail check",
        "`router/cpp/src/coupled_pathfinder.cpp` `rail_clear`",
    ),
    Group(
        8,
        "Search-time refinement",
        "Python coupled / diff-pair predicates",
        "`router/diffpair_routing.py` `_segment_cells_clear` / `_span_pad_clear` / `_copper_conflicts` / ...",
    ),
    Group(
        9,
        "Search-time refinement",
        "Lattice engine obstacles",
        "`router/lattice/obstacles.py` `seg_clear` / `node_clear` / `via_clear`; `lattice/coupled.py`; `lattice/pairwise.py`",
    ),
    Group(
        10,
        "Search-time refinement",
        "Mesh engine obstacles",
        "`router/mesh/obstacles.py` `is_clear`",
    ),
    Group(
        11,
        "Search-time refinement",
        "Escape router / stitcher",
        "`router/via_clearance.py` `drill_hole_to_hole_clear` / `point_clear_of_copper` / ...; `router/subgrid.py`",
    ),
    Group(
        12,
        "Commit validation",
        "Python commit gate",
        "`router/grid.py` `validate_segment_clearance` / `validate_via_clearance` / `validate_via_to_via_clearance`",
    ),
    Group(
        13,
        "Commit validation",
        "C++ commit gate",
        "`router/cpp/src/grid.cpp` `Grid3D::validate_route`",
    ),
    Group(
        14,
        "Commit validation",
        "Pairwise net-class matrix",
        "`router/pairwise_clearance.py` `required_clearance` / `path_is_clear`; `grid.cpp` `pairwise_required_clearance`",
    ),
    Group(
        15,
        "Post-route",
        "Optimizer collision checks",
        "`router/optimizer/collision.py` `path_is_clear`; `router/optimizer/trace.py` `_path_is_clear`",
    ),
    Group(
        16,
        "Post-route",
        "Match-group tuning",
        "`router/match_group_tuning.py` `_post_insertion_clearance_detail_*`",
    ),
    Group(
        17,
        "Post-route",
        "DRC nudge repair",
        "`router/drc_nudge.py` `_post_nudge_introduces_foreign_via_violation` / `_via_edge_sweep_clear`",
    ),
    Group(
        18,
        "Post-route",
        "kct check ClearanceRule",
        "`validate/rules/clearance.py` `ClearanceRule` (exact polygon pad model); `SegmentZoneClearanceRule`; `ViaZoneClearanceRule`",
    ),
    Group(
        19,
        "Post-route",
        "Incremental placement DRC (drc_cpp)",
        "`drc/cpp/src/drc_clearance.cpp` `check_pair_clearance`",
    ),
)

assert len(GROUPS) == 19, "Epic #5509 section 1 inventories nineteen groups"


# The wired consumer adapters, in Epic #5509 section-1 group order.
#
# Importing them here does NOT put a consumer import in this module: an adapter
# module is the one legitimate place for ``import kicad_tools.router`` /
# ``kicad_tools.validate``, which is why
# ``test_truth_side_does_not_import_the_code_it_measures`` scopes itself to the
# harness's own top-level modules and exempts the ``adapters/`` package.  The
# generator, the board writer and the oracle -- the truth side proper -- still
# import nothing from the code under test.
ADAPTERS: tuple[ConsumerAdapter, ...] = (
    KernelAdapter(),  # group 0 -- the Phase 1b control row, not a consumer group
    OccupancyAdapter(),
    GridCppMarkingAdapter(),
    CppTraceBlockedAdapter(),
    RouteHaloAdapter(),
    RouteGeometryCppAdapter(),
    FixedCopperAdapter(),
    CoupledRailAdapter(),
    DiffPairAdapter(),
    LatticeAdapter(),
    MeshAdapter(),
    ViaClearanceAdapter(),
    GridPyAdapter(),
    GridCppAdapter(),
    PairwiseAdapter(),
    OptimizerCollisionAdapter(),
    MatchGroupAdapter(),
    DrcNudgeAdapter(),
    KctCheckAdapter(),
    DrcCppAdapter(),
)


MIGRATED_GROUPS: frozenset[int] = frozenset({1, 2, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 18})
"""Consumer groups already switched onto the shared clearance kernel.

The single registry behind Epic #5509's scope guard #5 (*report-only until
switched*).  Every group **not** listed here has its adapter-vs-kicad-cli row
auto-``xfail``\\ ed by ``conftest.pytest_collection_modifyitems``, so a
measured disagreement is evidence for that consumer's own Phase 3/4 PR and
never a red build.  A group listed here has had that Phase 3/4 PR: its rows
are a hard gate, and a disagreement fails the suite.

Adding a group here is the *last* step of its migration, not a convenience --
``test_corpus`` asserts the flip really happened (the items carry no
``xfail``) and that the adapter can be driven at ground truth's own rule
value, and ``tests/router/test_clearance_kernel_parity.py`` asserts the
consumer really imports the kernel.

Migrated so far:

* **1** -- the Python grid's halo marking (``router/grid.py``), Phase 3a.
* **2** -- the C++ grid's halo marking (``Grid3D::mark_segment`` /
  ``mark_via``), Phase 3a.
* **4** -- the Python search-time route-halo refinement
  (``RouteHaloGeometry.clear``), Phase 3b.
* **5** -- the C++ search-time route-halo refinement
  (``Grid3D::route_trace_geometry_clear`` / ``route_via_geometry_clear`` /
  ``trace_stored_vias_clear``), Phase 3b.
* **6** -- the fixed-copper predicate (``router/fixed_copper.py`` and
  ``grid.cpp``'s ``fixed_fill_clear``), Phase 3f.
* **7** -- the C++ coupled rail gate (``CoupledPathfinder::rail_clear``), Phase 3c.
* **8** -- the Python coupled / diff-pair gates (``DiffPairRouter``), Phase 3c.
* **9** -- the lattice engine (``router/lattice/``), Phase 3d.
* **10** -- the mesh engine's per-leg consult
  (``router/mesh/obstacles.py``), Phase 3e.
* **15** -- the post-route trace optimizer's collision checkers
  (``router/optimizer/collision.py``, reached through
  ``optimizer/trace.py``'s ``_path_is_clear``), Phase 4a.
* **16** -- match-group length/phase tuning's post-insertion DRC
  self-check (``router/match_group_tuning.py``
  ``_post_insertion_clearance_detail_group`` /
  ``_post_insertion_clearance_detail_pair_group``), Phase 4b.
* **17** -- the post-route DRC-nudge repair pass's destination gates
  (``router/drc_nudge.py`` ``_post_nudge_introduces_foreign_via_violation``,
  ``_via_drill_overlaps_bbox``, ``_via_edge_sweep_clear``), Phase 4c.
* **18** -- ``kct check``'s clearance family (``validate/rules/clearance.py``'s
  ``ClearanceRule`` / ``SegmentZoneClearanceRule`` / ``ViaZoneClearanceRule``
  and ``validate/rules/edge.py``'s ``EdgeClearanceRule``), Phase 4d.
"""

_MIGRATION_PHASE: dict[int, str] = {
    1: "3a",
    2: "3a",
    4: "3b",
    5: "3b",
    6: "3f",
    7: "3c",
    8: "3c",
    9: "3d",
    10: "3e",
    15: "4a",
    16: "4b",
    17: "4c",
    18: "4d",
}
"""Which epic phase switched each migrated group, for the table's notes."""


QUANTISED_GROUPS: frozenset[int] = frozenset({1, 2})
"""Migrated groups whose gate tolerates a *pinned* over-rejection ledger.

Groups 1 and 2 answer with a **cell set** (grid occupancy), not a distance, and
a cell-quantised model over-rejects by construction: its halo radius rounds
outwards, and the adapter dilates the candidate as well as the existing copper.
Their gate is therefore *zero under-rejection, always*, plus *exactly* the
over-rejection recorded in ``test_corpus.QUANTISATION_LEDGER`` /
``test_named_fixtures.FIXTURE_QUANTISATION_LEDGER`` -- a new entry and a
vanished one both fail.  Every other migrated group is held to zero
disagreement in both directions, and ``test_corpus`` asserts no ledger entry
names a group outside this set.
"""


REFINEMENT_GATED_GROUPS: dict[int, str] = {}
"""Groups that are a merge gate **without** having been migrated, and by whose issue.

:data:`MIGRATED_GROUPS` answers *"which consumers are on the shared kernel"*.
That is not the same question as *"which consumers' rows are allowed to
disagree"*, and conflating the two leaves a third state unrepresentable: a
consumer that already agrees with ground truth, whose agreement someone paid
for, and which no later epic phase is scheduled to re-verify.

Groups 4 and 5 -- the search-time route-halo refinement, Python and native --
used to be exactly that: #5410 drove their over-rejection (a *legal*
candidate refused because a conservative halo swallowed it, the defect that
exhausted A*'s expansion budget on Board 07's DQ3) to zero, and driven at
kicad-cli's own clearance they agreed in both directions, but they still
carried their own arithmetic rather than the Phase 1b kernel.
``tests/conformance/test_route_halo_refinement_gate.py`` held that agreement
as a hard gate in the meantime, deliberately short of
:data:`MIGRATED_GROUPS` so as not to misreport the epic's progress.

Epic #5509 Phase 3b (#5661) did the actual switch, so groups 4 and 5 moved
into :data:`MIGRATED_GROUPS` and that dedicated gate module was retired --
``test_corpus.py``'s own migrated-group gate (``assert_migrated_group_agrees``)
now covers exactly the same property, so keeping both would have run one
assertion twice.  This registry stays (now empty) as the mechanism for the
*next* consumer that reaches "agrees with ground truth" before its own
kernel-switch phase lands.
"""


# Why a group has no adapter.  A bare ``not measured`` is indistinguishable
# from "nobody looked"; every gap here states its kind.
#
# EMPTY since Epic #5509 Phase 3c (#5662).  The single entry was group 7's
# ``CoupledPathfinder::rail_clear``, classified *unexposed*: a lambda inside
# the coupled search loop that ``bindings.cpp`` could not reach.  Phase 3c
# promoted it to a bound method as part of migrating it onto the clearance
# kernel, and ``adapters/coupled.py`` now measures it -- so nineteen of
# nineteen groups have an adapter.  The dict stays (with its renderer and its
# ``test_every_unwired_group_states_its_reason`` guard) because it is what
# would force a *future* gap to state its kind instead of rendering a blank.
NOT_MEASURED_REASONS: dict[int, str] = {}

# What a *measured* row does not cover.  Every sub-entry-point named in the
# epic's group inventory that this phase could not score is recorded here, so
# a measured row cannot imply more coverage than it has.
NOTES: dict[int, str] = {
    1: (
        "Cell-set answer, so no mm gap is reported. The halo is the kernel's "
        "exact dilation (#5660), not the Chebyshev square that swallowed "
        "#5410's legal via at a corner. The rate is unchanged by that switch "
        "-- the residual is outward *rounding* along the pair axis "
        "(`int(...) + 1`, doubled because this row's rejection rule dilates "
        "the candidate as well as the existing copper), not the square's "
        "diagonal excess. Retired by groups 4/5's refinement, not by a finer "
        "halo."
    ),
    2: (
        "Write side: `mark_segment` / `mark_via`, on the same kernel-derived "
        "disc as group 1 (#5660). Routed copper only -- the C++ grid never "
        "marks pads itself (`CppGrid.from_routing_grid` copies the Python "
        "blocked plane), so pad pairs would re-measure group 1."
    ),
    3: (
        "Read side: the Euclidean-disc acceptance kernel (#3229). Since #5660 "
        "the group 1/2 write side stamps a disc too, so read and write finally "
        "describe the same shape. Via candidates go through the sibling "
        "`is_via_blocked`."
    ),
    4: (
        "Issue #5661 (Epic #5509 Phase 3b): builds a `KSegment`/`KVia` for "
        "the candidate and for each surviving neighbour and asks "
        "`clearance_kernel.clear` for the verdict, in place of the retired "
        "`max(required, via_clearance)` widening for trace-vs-via pairs that "
        "made this search-time predicate stricter than the commit-time "
        "validator for the identical pair (#5410's asymmetry). Migrated: "
        "0.0% over-rejection is a hard gate (the legal-candidate refusal that "
        "exhausted A*'s budget on Board 07's DQ3), via `report.MIGRATED_GROUPS` "
        "rather than the retired `tests/conformance/test_route_halo_refinement_gate.py`. "
        "The under-rejection cell is a **rule** reading, not a geometry one, "
        "and that attribution is asserted pair-by-pair rather than claimed: "
        "every under-rejected pair (`seg-seg` only: since #6272 a `seg-via` pair "
        "resolves `max(trace_clearance, via_clearance)`, 0.20 mm at the router's "
        "own values, and is no longer under-rejected) sits in the 0.15-0.20 mm "
        "band the router's "
        "own `trace_clearance` does not require and the project's `Default` "
        "netclass does -- the #5398 / #5654 defect this row may not close. "
        "Driven at the clearance kicad-cli itself applies, the consumer "
        "agrees in **both** directions."
    ),
    5: (
        "Issue #5661 (Epic #5509 Phase 3b): the two copper checks (via-vs-"
        "segment, via-vs-via and seg-vs-segment, seg-vs-via) ask the shared "
        "kernel for the verdict instead of composing their own "
        "`centre_distance - half_a - half_b`; `route_trace_geometry_clear`'s "
        "seg-via branch no longer widens the requirement to "
        "`max(via_clearance, required(...))`, which fixed the same asymmetry "
        "as group 4. Measures the `Grid3D` predicate; the `Pathfinder` "
        "wrappers (`trace_halo_cell_clear`, `via_route_geometry_clear`) are "
        "unbound and add only cell-to-world conversion, per-net "
        "`search_fill_*` overrides and a `route_cell_has_geometry` pre-check "
        "-- no arithmetic of their own. Migrated, on the same two readings as "
        "group 4: 0.0% over-rejection is a hard gate, and the under-rejection "
        "cell (`seg-seg` only, since #6272 moved `seg-via` onto "
        "`max(trace_clearance, via_clearance)`) is "
        "asserted to be entirely the 0.15-0.20 mm "
        "`trace_clearance`-versus-`Default`-netclass band (#5398 / #5654) "
        "rather than geometry. At kicad-cli's own clearance this consumer "
        "agrees in both directions."
    ),
    6: (
        "Both halves driven (Python `FixedFillObstacles` + native "
        "`Grid3D::fixed_fill_clear`); a pair is flagged when either refuses. "
        "The fill polygon is the pad's exact outline via the `_pad_polygon` "
        "reference model, so the row measures group 6's arithmetic, not a "
        "harness approximation. Both halves now ask the kernel *per boundary "
        "edge* (`copper_gap_ring_edge` / `ring_edge_crosses_ray`) behind their "
        "own 1 mm index rather than handing it a whole `KZonePoly`: the index "
        "only selects candidate edges, and "
        "`tests/router/test_fixed_copper_kernel.py` asserts the indexed verdict "
        "equals the whole-pour one. The under-rejection cell is a **rule** "
        "reading, not a geometry one -- this row drives the consumer at the "
        "router's own `trace_clearance` (0.15 mm) while kicad-cli applies the "
        "project's `Default` netclass (0.20 mm), the #5398 / #5654 defect no "
        "Phase 3 PR is allowed to fix."
    ),
    7: (
        "The coupled search's own rail gate, driven through the bound "
        "`rail_clear_world` (Epic #5509 Phase 3c, #5662) so grid quantisation "
        "cannot move a verdict. Measures against **stored route geometry** -- "
        "the committed copper the old lambda could not see, which is #4507 -- "
        "and against fixed fills. **Not measured**: pad pairs, because "
        "`rail_clear` never consults `pads_`; pad copper reaches the coupled "
        "search through the blocked plane (groups 1-3) and through the Python "
        "constructor's exact pad gate (group 8)."
    ),
    8: (
        "Three gates in series (`_segment_cells_clear` raster walk through the "
        "**Python** `CoupledPathfinder._is_cell_blocked`, the #4571 exact "
        "`_segment_pad_clear`, the #4575 `_route_via_clear`); flagged when any "
        "refuses. The via gate is driven inside the consumer's own "
        "`_shadow_foreign_copper` context manager -- outside it "
        "`_shadow_foreign_universe` is `None` and the gate short-circuits to "
        "`(0.0, None)`, so an unarmed run would have reported a confident zero "
        "for a third of this row. **Not measured**: "
        "`find_intra_pair_clearance_violations` (`router/diffpair_routing.py`) "
        "scores a P/N pair against *itself*, and this corpus declares no diff "
        "pairs -- every object carries its own independent net so a kicad-cli "
        "row maps onto a pair by net alone."
    ),
    9: (
        "`CommittedCopper.seg_clear` / `via_clear` -- the epic's group 9 line "
        "citations land on `CommittedCopper`, not `LatticeObstacleModel`, so "
        "the row needs no board file and no `from_board`: the five derived gap "
        "values are copied from the model's one production call site "
        "(`LatticePathfinder.__init__` plus `_fresh_committed` in "
        "`router/lattice/pathfinder.py`). "
        "Routed copper only -- none of the three predicates consults a pad "
        "(pad keep-outs gate *site availability* on "
        "`LatticeObstacleModel.node_pads`, which is group 9's masking half, not "
        "its clearance arithmetic). The predicates return a bool and no gap, so "
        "no mm gap is reported. `pairwise` left `None`, the only path reachable "
        "without `--voltage-map` (Phase 2's corpus). The under-rejection cell is "
        "a **rule** reading, not a geometry one: this row drives the consumer at "
        "the router's own `trace_clearance` (0.15 mm), so every under-rejected "
        "pair sits in the 0.15-0.20 mm band the project's `Default` netclass "
        "requires and the router does not -- the #5398 / #5654 defect, which "
        "Phase 3d does not touch."
    ),
    10: (
        "`ObstacleModel.is_clear`, constructed directly as "
        "`MeshPathfinder._route_with_portals` does: other-net pads passed "
        "**verbatim** (`_foreign_pads`) and measured exactly through the "
        "kernel's Minkowski pad model, committed traces as capsule polygons "
        "inflated by a **full** `trace_width + clearance` (`_route_obstacles`' "
        "own over-approximation, square end-caps included, unchanged by this "
        "phase). Seg-candidate pairs only -- `is_clear(a, b)` takes two points "
        "and no width of its own, and a committed via is not in this model at "
        "all (`_route_via_injection` handles those). `fixed_fills` left "
        "`None`: group 6 already measures `FixedFillObstacles` on both its "
        "halves. Before Phase 3e a pad entered as its `pad_half_extents` "
        "**bounding box** grown by the agent radius -- a legal candidate "
        "refused during search, #5410's failure mode -- and the `outline` "
        "branch was a bare containment test with no clearance term; both are "
        "now kernel measurements against the real copper. The `copper-edge` "
        "cell that remains is a **rule** reading, not a geometry one: this row "
        "drives the consumer at the board-edge floor it actually resolves, "
        "which `MeshPathfinder.edge_clearance` takes from the owning "
        "`Autorouter._edge_clearance` and which is 0.0 with none configured, "
        "while kicad-cli applies the `.kicad_pro` board rule -- the #5398 / "
        "#5654 axis, which Phase 3e does not touch. The `pours` branch is "
        "still driven by `seg-zone` pairs against pour outlines verbatim, as "
        "`_route_obstacles` passes them, and is still a touch test with no "
        "clearance term, so a leg that merely comes close to a pour is "
        "accepted. **Not measured**: `via-zone`, excluded for the same reason "
        "`seg-via` is -- `is_clear(a, b)` takes two points and no width, so a "
        "via candidate has no call to make."
    ),
    11: (
        "`via_clearance.py`'s four pure predicates. **Not measured**: "
        "`_min_clearance_to_neighbors` and `_validate_segment_relaxed` "
        "(`router/subgrid.py`) are private `SubGridRouter` internals "
        "needing a live sub-grid escape context (parent grid, pad-cluster box, "
        "per-pad overrides) -- escape router's Phase 3 PR. Nor are the other "
        "two encodings a production caller gives a foreign pad: the escape "
        "router's rotation-aware box and the stitcher's disc-bound 4-tuple, "
        "both of which can only over-reject (Issue #9922)."
    ),
    12: "Via-first insertion order only; the other order is #5398's fixture.",
    13: (
        "Via-first insertion order only. Pads carry no roundrect/oval flag -- "
        "`Grid3D::add_pad` takes `is_circular` and a rotation and nothing else, "
        "which is the `roundrect-corner-gap` mechanism."
    ),
    14: (
        "Scalar (`dru`) behaviour: every net at 0 V, so no creepage widening "
        "applies and the HV `max()` can only tighten from here. **Not "
        "measured**: `Grid3D::pairwise_required_clearance` "
        "(`grid.cpp:751`) is dormant until `set_pairwise_domains` installs a "
        "domain matrix, so it would return a meaningless 0.0 -- it needs an HV "
        "corpus (Phase 2)."
    ),
    15: (
        "`VectorCollisionChecker`, which delegates to `GridCollisionChecker` "
        "when the per-layer R-tree is unpopulated, so both citations are "
        "exercised by this row. `ignore_overflow` left at its stricter default. "
        "Issue #5854 (Epic #5509 Phase 4a): every verdict this consumer "
        "reaches now comes from the shared clearance kernel -- segment, via "
        "and, new in that phase, **pad** copper, which both checkers used to "
        "judge off the raster (a pad's blocked footprint is its metal plus its "
        "own clearance halo, and the candidate was dilated by `width / 2 + "
        "trace_clearance` on top, so a path 0.247 mm from pad metal was "
        "refused against a 0.20 mm requirement). The raster stays the broad "
        "phase: a cell whose blockage registers no geometry to re-measure -- a "
        "keepout, an obstacle, a region bound, the board-edge band, all "
        "reported by `raster_only_blocked_cell` -- still rejects outright. "
        "Migrated, so the row is a hard gate; driven at the clearance "
        "kicad-cli itself applies, the consumer agrees in **both** directions. "
        "The published under-rejection cell keeps the consumer's own rule values "
        "and is a rule reading rather than a geometry one: the 0.15 mm "
        "`trace_clearance` sits below the project's 0.20 mm `Default` netclass "
        "-- the #5398 / #5654 defect no migration phase may close."
    ),
    16: (
        "**Not measured**: `_post_insertion_clearance_detail_pair_group` "
        "(`router/match_group_tuning.py`) needs a mirrored diff-pair candidate "
        "with declared P/N net ids, which the generator does not place. Group "
        "8's row does not close this: it drives the coupled *constructor's* "
        "gates over ordinary pairs, so the missing ingredient is a corpus pair "
        "kind, not a live `DiffPairRouter`."
    ),
    17: (
        "The destination gate's foreign-via clearance check. Issue #5856 (Epic "
        "#5509 Phase 4c): `_post_nudge_introduces_foreign_via_violation` takes "
        "its verdict from `clearance_shapes.shapes_clear` instead of "
        "`via_clearance.segment_clears_foreign_via`'s own `distance - "
        "via_radius - half_width`, so this post-route gate and the search / "
        "commit gates can no longer disagree about one pair; the geometry of a "
        "round barrel against a trace is the same reading either way, and what "
        "the switch removes is the second place it was written down (plus a "
        "private 1e-9 epsilon, now the kernel's `CLEARANCE_EPSILON_MM`). The "
        "under-rejection cell is a **rule** reading, not a geometry one: this "
        "row drives the gate at the router's own `trace_clearance` (0.15 mm) "
        "while kicad-cli applies the project's `Default` netclass (0.20 mm) -- "
        "the #5398 / #5654 defect, which this phase does not touch. Driven at "
        "the clearance kicad-cli itself applies, the consumer agrees in "
        "**both** directions. **Not measured**: `_via_drill_overlaps_bbox` is "
        "an overlap detector with no clearance term (every corpus pair has a "
        "positive gap, so it would be a meaningless zero), and "
        "`_via_edge_sweep_clear` is a displacement certificate against the "
        "board outline needing a before/after position pair and a "
        "copper-to-edge pair kind -- both moved onto the kernel's `hole_gap` / "
        "`copper_gap` in the same phase, with their verdict-for-verdict "
        "equivalence pinned by "
        "`tests/router/test_drc_nudge_kernel_5856.py` instead of by this row."
    ),
    18: (
        "Issue #5857: "
        "`ClearanceRule._calculate_clearance` and `EdgeClearanceRule` now ask "
        "`clearance_kernel.copper_gap` for the pair-distance verdict (via the "
        "`validate/clearance_shapes.py` translation), in place of the retired "
        "per-pair-kind dispatch (`_segment_circle_clearance` / "
        "`_circle_circle_clearance` and friends) that measured a rotated or "
        "non-square `rect` pad against its axis-aligned bounding box -- an "
        "over-rejection, the `roundrect-corner-gap` failure mode, on three of "
        "the twenty-three seeds that first surfaced it. The merge gate "
        "(`test_corpus.test_adapter_agrees_with_kicad_cli`) covers `seg-seg`, "
        "`seg-via`, `via-via`, `pad-seg`, `pad-via` and `copper-edge` -- every "
        "pair kind this row claims except the two zone kinds, which "
        "`pair.kind in PairKind.ZONE` excludes from that gate for the same "
        "reason the table scores them only on a refilled run (see below) -- "
        "and it already agreed on every `CI_SEEDS` item before this phase "
        "started measuring it, so the switch is a pure dedup here, not a "
        "fix with a before/after delta. One scalar `min_clearance_mm` for "
        "every pair, so it cannot reproduce groups 12/13's order asymmetry -- "
        "`KctCheckAdapter.verdicts_at_project_rules` is a plain alias of "
        "`verdicts`, because there is no separate rule-value axis for this "
        "consumer to pin. Five entry points driven as `validate/checker.py` "
        "registers them: `ClearanceRule`, `EdgeClearanceRule` (on "
        "`copper-edge` pairs), `SegmentZoneClearanceRule` / "
        "`ViaZoneClearanceRule` and `physical_gap.py`'s "
        "`check_physical_copper_gap` (on `seg-zone` / `via-zone` pairs, "
        "**not** switched to the kernel -- see that module's own Phase 4d "
        "scope note) -- the last three read committed `filled_polygon` "
        "copper, so this row **refills the board** (`kicad-cli pcb drc "
        "--refill-zones`) on every case that carries a pour and its zone "
        "cells are scored on the refilled run only (#5644), report-only "
        "regardless of this migration "
        "(`test_corpus.test_zone_pairs_are_measured_on_a_refilled_run` is "
        "never attributable to a single group). A fresh fill is backed off "
        "from foreign copper by the applied clearance, so a zone pair can "
        "only be placed above the threshold: those pairs can show "
        "over-rejection and cannot show under-rejection."
    ),
    19: (
        "Every pad is a disc of `max(w, h) / 2` "
        "(`_extract_pad_arrays` in `drc/cpp_backend.py`), which can over-reject "
        "but never "
        "under-reject. **Not measured on the corpus**: the corpus probes the one "
        "axis where that envelope is tight (each probe shape's long side is its "
        "local X side, so `max(w, h) / 2` is the true support there), and it "
        "places no `pad-pad` pair on the gate seeds at all. The off-axis "
        "over-approximation is demonstrated instead by the named control "
        "`drc-cpp-disc-off-axis` "
        "(`test_corpus.test_drc_cpp_disc_envelope_control`, Issue #9922): two "
        "1.2 x 0.8 mm rect pads 0.22 mm apart across their short sides are "
        "clean for kicad-cli and rejected here at the same rule."
    ),
    KERNEL_GROUP: (
        "Control row, not a consumer group: both ports (`clearance_kernel.py` "
        "and `router_cpp`) driven, flagged when either flags. `required_mm` is "
        "the `.kicad_pro` `Default` netclass value kicad-cli applies, because "
        "the kernel carries no rule values of its own -- Phase 2's resolver "
        "owns that. Any non-zero cell is a Phase 1b bug: capture a fixture and "
        "fix it through `test_clearance_kernel_parity.py`, never here."
    ),
}


# ---------------------------------------------------------------------------
# Advisory groups at a matched rule (Issue #9922, Epic #5509 items E1 / C3)
# ---------------------------------------------------------------------------
#
# ``MIGRATED_GROUPS`` says which consumers are on the shared kernel.  The
# complement is the rows that are still *advisory*: measured, published, and
# auto-``xfail``\\ ed, so a disagreement there is evidence rather than a red
# build.  Epic #5509's acceptance audit left those rows without a stated
# disposition -- a reader could not tell a rule-value difference (the caller
# resolves 0.15 mm where the project says 0.20 mm) from a distance difference
# (the caller's arithmetic disagrees with KiCad at the *same* rule), and the
# two call for opposite responses.  A rule value is Phase 2's resolver axis
# and no migration may close it by editing a number; a distance difference
# either is a retained conservative approximation the caller accepts, or is
# a gap somebody has to own.
#
# The split is made by reading every advisory adapter a second time, at the
# clearance kicad-cli itself applies (``verdicts_at_project_rules``, the
# reading every migrated adapter's merge gate already uses).  Whatever still
# disagrees at that rule is a distance; whatever disagrees only at the
# caller's own rule is a rule value.  Both readings are measured here, by the
# same generator that renders the table, so the published classification
# cannot drift from the numbers it explains.

ADVISORY_GROUPS: frozenset[int] = (
    frozenset(g.number for g in GROUPS) - MIGRATED_GROUPS - set(REFINEMENT_GATED_GROUPS)
)
"""Consumer groups whose adapter-vs-kicad-cli row is still advisory.

Derived, never typed: a group is advisory exactly while it is neither
migrated nor refinement-gated, so moving one into :data:`MIGRATED_GROUPS`
retires its classification here in the same edit rather than leaving a stale
one behind.  Issue #9922 classified six -- 3, 11, 12, 13, 14 and 19.
"""

ADVISORY_TRACE_SEEDS: range = range(0, 3)
"""The seeds whose same-rule disagreements are named pair by pair.

The merge gate's own ``test_corpus.CI_SEEDS``, so every pair the document
names is a pair a test pins.  Only seeds that are also inside the measured
corpus are traced; a report generated over another range says so instead of
launching extra ``kicad-cli`` runs to manufacture a trace.
"""

RULE_VALUE = "rule value"
CONSERVATIVE = "conservative approximation"
RETAINED_GAP = "retained gap"
OUT_OF_SCOPE = "out of scope by construction"

DISPOSITIONS: dict[str, str] = {
    RULE_VALUE: (
        "disagrees at the caller's own rule value and agrees once driven at the "
        "project minimum. Phase 2's resolver axis (#5398 / #5654): a rule value, "
        "not arithmetic."
    ),
    CONSERVATIVE: (
        "rejects a pair kicad-cli finds clean *at the project minimum*. The "
        "arithmetic errs outward; the caller accepts it for the reason recorded "
        "on the row."
    ),
    RETAINED_GAP: (
        "accepts a pair kicad-cli flags *at the project minimum*. The arithmetic "
        "or the model is short of a predicate; the row says which pairs and what "
        "decides them in production."
    ),
    OUT_OF_SCOPE: (
        "answers a narrower question than the corpus asks: the caller consults it "
        "only for some pairs, so its silence on the others is by construction."
    ),
}
"""The closed vocabulary a classification is stated in, with its definition.

Rendered verbatim under the classification table, so a reader never has to
guess what a disposition token means.
"""


@dataclass(frozen=True)
class Classification:
    """One advisory group's authored disposition (Issue #9922).

    The static half of the classification: what no measurement can supply.
    The measured half -- how many pairs, which seeds, which reading -- is
    :class:`SameRuleReading`, produced by the same run that renders the row.

    Attributes:
        group: The Epic #5509 section-1 group number.
        caller: The production path that consults this consumer.
        rule: The rule value the caller resolves, against the project minimum
            kicad-cli applies -- i.e. the two numbers the row compares.
        dispositions: Which :data:`DISPOSITIONS` tokens the row is stated in.
            Checked against the measurement: a token the numbers do not show
            is a classification error, not a style choice.
        retained: What persists at the matched rule and why the caller accepts
            it, written for the reader who has to decide whether to act.
    """

    group: int
    caller: str
    rule: str
    dispositions: frozenset[str]
    retained: str


CLASSIFICATIONS: dict[int, Classification] = {
    3: Classification(
        group=3,
        caller=(
            "the C++ A* acceptance read: `Pathfinder::is_trace_blocked` for a "
            "trace candidate, `Pathfinder::is_via_blocked` for a via"
        ),
        rule=(
            "halo radius from `trace_clearance` 0.15 mm / `via_clearance` 0.20 mm "
            "against the project minimum 0.20 mm"
        ),
        dispositions=frozenset({CONSERVATIVE}),
        retained=(
            "Cell quantisation, and the same quantisation groups 1 and 2 already "
            "carry in `test_corpus.QUANTISATION_LEDGER`: on every gate seed the "
            "pairs this row over-rejects at the matched rule are exactly the "
            "pairs group 2's ledger names, because the read side answers on the "
            "cells the write side marked. Over seeds 0-199 its matched-rule cell "
            "equals group 2's own row, 484 of 930. The caller accepts it because "
            "a cell-quantised model errs outward: the search-time refinement "
            "(groups 4 and 5) re-reads the exact geometry of a marked cell before "
            "refusing it, and at the matched rule neither refinement row flags any "
            "pair named here. The cost is A* expansions, not a rejected legal "
            "route. The clearance supplied to the search and the quantisation of "
            "the cell are recorded apart: only the first moves when the rule "
            "value does, and here nothing moves -- this row was never "
            "under-rejecting."
        ),
    ),
    11: Classification(
        group=11,
        caller=(
            "`Pathfinder.set_via_foreign_context` (`router/core.py`) feeding "
            "`point_clear_of_copper`, `segment_clears_foreign_via`, "
            "`via_clears_foreign_segment` and `drill_hole_to_hole_clear`"
        ),
        rule=(
            "`trace_clearance` 0.15 mm for a candidate trace, `via_clearance` "
            "0.20 mm for a candidate via, against the project minimum 0.20 mm"
        ),
        dispositions=frozenset({RULE_VALUE, CONSERVATIVE, RETAINED_GAP}),
        retained=(
            "Three findings, and only the first is a rule value. **Rule value** "
            "-- `seg-via`: this row drives the trace candidate at the 0.15 mm "
            "trace rule, and every such disagreement over seeds 0-199 (240 of "
            "them) is flagged once the rule is 0.20 mm; `via-via` agrees "
            "outright. What survives the matched rule is entirely `pad-via`, and "
            "it is the pad encoding: a pad handed to the predicate as an "
            "axis-aligned box. **Conservative** -- 10 pairs (0.21-0.25 mm, "
            "kicad-cli clean) are rejected at the matched rule because an "
            "axis-aligned box standing for a rotated pad over-reaches at its "
            "corners. Both callers that pass the 5-tuple share that: "
            "`Pathfinder` with the pad's pre-rotation local size, the escape "
            "router with its rotation-aware box, while the stitcher's disc "
            "accepts all ten. The caller accepts it because a refused legal via "
            "costs search, never a DRC violation. **Retained gap** -- 4 pairs "
            "are accepted at the matched rule, and these are `Pathfinder`'s "
            "alone: it passes the pad's *pre-rotation* local size, so a pad "
            "turned away from the board axes is scored against an unrotated box "
            "that under-reaches toward the via. On seed 1 the 1.2 x 0.8 mm rect "
            "pad at 250.5 degrees measures 0.3248 mm that way where kicad-cli "
            "measures 0.1933 mm; the rotation-aware box and the disc both reject "
            "it. That is the site #9236 fixed for the escape router and did not "
            "reach, so it is a gap rather than a chosen approximation: pinned, "
            "not excused, and tracked in #9923. Not measured as rows of their "
            "own: the `EscapeRouter` / `pad_access` encoding and the stitcher's "
            "disc-bound 4-tuple."
        ),
    ),
    12: Classification(
        group=12,
        caller=(
            "`RoutingGrid.validate_segment_clearance` / `validate_via_clearance` "
            "/ `validate_via_to_via_clearance`, the Python commit gate"
        ),
        rule=(
            "`trace_clearance` 0.15 mm for `seg-seg` and pad pairs, "
            "`max(trace_clearance, via_clearance)` 0.20 mm for `seg-via` "
            "(#6272), against the project minimum 0.20 mm"
        ),
        dispositions=frozenset({RULE_VALUE, RETAINED_GAP}),
        retained=(
            "The `seg-seg` and `pad-seg` under-rejections are rule values and are "
            "flagged at the matched rule. The `pad-via` under-rejection survives "
            "it: this gate has no via-versus-pad predicate at any rule value -- "
            "`validate_via_clearance` walks committed *segments* and "
            "`validate_via_to_via_clearance` committed *vias*; neither consults "
            "the pad list -- so it accepts the pair however loose or tight the "
            "rule. Over seeds 0-199 that is the whole matched-rule residual: 26 "
            "pairs, all `pad-via`. The caller accepts it because the pair is "
            "decided upstream of the commit gate by the pad halo in the "
            "occupancy model (group 1), which flags every `pad-via` pair named "
            "here at the matched rule; the gate alone would commit it, which is "
            "why the pair kind is scored rather than narrowed away. The C++ twin "
            "(group 13) does carry the predicate and flags the same pairs; the "
            "parity gap is tracked in #9924."
        ),
    ),
    13: Classification(
        group=13,
        caller="`Grid3D::validate_route`, through `CppPathfinder._validate_route_clearance`",
        rule=(
            "`trace_clearance` 0.15 mm for `seg-seg` and pad pairs, "
            "`max(trace_clearance, via_clearance)` 0.20 mm for `seg-via` "
            "(#6272), against the project minimum 0.20 mm"
        ),
        dispositions=frozenset({RULE_VALUE}),
        retained=(
            "Nothing. Every disagreement at the caller's own rule value is a "
            "`seg-seg`, `pad-seg` or `pad-via` pair between 0.15 mm and 0.20 mm "
            "-- the kinds `validate_route` judges at the trace rule -- and each "
            "is flagged once that argument is 0.20 mm: over seeds 0-199, 346 of "
            "1294 comparisons move and none survive. The row is advisory, not "
            "gated, only because its kernel migration has not been scheduled: "
            "driven at kicad-cli's own clearance it agrees in both directions on "
            "every traced seed and on the whole corpus. Its pad model keeps "
            "oval and roundrect pads inside a rectangle, which could over-reject "
            "(`roundrect-corner-gap`); over the same corpus at the matched rule "
            "it never does."
        ),
    ),
    14: Classification(
        group=14,
        caller=(
            "`board_pairwise_violations` -> `find_pairwise_violations`, the "
            "post-route replay of `PairwiseClearanceTable.path_is_clear`"
        ),
        rule="the scalar floor `dru` = `trace_clearance` 0.15 mm, against the project minimum 0.20 mm",
        dispositions=frozenset({OUT_OF_SCOPE}),
        retained=(
            "The whole under-rejection cell, at either rule value -- over seeds "
            "0-199, 632 of 1294 comparisons, which is every pair kicad-cli flags "
            "in this row's scope -- and it is a property of the gate's *scope* "
            "rather than of its arithmetic. The pairwise gate is the HV-widening "
            "overlay: it walks a route pair only when `required_clearance(a, b) > "
            "dru` and the copper-vs-pad branch only for a route some pad widens. "
            "With every net at 0 V no pair clears the 30 V threshold, so the "
            "requirement equals the floor for all of them and the loop skips "
            "every pair -- including the ones kicad-cli flags. That is "
            "deliberate: the scalar floor is another gate's question (the commit "
            "gates, groups 12 and 13), and this one reports only what the scalar "
            "path would miss. The measurement is therefore a lower bound on "
            "strictness, never a claim that the gate is wrong. The matched-rule "
            "control in `test_corpus.py` widens one pair's requirement and shows "
            "the same board then reporting it. Owned elsewhere: the HV "
            "domain-matrix oracle, and `Grid3D::pairwise_required_clearance`, "
            "which is dormant until a domain matrix is installed."
        ),
    ),
    19: Classification(
        group=19,
        caller=(
            "`check_pair_clearance_cpp` (`drc/cpp_backend.py`), the placer's "
            "candidate-move check over two whole footprints"
        ),
        rule=(
            "the placer's threshold = `trace_clearance` 0.15 mm, against the "
            "project minimum 0.20 mm"
        ),
        dispositions=frozenset({RULE_VALUE, CONSERVATIVE}),
        retained=(
            "On the corpus, only the rule value. Every pad-pad pair is placed "
            "along the first pad's local +X axis, the one direction where the "
            "consumer's disc of `max(w, h) / 2` is the pad's true support, so at "
            "the matched rule it agrees: over seeds 0-199 all 20 disagreements "
            "(of 42 comparisons) are 0.15-0.20 mm pairs and none survives. The "
            "disc is still an approximation and the corpus cannot see it: "
            "approached off its long axis a rect pad is over-approximated by "
            "`(w - h) / 2`. The named pad-pad control in `test_corpus.py` "
            "(`drc-cpp-disc-off-axis`) demonstrates it at the same rule -- two "
            "1.2 x 0.8 mm rect pads 0.22 mm apart across their short sides are "
            "clean for kicad-cli and rejected here, while the same pads 0.22 mm "
            "apart across their long sides are accepted. The placer accepts the "
            "envelope because it can only refuse a legal candidate, never place "
            "an illegal one."
        ),
    ),
}
"""Issue #9922's disposition for every advisory group.

Exactly :data:`ADVISORY_GROUPS`, asserted by ``test_report_shape``: a group
that becomes advisory with no entry here fails, and so does an entry left
behind by a group that has since been gated.
"""


def _adapter_available(adapter: ConsumerAdapter) -> bool:
    """Whether ``adapter`` can run here (optional protocol member)."""
    probe = getattr(adapter, "available", None)
    return True if probe is None else bool(probe())


def _adapter_pair_kinds(adapter: ConsumerAdapter) -> frozenset[str] | None:
    """The pair kinds ``adapter`` is in scope for, or ``None`` for all of them."""
    kinds = getattr(adapter, "pair_kinds", None)
    return None if kinds is None else frozenset(kinds)


@dataclass(frozen=True)
class CorpusStats:
    """What was actually exercised, for the document's provenance lines."""

    seed_range: str
    case_count: int = 0
    pair_count: int = 0
    boundary_count: int = 0
    flagged_pair_count: int = 0
    fill_states: tuple[str, ...] = ()
    oracle_runs: int = 0
    unmapped_rows: tuple[str, ...] = ()

    @property
    def corpus_cell(self) -> str:
        return f"{self.case_count} cases (seeds {self.seed_range})"


@dataclass(frozen=True)
class AdapterMeasurement:
    """One adapter's disagreement rates against kicad-cli."""

    group: int
    adapter_name: str
    pairs_compared: int = 0
    over_reject: int = 0
    under_reject: int = 0
    boundary: int = 0
    fill_states: tuple[str, ...] = ()

    def _pct(self, count: int) -> str:
        if self.pairs_compared == 0:
            return NOT_MEASURED
        return f"{100.0 * count / self.pairs_compared:.1f}% ({count}/{self.pairs_compared})"

    @property
    def over_reject_cell(self) -> str:
        return self._pct(self.over_reject)

    @property
    def under_reject_cell(self) -> str:
        return self._pct(self.under_reject)


@dataclass(frozen=True)
class SameRuleReading:
    """An advisory adapter read at both rule values (Issue #9922).

    ``own`` is the main table's row -- the consumer at the rule values it
    resolves today.  ``at_project`` is the *same* consumer driven at the
    clearance kicad-cli applies.  Subtracting the second from the first is
    what separates a rule value from a distance: a pair the own-rule reading
    accepts and the matched-rule reading flags disagreed about the number,
    not the geometry.

    Attributes:
        group: The advisory group this reading classifies.
        adapter_name: The adapter's row key.
        own: Disagreements at the consumer's own rule values.
        at_project: Disagreements at kicad-cli's own rule value.
        compared_by_kind: The supported-pair denominator, per pair kind, in
            sorted ``(kind, pairs)`` order.  Published beside every
            percentage because a rate over three pairs and a rate over three
            hundred are not the same claim.
        rule_value_flips: Pairs kicad-cli flags, the own-rule reading accepts
            and the matched-rule reading flags.
        trace: One line per named pair on each :data:`ADVISORY_TRACE_SEED`,
            saying which reading rejected or accepted it.  The exact pairs a
            disposition is stated over, so "retained" can never be read as
            "some pairs, unspecified".
    """

    group: int
    adapter_name: str
    own: AdapterMeasurement
    at_project: AdapterMeasurement
    compared_by_kind: tuple[tuple[str, int], ...]
    rule_value_flips: int = 0
    trace: tuple[str, ...] = ()


def classification_errors(
    dispositions: frozenset[str],
    at_project: AdapterMeasurement,
    rule_value_flips: int,
) -> tuple[str, ...]:
    """Authored disposition tokens the measurement contradicts.

    One direction only, deliberately.  A measured disagreement at the matched
    rule *must* be classified as arithmetic (it cannot be a rule value once
    the two rule values are equal), and a measured rule-value flip *must* be
    classified as one.  The reverse -- a token with nothing behind it -- is
    not an error here, because a corpus cannot show every disposition: group
    19's conservative disc only appears off the axis the corpus probes, and
    that evidence is the named control in ``test_corpus.py``.
    """
    errors: list[str] = []
    if at_project.over_reject and CONSERVATIVE not in dispositions:
        errors.append(
            f"over-rejects {at_project.over_reject} pair(s) at the project minimum "
            f"but is not classified {CONSERVATIVE!r}"
        )
    if at_project.under_reject and not dispositions & {RETAINED_GAP, OUT_OF_SCOPE}:
        errors.append(
            f"under-rejects {at_project.under_reject} pair(s) at the project minimum "
            f"but is classified {sorted(dispositions)!r}, none of which is arithmetic"
        )
    if rule_value_flips and RULE_VALUE not in dispositions:
        errors.append(
            f"has {rule_value_flips} rule-value flip(s) but is not classified {RULE_VALUE!r}"
        )
    return tuple(errors)


TABLE_COLUMNS: tuple[str, ...] = (
    "adapter",
    "corpus (seed range)",
    "over-reject %",
    "under-reject %",
    "boundary",
    "fill state",
    "notes",
)
"""The table's columns, in order.

The ``notes`` column is what makes the "every row is measured *or* states its
reason" criterion checkable rather than aspirational: it carries
:data:`NOT_MEASURED_REASONS` for an unmeasured group and :data:`NOTES` -- the
sub-entry-points a row does not cover -- for a measured one.
"""

CLASSIFICATION_COLUMNS: tuple[str, ...] = (
    "group · adapter",
    "production caller",
    "pair kinds (compared)",
    "caller rule → project minimum",
    "own-rule over / under",
    "matched-rule over / under",
    "disposition",
)
"""The advisory classification table's columns, in order (Issue #9922).

Each column answers one question the acceptance audit left open per group:
who calls it, what it can decide, which two numbers are being compared, how
it disagrees at each of them, and what the residual is called.
"""


def adapter_for_group(number: int) -> ConsumerAdapter | None:
    """The adapter registered for a group, if any."""
    for adapter in ADAPTERS:
        if adapter.group == number:
            return adapter
    return None


def not_measured_reason(number: int) -> str:
    """Why group ``number`` has no measurement, always a stated reason.

    Three cases, in order.  A group with **no registered adapter** must carry
    an explicit :data:`NOT_MEASURED_REASONS` entry; that is a static
    invariant, asserted by
    ``test_report_shape.test_every_unwired_group_states_its_reason`` rather
    than only at render time, so the gap is caught by a unit test on a laptop
    with no KiCad installed.  A group that *has* an adapter but is still
    unmeasured was skipped at runtime because ``available()`` said no -- a
    machine fact, not a coverage decision, and reported as such.  The final
    fallback exists only so rendering can never crash mid-document; reaching
    it means the invariant test is missing a case.
    """
    explicit = NOT_MEASURED_REASONS.get(number)
    if explicit is not None:
        return explicit
    adapter = adapter_for_group(number)
    if adapter is not None:
        return (
            f"adapter `{adapter.name}` is wired but could not run on the "
            "machine that generated this document (most often: the compiled "
            "extension is not built -- `uv run kct build-native`). A skipped "
            "adapter deliberately renders `not measured` rather than a "
            "zero-disagreement row."
        )
    return "no adapter registered and no reason recorded -- this is a harness bug."


def kicad_cli_version() -> str:
    """The kicad-cli release the oracle resolved, or ``not measured``.

    Read from the discovery probe :func:`find_kicad_cli` has already made, so
    asking costs no process.  The corpus percentages and the classification
    below are only meaningful against the release that produced them -- the
    acceptance audit listed the missing version as a gap -- so the
    classification states it.  It is a *toolchain* stamp, not a generating
    commit: the same pinned image answers the same on every regeneration,
    which is what keeps the document byte-identical.
    """
    from kicad_tools.cli import runner

    path = runner.find_kicad_cli()
    if path is None:
        return NOT_MEASURED
    return runner.PROBED_KICAD_CLI_VERSIONS.get(str(path)) or NOT_MEASURED


def _row_note(number: int) -> str:
    """The ``notes`` cell for a *measured* row, migration status included.

    A migrated group's row is no longer report-only, and a reader of the table
    must be able to tell that from the table -- otherwise a non-zero cell on a
    gated row reads like every other outstanding finding.  The prefix also
    records *which* reading is gated, because for a migrated group the two are
    deliberately different: the percentages here come from the consumer's own
    rule values (so the row keeps measuring the rule gap for its own issue),
    while the merge gate drives the same consumer at the clearance kicad-cli
    applies, isolating the geometry this epic actually unified.
    """
    note = NOTES.get(number, "")
    if number in MIGRATED_GROUPS:
        fails_on = (
            "so it fails on any **under**-rejection and on any over-rejection its "
            "pinned cell-quantisation ledger (`test_corpus.QUANTISATION_LEDGER`) "
            "does not record"
            if number in QUANTISED_GROUPS
            else "so it fails on a **geometry** disagreement in either direction"
        )
        prefix = (
            f"**Switched to the shared kernel in Phase {_MIGRATION_PHASE[number]} -- "
            "no longer report-only.** The merge gate "
            "(`test_corpus.test_adapter_agrees_with_kicad_cli`) drives this same "
            "consumer at the project netclass clearance kicad-cli itself applies, "
            f"{fails_on}; the percentages in this row keep the consumer's own rule "
            "values."
        )
        return f"{prefix} {note}".strip()
    if number in ADVISORY_GROUPS:
        # Derived, not typed into NOTES: a group that becomes advisory gets
        # its pointer in the same edit, and one that is gated loses it.
        suffix = (
            "**Still advisory (report-only).** Classified at a matched rule in "
            "the advisory section below (Issue #9922): the production caller, the "
            "pair kinds, both rule values and the named pairs."
        )
        return f"{note} {suffix}".strip()
    return note


def _escape_cell(text: str) -> str:
    """Make a note safe inside a markdown table cell.

    Only ``|`` needs handling: it would end the cell.  Newlines cannot occur
    (every note is a single string literal), and asserting that keeps a future
    multi-line note from silently corrupting the table.
    """
    assert "\n" not in text, f"table notes must be single-line: {text!r}"
    return text.replace("|", "\\|")


def _rows(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement],
) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for group in GROUPS:
        m = measurements.get(group.number)
        if m is None:
            reason = not_measured_reason(group.number)
            rows.append(
                (
                    f"{group.number}. {group.label} — *(no adapter)*",
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
                    NOT_MEASURED,
                    _escape_cell(reason),
                )
            )
            continue
        rows.append(
            (
                f"{group.number}. {group.label} — `{m.adapter_name}`",
                stats.corpus_cell,
                m.over_reject_cell,
                m.under_reject_cell,
                str(m.boundary),
                ", ".join(m.fill_states) or NOT_MEASURED,
                _escape_cell(_row_note(group.number)),
            )
        )
    return rows


def _kernel_row(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement],
) -> tuple[str, ...] | None:
    """The Phase 1b kernel's row, or ``None`` when it was not measured."""
    m = measurements.get(KERNEL_GROUP)
    if m is None:
        return None
    return (
        f"Phase 1b clearance kernel — `{m.adapter_name}`",
        stats.corpus_cell,
        m.over_reject_cell,
        m.under_reject_cell,
        str(m.boundary),
        ", ".join(m.fill_states) or NOT_MEASURED,
        _escape_cell(NOTES[KERNEL_GROUP]),
    )


def _markdown_table(
    rows: list[tuple[str, ...]],
    columns: tuple[str, ...] = TABLE_COLUMNS,
) -> list[str]:
    header = "| " + " | ".join(columns) + " |"
    sep = "|" + "---|" * len(columns)
    body = ["| " + " | ".join(row) + " |" for row in rows]
    for row in rows:
        assert len(row) == len(columns), f"row has {len(row)} cells: {row}"
    return [header, sep, *body]


def render_table(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement] | None = None,
) -> str:
    """Render the 19-row consumer-group table (delimited by begin/end markers).

    The kernel is deliberately **not** in here -- it is not one of the epic's
    nineteen consumer groups, and mixing it in would make the row count stop
    being a checkable invariant.  :func:`render_kernel_table` renders it.

    Args:
        stats: Corpus provenance for the measured rows.
        measurements: Per-group adapter results; groups absent from the map
            render as ``not measured`` plus their stated reason.

    Returns:
        Markdown, including the ``TABLE_BEGIN`` / ``TABLE_END`` markers that
        let the doc test count rows without parsing prose.
    """
    measurements = measurements or {}
    rows = _rows(stats, measurements)
    assert len(rows) == len(GROUPS)
    return "\n".join([TABLE_BEGIN, *_markdown_table(rows), TABLE_END])


def render_kernel_table(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement] | None = None,
) -> str:
    """Render the kernel's one-row table, or a ``not measured`` note."""
    row = _kernel_row(stats, measurements or {})
    if row is None:
        return (
            f"The kernel row is `{NOT_MEASURED}`: its adapter could not run on "
            "the machine that generated this document."
        )
    return "\n".join([KERNEL_TABLE_BEGIN, *_markdown_table([row]), KERNEL_TABLE_END])


def _count_cell(count: int, total: int) -> str:
    """``count/total``, or ``not measured`` over an empty denominator.

    A bare ``0`` next to nothing reads as agreement; ``0/0`` reads as what it
    is.  Group 19 is the case that needs this -- its pair kind is rare enough
    that a short seed range places none.
    """
    return NOT_MEASURED if total == 0 else f"{count}/{total}"


def _advisory_row(number: int, reading: SameRuleReading | None) -> tuple[str, ...]:
    group = next(g for g in GROUPS if g.number == number)
    classification = CLASSIFICATIONS[number]
    label = f"{number}. {group.label}"
    if reading is None:
        return (
            f"{label} — {NOT_MEASURED}",
            _escape_cell(classification.caller),
            NOT_MEASURED,
            _escape_cell(classification.rule),
            NOT_MEASURED,
            NOT_MEASURED,
            _escape_cell(not_measured_reason(number)),
        )
    own, at_project = reading.own, reading.at_project
    # A kind nobody compared is not a denominator: drop it, so an empty
    # corpus reads ``not measured`` rather than a column of zeros.
    kinds = (
        ", ".join(f"`{kind}` {pairs}" for kind, pairs in reading.compared_by_kind if pairs)
        or NOT_MEASURED
    )
    return (
        f"{label} — `{reading.adapter_name}`",
        _escape_cell(classification.caller),
        kinds,
        _escape_cell(classification.rule),
        f"{_count_cell(own.over_reject, own.pairs_compared)} / "
        f"{_count_cell(own.under_reject, own.pairs_compared)}",
        f"{_count_cell(at_project.over_reject, at_project.pairs_compared)} / "
        f"{_count_cell(at_project.under_reject, at_project.pairs_compared)}",
        _escape_cell(_ordered_dispositions(classification.dispositions)),
    )


def _ordered_dispositions(dispositions: frozenset[str]) -> str:
    """Disposition tokens in :data:`DISPOSITIONS` order -- rule value first.

    Alphabetical order happens to put the arithmetic tokens before the rule
    value, which reads as the more important finding when it is the less
    actionable one.
    """
    return ", ".join(token for token in DISPOSITIONS if token in dispositions)


def render_advisory_table(advisory: dict[int, SameRuleReading] | None = None) -> str:
    """Render one classification row per advisory group (Issue #9922).

    Delimited by :data:`CLASSIFICATION_TABLE_BEGIN` / ``_END`` so a test can
    count rows without parsing prose, exactly as the group table does.  A
    group that is advisory but was not measured on this machine keeps its
    row with the authored half stated and the measured half reading ``not
    measured`` -- never an omitted row, which would read as "agrees".

    Raises:
        AssertionError: If a measured reading contradicts the group's authored
            disposition.  The document must not publish a classification its
            own numbers refute.
    """
    advisory = advisory or {}
    rows: list[tuple[str, ...]] = []
    for group in GROUPS:
        if group.number not in ADVISORY_GROUPS:
            continue
        reading = advisory.get(group.number)
        if reading is not None:
            errors = classification_errors(
                CLASSIFICATIONS[group.number].dispositions,
                reading.at_project,
                reading.rule_value_flips,
            )
            assert not errors, (
                f"group {group.number}'s classification is contradicted by its "
                f"own measurement: {errors}"
            )
        rows.append(_advisory_row(group.number, reading))
    assert len(rows) == len(ADVISORY_GROUPS)
    return "\n".join(
        [
            CLASSIFICATION_TABLE_BEGIN,
            *_markdown_table(rows, CLASSIFICATION_COLUMNS),
            CLASSIFICATION_TABLE_END,
        ]
    )


def render_advisory_dispositions(advisory: dict[int, SameRuleReading] | None = None) -> list[str]:
    """The per-group prose under the classification table, with the named pairs.

    The ``retained`` text says *why* the caller accepts what persists; the
    trace lines say *which* pairs, so a reader can reproduce every one from
    its seed.
    """
    advisory = advisory or {}
    lines: list[str] = []
    for group in GROUPS:
        if group.number not in ADVISORY_GROUPS:
            continue
        classification = CLASSIFICATIONS[group.number]
        lines.append(f"- **{group.number}. {group.label}** -- {classification.retained}")
        reading = advisory.get(group.number)
        if reading is None:
            lines.append(f"  - trace: {NOT_MEASURED}")
            continue
        for entry in reading.trace:
            lines.append(f"  - {entry}")
    return lines


def _traced_seeds(stats: CorpusStats) -> list[int]:
    """Which :data:`ADVISORY_TRACE_SEEDS` the measured corpus contains."""
    first, last = (int(part) for part in stats.seed_range.split("-"))
    return [seed for seed in ADVISORY_TRACE_SEEDS if first <= seed <= last]


def _advisory_section(
    stats: CorpusStats,
    advisory: dict[int, SameRuleReading],
    trace_seeds: list[int],
) -> list[str]:
    """The classification section: provenance, table, vocabulary, named pairs."""
    groups = sorted(ADVISORY_GROUPS)
    if not groups:
        return []
    if trace_seeds:
        trace_text = (
            f"seeds {', '.join(str(s) for s in trace_seeds)} -- the merge gate's "
            "own `test_corpus.CI_SEEDS`, so every pair named below is a pair a "
            "test pins"
        )
    else:
        trace_text = (
            f"`{NOT_MEASURED}` -- none of seeds {ADVISORY_TRACE_SEEDS.start}-"
            f"{ADVISORY_TRACE_SEEDS.stop - 1} is inside the measured range"
        )
    return [
        "## Advisory groups at a matched rule",
        "",
        f"Issue #9922 (Epic #5509 acceptance items E1 and C3). {len(groups)} of "
        f"the {len(GROUPS)} rows above are still advisory -- groups "
        f"{', '.join(str(g) for g in groups)} -- and a disagreement there is "
        "evidence rather than a red build. Their percentages alone cannot say "
        "**what kind** of disagreement they are: a consumer that resolves "
        "0.15 mm where the project says 0.20 mm disagrees with kicad-cli "
        "without a single wrong distance, and a consumer whose arithmetic is "
        "short disagrees at the same rule. The first is Phase 2's resolver "
        "axis and no migration may close it by editing a number; the second "
        "either is a retained approximation the caller accepts, or is a gap "
        "somebody has to own.",
        "",
        "So every advisory adapter is read twice: at the caller's own rule "
        "values (the row above) and at the project netclass minimum kicad-cli "
        "itself applies. Whatever still disagrees at the matched rule is "
        "arithmetic; whatever disagrees only at the caller's rule is a rule "
        "value. Each row states the production caller, the pair kinds it is "
        "actually consulted for, the two rule values being compared, and its "
        "disposition.",
        "",
        f"- kicad-cli: `{kicad_cli_version()}`",
        f"- Fill states: {', '.join(stats.fill_states) or NOT_MEASURED}",
        f"- Seed range: `{stats.seed_range}`; counts are pairs compared over both fill states",
        f"- Named pairs: {trace_text}",
        "- Provenance of the prose: prevalence quoted under the table was measured "
        "for Issue #9922 at `efafdb1f4` on kicad-cli 10.0.6. The table itself is "
        "re-measured on every regeneration.",
        "",
        render_advisory_table(advisory),
        "",
        "**How to read ``disposition``:**",
        "",
        *[f"- `{token}` -- {definition}" for token, definition in DISPOSITIONS.items()],
        "",
        "**What persists, and why the caller accepts it:**",
        "",
        *render_advisory_dispositions(advisory),
        "",
    ]


def render_document(
    stats: CorpusStats,
    measurements: dict[int, AdapterMeasurement] | None = None,
    advisory: dict[int, SameRuleReading] | None = None,
) -> str:
    """Render the full ``docs/clearance-conformance.md`` body."""
    measurements = measurements or {}
    advisory = advisory or {}
    measured = sorted(g for g in measurements if g != KERNEL_GROUP)
    unmeasured = [g.number for g in GROUPS if g.number not in measurements]
    trace_seeds = _traced_seeds(stats)

    lines: list[str] = [
        "# Clearance conformance: kicad-cli vs. in-tree consumers",
        "",
        "<!-- Generated by `uv run python -m tests.conformance.report`. Do not edit by hand. -->",
        "",
        "Epic [#5509](https://github.com/2AMLogic/kicad-tools/issues/5509) "
        "(*one clearance kernel*) starts from a measurement, not a refactor: "
        "**how far does each of our clearance models sit from "
        "`kicad-cli pcb drc`?** This document is that measurement.",
        "",
        "`kicad-cli` is ground truth here. Every row below compares one "
        "in-tree consumer's answer -- *is this pair of copper objects too "
        "close?* -- against KiCad's answer on the same board, for a corpus of "
        "seeded random copper configurations plus four named fixtures "
        "reproducing known disagreements.",
        "",
        "**A row is report-only until its consumer is switched.** A "
        "disagreement on an unswitched row is evidence, not a bug report and "
        "not a patch: consumers move onto the shared kernel in their own epic "
        "phase, and only then does that row become a merge gate.",
        "",
        *(
            [
                "Already switched, and therefore **gated** rather than "
                "report-only: "
                + ", ".join(
                    f"group {n} (Phase {_MIGRATION_PHASE[n]})" for n in sorted(MIGRATED_GROUPS)
                )
                + ". A gated row's merge gate drives that consumer at the "
                "project netclass clearance kicad-cli itself applies, so it "
                "fails on a **geometry** disagreement and not on the rule "
                "value the consumer happens to resolve -- rule selection is "
                "Phase 2's axis, and the percentages below deliberately keep "
                "measuring it.",
                "",
            ]
            if MIGRATED_GROUPS
            else []
        ),
        *(
            [
                "Still **advisory** (report-only): "
                + ", ".join(f"group {n}" for n in sorted(ADVISORY_GROUPS))
                + ". Their disagreements are classified in the advisory section "
                "below -- each as a rule value, a retained conservative "
                "approximation, a retained gap, or out of scope -- by reading "
                "the same consumer a second time at kicad-cli's own rule value "
                "(Issue #9922).",
                "",
            ]
            if ADVISORY_GROUPS
            else []
        ),
        *(
            [
                "**Gated without having been migrated**: "
                + ", ".join(
                    f"group {n} ({REFINEMENT_GATED_GROUPS[n]})"
                    for n in sorted(REFINEMENT_GATED_GROUPS)
                )
                + ". Being on the shared kernel and being allowed to disagree "
                "are two different questions, and the search-time route-halo "
                "refinement is the case where they come apart: its "
                "over-rejection -- a legal candidate refused because a "
                "conservative halo swallowed it -- was driven to zero by "
                "#5410, and driven at kicad-cli's own clearance it now agrees "
                "in **both** directions, but it still carries its own "
                "arithmetic and its kernel switch is Phase 3b (#5661). "
                "`tests/conformance/test_route_halo_refinement_gate.py` holds "
                "that agreement as a hard failure so a regression reddens the "
                "build instead of quietly becoming a number in this table; "
                "the percentages below still keep the consumer's own rule "
                "values, so the rule axis stays measured rather than gated.",
                "",
            ]
            if REFINEMENT_GATED_GROUPS
            else []
        ),
        "## Status",
        "",
        *(
            [
                "Phase 1a has landed the **truth side** of the harness: the "
                "generator, the board writer, the kicad-cli oracle, the four "
                "named fixtures, and this document's shape. No adapter wraps "
                "a consumer yet, so every group row below reads "
                f"`{NOT_MEASURED}` -- that is the expected state for this "
                "phase, not a gap in the measurement. The corpus figures "
                "further down are real: the whole pipeline ran, it just had "
                "nothing to compare kicad-cli against.",
                "",
                "The adapter phase populates `ADAPTERS` in "
                "`tests/conformance/report.py` and regenerates this table; "
                "nothing else about the document changes.",
            ]
            if not measured
            else [
                f"{len(measured)} of {len(GROUPS)} consumer groups are wrapped "
                "by an adapter and measured below "
                f"(groups {', '.join(str(g) for g in measured)}). The rest "
                f"read `{NOT_MEASURED}`.",
                "",
                "Each adapter drives an **unmodified** consumer: no rule "
                "value, heuristic or consumer was changed to produce these "
                "numbers. Two things shape the denominators and are worth "
                "reading before the percentages. First, a consumer is only "
                "compared on the pair kinds it is actually consulted for in "
                "production (`ConsumerAdapter.pair_kinds`) -- the "
                "route-halo refinement, for instance, never sees pad copper, "
                "so pad pairs are out of its scope rather than counted "
                "against it. Second, the commit gates are measured under one "
                "named insertion order, **via-first**; #5398 is the "
                "observation that the other order gives a different answer "
                "for the same two objects, and the "
                "`issue5398-seg-via-0p18-order` fixture is where that is "
                "recorded.",
                "",
                "Every group that is still `not measured` carries its reason "
                "in the `notes` column, and so does every group that *is* "
                "measured but whose entry-point list this phase could not "
                'cover in full. A bare "not measured" reads the same as '
                '"nobody looked"; naming the gap is what keeps the next phase '
                "honest about what it inherits.",
            ]
        ),
        "",
        "## How to read the table",
        "",
        "- **over-reject** -- the consumer rejects a pair kicad-cli finds "
        "clean. This is the failure mode that makes legal candidates look "
        "unroutable and burns A* expansions (#5410).",
        "- **under-reject** -- the consumer accepts a pair kicad-cli flags. "
        'This is the failure mode that makes a route "complete" and then '
        "fail the final DRC gate (#5398, #3803).",
        "- **boundary** -- pairs whose gap is within "
        f"{BOUNDARY_BAND_MM * 1000:.0f} um of the requirement. Counted "
        "separately and never as a disagreement: at that distance the two "
        "models are arguing about rounding, not about geometry (kct's "
        "`CLEARANCE_EPSILON_MM` is 1e-4 mm; KiCad works in integer "
        "nanometres).",
        "- **fill state** -- whether zones were refilled "
        "(`kicad-cli pcb drc --refill-zones`) before measuring. Zone verdicts "
        "are only meaningful against fresh fills, so they are dropped from "
        "unrefilled runs.",
        "- **not measured** -- no adapter wraps that consumer yet. The row is "
        'kept rather than omitted: an omitted row reads as "agrees", and we '
        "do not know that. The `notes` column says *why* in every case.",
        "- **notes** -- for a measured row, the sub-entry-points the row does "
        "**not** cover, so the percentage cannot imply more coverage than it "
        "has. For an unmeasured row, the reason.",
        "",
        "## Conformance by consumer group",
        "",
        f"Groups are Epic #5509 section 1's inventory of implementations that "
        f"carry their own clearance arithmetic ({len(GROUPS)} of them, across "
        "two native extensions and Python).",
        "",
        render_table(stats, measurements),
        "",
        "## The kernel, measured as a consumer",
        "",
        "The row the rest of the table is *for*. Epic #5509's plan is to move "
        "every consumer above onto one exact-geometry kernel "
        "(`router/clearance_kernel.py` plus its `router_cpp` twin, Phase 1b), "
        "and that plan is only sound if the kernel itself can agree with "
        "`kicad-cli` everywhere. So the kernel is measured by the same harness, "
        "on the same corpus, and its acceptance criterion is a hard "
        "**0 % / 0 %**.",
        "",
        "It is not one of the nineteen groups -- it is the model they are to be "
        "unified onto -- which is why it sits in its own table. It is also the "
        "only row whose `required_mm` comes from the `.kicad_pro` netclass "
        "rather than from the consumer's own rule values: the kernel carries no "
        "rule values at all (Phase 2's resolver owns that), so its row isolates "
        "the **geometry** question from the **rule-resolution** question. A "
        "non-zero cell here is a Phase 1b bug -- capture a fixture and fix it "
        "through `tests/router/test_clearance_kernel_parity.py`, never by "
        "loosening this measurement.",
        "",
        render_kernel_table(stats, measurements),
        "",
        *_advisory_section(stats, advisory, trace_seeds),
        "## Corpus",
        "",
        f"- Seed range: `{stats.seed_range}` ({stats.case_count} generated cases)",
        f"- Analytic close pairs placed: {stats.pair_count}",
        f"- Pairs kicad-cli flagged: {stats.flagged_pair_count}",
        f"- Pairs in the boundary band: {stats.boundary_count}",
        f"- Fill states measured: {', '.join(stats.fill_states) or NOT_MEASURED}",
        f"- kicad-cli invocations: {stats.oracle_runs}",
        "",
        "Cases are generated by `tests/conformance/generator.py` with "
        "`random.Random(seed)` -- no `hypothesis` dependency. Each case places "
        "2-6 segments, 1-3 vias and up to 2 pads (rect / circle / oval / "
        "roundrect, rotated), optionally one zone, with **every object on its "
        "own net** so a kicad-cli finding maps onto a pair by net alone. Close "
        "pairs are placed *analytically* at a gap drawn near the requirement, "
        "rather than by sampling random positions and hoping some land near "
        "the threshold -- otherwise the corpus would almost never probe the "
        "boundary and every rate below would be vacuously zero.",
        "",
        "Nine pair kinds are placed: `seg-seg`, `seg-via`, `via-via`, "
        "`pad-seg`, `pad-via`, `pad-pad`, `seg-zone`, `via-zone` and "
        "`copper-edge`. The first five each have a *routing candidate* -- a "
        "segment or a via a router could propose -- and are what the "
        "router-side rows are scored on. `pad-pad` has none (both sides are "
        "placement copper) and exists for group 19, the incremental placement "
        "DRC, whose entry point takes two whole footprints and can answer "
        "nothing else.",
        "",
        "The last three exist for the board features a consumer routes "
        "*around* rather than proposes. `seg-zone` / `via-zone` place copper "
        "at a known gap outside the pour boundary and are scored **only on "
        "the refilled run**: an unfilled pour contributes no copper to KiCad "
        "at all, so a zone verdict from an unrefilled run would measure the "
        "fill state instead of the clearance model. `copper-edge` places a "
        "track at a known gap inside `Edge.Cuts` and pairs it with the "
        "`<board-edge>` pseudo-net, the same key the oracle gives a one-sided "
        "`copper_edge_clearance` row.",
        "",
        "**Zone pairs are drawn above the requirement, and that is KiCad's "
        "property rather than the generator's choice.** A freshly refilled "
        "pour is backed off from every piece of foreign copper by the applied "
        "clearance (measured at 0.2005 mm for a 0.20 mm netclass on "
        "kicad-cli 10, independent of the zone's own local clearance value), "
        "so fill copper can never be closer than the requirement and a "
        "sub-threshold zone pair does not exist on a refilled board. "
        "Manufacturing one would need a stale fill, which measures the fill. "
        "Zone rows can therefore show over-rejection and cannot show "
        "under-rejection; `copper-edge` has no such restriction and probes "
        "both sides.",
        "",
        "This document regenerates byte-identically from its seed range: "
        "`uv run python -m tests.conformance.report --seeds "
        f"{stats.seed_range} --out docs/clearance-conformance.md`. A "
        "generating-commit SHA is deliberately **not** stamped into the "
        "header -- it would make byte-identical regeneration impossible by "
        "construction, and the seed range plus the pinned generator is what "
        "actually makes the numbers reproducible.",
        "",
        "## Named fixtures",
        "",
        "Committed under `tests/fixtures/conformance/` as "
        "`<name>-seed<N>.kicad_pcb` + `.kicad_pro`. Each reproduces a "
        "*specific* observed disagreement; `test_named_fixtures.py` asserts "
        "kicad-cli's verdict on each one.",
        "",
        "| fixture | kicad-cli verdict | expected consumer disagreement |",
        "|---|---|---|",
        "| `issue5398-seg-via-0p18-order` | one `clearance` violation "
        "(required 0.20 mm, actual 0.180 mm) | Python/C++ commit gates "
        "**accept** when the via is inserted first, **reject** when the "
        "segment is (#5398) |",
        "| `issue5410-dqs-n-halo-vs-legal-via` | clean (0.213 mm copper, "
        "0.513 mm drill vs 0.20 / 0.50) | grid occupancy **rejects**. Since "
        "#5660 DQS_N's own halo no longer covers the DQ3 cell (Euclidean "
        "6.40 cells against a six-cell disc, where the Chebyshev square "
        "covered it at its corner); this row still rejects because the "
        "adapter grows DQ3's halo too and the two discs touch (#5410) |",
        "| `search-vs-commit-seg-via-max` | clean (project `Default` class "
        "0.15 mm) | route-halo geometry used to **reject** via "
        "`max(required, via_clearance)` while the commit gates **accept**; "
        "since Phase 3b (#5661) both resolve the same 0.15 mm through the "
        "shared kernel and **accept**, and this fixture is the guard that a "
        "consumer does not re-derive its own `max(...)` |",
        "| `roundrect-corner-gap` | clean (0.22 mm to the exact outline) | "
        "the router's rectangle-bounded pad model **rejects** (0.1164 mm to "
        "the bounding rectangle); `kct check`'s polygon model **accepts** |",
        "",
        "## Scope and known gaps",
        "",
        f"- Adapters wired: {', '.join(str(g) for g in measured) or 'none yet'}.",
        f"- Groups still `not measured`: {', '.join(str(g) for g in unmeasured) or 'none'}.",
        "- Same-net drill spacing (`validate_same_net_drill_spacing`) is out "
        "of scope: pair identity here is by net, so a same-net finding has no "
        "two-net key. It needs its own harness.",
        "- Verdict identity is `(kind, {net_a, net_b})` only. Gaps and "
        "required values are recorded for this table but never compared "
        "across models -- each model computes its own gap, and comparing "
        "those would measure arithmetic noise instead of the question under "
        "test (*did this model flag this pair at all?*).",
        "- `shorting_items` rows are dropped: that is a connectivity "
        "conclusion about copper that already overlaps, not a clearance "
        "measurement, and no adapter models it.",
        "",
    ]
    if stats.unmapped_rows:
        lines.extend(
            [
                "### Unmapped kicad-cli rows",
                "",
                "Clearance-family findings that could not be reduced to a net "
                "pair. These indicate a harness gap, not a consumer "
                "disagreement:",
                "",
                *[f"- `{row}`" for row in stats.unmapped_rows],
                "",
            ]
        )
    return "\n".join(lines)


def _case_pair_stats(case: CopperCase) -> tuple[int, int]:
    return len(case.pairs), len(case.boundary_pairs)


def measure_corpus(
    seeds: range,
    *,
    fill_states: tuple[bool, ...] = (False, True),
    work_dir: Path | None = None,
) -> tuple[CorpusStats, dict[int, AdapterMeasurement], dict[int, SameRuleReading]]:
    """Generate, write and measure the corpus; return stats + adapter results.

    The third value is each advisory group read at both rule values (Issue
    #9922), for the classification section; it is empty when every group is
    gated.  It reuses the oracle runs the table already paid for.

    With no adapters registered (Phase 1a) this still does real work: it
    exercises the generator, the board writer and the oracle across the whole
    seed range, so the document's corpus provenance is measured rather than
    asserted.

    Raises:
        KiCadCliAbsent: When kicad-cli is not installed.
    """
    if not kicad_cli_available():
        raise KiCadCliAbsent()

    owns_tmp = work_dir is None
    tmp = tempfile.TemporaryDirectory(prefix="conformance_report_") if owns_tmp else None
    directory = Path(tmp.name) if tmp is not None else Path(work_dir)  # type: ignore[arg-type]

    try:
        cases = generate_corpus(seeds)
        pair_count = 0
        boundary_count = 0
        flagged = 0
        runs = 0
        unmapped: list[str] = []
        results: list[tuple[CopperCase, OracleResult]] = []

        for case in cases:
            pairs, boundary = _case_pair_stats(case)
            pair_count += pairs
            boundary_count += boundary
            board = write_case(case, directory)
            for refill in fill_states:
                result = run_oracle(board.pcb_path, refill=refill, work_dir=directory)
                runs += 1
                unmapped.extend(result.dropped)
                results.append((case, result))
                if not refill:
                    flagged += len(result.without_zones())

        state_names = tuple("refilled" if refill else "as-is" for refill in fill_states)
        stats = CorpusStats(
            seed_range=f"{seeds.start}-{seeds.stop - 1}",
            case_count=len(cases),
            pair_count=pair_count,
            boundary_count=boundary_count,
            flagged_pair_count=flagged,
            fill_states=state_names,
            oracle_runs=runs,
            unmapped_rows=tuple(dict.fromkeys(unmapped)),
        )

        measurements: dict[int, AdapterMeasurement] = {}
        for adapter in ADAPTERS:
            if not _adapter_available(adapter):
                # No row rather than a zero-disagreement row: the same rule the
                # oracle applies to a missing kicad-cli.  A group whose adapter
                # could not run renders ``not measured``.
                continue
            kinds = _adapter_pair_kinds(adapter)
            over = under = boundary_hits = 0
            compared = 0
            # An adapter never sees zone fill, so its answer is the same at
            # both fill states; measure once per case and reuse it for each.
            verdict_cache: dict[str, set[frozenset[str]]] = {}
            for case, result in results:
                consumer = verdict_cache.get(case.name)
                if consumer is None:
                    consumer = {v.nets for v in adapter.verdicts(case)}
                    verdict_cache[case.name] = consumer
                found, compared_pairs, n_boundary = _score(case, result, consumer, kinds)
                compared += len(compared_pairs)
                boundary_hits += n_boundary
                over += sum(1 for _, direction in found if direction == "over")
                under += sum(1 for _, direction in found if direction == "under")
            measurements[adapter.group] = AdapterMeasurement(
                group=adapter.group,
                adapter_name=adapter.name,
                pairs_compared=compared,
                over_reject=over,
                under_reject=under,
                boundary=boundary_hits,
                fill_states=state_names,
            )

        advisory: dict[int, SameRuleReading] = {}
        for group in sorted(ADVISORY_GROUPS):
            if group in measurements:
                adapter = adapter_for_group(group)
                assert adapter is not None  # a measured group has an adapter by construction
                advisory[group] = _same_rule_reading(adapter, results, state_names)
        return stats, measurements, advisory
    finally:
        if tmp is not None:
            tmp.cleanup()


def _score(
    case: CopperCase,
    result: OracleResult,
    consumer: set[frozenset[str]],
    kinds: frozenset[str] | None,
) -> tuple[list[tuple[PairIntent, str]], list[PairIntent], int]:
    """Score one reading of ``case`` against one oracle run.

    Returns ``(disagreements, compared, boundary_hits)``.  A disagreement is
    ``(pair, "over" | "under")``; ``compared`` is every pair that was
    compared, which is the supported-pair denominator the classification
    publishes per kind.

    This is the one place the comparison's scope rule lives, so the table's
    own-rule reading and the classification's matched-rule reading cannot
    drift apart on which pairs count.  The order of the exclusions is
    load-bearing: a zone pair on an unrefilled run is not comparable at all,
    so it is dropped before the boundary band could claim it.
    """
    truth = {v.nets for v in result.without_zones()}
    # Zone verdicts are only meaningful against a fresh fill, so they are
    # read from the refilled run and only from there (#5644).  An unfilled
    # pour contributes no copper at all to kicad-cli, so scoring a zone pair
    # on the as-is run would measure the fill state rather than the
    # clearance model.
    zone_truth = {v.nets for v in result.verdicts if v.zone} if result.refill else set()
    found: list[tuple[PairIntent, str]] = []
    compared: list[PairIntent] = []
    boundary_hits = 0
    for pair in case.pairs:
        if kinds is not None and pair.kind not in kinds:
            # Out of this consumer's declared scope -- see
            # ``ConsumerAdapter.pair_kinds``.  Counting it would record the
            # absence of a check elsewhere in the pipeline, not a
            # disagreement in this model.
            continue
        is_zone_pair = pair.kind in PairKind.ZONE
        if is_zone_pair and not result.refill:
            continue
        if pair.boundary:
            boundary_hits += 1
            continue
        compared.append(pair)
        in_truth = pair.nets in (zone_truth if is_zone_pair else truth)
        in_consumer = pair.nets in consumer
        if in_consumer and not in_truth:
            found.append((pair, "over"))
        elif in_truth and not in_consumer:
            found.append((pair, "under"))
    return found, compared, boundary_hits


def _pair_label(pair: PairIntent) -> str:
    return f"`{pair.kind}` {sorted(pair.nets)}"


def _trace_entry(
    case: CopperCase, pair: PairIntent, kicad_gap: float | None, own: str | None, at: str | None
) -> str:
    """One named pair: what each reading decided, and what kicad-cli said.

    ``own`` / ``at`` are the direction each reading disagreed in, or ``None``
    where that reading agrees with kicad-cli.  A pair the two readings decide
    differently is a rule value; a pair both decide the same way against
    kicad-cli is arithmetic.
    """
    if own == at == "over":
        verdict = (
            f"rejected at both rule values (kicad-cli clean; {pair.target_gap_mm:.4f} mm placed)"
        )
    elif own == at == "under":
        verdict = f"accepted at both rule values (kicad-cli flags it at {kicad_gap:.4f} mm)"
    elif own == "under":
        verdict = f"accepted at the caller's rule, flagged at the project minimum (kicad-cli {kicad_gap:.4f} mm)"
    elif at == "over":
        verdict = (
            f"rejected only at the project minimum (kicad-cli clean; "
            f"{pair.target_gap_mm:.4f} mm placed)"
        )
    elif own == "over":
        verdict = (
            f"rejected only at the caller's rule (kicad-cli clean; "
            f"{pair.target_gap_mm:.4f} mm placed)"
        )
    else:  # pragma: no cover - a reading that agrees with kicad-cli is never traced
        verdict = f"own={own!r} matched={at!r}"
    return f"seed {case.seed}: {_pair_label(pair)} -- {verdict}"


def _same_rule_reading(
    adapter: ConsumerAdapter,
    results: list[tuple[CopperCase, OracleResult]],
    state_names: tuple[str, ...],
) -> SameRuleReading:
    """Read one advisory adapter at both rule values over the measured corpus.

    Costs no ``kicad-cli`` run: the oracle results are the ones the table
    already paid for, and only the consumer is asked a second question.  The
    trace is taken from the as-is run alone, so each named pair appears once
    rather than once per fill state.
    """
    kinds = _adapter_pair_kinds(adapter)
    own_cache: dict[str, set[frozenset[str]]] = {}
    at_cache: dict[str, set[frozenset[str]]] = {}
    own_over = own_under = own_compared = 0
    at_over = at_under = flips = 0
    by_kind: dict[str, int] = {}
    trace: list[str] = []
    for case, result in results:
        # Computed once per case and reused across its fill states: an adapter
        # never sees zone fill, so its answer cannot change between them.
        # (Not ``setdefault`` -- that evaluates the verdicts before looking.)
        if case.name not in own_cache:
            own_cache[case.name] = {v.nets for v in adapter.verdicts(case)}
            at_cache[case.name] = {v.nets for v in adapter.verdicts_at_project_rules(case)}
        own = own_cache[case.name]
        at = at_cache[case.name]
        own_found, compared_pairs, _ = _score(case, result, own, kinds)
        at_found, _, _ = _score(case, result, at, kinds)
        own_compared += len(compared_pairs)
        for pair in compared_pairs:
            by_kind[pair.kind] = by_kind.get(pair.kind, 0) + 1
        own_over += sum(1 for _, d in own_found if d == "over")
        own_under += sum(1 for _, d in own_found if d == "under")
        at_over += sum(1 for _, d in at_found if d == "over")
        at_under += sum(1 for _, d in at_found if d == "under")
        # A flip is a pair the own-rule reading accepts and the matched-rule
        # reading flags.  Measured against the matched-rule reading's
        # *under*-rejections, not against the set it flags: a pair both
        # readings accept is in neither set and is a retained gap, not a flip.
        at_under_nets = {pair.nets for pair, d in at_found if d == "under"}
        flips += sum(1 for pair, d in own_found if d == "under" and pair.nets not in at_under_nets)

        if result.refill or case.seed not in ADVISORY_TRACE_SEEDS:
            continue
        kicad_gaps = {v.nets: v.gap_mm for v in result.without_zones()}
        own_by_nets = {pair.nets: d for pair, d in own_found}
        at_by_nets = {pair.nets: d for pair, d in at_found}
        for pair in compared_pairs:
            own_direction = own_by_nets.get(pair.nets)
            at_direction = at_by_nets.get(pair.nets)
            if own_direction is None and at_direction is None:
                continue
            trace.append(
                _trace_entry(case, pair, kicad_gaps.get(pair.nets), own_direction, at_direction)
            )
    return SameRuleReading(
        group=adapter.group,
        adapter_name=adapter.name,
        own=AdapterMeasurement(
            group=adapter.group,
            adapter_name=adapter.name,
            pairs_compared=own_compared,
            over_reject=own_over,
            under_reject=own_under,
            fill_states=state_names,
        ),
        at_project=AdapterMeasurement(
            group=adapter.group,
            adapter_name=adapter.name,
            pairs_compared=own_compared,
            over_reject=at_over,
            under_reject=at_under,
            fill_states=state_names,
        ),
        compared_by_kind=tuple(sorted(by_kind.items())),
        rule_value_flips=flips,
        trace=tuple(trace),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate docs/clearance-conformance.md from a measured corpus."
    )
    parser.add_argument(
        "--seeds",
        default="0-23",
        help="Inclusive seed range, e.g. '0-199' (default: 0-23).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DOC_PATH,
        help=f"Output path (default: {DOC_PATH}).",
    )
    parser.add_argument(
        "--fill",
        choices=("both", "as-is", "refilled"),
        default="both",
        help="Fill states to measure (default: both).",
    )
    args = parser.parse_args(argv)

    fill_states = {
        "both": (False, True),
        "as-is": (False,),
        "refilled": (True,),
    }[args.fill]

    try:
        stats, measurements, advisory = measure_corpus(
            parse_seed_range(args.seeds), fill_states=fill_states
        )
    except KiCadCliAbsent as exc:
        # Refuse to emit a document. Without kicad-cli every disagreement rate
        # would read zero, which is a confident wrong answer.
        print(f"{REASON_ABSENT}: {exc}", file=sys.stderr)
        print("refusing to write a conformance table without ground truth", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_document(stats, measurements, advisory), encoding="utf-8")
    print(
        f"wrote {args.out} ({len(GROUPS)} group rows, {len(advisory)} advisory rows, "
        f"{stats.case_count} cases)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
