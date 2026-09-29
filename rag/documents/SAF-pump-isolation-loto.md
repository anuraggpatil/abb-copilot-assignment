---
doc_id: SAF-PUMP-LOTO
title: Pump Isolation and Lockout/Tagout
doc_type: safety_procedure
revision: 9
effective_date: 2026-04-01
owner: Health, Safety and Environment
applies_to:
  - Pumps
  - Rotating equipment
safety_critical: true
review_cycle_months: 12
---

# Pump Isolation and Lockout/Tagout

## §1 Purpose and authority

This procedure establishes the mandatory isolation requirements before any hands-on work on
a pump or its driver. It applies to all personnel, all pumps, and all work — including work
believed to be brief, non-invasive, or inspection-only.

**This procedure overrides operational convenience, production pressure, and every other
procedure in conflict with it.** No person may be instructed to omit a step in this
procedure, and no person may be penalised for refusing to work on equipment that is not
isolated in accordance with it.

The hazards this procedure controls have all caused fatalities on equipment of this class:

- **Unexpected start-up.** A pump can be started remotely, by an automatic sequence, by a
  protective system, or by another person who does not know work is in progress.
- **Stored rotational energy.** A large multistage pump coasts for a considerable time after
  power is removed, and a coupling that appears still may not be.
- **Stored pressure.** A feed pump's casing and discharge line hold pressure after the pump
  stops. Discharge pressure on a boiler feed pump reaches 138 bar.
- **Stored thermal energy.** Feedwater at deaerator temperature flashes to steam on release
  to atmosphere, causing severe scalding at a distance from the opening.
- **Residual inventory.** A drained line is not an empty line.

## §2 Before any intervention

This section applies from the moment work is contemplated, before any tool is collected.
Nothing here requires a permit to be in place.

### §2.1 Do not restart a tripped pump

**A pump that has tripped must not be restarted until the cause of the trip has been
identified.** This is the single most frequently violated requirement in this procedure and
the one with the most serious consequences.

A protective trip has already detected a condition the machine cannot tolerate. Restarting
without knowing what that condition was:

- Compounds mechanical damage. A pump that tripped on overload because the shaft is binding
  will bind again, and each restart attempt adds damage to the motor, the coupling and the
  driven end.
- Exposes personnel. A restart drives a machine with an unknown fault, potentially with
  people standing at it.
- Destroys the evidence needed to diagnose the fault.

A **motor overload trip** specifically prohibits restart until the driven-end cause is
established. An overload means the motor was working harder than it should against
something. That something — shaft binding, seal seizure, coupling damage, a blocked
discharge, a jammed impeller — is still there. Repeated restarts against a mechanical
overload escalate damage each time and are a recognised cause of motor and coupling
failure.

Before any restart of a tripped pump: establish what tripped it, confirm the condition no
longer exists, and obtain authorisation from the responsible engineer. Where the cause
cannot be established, the pump stays stopped and is isolated under §3 for inspection.

### §2.2 Assess before touching

1. Confirm the equipment's identity against its tag. Working on the wrong machine is a
   recurring cause of serious incident, and the standby pump is usually adjacent, similar
   and running.
2. Determine every energy source: electrical supply to the motor, process fluid on suction
   and discharge, seal flush, lube oil, cooling water, instrument air, and any auxiliary
   drive.
3. Determine what is stored: pressure, temperature, rotational energy, and inventory.
4. Determine what could start the pump: manual control, automatic sequence, protective
   system action, standby-pump auto-start logic, or another person.
5. Confirm the process can tolerate the pump being unavailable, and that the standby is
   available and confirmed running where the duty requires it.

### §2.3 Work that does not require isolation

A deliberately short list. Only the following may be performed on a running pump:

- Reading local gauges and indicators without contact.
- Visual inspection from outside the guarding, without opening any guard.
- Listening, including with a contact stethoscope applied to a designated, guarded point.
- Vibration measurement at permanently installed, accessible measurement points.
- Thermography from outside the guarding.
- Oil level and clarity observation through the sight glass.

Anything not on this list requires isolation. Specifically requiring isolation: opening any
guard, sampling oil, changing a filter element on a non-duplex system, touching the
coupling, adjusting packing or gland, tightening any fastener, and any work on the seal or
its flush.

If uncertain whether an activity is on this list, it is not.

### §2.4 Permit

A permit to work is required for all work requiring isolation. The permit is raised by the
work party, authorised by the area authority, and must state the isolation boundary, the
energy sources isolated, and the intended scope. Work outside the stated scope requires the
permit to be amended before that work begins, not afterwards.

## §3 Isolation sequence

Perform in this order. The order exists because each step depends on the one before it, and
reordering them has caused injury.

### §3.1 Electrical isolation

1. Stop the pump using normal controls per its operating procedure — for Boiler Feed
   Pump 101, OP-BFP-101 §3.1. An emergency stop is not an isolation.
2. Confirm the pump has stopped, locally, at the machine. A remote indication is not
   confirmation.
3. Disable the automatic start logic, including the standby-pump auto-start that may call
   this pump. Verify the disable locally.
4. Open the motor circuit breaker or isolator at the switchgear.
5. **Lock the isolator in the open position** with a personal lock, and attach a tag showing
   who applied it, when, and why. Where more than one person is working, each applies their
   own lock via a multi-lock hasp. The last lock off is the last person out.
6. **Prove dead.** Test for absence of voltage at the point of work with an instrument
   proven immediately before and immediately after the test. A tested-dead circuit with an
   unproven instrument is untested.
7. Attempt a start from the control room and confirm the pump does not start. This verifies
   the isolation rather than assuming it.

### §3.2 Mechanical and process isolation

1. Close the suction isolation valve. Lock it closed and tag it.
2. Close the discharge isolation valve. Lock it closed and tag it.
3. Close the minimum-flow recirculation isolation. This is frequently forgotten and leaves a
   live path to the casing.
4. Isolate the seal flush supply and return.
5. Isolate the lube oil supply where it is externally fed, **after** confirming the pump has
   come fully to rest. Removing lubrication from a coasting machine damages the bearings.
6. Isolate cooling water.
7. Isolate instrument air to any actuated valve inside the boundary.
8. Where the work requires breaking containment, a single closed valve is not sufficient
   isolation. Use double block and bleed, or a positive physical isolation — a spade or a
   removed spool piece — on any line that can deliver feedwater at temperature to the point
   of work.

### §3.3 De-energise stored energy

1. Confirm the shaft has come to a complete rest. Observe, do not assume; a large
   multistage pump coasts for minutes. Never place a hand on a coupling to check.
2. Vent the casing and lines within the isolation boundary to a safe location, through a
   route designed for it. Feedwater at temperature flashes on release: stand clear of the
   vent path, and never vent to an occupied area or to an open floor drain.
3. **Confirm zero pressure at a gauge inside the isolation boundary**, not at a gauge
   outside it. A gauge on the wrong side of a closed valve reads the system, not the
   equipment.
4. Drain the casing to the designated drain. Allow for residual inventory after the flow
   stops.
5. Allow the machine to cool to below 45 °C before contact. Recording the surface
   temperature is preferable to judging it.
6. Where the work involves the rotating assembly, apply a mechanical restraint to prevent
   rotation from residual flow, thermosyphon or an adjacent machine's vibration.

### §3.4 Verify before starting work

The work party leader confirms, at the machine, and records on the permit:

1. Every energy source identified in §2.2 is isolated, locked and tagged.
2. The isolation has been proved, not assumed — voltage tested dead, pressure read at zero
   inside the boundary.
3. The shaft is at rest and restrained where required.
4. Surface temperature is safe for contact.
5. Every member of the work party has applied a personal lock.

Any person may stop the work at this point if a step cannot be verified. That is not an
escalation; it is the procedure operating correctly.

## §4 Restoring to service

1. Confirm all work is complete, the work area is clear, all tools are accounted for, and
   all guards are refitted. A missing guard is a stop.
2. Confirm every person is clear of the machine and has removed their personal lock. **No
   lock may be removed by anyone other than the person who applied it**, except under the
   documented lock-removal authorisation in §5.3.
3. Remove mechanical restraints.
4. Restore auxiliary systems first: cooling water, lube oil, seal flush. Confirm each is
   established and reading normally before the main machine is available. A pump started
   without seal flush destroys its seals within seconds.
5. Restore process isolations: open suction fully, then set discharge and recirculation to
   their startup positions per the operating procedure.
6. Remove locks and tags from the electrical isolator and close it.
7. Re-enable automatic start logic, including the standby auto-start disabled in §3.1
   step 3. Verify it is enabled — an auto-start left disabled removes the plant's protection
   against loss of feedwater, and its absence will not be noticed until it is needed.
8. Close the permit.
9. Start per the operating procedure, and record the post-work baseline readings. For
   Boiler Feed Pump 101, OP-BFP-101 §2, including the vibration and temperature baseline at
   step 11 — post-overhaul readings become the reference for all subsequent condition
   monitoring under MM-CP-MAINT §5.2.

## §5 Special cases

### §5.1 Shared and cross-connected systems

Where a pump shares a lube oil system, seal flush, or cooling water header with another
machine, isolating this pump's supply may affect the other. Confirm the effect before
isolating, and where the other machine is running, arrange the isolation so it retains its
supply. A boundary drawn around one machine on a drawing does not always match the piping.

### §5.2 Testing that requires energy

Some commissioning and fault-finding activities require the machine to be energised — motor
rotation checks, protective function tests, valve stroking. These are performed under a
separate test authorisation, not under this procedure, with the work party clear of the
machine and a single nominated person in control of the energy source. The routine isolation
in §3 is restored immediately the test is complete.

### §5.3 Removing another person's lock

Permitted only when the person who applied it cannot be contacted and their absence is
confirmed, and only with written authorisation from the area authority after: confirming the
person is off site, confirming the work associated with the lock is complete or made safe,
and physically inspecting the machine. Every such removal is recorded and reviewed. This is
an exceptional measure, not an administrative convenience for shift handover.

### §5.4 Emergency intervention

Nothing in this procedure prevents action to make equipment safe in an emergency, or to
reach an injured person. Where the isolation sequence is abbreviated under emergency
conditions, the abbreviated steps are recorded afterwards and the event is reviewed. Do not
enter the machine space of a running pump in any circumstance short of reaching a person.
