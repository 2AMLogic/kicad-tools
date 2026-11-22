# Board 07 electrical review

The 48 MHz SDR SDRAM bus uses F.Cu and In2.Cu for data trunks, In3.Cu and
B.Cu for address and command trunks, and two reference planes. Control
signals share front copper with data near the packages. LDQM also runs
between the packages entirely on F.Cu. The spacing census therefore reads
the completed, hash-bound routing. The authored PCB contains partial copper.

ST recommends separate data and control layers or 5 mm separation on a
shared layer, plus three trace widths around the clock. On the reviewed
six-layer board, 99 distinct non-clock data/control net pairs have front
traces closer than 5 mm. The smallest trace-edge gap is 0.320 mm between
DQ4 and A11. Including via annuli, the minimum is 0.209844 mm between the
DQ7 track and an A3 via. These are deviations from the application note’s
separation recommendation. [ST AN4488 §8.4.2](https://www.st.com/resource/en/application_note/an4488-getting-started-with-stm32f4xxxx-mcu-hardware-development-stmicroelectronics.pdf)

The longest individual parallel overlap is 4.260 mm, between DQ6 and SDNWE
at a 3.820 mm gap. The closest parallel overlap is 2.238 mm at the 0.320 mm
DQ4/A11 gap. The independent clock screen checks signal tracks and vias on
all four signal layers. Its measured minimum is 0.560 mm against 0.540 mm.
TSOP-II pad interactions have their own reported geometry.
[Front-layer census](package-escape-spacing.json) · Clock screen (../reviewed-routing/evidence/clock-spacing.json, not included in this bundle)

## Coupling estimates

The model uses the reviewed cross section, including its 0.1164 mm outer
dielectric, and the measured length and gap of each parallel front-layer
trace span. The highest far-end estimate is on DQ4/A11 at all three
assumed rise times. The pair column adds the magnitudes of that pair’s
individual modeled contributions. The earlier release is included for
comparison using the same model and units.

| Assumed rise time | Largest individual span, FEXT | DQ4/A11 sum, FEXT | Earlier DQ4/A11 sum |
| --- | ---: | ---: | ---: |
| 1 ns | 0.140% | 0.157% | 0.153% |
| 0.1 ns | 1.400% | 1.570% | 1.534% |
| 0.05 ns | 2.800% | 3.140% | 3.068% |

At the fastest assumption, the modeled pair sum warrants bench review.
The estimate covers one aggressor on parallel front traces. Via coupling,
bends, package parasitics, simultaneous switching and driver/receiver
corners need separate characterization. The application-note deviations
and these rise-time assumptions remain part of the bench-demo scope.
Hardware operation and IBIS corner qualification are pending.
[Coupling estimates](escape-coupling-scenarios.json) · [Receiver and via load review](load-review.md)

Reproduce both JSON reports from the reviewed PCB with the command below.
To compare a previous PCB, pass its path with `--pcb` and choose a separate
`--output-dir`. The earlier routed PCB used for the table has SHA-256
`47e077d26764b4abb4ba9595ddcea4e36817829186f497741b271906bc132a0b`.

```sh
uv run python boards/07-matchgroup-test/real_design/engineering/measure_escapes.py
```

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

The production checkout adopted both physical boards, their bound manifests
and the matching fiducial-aware build recipes (the #8603 adoption). The
recipe copies in `output/manufacturing/design-source.zip` still predate the
adoption; #8604 re-issues the manufacturing evidence and independently
rechecks the routing.
