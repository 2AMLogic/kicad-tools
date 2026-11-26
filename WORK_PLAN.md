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
- **#9939**: Repair the five remaining Board 03 mask-to-copper findings after verified refill
- **#10451**: Restore Board 07 SDRAM validation and evidence publication under the reviewed net classes

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#10393**: Route links around non-plated mounting holes when a straight trace would cross the drill
- **#10451**: Restore Board 07 SDRAM validation and evidence publication under the reviewed net classes

## In Progress

Issues currently being built (`loom:building`).

- **#9406**: Converge boards 06/07 DRC re-check into the single-sourced job scripts (latent local-gate --full failure)
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410)
- **#9939**: Repair the five remaining Board 03 mask-to-copper findings after verified refill
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film
- **#10250**: pcb center-on-sheet moves wire anchors but leaves reinforcement coordinates at their old positions
- **#10261**: Fill the routed board before optional power-plane stitching on mixed-signal layouts
- **#10265**: Board 03 tier-1 USB_D+ route exceeds its 16 mm branch length limit
- **#10336**: Request an explicit routing corridor on an inner layer with a partial plane
- **#10358**: kct creepage --exceptions: docs say an unmatched selector is an error (it warns), and the gate summary double-counts a partially waived row
- **#10364**: Identify verified reinforcement wire-end holes when reviewing native KiCad dangling-via warnings
- **#10385**: Refresh maintained board schematics after the instance Datasheet fix
- **#10416**: Validate repeated circuit channels against expected pins, values and assembly parts
- **#10419**: kct pcb reinforce --clear writes the board before drop_wires fails on an unreadable sidecar envelope
- **#10421**: Board 01 release regeneration narrows nine Power tracks below its declared minimum trace width
- **#10422**: Export preflight: measure reinforcement mask strips against covered and exposed foreign copper in Gerbers
- **#10424**: BOM enrichment assigns incompatible packages, tolerance and wattage despite explicit schematic intent
- **#10430**: Reconcile 32 net-class findings blocking the Board 05 revision-B manufacturing package
- **#10431**: Export a supplementary procurement list for accessories and off-board assembly parts
- **#10448**: Reconcile LED_ANODE and mask clearances so Board 00 readiness can be re-pinned
- **#10449**: Finalize Board 04 silkscreen before binding the manufacturing DRC report
- **#10450**: Reconcile Board 06 LVDS and LVTTL project widths and re-pin the assembled demo
- **#10457**: Coupling test misses the inner-layer widths published in the current-paths cases section
- **#10458**: Intermittent empty staged-board path in the lint-gate SIGTERM test

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#10128**: fix(validate): stop pour_slot reporting packed parallel track pairs
- **#10248**: fix(fix-drc): guard mask-copper repairs against new warnings
- **#10272**: fix(router): shorten aligned differential-pair pad hops
- **#10375**: feat: route links through bounded inner-plane corridors
- **#10378**: feat: identify verified wire ends in native dangling-via reports
- **#10410**: fix: recognize round pad cutouts in radial polarity artwork
- **#10418**: fix: recognize visible pin-one marks at the rim of radial capacitors
- **#10426**: Recognize native root-relative footprint links in editable project handoffs
- **#10427**: Reject incompatible automatic parts in assembly BOM exports
- **#10429**: Write native symbol links when creating a PCB from a schematic
- **#10432**: fix: refuse reinforce --clear on an unreadable destination sidecar
- **#10435**: feat: validate repeated circuit channels against explicit design intent
- **#10443**: feat: measure reinforcement runs against the shipped Gerbers in export preflight
- **#10444**: feat: export a supplementary purchasing list for off-board parts
- **#10446**: fix(board01): author the Power net class so release cleanup keeps the 0.5 mm tracks
- **#10456**: chore(boards): refresh datasheets on four maintained schematics
- **#10459**: fix(boards): align board 00's LED_ANODE net class and fiducial pours
- **#10460**: docs(reference): cover inner-layer widths and unevaluated cases
- **#10462**: fix: count creepage exception rows by their final verdict
- **#10463**: fix(board06): declare LVTTL, LVDS and Power widths in source project
- **#10464**: test: publish lint-gate readiness atomically before signaling
- **#10465**: ci: restore strict release DRC on regenerated Boards 06 and 07

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#10253**: fix: translate reinforcement sidecar with pcb center-on-sheet

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
- **#9939**: Repair the five remaining Board 03 mask-to-copper findings after verified refill *(curated)*
- **#10105**: Support a repository layout with editable KiCad sources and a sibling generated-output directory *(curated)*
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film *(curated)*
- **#10250**: pcb center-on-sheet moves wire anchors but leaves reinforcement coordinates at their old positions *(curated)*
- **#10261**: Fill the routed board before optional power-plane stitching on mixed-signal layouts *(curated)*
- **#10265**: Board 03 tier-1 USB_D+ route exceeds its 16 mm branch length limit *(curated)*
- **#10336**: Request an explicit routing corridor on an inner layer with a partial plane *(curated)*
- **#10358**: kct creepage --exceptions: docs say an unmatched selector is an error (it warns), and the gate summary double-counts a partially waived row *(curated)*
- **#10364**: Identify verified reinforcement wire-end holes when reviewing native KiCad dangling-via warnings *(curated)*
- **#10385**: Refresh maintained board schematics after the instance Datasheet fix *(curated)*
- **#10393**: Route links around non-plated mounting holes when a straight trace would cross the drill *(curated)*
- **#10409**: Radial-capacitor hatch warnings return after clipping strokes to a round pad clearance *(curated)*
- **#10416**: Validate repeated circuit channels against expected pins, values and assembly parts *(curated)*
- **#10417**: Pin-1 check misses the visible positive mark on stock radial electrolytics *(curated)*
- **#10419**: kct pcb reinforce --clear writes the board before drop_wires fails on an unreadable sidecar envelope *(curated)*
- **#10421**: Board 01 release regeneration narrows nine Power tracks below its declared minimum trace width *(curated)*
- **#10422**: Export preflight: measure reinforcement mask strips against covered and exposed foreign copper in Gerbers *(curated)*
- **#10424**: BOM enrichment assigns incompatible packages, tolerance and wattage despite explicit schematic intent *(curated)*
- **#10425**: Editable project audit expects an extra schematic root UUID in native PCB footprint links *(curated)*
- **#10430**: Reconcile 32 net-class findings blocking the Board 05 revision-B manufacturing package *(curated)*
- **#10431**: Export a supplementary procurement list for accessories and off-board assembly parts *(curated)*
- **#10445**: Board 01's committed routed PCB is behind its recipe: a fresh regen moves the board-ID silkscreen and rewrites fiducial attributes and segment UUIDs *(curated)*
- **#10448**: Reconcile LED_ANODE and mask clearances so Board 00 readiness can be re-pinned *(curated)*
- **#10449**: Finalize Board 04 silkscreen before binding the manufacturing DRC report *(curated)*
- **#10450**: Reconcile Board 06 LVDS and LVTTL project widths and re-pin the assembled demo *(curated)*
- **#10451**: Restore Board 07 SDRAM validation and evidence publication under the reviewed net classes *(curated)*
- **#10457**: Coupling test misses the inner-layer widths published in the current-paths cases section *(curated)*
- **#10458**: Intermittent empty staged-board path in the lint-gate SIGTERM test *(curated)*

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
| Important | 3 |
| Ready (`loom:issue`) | 2 |
| In Progress (`loom:building`) | 24 |
| PRs awaiting review | 23 |
| Approved PRs awaiting merge | 1 |
| Curated | 50 |
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
