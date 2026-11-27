"""Doc/code coupling for the current-paths sidecar schema (Issue #10396).

``docs/reference/cli.md`` is the schema reference for ``--current-paths``.
PR #10374 added the sidecar's ``cases`` key (Issue #10361) without extending
that reference, and the parser that preceded it accepted the object form
while silently ignoring ``cases`` -- so a published example being valid JSON
proves nothing about it. This module keeps the published examples honest
against the shipped parser and checker:

* the operating-case sidecar is extracted verbatim and
  :func:`~kicad_tools.router.current_paths.parse_current_path_sidecar`
  returns its paths and each case with its signed contributions;
* the shared-feed arithmetic the example states (4 A combined, 2 A with one
  member omitted, cancellation when opposed, each case judged on its own,
  and nothing summed without ``cases``) is what ``PathAmpacityRule`` reports
  for that sidecar on a board laid out as the example describes;
* the ``pulse_overlap`` example parses, the ``burst`` illustration's currents
  are what the checker sizes the shared feed for, and every ``pulse_overlap``
  table row matches what the checker reports when its requirement is unmet;
* the DRC guide's ``--current-paths`` mention links to the schema section.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.physics.ampacity import width_for_current
from kicad_tools.router.current_paths import (
    CaseContribution,
    CurrentPathSidecar,
    CurrentPathSpec,
    OperatingCase,
    parse_current_path_sidecar,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule
from kicad_tools.validate.violations import DRCViolation
from tests.test_path_ampacity_check import _add_pad_footprint

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_MD = REPO_ROOT / "docs" / "reference" / "cli.md"
DRC_GUIDE = REPO_ROOT / "docs" / "guides" / "drc-and-validation.md"

SCHEMA_HEADING = "#### Declared branch current paths (`--current-paths`)"
CASES_HEADING = "##### Operating cases"
NET = "/VOUT"
# The example's copper is 1.0 mm wide on an external layer (``F.Cu``, the
# checker's default 10 C rise): it clears 2 A on 1 oz copper (0.781 mm) and not
# the 4 A of ``load+holdup`` (2.033 mm). On an inner layer 2 A would need 2.033 mm.
COPPER_WIDTH_MM = 1.0
FEED_MIDPOINT = (40.0, 50.0)  # midway from J1.1 (20, 50) to the junction (60, 50)

_FENCE = re.compile(r"^(```|~~~).*?^\1", re.DOTALL | re.MULTILINE)
_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
# The prose illustration that follows the published ``burst`` case, matched on
# whitespace-normalized text so a reflow of the paragraph cannot break it.
_BURST_ILLUSTRATION = re.compile(
    r'`VOUT_LOAD` declaring `"pulsed_a": (?P<load>[\d.]+)` and `VOUT_HOLDUP` '
    r'`"pulsed_a": (?P<holdup>[\d.]+)`, both at `"duty_cycle": (?P<duty>[\d.]+)`, '
    r"the `burst` case puts (?P<base>[\d.]+) A on the shared feed between pulses and "
    r"(?P<peak>[\d.]+) A at the coincident peak, and sizes the feed for their "
    r"(?P<rms>[\d.]+) A RMS"
)
# A row of the "Current on the shared feed" table, and the sentences stating
# the IPC-2221 widths for the example's 1.0 mm copper, on an external layer
# and then on an inner one.
_FEED_TABLE_ROW = re.compile(
    r"^\| `(?P<case>[\w+-]+)` \| \*{0,2}(?P<amps>[\d.]+) A\*{0,2} \|", re.MULTILINE
)
_WIDTH_SENTENCE = re.compile(
    r"(?P<alone>[\d.]+) A needs (?P<alone_mm>[\d.]+) mm and "
    r"(?P<together>[\d.]+) A needs (?P<together_mm>[\d.]+) mm"
)
_INNER_WIDTH_SENTENCE = re.compile(
    r"On an inner layer the same currents need "
    r"(?P<alone_mm>[\d.]+) mm and (?P<together_mm>[\d.]+) mm"
)


def _section(markdown: str, heading: str) -> str:
    """Text under the first heading starting with ``heading``, to the next peer."""
    level = len(heading) - len(heading.lstrip("#"))
    match = re.search(
        rf"^{re.escape(heading)}[^\n]*\n(.*?)(?=^#{{1,{level}}} |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"{heading!r} section is missing"
    return match.group(1)


def _cases_section() -> str:
    """The published "Operating cases" subsection."""
    return _section(CLI_MD.read_text(encoding="utf-8"), CASES_HEADING)


def _bullet(lead_in: str) -> str:
    """The subsection's bullet whose bold lead-in is ``lead_in``, whitespace-normalized."""
    match = re.search(
        rf"^- \*\*{re.escape(lead_in)}\*\* (?P<text>.*?)(?=^- |\n\n|\Z)",
        _cases_section(),
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"the {lead_in!r} bullet is missing from the Operating cases section"
    return " ".join(match.group("text").split())


def _published_cases_json() -> tuple[str, str]:
    """The section's two JSON blocks, verbatim: the sidecar and the ``burst`` case."""
    blocks = _JSON_BLOCK.findall(_cases_section())
    assert len(blocks) == 2, f"expected the sidecar and the burst case, got {len(blocks)}"
    return blocks[0], blocks[1]


def _published_sidecar() -> CurrentPathSidecar:
    return parse_current_path_sidecar(json.loads(_published_cases_json()[0]))


def _burst_case(overlap: str | None) -> OperatingCase:
    """The published ``burst`` case, its ``pulse_overlap`` replaced by ``overlap``."""
    data = json.loads(_published_cases_json()[1])
    data["pulse_overlap"] = overlap
    (case,) = parse_current_path_sidecar({"paths": [], "cases": [data]}).cases
    return case


def _pulsed(
    peaks: tuple[float | None, ...], duties: tuple[float | None, ...]
) -> list[CurrentPathSpec]:
    """The published paths, each declaring the given pulse and duty cycle."""
    return [
        dataclasses.replace(spec, pulsed_a=peak, duty_cycle=duty)
        for spec, peak, duty in zip(_published_sidecar().specs, peaks, duties, strict=True)
    ]


def _case_findings(
    specs: Sequence[CurrentPathSpec], cases: Sequence[OperatingCase]
) -> list[DRCViolation]:
    """What ``cases`` themselves report on the documented board.

    A case finding's first item is the case name; the per-path findings the
    same check still raises name the path instead.
    """
    results = PathAmpacityRule(specs=specs, cases=cases).check(_documented_board(), _one_oz())
    names = {case.name for case in cases}
    return [v for v in results.violations if v.items[:1] and v.items[0] in names]


def _documented_feed_currents() -> dict[str, float]:
    """The "Current on the shared feed" table: case name -> published amps."""
    rows = _FEED_TABLE_ROW.findall(_cases_section())
    assert len(rows) == 3, rows
    return {case: float(amps) for case, amps in rows}


def _feed_findings(
    specs: Sequence[CurrentPathSpec], case: OperatingCase, board: PCB
) -> list[DRCViolation]:
    """What ``case`` reports on the shared feed of ``board`` for paths declared as ``specs``."""
    results = PathAmpacityRule(specs=specs, cases=[case]).check(board, _one_oz())
    return [v for v in results.violations if v.location == pytest.approx(FEED_MIDPOINT)]


def _documented_board(*, feed_width: float = COPPER_WIDTH_MM) -> PCB:
    """The example's layout: J1.1 feeds a junction that splits to J2.1 and U3.1."""
    pcb = PCB.create(width=200, height=120, center=False)
    for ref, x, y in (("J1", 20, 50), ("J2", 120, 50), ("U3", 60, 70)):
        _add_pad_footprint(pcb, ref=ref, x=x, y=y, net=NET)
    for start, end, width in (
        (("J1", "1"), (60, 50), feed_width),
        ((60, 50), ("J2", "1"), COPPER_WIDTH_MM),
        ((60, 50), ("U3", "1"), COPPER_WIDTH_MM),
    ):
        pcb.add_trace(start, end, width=width, layer="F.Cu", net=NET)
    return pcb


def _one_oz() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
        outer_copper_oz=1.0,
        inner_copper_oz=1.0,
    )


def test_operating_case_sidecar_parses_with_its_cases() -> None:
    sidecar = _published_sidecar()
    assert [spec.name for spec in sidecar.specs] == ["VOUT_LOAD", "VOUT_HOLDUP"]
    assert all(spec.net_name == NET for spec in sidecar.specs)
    assert [
        (case.name, [(c.path, c.current_a) for c in case.contributions]) for case in sidecar.cases
    ] == [
        ("load+holdup", [("VOUT_LOAD", 2.0), ("VOUT_HOLDUP", 2.0)]),
        ("holdup-only", [("VOUT_HOLDUP", 2.0)]),
        ("holdup-discharge", [("VOUT_LOAD", 2.0), ("VOUT_HOLDUP", -2.0)]),
    ]


@pytest.mark.parametrize("case_name", ["load+holdup", "holdup-only", "holdup-discharge"])
def test_each_case_places_the_documented_current_on_the_shared_feed(case_name: str) -> None:
    sidecar = _published_sidecar()
    amps = _documented_feed_currents()[case_name]
    case = next(c for c in sidecar.cases if c.name == case_name)
    board, rules = _documented_board(), _one_oz()

    # On a feed too narrow for any current in the table, with the paths rated
    # far below them, the finding states exactly what the case puts there: a
    # cancelled case has nothing to report.
    rated_low = [dataclasses.replace(spec, continuous_a=0.01) for spec in sidecar.specs]
    findings = _feed_findings(rated_low, case, _documented_board(feed_width=0.5))
    assert len(findings) == (0 if amps == 0 else 1), findings
    if amps:
        assert f"combined {amps:g}A" in findings[0].message

    # As published, each path's own declared current already covers the feed
    # up to that current: only a larger total is news.
    results = PathAmpacityRule(specs=sidecar.specs, cases=[case]).check(board, rules)
    if amps <= max(spec.continuous_a for spec in sidecar.specs):
        assert (results.errors, results.warnings) == ([], [])
        return
    (error,) = results.errors
    assert results.warnings == []
    assert error.message.startswith(
        f"Operating case {case_name!r} (VOUT_LOAD +2A, VOUT_HOLDUP +2A)"
    )
    assert error.location == pytest.approx(FEED_MIDPOINT)
    assert error.actual_value == pytest.approx(COPPER_WIDTH_MM)
    assert error.required_value == pytest.approx(2.033, abs=1e-3)


def test_published_ipc_2221_widths_are_what_the_checker_requires() -> None:
    text = " ".join(_cases_section().split())
    published = _WIDTH_SENTENCE.search(text)
    assert published, "the 1.0 mm copper sentence is missing from the Operating cases section"
    inner = _INNER_WIDTH_SENTENCE.search(text, published.end())
    assert inner, "the inner-layer widths are missing from the 1.0 mm copper sentence"
    for amps, widths in (
        (published["alone"], (published["alone_mm"], inner["alone_mm"])),
        (published["together"], (published["together_mm"], inner["together_mm"])),
    ):
        for layer, width in zip(("external", "internal"), widths, strict=True):
            required = width_for_current(float(amps), copper_weight_oz=1.0, layer=layer)
            assert f"{required:.3f}" == width, f"{amps} A on {layer}: {required:.3f} mm"


@pytest.mark.parametrize(
    ("case", "checker_says", "documented"),
    [
        pytest.param(
            OperatingCase("idle", (CaseContribution("VOUT_LOAD", 0.0),)),
            "zero or non-finite current",
            "`current_a` is zero or non-finite",
            id="zero-current",
        ),
        pytest.param(
            OperatingCase("idle", (CaseContribution("VOUT_LOAD", math.inf),)),
            "zero or non-finite current",
            "`current_a` is zero or non-finite",
            id="non-finite-current",
        ),
        pytest.param(
            OperatingCase("idle", ()),
            "lists no active path",
            "lists no contributions",
            id="no-contributions",
        ),
    ],
)
def test_bad_reference_bullet_covers_zero_current_and_no_contributions(
    case: OperatingCase, checker_says: str, documented: str
) -> None:
    """What a case says about itself blocks a verdict too: an `error`, never a skip."""
    (finding,) = _case_findings(_published_sidecar().specs, [case])
    assert finding.severity == "error"
    assert finding.message.startswith("Operating case 'idle' cannot be evaluated: ")
    assert checker_says in finding.message
    assert documented in _bullet("A bad reference blocks a verdict.")


def test_cases_are_judged_together_but_never_merged() -> None:
    sidecar = _published_sidecar()
    board, rules = _documented_board(), _one_oz()
    results = PathAmpacityRule(specs=sidecar.specs, cases=sidecar.cases).check(board, rules)
    assert [error.items[0] for error in results.errors] == ["load+holdup"]
    assert results.warnings == []


def test_sidecar_without_cases_keeps_the_independent_checks() -> None:
    sidecar = _published_sidecar()
    results = PathAmpacityRule(specs=sidecar.specs).check(_documented_board(), _one_oz())
    assert results.errors == []
    assert results.warnings == []


def test_pulse_overlap_example_parses() -> None:
    fragment = _published_cases_json()[1]
    (case,) = parse_current_path_sidecar({"paths": [], "cases": [json.loads(fragment)]}).cases
    assert case.name == "burst"
    assert case.pulse_overlap == "coincident"
    assert [(c.path, c.current_a) for c in case.contributions] == [
        ("VOUT_LOAD", 0.5),
        ("VOUT_HOLDUP", 0.5),
    ]


def test_burst_illustration_is_what_the_checker_sizes_the_feed_for() -> None:
    published = _BURST_ILLUSTRATION.search(" ".join(_cases_section().split()))
    assert published, "the burst illustration is missing from the Operating cases section"
    duty = float(published["duty"])
    specs = _pulsed((float(published["load"]), float(published["holdup"])), (duty, duty))
    case = _burst_case("coincident")
    between = sum(c.current_a for c in case.contributions)
    peak = between
    for contribution in case.contributions:
        pulsed_a = next(spec.pulsed_a for spec in specs if spec.name == contribution.path)
        assert pulsed_a is not None
        peak += pulsed_a - contribution.current_a
    rms = math.sqrt(duty * peak**2 + (1 - duty) * between**2)
    assert (float(published["base"]), float(published["peak"])) == pytest.approx((between, peak))
    assert float(published["rms"]) == pytest.approx(rms, abs=0.005)
    (finding,) = _case_findings(specs, [case])
    assert finding.severity == "error"
    assert f"combined {rms:.4g}A" in finding.message
    assert finding.location == pytest.approx(FEED_MIDPOINT)


@pytest.mark.parametrize(
    ("overlap", "duties", "not_determined", "checker_says", "row", "documented"),
    [
        (
            None,
            (0.1, 0.1),
            "combined RMS and peak current",
            "'pulse_overlap'",
            "| omitted |",
            "RMS **and** peak are undetermined",
        ),
        (
            "coincident",
            (0.1, 0.25),
            "combined RMS current",
            "common 'duty_cycle'",
            '| `"coincident"` |',
            "The same `duty_cycle` on every pulsed path",
        ),
        (
            "disjoint",
            (0.7, 0.7),
            "combined RMS current",
            "summing to at most 1",
            '| `"disjoint"` |',
            "`duty_cycle` on every pulsed path, summing to at most 1",
        ),
    ],
)
def test_pulse_overlap_rows_match_what_the_checker_reports(
    overlap: str | None,
    duties: tuple[float, float],
    not_determined: str,
    checker_says: str,
    row: str,
    documented: str,
) -> None:
    """A row's unmet requirement leaves exactly the quantity the row names."""
    findings = _case_findings(_pulsed((4.0, 4.0), duties), [_burst_case(overlap)])
    (warning,) = [v for v in findings if "NOT determined" in v.message]
    assert warning.severity == "warning"
    assert warning.message.startswith(
        f"Operating case 'burst': {not_determined} on copper shared by "
        "VOUT_LOAD, VOUT_HOLDUP is NOT determined"
    )
    assert checker_says in warning.message
    (table_row,) = [line for line in _cases_section().splitlines() if line.startswith(row)]
    assert documented in table_row


def test_disjoint_pulses_with_duties_summing_to_one_at_most_are_determined() -> None:
    findings = _case_findings(_pulsed((4.0, 4.0), (0.3, 0.3)), [_burst_case("disjoint")])
    assert not any("NOT determined" in v.message for v in findings)


@pytest.mark.parametrize("overlap", [None, "coincident", "disjoint"])
def test_pulse_overlap_is_read_only_where_two_pulsed_members_share_copper(
    overlap: str | None,
) -> None:
    """Only one of the two paths pulses, so nothing about the feed is left open."""
    findings = _case_findings(_pulsed((4.0, None), (0.7, None)), [_burst_case(overlap)])
    assert not any("NOT determined" in v.message for v in findings)


def _heading_slugs(markdown: str) -> set[str]:
    """GitHub-style anchors of every heading outside a code fence."""
    slugs = set()
    for line in _FENCE.sub("", markdown).splitlines():
        heading = re.match(r"^#{1,6} (.+?)(?:\s+#+)?$", line)
        if heading:
            text = re.sub(r"[^\w\- ]", "", heading.group(1).lower())
            slugs.add(text.replace(" ", "-"))
    return slugs


def test_drc_guide_links_current_paths_to_the_schema_section() -> None:
    targets = re.findall(
        r"\[`--current-paths`\]\(([^)\s]+)\)", DRC_GUIDE.read_text(encoding="utf-8")
    )
    assert len(targets) == 1, targets
    path, _, fragment = targets[0].partition("#")
    assert (DRC_GUIDE.parent / path).resolve() == CLI_MD.resolve()
    slugs = _heading_slugs(CLI_MD.read_text(encoding="utf-8"))
    assert fragment in slugs
    assert fragment == _heading_slugs(SCHEMA_HEADING + "\n").pop()
