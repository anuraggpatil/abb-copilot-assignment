"""Tests for the dataset generator.

Two groups, and the distinction matters:

* **Generator properties** — determinism, relativity to `now`. If these break, every other
  test in the suite becomes unreliable rather than merely failing.
* **Dataset guarantees** — the specific assets and patterns the assignment's Postman
  collections assert on, and the causal story the acceptance scenario investigates. These
  are a contract between the seeder and the rest of the project: they are easy to break by
  innocuously "tidying" the seeder, and the failure would otherwise show up far away, as
  an assertion about an empty API response.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.alarm_api.domain import AlarmStatus, AlarmType, Severity, Site
from apps.alarm_api.seed import BFP101_NAME, MIN_DAYS_OF_HISTORY, build_world
from apps.alarm_api.store import AlarmFilter, AlarmStore

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


# --- generator properties ---------------------------------------------------------------


def test_same_seed_and_now_produce_identical_worlds() -> None:
    a = build_world(NOW, seed=99, days_of_history=200)
    b = build_world(NOW, seed=99, days_of_history=200)

    assert [x.asset_id for x in a.assets] == [x.asset_id for x in b.assets]
    assert [x.alarm_id for x in a.alarms] == [x.alarm_id for x in b.alarms]
    # Compare full payloads, not just ids: identical ids with drifting timestamps would
    # still make occurrence-count assertions flaky.
    assert [x.model_dump() for x in a.alarms] == [x.model_dump() for x in b.alarms]


def test_different_seeds_produce_different_worlds() -> None:
    a = build_world(NOW, seed=1, days_of_history=200)
    b = build_world(NOW, seed=2, days_of_history=200)
    assert [x.alarm_id for x in a.alarms] != [x.alarm_id for x in b.alarms]


def test_alarm_ids_are_unique() -> None:
    world = build_world(NOW, days_of_history=400)
    ids = [a.alarm_id for a in world.alarms]
    assert len(ids) == len(set(ids))


def test_asset_ids_are_unique() -> None:
    world = build_world(NOW, days_of_history=200)
    ids = [a.asset_id for a in world.assets]
    assert len(ids) == len(set(ids))


def test_all_alarms_fall_inside_the_requested_horizon() -> None:
    """No alarm may predate the window or sit in the future.

    A future-dated alarm would make "active alarms right now" include events that have not
    happened, which quietly corrupts every KPI.
    """
    world = build_world(NOW, days_of_history=200)
    for alarm in world.alarms:
        assert world.window_start <= alarm.start_time <= NOW, alarm.alarm_id


def test_alarms_are_sorted_by_start_time() -> None:
    """The store's bisect-based time filter depends on this ordering."""
    world = build_world(NOW, days_of_history=200)
    times = [a.start_time for a in world.alarms]
    assert times == sorted(times)


def test_data_is_relative_to_now_not_absolute() -> None:
    """Shifting `now` must shift the data with it, or "last 90 days" breaks over time."""
    later = NOW + timedelta(days=365)
    shifted = build_world(later, days_of_history=200)
    assert max(a.start_time for a in shifted.alarms) > NOW
    assert shifted.window_start > NOW - timedelta(days=200)


@pytest.mark.parametrize("days", [MIN_DAYS_OF_HISTORY, 200, 400])
def test_horizon_is_respected(days: int) -> None:
    world = build_world(NOW, days_of_history=days)
    assert world.window_start == NOW - timedelta(days=days)
    assert min(a.start_time for a in world.alarms) >= world.window_start


def test_too_short_a_horizon_is_rejected_loudly() -> None:
    """Refusing beats silently generating a world missing the planted patterns.

    The patterns are a contract with the Postman collections and the acceptance scenario.
    Quietly dropping the ones that fall outside a short window would surface much later as
    an unexplained empty API response.
    """
    with pytest.raises(ValueError, match="days_of_history must be at least"):
        build_world(NOW, days_of_history=MIN_DAYS_OF_HISTORY - 1)


def test_timestamps_are_internally_consistent() -> None:
    """ack/clear times and the derived minute fields must agree.

    These are two representations of the same fact, and the API exposes both — the
    aggregations read `ack_delay_minutes` while the GUI renders `ack_time`. If they
    disagree, the KPIs and the timeline tell the user different stories.
    """
    world = build_world(NOW, days_of_history=200)
    for alarm in world.alarms:
        if alarm.ack_time is not None:
            assert alarm.ack_time >= alarm.start_time, alarm.alarm_id
            assert alarm.ack_delay_minutes is not None
            expected = (alarm.ack_time - alarm.start_time).total_seconds() / 60
            assert alarm.ack_delay_minutes == pytest.approx(expected, abs=1e-6)
        else:
            assert alarm.ack_delay_minutes is None, alarm.alarm_id

        if alarm.clear_time is not None:
            assert alarm.clear_time >= alarm.start_time, alarm.alarm_id
            assert alarm.duration_minutes is not None
            expected = (alarm.clear_time - alarm.start_time).total_seconds() / 60
            assert alarm.duration_minutes == pytest.approx(expected, abs=1e-6)
        else:
            assert alarm.duration_minutes is None, alarm.alarm_id


def test_status_agrees_with_timestamps() -> None:
    world = build_world(NOW, days_of_history=200)
    for alarm in world.alarms:
        if alarm.status is AlarmStatus.ACTIVE:
            assert alarm.clear_time is None, alarm.alarm_id
        if alarm.status is AlarmStatus.CLEARED:
            assert alarm.clear_time is not None, alarm.alarm_id


def test_related_asset_ids_resolve() -> None:
    """A dangling relationship would make the copilot's related-asset hop a dead end."""
    world = build_world(NOW, days_of_history=180)
    known = {a.asset_id for a in world.assets}
    for asset in world.assets:
        for related in asset.related_asset_ids:
            assert related in known, f"{asset.name} points at unknown asset {related}"


def test_every_alarm_references_a_real_asset() -> None:
    world = build_world(NOW, days_of_history=200)
    by_id = {a.asset_id: a for a in world.assets}
    for alarm in world.alarms:
        asset = by_id.get(alarm.asset_id)
        assert asset is not None, alarm.alarm_id
        # Denormalised fields must match their source, or filtering by site returns rows
        # whose asset says otherwise.
        assert alarm.asset_name == asset.name
        assert alarm.site == asset.site
        assert alarm.unit == asset.unit


def test_severity_mix_is_plausible() -> None:
    """Critical alarms must be rare.

    Not cosmetic: `critical_count` and the priority ranking are meaningless if a third of
    the dataset is critical, and an earlier version of the seeder drew uniformly from the
    catalogue and produced exactly that.
    """
    world = build_world(NOW, days_of_history=400)
    critical = sum(1 for a in world.alarms if a.severity is Severity.CRITICAL)
    share = critical / len(world.alarms)
    assert 0.005 < share < 0.10, f"critical share {share:.3f} is not plausible"


# --- dataset guarantees the Postman collections rely on ---------------------------------


@pytest.fixture(scope="module")
def store() -> AlarmStore:
    return AlarmStore(build_world(NOW, days_of_history=400))


@pytest.mark.parametrize(
    ("query", "unit", "minimum"),
    [
        (BFP101_NAME, None, 1),
        ("Boiler Feed Pump 102", None, 1),
        # CHAIN-03 reads results[0..2], so three compressors is the real requirement.
        ("compressor", None, 3),
        # CHAIN-08 pairs the query with a unit filter and also reads three results.
        ("motor", "Unit 5", 3),
    ],
)
def test_searches_used_by_the_collections_return_enough_results(
    store: AlarmStore, query: str, unit: str | None, minimum: int
) -> None:
    results = store.search_assets(query, limit=10, unit=unit)
    assert len(results) >= minimum, f"{query!r} returned {len(results)}"


def test_exact_asset_name_search_ranks_that_asset_first(store: AlarmStore) -> None:
    """ "Boiler Feed Pump 101" must not be outranked by 102 or by its own sub-components."""
    results = store.search_assets(BFP101_NAME, limit=5)
    assert results[0].name == BFP101_NAME


def test_east_refinery_has_active_alarms(store: AlarmStore) -> None:
    """CHAIN-09 filters site=EastRefinery&status=active and asserts rows exist."""
    rows = store.query(AlarmFilter(site=Site.EAST_REFINERY, statuses=[AlarmStatus.ACTIVE]))
    assert rows


def test_boiler_feed_pump_102_has_an_active_alarm(store: AlarmStore) -> None:
    """CHAIN-05 resolves BFP-102 then expects an active alarm to act on."""
    asset = next(a for a in store.world.assets if a.name == "Boiler Feed Pump 102")
    rows = store.query(AlarmFilter(asset_ids=[asset.asset_id], statuses=[AlarmStatus.ACTIVE]))
    assert rows


def test_unit_5_motors_have_both_safety_and_device_alarms(store: AlarmStore) -> None:
    """CHAIN-08 filters alarm_types ["safety", "device"] after resolving Unit 5 motors."""
    rows = store.query(AlarmFilter(unit="Unit 5", alarm_types=[AlarmType.SAFETY, AlarmType.DEVICE]))
    types = {a.alarm_type for a in rows}
    assert AlarmType.SAFETY in types
    assert AlarmType.DEVICE in types


def test_north_plant_unit_1_has_stale_and_recurring_alarms(store: AlarmStore) -> None:
    """CHAIN-06 looks for both on this exact site/unit pair."""
    flt = AlarmFilter(site=Site.NORTH_PLANT, unit="Unit 1")
    assert store.stale_alarms(flt, NOW), "expected unacknowledged alarms older than 180 min"
    assert store.recurring_alarms(flt), "expected recurring alarm groups"


def test_unit_3_has_critical_alarms(store: AlarmStore) -> None:
    """CHAIN-04 computes critical alarm density on Unit 3; a zero numerator is no test."""
    assert store.query(AlarmFilter(unit="Unit 3", severities=[Severity.CRITICAL]))


def test_unit_4_has_nuisance_alarms_recurring_at_least_eight_times(store: AlarmStore) -> None:
    """CHAIN-10 uses a recurrence threshold of 8 when hunting nuisance alarms."""
    groups = store.recurring_alarms(AlarmFilter(unit="Unit 4"), threshold=8)
    assert groups
    assert any(g["chattering_share"] > 0.5 for g in groups)


def test_south_plant_has_a_spread_of_acknowledgement_delays(store: AlarmStore) -> None:
    """CHAIN-07 reports on operator response at SouthPlant."""
    rows = store.query(AlarmFilter(site=Site.SOUTH_PLANT))
    delays = [a.ack_delay_minutes for a in rows if a.ack_delay_minutes is not None]
    assert len(delays) > 50
    assert max(delays) - min(delays) > 30, "delays must vary or the KPI is trivial"


def test_unit_2_contains_an_alarm_flood(store: AlarmStore) -> None:
    """At least ten alarms inside one rolling ten-minute window in Unit 2.

    This is the precondition for `POST /alarms/flood-analysis` returning a non-empty
    `flood_windows`, so it is asserted on the data rather than only through the endpoint.
    """
    rows = store.query(AlarmFilter(unit="Unit 2", site=Site.EAST_REFINERY))
    times = sorted(a.start_time for a in rows)
    window = timedelta(minutes=10)
    busiest = 0
    left = 0
    for right, t in enumerate(times):
        while t - times[left] > window:
            left += 1
        busiest = max(busiest, right - left + 1)
    assert busiest >= 10, f"busiest 10-minute window held only {busiest} alarms"


def test_a_flood_falls_inside_the_collections_hard_coded_window(store: AlarmStore) -> None:
    """The collections query 2026-05-01..2026-07-01 — 90 to 151 days before the frozen now.

    The planted floods must straddle that range as well as recent time, or those requests
    come back empty.
    """
    start = NOW - timedelta(days=151)
    end = NOW - timedelta(days=90)
    rows = store.query(AlarmFilter(unit="Unit 2", start_time=start, end_time=end))
    times = sorted(a.start_time for a in rows)
    window = timedelta(minutes=10)
    busiest, left = 0, 0
    for right, t in enumerate(times):
        while t - times[left] > window:
            left += 1
        busiest = max(busiest, right - left + 1)
    assert busiest >= 10, f"no flood in the collections' window (busiest={busiest})"


# --- the planted causal story ------------------------------------------------------------


@pytest.fixture(scope="module")
def bfp101_last_90(store: AlarmStore) -> AlarmFilter:
    asset = next(a for a in store.world.assets if a.name == BFP101_NAME)
    return AlarmFilter(
        asset_ids=[asset.asset_id],
        start_time=NOW - timedelta(days=90),
        end_time=NOW,
    )


def test_bfp101_has_recurring_high_severity_vibration(
    store: AlarmStore, bfp101_last_90: AlarmFilter
) -> None:
    """The acceptance scenario's headline finding must actually be in the data."""
    groups = store.recurring_alarms(bfp101_last_90)
    vibration = next((g for g in groups if g["alarm_name"] == "Bearing Vibration High"), None)
    assert vibration is not None, "no recurring Bearing Vibration High group"
    assert vibration["occurrences"] >= 20
    assert vibration["max_severity"] in ("high", "critical")


def test_bfp101_vibration_is_escalating(store: AlarmStore, bfp101_last_90: AlarmFilter) -> None:
    """ "Getting worse" is the part of the answer that makes it worth escalating."""
    groups = store.recurring_alarms(bfp101_last_90)
    vibration = next(g for g in groups if g["alarm_name"] == "Bearing Vibration High")
    assert vibration["trend"] == "increasing"
    assert vibration["occurrences_second_half"] > vibration["occurrences_first_half"]


def test_bfp101_lube_oil_precedes_vibration(store: AlarmStore) -> None:
    """The planted contributing factor: lube-oil loss shortly before the vibration alarm.

    Asserted directly on timestamps rather than through the recommendations engine, so a
    change in the engine's detection window cannot mask the data going missing.
    """
    asset = next(a for a in store.world.assets if a.name == BFP101_NAME)
    rows = store.query(
        AlarmFilter(asset_ids=[asset.asset_id], start_time=NOW - timedelta(days=90), end_time=NOW)
    )
    vibration = [a for a in rows if a.alarm_name == "Bearing Vibration High"]
    lube = [a for a in rows if a.alarm_name == "Lube Oil Pressure Low"]
    assert vibration and lube

    window = timedelta(minutes=60)
    preceded = sum(
        1
        for v in vibration
        if any(v.start_time - window <= lo.start_time < v.start_time for lo in lube)
    )
    assert preceded / len(vibration) >= 0.4, (
        f"lube oil preceded only {preceded}/{len(vibration)} vibration alarms"
    )


def test_bfp101_has_a_nuisance_alarm_to_discount(
    store: AlarmStore, bfp101_last_90: AlarmFilter
) -> None:
    """A good answer separates signal from noise, so the noise has to be present."""
    groups = store.recurring_alarms(bfp101_last_90)
    noisy = [g for g in groups if g["chattering_share"] > 0.5]
    assert noisy, "expected at least one chattering alarm group on BFP-101"


def test_bfp101_has_an_open_alarm_to_act_on(store: AlarmStore) -> None:
    """Recommendations are for a live situation; the GUI needs something unresolved."""
    asset = next(a for a in store.world.assets if a.name == BFP101_NAME)
    rows = store.query(AlarmFilter(asset_ids=[asset.asset_id], statuses=[AlarmStatus.ACTIVE]))
    assert rows


def test_bfp101_suction_pressure_correlates_with_the_deaerator(store: AlarmStore) -> None:
    """The second contributing factor is only reachable via the related asset.

    This is what makes multi-step chaining necessary rather than decorative: the cause of
    the pump's low suction pressure is recorded against the deaerator that feeds it.
    """
    pump = next(a for a in store.world.assets if a.name == BFP101_NAME)
    deaerator = next(a for a in store.world.assets if a.name == "Deaerator DA-201")
    assert deaerator.asset_id in pump.related_asset_ids

    suction = store.query(
        AlarmFilter(asset_ids=[pump.asset_id], alarm_names=["Suction Pressure Low"])
    )
    levels = store.query(AlarmFilter(asset_ids=[deaerator.asset_id], alarm_names=["Level Low"]))
    assert suction and levels

    window = timedelta(hours=2)
    overlapping = sum(
        1
        for s in suction
        if any(
            abs((s.start_time - lo.start_time).total_seconds()) <= window.total_seconds()
            for lo in levels
        )
    )
    assert overlapping >= 3, f"only {overlapping} suction alarms near a deaerator level alarm"
