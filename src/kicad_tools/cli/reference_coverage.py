"""CLI for inspecting routed centerline coverage by adjacent saved zone fills."""

import json
import sys
from pathlib import Path


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "reference-coverage", help="Inspect routed nets over adjacent zone fills"
    )
    parser.add_argument("pcb", type=Path, help="KiCad PCB with saved zone fills")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--net", "-n", action="append", help="Net to inspect (repeatable)")
    selection.add_argument(
        "--pair",
        nargs=2,
        metavar=("FIRST", "SECOND"),
        help="Compare two nets (deltas are SECOND minus FIRST)",
    )
    parser.add_argument("--format", "-f", choices=("text", "json"), default="text")


def run(args) -> int:
    from kicad_tools.analysis.reference_coverage import reference_coverage
    from kicad_tools.exceptions import KiCadToolsError
    from kicad_tools.schema.pcb import PCB

    try:
        report = reference_coverage(
            PCB.load(args.pcb), args.pair or args.net, compare=args.pair is not None
        )
    except (KiCadToolsError, OSError, ValueError) as exc:
        print(f"Error inspecting {args.pcb}: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return 0

    for name, net in report["nets"].items():
        print(f"{name}  {net['length_mm']:g} mm, {net['via_count']} vias")
        for layer, row in net["layers"].items():
            widths = ", ".join(f"{w:g}" for w in row["widths_mm"]) or "—"
            print(
                f"  {layer}  {row['length_mm']:g} mm, widths (mm) {widths}, {row['via_count']} vias"
            )
            for side, ref in row["references"].items():
                if ref["coverage"] == "unknown":
                    print(f"    {side}  unknown (missing saved fill)")
                    continue
                copper = ", ".join(
                    f"{n or '(unassigned)'} {v:g} mm" for n, v in ref["by_net_mm"].items()
                )
                print(
                    f"    {side}  {ref['covered_mm']:g} mm covered, {ref['uncovered_mm']:g} mm over voids  {copper}"
                )
            for path in row["paths"]:
                print(f"    Path from {tuple(path['start_xy_mm'])} mm")
                for span in path["spans"]:
                    sides = []
                    for side, nets in span["reference_nets"].items():
                        state = (
                            "unknown"
                            if nets is None
                            else ("/".join(n or "(unassigned)" for n in nets) or "void")
                        )
                        sides.append(f"{side} {state}")
                    est = span["estimate"]
                    model = (
                        f"{est['model']} estimate {est['z0_ohm']:g} Ω, {est['delay_ps_per_mm']:g} ps/mm"
                        if est
                        else "no estimate"
                    )
                    print(
                        f"      {span['start_mm']:g}–{span['end_mm']:g} mm  {', '.join(sides)}  {model}"
                    )
    if pair := report.get("pair"):
        delta = pair["delta"]
        print(
            f"{pair['second']} minus {pair['first']}  {delta['length_mm']:+g} mm, {delta['via_count']:+d} vias"
        )
    print("Unmodeled effects include " + ", ".join(report["unmodeled"]))
    return 0
