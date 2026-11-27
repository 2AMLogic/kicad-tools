# Analog pin ranges from simulated waveforms

`kicad_tools.analysis.analog_pin_range` checks the voltage a simulated waveform
demands at a device pin against the operating limits that apply to one supply
state. An ERC or copper LVS pass says nothing about this: a passive line-sense
network can demand a negative voltage at a single-supply amplifier input and at
an MCU ADC input on every mains cycle while both checks pass.

The evaluator reads three reviewed inputs and runs nothing:

- a flat schematic,
- an assertion sidecar that binds device pins to schematic nets and to traces,
- JSON sample files produced outside this tool, for example by ngspice.

```sh
uv run python -m kicad_tools.analysis.analog_pin_range line_sense.kicad_sch pin-ranges.json > pin-range-report.json
```

Exit 0 means every assertion passed inside its declared scope, 1 means at least
one pin violated a limit, and 2 means at least one result is unassessed or the
sidecar could not be bound. Every report carries `release_eligible: false`.

## Sample files

One sample file holds one externally generated run: a single strictly
increasing time axis shared by every named trace. Times are in seconds and
voltages in volts, every value must be a finite number, and each trace must
have exactly one sample per time. `source` is free text recording how the run
was produced; it is copied into the report.

```json
{
  "schema_version": 1,
  "source": "ngspice-47 .tran 10u 100m of line_sense_original.cir",
  "time_s": [0.0, 1.15883833e-06, 2.61907815e-05],
  "traces": {
    "v(sense)": [0.0, 0.000407372121, 0.00926383323],
    "v(adc)": [0.0, 0.000407133328, 0.00914384784]
  }
}
```

ngspice writes `time v(sense) time v(adc)` columns for the decks below. These
lines convert that output; this repository never needs a simulator installed:

```python
import json
from pathlib import Path

rows = [line.split() for line in Path("line_sense_original.txt").read_text().splitlines()]
data = {
    "schema_version": 1,
    "source": "ngspice-47 .tran 10u 100m of line_sense_original.cir",
    "time_s": [float(row[0]) for row in rows],
    "traces": {
        "v(sense)": [float(row[1]) for row in rows],
        "v(adc)": [float(row[3]) for row in rows],
    },
}
Path("line_sense_original.samples.json").write_text(json.dumps(data))
```

## Assertion sidecar

The sidecar has `schema_version: 1`, the `schematic_sha256` of the exact
schematic bytes, the named `samples` sets and the `cases`. Any schematic-byte
change makes the sidecar stale until the hash is deliberately replaced. Each
sample set gives a `path`, relative to the sidecar unless absolute, and the
`sha256` of the file: a trace that no longer matches its digest is never
assessed.

| Case field | Meaning |
|---|---|
| `id` | Name reported with each of the case's pins |
| `waveform` | Name of the driven waveform these samples answer to |
| `state` | `powered` or `unpowered` |
| `window` | `start_s` and `end_s`, inclusive; every sample inside is compared |
| `reference_net` | Schematic net the trace voltages are referred to |
| `rails` | Device supply rails in this state: `net`, `voltage_v`, `source` |
| `samples` | Name of the sample set that holds this case's traces |
| `model` | `input_protection` (`omitted` or `modeled`) and `assumptions` |
| `pins` | Pin assertions |

Each pin assertion maps a `reference`, a `pin` number and the `net` the pin is
expected on in the schematic to a `trace` name, and gives the `limits` that
apply: `state`, `lower_v`, `upper_v` and a `source`. The analyzer compares the
declared reference, pin and net with the connectivity extracted from the
schematic, as [analog threshold budgets](analog-threshold-budgets.md) do. A pin
that moved to another net is unassessed. Unknown fields anywhere in the sidecar
are rejected so that a misspelled limit cannot be ignored.

## Worked case: line-sense network

The 60 Hz line reaches ±187 V relative to logic ground. Two 499 kΩ resistors
feed a 10 kΩ shunt to ground; that tap drives an ideal unity-gain buffer and a
series 100 nF capacitor, and a second 10 kΩ resistor ties the capacitor's far
side, the ADC input, to ground. A DC-only divider calculation sees 1.855 V at
the tap and nothing at the ADC. The transient run sees the tap loaded by the
capacitor and its shunt, and the ADC swinging with the settled high-pass
response.

The [fixture schematic](../../tests/fixtures/analog_pin_range/line_sense.kicad_sch)
binds `LINE_SENSE` to buffer input `U1` pin `1` and `ADC_IN` to MCU pin `U2`
pin `3`. The [deck](../../tests/fixtures/analog_pin_range/line_sense_original.cir)
for the original network is:

```spice
* Issue #10527 line-sense network, original 10k shunts.
* The buffer is an ideal unity-gain source with a high-impedance input.
* Neither input has protection clamps: the pin voltages are unclamped demand.
.param VPK=187
V1 LINE 0 SIN(0 {VPK} 60)
R1 LINE MID 499k
R2 MID SENSE 499k
R3 SENSE 0 10k
E1 BUF 0 SENSE 0 1
C1 SENSE ADC 100n
R4 ADC 0 10k
.tran 10u 100m
.measure tran sense_min MIN v(SENSE) from=50m to=100m
.measure tran sense_max MAX v(SENSE) from=50m to=100m
.measure tran adc_min MIN v(ADC) from=50m to=100m
.measure tran adc_max MAX v(ADC) from=50m to=100m
.control
run
wrdata line_sense_original.txt v(sense) v(adc)
.endc
.end
```

`E1` is an ideal unity-gain source with a high-impedance, unloaded input, and
neither input has protection clamps. The [biased deck](../../tests/fixtures/analog_pin_range/line_sense_biased.cir)
replaces each 10 kΩ shunt with 4.7 kΩ to a 3.3 V rail and 4.7 kΩ to ground.
The committed sample files keep every fifth simulator row plus the final row,
2003 of about 10009, so the fixture stays small; their extrema agree with the
full runs to within 0.01 mV.

Run each circuit with `ngspice -b <deck>` in a scratch directory, convert the
output as shown above, and write the sidecar. Replace every hash below with the
SHA256 of the reviewed file. The limits are **synthetic**: 0 to 3.3 V inclusive
for the powered state and −0.3 to +0.3 V for the unpowered state. A board
assertion supplies the limits of its own device, pin and supply state.

```json
{
  "schema_version": 1,
  "schematic_sha256": "<sha256 of line_sense.kicad_sch>",
  "samples": {
    "line_sense_original": {"path": "line_sense_original.samples.json", "sha256": "<sha256>"},
    "line_sense_biased": {"path": "line_sense_biased.samples.json", "sha256": "<sha256>"}
  },
  "cases": [
    {
      "id": "mains_powered_original",
      "waveform": "mains 187 V peak 60 Hz, zero offset, referred to GND",
      "state": "powered",
      "window": {"start_s": 0.05, "end_s": 0.1},
      "reference_net": "GND",
      "rails": {
        "VDD": {"net": "+3V3", "voltage_v": 3.3, "source": "Ideal fixture supply"},
        "VSS": {"net": "GND", "voltage_v": 0.0, "source": "Ideal fixture return"}
      },
      "samples": "line_sense_original",
      "model": {
        "input_protection": "omitted",
        "assumptions": [
          "Ideal unity-gain buffer with a high-impedance, unloaded input",
          "No input protection clamps on either input"
        ]
      },
      "pins": [
        {
          "reference": "U1", "pin": "1", "net": "LINE_SENSE", "trace": "v(sense)",
          "limits": {
            "state": "powered", "lower_v": 0.0, "upper_v": 3.3,
            "source": "Synthetic fixture limit, 0 to 3.3 V inclusive; not a device rating"
          }
        },
        {
          "reference": "U2", "pin": "3", "net": "ADC_IN", "trace": "v(adc)",
          "limits": {
            "state": "powered", "lower_v": 0.0, "upper_v": 3.3,
            "source": "Synthetic fixture limit, 0 to 3.3 V inclusive; not a device rating"
          }
        }
      ]
    },
    {
      "id": "mains_powered_biased",
      "waveform": "mains 187 V peak 60 Hz, zero offset, referred to GND",
      "state": "powered",
      "window": {"start_s": 0.05, "end_s": 0.1},
      "reference_net": "GND",
      "rails": {
        "VDD": {"net": "+3V3", "voltage_v": 3.3, "source": "Ideal fixture supply"},
        "VSS": {"net": "GND", "voltage_v": 0.0, "source": "Ideal fixture return"}
      },
      "samples": "line_sense_biased",
      "model": {
        "input_protection": "omitted",
        "assumptions": [
          "Ideal unity-gain buffer with a high-impedance, unloaded input",
          "No input protection clamps on either input"
        ]
      },
      "pins": [
        {
          "reference": "U1", "pin": "1", "net": "LINE_SENSE", "trace": "v(sense)",
          "limits": {
            "state": "powered", "lower_v": 0.0, "upper_v": 3.3,
            "source": "Synthetic fixture limit, 0 to 3.3 V inclusive; not a device rating"
          }
        },
        {
          "reference": "U2", "pin": "3", "net": "ADC_IN", "trace": "v(adc)",
          "limits": {
            "state": "powered", "lower_v": 0.0, "upper_v": 3.3,
            "source": "Synthetic fixture limit, 0 to 3.3 V inclusive; not a device rating"
          }
        }
      ]
    },
    {
      "id": "mains_unpowered_original",
      "waveform": "mains 187 V peak 60 Hz, zero offset, referred to GND",
      "state": "unpowered",
      "window": {"start_s": 0.05, "end_s": 0.1},
      "reference_net": "GND",
      "rails": {
        "VDD": {"net": "+3V3", "voltage_v": 0.0, "source": "Ideal fixture with the 3V3 supply off"},
        "VSS": {"net": "GND", "voltage_v": 0.0, "source": "Ideal fixture return"}
      },
      "samples": "line_sense_original",
      "model": {
        "input_protection": "omitted",
        "assumptions": [
          "Ideal unity-gain buffer with a high-impedance, unloaded input",
          "No input protection clamps on either input"
        ]
      },
      "pins": [
        {
          "reference": "U1", "pin": "1", "net": "LINE_SENSE", "trace": "v(sense)",
          "limits": {
            "state": "unpowered", "lower_v": -0.3, "upper_v": 0.3,
            "source": "Synthetic off-state fixture limit, -0.3 to +0.3 V; not a device rating"
          }
        },
        {
          "reference": "U2", "pin": "3", "net": "ADC_IN", "trace": "v(adc)",
          "limits": {
            "state": "unpowered", "lower_v": -0.3, "upper_v": 0.3,
            "source": "Synthetic off-state fixture limit, -0.3 to +0.3 V; not a device rating"
          }
        }
      ]
    }
  ]
}
```

The window 50–100 ms holds three settled cycles of the 100 ms run, so the
switch-on transient is excluded. The unpowered case names its own rail
evidence (the 3V3 rail at 0 V) and its own limits; it reuses the original
samples because the passive network and the ideal buffer do not depend on the
supply. A circuit whose response changes with the rail needs a run for each
state.

The command exits 1 and reports:

| Case | Pin | Net | Minimum | Maximum | Outcome |
|---|---|---|---:|---:|---|
| `mains_powered_original` | `U1.1` | `LINE_SENSE` | −1.58590 V | +1.58590 V | fail, 1.58590 V below 0 V |
| `mains_powered_original` | `U2.3` | `ADC_IN` | −0.55944 V | +0.55944 V | fail, 0.55944 V below 0 V |
| `mains_powered_biased` | `U1.1` | `LINE_SENSE` | +1.21186 V | +2.08039 V | pass |
| `mains_powered_biased` | `U2.3` | `ADC_IN` | +1.61168 V | +1.68832 V | pass |
| `mains_unpowered_original` | `U1.1` | `LINE_SENSE` | −1.58590 V | +1.58590 V | fail on both sides |
| `mains_unpowered_original` | `U2.3` | `ADC_IN` | −0.55944 V | +0.55944 V | fail on both sides |

All six measurements are labelled **unclamped demand**: the voltage the network
demands before any protection clamp conducts, not a prediction of clamp current
or damage. The original circuit demands ±1.586 V at the amplifier input and
±0.559 V at the ADC every cycle, outside the 0 to 3.3 V powered limits, while
the biased taps stay inside them.

## Reading a result

The report's `pins` array holds one row per pin and case with the waveform,
window, reference net, `reference`, `pin`, `net` and `trace`, the `rails` and
`state`, the `model`, the `limits`, the number of samples inside the window, the
signed `min_v` and `max_v`, the `violations` and the `outcome`.

- `outcome` is `pass`, `fail` or `unassessed`.
- `violations` has one entry per violated side, `lower` or `upper`, with the
  `limit_v`, the `extreme_v` that crossed it and the positive `excursion_v`
  beyond it. A trace outside both limits reports both sides. A sample exactly
  equal to a limit passes.
- `label` is `unclamped_demand` when `input_protection` is `omitted` and
  `simulated_with_input_protection` when it is `modeled`.
- `reasons` lists why an unassessed row was not judged. Measured extrema stay on
  the row when the reference net, pin binding, trace and window are sound, for
  example when only the limits are missing; otherwise they are `null`.

A pin is unassessed, with a reason, when:

- its reference is absent from the schematic, its pin is not connected, or the
  pin sits on a net other than the declared one;
- its trace is missing from the sample set, a sample is not a finite number, a
  trace does not match the time axis, or the time axis is not strictly
  increasing;
- the samples do not span the window, or the window holds fewer than two
  samples;
- no limits are declared for the case's state, a limit is not a finite number,
  the lower limit exceeds the upper one, or the limits have no source;
- rail evidence is absent, a rail's net is not in the schematic, or a rail has
  no source;
- the schematic does not identify a non-reference supply net on a device power
  input, or any such net lacks a declared voltage for this case;
- rail entries give conflicting voltages for the same device supply or voltage
  reference net, even when the entries have different names;
- the model assumptions or the input-protection declaration are missing;
- the voltage reference net is absent or not in the schematic.

A missing or mismatched binding, missing evidence or malformed data never
becomes a pass. The report's `issues` array lists problems that concern the
whole sidecar instead of one pin, such as a stale schematic hash, a hierarchical
schematic (not supported; there is no partial hierarchy pass), no cases, a
duplicate case id or pin assertion, or an unknown sidecar field.

A pass applies only to the named waveform, supply state, model and window. It
is not a device rating, a prediction of clamp current or damage, and it grants
no release approval.
