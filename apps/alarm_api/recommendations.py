"""Rule-based operator action recommendations.

This is the simulator's one genuinely "advanced" operation, and the acceptance scenario's
"provide recommended actions" depends on it. Two design points are load-bearing:

1. **Recommendations are history-aware, not a lookup table.** The same
   `Bearing Vibration High` alarm yields different urgency and extra actions depending on
   how often it has recurred, whether the trend is worsening, and whether a known
   precursor (lube-oil pressure) fired shortly before. That is what makes the copilot's
   multi-step chaining worth doing: a single alarm read cannot produce this answer.

2. **Every action carries a `procedure_reference` naming a document section — but not the
   text.** The API asserts *which* procedure applies; only RAG can supply what it says.
   This is the seam that forces the two capabilities into one workflow: the reference is
   the query the retrieval step is derived from, and the synthesis step compares the
   API's suggested action against what the retrieved procedure actually instructs.
   Deliberately, these references are the only place the two halves of the system meet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from apps.alarm_api.domain import SEVERITY_ORDER, Alarm, AlarmStatus, Severity
from apps.alarm_api.store import AlarmFilter, AlarmStore

# Urgency ladder, strongest first. Strings rather than an enum because they cross the HTTP
# boundary and the MCP tool schema unchanged.
URGENCY_IMMEDIATE = "immediate"
URGENCY_SHIFT = "this_shift"
URGENCY_PLANNED = "planned"
URGENCY_MONITOR = "monitor"

_URGENCY_ORDER = {
    URGENCY_IMMEDIATE: 0,
    URGENCY_SHIFT: 1,
    URGENCY_PLANNED: 2,
    URGENCY_MONITOR: 3,
}


@dataclass(frozen=True)
class _Action:
    action: str
    rationale: str
    expected_outcome: str
    urgency: str
    procedure_reference: str
    requires_isolation: bool = False


# Keyed by alarm_name. Section numbers match the authored corpus in rag/documents/, so a
# reference returned here is actually retrievable.
_PLAYBOOK: dict[str, tuple[_Action, ...]] = {
    "Bearing Vibration High": (
        _Action(
            action=(
                "Record vibration amplitude and spectrum at the drive- and non-drive-end bearings"
            ),
            rationale=(
                "Distinguishes cavitation-driven broadband vibration from a mechanical "
                "cause such as misalignment or bearing wear, which need different responses."
            ),
            expected_outcome="A diagnosis that selects between the process and mechanical branches",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="TS-BFP-VIB-CAV §2 Symptom-to-cause matrix",
        ),
        _Action(
            action="Verify suction pressure and NPSH margin against the design value",
            rationale=(
                "Low suction pressure is the most common root cause of feed-pump vibration; "
                "confirming it early avoids an unnecessary bearing intervention."
            ),
            expected_outcome="Confirmed or excluded cavitation as the driver",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="OP-BFP-101 §4.2 Low suction pressure response",
        ),
        _Action(
            action="Check lube oil pressure, temperature and filter differential",
            rationale="Lube-oil loss precedes bearing distress and is independently correctable.",
            expected_outcome="Restored oil film, or a confirmed lube-system fault to repair",
            urgency=URGENCY_SHIFT,
            procedure_reference="MM-CP-MAINT §3 Bearing lubrication",
        ),
        _Action(
            action=(
                "Trend vibration against the alert and trip setpoints and schedule a bearing "
                "inspection"
            ),
            rationale=(
                "Rising amplitude toward the trip point converts an unplanned trip into a "
                "planned outage."
            ),
            expected_outcome="A planned intervention before the trip threshold is reached",
            urgency=URGENCY_PLANNED,
            procedure_reference="MM-CP-MAINT §5 Condition-based inspection intervals",
        ),
    ),
    "Lube Oil Pressure Low": (
        _Action(
            action="Confirm lube oil level, pump discharge pressure and filter differential",
            rationale="Isolates a supply-side loss from a blocked filter or failed oil pump.",
            expected_outcome="Identified cause of the pressure loss",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="MM-CP-MAINT §3 Bearing lubrication",
        ),
        _Action(
            action=(
                "Start the standby lube oil pump and verify pressure recovers above the alert "
                "setpoint"
            ),
            rationale="Restores the oil film before bearing metal temperature rises.",
            expected_outcome="Lube oil pressure back within the normal band",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="OP-BFP-101 §5 Auxiliary systems",
        ),
        _Action(
            action="If pressure cannot be restored, prepare a controlled shutdown",
            rationale=(
                "Continued operation without lubrication causes rapid, expensive bearing damage."
            ),
            expected_outcome="Pump stopped before mechanical damage occurs",
            urgency=URGENCY_SHIFT,
            procedure_reference="OP-BFP-101 §3 Normal and emergency shutdown",
            requires_isolation=True,
        ),
    ),
    "Suction Pressure Low": (
        _Action(
            action="Check deaerator/suction vessel level and upstream valve line-up",
            rationale="The pump cannot make suction pressure the upstream system is not supplying.",
            expected_outcome="Restored suction pressure, or an identified upstream fault",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="OP-BFP-101 §4.2 Low suction pressure response",
        ),
        _Action(
            action="Open the minimum-flow recirculation valve to keep the pump above minimum flow",
            rationale="Protects the pump from running back on its curve while suction is restored.",
            expected_outcome="Pump held in a safe operating window",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="OP-BFP-101 §4.3 Minimum flow protection",
        ),
        _Action(
            action="Inspect the suction strainer for fouling",
            rationale=(
                "A partially blocked strainer produces the same symptom and is easily missed."
            ),
            expected_outcome="Strainer cleaned or ruled out",
            urgency=URGENCY_PLANNED,
            procedure_reference="MM-CP-MAINT §4 Strainers and seals",
        ),
    ),
    "Discharge Flow Deviation": (
        _Action(
            action="Compare the flow transmitter against a second indication before acting",
            rationale=(
                "This alarm chatters on this asset; an instrument fault is more likely than a "
                "real process excursion."
            ),
            expected_outcome="Confirmed real deviation, or an instrument defect to raise",
            urgency=URGENCY_MONITOR,
            procedure_reference="OP-BFP-101 §6 Instrumentation checks",
        ),
        _Action(
            action=(
                "Raise the alarm as a rationalization candidate with a deadband or on-delay change"
            ),
            rationale=(
                "A repeatedly self-clearing alarm consumes operator attention without "
                "conveying information, masking real events."
            ),
            expected_outcome="Reduced nuisance alarm load on the unit",
            urgency=URGENCY_PLANNED,
            procedure_reference="MM-CP-MAINT §6 Alarm rationalization",
        ),
    ),
    "Motor Overload Trip": (
        _Action(
            action="Do not attempt a restart until the driven-end cause is identified",
            rationale=(
                "Repeated restarts against a mechanical or hydraulic overload escalate motor "
                "and coupling damage."
            ),
            expected_outcome="Prevented compounding damage",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="SAF-PUMP-LOTO §2 Before any intervention",
            requires_isolation=True,
        ),
        _Action(
            action=(
                "Apply lockout/tagout, then check for shaft binding, seal seizure and coupling "
                "condition"
            ),
            rationale="A safety-critical trip requires isolation before any hands-on inspection.",
            expected_outcome="Asset made safe and the mechanical cause found",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="SAF-PUMP-LOTO §3 Isolation sequence",
            requires_isolation=True,
        ),
        _Action(
            action=(
                "Record motor current, winding temperature and the trip timestamp for "
                "engineering review"
            ),
            rationale="Establishes whether the trip was electrical or driven-end in origin.",
            expected_outcome="Evidence for the failure investigation",
            urgency=URGENCY_SHIFT,
            procedure_reference="MM-CP-MAINT §7 Failure reporting",
        ),
    ),
    "Level Low": (
        _Action(
            action=(
                "Verify level transmitter reading against the sight glass and check make-up flow"
            ),
            rationale="A false low level and a real one demand opposite responses.",
            expected_outcome="Confirmed vessel inventory",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="OP-BFP-101 §4.2 Low suction pressure response",
        ),
        _Action(
            action="Confirm downstream pumps still have adequate NPSH",
            rationale="Falling inventory starves the pumps it feeds and causes cavitation damage.",
            expected_outcome="Downstream equipment protected",
            urgency=URGENCY_IMMEDIATE,
            procedure_reference="TS-BFP-VIB-CAV §3 Cavitation mechanism",
        ),
    ),
}

# Used when no alarm-specific playbook exists. Generic but still actionable — better than
# returning an empty list, and it keeps the response schema uniform.
_FALLBACK: tuple[_Action, ...] = (
    _Action(
        action="Acknowledge the alarm and verify the measured value against a second indication",
        rationale="Separates a genuine process excursion from an instrument fault.",
        expected_outcome="Confirmed whether the condition is real",
        urgency=URGENCY_SHIFT,
        procedure_reference="OP-BFP-101 §6 Instrumentation checks",
    ),
    _Action(
        action="Review the asset's recent alarm history for a repeating pattern",
        rationale="A recurring alarm points to an unresolved underlying condition.",
        expected_outcome="Identified pattern or confirmed one-off event",
        urgency=URGENCY_PLANNED,
        procedure_reference="MM-CP-MAINT §6 Alarm rationalization",
    ),
)

# Precursor -> consequent pairs the engine looks for in the asset's recent history. The
# lube-oil/vibration pair is the one planted by the seeder; naming it here is what turns a
# statistical co-occurrence into a stated contributing factor.
_KNOWN_PRECURSORS: dict[str, tuple[str, str]] = {
    "Bearing Vibration High": (
        "Lube Oil Pressure Low",
        "Loss of lube oil pressure shortly before the vibration alarm indicates the "
        "bearing oil film is breaking down rather than a purely hydraulic cause.",
    ),
    "Motor Overload Trip": (
        "Bearing Vibration High",
        "Rising vibration before an overload trip points to increasing mechanical drag on "
        "the driven end.",
    ),
}

PRECURSOR_WINDOW_MINUTES = 60


def recommend(
    store: AlarmStore,
    now: datetime,
    *,
    alarm: Alarm | None = None,
    asset_id: str | None = None,
    lookback_days: int = 90,
    max_recommendations: int = 6,
) -> dict[str, Any]:
    """Build recommendations for one alarm, or for an asset's most severe open alarm.

    `now` and `lookback_days` are explicit so the caller controls the history window; the
    engine never reads the clock.
    """
    if alarm is None:
        if asset_id is None:
            raise ValueError("either alarm or asset_id is required")
        alarm = _most_pressing_alarm(store, asset_id, now, lookback_days)

    asset = store.get_asset(alarm.asset_id)
    history = store.query(
        AlarmFilter(
            asset_ids=[alarm.asset_id],
            start_time=now - timedelta(days=lookback_days),
            end_time=now,
        )
    )
    same_name = [a for a in history if a.alarm_name == alarm.alarm_name]
    context = _build_context(store, alarm, history, same_name, now, lookback_days)

    actions = list(_PLAYBOOK.get(alarm.alarm_name, _FALLBACK))
    actions.extend(_history_driven_actions(alarm, context))

    escalated = _escalate(alarm, context)
    ranked = sorted(
        actions,
        key=lambda a: (
            _URGENCY_ORDER[URGENCY_IMMEDIATE if escalated and a.requires_isolation else a.urgency],
            actions.index(a),
        ),
    )[:max_recommendations]

    return {
        "alarm_id": alarm.alarm_id,
        "alarm_name": alarm.alarm_name,
        "asset_id": asset.asset_id,
        "asset_name": asset.name,
        "site": asset.site.value,
        "unit": asset.unit,
        "severity": alarm.severity.value,
        "status": alarm.status.value,
        "context": context,
        "recommendations": [
            {
                "rank": i + 1,
                "action": a.action,
                "rationale": a.rationale,
                "expected_outcome": a.expected_outcome,
                "urgency": a.urgency,
                "procedure_reference": a.procedure_reference,
                "requires_isolation": a.requires_isolation,
            }
            for i, a in enumerate(ranked)
        ],
        "escalation": _escalation_block(alarm, context, escalated, asset.criticality),
        "safety_notes": _safety_notes(ranked, alarm),
        # Named so the copilot knows these are pointers, not evidence. It must retrieve
        # the documents to quote them.
        "procedure_references": sorted({a.procedure_reference for a in ranked}),
        "generated_at": now,
        "disclaimer": (
            "Generated from alarm history and equipment rules. Verify against the current "
            "approved operating procedure and plant conditions before acting."
        ),
    }


def _most_pressing_alarm(
    store: AlarmStore, asset_id: str, now: datetime, lookback_days: int
) -> Alarm:
    """Highest-severity open alarm; failing that, the most recent alarm of any kind."""
    store.get_asset(asset_id)  # raises AssetNotFoundError for an unknown id
    rows = store.query(
        AlarmFilter(
            asset_ids=[asset_id],
            start_time=now - timedelta(days=lookback_days),
            end_time=now,
        )
    )
    if not rows:
        raise LookupError(f"no alarms for asset {asset_id} in the last {lookback_days} days")
    open_rows = [a for a in rows if a.status in (AlarmStatus.ACTIVE, AlarmStatus.ACKNOWLEDGED)]
    pool = open_rows or rows
    return max(pool, key=lambda a: (SEVERITY_ORDER[a.severity], a.start_time))


def _build_context(
    store: AlarmStore,
    alarm: Alarm,
    history: list[Alarm],
    same_name: list[Alarm],
    now: datetime,
    lookback_days: int,
) -> dict[str, Any]:
    acked = [a.ack_delay_minutes for a in same_name if a.ack_delay_minutes is not None]
    recent_cutoff = now - timedelta(days=14)
    precursor = _find_precursor(alarm, history)
    return {
        "lookback_days": lookback_days,
        "occurrences_in_window": len(same_name),
        "occurrences_last_14_days": sum(1 for a in same_name if a.start_time >= recent_cutoff),
        "total_alarms_on_asset": len(history),
        "first_seen": min((a.start_time for a in same_name), default=None),
        "last_seen": max((a.start_time for a in same_name), default=None),
        "avg_ack_delay_minutes": round(sum(acked) / len(acked), 1) if acked else None,
        "highest_severity_in_window": max(
            (a.severity for a in same_name), key=lambda s: SEVERITY_ORDER[s], default=alarm.severity
        ).value,
        "open_alarms_on_asset": sum(
            1 for a in history if a.status in (AlarmStatus.ACTIVE, AlarmStatus.ACKNOWLEDGED)
        ),
        "likely_precursor": precursor,
        "related_asset_ids": [a.asset_id for a in store.related_assets(alarm.asset_id)],
    }


def _find_precursor(alarm: Alarm, history: list[Alarm]) -> dict[str, Any] | None:
    """Look for the known precursor alarm inside the window before each occurrence."""
    pair = _KNOWN_PRECURSORS.get(alarm.alarm_name)
    if pair is None:
        return None
    precursor_name, explanation = pair
    occurrences = [a for a in history if a.alarm_name == alarm.alarm_name]
    candidates = [a for a in history if a.alarm_name == precursor_name]
    if not occurrences or not candidates:
        return None

    window = timedelta(minutes=PRECURSOR_WINDOW_MINUTES)
    leads: list[float] = []
    for occurrence in occurrences:
        preceding = [
            c
            for c in candidates
            if occurrence.start_time - window <= c.start_time < occurrence.start_time
        ]
        if preceding:
            closest = max(preceding, key=lambda c: c.start_time)
            leads.append((occurrence.start_time - closest.start_time).total_seconds() / 60.0)
    if not leads:
        return None
    return {
        "alarm_name": precursor_name,
        "preceded_count": len(leads),
        "of_occurrences": len(occurrences),
        "share": round(len(leads) / len(occurrences), 3),
        "median_lead_minutes": round(sorted(leads)[len(leads) // 2], 1),
        "window_minutes": PRECURSOR_WINDOW_MINUTES,
        "interpretation": explanation,
    }


def _history_driven_actions(alarm: Alarm, context: dict[str, Any]) -> list[_Action]:
    """Extra actions that only make sense given the asset's history."""
    out: list[_Action] = []
    occurrences = int(context["occurrences_in_window"])

    if occurrences >= 10:
        out.append(
            _Action(
                action=(
                    f"Raise a work request citing {occurrences} occurrences of "
                    f"'{alarm.alarm_name}' in {context['lookback_days']} days"
                ),
                rationale=(
                    "Repetition at this rate means the underlying condition has not been "
                    "corrected; per-occurrence acknowledgement is treating the symptom."
                ),
                expected_outcome="Root cause assigned to an owner rather than re-acknowledged",
                urgency=URGENCY_SHIFT,
                procedure_reference="MM-CP-MAINT §7 Failure reporting",
            )
        )

    recent = int(context["occurrences_last_14_days"])
    if occurrences >= 6 and recent * 4 > occurrences:
        out.append(
            _Action(
                action=(
                    "Escalate to the rotating equipment engineer: occurrence rate is accelerating"
                ),
                rationale=(
                    f"{recent} of {occurrences} occurrences fall in the last 14 days, so the "
                    "condition is deteriorating rather than stable."
                ),
                expected_outcome="Engineering assessment before failure",
                urgency=URGENCY_IMMEDIATE,
                procedure_reference="MM-CP-MAINT §5 Condition-based inspection intervals",
            )
        )

    precursor = context.get("likely_precursor")
    if isinstance(precursor, dict) and float(precursor["share"]) >= 0.5:
        out.append(
            _Action(
                action=(
                    f"Investigate '{precursor['alarm_name']}' as the upstream cause — it "
                    f"preceded {precursor['preceded_count']} of "
                    f"{precursor['of_occurrences']} occurrences by a median of "
                    f"{precursor['median_lead_minutes']} minutes"
                ),
                rationale=str(precursor["interpretation"]),
                expected_outcome="The driver addressed, not just its downstream symptom",
                urgency=URGENCY_IMMEDIATE,
                procedure_reference="TS-BFP-VIB-CAV §2 Symptom-to-cause matrix",
            )
        )

    avg_ack = context.get("avg_ack_delay_minutes")
    if isinstance(avg_ack, float) and avg_ack > 15.0 and occurrences >= 5:
        out.append(
            _Action(
                action="Review alarm priority and operator loading for this alarm",
                rationale=(
                    f"Mean acknowledgement delay of {avg_ack} minutes across {occurrences} "
                    "occurrences suggests alarm fatigue is delaying response."
                ),
                expected_outcome="Faster response, or a justified priority change",
                urgency=URGENCY_PLANNED,
                procedure_reference="MM-CP-MAINT §6 Alarm rationalization",
            )
        )
    return out


def _escalate(alarm: Alarm, context: dict[str, Any]) -> bool:
    if alarm.severity is Severity.CRITICAL:
        return True
    if (
        alarm.status is AlarmStatus.ACTIVE
        and SEVERITY_ORDER[alarm.severity] >= SEVERITY_ORDER[Severity.HIGH]
    ):
        return int(context["occurrences_in_window"]) >= 6
    return False


def _escalation_block(
    alarm: Alarm, context: dict[str, Any], escalated: bool, criticality: int
) -> dict[str, Any]:
    if not escalated:
        return {
            "required": False,
            "reason": "Severity and recurrence are within normal operator handling.",
        }
    if alarm.severity is Severity.CRITICAL:
        reason = f"Critical-severity {alarm.alarm_type.value} alarm."
    else:
        reason = (
            f"Active {alarm.severity.value}-severity alarm recurring "
            f"{context['occurrences_in_window']} times in "
            f"{context['lookback_days']} days."
        )
    return {
        "required": True,
        "escalate_to": "Rotating Equipment Engineer" if criticality >= 4 else "Unit Supervisor",
        "reason": reason,
        "asset_criticality": criticality,
    }


def _safety_notes(actions: list[_Action], alarm: Alarm) -> list[str]:
    notes: list[str] = []
    if any(a.requires_isolation for a in actions):
        notes.append(
            "One or more recommended actions require the asset to be isolated and locked "
            "out first. Follow SAF-PUMP-LOTO before any hands-on work."
        )
    if alarm.alarm_type.value == "safety":
        notes.append(
            "This is a safety-system alarm. Do not bypass, suppress or reset it without "
            "authorisation from the responsible engineer."
        )
    if alarm.severity is Severity.CRITICAL:
        notes.append("Critical alarm: notify the unit supervisor before changing operating state.")
    return notes
