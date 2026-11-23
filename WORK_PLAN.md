# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-22*

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

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI

## In Progress

Issues currently being built (`loom:building`).

- **#8486**: Reduce repeated full-budget reroutes of six narrow nets on the 97-net board
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410)
- **#9800**: Boards 02/03/05: drop per-board silk wiring and retire silk tolerances (#9795 phase 4)
- **#9892**: Board 07: reconcile the seed-42 recipe and reviewed release under the manufacturing gate
- **#9921**: Repair Board 05’s reviewed revision-B copper and regenerate its manufacturing package
- **#9939**: fix-drc mask-copper: 41 board-03 findings declined for structural reasons (junction/clearance/no-end classes)
- **#9958**: fix-drc aborts the mask repair batch while undoing two nudges that share a track endpoint
- **#9959**: _run_drc_once: the flag-less DRC retry can inherit the first attempt's report
- **#9966**: Add a negative control for the segment arm of reset_escape's direct-contact guard
- **#9972**: kct zones fill --output: thermal_asymmetry warning names the discarded temp candidate, not the output board
- **#9973**: Harden the entry-copper checkpoint/resume regression's vacuity guards
- **#9985**: Make the native zone-fill probe controls reliable in CI and count saved opens from KiCad’s report
- **#9987**: Keep the Board 06 JLCPCB Gerbers and project ZIP in sync with the zone clearances added during export
- **#9989**: Clear Board 06 U4 reset and USB-C configuration-channel pad departures in the nine-pair recipe
- **#9990**: fix: a named-stackup export fails with 'PCB changed during manufacturing export' when the silk pass edits the board
- **#9995**: test flake: route silk opt-out regression times out in the native post-route connectivity check
- **#10003**: Board 06 route-only CI leaves 12 silkscreen findings on the nine-pair fixture
- **#10005**: test flake: input pad-overlap CLI test times out in the post-route connectivity check
- **#10007**: Board 06 skips its first pour repair when stitching writes named segment and via nets

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#9977**: perf(router): scope the known-failure search witness to reachable copper
- **#9981**: test(board06): cover a track-only contact on the reset escape
- **#9982**: fix(zones): name the published board in the thermal relief hint
- **#9986**: test: count KiCad 10 unconnected_items and fit the zone-fill probe controls into the zones budget
- **#9991**: fix: refill saved zone fills when export changes the rules they were filled under
- **#9992**: fix: re-bind stackup provenance to the silk pass's written bytes
- **#9993**: fix(board07): clear fixture pad exits and verify the finished manufacturing bundle
- **#9996**: test: bound the silk opt-out routing regression
- **#9998**: fix: search alternate via displacements in the verified mask-copper repair
- **#10000**: chore(deps): bump the uv-minor-patch group across 1 directory with 4 updates
- **#10001**: chore(board02): legalize silk through the shared default pass
- **#10004**: fix(board06): finish route-only silkscreen before the regression gate
- **#10006**: test: bound the input pad-overlap CLI regression
- **#10008**: fix(board06): resolve stitched net references in pour repair

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#9976**: fix: clear the DRC report path before every kicad-cli launch

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#7856**: [Epic #6441] Phase 5 measurement: coupled-trunk retention >= 95% on boards 03, 06b, 06rf, 07 *(curated)*
- **#8486**: Reduce repeated full-budget reroutes of six narrow nets on the 97-net board *(curated)*
- **#8610**: route --complete chain pass commits ~282 shorts against Net 0 copper on dense mains board (rc 4) *(curated)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)*
- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(curated)*
- **#8943**: Record JLCDFM silkscreen line-width selection criterion and per-item rows (residue of #8844) *(curated)*
- **#8962**: Same-net slit population on fresh regens: repair-pass + route-side producers (stale fixture attribution from #8957) *(curated)*
- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors) *(curated)*
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06) *(curated)*
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings *(curated)*
- **#9058**: Board 01: re-pin readiness evidence after PR #8848 on a host where mask_to_copper completes *(curated)*
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
- **#9850**: Board 06 seed 42: trace the nine remaining pure-Python routing failures *(curated)*
- **#9891**: Board 06: repair the seed-42 recipe to reproduce its strict release DRC result *(curated)*
- **#9892**: Board 07: reconcile the seed-42 recipe and reviewed release under the manufacturing gate *(curated)*
- **#9921**: Repair Board 05’s reviewed revision-B copper and regenerate its manufacturing package *(curated)*
- **#9939**: fix-drc mask-copper: 41 board-03 findings declined for structural reasons (junction/clearance/no-end classes) *(curated)*
- **#9958**: fix-drc aborts the mask repair batch while undoing two nudges that share a track endpoint *(curated)*
- **#9959**: _run_drc_once: the flag-less DRC retry can inherit the first attempt's report *(curated)*
- **#9966**: Add a negative control for the segment arm of reset_escape's direct-contact guard *(curated)*
- **#9972**: kct zones fill --output: thermal_asymmetry warning names the discarded temp candidate, not the output board *(curated)*
- **#9973**: Harden the entry-copper checkpoint/resume regression's vacuity guards *(curated)*
- **#9987**: Keep the Board 06 JLCPCB Gerbers and project ZIP in sync with the zone clearances added during export *(curated)*
- **#9989**: Clear Board 06 U4 reset and USB-C configuration-channel pad departures in the nine-pair recipe *(curated)*
- **#9990**: fix: a named-stackup export fails with 'PCB changed during manufacturing export' when the silk pass edits the board *(curated)*
- **#9995**: test flake: route silk opt-out regression times out in the native post-route connectivity check *(curated)*
- **#10003**: Board 06 route-only CI leaves 12 silkscreen findings on the nine-pair fixture *(curated)*
- **#10005**: test flake: input pad-overlap CLI test times out in the post-route connectivity check *(curated)*
- **#10007**: Board 06 skips its first pour repair when stitching writes named segment and via nets *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)
- **#8838**: Epic: fab DFM parity - a board that passes kct check --mfr jlcpcb must pass JLCPCB's DFM tool

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Very important | 1 |
| Important | 1 |
| Ready (`loom:issue`) | 1 |
| In Progress (`loom:building`) | 20 |
| PRs awaiting review | 15 |
| Approved PRs awaiting merge | 1 |
| Curated | 43 |
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
