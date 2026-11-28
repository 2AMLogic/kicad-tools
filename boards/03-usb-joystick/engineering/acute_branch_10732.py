#!/usr/bin/env python3
"""Board 03 measurement for the acute-branch repair (Issue #10732).

Runs the repo's own tools, in the order ``kct readiness`` cleans copper, on a
scratch copy of the routed board and records what each stage measures on the
pinned KiCad toolchain.  Like the readiness producers, the run refuses any
other kicad-cli and writes nothing (#6962).  Nothing here edits the published
``output/`` or the recipe: the manufacturing reissue stays with #8900.

Stages, each measured before the next one runs:

1. ``input`` -- the board as given;
2. ``dedupe`` -- ``PCB.dedupe_copper``, where readiness starts its cleanup;
3. ``redundant-copper`` -- ``kct fix-drc --only redundant-copper`` (#10667);
4. ``acute-branch`` -- ``kct fix-drc --only acute-branch`` (#10732, this repair).

Every stage's ``sharp_trace_corner`` sites and ``kct check`` rule-set census
(with the mask assessment) are recorded.  The first and the last stage are
measured in full: the recipe's strict manufacturing check
(``check_manufacturing.py``), native DRC on the saved copper and on an
independently refilled copy, KiCad's physical pad groups, copper and label
LVS, and the recipe's USB bounds (``routing_plan.usb_geometry``).

Usage (the scratch copy keeps the project sidecars next to the board)::

    KCT_KICAD_DOCKER=1 uv run python \\
        boards/03-usb-joystick/engineering/acute_branch_10732.py \\
        --input boards/03-usb-joystick/output --work /tmp/board03-acute-10732 \\
        --record boards/03-usb-joystick/engineering/acute-branch-10732.json
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

BOARD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOARD_DIR))

MANUFACTURER = "jlcpcb-tier1"
BOARD_NAME = "usb_joystick_routed.kicad_pcb"
SIDECARS = ("usb_joystick_routed.kicad_pro", "usb_joystick_routed.kicad_dru", "net_class_map.json")
#: What ``check_manufacturing.make_checker`` reads beside the board.
RECIPE_SIDECARS = ("fabrication_overrides.json", "manufacturing-requirements.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sharp_sites(board: Path) -> list[dict]:
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.sharp_trace_corner import SharpTraceCornerRule

    sites = SharpTraceCornerRule().check(PCB.load(board), None).violations
    return [
        {
            "net": v.nets[0],
            "layer": v.layer,
            "x": round(v.location[0], 3),
            "y": round(v.location[1], 3),
            "angle_deg": round(v.actual_value, 1),
            "corner_class": v.corner_class,
        }
        for v in sorted(sites, key=lambda v: (v.nets[0], v.layer, v.location))
    ]


def _copy_board(board: Path, target_dir: Path, name: str = BOARD_NAME) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    for extra in (*SIDECARS, *RECIPE_SIDECARS, "usb_joystick.kicad_sch"):
        source = board.parent / extra
        if source.is_file():
            shutil.copy2(source, target_dir / extra)
    for folder in ("footprints", "3dshapes"):
        source = board.parent / folder
        if source.is_dir():
            shutil.copytree(source, target_dir / folder, dirs_exist_ok=True)
    if (board.parent / "fp-lib-table").is_file():
        shutil.copy2(board.parent / "fp-lib-table", target_dir / "fp-lib-table")
    target = target_dir / name
    shutil.copy2(board, target)
    return target


def _native(board: Path, *, refill_first: bool, scratch: Path) -> dict:
    """Native DRC of the bytes on disk; with ``refill_first`` the pours are
    refilled and saved on a copy first, so the fill is KiCad's own."""
    from kicad_tools.cli.runner import run_refill_zones
    from kicad_tools.drc.geometric import run_geometric_drc

    target = board
    if refill_first:
        target = _copy_board(board, scratch / "refilled")
        refill = run_refill_zones(target, require_save=True)
        if not refill.success:
            return {"ran": False, "note": f"refill failed: {refill.stderr}"}
    result = run_geometric_drc(target, refill_zones=False, all_track_errors=True)
    if not result.ran:
        return {"ran": False, "note": result.note}
    return {
        "ran": True,
        "violations": result.error_count,
        "opens": result.unconnected_count,
        "by_type": dict(sorted(result.by_type.items())),
        "all_by_type": dict(sorted(result.all_by_type.items())),
        "sha256": _sha256(target) if refill_first else None,
    }


def _census(board: Path) -> dict:
    """The ``kct check`` rule set on the saved bytes of ``board``."""
    from kicad_tools.cli import fix_drc_cmd
    from kicad_tools.schema.pcb import PCB

    layers = len(PCB.load(board).copper_layers)
    check = fix_drc_cmd._run_python_drc(
        board,
        manufacturer=MANUFACTURER,
        layers=layers,
        project_pcb_path=board,
        mask_copper=True,
        guard_warnings=True,
        copper_gap=True,
    )
    if check is None:
        raise RuntimeError("the kct check rule set did not run")
    counts = Counter(v.type_str for v in check.violations)
    return {
        "errors": sum(1 for v in check.violations if v.is_error),
        "warnings": sum(1 for v in check.violations if v.severity == "warning"),
        "by_rule": dict(sorted(counts.items())),
        "mask_assessment_coverage": [a.coverage for a in check.mask_copper_assessments],
        "mask_findings": sum(
            1 for v in check.violations if v.type_str.startswith("mask_to_copper")
        ),
    }


def _full_measure(board: Path, scratch: Path) -> dict:
    """Everything the acceptance names, on the saved bytes of ``board``."""
    import check_manufacturing
    from routing_plan import usb_geometry

    from kicad_tools.cli import fix_drc_cmd
    from kicad_tools.lvs.recipe import write_lvs_report

    out: dict = {"sha256": _sha256(board), "kct_check": _census(board)}
    out["native_saved"] = _native(board, refill_first=False, scratch=scratch)
    out["native_refilled"] = _native(board, refill_first=True, scratch=scratch)
    groups = fix_drc_cmd._mask_native_pad_groups(board)
    out["pad_groups"] = {"groups": len(groups), "pads": sum(len(g) for g in groups)}
    lvs_dir = scratch / "lvs"
    copper_clean, label_clean = write_lvs_report(
        board.parent / "usb_joystick.kicad_sch",
        board,
        lvs_dir,
        require_clean=False,
        run_copper=True,
        run_label=True,
    )
    lvs = json.loads((lvs_dir / "lvs.json").read_text())
    out["lvs"] = {
        "copper_clean": copper_clean,
        "label_clean": label_clean,
        "copper_bound_pad_count": lvs.get("copper_bound_pad_count"),
        "mismatches": len(lvs.get("mismatches") or []),
        "copper_mismatches": len(lvs.get("copper_mismatches") or []),
    }
    try:
        out["usb_geometry"] = usb_geometry(board)
    except ValueError as error:
        out["usb_geometry"] = {"error": str(error)}
    report = scratch / "check-report.json"
    strict = check_manufacturing.check(board, report)
    meta = strict.get("meta_checks") or {}
    summary = strict.get("summary") or {}
    out["strict_manufacturing_check"] = {
        "summary": {key: summary.get(key) for key in ("errors", "warnings", "infos", "waived")},
        "meta_overall": meta.get("overall"),
        "meta_checks": {
            name: {"status": item.get("status"), "detail": item.get("detail")}
            for name, item in meta.items()
            if isinstance(item, dict)
        },
    }
    return out


def _fix_drc(board: Path, only: str) -> dict:
    """``kct fix-drc --only <only>``, as the readiness cleanup invokes it."""
    from kicad_tools.cli import fix_drc_cmd

    out, err = io.StringIO(), io.StringIO()
    argv = [str(board), "--only", only, "--mfr", MANUFACTURER, "--format", "json"]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fix_drc_cmd.main(argv)
    try:
        doc = json.loads(out.getvalue())
    except ValueError:
        doc = {}
    return {"exit": rc, "doc": doc, "stderr_tail": err.getvalue()[-600:]}


def _block(doc: dict, name: str) -> dict:
    block = doc.get(name) if isinstance(doc, dict) else None
    if not isinstance(block, dict):
        return {}
    return {
        "findings": block.get("violations"),
        "repaired": block.get("repaired"),
        "edits": [
            {"net": e.get("net"), "layer": e.get("layer"), "detail": e.get("detail")}
            for e in block.get("edits") or []
        ],
        "unrepaired": block.get("unrepaired") or [],
    }


def _producer() -> dict:
    """The checkout that ran the measurement."""
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False
        ).stdout.strip()

    return {
        "head": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "script_sha256": _sha256(Path(__file__).resolve()),
    }


def run(input_dir: Path, work: Path) -> dict:
    from kicad_tools.cli.kicad_toolchain import active_evidence_toolchain, evidence_toolchain
    from kicad_tools.schema.pcb import PCB

    if (input_dir / BOARD_NAME).is_file():
        source = input_dir / BOARD_NAME
    elif input_dir.suffix == ".kicad_pcb":
        source = input_dir
    else:
        raise SystemExit(f"no {BOARD_NAME} under {input_dir}")
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    record: dict = {
        "schema": "board03-acute-branch-measurement/1",
        "issue": 10732,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "input": {"path": str(source), "sha256": _sha256(source)},
        "producer": _producer(),
        "stages": {},
    }
    with evidence_toolchain([work.resolve()]):
        chain = active_evidence_toolchain()
        record["toolchain"] = {
            "kicad_version": chain.version if chain else None,
            "image": chain.docker_image if chain else None,
        }
        board = _copy_board(source, work / "stage")
        record["stages"]["input"] = {"sharp_trace_corner": _sharp_sites(board)}
        record["stages"]["input"].update(_full_measure(board, work / "measure-input"))

        pcb = PCB.load(board)
        record["stages"]["dedupe"] = {
            "removed": pcb.dedupe_copper(),
            "sha256_before": _sha256(board),
        }
        pcb.save(board)
        record["stages"]["dedupe"]["sha256"] = _sha256(board)
        record["stages"]["dedupe"]["sharp_trace_corner"] = _sharp_sites(board)

        redundant = _fix_drc(board, "redundant-copper")
        record["stages"]["redundant-copper"] = {
            "exit": redundant["exit"],
            "sha256": _sha256(board),
            **_block(redundant["doc"], "redundant_copper"),
            "sharp_trace_corner": _sharp_sites(board),
            "kct_check": _census(board),
        }
        acute = _fix_drc(board, "acute-branch")
        record["stages"]["acute-branch"] = {
            "exit": acute["exit"],
            "sha256": _sha256(board),
            **_block(acute["doc"], "acute_branch"),
            "sharp_trace_corner": _sharp_sites(board),
        }
        record["stages"]["acute-branch"].update(_full_measure(board, work / "measure-final"))
    record["elapsed_s"] = round(time.monotonic() - started)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, type=Path, help="the routed board's directory")
    parser.add_argument("--work", required=True, type=Path, help="scratch directory")
    parser.add_argument("--record", type=Path, help="write the measurement here (JSON)")
    args = parser.parse_args(argv)
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError

    try:
        record = run(args.input, args.work)
    except KiCadEnvironmentError as error:
        print(f"REFUSED (KiCad environment, Issue #6962): {error}", file=sys.stderr)
        return 2
    text = json.dumps(record, indent=2) + "\n"
    if args.record:
        args.record.write_text(text)
    sites = {name: len(stage["sharp_trace_corner"]) for name, stage in record["stages"].items()}
    print(f"sharp_trace_corner sites per stage: {sites}")
    print(f"elapsed: {record['elapsed_s']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
