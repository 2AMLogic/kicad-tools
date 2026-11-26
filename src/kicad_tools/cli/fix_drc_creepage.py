"""Operating-state creepage checks for fix-drc mask repairs."""

import argparse
import math
from pathlib import Path


def add_mask_creepage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--voltage-map",
        metavar="[NAME=]PATH",
        action="append",
        help="Verify mask repairs under this operating state's JSON voltage map. "
        "Repeat for additional states. The name defaults to the file stem.",
    )
    parser.add_argument(
        "--creepage-standard",
        choices=["iec60664", "iec62368"],
        default="iec60664",
        help="Creepage standard for --voltage-map (default: iec60664)",
    )
    parser.add_argument(
        "--pollution-degree",
        type=int,
        choices=[1, 2, 3],
        default=2,
        help="IEC pollution degree for --voltage-map (default: 2)",
    )
    parser.add_argument(
        "--material-group",
        choices=["I", "II", "IIIa", "IIIb"],
        default="IIIa",
        help="Insulation material group for --voltage-map (default: IIIa)",
    )
    parser.add_argument(
        "--hv-threshold",
        type=float,
        default=30.0,
        metavar="VOLTS",
        help="Minimum net-pair |dV| for the creepage census (default: 30.0)",
    )


class CreepageVerificationError(RuntimeError):
    """Discard the staged run, including edits from earlier passes."""


class MaskCreepageAudit:
    """Compare each operating state independently and retain the rejected measurements."""

    def __init__(self, args: argparse.Namespace) -> None:
        from kicad_tools.creepage.output_audit import CreepageAudit

        if args.only not in (None, "mask-copper"):
            raise ValueError("--voltage-map applies to mask-copper repairs")
        if not math.isfinite(args.hv_threshold) or args.hv_threshold < 0:
            raise ValueError("--hv-threshold must be finite and nonnegative")
        self.audits: dict[str, CreepageAudit] = {}
        self.findings: list[dict] = []
        for spec in args.voltage_map:
            name, sep, path = spec.partition("=")
            if not sep:
                path, name = spec, Path(spec).stem
            if not name or not path:
                raise ValueError(f"Expected --voltage-map [NAME=]PATH, got {spec!r}")
            if name in self.audits:
                raise ValueError(f"duplicate voltage map name: {name!r}")
            self.audits[name] = CreepageAudit(
                voltage_map=path,
                standard=args.creepage_standard,
                pollution_degree=args.pollution_degree,
                material_group=args.material_group,
                hv_threshold=args.hv_threshold,
            )

    def compare(self, before: str, after: str, stage: str) -> set[str]:
        """Record every regressed pair and return its board net names."""
        from kicad_tools.creepage.output_audit import pair_key, worse_pairs

        nets: set[str] = set()
        unavailable = []
        for name, audit in self.audits.items():
            worse = worse_pairs(audit, before, after)
            if not worse:
                continue
            if "unverifiable" in worse:
                unavailable.append(name)
                self.findings.append({"map": name, "stage": stage, "reason": "unverifiable"})
                continue
            old = {pair_key(row): row for row in audit.census(before) or []}
            for row in audit.census(after) or []:
                key = pair_key(row)
                if key not in worse:
                    continue
                previous = old.get(key, {})
                nets.update((row["net_a"], row["net_b"]))
                self.findings.append(
                    {
                        "map": name,
                        "stage": stage,
                        "reason": "creepage_worse",
                        "net_a": row["net_a"],
                        "net_b": row["net_b"],
                        "layer": row["layer"],
                        "creepage_mm_before": previous.get("creepage_mm"),
                        "creepage_mm_after": row["creepage_mm"],
                        "margin_mm_before": previous.get("margin_mm"),
                        "margin_mm_after": row["margin_mm"],
                    }
                )
        if unavailable:
            raise CreepageVerificationError(
                f"Creepage census unavailable for {', '.join(unavailable)}"
            )
        return nets

    def report(self) -> dict:
        return {"maps": list(self.audits), "findings": self.findings}

    def print_findings(self) -> None:
        for row in self.findings:
            prefix = f"Creepage [{row['map']}, {row['stage']}]"
            if row["reason"] == "unverifiable":
                print(f"{prefix} census unavailable")
            else:
                print(
                    f"{prefix} {row['net_a']} / {row['net_b']} on {row['layer']}: "
                    f"{row['creepage_mm_before']} -> {row['creepage_mm_after']} mm, "
                    f"margin {row['margin_mm_before']} -> {row['margin_mm_after']} mm"
                )
