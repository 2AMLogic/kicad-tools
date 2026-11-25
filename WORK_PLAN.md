# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-25*

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

- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper

## In Progress

Issues currently being built (`loom:building`).

- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410)
- **#9891**: Board 06: repair the seed-42 recipe to reproduce its strict release DRC result
- **#9939**: Repair Board 03’s 16 remaining mask-to-copper findings while preserving legal trace angles
- **#10097**: Board 06: sharpen the PCIe taper authored-rule guard's keepout regression and its two fail-closed edges
- **#10106**: Resolve a project-declared output directory during readiness staging and publication
- **#10108**: Package a native KiCad project with a coherent basename and revision identity
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film
- **#10125**: test flake: Board 06 permitted-width rule-area refill reaches the 60-second bulk timeout
- **#10134**: check: report bypass capacitor count per power pin (redundant or missing decoupling)
- **#10136**: route: avoid power-plane stitches on SMD pads already bonded by filled copper
- **#10137**: check: report pad stubs proven redundant by refilled pour connectivity
- **#10139**: netclasses: check sidecar/project/track-width agreement and honour project class widths on re-route
- **#10141**: check: bypass_distance rule using schematic cap-pin association
- **#10142**: placement: use schematic cap-pin association as optimizer constraint
- **#10143**: schematic/blocks: take decoupling-cap counts from one place so blocks do not stack default caps on a shared rail
- **#10145**: sch re-annotate: carry the renumbering mapping to the .kicad_pcb (--pcb)
- **#10171**: sch re-annotate: report reversed reference order across LED and resistor pairs
- **#10173**: 0.1016 mm traces pass the jlcpcb profile; JLCDFM reports Danger at 0.1 mm
- **#10185**: test flake: loader-isolation test asserts a global sys.modules absence that any generate_design importer breaks
- **#10189**: router/optimize-traces: honour project netclass track widths on re-route
- **#10198**: JLCPCB CPL: SOT-23-5 parts come out a quarter turn off (the "SOT-23*": 180 rotation entry is wrong for the 5-pin package)

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#10115**: feat(readiness): resolve a project-declared output root for staging and publication
- **#10120**: fix(export): package the KiCad project under the root sheet's basename
- **#10128**: fix(validate): stop pour_slot reporting packed parallel track pairs
- **#10129**: ci: run Board 06 rule-area tests in a serial lane with a 120 s budget
- **#10149**: feat: sync footprint references from sch re-annotate via --pcb
- **#10156**: feat(check): report pad branches proven redundant by refilled pours
- **#10157**: feat(schematic): report circuit block decoupling by supply net
- **#10167**: fix: skip power-plane stitches for SMD pads bonded to the plane by current fill
- **#10178**: feat: warn on JLCPCB 4-layer outer tracks JLCDFM grades Danger
- **#10184**: feat(placement): pair decoupling caps by their schematic designation
- **#10186**: feat(check): add bypass_distance rule using the schematic cap-to-pin association
- **#10188**: feat: report reversed schematic references across electrical partners
- **#10190**: feat(check): add netclass_agreement rule for sidecar/project/copper width agreement
- **#10191**: test: check recipe loader isolation for preloaded modules
- **#10193**: fix: honour project netclass widths in route and optimize-traces
- **#10199**: fix(export): apply verified JLCPCB rotations by catalog part
- **#10201**: feat(explain): inventory decoupling caps by schematic designation

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

_None._

## Proposed

Issues carrying `loom:curated`.

- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#7856**: [Epic #6441] Phase 5 measurement: coupled-trunk retention >= 95% on boards 03, 06b, 06rf, 07 *(curated)*
- **#8486**: Reduce repeated full-budget reroutes of six narrow nets on the 97-net board *(curated)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)*
- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(curated)*
- **#8943**: Record JLCDFM silkscreen line-width selection criterion and per-item rows (residue of #8844) *(curated)*
- **#8962**: Same-net slit population on fresh regens: repair-pass + route-side producers (stale fixture attribution from #8957) *(curated)*
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
- **#9891**: Board 06: repair the seed-42 recipe to reproduce its strict release DRC result *(curated)*
- **#9893**: docs: surface JLCPCB assembly and submission automation on kicad-tools.org and in the README *(curated)*
- **#9921**: Repair Board 05’s reviewed revision-B copper and regenerate its manufacturing package *(curated)*
- **#9939**: Repair Board 03’s 16 remaining mask-to-copper findings while preserving legal trace angles *(curated)*
- **#10097**: Board 06: sharpen the PCIe taper authored-rule guard's keepout regression and its two fail-closed edges *(curated)*
- **#10105**: Support a repository layout with editable KiCad sources and a sibling generated-output directory *(curated)*
- **#10106**: Resolve a project-declared output directory during readiness staging and publication *(curated)*
- **#10108**: Package a native KiCad project with a coherent basename and revision identity *(curated)*
- **#10121**: Verify the nine Board 05 pour-slot warnings against the native copper film *(curated)*
- **#10125**: test flake: Board 06 permitted-width rule-area refill reaches the 60-second bulk timeout *(curated)*
- **#10134**: check: report bypass capacitor count per power pin (redundant or missing decoupling) *(curated)*
- **#10136**: route: avoid power-plane stitches on SMD pads already bonded by filled copper *(curated)*
- **#10137**: check: report pad stubs proven redundant by refilled pour connectivity *(curated)*
- **#10139**: netclasses: check sidecar/project/track-width agreement and honour project class widths on re-route *(curated)*
- **#10141**: check: bypass_distance rule using schematic cap-pin association *(curated)*
- **#10142**: placement: use schematic cap-pin association as optimizer constraint *(curated)*
- **#10143**: schematic/blocks: take decoupling-cap counts from one place so blocks do not stack default caps on a shared rail *(curated)*
- **#10145**: sch re-annotate: carry the renumbering mapping to the .kicad_pcb (--pcb) *(curated)*
- **#10171**: sch re-annotate: report reversed reference order across LED and resistor pairs *(curated)*
- **#10173**: 0.1016 mm traces pass the jlcpcb profile; JLCDFM reports Danger at 0.1 mm *(curated)*
- **#10185**: test flake: loader-isolation test asserts a global sys.modules absence that any generate_design importer breaks *(curated)*
- **#10189**: router/optimize-traces: honour project netclass track widths on re-route *(curated)*
- **#10198**: JLCPCB CPL: SOT-23-5 parts come out a quarter turn off (the "SOT-23*": 180 rotation entry is wrong for the 5-pin package) *(curated)*

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
| Ready (`loom:issue`) | 3 |
| In Progress (`loom:building`) | 21 |
| PRs awaiting review | 18 |
| Approved PRs awaiting merge | 0 |
| Curated | 44 |
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
