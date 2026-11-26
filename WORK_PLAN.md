# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-26*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#10253**: fix: translate reinforcement sidecar with pcb center-on-sheet

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors)

## Important

`loom:important`; taken before unlabelled work.

- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9939**: Repair Board 03’s 16 remaining mask-to-copper findings while preserving legal trace angles

## Ready

Human-approved issues ready for implementation (`loom:issue`).

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410)
- **#9939**: Repair Board 03’s 16 remaining mask-to-copper findings while preserving legal trace angles
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film
- **#10250**: pcb center-on-sheet moves wire anchors but leaves reinforcement coordinates at their old positions
- **#10261**: Fill the routed board before optional power-plane stitching on mixed-signal layouts
- **#10265**: Board 03 tier-1 USB_D+ route exceeds its 16 mm branch length limit
- **#10294**: Board 06: clear the J1/B2 USB3_TX2+ mask overlap at its connector exit
- **#10299**: Assembly preflight should compare through-hole lead dimensions with the finished plated hole
- **#10317**: creepage: support reviewed per-pad-pair exceptions alongside the global same-footprint waiver
- **#10334**: Hierarchical net names are qualified only on collision, so an unrelated sheet's label renames a net
- **#10336**: Request an explicit routing corridor on an inner layer with a partial plane
- **#10345**: Verify copper repairs against each selected operating-state voltage map
- **#10357**: Report plated via overlaps with same-face assembly lands in PCBA exports
- **#10362**: Datasheet pitch extraction truncates a measurement when a letter follows the number or unit
- **#10364**: Identify verified reinforcement wire-end holes when reviewing native KiCad dangling-via warnings
- **#10368**: Summarize nominal Gerber mask exposure separately from near-pad fabrication findings
- **#10384**: Untented via mask apertures ignore the board's declared pad_to_mask_clearance
- **#10386**: Readiness evidence silently downgrades parts/stock verification when the producing host has no offline catalog
- **#10387**: Recognize Kelvin sense taps that exit a through via on an inner layer
- **#10396**: Document the current-paths sidecar's `cases` key in docs/reference/cli.md
- **#10398**: Reinforcement courtyard rule reads sidecar records without the isinstance guard its siblings use
- **#10401**: kct pcb reinforce raises a raw traceback on a sidecar entry that is not an object
- **#10408**: Readiness fails on a symlinked TMPDIR when staging the selected schematic

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#10128**: fix(validate): stop pour_slot reporting packed parallel track pairs
- **#10248**: fix(fix-drc): guard mask-copper repairs against new warnings
- **#10262**: fix(route): stitch power planes after the routed copper is refilled
- **#10272**: fix(router): shorten aligned differential-pair pad hops
- **#10286**: Report project DRC settings changed by manufacturing export
- **#10298**: fix(board06): clear the USB3_TX2+ connector exit at J1/B2
- **#10322**: fix: check fitted wire spans on mixed copper and wire paths
- **#10339**: Fix false current-path opens at offset via landings
- **#10344**: fix: respect project copper spacing in route-link search
- **#10373**: fix: qualify sheet-local net names independently of sibling sheets
- **#10378**: feat: identify verified wire ends in native dangling-via reports
- **#10392**: fix: expand untented via mask openings by pad_to_mask_clearance
- **#10400**: fix: recognise Kelvin sense taps leaving a through via on an inner layer
- **#10402**: fix: drop non-object entries from the read-only reinforcement sidecar loader
- **#10403**: feat: add opt-in via land overlap measurement to kct export
- **#10406**: fix: measure bypass distance across repeated pin lands
- **#10407**: fix: report unplanned findings in DRC repair previews
- **#10410**: fix: recognize round pad cutouts in radial polarity artwork

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#10253**: fix: translate reinforcement sidecar with pcb center-on-sheet
- **#10309**: feat: compare through-hole lead dimensions with the finished hole in preflight
- **#10363**: feat: verify mask repairs against named voltage maps
- **#10377**: fix: treat datasheet pitch tokens with an attached letter as ambiguous
- **#10381**: feat: report nominal mask exposure per plotted opening

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
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film *(curated)*
- **#10250**: pcb center-on-sheet moves wire anchors but leaves reinforcement coordinates at their old positions *(curated)*
- **#10261**: Fill the routed board before optional power-plane stitching on mixed-signal layouts *(curated)*
- **#10265**: Board 03 tier-1 USB_D+ route exceeds its 16 mm branch length limit *(curated)*
- **#10284**: Report project DRC settings rewritten by manufacturing export *(curated)*
- **#10294**: Board 06: clear the J1/B2 USB3_TX2+ mask overlap at its connector exit *(curated)*
- **#10299**: Assembly preflight should compare through-hole lead dimensions with the finished plated hole *(curated)*
- **#10317**: creepage: support reviewed per-pad-pair exceptions alongside the global same-footprint waiver *(curated)*
- **#10321**: Wire courtyard DRC treats copper-only legs of a partially wire-reinforced current path as fitted wire *(curated)*
- **#10334**: Hierarchical net names are qualified only on collision, so an unrelated sheet's label renames a net *(curated)*
- **#10336**: Request an explicit routing corridor on an inner layer with a partial plane *(curated)*
- **#10345**: Verify copper repairs against each selected operating-state voltage map *(curated)*
- **#10362**: Datasheet pitch extraction truncates a measurement when a letter follows the number or unit *(curated)*
- **#10364**: Identify verified reinforcement wire-end holes when reviewing native KiCad dangling-via warnings *(curated)*
- **#10368**: Summarize nominal Gerber mask exposure separately from near-pad fabrication findings *(curated)*
- **#10384**: Untented via mask apertures ignore the board's declared pad_to_mask_clearance *(curated)*
- **#10386**: Readiness evidence silently downgrades parts/stock verification when the producing host has no offline catalog *(curated)*
- **#10387**: Recognize Kelvin sense taps that exit a through via on an inner layer *(curated)*
- **#10398**: Reinforcement courtyard rule reads sidecar records without the isinstance guard its siblings use *(curated)*
- **#10401**: kct pcb reinforce raises a raw traceback on a sidecar entry that is not an object *(curated)*
- **#10404**: fix-drc dry run describes unsupported and skipped candidates as repairable *(curated)*
- **#10405**: Bypass distance depends on footprint pad order for regulators with a repeated pin number *(curated)*
- **#10408**: Readiness fails on a symlinked TMPDIR when staging the selected schematic *(curated)*
- **#10409**: Radial-capacitor hatch warnings return after clipping strokes to a round pad clearance *(curated)*

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
| Operator merge-risk holds | 1 |
| Very important | 1 |
| Important | 2 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 24 |
| PRs awaiting review | 19 |
| Approved PRs awaiting merge | 5 |
| Curated | 48 |
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
