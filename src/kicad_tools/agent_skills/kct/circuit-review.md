---
name: circuit-review
invocation: /kct:circuit-review
suggestedModel: opus
description: Review a schematic against each IC's datasheet before layout — pin/strap/mode tables and typical-application circuit vs the netlist — and write a cited, confidence-graded findings document.
---

# Circuit Review

Review a schematic **against the datasheets of its ICs** and produce a cited findings document, so circuit-level mistakes are caught *before* layout, routing and tapeout. Every downstream gate (`kct check`, DRC, LVS, `{{skill:tapeout}}`) proves the board matches the schematic; none proves the schematic is *right*. This skill is that missing check.

> **Scope (advisory only).** This skill produces **findings, not edits**. It reads the schematic/netlist and datasheets and writes a single document. It **never edits the schematic or PCB, never changes a component value, and never self-applies a work label.** The owner ratifies each finding before anyone changes the design.

> **No PCB required.** The input is a schematic or project, so the review works *before* layout. A `.kicad_pcb` is never read.

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<schematic-or-project-path> [--ics U1,U3,...]`.

| Token | Meaning |
|-------|---------|
| `<schematic-or-project-path>` | A `.kicad_sch` file, or the project directory holding it. Hierarchical sheets are followed. |
| `--ics <refs>` | Optional comma-separated reference designators to restrict the review. Default: every active IC (anything with a datasheet pin/mode table: regulators, codecs, MCUs, drivers, amplifiers, level shifters). Passives, connectors and mechanical parts are not reviewed. |

## Inputs (read-only)

1. **Netlist and connectivity** — `kct sch summary <sch>`, `kct sch pin-map <sch>` (resolved pin-to-net assignments), `kct nets <sch> --net <NAME>` (trace one net), `kct netlist analyze|show` once a netlist exists. These are the ground truth for "what is wired to what".
2. **Component values** — `kct bom <sch>` (value, footprint, MPN per reference).
3. **Datasheets** — `kct datasheet search|download|convert|extract-tables` for the IC's MPN. Use the cached/converted copy when present. A datasheet you could not obtain is **not** guessed from memory (see "What this cannot check").

## Procedure (per IC)

Work one IC at a time and finish its section before starting the next.

1. **Identify** the part: reference, MPN, package, supply rails. Record the datasheet revision/date used.
2. **Read the datasheet's pin table.** For every pin, note the function, direction, and any "must be tied to ..." / "do not float" rule.
3. **Read the mode / strap / configuration table.** Note which pins are sampled at reset or select a mode, and which other pins are *only valid in a particular mode*.
4. **Read the typical-application circuit** and the recommended external components (filters, bias networks, bypassing, termination).
5. **Compare with the netlist**: for each pin, the net it is on, and for each external component, its value. Compute derived quantities by hand (corner frequencies, divider ratios, input thresholds vs drive level) and show the arithmetic.
6. **Record a finding** for every mismatch, using the output template below. Record "checked, matches" lines too for the load-bearing pins, so a reader can see what was verified rather than inferring it from silence.

Run the three checklist items below on **every** IC they could apply to, not only where the motivating example occurred.

## Checklist: the three escapes (worked examples)

Each of these passes ERC, DRC, LVS and fab checks, because the netlist is self-consistent; only the datasheet shows the error.

### 1. Output filter vs datasheet-recommended RC

Compare every RC/LC on an analog output, reference or feedback pin with the datasheet's recommended network. **Compute the corner** `f_c = 1 / (2 pi R C)` and compare it with the signal band the pin carries.

*Worked example:* an audio output stage feeds a 470 ohm series resistor into 10.2 uF of shunt capacitance. `f_c = 1 / (2 pi * 470 * 10.2e-6) = 33 Hz` — **inside** the audio band, so the network is a high-pass/low-pass that removes the signal it was meant to pass. The datasheet's recommended network puts the corner well outside the band. Flag whenever the corner lies inside the signal band, or differs from the datasheet-recommended corner by a large factor (state the factor).

### 2. Strap / mode conflict

For each configuration pin, determine which mode the strap selects, then check that **every pin valid only in a different mode** is not wired as if that other mode were selected.

*Worked example:* a mode pin is tied to a supply rail, selecting mode B. The same part also has pins that are only meaningful in mode A (for example a serial-data or clock pin), and those are wired up for mode A. Both are individually legal in the netlist; together they contradict the datasheet's mode table. Flag the strap, the conflicting pins, and the table row that shows the conflict.

### 3. Clamped logic input

For every logic input driven through a clamp (a diode to ground or to a supply, a series-resistor-plus-diode, a protection array), check the voltage the input actually sees **at every drive level** against `V_IH` / `V_IL` from the datasheet's electrical-characteristics table.

*Worked example:* a diode to GND holds an input at about one diode drop (roughly 0.3 to 0.7 V depending on the diode) when the source drives it high through a resistor divider. If `V_IH` is above that, the input can never read high; if the source is open or tristate the clamp sets the level instead. Flag the clamp, the resulting voltage range, and the threshold it violates, at minimum drive and maximum drive, including the unpowered and tristate cases.

## Grounding rules (mandatory)

- **Every finding cites the datasheet**: part number, revision/date, and page, table or figure (for example `<MPN> rev C, Table 7-2, p. 31`). A finding without a citation is not valid and must be removed or downgraded to a question.
- **Every finding carries a confidence grade**, and the header grade reflects the weakest load-bearing step:
  - `high` — datasheet read directly, netlist traced directly, arithmetic shown.
  - `med` — datasheet read, but a value was inferred (for example a symbol pin name did not map cleanly onto the datasheet pin table).
  - `low` — datasheet unavailable or partly unreadable (scanned tables, paywalled); the claim rests on an app note or general practice, and says so.
- **Measured vs. datasheet separation**: tag what came from the netlist (`[netlist]`) and what came from the datasheet (`[datasheet]`). Conflicts between sources are stated, not smoothed over.
- **Never fabricate datasheet contents.** If a table cannot be read, say so and mark the IC unreviewed or the pin unverified.
- **Read-only.** Write exactly one findings document (or post it as one issue comment) and nothing else.

## Grading rules

- **The basis sets the severity floor.** Tag every finding that compares the fitted design against the datasheet with which kind of statement it rests on:
  - `[basis: explicit]` — the datasheet states it explicitly for that pin or component: pin-table text, "must" / "should" wording, or a stated range. A component value (or connection) that **contradicts an explicit statement** is graded **at least `warning`** — never `note`.
  - `[basis: typical]` — the only disagreement is with a typical-application figure (a value, network or corner the "typical application" circuit shows). This may stay `note`.
- **Grade is independent of confidence.** Confidence records how sure the review is of its reading; basis records what kind of datasheet statement the finding contradicts. A `med`-confidence explicit contradiction (pin mapping inferred, scanned-but-readable table) is still `warning` — never average the two into a softer grade, and never let a "should" where the datasheet could have said "must" downgrade it.
- **Never merge the two bases into one finding.** An explicit contradiction and a typical-application difference on the same IC are separate findings, each graded on its own basis, so the explicit one cannot hide inside a `note`.

*Worked example (the escape this rule closes):* the pin table says the internal LDO output "should be used with a 0.1-uF decoupling cap"; the board fits 10 uF there — 100x the stated value. That is `[basis: explicit]`, graded **at least `warning`** even at `med` confidence. The charge-pump flying and reservoir caps (1 uF and 10 uF fitted where the typical application shows 2.2 uF) contradict no explicit statement — `[basis: typical]`, may stay `note`, recorded as their own findings.

## Output

Write `<project>/CIRCUIT_REVIEW.md` (or post it as a single issue comment when the invoker names an issue) with these sections in order:

```
## Circuit review — <project> (YYYY-MM-DD)

**Confidence: [high|med|low]** | Schematic: <path> @ <commit or file hash> | Datasheets: <n of m ICs obtained>

### Open decisions
| Finding | Decision | Owner action |
|---------|----------|--------------|
| F1      | open     | fix / accept (reason) |

### ICs reviewed
| Ref | MPN | Datasheet (rev, source) | Result |
|-----|-----|-------------------------|--------|
| U1  | ... | ...                     | N findings / matches |

### ICs NOT reviewed
| Ref | MPN | Reason (datasheet unavailable / paywalled / unreadable / out of scope) |
|-----|-----|--------|

### Findings
#### F1 — <short title>  [severity: blocker|warning|note] [confidence: high|med|low] [basis: explicit|typical]
- Where: <ref.pin>, net <NAME> [netlist]
- Datasheet says: <quote or paraphrase> — <MPN> rev X, Table/Fig N, p. N [datasheet]
- Computed: <arithmetic, if any>
- Why it matters: <consequence>
- Suggested change: <proposal only; the owner decides>

### Checked, matches
- <ref.pin> ... <what was verified, with citation>

### What this review cannot check
<the section below, specialised to this project>
```

An IC whose datasheet could not be obtained goes in **ICs NOT reviewed**. It is never silently passed.

**Open decisions are the hand-off.** Every `blocker` or `warning` finding gets one row in `### Open decisions`, written by the review with its Decision cell `open`. **The review itself never fills in a decision** — not `accepted`, not `fixed`, however obvious the call looks; deciding is the owner's act, and a review that decides for the owner is invalid. The owner edits the cell from `open` to `fixed` or to `accepted: <reason>`. `{{skill:tapeout}}` (Gate 4c) prints this table and refuses a tapeout while any row's Decision cell is still `open`. A review with no `blocker`/`warning` findings keeps the section, holding a single `none — no blocker or warning findings` line.

## What this cannot check

State these in every output, specialised to the project:

- **Datasheet unavailable or paywalled** — the IC is listed as unreviewed/unverified, not passed.
- **Analog behaviour beyond hand calculation** — stability, noise, distortion, start-up sequencing and thermal behaviour need simulation or measurement, not this review.
- **Errata** — a datasheet's published errata sheet is separate; check it only if it was obtained, and say whether it was.
- **Anything not in the netlist** — firmware configuration, resistor tolerances/temperature drift beyond the stated values, mechanical fit, supply behaviour under fault.
- **Datasheet errors** — the review trusts the datasheet it read; two conflicting revisions are reported as a conflict.

## Limitations

- **Single reviewer.** No automatic datasheet parsing; the reviewer reads the converted datasheet and reasons over it. There is no `kct` subcommand and no rule engine behind this skill — the three escapes are checklist items, not automated checks.
- **Decisions gate tapeout; the review itself never decides.** `{{skill:tapeout}}` (Gate 4c) reads a `CIRCUIT_REVIEW.md` sitting beside the board, prints its Open-decisions table, and **refuses while any decision is still `open`** — so a datasheet contradiction cannot silently ride to fab the way a `note` finding once could. A board with no review document is unaffected: the review is advisory and never required. Run this skill before layout and keep the findings document beside the board so the gate can see it.
- **Advisory only.** The output is a proposal for the owner to ratify.

## References

- Issue #8704 — the motivating schematic-level escapes (output RC corner in-band, strap/mode conflict, clamped logic input) that passed every existing gate.
- `{{skill:ee-review}}` — sibling skill whose grounding and confidence conventions this one follows (it covers placement-blocked boards from a PCB; this one covers the schematic).
- `{{skill:tapeout}}` — the downstream gate this review precedes.
