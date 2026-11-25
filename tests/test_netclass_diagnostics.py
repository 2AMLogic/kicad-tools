"""Read-only diagnostic contract and independently captured KiCad oracle."""

import copy
import json
from pathlib import Path

import pytest

from kicad_tools.core.netclass_diagnostics import diagnose_netclasses, exact_netclass_pattern


def project(patterns=None):
    return {
        "net_settings": {
            "classes": [{"name": "Default"}, {"name": "Power"}],
            "netclass_patterns": patterns or [],
        }
    }


def codes(report):
    return [d["code"] for d in report["diagnostics"]]


def test_missing_target_and_duplicate_overlap_witness():
    data = project(
        [{"pattern": "USB_*", "netclass": "Missing"}, {"pattern": "USB_*", "netclass": "Power"}]
    )
    report = diagnose_netclasses(data, ["USB_D", "USB", "OTHER"])
    assert [c["name"] for c in report["classes"]] == ["Default", "Power"]
    assert [p["index"] for p in report["patterns"]] == [0, 1]
    assert report["patterns"][0]["target_defined"] is False
    assert report["patterns"][0]["matches"] == ["USB", "USB_D"]
    assert report["patterns"][1]["matches"] == ["USB", "USB_D"]
    assert "undefined_class" in codes(report)
    assert "effective_class" not in str(report)


def test_missing_settings_and_default_are_not_fabricated():
    assert diagnose_netclasses({})["settings_status"] == "missing"
    assert diagnose_netclasses({})["classes"] == []
    assert diagnose_netclasses({})["default_status"] == "absent"
    assert diagnose_netclasses(project())["default_status"] == "declared"


def test_inventory_absent_empty_and_no_match():
    data = project([{"pattern": "USB", "netclass": "Power"}])
    assert diagnose_netclasses(data)["patterns"][0]["matches"] is None
    for nets in ([], ["USB_D"]):
        r = diagnose_netclasses(data, nets)
        assert r["patterns"][0]["matches"] == []
        assert "no_matches" in codes(r)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        1,
        {"net_settings": None},
        {"net_settings": []},
        {"net_settings": {"classes": 4, "netclass_patterns": {}, "netclass_assignments": []}},
    ],
)
def test_malformed_containers(data):
    before = copy.deepcopy(data)
    assert diagnose_netclasses(data)["diagnostics"]
    assert data == before


def test_malformed_siblings_duplicates_and_explicit_assignments():
    data = {
        "net_settings": {
            "classes": [None, {}, {"name": "Power"}, {"name": "Power"}],
            "netclass_patterns": [
                None,
                {"pattern": 2, "netclass": "Missing"},
                {"pattern": "USB", "netclass": "Power"},
            ],
            "netclass_assignments": {"USB": ["Power", "Missing", "Power", None], "VCC": "Power"},
        }
    }
    r = diagnose_netclasses(data, ["USB"])
    assert len(r["classes"]) == 4
    assert len(r["patterns"]) == 3
    assert r["patterns"][2]["matches"] == ["USB"]
    assert "duplicate_class" in codes(r)
    assert "undefined_class" in codes(r)
    assert "unsupported_assignment" in codes(r)
    assert [x["target"] for x in r["assignments"][:4]] == ["Power", "Missing", "Power", None]


def test_deep_immutability_and_determinism():
    data = project([{"pattern": "USB", "netclass": "Power", "extra": [1]}])
    nets = ["Z", "USB", "USB"]
    before = copy.deepcopy((data, nets))
    r = diagnose_netclasses(data, nets)
    assert r == diagnose_netclasses(data, set(nets))
    r["patterns"][0]["entry"]["extra"].append(2)
    assert (data, nets) == before


def test_invalid_inventory_is_not_an_empty_inventory():
    r = diagnose_netclasses(project([{"pattern": "*", "netclass": "Power"}]), "USB")
    assert r["inventory_status"] == "invalid"
    assert r["patterns"][0]["matches"] is None
    r = diagnose_netclasses(project(), ["USB", 3])
    assert r["inventory_status"] == "invalid"


def test_native_oracle():
    corpus = json.loads(
        (Path(__file__).parent / "fixtures/netclass_diagnostics/oracle.json").read_text()
    )
    assert corpus["kicad_version"] == "10.0.6"
    # Rows whose syntax stays outside the verified subset (#6262 kept "/USB/"
    # evaluated): native results are boundary evidence, never implemented.
    unsupported = {
        r"USB\*",
        "USB[0-9]",
        "^USB$",
        "a**",
        "A|B",
        "(AB)*",
        "((A))",
        "A{2}",
        "Net-(U5-CAP)?",
    }
    for case in corpus["cases"]:
        p = {"pattern": case["pattern"], "netclass": "Power"}
        r = diagnose_netclasses(project([p]), [case["net"]])
        row = r["patterns"][0]
        if case["pattern"] in unsupported:
            assert row["status"] == "unsupported"
            assert row["matches"] is None
        else:
            assert row["matches"] == ([case["net"]] if case["matches"] else []), case


def test_unsupported_pattern_retains_undefined_target_and_original_fields():
    data = project([{"pattern": r"USB\*", "netclass": "Absent", "custom": 7}])
    r = diagnose_netclasses(data, [])
    assert r["patterns"][0]["entry"] == data["net_settings"]["netclass_patterns"][0]
    assert {"unsupported_pattern", "undefined_class"} <= set(codes(r))
    assert "no_matches" not in codes(r)


def test_punctuated_supply_and_generated_names_issue_10122():
    """The five #10122 rows: exact spellings, both generated spellings, negatives.

    Native DRC on pinned KiCad flagged the 0.450 mm gap against a 0.500 mm
    class for the three positive rows only; the similarly named negatives
    keep the default class. ``A.B``/``AXB`` and ``C+P``/``CCP`` pin that a
    blanket literal treatment of punctuation would miss real memberships.
    """
    nets = ["+3.3V", "+3x3V", "Net-(U5-CAPP)", "Net-U5-CAPP", "Net-(U5-CAPX)"]
    expectations = {
        "+3.3V": ["+3.3V"],
        "Net-(U5-CAPP)": ["Net-(U5-CAPP)", "Net-U5-CAPP"],
    }
    for pattern, expected in expectations.items():
        r = diagnose_netclasses(project([{"pattern": pattern, "netclass": "Power"}]), nets)
        assert r["patterns"][0]["status"] == "supported"
        assert r["patterns"][0]["matches"] == expected
    for pattern, net in [("A.B", "AXB"), ("C+P", "CCP"), ("C+P", "CP")]:
        r = diagnose_netclasses(project([{"pattern": pattern, "netclass": "Power"}]), [net])
        assert r["patterns"][0]["matches"] == [net], (pattern, net)


def test_group_and_quantifier_boundary_syntax_stays_unsupported():
    for pattern in ["A|B", "(AB)*", "((A))", "A{2}", "Net-(U5-CAP)?", "A(", "A)", "()", "(*)"]:
        r = diagnose_netclasses(project([{"pattern": pattern, "netclass": "Power"}]), ["AB"])
        assert r["patterns"][0]["status"] == "unsupported", pattern
        assert r["patterns"][0]["matches"] is None
        assert "unsupported_pattern" in codes(r)


def test_explicit_assignment_membership_empty_and_legacy_forms():
    data = project()
    data["net_settings"]["netclass_assignments"] = {
        "USB": ["Power", "Power"],
        "OTHER": [],
        "OLD": "Power",
    }
    r = diagnose_netclasses(data, [])
    assert [a["net_present"] for a in r["assignments"][:3]] == [False, False, False]
    assert r["assignments"][2]["entry"] == []
    assert r["assignments"][3]["status"] == "unsupported"
    assert diagnose_netclasses(data)["assignments"][0]["net_present"] is None


def test_legacy_class_assignments_cannot_silently_disappear():
    data = project()
    data["net_settings"]["classes"][0]["nets"] = ["USB"]
    assert "unsupported_assignment" in codes(diagnose_netclasses(data))


@pytest.mark.parametrize("patterns", [[{}, {"pattern": "USB", "netclass": []}], ["bad", 2]])
def test_invalid_pattern_and_target_entries_are_retained(patterns):
    data = project(patterns)
    before = copy.deepcopy(data)
    r = diagnose_netclasses(data, ["USB"])
    assert [p["entry"] for p in r["patterns"]] == patterns
    assert "invalid_target" in codes(r)
    assert data == before


def test_encoded_spelling_is_also_a_native_wildcard_match():
    net = "USB*"
    spelling = r"\AUSB\x2a\z"
    inventory = {net, "USB1", spelling, spelling + "\n"}

    def matches(pattern):
        return diagnose_netclasses(project([{"netclass": "Power", "pattern": pattern}]), inventory)[
            "patterns"
        ][0]["matches"]

    assert matches(spelling) == sorted({net, spelling, spelling + "\n"})
    assert matches(exact_netclass_pattern(net, inventory)) == [net]
