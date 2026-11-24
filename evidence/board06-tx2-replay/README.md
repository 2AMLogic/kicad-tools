# Board 06 TX2 relief replay

Issues #9936 and #10032 use this reduced test case. It restores the saved copper, negotiated usage and congestion history from an earlier iteration-two relief capture on Board 06. Its input PCB has the same SHA-256 as the input to the complete seed-42 run. The captured occupancy predates that run. The routing source for this replay is `efafdb1f4c2d16743fec94b1e96f0b458e81b76a`. [Full run and both saved boards](https://github.com/2AMLogic/kicad-tools/actions/runs/45017437753) · [Historical capture context](https://github.com/2AMLogic/kicad-tools/pull/9915#issuecomment-7078414198)

From the root of this checkout, run the following commands. Each output directory must be new.

```sh
uv sync --frozen --extra dev
uv run kct build-native
uv run kct build-native --check
PYTHONHASHSEED=42 uv run python evidence/board06-tx2-replay/replay.py python /tmp/tx2-positive-first
PYTHONHASHSEED=42 uv run python evidence/board06-tx2-replay/replay.py python /tmp/tx2-negative-first --negative-first
PYTHONHASHSEED=42 uv run python evidence/board06-tx2-replay/replay.py cpp /tmp/tx2-cpp
```

The first transaction connects its target. The second reaches the opposite pad pair, displaces the first route and fails to reconnect it. The nested attempt encounters the reciprocal failure. The saved output keeps the first net and contains zero added or removed tracks or vias relative to the preceding saved board. Reversing the order exchanges which net survives. The Python runs hit two 120-second deadlines. C++ reached its million-expansion limit in the reduced case. The complete C++ run connected all 21 benchmark signals.

`results.json` names both the outer transaction and the net actually being searched. Each run writes the starting board and both post-transaction PCBs with their matching project and design-rule sidecars. The checkpoint uses JSON, and the numeric NumPy archive loads with `allow_pickle=False`.

## Post-#9936 measurements

The #9936 work re-ran this replay on top of the fix commit and measured the failing re-land search directly (all on the identical restored grid state, before any copper is placed; the intercept harness drove the same two-transaction order through production `_relief_rescue`):

| Search of displaced `USB3_TX2+` (`J1.B2->U2.F2`, partner route committed) | Outcome |
| --- | --- |
| Forward, unbiased, 120 s backstop | deadline hit at 277,504–290,816 of 1,000,000 expansions (cut short) |
| Forward, goal-bias weight 1.5 (#9901 extension) | deadline hit — same outcome as unbiased |
| Forward, weights 2.0 and 3.0 | deadline hit at 380,928 / 303,104 expansions |
| Forward, unbiased, 600 s | stopped at ~534 s by the 1,000,000-expansion cap (iteration-limited, not a drain; an uncapped rerun was still expanding well past that count) |
| Reverse pad order, unbiased | **frontier drained in 0.9 s (`last_route_exhausted` = proof)** |

A drained open set is the pathfinder's unreachability proof (#6098), and unreachability is direction-independent — so the 0.9 s reverse drain proves the displaced partner has **no normal-sharing-mode path once the Python rescue's own `USB3_TX2-` route is committed** (the forward direction simply cannot afford the drain: its reachable component needs more than the 1,000,000-expansion cap to exhaust). The 120-second backstop was reporting this proof as a cut-short deadline. With the #9936 instrumentation the production rollback log now states the measured exit per victim, for example:

```
Relief rescue: USB3_TX2- re-land failure exits (issue #9936): USB3_TX2+ -- J1.B2->U2.F2: wall deadline hit at 290816 expansions (search cut short)
```

The C++ replay of the same transaction (also re-run on top of the fix) stops at its 1,000,000-expansion iteration cap instead of the wall backstop, and its structured diagnostics name the closing copper: the exit line reports `iteration cap reached at 1,000,000 expansions (search cut short); last blocker USB3_RX2+ at (118.0, 55.5)` for the TX2+ re-land and the symmetric `USB3_RX2- at (117.0, 55.5)` blocker for the reciprocal TX2− re-land inside the nested rescue. Both backends restore the board exactly (zero added or removed tracks and vias).

The saved boards were re-checked with the pinned evidence toolchain (KiCad 10.0.6 through CI's image, scratch `KICAD_CONFIG_HOME`, each board with its matching `.kicad_pro` / `.kicad_dru`): `jointly-connected.kicad_pcb` has 0 DRC errors and 0 opens on the TX2 nets (the feasible control); both post-transaction rollback boards (`after-2` of the Python and C++ replays) have 0 DRC errors and exactly the pre-attempt connectivity — 1 open on USB3_TX2−, TX2+ connected, 156 unrelated opens inherited from the fixture (the constrained control).

The jointly-connected control below demonstrates the captured copper admits a coexisting pair — but with the C++ routes. The measured limitation handed to #9850: the sequential Python rescue commits the first conflict-free probe path for the rescued leg, that route's halo seals the displaced partner's re-land (proved), the depth-1 nested rescue only swaps which leg is sealed, and the transaction correctly rolls back verbatim (zero added or removed tracks and vias). Repairing reach requires choosing the rescued leg's route under the partner's re-land constraint (coupled re-landing), which this issue leaves to the parent.

## Physical controls

Both controls retain the captured neighbor copper. The routed profile files are byte-identical across both backends in the complete run. For a native check, copy the PCB and its matching sidecars into a new directory under one basename, then run KiCad 10.0.6 with the project's pinned configuration.

| PCB | TX2+ and TX2− pad islands | Native opens on the two nets | Native DRC errors |
| --- | --- | --- | --- |
| `jointly-connected.kicad_pcb` | One each | Zero each | 0 |
| `crossing-candidates.kicad_pcb` | One each | Zero each | 9 |

The first control adds the two actual TX2 routes from the complete C++ board. TX2+ uses 66 front-copper segments and TX2− uses nine, all 0.275 mm wide, with no vias. The second combines Python's two independently successful routes. KiCad finds five shorts, one crossing, two clearance errors and one differential-pair gap error. These partially routed controls have 156 opens on other nets and six footprint-library warnings. The first control demonstrates that both nets can coexist with this captured neighbor copper. The four outer failures in the complete Python run have different occupancy and victim counts.

## Partner drill clearance on the Python replay (#10032)

The first TX2− probe selects a BGA via at (151.75, 70.35) mm. This leaves 0.0500 mm between its drill and the displaced TX2+ drill at (151.45, 70.35) mm. The project requires 0.5000 mm. The Python relief transaction tries a normal route from U2.F3 to J1.B3 with the original TX2+ drill sites reserved. This extra search has a 4,096-expansion ceiling and honors a smaller existing cap. On this capture the frontier drained in 0.28 s. The ordinary rescue then found a TX2− path, failed to reconnect TX2+, and restored every track, via and pad island. TX2+ remained connected and TX2− remained open.

An independent reverse search on the captured neighbors, with the other leg absent, excludes each BGA-side via it finds before trying again. Both legs exhausted their frontiers after two sites. All dimensions below are in millimeters. The two 0.25 mm drills leave their center distance minus 0.25 mm between hole edges.

| TX2+ site | TX2− site | Hole gap |
| --- | --- | ---: |
| (151.45, 69.70) | (151.75, 69.70) | 0.0500 |
| (151.45, 69.70) | (151.75, 70.35) | 0.4659 |
| (151.45, 70.35) | (151.75, 69.70) | 0.4659 |
| (151.45, 70.35) | (151.75, 70.35) | 0.0500 |

`partner-candidate.json` contains the original connected TX2+ and a Python TX2− candidate found while reserving space around that positive path. Its segment records store (x1, y1, x2, y2, layer, width), and its via records store (x, y, first layer, last layer, diameter, drill). Both routes use the captured neighbors. The second control moves only the TX2− via from (151.75, 69.70) to (151.75, 69.65) and joins the old center to the new one on F.Cu and In1.Cu inside the via land. This yields 0.5116 mm of hole clearance. The original Python grid rejects the moved via. Its via-clearance footprint reaches the F.Cu cell at (151.80, 69.20), which `_apply_stitch_via_halo` reserved when registering U2.E3 (+1V2). That reservation preserves space to stitch a plane-net pad. The C++ physical control instead uses front copper throughout both legs.

| Saved PCB | TX2+ / TX2− pad islands | Native TX2 opens | Native errors | Native warnings |
| --- | --- | --- | ---: | ---: |
| Before and after the failed rescue | 1 / 2 | One on TX2− | 0 | 11 |
| Recorded candidate, opposite drill rows | 1 / 1 | Zero | 0 | 12, including `hole_to_hole` |
| One-cell physical control | 1 / 1 | Zero | 0 | 11 |
| Existing C++ joint control | 1 / 1 | Zero | 0 | 11 |

The 11 shared warnings concern silkscreen and text. All boards have 156 unrelated opens. These partial-board measurements apply to the captured occupancy. [Full-board comparison and remaining four negative legs](https://github.com/2AMLogic/kicad-tools/issues/9850)

From the repository root, use new output paths. The native checker regenerates the seven fixture footprints from Board 06’s generator, installs them in a scratch project, and runs pinned KiCad 10.0.6 with the board’s routed project and rule files. It refuses a library-environment finding.

```sh
fixture=evidence/board06-tx2-replay
PYTHONHASHSEED=42 uv run python "$fixture/replay.py" python /tmp/tx2-rescue
PYTHONHASHSEED=42 uv run python "$fixture/terminal_sites.py" /tmp/tx2-sites
uv run python "$fixture/native_check.py" /tmp/tx2-rescue/after-2.kicad_pcb /tmp/tx2-rollback.json
uv run python "$fixture/native_check.py" /tmp/tx2-sites/paired-candidate.kicad_pcb /tmp/tx2-paired.json
uv run python "$fixture/native_check.py" /tmp/tx2-sites/one-cell-control.kicad_pcb /tmp/tx2-one-cell.json
uv run python "$fixture/native_check.py" "$fixture/jointly-connected.kicad_pcb" /tmp/tx2-joint.json
```
