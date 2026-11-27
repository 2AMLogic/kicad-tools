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
(`KCT_KICAD_DOCKER=1`, pinned kicad-cli 10.0.6; the gate refuses any other
kicad-cli release), Linux x86_64, load average 12 to 14 while it ran. The
revision is `be9eb6a12`, which requires C++ router build 79
(`_REQUIRED_CPP_BUILD_VERSION` in `router/cpp_backend.py`; the 2026-11-22 runs
above used build 78). The extension was built in the worktree; the
`kct build-native --check` output itself was not captured, so build 79 is the
build the revision requires rather than a recorded check line. Seeds 7 and
1337 were **not** run in this pass, so the three-seed criterion is still open.
The seed-42 route log carries no `[deterministic-budget] per-net wall clock
bound` or `stage deadline fired` line.

| | seed 42 |
|---|---|
| `kct route` exit / nets | 0 / 37 of 37 (142 of 142 connections) |
| kicad-cli DRC err / warn / unconnected, ERC | 0 / 0 / 0, 0 |
| copper / label LVS | clean / clean |
| `kct check --skip mask_to_copper` | **5 err** / 65 warn |
| vias / route wall s | 128 / 545.7 |
| board sha1 | `59865268d8fc...` |

The board is not the one the 2026-11-22 Linux runs produced (124 vias, sha1
`64411406e173...`, 0 errors, 77 warnings). The revision window is `7416c1c24`
(router build 78) to `be9eb6a12` (build 79), including the 78 to 79 build bump.
It was not bisected, so no individual merge is identified as the cause. The
route now carries the same family of same-net via/trace slits the macOS
replays found:

| Net | Layer | Gap (mm) | Required (mm) | Items |
|---|---|---:|---:|---|
| DRIVER_SLEEP | B.Cu | 0.0387 | 0.1016 | trace `144.945,104.425 -> 149.625,109.105` beside via `146.83,106.895` |
| RESET | B.Cu | 0.0772 | 0.1016 | trace `170.49,75.305 -> 174.325,79.14` beside via `170.165,75.63` |
| SPEED | F.Cu | 0.0467 | 0.1500 | trace `169.96,126.59 -> 169.96,131` against a pad (`fab_band: pad`) |
| RUN | F.Cu | 0.0858 | 0.1016 | trace `163.99,105.465 -> 164.9,106.375` beside via `164.25,105.205` |
| RESET | F.Cu | 0.0700 | 0.1016 | traces `159.83,94.415 -> 159.83,95.568` and `160.1,90.313 -> 160.1,95.838` |

Against the macOS build-79 replay at `06b494587`
([#9844 comment](https://github.com/2AMLogic/kicad-tools/issues/9844#issuecomment-7086213789),
witnesses SPEED, RUN, VM and RESET), SPEED (0.0467 mm) and RUN (0.0858 mm)
reproduce the same gap widths. RESET (F.Cu) is the same net, layer and region
and the same trace-to-trace class, but a different gap: 0.0700 mm here against
0.02981 mm at (159.943, 94.690) there. DRIVER_SLEEP is not among those
witnesses, and VM did not recur in this run. Against the earlier build-78
macOS replay above (SPEED and two IREF gaps) only SPEED recurs. All five are
`same_net`, so none is a short. The 65 warnings are 49 `pour_slot`, 6
`redundant_copper_covered`, 4 `redundant_copper_detour` and 6
`silk_hole_overlap`. The 0-error, 0-warning bar fails, and
`--skip mask_to_copper` is still in the gate.

This pass changed no router source. The gap errors are the new work. Whether
they follow the revision window above or the host load (12 to 14 here, 17.8 to
40.5 on the 2026-11-22 runs) was not tested: no bisect or repeated low-load run
was done. The 49 structural
`pour_slot` warnings are the larger remainder under the operator ruling of
2026-11-23 (keep the strict bar, stay red until source repair lands).

## Recipe copper cleanup does not clear the gate (2026-11-23, issue #9424)

Seed 42 only, pinned kicad-cli via `KCT_KICAD_DOCKER=1`, Linux x86_64, router
build 79, worktree on `b0222a8d5` (loadavg 6.5 to 8.6 at start and end of the
route).

**Rerun reproduces.** A second seed-42 run of `board_05_unattended.py` gave
the same 5 `physical_copper_gap` errors and 65 warnings as the run above
(route exit 0, 37/37 nets, 128 vias, DRC 0/0/0, ERC 0, LVS clean; route wall
433.5 s against 545.7 s). The error and warning counts did not move between
the two runs, and the board sha1 was not compared.

**Experiment: add the recipe's cleanup to the gate.** `redesign/build.py`
runs `cleanup_copper` (`kct pcb dedupe` plus the DRC-verified `kct pcb prune`,
issue #8278) after the copper is placed; the unattended gate does not. The
hypothesis was that the six `redundant_copper_covered` warnings are a missing
gate step, not a router defect. The same loop was applied to the saved seed-42
board and the board was regraded with `--grade-only`. It was a throwaway
experiment; the gate script is unchanged.

| `kct check --skip mask_to_copper` | Before cleanup | After cleanup |
|---|---:|---:|
| `physical_copper_gap` errors | 5 | 5 |
| `pour_slot` | 49 | 50 |
| `redundant_copper_covered` | 6 | 2 |
| `redundant_copper_detour` | 4 | 6 |
| `redundant_copper_staircase` | 0 | 1 |
| `silk_hole_overlap` | 6 | 6 |
| total warnings | 65 | 65 |

- The cleanup does **not** reach a fixed point: it raised "did not converge in
  10 passes" with 12 segments removed and 12 ends snapped in total. After the
  first passes only net VM is still planned. Each pass adds the stub
  `(21.0, 8.0) -> (21.755, 7.245)` and the next pass removes the segment it
  added as a "redundant branch", so the loop churns on a net it has already
  cleaned. Whether that is a defect in `pcb_prune` or a property of this
  route was not isolated.
- Pruning moved warnings between classes (covered down by 4, detour up by 2,
  staircase up by 1, `pour_slot` up by 1) and left the 5 same-net gaps
  untouched, so it is not a repair path for the gate.

Not done this pass: seeds 7 and 1337, the full-mask `mask_to_copper` run, and
any source repair of the five gaps or the pour-slot warnings. The skip and the
0-error, 0-warning bar stay as they are, per the 2026-11-23 operator ruling.

## VM trace clearance beside a branch via, 2026-11-24 (issue #9424)

On `ca3e73781`, the ordinary trace optimizer replaced five VM legs on F.Cu
with a two-leg shortcut. Its 0.20 mm trace passed within 0.017193 mm of a
0.60 mm via on another branch of VM. The applied fabrication rule requires
0.1016 mm. On `55af96912`, the optimizer keeps the original path in this
case. Moving the same via 0.40 mm farther away still gives the two-leg
shortcut in the normal optimizer regression. [Original route and geometry controls](https://github.com/2AMLogic/kicad-tools/issues/9424#issuecomment-7128595990)

The public unattended recipe ran on macOS 27.2.1 arm64 with C++ router build 79 and
pinned KiCad 10.0.6. Each unskipped check used the saved board's own project,
footprint libraries, fabrication profile and net-class map. An independent
refill used `kicad-cli pcb drc --refill-zones --save-board` on a copy, then
repeated full-mask fabrication checks, native DRC and copper LVS on that copy.

| Seed | Route time (s) | Nightly errors | Nightly warnings | Saved full-mask errors | Saved full-mask warnings |
|---:|---:|---:|---:|---:|---:|
| 42 | 324.9 | 2 | 77 | 15 | 100 |
| 7 | 413.0 | 2 | 77 | 15 | 100 |
| 1337 | 488.7 | 2 | 77 | 15 | 100 |

All three saved PCBs have SHA-256
`6da1a3cb6d1d7dab22717546dd139b88a39ef914e595551b31c2c1e4115f61f2`.
Each route completed 37/37 nets and 143/143 connections with 121 vias. Native
DRC reported zero violations or unconnected items, ERC reported zero violations,
and copper and label LVS passed. Every saved board and refilled copy had 149
matched pads and 398 horizontal, vertical or 45-degree tracks. The route logs
showed the configured 60 s whole-sweep and 10 s per-net wall-clock limits and
contained zero per-net or stage deadline markers. The public gate exited 1 on
the fabrication findings.

| Seed 42 comparison | Full-mask errors | Full-mask warnings | Waived |
|---|---:|---:|---:|
| Original saved board | 16 | 100 | 0 |
| Repaired saved board | 15 | 100 | 0 |
| Original after independent refill | 15 | 104 | 0 |
| Repaired after independent refill | 14 | 104 | 0 |

The independent refills for seeds 7 and 1337 also had 14 errors and 104 warnings.
Comparing each form against its own original removed exactly the VM error and
preserved every warning identity, including repeated findings. On the repaired
saved board the remaining errors were two physical copper gaps (RUN and RESET)
and 13 mask-to-copper findings.

Two full-mask checks of the saved seed-42 board took 41.5 and 45.0 seconds.
Two checks with the nightly `--skip mask_to_copper` took 12.8 and 17.4 seconds
on the same host. A second from-scratch seed-42 route reproduced the saved
PCB byte for byte. Running the public `--grade-only` control on the original
seed-42 board exited 1 with three errors and 77 warnings.

## RUN trace clearance at its own via, 2026-11-24 (issue #9424)

On `2de8d8e2e`, the seed-42 RUN shortcut left a 0.085833 mm gap beside
its own F.Cu via. The fabrication profile requires 0.1016 mm. On
`c75ceefd2` the optimizer extends the trace leaving the via and reconnects
it to the same straight run. The saved board replaces two 0.15 mm RUN
segments with three. All 396 other tracks, 121 vias and 200 pad records
retain their geometry.

| Unattended run | Route time | Nightly errors | Nightly warnings |
|---|---:|---:|---:|
| Original, seed 42 | 332.1 s | 2 | 77 |
| Revised, seed 42 | 371.7 s | 1 | 77 |
| Revised, seed 7 | 351.6 s | 1 | 77 |
| Revised, seed 1337 | 323.7 s | 1 | 77 |
| Revised, seed 42 repeated from scratch | 363.5 s | 1 | 77 |

The original seed-42 PCB has the same SHA-256 as the three boards in the VM
comparison above. All four revised boards have SHA-256
`c5e6401c916c3ffcae56bb68014a3fad11b4797df6f0adc9770843d964808aa5`.
Every route completed 37/37 nets and 143/143 connections. Pinned KiCad
10.0.6 reported zero DRC violations, unconnected items or ERC violations.

| Full fabrication check including mask | Original errors / warnings | Revised errors / warnings |
|---|---:|---:|
| Saved board | 15 / 100 | 14 / 100 |
| After independent KiCad refill | 14 / 104 | 13 / 104 |

For each seed and each board form, the RUN gap was the only error removed.
The complete warning records matched the corresponding original, with zero
waivers. Native DRC remained clean after refill and copper LVS matched 149
pads on both forms. All 399 revised tracks are horizontal, vertical or 45
degrees. The stored board still has 13 mask-to-copper errors, and the
refilled board has 12. RESET and the 77 nightly warnings keep the unattended
gate at exit 1.

## RESET branch entering a via beside its trunk, 2026-11-24 (issue #9424)

On the public recipe at `b66f14f47`, the optimizer introduced a 0.029810 mm
F.Cu gap between two RESET tracks. The fabrication limit was 0.1016 mm.
The shorter branch already ran down a longer RESET trace. Continuing along
that trace to the level of the via and then entering its barrel removes the
diagonal facing the neighbouring branch. Both new legs lie within the existing
trunk and barrel. The optimizer retains a connected path between the original
endpoints and checks for other copper that depended on the removed diagonal.

| Pinned KiCad 10.0.6, C++ router build 79 | Before | After |
| --- | ---: | ---: |
| Nightly manufacturer check, errors / warnings | 1 / 77 | 0 / 77 |
| Full manufacturer check on the saved board | 14 / 100 | 13 / 100 |
| Full manufacturer check after independent refill | 13 / 104 | 12 / 104 |

Seeds 42, 7 and 1337 each routed 37 of 37 nets and 143 of 143 connections
with 121 vias. Native DRC and ERC were clean. Copper LVS matched 149 pads on
both the saved and refilled boards. The three saved boards and a second fresh
seed-42 run produced the same PCB bytes.

The RESET slit was the sole manufacturer error removed. One existing
mask-to-copper warning on U1/29 names a different generated UUID for the
same neighbouring track. Its location, measurement (0.130009 mm) and limit
(0.090 mm) are unchanged. The remaining warning records match in full. All
checks used zero waivers. The 77 nightly warnings and the full-check
mask findings keep Board 05 below the unattended acceptance target.

## C3 and R11 silkscreen over routed vias, 2026-11-25 (issue #9424)

On a fresh seed-42 board, KiCad's front silkscreen Gerber had ink inside
three 0.30 mm via drills. C3 crossed one and R11 crossed two. The unattended
recipe now runs `place-silk-refs --mfr jlcpcb --layers 4 --over-holes` after
adding the fiducials and board ID. The placer uses the drills present in
each routed board. Running `--grade-only` on the original board preserved its
SHA-256 and reported all three overlaps.

| Via centre in KiCad coordinates (mm) | Reference | Original ink inside drill (mm²) | Final ink-to-drill edge (mm) |
| --- | --- | ---: | ---: |
| 131.360, 102.475 | C3 | 0.02657 | 0.565 |
| 172.245, 95.455 | R11 | 0.00849 | 1.965 |
| 174.130, 95.715 | R11 | 0.03947 | 2.598 |

KiCad 10.0.6 plotted both seed-42 boards. All four copper layers, both mask
and paste layers, back silkscreen, board outline and all 170 Excellon drills
matched. Only the two front reference markings moved.

| Seed-42 check | Before | After |
| --- | ---: | ---: |
| Nightly manufacturer errors / warnings | 0 / 80 | 0 / 77 |
| Full manufacturer errors / warnings on the saved board | 14 / 84 | 14 / 81 |
| `silk_hole_overlap` warnings | 3 | 0 |

Every other manufacturer error and warning kept its original identity and
message. The full check used zero waivers. The 14 mask-to-copper errors and
77 nightly warnings keep the unattended gate at exit 1.

With the recipe based on `59d5121b5` and C++ router build 79, seeds 42, 7
and 1337 produced identical saved PCBs (SHA-256
`f6fbdbcb81a6e80e5732af89e6dec8584cfca182850c56018a266e33c35b8f27`).
Each completed 37/37 nets and 143/143 connections with 120 vias. Native
DRC and ERC returned zero findings, and both copper and label LVS passed.

## Optimizer bends beside their own pad's mask opening, 2026-11-27 (issue #9424)

Measured on Linux x86_64 with C++ router build 79 and pinned kicad-cli
10.0.6 (`KCT_KICAD_DOCKER=1`). Other work shared the host throughout
(1-minute load 11 to 28 during the first routes, up to 53 on the re-run).

On `origin/main` @ `73575b4dd`, and again at `0a3c4eb6c` with the same board
bytes (SHA-256 `b5d0e180347327366495b3fb7955e0860066ac48b7fb5e156d7a63d179610716`),
seed 42 passed the route and every native check and carried **18
`mask_to_copper` errors, 101 warnings and 252 infos** in the full JLCPCB
check, and 0 errors / 97 warnings in the nightly rule set. Fourteen of the 18
errors are track
segments against openings of their own net. Five are a leg whose rounded end
comes within the 0.09 mm mask clearance of an opening it does not touch
(HALL_A at J3/3 0.0575 mm, nFAULT at U1/14 0.020 mm, GND_SENSE at U2/7
0.015 mm, VM at U3/5 0.045 mm, SPEED at RV1/2 0.014 mm); nine are copper
over an opening that is not one clean entry. The rest are three vias beside
same-net pads (C2/1, R6/2, R4/1) and the V3P3 pour at U2/15.

The commit journal (`access_witness.json`) dates each segment's first
commit. Nine of the fourteen first appear in the optimizer's post-route pass
(GND_SENSE, SENSE_IN, HALL_A, EN_C, SPEED, nFAULT, RESET twice and
DRIVER_SLEEP). Four come from the router's own path (VM at U3/5 and C1/1
twice, PG_DELAY at U3/7), and the GND segment at C3/2 is not in the journal.

`kct route` now gives the trace optimizer the manufacturer's
mask-to-copper clearance (the cited policy `kct check --mfr` judges
`mask_to_copper` with) and the board's solder-mask expansion. Each rewrite
counts, per track segment against the openings of its own net, the defects
the mask rule reports: copper nearer than the clearance without touching,
or copper over an opening that fails the rule's one-clean-entry test. The
rewrite is declined when the count grows and kept when the unoptimised
chain carries as many (the SPEED exit at RV1/2), so no shortcut is traded
for a worse path. A board whose mask margin is not uniform, or a route
without a cited mask policy, keeps the previous behaviour.

| Seed | Route wall (s) | Vias | Nightly errors / warnings | Full-mask errors / warnings / infos |
| --- | ---: | ---: | ---: | ---: |
| 42 | 287.6 | 124 | 0 / 98 | 14 / 103 / 255 |
| 7 | 281.8 | 124 | 0 / 98 | 14 / 103 / 255 |
| 1337 | 339.6 | 124 | 0 / 98 | 14 / 103 / 255 |

All three seeds exited 0 with 37/37 nets and 143/143 connections, kicad-cli
DRC 0 errors / 0 warnings / 0 unconnected, ERC 0 and both LVS checks clean.
The saved boards are byte-identical (SHA-256
`6c50088c0656a808c019fee1f9bb13c4831eacef40a858d75ad16d4374875357`), and no
route log carries a `per-net wall clock bound` or `stage deadline fired`
line. `kct check` took 12.6 s with the mask pass skipped and 43.2 to 48.3 s
with it. The three seeds were routed again on the merged head (`0a3c4eb6c`
plus this change): the same board bytes and counts, walls 384.7, 293.3 and
270.8 s.

Against the seed-42 baseline four errors are gone: HALL_A at J3/3, nFAULT
at U1/14, EN_C at U1/12 (an off-centre exit hugging the pad's side) and
DRIVER_SLEEP at R8/1. Two warnings are new: nFAULT at U1/14 now measures
0.135 mm, advisory and the shape the router's own path has, and one
`pour_slot` pair (EN_C and DRIVER_SLEEP; `pour_slot` 64 to 65). Every other
finding kept its identity. The 98 nightly warnings are 65 `pour_slot`, 9
`redundant_copper_covered`, 6 `redundant_copper_staircase`, 4
`redundant_copper_detour`, 2 `redundant_copper_fragments`, 11
`netclass_agreement` findings (Issue #10139, not examined here) and 1
`plane_split`.

Seeds 7 and 1337 were not run on the baseline revision. The ten remaining
segment errors and the three vias are the router's own geometry, which a
rewrite-relative guard cannot reach: the pathfinder has to keep its bends
and vias clear of the openings of the pads they connect, or a repair has to
move them. `--skip mask_to_copper` stays in the gate.
