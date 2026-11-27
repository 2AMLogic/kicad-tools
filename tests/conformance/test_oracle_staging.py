"""The oracle stages a board's same-stem ``.kicad_dru`` (Issue #10048).

``run_oracle`` measures a scratch copy of the board.  Before #10048 it copied
the ``.kicad_pro`` but not the optional ``.kicad_dru``, so a rule-driven pair
read clean in the oracle while kicad-cli, given all three files, reported it.
These tests pin the staging contract: the rule file reaches both fill states,
the source directory is never touched, and a reused ``work_dir`` does not keep
a rule file whose source has been removed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.conformance.conftest import requires_kicad_cli
from tests.conformance.fixtures import (
    HV_COUPON_NAME,
    HV_COUPON_PAIRS,
    domain_matrix_fixture_path,
)
from tests.conformance.oracle import run_oracle

pytestmark = requires_kicad_cli

_SCALAR_FAILURES = frozenset(
    frozenset({pair.net_a, pair.net_b})
    for pair in HV_COUPON_PAIRS
    if pair.gap_mm < 0.2  # below the project floor: flagged without any rule
)
_ALL_FAILURES = frozenset(
    frozenset({pair.net_a, pair.net_b}) for pair in HV_COUPON_PAIRS if pair.violates
)


def _copy_board_inputs(source: Path, directory: Path) -> Path:
    """Copy the board and every same-stem sibling into ``directory``."""
    for sibling in source.parent.glob(f"{source.stem}.*"):
        shutil.copyfile(sibling, directory / sibling.name)
    return directory / source.name


def _flagged(
    board: Path, *, refill: bool, work_dir: Path | None = None
) -> frozenset[frozenset[str]]:
    result = run_oracle(board, refill=refill, work_dir=work_dir)
    assert not result.dropped, result.dropped
    return frozenset(v.nets for v in result.of_kind("clearance"))


@pytest.mark.parametrize("refill", [False, True], ids=["asis", "refilled"])
def test_rule_sidecar_is_staged_for_both_fill_states(refill: bool, tmp_path: Path) -> None:
    board = domain_matrix_fixture_path(HV_COUPON_NAME)
    assert board.with_suffix(".kicad_dru").exists(), "the committed coupon lost its rule file"
    assert _flagged(board, refill=refill, work_dir=tmp_path) == _ALL_FAILURES


@pytest.mark.parametrize("refill", [False, True], ids=["asis", "refilled"])
def test_removing_the_rule_sidecar_restores_the_scalar_result(refill: bool, tmp_path: Path) -> None:
    """A reused ``work_dir`` must not keep the previous run's rule file."""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    work = tmp_path / "work"
    board = _copy_board_inputs(domain_matrix_fixture_path(HV_COUPON_NAME), inputs)

    assert _flagged(board, refill=refill, work_dir=work) == _ALL_FAILURES

    board.with_suffix(".kicad_dru").unlink()
    assert _flagged(board, refill=refill, work_dir=work) == _SCALAR_FAILURES
    staged = work / f"oracle-{'refilled' if refill else 'asis'}-{board.stem}"
    assert not (staged / f"{board.stem}.kicad_dru").exists()


def test_the_scalar_and_rule_driven_results_differ_by_the_hv_pairs() -> None:
    """The reproduction in the issue: four flagged pairs with the rules, two without."""
    assert len(_ALL_FAILURES) == 4
    assert len(_SCALAR_FAILURES) == 2
    widened_only = {frozenset({"P1A", "P1B"}), frozenset({"P3A", "P3B"})}
    assert widened_only == _ALL_FAILURES - _SCALAR_FAILURES


def test_staging_never_modifies_the_source_files(tmp_path: Path) -> None:
    source = domain_matrix_fixture_path(HV_COUPON_NAME)
    before = {p.name: p.read_bytes() for p in source.parent.iterdir() if p.is_file()}
    run_oracle(source, refill=True, work_dir=tmp_path)
    after = {p.name: p.read_bytes() for p in source.parent.iterdir() if p.is_file()}
    assert after == before, "an oracle run changed or added files beside the committed board"


def test_a_board_without_a_rule_file_is_measured_as_before(tmp_path: Path) -> None:
    """No ``.kicad_dru`` anywhere: unchanged behaviour, and none is invented."""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    board = _copy_board_inputs(domain_matrix_fixture_path(HV_COUPON_NAME), inputs)
    board.with_suffix(".kicad_dru").unlink()
    work = tmp_path / "work"
    run_oracle(board, refill=False, work_dir=work)
    assert not list(work.rglob("*.kicad_dru"))
