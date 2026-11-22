"""Reproduce the Board 07 repair against its original and proposed release inputs."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

BASE_REVISION = "47b710fac93bc6c1f93df52a930ec6dc336f7601"
RELEASE_FILES = (
    "build.py",
    "build_source.py",
    "generate.py",
    "validate.py",
    "procurement.json",
    "engineering/clock_spacing.py",
)


def stage_design(design: Path, output: Path, *, original: bool = False) -> None:
    """Copy the two hash-bound fixtures and their release builder into a fresh directory."""
    output.mkdir(parents=True, exist_ok=False)
    repo = design.parents[2]

    def copy(name: str) -> None:
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if original:
            spec = f"{BASE_REVISION}:{design.relative_to(repo) / name}"
            destination.write_bytes(subprocess.check_output(["git", "show", spec], cwd=repo))
        else:
            shutil.copyfile(design / name, destination)

    for name in RELEASE_FILES:
        copy(name)
    for kind in ("authored-source", "reviewed-routing"):
        manifest = f"{kind}/manifest.json"
        copy(manifest)
        for name in json.loads((output / manifest).read_bytes())["sha256"]:
            copy(f"{kind}/{name}")


def stage_passing_pair(design: Path, cleaned: Path, evidence: Path, output: Path) -> None:
    """Stage the prospective pins only after the full design validator passed."""
    report = json.loads((evidence / "validation.json").read_bytes())
    if report["status"] != "pass" or report["errors"]:
        raise ValueError("A fully passing design is required before staging release hashes")
    stage_design(design, output)
    manifests = {}
    for kind, pcb in (
        ("authored-source", cleaned / "source/sdram_demo.kicad_pcb"),
        ("reviewed-routing", cleaned / "candidate.kicad_pcb"),
    ):
        folder = output / kind
        shutil.copyfile(pcb, folder / "sdram_demo.kicad_pcb")
        manifests[kind] = json.loads((folder / "manifest.json").read_bytes())
        manifests[kind]["sha256"]["sdram_demo.kicad_pcb"] = hashlib.sha256(
            pcb.read_bytes()
        ).hexdigest()
    authored, reviewed = manifests["authored-source"], manifests["reviewed-routing"]
    authored["fiducials"] = {f"FID{n}": "F.Cu" if n <= 3 else "B.Cu" for n in range(1, 7)}
    reviewed["authored_source_sha256"] = authored["sha256"]
    reviewed["reviewed_on"] = "2026-11-22"
    for kind, contents in manifests.items():
        (output / kind / "manifest.json").write_text(json.dumps(contents, indent=2) + "\n")
