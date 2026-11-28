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

The published routed PCB is the public stand-in for the coordinated reissue
input, which is not on ``main``; the rendered note says so.

Usage (the scratch copy keeps the project sidecars next to the board)::

    KCT_KICAD_DOCKER=1 uv run python \\
        boards/03-usb-joystick/engineering/acute_branch_10732.py \\
        --input boards/03-usb-joystick/output --work /tmp/board03-acute-10732 \\
        --record boards/03-usb-joystick/engineering/acute-branch-10732.json \\
        --note boards/03-usb-joystick/engineering/acute-branch-10732.md
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


def render_note(record: dict) -> str:
    """The record as the committed markdown note; every value is read from ``record``."""
    stages = record["stages"]
    first, middle, last = (
        stages["input"],
        stages["redundant-copper"],
        stages["acute-branch"],
    )
    tool = record["toolchain"]

    def native(stage: dict, key: str) -> str:
        result = stage[key]
        if not result.get("ran"):
            return f"not run: {result.get('note')}"
        return f"{result['violations']} violations / {result['opens']} opens"

    def lvs(stage: dict) -> str:
        result = stage["lvs"]
        return (
            f"label {'clean' if result['label_clean'] else 'DIRTY'}, "
            f"copper {'clean' if result['copper_clean'] else 'DIRTY'}, "
            f"{result['copper_bound_pad_count']} bound pads"
        )

    def rule(stage: dict, name: str) -> int:
        return stage["kct_check"]["by_rule"].get(name, 0)

    def strict(stage: dict, key: str) -> int:
        return stage["strict_manufacturing_check"]["summary"][key]

    def erc(stage: dict) -> str:
        leg = stage["strict_manufacturing_check"]["meta_checks"].get("erc", {})
        detail = str(leg.get("detail") or "").split(" -- ")[0]
        return f"`{leg.get('status', '?')}` ({detail})" if detail else f"`{leg.get('status', '?')}`"

    names = sorted(
        set(first["kct_check"]["by_rule"])
        | set(middle["kct_check"]["by_rule"])
        | set(last["kct_check"]["by_rule"])
    )
    moved = [
        f"`{name}` {rule(middle, name)} -> {rule(last, name)}"
        for name in names
        if rule(middle, name) != rule(last, name)
    ]
    reasons = {item["net"]: item["reason"] for item in last["unrepaired"]}
    producer = record["producer"]
    lines = [
        "# Board 03 acute-branch measurement (Issue #10732)",
        "",
        f"Produced by `engineering/acute_branch_10732.py` (SHA-256 "
        f"`{producer['script_sha256']}`) on {record['measured_at']} in {record['elapsed_s']} s, "
        f"on KiCad {tool['kicad_version']} (`{tool['image']}`), from the published routed PCB "
        f"`output/usb_joystick_routed.kicad_pcb` (SHA-256 `{record['input']['sha256']}`) at "
        f"`{producer['head']}`"
        + (" with uncommitted changes" if producer["dirty"] else "")
        + ". The machine-readable record is `acute-branch-10732.json` next to this note.",
        "",
        "This is the public stand-in, not the coordinated reissue input: the tool-generated "
        "rebuild #8900 measures from is not on `main`. The run is the readiness copper cleanup's "
        "order on a scratch copy -- `dedupe`, `kct fix-drc --only redundant-copper` (#10667), "
        "then `kct fix-drc --only acute-branch` (this issue) -- with each stage measured on its "
        "saved bytes.",
        "",
        "## Stages",
        "",
        "| Stage | `sharp_trace_corner` sites | PCB SHA-256 | `kct check` errors / warnings |",
        "| --- | ---: | --- | --- |",
    ]
    for name, stage in stages.items():
        census = stage.get("kct_check")
        count = f"{census['errors']} / {census['warnings']}" if census else "not measured"
        lines.append(
            f"| `{name}` | {len(stage['sharp_trace_corner'])} | `{stage['sha256'][:16]}` | {count} |"
        )
    lines += [
        "",
        "## Acute-branch pass",
        "",
        f"`kct fix-drc --only acute-branch --mfr {MANUFACTURER}` planned {last['findings']} tap "
        f"findings and kept {last['repaired']} edits, each re-checked on the saved, refilled board "
        f"with `kct check`'s rule set and native DRC (exit {last['exit']}: the residuals below).",
        "",
        "| Residual site | Reason |",
        "| --- | --- |",
    ]
    for site in last["sharp_trace_corner"]:
        lines.append(
            f"| `{site['net']}` {site['layer']} ({site['x']:.3f}, {site['y']:.3f}), "
            f"{site['angle_deg']:.0f} deg | `{reasons[site['net']]}` |"
        )
    lines += [
        "",
        "Each residual is a tap whose every right-angle entry turns its own feeding tracks, or "
        "the carrier it lands on, into a chain the `redundant_copper` rule then reports as a "
        "staircase; the repair refuses that trade rather than swapping one warning for another.",
        "",
        "## Saved and refilled copper",
        "",
        "| Measurement | Input | After the cleanup |",
        "| --- | --- | --- |",
        f"| Native DRC, saved copper (pinned KiCad {tool['kicad_version']}) | "
        f"{native(first, 'native_saved')} | {native(last, 'native_saved')} |",
        "| Native DRC, independently refilled copy | "
        f"{native(first, 'native_refilled')} | {native(last, 'native_refilled')} |",
        f"| KiCad physical pad groups | {first['pad_groups']['groups']} groups / "
        f"{first['pad_groups']['pads']} pads | {last['pad_groups']['groups']} groups / "
        f"{last['pad_groups']['pads']} pads |",
        f"| Copper and label LVS | {lvs(first)} | {lvs(last)} |",
        f"| USB D+ / D- copper length (mm) | "
        f"{first['usb_geometry']['lengths_mm']['USB_D+']:.6f} / "
        f"{first['usb_geometry']['lengths_mm']['USB_D-']:.6f} | "
        f"{last['usb_geometry']['lengths_mm']['USB_D+']:.6f} / "
        f"{last['usb_geometry']['lengths_mm']['USB_D-']:.6f} |",
        f"| USB common coupled run (mm) | {first['usb_geometry']['common_coupled_mm']:.6f} | "
        f"{last['usb_geometry']['common_coupled_mm']:.6f} |",
        f"| USB D+ / D- skew (mm) | {first['usb_geometry']['branch_skew_mm']:.6f} | "
        f"{last['usb_geometry']['branch_skew_mm']:.6f} |",
        f"| `kct check` ({MANUFACTURER}) errors / warnings | "
        f"{first['kct_check']['errors']} / {first['kct_check']['warnings']} | "
        f"{last['kct_check']['errors']} / {last['kct_check']['warnings']} |",
        f"| Mask assessment | "
        f"{', '.join(first['kct_check']['mask_assessment_coverage'])}, "
        f"{first['kct_check']['mask_findings']} `mask_to_copper*` findings | "
        f"{', '.join(last['kct_check']['mask_assessment_coverage'])}, "
        f"{last['kct_check']['mask_findings']} `mask_to_copper*` findings |",
        f"| Strict manufacturing check (`check_manufacturing.py`) | "
        f"{strict(first, 'errors')} errors / {strict(first, 'warnings')} warnings | "
        f"{strict(last, 'errors')} errors / {strict(last, 'warnings')} warnings |",
        "",
        f"`routing_plan.usb_geometry` accepted both boards (at most "
        f"{_usb_skew_limit():g} mm of D+ / D- skew). The strict check's ERC leg reads "
        f"{erc(first)} on the input and {erc(last)} on the result; the record keeps its detail "
        "(a warning about the host's KiCad library tables, not a schematic defect). Its manifest "
        "leg is `NOT RUN` because the scratch copy carries no manufacturing bundle: the package "
        "and release stay with #8900.",
        "",
        "## Rule-set census",
        "",
        "| Rule | Input | After redundant-copper | After acute-branch |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines += [
        f"| `{name}` | {rule(first, name)} | {rule(middle, name)} | {rule(last, name)} |"
        for name in names
    ]
    lines += [
        "",
        "The acute-branch pass moved " + ", ".join(moved) + "; every other count is the "
        "redundant-copper stage's.",
        "",
    ]
    return "\n".join(lines)


def _usb_skew_limit() -> float:
    from routing_plan import USB_FS_MAX_SKEW_MM

    return float(USB_FS_MAX_SKEW_MM)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, type=Path, help="the routed board's directory")
    parser.add_argument("--work", required=True, type=Path, help="scratch directory")
    parser.add_argument("--record", type=Path, help="write the measurement here (JSON)")
    parser.add_argument("--note", type=Path, help="write the markdown note rendered from it here")
    args = parser.parse_args(argv)
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError

    try:
        record = run(args.input, args.work)
    except KiCadEnvironmentError as error:
        print(f"REFUSED (KiCad environment, Issue #6962): {error}", file=sys.stderr)
        return 2
    if args.record:
        args.record.write_text(json.dumps(record, indent=2) + "\n")
    if args.note:
        args.note.write_text(render_note(record))
    sites = {name: len(stage["sharp_trace_corner"]) for name, stage in record["stages"].items()}
    print(f"sharp_trace_corner sites per stage: {sites}")
    print(f"elapsed: {record['elapsed_s']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
