# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-26*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

_None._

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors)

## Important

`loom:important`; taken before unlabelled work.

- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9939**: Repair Board 03’s 16 remaining mask-to-copper findings while preserving legal trace angles

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#10268**: kct check misses dangling copper when a dead-end branch is duplicated in reverse
- **#10269**: Dangling branch tip is masked by the side of its own parent track

## In Progress

Issues currently being built (`loom:building`).

- **#8486**: Reduce repeated full-budget reroutes of six narrow nets on the 97-net board
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410)
- **#9939**: Repair Board 03’s 16 remaining mask-to-copper findings while preserving legal trace angles
- **#10107**: Use the declared layout for receipt validation, render output and fabrication handoff
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film
- **#10189**: router/optimize-traces: honour project netclass track widths on re-route
- **#10240**: schematic: get_net_for_pin and extract_netlist disagree with KiCad (and each other) when a rail carries two names
- **#10250**: pcb center-on-sheet moves wire anchors but leaves reinforcement coordinates at their old positions
- **#10258**: Board 06: clear eight USB2 mask findings at the J1 connector on the seed-42 fixture
- **#10261**: Fill the routed board before optional power-plane stitching on mixed-signal layouts
- **#10265**: Board 03 tier-1 USB_D+ route exceeds its 16 mm branch length limit
- **#10267**: Board 06 clear the six remaining power-net mask findings at U2 and U3
- **#10277**: Router-generated netclasses disappear from a fresh KiCad schematic netlist
- **#10290**: Clear the USB3 transmitter mask errors at J1/A3 and U2/B3 on Board 06
- **#10294**: Board 06: clear the J1/B2 USB3_TX2+ mask overlap at its connector exit
- **#10297**: Board 06: clear three USB3_RX1 mask errors at J1/B10 and U2/B5
- **#10299**: Assembly preflight should compare through-hole lead dimensions with the finished plated hole
- **#10317**: creepage: support reviewed per-pad-pair exceptions alongside the global same-footprint waiver
- **#10326**: silk_pad_clearance misses or misreports gaps when a pad inherits its footprint mask margin

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#10128**: fix(validate): stop pour_slot reporting packed parallel track pairs
- **#10193**: fix: honour project netclass widths in route and optimize-traces
- **#10248**: fix(board05): run the mask-copper repair in the unattended gate without adding warnings
- **#10252**: fix: resolve one net identity across schematic netlist APIs
- **#10253**: fix: translate reinforcement sidecar with pcb center-on-sheet
- **#10262**: fix(route): stitch power planes after the routed copper is refilled
- **#10271**: fix(drc): detect dangling branches hidden by overlapping copper
- **#10272**: fix(router): shorten aligned differential-pair pad hops
- **#10278**: Preserve router netclasses in KiCad schematic exports
- **#10283**: Recognize the painted polarity sector of KiCad radial capacitors
- **#10289**: fix(drc): add orthogonal elbow fallback and refill-safe bend checks
- **#10302**: Expose the datasheet package commands in kct
- **#10307**: Use circular mask apertures for round pads in silkscreen checks
- **#10309**: feat: compare through-hole lead dimensions with the finished hole in preflight
- **#10311**: fix(sync): reconcile schematic purchasing fields on board footprints
- **#10319**: fix: recognize wide track ends covering a plated terminal
- **#10320**: feat: add scoped per-pad-pair creepage exceptions to kct creepage
- **#10322**: fix: check fitted wire spans on mixed copper and wire paths
- **#10324**: fix: identify copper lands beneath separate mask pads
- **#10327**: Preserve datasheets on generated schematic instances

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#10273**: fix: clear Board 06 power-net mask errors at U2 and U3
- **#10293**: fix(board06): clear the USB3 transmitter mask errors at both ends
- **#10298**: fix(board06): clear the USB3_TX2+ connector exit at J1/B2
- **#10304**: Keep pin count and pitch separate in datasheet footprint suggestions
- **#10305**: fix(board06): clear the USB3 RX1 connector and BGA mask openings

## Proposed

Issues carrying `loom:curated`.

- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-10-19 (#7457 / #7084 Q10) *(curated)*
- **#7856**: [Epic #6441] Phase 5 measurement: coupled-trunk retention >= 95% on boards 03, 06b, 06rf, 07 *(curated)*
- **#8486**: Reduce repeated full-budget reroutes of six narrow nets on the 97-net board *(curated)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)*
- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(curated)*
- **#8943**: Record JLCDFM silkscreen line-width selection criterion and per-item rows (residue of #8844) *(curated)*
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings *(curated)*
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI *(curated)*
- **#9068**: Board 05: clear the strict-gate flood (29 physical_copper_gap errors, 13 silk_hole_overlap, 348 provisional mask_to_copper) and re-pin readiness *(curated)*
- **#9093**: submit-jlcpcb: attended, exact-total-approved JLC parts purchase leg *(curated)*
- **#9298**: Repository moved to the 2AMLogic organization: update hard-coded owner slug and the PyPI trusted publisher *(curated)*
- **#9381**: Committed demo-board BOMs still carry the pre-#9323 duplicate-LCSC lines *(curated)*
- **#9406**: Converge boards 06/07 DRC re-check into the single-sourced job scripts (latent local-gate --full failure) *(curated)*
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper *(curated)*
- **#9454**: No migration path for evidence-bound waivers across the ev3 -> ev4 bump: every consumer waiver goes stale *(curated)*
- **#9464**: Boards 03, 07 and 09 have parts off the 0.1 mm grid that pad_grid now reports (surfaced by #9434) *(curated)*
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410) *(curated)*
- **#9748**: optimize-traces --drc-aware --mfr jlcpcb introduces mask_to_copper errors at pad entries that the un-optimised board does not have *(curated)*
- **#9800**: Boards 02/03/05: drop per-board silk wiring and retire silk tolerances (#9795 phase 4) *(curated)*
- **#9844**: Router: route turns back past its own via inside the slit band (board 05 RESET V-turn) *(curated)*
- **#9893**: docs: surface JLCPCB assembly and submission automation on kicad-tools.org and in the README *(curated)*
- **#9921**: Repair Board 05’s reviewed revision-B copper and regenerate its manufacturing package *(curated)*
- **#9939**: Repair Board 03’s 16 remaining mask-to-copper findings while preserving legal trace angles *(curated)*
- **#10105**: Support a repository layout with editable KiCad sources and a sibling generated-output directory *(curated)*
- **#10107**: Use the declared layout for receipt validation, render output and fabrication handoff *(curated)*
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film *(curated)*
- **#10189**: router/optimize-traces: honour project netclass track widths on re-route *(curated)*
- **#10240**: schematic: get_net_for_pin and extract_netlist disagree with KiCad (and each other) when a rail carries two names *(curated)*
- **#10250**: pcb center-on-sheet moves wire anchors but leaves reinforcement coordinates at their old positions *(curated)*
- **#10258**: Board 06: clear eight USB2 mask findings at the J1 connector on the seed-42 fixture *(curated)*
- **#10261**: Fill the routed board before optional power-plane stitching on mixed-signal layouts *(curated)*
- **#10265**: Board 03 tier-1 USB_D+ route exceeds its 16 mm branch length limit *(curated)*
- **#10267**: Board 06 clear the six remaining power-net mask findings at U2 and U3 *(curated)*
- **#10268**: kct check misses dangling copper when a dead-end branch is duplicated in reverse *(curated)*
- **#10269**: Dangling branch tip is masked by the side of its own parent track *(curated)*
- **#10280**: silk_overlap reports 1,105 collisions inside one radial capacitor polarity marking *(curated)*
- **#10284**: Report project DRC settings rewritten by manufacturing export *(curated)*
- **#10290**: Clear the USB3 transmitter mask errors at J1/A3 and U2/B3 on Board 06 *(curated)*
- **#10294**: Board 06: clear the J1/B2 USB3_TX2+ mask overlap at its connector exit *(curated)*
- **#10297**: Board 06: clear three USB3_RX1 mask errors at J1/B10 and U2/B5 *(curated)*
- **#10299**: Assembly preflight should compare through-hole lead dimensions with the finished plated hole *(curated)*
- **#10301**: Public kct datasheet CLI rejects extract-package and suggest-footprint *(curated)*
- **#10303**: Datasheet package extraction interprets the P48 in LQFP48 as a 48 mm lead pitch *(curated)*
- **#10306**: silk_over_copper flags clean concentric rings around round pad apertures *(curated)*
- **#10310**: kct sync loses or leaves stale purchasing fields when adding, swapping or changing a part *(curated)*
- **#10317**: creepage: support reviewed per-pad-pair exceptions alongside the global same-footprint waiver *(curated)*
- **#10318**: path_ampacity: wide copper covering a plated terminal can be reported disconnected *(curated)*
- **#10321**: Wire courtyard DRC treats copper-only legs of a partially wire-reinforced current path as fitted wire *(curated)*
- **#10323**: Mask assessment reports a shunt’s own land through its separate mask-only aperture *(curated)*
- **#10325**: Schematic generation shadows the library Datasheet with an empty instance field *(curated)*
- **#10326**: silk_pad_clearance misses or misreports gaps when a pad inherits its footprint mask margin *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)
- **#8838**: Epic: fab DFM parity - a board that passes kct check --mfr jlcpcb must pass JLCPCB's DFM tool
- **#10105**: Support a repository layout with editable KiCad sources and a sibling generated-output directory

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Very important | 1 |
| Important | 2 |
| Ready (`loom:issue`) | 2 |
| In Progress (`loom:building`) | 20 |
| PRs awaiting review | 21 |
| Approved PRs awaiting merge | 5 |
| Curated | 51 |
| Architect / Hermit proposals | 0 |
| Active epics | 4 |
<!-- guide:plan-body:end -->

## Release History (static archive)

_Historical release notes below this point are a static archive, not regenerated by Guide — see WORK_LOG.md for the current chronological record of merged PRs and closed issues._

| Release | Highlights |
|---------|-----------|
| **v0.20.0** (2026-08-06) | `kct route --complete` completion pass, route-time HV-isolation enforcement in every engine, `.kct_waivers.json` waiver mechanism, `net-status` strict-by-default, KiCad-10 net-dialect / export-manifest / LVS-identity correctness sweep; shipped as a 13-PR train through a GitHub Actions outage |
| **v0.19.0** (2026-07-20) | HV-isolation design loop (`kct creepage --voltage-map`, `zones hv-keepout`, HV-aware placement, `/kct:hv-isolation-loop` skill) + via-in-pad manufacturability (`kct fix-vias` off-pad relocation), `analyze electrical-rating`, `--emit-dru`/`--emit-drc-constraints` rule-identity sidecars |
| **v0.18.0** (2026-07-20) | HV / analog manufacturing gates — `kct creepage` (surface-path creepage audit vs IEC 60664-1/62368-1) and `kct analyze current-sense` (analog layout lint). Plus real `--nets` route filter, `pcb reinforce` multi-branch anchoring, `route --layers auto` inner-layer advisory |
| **v0.17.0** (2026-07-17) | Experimental alternative routing substrate — adaptive octilinear **lattice** engine + constrained-Delaunay **navmesh/mesh** engine, both default-OFF; routes large mixed-pitch boards the grid can't fit in memory |
| **v0.16.0** (2026-07-15) | Region-bounded routing, ampacity-aware net-class min-width + DRC (IPC-2221), copper dedupe, `pcb reinforce` anchor-PTH rows, `net-status --strict` real-geometry connectivity, off-board preflight |
| **v0.15.0** (2026-07-13) | Router feasibility certificates + constructive escape ordering, coupled diff-pair corridor attractors + C++ joint-state A\* port, copper-LVS gates across boards 01–07, LCSC/EasyEDA + cross-library 3D model resolver tiers |
| **v0.14.0** (2026-06-16) | Demo gallery website (kicad-tools.org), zone-fill foreign-pad clearance fix, PCB `page_fit`, oblique 3D + 2D-SVG renders, gallery LVS status, ERC/LVS/Manifest meta sub-checks for `kct check` |

**LVS Soundness Epic — COMPLETE (2026-06-17).** Independent copper-LVS soundness epic (motivated by #3742), shipped via a `/loom:sweep` run processing 16 PRs: gate + extractor hardening (independent copper-extracted LVS gate, label-free pour extraction, per-zone pour-pad bonding, foreign-net track-segment carve, layer-aware segment chaining), fleet rollout across boards 00–07, and real defects the gate surfaced and fixed on boards 02/03/04 that DRC had missed.
