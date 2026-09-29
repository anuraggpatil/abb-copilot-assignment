---
doc_id: TS-BFP-VIB-CAV
title: Boiler Feed Pump Vibration and Cavitation — Troubleshooting Guide
doc_type: troubleshooting_guide
revision: 4
effective_date: 2026-01-15
owner: Rotating Equipment Engineering
applies_to:
  - Boiler Feed Pump 101
  - Boiler Feed Pump 102
  - Multistage centrifugal feed pumps
review_cycle_months: 36
---

# Boiler Feed Pump Vibration and Cavitation — Troubleshooting Guide

## §1 Purpose and how to use this guide

Vibration is a symptom. This guide exists because the four common causes of feed-pump
vibration require materially different responses, and choosing the wrong one wastes an
outage or destroys a bearing.

The single most common diagnostic error on boiler feed pumps is treating a vibration alarm
as a bearing problem. On this equipment class the majority of vibration alarms originate in
the process — inadequate suction conditions — and are corrected by restoring suction
pressure or reducing flow, not by touching the bearing. A bearing replaced because of
cavitation-driven vibration will fail again on the same timescale, because nothing that
caused the vibration was changed.

Use §2 to narrow the cause from the evidence available in the control room. Use §3 to
understand why low suction pressure produces vibration, and §4 for the hands-on diagnostic
sequence. §5 states when to stop the pump regardless of diagnosis.

This guide diagnoses. It does not authorise intervention: any hands-on work requires
isolation under SAF-PUMP-LOTO, and the corrective actions live in OP-BFP-101 §4 and
MM-CP-MAINT.

## §2 Symptom-to-cause matrix

Read across from the observed evidence. The spectrum column is the discriminating
evidence — amplitude alone cannot separate these causes, which is why OP-BFP-101 §4.4
step 1 requires a spectrum and not just a reading.

| Cause | Vibration spectrum | Bearing temp | Suction pressure | Flow | Noise | Correct response |
|---|---|---|---|---|---|---|
| **Cavitation** | Broadband, random, energy above 2 kHz; no dominant order | Normal or slightly raised | **Low or falling** | Normal or erratic | Gravel or crackling at suction | Restore suction pressure; reduce flow. OP-BFP-101 §4.2 |
| **Low-flow recirculation** | Low-frequency, below 1× running speed; random pulsation | Raised, slow rise | Normal | **Below minimum** | Rumbling, surging | Establish minimum flow. OP-BFP-101 §4.3 |
| **Loss of oil film** | 1× and 2× running speed rising together; may become unstable at 0.4–0.5× | **Rising, tracks vibration** | Normal | Normal | Whine, then knocking | Restore lube oil pressure. OP-BFP-101 §5.1 |
| **Bearing wear or damage** | Discrete high-frequency bearing defect frequencies, sidebands | Raised, steady or rising | Normal | Normal | Rough, periodic | Inspect and replace. MM-CP-MAINT §5 |
| **Misalignment** | Strong 2× running speed, high axial component | Raised at one bearing | Normal | Normal | Steady | Re-align to MM-CP-MAINT §5 tolerance |
| **Impeller imbalance or damage** | Dominant 1× running speed, stable phase | Normal | Normal | May be reduced | Steady hum | Inspect impeller at next outage |
| **Instrument fault** | Implausible step change; no corroborating change | Normal | Normal | Normal | **None** | Verify per OP-BFP-101 §6 |

### §2.1 Using alarm history as diagnostic evidence

The pattern of recurrence discriminates between causes almost as well as the spectrum, and
it is available without going to the machine.

- **A vibration alarm consistently preceded by a lube oil pressure alarm within the hour**
  indicates loss of oil film, not a hydraulic cause. The oil system is the thing to fix.
  This precursor relationship is the most reliable single indicator in this table, because
  the causal direction is unambiguous: oil pressure does not fall because of vibration.
- **A vibration alarm accompanied or preceded by low suction pressure** indicates
  cavitation.
- **A vibration alarm with no precursor, appearing at a slowly increasing rate over weeks**
  indicates progressive mechanical deterioration — bearing wear or developing
  misalignment.
- **An accelerating occurrence rate** — a materially larger share of occurrences in the
  most recent quarter of the window than in the earliest — means the mechanism is
  progressing. This is the distinction between a condition to monitor and a condition to
  act on now, and it is the strongest argument available for taking a planned outage.
- **A steady occurrence rate at constant amplitude** with no other symptom points to an
  alarm setpoint set too close to the machine's normal operating vibration. That is an
  alarm rationalization item under MM-CP-MAINT §6, not a mechanical fault.

A recurring vibration alarm on a high-criticality feed pump should never be closed on the
basis of recurrence alone. Recurrence establishes that the condition is real and
uncorrected; it does not identify which of the above it is.

## §3 Cavitation mechanism

### §3.1 Why low suction pressure produces vibration

Cavitation occurs when the pressure at the impeller inlet falls below the vapour pressure
of the feedwater at its current temperature. The water boils locally, forming vapour
cavities in the low-pressure region at the leading edge of the impeller vanes.

As those cavities are carried into the higher-pressure region of the impeller passage, they
collapse. The collapse is violent and asymmetric: liquid rushes into the void and impacts
the adjacent surface as a microjet, with local pressures high enough to deform metal. Each
collapse is a small mechanical impact. Millions of them per minute, distributed randomly
across the impeller inlet, produce exactly the signature in §2 — broadband, random, high
frequency, with no relationship to running speed.

The controlling quantity is net positive suction head (NPSH). NPSH available is the margin
between the absolute pressure at the pump inlet and the fluid's vapour pressure, expressed
as a head of liquid. NPSH required is a property of the pump and rises steeply with flow.
Cavitation begins when available falls below required.

On Boiler Feed Pump 101, NPSH required at design flow is 4.1 m and NPSH available at normal
deaerator level is 7.3 m. The 3.2 m margin is consumed by any of:

- **Falling deaerator level** — directly reduces static head at the pump inlet.
- **Falling deaerator pressure** — reduces inlet absolute pressure.
- **Rising feedwater temperature** — raises vapour pressure. This is the most easily missed
  cause: suction *pressure* can read entirely normal while the margin has vanished, because
  the fluid has moved closer to saturation. Feedwater at the deaerator is by design close
  to its boiling point, so this pump operates with less thermal margin than most.
- **Suction line restriction** — a fouled strainer or a partially closed valve consumes
  margin as friction loss. See MM-CP-MAINT §4.
- **Increased flow** — raises NPSH required. This is why flow reduction is the one action
  at the pump that increases margin, per OP-BFP-101 §4.2 step 7.

### §3.2 Progression from cavitation to bearing failure

Cavitation does not stay a vibration problem. The progression is consistent and is the
reason a recurring cavitation-driven vibration alarm on a feed pump is treated as a
developing failure rather than a nuisance:

1. **Cavitation begins.** Broadband vibration appears. Efficiency falls slightly. Nothing
   is yet damaged. At this stage the condition is fully reversible by restoring suction
   conditions.
2. **Surface erosion.** Repeated cavity collapse pits the impeller vane leading edges and
   the inlet casing. Material loss is slow but cumulative. Hydraulic performance degrades.
3. **Hydraulic imbalance.** Asymmetric material loss and unsteady vapour distribution
   produce a fluctuating radial load on the impeller. The shaft now sees a cyclic side load
   it was not designed for.
4. **Bearing loading.** That radial load is carried by the journal bearings. Dynamic
   loading beyond design accelerates wear of the bearing surface and, in a pressure-fed
   bearing, disturbs the oil film. Bearing metal temperature begins to rise.
5. **Oil film breakdown.** As clearances open and loading becomes irregular, the
   hydrodynamic film supporting the journal becomes unstable. Oil pressure may fall as
   leakage through widened clearances increases. **At this point the vibration spectrum
   changes character** — the broadband cavitation signature is joined by rising 1× and 2×
   running-speed components, and a lube oil pressure alarm may appear for the first time.
6. **Mechanical failure.** Bearing metal-to-metal contact, rapid temperature rise, shaft
   damage, and consequential damage to seals and coupling.

Stages 1 to 3 are measured in weeks to months. Stage 5 to stage 6 can be measured in hours.

**This progression explains why lube oil pressure alarms and vibration alarms appearing
together on a feed pump are a late-stage indication and not two unrelated faults.** Where
the alarm history shows a lube oil pressure alarm preceding a vibration alarm repeatedly,
two readings are possible: the oil system is failing independently and causing the
vibration (§2, loss of oil film), or cavitation has progressed to stage 5 and is disturbing
the oil film. The discriminator is suction pressure history. If suction pressure alarms are
also present in the window, the cavitation reading is the correct one and correcting the
oil system alone will not stop the progression.

### §3.3 Distinguishing cavitation from flashing

Both produce vapour at the pump inlet and both sound similar. Flashing occurs when the
fluid vaporises in the suction line itself because the line pressure has fallen below
vapour pressure, rather than only in the impeller's low-pressure region. Flashing produces
a more complete loss of discharge pressure and flow, often with the pump losing prime
entirely, whereas cavitation typically maintains most of its duty while degrading. The
response is the same: restore suction conditions and reduce flow.

## §4 Diagnostic sequence

Work in this order. Each step is chosen to be cheap and non-invasive before the one after
it, and to exclude a cause rather than confirm one.

### §4.1 From the control room, before going to the machine

1. Read current vibration at both bearings and compare against the startup baseline
   recorded under OP-BFP-101 §2 step 11 and against the 4.5 mm/s alert setpoint.
2. Read suction pressure, deaerator level, deaerator pressure and feedwater temperature.
   Calculate NPSH margin. Do not skip the temperature: see §3.1.
3. Read flow and compare against the 55 m³/h minimum.
4. Read lube oil pressure, oil temperature and filter differential.
5. Read bearing metal temperatures at both bearings.
6. Retrieve the alarm history for the asset over at least ninety days. Establish the
   occurrence count for each alarm, whether the rate is increasing, and whether any alarm
   consistently precedes the vibration alarm. Apply §2.1.
7. Form a provisional diagnosis from §2 before going to the machine. Going without one
   produces a report of what was observed rather than an answer.

### §4.2 At the machine

1. Listen at the suction. Cavitation is distinctive: irregular crackling, commonly
   described as gravel passing through the pump. Low-flow recirculation rumbles instead.
2. Take a vibration spectrum at the drive-end and non-drive-end bearings with a portable
   analyser, in both radial and axial directions. The axial component discriminates
   misalignment.
3. Check the suction strainer differential at the local gauge.
4. Confirm the minimum-flow recirculation valve is passing by temperature rise across the
   line, not by position feedback — a stuck valve reports open.
5. Check for oil leakage, oil level and oil clarity. Water in the oil appears as a milky
   emulsion and destroys film strength.
6. Feel for and measure bearing housing temperature at the housing, as a check on the RTD.
7. Check coupling guard, foundation bolts and grouting for looseness. Looseness amplifies
   whatever vibration exists and is cheap to exclude.

### §4.3 Interpreting the result

- Broadband spectrum plus low NPSH margin: cavitation. Correct suction conditions per
  OP-BFP-101 §4.2. Do not open the bearing.
- Low-frequency spectrum plus flow below minimum: recirculation. Correct per OP-BFP-101
  §4.3.
- Rising 1× and 2× plus rising bearing temperature plus low oil pressure: oil film loss.
  Correct per OP-BFP-101 §5.1, then investigate why — if suction history shows cavitation,
  read §3.2 stage 5.
- Discrete bearing defect frequencies: bearing damage. Plan an inspection under
  MM-CP-MAINT §5. Do not wait for the trip.
- Nothing corroborates the reading: instrument fault. Verify per OP-BFP-101 §6.
- Two causes present at once is common and is not a reason to pick one. Correct the process
  cause first, because it is upstream of the mechanical one.

## §5 When to stop the pump

Stop the pump regardless of how far the diagnosis has progressed if any of the following
hold. A diagnosis is not worth a machine.

- Vibration at or above the 7.1 mm/s trip setpoint.
- Bearing metal temperature at or above 95 °C, or rising faster than 1 °C per minute.
- Lube oil pressure below 1.0 bar g, or unrecoverable above the 1.4 bar g alert.
- Audible cavitation that cannot be cleared within minutes by reducing flow.
- Any combination of rising vibration **and** rising bearing temperature **and** falling
  oil pressure. This is §3.2 stage 5, the interval before failure is short, and it is the
  one combination that warrants stopping the pump without waiting for any single parameter
  to reach its trip.

Follow OP-BFP-101 §3.2 for a controlled shutdown or §3.3 for an emergency shutdown, then
isolate under SAF-PUMP-LOTO §3 before any hands-on inspection. Record the event under
MM-CP-MAINT §7 with the vibration spectrum attached — the spectrum at the time of the event
is the single most useful record for the subsequent investigation and cannot be recovered
afterwards.
