"""Rebuild revision B from source and fingerprinted copper, then run real gates."""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hardware import ROOT, generate
from routing import apply_routing

from kicad_tools.manufacturers.attach_zone_dru import PAD_ZONE_CLEARANCES_META_KEY
from kicad_tools.sexp import parse_file, parse_string, serialize_sexp
from kicad_tools.validate.known_warnings import unaccepted_report_warnings

#: Issue #7298: no warning is accepted.  #7296 (the router refuses pad-edge
#: vias) removed the three ``via_at_pad_edge`` findings (R11-1, R9-1, D1-1)
#: this board used to pin, so any warning now fails; errors never pass.
BOARD05_KNOWN_WARNINGS: dict[tuple[str, str, str], str] = {}


def manufacturer_check_failures(check):
    """Why a ``kct check`` report fails this build's gate (empty when it passes)."""
    failures = []
    if check["summary"]["errors"]:
        failures.append(f"{check['summary']['errors']} error(s)")
    failures += unaccepted_report_warnings(check, BOARD05_KNOWN_WARNINGS)
    return failures


#: The net classes the staged routed sidecars declare (#6390) and whose copper
#: #6395 repaired.  ``Motor power`` and ``Ground plane`` are the reviewed classes
#: the board's ``net_class_map.json`` gives the motor and ground nets, which the
#: name classifier otherwise labels ``Digital``, ``Audio``,
#: ``HighCurrentSignal`` and ``Power`` (#10430).  ``Reviewed clearance -
#: <class>`` rules for these must survive every regeneration (#7391).
ROUTER_NETCLASSES = (
    "Audio",
    "Clock",
    "Debug",
    "Differential",
    "Digital",
    "Ground plane",
    "Motor power",
    "Power",
)


def clear_fiducial_openings(routed):
    """Leave at least 0.2 mm between each fiducial's mask opening and foreign copper."""
    board = parse_file(routed)
    setup = board.find_child("setup")
    board_margin = setup.find_child("pad_to_mask_clearance") if setup is not None else None
    for footprint in board.find_children("footprint"):
        if footprint.get_string(0) != "Fiducial:Fiducial_1mm_Mask2mm":
            continue
        for pad in footprint.find_children("pad"):
            # The stock pad opens 0.5 mm beyond its copper disk. KiCad takes
            # a pad's mask margin from its pad, footprint or board, in that order.
            margin = (
                pad.find_child("solder_mask_margin")
                or footprint.find_child("solder_mask_margin")
                or board_margin
            )
            minimum = round((margin.get_float(0) if margin is not None else 0.0) + 0.2, 6)
            clearance = pad.find_child("clearance")
            effective = clearance or footprint.find_child("clearance")
            if effective is not None and effective.get_float(0) >= minimum:
                continue
            if clearance is None:
                pad.append(parse_string(f"(clearance {minimum})"))
            else:
                clearance.set_value(0, minimum)
    routed.write_text(serialize_sexp(board))


#: Issue #9921: the pads whose front/back GND pour must keep the authored
#: 0.2 mm standoff the four ``Zone clearance`` DRU rules declare.  Inside
#: every rated part's courtyard the #8048 attach-zone exemptions drop that
#: pair to the 0.1016 mm fab floor, so the pour fills 0.1021 mm from these
#: pads' mask openings -- inside JLCPCB's 0.15 mm mask-to-copper warning
#: band (#8842). KiCad applies each pad's local clearance when it fills the
#: zone. U2's numbered leads have room for 0.2 mm. Its exposed pad and
#: thermal barrels retain their stock clearances. At U3, the 0.15 mm gaps
#: between adjacent leads require a separate zone-to-pad rule for the pour.
POUR_STANDOFF_MM = 0.2
POUR_STANDOFF_PADS = {
    "U1": frozenset(
        {
            "1",
            "2",
            "4",
            "6",
            "9",
            "10",
            "11",
            "12",
            "13",
            "14",
            "15",
            "16",
            "17",
            "18",
            "20",
            "23",
            "24",
            "25",
            "26",
            "28",
            "29",
            "30",
            "32",
        }
    ),
    "U2": frozenset(
        {
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
            "10",
            "11",
            "12",
            "13",
            "15",
            "16",
            "17",
            "18",
            "22",
            "23",
            "24",
            "25",
            "26",
            "27",
        }
    ),
    "J1": frozenset({"1"}),
    "J3": frozenset({"1", "3", "4", "5"}),
    "J4": frozenset({"1", "2", "3", "4", "5"}),
    "J5": frozenset({"1"}),
    "RV1": frozenset({"1", "2"}),
    "C1": frozenset({"1"}),
    "C2": frozenset({"1"}),
    "C3": frozenset({"1"}),
    "C6": frozenset({"1"}),
    "C7": frozenset({"1"}),
    "C8": frozenset({"1"}),
    "C9": frozenset({"1"}),
    "C10": frozenset({"1"}),
    "C11": frozenset({"1"}),
    "C12": frozenset({"1"}),
    "C13": frozenset({"1"}),
    "C14": frozenset({"1"}),
    "C15": frozenset({"1"}),
    "C16": frozenset({"1"}),
    "D2": frozenset({"1"}),
    "D3": frozenset({"2"}),
    "R1": frozenset({"1"}),
    "R3": frozenset({"1"}),
    "R8": frozenset({"1"}),
}


def restore_pour_standoff(routed):
    """Keep the authored 0.2 mm pour standoff at the #9921 pad openings.

    Every pad in :data:`POUR_STANDOFF_PADS` had the front or back GND pour
    fill to the exempted 0.1016 mm fab floor beside its mask opening; the
    pad-local clearance raises that fill back to the authored zone standoff
    (0.2005 mm measured after refill) without touching any other pad, net
    or rule.  Pads are matched by reference and number, so a stock-footprint
    regeneration cannot silently drop the declaration, and a missing pad
    fails the rebuild instead of shipping the finding.
    """
    board = parse_file(routed)
    bumped = 0
    seen = set()
    for footprint in board.find_children("footprint"):
        ref = next(
            (
                prop.get_string(1)
                for prop in footprint.find_all("property")
                if prop.get_string(0) == "Reference"
            ),
            None,
        )
        wanted = POUR_STANDOFF_PADS.get(ref)
        if not wanted:
            continue
        seen.add(ref)
        numbers = {pad.get_string(0) for pad in footprint.find_children("pad")}
        missing = wanted - numbers
        if missing:
            raise RuntimeError(f"{ref} lost the pour-standoff pads {sorted(missing)}")

        for pad in footprint.find_children("pad"):
            if pad.get_string(0) not in wanted:
                continue
            clearance = pad.find_child("clearance")
            if clearance is None:
                pad.append(parse_string(f"(clearance {POUR_STANDOFF_MM})"))
            elif clearance.get_float(0) < POUR_STANDOFF_MM:
                clearance.set_value(0, POUR_STANDOFF_MM)
            bumped += 1
    absent = set(POUR_STANDOFF_PADS) - seen
    if absent:
        raise RuntimeError(f"pour-standoff footprints missing from the board: {sorted(absent)}")
    expected = sum(len(pads) for pads in POUR_STANDOFF_PADS.values())
    if bumped != expected:
        raise RuntimeError(f"Pour standoff applied to {bumped} pads, expected {expected}")
    routed.write_text(serialize_sexp(board))


def reviewed_net_class_map(routed):
    """The reviewed ``net_class_map.json`` entries for the nets on ``routed`` (#10430).

    The map beside the routed PCB is the one ``kct check`` auto-loads, so it is
    read from the stage rather than from the recipe source: a stage carrying
    another copy is verified against what it actually ships.
    """
    from kicad_tools.manufacturers.project_generator import board_net_names
    from kicad_tools.router.rules import net_class_map_from_path

    nets = set(board_net_names(routed) or ())
    staged = net_class_map_from_path(routed.parent / "net_class_map.json", pcb_path=routed)
    return {net: routing for net, routing in staged.items() if net in nets}


def declare_router_netclasses(routed):
    """Declare the board's reviewed net classes in the routed sidecars (#6390, #7391).

    ``routing.json`` was routed by ``kct route`` at the router's per-class
    widths and clearances (Differential 0.15 mm wide at 0.15 mm, the signal
    classes 0.2 mm wide at 0.15 or 0.2 mm, Power 0.5 mm wide at 0.2 mm), and
    #6390 declared those classes in the committed ``.kicad_pro``/``.kicad_dru``
    so that kicad-cli and ``kct check`` hold the copper to them (#6395
    repaired the copper that sat below).  The authored project from
    ``hardware.generate`` declares only ``Default``, so without this step a
    regeneration silently dropped the ``Reviewed clearance - <class>`` rules,
    loosening those nets to the 0.1016 mm fab floor.

    The map is the router's effective one (``DEFAULT_NET_CLASS_MAP`` plus its
    name classifier) overlaid with the board's reviewed ``net_class_map.json``,
    the same overlay ``kct route`` applies to a loaded ``--net-class-map``.  It
    names the motor and ground nets ``Motor power`` and ``Ground plane`` where
    the classifier alone called FUSED and VM ``Digital``, GND_SENSE ``Audio``
    and GND ``Power`` (#10430).  The sidecar writer declares the classes and
    their clearances but clones each one from ``Default``, so every class
    inherited ``Default``'s 0.2 mm width; the route CLI's width-aware writer
    (#10133) then declares each class's own ``trace_width``.  The sidecar
    writer keeps the stricter authored minima (``min_track_width`` 0.15 mm, the
    routed copper's narrowest track) and emits the four 0.2 mm net- and
    layer-specific pour rules before the #8048 attach-zone exemptions.  KiCad
    uses the last matching rule when refilling under U2/U3.
    """
    from kicad_tools.cli.route_cmd import (
        _write_drc_constraint_sidecars,
        _write_netclasses_to_project,
    )
    from kicad_tools.manufacturers.project_generator import board_net_names
    from kicad_tools.router.net_class import classify_and_apply_rules
    from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP

    nets = sorted(name for name in (board_net_names(routed) or ()) if name)
    effective = dict(DEFAULT_NET_CLASS_MAP)
    for name, routing in classify_and_apply_rules(dict(enumerate(nets, 1))).items():
        effective.setdefault(name, routing)
    reviewed = reviewed_net_class_map(routed)
    effective.update(reviewed)
    _write_drc_constraint_sidecars(routed, "jlcpcb", 4, quiet=True, net_class_map=effective)
    _write_netclasses_to_project(routed, effective, quiet=True)
    _write_reviewed_class_widths(routed, effective)
    verify_router_netclasses(routed, reviewed)


def _write_reviewed_class_widths(routed, effective):
    """Declare each router class at exactly the width ``effective`` gives it.

    ``_write_netclasses_to_project`` merges floors with the larger value
    (#7385), so it raises a class above the ``Default`` clone the sidecar
    writer made but never lowers one: ``Differential`` is routed at 0.15 mm
    and would stay at ``Default``'s 0.2 mm, reporting every one of its own
    segments below class.  These classes are this recipe's own, so the width
    is overwritten and the project re-stamped as kct-owned.
    """
    from kicad_tools.core.project_file import add_netclass_definition, save_project
    from kicad_tools.manufacturers.project_generator import stamp_kct_project

    project = json.loads(routed.with_suffix(".kicad_pro").read_text())
    meta = project.get("meta", {})
    declared = set(meta.get("kct_router_netclasses", {}).get("classes", []))
    by_class = {routing.name: routing for routing in effective.values() if routing.name in declared}
    for class_name, routing in sorted(by_class.items()):
        add_netclass_definition(
            project, class_name, track_width=routing.trace_width, keep_stricter=False
        )
    stamp_kct_project(project)
    save_project(project, routed.with_suffix(".kicad_pro"))


def verify_router_netclasses(routed, reviewed):
    """Fail unless the staged project, its rules and its copper agree (#10430).

    Checking class names said nothing about what a net resolved to: FUSED was
    ``Digital`` at 0.2 mm while the map said ``Motor power`` at 0.5 mm.  Every
    routed net must now resolve, in the project on disk, to the class, width
    and clearance the reviewed map gives it; each declared class must carry its
    ``Reviewed clearance`` rule at that clearance; and the ``kct check`` rule
    that reported the 8 project and 24 width findings must find nothing on
    the staged copper.  Nothing here is waivable: a disagreement stops the
    rebuild before any native step spends time on it.
    """
    from kicad_tools.core.project_clearance import (
        authored_netclass_clearances,
        authored_netclass_widths,
    )
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.base import DRC_TOLERANCE
    from kicad_tools.validate.rules.netclass_agreement import check_netclass_agreement

    project = json.loads(routed.with_suffix(".kicad_pro").read_text())
    meta = project.get("meta", {})
    declared = tuple(sorted(meta.get("kct_router_netclasses", {}).get("classes", [])))
    if declared != ROUTER_NETCLASSES:
        raise RuntimeError(f"Router net classes not declared in the routed project: {declared}")
    if not reviewed:
        raise RuntimeError("No reviewed net-class entry matches a net on the routed board")

    nets = sorted(reviewed)
    widths = authored_netclass_widths(project, nets)
    clearances = authored_netclass_clearances(project, nets)
    dru = routed.with_suffix(".kicad_dru").read_text()
    for net, routing in sorted(reviewed.items()):
        width = widths.get(net)
        if (
            width is None
            or width.source_class != routing.name
            or abs(width.width - routing.trace_width) > DRC_TOLERANCE
        ):
            raise RuntimeError(
                f"Net {net!r} does not resolve to its reviewed class: the project says "
                f"{width.source_class if width else 'Default'} at "
                f"{width.width if width else 0:.3f} mm, the map says {routing.name} at "
                f"{routing.trace_width:.3f} mm"
            )
        held = clearances.get(net)
        if (
            held is None
            or held.source_class != routing.name
            or abs(held.clearance - routing.clearance) > DRC_TOLERANCE
        ):
            raise RuntimeError(
                f"Net {net!r} does not hold its reviewed clearance: the project gives "
                f"{held.clearance if held else 0:.3f} mm, the map says {routing.clearance:.3f} mm"
            )
        marker = f'(rule "Reviewed clearance - {routing.name}"'
        rule = dru.split(marker)[1] if dru.count(marker) == 1 else ""
        if f"(constraint clearance (min {routing.clearance:g}mm))" not in rule.split("\n\n")[0]:
            raise RuntimeError(f"No reviewed clearance rule for class {routing.name!r}: {marker}")

    findings = check_netclass_agreement(routed, PCB.load(str(routed)), reviewed)
    if findings.violations:
        raise RuntimeError(
            "Staged copper, project and net_class_map.json disagree: "
            + "; ".join(f"{v.rule_id} {v.nets[0]}: {v.message}" for v in findings.violations)
        )


def cleanup_copper(routed, max_passes=10):
    """Remove avoidable router copper to a fixed point (issue #8278).

    The grid router leaves 0.1 mm collinear steps, a few exact duplicates and
    redundant loop branches, which ``kct check`` reports as
    ``redundant_copper_*``.  ``kct pcb dedupe`` plus the DRC-verified
    ``kct pcb prune`` (kicad-cli ``--refill-zones`` judges every net's edit)
    remove them; each prune pass can expose more, so repeat until nothing
    changes.  Never ``--unverified``: an unverified prune raises.
    """
    from kicad_tools.pcb_prune import prune_board
    from kicad_tools.schema.pcb import PCB

    totals = {"duplicates": 0, "segments_removed": 0, "ends_snapped": 0}
    for _ in range(max_passes):
        pcb = PCB.load(str(routed))
        dup = pcb.dedupe_copper()
        if dup["segments"] or dup["vias"]:
            pcb.save(str(routed))
        pruned, result = prune_board(routed)
        if result.accepted and not result.drc_verified:
            raise RuntimeError(f"Copper prune was not DRC-verified: {result.drc_note}")
        if result.accepted:
            pruned.save(str(routed))
        totals["duplicates"] += dup["segments"] + dup["vias"]
        totals["segments_removed"] += result.stats["segments_removed"]
        totals["ends_snapped"] += result.stats["ends_snapped"]
        if not (dup["segments"] or dup["vias"] or result.accepted):
            return totals
    raise RuntimeError(f"Copper cleanup did not converge in {max_passes} passes: {totals}")


def prepare_routed_board(output):
    """Prepare the staged board, from fingerprinted copper through the pre-refill check."""
    routed = output / "bldc_controller_routed.kicad_pcb"
    shutil.copy2(output / "bldc_controller.kicad_pcb", routed)
    project = json.loads((output / "bldc_controller.kicad_pro").read_text())
    # These five stock openings need the front pour's authored clearance even
    # inside U3's attach zone. Pad-local clearance would also repel other pads.
    project.setdefault("meta", {})[PAD_ZONE_CLEARANCES_META_KEY] = {
        "F.Cu": {"GND": {"U3": ["1", "3", "5", "7", "8"]}}
    }
    routed.with_suffix(".kicad_pro").write_text(json.dumps(project, indent=2))
    fingerprint = apply_routing(routed)
    declare_router_netclasses(routed)
    # Issue #8532: every board carries global fiducials and a board-ID
    # silkscreen.  Added after the reviewed copper recipe is applied (the
    # recipe's pad/placement fingerprint is checked before this) and before
    # stitching, copper cleanup and the native refill, so every downstream
    # gate sees the final geometry (#9057).  Fixed here, never acknowledged.
    from kicad_tools.cli.pcb_add_board_info import run_add_board_info
    from kicad_tools.cli.pcb_add_fiducials import run_add_fiducials

    if run_add_fiducials(routed, sides="populated") != 0:
        raise RuntimeError("pcb add-fiducials failed")
    clear_fiducial_openings(routed)
    restore_pour_standoff(routed)
    if run_add_board_info(routed, auto=True) != 0:
        raise RuntimeError("pcb add-board-info failed")
    evidence = output / "readiness"
    evidence.mkdir(exist_ok=True)
    # First check emits current manufacturer DRU. Unfilled zones are not a pass.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(routed),
            "--mfr",
            "jlcpcb",
            "--emit-dru",
            "--format",
            "json",
            "--output",
            str(evidence / "before-refill.json"),
        ],
        stdout=subprocess.DEVNULL,
        check=False,
    )
    return routed, fingerprint


def generate_routed_board(output):
    """Generate the hardware and prepare its reviewed copper and sidecars for refill."""
    output = Path(output).resolve()
    generate(output)
    # A local copy lets procurement/export discover exact identities in scratch.
    (output / "project.kct").write_text((ROOT / "project.kct").read_text().replace("output/", ""))
    # Published beside the PCB so `kct check` auto-loads the declared ampacity targets.
    shutil.copy2(ROOT / "net_class_map.json", output / "net_class_map.json")
    return prepare_routed_board(output)


def build(output):
    output = Path(output).resolve()
    routed, fingerprint = generate_routed_board(output)
    evidence = output / "readiness"
    # The shared stage proves every plane net on actual refilled copper.
    # A failed or dangling stitch never replaces the staged input, and its
    # complete candidate/evidence directory survives outside recipe staging.
    from kicad_tools.stitching import complete_power_connections

    stitch_evidence = Path(tempfile.mkdtemp(prefix="bldc-power-stitch-")) / "evidence"
    complete_power_connections(routed, evidence_dir=stitch_evidence)
    shutil.copytree(stitch_evidence, evidence / "stitch")
    print(f"Copper cleanup (dedupe + verified prune): {cleanup_copper(routed)}")
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            str(routed),
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "--output",
            str(evidence / "native-drc.json"),
        ],
        check=True,
    )
    native = json.loads((evidence / "native-drc.json").read_text())
    from kicad_tools.cli.kicad_toolchain import require_library_environment

    # Issue #6962: a library-table finding describes the environment, not
    # the board, so it must never become evidence.
    require_library_environment(native)
    # Only "error"-severity findings are a fab-stop. kicad-cli's default JSON
    # includes warnings in `violations` too (e.g. the JLCPCB "Silk to Pad"
    # floor is a legibility/DFM limit KiCad itself classifies as a warning,
    # per dru_generator.py's rationale for leaving it unforced) -- gating on
    # any violation regardless of severity turned a cosmetic advisory into
    # "do not manufacture".
    blocking = [v for v in native["violations"] if v.get("severity") == "error"]
    if blocking or native["unconnected_items"]:
        # main() removes failed staging directories; keep the actual native
        # findings in the CI log so a missing library or copper fault is clear.
        print(json.dumps(native, indent=2), file=sys.stderr)
        raise RuntimeError("Fresh native refill/DRC failed; do not manufacture")
    subprocess.run(
        [
            "kicad-cli",
            "sch",
            "erc",
            str(output / "bldc_controller.kicad_sch"),
            "--format",
            "json",
            "--output",
            str(evidence / "native-erc.json"),
        ],
        check=True,
    )
    erc = json.loads((evidence / "native-erc.json").read_text())
    if any(sheet["violations"] for sheet in erc["sheets"]):
        print(json.dumps(erc, indent=2), file=sys.stderr)
        raise RuntimeError("Fresh native schematic ERC failed")
    from kicad_tools.lvs import write_lvs_report

    write_lvs_report(output / "bldc_controller.kicad_sch", routed, output, require_clean=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(routed),
            "--mfr",
            "jlcpcb",
            "--format",
            "json",
            "--output",
            str(evidence / "kct-check.json"),
        ],
        stdout=subprocess.DEVNULL,
        check=False,
    )
    check = json.loads((evidence / "kct-check.json").read_text())
    failures = manufacturer_check_failures(check)
    if failures:
        raise RuntimeError(
            "Manufacturer check contains errors or unaccepted warnings: " + "; ".join(failures)
        )
    if any(check["meta_checks"][gate]["status"] != "PASSED" for gate in ("drc", "erc", "lvs")):
        raise RuntimeError("Electrical/manufacturer meta-check failed")
    print(f"Revision B rebuilt and verified; pad/placement fingerprint {fingerprint}")
    return routed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=ROOT.parent / "output")
    args = parser.parse_args()
    from package import export_package

    # Failed regeneration never replaces the last reviewed manufacturing output.
    with tempfile.TemporaryDirectory(prefix="bldc-rebuild-") as staging:
        stage = Path(staging)
        build(stage)
        export_package(stage)
        if args.output.exists():
            backup = Path(tempfile.mkdtemp(prefix="bldc-previous-output-")) / "output"
            shutil.copytree(args.output, backup)
            print(f"Previous output preserved at {backup}")
            shutil.rmtree(args.output)
        shutil.copytree(stage, args.output)
    return 0


if __name__ == "__main__":
    from kicad_tools.cli.kicad_toolchain import run_on_evidence_toolchain

    # Issue #6962: reject any other KiCad release before writing evidence.
    # The host-side generator reads the same pinned image's stock land patterns
    # used by the native commands in both this producer and package.py.
    raise SystemExit(run_on_evidence_toolchain(main, [ROOT.parents[2]], pin_host_footprints=True))
