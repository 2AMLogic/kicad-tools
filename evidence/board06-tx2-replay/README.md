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

## Physical controls

Both controls retain the captured neighbor copper. The routed profile files are byte-identical across both backends in the complete run. For a native check, copy the PCB and its matching sidecars into a new directory under one basename, then run KiCad 10.0.6 with the project's pinned configuration.

| PCB | TX2+ and TX2− pad islands | Native opens on the two nets | Native DRC errors |
| --- | --- | --- | --- |
| `jointly-connected.kicad_pcb` | One each | Zero each | 0 |
| `crossing-candidates.kicad_pcb` | One each | Zero each | 9 |

The first control adds the two actual TX2 routes from the complete C++ board. TX2+ uses 66 front-copper segments and TX2− uses nine, all 0.275 mm wide, with no vias. The second combines Python's two independently successful routes. KiCad finds five shorts, one crossing, two clearance errors and one differential-pair gap error. These partially routed controls have 156 opens on other nets and six footprint-library warnings. The first control demonstrates that both nets can coexist with this captured neighbor copper. The four outer failures in the complete Python run have different occupancy and victim counts.
