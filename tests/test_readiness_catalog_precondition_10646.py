"""Readiness evidence needs the offline parts catalog (issue #10646).

Board 01's committed procurement evidence regressed OK -> WARN when the worker
that regenerated its package had no offline jlcparts catalog: ``kct readiness
--generate`` published a ``ready`` report whose stock check described the
generating host's cache rather than the board, and ``--verify`` carried that
record forward as a pass.  The catalog is now a recorded precondition of
readiness evidence: when the bundle's ``sources.offline_catalog`` says the
catalog was never searched (``missing`` / ``unreadable``, issue #10386), the
producer refuses with exit 2 and writes nothing, the way an unqualified
kicad-cli is refused (issue #6962).  ``hit`` / ``miss`` mean the catalog was
searched and still publish exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.export.preflight import evaluate_lcsc_stock
from kicad_tools.parts.jlcparts_catalog import get_catalog_path
from tests.test_kct_readiness_cmd import FakeEngines, make_board
from tests.test_readiness_stock_inventory_8807 import (
    CATALOG_CONDITION_TEXT,
    _host_with_catalog,
    _part,
    _produced_entry,
    _row,
    _unverified_data,
)

#: Catalog conditions in which the lookups never reached the catalog's
#: contents, so the stock check says nothing about the board.
HOST_GAPS = ("missing", "unreadable")


def _inventory(board: Path) -> list[tuple[str, bytes]]:
    """Every file under the board with its bytes, so "nothing written" covers edits too."""
    return sorted(
        (p.relative_to(board).as_posix(), p.read_bytes()) for p in board.rglob("*") if p.is_file()
    )


def _generate(board: Path, entry: dict | None, *extra: str) -> int:
    fake = FakeEngines(board, stock_check=entry)
    return readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--generate", *extra], engines=fake.bundle()
    )


def _published_metric(board: Path) -> dict:
    report = json.loads((board / "output" / "readiness.json").read_text())
    return report["metrics"]["lcsc_stock_order"]


def _host_without_catalog(lookup_dir: Path, monkeypatch) -> Path:
    """Make ``lookup_dir`` this host's cache dir, with no catalog in it.

    Returns the default catalog path after checking it really does not exist,
    so the refusal's premise -- this host has no catalog -- holds rather than
    being assumed, and no test touches the real ``~/.cache``.
    """
    lookup_dir.mkdir()
    local = lambda: lookup_dir / "parts.db"  # noqa: E731
    monkeypatch.setattr("kicad_tools.parts.cache.get_default_cache_path", local)
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_default_cache_path", local)
    assert not get_catalog_path().exists()
    return get_catalog_path()


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("condition", HOST_GAPS)
def test_a_catalog_the_lookups_never_reached_is_a_host_gap(condition):
    record = {"sources": {"parts_cache": "miss", "offline_catalog": condition}}
    gap = readiness_cmd._recorded_catalog_gap(record)
    assert gap == CATALOG_CONDITION_TEXT[condition]


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"sources": None},
        {"sources": {}},
        {"sources": {"parts_cache": "miss"}},
        {"sources": {"offline_catalog": "hit"}},
        {"sources": {"offline_catalog": "miss"}},
        {"sources": {"offline_catalog": "not_consulted"}},
        {"sources": {"offline_catalog": "future-state"}},
    ],
    ids=[
        "no-record",
        "no-sources",
        "sources-none",
        "sources-empty",
        "cache-only",
        "hit",
        "miss",
        "not-consulted",
        "unknown-state",
    ],
)
def test_a_searched_or_unrecorded_catalog_makes_no_gap(record):
    assert readiness_cmd._recorded_catalog_gap(record) is None


# ---------------------------------------------------------------------------
# --generate: a host gap refuses and writes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("condition", HOST_GAPS)
def test_generate_refuses_when_the_host_had_no_usable_catalog(tmp_path, monkeypatch, condition):
    _host_without_catalog(tmp_path / "host", monkeypatch)
    # What a real export records on a host in this condition.
    entry = _produced_entry(tmp_path / "producer", [], [("R1", "C999")], condition)
    board = make_board(tmp_path)
    before = _inventory(board)

    code = _generate(board, entry)

    assert code == 2
    assert _inventory(board) == before
    assert not (board / "output" / "readiness.json").exists()
    assert not (board / "output" / "readiness-attempt").exists()
    assert not (board / "output" / "manufacturing").exists()


def test_pcb_only_package_makes_no_procurement_claim_so_needs_no_catalog(tmp_path):
    entry = _produced_entry(tmp_path / "producer", [], [("R1", "C999")])
    board = make_board(tmp_path)

    assert _generate(board, entry, "--pcb-only") == 0
    report = json.loads((board / "output" / "readiness.json").read_text())
    assert report["status"] == "ready"
    assert "lcsc_stock_order" not in report["metrics"]


def test_refusal_names_the_catalog_path_and_the_download_command(tmp_path, monkeypatch, capsys):
    _host_without_catalog(tmp_path / "host", monkeypatch)
    entry = _produced_entry(tmp_path / "producer", [], [("R1", "C999")])
    board = make_board(tmp_path)

    code = _generate(board, entry)

    assert code == 2
    err = capsys.readouterr().err
    assert str(get_catalog_path()) in err
    assert "kct parts sync-catalog" in err
    assert "kct readiness <board> --generate" in err
    assert CATALOG_CONDITION_TEXT["missing"] in err
    assert err.startswith("error: Evidence environment refused; no evidence written: ")


def test_refusal_in_json_mode_is_one_unverified_envelope(tmp_path, monkeypatch, capsys):
    catalog = _host_without_catalog(tmp_path / "host", monkeypatch)
    entry = _produced_entry(tmp_path / "producer", [], [("R1", "C999")], "unreadable")
    board = make_board(tmp_path)

    code = _generate(board, entry, "--format", "json")

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "readiness"
    assert payload["success"] is False
    assert payload["status"] == "unverified"
    assert CATALOG_CONDITION_TEXT["unreadable"] in payload["error"]
    assert "kct parts sync-catalog" in payload["error"]
    assert str(catalog) in payload["error"]


# ---------------------------------------------------------------------------
# --verify: the producer's record is judged, never this host's catalog
# ---------------------------------------------------------------------------


def _record_host_gap(board: Path, condition: str) -> None:
    """Rewrite a published package as a host in *condition* would have recorded it."""
    out = board / "output"
    manifest_path = out / "manufacturing" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(e for e in manifest["preflight"] if e["name"] == "bom_lcsc_stock")
    entry["data"]["sources"]["offline_catalog"] = condition
    unverified = evaluate_lcsc_stock(entry["data"], 5, frozenset(), False)
    entry["status"], entry["message"], entry["details"] = (
        unverified.status,
        unverified.message,
        unverified.details,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2))
    readiness_cmd.write_manufacturing_archive(out / "manufacturing")


def _verify(board: Path) -> int:
    return readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--verify"], engines=FakeEngines(board).bundle()
    )


def _receipt(board: Path) -> dict:
    return json.loads((board / "output" / "readiness-verification" / "readiness.json").read_text())


@pytest.mark.parametrize("condition", HOST_GAPS)
def test_verify_refuses_a_package_recorded_without_the_catalog_whatever_this_host_has(
    tmp_path, monkeypatch, capsys, condition
):
    board = make_board(tmp_path)
    searched = _produced_entry(tmp_path / "producer", [], [("R1", "C999")], "miss")
    assert _generate(board, searched) == 0
    _record_host_gap(board, condition)
    before = _inventory(board)
    # This host's catalog lists the part; had verification looked, it would have found it.
    _host_with_catalog(tmp_path / "verifier", monkeypatch)

    code = _verify(board)

    assert code == 2
    assert _inventory(board) == before
    assert not (board / "output" / "readiness-verification").exists()
    err = capsys.readouterr().err
    assert CATALOG_CONDITION_TEXT[condition] in err
    assert str(get_catalog_path()) in err
    assert "regenerate" in err.lower()


def test_verify_of_a_package_recorded_with_the_catalog_passes_without_one(tmp_path, monkeypatch):
    # Catalog-backed evidence must not come to depend on the verifying host.
    board = make_board(tmp_path)
    entry = _produced_entry(tmp_path / "producer", [], [("R1", "C999")], "hit", [_row("C999")])
    assert _generate(board, entry) == 0
    metric = _published_metric(board)
    assert metric["sources"] == {"parts_cache": "miss", "offline_catalog": "hit"}
    # A verifier with an empty cache and no catalog at the default path.
    _host_without_catalog(tmp_path / "verifier", monkeypatch)

    assert _verify(board) == 0
    verified = _receipt(board)
    assert verified["status"] == "ready"
    assert verified["metrics"]["lcsc_stock_order"] == metric


# ---------------------------------------------------------------------------
# With the catalog searched, behaviour and output are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "expected"),
    [("hit", "OK"), ("miss", "WARN")],
)
def test_a_searched_catalog_publishes_as_before(tmp_path, condition, expected):
    entry = _produced_entry(tmp_path / "producer", [], [("R1", "C999")], condition, [_row("C999")])
    board = make_board(tmp_path)

    assert _generate(board, entry) == 0
    metric = _published_metric(board)
    assert metric["status"] == expected
    assert metric["sources"] == {"parts_cache": "miss", "offline_catalog": condition}
    assert metric["message"] == entry["message"]


def test_a_cache_settled_lookup_never_needed_the_catalog(tmp_path):
    entry = _produced_entry(tmp_path / "producer", [_part("C1", 500)], [("R1", "C1")])
    assert entry["data"]["sources"] == {"parts_cache": "hit", "offline_catalog": "not_consulted"}
    board = make_board(tmp_path)

    assert _generate(board, entry) == 0
    assert _published_metric(board)["sources"] == {
        "parts_cache": "hit",
        "offline_catalog": "not_consulted",
    }


def test_a_legacy_record_without_sources_makes_no_claim(tmp_path):
    entry = {
        "name": "bom_lcsc_stock",
        "status": "WARN",
        "message": "Stock not verified",
        "details": "C999",
        "data": _unverified_data(),
    }
    board = make_board(tmp_path)

    assert _generate(board, entry) == 0
    assert "sources" not in _published_metric(board)


def test_partial_coverage_searched_by_the_catalog_keeps_its_provenance(tmp_path):
    entry = _produced_entry(
        tmp_path / "producer", [_part("C1", 500)], [("Y2", "C1"), ("R1", "C999")], "miss"
    )
    assert entry["status"] == "WARN"  # the honest result: C999 is in no source
    board = make_board(tmp_path)

    assert _generate(board, entry) == 0
    assert _published_metric(board)["sources"] == {"parts_cache": "hit", "offline_catalog": "miss"}
