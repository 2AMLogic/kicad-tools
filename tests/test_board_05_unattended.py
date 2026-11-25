"""Exit-code logic of ``scripts/ci/board_05_unattended.py`` (issue #7834).

The gate is pure (a graded row in, failure reasons out), so it is tested on
fixture rows; the route itself is exercised by the scheduled workflow
``.github/workflows/nightly-board-05-unattended.yml``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import runpy
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci" / "board_05_unattended.py"
WORKFLOW = REPO / ".github" / "workflows" / "nightly-board-05-unattended.yml"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("board_05_unattended", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["board_05_unattended"] = module
    spec.loader.exec_module(module)
    return module


def clean_row(**overrides) -> dict:
    row = {
        "seed": 42,
        "exit_code": 0,
        "artifact": "final",
        "cache_hit": False,
        "oracle_unverified": False,
        "nets_complete": 37,
        "nets_total": 37,
        "kicad_drc_errors": 0,
        "kicad_drc_unconnected": 0,
        "kicad_erc_violations": 0,
        "kct_check_errors": 0,
        "kicad_drc_warnings": 0,
        "kct_check_warnings": 0,
        "lvs_copper_clean": True,
        "lvs_label_clean": True,
    }
    row.update(overrides)
    return row


def test_clean_row_passes(mod):
    assert mod.gate_failures(clean_row()) == []
    assert mod.exit_code([clean_row(), clean_row(seed=7)]) == 0


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"nets_complete": 36}, "nets 36/37"),
        ({"kicad_drc_unconnected": 1}, "unconnected"),
        ({"kicad_drc_errors": 2}, "kicad-cli DRC errors: 2"),
        ({"kct_check_errors": 1}, "kct check"),
        ({"kicad_erc_violations": 3}, "ERC"),
        ({"kicad_drc_warnings": 5}, "kicad-cli DRC warnings: 5"),
        ({"kct_check_warnings": 137}, "warnings: 137"),
        ({"lvs_copper_clean": False}, "copper LVS dirty"),
        ({"lvs_label_clean": False}, "label LVS dirty"),
        ({"exit_code": 5}, "exited 5"),
        ({"artifact": "deadline"}, "deadline"),
        ({"cache_hit": True}, "cached"),
        (
            {"oracle_unverified": True, "oracle_unverified_reason": "oracle_failed"},
            "oracle did not answer (oracle_failed)",
        ),
        ({"error": "no routed output (exit_code=124)"}, "no routed output"),
        (
            {"kct_check_mask_incomplete": True},
            "mask_to_copper coverage incomplete",
        ),
    ],
)
def test_any_single_failure_fails_the_run(mod, overrides, needle):
    failures = mod.gate_failures(clean_row(**overrides))
    assert any(needle in f for f in failures), failures
    # One bad seed fails the whole run, however many seeds are clean.
    assert mod.exit_code([clean_row(), clean_row(**overrides), clean_row()]) == 1


@pytest.mark.parametrize(
    "missing",
    [
        "nets_complete",
        "kicad_drc_errors",
        "kicad_drc_unconnected",
        "kicad_erc_violations",
        "kct_check_errors",
        "kicad_drc_warnings",
        "kct_check_warnings",
        "lvs_copper_clean",
        "lvs_label_clean",
    ],
)
def test_a_gate_that_did_not_run_is_a_failure(mod, missing):
    row = clean_row()
    del row[missing]
    assert mod.gate_failures(row), f"absent {missing} must not read as clean"
    assert mod.exit_code([row]) == 1


def test_zero_nets_is_not_complete(mod):
    assert mod.gate_failures(clean_row(nets_complete=0, nets_total=0))


def test_no_rows_fails(mod):
    assert mod.exit_code([]) == 1


def test_grade_only_row_without_route_fields_can_pass(mod):
    row = clean_row()
    for key in ("exit_code", "artifact", "cache_hit", "oracle_unverified"):
        del row[key]
    assert mod.gate_failures(row) == []


def test_grade_only_passes_the_supplied_board_directly_to_the_checker(mod, tmp_path, monkeypatch):
    board = tmp_path / "routed.kicad_pcb"
    original = b"(kicad_pcb)"
    board.write_bytes(original)
    checked = []

    def grade(path, sch):
        checked.append((path, path.read_bytes()))
        return clean_row()

    monkeypatch.setattr(
        "kicad_tools.cli.kicad_toolchain.evidence_toolchain", lambda _: nullcontext()
    )
    monkeypatch.setattr(mod, "grade", grade)
    monkeypatch.setattr(
        mod, "prepare_for_grading", lambda _: pytest.fail("preparing in grade-only mode")
    )
    assert mod.main(["--work-dir", str(tmp_path / "work"), "--grade-only", str(board)]) == 0
    assert checked == [(board, original)]
    assert board.read_bytes() == original


def test_no_allowlist_machinery(mod):
    text = SCRIPT.read_text()
    # No identity allowance, no accepted-warnings table, no tolerated-net list.
    for token in ("identity_allowance", "KNOWN_WARNINGS", "allowed_nets", "tolerated"):
        assert token not in text


def test_route_flags_are_the_6508_protocol(mod):
    flags = mod.route_flags(7)
    for flag in (
        "--no-auto-layers",
        "--deterministic-budget",
        "--no-placement-delta-feedback",
        "--deterministic-rescue",
        "--no-fallback-bidirectional",
    ):
        assert flag in flags
    assert flags[flags.index("--per-net-iterations") + 1] == "1000000"
    assert flags[-2:] == ["--seed", "7"]
    # The replay path is never touched.
    assert "routing.json" not in " ".join(flags)


def test_never_reads_routing_json(mod):
    code = SCRIPT.read_text()
    assert "apply_routing" not in code
    assert "from routing import" not in code


def test_post_route_prep_runs_on_the_finished_board(mod, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "kicad_tools.cli.pcb_add_fiducials.run_add_fiducials",
        lambda routed, sides: (calls.append(("fiducials", routed, sides)), 0)[1],
    )
    monkeypatch.setattr(
        "kicad_tools.cli.pcb_add_board_info.run_add_board_info",
        lambda routed, auto: (calls.append(("board_info", routed, auto)), 0)[1],
    )
    monkeypatch.setattr(
        "kicad_tools.cli.fix_drc_cmd.main",
        lambda argv: (calls.append(("mask_copper_repair", argv)), 2)[1],
    )
    monkeypatch.setattr(
        "kicad_tools.cli.place_silk_refs_cmd.main",
        lambda argv: (calls.append(("silk_refs_over_holes", argv)), 0)[1],
    )
    board = tmp_path / "routed.kicad_pcb"
    board.write_text("(kicad_pcb)")
    row = mod.prepare_for_grading(board)
    assert row["prep"] == ["fiducials", "board_info", "mask_copper_repair", "silk_refs_over_holes"]
    assert [c[0] for c in calls] == row["prep"]
    assert calls[0][2] == "populated"  # build.py's sides, verbatim
    # The repair runs before the silkscreen pass (it can move a via) and may
    # never trade a mask error for a warning the no-warning gate would count.
    assert calls[2][1][0] == str(board)
    assert "--no-new-warnings" in calls[2][1]
    assert "--transactional" in calls[2][1]
    assert calls[2][1][calls[2][1].index("--only") + 1] == "mask-copper"
    assert calls[3][1] == [str(board), "--mfr", "jlcpcb", "--layers", "4", "--over-holes"]
    # run_seed wires the prep in before grading, and its failure is fatal.
    code = SCRIPT.read_text()
    assert "prepare_for_grading(artifact)" in code


def test_post_route_prep_clears_only_references_over_the_seed_42_vias(mod, monkeypatch, tmp_path):
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap

    path = tmp_path / "bldc_controller_routed.kicad_pcb"
    runpy.run_path(str(mod.REDESIGN / "hardware.py"))["build_pcb"]().save(path)
    board = PCB.load(path)
    ox, oy = board.board_origin
    for x, y in [(131.360, 102.475), (172.245, 95.455), (174.130, 95.715)]:
        board.add_via(x - ox, y - oy, size=0.6, drill=0.3)
    board.save(path)

    def positions(pcb):
        return {
            fp.reference: text.position
            for fp in pcb.footprints
            for text in fp.texts
            if text.text_type == "reference"
        }

    before = positions(board)
    findings = check_silk_hole_overlap(board).violations
    assert sorted(v.items[0] for v in findings) == [
        "C3 (reference)",
        "R11 (reference)",
        "R11 (reference)",
    ]
    monkeypatch.setattr("kicad_tools.cli.fix_drc_cmd.main", lambda argv: 1)
    mod.prepare_for_grading(path)
    prepared = PCB.load(path)
    assert not check_silk_hole_overlap(prepared).violations
    after = positions(prepared)
    assert {ref for ref in before if before[ref] != after[ref]} == {"C3", "R11"}
    assert [(v.position, v.drill) for v in prepared.vias] == [
        (v.position, v.drill) for v in board.vias
    ]


@pytest.mark.parametrize("code", [0, 1, 2])
def test_mask_repair_accepts_repaired_unrepaired_and_partial_exits(
    mod, monkeypatch, tmp_path, code
):
    monkeypatch.setattr("kicad_tools.cli.fix_drc_cmd.main", lambda argv: code)
    mod.repair_mask_copper(tmp_path / "routed.kicad_pcb")


def test_mask_repair_rollback_is_fatal(mod, monkeypatch, tmp_path):
    monkeypatch.setattr("kicad_tools.cli.fix_drc_cmd.main", lambda argv: 3)
    with pytest.raises(RuntimeError, match="mask-copper failed"):
        mod.repair_mask_copper(tmp_path / "routed.kicad_pcb")


@pytest.mark.parametrize("incomplete", [False, True])
def test_mask_detection_failure_reaches_nightly_prep(mod, monkeypatch, tmp_path, incomplete):
    from kicad_tools.drc import DRCReport, DRCViolation, Severity, ViolationType

    board = tmp_path / "routed.kicad_pcb"
    board.write_text('(kicad_pcb (version 20240108) (generator "kct") (net 0 ""))')
    original = board.read_bytes()
    assessment = None
    if incomplete:
        finding = DRCViolation(
            type=ViolationType.MASK_TO_COPPER_INCOMPLETE,
            type_str="mask_to_copper_incomplete",
            severity=Severity.INFO,
            message="Mask coverage unavailable",
        )
        assessment = DRCReport(
            source_file=board.name, created_at=None, pcb_name=board.name, violations=[finding]
        )
    monkeypatch.setattr("kicad_tools.cli.fix_drc_cmd._run_python_drc", lambda *a, **kw: assessment)
    with pytest.raises(RuntimeError, match="mask-copper failed"):
        mod.repair_mask_copper(board)
    assert board.read_bytes() == original


def test_nightly_gate_rejects_failed_mask_detection_and_accepts_completed_check(
    mod, tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from kicad_tools.drc import DRCReport

    original = b'(kicad_pcb (version 20240108) (generator "test") (net 0 ""))'
    failures = []

    def stage(generated, run_dir):
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / "input.kicad_pcb"

    def route(inp, routed, flags, timeout, log):
        routed.write_bytes(original)
        log.write_text("")
        return {"exit_code": 0}

    harness = Mock()
    harness.run_route.side_effect = route
    harness.parse_route_log.return_value = {}
    harness.pick_artifact.side_effect = lambda routed, exit_code: (routed, "final")
    monkeypatch.setattr(mod, "stage_seed", stage)
    monkeypatch.setattr(mod, "_load_harness", lambda: harness)
    monkeypatch.setattr(mod, "generate_unrouted", lambda path: None)
    monkeypatch.setattr(mod, "grade", lambda *a: clean_row())
    monkeypatch.setattr(
        "kicad_tools.cli.kicad_toolchain.evidence_toolchain", lambda _: nullcontext()
    )
    monkeypatch.setattr("kicad_tools.cli.pcb_add_fiducials.run_add_fiducials", lambda *a, **kw: 0)
    monkeypatch.setattr("kicad_tools.cli.pcb_add_board_info.run_add_board_info", lambda *a, **kw: 0)
    monkeypatch.setattr("kicad_tools.cli.place_silk_refs_cmd.main", lambda argv: 0)
    report = DRCReport(source_file="", created_at=None, pcb_name="", violations=[])
    for assessment, expected_code in [(None, 1), (report, 0)]:
        monkeypatch.setattr(
            "kicad_tools.cli.fix_drc_cmd._run_python_drc", lambda *a, **kw: assessment
        )
        receipt = tmp_path / "rows.json"
        assert (
            mod.main(["--seeds", "42", "--work-dir", str(tmp_path), "--json", str(receipt)])
            == expected_code
        )
        row = json.loads(receipt.read_text())["rows"][0]
        failures.append(row.get("error"))
        assert Path(row["routed"]).read_bytes() == original
    assert failures == [
        "post-route prep failed: RuntimeError: fix-drc --only mask-copper failed (exit 4)",
        None,
    ]


def test_post_route_prep_failure_is_fatal(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "kicad_tools.cli.pcb_add_fiducials.run_add_fiducials",
        lambda routed, sides: 1,
    )
    board = tmp_path / "routed.kicad_pcb"
    board.write_text("(kicad_pcb)")
    with pytest.raises(RuntimeError, match="add-fiducials failed"):
        mod.prepare_for_grading(board)


def test_run_kct_check_flags_incomplete_mask_coverage(mod, monkeypatch, tmp_path):
    """A degraded mask pass (zero findings + the #8914 info marker) fails (#9424).

    On an unqualified host kicad-cli the mask pass cannot attribute native
    mask regions and emits no violations except one info
    ``mask_to_copper_incomplete`` marker; the error/warning counters read 0/0
    and the row must still carry the degradation for ``gate_failures``.
    """

    def fake_run(cmd, **kwargs):  # noqa: ANN001 -- matches subprocess.run shape
        report = {
            "summary": {"errors": 0, "warnings": 0},
            "violations": [
                {
                    "rule_id": "mask_to_copper_incomplete",
                    "severity": "info",
                    "message": "Mask-to-copper coverage incomplete: ...",
                }
            ],
        }
        (tmp_path / "kct-check.json").write_text(json.dumps(report))
        return None

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    row = mod.run_kct_check(tmp_path / "board.kicad_pcb", tmp_path)
    assert row["kct_check_errors"] == 0
    assert row["kct_check_warnings"] == 0
    assert row["kct_check_mask_incomplete"] is True
    assert mod.gate_failures(clean_row(**{"kct_check_mask_incomplete": True}))

    # A report without the marker (the pass completed, or the rule is
    # skipped) carries no flag.
    (tmp_path / "kct-check.json").unlink()
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda cmd, **kwargs: (
            (tmp_path / "kct-check.json").write_text(
                json.dumps({"summary": {"errors": 0, "warnings": 0}, "violations": []})
            )
            or None
        ),
    )
    row = mod.run_kct_check(tmp_path / "board.kicad_pcb", tmp_path)
    assert "kct_check_mask_incomplete" not in row
    assert mod.gate_failures(clean_row()) == []


def test_workflow_runs_the_script_at_three_seeds_and_is_not_per_pr():
    text = WORKFLOW.read_text()
    assert "scripts/ci/board_05_unattended.py" in text
    assert "pull_request" not in text
    assert "schedule:" in text and "workflow_dispatch" in text
    assert "kct build-native" in text
    assert "continue-on-error" not in text
    seeds = [s for s in ("42", "7", "1337") if f"- {s}" in text or f"{s}," in text]
    assert len(seeds) >= 3, seeds
    # The image digest is read from ci.yml, never re-pinned here.
    assert not re.search(r"kicad/kicad@sha256:[0-9a-f]{64}", text)


def test_non_pinned_kicad_cli_refuses_with_exit_2(mod, monkeypatch, tmp_path):
    """A kicad-cli other than the pinned release is a refusal (2), never a pass."""
    import contextlib

    from kicad_tools.cli import kicad_toolchain

    @contextlib.contextmanager
    def refuse(_mounts):
        raise kicad_toolchain.KiCadEnvironmentError("kicad-cli 10.0.7 is not the pinned release")
        yield  # pragma: no cover

    monkeypatch.setattr(kicad_toolchain, "evidence_toolchain", refuse)
    graded = []
    monkeypatch.setattr(mod, "grade", lambda *a, **k: graded.append(a) or {})
    pcb = tmp_path / "routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert mod.main(["--work-dir", str(tmp_path / "w"), "--grade-only", str(pcb)]) == 2
    assert graded == []
