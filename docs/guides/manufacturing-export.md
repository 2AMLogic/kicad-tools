# Tutorial: Exporting for Manufacturing (JLCPCB Workflow)

This tutorial walks through exporting your KiCad design for PCB fabrication and assembly at JLCPCB, one of the most popular low-cost PCB manufacturers.

## Overview

To order assembled PCBs from JLCPCB, you need:

1. **Gerber files** - PCB fabrication data (copper layers, soldermask, silkscreen)
2. **Drill files** - Hole locations and sizes
3. **BOM (Bill of Materials)** - List of components with LCSC part numbers
4. **CPL (Component Placement List)** - Pick-and-place coordinates

kicad-tools can generate all of these in JLCPCB's required format.

The manufacturing package's `kicad_project.zip` includes the selected PCB,
adjacent schematic/project files, and the project-local `sym-lib-table` with
its referenced KiCad symbol libraries. Relative paths and `${KIPRJMOD}/`
paths retain their subdirectories after extraction. Repeated references
to the same file produce one archive member.

Project ZIP creation reports an export error for a malformed table, missing
library, or nonportable table entry (absolute paths, parent traversal,
host environment variables, non-KiCad libraries, or symlinks outside the
project). Move those dependencies under the project and use project-relative
URIs before exporting. Libraries supplied by KiCad's global installation
are not copied; footprint libraries and 3D models are outside this symbol
packaging support.

## The Editable Project: One Basename, One Working Revision

KiCad opens a project through one basename: `board.kicad_pro` pairs with
`board.kicad_sch` and `board.kicad_pcb` in the same directory. Keep that
layout for the working revision, and let an adjacent `VERSION.md` say which
revision it holds:

```
board/
├── board.kicad_pro     # net classes, design rules and library tables
├── board.kicad_sch     # root sheet; its sub-sheets stay beside it
├── board.kicad_pcb     # the working revision's board
├── board.kicad_dru     # custom rules, when the board has them
├── VERSION.md          # the working revision and where the retired ones are
└── history/            # retired snapshots, suffixed with the revision they froze
    └── board_v07.{kicad_pro,kicad_sch,kicad_pcb,kicad_dru}
```

The working revision always keeps the same file names. When a revision is
finished, copy the trio into `history/` with the `_vNN` suffix of the revision
it freezes, then continue editing the same `board.*` files as the next
revision:

| Step | Files in `board/` | `VERSION.md` |
| --- | --- | --- |
| Working on v07 | `board.kicad_pro`, `board.kicad_sch`, `board.kicad_pcb` | `Working revision: v07` |
| v07 retired | add `history/board_v07.kicad_pro`, `history/board_v07.kicad_sch`, `history/board_v07.kicad_pcb` | v07 is frozen under `history/` |
| Working on v08 | the same `board.kicad_pro`, `board.kicad_sch`, `board.kicad_pcb` | `Working revision: v08` |

```markdown
# board revisions

Working revision: v08

| Revision | Status | Where |
| --- | --- | --- |
| v07 | retired | `history/board_v07.kicad_pcb` |
| v08 | working | `board.kicad_pcb` |
```

### What the package delivers

`kct export` and `kct readiness --generate` put one openable project in
`kicad_project.zip`: the board, its `.kicad_dru`, the project file and the
root sheet share the root sheet's basename, and the `VERSION.md` kept beside
the board travels with them.

The checked files do not have to be laid out that way yet. A revisioned board
(`board_v07.kicad_pcb` with `board_v07.kicad_pro`) or `kct route`'s
`board_routed.kicad_pcb`, checked against `board.kicad_sch`, is packaged as
one `board` project:

| In `kicad_project.zip` | Checked source |
| --- | --- |
| `board.kicad_pcb` | `board_v07.kicad_pcb` |
| `board.kicad_dru` | `board_v07.kicad_dru` |
| `board.kicad_pro` | `board_v07.kicad_pro` |
| `board.kicad_sch` | `board.kicad_sch` |
| `VERSION.md` | written while packaging |

The renaming happens in the archive only. The checked files are not modified,
the packaged `board.kicad_pro` names itself correctly in `meta.filename`, and
`VERSION.md` records the working revision and the table above, so the revision
stays recoverable after extraction. With `--project-root` the archive keeps
the project tree, so `${KIPRJMOD}/../` references still resolve, and the same
renaming applies inside it.

`kct readiness --verify` binds every member to its checked source: a changed
source or an edited member fails verification. A bundle written before this
layout, which keeps the checked file names, still verifies; regenerate it to
receive the paired project. When the archive already holds another file under
the project's name — the unrouted original's `board.kicad_pro` beside a
`board_routed.kicad_pro`, for instance — readiness refuses before advertising
the archive and names both files: keep only the selected board's project
beside it, or rename one of them.

If the checked root sheet lives in a different directory, the archive puts it
beside the delivered board and moves its child sheets with it. Their relative
paths stay intact. The project's library tables and libraries keep their paths
relative to the delivered project. For a schematic tree outside the PCB
directory, pass its root with `kct readiness --sch` and select their common
directory with `--project-root`. If a child cannot travel with the root sheet,
put the root beside the checked board and update its sheet references before
exporting.

## Prerequisites

```bash
pip install kicad-tools
```

You'll also need KiCad 8+ installed for Gerber generation via `kicad-cli`.

## Separate editable KiCad files from generated output

Keep the PCB, schematic, project, rules and local libraries under version control.
For example, place them in `kicad/`, alongside a project-root `.kicad-tools.toml`
that declares a sibling output directory. Both the output path and receipt
input names are relative to the directory containing the TOML file. You can
pass an enclosing checkout to `--project-root` and move the declared project
on its own. A narrower `--project-root` must contain the board, its dependencies
and the output directory.

```toml
[readiness]
output_root = "output"
```

From the project root, generate the package and its readiness receipt. Rendering
the selected board puts the views and their provenance in `output/renders/`.
The verification command writes a second receipt in `output/readiness-verification/`.

```bash
kct readiness kicad/controller.kicad_pcb --sch kicad/controller.kicad_sch --mfr jlcpcb --generate
kct render kicad/controller.kicad_pcb
kct readiness kicad/controller.kicad_pcb --sch kicad/controller.kicad_sch --mfr jlcpcb --verify
```

After reviewing the package, use your reviewer name to sign it. The generated
receipt at `output/readiness.json` and the verification receipt shown here
both bind the release to the selected PCB and bundle. Keep the receipt used
at sign-off with the package. Submission checks the PCB and schematic paths
and hashes against that receipt, including a schematic in a sibling source
directory. A dry run prepares a local order preview and checks stock. Give it
a new directory outside the source and manufacturing directories.

```bash
kct tapeout-sign-off output/manufacturing --board kicad/controller.kicad_pcb \
  --readiness output/readiness-verification/readiness.json --reviewer "Your name"
kct submit-jlcpcb output/manufacturing --board kicad/controller.kicad_pcb \
  --schematic kicad/controller.kicad_sch --quantity 5 --dry-run -o ../controller-submission-preview
```

These root-anchored `.gitignore` entries cover disposable output and local
editor state.

```gitignore
/output/
/kicad/*.kicad_prl
/kicad/~*.lck
```

Keep historical source revisions in `history/`. Copy the exact released
package, signed manifest, readiness receipt and human fabrication review
records you intend to retain into a versioned directory such as
`releases/rev-d/`. These paths stay trackable alongside the editable design.

## Are We Ship-Ready? (`kct fleet status`)

Before generating manufacturing artefacts, ask the repo whether every board
is actually ready to ship. `kct fleet status` walks every per-board
subdirectory under `boards/`, inspects the routed PCB, DRC report, and
manufacturing outputs, and surfaces a one-line verdict per board.

```bash
# Plain summary (default: --boards-dir boards, table output)
kct fleet status

# CI-friendly JSON for downstream tooling
kct fleet status --format json > fleet.json

# Only show what is actually ready to ship right now
kct fleet status --ship-only
```

A board is "ship-ready" when **all** gates are green:

1. Routed PCB exists at the expected glob (`*_routed.kicad_pcb` by default).
2. Net status (the same check `kct net-status` runs) is clean — no
   incomplete or unrouted nets.
3. DRC report exists and has zero errors.
4. Manufacturing artefacts (gerbers, BOM, CPL) are present and not stale
   relative to the routed PCB.
5. No open known defect is recorded in `output/readiness.json` (Issue #7261).
   An open defect adds a `known defect #N` blocker. Boards that are ready but
   carry disclosed exceptions or advisories show `YES*` / `known: #N` instead
   of a plain `YES`.

If any gate fails, the table view lists the first blocker; `--format json`
emits the full per-board breakdown. Treat `kct fleet status --ship-only`
returning a non-empty list as "go" for the export pipeline below.

See also: [CLI Reference → fleet status](../reference/cli.md#fleet-status).

---

## Routing Completeness Preflight

The default `kct build` sequence runs a routing-completeness preflight
between `stitch` and `verify` that blocks the build if any nets are
incomplete or unrouted. This closes the gap where `kct build` would emit
gerbers, BOM, and CPL even when nets were unconnected.

On failure the build prints, for example:

```text
preflight-routing: 3/214 nets incomplete
  /SDA: incomplete (2 segments missing)
  /SCL: incomplete (1 segment missing)
  /HALL_B: unrouted
```

…and halts before any manufacturing artefact is written.

Two escape hatches are recognised:

- `--allow-incomplete` — the advertised, CI-greppable opt-out. Use this for
  intentional WIP builds (e.g. preview gerbers for a board still in
  routing). Failure becomes a warning, the build continues.
- `--force` — global build override; parity with `--step sync --force`.
  Also converts the failure into a warning.

```bash
# Default end-to-end build (preflight runs automatically between stitch
# and verify; refuses to ship gerbers for a board with unrouted nets).
# `spec` is positional, not a flag.
kct build boards/05-bldc-motor-controller/project.kct

# Run only the preflight check (read-only, no side-effects).
kct build boards/05-bldc-motor-controller/project.kct \
    --step preflight-routing

# Bypass for an intentional WIP preview.
kct build boards/05-bldc-motor-controller/project.kct --allow-incomplete
```

The check is read-only and uses `NetStatusAnalyzer` in-process (no subprocess
overhead). See [CLI Reference → build](../reference/cli.md#build).

---

## Quick Start: One-Command Export

The fastest way to export everything:

```python
from kicad_tools.export import AssemblyPackage

# Create complete manufacturing package
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="board.kicad_sch",
    manufacturer="jlcpcb",
)

# Export to output directory
result = pkg.export("output/jlcpcb/")

print(f"Exported to: {result.output_dir}")
print(f"  Gerbers: {result.gerber_zip}")
print(f"  BOM: {result.bom_file}")
print(f"  CPL: {result.cpl_file}")
```

This creates:
```
output/jlcpcb/
├── gerbers.zip          # Upload to JLCPCB for PCB fab
├── bom_jlcpcb.csv       # Upload for assembly BOM
└── cpl_jlcpcb.csv       # Upload for assembly placement
```

## Step-by-Step Export

### Step 1: Generate Gerber Files

```python
from kicad_tools.export import GerberExporter

# Create exporter
exporter = GerberExporter("board.kicad_pcb")

# Export with the JLCPCB preset (layer set, origin, drill options)
result = exporter.export_for_manufacturer("jlcpcb", "output/gerbers/")

print(f"Generated {len(result.files)} Gerber files")
for f in result.files:
    print(f"  {f}")
```

The zip holds kicad-cli's own file names, with Protel extensions and X2
`%TF.FileFunction` attributes, plus a `.gbrjob` job file:
```
output/gerbers/
├── board-F_Cu.gtl           # Front copper
├── board-B_Cu.gbl           # Back copper
├── board-In1_Cu.g1          # Inner copper (4-layer boards: In1/In2)
├── board-F_Mask.gts         # Front soldermask
├── board-B_Mask.gbs         # Back soldermask
├── board-F_Silkscreen.gto   # Front silkscreen
├── board-B_Silkscreen.gbo   # Back silkscreen
├── board-Edge_Cuts.gm1      # Board outline
├── board-job.gbrjob         # Gerber job file (references every layer)
├── board-PTH.drl            # Plated holes (Excellon)
└── board-NPTH.drl           # Non-plated holes (Excellon)
```

Drill files follow the preset's `merge_pth_npth` choice (Issue #6167).
`jlcpcb`, `pcbway` and `seeed` ship KiCad's default separate
`board-PTH.drl` + `board-NPTH.drl` (the NPTH file is written even when the
board has no non-plated holes). `oshpark` ships one merged `board.drl`,
which OSH Park's KiCad guide asks for. Set `GerberConfig(merge_pth_npth=...)`
to override; `drill_units` (`mm`/`in`), `drill_zeros_format` and
`minimal_header` map onto kicad-cli's `--excellon-*` flags. A drill export
that kicad-cli rejects, or that writes no drill file, raises `ExportError`.

Every manufacturer preset (`jlcpcb`, `pcbway`, `seeed`, `oshpark`) keeps
these names. A preset chooses the layer set, origin and drill options. It
never renames files (Issue #6163). The fab KiCad guides for JLCPCB, PCBWay
and OSH Park take this output as-is, and Seeed's own KiCad tooling exports
the same kicad-cli names. Fab-specific renames such as `.GKO` for the outline
are unnecessary, and they would break the kicad-tools consumers that read
layers from these names.

#### V-score layers (panels)

A V-cut panel's score lines live on a user drawing layer (`Cmts.User`,
`Dwgs.User`, `Eco1.User`, `Eco2.User` or `User.N`), and the exporter adds
that layer to the Gerber set when it finds score lines there (Issues #6156,
#6193). It accepts the following:

- **Lines drawn by `kct panel`.** These are tagged, so they always count.
- **Untagged straight lines that cross the whole outline**, as KiKit draws
  them.
- **Untagged partial and jump scores that complete a separation.** Walk
  the line across the outline. Every stretch of board material on it must
  be covered by score pieces, and every gap must lie off the board: in a
  slot or cutout, or between board outlines. A score that hands off to a
  routed slot running on to the far edge qualifies. So does a seam that
  jumps the space between boards.

Section dividers, fold lines, title-block rules and dashed lines on a single
board do not count. Nor do lines that end on a mounting hole, a cutout or a
connector slot with solid board beyond it, the usual result of snapping a
drafting line to a hole. Two drawings do count: a line broken only around a
hole, and a divider ending on the notch wall of an L-shaped board. Each
crosses all the material on its line, so it carries the same risk as a
solid edge-to-edge line. Only top-level Edge.Cuts graphics count as slot or
cutout geometry; a slot drawn inside a footprint does not.

If a panel's scores do not fit these rules, name the layer yourself. The
layer is then plotted whether or not detection finds it:

```bash
kct export panel.kicad_pcb --vscore-layer User.2      # repeatable
python -m kicad_tools.cli.export_gerbers panel.kicad_pcb --vscore-layer User.2
```

The Python equivalent is `GerberConfig(vscore_layers=["User.2"])`, or
`export_for_manufacturer(..., vscore_layers=[...])` when you use a preset.
`GerberConfig(include_vscore=False)` turns detection off, but it does not
drop layers you named explicitly. If you name a layer the board's layer
table does not define, kicad-cli plots nothing for it, so the export warns
on stderr.

### Step 2: Generate BOM

JLCPCB's BOM format requires LCSC part numbers:

```python
from kicad_tools import Schematic
from kicad_tools.export import export_bom

sch = Schematic.load("board.kicad_sch")

# Export in JLCPCB format
export_bom(
    schematic=sch,
    output_path="output/bom_jlcpcb.csv",
    manufacturer="jlcpcb",
)
```

Output format:
```csv
Comment,Designator,Footprint,LCSC Part Number
100nF,C1,Capacitor_SMD:C_0402,C1525
100nF,C2,Capacitor_SMD:C_0402,C1525
10k,R1,Resistor_SMD:R_0402,C25744
ATmega328P,U1,Package_QFP:TQFP-32,C14877
```

#### Adding LCSC Part Numbers

LCSC part numbers come from your schematic's component properties. In KiCad:

1. Open Symbol Properties
2. Add a field named `LCSC` (or `JLCPCB Part #`)
3. Enter the LCSC part number (e.g., `C1525`)

Or use kicad-tools to lookup parts:

```python
from kicad_tools.parts import lookup_lcsc_part

# Find LCSC part for a component
results = lookup_lcsc_part(
    value="100nF",
    footprint="0402",
    category="capacitor",
)

for part in results[:5]:
    print(f"{part.number}: {part.description} (${part.price})")
```

### Step 3: Generate CPL (Pick-and-Place)

```python
from kicad_tools import PCB
from kicad_tools.export import export_pnp

pcb = PCB.load("board.kicad_pcb")

# Export in JLCPCB format
export_pnp(
    pcb=pcb,
    output_path="output/cpl_jlcpcb.csv",
    manufacturer="jlcpcb",
)
```

Output format:
```csv
Designator,Val,Package,Mid X,Mid Y,Rotation,Layer
C1,100nF,0402,23.45,15.67,90,top
C2,100nF,0402,25.12,15.67,90,top
R1,10k,0402,30.00,20.00,0,top
U1,ATmega328P,TQFP-32,50.00,40.00,0,top
```

## CLI Commands

### Export Gerbers

```bash
# Export with the JLCPCB preset
kct export gerbers board.kicad_pcb --mfr jlcpcb -o output/

# Generic export
kct export gerbers board.kicad_pcb -o output/
```

### Export BOM

```bash
# JLCPCB format
kct bom board.kicad_sch --format jlcpcb -o bom_jlcpcb.csv

# Generic CSV
kct bom board.kicad_sch --format csv --group -o bom.csv
```

### Via Stitching for Power Planes

`kct stitch` adds via stitching to power-plane nets between layers. When you
pass `--mfr`, the via diameter and drill are resolved from the manufacturer
YAML using the board's actual copper layer count — so stitching stays
consistent with the rules the router and DRC are enforcing. See
[CLI Reference → stitch](../reference/cli.md#stitch) for the full flag list.

```bash
# Auto-detect power nets, JLCPCB tier-1 via geometry
kct stitch board.kicad_pcb --mfr jlcpcb-tier1 --copper 1.0

# Blanket-stitch GND on a 3mm grid
kct stitch board.kicad_pcb --net GND --blanket --spacing 3.0
```

---

## Complete JLCPCB Workflow

Here's a complete script for preparing a JLCPCB order:

```python
#!/usr/bin/env python3
"""
Generate complete JLCPCB manufacturing package.

Usage:
    python export_jlcpcb.py board.kicad_pcb board.kicad_sch output/
"""

import sys
from pathlib import Path
from kicad_tools import Schematic, PCB
from kicad_tools.export import (
    AssemblyPackage,
    GerberExporter,
    export_bom,
    export_pnp,
)
from kicad_tools.drc import check_manufacturer_rules


def export_for_jlcpcb(pcb_path: str, sch_path: str, output_dir: str):
    """Generate all files needed for JLCPCB order."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    print("JLCPCB Manufacturing Export")
    print("=" * 50)

    # Load files
    print("\n1. Loading design files...")
    pcb = PCB.load(pcb_path)
    sch = Schematic.load(sch_path)
    print(f"   PCB: {len(pcb.footprints)} footprints")
    print(f"   Schematic: {len(sch.symbols)} symbols")

    # Validate against JLCPCB rules
    print("\n2. Checking against JLCPCB design rules...")
    result = check_manufacturer_rules(pcb, "jlcpcb")
    if result.passed:
        print("   ✓ Design passes all JLCPCB rules")
    else:
        print("   ✗ Warnings:")
        for v in result.violations:
            print(f"     - {v}")

    # Generate Gerbers
    print("\n3. Generating Gerber files...")
    gerber_dir = output / "gerbers"
    exporter = GerberExporter(pcb_path)
    gerber_result = exporter.export_for_manufacturer("jlcpcb", str(gerber_dir))
    print(f"   Generated {len(gerber_result.files)} files")

    # Create ZIP for upload
    import shutil

    gerber_zip = output / "gerbers.zip"
    shutil.make_archive(str(output / "gerbers"), "zip", gerber_dir)
    print(f"   Created: {gerber_zip}")

    # Generate BOM
    print("\n4. Generating BOM...")
    bom_path = output / "bom_jlcpcb.csv"
    export_bom(sch, str(bom_path), manufacturer="jlcpcb")
    print(f"   Created: {bom_path}")

    # Check for missing LCSC numbers
    missing_lcsc = []
    for symbol in sch.symbols:
        if not symbol.get_property("LCSC"):
            missing_lcsc.append(symbol.reference)
    if missing_lcsc:
        print(f"   ⚠ Missing LCSC numbers: {', '.join(missing_lcsc[:5])}")
        if len(missing_lcsc) > 5:
            print(f"     ...and {len(missing_lcsc) - 5} more")

    # Generate CPL
    print("\n5. Generating pick-and-place file...")
    cpl_path = output / "cpl_jlcpcb.csv"
    export_pnp(pcb, str(cpl_path), manufacturer="jlcpcb")
    print(f"   Created: {cpl_path}")

    # Summary
    print("\n" + "=" * 50)
    print("Export Complete!")
    print(f"\nFiles ready for JLCPCB upload:")
    print(f"  1. Gerbers:  {gerber_zip}")
    print(f"  2. BOM:      {bom_path}")
    print(f"  3. CPL:      {cpl_path}")
    print("\nNext steps:")
    print("  1. Go to jlcpcb.com and start a new order")
    print("  2. Upload gerbers.zip for PCB fabrication")
    print("  3. Enable 'SMT Assembly' and upload BOM + CPL")
    print("  4. Review and place order")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python export_jlcpcb.py <pcb> <schematic> <output_dir>")
        sys.exit(1)
    export_for_jlcpcb(sys.argv[1], sys.argv[2], sys.argv[3])
```

## Via Drills Against the Mask, Stencil and Land (`--via-land-overlap`)

Under an ordinary via process every routed via should stay tented and off the
stencil. `--via-land-overlap` measures the archive the export has just written,
so it reports what the assembler receives rather than what the source board
says (Issue #10357):

```bash
kct export board.kicad_pcb --mfr jlcpcb --via-land-overlap --format json
```

The opt-in appends a `via_land_overlap` entry to the preflight list of the JSON
output and of `manifest.json`. A drill counts when the exported drill disk of a
via matched to a plated Excellon hit shares area with the solder-mask opening,
the stencil opening and one SMD land on the same face. A drill whose center
lies outside the opening still counts when its edge reaches it.

```json
{
  "name": "via_land_overlap",
  "status": "WARN",
  "data": {
    "expected_vias": 499, "matched_vias": 499, "distinct_holes": 22,
    "dispositions": {"undeclared": 22, "filled_capped": 0, "exposed_thermal": 0},
    "coverage": {"status": "complete", "reasons": []},
    "observations": [
      {"face": "F", "reference": "U1", "pad": "3", "drill_file": "board-PTH.drl",
       "drill_center_mm": [12.7, -3.4], "drill_diameter_mm": 0.3,
       "overlap_area_mm2": 0.0075, "edge_overlap": true,
       "process_disposition": "undeclared", "declarations": []}
    ]
  }
}
```

- `drill_center_mm` is the coordinate as printed in `drill_file`, in millimetres.
  A physical hole is counted once in `distinct_holes` however many faces
  observe it.
- `process_disposition` names the declaration that applies to an intersection:
  `filled_capped` under the bundle's declared via-in-pad process (the
  manifest's `fabrication_process`), `exposed_thermal` on a pad carrying
  KiCad's `pad_prop_heatsink` property, or `undeclared`. A manufacturer's
  via-in-pad capability or a pad name alone is not a declaration.
- `coverage.status` is `complete` only when every routed via matched a drill
  hit and the mask and paste plots of every reached face were read and line up
  with the board. Otherwise `reasons` names what is missing, unsupported,
  unregistered or ambiguous, the findings of the measured faces are kept, and
  a zero is not a complete one.

The result is `OK` when coverage is complete and every intersection is
declared, and `WARN` otherwise; it never blocks the export. A standard export
does not run the measurement.

## Other Manufacturers

kicad-tools supports multiple manufacturers:

### PCBWay

```python
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="board.kicad_sch",
    manufacturer="pcbway",
)
result = pkg.export("output/pcbway/")
```

### Seeed Fusion

```python
pkg = AssemblyPackage.create(
    pcb="board.kicad_pcb",
    schematic="board.kicad_sch",
    manufacturer="seeed",
)
result = pkg.export("output/seeed/")
```

### OSHPark (PCB only, no assembly)

```python
from kicad_tools.export import GerberExporter

exporter = GerberExporter("board.kicad_pcb")
exporter.export_for_manufacturer("oshpark", "output/oshpark/")
```

## Troubleshooting

### "kicad-cli not found"

Install KiCad 8+ and ensure `kicad-cli` is in your PATH:

```bash
# macOS
export PATH="/Applications/KiCad/KiCad.app/Contents/MacOS:$PATH"

# Linux
export PATH="/usr/bin:$PATH"

# Windows
# Add KiCad bin folder to PATH in System Settings
```

### "Missing LCSC part numbers"

Add LCSC numbers to your schematic symbols:
1. Open KiCad Schematic Editor
2. Select component → Edit Properties
3. Add field: `LCSC` = `C1525` (or appropriate part number)

Find LCSC part numbers at: https://www.lcsc.com

### "CPL rotation is wrong"

JLCPCB's library models do not always share KiCad's 0° orientation, so a part
can land a quarter turn off in the fab's placement preview. Two sources of
correction feed the CPL, and corrections are always **added** to the KiCad
rotation (`(kicad + correction) % 360`):

1. The shipped manufacturer table in `manufacturers/data/jlcpcb_rotations.yaml`
   contains footprint defaults and corrections verified for individual catalog
   parts on their KiCad footprints.
2. A **per-project sidecar**, `cpl_rotations.yaml`, committed next to your
   `.kicad_pcb`. `kct export` and `kct readiness --generate` discover it
   automatically:

```yaml
# cpl_rotations.yaml -- degrees added to the KiCad rotation in the CPL
by_reference:        # one specific placement
  J2: 90
by_lcsc:             # every placement of that LCSC part
  C5156614: 90       # 1x05 pin header
  C5116483: 90       # 1x04 pin header
  C4991872: 270      # SJ-3523-SMT audio jack
```

For each placement, the exporter first checks the project's `by_reference`
and then `by_lcsc`. It next checks the manufacturer's catalog entry for the
part and footprint, then the footprint default. It uses the first matching
offset. The selected BOM part supplies the LCSC number when available. The PCB
footprint's `LCSC` property is the fallback. With `--no-bom`, a JLCPCB export
still reads part assignments from the design, project spec and any existing
BOM selected for merging. To reuse catalog selections from an earlier export
in another directory, pass `--merge-lcsc-from path/to/bom_jlcpcb.csv`.

| JLCPCB part | KiCad footprint | Total offset |
| --- | --- | ---: |
| C51118 (AP2112K-3.3TRG1) and C19829593 (74LVC1G17GV) | SOT-23-5 | 270° |
| C2286 (KT-0603R) and C12624 (KT-0603G) | LED_0603 | 0° |
| C408389 (BAT54S) | SOT-23, three pins | 180° from the footprint default |

Remove any post-export script adjustment for these parts before regenerating
the CPL. For example, a C19829593 placed at 0° in KiCad exports at 270°.
Before ordering, check lead alignment and pin 1 or polarity in JLCPCB's
placement preview. [JLCPCB orientation guidance](https://jlcpcb.com/help/article/pcb-assembly-faqs-part-2)

An invalid sidecar (unknown key, non-numeric degrees, a `by_lcsc` key that is not
`Cnnnn`) fails the CPL step instead of silently shipping an uncorrected file.

`kct readiness` records the result in `output/readiness.json`:

- the sidecar is hashed as a readiness input, so editing, adding or deleting it
  makes the evidence stale and `--verify` fails;
- `cpl_rotations.applied` records the reference, matched key and offset for
  shifted placements and explicit catalog or project matches, including 0°.
  `cpl_rotations.sidecar_sha256` pins the project sidecar;
- `cpl_rotations.unreviewed` lists connectors, ICs, diodes, transistors, LEDs,
  switches, crystals/oscillators and jumpers (reference prefix
  `J`/`U`/`D`/`Q`/`LED`/`SW`/`Y`/`JP`/`X`) that **no** correction matched.
  This is a review list, not a blocker: check each one in the fab's placement
  preview before ordering. `kct export` prints the same list when it writes
  the CPL, and an empty-but-present sidecar is still recorded (path + hash);
- `--verify` re-derives every sidecar-corrected rotation from the board and the
  current sidecar and fails if the retained CPL disagrees. If the re-derivation
  itself cannot run (an unreadable or malformed bundle file, an unexpected
  error), `--verify` reports the skip instead of passing silently, and
  `readiness.json` records the reason under `cpl_rotations.rederivation_skipped`.

The hand-edited-CSV route is not an option: it breaks the manifest checksums.

## Retained recovery copies beside the board

Export sometimes rewrites the source board itself under a verified-bytes
check: refreshing the saved zone fills to match the generated DRC rules,
and the default silk legalisation pass. Each successful checked rewrite
keeps the displaced previous file as a recoverable copy in a sibling
directory named
`.<board>.kicad_pcb.recovery-<UTC timestamp>-<random>/original` (issue
#10056). If KiCad or another editor still had the previous file open and
saves into it after the export published, that save stays recoverable in
the retained copy rather than being lost.

If the exporter rolls back after the repaired board appeared at the live
filename, the recovery directory also contains `provisional`, the file an
editor could have opened during that interval. `withdrawn`, when present,
is the file found at the live name during rollback. It can be another hard
link to `provisional` or a separate file saved by an editor. Compare these
files with the live PCB before discarding a recovery. Close any editors
still writing those versions before cleanup.

Inspect the retained versions with any diff tool:

```bash
diff boards/04-stm32-devboard/04-stm32-devboard.kicad_pcb \
     boards/04-stm32-devboard/.04-stm32-devboard.kicad_pcb.recovery-*/original
```

One copy is kept per rewrite, so the directories accumulate until you
remove them:

```bash
kct clean boards/04-stm32-devboard/04-stm32-devboard.kicad_pro --force
```

`kct clean` lists completed recovery copies in its default dry run and
deletes them with `--force` while the board exists. If the project's PCB
is a symlink, export retains the previous version beside the source board.
Cleanup from the project finds that source's copies when the source is
inside the project directory tree. For a source outside the project,
run cleanup on the source project. The recovery API and cleanup preview
order copies by their marker timestamps, with names breaking ties.

The writer reserves
the `complete` marker before moving the board. The copy becomes eligible
for cleanup once the writer verifies the new board and removes its working
link, `repaired`. A completed rollback renames that link to `provisional`
after restoring the live name. Interrupted writes remain available for manual recovery.

## Next Steps

- **[Query API](query-api.md)** - Advanced filtering for design analysis
- **[Schematic Analysis](schematic-analysis.md)** - Deep dive into schematic parsing

## Offline submission preparation

For exact-byte, locally verified Gerber/BOM/CPL handoffs, see
[Offline assembly submission preparation](submission-preparation.md). This Python
API produces a deterministic plan and expected reference matching list without
supplier access, uploads, approval, or orders. A narrow `refresh_inventory`
increment can observe exact-ID stock through a caller-supplied official adapter,
preserving unknown-vs-zero evidence; the full milestone B provenance/freshness
contract remains blocked on #5033/#5034 (PRs #5115/#5090), so #5142 stays open.

## Inventory provenance for parts and BOMs

A catalog match establishes part identity; it does not prove current orderable
stock. Lookup/search JSON, BOM availability, suggestions and enrichment records
carry `inventory` metadata with `source` (`live`, `offline_catalog`, or
`unknown`), the original `observed_at`, snapshot revision/time when known,
local `read_at`, and `from_cache`. Cache reads and reinsertion preserve the
original observation time. Older cache rows without provenance remain unknown.

`stock_verified` requires a live observation no more than 24 hours old. This
is a screening freshness limit, not a supplier reservation or order guarantee;
refresh inventory before ordering. Offline snapshots never qualify as verified
stock, even when their recorded count exceeds the requested quantity.
Availability reports mark such matches unverified/unknown, while suggestions
and BOM enrichment can still use them to identify parts. Export enrichment
reports retain this provenance and warn when stock remains unverified.

The offline dataset does not guarantee a stock-observation timestamp. Missing
ages remain null; file modification time and download time are not substitutes.
Callers with trusted snapshot metadata can provide `snapshot_revision`,
`snapshot_at`, and `observed_at` to `JlcpartsCatalog` explicitly.
