# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-27*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#9711**: fix: routed-DRC identity gate fails on incomplete mask_to_copper pass, CI runs it pinned
- **#10427**: Reject incompatible automatic parts in assembly BOM exports
- **#10474**: feat(export): report revision-bound simulation limits and fault-case evidence
- **#10497**: ci: retry the pinned actionlint download on a transient 504
- **#10520**: fix(route-link): resolve the non-plated hole clearance through the board's rules
- **#10526**: chore(loom): resync installed Loom to v0.20.37
- **#10532**: Expand BOM reference intervals with matching designator suffixes
- **#10544**: perf(router): prove a timed-out connection from its goal side before repeating its search

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors)

## Important

`loom:important`; taken before unlabelled work.

- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#10451**: Restore Board 07 SDRAM validation and evidence publication under the reviewed net classes

## Ready

Human-approved issues ready for implementation (`loom:issue`).

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#8486**: Reduce repeated full-budget reroutes of six narrow nets on the 97-net board
- **#9381**: Committed demo-board BOMs still carry the pre-#9323 duplicate-LCSC lines
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410)
- **#10424**: BOM enrichment assigns incompatible packages, tolerance and wattage despite explicit schematic intent
- **#10451**: Restore Board 07 SDRAM validation and evidence publication under the reviewed net classes
- **#10468**: Manufacturing review: include revision-bound simulation limits and fault-case evidence
- **#10485**: CI flake: Install pinned actionlint download returned HTTP 504 in Test
- **#10504**: route-link: non-plated-hole clearance ignores the board's hole_clearance rule
- **#10510**: create-pr.sh exits 126 on hosts with a config tier over 128 KiB: installed config resolver predates loom#15313
- **#10604**: Default route-link misses a rotated, offset plated source pad
- **#10611**: Board 03 exporter appends a second rules filename that makes the editable project fail readiness
- **#10645**: mask_to_copper reports a new error when a straight own-pad entry is split at the pad edge
- **#10650**: DRC preflight counts informational findings as warnings in a manufacturing manifest
- **#10662**: Board 04 reports ready in fleet status but fails public readiness on the same PCB
- **#10664**: Board 04: clear the ten same-net mask_to_copper errors the public readiness check reports
- **#10667**: Board 03: 90 redundant_copper warnings outlive the mask-copper repair and readiness's cleanup, so the reissue cannot reach ready
- **#10669**: pad_outline_is_exact() misses a single-corner chamfer, so pad copper is over-reported
- **#10677**: Make the sharp-corner check's 89-degree boundary consistent on macOS and Linux
- **#10681**: Installed help, HV, readiness and tapeout skills point to a missing model hint
- **#10683**: Remaining check_all callers still grade pour_slot without the board's clearance rules
- **#10684**: Board 04's committed DRC evidence records 5 pour_slot warnings; a fresh pinned run reportedly gives 4
- **#10692**: No gate compares a committed board bundle's per-rule warning counts to a fresh run (Board 04 pour_slot 5 vs 4 went unnoticed)
- **#10703**: Six other installed kct skills still assert a frontmatter suggestedModel that Codex and opencode strip
- **#10704**: sharp_trace_corner: 'N-segment junction' text now counts directions, not segments

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#10507**: fix(boards): declare Board 07's reviewed class widths and re-pin its SDRAM evidence
- **#10608**: fix: land named route-link endpoints on the pad copper outline
- **#10653**: fix: read a pad-edge-split own-pad entry through its authored continuation
- **#10673**: fix(drc): count only warning-severity findings as warnings
- **#10674**: fix(geometry): cut chamfered corners from the shared pad copper outline
- **#10687**: fix(drc): repair pour, fragment and boxed-via mask_to_copper findings; re-issue Board 04
- **#10691**: fix: guard the sharp-corner 89-degree boundary against float rounding
- **#10693**: fix(boards): refresh Board 04 bundle so its pour_slot count matches a fresh pinned run
- **#10697**: feat(scripts): add --check drift gate to refresh_bundles_6404.py
- **#10698**: feat: add fix-drc --only redundant-copper and run it in the readiness cleanup
- **#10700**: fix: resolve pour_slot clearances from the board project in DRCChecker
- **#10705**: docs: clarify model selection in the tapeout, readiness, HV-loop and help skills

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#9711**: fix: routed-DRC identity gate fails on incomplete mask_to_copper pass, CI runs it pinned
- **#10427**: Reject incompatible automatic parts in assembly BOM exports
- **#10474**: feat(export): report revision-bound simulation limits and fault-case evidence
- **#10497**: ci: retry the pinned actionlint download on a transient 504
- **#10518**: Reject simulation results with undeclared measured inputs
- **#10520**: fix(route-link): resolve the non-plated hole clearance through the board's rules
- **#10526**: chore(loom): resync installed Loom to v0.20.37
- **#10532**: Expand BOM reference intervals with matching designator suffixes
- **#10544**: perf(router): prove a timed-out connection from its goal side before repeating its search
- **#10668**: fix(readiness): publish the public verdict for boards with a board-local check
- **#10670**: fix(boards): group Board 06 connector headers in the JLCPCB BOM
- **#10686**: docs: describe model selection in the installed workflow skills

## Proposed

Issues carrying `loom:curated`.

- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-10-19 (#7457 / #7084 Q10) *(curated)*
- **#7856**: [Epic #6441] Phase 5 measurement: coupled-trunk retention >= 95% on boards 03, 06b, 06rf, 07 *(curated)*
- **#8486**: Reduce repeated full-budget reroutes of six narrow nets on the 97-net board *(curated)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)*
- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(curated)*
- **#8943**: Record JLCDFM silkscreen line-width selection criterion and per-item rows (residue of #8844) *(curated)*
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI *(curated)*
- **#9093**: submit-jlcpcb: attended, exact-total-approved JLC parts purchase leg *(curated)*
- **#9298**: Repository moved to the 2AMLogic organization: update hard-coded owner slug and the PyPI trusted publisher *(curated)*
- **#9381**: Committed demo-board BOMs still carry the pre-#9323 duplicate-LCSC lines *(curated)*
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper *(curated)*
- **#9454**: No migration path for evidence-bound waivers across the ev3 -> ev4 bump: every consumer waiver goes stale *(curated)*
- **#9464**: Boards 03, 07 and 09 have parts off the 0.1 mm grid that pad_grid now reports (surfaced by #9434) *(curated)*
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410) *(curated)*
- **#9748**: optimize-traces --drc-aware --mfr jlcpcb introduces mask_to_copper errors at pad entries that the un-optimised board does not have *(curated)*
- **#9800**: Boards 02/03/05: drop per-board silk wiring and retire silk tolerances (#9795 phase 4) *(curated)*
- **#9893**: docs: surface JLCPCB assembly and submission automation on kicad-tools.org and in the README *(curated)*
- **#10105**: Support a repository layout with editable KiCad sources and a sibling generated-output directory *(curated)*
- **#10385**: Refresh maintained board schematics after the instance Datasheet fix *(curated)*
- **#10424**: BOM enrichment assigns incompatible packages, tolerance and wattage despite explicit schematic intent *(curated)*
- **#10451**: Restore Board 07 SDRAM validation and evidence publication under the reviewed net classes *(curated)*
- **#10468**: Manufacturing review: include revision-bound simulation limits and fault-case evidence *(curated)*
- **#10485**: CI flake: Install pinned actionlint download returned HTTP 504 in Test *(curated)*
- **#10504**: route-link: non-plated-hole clearance ignores the board's hole_clearance rule *(curated)*
- **#10510**: create-pr.sh exits 126 on hosts with a config tier over 128 KiB: installed config resolver predates loom#15313 *(curated)*
- **#10530**: Expand BOM numeric reference ranges with a shared suffix such as Q1A-Q4A *(curated)*
- **#10575**: Preserve Board 03’s connector and MCU USB widths in the routed KiCad project *(curated)*
- **#10604**: Default route-link misses a rotated, offset plated source pad *(curated)*
- **#10611**: Board 03 exporter appends a second rules filename that makes the editable project fail readiness *(curated)*
- **#10645**: mask_to_copper reports a new error when a straight own-pad entry is split at the pad edge *(curated)*
- **#10647**: config-resolver.sh passes config tiers as single jq arguments, so a >128 KiB defaults tier breaks create-issue.sh on Linux *(curated)*
- **#10650**: DRC preflight counts informational findings as warnings in a manufacturing manifest *(curated)*
- **#10664**: Board 04: clear the ten same-net mask_to_copper errors the public readiness check reports *(curated)*
- **#10667**: Board 03: 90 redundant_copper warnings outlive the mask-copper repair and readiness's cleanup, so the reissue cannot reach ready *(curated)*
- **#10669**: pad_outline_is_exact() misses a single-corner chamfer, so pad copper is over-reported *(curated)*
- **#10677**: Make the sharp-corner check's 89-degree boundary consistent on macOS and Linux *(curated)*
- **#10681**: Installed help, HV, readiness and tapeout skills point to a missing model hint *(curated)*
- **#10683**: Remaining check_all callers still grade pour_slot without the board's clearance rules *(curated)*
- **#10684**: Board 04's committed DRC evidence records 5 pour_slot warnings; a fresh pinned run reportedly gives 4 *(curated)*
- **#10692**: No gate compares a committed board bundle's per-rule warning counts to a fresh run (Board 04 pour_slot 5 vs 4 went unnoticed) *(curated)*
- **#10703**: Six other installed kct skills still assert a frontmatter suggestedModel that Codex and opencode strip *(curated)*
- **#10704**: sharp_trace_corner: 'N-segment junction' text now counts directions, not segments *(curated)*

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
| Operator merge-risk holds | 8 |
| Very important | 1 |
| Important | 2 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 25 |
| PRs awaiting review | 13 |
| Approved PRs awaiting merge | 12 |
| Curated | 42 |
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
