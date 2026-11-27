"""The conformance document's shape is an acceptance criterion in itself.

Epic #5509 section 1 inventories nineteen implementation groups that carry
their own clearance arithmetic. The table must show **all nineteen** -- groups
with no adapter read ``not measured``, never disappear. An omitted row reads
as "this one agrees", and we do not know that; that gap is precisely how a
stream of one-site "router: preserve physical X" fixes stayed invisible as a
class.

These tests pin the shape (rows, columns, markers) against a stub measurement,
so the contract holds before any adapter exists and keeps holding after.

No kicad-cli needed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from pathlib import Path

import pytest

from tests.conformance.report import (
    ADAPTERS,
    ADVISORY_GROUPS,
    CLASSIFICATION_COLUMNS,
    CLASSIFICATION_TABLE_BEGIN,
    CLASSIFICATION_TABLE_END,
    CLASSIFICATIONS,
    CONSERVATIVE,
    DISPOSITIONS,
    DOC_PATH,
    GROUPS,
    KERNEL_GROUP,
    KERNEL_TABLE_BEGIN,
    KERNEL_TABLE_END,
    MIGRATED_GROUPS,
    NOT_MEASURED,
    NOT_MEASURED_REASONS,
    NOTES,
    OUT_OF_SCOPE,
    REFINEMENT_GATED_GROUPS,
    RETAINED_GAP,
    RULE_VALUE,
    TABLE_BEGIN,
    TABLE_COLUMNS,
    TABLE_END,
    AdapterMeasurement,
    CorpusStats,
    SameRuleReading,
    adapter_for_group,
    classification_errors,
    main,
    render_advisory_table,
    render_document,
    render_kernel_table,
    render_table,
)

_STUB_STATS = CorpusStats(
    seed_range="0-23",
    case_count=24,
    pair_count=72,
    boundary_count=0,
    flagged_pair_count=31,
    fill_states=("as-is", "refilled"),
    oracle_runs=48,
)


def _table_rows(markdown: str) -> list[str]:
    body = markdown.split(TABLE_BEGIN, 1)[1].split(TABLE_END, 1)[0]
    lines = [line for line in body.strip().splitlines() if line.startswith("|")]
    # Drop the header and the separator.
    return lines[2:]


def test_groups_are_the_nineteen_from_the_epic() -> None:
    assert len(GROUPS) == 19
    assert [g.number for g in GROUPS] == list(range(1, 20))
    assert len({g.label for g in GROUPS}) == 19


def test_table_has_one_row_per_group() -> None:
    rows = _table_rows(render_table(_STUB_STATS))
    assert len(rows) == len(GROUPS) == 19


def test_table_columns_match_the_specified_seven() -> None:
    """Six data columns plus ``notes``.

    ``notes`` is not decoration: Epic #5509 Phase 1c's acceptance criterion is
    that every row is *either* measured *or* states its reason, and the column
    is where that reason lives.  Without it, "not measured" is
    indistinguishable from "nobody looked" -- exactly the invisibility the
    document exists to remove.
    """
    table = render_table(_STUB_STATS)
    header = table.splitlines()[1]
    columns = [c.strip() for c in header.strip("|").split("|")]
    assert columns == list(TABLE_COLUMNS)
    assert columns == [
        "adapter",
        "corpus (seed range)",
        "over-reject %",
        "under-reject %",
        "boundary",
        "fill state",
        "notes",
    ]
    for row in _table_rows(table):
        assert len(row.strip("|").split("|")) == len(TABLE_COLUMNS)


def test_groups_without_an_adapter_say_not_measured() -> None:
    """The unmeasured groups are named explicitly, not inferred."""
    measured = {
        12: AdapterMeasurement(
            group=12,
            adapter_name="grid_py",
            pairs_compared=72,
            over_reject=3,
            under_reject=5,
            boundary=0,
            fill_states=("as-is", "refilled"),
        )
    }
    rows = _table_rows(render_table(_STUB_STATS, measured))
    by_group = {int(re.match(r"\|\s*(\d+)\.", row).group(1)): row for row in rows}  # type: ignore[union-attr]

    assert "grid_py" in by_group[12]
    assert "4.2% (3/72)" in by_group[12]  # over-reject
    assert "6.9% (5/72)" in by_group[12]  # under-reject

    for group in (2, 3, 5, 6, 7, 8, 9, 10, 11, 14, 15, 16, 17, 19):
        # Five data columns read ``not measured``; the sixth (notes) carries
        # the reason, which is why the count is five and not six.
        assert by_group[group].count(NOT_MEASURED) >= 5, (
            f"group {group} must read '{NOT_MEASURED}' in all five data columns"
        )
    # Group 18 has no adapter in the stub either, and must still be present.
    assert NOT_MEASURED in by_group[18]


def test_every_unmeasured_row_states_a_reason() -> None:
    """No row may read a bare ``not measured``.

    The render-time half of the criterion: whatever the cause -- no adapter
    wired at all, or one wired but unavailable on this machine -- the
    ``notes`` cell is non-empty and is not itself the string ``not measured``.
    """
    rows = _table_rows(render_table(_STUB_STATS))
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        note = cells[-1]
        assert note, f"row without a stated reason: {row}"
        assert note != NOT_MEASURED, f"row whose reason is itself the placeholder: {row}"


def test_every_unwired_group_states_its_reason() -> None:
    """The *static* half: an unwired group needs an explicit, authored reason.

    Deliberately independent of any measurement, so it fails on a laptop with
    no KiCad and no compiled extension -- i.e. wherever a new unwired group
    would be introduced.  ``not_measured_reason``'s machine-fact fallback is
    only legitimate for a group that *has* an adapter; a group with none must
    justify itself in ``NOT_MEASURED_REASONS``.
    """
    unwired = [g.number for g in GROUPS if adapter_for_group(g.number) is None]
    missing = [n for n in unwired if n not in NOT_MEASURED_REASONS]
    assert not missing, (
        f"groups {missing} have no adapter and no entry in NOT_MEASURED_REASONS; "
        'a bare "not measured" is indistinguishable from nobody having looked'
    )
    # Every recorded reason must classify itself, so a reader can tell an
    # unreachable consumer from a deferred one at a glance.
    for number, reason in NOT_MEASURED_REASONS.items():
        assert any(
            kind in reason for kind in ("unexposed", "needs engine plumbing", "out of corpus scope")
        ), f"group {number}'s reason does not classify the gap: {reason}"


def test_no_consumer_is_unexposed_any_more() -> None:
    """The Phase 1c criterion, now discharged (Epic #5509 Phase 3c, #5662).

    Phase 1c permitted exactly one *unexposed* gap: ``rail_clear`` was a
    lambda inside ``coupled_pathfinder.cpp``'s search loop, so no binding
    could reach it without adding C++ -- which that phase forbade.  Phase 3c
    is the phase that migrates group 7 onto the clearance kernel, and
    promoting the lambda to a bound method was part of doing so; the gap is
    closed and ``adapters/coupled.py`` measures the group.

    The assertion is kept, inverted, because it is the one that would catch a
    *reintroduction*: a consumer becoming unreachable from Python again means
    a binding regressed, and this phase has already paid to prove that does
    not have to be tolerated.
    """
    unexposed = [n for n, reason in NOT_MEASURED_REASONS.items() if "unexposed" in reason]
    assert unexposed == [], (
        "no consumer group may be unexposed: group 7 was the last one and "
        f"Phase 3c bound it, but these are unreachable again: {unexposed}"
    )


def test_adapters_are_registered_for_the_wired_groups() -> None:
    """``ADAPTERS`` wires one consumer per measured group, and no more.

    Succeeds ``test_phase_1a_registers_no_adapters`` (#5513 / PR #5532), which
    pinned ``ADAPTERS == ()`` while only the truth side existed, and the
    five-group pin that replaced it in #5533.  Each was a *phase* assertion:
    Phase 1c wired fourteen more consumer groups plus the Phase 1b kernel,
    leaving group 7 -- an unexposed C++ lambda -- as the sole stated gap, and
    Phase 3c (#5662) closed that one by binding ``rail_clear``.  Nineteen of
    nineteen.

    The invariant the original test really protected -- that the harness does
    not import the code it measures -- is unchanged and still enforced, one
    test down, by ``test_truth_side_does_not_import_the_code_it_measures``.
    """
    consumer_groups = {adapter.group for adapter in ADAPTERS} - {KERNEL_GROUP}
    assert consumer_groups == set(range(1, 20))
    assert consumer_groups | set(NOT_MEASURED_REASONS) == {g.number for g in GROUPS}, (
        "every group is either wired or has a stated reason -- no group may be both or neither"
    )
    assert [adapter.group for adapter in ADAPTERS] == sorted(a.group for a in ADAPTERS), (
        "keep ADAPTERS in group order so the table's measured rows read top-to-bottom"
    )
    assert len({adapter.name for adapter in ADAPTERS}) == len(ADAPTERS)


def test_the_kernel_renders_in_its_own_table() -> None:
    """The kernel is measured, but it is not a twentieth group row.

    Mixing it into the group table would break the one invariant that makes
    the table auditable -- exactly nineteen rows, one per epic group -- and
    would read as if the kernel were another consumer to be migrated rather
    than the thing they are migrated *onto*.
    """
    measured = {
        KERNEL_GROUP: AdapterMeasurement(
            group=KERNEL_GROUP,
            adapter_name="clearance_kernel",
            pairs_compared=596,
            over_reject=0,
            under_reject=0,
            boundary=0,
            fill_states=("as-is", "refilled"),
        )
    }
    assert len(_table_rows(render_table(_STUB_STATS, measured))) == 19

    kernel = render_kernel_table(_STUB_STATS, measured)
    assert KERNEL_TABLE_BEGIN in kernel and KERNEL_TABLE_END in kernel
    body = kernel.split(KERNEL_TABLE_BEGIN, 1)[1].split(KERNEL_TABLE_END, 1)[0]
    rows = [line for line in body.strip().splitlines() if line.startswith("|")][2:]
    assert len(rows) == 1
    assert "clearance_kernel" in rows[0]
    assert "0.0% (0/596)" in rows[0]

    doc = render_document(_STUB_STATS, measured)
    assert KERNEL_TABLE_BEGIN in doc
    assert NOTES[KERNEL_GROUP].split(":")[0] in doc


def test_truth_side_does_not_import_the_code_it_measures() -> None:
    """The oracle must not depend on any consumer, checked mechanically.

    A conformance oracle that imports the implementations under test can drift
    towards them: a shared constant, a shared helper, a shared bug. The rule is
    that the truth side sees only KiCad's answer and the case it generated.

    This is the split line for the adapters too -- an adapter module is
    exactly the place where ``import kicad_tools.router`` becomes legitimate,
    so this test is scoped to the harness modules and deliberately exempts the
    ``adapters/`` package.  ``report.py`` imports the adapter *modules* (to
    populate ``ADAPTERS``) and that is fine: the forbidden thing is a harness
    module reaching into a consumer directly, not a harness module composing
    the wrappers.
    """
    package = Path(__file__).resolve().parent
    forbidden = ("kicad_tools.router", "kicad_tools.validate")
    offenders: list[str] = []

    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in forbidden):
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")

    assert not offenders, (
        "the conformance harness must not import the consumers it measures; "
        f"move these into an adapter module: {offenders}"
    )


def test_document_mentions_every_group_and_both_failure_directions() -> None:
    doc = render_document(_STUB_STATS)
    assert doc.count(TABLE_BEGIN) == 1 and doc.count(TABLE_END) == 1
    assert len(_table_rows(doc)) == 19
    for group in GROUPS:
        assert group.label in doc
    assert "over-reject" in doc and "under-reject" in doc
    assert "report-only" in doc
    assert "Seed range: `0-23`" in doc
    assert "24 generated cases" in doc


def test_committed_document_is_up_to_date_in_shape() -> None:
    """The checked-in doc has the same 19 rows and columns as the renderer.

    Shape only, deliberately: the numbers are regenerated out-of-band over
    seeds 0-199 (~400 kicad-cli invocations) and this test runs on every PR,
    including on machines with no KiCad.  Their *reproducibility* is what
    ``test_corpus.test_report_regenerates_byte_identically`` asserts.
    """
    assert DOC_PATH.exists(), f"missing {DOC_PATH}"
    doc = DOC_PATH.read_text(encoding="utf-8")
    rows = _table_rows(doc)
    assert len(rows) == 19
    numbers = [int(re.match(r"\|\s*(\d+)\.", row).group(1)) for row in rows]  # type: ignore[union-attr]
    assert numbers == list(range(1, 20))

    header = doc.split(TABLE_BEGIN, 1)[1].strip().splitlines()[0]
    assert [c.strip() for c in header.strip("|").split("|")] == list(TABLE_COLUMNS), (
        "the committed table's columns have drifted from the renderer; "
        "regenerate with `uv run python -m tests.conformance.report --seeds 0-199`"
    )
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        assert len(cells) == len(TABLE_COLUMNS)
        assert cells[-1], f"committed row with an empty notes cell: {row}"


def test_committed_document_carries_the_kernel_control_row() -> None:
    """The kernel's 0 %/0 % row is in the committed document, not just in code.

    Epic #5509 Phase 1c's central claim -- that one exact-geometry model *can*
    agree with kicad-cli everywhere -- is only evidence if it is written down
    where a reader of the epic will find it.  A regeneration that silently
    dropped the section (an adapter that stopped being ``available()`` on the
    generating machine, say) would leave the claim unsupported while every
    other check stayed green.
    """
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert KERNEL_TABLE_BEGIN in doc and KERNEL_TABLE_END in doc, (
        "the committed document has no kernel section; regenerate it on a "
        "machine where the `clearance_kernel` adapter is available"
    )
    body = doc.split(KERNEL_TABLE_BEGIN, 1)[1].split(KERNEL_TABLE_END, 1)[0]
    rows = [line for line in body.strip().splitlines() if line.startswith("|")][2:]
    assert len(rows) == 1, f"expected exactly one kernel row, got {len(rows)}"
    (row,) = rows
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    over, under = cells[2], cells[3]
    assert over.startswith("0.0%"), (
        f"the kernel over-rejects in the committed table ({over}) -- that is a "
        "Phase 1b bug, not a Phase 1c finding: capture a fixture and fix the "
        "kernel through tests/router/test_clearance_kernel_parity.py"
    )
    assert under.startswith("0.0%"), (
        f"the kernel under-rejects in the committed table ({under}) -- see above"
    )


def test_report_refuses_to_write_without_ground_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table generated with no kicad-cli would read as "zero disagreement".

    That is a confident wrong answer, so the generator exits non-zero with the
    machine-readable ``kicad_cli_absent`` reason and writes nothing.
    """
    monkeypatch.setattr("tests.conformance.report.kicad_cli_available", lambda: False)
    out = tmp_path / "clearance-conformance.md"
    rc = main(["--seeds", "0-1", "--out", str(out)])
    assert rc != 0
    assert not out.exists()


# ---------------------------------------------------------------------------
# Issue #9922 -- the advisory classification is part of the document's shape
# ---------------------------------------------------------------------------
#
# Thirteen of the nineteen rows are gated.  The other six are advisory, and
# before this section the document said only *that* they disagreed -- never
# whether the disagreement was the caller's rule value or its arithmetic,
# which are the acceptance audit's E1 / C3 items and call for opposite
# responses.  These tests pin the classification's shape the way the ones
# above pin the table's: every advisory group has a row stating its caller,
# pair kinds, both rule values and both readings; a row that was not measured
# says so; and a classification its own measurement refuses is not published.


def _classification_rows(markdown: str) -> list[str]:
    body = markdown.split(CLASSIFICATION_TABLE_BEGIN, 1)[1].split(CLASSIFICATION_TABLE_END, 1)[0]
    return [line for line in body.strip().splitlines() if line.startswith("|")][2:]


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _row_by_group(markdown: str, number: int) -> list[str]:
    (row,) = [r for r in _classification_rows(markdown) if re.match(rf"\|\s*{number}\.", r)]
    return _cells(row)


def _reading(
    group: int,
    *,
    compared: int,
    own_over: int = 0,
    own_under: int = 0,
    at_over: int = 0,
    at_under: int = 0,
    flips: int = 0,
    kinds: tuple[tuple[str, int], ...] | None = None,
    trace: tuple[str, ...] = (),
) -> SameRuleReading:
    """A stub matched-rule reading for one group, in whatever state a test needs."""
    adapter = adapter_for_group(group)
    assert adapter is not None
    name = adapter.name
    fill = ("as-is", "refilled")
    return SameRuleReading(
        group=group,
        adapter_name=name,
        own=AdapterMeasurement(
            group=group,
            adapter_name=name,
            pairs_compared=compared,
            over_reject=own_over,
            under_reject=own_under,
            fill_states=fill,
        ),
        at_project=AdapterMeasurement(
            group=group,
            adapter_name=name,
            pairs_compared=compared,
            over_reject=at_over,
            under_reject=at_under,
            fill_states=fill,
        ),
        compared_by_kind=kinds if kinds is not None else (("seg-seg", compared),),
        rule_value_flips=flips,
        trace=trace,
    )


def test_advisory_groups_are_the_rows_no_gate_has_claimed() -> None:
    """Advisory is the complement of the gate registries, never a typed list.

    Derived in ``report`` and re-derived here, so the two cannot disagree:
    a group is advisory exactly while it is neither migrated nor
    refinement-gated, and every one of the nineteen is one or the other.
    """
    assert {g.number for g in GROUPS} - MIGRATED_GROUPS - set(
        REFINEMENT_GATED_GROUPS
    ) == ADVISORY_GROUPS
    assert frozenset({3, 11, 12, 13, 14, 19}) == ADVISORY_GROUPS, "Issue #9922 classified these six"
    assert not ADVISORY_GROUPS & MIGRATED_GROUPS
    assert {g.number for g in GROUPS} == ADVISORY_GROUPS | MIGRATED_GROUPS


def test_every_advisory_group_is_classified_and_nothing_else_is() -> None:
    """One authored disposition per advisory row, and none left behind by a gated one."""
    assert set(CLASSIFICATIONS) == ADVISORY_GROUPS
    for number, classification in CLASSIFICATIONS.items():
        assert classification.group == number
        for attribute in ("caller", "rule", "retained"):
            assert getattr(classification, attribute).strip(), (
                f"group {number}'s classification states no {attribute}"
            )
        assert classification.dispositions, f"group {number} states no disposition"
        unknown = classification.dispositions - set(DISPOSITIONS)
        assert not unknown, f"group {number} uses tokens outside the vocabulary: {sorted(unknown)}"


def test_the_vocabulary_separates_a_rule_value_from_arithmetic() -> None:
    """Exactly one token means "rule value"; every other token is a same-rule residual.

    That one-to-many split is the section's whole purpose, so it is asserted
    rather than implied by the prose.
    """
    assert set(DISPOSITIONS) == {RULE_VALUE, CONSERVATIVE, RETAINED_GAP, OUT_OF_SCOPE}
    assert set(DISPOSITIONS) - {RULE_VALUE} == {CONSERVATIVE, RETAINED_GAP, OUT_OF_SCOPE}
    assert all(DISPOSITIONS[token].strip() for token in DISPOSITIONS)


def test_every_advisory_adapter_is_drivable_at_the_project_rule() -> None:
    """The matched-rule reading exists before any measurement needs it.

    Without ``verdicts_at_project_rules`` a row cannot be split into a rule
    value and arithmetic at all, and its classification would be prose with
    nothing behind it.
    """
    for group in sorted(ADVISORY_GROUPS):
        adapter = adapter_for_group(group)
        assert adapter is not None, f"group {group} is advisory but has no adapter"
        assert callable(getattr(adapter, "verdicts_at_project_rules", None)), (
            f"{adapter.name} (group {group}) cannot be driven at kicad-cli's own rule "
            "value, so its disagreements cannot be separated into a rule value "
            "and arithmetic"
        )


def test_classification_table_has_one_row_per_advisory_group() -> None:
    table = render_advisory_table()
    rows = _classification_rows(table)
    assert [int(re.match(r"\|\s*(\d+)\.", row).group(1)) for row in rows] == sorted(  # type: ignore[union-attr]
        ADVISORY_GROUPS
    )
    header = table.splitlines()[1]
    assert [c.strip() for c in header.strip("|").split("|")] == list(CLASSIFICATION_COLUMNS)
    for row in rows:
        assert len(_cells(row)) == len(CLASSIFICATION_COLUMNS)


def test_an_unmeasured_advisory_row_never_reads_as_agreement() -> None:
    """No reading: the measured cells say ``not measured``; the authored half still stands.

    A zero over an empty denominator is the confident wrong answer this
    harness refuses everywhere else; the classification is not where it
    starts.
    """
    for group in sorted(ADVISORY_GROUPS):
        cells = _row_by_group(render_advisory_table({}), group)
        assert NOT_MEASURED in cells[0]
        assert cells[2] == NOT_MEASURED, f"group {group}: pair kinds"
        assert cells[4] == NOT_MEASURED, f"group {group}: own rule"
        assert cells[5] == NOT_MEASURED, f"group {group}: matched rule"
        classification = CLASSIFICATIONS[group]
        assert classification.caller in cells[1], f"group {group}: production caller"
        assert classification.rule in cells[3], f"group {group}: rule values"


def test_a_measured_advisory_row_states_caller_kinds_rules_and_both_readings() -> None:
    reading = _reading(19, compared=8, own_under=4, at_over=2, flips=4, kinds=(("pad-pad", 8),))
    cells = _row_by_group(render_advisory_table({19: reading}), 19)
    assert "`drc_cpp`" in cells[0]
    assert CLASSIFICATIONS[19].caller in cells[1]
    assert cells[2] == "`pad-pad` 8", "the supported-pair denominator, per kind"
    assert CLASSIFICATIONS[19].rule in cells[3]
    assert cells[4] == "0/8 / 4/8", "own rule: over / under"
    assert cells[5] == "2/8 / 0/8", "project minimum: over / under"
    assert cells[6] == "rule value, conservative approximation", (
        "dispositions read in vocabulary order -- rule value first -- not alphabetically"
    )


def test_an_empty_denominator_reads_not_measured_not_zero() -> None:
    """``0/0`` is not agreement.  Group 19 over a short seed range is the real case."""
    cells = _row_by_group(render_advisory_table({19: _reading(19, compared=0)}), 19)
    assert cells[2] == NOT_MEASURED
    assert cells[4] == f"{NOT_MEASURED} / {NOT_MEASURED}"
    assert cells[5] == f"{NOT_MEASURED} / {NOT_MEASURED}"


def test_the_table_refuses_a_classification_its_measurement_contradicts() -> None:
    """Group 13 is classified a pure rule value; a matched-rule under-rejection refutes that."""
    contradicting = _reading(13, compared=16, own_under=6, at_under=2, flips=4)
    with pytest.raises(AssertionError, match="group 13's classification is contradicted"):
        render_advisory_table({13: contradicting})


@pytest.mark.parametrize(
    ("dispositions", "at_over", "at_under", "flips", "guilty"),
    [
        (frozenset({RULE_VALUE}), 0, 0, 4, ()),
        (frozenset({RULE_VALUE}), 0, 1, 0, ("none of which is arithmetic",)),
        (frozenset({RULE_VALUE}), 2, 0, 0, ("not classified 'conservative approximation'",)),
        (frozenset({CONSERVATIVE}), 2, 0, 4, ("not classified 'rule value'",)),
        (frozenset({CONSERVATIVE}), 2, 0, 0, ()),
        (frozenset({RETAINED_GAP}), 0, 1, 0, ()),
        (frozenset({OUT_OF_SCOPE}), 0, 9, 0, ()),
        (frozenset({OUT_OF_SCOPE}), 1, 9, 0, ("not classified 'conservative approximation'",)),
        # A token with nothing behind it is not this function's job: a corpus
        # cannot show every disposition (group 19's disc is off-axis only).
        (frozenset(), 0, 0, 0, ()),
    ],
)
def test_classification_errors_convict_only_on_evidence(
    dispositions: frozenset[str],
    at_over: int,
    at_under: int,
    flips: int,
    guilty: tuple[str, ...],
) -> None:
    at_project = AdapterMeasurement(
        group=0, adapter_name="stub", over_reject=at_over, under_reject=at_under
    )
    errors = classification_errors(dispositions, at_project, flips)
    assert len(errors) == len(guilty)
    for error, fragment in zip(errors, guilty, strict=True):
        assert fragment in error


def test_document_carries_the_classification_with_its_provenance() -> None:
    doc = render_document(_STUB_STATS)
    assert doc.count(CLASSIFICATION_TABLE_BEGIN) == 1
    assert doc.count(CLASSIFICATION_TABLE_END) == 1
    assert doc.count(TABLE_BEGIN) == 1, "the nineteen-row table is still there, once"
    assert len(_classification_rows(doc)) == len(ADVISORY_GROUPS)
    assert "Issue #9922" in doc
    assert (
        "Still **advisory** (report-only): "
        + ", ".join(f"group {n}" for n in sorted(ADVISORY_GROUPS))
        in doc
    ), "the introduction must say which rows are not gated, and where they are classified"

    assert "- kicad-cli: `" in doc, (
        "the counts are evidence only for the release that produced them"
    )
    assert "- Fill states: as-is, refilled" in doc
    assert "- Seed range: `0-23`" in doc
    assert "Named pairs: seeds 0, 1, 2" in doc, "the named pairs are the merge gate's own seeds"

    for token, definition in DISPOSITIONS.items():
        assert f"- `{token}` -- {definition}" in doc
    for group in ADVISORY_GROUPS:
        assert CLASSIFICATIONS[group].retained in doc

    kernel = doc.index("## The kernel, measured as a consumer")
    advisory = doc.index("## Advisory groups at a matched rule")
    assert kernel < advisory < doc.index("## Corpus"), (
        "the classification belongs with the measurements, before the corpus provenance"
    )


def test_a_range_without_the_trace_seeds_says_so() -> None:
    """No extra ``kicad-cli`` runs are launched to manufacture a trace."""
    narrow = render_document(replace(_STUB_STATS, seed_range="40-63"))
    assert "none of seeds 0-2 is inside the measured range" in narrow
    assert "Named pairs: seeds 0, 1, 2" not in narrow


def test_named_pairs_follow_the_reading_into_the_document() -> None:
    """The trace is rendered as it was measured, one line per pair."""
    line = (
        "seed 1: `pad-via` ['N6', 'N7'] -- accepted at both rule values "
        "(kicad-cli flags it at 0.1933 mm)"
    )
    reading = _reading(11, compared=10, own_under=6, at_under=2, flips=4, trace=(line,))
    doc = render_document(_STUB_STATS, None, {11: reading})
    assert f"  - {line}" in doc


def test_committed_document_carries_a_measured_classification() -> None:
    """The committed classification is measured on every row, on the pinned toolchain.

    The counts are shape, like the group table's, and are not re-derived
    here.  Two things are content and are asserted.  No row may read ``not
    measured``: a regeneration on a machine without the native extensions
    would blank groups 3 and 13 and the document would say so only in a
    cell.  And the ``kicad-cli`` stamp must be CI's pinned release, because
    the numbers are evidence only for the release that produced them -- a
    pin bump therefore has to regenerate this document in the same PR.
    """
    from kicad_tools.cli.kicad_toolchain import PINNED_KICAD_VERSION

    doc = DOC_PATH.read_text(encoding="utf-8")
    assert CLASSIFICATION_TABLE_BEGIN in doc and CLASSIFICATION_TABLE_END in doc, (
        "the committed document has no classification section; regenerate it on a "
        "machine where every advisory adapter is available"
    )
    rows = _classification_rows(doc)
    assert [int(re.match(r"\|\s*(\d+)\.", row).group(1)) for row in rows] == sorted(  # type: ignore[union-attr]
        ADVISORY_GROUPS
    )
    counted = r"\d+/\d+ / \d+/\d+"
    for row in rows:
        cells = _cells(row)
        assert len(cells) == len(CLASSIFICATION_COLUMNS)
        number = cells[0].split(".", 1)[0]
        assert NOT_MEASURED not in cells[2], f"group {number}: pair kinds not measured"
        assert re.fullmatch(counted, cells[4]), f"group {number}: own rule reads {cells[4]!r}"
        assert re.fullmatch(counted, cells[5]), f"group {number}: matched rule reads {cells[5]!r}"
    assert f"- kicad-cli: `{PINNED_KICAD_VERSION}`" in doc, (
        "the committed classification was not measured on CI's pinned KiCad; "
        "regenerate it inside the pinned image (KCT_KICAD_DOCKER=1)"
    )
