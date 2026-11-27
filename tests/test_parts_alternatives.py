"""Tests for ``kct parts alternatives`` (offline Basic-part substitution)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

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
STOCK_BY_LCSC = {f"C{r[0]}": r[5] for r in ROWS}
ROW_KEYS = {
    "lcsc",
    "mfr_part",
    "package",
    "library_type",
    "stock",
    "unit_price",
    "inventory",
}


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


def _assert_offline_catalog_provenance(row):
    inventory = row["inventory"]
    assert set(inventory) == set(Part(lcsc_part="C0").inventory_provenance())
    assert inventory["source"] == "offline_catalog"
    assert inventory["observed_at"] is None
    assert inventory["snapshot_revision"] is None
    assert inventory["snapshot_at"] is None
    assert inventory["from_cache"] is False
    assert inventory["stock_verified"] is False
    assert inventory["verification_max_age_hours"] == 24
    assert inventory["read_at"] is not None
    datetime.fromisoformat(inventory["read_at"])


def test_cli_json(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C100", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"]["lcsc"] == "C100"
    ids = [a["lcsc"] for a in data["alternatives"]]
    assert ids == ["C102"]
    for item in (data["source"], *data["alternatives"]):
        assert set(item) == ROW_KEYS
        assert type(item["stock"]) is int
        assert item["stock"] == STOCK_BY_LCSC[item["lcsc"]]
        _assert_offline_catalog_provenance(item)
    assert data["alternatives"][0]["library_type"] == "preferred"


def test_cli_json_source_provenance_without_alternatives(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C201", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["alternatives"] == []
    assert set(data["source"]) == ROW_KEYS
    assert data["source"]["stock"] == STOCK_BY_LCSC["C201"]
    _assert_offline_catalog_provenance(data["source"])


def test_cli_text_and_limit(catalog_db, capsys):
    assert parts_cmd.main(["alternatives", "C100", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "C102" in out and "C101" not in out
    _, header, _, c102 = out.splitlines()
    heading = "Offline catalog stock"
    assert heading in header
    stock_end = header.index(heading) + len(heading)
    assert c102[:stock_end].endswith(str(STOCK_BY_LCSC["C102"]))


def test_looser_tolerance_is_excluded(catalog_db, capsys):
    # C201 is cheaper but widens tolerance from 1% to 5%.
    assert parts_cmd.main(["alternatives", "C200", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["lcsc"] for a in data["alternatives"]] == []
    assert parts_cmd.main(["alternatives", "C201", "--format", "json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["alternatives"] == []
    _assert_offline_catalog_provenance(empty["source"])
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
                (214, "ap63203wu-7", "TSOT-23-6", "Diodes", "basic", "-40℃~+85℃ Fixed", "{}"),
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


def _insert(db, code, mpn, package, tier, desc, attrs, stock=50, manufacturer="M"):
    db.execute(
        "INSERT INTO jlc_components VALUES (?,?,?,?,?,?,'',?,'1-:0.01',?)",
        (code, mpn, package, manufacturer, tier, desc, stock, json.dumps(attrs)),
    )


def _cap(nominal="100nF", voltage="50V", tolerance="±10%"):
    return {
        "Capacitance": nominal,
        "Voltage Rating": voltage,
        "Temperature Coefficient": "X7R",
        "Tolerance": tolerance,
    }


def _alternatives(capsys, code):
    assert parts_cmd.main(["alternatives", code, "--format", "json"]) == 0
    return [p["lcsc"] for p in json.loads(capsys.readouterr().out)["alternatives"]]


def test_description_contradicting_nominal_is_excluded(catalog_db, capsys):
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 700, "A700", "0603", "extended", f"220nF 50V X7R {CAP_FAMILY}", _cap("220nF"))
        _insert(db, 701, "A701", "0603", "basic", f"2.2nF 50V X7R {CAP_FAMILY}", _cap("220nF"))
        _insert(db, 702, "A702", "0603", "basic", f"0.22µF 50V X7R {CAP_FAMILY}", _cap("0.22µF"))
        _insert(db, 703, "A703", "0603", "preferred", f"220nF 50V X7R {CAP_FAMILY}", _cap("220nF"))
    assert _alternatives(capsys, "C700") == ["C702", "C703"]


def test_unparseable_nominal_requires_exact_device(catalog_db, capsys):
    attrs = {
        "Resistance": "See datasheet",
        "Type": "Thick Film Resistor",
        "Tolerance": "1%",
        "Power(Watts)": "1W",
    }
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 710, "R710", "0603", "extended", f"1% {RES_FAMILY}", attrs)
        _insert(db, 711, "R711", "0603", "basic", f"1% {RES_FAMILY}", attrs)
        _insert(db, 712, "R710", "0603", "basic", f"1% {RES_FAMILY}", attrs)
    assert _alternatives(capsys, "C710") == ["C712"]


def test_unreadable_source_temperature_range_rejects_substitutes(catalog_db, capsys):
    with sqlite3.connect(catalog_db) as db:
        _insert(
            db,
            720,
            "T720",
            "0603",
            "extended",
            f"100nF 50V X7R -55°C ... +125°C {CAP_FAMILY}",
            _cap(),
        )
        _insert(
            db, 721, "T721", "0603", "basic", f"100nF 50V X7R -40°C ~ +85°C {CAP_FAMILY}", _cap()
        )
        _insert(db, 722, "T722", "0603", "basic", f"100nF 50V X7R {CAP_FAMILY}", _cap())
        _insert(
            db, 723, "T723", "0603", "basic", f"100nF 50V X7R -55°C ~ +150°C {CAP_FAMILY}", _cap()
        )
    assert _alternatives(capsys, "C720") == ["C723"]


def test_original_schema_without_attributes_still_finds_c102(tmp_path, monkeypatch, capsys):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE jlc_components (lcsc INTEGER PRIMARY KEY, mfr TEXT, package TEXT,"
            " manufacturer TEXT, library_type TEXT, description TEXT, datasheet TEXT,"
            " stock INTEGER, price TEXT)"
        )
        conn.executemany("INSERT INTO jlc_components VALUES (?,?,?,'M',?,?,'',?,?)", ROWS)
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: path)
    assert _alternatives(capsys, "C100") == ["C102"]
    assert parts_cmd.main(["alternatives", "C100", "--limit", "1"]) == 0
    assert "C102" in capsys.readouterr().out


def test_incompatible_rows_cannot_displace_valid_matches(catalog_db, capsys):
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 900, "B900", "0603", "extended", f"220nF 50V X7R {CAP_FAMILY}", _cap("220nF"))
        _insert(db, 901, "B901", "0603", "basic", f"220nF 50V X7R {CAP_FAMILY}", _cap("220nF"), 1)
        for i in range(600):
            _insert(
                db,
                1000 + i,
                f"B900X{i}",
                "0603",
                "basic",
                f"220nF 16V X7R {CAP_FAMILY}",
                _cap("220nF", "16V"),
                stock=5000,
            )
    assert _alternatives(capsys, "C900") == ["C901"]


def test_exact_device_not_displaced_by_prefix_matches(catalog_db, capsys):
    attrs = {"Resistance": "See datasheet", "Type": "Thick Film Resistor"}
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 950, "Q950", "0603", "extended", f"{RES_FAMILY}", attrs)
        _insert(db, 951, "Q950", "0603", "basic", f"{RES_FAMILY}", attrs, stock=1)
        for i in range(600):
            _insert(db, 3000 + i, f"Q950-{i}", "0603", "basic", f"{RES_FAMILY}", attrs, stock=9000)
    assert _alternatives(capsys, "C950") == ["C951"]


@pytest.mark.parametrize(
    ("family", "attributes", "other"),
    [
        (CAP_FAMILY, _cap(), "100nF 50V X7R Three-Terminal Feedthrough EMI Filter"),
        (RES_FAMILY, _attributes("10kΩ 1% 0603")[1], "10kΩ Four-Resistor Network"),
    ],
)
def test_numeric_attributes_do_not_establish_family(catalog_db, capsys, family, attributes, other):
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 800, "SRC", "0805", "extended", family, attributes)
        _insert(db, 801, "OTHER", "0805", "basic", other, attributes)
        _insert(db, 802, "GOOD", "0805", "basic", family, attributes)
    assert _alternatives(capsys, "C800") == ["C802"]


@pytest.mark.parametrize(
    ("family", "attributes", "bad_description"),
    [
        (CAP_FAMILY, _cap(), "1nF 50V X7R"),
        (CAP_FAMILY, _cap(), "100nF 50V Y5V"),
        (RES_FAMILY, _attributes("10kΩ 1% 0603")[1], "30Ω"),
    ],
)
def test_exact_mpn_rejects_contradictions(catalog_db, capsys, family, attributes, bad_description):
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 800, "SAME", "0805", "extended", family, attributes)
        _insert(db, 801, "SAME", "0805", "basic", bad_description, attributes)
        _insert(db, 802, "SAME", "0805", "basic", family, attributes)
    assert _alternatives(capsys, "C800") == ["C802"]


def test_resistor_coefficient_in_description(catalog_db, capsys):
    attrs = _attributes("10kΩ 1% 0603")[1]
    with sqlite3.connect(catalog_db) as db:
        for code, coefficient in [
            (800, "±25ppm/°C"),
            (801, "±200ppm/°C"),
            (802, ""),
            (803, "±25ppm/°C"),
            (804, "10 ppm/℃"),
        ]:
            tier = "extended" if code == 800 else "basic"
            _insert(db, code, str(code), "0805", tier, f"{RES_FAMILY} {coefficient}", attrs)
    assert _alternatives(capsys, "C800") == ["C803", "C804"]


@pytest.mark.parametrize(("source", "alternative"), [("10kΩ", "10 kOhm"), ("0Ω", "0 Ohms")])
def test_equivalent_resistance_spellings(catalog_db, capsys, source, alternative):
    attrs = _attributes("10kΩ 1% 0603")[1]
    with sqlite3.connect(catalog_db) as db:
        for code, nominal, tier in [(800, source, "extended"), (801, alternative, "basic")]:
            _insert(db, code, str(code), "0805", tier, RES_FAMILY, {**attrs, "Resistance": nominal})
    assert _alternatives(capsys, "C800") == ["C801"]


def test_legacy_mlcc_description_and_equivalent_spellings(catalog_db, capsys):
    with sqlite3.connect(catalog_db) as db:
        for code, nominal, tier in [
            (800, "100nF", "extended"),
            (801, "100 nF", "basic"),
            (802, "0.10µF", "basic"),
        ]:
            _insert(db, code, str(code), "0805", tier, f"{nominal} 50V ±10% X7R {CAP_FAMILY}", {})
        db.execute("ALTER TABLE jlc_components DROP COLUMN attributes")
    assert _alternatives(capsys, "C800") == ["C801", "C802"]


@pytest.mark.parametrize(
    "description,wrong",
    [
        ("Fixed Output 3.3V Buck Regulator", "Fixed Output 5V Buck Regulator"),
        ("32V Input 3.3V Fixed Output", "32V Fixed Output 3.3V Input"),
        ("32V Input 3.3V Fixed Output Buck Regulator", "32V Input 5V Fixed Output Buck Regulator"),
    ],
)
def test_exact_regulator_voltage(catalog_db, capsys, description, wrong):
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 800, "BUCK", "SOT23", "extended", description, {})
        _insert(db, 801, "BUCK", "SOT23", "basic", wrong, {})
        _insert(db, 802, "BUCK", "SOT23", "basic", description, {})
    assert _alternatives(capsys, "C800") == ["C802"]


def test_resistor_dc_working_voltage(catalog_db, capsys):
    attrs = _attributes("10kΩ 1% 0603")[1]
    with sqlite3.connect(catalog_db) as db:
        for code, voltage in enumerate(("200VDC", "50VDC", "", "200VDC", "250 VDC"), 800):
            tier = "extended" if code == 800 else "basic"
            _insert(db, code, str(code), "0603", tier, f"{RES_FAMILY} {voltage}", attrs)
    assert _alternatives(capsys, "C800") == ["C803", "C804"]


def test_mlcc_safety_class(catalog_db, capsys):
    with sqlite3.connect(catalog_db) as db:
        for code, safety in enumerate(("X1/Y2 Safety Certified", "", "X1", "X1/Y2"), 800):
            tier = "extended" if code == 800 else "basic"
            _insert(db, code, str(code), "2220", tier, f"{CAP_FAMILY} {safety}", _cap())
    assert _alternatives(capsys, "C800") == ["C803"]


def test_absolute_capacitance_tolerance(catalog_db, capsys):
    with sqlite3.connect(catalog_db) as db:
        for code, tol in enumerate(("±0.5pF", "±0.25pF", "±1pF", "±0.5pF", "±5%"), 800):
            tier = "extended" if code == 800 else "basic"
            attrs = {**_cap("10pF", tolerance=tol), "Temperature Coefficient": "C0G"}
            desc = f"10pF 50V {tol} C0G {CAP_FAMILY}"
            _insert(db, code, str(code), "0402", tier, desc, attrs)
    assert _alternatives(capsys, "C800") == ["C801", "C803", "C804"]


@pytest.mark.parametrize(
    ("source", "kept"),
    [
        ("250 V AC", ["250 V AC", "400VAC", "400 V AC"]),
        ("250VAC", ["250 V AC", "400VAC", "400 V AC"]),
        ("250 V DC", ["250 V DC", "250V"]),
    ],
)
def test_mlcc_ac_dc_ratings_stay_distinct(catalog_db, capsys, source, kept):
    ratings = ["250 V DC", "250 V AC", "100 V AC", "400VAC", "250V", "400 V AC", "250 V DC"]
    attrs = {k: v for k, v in _cap("1nF").items() if k != "Voltage Rating"}
    attrs["Temperature Coefficient"] = "C0G"
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 800, "SRC", "2220", "extended", f"1 nF {source} C0G {CAP_FAMILY}", attrs)
        for code, rating in enumerate(ratings, 801):
            desc = f"1 nF {rating} C0G {CAP_FAMILY}"
            _insert(db, code, f"A{code}", "2220", "basic", desc, attrs)
    expected = [f"C{c}" for c, r in enumerate(ratings, 801) if r in kept]
    assert sorted(_alternatives(capsys, "C800")) == sorted(expected)


def test_resistor_ac_working_voltage(catalog_db, capsys):
    attrs = _attributes("10kΩ 1% 0603")[1]
    with sqlite3.connect(catalog_db) as db:
        for code, voltage in enumerate(("200VAC", "50VAC", "", "200VAC", "250 V AC"), 800):
            tier = "extended" if code == 800 else "basic"
            _insert(db, code, str(code), "0603", tier, f"{RES_FAMILY} {voltage}", attrs)
    assert _alternatives(capsys, "C800") == ["C803", "C804"]


@pytest.mark.parametrize(
    ("description", "kept", "wrong"),
    [
        (
            "Fixed Output -5V Linear Regulator",
            "Fixed Output -5V Linear Regulator",
            ["Fixed Output -12V Linear Regulator", "Fixed Output +5V Linear Regulator"],
        ),
        (
            "Input 32V Output 3.3V",
            "Output 3.3V Input 32V",
            ["Input 32V Output 5V", "Input 3.3V Output 32V"],
        ),
    ],
)
def test_regulator_voltage_polarity_and_role(catalog_db, capsys, description, kept, wrong):
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 800, "REG", "SOT23", "extended", description, {})
        for code, text in enumerate(wrong, 801):
            _insert(db, code, "REG", "SOT23", "basic", text, {})
        _insert(db, 810, "REG", "SOT23", "basic", kept, {})
        _insert(db, 811, "REG", "SOT23", "basic", description, {})
    assert _alternatives(capsys, "C800") == ["C810", "C811"]


@pytest.mark.parametrize("legacy", [False, True], ids=["attributes", "legacy"])
@pytest.mark.parametrize(
    ("source", "equivalent", "conflicting"),
    [
        ("Output ±5V", "Output +/-5V", "Output +5V"),
        ("VIN 32V VOUT 3.3V", "VOUT 3.3V VIN 32V", "VIN 3.3V VOUT 32V"),
        ("Input 32V Output 3.3V", "VOUT 3.3V VIN 32V", "VIN 3.3V VOUT 32V"),
        ("Fixed Output 3V3", "Fixed Output 3.3V", "Fixed Output 5V"),
        ("Fixed Output 1V8", "Fixed Output 1.8V", "Fixed Output 5V"),
        ("Fixed Output -1V8", "Fixed Output -1.8V", "Fixed Output +1V8"),
        ("Input 4.5V~32V Output 3.3V", "Output 3V3 Input 4.5V~32V", "Input 4V~32V Output 3V3"),
        ("Input 32V Output 0.8V~24V", "Output 0.8V~24V Input 32V", "Output 0.8V~32V Input 24V"),
        ("4.5–32V Input 3V3 Output", "3.3V Output 4.5V-32V Input", "3.3V Input 4.5V-32V Output"),
        (
            "Input 450...900mV Output 1V8",
            "Output 1.8V Input 0.45V to 0.9V",
            "Output 1.8V Input 450...950mV",
        ),
    ],
)
def test_regulator_voltage_notation(catalog_db, capsys, legacy, source, equivalent, conflicting):
    with sqlite3.connect(catalog_db) as db:
        for code, tier, description in (
            (800, "extended", source),
            (801, "basic", equivalent),
            (802, "basic", conflicting),
            (803, "basic", source),
        ):
            _insert(db, code, "AP63203WU-7", "SOT-23-6", tier, f"{description} Buck", {})
        if legacy:
            db.execute("ALTER TABLE jlc_components DROP COLUMN attributes")
    assert _alternatives(capsys, "C800") == ["C801", "C803"]


@pytest.mark.parametrize("legacy", [True, False])
def test_resistor_type_from_catalog_description(catalog_db, capsys, legacy):
    desc = "-55℃~+155℃ 1W 200V 8.2Ω Thick Film Resistor ±1% ±400ppm/℃ 2512 " + RES_FAMILY
    better = desc.replace("400ppm", "200ppm")
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 175238, "CR2512F8R20E04S", "2512", "extended", desc, {})
        _insert(db, 35219, "25121WF820KT4E", "2512", "basic", better, {})
        _insert(db, 800, "R800", "2512", "basic", better.replace("Thick", "Thin"), {})
        if legacy:
            db.execute("ALTER TABLE jlc_components DROP COLUMN attributes")
        else:
            _insert(db, 801, "R801", "2512", "basic", better, {"Type": "Thin Film Resistor"})
    assert _alternatives(capsys, "C175238") == ["C35219"]


@pytest.mark.parametrize("legacy", [True, False])
def test_dielectric_aliases(catalog_db, capsys, legacy):
    with sqlite3.connect(catalog_db) as db:
        for code, name in enumerate(("C0G/NP0", "NP0", "C0G/NP0", "X7R", "C0G/X7R"), 800):
            tier = "extended" if code == 800 else "basic"
            _insert(
                db,
                code,
                str(code),
                "0402",
                tier,
                f"10pF 50V ±5% {name}",
                {"Temperature Coefficient": name},
            )
        if legacy:
            db.execute("ALTER TABLE jlc_components DROP COLUMN attributes")
    assert _alternatives(capsys, "C800") == ["C801", "C802"]


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize(
    "description,source,tighter,looser_lower,looser_upper",
    [
        ("1µF 16V Y5V {} 0805 " + CAP_FAMILY, "-20%~+80%", "±10%", "±80%", "-10%~+90%"),
        ("1W 8.2Ω Thick Film {} " + RES_FAMILY, "-1%~+2%", "±1%", "±5%", "-1%~+3%"),
    ],
)
def test_asymmetric_tolerance(
    catalog_db, capsys, legacy, description, source, tighter, looser_lower, looser_upper
):
    with sqlite3.connect(catalog_db) as db:
        for code, mpn, tolerance, attribute in (
            (800, "SRC", source, source),
            (801, "EQUAL", source, source),
            (802, "TIGHTER", tighter, tighter),
            (803, "SRC", looser_lower, looser_lower),
            (804, "SRC", looser_upper, looser_upper),
            (805, "SRC", "-?%~+1%", tighter),
        ):
            _insert(
                db,
                code,
                mpn,
                "0805",
                "extended" if code == 800 else "basic",
                description.format(tolerance),
                {"Tolerance": attribute},
            )
        if legacy:
            db.execute("ALTER TABLE jlc_components DROP COLUMN attributes")
        else:
            _insert(
                db, 806, "SRC", "0805", "basic", description.format(source), {"Tolerance": tighter}
            )
    assert _alternatives(capsys, "C800") == ["C801", "C802"]
    with sqlite3.connect(catalog_db) as db:
        db.execute(
            "UPDATE jlc_components SET description=? WHERE lcsc=800",
            (description.format("-?%~+1%"),),
        )
    assert _alternatives(capsys, "C800") == []


def test_resistor_catalog_maximum_voltage(catalog_db, capsys):
    attrs = _attributes("16kΩ 1% 1206")[1]
    with sqlite3.connect(catalog_db) as db:
        for code, stated, rated in (
            (4436, "150V", "150V"),
            (25829, "200V", "200V"),
            (801, "", "100V"),
            (802, "150V", "200V"),
            (803, "200V", "unknown voltage"),
        ):
            _insert(
                db,
                code,
                str(code),
                "1206",
                "extended" if code == 4436 else "basic",
                f"{RES_FAMILY} {stated}",
                attrs | {"Voltage-Supply(Max)": rated},
            )
    assert _alternatives(capsys, "C4436") == ["C25829"]


@pytest.mark.parametrize("legacy", [False, True])
def test_discrete_inductor_ratings(catalog_db, capsys, legacy):
    description = "1uH 400mΩ 45@10MHz 50mA 75MHz Multilayer inductor ±10% 0805 Inductors (SMD) ROHS"
    attrs = {
        "Inductance": "1uH",
        "Rated Current": "50mA",
        "DC Resistance": "400mΩ",
        "Type": "Multilayer inductor",
        "Q @ Frequency": "45@10MHz",
        "Frequency - Self Resonant": "75MHz",
        "Tolerance": "±10%",
    }
    with sqlite3.connect(catalog_db) as db:
        _insert(db, 281088, "SOURCE", "0805", "extended", description, attrs)
        _insert(db, 1042, "SUB", "0805", "preferred", description, attrs)
        for code, (old, new, key) in enumerate(
            (
                ("50mA", "30mA", "Rated Current"),
                ("1uH", "10uH", "Inductance"),
                ("400mΩ", "800mΩ", "DC Resistance"),
                ("45@10MHz", "", "Q @ Frequency"),
                ("75MHz", "55MHz", "Frequency - Self Resonant"),
            ),
            800,
        ):
            changed = attrs.copy()
            if new:
                changed[key] = new
            else:
                del changed[key]
            _insert(db, code, str(code), "0805", "basic", description.replace(old, new), changed)
        if legacy:
            db.execute("ALTER TABLE jlc_components DROP COLUMN attributes")
    assert _alternatives(capsys, "C281088") == ["C1042"]
