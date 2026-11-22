# Board07 electrical review evidence

The physical source is a six-layer SDR SDRAM bench demonstration. A native
manufacturing DRC pass alone does not approve its electrical interface.

`package-escape-spacing.json` measures the authored front-side bus escape paths
used by the physical source. The longest is 5.1199 mm; the two SDCLK escapes are
3.3279 and 4.6491 mm. No clock-to-other-bus escape pair is closer than 0.54 mm
edge-to-edge. There are 64 data/control escape pairs closer than 5 mm. These
are measured deviations from the same-layer separation recommendation, not
an automatic footprint exemption or a transient signal-integrity pass.

[ST AN4488 §8.4.2](https://www.st.com/resource/en/application_note/an4488-getting-started-with-stm32f4xxxx-mcu-hardware-development-stmicroelectronics.pdf)
recommends separate data/control routing layers, or 5 mm separation on a shared
layer, and at least three trace widths around the clock. The note does not
explicitly establish a package-escape exception. The source uses separate
layers for the long groups and conservatively interprets clock spacing as
0.54 mm edge-to-edge for 0.18 mm traces. Final routing must separately measure
new copper; this source-only report cannot cover later routing changes.

The source's reserved reference planes, short package escapes and local power
connections reduce the coupled run length. The short data/control escape deviation is accepted for this bench-demo
manufacturing release based on its measured extent, reserved reference planes,
separate long-run routing layers and the documented load/clock screens.
This is a bounded engineering judgment; it does not establish literal blanket
compliance with the application note. No IBIS transient/corner qualification or
hardware operation has been claimed.

The independent receiver/via load review is in `load-review.md`. Its 1 pF per
complete through-via allowance is an engineering reserve over an approximate
nominal model. DQ receiver load needs an explicit reserve because STM32 lists
5 pF typical without a maximum. The validator reports that assumption; it
must not describe it as a guaranteed MCU capacitance bound.

Reproduce the source spacing census with:

```sh
uv run python boards/07-matchgroup-test/real_design/engineering/measure_escapes.py
```

`clock_spacing.py PCB OUTPUT.json` separately measures the final candidate's
clock tracks and complete through vias against all other signal copper, in both
directions. It also reports package pad interactions separately. The native
TSOP-II's adjacent pad geometry cannot establish three-width spacing between
pads; the clock-to-trace requirement must not be silently generalized into an
impossible package requirement. Final traces and vias remain subject to the
explicit 0.54 mm screen.

`escape-coupling-scenarios.json` records illustrative existing-model results for
one aggressor, the measured 0.320000 mm minimum gap and the full 5.1199 mm escape length treated as
parallel. Rise times of 1, 0.1 and 0.05 ns are assumptions, not measured or
manufacturer-guaranteed minima. These scenarios give context for the short
coupled length; their automatic `acceptable` labels are not a board sign-off.
Multiple aggressors, package parasitics and receiver/driver corners remain
outside that calculation.

The issue #5044 angle repair subdivides fourteen authored clock/neighbor/data escape chords into four
45-degree doglegs to retain the clock-spacing screen and the original 0.320 mm minimum data/control gap. These measurements and
the illustrative coupling scenarios were rerun on that geometry; placement,
trace widths, layer assignments and through-via construction are unchanged.

## Reproduce the six-layer post-optimization candidate

Run the Board 07 repair from the repository root with a new output directory.
The recipe builds the reviewed starting layout, optimizes against its generated
KiCad project and custom rules with the JLCPCB six-layer profile, applies the
measured paths in `retune-8603.toml`, and runs the release validator.

```sh
KCT_KICAD_DOCKER=1 uv run python boards/07-matchgroup-test/real_design/engineering/retune_8603.py /tmp/board07-retune
```

`retuned/candidate.kicad_pcb` is the saved layout. Its matching validator
inputs are in `retuned/source/`. The recipe checks the SHA-256 of both the
optimized layout and the generated authored PCB before applying the path data.
It recovers authored tracks by copper coverage. Fourteen changed front-side
escapes connect the same physical pad and original through-via at the same
length. The tuning adds series length on the bus tracks and moves six clock
corridor junctions on B.Cu. All coordinates in the record are absolute
KiCad millimeters.

On the November 22 candidate, the shortest pad-to-pad paths including via
travel gave group spreads of 0.050 mm (byte 0), 4.388 mm (byte 1), 4.237 mm
(address), and 3.770 mm (command). The largest clock-relative difference was
9.370 mm. The minimum clock-to-signal track/via edge gap was 0.567 mm. Pinned
KiCad 10.0.6 reported zero DRC violations and opens, and copper LVS bound 216
pads. The full validator returned 2 for the remaining design check, including
nine mask errors and 193 warnings. The design check still reports 16 fragment,
22 covered-track, 20 staircase and 11 detour warnings. See
`evidence/validation.json` and `evidence/check.json` for the current result.

## Reproduce the warning-free six-layer design

Run the complete Board 07 repair from a Git checkout with a fresh output
directory. It retrieves the original design inputs from commit
`47b710fac93bc6c1f93df52a930ec6dc336f7601`, repeats the optimizer and
retuning, then applies the measured copper in `cleanup-8603.json`.

```sh
KCT_KICAD_DOCKER=1 uv run python boards/07-matchgroup-test/real_design/engineering/cleanup_8603.py /tmp/board07-clean
```

Sixteen runs of sub-0.1 mm tracks are simplified between their original
terminals, including the fragmented UART and clock escapes. The remaining
edits remove 22 covered tracks, straighten redundant bends and fix mask-side
pad entries. Seven SDRAM nets now use 21 additional plated through-vias to
move between their assigned pair of signal layers. Every new via joins
same-net track endpoints on both layers. The shorter paths and redistributed
series tuning meet the physical pad-to-pad timing requirements. The two
assembly sides each carry three stock fiducials. They have unnumbered,
unconnected pads and are excluded from both the BOM and placement output.

| Pinned KiCad 10.0.6 measurement | Retuned input | Cleaned design |
| --- | ---: | ---: |
| Design errors / warnings | 9 / 193 | 0 / 0 |
| Redundant fragments / covered / staircases / detours | 16 / 22 / 20 / 11 | 0 / 0 / 0 / 0 |
| Byte 0 / byte 1 spreads (mm) | 0.050 / 4.388 | 1.952 / 4.605 |
| Address / command spreads (mm) | 4.237 / 3.770 | 3.648 / 4.027 |
| Maximum clock-relative difference (mm) | 9.370 | 9.881 |
| Minimum non-pad clock edge gap (mm) | 0.567 | 0.560 |
| Copper LVS bound pads | 216 | 216 |

`cleaned/candidate.kicad_pcb` and `cleaned/source/sdram_demo.kicad_pcb`
are the repaired physical pair. Their SHA-256 digests are
`45943c244519945ee5d51c4b58a28a901cf67b092129c64dcd2ea90a11d2bc84` and
`ebff1f52709d4cb1a56d6b1558ca1051c223b746c37825ecd93c38ea81bc7575`.
The unchanged validator writes the measurements to `evidence/`. Its native
DRC checks before and after saving the refill, native ERC, and both LVS
checks passed with zero violations, shorts or opens. All 1,738 routed
segments and 340 authored segments pass the release angle census.

After validation passes, `proposed-design/` holds the two candidate manifests
and their bound files. The staging step applies
[`assembly-fiducials.patch`](assembly-fiducials.patch) to its copies of
`build.py` and `build_source.py` if those copies do not already contain it.
This lets the release builder verify the six
board-only marks while it independently regenerates the 43 electrical
components and 216 bound pads. It runs the ordinary validator on the complete
pair and writes the result to `release/`.

The published recipes still match the versions in
`output/manufacturing/design-source.zip`. Issue #8604 owns the independent
recheck, adoption of the staged recipe and physical hashes, and gated refresh
of the manufacturing bundle.
