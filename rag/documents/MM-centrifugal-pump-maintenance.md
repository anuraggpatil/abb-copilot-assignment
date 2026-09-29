---
doc_id: MM-CP-MAINT
title: Centrifugal Pump Maintenance Manual
doc_type: maintenance_manual
revision: 11
effective_date: 2026-02-10
owner: Maintenance Engineering
applies_to:
  - Centrifugal pumps
  - Boiler feed pumps
  - Multistage horizontal pumps
review_cycle_months: 24
---

# Centrifugal Pump Maintenance Manual

## §1 Scope

This manual covers planned and condition-based maintenance of horizontal centrifugal pumps,
including multistage boiler feed pumps. It applies plant-wide. Asset-specific setpoints and
operating limits are in the relevant operating procedure — for Boiler Feed Pump 101, see
OP-BFP-101 §1.

No activity in this manual may begin before the asset is isolated and locked out under
SAF-PUMP-LOTO. Where this manual specifies a maintenance interval and a condition-based
indication suggests an earlier intervention, the condition-based indication governs: see
§5.

## §2 Planned maintenance schedule

| Activity | Interval | Section |
|---|---|---|
| Oil level, clarity and leak check | Weekly | §3.1 |
| Filter differential check | Weekly | §3.2 |
| Vibration trend review | Monthly | §5.2 |
| Suction strainer differential check | Monthly | §4.1 |
| Oil sample for analysis | Quarterly | §3.3 |
| Oil change (mineral) | 8,000 operating hours | §3.3 |
| Oil change (synthetic) | 16,000 operating hours | §3.3 |
| Seal inspection | Annually | §4.2 |
| Suction strainer clean | Annually or on differential | §4.1 |
| Coupling inspection and alignment check | Annually | §5.4 |
| Bearing inspection | Per §5.3 | §5.3 |
| Alarm performance review | Quarterly | §6 |

Intervals are for continuously operating pumps in clean service. Halve the oil-related
intervals for pumps in intermittent service, where condensation in the reservoir is the
dominant degradation mechanism rather than oxidation.

## §3 Bearing lubrication

Bearing lubrication is the single highest-value maintenance activity on a centrifugal pump.
The large majority of premature bearing failures are lubrication-related — wrong oil,
contaminated oil, insufficient oil, or oil too hot to maintain a film — and every one of
those is preventable at a cost far below that of a bearing replacement.

### §3.1 Oil level, condition and leaks

Check weekly with the pump running and at normal operating temperature. Readings taken cold
or stopped are not comparable.

1. Confirm the level is between the marks on the sight glass. Overfilling is as harmful as
   underfilling: excess oil churns, aerates and overheats.
2. Inspect clarity against a white background. Clear amber is normal. **A milky or cloudy
   appearance indicates water ingress** and requires the oil to be changed and the source
   found — water displaces the load-bearing film and reduces bearing life by an order of
   magnitude at concentrations as low as 0.1%.
3. Darkening indicates oxidation; a burnt smell indicates local overheating.
4. Check for visible leakage at the housing seals, drain plug, sight glass and cooler
   connections. A leak that requires weekly topping up is a defect, not a routine task, and
   the topping up masks it.
5. Record the level and condition. A falling trend over weeks locates a slow leak long
   before an alarm would.

### §3.2 Filters

The duplex filter allows element replacement without stopping the pump.

1. Read the differential weekly. A clean element reads below 0.3 bar.
2. At 1.0 bar differential, switch to the standby element and replace the used one. Do not
   wait for a lube oil pressure alarm — by the time filter blockage shows as low bearing
   header pressure, the bearings have already run with reduced flow.
3. Cut open and inspect each removed element. The debris is diagnostic: bronze or babbitt
   particles indicate bearing wear in progress; iron indicates housing or shaft wear; dirt
   indicates a breather or seal fault letting the environment in.
4. Record what the element contained. An element full of bearing metal is the clearest
   early warning of bearing failure available, and it is routinely discarded unexamined.

### §3.3 Oil analysis and changes

Sample quarterly from a consistent point with the pump running and warm. Analyse for
viscosity, water content, total acid number, and wear metals.

| Result | Threshold | Action |
|---|---|---|
| Water content | > 500 ppm | Change oil; find ingress path |
| Viscosity change | > ±10% of new | Change oil; confirm correct grade |
| Total acid number | > 2.0 mg KOH/g | Change oil |
| Iron | > 100 ppm | Investigate; inspect filter debris |
| Copper, tin or lead rising | Any consistent rise | **Bearing wear in progress — see §5.3** |

A rising trend in bearing-metal wear elements matters more than any single absolute value.
Two consecutive rising samples justify a bearing inspection ahead of the planned interval.

Change oil at the interval in §2 or whenever analysis requires it, whichever comes first.
Flush the reservoir at every change; new oil into a dirty reservoir is contaminated
immediately. Confirm the grade against the machine's nameplate rather than against what was
last used — substituted grades are a recurring cause of repeat failures.

### §3.4 Lube oil system faults

Where the asset has a pressurised lube oil system with a standby pump, low oil pressure has
a small number of causes. Diagnose in this order, cheapest first: oil level, filter
differential, oil temperature and viscosity, relief valve set or stuck, pump wear,
widened bearing clearances.

Widened bearing clearances are last on the list but are the most serious: as clearances
open, leakage rises and header pressure falls. **Falling oil pressure with adequate level,
clean filters and a healthy oil pump means the bearings are passing more oil than they
should, which means they are worn.** Treat this finding as a bearing inspection trigger
under §5.3, not as an oil system fault to be compensated for by running the standby pump.

The operational response to a low oil pressure alarm belongs to the operating procedure —
see OP-BFP-101 §5.1. The maintenance response is to determine which of the above it was and
correct it, rather than restoring pressure and closing the alarm.

## §4 Strainers and seals

### §4.1 Suction strainers

A fouled suction strainer consumes NPSH margin as friction loss and so causes cavitation
while every indication at the pump itself appears normal — the deaerator level is fine, the
upstream valves are open, and only the pump is complaining. It is among the most frequently
missed causes of feed-pump vibration. See TS-BFP-VIB-CAV §3.1.

1. Read the differential monthly, at a known flow. Differential varies with flow, so a
   reading without a flow reference is not comparable.
2. Below 0.3 bar is clean. Above 0.5 bar, plan a clean at the next opportunity. Above
   1.0 bar, clean it now.
3. Clean at the interval in §2 regardless of differential. A strainer that has never needed
   cleaning is more likely to be bypassing than clean.
4. Inspect the element for damage every time it is removed. **A holed strainer element
   passes debris to the impeller and is worse than a blocked one**, and it reads as clean
   on the differential gauge.
5. Record what the strainer contained. Repeated heavy fouling from the same source is a
   condition to correct upstream, not a cleaning interval to shorten.

### §4.2 Mechanical seals

1. Inspect annually and whenever leakage is reported.
2. Confirm the flush system is delivering flow at the correct temperature and pressure
   before returning the pump to service. Seal faces run dry within seconds without flush.
3. Weeping — a few drops per minute, no spray — is a planned replacement. Schedule it;
   do not defer it indefinitely, because weeping progresses.
4. Spraying or a continuous stream is an immediate shutdown. At feedwater temperature this
   is a personnel hazard as well as an equipment one.
5. On replacement, inspect the faces before discarding them. Even heat-checking indicates
   loss of flush; uneven wear indicates shaft deflection or misalignment; abrasive scoring
   indicates solids in the flush, which points back to §4.1.
6. Never reuse a seal face. Never run a seal dry, including on short test runs.

## §5 Condition-based inspection intervals

### §5.1 Principle

A fixed interval is a compromise made in the absence of information: too short and sound
machines are opened, which itself introduces failures; too long and failures happen in
service. Where condition data exists, it governs.

This section defines what condition indications shorten an interval, and by how much. A
condition-based trigger always overrides the planned interval in §2. It is never valid to
defer an inspection because the planned date has not arrived.

### §5.2 Vibration trending

Review monthly against the machine's own baseline, recorded at commissioning and after each
overhaul. Absolute values matter less than change: a machine that has always run at
3.5 mm/s is healthier than one that has risen from 1.5 to 3.0 mm/s.

| Indication | Interpretation | Action |
|---|---|---|
| Stable at or below baseline +1 mm/s | Normal | Continue monthly review |
| Rising, under 20% over three months | Early deterioration | Monthly spectrum; review at next month |
| Rising, over 20% over three months | Active deterioration | Inspect within 3 months |
| Above alert setpoint | Confirmed problem | Diagnose per TS-BFP-VIB-CAV §4; inspect within 1 month |
| Rising toward the trip setpoint | Failure in progress | **Plan an outage now** |
| Doubling within one month | Rapid progression | Inspect at the first opportunity |

Rising amplitude approaching a trip setpoint is the clearest opportunity a maintenance
organisation gets: it converts an unplanned trip, with its consequential damage and its
unscheduled outage, into a planned intervention at a time of the plant's choosing. The cost
difference is typically an order of magnitude. Acting on this trend is the main purpose of
vibration monitoring.

### §5.3 Bearing inspection triggers

Inspect the bearings, ahead of any planned interval, on any one of:

- Vibration trend per §5.2 indicating inspection.
- Bearing metal temperature risen more than 10 °C above the established baseline at
  equivalent load and cooling conditions.
- Two consecutive oil analyses showing rising bearing-metal wear elements (§3.3).
- Bearing metal debris in a filter element (§3.2).
- Falling lube oil pressure with level, filters and oil pump confirmed sound (§3.4).
- Any vibration spectrum showing discrete bearing defect frequencies
  (TS-BFP-VIB-CAV §2).
- Operation through a period of known cavitation or sustained low flow — cavitation loads
  bearings dynamically beyond design, so the bearings are a casualty of the process
  excursion even though the excursion was not mechanical. See TS-BFP-VIB-CAV §3.2.

### §5.4 Alignment

Check annually and after any work that disturbs the coupling, the bearing housings, the
foundation or the pipework.

Tolerances at operating temperature, for a flexible coupling on a machine of this class:

| Measure | Tolerance |
|---|---|
| Parallel (radial) offset | 0.05 mm |
| Angular offset | 0.05 mm per 100 mm of coupling diameter |
| Soft foot | 0.05 mm |

Align cold with the calculated thermal offset applied, and verify hot where practical.
Misalignment appears as a strong 2× running-speed component with an elevated axial
reading — see TS-BFP-VIB-CAV §2 — and is a common cause of repeat bearing and seal
failures, because a misaligned machine destroys each new bearing on the same schedule.

Check pipe strain at the same time. Pipework that pulls the casing out of alignment when
its flange bolts are tightened produces exactly the same symptoms as a coupling
misalignment and is not corrected by re-aligning the coupling.

### §5.5 Escalation to engineering

Refer to the rotating equipment engineer, rather than continuing to manage the condition
through maintenance activity, when any of the following holds:

- An alarm has recurred more than ten times in ninety days on a criticality 4 or 5 asset.
- The occurrence rate of a recurring alarm is increasing, judged by comparing the first and
  second halves of the review window. An accelerating rate means the mechanism is
  progressing and the remaining time is unknown.
- The same failure has recurred within one year of a repair. This indicates the root cause
  was not addressed by that repair, and repeating the repair will produce the same result.
- A condition-based trigger in §5.3 has fired twice without the cause being established.

## §6 Alarm rationalization

### §6.1 Why this sits in a maintenance manual

Alarm configuration is a maintenance-managed asset property, like a setpoint or a
calibration. A badly configured alarm degrades the operator's ability to respond to every
other alarm, so it is a plant-wide reliability concern rather than a control-system detail.

The failure mode is well established: when operators receive many alarms that do not
require or reward action, they stop treating alarms as requiring action. Serious incidents
across the industry repeatedly feature a real alarm that was correctly annunciated and
disregarded because it arrived among many that did not matter.

### §6.2 Rationalization candidates

Review quarterly. An alarm is a candidate for reconfiguration if it meets any of:

- **Chattering** — repeatedly activates and self-clears within minutes. Almost always a
  deadband set inside the measurement's normal noise band, or a missing on-delay.
- **Fleeting** — clears before an operator could reasonably respond. Conveys no actionable
  information.
- **Standing** — active for long periods, so it conveys no event information and masks the
  annunciator.
- **Consequential** — a predictable downstream effect of another alarm already annunciated.
- **High-frequency without action** — activates often and is acknowledged without any
  operator action being taken or required.
- **Duplicated** — the same condition annunciated by more than one alarm.

Each candidate requires the observed activation count over a defined window, the
measurement's normal variability, and a statement of what the operator is expected to do.
An alarm with no defined operator response should not exist.

### §6.3 Corrections, and what is not a correction

Appropriate corrections: widen the deadband to sit outside normal measurement noise; add an
on-delay matched to the process time constant; filter the measurement; change the setpoint;
change the priority; or remove the alarm where it has no operator response.

**Suppression is not a correction.** Suppressing or shelving an alarm to stop the nuisance,
without changing what makes it a nuisance, leaves the condition unannunciated and the record
showing it was addressed. Suppression is permitted only with documented authorisation from
the responsible engineer, for a stated duration, with the reason recorded and an expiry
that is actively reviewed.

Reducing an alarm's priority is also not a correction where the underlying condition is
real and serious. A high-severity alarm that recurs because the equipment problem has not
been fixed is correctly configured and correctly annunciating — it is telling the truth. It
is a maintenance backlog item, not a rationalization item, and reclassifying it converts a
visible equipment problem into an invisible one.

### §6.4 Distinguishing the two cases

The distinction matters because the two look identical in an alarm count and have opposite
correct responses.

| Evidence | Reading |
|---|---|
| High count, short durations, self-clearing, no corroborating measurement change | Nuisance — reconfigure per §6.3 |
| High count, and the measured value genuinely crossed the setpoint each time | Real condition — raise under §7 |
| High count, rate increasing, corroborating symptoms present | Developing failure — escalate per §5.5 |
| High count, other alarms on the same asset also elevated | Look for a common cause before treating any of them as nuisance |

When the evidence is ambiguous, treat the alarm as real. The cost of investigating a
nuisance alarm is an engineer's afternoon; the cost of rationalising away a real one is
the failure it was warning about.

## §7 Failure reporting

### §7.1 What to report

Raise a failure report for: any trip, any unplanned shutdown, any component replaced
outside its planned interval, any repeat of a failure within one year, and **any alarm that
has recurred persistently without its cause being established**.

The last of these is the one most often omitted, because nothing has visibly broken. An
alarm recurring ten or more times over ninety days is reporting a condition that has not
been corrected, and per-occurrence acknowledgement is treating the symptom. Without a report
there is no owner, no investigation and no record, and the alarm continues until the
equipment fails.

### §7.2 Contents

1. Asset tag, date and time, and operating state at the time of the event.
2. The alarm or symptom, with its occurrence count over a stated window and whether the
   rate is increasing.
3. All relevant process data: for a pump, suction and discharge pressure, flow, vibration
   at both bearings, bearing temperatures, lube oil pressure and temperature.
4. Vibration spectra where taken. Attach them; a spectrum cannot be reconstructed later and
   is the most valuable single record for the investigation.
5. Any precursor alarms and their timing relative to the event. Where one alarm reliably
   precedes another, state the lead time — this is frequently the finding that identifies
   the cause.
6. What was done, and whether it resolved the condition.
7. The assessed cause, and explicitly whether the root cause was addressed or only the
   symptom. A repair that restored service without addressing the cause must say so, or the
   repeat failure will be investigated from scratch.

### §7.3 Review

Review reports monthly for repeats and for common causes across assets. A failure that has
occurred on one pump usually applies to its siblings in the same service: check whether the
same condition is developing on the standby machine before it is called on to run.
