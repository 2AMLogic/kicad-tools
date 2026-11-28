"""Complete the reviewed assembly bundle, including manual parts and firmware."""

import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path

from hardware import ROOT, parts

from kicad_tools.cli.readiness_cmd import write_manufacturing_archive
from kicad_tools.cli.runner import parity_skipped_in_output
from kicad_tools.drc.report_binding import sha256_file
from kicad_tools.export.project_identity import archived_member, project_identity
from kicad_tools.schema.pcb import PCB
from kicad_tools.sidecars import net_class_map_sidecar_names


def verify_placements(routed, placement_refs):
    """Check the PCB and the CPL against the revision-B assembly list."""
    expected = Counter(part.ref for part in parts())
    required = {"J1", "J2", "J3", "J4", "J5", "RV1", "U1", "U2", "U3"}
    if len(expected) != 42 or expected.total() != 42 or not required <= expected.keys():
        raise ValueError(
            "Board 05 requires 42 distinct assembly parts including J1–J5, RV1 and U1–U3"
        )

    pcb_refs = [
        fp.reference
        for fp in PCB.load(routed).footprints
        if not fp.exclude_from_pos_files and not fp.dnp
    ]
    for name, refs in (("routed PCB", pcb_refs), ("CPL", placement_refs)):
        actual = Counter(refs)
        if actual != expected:
            missing = sorted((expected - actual).elements())
            extra = sorted((actual - expected).elements())
            raise ValueError(
                f"Board 05 {name} placement mismatch (missing {missing}, extra {extra})"
            )


def complete_editable_project(output, routed, net_class_map):
    """Verify the delivered sources and include the checked motor map beside the PCB."""
    schematic = output / "bldc_controller.kicad_sch"
    identity = project_identity(routed, routed.with_suffix(".kicad_pro"), schematic)
    members = dict(identity.renamed)
    for source in (
        schematic,
        output / "board05_revB.kicad_sym",
        output / "fp-lib-table",
        output / "sym-lib-table",
        *(output / "board05_revB.pretty").glob("*.kicad_mod"),
    ):
        members[source] = source.relative_to(output).as_posix()
    with zipfile.ZipFile(
        output / "manufacturing/kicad_project.zip", "a", zipfile.ZIP_DEFLATED
    ) as archive:
        accepted_names = net_class_map_sidecar_names(identity.name)
        present = [name for name in archive.namelist() if name in accepted_names]
        if len(present) > 1:
            raise ValueError("Board 05 project archive contains multiple net-class maps")
        map_member = present[0] if present else net_class_map.name
        if not present:
            archive.write(net_class_map, map_member)
        members[net_class_map] = map_member
        for source, name in members.items():
            if archive.read(name) != archived_member(source, source.read_bytes(), identity, None):
                raise ValueError(f"Board 05 project archive differs from the checked source {name}")
    return map_member


def export_package(output):
    output = Path(output).resolve()
    destination = output / "manufacturing"
    routed = output / "bldc_controller_routed.kicad_pcb"
    net_class_map = output / "net_class_map.json"
    checked = json.loads((output / "readiness/kct-check.json").read_text())["pcb_sha256"]
    inputs = {routed: checked}
    for path in (routed.with_suffix(".kicad_pro"), routed.with_suffix(".kicad_dru"), net_class_map):
        inputs[path] = sha256_file(path)
    if sha256_file(routed) != checked:
        raise RuntimeError("Board 05 has changed since its manufacturer check")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "export",
            str(routed),
            "--mfr",
            "jlcpcb",
            "--output",
            str(destination),
            "--strict-preflight",
            # Plot the silk already checked by native DRC and the manufacturer.
            "--no-silk-fix",
            "--drc-report",
            str(output / "readiness" / "native-drc.json"),
            "--erc-report",
            str(output / "readiness" / "native-erc.json"),
        ],
        check=True,
    )
    for path, digest in inputs.items():
        if sha256_file(path) != digest:
            raise RuntimeError(
                f"Manufacturing export changed the checked Board 05 input {path.name}"
            )
    for command in [
        [
            "kicad-cli",
            "sch",
            "export",
            "pdf",
            str(output / "bldc_controller.kicad_sch"),
            "--output",
            str(destination / "schematic.pdf"),
        ],
        [
            "kicad-cli",
            "pcb",
            "export",
            "pdf",
            str(output / "bldc_controller_routed.kicad_pcb"),
            "--layers",
            "F.Cu,F.SilkS,Edge.Cuts",
            "--output",
            str(destination / "assembly-front.pdf"),
        ],
        [
            "kicad-cli",
            "pcb",
            "export",
            "pdf",
            str(output / "bldc_controller_routed.kicad_pcb"),
            "--layers",
            "B.Cu,B.SilkS,Edge.Cuts",
            "--output",
            str(destination / "assembly-back.pdf"),
        ],
    ]:
        subprocess.run(command, check=True)
    shutil.copy2(ROOT / "HARDWARE.md", destination / "HARDWARE.md")
    shutil.copy2(ROOT / "project.kct", destination / "project.kct")
    shutil.copytree(
        ROOT / "firmware",
        destination / "firmware",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.elf", ".gitignore"),
    )
    for filename in ["native-drc.json", "native-erc.json", "kct-check.json"]:
        shutil.copy2(output / "readiness" / filename, destination / filename)
    shutil.copy2(output / "readiness/kct-check.json", destination / "check-report.json")
    shutil.copy2(output / "lvs.json", destination / "lvs.json")
    map_member = complete_editable_project(output, routed, net_class_map)
    (destination / "README.txt").write_text(
        "Sensored BLDC controller, revision B — JLCPCB 4-layer/1oz assembly\n\n"
        "Start with an unloaded small motor, RUN open, a regulated 12V bench supply\n"
        "limited to 0.5A, and RV1 at its measured low-voltage end. Initial phase-current\n"
        "target is 0.5A; nominal comparator trip is 1A and firmware caps duty at 25%.\n"
        "Physical motor operation and thermal performance have not been measured.\n"
        "Do not increase these limits without bench validation. Read HARDWARE.md.\n\n"
        "Assembly: 42 CPL placements (36 SMT + six through-hole parts: J1-J5, RV1).\n"
        "JLCPCB assembles through-hole parts (#7242); J1-J5 and RV1 are in the BOM and CPL.\n"
        "Solder U2/U3 exposed pads; their thermal vias are part of the SMT land pattern.\n"
        "Observe IC/header pin1, C1 polarity and D1/D2/D3 polarity marks.\n\n"
        "J1:1=VIN,2=GND; J2:1/2/3=phases A/B/C; J3:1=5V,2=GND,3/4/5=Hall A/B/C,6=GND.\n"
        "J4 ISP:1=MISO,2=5V target sense,3=SCK,4=MOSI,5=RESET,6=GND. Disable programmer power.\n"
        "J5 RUN: close pin1 to ground pin2 after first leaving open to arm firmware.\n"
        "Power from J1 only; limit Hall load to 20mA and total 5V load to 30mA.\n"
        "Program firmware/bldc.hex via slow AVR ISP, using factory internal-RC clock fuses.\n\n"
        "Fresh native DRC/refill:0 violations/0opens; native ERC:0; label/copper LVS:0.\n"
        "Manufacturer checks:0 errors/0warnings. No DRC waivers.\n"
        "gerbers/gerbers.zip: fabrication; bom_jlcpcb.csv: procurement; cpl_jlcpcb.csv: SMT+THT.\n"
        "kicad_project.zip includes the PCB, schematic, design rules and local libraries.\n"
        f"Keep the enclosed {map_member} beside the PCB when re-exporting its current limits.\n"
        "schematic.pdf and assembly-front/back.pdf are production drawings.\n"
        "HARDWARE.md, firmware/, check reports and manifest.json complete the package.\n"
    )
    with (destination / "cpl_jlcpcb.csv").open(newline="", encoding="utf-8-sig") as stream:
        verify_placements(
            output / "bldc_controller_routed.kicad_pcb",
            [row["Designator"] for row in csv.DictReader(stream)],
        )
    refresh_manifest(destination)
    # The design-stage report precedes the package. Verify the completed
    # archive and record its manifest check in every delivered report copy.
    report_path = output / "readiness/kct-check.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            routed.name,
            "--mfr",
            "jlcpcb",
            "--format",
            "json",
            "--output",
            str(report_path),
        ],
        cwd=output,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    report = json.loads(report_path.read_bytes())
    if (
        report["pcb_sha256"] != checked
        or any(sha256_file(path) != digest for path, digest in inputs.items())
        or any(report["summary"][key] for key in ("errors", "warnings", "waived"))
        or any(
            report["meta_checks"][gate]["status"] != "PASSED"
            for gate in ("drc", "erc", "lvs", "manifest")
        )
    ):
        raise RuntimeError("Board 05 final manufacturer or package manifest check failed")
    for name in ("kct-check.json", "check-report.json"):
        shutil.copy2(report_path, destination / name)
    refresh_manifest(destination)
    bundle = write_manufacturing_archive(destination)
    with tempfile.TemporaryDirectory(prefix="bldc-editable-") as work, zipfile.ZipFile(bundle) as z:
        with zipfile.ZipFile(io.BytesIO(z.read("manufacturing/kicad_project.zip"))) as project:
            project.extractall(work)
        (board,) = Path(work).rglob("*.kicad_pcb")
        netlist = verify_editable_project(board)
        schematic_parts = {
            comp.attrib["ref"]: comp.findtext("./fields/field[@name='LCSC']")
            for comp in netlist.findall("./components/comp")
        }
        with z.open("manufacturing/bom_jlcpcb.csv") as raw:
            assignments = [
                (ref.strip(), row["LCSC Part #"])
                for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
                for ref in row["Designator"].split(",")
            ]
        if len(assignments) != 42 or sorted(assignments) != sorted(schematic_parts.items()):
            raise RuntimeError("Grouped purchasing BOM differs from the editable schematic")
    return destination


def verify_editable_project(board):
    """Validate the delivered filenames and saved copper in an extracted project."""
    sch = board.with_suffix(".kicad_sch")
    report = board.parent / "parity.json"
    flags = ["--severity-all", "--exit-code-violations", "--format", "json"]
    result = subprocess.run(
        ["kicad-cli", "pcb", "drc", board, "--schematic-parity", *flags, "-o", report],
        capture_output=True,
        text=True,
    )
    if parity_skipped_in_output(result.stdout, result.stderr):
        raise RuntimeError(f"KiCad could not load the matching schematic {sch.name} for parity")
    if not report.is_file():
        raise RuntimeError(f"No editable-project DRC report: {result.stdout}{result.stderr}")
    drc = json.loads(report.read_text())
    for key in ("schematic_parity", "violations", "unconnected_items"):
        if drc[key]:
            raise RuntimeError(f"Editable project {key}: {drc[key]}")
    result.check_returncode()
    netlist = board.parent / "netlist.xml"
    subprocess.run(
        ["kicad-cli", "sch", "export", "netlist", sch, "--format", "kicadxml", "-o", netlist],
        check=True,
    )
    exported = ET.parse(netlist)
    if not exported.findall("./nets/net/node"):
        raise RuntimeError(f"No schematic connections exported from {sch.name}")
    erc = board.parent / "erc.json"
    subprocess.run(["kicad-cli", "sch", "erc", sch, *flags, "-o", erc], check=True)
    if any(sheet["violations"] for sheet in json.loads(erc.read_text())["sheets"]):
        raise RuntimeError(f"Editable project ERC findings in {erc}")
    return exported


def refresh_manifest(destination):
    path = destination / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(
        mode="assembly", warning_counts={}, native_warning_counts={}, acknowledged_warning_rules=[]
    )
    manifest["files"] = {
        p.relative_to(destination).as_posix(): {
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "size": p.stat().st_size,
        }
        for p in sorted(destination.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n")
