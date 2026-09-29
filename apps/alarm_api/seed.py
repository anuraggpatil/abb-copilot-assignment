"""Deterministic generation of the simulated alarm/asset world.

Two properties matter and are easy to lose:

1. **Deterministic.** Everything derives from `random.Random(seed)`, so a fixed `now`
   produces byte-identical output. `tests/unit/test_seed_determinism.py` depends on this.
2. **Relative to now.** `now` is *injected*, never read from the clock inside this module.
   The app passes `datetime.now(UTC)` at startup so "last 90 days" always has data; tests
   pass a frozen instant so fixtures are stable. Reading the clock here would force a
   choice between the two.

The default horizon is 400 days because the Postman collections hard-code a window
starting 2026-05-01 — 151 days before 2026-09-29. A shorter horizon leaves those requests
returning empty rows and fails their assertions.

Beyond volume, the data is shaped to carry a **causal story** on Boiler Feed Pump 101 so
the acceptance scenario has something real to discover: lube-oil pressure loss precedes
bearing vibration, which escalates in frequency while acknowledgement gets slower. See
`_seed_bfp101_story`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from random import Random

from apps.alarm_api.domain import (
    UNITS,
    Alarm,
    AlarmStatus,
    AlarmType,
    Asset,
    AssetType,
    Severity,
    Site,
)

DEFAULT_SEED = 1729
DEFAULT_DAYS_OF_HISTORY = 400

# The planted patterns sit at fixed offsets from `now` — the furthest is an alarm flood at
# 135 days and background clusters reaching 160. A shorter horizon than this would push
# them outside the declared window, so `build_world` refuses rather than silently
# generating a world that is missing the guarantees the rest of the project relies on.
MIN_DAYS_OF_HISTORY = 180

# The asset the mandatory acceptance scenario investigates.
BFP101_NAME = "Boiler Feed Pump 101"


@dataclass
class World:
    """An immutable-by-convention snapshot of generated data."""

    generated_at: datetime
    seed: int
    days_of_history: int
    assets: list[Asset] = field(default_factory=list)
    alarms: list[Alarm] = field(default_factory=list)

    @property
    def window_start(self) -> datetime:
        return self.generated_at - timedelta(days=self.days_of_history)


class _IdFactory:
    """Sequential, collision-free ids. Deterministic because call order is deterministic."""

    def __init__(self) -> None:
        self._alarm_seq = 0
        self._asset_seq: dict[str, int] = {}

    def asset_id(self, asset_type: AssetType) -> str:
        abbrev = {
            AssetType.CENTRIFUGAL_PUMP: "PMP",
            AssetType.RECIPROCATING_COMPRESSOR: "CMP",
            AssetType.CENTRIFUGAL_COMPRESSOR: "CMP",
            AssetType.MOTOR: "MTR",
            AssetType.HEAT_EXCHANGER: "HEX",
            AssetType.CONTROL_VALVE: "CV",
            AssetType.VESSEL: "VES",
            AssetType.FAN: "FAN",
        }[asset_type]
        self._asset_seq[abbrev] = self._asset_seq.get(abbrev, 0) + 1
        return f"AST-{abbrev}-{self._asset_seq[abbrev]:04d}"

    def alarm_id(self, when: datetime) -> str:
        self._alarm_seq += 1
        return f"ALM-{when:%Y%m%d}-{self._alarm_seq:06d}"


# --- Alarm catalogue -------------------------------------------------------------------
# (alarm_name suffix, alarm_type, default severity, setpoint, unit_of_measure)
_CATALOGUE: dict[AssetType, tuple[tuple[str, AlarmType, Severity, float, str], ...]] = {
    AssetType.CENTRIFUGAL_PUMP: (
        ("Bearing Vibration High", AlarmType.PROCESS, Severity.HIGH, 4.5, "mm/s"),
        ("Lube Oil Pressure Low", AlarmType.DEVICE, Severity.HIGH, 1.8, "bar"),
        ("Suction Pressure Low", AlarmType.PROCESS, Severity.MEDIUM, 3.2, "bar"),
        ("Discharge Flow Deviation", AlarmType.PROCESS, Severity.MEDIUM, 12.0, "%"),
        ("Seal Leakage Detected", AlarmType.DEVICE, Severity.MEDIUM, 1.0, "L/h"),
        ("Motor Overload Trip", AlarmType.SAFETY, Severity.CRITICAL, 110.0, "%FLA"),
    ),
    AssetType.RECIPROCATING_COMPRESSOR: (
        ("Compressor Discharge Pressure High", AlarmType.PROCESS, Severity.HIGH, 46.0, "bar"),
        ("Cylinder Temperature High", AlarmType.PROCESS, Severity.HIGH, 165.0, "degC"),
        ("Suction Filter Differential High", AlarmType.DEVICE, Severity.MEDIUM, 0.5, "bar"),
        ("Lube Oil Level Low", AlarmType.DEVICE, Severity.MEDIUM, 25.0, "%"),
        ("Emergency Shutdown Activated", AlarmType.SAFETY, Severity.CRITICAL, 1.0, "bool"),
    ),
    AssetType.CENTRIFUGAL_COMPRESSOR: (
        ("Compressor Discharge Pressure High", AlarmType.PROCESS, Severity.HIGH, 52.0, "bar"),
        ("Surge Detected", AlarmType.PROCESS, Severity.CRITICAL, 1.0, "bool"),
        ("Aftercooler Outlet Temperature High", AlarmType.PROCESS, Severity.MEDIUM, 48.0, "degC"),
        ("Axial Displacement High", AlarmType.DEVICE, Severity.HIGH, 0.6, "mm"),
    ),
    AssetType.MOTOR: (
        ("Motor Winding Temperature High", AlarmType.DEVICE, Severity.HIGH, 130.0, "degC"),
        ("Motor Overload Trip", AlarmType.SAFETY, Severity.CRITICAL, 110.0, "%FLA"),
        ("Earth Fault Detected", AlarmType.SAFETY, Severity.CRITICAL, 1.0, "bool"),
        ("Bearing Temperature High", AlarmType.DEVICE, Severity.HIGH, 95.0, "degC"),
        ("Insulation Resistance Low", AlarmType.DEVICE, Severity.MEDIUM, 5.0, "MOhm"),
    ),
    AssetType.HEAT_EXCHANGER: (
        ("Tube Side Fouling Indicated", AlarmType.PROCESS, Severity.MEDIUM, 0.8, "bar"),
        ("Outlet Temperature Deviation", AlarmType.PROCESS, Severity.MEDIUM, 8.0, "degC"),
        ("Shell Pressure High", AlarmType.PROCESS, Severity.HIGH, 22.0, "bar"),
    ),
    AssetType.CONTROL_VALVE: (
        ("Valve Position Deviation", AlarmType.INSTRUMENT, Severity.MEDIUM, 5.0, "%"),
        ("Actuator Air Supply Low", AlarmType.DEVICE, Severity.HIGH, 4.0, "bar"),
        ("Valve Stiction Detected", AlarmType.INSTRUMENT, Severity.LOW, 2.0, "%"),
    ),
    AssetType.VESSEL: (
        ("Level Low", AlarmType.PROCESS, Severity.MEDIUM, 30.0, "%"),
        ("Level High High", AlarmType.SAFETY, Severity.CRITICAL, 90.0, "%"),
        ("Pressure High", AlarmType.PROCESS, Severity.HIGH, 14.0, "bar"),
    ),
    AssetType.FAN: (
        ("Fan Vibration High", AlarmType.PROCESS, Severity.HIGH, 6.0, "mm/s"),
        ("Motor Current High", AlarmType.DEVICE, Severity.MEDIUM, 95.0, "%FLA"),
        ("Inlet Damper Fault", AlarmType.INSTRUMENT, Severity.LOW, 1.0, "bool"),
    ),
}

_MANUFACTURERS = {
    AssetType.CENTRIFUGAL_PUMP: ("Sulzer", "HPcp 200-400"),
    AssetType.RECIPROCATING_COMPRESSOR: ("Ariel", "JGK/4"),
    AssetType.CENTRIFUGAL_COMPRESSOR: ("Elliott", "38M9"),
    AssetType.MOTOR: ("ABB", "M3BP 355"),
    AssetType.HEAT_EXCHANGER: ("Alfa Laval", "M15-BFG"),
    AssetType.CONTROL_VALVE: ("Fisher", "ED-2052"),
    AssetType.VESSEL: ("Chart", "DA-2000"),
    AssetType.FAN: ("Howden", "AF-1600"),
}


@dataclass
class _AssetSpec:
    """Declarative description of an anchor asset, resolved into an `Asset` later."""

    name: str
    asset_type: AssetType
    site: Site
    unit: str
    tag: str
    criticality: int
    related: tuple[str, ...] = ()


# Anchor assets exist to satisfy specific, named requirements. Each comment records which.
_ANCHORS: tuple[_AssetSpec, ...] = (
    # --- The acceptance scenario, EastRefinery / Unit 2 ---
    _AssetSpec(
        BFP101_NAME,
        AssetType.CENTRIFUGAL_PUMP,
        Site.EAST_REFINERY,
        "Unit 2",
        "2-BFP-101",
        5,
        related=(
            "Boiler Feed Pump 102",
            "BFP-101 Lube Oil Pump",
            "BFP-101 Discharge Control Valve",
            "Motor M-201",
            "Deaerator DA-201",
        ),
    ),
    # CHAIN-05 searches "Boiler Feed Pump 102" and needs at least one ACTIVE alarm.
    _AssetSpec(
        "Boiler Feed Pump 102",
        AssetType.CENTRIFUGAL_PUMP,
        Site.EAST_REFINERY,
        "Unit 2",
        "2-BFP-102",
        5,
        related=(BFP101_NAME,),
    ),
    _AssetSpec(
        "BFP-101 Lube Oil Pump",
        AssetType.CENTRIFUGAL_PUMP,
        Site.EAST_REFINERY,
        "Unit 2",
        "2-BFP-101-LOP",
        3,
        related=(BFP101_NAME,),
    ),
    _AssetSpec(
        "BFP-101 Discharge Control Valve",
        AssetType.CONTROL_VALVE,
        Site.EAST_REFINERY,
        "Unit 2",
        "2-FCV-1014",
        3,
        related=(BFP101_NAME,),
    ),
    _AssetSpec(
        "Motor M-201",
        AssetType.MOTOR,
        Site.EAST_REFINERY,
        "Unit 2",
        "2-M-201",
        4,
        related=(BFP101_NAME,),
    ),
    # Suction source: its level alarms co-occur with BFP-101 Suction Pressure Low, so the
    # copilot can only find that contributing factor by following related assets.
    _AssetSpec(
        "Deaerator DA-201",
        AssetType.VESSEL,
        Site.EAST_REFINERY,
        "Unit 2",
        "2-DA-201",
        4,
        related=(BFP101_NAME,),
    ),
    # Extra Unit 2 assets give the flood bursts somewhere to land.
    _AssetSpec(
        "Feedwater Heater E-204",
        AssetType.HEAT_EXCHANGER,
        Site.EAST_REFINERY,
        "Unit 2",
        "2-E-204",
        3,
    ),
    _AssetSpec(
        "Condensate Pump 201",
        AssetType.CENTRIFUGAL_PUMP,
        Site.EAST_REFINERY,
        "Unit 2",
        "2-CP-201",
        2,
    ),
    _AssetSpec("Deaerator Vent Fan", AssetType.FAN, Site.EAST_REFINERY, "Unit 2", "2-FN-201", 2),
    # CHAIN-03 searches "compressor" and reads results[0..2]: at least three must match.
    # SouthPlant / Unit 3 also serves CHAIN-04 (critical alarm density on Unit 3).
    _AssetSpec(
        "Air Compressor K-301",
        AssetType.RECIPROCATING_COMPRESSOR,
        Site.SOUTH_PLANT,
        "Unit 3",
        "3-K-301",
        4,
        related=("Air Compressor K-302",),
    ),
    _AssetSpec(
        "Air Compressor K-302",
        AssetType.RECIPROCATING_COMPRESSOR,
        Site.SOUTH_PLANT,
        "Unit 3",
        "3-K-302",
        4,
        related=("Air Compressor K-301",),
    ),
    _AssetSpec(
        "Recycle Gas Compressor K-303",
        AssetType.CENTRIFUGAL_COMPRESSOR,
        Site.SOUTH_PLANT,
        "Unit 3",
        "3-K-303",
        5,
    ),
    _AssetSpec(
        "Instrument Air Compressor K-304",
        AssetType.RECIPROCATING_COMPRESSOR,
        Site.SOUTH_PLANT,
        "Unit 3",
        "3-K-304",
        3,
    ),
    # CHAIN-08 searches query=motor&unit=Unit 5 and reads results[0..2], then filters
    # alarm_types ["safety","device"] — the motor catalogue supplies both.
    _AssetSpec(
        "Motor M-501",
        AssetType.MOTOR,
        Site.NORTH_PLANT,
        "Unit 5",
        "5-M-501",
        4,
        related=("Motor M-502",),
    ),
    _AssetSpec(
        "Motor M-502",
        AssetType.MOTOR,
        Site.NORTH_PLANT,
        "Unit 5",
        "5-M-502",
        3,
        related=("Motor M-501",),
    ),
    _AssetSpec("Motor M-503", AssetType.MOTOR, Site.NORTH_PLANT, "Unit 5", "5-M-503", 3),
    # CHAIN-06 filters site=NorthPlant AND unit=Unit 1 for stale + recurring alarms.
    _AssetSpec(
        "Reflux Pump 110", AssetType.CENTRIFUGAL_PUMP, Site.NORTH_PLANT, "Unit 1", "1-P-110", 3
    ),
    _AssetSpec(
        "Overhead Condenser E-101",
        AssetType.HEAT_EXCHANGER,
        Site.NORTH_PLANT,
        "Unit 1",
        "1-E-101",
        3,
    ),
    _AssetSpec(
        "Column Feed Valve", AssetType.CONTROL_VALVE, Site.NORTH_PLANT, "Unit 1", "1-FCV-101", 2
    ),
    # CHAIN-10 needs nuisance/chattering alarms on Unit 4 with recurrence >= 8.
    _AssetSpec(
        "Cooling Water Pump 401",
        AssetType.CENTRIFUGAL_PUMP,
        Site.SOUTH_PLANT,
        "Unit 4",
        "4-P-401",
        2,
    ),
    _AssetSpec("Cooling Tower Fan 401", AssetType.FAN, Site.SOUTH_PLANT, "Unit 4", "4-FN-401", 2),
    _AssetSpec(
        "Cooling Water Return Valve",
        AssetType.CONTROL_VALVE,
        Site.SOUTH_PLANT,
        "Unit 4",
        "4-TCV-401",
        2,
    ),
)


def build_world(
    now: datetime,
    seed: int = DEFAULT_SEED,
    days_of_history: int = DEFAULT_DAYS_OF_HISTORY,
) -> World:
    """Build the full simulated world. Pure: same inputs -> same output."""
    if days_of_history < MIN_DAYS_OF_HISTORY:
        raise ValueError(
            f"days_of_history must be at least {MIN_DAYS_OF_HISTORY}, got {days_of_history}. "
            "The planted patterns the Postman collections and the acceptance scenario rely "
            "on sit up to 160 days before `now`; a shorter horizon would place them outside "
            "the generated window."
        )
    rng = Random(seed)
    ids = _IdFactory()
    world = World(generated_at=now, seed=seed, days_of_history=days_of_history)

    assets = _build_assets(rng, ids, now)
    world.assets = assets
    by_name = {a.name: a for a in assets}

    alarms: list[Alarm] = []
    # Baseline noise across every asset and the whole horizon, so filters on any
    # site/unit/type return something and KPIs are not degenerate.
    for asset in assets:
        alarms.extend(_seed_background(rng, ids, asset, now, days_of_history))

    # Planted, requirement-driven patterns layered on top of the noise.
    alarms.extend(_seed_bfp101_story(rng, ids, by_name, now))
    alarms.extend(_seed_unit2_floods(rng, ids, assets, now))
    alarms.extend(_seed_northplant_unit1_stale(rng, ids, by_name, now))
    alarms.extend(_seed_unit4_nuisance(rng, ids, by_name, now))
    alarms.extend(_seed_unit3_critical(rng, ids, by_name, now))
    alarms.extend(_ensure_active_alarms(rng, ids, by_name, now))

    alarms.sort(key=lambda a: a.start_time)
    world.alarms = alarms
    return world


def _build_assets(rng: Random, ids: _IdFactory, now: datetime) -> list[Asset]:
    specs = list(_ANCHORS)

    # Filler assets so every (site, unit) pair is populated. Keeps site/unit filters
    # honest without hand-writing 40 more anchors.
    filler_types = (
        AssetType.CENTRIFUGAL_PUMP,
        AssetType.HEAT_EXCHANGER,
        AssetType.CONTROL_VALVE,
        AssetType.MOTOR,
    )
    for site in Site:
        for unit in UNITS:
            existing = sum(1 for s in specs if s.site is site and s.unit == unit)
            for i in range(max(0, 3 - existing)):
                atype = filler_types[i % len(filler_types)]
                n = rng.randint(10, 99)
                unit_no = unit.split()[-1]
                label = {
                    AssetType.CENTRIFUGAL_PUMP: "Transfer Pump",
                    AssetType.HEAT_EXCHANGER: "Exchanger",
                    AssetType.CONTROL_VALVE: "Flow Valve",
                    AssetType.MOTOR: "Drive Motor",
                }[atype]
                specs.append(
                    _AssetSpec(
                        name=f"{site.value} {unit} {label} {unit_no}{n:02d}",
                        asset_type=atype,
                        site=site,
                        unit=unit,
                        tag=f"{unit_no}-{label.split()[0][:2].upper()}-{n:03d}",
                        criticality=rng.randint(1, 3),
                    )
                )

    assets: list[Asset] = []
    name_to_id: dict[str, str] = {}
    for spec in specs:
        asset_id = ids.asset_id(spec.asset_type)
        name_to_id[spec.name] = asset_id
        manufacturer, model = _MANUFACTURERS[spec.asset_type]
        assets.append(
            Asset(
                asset_id=asset_id,
                name=spec.name,
                asset_type=spec.asset_type,
                site=spec.site,
                unit=spec.unit,
                tag=spec.tag,
                criticality=spec.criticality,
                manufacturer=manufacturer,
                model=model,
                commissioned_on=now - timedelta(days=rng.randint(1200, 6000)),
                maintenance_strategy=rng.choice(
                    ["condition_based", "preventive", "run_to_failure"]
                ),
                design_params=_design_params(spec.asset_type),
            )
        )

    # Resolve related-asset names to ids now that every id exists.
    by_id = {a.asset_id: a for a in assets}
    for spec in specs:
        if not spec.related:
            continue
        asset = by_id[name_to_id[spec.name]]
        asset.related_asset_ids = [name_to_id[r] for r in spec.related if r in name_to_id]
    return assets


def _design_params(asset_type: AssetType) -> dict[str, float | str]:
    match asset_type:
        case AssetType.CENTRIFUGAL_PUMP:
            return {
                "rated_flow_m3h": 320.0,
                "rated_head_m": 1450.0,
                "vibration_alert_mm_s": 4.5,
                "vibration_trip_mm_s": 7.1,
                "lube_oil_normal_bar": 2.0,
                "lube_oil_trip_bar": 1.4,
            }
        case AssetType.MOTOR:
            return {"rated_kw": 2500.0, "voltage_kv": 6.6, "insulation_class": "F"}
        case AssetType.RECIPROCATING_COMPRESSOR | AssetType.CENTRIFUGAL_COMPRESSOR:
            return {"rated_flow_nm3h": 12000.0, "discharge_design_bar": 52.0}
        case _:
            return {}


def _mk_alarm(
    ids: _IdFactory,
    asset: Asset,
    alarm_name: str,
    alarm_type: AlarmType,
    severity: Severity,
    start_time: datetime,
    *,
    status: AlarmStatus,
    ack_after_minutes: float | None = None,
    clear_after_minutes: float | None = None,
    value: float | None = None,
    setpoint: float | None = None,
    unit_of_measure: str | None = None,
    operator_id: str | None = None,
    chattering: bool = False,
) -> Alarm:
    """Build one alarm, deriving ack/clear timestamps and delays consistently."""
    ack_time = (
        start_time + timedelta(minutes=ack_after_minutes) if ack_after_minutes is not None else None
    )
    clear_time = (
        start_time + timedelta(minutes=clear_after_minutes)
        if clear_after_minutes is not None
        else None
    )
    priority = {
        Severity.LOW: 5,
        Severity.MEDIUM: 4,
        Severity.HIGH: 2,
        Severity.CRITICAL: 1,
    }[severity]
    return Alarm(
        alarm_id=ids.alarm_id(start_time),
        # Deliberately NOT prefixed with the asset tag: recurrence counting and
        # correlation group by alarm_name, so it has to stay comparable across assets.
        # Asset context travels in asset_id / asset_name.
        alarm_name=alarm_name,
        alarm_type=alarm_type,
        asset_id=asset.asset_id,
        asset_name=asset.name,
        site=asset.site,
        unit=asset.unit,
        severity=severity,
        status=status,
        start_time=start_time,
        ack_time=ack_time,
        clear_time=clear_time,
        ack_delay_minutes=ack_after_minutes,
        duration_minutes=clear_after_minutes,
        value=value,
        setpoint=setpoint,
        unit_of_measure=unit_of_measure,
        priority_hint=priority,
        operator_id=operator_id,
        suppressed=status is AlarmStatus.SUPPRESSED,
        chattering=chattering,
    )


def _instant(
    rng: Random, now: datetime, max_days_ago: float, min_days_ago: float = 0.0
) -> datetime:
    """A uniformly random instant in `[now - max_days_ago, now - min_days_ago]`.

    Exists because the obvious spelling is subtly wrong: writing
    `now - timedelta(days=rng.uniform(0, days), hours=rng.uniform(0, 24))` adds up to a
    further day of offset on top of an already-full-range day draw, so it can place an
    alarm *before* `window_start`. A fractional day offset already gives sub-day
    resolution; the extra hours term was never needed.
    """
    return now - timedelta(days=rng.uniform(min_days_ago, max_days_ago))


def _shift_bias(rng: Random, when: datetime) -> float:
    """Night shift acknowledges more slowly, so ack-delay KPIs are not flat."""
    return rng.uniform(1.8, 3.2) if when.hour < 6 or when.hour >= 22 else rng.uniform(0.7, 1.4)


def _seed_background(
    rng: Random, ids: _IdFactory, asset: Asset, now: datetime, days: int
) -> list[Alarm]:
    """Low-rate noise for one asset across the whole horizon."""
    catalogue = _CATALOGUE[asset.asset_type]
    # More critical assets alarm more often.
    per_month = rng.uniform(1.0, 2.5) + asset.criticality * 0.6
    count = int(per_month * days / 30)
    out: list[Alarm] = []
    # Weighted, not uniform: a uniform draw over the catalogue gives a pump six critical
    # motor trips in 90 days, and real plant data does not look like that — an asset that
    # tripped that often would have been taken out of service. Weighting keeps the severity
    # mix plausible so the KPIs (critical_count especially) mean something.
    weights = [
        {
            Severity.LOW: 6.0,
            Severity.MEDIUM: 5.0,
            Severity.HIGH: 2.0,
            Severity.CRITICAL: 0.35,
        }[entry[2]]
        for entry in catalogue
    ]
    for _ in range(count):
        name, atype, severity, setpoint, uom = rng.choices(catalogue, weights=weights, k=1)[0]
        start = _instant(rng, now, days)
        # Most background alarms are resolved; a few remain open.
        roll = rng.random()
        bias = _shift_bias(rng, start)
        if roll < 0.80:
            ack = round(rng.uniform(1, 25) * bias, 1)
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    start,
                    status=AlarmStatus.CLEARED,
                    ack_after_minutes=ack,
                    clear_after_minutes=ack + rng.uniform(2, 240),
                    value=setpoint * rng.uniform(1.02, 1.3),
                    setpoint=setpoint,
                    unit_of_measure=uom,
                    operator_id=f"OP-{rng.randint(1, 12):02d}",
                )
            )
        elif roll < 0.93:
            ack = round(rng.uniform(2, 45) * bias, 1)
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    start,
                    status=AlarmStatus.ACKNOWLEDGED,
                    ack_after_minutes=ack,
                    value=setpoint * rng.uniform(1.02, 1.25),
                    setpoint=setpoint,
                    unit_of_measure=uom,
                    operator_id=f"OP-{rng.randint(1, 12):02d}",
                )
            )
        else:
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    start,
                    status=AlarmStatus.ACTIVE,
                    value=setpoint * rng.uniform(1.05, 1.4),
                    setpoint=setpoint,
                    unit_of_measure=uom,
                )
            )
    return out


def _seed_bfp101_story(
    rng: Random, ids: _IdFactory, by_name: dict[str, Asset], now: datetime
) -> list[Alarm]:
    """The planted causal narrative the acceptance scenario is meant to discover.

    Shape (all inside the last 90 days so a "last 90 days" question finds it):

    * ``Bearing Vibration High`` recurs 23 times and **escalates** — roughly weekly at day
      90, roughly four times a week in the last fortnight — while mean acknowledgement
      delay drifts from ~4 to ~19 minutes. This is the headline finding.
    * ``Lube Oil Pressure Low`` occurs 14 times and **83% of them start 6-22 minutes
      before** a vibration alarm. Correlation with a 30-minute lag window therefore
      returns a directional, high-support pair: the derivable contributing factor.
    * ``Suction Pressure Low`` clusters on four days alongside Deaerator DA-201 level
      alarms — a second factor (cavitation) that is only visible if related assets are
      followed.
    * ``Discharge Flow Deviation`` fires 31 times, mostly self-clearing inside a minute
      and never acknowledged: a nuisance alarm, so the answer can separate noise from
      signal.
    * ``Motor Overload Trip`` twice, one still ``active``, each immediately preceding a
      Unit 2 flood burst and giving the recommendation tool a live critical alarm.
    """
    bfp = by_name[BFP101_NAME]
    deaerator = by_name["Deaerator DA-201"]
    out: list[Alarm] = []

    # --- escalating bearing vibration -------------------------------------------------
    vibration_starts: list[datetime] = []
    # 15 occurrences spread over days 90..14, then 8 packed into the last 14 days.
    for i in range(15):
        day = 90 - i * (76 / 15)
        vibration_starts.append(now - timedelta(days=day, hours=rng.uniform(0, 24)))
    for i in range(8):
        day = 14 - i * (14 / 8)
        vibration_starts.append(now - timedelta(days=max(0.2, day), hours=rng.uniform(0, 12)))
    vibration_starts.sort()

    total = len(vibration_starts)
    for idx, start in enumerate(vibration_starts):
        # Acknowledgement degrades as the problem becomes routine ("alarm fatigue").
        progress = idx / max(1, total - 1)
        ack = round(4.0 + progress * 15.0 + rng.uniform(-1.5, 1.5), 1)
        last_one = idx == total - 1
        out.append(
            _mk_alarm(
                ids,
                bfp,
                "Bearing Vibration High",
                AlarmType.PROCESS,
                Severity.HIGH,
                start,
                # Leave the most recent one open so the GUI has a live alarm to act on.
                status=AlarmStatus.ACTIVE if last_one else AlarmStatus.CLEARED,
                ack_after_minutes=None if last_one else ack,
                clear_after_minutes=None if last_one else ack + rng.uniform(25, 180),
                value=round(4.5 * (1.05 + progress * 0.45), 2),
                setpoint=4.5,
                unit_of_measure="mm/s",
                operator_id=None if last_one else f"OP-{rng.randint(1, 12):02d}",
            )
        )

    # --- lube oil pressure low: precursor in 12 of 14 cases ---------------------------
    precursor_targets = rng.sample(vibration_starts, 12)
    for start_of_vibration in precursor_targets:
        lead = rng.uniform(6, 22)
        start = start_of_vibration - timedelta(minutes=lead)
        ack = round(rng.uniform(3, 18), 1)
        out.append(
            _mk_alarm(
                ids,
                bfp,
                "Lube Oil Pressure Low",
                AlarmType.DEVICE,
                Severity.HIGH,
                start,
                status=AlarmStatus.CLEARED,
                ack_after_minutes=ack,
                clear_after_minutes=ack + rng.uniform(15, 90),
                value=round(rng.uniform(1.45, 1.78), 2),
                setpoint=1.8,
                unit_of_measure="bar",
                operator_id=f"OP-{rng.randint(1, 12):02d}",
            )
        )
    for _ in range(2):  # a couple unrelated to vibration, so correlation isn't 100%
        start = _instant(rng, now, 85, min_days_ago=20)
        out.append(
            _mk_alarm(
                ids,
                bfp,
                "Lube Oil Pressure Low",
                AlarmType.DEVICE,
                Severity.HIGH,
                start,
                status=AlarmStatus.CLEARED,
                ack_after_minutes=8.0,
                clear_after_minutes=55.0,
                value=1.72,
                setpoint=1.8,
                unit_of_measure="bar",
                operator_id="OP-04",
            )
        )

    # --- suction pressure low, clustered with deaerator level --------------------------
    for day in (74.0, 51.0, 33.0, 9.0):
        base = now - timedelta(days=day, hours=rng.uniform(0, 20))
        for k in range(rng.randint(2, 3)):
            start = base + timedelta(minutes=k * rng.uniform(25, 90))
            out.append(
                _mk_alarm(
                    ids,
                    bfp,
                    "Suction Pressure Low",
                    AlarmType.PROCESS,
                    Severity.MEDIUM,
                    start,
                    status=AlarmStatus.CLEARED,
                    ack_after_minutes=6.0,
                    clear_after_minutes=48.0,
                    value=round(rng.uniform(2.6, 3.15), 2),
                    setpoint=3.2,
                    unit_of_measure="bar",
                    operator_id="OP-07",
                )
            )
        # The upstream cause, on a different asset.
        out.append(
            _mk_alarm(
                ids,
                deaerator,
                "Level Low",
                AlarmType.PROCESS,
                Severity.MEDIUM,
                base - timedelta(minutes=rng.uniform(4, 14)),
                status=AlarmStatus.CLEARED,
                ack_after_minutes=5.0,
                clear_after_minutes=40.0,
                value=round(rng.uniform(24, 29), 1),
                setpoint=30.0,
                unit_of_measure="%",
                operator_id="OP-07",
            )
        )

    # --- chattering nuisance alarm ----------------------------------------------------
    for _ in range(31):
        start = _instant(rng, now, 90)
        quick = rng.random() < 0.78
        out.append(
            _mk_alarm(
                ids,
                bfp,
                "Discharge Flow Deviation",
                AlarmType.PROCESS,
                Severity.MEDIUM,
                start,
                status=AlarmStatus.CLEARED,
                ack_after_minutes=None,  # never acknowledged
                clear_after_minutes=rng.uniform(0.2, 0.9) if quick else rng.uniform(2, 20),
                value=round(rng.uniform(12.5, 22.0), 1),
                setpoint=12.0,
                unit_of_measure="%",
                chattering=True,
            )
        )

    # --- critical motor overload trips ------------------------------------------------
    for day, still_active in ((19.0, False), (6.0, True)):
        start = now - timedelta(days=day, hours=3, minutes=12)
        out.append(
            _mk_alarm(
                ids,
                bfp,
                "Motor Overload Trip",
                AlarmType.SAFETY,
                Severity.CRITICAL,
                start,
                status=AlarmStatus.ACTIVE if still_active else AlarmStatus.CLEARED,
                ack_after_minutes=None if still_active else 2.0,
                clear_after_minutes=None if still_active else 95.0,
                value=round(rng.uniform(112, 128), 1),
                setpoint=110.0,
                unit_of_measure="%FLA",
                operator_id=None if still_active else "OP-02",
            )
        )
    return out


def _seed_unit2_floods(
    rng: Random, ids: _IdFactory, assets: list[Asset], now: datetime
) -> list[Alarm]:
    """Alarm floods in Unit 2: >=10 alarms inside a 10-minute rolling window.

    Placed so that `POST /alarms/flood-analysis` returns non-empty `flood_windows` both
    for a recent question and for the Postman collections' hard-coded 2026-05-01..
    2026-07-01 window (90-151 days before today) — hence the bursts at 118 and 135 days.
    Two of them trail the BFP-101 motor trips, tying the flood into the story.
    """
    unit2 = [a for a in assets if a.unit == "Unit 2" and a.site is Site.EAST_REFINERY]
    out: list[Alarm] = []
    for day in (6.0, 19.0, 47.0, 118.0, 135.0):
        burst_start = now - timedelta(days=day, hours=3, minutes=10)
        for i in range(rng.randint(18, 25)):
            asset = rng.choice(unit2)
            name, atype, severity, setpoint, uom = rng.choice(_CATALOGUE[asset.asset_type])
            # All inside ~6 minutes, comfortably above 10-per-10-minutes.
            start = burst_start + timedelta(seconds=i * rng.uniform(8, 18))
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    start,
                    status=AlarmStatus.CLEARED,
                    ack_after_minutes=round(rng.uniform(8, 40), 1),
                    clear_after_minutes=rng.uniform(45, 200),
                    value=setpoint * rng.uniform(1.02, 1.35),
                    setpoint=setpoint,
                    unit_of_measure=uom,
                    operator_id=f"OP-{rng.randint(1, 12):02d}",
                )
            )
    return out


def _seed_northplant_unit1_stale(
    rng: Random, ids: _IdFactory, by_name: dict[str, Asset], now: datetime
) -> list[Alarm]:
    """CHAIN-06: NorthPlant/Unit 1 needs stale (>180 min unacked) and recurring (>6)."""
    out: list[Alarm] = []
    for asset_name in ("Reflux Pump 110", "Overhead Condenser E-101", "Column Feed Valve"):
        asset = by_name[asset_name]
        name, atype, severity, setpoint, uom = _CATALOGUE[asset.asset_type][0]
        # Recurring: comfortably above the recurrence_threshold of 6.
        for _ in range(rng.randint(9, 13)):
            start = _instant(rng, now, 150)
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    start,
                    status=AlarmStatus.CLEARED,
                    ack_after_minutes=round(rng.uniform(5, 60), 1),
                    clear_after_minutes=rng.uniform(70, 300),
                    value=setpoint * 1.15,
                    setpoint=setpoint,
                    unit_of_measure=uom,
                    operator_id=f"OP-{rng.randint(1, 12):02d}",
                )
            )
        # Stale: active and unacknowledged for far longer than 180 minutes.
        for _ in range(2):
            start = now - timedelta(days=rng.uniform(1, 40))
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    start,
                    status=AlarmStatus.ACTIVE,
                    value=setpoint * 1.2,
                    setpoint=setpoint,
                    unit_of_measure=uom,
                )
            )
    return out


def _seed_unit4_nuisance(
    rng: Random, ids: _IdFactory, by_name: dict[str, Asset], now: datetime
) -> list[Alarm]:
    """CHAIN-10: Unit 4 needs nuisance alarms recurring at least 8 times."""
    out: list[Alarm] = []
    for asset_name in (
        "Cooling Water Pump 401",
        "Cooling Tower Fan 401",
        "Cooling Water Return Valve",
    ):
        asset = by_name[asset_name]
        name, atype, severity, setpoint, uom = _CATALOGUE[asset.asset_type][-1]
        for _ in range(rng.randint(14, 22)):
            start = _instant(rng, now, 160)
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    start,
                    status=AlarmStatus.CLEARED,
                    ack_after_minutes=None,
                    clear_after_minutes=rng.uniform(0.2, 0.8),
                    value=setpoint * 1.05,
                    setpoint=setpoint,
                    unit_of_measure=uom,
                    chattering=True,
                )
            )
    return out


def _seed_unit3_critical(
    rng: Random, ids: _IdFactory, by_name: dict[str, Asset], now: datetime
) -> list[Alarm]:
    """CHAIN-04: Unit 3 critical-alarm density needs a meaningful critical population."""
    out: list[Alarm] = []
    for asset_name in (
        "Air Compressor K-301",
        "Air Compressor K-302",
        "Recycle Gas Compressor K-303",
    ):
        asset = by_name[asset_name]
        criticals = [c for c in _CATALOGUE[asset.asset_type] if c[2] is Severity.CRITICAL]
        highs = [c for c in _CATALOGUE[asset.asset_type] if c[2] is Severity.HIGH]
        for _ in range(rng.randint(5, 9)):
            name, atype, severity, setpoint, uom = rng.choice(criticals or highs)
            start = _instant(rng, now, 160)
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    start,
                    status=AlarmStatus.CLEARED,
                    ack_after_minutes=round(rng.uniform(1, 12), 1),
                    clear_after_minutes=rng.uniform(30, 260),
                    value=setpoint * 1.1,
                    setpoint=setpoint,
                    unit_of_measure=uom,
                    operator_id=f"OP-{rng.randint(1, 12):02d}",
                )
            )
        # Co-occurring pairs so correlation on the compressors returns support.
        for _ in range(4):
            base = _instant(rng, now, 150)
            name, atype, severity, setpoint, uom = rng.choice(highs or criticals)
            out.append(
                _mk_alarm(
                    ids,
                    asset,
                    name,
                    atype,
                    severity,
                    base,
                    status=AlarmStatus.CLEARED,
                    ack_after_minutes=4.0,
                    clear_after_minutes=60.0,
                    value=setpoint * 1.08,
                    setpoint=setpoint,
                    unit_of_measure=uom,
                    operator_id="OP-05",
                )
            )
    return out


def _ensure_active_alarms(
    rng: Random, ids: _IdFactory, by_name: dict[str, Asset], now: datetime
) -> list[Alarm]:
    """Guarantee the ACTIVE alarms specific chains assert on.

    CHAIN-09 requires at least one active alarm at EastRefinery; CHAIN-05 requires one on
    Boiler Feed Pump 102. Background noise makes these *likely* but not *certain*, and a
    probabilistic guarantee is not a guarantee — so plant them explicitly.
    """
    out: list[Alarm] = []
    guaranteed = ("Boiler Feed Pump 102", "Feedwater Heater E-204", "Motor M-201")
    for asset_name in guaranteed:
        asset = by_name[asset_name]
        name, atype, severity, setpoint, uom = _CATALOGUE[asset.asset_type][0]
        start = now - timedelta(hours=rng.uniform(2, 60))
        out.append(
            _mk_alarm(
                ids,
                asset,
                name,
                atype,
                severity,
                start,
                status=AlarmStatus.ACTIVE,
                value=setpoint * rng.uniform(1.08, 1.3),
                setpoint=setpoint,
                unit_of_measure=uom,
            )
        )
    return out
