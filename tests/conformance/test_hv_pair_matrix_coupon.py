"""Group 14 on the eight-pair HV coupon (Issue #10048, acceptance item P2c of #5509).

The generated corpus keeps every net at 0 V, so the C++ domain matrix is
dormant there.  This module drives the **production** consumers over the named
``hv-pair-matrix-coupon`` fixture and reports each check's coverage
separately, because they answer different questions:

* ``board_pairwise_violations`` -- the Python board-level **widening** pass.
  It walks only pairs whose requirement exceeds the scalar floor.
* ``GridPyAdapter`` -- the Python **scalar** commit validator, which knows no
  voltages.
* ``Grid3D::validate_route`` -- the production C++ validator, with the domain
  matrix installed (in both insertion orders) and dormant.

The expected sets are derived from the fixture's own pair table, and the
kicad-cli comparison asserts the matrix-installed C++ validator against
ground truth, so a change in any consumer has to move a stated number.  This is
calibration only: no clearance value or production behaviour is changed here,
and nothing here promotes group 14 from advisory to gating.
"""

from __future__ import annotations

import pytest

from tests.conformance.adapters.grid_py import GridPyAdapter
from tests.conformance.adapters.pairwise import INSERTION_ORDERS, PairwiseAdapter
from tests.conformance.conftest import requires_kicad_cli
from tests.conformance.fixtures import (
    DOMAIN_MATRIX_FIXTURE_BUILDERS,
    HV_COUPON_NAME,
    HV_COUPON_PAIRS,
    HV_COUPON_PROJECT_CLEARANCE_MM,
    HV_COUPON_VOLTAGES,
    domain_matrix_fixture_path,
)
from tests.conformance.oracle import run_oracle

_ALL_PAIRS = {frozenset({p.net_a, p.net_b}) for p in HV_COUPON_PAIRS}
_FAILING = {frozenset({p.net_a, p.net_b}) for p in HV_COUPON_PAIRS if p.violates}
_FLOOR = HV_COUPON_PROJECT_CLEARANCE_MM
# Fail the scalar floor in every reading.
_SCALAR_FAILING = {frozenset({p.net_a, p.net_b}) for p in HV_COUPON_PAIRS if p.gap_mm < _FLOOR}
# Fail only against the HV requirement the voltage map widens to.
_WIDENED_ONLY = _FAILING - _SCALAR_FAILING


@pytest.fixture(scope="module")
def case():
    return DOMAIN_MATRIX_FIXTURE_BUILDERS[HV_COUPON_NAME]()


def _nets(verdicts) -> set[frozenset[str]]:
    return {v.nets for v in verdicts}


class _ConsumerDisagreement(AssertionError):
    """Only a comparison of a consumer reading is advisory on the HV coupon."""


_group14_measurement = pytest.mark.consumer(
    group=PairwiseAdapter.group, raises=_ConsumerDisagreement
)


def _compare_reading(actual, expected, context: str = "") -> None:
    if actual != expected:
        raise _ConsumerDisagreement(f"{context} expected {expected!r}, measured {actual!r}")


def test_expected_sets_match_the_issue_table() -> None:
    assert len(_ALL_PAIRS) == 8
    assert {tuple(sorted(n)) for n in _FAILING} == {
        ("P1A", "P1B"),
        ("P3A", "P3B"),
        ("P5A", "P5B"),
        ("P7A", "P7B"),
    }
    assert {tuple(sorted(n)) for n in _WIDENED_ONLY} == {("P1A", "P1B"), ("P3A", "P3B")}


@_group14_measurement
def test_python_board_widening_pass_reports_only_the_two_hv_pairs(case) -> None:
    """P1 (0.700/1.600) and P3 (3.000/3.200); P5/P7 sit at the floor and are skipped."""
    verdicts = PairwiseAdapter().verdicts_at_voltages(case, HV_COUPON_VOLTAGES)
    _compare_reading(_nets(verdicts), _WIDENED_ONLY)
    by_nets = {v.nets: v for v in verdicts}
    p1 = by_nets[frozenset({"P1A", "P1B"})]
    p3 = by_nets[frozenset({"P3A", "P3B"})]
    _compare_reading((p1.gap_mm, p1.required_mm), pytest.approx((0.700, 1.600)), "P1")
    _compare_reading((p3.gap_mm, p3.required_mm), pytest.approx((3.000, 3.200)), "P3")


@_group14_measurement
def test_python_board_widening_pass_is_silent_on_a_flat_voltage_map(case) -> None:
    """The generated corpus's all-0 V reading: zero findings, by design."""
    _compare_reading(PairwiseAdapter().verdicts(case), set())


@_group14_measurement
def test_python_scalar_commit_validator_reports_only_the_two_floor_failures(case) -> None:
    """The scalar validator has no voltages: P5 and P7 only, never P1 / P3."""
    _compare_reading(_nets(GridPyAdapter().verdicts(case)), _SCALAR_FAILING)


@_group14_measurement
def test_the_two_python_passes_cover_disjoint_failures(case) -> None:
    """Neither Python pass alone covers all four failures; together they do."""
    widening = _nets(PairwiseAdapter().verdicts_at_voltages(case, HV_COUPON_VOLTAGES))
    scalar = _nets(GridPyAdapter().verdicts(case))
    _compare_reading(widening & scalar, set())
    _compare_reading(widening | scalar, _FAILING)


@_group14_measurement
@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("order", INSERTION_ORDERS)
def test_cpp_validator_with_the_matrix_installed_rejects_all_four(case, order: str) -> None:
    verdicts = PairwiseAdapter().cpp_validator_verdicts(case, HV_COUPON_VOLTAGES, order=order)
    _compare_reading(_nets(verdicts), _FAILING, order)


@_group14_measurement
@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("order", INSERTION_ORDERS)
def test_cpp_validator_with_the_matrix_dormant_is_the_scalar_gate(case, order: str) -> None:
    verdicts = PairwiseAdapter().cpp_validator_verdicts(case, None, order=order)
    _compare_reading(_nets(verdicts), _SCALAR_FAILING, order)


@_group14_measurement
@pytest.mark.requires_router_cpp
def test_cpp_matrix_installed_rejects_what_the_dormant_path_accepts(case) -> None:
    """The matrix is what adds P1 and P3, in both insertion orders."""
    adapter = PairwiseAdapter()
    for order in INSERTION_ORDERS:
        installed = _nets(adapter.cpp_validator_verdicts(case, HV_COUPON_VOLTAGES, order=order))
        dormant = _nets(adapter.cpp_validator_verdicts(case, None, order=order))
        _compare_reading(installed - dormant, _WIDENED_ONLY, order)
        _compare_reading(dormant - installed, set(), order)


@_group14_measurement
@pytest.mark.requires_router_cpp
@pytest.mark.parametrize("order", INSERTION_ORDERS)
def test_cpp_reports_the_requirement_actually_installed(case, order: str) -> None:
    """Grounding P6B widens P6 from 0.2 to 1.6 mm. A dormant grid uses the scalar floor."""
    adapter = PairwiseAdapter()
    for voltages, pair, required in (
        ({**HV_COUPON_VOLTAGES, "P6B": 0.0}, frozenset({"P6A", "P6B"}), 1.6),
        (None, frozenset({"P5A", "P5B"}), _FLOOR),
    ):
        verdicts = {v.nets: v for v in adapter.cpp_validator_verdicts(case, voltages, order=order)}
        _compare_reading(pair in verdicts, True, f"{order} {sorted(pair)} rejected")
        assert verdicts[pair].required_mm == pytest.approx(required)


@pytest.mark.requires_router_cpp
def test_cpp_validator_requires_a_known_insertion_order(case) -> None:
    with pytest.raises(ValueError, match="order must be one of"):
        PairwiseAdapter().cpp_validator_verdicts(case, None, order="sideways")


@_group14_measurement
@pytest.mark.requires_router_cpp
@requires_kicad_cli
@pytest.mark.parametrize("refill", [False, True], ids=["asis", "refilled"])
def test_cpp_matrix_installed_agrees_with_kicad_cli_on_every_pair(case, refill: bool) -> None:
    """KiCad, with the authored rules, and the matrix-installed C++ validator agree.

    Compared per net pair across all eight pairs, controls included, in both
    fill modes and both insertion orders.
    """
    truth = _nets(run_oracle(domain_matrix_fixture_path(HV_COUPON_NAME), refill=refill).verdicts)
    assert truth == _FAILING
    adapter = PairwiseAdapter()
    for order in INSERTION_ORDERS:
        consumer = _nets(adapter.cpp_validator_verdicts(case, HV_COUPON_VOLTAGES, order=order))
        _compare_reading(consumer, truth, order)
