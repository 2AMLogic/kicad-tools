"""Simulated analog pin ranges bound to a schematic and to supplied traces."""

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.analysis import analog_pin_range
from kicad_tools.analysis.analog_pin_range import analyze_pin_ranges, main
from kicad_tools.operations import netlist as netlist_module

FIXTURES = Path(__file__).parent / "fixtures/analog_pin_range"
ORIGINAL = "line_sense_original"
BIASED = "line_sense_biased"
LIMITS = {"lower_v": 0.0, "upper_v": 3.3}
OFF_STATE_LIMITS = {"lower_v": -0.3, "upper_v": 0.3}
TOLERANCE = 5e-3


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_samples(path: Path, time_s, traces) -> None:
    payload = {
        "schema_version": 1,
        "source": "synthetic test trace",
        "time_s": list(time_s),
        "traces": {name: list(values) for name, values in traces.items()},
    }
    path.write_text(json.dumps(payload))


def assertion(reference, pin, net, trace, state="powered", limits=LIMITS):
    bound = {"state": state, "source": "synthetic fixture limit", **limits}
    return {"reference": reference, "pin": pin, "net": net, "trace": trace, "limits": bound}


def case(case_id, state, samples, rail_v, pins):
    return {
        "id": case_id,
        "waveform": "mains 187 V peak 60 Hz, zero offset",
        "state": state,
        "window": {"start_s": 0.05, "end_s": 0.1},
        "reference_net": "GND",
        "rails": {
            "VDD": {"net": "+3V3", "voltage_v": rail_v, "source": "ideal fixture supply"},
            "VSS": {"net": "GND", "voltage_v": 0.0, "source": "ideal fixture return"},
        },
        "samples": samples,
        "model": {
            "input_protection": "omitted",
            "assumptions": [
                "Ideal unity-gain buffer with a high-impedance input",
                "No input protection clamps on either input",
            ],
        },
        "pins": pins,
    }


def powered(case_id, samples):
    return case(
        case_id,
        "powered",
        samples,
        3.3,
        [assertion("U1", "1", "LINE_SENSE", "v(sense)"), assertion("U2", "3", "ADC_IN", "v(adc)")],
    )


def unpowered():
    return case(
        "mains_unpowered_original",
        "unpowered",
        ORIGINAL,
        0.0,
        [
            assertion("U1", "1", "LINE_SENSE", "v(sense)", "unpowered", OFF_STATE_LIMITS),
            assertion("U2", "3", "ADC_IN", "v(adc)", "unpowered", OFF_STATE_LIMITS),
        ],
    )


class Board:
    """The fixture schematic and sample files copied into a temp directory."""

    def __init__(self, root: Path, cases) -> None:
        self.root = root
        for name in ("line_sense.kicad_sch", f"{ORIGINAL}.samples.json", f"{BIASED}.samples.json"):
            shutil.copy(FIXTURES / name, root / name)
        self.schematic = root / "line_sense.kicad_sch"
        self.sidecar = root / "pin-ranges.json"
        self.policy = {
            "schema_version": 1,
            "schematic_sha256": sha256(self.schematic),
            "samples": {
                stem: {
                    "path": f"{stem}.samples.json",
                    "sha256": sha256(root / f"{stem}.samples.json"),
                }
                for stem in (ORIGINAL, BIASED)
            },
            "cases": cases,
        }

    def write(self) -> None:
        self.sidecar.write_text(json.dumps(self.policy))

    def report(self) -> dict:
        self.write()
        return analyze_pin_ranges(self.schematic, self.sidecar)

    def run(self, capsys) -> tuple[int, dict]:
        self.write()
        code = main([str(self.schematic), str(self.sidecar)])
        return code, json.loads(capsys.readouterr().out)

    def add_samples(self, key: str, time_s, traces) -> None:
        path = self.root / f"{key}.json"
        write_samples(path, time_s, traces)
        self.policy["samples"][key] = {"path": path.name, "sha256": sha256(path)}


def row(report: dict, case_id: str, reference: str, pin: str) -> dict:
    matches = [
        entry
        for entry in report["pins"]
        if (entry["case"], entry["reference"], entry["pin"]) == (case_id, reference, pin)
    ]
    assert len(matches) == 1, report["pins"]
    return matches[0]


def synthetic_board(tmp_path, time_s, values, limits=LIMITS):
    board = Board(tmp_path, [])
    board.add_samples("synthetic", time_s, {"v(pin)": values})
    board.policy["cases"] = [
        case(
            "synthetic",
            "powered",
            "synthetic",
            3.3,
            [assertion("U1", "1", "LINE_SENSE", "v(pin)", limits=limits)],
        )
    ]
    return board


def edit_samples(board: Board, edit) -> None:
    """Rewrite the synthetic sample file after ``edit`` changes its parsed content."""
    path = board.root / "synthetic.json"
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))
    board.policy["samples"]["synthetic"]["sha256"] = sha256(path)


def synthetic_row(board: Board) -> dict:
    return row(board.report(), "synthetic", "U1", "1")


@pytest.fixture
def board(tmp_path):
    return Board(
        tmp_path,
        [
            powered("mains_powered_original", ORIGINAL),
            powered("mains_powered_biased", BIASED),
            unpowered(),
        ],
    )


def test_documented_entry_point_reproduces_both_circuits(board, capsys):
    code, report = board.run(capsys)
    assert (code, report["status"]) == (1, "failed")
    assert not report["release_eligible"]
    assert report["schematic_sha256"] == sha256(board.schematic)
    assert report["sidecar_sha256"] == sha256(board.sidecar)
    expected = {
        ("mains_powered_original", "U1", "1"): (-1.5859, 1.5859),
        ("mains_powered_original", "U2", "3"): (-0.5594, 0.5594),
        ("mains_powered_biased", "U1", "1"): (1.2119, 2.0804),
        ("mains_powered_biased", "U2", "3"): (1.6117, 1.6883),
    }
    for identity, (low, high) in expected.items():
        entry = row(report, *identity)
        assert (entry["min_v"], entry["max_v"]) == pytest.approx((low, high), abs=TOLERANCE), (
            identity
        )
        assert (entry["label"], entry["state"]) == ("unclamped_demand", "powered")
    amplifier = row(report, "mains_powered_original", "U1", "1")
    adc = row(report, "mains_powered_original", "U2", "3")
    assert (amplifier["reference"], amplifier["pin"], amplifier["net"], amplifier["trace"]) == (
        "U1",
        "1",
        "LINE_SENSE",
        "v(sense)",
    )
    assert (amplifier["case"], amplifier["outcome"]) == ("mains_powered_original", "fail")
    assert (adc["reference"], adc["pin"], adc["net"], adc["trace"], adc["outcome"]) == (
        "U2",
        "3",
        "ADC_IN",
        "v(adc)",
        "fail",
    )
    assert amplifier["violations"] == [
        {
            "side": "lower",
            "limit_v": 0.0,
            "extreme_v": pytest.approx(-1.5859, abs=TOLERANCE),
            "excursion_v": pytest.approx(1.586, abs=TOLERANCE),
        }
    ]
    assert [violation["side"] for violation in adc["violations"]] == ["lower"]
    assert adc["violations"][0]["excursion_v"] == pytest.approx(0.559, abs=TOLERANCE)
    for reference, pin in (("U1", "1"), ("U2", "3")):
        passed = row(report, "mains_powered_biased", reference, pin)
        assert (passed["outcome"], passed["violations"], passed["reasons"]) == ("pass", [], [])


def test_passing_board_exits_zero(board, capsys):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    code, report = board.run(capsys)
    assert (code, report["status"]) == (0, "passed")
    assert not report["release_eligible"] and report["issues"] == []


def test_module_command_exits_zero_for_a_passing_board(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.write()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.analysis.analog_pin_range",
            str(board.schematic),
            str(board.sidecar),
        ],
        capture_output=True,
        cwd=board.root,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert (report["status"], report["release_eligible"], report["issues"]) == ("passed", False, [])
    assert "only to the named waveform" in report["scope"]


def test_each_row_names_its_waveform_window_rails_state_and_limits(board):
    report = board.report()
    entry = row(report, "mains_powered_original", "U1", "1")
    assert entry["waveform"] == "mains 187 V peak 60 Hz, zero offset"
    assert entry["window"] == {"start_s": 0.05, "end_s": 0.1}
    assert entry["reference_net"] == "GND"
    assert entry["rails"]["VDD"] == {
        "net": "+3V3",
        "voltage_v": 3.3,
        "source": "ideal fixture supply",
    }
    assert entry["limits"] == {"state": "powered", "source": "synthetic fixture limit", **LIMITS}
    assert entry["model"]["input_protection"] == "omitted"
    assert entry["model"]["assumptions"]
    assert entry["samples"]["sha256"] == board.policy["samples"][ORIGINAL]["sha256"]
    assert entry["samples"]["source"].startswith("ngspice-47 .tran 10u 100m of line_sense_original")
    assert entry["sample_count"] > 2
    assert "only to the named waveform" in report["scope"]
    for reference, pin in (("U1", "1"), ("U2", "3")):
        off = row(report, "mains_unpowered_original", reference, pin)
        assert (off["state"], off["limits"]["state"]) == ("unpowered", "unpowered")
        assert off["rails"]["VDD"]["voltage_v"] == 0.0


def test_equality_with_either_limit_passes(tmp_path):
    time_s = [0.0, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1]
    board = synthetic_board(tmp_path, time_s, [5.0, 0.0, 3.3, 1.65, 3.3, 0.0, 2.0])
    entry = synthetic_row(board)
    assert (entry["min_v"], entry["max_v"], entry["sample_count"]) == (0.0, 3.3, 6)
    assert (entry["outcome"], entry["violations"]) == ("pass", [])


def test_trace_crossing_both_limits_reports_each_side(tmp_path):
    time_s = [0.0, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1]
    board = synthetic_board(tmp_path, time_s, [1.0, 1.0, -0.5, 1.0, 3.9, 1.0, 1.0])
    entry = synthetic_row(board)
    assert entry["outcome"] == "fail"
    assert entry["violations"] == [
        {"side": "lower", "limit_v": 0.0, "extreme_v": -0.5, "excursion_v": 0.5},
        {"side": "upper", "limit_v": 3.3, "extreme_v": 3.9, "excursion_v": pytest.approx(0.6)},
    ]


def test_interior_extremes_ignore_out_of_window_samples(tmp_path):
    time_s = [0.0, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1, 0.11]
    board = synthetic_board(tmp_path, time_s, [9.0, 8.0, 1.0, -0.5, 1.2, 1.0, 1.1, 1.0, 7.0])
    entry = synthetic_row(board)
    assert (entry["min_v"], entry["max_v"], entry["sample_count"]) == (-0.5, 1.2, 6)
    assert entry["violations"] == [
        {"side": "lower", "limit_v": 0.0, "extreme_v": -0.5, "excursion_v": 0.5}
    ]


def test_window_selects_the_samples_it_names(tmp_path):
    time_s = [0.0, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1, 0.11]
    board = synthetic_board(tmp_path, time_s, [9.0, 8.0, 1.0, -0.5, 1.2, 1.0, 3.5, 1.0, 7.0])
    board.policy["cases"][0]["window"] = {"start_s": 0.05, "end_s": 0.07}
    entry = synthetic_row(board)
    assert (entry["min_v"], entry["max_v"], entry["sample_count"]) == (-0.5, 1.2, 3)
    assert [violation["side"] for violation in entry["violations"]] == ["lower"]
    board.policy["cases"][0]["window"] = {"start_s": 0.07, "end_s": 0.1}
    entry = synthetic_row(board)
    assert (entry["min_v"], entry["max_v"], entry["sample_count"]) == (1.0, 3.5, 4)
    assert [violation["side"] for violation in entry["violations"]] == ["upper"]


def test_samples_must_cover_the_window(tmp_path):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.06, 0.07, 0.08], [1.0, 1.0, 1.1, 1.0, 1.2])
    entry = synthetic_row(board)
    assert (entry["outcome"], entry["min_v"]) == ("unassessed", None)
    assert "samples span 0..0.08 s and do not cover the window 0.05..0.1 s" in " ".join(
        entry["reasons"]
    )


def test_window_with_a_single_sample_is_unassessed(tmp_path):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.2], [9.0, 1.0, 9.0])
    entry = synthetic_row(board)
    assert entry["outcome"] == "unassessed"
    assert "window holds 1 sample; at least two are required" in " ".join(entry["reasons"])


def test_unpowered_case_is_judged_by_its_own_limits(board):
    entry = row(board.report(), "mains_unpowered_original", "U1", "1")
    assert (entry["outcome"], entry["label"]) == ("fail", "unclamped_demand")
    assert [(violation["side"], violation["limit_v"]) for violation in entry["violations"]] == [
        ("lower", -0.3),
        ("upper", 0.3),
    ]
    assert entry["violations"][1]["extreme_v"] == pytest.approx(1.5859, abs=TOLERANCE)
    assert entry["violations"][1]["excursion_v"] == pytest.approx(1.5859 - 0.3, abs=TOLERANCE)


def test_missing_off_state_limits_leave_the_pins_unassessed(board):
    board.policy["cases"] = [unpowered()]
    board.policy["cases"][0]["pins"][0].pop("limits")
    board.policy["cases"][0]["pins"][1]["limits"]["state"] = "powered"
    report = board.report()
    assert report["status"] == "unassessed"
    limits_removed, limits_wrong_state = report["pins"]
    for entry, expected in ((limits_removed, -1.5859), (limits_wrong_state, -0.5594)):
        assert (entry["outcome"], entry["violations"]) == ("unassessed", [])
        assert entry["min_v"] == pytest.approx(expected, abs=TOLERANCE)
        assert "limits applicable to the unpowered state are absent" in " ".join(entry["reasons"])
    assert "declared state is None" in " ".join(limits_removed["reasons"])
    assert "declared state is 'powered'" in " ".join(limits_wrong_state["reasons"])


def test_missing_rail_evidence_leaves_the_case_unassessed(board):
    board.policy["cases"] = [unpowered()]
    board.policy["cases"][0]["rails"] = {}
    report = board.report()
    assert report["status"] == "unassessed"
    for entry in report["pins"]:
        assert entry["outcome"] == "unassessed" and entry["rails"] is None
        assert "rail evidence absent for the unpowered state" in " ".join(entry["reasons"])


def test_unpowered_limits_cannot_be_borrowed_by_a_powered_case(board):
    board.policy["cases"] = [unpowered()]
    board.policy["cases"][0]["state"] = "powered"
    entry = row(board.report(), "mains_unpowered_original", "U1", "1")
    assert entry["outcome"] == "unassessed"
    assert "limits applicable to the powered state are absent" in " ".join(entry["reasons"])


@pytest.mark.parametrize(
    ("field", "value", "identity", "expected"),
    [
        ("net", "ADC_IN", ("U1", "1"), "U1.1: expected net 'ADC_IN', schematic has 'LINE_SENSE'"),
        ("reference", "U9", ("U9", "1"), "U9: reference is not in the schematic"),
        ("pin", "9", ("U1", "9"), "U1.9: pin is not connected in the schematic"),
    ],
)
def test_unbound_pin_is_unassessed(board, field, value, identity, expected):
    board.policy["cases"] = [powered("mains_powered_original", ORIGINAL)]
    board.policy["cases"][0]["pins"][0][field] = value
    report = board.report()
    entry = row(report, "mains_powered_original", *identity)
    assert (entry["outcome"], entry["min_v"]) == ("unassessed", None)
    assert expected in " ".join(entry["reasons"]), entry["reasons"]
    assert row(report, "mains_powered_original", "U2", "3")["outcome"] == "fail"


def test_schematic_change_moves_the_pin_off_its_declared_net(board):
    board.policy["cases"] = [powered("mains_powered_original", ORIGINAL)]
    text = board.schematic.read_text()
    before = '(label "LINE_SENSE"\n\t\t(at 132.38 97.46 0)'
    assert before in text
    board.schematic.write_text(text.replace(before, '(label "SENSE_X"\n\t\t(at 132.38 97.46 0)'))
    board.policy["schematic_sha256"] = sha256(board.schematic)
    entry = row(board.report(), "mains_powered_original", "U1", "1")
    assert entry["outcome"] == "unassessed"
    assert "U1.1: expected net 'LINE_SENSE', schematic has 'SENSE_X'" in " ".join(entry["reasons"])


@pytest.mark.parametrize("key", ["reference", "pin", "net", "trace"])
def test_missing_pin_field_is_unassessed(tmp_path, key):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.06, 0.1], [1.0, 1.0, 1.5, 1.0])
    del board.policy["cases"][0]["pins"][0][key]
    entry = board.report()["pins"][0]
    assert entry["outcome"] == "unassessed"
    expected = {
        "reference": "pin reference and number are required",
        "pin": "pin reference and number are required",
        "net": "expected net None, schematic has 'LINE_SENSE'",
        "trace": "trace name absent",
    }[key]
    assert expected in " ".join(entry["reasons"]), entry["reasons"]


def test_pin_number_may_be_an_integer(board):
    board.policy["cases"] = [powered("mains_powered_original", ORIGINAL)]
    board.policy["cases"][0]["pins"][0]["pin"] = 1
    entry = row(board.report(), "mains_powered_original", "U1", "1")
    assert (entry["pin"], entry["outcome"]) == ("1", "fail")


def test_pin_assertion_must_be_an_object(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.policy["cases"][0]["pins"].append("U1")
    report = board.report()
    stray = report["pins"][2]
    assert (stray["outcome"], stray["reasons"]) == (
        "unassessed",
        ["mains_powered_biased: pin: object required"],
    )


def test_missing_trace_and_nonfinite_samples_are_unassessed(tmp_path):
    time_s = [0.0, 0.05, 0.06, 0.07, 0.08, 0.1]
    board = synthetic_board(tmp_path, time_s, [1.0, 1.0, 1.1, 1.0, 1.2, 1.0])
    board.policy["cases"][0]["pins"].append(assertion("U2", "3", "ADC_IN", "v(absent)"))
    report = board.report()
    absent = row(report, "synthetic", "U2", "3")
    assert absent["outcome"] == "unassessed"
    assert "trace 'v(absent)' is absent from samples 'synthetic'" in " ".join(absent["reasons"])
    assert row(report, "synthetic", "U1", "1")["outcome"] == "pass"
    board.add_samples("synthetic", time_s, {"v(pin)": [1.0, 1.0, 1.1, float("nan"), 1.2, 1.0]})
    entry = synthetic_row(board)
    assert (entry["outcome"], entry["min_v"]) == ("unassessed", None)
    assert "sample 3 is not a finite number" in " ".join(entry["reasons"])


@pytest.mark.parametrize(
    ("time_s", "expected"),
    [
        pytest.param([0.0, 0.05, 0.05, 0.1], "strictly increasing", id="repeated"),
        pytest.param([0.0, 0.1, 0.05], "strictly increasing", id="decreasing"),
        pytest.param([0.0, "0.05", 0.1], "not a finite number", id="non-numeric"),
        pytest.param([0.0, 0.05], "do not cover the window", id="too-short"),
    ],
)
def test_malformed_time_axis_is_unassessed(tmp_path, time_s, expected):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.1], [1.0, 1.0, 1.0])
    edit_samples(
        board,
        lambda data: (
            data.update(time_s=time_s),
            data["traces"].update({"v(pin)": [1.0] * len(time_s)}),
        ),
    )
    entry = synthetic_row(board)
    assert entry["outcome"] == "unassessed"
    assert expected in " ".join(entry["reasons"]), entry["reasons"]


def test_short_trace_is_unassessed(tmp_path):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.1], [1.0, 1.0, 1.0])
    edit_samples(board, lambda data: data["traces"].update({"v(pin)": [1.0, 1.0]}))
    entry = synthetic_row(board)
    assert (entry["outcome"], entry["min_v"]) == ("unassessed", None)
    assert "trace 'v(pin)' has 2 samples for 3 times" in " ".join(entry["reasons"])


def test_degenerate_sample_content_is_unassessed(tmp_path):
    time_s = [0.0, 0.05, 0.06, 0.1]
    board = synthetic_board(tmp_path, time_s, [1.0, 1.0, 1.5, 1.0])
    edit_samples(
        board,
        lambda data: (
            data.update(time_s=[0.0]),
            data["traces"].update({"v(pin)": [1.0]}),
        ),
    )
    entry = synthetic_row(board)
    assert "time_s needs at least two samples" in " ".join(entry["reasons"])
    edit_samples(
        board,
        lambda data: (
            data.update(time_s=time_s),
            data["traces"].update({"v(pin)": 1.0}),
        ),
    )
    entry = synthetic_row(board)
    assert "trace 'v(pin)': array of numbers required" in " ".join(entry["reasons"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("{not json", "is not valid JSON", id="invalid-json"),
        pytest.param("[]", "object required", id="not-an-object"),
        pytest.param(
            '{"schema_version": 2, "time_s": [0, 0.1], "traces": {"v(pin)": [1, 1]}}',
            "unsupported sample schema",
            id="schema",
        ),
        pytest.param(
            '{"schema_version": 1, "time_s": [0, 0.1], "traces": [1, 1]}',
            "traces must be a nonempty object",
            id="traces-array",
        ),
        pytest.param(
            '{"schema_version": 1, "time_s": [0, 0.1], "traces": {"v(pin)": [1, 1]}, "extra": 1}',
            "unknown field 'extra'",
            id="unknown-field",
        ),
        pytest.param(
            '{"schema_version": 1, "time_s": [0, 0.1], "traces": {"v(pin)": [1, "x"]}}',
            "sample 1 is not a finite number",
            id="non-numeric-sample",
        ),
    ],
)
def test_malformed_sample_file_is_unassessed(tmp_path, text, expected):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.1], [1.0, 1.0, 1.0])
    board.root.joinpath("synthetic.json").write_text(text)
    board.policy["samples"]["synthetic"]["sha256"] = sha256(board.root / "synthetic.json")
    entry = synthetic_row(board)
    assert (entry["outcome"], entry["min_v"]) == ("unassessed", None)
    assert any(expected in reason for reason in entry["reasons"]), entry["reasons"]


def test_unreadable_and_oversized_sample_files_are_unassessed(tmp_path, monkeypatch):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.1], [1.0, 1.0, 1.0])
    board.policy["samples"]["synthetic"]["path"] = "absent.json"
    entry = synthetic_row(board)
    assert entry["outcome"] == "unassessed"
    assert "cannot read absent.json" in " ".join(entry["reasons"])
    monkeypatch.setattr(analog_pin_range, "MAX_SAMPLE_BYTES", 10)
    board.policy["samples"]["synthetic"]["path"] = "synthetic.json"
    entry = synthetic_row(board)
    assert "exceeds the 10 byte limit" in " ".join(entry["reasons"])


def test_sample_entry_problems_are_unassessed(tmp_path):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.06, 0.1], [1.0, 1.0, 1.5, 1.0])
    entry = board.policy["samples"]["synthetic"]
    path = board.root / "synthetic.json"
    entry["sha"] = "x"
    assert "unknown field 'sha'" in " ".join(synthetic_row(board)["reasons"])
    del entry["sha"]
    del entry["path"]
    assert "path absent" in " ".join(synthetic_row(board)["reasons"])
    entry.update(path=str(path), sha256=sha256(path))
    assert synthetic_row(board)["outcome"] == "pass"
    board.policy["samples"]["synthetic"] = []
    assert "samples 'synthetic': object required" in " ".join(synthetic_row(board)["reasons"])


def test_sample_digest_mismatch_is_unassessed(board):
    board.policy["samples"][ORIGINAL]["sha256"] = "0" * 64
    report = board.report()
    assert report["status"] == "unassessed"
    assert len(report["pins"]) == 6
    for entry in report["pins"]:
        if entry["samples"]["name"] == ORIGINAL:
            assert (entry["outcome"], entry["min_v"]) == ("unassessed", None)
            assert "digest mismatch" in " ".join(entry["reasons"])
        else:
            assert entry["outcome"] == "pass" and entry["min_v"] is not None


def test_undeclared_samples_are_unassessed(board):
    board.policy["cases"] = [powered("mains_powered_original", "ghost")]
    entry = row(board.report(), "mains_powered_original", "U1", "1")
    assert entry["outcome"] == "unassessed"
    assert "samples 'ghost' are not declared in the sidecar" in entry["reasons"]


def test_missing_model_assumptions_are_unassessed(board):
    board.policy["cases"] = [powered("mains_powered_original", ORIGINAL)]
    board.policy["cases"][0]["model"] = {"input_protection": "omitted", "assumptions": []}
    entry = row(board.report(), "mains_powered_original", "U1", "1")
    assert (entry["outcome"], entry["label"], entry["model"]) == (
        "unassessed",
        "unclamped_demand",
        None,
    )
    assert "model assumptions absent" in " ".join(entry["reasons"])


def test_undeclared_input_protection_is_unassessed(board):
    board.policy["cases"] = [powered("mains_powered_original", ORIGINAL)]
    del board.policy["cases"][0]["model"]["input_protection"]
    entry = row(board.report(), "mains_powered_original", "U1", "1")
    assert (entry["outcome"], entry["label"]) == ("unassessed", None)
    assert "input_protection" in " ".join(entry["reasons"])


def test_modeled_input_protection_changes_the_label(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.policy["cases"][0]["model"]["input_protection"] = "modeled"
    for reference, pin in (("U1", "1"), ("U2", "3")):
        entry = row(board.report(), "mains_powered_biased", reference, pin)
        assert (entry["outcome"], entry["label"]) == ("pass", "simulated_with_input_protection")


def test_unknown_and_invalid_limits_cannot_pass(tmp_path):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.06, 0.1], [1.0, 1.0, 1.5, 1.0])
    limits = board.policy["cases"][0]["pins"][0]["limits"]
    limits["uppper_v"] = 99.0
    entry = synthetic_row(board)
    assert entry["outcome"] == "unassessed"
    assert "unknown field 'uppper_v'" in " ".join(entry["reasons"])
    del limits["uppper_v"]
    limits["lower_v"], limits["upper_v"] = 3.3, 0.0
    entry = synthetic_row(board)
    assert (entry["outcome"], entry["limits"]) == ("unassessed", None)
    assert "limits lower_v exceeds upper_v" in " ".join(entry["reasons"])
    limits["lower_v"], limits["upper_v"], limits["source"] = 0.0, 3.3, ""
    entry = synthetic_row(board)
    assert entry["outcome"] == "unassessed"
    assert "limits source absent" in " ".join(entry["reasons"])


def test_non_finite_numbers_are_unassessed(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    biased = board.policy["cases"][0]
    biased["window"]["start_s"] = float("nan")
    biased["rails"]["VDD"]["voltage_v"] = float("inf")
    biased["pins"][0]["limits"]["lower_v"] = "0"
    entry = row(board.report(), "mains_powered_biased", "U1", "1")
    assert (entry["outcome"], entry["min_v"], entry["window"]) == ("unassessed", None, None)
    assert " ".join(entry["reasons"]).count("finite number required") == 3


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        pytest.param("id", "case id absent", id="id"),
        pytest.param("waveform", "waveform name absent", id="waveform"),
        pytest.param("state", "supply state must be", id="state"),
        pytest.param("window", "window: object required", id="window"),
        pytest.param("reference_net", "voltage reference net absent", id="reference-net"),
        pytest.param("samples", "samples are not named", id="samples"),
        pytest.param("model", "model: object required", id="model"),
    ],
)
def test_missing_case_field_is_unassessed(board, field, expected):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    del board.policy["cases"][0][field]
    entry = row(board.report(), "<unnamed>" if field == "id" else "mains_powered_biased", "U1", "1")
    assert entry["outcome"] == "unassessed"
    assert expected in " ".join(entry["reasons"]), entry["reasons"]


def test_unknown_case_field_is_unassessed(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.policy["cases"][0]["windw"] = {"start_s": 0.05, "end_s": 0.1}
    entry = row(board.report(), "mains_powered_biased", "U1", "1")
    assert entry["outcome"] == "unassessed"
    assert "unknown case field 'windw'" in " ".join(entry["reasons"])


def test_unknown_sidecar_field_is_unassessed(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.policy["case"] = []
    report = board.report()
    assert report["status"] == "unassessed"
    assert "sidecar: unknown field 'case'" in " ".join(report["issues"])
    assert all(entry["outcome"] == "pass" for entry in report["pins"])


def test_reference_and_rail_nets_must_exist_in_the_schematic(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    biased = board.policy["cases"][0]
    biased["reference_net"] = "EGND"
    biased["rails"]["VDD"]["net"] = "+5V"
    entry = row(board.report(), "mains_powered_biased", "U1", "1")
    assert (entry["outcome"], entry["min_v"]) == ("unassessed", None)
    joined = " ".join(entry["reasons"])
    assert "voltage reference net 'EGND' is not in the schematic" in joined
    assert "rail VDD: net '+5V' is not in the schematic" in joined


def test_rail_entry_problems_are_unassessed(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    rails = board.policy["cases"][0]["rails"]
    rails["VDD"].update(net="", source="", extra=True)
    rails["VSS"] = "GND"
    entry = row(board.report(), "mains_powered_biased", "U1", "1")
    assert (entry["outcome"], entry["rails"]) == ("unassessed", None)
    joined = " ".join(entry["reasons"])
    assert "rail VDD: net absent" in joined
    assert "rail VDD: source absent" in joined
    assert "rail VDD: unknown field 'extra'" in joined
    assert "rail VSS: object required" in joined


def test_invalid_window_is_unassessed(tmp_path):
    board = synthetic_board(tmp_path, [0.0, 0.05, 0.06, 0.1], [1.0, 1.0, 1.5, 1.0])
    board.policy["cases"][0]["window"] = {"start_s": 0.1, "end_s": 0.05}
    entry = synthetic_row(board)
    assert (entry["outcome"], entry["min_v"], entry["window"]) == ("unassessed", None, None)
    assert "window end_s must exceed start_s" in " ".join(entry["reasons"])


def test_stale_schematic_hash_reports_no_rows(board):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.write()
    board.schematic.write_text(board.schematic.read_text() + "\n")
    report = board.report()
    assert (report["status"], report["pins"]) == ("unassessed", [])
    assert "Stale schematic binding" in report["issues"][0]


def test_schematic_mutation_during_analysis_is_reported(board, monkeypatch):
    extract = netlist_module.build_netlist_from_schematic

    def extract_then_touch(path, **kwargs):
        netlist = extract(path, **kwargs)
        Path(path).write_text(Path(path).read_text() + "\n")
        return netlist

    monkeypatch.setattr(netlist_module, "build_netlist_from_schematic", extract_then_touch)
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    report = board.report()
    assert "Schematic changed while calculating evidence" in report["issues"]
    assert report["status"] == "unassessed"


def test_hierarchical_schematic_is_refused(tmp_path):
    root = FIXTURES.parent / "hierarchical" / "root.kicad_sch"
    sidecar = tmp_path / "pin-ranges.json"
    sidecar.write_text(
        json.dumps(
            {"schema_version": 1, "schematic_sha256": sha256(root), "samples": {}, "cases": []}
        )
    )
    report = analyze_pin_ranges(root, sidecar)
    assert (report["status"], report["pins"]) == ("unassessed", [])
    assert "Hierarchical schematic binding is not supported" in report["issues"][0]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param({"schema_version": 2}, "Unsupported sidecar schema", id="schema"),
        pytest.param({"samples": []}, "Sample sets must be an object", id="samples"),
        pytest.param({"cases": [7]}, "Each case must be an object", id="case"),
    ],
)
def test_report_level_sidecar_problems(board, change, expected):
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.policy.update(change)
    report = board.report()
    assert expected in report["issues"]
    assert (report["status"], report["release_eligible"]) == ("unassessed", False)


def test_empty_and_duplicate_declarations_cannot_pass(board):
    board.policy["cases"] = []
    report = board.report()
    assert report["status"] == "unassessed" and "No cases declared" in report["issues"]
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.policy["cases"][0]["pins"] = []
    report = board.report()
    assert report["status"] == "unassessed"
    assert "mains_powered_biased: no pin assertions" in report["issues"]
    board.policy["cases"] = [powered("mains_powered_biased", BIASED)]
    board.policy["cases"][0]["pins"].append(copy.deepcopy(board.policy["cases"][0]["pins"][0]))
    report = board.report()
    assert report["status"] == "unassessed"
    assert "mains_powered_biased: duplicate assertion for U1.1" in report["issues"]
    board.policy["cases"].append(powered("mains_powered_biased", BIASED))
    report = board.report()
    assert "Duplicate case id: mains_powered_biased" in report["issues"]


def test_cli_reports_unreadable_or_invalid_input_as_unassessed(tmp_path, capsys):
    schematic, sidecar = tmp_path / "test.kicad_sch", tmp_path / "pin-ranges.json"
    schematic.write_text("(kicad_sch)")
    sidecar.write_text("[]")
    assert main([str(schematic), str(sidecar)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert (report["status"], report["release_eligible"]) == ("unassessed", False)
    sidecar.write_text("{not json")
    assert main([str(schematic), str(sidecar)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unassessed"
    assert main([str(schematic), str(tmp_path / "missing.json")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unassessed"


@pytest.mark.parametrize("state", ["powered", "unpowered"])
def test_missing_device_supply_leaves_pins_unassessed(board, state):
    board.policy["cases"] = [powered("p", ORIGINAL) if state == "powered" else unpowered()]
    del board.policy["cases"][0]["rails"]["VDD"]
    report = board.report()
    assert report["status"] == "unassessed"
    for entry in report["pins"]:
        assert entry["outcome"] == "unassessed"
        assert "no rail evidence for a supply net" in " ".join(entry["reasons"])


@pytest.mark.parametrize("state", ["powered", "unpowered"])
def test_signal_pin_cannot_supply_rail_evidence(board, capsys, state):
    case = powered("biased", BIASED) if state == "powered" else unpowered()
    case["rails"]["VDD"]["net"] = "BUF_OUT"
    board.policy["cases"] = [case]
    code, report = board.run(capsys)
    assert code == 2
    for entry in report["pins"]:
        assert entry["outcome"] == "unassessed"
        assert "+3V3" in " ".join(entry["reasons"])


@pytest.mark.parametrize("net", ["+3V3", "GND"])
def test_conflicting_voltages_on_one_physical_net(board, capsys, net):
    case = powered("biased", BIASED)
    case["rails"]["second"] = {
        "net": net,
        "voltage_v": 0.0 if net == "+3V3" else 3.3,
        "source": "second fixture declaration",
    }
    board.policy["cases"] = [case]
    code, report = board.run(capsys)
    assert code == 2
    for entry in report["pins"]:
        assert entry["outcome"] == "unassessed"
        assert any(net in r and "0.0" in r and "3.3" in r for r in entry["reasons"])


def test_consistent_aliases_and_unrelated_conflicts_allow_assessment(board):
    case = powered("biased", BIASED)
    rails = case["rails"]
    rails["alias"] = dict(rails["VDD"])
    rails["other1"] = dict(rails["VDD"], net="BUF_OUT")
    rails["other2"] = dict(rails["other1"], voltage_v=0)
    board.policy["cases"] = [case]
    assert [entry["outcome"] for entry in board.report()["pins"]] == ["pass", "pass"]


def test_supply_voltage_must_match_the_declared_state(board):
    live = unpowered()
    live["rails"]["VDD"]["voltage_v"] = 3.3
    dead = powered("dead", ORIGINAL)
    dead["rails"]["VDD"]["voltage_v"] = 0.0
    board.policy["cases"] = [live, dead]
    report = board.report()
    assert {e["outcome"] for e in report["pins"]} == {"unassessed"}


def test_duplicate_sample_keys_are_unassessed(tmp_path):
    board = synthetic_board(tmp_path, [0.0, 0.06, 0.1, 0.2], [0, 1, 1, 0])
    path = tmp_path / "synthetic.json"
    path.write_text(
        '{"schema_version": 1, "time_s": [0, 0.06, 0.1, 0.2], "traces": '
        '{"v(pin)": [-1, -1, -1, -1], "v(pin)": [0.1, 0.1, 0.1, 0.1]}}'
    )
    board.policy["samples"]["synthetic"]["sha256"] = sha256(path)
    entry = synthetic_row(board)
    assert entry["outcome"] == "unassessed"
    assert "duplicate object key 'v(pin)'" in " ".join(entry["reasons"])


def test_duplicate_sidecar_keys_are_unassessed(tmp_path):
    board = synthetic_board(tmp_path, [0.0, 0.06, 0.1, 0.2], [1.5] * 4)
    board.write()
    text = board.sidecar.read_text().replace('"upper_v": 3.3', '"upper_v": 1.0, "upper_v": 3.3')
    board.sidecar.write_text(text)
    report = analyze_pin_ranges(board.schematic, board.sidecar)
    assert report["status"] == "unassessed" and report["pins"] == []
    assert "duplicate object key 'upper_v'" in " ".join(report["issues"])


def test_unrepresentable_numbers_keep_a_json_result(tmp_path, capsys):
    board = synthetic_board(tmp_path, [0.0, 0.06, 0.1, 0.2], [0, 10**400, 1, 0])
    assert synthetic_row(board)["outcome"] == "unassessed"
    board = synthetic_board(
        tmp_path,
        [0.0, 0.06, 0.1, 0.2],
        [0, -1e308, -1e308, 0],
        {"lower_v": 1e308, "upper_v": 1e308},
    )
    code, report = board.run(capsys)
    assert code == 2 and report["pins"][0]["outcome"] == "unassessed"
    assert "not representable" in " ".join(report["pins"][0]["reasons"])
