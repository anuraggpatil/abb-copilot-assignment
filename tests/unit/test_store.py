"""Tests for the query and analytics layer.

The KPI and grouping tests build a **handcrafted** eight-alarm world rather than using the
generated one. Asserting `avg_ack_delay == 7.5` against generated data would mean either
recomputing the expected value with the same code under test, or hard-coding a magic number
that changes whenever the seeder is touched. A tiny hand-built fixture makes every expected
value obvious by inspection.

The filtering and pagination tests use the real generated world, because there the property
under test — "the filter excludes nothing it should keep" — needs volume and variety.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.alarm_api.domain import (
    Alarm,
    AlarmStatus,
    AlarmType,
    Asset,
    AssetType,
    Severity,
    Site,
)
from apps.alarm_api.seed import World, build_world
from apps.alarm_api.store import (
    RECURRENCE_THRESHOLD,
    AlarmFilter,
    AlarmNotFoundError,
    AlarmStore,
    AssetNotFoundError,
)

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


# --- handcrafted fixture ----------------------------------------------------------------


def _asset(
    asset_id: str, name: str, unit: str = "Unit 1", site: Site = Site.EAST_REFINERY
) -> Asset:
    return Asset(
        asset_id=asset_id,
        name=name,
        asset_type=AssetType.CENTRIFUGAL_PUMP,
        site=site,
        unit=unit,
        tag=f"1-{asset_id}",
        criticality=3,
        manufacturer="TestCo",
        model="T-1",
        commissioned_on=NOW - timedelta(days=2000),
        maintenance_strategy="preventive",
    )


def _alarm(
    alarm_id: str,
    asset: Asset,
    name: str,
    *,
    days_ago: float,
    severity: Severity = Severity.MEDIUM,
    status: AlarmStatus = AlarmStatus.CLEARED,
    ack: float | None = None,
    duration: float | None = None,
    chattering: bool = False,
) -> Alarm:
    start = NOW - timedelta(days=days_ago)
    return Alarm(
        alarm_id=alarm_id,
        alarm_name=name,
        alarm_type=AlarmType.PROCESS,
        asset_id=asset.asset_id,
        asset_name=asset.name,
        site=asset.site,
        unit=asset.unit,
        severity=severity,
        status=status,
        start_time=start,
        ack_time=start + timedelta(minutes=ack) if ack is not None else None,
        clear_time=start + timedelta(minutes=duration) if duration is not None else None,
        ack_delay_minutes=ack,
        duration_minutes=duration,
        priority_hint=3,
        chattering=chattering,
    )


@pytest.fixture
def tiny_store() -> AlarmStore:
    """Eight alarms with values chosen so every KPI is checkable by hand.

    Pump A: 'Vib High' x 6 (meets the recurrence threshold exactly), acks 5,10,5,10,5,10
            -> mean 7.5. Plus one critical, unacknowledged, active.
    Pump B: 'Flow Dev' x 1, chattering, cleared in 0.5 min, never acknowledged.
    """
    a = _asset("A", "Pump A")
    b = _asset("B", "Pump B", unit="Unit 2", site=Site.NORTH_PLANT)
    alarms = [
        _alarm("AL-1", a, "Vib High", days_ago=50, ack=5.0, duration=30.0),
        _alarm("AL-2", a, "Vib High", days_ago=40, ack=10.0, duration=30.0),
        _alarm("AL-3", a, "Vib High", days_ago=30, ack=5.0, duration=30.0),
        _alarm("AL-4", a, "Vib High", days_ago=20, ack=10.0, duration=30.0),
        _alarm("AL-5", a, "Vib High", days_ago=10, ack=5.0, duration=30.0),
        _alarm("AL-6", a, "Vib High", days_ago=5, ack=10.0, duration=30.0),
        _alarm(
            "AL-7",
            a,
            "Overload Trip",
            days_ago=1,
            severity=Severity.CRITICAL,
            status=AlarmStatus.ACTIVE,
        ),
        _alarm(
            "AL-8",
            b,
            "Flow Dev",
            days_ago=3,
            duration=0.5,
            chattering=True,
        ),
    ]
    alarms.sort(key=lambda x: x.start_time)
    return AlarmStore(
        World(generated_at=NOW, seed=0, days_of_history=200, assets=[a, b], alarms=alarms)
    )


# --- KPIs -------------------------------------------------------------------------------


def test_kpi_values_are_exact(tiny_store: AlarmStore) -> None:
    rows = tiny_store.query(AlarmFilter())
    kpis = tiny_store.compute_kpis(
        rows,
        [
            "alarm_count",
            "critical_count",
            "avg_ack_delay",
            "max_ack_delay",
            "avg_duration",
            "unacknowledged_rate",
        ],
    )
    assert kpis["alarm_count"] == 8
    assert kpis["critical_count"] == 1
    # Six acknowledged alarms: (5+10+5+10+5+10)/6 = 7.5. The two unacknowledged ones are
    # excluded rather than counted as zero — averaging in a zero would make slow response
    # look fast.
    assert kpis["avg_ack_delay"] == 7.5
    assert kpis["max_ack_delay"] == 10.0
    # Seven cleared alarms: six at 30 min plus one at 0.5 -> 180.5 / 7.
    assert kpis["avg_duration"] == pytest.approx(25.79, abs=0.01)
    assert kpis["unacknowledged_rate"] == 0.25  # AL-7 and AL-8 of 8


def test_avg_ack_delay_is_none_when_nothing_was_acknowledged(tiny_store: AlarmStore) -> None:
    """None, not 0.0. Zero would read as instant acknowledgement."""
    rows = tiny_store.query(AlarmFilter(asset_ids=["B"]))
    assert tiny_store.compute_kpis(rows, ["avg_ack_delay"])["avg_ack_delay"] is None


def test_recurring_rate_counts_only_groups_over_the_threshold(tiny_store: AlarmStore) -> None:
    rows = tiny_store.query(AlarmFilter(asset_ids=["A"]))
    # Six 'Vib High' meets the threshold; the single 'Overload Trip' does not.
    assert RECURRENCE_THRESHOLD == 6
    assert tiny_store.compute_kpis(rows, ["recurring_rate"])["recurring_rate"] == pytest.approx(
        6 / 7, abs=0.001
    )


def test_suppression_candidate_rate_flags_chattering_and_self_clearing(
    tiny_store: AlarmStore,
) -> None:
    rows = tiny_store.query(AlarmFilter())
    # Only AL-8: chattering, cleared in 0.5 min, never acknowledged.
    assert (
        tiny_store.compute_kpis(rows, ["suppression_candidate_rate"])["suppression_candidate_rate"]
        == 0.125
    )


def test_operator_response_efficiency(tiny_store: AlarmStore) -> None:
    rows = tiny_store.query(AlarmFilter(asset_ids=["A"]))
    # Of seven alarms on Pump A, six were acknowledged within 10 minutes.
    assert tiny_store.compute_kpis(rows, ["operator_response_efficiency"])[
        "operator_response_efficiency"
    ] == pytest.approx(6 / 7, abs=0.001)


def test_kpis_on_an_empty_set_do_not_divide_by_zero(tiny_store: AlarmStore) -> None:
    kpis = tiny_store.compute_kpis([], list(("alarm_count", "recurring_rate", "avg_ack_delay")))
    assert kpis == {"alarm_count": 0, "recurring_rate": 0.0, "avg_ack_delay": None}


def test_unknown_kpi_raises_with_the_valid_options(tiny_store: AlarmStore) -> None:
    """The message must list what IS valid — a tool-calling model retries from it."""
    with pytest.raises(ValueError, match="unknown kpis") as exc:
        tiny_store.compute_kpis([], ["made_up_kpi"])
    assert "alarm_count" in str(exc.value)


# --- grouping ---------------------------------------------------------------------------


def test_summarize_groups_and_preserves_key_order(tiny_store: AlarmStore) -> None:
    result = tiny_store.summarize(
        AlarmFilter(), group_by=["asset_id", "alarm_name"], kpis=["alarm_count"]
    )
    assert result["total_alarms"] == 8
    assert [list(g["key"]) for g in result["groups"]] == [["asset_id", "alarm_name"]] * len(
        result["groups"]
    )
    biggest = result["groups"][0]
    assert biggest["key"] == {"asset_id": "A", "alarm_name": "Vib High"}
    assert biggest["alarm_count"] == 6


def test_group_counts_sum_to_the_total(tiny_store: AlarmStore) -> None:
    """Grouping must partition the set — no alarm counted twice or dropped."""
    result = tiny_store.summarize(AlarmFilter(), group_by=["severity"], kpis=["alarm_count"])
    assert sum(g["alarm_count"] for g in result["groups"]) == result["total_alarms"]


def test_summarize_without_group_by_returns_only_overall(tiny_store: AlarmStore) -> None:
    result = tiny_store.summarize(AlarmFilter(), kpis=["alarm_count"])
    assert result["groups"] == []
    assert result["overall"]["alarm_count"] == 8


def test_unknown_group_by_raises(tiny_store: AlarmStore) -> None:
    with pytest.raises(ValueError, match="unknown group_by"):
        tiny_store.summarize(AlarmFilter(), group_by=["colour"])


# --- recurrence -------------------------------------------------------------------------


def test_recurring_alarms_reports_counts_and_trend(tiny_store: AlarmStore) -> None:
    groups = tiny_store.recurring_alarms(
        AlarmFilter(start_time=NOW - timedelta(days=60), end_time=NOW)
    )
    assert len(groups) == 1
    group = groups[0]
    assert group["alarm_name"] == "Vib High"
    assert group["occurrences"] == 6
    assert group["avg_ack_delay_minutes"] == 7.5
    # Halves of the 60-day window split at 30 days ago: 50/40 days fall in the first half,
    # 30/20/10/5 in the second.
    assert group["occurrences_first_half"] == 2
    assert group["occurrences_second_half"] == 4
    assert group["trend"] == "increasing"


def test_recurrence_threshold_is_inclusive(tiny_store: AlarmStore) -> None:
    assert tiny_store.recurring_alarms(AlarmFilter(), threshold=6)
    assert not tiny_store.recurring_alarms(AlarmFilter(), threshold=7)


def test_stale_alarms_needs_active_and_unacknowledged(tiny_store: AlarmStore) -> None:
    stale = tiny_store.stale_alarms(AlarmFilter(), NOW, older_than_minutes=180)
    # AL-7 alone: active, never acknowledged, started a day ago. The cleared alarms are
    # older still but were acted on, so they are not stale.
    assert [a.alarm_id for a in stale] == ["AL-7"]


# --- filtering against the real world ---------------------------------------------------


@pytest.fixture(scope="module")
def store() -> AlarmStore:
    return AlarmStore(build_world(NOW, days_of_history=400))


def test_site_filter_returns_only_that_site(store: AlarmStore) -> None:
    rows = store.query(AlarmFilter(site=Site.NORTH_PLANT))
    assert rows
    assert {a.site for a in rows} == {Site.NORTH_PLANT}


def test_unit_and_site_filters_combine(store: AlarmStore) -> None:
    rows = store.query(AlarmFilter(site=Site.SOUTH_PLANT, unit="Unit 3"))
    assert rows
    assert all(a.site is Site.SOUTH_PLANT and a.unit == "Unit 3" for a in rows)


def test_severity_threshold_is_inclusive_of_the_named_level(store: AlarmStore) -> None:
    rows = store.query(AlarmFilter(min_severity=Severity.HIGH))
    assert rows
    assert {a.severity for a in rows} <= {Severity.HIGH, Severity.CRITICAL}
    assert any(a.severity is Severity.HIGH for a in rows)


def test_alarm_type_filter_accepts_several_types(store: AlarmStore) -> None:
    rows = store.query(AlarmFilter(alarm_types=[AlarmType.SAFETY, AlarmType.DEVICE]))
    assert rows
    assert {a.alarm_type for a in rows} <= {AlarmType.SAFETY, AlarmType.DEVICE}


def test_time_range_filter_is_inclusive_and_bounded(store: AlarmStore) -> None:
    start, end = NOW - timedelta(days=30), NOW - timedelta(days=10)
    rows = store.query(AlarmFilter(start_time=start, end_time=end))
    assert rows
    assert all(start <= a.start_time <= end for a in rows)


def test_time_range_gives_the_same_answer_with_and_without_the_index(
    store: AlarmStore,
) -> None:
    """The bisect fast path and the linear path must agree.

    `query` takes a bisect over the global time index when no asset/site/unit filter is
    given, and a linear scan otherwise. Two code paths for one semantic is exactly where a
    subtle off-by-one hides.
    """
    start, end = NOW - timedelta(days=45), NOW - timedelta(days=15)
    windowed = store.query(AlarmFilter(start_time=start, end_time=end))
    via_site = {
        a.alarm_id
        for site in Site
        for a in store.query(AlarmFilter(site=site, start_time=start, end_time=end))
    }
    assert {a.alarm_id for a in windowed} == via_site


def test_duplicate_asset_ids_do_not_double_count(store: AlarmStore) -> None:
    """The collections' correlation request sends the same asset id twice."""
    asset_id = store.world.alarms[0].asset_id
    once = store.query(AlarmFilter(asset_ids=[asset_id]))
    twice = store.query(AlarmFilter(asset_ids=[asset_id, asset_id]))
    assert len(once) == len(twice)


def test_query_results_are_in_start_time_order(store: AlarmStore) -> None:
    rows = store.query(AlarmFilter(site=Site.EAST_REFINERY))
    assert [a.start_time for a in rows] == sorted(a.start_time for a in rows)


def test_unmatched_filter_returns_empty_not_an_error(store: AlarmStore) -> None:
    assert store.query(AlarmFilter(asset_ids=["does-not-exist"])) == []


def test_describe_reports_what_was_applied(store: AlarmStore) -> None:
    described = AlarmFilter(
        site=Site.EAST_REFINERY, unit="Unit 2", severities=[Severity.HIGH]
    ).describe()
    assert described == {"unit": "Unit 2", "site": "EastRefinery", "severity": ["high"]}


# --- pagination and sorting -------------------------------------------------------------


def test_pagination_partitions_the_rows_exactly(store: AlarmStore) -> None:
    """Walking every page must yield each alarm exactly once.

    The store sorts with a secondary key on alarm_id for this reason: with ties on
    start_time alone, a row can appear on two pages or on none.
    """
    rows = store.query(AlarmFilter(site=Site.EAST_REFINERY, unit="Unit 2"))
    seen: list[str] = []
    page = 1
    while True:
        window, meta = store.paginate(rows, page=page, page_size=25)
        seen.extend(a.alarm_id for a in window)
        if not meta["has_next"]:
            break
        page += 1
    assert len(seen) == len(rows)
    assert len(set(seen)) == len(rows)


def test_pagination_metadata_is_consistent(store: AlarmStore) -> None:
    rows = store.query(AlarmFilter(site=Site.EAST_REFINERY))
    window, meta = store.paginate(rows, page=1, page_size=10)
    assert len(window) == 10
    assert meta["total"] == len(rows)
    assert meta["total_pages"] == (len(rows) + 9) // 10
    assert meta["has_next"] is True


def test_page_past_the_end_is_empty_not_an_error(store: AlarmStore) -> None:
    rows = store.query(AlarmFilter(asset_ids=[store.world.alarms[0].asset_id]))
    window, meta = store.paginate(rows, page=9999, page_size=50)
    assert window == []
    assert meta["has_next"] is False


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_sorting_by_severity_respects_the_severity_ladder(store: AlarmStore, order: str) -> None:
    """Alphabetical order would put 'critical' below 'high' and 'low' above 'medium'."""
    from apps.alarm_api.domain import SEVERITY_ORDER

    rows = store.query(AlarmFilter(unit="Unit 3"))
    window, _ = store.paginate(rows, page_size=40, sort_by="severity", sort_order=order)
    ranks = [SEVERITY_ORDER[a.severity] for a in window]
    assert ranks == sorted(ranks, reverse=order == "desc")


def test_sorting_puts_nulls_last_for_ack_delay(store: AlarmStore) -> None:
    """Unacknowledged alarms have no delay; they must not sort as if it were zero."""
    rows = store.query(AlarmFilter(unit="Unit 1"))
    window, _ = store.paginate(rows, page_size=500, sort_by="ack_delay_minutes", sort_order="asc")
    delays = [a.ack_delay_minutes for a in window]
    first_none = next((i for i, d in enumerate(delays) if d is None), len(delays))
    assert all(d is None for d in delays[first_none:])


@pytest.mark.parametrize(
    ("sort_by", "sort_order"), [("nonexistent", "asc"), ("start_time", "sideways")]
)
def test_invalid_sort_arguments_raise(store: AlarmStore, sort_by: str, sort_order: str) -> None:
    with pytest.raises(ValueError):
        store.paginate([], sort_by=sort_by, sort_order=sort_order)


# --- lookups ----------------------------------------------------------------------------


def test_unknown_asset_raises_asset_not_found(store: AlarmStore) -> None:
    with pytest.raises(AssetNotFoundError):
        store.get_asset("AST-NOPE-9999")


def test_unknown_alarm_raises_alarm_not_found(store: AlarmStore) -> None:
    with pytest.raises(AlarmNotFoundError):
        store.get_alarm("ALM-19700101-000000")


def test_search_with_an_empty_query_returns_the_most_critical_assets(
    store: AlarmStore,
) -> None:
    results = store.search_assets("", limit=5)
    assert len(results) == 5
    criticalities = [a.criticality for a in results]
    assert criticalities == sorted(criticalities, reverse=True)


def test_search_matches_on_tag_as_well_as_name(store: AlarmStore) -> None:
    results = store.search_assets("2-BFP-101", limit=3)
    assert results
    assert results[0].tag == "2-BFP-101"


def test_search_respects_the_limit(store: AlarmStore) -> None:
    assert len(store.search_assets("pump", limit=2)) == 2


def test_search_for_nonsense_returns_nothing(store: AlarmStore) -> None:
    assert store.search_assets("zzzz-not-an-asset", limit=10) == []


def test_related_assets_are_resolvable_objects(store: AlarmStore) -> None:
    pump = next(a for a in store.world.assets if a.name == "Boiler Feed Pump 101")
    related = store.related_assets(pump.asset_id)
    assert {r.name for r in related} >= {"Deaerator DA-201", "Boiler Feed Pump 102"}
