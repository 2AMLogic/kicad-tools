"""``--quantity`` / ``--inventory-covered`` on the stock gate (issue #8807),
and the lookup provenance its record carries (issue #10386)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kicad_tools.export.preflight import (
    PreflightChecker,
    PreflightConfig,
    describe_lookup_sources,
    evaluate_lcsc_stock,
    parse_inventory_covered,
    summarize_lookup_states,
)
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.jlcparts_catalog import JlcpartsCatalog, get_catalog_path
from kicad_tools.parts.models import Part
from kicad_tools.schema.bom import BOM, BOMItem

FP = "Resistor_SMD:R_0805_2012Metric"


def _part(lcsc, stock, source="live"):
    return Part(
        lcsc_part=lcsc,
        mfr_part="CRCW0805137KFKEAHP",
        description="137k 0805",
        package="0805",
        value="137kΩ",
        stock=stock,
        stock_source=source,
        fetched_at=datetime.now(tz=timezone.utc) - timedelta(hours=0),
    )


def _checker(tmp_path, parts, refs, **cfg) -> PreflightChecker:
    """A stock checker over a cache in ``tmp_path``; its catalog is ``tmp_path``'s."""
    cache = PartsCache(db_path=tmp_path / "parts.db")
    for p in parts:
        cache.put(p)
    checker = PreflightChecker(
        pcb_path=tmp_path / "board.kicad_pcb",
        config=PreflightConfig(skip_drc=True, skip_erc=True, **cfg),
        parts_cache=cache,
    )
    checker._bom = BOM(
        items=[
            BOMItem(reference=r, value="137k", footprint=FP, lib_id="Device:R", lcsc=lc)
            for r, lc in refs
        ]
    )
    return checker


def _run(tmp_path, parts, refs, **cfg):
    return _checker(tmp_path, parts, refs, **cfg).check_bom_lcsc_stock()


def test_parse_inventory_covered_normalises():
    assert parse_inventory_covered(" c123 , C456,,") == frozenset({"C123", "C456"})
    assert parse_inventory_covered("") == frozenset()
    assert parse_inventory_covered(None) == frozenset()


def test_short_at_five_passes_at_two(tmp_path):
    parts, refs = [_part("C1", 2)], [("Y2", "C1")]
    assert _run(tmp_path, parts, refs, order_boards=5).status == "FAIL"
    assert _run(tmp_path, parts, refs, order_boards=2).status == "OK"


def test_covered_short_line_passes_and_is_listed(tmp_path):
    r = _run(tmp_path, [_part("C1", 2)], [("Y2", "C1")], inventory_covered=frozenset({"C1"}))
    assert r.status == "OK"
    assert "held in operator inventory, not stock-verified: C1" in r.details
    assert r.data["inventory_covered"] == ["C1"]


def test_covered_but_not_short_and_unused_are_reported(tmp_path):
    r = _run(
        tmp_path,
        [_part("C1", 500)],
        [("R1", "C1")],
        inventory_covered=frozenset({"C1", "C999"}),
    )
    assert r.status == "WARN"
    assert "not short" in r.details and "C1" in r.details
    assert "not in the BOM: C999" in r.details


def test_only_covered_part_does_not_hide_other_shortfall(tmp_path):
    r = _run(
        tmp_path,
        [_part("C1", 2), _part("C2", 1)],
        [("Y2", "C1"), ("R1", "C2")],
        inventory_covered=frozenset({"C1"}),
    )
    assert r.status == "FAIL"
    assert "C2" in r.details


def test_evaluator_parity_from_recorded_data(tmp_path):
    parts, refs = [_part("C1", 2)], [("Y2", "C1")]
    first = _run(tmp_path, parts, refs)
    again = evaluate_lcsc_stock(first.data, 5, frozenset(), False)
    assert (again.status, again.message, again.details) == (
        first.status,
        first.message,
        first.details,
    )
    assert evaluate_lcsc_stock(first.data, 2, frozenset(), False).status == "OK"
    assert evaluate_lcsc_stock(first.data, 5, frozenset({"c1"}), False).status == "OK"


def test_unverified_shortfall_is_warn_with_source_named(tmp_path):
    r = _run(tmp_path, [_part("C1", 2, source="cache")], [("Y2", "C1")])
    assert r.status == "WARN"
    assert "unverified cache" in r.details


# ---- lookup provenance (issue #10386) -------------------------------------
#
# ``bom_lcsc_stock`` used to give the same WARN for a host with no catalog
# file, a host whose catalog it could not read, and a catalog that was
# searched and does not list the part.  The record now says which it was.

_spec = importlib.util.spec_from_file_location(
    "build_jlcparts_fixture_10386",
    Path(__file__).parent / "parts" / "fixtures" / "build_jlcparts_fixture.py",
)
assert _spec is not None and _spec.loader is not None
_builder = importlib.util.module_from_spec(_spec)
sys.modules["build_jlcparts_fixture_10386"] = _builder
_spec.loader.exec_module(_builder)

# The catalog beside the cache, in each condition a host can have.  Every
# one of them makes ``JlcpartsCatalog.lookup`` answer ``None``.
CATALOG_CONDITIONS = ("missing", "unreadable", "miss")

CATALOG_CONDITION_TEXT = {
    "missing": "offline catalog not available on the generating host (no catalog file)",
    "unreadable": "offline catalog could not be read on the generating host",
    "miss": "offline catalog searched, no matching row",
}


def _row(lcsc: str, stock: int = 5000) -> dict:
    return {
        "lcsc": int(lcsc[1:]),
        "mfr": "CRCW0805137KFKEAHP",
        "package": "0805",
        "manufacturer": "YAGEO",
        "library_type": "basic",
        "description": "137k 1% 0805 resistor",
        "datasheet": "",
        "stock": stock,
        "price": None,
    }


def _give_host_catalog(tmp_path, condition: str, rows: list[dict] | None = None) -> Path:
    """Put a catalog in ``condition`` beside the cache ``_checker`` will use."""
    path = tmp_path / "jlcparts.sqlite3"
    if condition == "unreadable":
        path.write_bytes(b"SQLite format 3\x00truncated" * 8)
    elif condition == "hit":
        _builder.build_fixture(path, rows=rows)
    elif condition == "miss":
        _builder.build_fixture(path, rows=[])
    return path


@pytest.mark.parametrize("condition", CATALOG_CONDITIONS)
def test_unverified_stock_records_what_became_of_the_catalog(tmp_path, condition):
    _give_host_catalog(tmp_path, condition)
    refs = [("R1", "C999")]
    r = _run(tmp_path, [], refs)

    # The verdict and the unverified id are what they always were.
    assert r.status == "WARN"
    assert r.data["unresolved"] == ["C999"]
    assert r.data["parts"] == []
    assert r.data["sources"] == {"parts_cache": "miss", "offline_catalog": condition}
    assert f"; {CATALOG_CONDITION_TEXT[condition]}" in r.message
    # Severity is untouched: a host gap still fails a strict preflight.
    strict = _run(tmp_path, [], refs, strict_report_source=True)
    assert strict.status == "FAIL"
    assert strict.data["sources"] == r.data["sources"]


@pytest.mark.parametrize("condition", CATALOG_CONDITIONS)
def test_part_check_keeps_the_id_unverified_whatever_the_catalog(tmp_path, condition):
    _give_host_catalog(tmp_path, condition)
    checker = _checker(tmp_path, [], [("R1", "C999")])
    verdict = checker._check_bom_lcsc_values()

    assert verdict.status == "WARN"
    assert "0 of 1 assigned item(s) verified (no source)" in verdict.message
    assert "R1 -> C999" in verdict.details


def test_host_gap_and_catalog_miss_are_distinguishable(tmp_path):
    recorded = {}
    for condition in CATALOG_CONDITIONS:
        host = tmp_path / condition
        host.mkdir()
        _give_host_catalog(host, condition)
        recorded[condition] = _run(host, [], [("R1", "C999")])

    # Same assignment, same WARN -- but no two records say the same thing.
    assert {r.status for r in recorded.values()} == {"WARN"}
    assert {tuple(r.data["unresolved"]) for r in recorded.values()} == {("C999",)}
    assert {r.data["sources"]["offline_catalog"] for r in recorded.values()} == set(
        CATALOG_CONDITIONS
    )
    assert len({r.message for r in recorded.values()}) == len(CATALOG_CONDITIONS)
    for condition, r in recorded.items():
        assert r.message.endswith(
            f"; parts cache searched, no matching row; {CATALOG_CONDITION_TEXT[condition]}"
        )


def test_cache_only_and_catalog_backed_hits_are_attributable(tmp_path):
    # A cache record that states a package settles the lookup: the catalog is
    # never opened, so its state is not a claim that the part was looked for.
    _give_host_catalog(tmp_path, "miss")
    cache_only = _run(tmp_path, [_part("C1", 500)], [("Y2", "C1")])
    assert cache_only.status == "OK"
    assert cache_only.data["sources"] == {
        "parts_cache": "hit",
        "offline_catalog": "not_consulted",
    }
    assert cache_only.data["parts"][0]["source"] == "live"

    # Same assignment, empty cache, a catalog that lists it.
    other = tmp_path / "catalog-host"
    other.mkdir()
    _give_host_catalog(other, "hit", [_row("C1")])
    catalog_backed = _run(other, [], [("Y2", "C1")])
    assert catalog_backed.status == "OK"
    assert catalog_backed.data["sources"] == {"parts_cache": "miss", "offline_catalog": "hit"}
    assert catalog_backed.data["parts"][0]["source"] == "offline_catalog"


def test_partial_coverage_keeps_observations_and_unresolved_ids(tmp_path):
    _give_host_catalog(tmp_path, "hit", [_row("C2")])
    refs = [("Y2", "C1"), ("R1", "C2"), ("R2", "C3")]
    r = _run(tmp_path, [_part("C1", 500)], refs)

    assert r.status == "OK"  # an unresolvable id beside a checked part (issue #8121)
    assert [(p["lcsc"], p["source"]) for p in r.data["parts"]] == [
        ("C1", "live"),
        ("C2", "offline_catalog"),
    ]
    assert r.data["unresolved"] == ["C3"]
    assert r.data["sources"] == {"parts_cache": "hit", "offline_catalog": "hit"}
    assert r.message == "2 LCSC part(s) have recorded stock for 5 board(s)"


def test_verdict_carries_where_each_lookup_reached(tmp_path):
    from kicad_tools.export.lcsc_value_check import verify_lcsc_assignment

    cache = PartsCache(db_path=tmp_path / "parts.db")
    cache.put(_part("C1", 500))
    searched = JlcpartsCatalog(_give_host_catalog(tmp_path, "miss", []))
    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    broken = JlcpartsCatalog(_give_host_catalog(broken_dir, "unreadable"))

    settled = verify_lcsc_assignment(cache, searched, "C1", "137k", "Y2")
    assert (settled.status, settled.lookups.cache, settled.lookups.catalog) == (
        "match",
        "hit",
        "not_consulted",
    )
    absent = verify_lcsc_assignment(cache, searched, "C999", "137k", "R1")
    assert (absent.status, absent.lookups.cache, absent.lookups.catalog) == (
        "unverified",
        "miss",
        "miss",
    )
    failed = verify_lcsc_assignment(cache, broken, "C999", "", "R1")
    assert (failed.status, failed.lookups.catalog) == ("unverified", "unreadable")


def test_unreadable_parts_cache_is_not_recorded_as_searched(tmp_path):
    class BrokenCache(PartsCache):
        def get(self, lcsc_part, ignore_expiry=False):
            raise sqlite3.DatabaseError("file is not a database")

    checker = PreflightChecker(
        pcb_path=tmp_path / "board.kicad_pcb",
        config=PreflightConfig(skip_drc=True, skip_erc=True),
        parts_cache=BrokenCache(db_path=tmp_path / "parts.db"),
    )
    checker._bom = BOM(
        items=[BOMItem(reference="R1", value="137k", footprint=FP, lib_id="Device:R", lcsc="C999")]
    )

    r = checker.check_bom_lcsc_stock()
    assert r.status == "WARN"
    assert r.data["sources"] == {"parts_cache": "unreadable", "offline_catalog": "missing"}
    assert "parts cache could not be read on the generating host" in r.message


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({"hit", "miss"}, "hit"),
        ({"hit", "not_consulted"}, "hit"),
        ({"miss", "not_consulted"}, "miss"),
        ({"missing", "not_consulted"}, "missing"),
        ({"unreadable", "hit"}, "unreadable"),
        ({"unreadable", "miss", "missing"}, "unreadable"),
        (set(), "not_consulted"),
    ],
)
def test_run_state_never_summarises_a_failed_query_as_a_search(states, expected):
    assert summarize_lookup_states("offline_catalog", states) == expected


def _unverified_data(**extra) -> dict:
    """The record of one assigned LCSC number no source resolved."""
    return {
        "order_boards": 5,
        "inventory_covered": [],
        "strict": False,
        "parts": [],
        "unresolved": ["C999"],
        **extra,
    }


OLD_UNVERIFIED_MESSAGE = (
    "Stock not verified: 1 assigned LCSC number(s) found in neither the parts cache "
    "nor the offline catalog"
)


def test_data_without_sources_makes_no_claim():
    # A bundle recorded before issue #10386 carries no ``sources``: its verdict
    # and message are the old ones, and nothing is said about source availability.
    legacy = _unverified_data()
    legacy_result = evaluate_lcsc_stock(legacy, 5, frozenset(), False)
    assert (legacy_result.status, legacy_result.message) == ("WARN", OLD_UNVERIFIED_MESSAGE)

    recorded = _unverified_data(sources={"parts_cache": "miss", "offline_catalog": "missing"})
    recorded_result = evaluate_lcsc_stock(recorded, 5, frozenset(), False)
    assert recorded_result.status == legacy_result.status
    assert recorded_result.message == OLD_UNVERIFIED_MESSAGE + (
        "; parts cache searched, no matching row; "
        "offline catalog not available on the generating host (no catalog file)"
    )
    # Strict still turns either into the same FAIL.
    assert (
        evaluate_lcsc_stock(legacy, 5, frozenset(), True).status
        == evaluate_lcsc_stock(recorded, 5, frozenset(), True).status
        == "FAIL"
    )


def test_describe_lookup_sources_says_nothing_without_a_record():
    assert describe_lookup_sources(None) == ""
    assert describe_lookup_sources({}) == ""
    assert describe_lookup_sources({"parts_cache": "hit"}) == "parts cache searched, match found"
    # A state this version does not know is shown, not dropped or guessed at.
    assert describe_lookup_sources({"parts_cache": "future-state"}) == "parts cache future-state"


# ---- readiness gate -------------------------------------------------------

from kicad_tools.cli import readiness_cmd  # noqa: E402
from tests.test_kct_readiness_cmd import FakeEngines, make_board  # noqa: E402


def _stock_entry(stock=2):
    data = {
        "order_boards": 5,
        "inventory_covered": [],
        "strict": False,
        "parts": [
            {
                "lcsc": "C1",
                "refs": ["Y2"],
                "per_board": 1,
                "stock": stock,
                "verified": True,
                "source": "live",
                "age_days": 0,
            }
        ],
        "unresolved": [],
    }
    res = evaluate_lcsc_stock(data, 5, frozenset(), False)
    return res.to_dict()


def _gate(board, entry, *flags):
    fake = FakeEngines(board, stock_check=entry)
    code = readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--generate", *flags], engines=fake.bundle()
    )
    out = board / "output"
    path = next(
        c
        for c in (out / "readiness-attempt" / "readiness.json", out / "readiness.json")
        if c.is_file()
    )
    return code, json.loads(path.read_text())


def test_gate_fails_by_default_passes_with_quantity_or_covered(tmp_path):
    board = make_board(tmp_path)
    entry = _stock_entry()
    code, report = _gate(board, entry)
    assert code != 0
    assert any("Y2 -> C1: needs 5, stock 2 (live)" in b for b in report["blockers"])


@pytest.mark.parametrize("flags", [("--quantity", "2"), ("--inventory-covered", "c1,")])
def test_gate_honours_flags(tmp_path, flags):
    board = make_board(tmp_path)
    code, report = _gate(board, _stock_entry(), *flags)
    bom = next(c for c in report["checks"] if c["name"] == "bom")
    assert bom["status"] != "failed", bom
    assert report["metrics"]["lcsc_stock_order"]["order_boards"] in (2, 5)
    readme = next((board / "output").rglob("README.txt")).read_text()
    assert "LCSC stock order" in readme
    if "--inventory-covered" in flags:
        assert "held in operator inventory, not stock-verified: C1" in readme


def test_legacy_manifest_without_data_refuses(tmp_path):
    board = make_board(tmp_path)
    entry = {"name": "bom_lcsc_stock", "status": "OK", "message": "ok"}
    code, report = _gate(board, entry, "--quantity", "2")
    bom = next(c for c in report["checks"] if c["name"] == "bom")
    assert bom["status"] == "failed"
    assert any("regenerate" in b for b in report["blockers"])
    # No flags: legacy verdict stands.
    board2 = make_board(tmp_path / "b")
    _, report2 = _gate(board2, entry)
    assert next(c for c in report2["checks"] if c["name"] == "bom")["status"] != "failed"


def test_quantity_zero_rejected(tmp_path, capsys):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    code = readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--generate", "--quantity", "0"], engines=fake.bundle()
    )
    assert code != 0


def test_verify_reevaluates_recorded_data_and_keeps_recorded_values(tmp_path):
    board = make_board(tmp_path)
    entry = _stock_entry()
    entry["data"]["order_boards"] = 2  # what a real --quantity 2 export records
    code, _ = _gate(board, entry, "--quantity", "2")
    assert code == 0
    out = board / "output"
    # No flags: verify keeps the recorded order size, so it stays passing.
    fake = FakeEngines(board)
    assert readiness_cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle()) == 0
    # Explicit larger quantity re-evaluates the recorded stock and blocks.
    fake = FakeEngines(board)
    code = readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--quantity", "5"], engines=fake.bundle()
    )
    assert code != 0
    assert out.exists()


# ---- recorded provenance, generate -> verify (issue #10386) ----------------


def _produced_entry(host: Path, parts, refs, condition: str = "missing", rows=None) -> dict:
    """The ``bom_lcsc_stock`` entry a real export records on a given host."""
    host.mkdir()
    _give_host_catalog(host, condition, rows)
    return _run(host, parts, refs).to_dict()


def _verify(board: Path) -> dict:
    """Verify the retained bundle as this host; return the verification receipt."""
    fake = FakeEngines(board)
    assert readiness_cmd.main([str(board), "--mfr", "jlcpcb"], engines=fake.bundle()) == 0
    assert "export" not in fake.calls
    return json.loads((board / "output" / "readiness-verification" / "readiness.json").read_text())


def _bundle_record(board: Path) -> tuple[dict, str]:
    """The ``bom_lcsc_stock`` entry the published manifest carries, and the README."""
    readme_path = next((board / "output").rglob("README.txt"))
    manifest = json.loads((readme_path.parent / "manifest.json").read_text())
    entry = next(e for e in manifest["preflight"] if e["name"] == "bom_lcsc_stock")
    return entry, readme_path.read_text()


def _host_with_catalog(lookup_dir: Path, monkeypatch, lcsc: str = "C999") -> None:
    """Make ``lookup_dir`` this host's cache dir, with a catalog listing ``lcsc``.

    Returns only once a default-path lookup really resolves ``lcsc``, so the
    premise of the tests that use it -- this host's data differs from the
    producer's -- is checked rather than assumed.
    """
    _give_host_catalog(lookup_dir, "hit", [_row(lcsc)])
    local = lambda: lookup_dir / "parts.db"  # noqa: E731
    monkeypatch.setattr("kicad_tools.parts.cache.get_default_cache_path", local)
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_default_cache_path", local)
    assert JlcpartsCatalog(get_catalog_path()).lookup(lcsc) is not None


@pytest.mark.parametrize("condition", CATALOG_CONDITIONS)
def test_bundle_names_the_catalog_condition(tmp_path, condition):
    # Same assignment, empty cache: only the host's catalog differs.
    entry = _produced_entry(tmp_path / "producer", [], [("R1", "C999")], condition)
    assert entry["status"] == "WARN"

    board = make_board(tmp_path)
    code, report = _gate(board, entry)
    assert code == 0  # the WARN never blocked; it now explains itself too
    metric = report["metrics"]["lcsc_stock_order"]
    assert metric["sources"] == {"parts_cache": "miss", "offline_catalog": condition}
    assert metric["message"] == entry["message"]
    recorded, readme = _bundle_record(board)
    assert recorded["data"]["sources"] == metric["sources"]
    assert recorded["data"]["unresolved"] == ["C999"]
    assert (
        f"  parts data sources: parts cache searched, no matching row; "
        f"{CATALOG_CONDITION_TEXT[condition]}\n"
    ) in readme
    assert f"  stock check WARN: {entry['message']}\n" in readme


def test_verify_keeps_the_producers_provenance_whatever_this_host_has(tmp_path, monkeypatch):
    # Produced on a worker with no offline catalog, the situation in #10386.
    entry = _produced_entry(tmp_path / "producer", [], [("R1", "C999")])
    board = make_board(tmp_path)
    code, report = _gate(board, entry)
    assert code == 0
    metric = report["metrics"]["lcsc_stock_order"]
    assert metric["sources"] == {"parts_cache": "miss", "offline_catalog": "missing"}

    # This host's catalog lists the part: had verification looked, it would
    # have found it.  The record stays the producer's.
    _host_with_catalog(tmp_path / "verifier", monkeypatch)
    assert _verify(board)["metrics"]["lcsc_stock_order"] == metric


def test_bundle_keeps_partial_coverage_with_its_provenance(tmp_path):
    refs = [("Y2", "C1"), ("R1", "C999")]
    entry = _produced_entry(tmp_path / "producer", [_part("C1", 500)], refs)

    board = make_board(tmp_path)
    code, report = _gate(board, entry)
    assert code == 0
    metric = report["metrics"]["lcsc_stock_order"]
    assert (metric["status"], metric["message"]) == (entry["status"], entry["message"])
    assert metric["sources"] == {"parts_cache": "hit", "offline_catalog": "missing"}
    recorded, readme = _bundle_record(board)
    assert [p["lcsc"] for p in recorded["data"]["parts"]] == ["C1"]
    assert recorded["data"]["unresolved"] == ["C999"]
    assert CATALOG_CONDITION_TEXT["missing"] in readme
    assert _verify(board)["metrics"]["lcsc_stock_order"] == metric


def test_legacy_bundle_without_sources_verifies_and_makes_no_claim(tmp_path, monkeypatch):
    # What a bundle recorded before issue #10386 looks like: per-part data, no
    # record of where the lookups reached.
    legacy = {
        "name": "bom_lcsc_stock",
        "status": "WARN",
        "message": OLD_UNVERIFIED_MESSAGE,
        "details": "C999",
        "data": _unverified_data(),
    }
    board = make_board(tmp_path)
    code, report = _gate(board, legacy)
    assert code == 0
    metric = report["metrics"]["lcsc_stock_order"]
    assert metric["message"] == OLD_UNVERIFIED_MESSAGE
    assert "sources" not in metric
    _, readme = _bundle_record(board)
    assert "parts data sources" not in readme
    assert f"  stock check WARN: {OLD_UNVERIFIED_MESSAGE}\n" in readme

    # A host that does have the catalog verifies it under the same rules and
    # still claims nothing about source availability.
    _host_with_catalog(tmp_path / "verifier", monkeypatch)
    assert _verify(board)["metrics"]["lcsc_stock_order"] == metric
