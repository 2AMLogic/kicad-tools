# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-27*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#10253**: fix: translate reinforcement sidecar with pcb center-on-sheet
- **#10474**: feat(export): report revision-bound simulation limits and fault-case evidence
- **#10497**: ci: retry the pinned actionlint download on a transient 504
- **#10520**: fix(route-link): resolve the non-plated hole clearance through the board's rules
- **#10526**: chore(loom): resync installed Loom to v0.20.37
- **#10532**: Expand BOM reference intervals with matching designator suffixes

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

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#8486**: Reduce repeated full-budget reroutes of six narrow nets on the 97-net board
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410)
- **#9939**: Repair the five remaining Board 03 mask-to-copper findings after verified refill
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film
- **#10250**: pcb center-on-sheet moves wire anchors but leaves reinforcement coordinates at their old positions
- **#10261**: Fill the routed board before optional power-plane stitching on mixed-signal layouts
- **#10416**: Validate repeated circuit channels against expected pins, values and assembly parts
- **#10424**: BOM enrichment assigns incompatible packages, tolerance and wattage despite explicit schematic intent
- **#10448**: Reconcile LED_ANODE and mask clearances so Board 00 readiness can be re-pinned
- **#10451**: Restore Board 07 SDRAM validation and evidence publication under the reviewed net classes
- **#10468**: Manufacturing review: include revision-bound simulation limits and fault-case evidence
- **#10473**: Assembly preflight: validate soldered wire or ferrule fit without assigning the wire land a component MPN
- **#10476**: Assembly preflight: check snap-in mounting-hole limits against both finished-hole tolerances
- **#10485**: CI flake: Install pinned actionlint download returned HTTP 504 in Test
- **#10486**: Route-link option to join an existing same-net branch outside a component pad
- **#10492**: Offer an export mode that verifies reviewed copper before publishing manufacturing artwork
- **#10504**: route-link: non-plated-hole clearance ignores the board's hole_clearance rule
- **#10505**: Request an MPN-specific SMD land-pattern check in assembly preflight
- **#10510**: create-pr.sh exits 126 on hosts with a config tier over 128 KiB: installed config resolver predates loom#15313
- **#10514**: Manufacturing handoff: map PCB wire landings to the purchased panel part’s terminals
- **#10515**: reference-coverage reports coverage: "none" on layers a net only passes through via
- **#10525**: Test flake in test_escape_budget_expires_returns_early on CI
- **#10527**: Check simulated analog pin ranges across the driven waveform and supply states
- **#10543**: test_drc_constraints_export::test_subspec_standard_via_fires_under_kicad_cli fails on main: board 04 via not found at the derived position
- **#10552**: Pin the unverified-shortfall + unresolved-code row of the #10523 stock table
- **#10555**: route: a pre-existing net class wider than its router width keeps the wider value, so _write_netclasses_to_project cannot declare a narrower class
- **#10556**: tests: board05 staged fixture is module-scoped under xdist --dist load, so it rebuilds per worker and its cost lands on a 60s-capped test
- **#10559**: Reinforcement sidecar consumers raise raw TypeError on a malformed nested coordinate point
- **#10561**: Board 03 USB common-run measurement depends on where an unchanged track is split

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#10128**: fix(validate): stop pour_slot reporting packed parallel track pairs
- **#10262**: fix(route): stitch power planes after the routed copper is refilled
- **#10427**: Reject incompatible automatic parts in assembly BOM exports
- **#10429**: Write native symbol links when creating a PCB from a schematic
- **#10459**: fix(boards): align board 00's LED_ANODE net class and fiducial pours
- **#10472**: fix: carry a pad-edge escape end into the pad along its own line
- **#10488**: Warn when native KiCad BOM export changes a component reference
- **#10491**: feat: assess declared wire and ferrule landings in the lead-fit preflight check
- **#10496**: Include project-local 3D libraries in manufacturing archives
- **#10498**: feat: check declared mounting holes against their drawing and finished-hole interval
- **#10502**: Check electrical compatibility and identify catalog inventory in parts alternatives
- **#10503**: Wire authorized distributor stock lookup through kct parts
- **#10507**: fix(boards): declare Board 07's reviewed class widths and re-pin its SDRAM evidence
- **#10513**: feat: verify MPN-specific two-terminal SMD land patterns in preflight
- **#10518**: Reject simulation results with undeclared measured inputs
- **#10519**: feat: add route-link --join-track to tee into a pad's track
- **#10533**: Recognize the Docker platform notice during native solder-mask export
- **#10534**: fix: report not_applicable for via-only layers in reference-coverage
- **#10536**: feat: check simulated analog pin voltages against supply-state limits
- **#10537**: feat: carry purchasing-row terminal maps into the assembly handoff
- **#10544**: perf(router): prove a timed-out connection from its goal side before repeating its search
- **#10545**: feat: add kct export --verify-reviewed-copper to publish only reviewed copper
- **#10546**: test: check the edited Board 04 via in native DRC
- **#10554**: test: cover stock shortages alongside unresolved BOM items
- **#10558**: fix(route): lower kct-created net classes to the routed width
- **#10560**: ci: group Board 05 recipe staging on one worker

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#10253**: fix: translate reinforcement sidecar with pcb center-on-sheet
- **#10435**: feat: validate repeated circuit channels against explicit design intent
- **#10474**: feat(export): report revision-bound simulation limits and fault-case evidence
- **#10497**: ci: retry the pinned actionlint download on a transient 504
- **#10520**: fix(route-link): resolve the non-plated hole clearance through the board's rules
- **#10526**: chore(loom): resync installed Loom to v0.20.37
- **#10528**: test: make escape-budget expiration deterministic under load
- **#10532**: Expand BOM reference intervals with matching designator suffixes

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
- **#10385**: Refresh maintained board schematics after the instance Datasheet fix *(curated)*
- **#10416**: Validate repeated circuit channels against expected pins, values and assembly parts *(curated)*
- **#10424**: BOM enrichment assigns incompatible packages, tolerance and wattage despite explicit schematic intent *(curated)*
- **#10428**: Preserve native schematic instance links on the footprints placed by create-pcb *(curated)*
- **#10448**: Reconcile LED_ANODE and mask clearances so Board 00 readiness can be re-pinned *(curated)*
- **#10451**: Restore Board 07 SDRAM validation and evidence publication under the reviewed net classes *(curated)*
- **#10468**: Manufacturing review: include revision-bound simulation limits and fault-case evidence *(curated)*
- **#10473**: Assembly preflight: validate soldered wire or ferrule fit without assigning the wire land a component MPN *(curated)*
- **#10476**: Assembly preflight: check snap-in mounting-hole limits against both finished-hole tolerances *(curated)*
- **#10485**: CI flake: Install pinned actionlint download returned HTTP 504 in Test *(curated)*
- **#10486**: Route-link option to join an existing same-net branch outside a component pad *(curated)*
- **#10492**: Offer an export mode that verifies reviewed copper before publishing manufacturing artwork *(curated)*
- **#10495**: Export and readiness omit project-local .3dshapes libraries from the KiCad project archive *(curated)*
- **#10499**: parts alternatives offers a 30 Ω resistor for 4.7 kΩ and adjustable regulators for a fixed 3.3 V part *(curated)*
- **#10501**: Public kct parts stock command is unreachable from the root parser and dispatch *(curated)*
- **#10504**: route-link: non-plated-hole clearance ignores the board's hole_clearance rule *(curated)*
- **#10505**: Request an MPN-specific SMD land-pattern check in assembly preflight *(curated)*
- **#10510**: create-pr.sh exits 126 on hosts with a config tier over 128 KiB: installed config resolver predates loom#15313 *(curated)*
- **#10514**: Manufacturing handoff: map PCB wire landings to the purchased panel part’s terminals *(curated)*
- **#10515**: reference-coverage reports coverage: "none" on layers a net only passes through via *(curated)*
- **#10525**: Test flake in test_escape_budget_expires_returns_early on CI *(curated)*
- **#10527**: Check simulated analog pin ranges across the driven waveform and supply states *(curated)*
- **#10530**: Expand BOM numeric reference ranges with a shared suffix such as Q1A-Q4A *(curated)*
- **#10531**: Docker platform notice invalidates a successful native solder-mask export *(curated)*
- **#10543**: test_drc_constraints_export::test_subspec_standard_via_fires_under_kicad_cli fails on main: board 04 via not found at the derived position *(curated)*
- **#10552**: Pin the unverified-shortfall + unresolved-code row of the #10523 stock table *(curated)*
- **#10555**: route: a pre-existing net class wider than its router width keeps the wider value, so _write_netclasses_to_project cannot declare a narrower class *(curated)*
- **#10556**: tests: board05 staged fixture is module-scoped under xdist --dist load, so it rebuilds per worker and its cost lands on a 60s-capped test *(curated)*
- **#10559**: Reinforcement sidecar consumers raise raw TypeError on a malformed nested coordinate point *(curated)*
- **#10561**: Board 03 USB common-run measurement depends on where an unchanged track is split *(curated)*

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
| Operator merge-risk holds | 6 |
| Very important | 1 |
| Important | 3 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 30 |
| PRs awaiting review | 27 |
| Approved PRs awaiting merge | 8 |
| Curated | 56 |
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
