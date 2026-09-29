"""Tests for the operator-action recommendation engine.

Mostly against handcrafted histories, because the behaviour under test is precisely "how
does the output change as the history changes" — 9 occurrences versus 10, a precursor 15
minutes before versus 3 hours before. Generated data cannot express those contrasts
deliberately.

One group at the end runs against the real generated world, to confirm the acceptance
scenario's asset actually produces the answer the scenario claims it will.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.alarm_api import recommendations as engine
from apps.alarm_api.domain import (
    Alarm,
    AlarmStatus,
    AlarmType,
    Asset,
    AssetType,
    Severity,
    Site,
)
from apps.alarm_api.seed import BFP101_NAME, World, build_world
from apps.alarm_api.store import AlarmFilter, AlarmStore

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


def _pump(criticality: int = 5) -> Asset:
    return Asset(
        asset_id="AST-PMP-9001",
        name="Test Pump",
        asset_type=AssetType.CENTRIFUGAL_PUMP,
        site=Site.EAST_REFINERY,
        unit="Unit 2",
        tag="2-TP-001",
        criticality=criticality,
        manufacturer="TestCo",
        model="T-1",
        commissioned_on=NOW - timedelta(days=3000),
        maintenance_strategy="condition_based",
    )


def _alarm(
    alarm_id: str,
    asset: Asset,
    name: str,
    *,
    at: datetime,
    severity: Severity = Severity.HIGH,
    alarm_type: AlarmType = AlarmType.PROCESS,
    status: AlarmStatus = AlarmStatus.CLEARED,
    ack: float | None = 5.0,
) -> Alarm:
    return Alarm(
        alarm_id=alarm_id,
        alarm_name=name,
        alarm_type=alarm_type,
        asset_id=asset.asset_id,
        asset_name=asset.name,
        site=asset.site,
        unit=asset.unit,
        severity=severity,
        status=status,
        start_time=at,
        ack_time=at + timedelta(minutes=ack) if ack is not None else None,
        clear_time=at + timedelta(minutes=60) if status is AlarmStatus.CLEARED else None,
        ack_delay_minutes=ack,
        duration_minutes=60.0 if status is AlarmStatus.CLEARED else None,
        priority_hint=2,
    )


def _store(asset: Asset, alarms: list[Alarm]) -> AlarmStore:
    alarms = sorted(alarms, key=lambda a: a.start_time)
    return AlarmStore(
        World(generated_at=NOW, seed=0, days_of_history=200, assets=[asset], alarms=alarms)
    )


# --- playbook selection -----------------------------------------------------------------


def test_known_alarm_uses_its_playbook() -> None:
    asset = _pump()
    alarm = _alarm("AL-1", asset, "Bearing Vibration High", at=NOW - timedelta(hours=2))
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)

    actions = " ".join(r["action"] for r in result["recommendations"])
    assert "vibration amplitude" in actions
    assert "suction pressure" in actions.lower()


def test_unknown_alarm_falls_back_rather_than_returning_nothing() -> None:
    """An empty recommendation list gives the copilot nothing to say."""
    asset = _pump()
    alarm = _alarm("AL-1", asset, "Totally Novel Condition", at=NOW - timedelta(hours=1))
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)
    assert result["recommendations"]
    assert "second indication" in result["recommendations"][0]["action"]


def test_max_recommendations_is_respected() -> None:
    asset = _pump()
    alarm = _alarm("AL-1", asset, "Bearing Vibration High", at=NOW - timedelta(hours=2))
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm, max_recommendations=2)
    assert len(result["recommendations"]) == 2
    assert [r["rank"] for r in result["recommendations"]] == [1, 2]


def test_immediate_actions_are_ranked_above_planned_ones() -> None:
    asset = _pump()
    alarm = _alarm("AL-1", asset, "Bearing Vibration High", at=NOW - timedelta(hours=2))
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)
    order = [engine._URGENCY_ORDER[r["urgency"]] for r in result["recommendations"]]
    assert order == sorted(order)


def test_every_recommendation_carries_a_procedure_reference() -> None:
    """The reference is the seam RAG retrieval is derived from; a blank one breaks it."""
    asset = _pump()
    alarm = _alarm("AL-1", asset, "Lube Oil Pressure Low", at=NOW - timedelta(hours=1))
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)
    assert all(r["procedure_reference"].strip() for r in result["recommendations"])
    assert result["procedure_references"] == sorted(set(result["procedure_references"]))


# --- precursor detection ----------------------------------------------------------------


def test_precursor_is_detected_when_it_leads_the_alarm() -> None:
    asset = _pump()
    alarms: list[Alarm] = []
    for i in range(4):
        at = NOW - timedelta(days=5 * (i + 1))
        alarms.append(_alarm(f"VIB-{i}", asset, "Bearing Vibration High", at=at))
        # Three of four preceded by lube oil loss, 15 minutes earlier.
        if i < 3:
            alarms.append(
                _alarm(
                    f"LOP-{i}",
                    asset,
                    "Lube Oil Pressure Low",
                    at=at - timedelta(minutes=15),
                    alarm_type=AlarmType.DEVICE,
                )
            )
    target = next(a for a in alarms if a.alarm_id == "VIB-0")
    result = engine.recommend(_store(asset, alarms), NOW, alarm=target)

    precursor = result["context"]["likely_precursor"]
    assert precursor is not None
    assert precursor["alarm_name"] == "Lube Oil Pressure Low"
    assert precursor["preceded_count"] == 3
    assert precursor["of_occurrences"] == 4
    assert precursor["share"] == 0.75
    assert precursor["median_lead_minutes"] == 15.0


def test_precursor_outside_the_window_is_not_counted() -> None:
    """Three hours earlier is not a precursor; treating it as one invents causation."""
    asset = _pump()
    at = NOW - timedelta(days=2)
    alarms = [
        _alarm("VIB-0", asset, "Bearing Vibration High", at=at),
        _alarm(
            "LOP-0",
            asset,
            "Lube Oil Pressure Low",
            at=at - timedelta(hours=3),
            alarm_type=AlarmType.DEVICE,
        ),
    ]
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0])
    assert result["context"]["likely_precursor"] is None


def test_a_following_alarm_is_not_treated_as_a_precursor() -> None:
    """Direction matters: the candidate must start strictly before the alarm."""
    asset = _pump()
    at = NOW - timedelta(days=2)
    alarms = [
        _alarm("VIB-0", asset, "Bearing Vibration High", at=at),
        _alarm(
            "LOP-0",
            asset,
            "Lube Oil Pressure Low",
            at=at + timedelta(minutes=10),
            alarm_type=AlarmType.DEVICE,
        ),
    ]
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0])
    assert result["context"]["likely_precursor"] is None


def test_a_strong_precursor_adds_an_investigate_the_cause_action() -> None:
    asset = _pump()
    alarms: list[Alarm] = []
    for i in range(4):
        at = NOW - timedelta(days=5 * (i + 1))
        alarms.append(_alarm(f"VIB-{i}", asset, "Bearing Vibration High", at=at))
        alarms.append(
            _alarm(
                f"LOP-{i}",
                asset,
                "Lube Oil Pressure Low",
                at=at - timedelta(minutes=12),
                alarm_type=AlarmType.DEVICE,
            )
        )
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0], max_recommendations=10)
    assert any(
        "Lube Oil Pressure Low" in r["action"] and "upstream cause" in r["action"]
        for r in result["recommendations"]
    )


# --- history-driven behaviour -----------------------------------------------------------


@pytest.mark.parametrize(
    ("occurrences", "expect_work_request"), [(9, False), (10, True), (15, True)]
)
def test_work_request_appears_only_past_ten_occurrences(
    occurrences: int, expect_work_request: bool
) -> None:
    asset = _pump()
    alarms = [
        _alarm(f"AL-{i}", asset, "Bearing Vibration High", at=NOW - timedelta(days=i + 1))
        for i in range(occurrences)
    ]
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0], max_recommendations=12)
    found = any("work request" in r["action"] for r in result["recommendations"])
    assert found is expect_work_request


def test_accelerating_recurrence_triggers_engineering_escalation() -> None:
    """Most occurrences bunched into the last fortnight means deteriorating, not stable."""
    asset = _pump()
    # Eight occurrences, six of them inside the last 14 days.
    days = [1, 3, 5, 7, 9, 11, 45, 70]
    alarms = [
        _alarm(f"AL-{i}", asset, "Bearing Vibration High", at=NOW - timedelta(days=d))
        for i, d in enumerate(days)
    ]
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0], max_recommendations=12)
    assert any("accelerating" in r["action"] for r in result["recommendations"])


def test_a_stable_recurrence_does_not_claim_acceleration() -> None:
    asset = _pump()
    days = [5, 20, 35, 50, 65, 80]
    alarms = [
        _alarm(f"AL-{i}", asset, "Bearing Vibration High", at=NOW - timedelta(days=d))
        for i, d in enumerate(days)
    ]
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0], max_recommendations=12)
    assert not any("accelerating" in r["action"] for r in result["recommendations"])


def test_slow_acknowledgement_prompts_a_priority_review() -> None:
    asset = _pump()
    alarms = [
        _alarm(
            f"AL-{i}",
            asset,
            "Bearing Vibration High",
            at=NOW - timedelta(days=i * 4 + 1),
            ack=25.0,
        )
        for i in range(6)
    ]
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0], max_recommendations=12)
    assert any("alarm priority" in r["action"] for r in result["recommendations"])


def test_context_reports_the_occurrence_counts_it_reasoned_from() -> None:
    """The copilot cites these numbers, so they must be in the response, not just implied."""
    asset = _pump()
    days = [1, 2, 3, 40, 50]
    alarms = [
        _alarm(f"AL-{i}", asset, "Bearing Vibration High", at=NOW - timedelta(days=d))
        for i, d in enumerate(days)
    ]
    context = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0])["context"]
    assert context["occurrences_in_window"] == 5
    assert context["occurrences_last_14_days"] == 3
    assert context["lookback_days"] == 90


def test_lookback_window_excludes_older_history() -> None:
    asset = _pump()
    alarms = [
        _alarm("RECENT", asset, "Bearing Vibration High", at=NOW - timedelta(days=5)),
        _alarm("OLD", asset, "Bearing Vibration High", at=NOW - timedelta(days=120)),
    ]
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0], lookback_days=30)
    assert result["context"]["occurrences_in_window"] == 1


# --- escalation and safety --------------------------------------------------------------


def test_critical_alarm_always_escalates() -> None:
    asset = _pump()
    alarm = _alarm(
        "AL-1",
        asset,
        "Motor Overload Trip",
        at=NOW - timedelta(hours=3),
        severity=Severity.CRITICAL,
        alarm_type=AlarmType.SAFETY,
        status=AlarmStatus.ACTIVE,
        ack=None,
    )
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)
    assert result["escalation"]["required"] is True
    assert result["escalation"]["escalate_to"] == "Rotating Equipment Engineer"


def test_low_criticality_asset_escalates_to_the_supervisor_instead() -> None:
    asset = _pump(criticality=2)
    alarm = _alarm(
        "AL-1",
        asset,
        "Motor Overload Trip",
        at=NOW - timedelta(hours=3),
        severity=Severity.CRITICAL,
        alarm_type=AlarmType.SAFETY,
        status=AlarmStatus.ACTIVE,
        ack=None,
    )
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)
    assert result["escalation"]["escalate_to"] == "Unit Supervisor"


def test_a_single_cleared_medium_alarm_does_not_escalate() -> None:
    asset = _pump()
    alarm = _alarm(
        "AL-1",
        asset,
        "Suction Pressure Low",
        at=NOW - timedelta(days=3),
        severity=Severity.MEDIUM,
    )
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)
    assert result["escalation"]["required"] is False


def test_active_high_severity_escalates_once_it_recurs() -> None:
    asset = _pump()
    alarms = [
        _alarm(
            f"AL-{i}",
            asset,
            "Bearing Vibration High",
            at=NOW - timedelta(days=i * 3 + 1),
            status=AlarmStatus.ACTIVE if i == 0 else AlarmStatus.CLEARED,
            ack=None if i == 0 else 5.0,
        )
        for i in range(6)
    ]
    result = engine.recommend(_store(asset, alarms), NOW, alarm=alarms[0])
    assert result["escalation"]["required"] is True
    assert "recurring 6 times" in result["escalation"]["reason"]


def test_safety_alarm_warns_against_bypassing() -> None:
    asset = _pump()
    alarm = _alarm(
        "AL-1",
        asset,
        "Motor Overload Trip",
        at=NOW - timedelta(hours=1),
        severity=Severity.CRITICAL,
        alarm_type=AlarmType.SAFETY,
        status=AlarmStatus.ACTIVE,
        ack=None,
    )
    notes = " ".join(engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)["safety_notes"])
    assert "bypass" in notes
    assert "lock" in notes.lower()


def test_isolation_requiring_actions_are_flagged() -> None:
    asset = _pump()
    alarm = _alarm(
        "AL-1",
        asset,
        "Motor Overload Trip",
        at=NOW - timedelta(hours=1),
        severity=Severity.CRITICAL,
        alarm_type=AlarmType.SAFETY,
        status=AlarmStatus.ACTIVE,
        ack=None,
    )
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)
    assert any(r["requires_isolation"] for r in result["recommendations"])


def test_the_response_carries_a_verification_disclaimer() -> None:
    """Generated guidance must not read as an approved procedure."""
    asset = _pump()
    alarm = _alarm("AL-1", asset, "Suction Pressure Low", at=NOW - timedelta(days=1))
    result = engine.recommend(_store(asset, [alarm]), NOW, alarm=alarm)
    assert "Verify against the current" in result["disclaimer"]


# --- subject selection ------------------------------------------------------------------


def test_asset_only_request_picks_the_most_severe_open_alarm() -> None:
    asset = _pump()
    alarms = [
        _alarm(
            "OPEN-CRIT",
            asset,
            "Motor Overload Trip",
            at=NOW - timedelta(days=4),
            severity=Severity.CRITICAL,
            status=AlarmStatus.ACTIVE,
            ack=None,
        ),
        _alarm(
            "OPEN-HIGH",
            asset,
            "Bearing Vibration High",
            at=NOW - timedelta(hours=1),
            status=AlarmStatus.ACTIVE,
            ack=None,
        ),
        _alarm("CLEARED", asset, "Suction Pressure Low", at=NOW - timedelta(minutes=10)),
    ]
    result = engine.recommend(_store(asset, alarms), NOW, asset_id=asset.asset_id)
    # Critical beats both the more recent open alarm and the most recent cleared one.
    assert result["alarm_id"] == "OPEN-CRIT"


def test_asset_with_only_cleared_alarms_falls_back_to_the_latest() -> None:
    asset = _pump()
    alarms = [
        _alarm("OLD", asset, "Suction Pressure Low", at=NOW - timedelta(days=40)),
        _alarm("NEW", asset, "Suction Pressure Low", at=NOW - timedelta(days=2)),
    ]
    result = engine.recommend(_store(asset, alarms), NOW, asset_id=asset.asset_id)
    assert result["alarm_id"] == "NEW"


def test_asset_with_no_alarms_in_the_window_raises() -> None:
    asset = _pump()
    alarms = [_alarm("OLD", asset, "Suction Pressure Low", at=NOW - timedelta(days=150))]
    with pytest.raises(LookupError, match="no alarms for asset"):
        engine.recommend(_store(asset, alarms), NOW, asset_id=asset.asset_id, lookback_days=90)


def test_neither_alarm_nor_asset_raises() -> None:
    asset = _pump()
    with pytest.raises(ValueError, match="either alarm or asset_id"):
        engine.recommend(_store(asset, []), NOW)


# --- against the real generated world ---------------------------------------------------


def test_the_acceptance_scenario_asset_yields_the_expected_answer() -> None:
    """End to end on real data: the answer the mandatory scenario is supposed to reach.

    Asserted here as well as in the e2e test because this is the layer where the finding is
    actually derived. If the engine stops spotting the lube-oil precursor, this fails with a
    clear cause rather than as a vague "the answer lacked a contributing factor".
    """
    store = AlarmStore(build_world(NOW, days_of_history=400))
    pump = next(a for a in store.world.assets if a.name == BFP101_NAME)
    vibration = store.query(
        AlarmFilter(asset_ids=[pump.asset_id], alarm_names=["Bearing Vibration High"])
    )
    latest = max(vibration, key=lambda a: a.start_time)
    result = engine.recommend(store, NOW, alarm=latest, lookback_days=90)

    assert result["asset_name"] == BFP101_NAME
    assert result["context"]["occurrences_in_window"] >= 20
    precursor = result["context"]["likely_precursor"]
    assert precursor is not None and precursor["alarm_name"] == "Lube Oil Pressure Low"
    assert result["escalation"]["required"] is True
    # The procedure the RAG corpus must contain, named by the API.
    assert any("OP-BFP-101" in ref for ref in result["procedure_references"])
