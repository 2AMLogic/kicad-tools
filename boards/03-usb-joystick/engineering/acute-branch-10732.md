# Board 03 acute-branch measurement (Issue #10732)

Produced by `engineering/acute_branch_10732.py` (SHA-256 `8c7ad142450a9216f7615177a44140f5cd4f1035b8ed1e88f285f411d25b8056`) on 2026-11-28T13:43:40Z in 858 s, on KiCad 10.0.6 (`kicad/kicad@sha256:18693567392b80da435f9fa952ce3a3e534c66eb5a6033f5b9c80aa3b19dd3ec`), from the published routed PCB `output/usb_joystick_routed.kicad_pcb` (SHA-256 `4d3ad6dad097f7722c0587649156ad504bd158510d712461ccd8c6a35b8cde7d`) at `ed3bad72a1fc486592603ff7b82e6cc6d4a9d387`. The machine-readable record is `acute-branch-10732.json` next to this note.

This is the public stand-in, not the coordinated reissue input: the tool-generated rebuild #8900 measures from is not on `main`. The run is the readiness copper cleanup's order on a scratch copy -- `dedupe`, `kct fix-drc --only redundant-copper` (#10667), then `kct fix-drc --only acute-branch` (this issue) -- with each stage measured on its saved bytes.

## Stages

| Stage | `sharp_trace_corner` sites | PCB SHA-256 | `kct check` errors / warnings |
| --- | ---: | --- | --- |
| `input` | 25 | `4d3ad6dad097f772` | 71 / 386 |
| `dedupe` | 25 | `ba43fb2d44c07e64` | not measured |
| `redundant-copper` | 22 | `739600ac1f4959fb` | 49 / 146 |
| `acute-branch` | 3 | `d981d3bf03331319` | 48 / 124 |

## Acute-branch pass

`kct fix-drc --only acute-branch --mfr jlcpcb-tier1` planned 23 tap findings and kept 20 edits, each re-checked on the saved, refilled board with `kct check`'s rule set and native DRC (exit 2: the residuals below).

| Residual site | Reason |
| --- | --- |
| `BTN3` B.Cu (33.200, 34.800), 45 deg | `new_redundant_copper_staircase` |
| `JOY_X` F.Cu (36.800, 27.200), 45 deg | `new_redundant_copper_staircase` |
| `JOY_Y` B.Cu (22.000, 29.600), 45 deg | `new_redundant_copper_staircase` |

Each residual is a tap whose every right-angle entry turns its own feeding tracks, or the carrier it lands on, into a chain the `redundant_copper` rule then reports as a staircase; the repair refuses that trade rather than swapping one warning for another.

## Saved and refilled copper

| Measurement | Input | After the cleanup |
| --- | --- | --- |
| Native DRC, saved copper (pinned KiCad 10.0.6) | 0 violations / 0 opens | 0 violations / 0 opens |
| Native DRC, independently refilled copy | 0 violations / 0 opens | 0 violations / 0 opens |
| KiCad physical pad groups | 27 groups / 142 pads | 27 groups / 142 pads |
| Copper and label LVS | label clean, copper clean, 129 bound pads | label clean, copper clean, 129 bound pads |
| USB D+ / D- copper length (mm) | 15.790878 / 15.790878 | 15.790878 / 15.790878 |
| USB common coupled run (mm) | 4.070927 | 4.070927 |
| USB D+ / D- skew (mm) | 0.000000 | 0.000000 |
| `kct check` (jlcpcb-tier1) errors / warnings | 71 / 386 | 48 / 124 |
| Mask assessment | complete, 465 `mask_to_copper*` findings | complete, 368 `mask_to_copper*` findings |
| Strict manufacturing check (`check_manufacturing.py`) | 0 errors / 307 warnings | 0 errors / 51 warnings |

`routing_plan.usb_geometry` accepted both boards (at most 2.5 mm of D+ / D- skew). The strict check's ERC leg reads `FAILED` (0 error(s), 1 warning(s) (strict)) on the input and `FAILED` (0 error(s), 1 warning(s) (strict)) on the result; the record keeps its detail (a warning about the host's KiCad library tables, not a schematic defect). Its manifest leg is `NOT RUN` because the scratch copy carries no manufacturing bundle: the package and release stay with #8900.

## Rule-set census

| Rule | Input | After redundant-copper | After acute-branch |
| --- | ---: | ---: | ---: |
| `bypass_distance_unassociated` | 7 | 7 | 7 |
| `diffpair_routing_continuity` | 1 | 1 | 1 |
| `mask_to_copper` | 464 | 368 | 367 |
| `mask_to_copper_openings` | 1 | 1 | 1 |
| `netclass_sidecar_mismatch` | 9 | 9 | 9 |
| `pad_grid` | 2 | 2 | 2 |
| `plane_split` | 2 | 2 | 2 |
| `pour_slot` | 25 | 16 | 14 |
| `redundant_copper_covered` | 52 | 0 | 0 |
| `redundant_copper_duplicate` | 134 | 0 | 0 |
| `redundant_copper_staircase` | 37 | 1 | 0 |
| `sharp_trace_corner` | 25 | 22 | 3 |
| `silk_hole_overlap` | 6 | 6 | 6 |
| `silk_pad_clearance` | 21 | 21 | 21 |

The acute-branch pass moved `mask_to_copper` 368 -> 367, `pour_slot` 16 -> 14, `redundant_copper_staircase` 1 -> 0, `sharp_trace_corner` 22 -> 3; every other count is the redundant-copper stage's.
