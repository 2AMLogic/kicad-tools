# Board 06 TX2 relief replay

This is a reduced test case for issue #9936. It restores the saved copper, negotiated usage and congestion history from an earlier iteration-two relief capture on Board 06. Its input PCB has the same SHA-256 as the input to the complete seed-42 run. The captured occupancy predates that run. The routing source for this replay is `efafdb1f4c2d16743fec94b1e96f0b458e81b76a`. [Full run and both saved boards](https://github.com/2AMLogic/kicad-tools/actions/runs/45017437753) · [Historical capture context](https://github.com/2AMLogic/kicad-tools/pull/9915#issuecomment-7078414198)

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

`results.json` names both the outer transaction and the net actually being searched. Each run writes two PCBs with their routed project and design-rule sidecars. The checkpoint uses JSON, and the numeric NumPy archive loads with `allow_pickle=False`.

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
