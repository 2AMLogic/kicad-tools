"""Each wired adapter, measured against kicad-cli over the smoke corpus.

``test_corpus_truth.py`` proved the *harness* is honest: the geometry the
generator intends is the geometry KiCad measures.  This module is the layer
above it -- the same seeded cases, but now asking each in-tree clearance
consumer the question kicad-cli already answered.

**An adapter-vs-truth row is report-only until its consumer is switched.**
Each carries ``@pytest.mark.consumer``, and ``conftest.py``'s collection hook
turns that into ``xfail(strict=False)``.  A measured disagreement must not
redden the build: consumers are switched to the shared kernel in their own
Epic #5509 phase, and only then does a row become a merge gate.  That
mechanism landed untested in PR #5532 (there were no consumer items to
exercise it), so :func:`test_every_unmigrated_consumer_item_carries_xfail`
asserts it over the whole collected suite.

**A row whose group is in ``report.MIGRATED_GROUPS`` is a merge gate.**  Its
consumer has had its Phase 3/4 PR, the hook leaves the item un-``xfail``\\ ed,
and :func:`test_adapter_agrees_with_kicad_cli` asserts agreement for real.
The gated reading drives that same unmodified consumer at the clearance
**kicad-cli itself applies** (``adapter.verdicts_at_project_rules``), so the
gate fails on a geometry disagreement -- what a migration changes -- and not
on the rule value the consumer resolves, which is Phase 2's axis and stays
measured by the published table.
:func:`test_migrated_consumer_items_are_hard_gates` asserts the flip really
reached the collected items.

**Groups 1 and 2 carry a pinned quantisation ledger.**  They were migrated by
#5660 (Phase 3a) and answer with a *cell set*, which over-rejects by
construction, so :data:`QUANTISATION_LEDGER` records the exact residual they
are still allowed; zero under-rejection stays unconditional.  Every other
migrated group has no entry and is held to zero disagreement in both
directions (``conftest.assert_migrated_group_agrees``).

**Hard assertions here are about the adapters, not the consumers.**  An
adapter must satisfy the protocol, must be deterministic, and must only ever
speak about nets the case actually declares.  Those are properties of the
harness and a failure in one is a real bug -- so they carry no ``consumer``
marker and no ``xfail``.

One test item per ``(seed, adapter)``, never a loop over seeds inside one
item: the CI ``test`` job runs ``--timeout=60``, and a batched item would both
risk the reaper and report one failure for the whole corpus instead of naming
the seed that broke.  The smoke corpus is deliberately tiny -- each *seed*
costs a kicad-cli process, measured once and shared by every adapter's item
through ``conftest.corpus_oracle`` -- while the published table is measured
out-of-band over seeds 0-199 by ``python -m tests.conformance.report``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conformance.adapters import BOARD_EDGE, ConsumerAdapter, Verdict
from tests.conformance.adapters._support import net_ids, pair_contexts, router_pad, router_via
from tests.conformance.adapters.grid_py import pathfinder_via_pad_control
from tests.conformance.adapters.kernel import KERNEL_GROUP
from tests.conformance.adapters.via_clearance import PAD_FORMS, pad_form_is_clear
from tests.conformance.board import write_case
from tests.conformance.conftest import (
    CorpusOracle,
    assert_migrated_group_agrees,
    item_group,
    requires_adapter,
    requires_kicad_cli,
)
from tests.conformance.generator import (
    DEFAULT_RULES,
    CopperCase,
    PadSpec,
    PairIntent,
    PairKind,
    ViaSpec,
    generate_case,
    pad_shape,
)
from tests.conformance.oracle import run_oracle
from tests.conformance.report import (
    ADAPTERS,
    ADVISORY_GROUPS,
    ADVISORY_TRACE_SEEDS,
    CLASSIFICATIONS,
    CONSERVATIVE,
    MIGRATED_GROUPS,
    OUT_OF_SCOPE,
    QUANTISED_GROUPS,
    RETAINED_GAP,
    RULE_VALUE,
    main,
)

_ADAPTERS_BY_GROUP = {adapter.group: adapter for adapter in ADAPTERS}

# Kept smaller than ``test_corpus_truth.CI_SEEDS``: this module multiplies its
# seeds by every wired adapter.  The kicad-cli measurement is per seed, not per
# item (``conftest.corpus_oracle``), but each seed still adds one item per
# adapter.
CI_SEEDS = (0, 1, 2)

# The Epic #5509 section-1 groups with no adapter, whose reasons are recorded
# in ``report.NOT_MEASURED_REASONS``.
#
# EMPTY since Phase 3c (#5662).  Phase 1c left exactly one entry -- group 7's
# ``CoupledPathfinder::rail_clear``, a lambda inside the C++ coupled search
# loop with no Python entry point to wrap, the single *unexposed* gap the
# epic's acceptance criterion permitted.  Phase 3c promoted it to a bound
# method as part of migrating it onto the clearance kernel, so ``coupled.py``
# measures it and all nineteen groups are wired.  Keeping this an explicit set
# is what makes "all nineteen" a test rather than a claim: dropping a group's
# adapter without recording it here fails
# ``test_adapters_cover_the_wired_groups``.
UNWIRED_GROUPS: set[int] = set()

# Every group row the table measures, plus the Phase 1b kernel's control row.
WIRED_GROUPS = {n for n in range(1, 20) if n not in UNWIRED_GROUPS} | {KERNEL_GROUP}

# The residual over-rejection a migrated *cell-set* group is still allowed, per
# ``(group, seed)``, measured on the gated reading (the consumer driven at
# kicad-cli's own clearance, ``verdicts_at_project_rules``).  Entries use the
# same string form the failure message prints, so re-pinning is a copy-paste
# rather than a transcription.  A ``(group, seed)`` with no key is held to an
# empty set -- which is every migrated group other than 1 and 2, so this ledger
# widens nothing for groups 6-10.
#
# Every entry below is cell quantisation, and it survives Phase 3a (#5660) by
# construction rather than by oversight.  Groups 1 and 2 answer with a *cell
# set*, and the adapter's rejection rule dilates **both** sides -- the existing
# copper's halo and the candidate's own -- so two objects whose copper is a
# comfortable 0.23 mm apart still share a marked cell once each has been grown
# by a radius that rounds outwards (``int(...) + 1``, plus a second ``+ 1`` for
# a segment).  #5660 replaced the halo's *shape* (Chebyshev square -> the
# kernel's exact disc), which removes the sqrt(2) diagonal excess; it does not
# and cannot remove the outward rounding along the pair axis, which is where
# every entry here sits.
#
# These are retired by search-time *refinement* (Epic #5509 groups 4 and 5,
# Phase 3b), which re-reads the exact geometry of a cell the halo marked -- not
# by making the halo itself finer, which would spend the grid-quantisation
# safety margin (#1666, #1692, #1797) that keeps the marking conservative.
QUANTISATION_LEDGER: dict[tuple[int, int], tuple[str, ...]] = {
    (1, 0): ("pad-seg ['N3', 'N4'] gap=0.2468",),
    (1, 1): ("seg-seg ['N3', 'N4'] gap=0.2289",),
    (1, 2): ("seg-via ['N1', 'N2'] gap=0.2336",),
    (2, 1): ("seg-seg ['N3', 'N4'] gap=0.2289",),
    (2, 2): ("seg-via ['N1', 'N2'] gap=0.2336",),
}

_ADAPTER_PARAMS = [
    pytest.param(adapter, id=adapter.name, marks=(requires_adapter(adapter),))
    for adapter in ADAPTERS
]


# ---------------------------------------------------------------------------
# Adapter contract -- hard assertions, no kicad-cli needed
# ---------------------------------------------------------------------------


def test_adapters_cover_the_wired_groups() -> None:
    """``ADAPTERS`` wires exactly the groups this phase set out to measure.

    Both directions matter.  A *missing* group is coverage that quietly went
    away; an *extra* one is a group wired without a row in the epic's
    inventory, which the report's 19-row invariant would then have nowhere to
    put.
    """
    assert {adapter.group for adapter in ADAPTERS} == WIRED_GROUPS
    assert len({adapter.name for adapter in ADAPTERS}) == len(ADAPTERS)


@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_adapter_satisfies_the_protocol(adapter: ConsumerAdapter) -> None:
    assert isinstance(adapter, ConsumerAdapter)
    assert isinstance(adapter.name, str) and adapter.name
    assert adapter.group in WIRED_GROUPS
    assert adapter.pair_kinds <= frozenset(PairKind.ALL)
    assert adapter.pair_kinds, f"{adapter.name} declares no pair kinds at all"
    assert isinstance(adapter.available(), bool)


def test_only_the_kernel_answers_pad_pad_pairs() -> None:
    """``pad-pad`` is placement copper: no router-side consumer sees it.

    The kind exists for group 19's incremental placement DRC, whose entry
    point takes two whole footprints.  Any *other* adapter claiming it would
    be scored on pairs its consumer is never consulted about in production,
    which is the exact error ``ConsumerAdapter.pair_kinds`` exists to prevent
    -- and the reason ``_support.ALL_PAIR_KINDS`` is ``PairKind.ROUTING``
    rather than ``PairKind.ALL``.
    """
    claimants = {a.group for a in ADAPTERS if PairKind.PAD_PAD in a.pair_kinds}
    assert claimants == {19, KERNEL_GROUP}, (
        "pad-pad may only be claimed by group 19 (drc_cpp) and the kernel "
        f"control row; also claimed by groups {sorted(claimants - {19, KERNEL_GROUP})}"
    )


def test_only_zone_and_edge_aware_consumers_answer_those_pairs() -> None:
    """The #5644 kinds are claimed by the two groups that model them, and no more.

    A consumer with no zone-fill or board-outline term in its arithmetic is
    never consulted about one in production, so scoring it on those pairs
    would manufacture an under-rejection rate out of a question nobody asks
    it -- the same error ``pair_kinds`` exists to prevent for ``pad-pad``.
    Group 18 drives four rules that really read that geometry; group 10's
    ``is_clear`` really has a ``pours`` and an ``outline`` branch (and no via
    candidate, which is why it claims ``seg-zone`` but not ``via-zone``).
    """
    zone_claimants = {a.group for a in ADAPTERS if set(PairKind.ZONE) & a.pair_kinds}
    edge_claimants = {a.group for a in ADAPTERS if set(PairKind.EDGE) & a.pair_kinds}
    assert zone_claimants == {10, 18}, sorted(zone_claimants)
    assert edge_claimants == {10, 18}, sorted(edge_claimants)

    mesh = next(a for a in ADAPTERS if a.group == 10)
    assert PairKind.VIA_ZONE not in mesh.pair_kinds, (
        "ObstacleModel.is_clear takes two points and no width, so it has no "
        "via candidate to answer about"
    )
    kernel = _ADAPTERS_BY_GROUP[KERNEL_GROUP]
    assert not (set(PairKind.ZONE) | set(PairKind.EDGE)) & kernel.pair_kinds, (
        "the kernel control row is scored on the same copper the consumer "
        "rows are; modelling a pour by its declared boundary while kicad-cli "
        "measures the filled polygon would make its 0%/0% criterion fiction"
    )


@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_adapter_is_deterministic(adapter: ConsumerAdapter) -> None:
    """Two runs over the same case give the same answer.

    A consumer driven through a grid, a temp file or a compiled extension has
    plenty of places for run-to-run variation to creep in; a table regenerated
    from a non-deterministic adapter would not be reproducible evidence.
    """
    case = generate_case(3)
    assert adapter.verdicts(case) == adapter.verdicts(case)


@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_adapter_only_speaks_about_declared_nets(adapter: ConsumerAdapter) -> None:
    """Verdict nets come from the case, never from somewhere else.

    Guards the translation in ``adapters/_support.py``: a net name an adapter
    invented (or a net id it mapped back wrongly) would silently miss every
    pair and read as a 0% disagreement rate.
    """
    for seed in CI_SEEDS:
        case = generate_case(seed)
        # ``BOARD_EDGE`` is the one name that is legitimately not a declared
        # net: it is the reserved pseudo-net both the oracle and the adapters
        # pair a one-sided ``copper_edge_clearance`` finding with, so that
        # every verdict stays a symmetric two-element set.
        declared = set(case.nets) | {BOARD_EDGE}
        for verdict in adapter.verdicts(case):
            assert isinstance(verdict, Verdict)
            assert verdict.nets <= declared, (
                f"{adapter.name} on seed {seed} named nets outside the case: "
                f"{sorted(verdict.nets - declared)}"
            )


# ---------------------------------------------------------------------------
# The measurement itself -- report-only until the consumer is switched
# ---------------------------------------------------------------------------


def _consumer_verdicts(adapter: ConsumerAdapter, case) -> set[frozenset[str]]:
    """The reading this item scores the adapter on.

    An **unmigrated** group is read exactly as the published table reads it:
    the consumer at its own rule values, with the result xfailed.  Both halves
    of a disagreement -- geometry and rule selection -- are evidence for that
    consumer's own phase, and separating them there would be premature.

    A **migrated** group is read at the clearance kicad-cli itself applies.
    Its migration PR changed *geometry* and nothing else (Epic #5509 scope
    guard #1 forbids touching a rule value), so gating it on the consumer's
    own resolved clearance would redden this build for the rule defect
    #5398 / #5654 tracks, which no Phase 3 PR is allowed to fix.  Pinning the
    rule axis to ground truth's own value is what makes the gate a statement
    about the thing the migration actually moved.
    """
    if adapter.group in MIGRATED_GROUPS:
        at_project = getattr(adapter, "verdicts_at_project_rules", None)
        assert at_project is not None, (
            f"{adapter.name} (group {adapter.group}) is in MIGRATED_GROUPS but "
            "does not implement verdicts_at_project_rules(case); a gated row "
            "must be drivable at kicad-cli's own rule value"
        )
        return {v.nets for v in at_project(case)}
    return {v.nets for v in adapter.verdicts(case)}


@requires_kicad_cli
@pytest.mark.consumer
@pytest.mark.parametrize("seed", CI_SEEDS)
@pytest.mark.parametrize("adapter", _ADAPTER_PARAMS)
def test_adapter_agrees_with_kicad_cli(
    adapter: ConsumerAdapter, seed: int, corpus_oracle: CorpusOracle
) -> None:
    """One consumer, one seed, against ground truth.

    Compared on pair identity only -- *did this model flag this pair at all?*
    Gaps are never compared across models: each computes its own, and the
    difference would be arithmetic noise rather than the question under test.

    Only the pairs the adapter declares itself in scope for
    (``ConsumerAdapter.pair_kinds``) are compared, and boundary-band pairs are
    excluded: within 1 um of the requirement the two models are arguing about
    rounding.  For an unmigrated group this mirrors ``report.measure_corpus``
    exactly, so a red (well, xfailed) item here corresponds to a non-zero cell
    in the published table.

    For a **migrated** group (``report.MIGRATED_GROUPS``) the item carries no
    ``xfail`` -- that is the flip its epic phase exists to make -- and the
    consumer is driven at kicad-cli's own rule value; see
    :func:`_consumer_verdicts` for why the two readings differ.  A gated
    adapter must compare something on at least one of ``CI_SEEDS``, or the
    gate is vacuous.

    Zone pairs are the one exception, and for the same reason the report
    scores them only on its refilled run: this item measures the *as-is* board,
    where an unfilled pour contributes no copper to kicad-cli at all, so a zone
    comparison here would score the fill state rather than the model.
    :func:`test_zone_pairs_are_measured_on_a_refilled_run` covers them.
    """
    case = generate_case(seed)
    result = corpus_oracle(seed, refill=False)

    truth = {v.nets for v in result.without_zones()}
    consumer = _consumer_verdicts(adapter, case)

    over: list[str] = []
    under: list[str] = []
    compared = 0
    for pair in case.pairs:
        if pair.kind not in adapter.pair_kinds or pair.boundary:
            continue
        if pair.kind in PairKind.ZONE:
            continue
        compared += 1
        in_truth = pair.nets in truth
        in_consumer = pair.nets in consumer
        if in_consumer and not in_truth:
            over.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")
        elif in_truth and not in_consumer:
            under.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")

    if adapter.group in MIGRATED_GROUPS and not compared:
        # A gate over an empty denominator is green for the wrong reason.  An
        # xfailed row can afford to be vacuous (the published table carries
        # the real denominator); a gated one cannot, because "no in-scope pair
        # on this seed" and "this consumer agrees" would look identical.
        #
        # The statement is made over ``CI_SEEDS`` rather than over this one
        # seed, because a consumer's ``pair_kinds`` can be rare enough that no
        # single seed carries one: group 6 sees only static placement copper
        # (``pad-seg`` / ``pad-via``) and seed 2 places no pad pair at all.
        # Requiring *every* seed to contribute would force CI_SEEDS to grow --
        # a kicad-cli process per seed and an item per seed per adapter -- to
        # say something the union already says.  Counting pairs needs no
        # oracle, so this stays cheap.
        elsewhere = sum(
            1
            for other in CI_SEEDS
            for pair in generate_case(other).pairs
            if pair.kind in adapter.pair_kinds
            and not pair.boundary
            and pair.kind not in PairKind.ZONE
        )
        assert elsewhere, (
            f"{adapter.name} (group {adapter.group}) is gated but no seed in "
            f"{CI_SEEDS} offers an in-scope pair to compare "
            f"(pair_kinds={sorted(adapter.pair_kinds)}) -- the gate is vacuous; "
            "re-pin CI_SEEDS or widen the scope"
        )

    if adapter.group in MIGRATED_GROUPS:
        assert_migrated_group_agrees(
            adapter,
            over=over,
            under=under,
            expected_over=QUANTISATION_LEDGER.get((adapter.group, seed), ()),
            what=f"seed {seed}",
            context=result.describe(),
        )
        return

    assert not over and not under, (
        f"{adapter.name} (group {adapter.group}) disagrees with kicad-cli on seed {seed}\n"
        f"  over-rejected (consumer flags, KiCad clean): {over}\n"
        f"  under-rejected (KiCad flags, consumer clean): {under}\n"
        f"{result.describe()}"
    )


#: One seed per consumer that claims a zone kind, chosen so each one really
#: carries a zone pair: seed 7 draws a ``via-zone`` pair (group 18 only) and
#: seed 22 a ``seg-zone`` one (groups 18 and 10).  Pinned rather than searched
#: for, so a generator change that stopped placing a kind fails loudly instead
#: of silently skipping the only item that measures it.
ZONE_PAIR_SEEDS = (7, 22)


@requires_kicad_cli
@pytest.mark.consumer
@pytest.mark.parametrize("seed", ZONE_PAIR_SEEDS)
def test_zone_pairs_are_measured_on_a_refilled_run(seed: int, corpus_oracle: CorpusOracle) -> None:
    """The zone half of the table, exercised end to end on one seed per kind.

    Everything the report does differently for a zone pair is here: the oracle
    runs with ``refill=True``, ground truth comes from the verdicts that
    *involve* zone copper (rather than from ``without_zones()``), and only the
    adapters that claim a zone kind are asked.  Without this item the whole
    refilled-run path would be exercised only by the out-of-band table
    regeneration, which no PR runs.

    Report-only like every other consumer row -- a disagreement is evidence
    for group 18's or group 10's own epic phase, not a red build.
    """
    case = generate_case(seed)
    zone_pairs = [p for p in case.pairs if p.kind in PairKind.ZONE]
    assert zone_pairs, f"seed {seed} no longer carries a zone pair -- re-pin ZONE_PAIR_SEEDS"

    result = corpus_oracle(seed, refill=True)
    truth = {v.nets for v in result.verdicts if v.zone}

    findings: list[str] = []
    measured = 0
    for adapter in ADAPTERS:
        in_scope = [p for p in zone_pairs if p.kind in adapter.pair_kinds and not p.boundary]
        if not in_scope or not adapter.available():
            continue
        consumer = {v.nets for v in adapter.verdicts(case)}
        for pair in in_scope:
            measured += 1
            in_truth = pair.nets in truth
            in_consumer = pair.nets in consumer
            if in_consumer != in_truth:
                direction = "over" if in_consumer else "under"
                findings.append(
                    f"{adapter.name} (group {adapter.group}) {direction}-rejects "
                    f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}"
                )

    assert measured, (
        f"seed {seed}: no adapter claimed a zone pair -- the zone cells of the "
        "published table would be an empty denominator"
    )
    assert not findings, (
        f"zone-pair disagreements with kicad-cli on seed {seed} "
        f"(refilled):\n  " + "\n  ".join(findings) + f"\n{result.describe()}"
    )


# ---------------------------------------------------------------------------
# The kernel is NOT report-only -- it is a hard gate
# ---------------------------------------------------------------------------


@requires_kicad_cli
@pytest.mark.parametrize("seed", CI_SEEDS)
def test_kernel_agrees_with_kicad_cli(seed: int, corpus_oracle: CorpusOracle) -> None:
    """The Phase 1b kernel, against ground truth, with **no** ``xfail``.

    Every consumer row above is deliberately non-strict-xfailed: a measured
    disagreement is evidence for a later phase, not a broken build.  The
    kernel is the exception, and the asymmetry is the point.  It is not a
    consumer awaiting migration -- it is the model every consumer is to be
    migrated *onto*, so a disagreement here is not a finding about a consumer,
    it is a **Phase 1b bug**: unifying onto a model that cannot match
    kicad-cli would replace nineteen wrong answers with one.

    Deliberately redundant with the ``consumer``-marked item above (the kernel
    is in ``ADAPTERS`` and is measured there too, for the table).  The
    duplicate costs no extra kicad-cli process worth caring about and buys the
    one thing the xfailed item cannot: a **red** build if the control row
    stops being zero.

    Per Epic #5509's scope guard, the fix for a failure here is a fixture
    under ``tests/fixtures/conformance/`` plus a kernel PR through
    ``tests/router/test_clearance_kernel_parity.py`` -- never a patch to the
    kernel from inside this phase, and never a relaxation of this assertion.
    """
    kernel = _ADAPTERS_BY_GROUP[KERNEL_GROUP]
    case = generate_case(seed)
    result = corpus_oracle(seed, refill=False)

    truth = {v.nets for v in result.without_zones()}
    verdicts = {v.nets for v in kernel.verdicts(case)}

    over: list[str] = []
    under: list[str] = []
    for pair in case.pairs:
        if pair.boundary or pair.kind not in kernel.pair_kinds:
            # The zone and edge kinds are out of this row's scope (#5644):
            # see ``KernelAdapter.pair_kinds`` on why widening it needs a
            # fill-aware kernel shape rather than a wider comparison here.
            continue
        in_truth = pair.nets in truth
        in_kernel = pair.nets in verdicts
        if in_kernel and not in_truth:
            over.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")
        elif in_truth and not in_kernel:
            under.append(f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}")

    assert not over and not under, (
        f"the Phase 1b clearance kernel disagrees with kicad-cli on seed {seed} "
        "-- this is a KERNEL bug, not a consumer finding\n"
        f"  over-rejected (kernel flags, KiCad clean): {over}\n"
        f"  under-rejected (KiCad flags, kernel clean): {under}\n"
        f"{result.describe()}\n"
        "Capture this seed as a fixture under tests/fixtures/conformance/ and "
        "fix the kernel in its own PR through "
        "tests/router/test_clearance_kernel_parity.py."
    )


# ---------------------------------------------------------------------------
# The published table regenerates deterministically
# ---------------------------------------------------------------------------


@requires_kicad_cli
def test_report_regenerates_byte_identically(tmp_path: Path) -> None:
    """Two runs over the same seed range produce identical bytes.

    Epic #5509 Phase 1c's determinism criterion, asserted **inside the
    suite**.  It cannot be a post-hoc diff of a CI artifact against
    ``docs/``: ``ci.yml``'s ``code`` path filter excludes ``docs/**`` and
    ``**/*.md`` (``:57-66``), so a PR that only regenerates the table never
    runs the ``test`` job at all and the diff would never be computed.  A
    check that does not run on the change it guards is not a gate.

    Kept to two seeds -- this is a determinism property of the generator, the
    oracle and every adapter, and it either holds on two cases or it does not
    hold at all.  Widening the range would multiply kicad-cli processes for no
    extra discrimination.
    """
    seeds = range(0, 2)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"

    for out in (first, second):
        rc = main(["--seeds", f"{seeds.start}-{seeds.stop - 1}", "--out", str(out)])
        assert rc == 0, "the report generator refused to write"

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8"), (
        "the conformance report is not reproducible: two runs over seeds "
        f"{seeds.start}-{seeds.stop - 1} produced different documents. Either an "
        "adapter, the generator or the oracle has run-to-run variation, and the "
        "committed table stops being evidence anyone can reproduce."
    )


# ---------------------------------------------------------------------------
# The auto-xfail mechanism -- and its migrated-group exception -- under test
# ---------------------------------------------------------------------------


def test_migrated_groups_are_wired_and_drivable_at_ground_truths_rule_value() -> None:
    """``MIGRATED_GROUPS`` names real, gated, project-rule-drivable adapters.

    Three static properties, none of which needs kicad-cli, so a migration
    registered without the plumbing to back it fails on any laptop:

    * every migrated group is one of the epic's nineteen (never the kernel
      control row, which was never a consumer to migrate);
    * every migrated group actually has an adapter -- listing an unwired group
      would flip a gate that has no row to gate;
    * that adapter implements ``verdicts_at_project_rules``, the reading
      ``_consumer_verdicts`` gates on.  Without it the gate would silently
      fall back to the consumer's own rule values and start failing for the
      #5654 rule defect instead of for a geometry disagreement.
    """
    consumer_groups = {g for g in WIRED_GROUPS if g != KERNEL_GROUP}
    assert not (MIGRATED_GROUPS - consumer_groups), (
        "MIGRATED_GROUPS names groups that are not wired consumer groups: "
        f"{sorted(MIGRATED_GROUPS - consumer_groups)}"
    )
    for group in sorted(MIGRATED_GROUPS):
        adapter = _ADAPTERS_BY_GROUP[group]
        assert hasattr(adapter, "verdicts_at_project_rules"), (
            f"{adapter.name} (group {group}) is migrated but cannot be driven "
            "at the project netclass clearance"
        )


def test_the_quantisation_ledger_only_names_migrated_cell_set_rows() -> None:
    """Every ledger key is a migrated group on a seed the gate actually runs.

    The gate itself already fails on an entry that stopped reproducing (the
    recorded set must *equal* the measured one), so this guards the other way a
    ledger rots: an entry for a group that is not migrated, or for a seed that
    left ``CI_SEEDS``, would sit there looking authoritative while nothing ever
    consulted it.  It is also where "only the cell-set groups may carry
    over-rejection" is a test rather than a comment.
    """
    stale = sorted(
        key
        for key in QUANTISATION_LEDGER
        if key[0] not in MIGRATED_GROUPS or key[1] not in CI_SEEDS
    )
    assert not stale, f"quantisation-ledger entries no gated row consults: {stale}"
    assert {group for group, _ in QUANTISATION_LEDGER} <= QUANTISED_GROUPS, (
        "only report.QUANTISED_GROUPS (the cell-set occupancy rows) may carry "
        "a quantisation ledger; every other migrated group is held to zero "
        "disagreement"
    )
    assert QUANTISED_GROUPS <= MIGRATED_GROUPS, "a quantised group must be a migrated one"
    assert all(QUANTISATION_LEDGER.values()), "an empty ledger entry is the default -- drop the key"


def _consumer_items(request: pytest.FixtureRequest) -> list[pytest.Item]:
    items = [
        item for item in request.session.items if item.get_closest_marker("consumer") is not None
    ]
    if not items:
        pytest.skip(
            "no @pytest.mark.consumer items were collected -- this assertion "
            "is only meaningful for a whole-suite run (uv run pytest tests/conformance)"
        )
    return items


def test_every_unmigrated_consumer_item_carries_xfail(request: pytest.FixtureRequest) -> None:
    """``conftest.pytest_collection_modifyitems`` really fired.

    The hook is the *only* thing standing between a measured disagreement and
    a red build, and it shipped with no item to exercise it.  This walks the
    session's collected items rather than trusting the marker: every
    ``@pytest.mark.consumer`` item whose group is not yet migrated must also
    carry an ``xfail``, and that ``xfail`` must be non-strict (a strict one
    would fail the moment a consumer starts agreeing, which is the outcome the
    epic is working towards).
    """
    candidates = [i for i in _consumer_items(request) if item_group(i) not in MIGRATED_GROUPS]

    missing = [item.nodeid for item in candidates if item.get_closest_marker("xfail") is None]
    assert not missing, f"consumer items collected without an auto-xfail: {missing}"

    strict = [
        item.nodeid
        for item in candidates
        if item.get_closest_marker("xfail").kwargs.get("strict") is not False
    ]
    assert not strict, f"consumer items whose xfail is strict: {strict}"


def test_migrated_consumer_items_are_hard_gates(request: pytest.FixtureRequest) -> None:
    """The flip reached the collected items, not just the registry.

    The mirror image of the test above, and the assertion that makes "group N
    is no longer report-only" checkable rather than declared.  Registering a
    group in ``MIGRATED_GROUPS`` while the hook still xfails its items would
    leave the epic's Phase 3/4 acceptance criterion unmet *and* invisible --
    the suite would stay green either way.

    Skips rather than fails when the whole suite was not collected: with
    ``-k`` or a single-module run there may be no migrated item present, which
    says nothing about the mechanism.
    """
    gated = [i for i in _consumer_items(request) if item_group(i) in MIGRATED_GROUPS]
    if not gated:
        pytest.skip(
            "no migrated-group consumer items collected -- run the whole "
            "module (uv run pytest tests/conformance/test_corpus.py)"
        )

    still_xfailed = [item.nodeid for item in gated if item.get_closest_marker("xfail") is not None]
    assert not still_xfailed, (
        "these consumer items belong to a group in MIGRATED_GROUPS but are "
        f"still auto-xfailed, so their disagreement cannot redden a build: {still_xfailed}"
    )


# ---------------------------------------------------------------------------
# The advisory rows at a matched rule (Issue #9922)
# ---------------------------------------------------------------------------
#
# Thirteen groups are gated outright.  The other six -- ``report.ADVISORY_GROUPS``
# -- are advisory: their ``consumer`` rows above stay auto-xfailed, because a
# kernel migration that has not been scheduled must not be forced by a gate.
# What *can* be gated today is their classification.  ``report.CLASSIFICATIONS``
# says, per group, which of a row's disagreements are rule values and which
# survive kicad-cli's own rule value; the three ledgers below pin that pair by
# pair on the seeds the published document names, so the classification cannot
# drift from the consumer it describes.
#
# All three are measured on the **matched-rule** reading
# (``verdicts_at_project_rules``) against the as-is oracle run, in the same
# string form the failure message prints.
#
# * ``SAME_RULE_OVER_LEDGER`` -- pairs the consumer rejects at the project
#   minimum that kicad-cli finds clean: arithmetic that errs outward.
# * ``SAME_RULE_UNDER_LEDGER`` -- pairs the consumer accepts at the project
#   minimum that kicad-cli flags: arithmetic or a model that is short.
# * ``RULE_VALUE_LEDGER`` -- pairs the consumer accepts at its own rule value
#   and flags at the project minimum.  These are the decisions the rule value
#   alone was deciding, and the set a repaired consumer must keep flagging.
#
# A ``(group, seed)`` with no key in a ledger is held to an empty set.  Group 19
# has no key in any of them: its only pair kind, ``pad-pad``, is placed on none
# of ``CI_SEEDS``, so its corpus gate is vacuous by construction and its
# evidence is the named control further down.
SAME_RULE_OVER_LEDGER: dict[tuple[int, int], tuple[str, ...]] = {
    (3, 1): ("seg-seg ['N3', 'N4'] gap=0.2289",),
    (3, 2): ("seg-via ['N1', 'N2'] gap=0.2336",),
}

SAME_RULE_UNDER_LEDGER: dict[tuple[int, int], tuple[str, ...]] = {
    (11, 1): ("pad-via ['N6', 'N7'] gap=0.1933",),
    (14, 0): ("seg-via ['N1', 'N2'] gap=0.1905", "pad-via ['N5', 'N6'] gap=0.1782"),
    (14, 1): ("seg-via ['N1', 'N2'] gap=0.1972", "pad-via ['N6', 'N7'] gap=0.1933"),
    (14, 2): ("seg-seg ['N3', 'N4'] gap=0.1752",),
}

RULE_VALUE_LEDGER: dict[tuple[int, int], tuple[str, ...]] = {
    (11, 0): ("seg-via ['N1', 'N2'] gap=0.1905",),
    (11, 1): ("seg-via ['N1', 'N2'] gap=0.1972",),
    (12, 0): ("pad-via ['N5', 'N6'] gap=0.1782",),
    (12, 1): ("pad-via ['N6', 'N7'] gap=0.1933",),
    (12, 2): ("seg-seg ['N3', 'N4'] gap=0.1752",),
    (13, 0): ("pad-via ['N5', 'N6'] gap=0.1782",),
    (13, 1): ("pad-via ['N6', 'N7'] gap=0.1933",),
    (13, 2): ("seg-seg ['N3', 'N4'] gap=0.1752",),
}

_SAME_RULE_LEDGERS = (SAME_RULE_OVER_LEDGER, SAME_RULE_UNDER_LEDGER, RULE_VALUE_LEDGER)

_ADVISORY_PARAMS = [
    pytest.param(adapter, id=adapter.name, marks=(requires_adapter(adapter),))
    for adapter in ADAPTERS
    if adapter.group in ADVISORY_GROUPS
]


def _pair_tag(pair: PairIntent) -> str:
    return f"{pair.kind} {sorted(pair.nets)} gap={pair.target_gap_mm:.4f}"


def _compared_pairs(adapter: ConsumerAdapter, case: CopperCase) -> list[PairIntent]:
    """The pairs an advisory reading is scored on -- the report's scope rule."""
    return [
        pair
        for pair in case.pairs
        if pair.kind in adapter.pair_kinds and not pair.boundary and pair.kind not in PairKind.ZONE
    ]


def _same_rule_disagreements(
    adapter: ConsumerAdapter,
    case: CopperCase,
    truth: set[frozenset[str]],
) -> tuple[list[str], list[str], list[str]]:
    """``(over, under, flips)`` for one advisory adapter on one case.

    ``over`` / ``under`` are the **matched-rule** reading's disagreements with
    kicad-cli.  ``flips`` are the pairs kicad-cli flags, the own-rule reading
    accepts and the matched-rule reading flags -- measured against the
    matched-rule reading's under-rejections, never against the set it flags,
    because a pair both readings accept is in neither set and is a retained
    gap rather than a flip.
    """
    own = {v.nets for v in adapter.verdicts(case)}
    at_project = {v.nets for v in adapter.verdicts_at_project_rules(case)}
    over: list[str] = []
    under: list[str] = []
    flips: list[str] = []
    for pair in _compared_pairs(adapter, case):
        in_truth = pair.nets in truth
        if pair.nets in at_project and not in_truth:
            over.append(_pair_tag(pair))
        elif in_truth and pair.nets not in at_project:
            under.append(_pair_tag(pair))
        if in_truth and pair.nets not in own and pair.nets in at_project:
            flips.append(_pair_tag(pair))
    return over, under, flips


def assert_same_rule_residual(
    adapter: object,
    *,
    over: list[str],
    under: list[str],
    flips: list[str],
    expected_over: tuple[str, ...] = (),
    expected_under: tuple[str, ...] = (),
    expected_flips: tuple[str, ...] = (),
    what: str,
    context: str = "",
) -> None:
    """The gate an advisory row's matched-rule reading is held to (Issue #9922).

    Unlike :func:`~tests.conformance.conftest.assert_migrated_group_agrees`
    this does not demand zero: a retained gap or a conservative approximation
    is *allowed*, but only as the exact set that is already recorded.  Pinning
    the set rather than a budget means three different events all redden the
    build with the evidence that has to be re-measured -- the consumer grew a
    new disagreement, lost one it was classified for, or stopped flagging a
    pair whose verdict was the rule value's alone.

    Args:
        adapter: The adapter under test; used for the failure message only.
        over: Matched-rule over-rejections, as the caller formats them.
        under: Matched-rule under-rejections, same format.
        flips: Rule-value flips, same format.
        expected_over: The pinned over-rejection set for this ``(group, case)``.
        expected_under: The pinned under-rejection set.
        expected_flips: The pinned rule-value set.
        what: Human label for the case, e.g. ``"seed 2"``.
        context: Optional extra provenance appended to a failure.
    """
    group = getattr(adapter, "group", None)
    name = getattr(adapter, "name", repr(adapter))
    suffix = f"\n{context}" if context else ""
    columns = (
        ("rejected at the project minimum, kicad-cli clean", over, expected_over),
        ("accepted at the project minimum, kicad-cli flags", under, expected_under),
        ("accepted at the caller's rule, flagged at the project minimum", flips, expected_flips),
    )
    problems = [
        f"  {label}:\n    measured: {sorted(got)}\n    recorded: {sorted(recorded)}"
        for label, got, recorded in columns
        if sorted(got) != sorted(recorded)
    ]
    assert not problems, (
        f"{name} (group {group}) does not match its Issue #9922 classification on "
        f"{what} (GATED at the matched rule).\n" + "\n".join(problems) + "\n"
        "A new entry means the consumer's arithmetic changed; a missing one means "
        "it was repaired or regressed. Either way re-classify it in "
        "report.CLASSIFICATIONS, re-pin the ledger here, and regenerate "
        f"docs/clearance-conformance.md in the same PR.{suffix}"
    )


@requires_kicad_cli
@pytest.mark.parametrize("seed", CI_SEEDS)
@pytest.mark.parametrize("adapter", _ADVISORY_PARAMS)
def test_advisory_same_rule_residual_is_pinned(
    adapter: ConsumerAdapter, seed: int, corpus_oracle: CorpusOracle
) -> None:
    """Every advisory row, driven at kicad-cli's own clearance, equals its ledger.

    Carries **no** ``consumer`` marker, so it is a hard gate: the advisory
    policy is unchanged -- the own-rule rows above still xfail -- but what the
    row *is* may no longer drift.  This is the "exercise its decision at that
    rule" half of Issue #9922: a consumer that was under-rejecting a pair
    because of its rule value is shown flagging it at the project rule, and a
    consumer with a retained gap is shown with exactly that gap and no other.
    """
    case = generate_case(seed)
    result = corpus_oracle(seed, refill=False)
    over, under, flips = _same_rule_disagreements(
        adapter, case, {v.nets for v in result.without_zones()}
    )
    key = (adapter.group, seed)
    assert_same_rule_residual(
        adapter,
        over=over,
        under=under,
        flips=flips,
        expected_over=SAME_RULE_OVER_LEDGER.get(key, ()),
        expected_under=SAME_RULE_UNDER_LEDGER.get(key, ()),
        expected_flips=RULE_VALUE_LEDGER.get(key, ()),
        what=f"seed {seed}",
        context=result.describe(),
    )


def test_same_rule_ledgers_only_name_advisory_rows_the_gate_runs() -> None:
    """Every ledger key is an advisory group on a seed the gate measures.

    Same contract as the quantisation ledger's: the gate already fails when a
    recorded entry stops reproducing, so this guards the other ways a ledger
    rots silently -- an entry for a group that has since been gated (its row
    would then be held to zero twice), for a seed outside ``CI_SEEDS``, or for
    a pair kind the adapter never sees.
    """
    for ledger in _SAME_RULE_LEDGERS:
        stale = sorted(
            key for key in ledger if key[0] not in ADVISORY_GROUPS or key[1] not in CI_SEEDS
        )
        assert not stale, f"same-rule ledger entries no gated row consults: {stale}"
        assert all(ledger.values()), "an empty ledger entry is the default -- drop the key"
        for (group, _seed), entries in ledger.items():
            kinds = _ADAPTERS_BY_GROUP[group].pair_kinds
            foreign = [e for e in entries if e.split(" ", 1)[0] not in kinds]
            assert not foreign, f"group {group} pins pair kinds it never sees: {foreign}"


def test_same_rule_ledgers_agree_with_the_authored_dispositions() -> None:
    """The static twin of ``report.classification_errors``.

    The document refuses to publish a classification its own numbers refute;
    this holds the ledgers to the same rule with no ``kicad-cli`` involved, so
    the pairing is checked on a laptop too.  A pinned over-rejection needs the
    conservative token, a pinned under-rejection an arithmetic token, and a
    pinned flip the rule-value token.
    """
    for ledger, tokens in (
        (SAME_RULE_OVER_LEDGER, frozenset({CONSERVATIVE})),
        (SAME_RULE_UNDER_LEDGER, frozenset({RETAINED_GAP, OUT_OF_SCOPE})),
        (RULE_VALUE_LEDGER, frozenset({RULE_VALUE})),
    ):
        for group, _seed in ledger:
            assert CLASSIFICATIONS[group].dispositions & tokens, (
                f"group {group} pins a ledger entry that needs one of "
                f"{sorted(tokens)} but is classified "
                f"{sorted(CLASSIFICATIONS[group].dispositions)}"
            )


def test_group_threes_residual_is_the_cell_set_quantisation() -> None:
    """Group 3's matched-rule over-rejection *is* groups 1 and 2's ledger.

    The classification says the read side answers on the cells the write side
    marked.  That is a claim about identity, so it is asserted as one: on
    every gate seed the pairs group 3 over-rejects at the project minimum are
    exactly the pairs group 2's quantisation ledger names, and group 3 retains
    no gap and moves no decision -- it was never under-rejecting.
    """
    for seed in CI_SEEDS:
        key = (3, seed)
        assert SAME_RULE_OVER_LEDGER.get(key, ()) == QUANTISATION_LEDGER.get((2, seed), ())
        assert key not in SAME_RULE_UNDER_LEDGER
        assert key not in RULE_VALUE_LEDGER


def test_every_advisory_gate_pins_something() -> None:
    """A gate whose ledgers are all empty is green for the wrong reason.

    Group 19 is the deliberate exception: its only pair kind is placed on none
    of ``CI_SEEDS``, so on the corpus it has nothing to pin and
    ``test_drc_cpp_disc_envelope_control`` is its evidence instead.
    """
    for group in sorted(ADVISORY_GROUPS - {19}):
        pinned = [key for ledger in _SAME_RULE_LEDGERS for key in ledger if key[0] == group]
        assert pinned, (
            f"group {group} is advisory but pins no matched-rule decision on "
            f"{CI_SEEDS} -- its gate is vacuous; re-pin CI_SEEDS or classify it"
        )
    assert tuple(ADVISORY_TRACE_SEEDS) == CI_SEEDS, (
        "the document names pairs on report.ADVISORY_TRACE_SEEDS; the gate must "
        "pin the same seeds or they describe different corpora"
    )


@pytest.mark.parametrize("group", sorted(ADVISORY_GROUPS))
def test_same_rule_gate_detects_a_decision_outside_the_classification(group: int) -> None:
    """Negative control for ``test_advisory_same_rule_residual_is_pinned``.

    A gate that cannot fail is not evidence.  For every advisory group this
    feeds the gate (a) the pinned residual plus one invented pair and (b) the
    pinned residual minus one pair, and requires it to fail both times,
    naming the pair; then (c) the pinned residual itself, and requires it to
    pass.  (a) is what a regression looks like -- the consumer grew a
    disagreement nobody has classified -- and (b) is what a repair looks
    like: a retained gap that closed under a classification which now
    overstates it.
    """
    adapter = _ADAPTERS_BY_GROUP[group]
    kind = sorted(adapter.pair_kinds)[0]
    invented = f"{kind} ['N1', 'N2'] gap=0.1900"
    for seed in CI_SEEDS:
        key = (group, seed)
        pinned = (
            SAME_RULE_OVER_LEDGER.get(key, ()),
            SAME_RULE_UNDER_LEDGER.get(key, ()),
            RULE_VALUE_LEDGER.get(key, ()),
        )

        def columns_of(*groups: tuple[str, ...]) -> tuple[list[str], list[str], list[str]]:
            return list(groups[0]), list(groups[1]), list(groups[2])

        def gate(reading: tuple[list[str], list[str], list[str]]) -> None:
            assert_same_rule_residual(
                adapter,
                over=reading[0],
                under=reading[1],
                flips=reading[2],
                expected_over=pinned[0],
                expected_under=pinned[1],
                expected_flips=pinned[2],
                what=f"seed {seed}",
            )

        # (c) the pinned residual itself passes.
        gate(columns_of(*pinned))
        for column in range(3):
            # (a) a decision nobody has classified.
            grown = columns_of(*pinned)
            grown[column].append(invented)
            with pytest.raises(AssertionError, match=re.escape(invented)):
                gate(grown)
            if not pinned[column]:
                continue
            # (b) a pinned decision that vanished -- a repair, or a regression.
            vanished = pinned[column][0]
            shrunk = columns_of(*pinned)
            shrunk[column].remove(vanished)
            with pytest.raises(AssertionError, match=re.escape(vanished)):
                gate(shrunk)


def _rule_value_params() -> list[object]:
    return [
        pytest.param(
            _ADAPTERS_BY_GROUP[group], seed, id=f"{_ADAPTERS_BY_GROUP[group].name}-seed{seed}"
        )
        for (group, seed), flips in RULE_VALUE_LEDGER.items()
        if flips
    ]


@requires_kicad_cli
@pytest.mark.parametrize(("adapter", "seed"), _rule_value_params())
def test_same_rule_gate_rejects_the_own_rule_decision(
    adapter: ConsumerAdapter, seed: int, corpus_oracle: CorpusOracle
) -> None:
    """Negative control for a decision the rule value alone was making.

    The "old behaviour" behind a rule-value flip is the consumer *accepting*
    the pair.  This drives the consumer at its own rule value -- unmodified,
    nothing invented -- and hands that reading to the matched-rule gate.  The
    gate must fail and name the flipped pair: proof, on a supported pair
    kicad-cli really flags, that the gate detects the old decision rather
    than passing over an empty denominator.

    It also pins the attribution pair by pair instead of asserting it.  The
    own-rule reading's under-rejections must be exactly the matched-rule
    reading's plus ``RULE_VALUE_LEDGER`` -- so nothing else was ever blamed
    on the rule value -- and every flipped pair must sit below the project
    minimum in kicad-cli's own measurement.
    """
    case = generate_case(seed)
    result = corpus_oracle(seed, refill=False)
    truth = {v.nets for v in result.without_zones()}
    flipped = RULE_VALUE_LEDGER[(adapter.group, seed)]

    own = {v.nets for v in adapter.verdicts(case)}
    own_over = [
        _pair_tag(p)
        for p in _compared_pairs(adapter, case)
        if p.nets in own and p.nets not in truth
    ]
    own_under = [
        _pair_tag(p)
        for p in _compared_pairs(adapter, case)
        if p.nets in truth and p.nets not in own
    ]
    _, retained, _ = _same_rule_disagreements(adapter, case, truth)
    assert sorted(own_under) == sorted([*retained, *flipped]), (
        f"{adapter.name} on seed {seed} under-rejects pairs at its own rule that are "
        "neither retained gaps nor rule-value flips"
    )

    kicad_gap = {v.nets: v.gap_mm for v in result.without_zones()}
    for tag in flipped:
        gap = kicad_gap[_nets_of(tag)]
        assert gap is not None and gap < case.rules.project_clearance, (
            f"{adapter.name} on seed {seed}: {tag} is pinned as a rule-value flip but "
            "kicad-cli does not flag it below the project minimum"
        )

    with pytest.raises(AssertionError) as excinfo:
        assert_same_rule_residual(
            adapter,
            over=own_over,
            under=own_under,
            flips=list(flipped),
            expected_over=SAME_RULE_OVER_LEDGER.get((adapter.group, seed), ()),
            expected_under=SAME_RULE_UNDER_LEDGER.get((adapter.group, seed), ()),
            expected_flips=flipped,
            what=f"seed {seed} driven at the caller's own rule (negative control)",
        )
    for tag in flipped:
        assert tag in str(excinfo.value), (
            f"the gate failed without naming the old decision's pair {tag}"
        )


def _nets_of(tag: str) -> frozenset[str]:
    """The net names in a ledger entry such as ``seg-via ['N1', 'N2'] gap=...``."""
    return frozenset(re.findall(r"'([^']+)'", tag))


def _pairs_tagged(
    adapter: ConsumerAdapter, case: CopperCase, tags: tuple[str, ...]
) -> list[PairIntent]:
    by_tag = {_pair_tag(pair): pair for pair in _compared_pairs(adapter, case)}
    missing = [tag for tag in tags if tag not in by_tag]
    assert not missing, f"{adapter.name}: ledger names pairs the case no longer places: {missing}"
    return [by_tag[tag] for tag in tags]


@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("seed", (1, 2))
def test_search_refinement_retires_the_group_three_over_rejection(seed: int) -> None:
    """Group 3's stated reason for accepting its residual, checked.

    The classification says the refinement rows (groups 4 and 5) re-read the
    exact geometry of a marked cell before refusing it, so at the matched
    rule they flag none of the pairs group 3 over-rejects.  If either
    refinement row started rejecting the same pair, the residual would no
    longer be retired downstream and "costs A* expansions, never a refused
    legal route" would stop being true.  Which pairs those are is already
    settled by the pinned ledger, so no ``kicad-cli`` run is needed here.
    """
    case = generate_case(seed)
    blocked = _ADAPTERS_BY_GROUP[3]
    pairs = _pairs_tagged(blocked, case, SAME_RULE_OVER_LEDGER[(3, seed)])
    for group in (4, 5):
        adapter = _ADAPTERS_BY_GROUP[group]
        flagged = {v.nets for v in adapter.verdicts_at_project_rules(case)}
        for pair in pairs:
            assert pair.nets not in flagged, (
                f"{adapter.name} (group {group}) rejects {pair.kind} {sorted(pair.nets)} on "
                f"seed {seed} at the project minimum -- the pair group 3 over-rejects is no "
                "longer retired by the search-time refinement"
            )


@requires_kicad_cli
@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("seed", (0, 1))
def test_other_gates_reject_the_trace_retry_pad_via_residuals(
    seed: int, corpus_oracle: CorpusOracle
) -> None:
    """The repaired retry and the three independent gates reject the historical gaps (#9924)."""
    case = generate_case(seed)
    result = corpus_oracle(seed, refill=False)
    truth = {v.nets for v in result.without_zones()}
    commit = _ADAPTERS_BY_GROUP[12]
    pairs = _pairs_tagged(commit, case, RULE_VALUE_LEDGER[(12, seed)])
    assert pairs and all(pair.kind == PairKind.PAD_VIA for pair in pairs)
    contexts = {context.pair: context for context in pair_contexts(case)}
    for pair in pairs:
        assert pair.nets in truth, f"kicad-cli no longer flags {_pair_tag(pair)}"
        context = contexts[pair]
        assert isinstance(context.existing, PadSpec) and isinstance(context.candidate, ViaSpec)
        deficit, accepted = pathfinder_via_pad_control(case, context.existing, context.candidate)
        assert deficit > 0, _pair_tag(pair)
        assert not accepted, _pair_tag(pair)
    for group in (1, 12, 13):
        adapter = _ADAPTERS_BY_GROUP[group]
        flagged = {v.nets for v in adapter.verdicts_at_project_rules(case)}
        for pair in pairs:
            assert pair.nets in flagged, (
                f"{adapter.name} (group {group}) no longer flags {_pair_tag(pair)} on seed "
                f"{seed} at the project minimum"
            )


@requires_kicad_cli
def test_via_clearance_retained_gap_is_the_unrotated_pad_box(
    corpus_oracle: CorpusOracle,
) -> None:
    """Group 11's retained gap, isolated to the one thing that causes it.

    Seed 1's ``pad-via`` pair is under-rejected at the matched rule, and the
    classification blames the pad encoding ``Pathfinder`` passes: the pad's
    pre-rotation local size.  Same predicate, same rule value, same two
    objects -- only the encoding varies -- and the two other production
    encodings reject the pair.  So the gap is that encoding and nothing
    else.  The day ``Pathfinder`` passes ``pad_half_extents`` the pinned
    entry vanishes and the gate above says so.
    """
    case = generate_case(1)
    result = corpus_oracle(1, refill=False)
    truth = {v.nets for v in result.without_zones()}
    adapter = _ADAPTERS_BY_GROUP[11]
    (pair,) = _pairs_tagged(adapter, case, SAME_RULE_UNDER_LEDGER[(11, 1)])
    assert pair.nets in truth

    by_net = {obj.net: obj for obj in case.copper_objects}
    nets = net_ids(case)
    pad = next(obj for obj in (by_net[name] for name in pair.nets) if isinstance(obj, PadSpec))
    via = next(obj for obj in (by_net[name] for name in pair.nets) if not isinstance(obj, PadSpec))
    verdicts = {
        form: pad_form_is_clear(
            router_pad(pad, nets),
            router_via(via, nets),  # type: ignore[arg-type]
            case.rules.project_clearance,
            form,
        )
        for form, _description in PAD_FORMS
    }
    assert verdicts == {
        "pathfinder_local_box": True,
        "escape_rotation_aware_box": False,
        "stitcher_disc": False,
    }, f"the retained gap is no longer explained by the pad encoding alone: {verdicts}"


@requires_kicad_cli
def test_via_clearance_conservative_residual_is_the_axis_aligned_pad_box(
    corpus_oracle: CorpusOracle,
) -> None:
    """Group 11's conservative residual, isolated the same way.

    Seed 99's ``pad-via`` pair is over-rejected at the matched rule:
    kicad-cli finds it clean.  Both callers that pass the 5-tuple reject it
    -- ``Pathfinder`` with the pad's pre-rotation box, the escape router with
    its rotation-aware box -- because an axis-aligned box standing for a
    rotated pad over-reaches at its corners; the stitcher's disc accepts it.
    So this half of the residual is the box encoding both callers share, not
    ``Pathfinder``'s alone, and it errs outward -- which is the caller's
    stated reason for accepting it.
    """
    seed = 99
    case = generate_case(seed)
    result = corpus_oracle(seed, refill=False)
    truth = {v.nets for v in result.without_zones()}
    adapter = _ADAPTERS_BY_GROUP[11]
    over, _under, _flips = _same_rule_disagreements(adapter, case, truth)
    pad_via_over = [tag for tag in over if tag.startswith(f"{PairKind.PAD_VIA} ")]
    assert len(pad_via_over) == 1, (
        f"seed {seed} is the control's case, one over-rejected pad-via pair:\n"
        f"over-rejected: {over}\n{result.describe()}"
    )
    (pair,) = _pairs_tagged(adapter, case, (pad_via_over[0],))
    assert pair.nets not in truth

    by_net = {obj.net: obj for obj in case.copper_objects}
    nets = net_ids(case)
    pad = next(obj for obj in (by_net[name] for name in pair.nets) if isinstance(obj, PadSpec))
    via = next(obj for obj in (by_net[name] for name in pair.nets) if not isinstance(obj, PadSpec))
    verdicts = {
        form: pad_form_is_clear(
            router_pad(pad, nets),
            router_via(via, nets),  # type: ignore[arg-type]
            case.rules.project_clearance,
            form,
        )
        for form, _description in PAD_FORMS
    }
    assert verdicts == {
        "pathfinder_local_box": False,
        "escape_rotation_aware_box": False,
        "stitcher_disc": True,
    }, f"the conservative residual is no longer explained by the pad encoding alone: {verdicts}"


@requires_kicad_cli
def test_pairwise_gate_decides_only_what_the_scalar_floor_misses(
    corpus_oracle: CorpusOracle,
) -> None:
    """Group 14's matched-rule control: the gate's silence is its scope.

    Seed 2 has one pair kicad-cli flags, ``seg-seg`` N3-N4 at 0.1752 mm.  At
    the project minimum with every net at 0 V the pairwise replay reports
    nothing: the pair's requirement equals the scalar floor, so
    ``find_pairwise_violations`` skips it.  Raise N3 to 100 V -- through the
    table's own public builder, so the widened value is the consumer's and
    not a number invented here -- and the same board, same replay reports
    the pair with the requirement it widened to, while the un-widened N1-N2
    pair stays unreported.  The gate decides when it is consulted; on an
    all-0 V board it is consulted for nothing.
    """
    seed = 2
    case = generate_case(seed)
    result = corpus_oracle(seed, refill=False)
    truth = {v.nets for v in result.without_zones()}
    assert truth == {frozenset({"N3", "N4"})}, (
        f"seed {seed} is the control's simplest case, one flagged pair:\n{result.describe()}"
    )

    adapter = _ADAPTERS_BY_GROUP[14]
    assert not adapter.verdicts_at_project_rules(case), (
        "the pairwise gate reported something on an all-0 V board -- it is no "
        "longer an HV-widening overlay, and its classification is stale"
    )

    volts = {**dict.fromkeys(case.nets, 0.0), "N3": 100.0}
    widened = {v.nets: v for v in adapter.verdicts_at_voltages(case, volts)}
    assert set(widened) == {frozenset({"N3", "N4"})}, (
        f"with N3 raised the replay decided {sorted(map(sorted, widened))}, "
        "not the one widened pair"
    )
    (verdict,) = widened.values()
    assert verdict.required_mm is not None
    assert verdict.required_mm > case.rules.project_clearance, (
        "the reported requirement is the scalar floor, not the widened one"
    )
    assert verdict.gap_mm == pytest.approx(0.1752, abs=0.001)


# ---------------------------------------------------------------------------
# The named pad-pad control for group 19 (Issue #9922)
# ---------------------------------------------------------------------------
#
# The corpus places every ``pad-pad`` pair along the first pad's local +X
# axis, the one direction in which the consumer's disc of ``max(w, h) / 2` is
# the pad's true support -- and on ``CI_SEEDS`` it places no ``pad-pad`` pair
# at all, so the advisory gate above is vacuous for this group.  This case is
# hand-authored to do what the corpus cannot: it holds three pairs of the
# *same* two pads, differing only in which way the pads face and in the gap.
#
#   pair             faces across      gap    kicad-cli   disc model
#   DISC_OFF_*       short sides       0.22   clean       -0.18 mm  -> rejects
#   DISC_ON_*        long sides        0.22   clean       0.22 mm  -> accepts
#   DISC_RULE_*      long sides        0.18   flags       0.18 mm  -> 0.15: accepts, 0.20: rejects
#
# The first two differ only in axis: same pads, same gap, same rule value, so
# the rejection is the disc's and nothing else.  The third is the corpus
# behaviour in one pair -- a rule value, flagged as soon as the rule is the
# project's.

_DISC_PAD = pad_shape("Pad_Rect")

DISC_OFF_AXIS = "DISC_OFF"
DISC_ON_AXIS = "DISC_ON"
DISC_RULE = "DISC_RULE"


def _facing_pair(
    prefix: str,
    centre: tuple[float, float],
    *,
    across_long_side: bool,
    gap: float,
    first: int,
) -> tuple[PadSpec, PadSpec, float]:
    """Two ``Pad_Rect`` footprints facing each other across ``gap`` mm.

    Across the long side the pads are side by side along X, so the centre
    distance is ``w + gap``; across the short side they are stacked along Y,
    so it is ``h + gap``.  Both pads are unrotated.
    """
    width, height = _DISC_PAD.size
    span = (width if across_long_side else height) + gap
    dx, dy = (span / 2.0, 0.0) if across_long_side else (0.0, span / 2.0)
    pad_a = PadSpec(
        net=f"{prefix}_A",
        reference=f"P{first}",
        footprint=_DISC_PAD.name,
        x=centre[0] - dx,
        y=centre[1] - dy,
        rotation=0.0,
    )
    pad_b = PadSpec(
        net=f"{prefix}_B",
        reference=f"P{first + 1}",
        footprint=_DISC_PAD.name,
        x=centre[0] + dx,
        y=centre[1] + dy,
        rotation=0.0,
    )
    return pad_a, pad_b, gap


def _disc_envelope_control() -> CopperCase:
    """The named control ``drc-cpp-disc-off-axis`` (Issue #9922)."""
    off_a, off_b, off_gap = _facing_pair(
        DISC_OFF_AXIS, (10.0, 10.0), across_long_side=False, gap=0.22, first=1
    )
    on_a, on_b, on_gap = _facing_pair(
        DISC_ON_AXIS, (20.0, 10.0), across_long_side=True, gap=0.22, first=3
    )
    rule_a, rule_b, rule_gap = _facing_pair(
        DISC_RULE, (30.0, 10.0), across_long_side=True, gap=0.18, first=5
    )
    required = DEFAULT_RULES.project_clearance
    return CopperCase(
        seed=9922,
        name="drc-cpp-disc-off-axis",
        width=40.0,
        height=20.0,
        layers=2,
        rules=DEFAULT_RULES,
        pads=(off_a, off_b, on_a, on_b, rule_a, rule_b),
        pairs=(
            PairIntent(
                kind=PairKind.PAD_PAD,
                net_a=off_a.net,
                net_b=off_b.net,
                target_gap_mm=off_gap,
                required_mm=required,
                expect_violation=False,
            ),
            PairIntent(
                kind=PairKind.PAD_PAD,
                net_a=on_a.net,
                net_b=on_b.net,
                target_gap_mm=on_gap,
                required_mm=required,
                expect_violation=False,
            ),
            PairIntent(
                kind=PairKind.PAD_PAD,
                net_a=rule_a.net,
                net_b=rule_b.net,
                target_gap_mm=rule_gap,
                required_mm=required,
                expect_violation=True,
            ),
        ),
        notes=(
            "Issue #9922: the placement DRC models every pad as a disc of "
            "max(w, h) / 2. Two 1.2 x 0.8 mm rect pads 0.22 mm apart across "
            "their short sides are clean for kicad-cli and rejected by the "
            "disc (-0.18 mm); the same pads 0.22 mm apart across their long "
            "sides are accepted by both; 0.18 mm across the long sides is a "
            "rule value."
        ),
    )


@requires_kicad_cli
@requires_adapter(_ADAPTERS_BY_GROUP[19])
def test_drc_cpp_disc_envelope_control(tmp_path: Path) -> None:
    """Group 19's named pad-pad control, at the same rule.

    Group 19's corpus gate is vacuous on ``CI_SEEDS`` (no ``pad-pad`` pair is
    placed), so this is its evidence.  Ground truth is asserted first and
    from the real tool: only the 0.18 mm pair is flagged.  Then the consumer
    at both rule values, through the same matched-rule gate every other
    advisory row is held to -- so the conservative disc is *pinned* here, as
    the exact pair it over-rejects, rather than described.

    The on-axis 0.22 mm pair is the negative control: identical pads, gap and
    threshold, accepted.  It is what shows the off-axis rejection is the
    envelope's approximation and not the rule value or a broken predicate.
    """
    assert _DISC_PAD.size == (1.2, 0.8), "the control's quoted arithmetic assumes this pad"
    case = _disc_envelope_control()
    board = write_case(case, tmp_path)
    result = run_oracle(board.pcb_path, refill=False, work_dir=tmp_path)
    truth = {v.nets for v in result.without_zones()}

    assert truth == {frozenset({f"{DISC_RULE}_A", f"{DISC_RULE}_B"})}, (
        "kicad-cli's verdict on the disc control changed:\n" + result.describe()
    )
    assert not result.dropped, f"unmapped clearance rows: {result.dropped}"

    adapter = _ADAPTERS_BY_GROUP[19]
    over, under, flips = _same_rule_disagreements(adapter, case, truth)
    assert_same_rule_residual(
        adapter,
        over=over,
        under=under,
        flips=flips,
        expected_over=(f"pad-pad ['{DISC_OFF_AXIS}_A', '{DISC_OFF_AXIS}_B'] gap=0.2200",),
        expected_under=(),
        expected_flips=(f"pad-pad ['{DISC_RULE}_A', '{DISC_RULE}_B'] gap=0.1800",),
        what="the drc-cpp-disc-off-axis control",
        context=result.describe(),
    )

    # The negative control, stated on its own: the on-axis pair is decided
    # the same way kicad-cli decides it, at both rule values.
    on_axis = frozenset({f"{DISC_ON_AXIS}_A", f"{DISC_ON_AXIS}_B"})
    assert on_axis not in {v.nets for v in adapter.verdicts(case)}
    assert on_axis not in {v.nets for v in adapter.verdicts_at_project_rules(case)}

    # The arithmetic the classification quotes, in closed form.
    width, height = _DISC_PAD.size
    radius = max(width, height) / 2.0
    assert (height + 0.22) - 2.0 * radius == pytest.approx(-0.18)
    assert (width + 0.22) - 2.0 * radius == pytest.approx(0.22)
    assert (width + 0.18) - 2.0 * radius == pytest.approx(0.18)
