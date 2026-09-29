---
doc_id: OP-BFP-101
title: Boiler Feed Pump 101 — Operating Procedure
doc_type: operating_procedure
revision: 7
effective_date: 2026-03-01
owner: Unit 2 Operations
applies_to:
  - Boiler Feed Pump 101
  - Boiler Feed Pump 102
site: EastRefinery
unit: Unit 2
review_cycle_months: 24
---

# Boiler Feed Pump 101 — Operating Procedure

## §1 Scope and applicability

This procedure governs routine and abnormal operation of Boiler Feed Pump 101 (tag
`2-BFP-101`), a five-stage horizontal centrifugal pump supplying feedwater to Boiler 2 from
the No. 2 deaerator. It applies to Boiler Feed Pump 102 except where a step is marked
**101 only**.

Design duty: 185 m³/h at 138 bar discharge, 3565 rpm, motor rated 1250 kW. Minimum
continuous stable flow is **55 m³/h**. Required NPSH at design flow is **4.1 m**; the
available NPSH at normal deaerator level is 7.3 m, giving a design margin of 3.2 m.

This procedure does not cover mechanical repair (see MM-CP-MAINT), vibration diagnosis
(see TS-BFP-VIB-CAV), or isolation for maintenance (see SAF-PUMP-LOTO). Where this
procedure and a permit-to-work instruction disagree, the permit takes precedence.

### Setpoint summary

| Parameter | Alert | Trip | Units |
|---|---|---|---|
| Suction pressure | 8.5 | 6.0 | bar g |
| Discharge pressure (low) | 120 | 105 | bar g |
| Discharge flow (low) | 60 | 52 | m³/h |
| Bearing vibration, velocity RMS | 4.5 | 7.1 | mm/s |
| Bearing metal temperature | 85 | 95 | °C |
| Lube oil pressure | 1.4 | 1.0 | bar g |
| Lube oil temperature | 60 | 70 | °C |
| Deaerator level (low) | 35 | 20 | % |

Alert setpoints raise a high-severity alarm and require operator response within the
shift. Trip setpoints actuate the protective system and stop the pump.

## §2 Startup

Startup is permitted only with a completed pre-start checklist and the boiler in a state
that can accept feedwater.

1. Confirm the pump is not under a lockout (see SAF-PUMP-LOTO §4) and all permits are
   closed.
2. Verify deaerator level above 45% and rising or steady. Do not start against a falling
   level.
3. Open the suction isolation valve fully. Confirm suction pressure is at least 9.5 bar g
   with the pump stopped.
4. Confirm the suction strainer differential is below 0.3 bar. A higher reading indicates
   fouling; refer to MM-CP-MAINT §4 before starting.
5. Start the lube oil system and confirm oil pressure stabilises above 1.8 bar g and oil
   temperature is above 25 °C. Use the oil heater if the pump has been idle in cold
   weather.
6. Open the minimum-flow recirculation valve fully. The pump must start with a guaranteed
   flow path.
7. Confirm the discharge valve is closed or at its minimum position.
8. Start the pump. Confirm discharge pressure develops within 10 seconds.
9. Open the discharge valve gradually over not less than 2 minutes, watching flow and
   motor current.
10. Once flow exceeds 75 m³/h, place the minimum-flow recirculation valve in automatic.
11. Record startup vibration, bearing temperatures and lube oil pressure in the shift log.
    These become the baseline for condition monitoring under MM-CP-MAINT §5.

Do not restart a pump that has tripped until the cause of the trip has been identified.
A motor overload trip specifically prohibits restart — see SAF-PUMP-LOTO §2.

## §3 Normal and emergency shutdown

### §3.1 Normal shutdown

1. Confirm the standby feed pump is running and carrying the load, or that the boiler can
   tolerate loss of feedwater.
2. Open the minimum-flow recirculation valve fully.
3. Reduce discharge flow gradually to the minimum continuous flow of 55 m³/h.
4. Close the discharge valve to its minimum position.
5. Stop the pump.
6. Run the lube oil system for a further 15 minutes to remove heat from the bearings. Do
   not stop the oil pump immediately after the main pump.
7. Leave the suction valve open and the casing full unless the pump is being handed over
   for maintenance.
8. Record running hours and final vibration readings.

### §3.2 Controlled shutdown on an abnormal condition

Use this when a parameter has reached its alert setpoint and cannot be recovered, but the
trip has not yet actuated. The intent is to stop the pump on the operator's terms rather
than the protective system's.

1. Notify the unit supervisor and the control room before starting.
2. Transfer load to the standby pump first wherever possible.
3. Follow §3.1, but compress steps 3 and 4 to under 60 seconds if bearing metal
   temperature is rising faster than 1 °C per minute.
4. Do not delay the shutdown to complete record-keeping.

A controlled shutdown is always preferable to a trip: it avoids the thermal and mechanical
shock of an instantaneous stop at full speed, and it leaves the machine in a known state.

### §3.3 Emergency shutdown

Stop the pump immediately, without transferring load, if any of the following occur:

- Bearing vibration exceeds 7.1 mm/s velocity RMS.
- Bearing metal temperature exceeds 95 °C.
- Lube oil pressure falls below 1.0 bar g and does not recover within 10 seconds.
- Loud, irregular noise, visible smoke, or mechanical seal failure with escaping
  feedwater.
- Loss of suction with cavitation audible at the pump.

After an emergency shutdown, isolate the pump per SAF-PUMP-LOTO §3 and do not attempt a
restart until engineering has reviewed the event. Record the trip under MM-CP-MAINT §7.

## §4 Abnormal condition response

### §4.1 General principles

Three rules apply to every abnormal condition below.

**Verify before acting.** Every response in this section begins with confirming the
reading against a second indication. Instrument faults present exactly as process
excursions, and the corrective actions are opposite: a real low suction pressure needs
flow reduced, while a failed transmitter needs the reading disregarded and the instrument
raised for repair. Acting on a false reading can create the problem it was meant to
prevent.

**Treat the cause, not the alarm.** An alarm that has been acknowledged twenty times in
ninety days has not been addressed. Repetition is evidence that the underlying condition
persists; raise it under MM-CP-MAINT §7 rather than acknowledging it again.

**Protect the machine while diagnosing.** Establishing a guaranteed flow path and a sound
oil film takes seconds and is almost never wrong. Do this before investigating.

### §4.2 Low suction pressure response

Low suction pressure is the most frequent abnormal condition on this pump and the most
common root cause of downstream vibration and bearing damage. It reduces the available
NPSH; when available NPSH falls below the 4.1 m requirement, the pump cavitates. See
TS-BFP-VIB-CAV §3 for the mechanism.

On a suction pressure alert at 8.5 bar g:

1. Confirm suction pressure on the local gauge as well as the DCS transmitter. Divergence
   greater than 0.5 bar indicates an instrument fault — see §6.
2. Check deaerator level and trend. A falling level is the most common cause. Verify the
   level indication against the sight glass; deaerator level transmitters are prone to
   drift with steam-space pressure changes.
3. Verify the upstream valve line-up. Confirm the suction isolation valve is fully open
   and has not drifted; confirm no maintenance isolation has been left partially closed.
   **The pump cannot develop suction pressure that the upstream system is not supplying** —
   if the deaerator is not delivering, no action at the pump will correct it.
4. Check the suction strainer differential. A partially blocked strainer produces an
   identical symptom at the pump and is easily overlooked because the deaerator appears
   healthy. Above 0.5 bar differential, plan a strainer clean under MM-CP-MAINT §4.
5. Confirm deaerator pressure and temperature are consistent. Feedwater close to its
   saturation temperature flashes at the pump inlet and cavitates even at an apparently
   adequate pressure.
6. Open the minimum-flow recirculation valve to hold the pump above minimum flow while
   suction is being restored — see §4.3.
7. If suction pressure continues to fall toward the 6.0 bar g trip, reduce discharge flow
   to lower the NPSH requirement. Flow reduction is the only action at the pump that
   increases NPSH margin.
8. If suction pressure cannot be held above the trip setpoint, execute a controlled
   shutdown per §3.2 rather than waiting for the trip.

Do not attempt to run the pump through sustained cavitation to maintain boiler feed. Ten
minutes of cavitation causes impeller and bearing damage that takes weeks to repair;
boiler load can be reduced in seconds.

### §4.3 Minimum flow protection

A centrifugal pump running below minimum continuous stable flow recirculates internally.
The recirculated energy has nowhere to go and appears as heat, radial hydraulic thrust,
and low-frequency vibration. On this pump, sustained operation below 55 m³/h damages the
impeller and the thrust bearing, and it does so whether or not suction pressure is normal.

1. On a low discharge flow alert at 60 m³/h, confirm the flow reading against motor
   current. Motor current falling with indicated flow confirms a real reduction; current
   holding steady while indicated flow falls points to an instrument fault.
2. Confirm the minimum-flow recirculation valve is open and passing. Verify by the
   temperature rise across the recirculation line, not by valve position feedback alone —
   a stuck valve frequently reports open.
3. If the valve is not passing, open it manually at the local station.
4. If minimum flow cannot be established, execute a controlled shutdown per §3.2. Running
   a feed pump with no flow path is worse than stopping it.
5. Never isolate or defeat minimum-flow protection to satisfy a boiler demand. If the
   protection is preventing the duty required, the duty is wrong.

The recirculation valve is placed in automatic only above 75 m³/h (§2 step 10) to leave
margin between the automatic-control threshold and the alert setpoint.

### §4.4 High vibration response

Bearing vibration alerts at 4.5 mm/s velocity RMS and trips at 7.1 mm/s. Vibration is a
symptom with several possible causes, and the responses diverge sharply. Do not treat a
vibration alarm as a bearing problem until the process causes have been excluded.

1. Record vibration amplitude **and spectrum** at both the drive-end and non-drive-end
   bearings. Amplitude alone cannot distinguish the causes. Use TS-BFP-VIB-CAV §2 to
   interpret the spectrum.
2. Check suction pressure and calculate NPSH margin (§4.2). Cavitation is the most common
   cause on this asset, and it is corrected at the process rather than at the bearing.
3. Check lube oil pressure, temperature and filter differential (§5.1). Loss of oil film
   produces rising vibration and rising bearing temperature together, and it is
   independently correctable.
4. Check bearing metal temperatures. Vibration rising with temperature indicates
   mechanical distress; vibration rising with steady temperature points to a hydraulic
   cause.
5. Trend the amplitude against the alert and trip setpoints. A rising trend approaching
   the trip converts an unplanned trip into a planned outage if acted on early — schedule
   an inspection under MM-CP-MAINT §5.
6. At 7.1 mm/s, stop the pump per §3.3. Do not attempt to ride out a vibration trip.

**Recurring vibration alarms with an accelerating rate are a developing failure, not a
nuisance.** If the alarm has occurred more than ten times in ninety days, or if more than
a quarter of occurrences fall in the most recent fortnight, escalate to the rotating
equipment engineer rather than continuing to acknowledge it.

## §5 Auxiliary systems

### §5.1 Lube oil system

The lube oil system supplies filtered oil at 1.8–2.4 bar g to both journal bearings and
the thrust bearing. It comprises a main shaft-driven pump, a motor-driven standby pump, a
duplex filter, a cooler and a 400-litre reservoir.

Loss of lube oil pressure is the precursor most often seen before bearing vibration on
this asset. Oil pressure falling into the alert band means the oil film supporting the
bearing is thinning; vibration follows within the hour as metal-to-metal contact begins.
Treat a lube oil pressure alarm as an early warning of bearing damage, not as an auxiliary
system nuisance.

On a lube oil pressure alert at 1.4 bar g:

1. Confirm oil level in the reservoir. A falling level indicates an external leak — locate
   it before it empties the reservoir.
2. Check the duplex filter differential. Above 1.0 bar, switch to the standby filter
   element and raise the used element for replacement under MM-CP-MAINT §3.
3. Check the oil pump discharge pressure directly. Low discharge with adequate level and
   clean filters indicates a failed or worn oil pump.
4. **Start the standby lube oil pump** and confirm pressure recovers above the 1.4 bar g
   alert setpoint. This is the fastest way to restore the oil film and takes priority over
   completing the diagnosis.
5. Check oil temperature. Oil above 70 °C loses viscosity and will not maintain film
   pressure; verify cooling water flow to the oil cooler.
6. If pressure cannot be restored above 1.4 bar g, prepare a controlled shutdown per
   §3.2. Continued operation without lubrication causes rapid bearing damage that is
   disproportionately expensive relative to the cost of stopping.
7. Below 1.0 bar g, the pump trips. Do not defeat this protection.

### §5.2 Seal flush system

The mechanical seals are flushed with cooled, filtered feedwater from a dedicated
circulation loop (API Plan 23). Seal flush protects the seal faces from the pumped fluid's
temperature and from any solids carried through the strainer.

1. Confirm flush flow is established before startup and remains established throughout
   operation. Seal faces run dry within seconds of losing flush.
2. Monitor seal chamber temperature. A rise above 70 °C indicates loss of flush
   circulation or a fouled seal cooler.
3. Inspect for leakage at each shift change. A weeping seal is a planned replacement under
   MM-CP-MAINT §4; a spraying seal is an immediate shutdown under §3.3.
4. Never run the pump with the flush isolated, including during short test runs.

### §5.3 Cooling water

Cooling water serves the lube oil cooler, the seal flush cooler and the bearing housing
jackets. Confirm supply pressure above 2.5 bar g and return temperature below 45 °C. Loss
of cooling water manifests first as rising oil temperature (§5.1 step 5) rather than as a
cooling water alarm, so do not wait for a dedicated alarm.

## §6 Instrumentation checks

Instrument faults are indistinguishable from process excursions at the DCS and account for
a substantial share of alarms on this asset. Verification is therefore the first step in
every response in §4 rather than an afterthought.

### §6.1 Verifying a reading

1. Compare the DCS value against the local gauge or a second transmitter on the same
   service.
2. Check whether the value moved in a physically plausible way. A step change to a round
   number, a flatlined reading, or a value outside the instrument's range is an instrument
   fault, not a process event.
3. Check whether related measurements moved consistently. A genuine flow reduction shows
   up in motor current and discharge pressure; a transmitter fault does not.
4. Where the second indication disagrees, treat the reading as unreliable, raise the
   instrument for repair, and monitor the service by its confirming indication until the
   repair is complete.

### §6.2 Chattering and self-clearing alarms

An alarm that repeatedly activates and clears within a short period conveys no information
and consumes operator attention that real events need. On this asset the discharge flow
deviation alarm is the most frequent offender, driven by a deadband set too tightly around
a naturally noisy measurement.

Chattering alarms are corrected by changing the alarm configuration, not by operator
action. Raise them as rationalization candidates under MM-CP-MAINT §6 with the observed
activation count and the measurement's normal variability. Do not suppress a chattering
alarm locally without authorisation — a suppressed alarm that later matters is the
mechanism behind a high proportion of serious incidents.

### §6.3 Scheduled verification

| Instrument | Check | Interval |
|---|---|---|
| Suction and discharge pressure | Against local gauge | Weekly |
| Discharge flow | Against motor current and heat balance | Monthly |
| Vibration probes | Against portable analyser | Monthly |
| Bearing RTDs | Against portable pyrometer at housing | Quarterly |
| Deaerator level | Against sight glass | Weekly |
| Trip functions | Full functional test | Annually |

## §7 Records and handover

Record at every shift change: running hours, suction and discharge pressure, flow,
vibration at both bearings, bearing metal temperatures, lube oil pressure and temperature,
and any alarm that activated during the shift.

Record additionally, as a separate entry, any alarm that activated more than twice in the
shift, with the time of each activation. This is the raw material for the recurrence
analysis in MM-CP-MAINT §5 and §6, and it cannot be reconstructed later from the alarm
historian alone once the events have aged out.

Escalate to the unit supervisor at handover: any trip, any alarm at critical severity, any
alert setpoint reached that could not be recovered within the shift, and any alarm whose
occurrence rate is visibly increasing.
