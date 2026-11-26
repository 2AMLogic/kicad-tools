"""Tests for ``kct parts alternatives`` (offline Basic-part substitution)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from kicad_tools.cli import parts_cmd
from kicad_tools.parts.alternatives import rank_alternatives
from kicad_tools.parts.models import Part, PartPrice

ROWS = [
    # lcsc, mfr, package, library_type, description, stock, price
    (100, "EXT-100N", "0603", "extended", "100nF 50V X7R 0603", 500, "1-:0.0100"),
    (101, "BASIC-100N", "0603", "basic", "100nF 25V X7R 0603", 9000, "1-:0.0050"),
    (102, "PREF-100N", "0603", "preferred", "100nF 50V X7R 0603", 800, "1-:0.0040"),
    (103, "EXT2-100N", "0603", "extended", "100nF 16V X7R 0603", 100, "1-:0.0030"),
    (104, "OOS-BASIC-100N", "0603", "basic", "100nF 10V X7R 0603", 0, "1-:0.0010"),
    (105, "OTHERPKG-100N", "0402", "basic", "100nF 10V X7R 0402", 900, "1-:0.0010"),
    (106, "PREFIX", "0603", "basic", "1100nF 50V X7R 0603", 900, "1-:0.0010"),
    (200, "BASIC-10K", "0603", "basic", "10kΩ 1% 0603", 100, "1-:0.0020"),
    (201, "BASIC-10K-CHEAP", "0603", "basic", "10kΩ 5% 0603", 100, "1-:0.0010"),
]


CAP_FAMILY = "Multilayer Ceramic Capacitors MLCC - SMD/SMT"
RES_FAMILY = "Chip Resistor - Surface Mount"


def _attributes(description):
    if "nF" in description:
        nominal, voltage, dielectric, _ = description.split()
        return CAP_FAMILY, {
            "Capacitance": nominal,
            "Voltage Rating": voltage,
            "Temperature Coefficient": dielectric,
            "Tolerance": "±10%",
        }
    resistance, tolerance, _ = description.split()
    return RES_FAMILY, {
        "Resistance": resistance,
        "Tolerance": tolerance,
        "Power(Watts)": "1W",
        "Type": "Thick Film Resistor",
    }


@pytest.fixture
def catalog_db(tmp_path, monkeypatch):
    path = tmp_path / "jlcparts.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE jlc_components (lcsc INTEGER PRIMARY KEY, mfr TEXT, package TEXT,"
            " manufacturer TEXT, library_type TEXT, description TEXT, datasheet TEXT,"
            " stock INTEGER, price TEXT, attributes TEXT)"
        )
        records = []
        for code, mpn, package, tier, desc, stock, price in ROWS:
            family, attrs = _attributes(desc)
            records.append(
                (code, mpn, package, tier, f"{desc} {family}", stock, price, json.dumps(attrs))
            )
        conn.executemany(
            "INSERT INTO jlc_components VALUES (?,?,?,'M',?,?,'',?,?,?)",
            records,
        )
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: path)
    return path


def _part(lcsc, tier, price, stock=10):
    return Part(
        lcsc_part=lcsc,
        description=RES_FAMILY,
        package="0603",
        specs=_attributes("10kΩ 1% 0603")[1],
        is_basic=tier == "basic",
        is_preferred=tier == "preferred",
        stock=stock,
        prices=[PartPrice(quantity=1, unit_price=price)] if price is not None else [],
    )


def test_rank_order_stock_and_self_exclusion():
    src = _part("C1", "extended", 0.01)
    cands = [
        src,
        _part("C2", "extended", 0.001),
        _part("C3", "preferred", 0.004),
        _part("C4", "basic", 0.005),
        _part("C5", "basic", 0.002, stock=0),
    ]
    assert [p.lcsc_part for p in rank_alternatives(src, cands)] == ["C4", "C3", "C2"]


def test_rank_basic_source_only_cheaper_same_tier():
    src = _part("C1", "basic", 0.005)
    cands = [_part("C2", "basic", 0.006), _part("C3", "basic", 0.001), _part("C4", "preferred", 0)]
    assert [p.lcsc_part for p in rank_alternatives(src, cands)] == ["C3"]


def test_rank_limit_and_empty():
    src = _part("C1", "extended", 1.0)
    assert rank_alternatives(src, [], 5) == []
    cands = [_part(f"C{i}", "basic", 0.1) for i in range(2, 9)]
    assert len(rank_alternatives(src, cands, 3)) == 3


def test_cli_json(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C100", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"]["lcsc"] == "C100"
    ids = [a["lcsc"] for a in data["alternatives"]]
    assert ids == ["C102"]
    assert set(data["alternatives"][0]) == {
        "lcsc",
        "mfr_part",
        "package",
        "library_type",
        "stock",
        "unit_price",
        "inventory",
    }
    for row in [data["source"], *data["alternatives"]]:
        _assert_inventory(row, 500 if row["lcsc"] == "C100" else 800)
    assert data["alternatives"][0]["library_type"] == "preferred"


def _assert_inventory(row, stock):
    assert type(row["stock"]) is int and row["stock"] == stock
    assert row["inventory"]["source"] == "offline_catalog"
    assert row["inventory"]["stock_verified"] is False
    assert row["inventory"]["observed_at"] is None
    assert row["inventory"]["read_at"]


def test_cli_text_and_limit(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C100", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "C102" in out and "C101" not in out and "800" in out
    assert "Offline catalog stock" in out


def test_looser_tolerance_is_excluded(catalog_db, capsys):
    # C201 is cheaper but widens tolerance from 1% to 5%.
    assert parts_cmd.main(["alternatives", "C200", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["lcsc"] for a in data["alternatives"]] == []
    assert parts_cmd.main(["alternatives", "C201", "--format", "json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["alternatives"] == []
    _assert_inventory(empty["source"], 100)
    assert parts_cmd.main(["alternatives", "C200"]) == 0
    assert "No better" in capsys.readouterr().out


def test_cli_unknown_part(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C999999"]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_missing_catalog(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: tmp_path / "none.sqlite3"
    )
    assert parts_cmd.main(["alternatives", "C100"]) == 1
    err = capsys.readouterr().err
    assert "sync-catalog" in err
    assert "Traceback" not in err


def test_incomplete_source_and_adjustable_buck(catalog_db, capsys):
    with sqlite3.connect(catalog_db) as db:
        db.executemany(
            "INSERT INTO jlc_components VALUES (?,?,?,?,?,?,'',50,'1-:0.01',?)",
            [
                (346540, "RHC2512FT4K70", "2512", "", "extended", f"2512 {RES_FAMILY}", "{}"),
                (25483, "30ROHM", "2512", "M", "basic", f"30Ω 2512 {RES_FAMILY}", "{}"),
                (780769, "AP63203WU-7", "TSOT-23-6", "Diodes", "extended", "-40℃~+85℃ Fixed", "{}"),
                (
                    2070724,
                    "TPS562200DDCT",
                    "TSOT-23-6",
                    "TI",
                    "basic",
                    "-40℃~+85℃ Adjustable",
                    "{}",
                ),
                (212, "AP63203WU-7B", "TSOT-23-6", "Diodes", "basic", "-40℃~+85℃ Fixed", "{}"),
                (213, "AP63203WU-7", "TSOT-23-6", "Other", "basic", "Fixed", "{}"),
                (214, "AP63203WU-7", "TSOT-23-6", "Diodes", "basic", "-40℃~+85℃ Fixed", "{}"),
                (215, "AP63203WU-7", "SOT563", "Diodes", "basic", "-40℃~+85℃ Fixed", "{}"),
            ],
        )
    for code, expected in [("C346540", []), ("C780769", ["C214"])]:
        assert parts_cmd.main(["alternatives", code, "--format", "json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert [p["lcsc"] for p in data["alternatives"]] == expected


@pytest.mark.parametrize("attributes", [None, "{", "[]", '{"Resistance": ""}', '{"Resistance":42}'])
def test_unusable_attributes_require_exact_device(catalog_db, capsys, attributes):
    with sqlite3.connect(catalog_db) as db:
        db.execute("UPDATE jlc_components SET attributes=? WHERE lcsc=100", (attributes,))
    assert parts_cmd.main(["alternatives", "C100", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["alternatives"] == []


def test_1206_resistor_with_lower_power_is_excluded(catalog_db, capsys):
    family, attrs = _attributes("10kΩ 1% 1206")
    with sqlite3.connect(catalog_db) as db:
        for code, nominal, power, tier in [
            (301, "10kΩ", "2W", "extended"),
            (302, "10kΩ", "1W", "basic"),
            (303, "10kΩ", "3W", "basic"),
        ]:
            db.execute(
                "INSERT INTO jlc_components VALUES (?,?,'1206','M',?,?,'',50,'1-:0.01',?)",
                (
                    code,
                    str(code),
                    tier,
                    f"1206 {family}",
                    json.dumps({**attrs, "Resistance": nominal, "Power(Watts)": power}),
                ),
            )
    assert parts_cmd.main(["alternatives", "C301", "--format", "json"]) == 0
    assert [row["lcsc"] for row in json.loads(capsys.readouterr().out)["alternatives"]] == ["C303"]


@pytest.mark.parametrize("field", ["manufacturer", "mfr", "package"])
def test_placeholder_identity_is_excluded(catalog_db, capsys, field):
    with sqlite3.connect(catalog_db) as db:
        db.execute(
            "UPDATE jlc_components SET manufacturer='M', mfr='REG', "
            "description='Regulator', attributes=NULL WHERE lcsc IN (100,102)"
        )
        db.execute(f"UPDATE jlc_components SET {field}='-' WHERE lcsc IN (100,102)")
    assert parts_cmd.main(["alternatives", "C100", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["alternatives"] == []


@pytest.mark.parametrize(
    ("voltage", "temp", "tolerance", "expected"),
    [
        ("63V", "−55 to +150 °C", "±5%", ["C102"]),
        ("63V", "-40℃~+85℃", "±5%", []),
        ("63V", "", "±5%", []),
        ("25V", "−55 to +150 °C", "±5%", []),
        ("63V", "−55 to +150 °C", None, []),
    ],
)
def test_cli_published_limits(catalog_db, capsys, voltage, temp, tolerance, expected):
    with sqlite3.connect(catalog_db) as db:
        db.execute(
            "UPDATE jlc_components SET description=description || ' −55 to +125 °C' WHERE lcsc=100"
        )
        family, attrs = _attributes(f"100nF {voltage} X7R 0603")
        attrs.update({"Tolerance": tolerance, "Notes": "-"})
        db.execute(
            "UPDATE jlc_components SET description=?, attributes=?, mfr='EXT-100N' WHERE lcsc=102",
            (f"100nF {voltage} X7R 0603 {family} {temp}", json.dumps(attrs)),
        )
    assert parts_cmd.main(["alternatives", "C100", "--format", "json"]) == 0
    assert [p["lcsc"] for p in json.loads(capsys.readouterr().out)["alternatives"]] == expected


def test_conflicting_description_and_attribute_excluded(catalog_db, capsys):
    with sqlite3.connect(catalog_db) as db:
        db.execute(
            "UPDATE jlc_components SET description=replace(description,'50V','25V') WHERE lcsc=102"
        )
    assert parts_cmd.main(["alternatives", "C100", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["alternatives"] == []
