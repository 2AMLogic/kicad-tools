# Reviewed physical routing

`sdram_demo.kicad_pcb` is the explicit routing/length-tuning artifact for the
43-part, 216-binding real SDRAM circuit. `manifest.json` binds its SHA-256 and
the exact authored source manifest. It includes the completed copper and
operator silkscreen; it is not a generic autorouter cache entry.

Run `../build.py OUTPUT` through `uv run python` to regenerate the independent
electrical source and repeat every design gate. A new logical circuit, physical
package placement or source-rule manifest requires a new reviewed routing
artifact and fresh validation; updating a hash alone is not a design review.

The build intentionally distinguishes design validation from manufacturing
export. The final order package must be generated afterward and its manifest
must pass before manufacturing readiness is claimed. Hardware has not yet been
tested.

## Class-clearance repair (Issue #6396)

The reviewed copper was first validated against the 0.15 mm native floor only.
Once #6390 declared the board's own 0.175 mm `AUX`/`SDRAM_*` classes, 42
track-to-via/pad gaps fell 0.2–25 µm short. `../repair_class_clearance_6396.py`
is the complete record of the fix. It moves 24 vias by 10–47 µm, re-attaches
their stubs at 0/45/90°, and moves one GND escape by 7 µm, then re-pins both
manifests. No class value changed. Run it with `--verify-from-git 810f833f` to
reproduce both committed PCBs byte for byte from the pre-repair artifacts.

## Evidence refresh (Issue #6404)

`build.py` fills the planes under the published router net classes, the same
`.kicad_pro`/`.kicad_dru` that sit beside the routed board. Before this, the
fills used the fab floor alone, and the saved fills failed native DRC without
`--refill-zones`. The files under `evidence/` come from that build of the
repaired copper. `scripts/boards/refresh_bundles_6404.py` regenerates them,
the routed board's fills and the manufacturing bundle together.

## Same-net copper-gap repair (Issue #9091)

Once #8938 graded same-net `physical_copper_gap` findings as errors, the
reviewed copper had 25 same-net slits: nine vias 0.07–0.12 mm from their own
pads, and narrow same-net wedges at U2.50 (DQ13), U2.39 (UDQM) and U2.53
(DQ15). `../repair_copper_gap_9091.py` is the complete record of the fix.
- It moves the nine vias 53–87 µm clear of their pads.
- It replaces a +5V staircase beside C26 with a plain T, which frees one of
  those vias.
- At DQ13 and UDQM, it removes the loops. Each trunk ran through its own pad
  and doubled back along the pad's escape, so the loop counted about 10 mm of
  length the signal never travelled. The script restores that length with
  `SerpentineGenerator` trombones on the trunks.
- It turns DQ15's shallow staircase join into a right angle and puts the
  1.5 mm it removes back into DQ15's existing meander.

Every `SDRAM_BYTE1` length is unchanged to 1 µm. DQ7 gains 0.145 mm. Run the
script with `--verify-from-git 89c2ee058` to reproduce both PCBs byte for byte.

The files under `evidence/` now describe the adopted #8603 copper (see the
section below); `refill-stability.json` and the published `../../output/`
release still describe the copper as it was before the November 2026 prune
(95c4c7eaf), until #8604's evidence refresh re-issues them.

## SDRAM_BYTE0 same-net loops (Issue #9118)

DQ7 (U2.13) and LDQM (U1.141) had the byte-1 defect from #9091: each trunk ran
through its own pad and on past it, and the authored escape doubled back along
the trunk. That is 6–7 mm of copper per net the signal never travels, but
`validate.py` sums it as matched length. Measured as the shortest path through
the copper, the real byte-0 spread was 7.51 mm against `group_skew_mm` 5.0 as
filed (the board-local model #9118 was filed with; removed by #9176). The
shared contact model, `TraceLengthAnalyzer.measure_net_path`, reads 8.16 mm on
the same stripped copper. `../repair_byte0_loops_9118.py` is the complete
record of the fix.
- It removes the loop copper, so each trunk ends inside its own pad.
- It restores the length as real series length: two `SerpentineGenerator`
  trombones, 5.93 mm on DQ7 and 6.98 mm on LDQM, on free F.Cu.
- It changes no tolerance and not `authored-source`, which never held the loops
  in this form. Only this directory's manifest is re-pinned.

Physical byte-0 spread is now 1.18 mm through `TraceLengthAnalyzer
.measure_net_path` (canonical since #9176: DQ5 62.20 to DQ2 63.37 mm; the
removed board-local model read 0.92 mm). Run the script with
`--verify-from-git c694302cd` to reproduce the PCB byte for byte, and print
the per-net lengths by measuring each with `measure_net_path` the way
`tests/test_board07_byte0_physical_spread.py` pins them.

`build.py` still reaches the same end state as after #9091: 0 native DRC
violations, 0 unconnected items, clean label and copper LVS, and only its full
`kct check` gate failing on `mask_to_copper` (#8858). `../../output/` is
therefore not re-issued here.

## Six-layer warning-free re-issue (Issue #8603)

After the November 2026 prune the reviewed copper still failed the full
`kct check` gate with 210 warnings (163 `redundant_copper_fragments`, 30
covered tracks, 14 detours and 3 silk findings), all traced to 0.05 mm
optimizer staircases that `kct pcb prune` cannot collapse. The #8603
operator ruling (option 2, 2026-11-10) authorized re-optimizing the
reviewed copper instead of accepting them.

`../engineering/retune_8603.py` optimizes the release build against the
board's own generated `.kicad_pro`/`.kicad_dru` (JLCPCB six-layer profile),
retunes the four match groups and re-derives the 14 pad-to-via authored
escapes. `../engineering/cleanup_8603.py` then simplifies the remaining
sub-0.1 mm fragment runs between their original terminals, removes covered
tracks, adds 21 same-net layer-handoff vias and stages the assembly
artwork. Both scripts refuse any input whose SHA-256 differs from the
recorded digest, and the recipe reproduces both committed PCBs byte for
byte from Git inputs:

```sh
KCT_KICAD_DOCKER=1 uv run python boards/07-matchgroup-test/real_design/engineering/cleanup_8603.py OUT
```

`sdram_demo.kicad_pcb` here is
`45943c244519945ee5d51c4b58a28a901cf67b092129c64dcd2ea90a11d2bc84` and the
authored source is
`ebff1f52709d4cb1a56d6b1558ca1051c223b746c37825ecd93c38ea81bc7575`
(`reviewed_on` 2026-11-22; the authored manifest also declares the six
board-only fiducial marks that `../build.py` and `../build_source.py` now
verify). The full gate this build repeats reports 0 errors and 0 warnings:
every `redundant_copper_*` class is 0, native DRC and ERC report no
violations, both LVS checks bind all 216 pads, the byte-0 / byte-1 /
address / command spreads are 1.952 / 4.605 / 3.648 / 4.027 mm against the
5.0 mm limit, the largest clock-relative difference is 9.881 mm and the
smallest non-pad clock gap is 0.560 mm against the 0.54 mm screen.

The files under `evidence/` come from that passing build, except
`refill-stability.json`, which #8604's manufacturing evidence refresh
re-issues together with `../../output/` and the independent routing
recheck. The redundant-copper census above is the release gate's: it runs
on the validated artifact with the board's generated `.kicad_pro` net
classes beside it. Checking the raw committed file without that project
context instead flags five candidate staircases (BOOT0, DQ1 and three
more) whose one-bend shortcuts would violate the 0.175 mm class
clearances, so the gate context is the authoritative one.
