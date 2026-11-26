Manufacturing package for voltage_divider_routed.kicad_pcb

Fabrication tier : jlcpcb
Mode             : assembly
Generated        : 2026-11-26T15:50:05Z

Assembly package: BOM and CPL are included.
CPL rotations combine the KiCad angle with a catalog-part or footprint table offset.
Project overrides in cpl_rotations.yaml take precedence. Before ordering, open the JLCPCB placement preview.
Check each part's leads against its pads and verify pin 1 or polarity against the board markings.

Contents
--------
  assembly-back.pdf
  assembly-front.pdf
  bom_jlcpcb.csv
  check-report.json
  cpl_jlcpcb.csv
  gerbers/gerbers.zip
  images/assembly.png
  images/layer_B_Cu.png
  images/layer_F_Cu.png
  images/pcb_back.png
  images/pcb_copper.png
  images/pcb_front.png
  images/schematic_voltage_divider.png
  kicad_project.zip
  manifest.json
  native-drc.json
  procurement-review.md
  report.md
  report.pdf
  schematic.pdf

Hand-solder / through-hole items
--------------------------------
  The following through-hole parts are INCLUDED in the CPL and BOM for fab assembly (flag for procurement review):
  J1, J2

Warning review (per rule)
-------------------------
  mask_to_copper: 8
  netclass_sidecar_mismatch: 3
  All assembly-affecting warning counts are zero.

Copper cleanup
--------------
  copper_cleanup: skipped (--skip-cleanup)

LCSC stock order
----------------
  order size: 5 board(s)
  no parts declared as held in operator inventory
  parts data sources: parts cache searched, match found; offline catalog searched, match found
  stock check OK: 2 LCSC part(s) have recorded stock for 5 board(s)

Assembly prep
-------------
  fiducials: applied
  board_info: applied

Accepted-risk waivers
---------------------
  no accepted-risk waivers

Sign-off
--------
  Fresh kct check and native KiCad DRC (saved and refilled copper): zero errors.
  Procurement identities reviewed against supplier listings; see procurement-review.md and source project.kct.
