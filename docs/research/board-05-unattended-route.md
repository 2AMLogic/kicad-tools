# Board 05 routed from scratch, unattended (Issue #7834)

Measured 2026-10-22 on `origin/main` @ `2de924703` (after #7835 turned
`--reserve-pour-only-passives` on by default and #7838 chained the
`--complete` lattice pass into `kct route`). Part of epic #4410.

## Why this note exists

Board 05's shipping recipe (`design.py` -> `redesign/build.py`) replays
reviewed copper from `redesign/routing.json`. The `board-05-routing-regression`
job in `ci.yml` runs that recipe, so its "0 blocking" says nothing about the
router. `scripts/ci/board_05_unattended.py` is the from-scratch proof. It
writes the unrouted board with `redesign/hardware.generate` (the step before
`build.py` applies `routing.json`), routes it with `kct route`, and gates the
result. `.github/workflows/nightly-board-05-unattended.yml` runs it daily and
on dispatch, one matrix job per seed (42, 7, 1337).

Route flags (the #6508 protocol from the #7831 evidence; `--reserve-pour-only-passives`
is the default now, so it is not passed):

```
kct route <unrouted> -o <routed> --timeout 4500 --no-cache \
  --no-auto-layers --deterministic-budget --per-net-iterations 1000000 \
  --no-placement-delta-feedback --deterministic-rescue \
  --no-fallback-bidirectional --seed <S>
```

Gates, all required and none allowlisted: route exit 0 and a final (not
deadline) artifact; the stranded-pour oracle answered (#7836); every net
complete; kicad-cli `pcb drc --refill-zones --all-track-errors` 0
error-severity violations and 0 unconnected items; kicad-cli `sch erc` 0
violations; `kct check --mfr jlcpcb` 0 errors; copper and label LVS clean.

## Results

Host: macOS arm64, C++ backend built. kicad-cli was the pinned 10.0.6 image
(`KCT_KICAD_DOCKER=1`), with the board's `fp-lib-table` and `board05_revB.pretty`
beside the routed board. The seeds ran one after another.

| Seed | Nets | kicad-cli DRC err | Unconnected | ERC | kct check err | LVS (copper / label) | Vias | Wall s | Load avg (start) | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 42 | 37/37 | 0 | 0 | 0 | 0 | clean / clean | 124 | 242.2 | 2.56 | PASS |
| 7 | 37/37 | 0 | 0 | 0 | 0 | clean / clean | 124 | 244.1 | 4.28 | PASS |
| 1337 | 37/37 | 0 | 0 | 0 | 0 | clean / clean | 124 | 242.1 | 6.00 | PASS |

**The outcome does not depend on the seed.** All three routed boards are
byte-identical (sha1 `b80c221d49d6d5d9274e603db66c731ef786a0ef`). They are also
identical to the #7838 Judge's harness run at seed 42, whose input went through
`expand_route_experiment.py`'s re-serialisation. The main pass reaches 32/33
signal nets and leaves VCP open; the chained `--complete` pass closes it.

Linux (the pinned `kicad/kicad` image, amd64): see "CI / Linux" below.

### Re-measured 2026-11-20 (Issue #9424)

Same flags and host, pinned kicad-cli 10.0.6 (`KCT_KICAD_DOCKER=1`), the
gate's #8532 fiducial + board-ID prep, three seeds run in parallel (load
average 10-20). No `[deterministic-budget] per-net wall clock bound` or
`stage deadline fired` line appeared in any route log. All three seeds were
byte-identical again in both columns, so the figures below hold for each of
42, 7 and 1337.

| | `origin/main` @ `e56ea23dd` | + #9424 via union depth |
|---|---|---|
| `kct route` exit / nets | 0 / 37 of 37 | 0 / 37 of 37 |
| copper / label LVS | clean / clean | clean / clean |
| kicad-cli DRC err / warn / unconnected, ERC | 0 / 0 / 0, 0 | 0 / 0 / 0, 0 |
| `kct check` errors (`--skip mask_to_copper`) | 8 `physical_copper_gap` | 1 `physical_copper_gap` |
| `kct check` warnings | 74 (48 `pour_slot`, 13 `silk_hole_overlap`, 13 `redundant_copper_*`) | 85 (62 `pour_slot`, 9 `silk_hole_overlap`, 14 `redundant_copper_*`) |
| `mask_to_copper` err / warn / info (pass on) | 33 / 18 / 224 | 22 / 19 / 218 |
| `kct check` wall time, pass off / on | 5.2 s / 24.4 s | 5.2 s / 24.0 s |
| vias / route wall time | 117 / 315 s | 115 / 283 s |

The route itself is green: VCP, the net #9517 and #9734 left open, connects. Five of
the eight errors were vias the C++ A* seated beside their own SMD pad as a
"union": three overlapped only the bounding box's square corner of a
`roundrect` pad (air beside the rounded copper), and one overlapped a flat face
by 5 um, too shallow for the checker's shoulder exemption. #9424 requires the
union to reach `same_net_pad_union_depth_mm` into the box's inscribed stadium
(0.0218 mm for the 0.6 mm via at the 0.15 mm edge). The one remaining error is
a different class: a trace that leaves its own via in a 0.46 mm stub and then
turns 90 degrees back past the via (RESET, B.Cu). The warning count rose
because the moved vias re-shaped neighbouring routes (`pour_slot` 48 -> 62). The gate's
0-error / 0-warning bar is still not met, and `--skip mask_to_copper` stays.

#### Re-measured after the rect-corner fix (Doctor, PR #9845)

The first #9424 rule measured every pad's union against the stadium inscribed
in its box. That also refused real overlap with a `rect` pad's square corner,
and on board 06 it re-routed USB3_TX1- around U2.C2 and broke the +3V3 pour
(the CI post-legalize gate failed). The union now uses the pad's true corner
radius when the shape is known. Same flags, pinned kicad-cli 10.0.6, seeds run
one at a time (an earlier parallel attempt had kicad-cli SIGKILLed, exit 137,
and is discarded). The routed boards of seeds 42, 7 and 1337 are
byte-identical:

| | + #9424, stadium only | + #9424, true corner radius |
|---|---|---|
| `kct route` exit / nets | 0 / 37 of 37 | 0 / 37 of 37 |
| copper / label LVS | clean / clean | clean / clean |
| kicad-cli DRC err / warn / unconnected, ERC | 0 / 0 / 0, 0 | 0 / 0 / 0, 0 |
| `kct check` errors (`--skip mask_to_copper`) | 1 `physical_copper_gap` (RESET, B.Cu) | 1 `physical_copper_gap` (nTRIP, F.Cu) |
| `kct check` warnings | 85 | 80 (61 `pour_slot`, 9 `_covered`, 4 `_detour`, 4 `_staircase`, 2 `silk_hole_overlap`) |
| vias / route wall time | 115 / 283 s | 115 / 213-228 s |

The remaining error moved. It is now a same-net trace-to-trace slit: two
parallel nTRIP traces 0.15 mm wide with a 0.021 mm gap at (146.05, 101.71),
graded on the 0.1016 mm trace edge. It is not a via seat. The #9844 RESET
stub-turnback does not occur on this route. `mask_to_copper` was not
re-measured.

### Re-measured 2026-11-22 (Issue #9424: errors 0, mask face re-measured)

`origin/main` @ `dc59d8c78` (the morning's merges include the #9846 stitcher
stub band fix #9857 and the #9799 silk legalisation #9865), pinned kicad-cli
10.0.6 via `KCT_KICAD_DOCKER=1`, AWS x86_64 host (load average 12-40 during
the runs), C++ backend built. All three seeds ran through the gate script one
at a time.

The boards of seeds 42, 7 and 1337 are byte-identical again
(sha1 `64411406e1732fd285f38034c9cdc0fb8c183d36`), so the cross-seed property
survived the #9875/#9854/#9857 routing changes. No
`[deterministic-budget] per-net wall clock bound` or `stage deadline fired`
line appeared in any route log; `hit_timeout` and `rescue_wall_clock` are
false everywhere.

| | seed 42 | seed 7 | seed 1337 |
|---|---|---|---|
| `kct route` exit / nets | 0 / 37 of 37 | 0 / 37 of 37 | 0 / 37 of 37 |
| kicad-cli DRC err / warn / unconnected, ERC | 0 / 0 / 0, 0 | 0 / 0 / 0, 0 | 0 / 0 / 0, 0 |
| copper / label LVS | clean / clean | clean / clean | clean / clean |
| `kct check --skip mask_to_copper` | **0 err** / 77 warn | 0 err / 77 warn | 0 err / 77 warn |
| route wall s / vias | 630.3 / 124 | 577.0 / 124 | 401.0 / 124 |

**Non-mask errors reached 0.** Two changes moved since the 2026-11-22
curation pass measured 1 error:

* The curated `nTRIP` F.Cu trace-to-trace slit no longer occurs on this
  route (the #9875 via-turnback retry reshaped it).
* At `8564ae74c` (pre-#9857) the board carried 3 same-net
  `physical_copper_gap` errors instead: the oracle-completion stitch cascade
  seated U2's GND pin 14 with a 0.2 mm two-leg stub down the 0.375 mm
  corridor between the EP (0.06 mm slit) and pin 14 (0.115 mm slit) -- no
  0.2 mm trace crosses that corridor legally. The #9846 leg-band refusal
  (`stitch_cmd.py`, PR #9857) seats the weld legally; the errors are gone.

The 77 warnings: 51 `pour_slot` (the structural congestion population,
unchanged), 15 `redundant_copper_*`, 9 `silk_hole_overlap` (was 14 at
`8564ae74c`, before the 2026-11-22 merges that include the #9799 silk
legalisation), 1 `sharp_trace_corner` (the unrepairable acute fold-back),
1 `plane_split`. The 0-warning bar still fails; the pour-slot question went
to the operator with the mask question below.

**`mask_to_copper` re-measured on the pinned toolchain** (pass enabled on the
graded board, outside the gate): **25 errors / 20 warnings / 221 info** --
the same population class as the PR #9845-era 22/19/218 face. 14 of the 25
errors are the U2 EP mask opening (`aaf9bcde`, the 3.1 x 5.18 `F.Mask` pad)
exposing GND copper (thermal vias and stitch copper beside it) in the
#9420 `within_own_footprint` shape whose owner does not resolve to a
footprint ref; the rest are `same-net pass-through` (#8914), via-ring and
`different-net` openings. The classes the issue scopes out (#9260/#9261 rule
changes, #8839 threshold tables) own this population; repair to ~0 is not
achievable inside this issue. Per the Decision flag the policy question was
filed as an operator decision on #9424 (allowances vs keep-skip), and
`--skip mask_to_copper` stays at `scripts/ci/board_05_unattended.py` until
that ruling lands.

**Grading trap (measured, now guarded).** Grading the mask pass on a host
whose kicad-cli is not 10.0.5/10.0.6 (this host has 10.0.8) silently
degrades: zero findings plus one info `mask_to_copper_incomplete` marker
(#8914), which the error/warning counters read as a clean board -- a
standalone `kct check` on this host scored the byte-identical board 0/77
where the pinned toolchain scores 25/97. The gate now fails a report
carrying that marker ("not checked" is not "clean"), so a future skip-drop
cannot turn an unqualified toolchain into a false green. The mask pass costs
+29.3 s on the pinned toolchain (37.6 s vs 8.3 s for the skipped run), the
AC4 number to accept or reduce when the skip drops.

### Negative run

The gate was checked against a known-bad board. The seed-42 board had all 20
SPEED segments and vias deleted, and `--grade-only` was run on it. The script
exited 1 with `nets 36/37 complete; kicad-cli unconnected items: 2; kct check
--mfr jlcpcb errors: 1 (connectivity); copper LVS dirty`. Run against the
host's Homebrew kicad-cli 10.0.7 without the Docker toolchain, it exits 2
(`REFUSED (KiCad environment, Issue #6962)`) and grades nothing.

## Which outcomes depend on load

`--deterministic-budget` and `--deterministic-rescue` make the A* loop and the
relief-rescue sub-searches iteration-bounded. Two wall clocks are still live in
this route, and the route log reports both:

* **Post-negotiation sweep**: `60.0s whole-sweep / 10.0s per-net wall clock`
  (`sweep_wall_clock: true`, #4159 / #6329).
* **Chained `--complete` lattice pass**: `per-link budget 60s` (netset deadline
  = 60 s x link count). It closes VCP, the one net the main pass leaves open.

Neither bound was reached at a load average of 2.6-6.0 on this host, so the
boards came out byte-identical. A much slower runner could hit either one. If
it hit the completion budget, VCP would stay open and the gate would fail. It
would fail loudly as `nets 36/37`, with kicad-cli unconnected > 0, never as a
pass. So a red nightly with VCP open and no router change is a load signal
first. Read the uploaded `route.log` before assuming a regression.

## Gated: warnings

The nightly gates warnings too (#8073, finishing #7848 AC 2), matching the
no-warning gate in the shipping `build.py` (#7298): `kct check` warnings and
kicad-cli DRC warnings must both be 0, and a count that did not run fails like
the error counters.

This was not always so. The unrouted board from `hardware.generate` used to
carry 136 `silkscreen_line_width` and 1 `silk_overlap` (`kct check`) plus 4
`silk_over_copper` and 1 `silk_overlap` (kicad-cli), because the DFM silk
corrections (0.15 mm strokes, the R4/R11/RV1 `REFERENCE_OFFSETS`) lived only in
`redesign/routing.py:apply_routing`. #7848 moved them into
`hardware.generate`, and the nightly of 2026-10-28 (run 40705286766) reported
0 warnings in both engines on seeds 42, 7 and 1337.

## CI / Linux

Host and CI diverge on board 05 (#3822), so seed 42 was also run on Linux.
The job's steps were replayed inside the pinned `kicad/kicad@sha256:186935...`
image (amd64, emulated on the arm64 host, `--memory 7g`): `uv sync --frozen`,
`kct build-native` (`router_cpp.cpython-312-x86_64-linux-gnu.so`), then
`board_05_unattended.py --seeds 42`, with kicad-cli native to the image.

| Seed | Nets | kicad-cli DRC err | Unconnected | ERC | kct check err | LVS | Vias | Wall s | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 42 | 37/37 | 0 | 0 | 0 | 0 | clean / clean | 124 | 431.1 | PASS |

The Linux board passes every gate, with the same main-pass 32/33 and the same
chained completion of VCP. It is not byte-identical to the macOS board (sha1
`50fa6559...` vs `b80c221d...`), which is the #3822 host-vs-Linux divergence,
but every graded number matches. Emulation made this run about 1.8x slower than
the host, and it still did not reach either live wall clock.

This was a local container, not a GitHub-hosted runner. A new workflow cannot
be dispatched until it is on the default branch, so the first hosted-runner
numbers come from the first `workflow_dispatch` (or the 07:30 UTC schedule)
after merge. Add them here when they exist.

## Board 05 copper gaps on Linux and macOS, 2026-11-22

The original RESET via-to-trace gap cleared on both hosts. The complete
board had zero fabrication errors on Linux x86_64 and three on macOS arm64.
Both used router build 78 at `7416c1c24`, which includes #9873's stored-via
check and #9875's within-route guard. The macOS replay used `9c26521df`,
whose only change from that revision was this research note.
Both ran the #6508 protocol sequentially for seeds 42, 7 and 1337 with
`KCT_KICAD_DOCKER=1` and pinned kicad-cli 10.0.6. [Linux run](https://github.com/2AMLogic/kicad-tools/blob/9c26521dff072cad992adb2cf9a40d0233402fcd/docs/research/board-05-unattended-route.md#re-measured-2026-11-22-zero-copper-gap-errors-9844-acceptance) · [macOS run and measured geometry](https://github.com/2AMLogic/kicad-tools/issues/9424#issuecomment-7074550273)

| Host | Seed | Vias | Physical gap errors | All kct warnings | Route time (s) |
|---|---:|---:|---:|---:|---:|
| Linux x86_64 | 42 | 124 | 0 | 77 | 369.6 |
| Linux x86_64 | 7 | 124 | 0 | 77 | 375.6 |
| Linux x86_64 | 1337 | 124 | 0 | 77 | 351.1 |
| macOS arm64 | 42 | 118 | 3 | 73 | 231.6 |
| macOS arm64 | 7 | 118 | 3 | 73 | 313.1 |
| macOS arm64 | 1337 | 118 | 3 | 73 | 299.1 |

All six routes exited 0 with 37/37 nets and 142/142 connections. Pinned KiCad
reported zero DRC errors, unconnected items or ERC findings. Copper and label
LVS passed. The three seeds produced byte-identical boards on each host.
The Linux board's SHA-1 starts with `64411406e173`. The macOS board's SHA-1
is `0f1f7745e4b2c3dd4aab42309e762daec526a960`. The logs recorded no
stage or per-net wall-clock cutoff. Linux load fell from 40.5 to 17.8 during
seed 42. The cause of the cross-host geometry difference remains open.

On macOS the SPEED trace passes C14.1 on F.Cu at 0.046747 mm against the
0.150000 mm pad clearance. Two IREF trace-to-trace gaps on F.Cu measure
0.019203 mm against 0.101600 mm. #9424 tracks these three findings. #9844's
board-wide zero-gap criterion remains open. [Gap identities and closest points](https://github.com/2AMLogic/kicad-tools/issues/9424#issuecomment-7074550273) · [Board-wide acceptance criteria](https://github.com/2AMLogic/kicad-tools/issues/9844)

The unattended harness exited 1 on both hosts. Linux tripped the #8073
no-warning gate with 51 `pour_slot`, 9 `silk_hole_overlap`, 8
`redundant_copper_covered`, 4 `redundant_copper_detour`, 2
`redundant_copper_staircase`, 1 `redundant_copper_fragments`, 1 `plane_split`
and 1 `sharp_trace_corner` warning. Its two zone-fill clearance findings were
informational under #8938. On macOS the harness recorded the three physical
gap errors and 73 warnings per seed.

## Re-measured 2026-11-23 on `be9eb6a12`: the gap errors are back (Issue #9424)

One seed (42) through the gate script on the pinned toolchain
(`KCT_KICAD_DOCKER=1`; the gate refuses any other kicad-cli release),
Linux x86_64, C++ backend built in the worktree, load average 12 to 14 while
it ran. Seeds 7 and 1337 were **not** run in this pass, so the three-seed
criterion is still open. No route log carries a `[deterministic-budget]
per-net wall clock bound` or `stage deadline fired` line.

| | seed 42 |
|---|---|
| `kct route` exit / nets | 0 / 37 of 37 (142 of 142 connections) |
| kicad-cli DRC err / warn / unconnected, ERC | 0 / 0 / 0, 0 |
| copper / label LVS | clean / clean |
| `kct check --skip mask_to_copper` | **5 err** / 65 warn |
| vias / route wall s | 128 / 545.7 |
| board sha1 | `59865268d8fc...` |

The board is not the one the 2026-11-22 Linux runs produced (124 vias, sha1
`64411406e173...`, 0 errors, 77 warnings). Router, DRC and stitch merges
landed in between (#9936, #9939, #9959, #9891 and others), and the route now
carries the same family of same-net via/trace slits the macOS replay found:

| Net | Layer | Gap (mm) | Required (mm) | Items |
|---|---|---:|---:|---|
| DRIVER_SLEEP | B.Cu | 0.0387 | 0.1016 | trace `144.945,104.425 -> 149.625,109.105` beside via `146.83,106.895` |
| RESET | B.Cu | 0.0772 | 0.1016 | trace `170.49,75.305 -> 174.325,79.14` beside via `170.165,75.63` |
| SPEED | F.Cu | 0.0467 | 0.1500 | trace `169.96,126.59 -> 169.96,131` against a pad (`fab_band: pad`) |
| RUN | F.Cu | 0.0858 | 0.1016 | trace `163.99,105.465 -> 164.9,106.375` beside via `164.25,105.205` |
| RESET | F.Cu | 0.0700 | 0.1016 | traces `159.83,94.415 -> 159.83,95.568` and `160.1,90.313 -> 160.1,95.838` |

SPEED, RUN and RESET (F.Cu) match three of the four witnesses recorded for
the macOS replay; DRIVER_SLEEP and RESET (B.Cu) are new. All five are
`same_net`, so none is a short. The 65 warnings are 49 `pour_slot`, 6
`redundant_copper_covered`, 4 `redundant_copper_detour` and 6
`silk_hole_overlap`. The 0-error, 0-warning bar fails, and
`--skip mask_to_copper` is still in the gate.

This pass changed no router source. The gap errors are the new work: they
moved with the merges above rather than with load, and the 49 structural
`pour_slot` warnings are the larger remainder under the operator ruling of
2026-11-23 (keep the strict bar, stay red until source repair lands).
