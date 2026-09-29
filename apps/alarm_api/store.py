"""Read-only query and analytics layer over a `World`.

In-memory rather than SQLite: the whole point of the simulator is to stand in for a real
alarm historian with zero setup, and every query here is a filter-then-aggregate over a
few tens of thousands of rows. Indexes on the high-selectivity columns plus a bisect over
the time axis keep it comfortably sub-millisecond, and the absence of a schema migration
step is worth more than the generality SQL would buy.

The store is deliberately pure and synchronous. Nothing here touches HTTP concepts or the
clock — request parsing lives in the routers, `now` arrives as an argument. That is what
makes the analytics unit-testable without a client.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from apps.alarm_api.domain import (
    SEVERITY_ORDER,
    Alarm,
    AlarmStatus,
    AlarmType,
    Asset,
    Severity,
    Site,
)
from apps.alarm_api.seed import World

# An (asset_id, alarm_name) pair seen at least this many times in the window is
# "recurring". Exposed via /analytics/kpi-definitions so the copilot can cite the rule
# rather than inventing a threshold.
RECURRENCE_THRESHOLD = 6

# Alarms that clear faster than this are transient noise, not events an operator acted on.
NUISANCE_MAX_DURATION_MINUTES = 1.0

# An unacknowledged active alarm older than this is "stale".
STALE_AFTER_MINUTES = 180


class AssetNotFoundError(LookupError):
    """Raised when an asset id does not exist. Routers map this to 404."""


class AlarmNotFoundError(LookupError):
    """Raised when an alarm id does not exist. Routers map this to 404."""


@dataclass(frozen=True)
class AlarmFilter:
    """Every way the API lets a caller narrow the alarm set.

    One filter object shared by `/alarms`, `/alarms/summary` and the analytics endpoints,
    so the filter semantics cannot drift between them.
    """

    asset_ids: Sequence[str] | None = None
    unit: str | None = None
    site: Site | None = None
    statuses: Sequence[AlarmStatus] | None = None
    severities: Sequence[Severity] | None = None
    alarm_types: Sequence[AlarmType] | None = None
    alarm_names: Sequence[str] | None = None
    min_severity: Severity | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None

    def describe(self) -> dict[str, Any]:
        """Echoed back as `filters_applied` so a caller can see what was actually used."""
        out: dict[str, Any] = {}
        if self.asset_ids:
            out["asset_ids"] = list(dict.fromkeys(self.asset_ids))
        if self.unit:
            out["unit"] = self.unit
        if self.site:
            out["site"] = self.site.value
        if self.statuses:
            out["status"] = [s.value for s in self.statuses]
        if self.severities:
            out["severity"] = [s.value for s in self.severities]
        if self.alarm_types:
            out["alarm_types"] = [t.value for t in self.alarm_types]
        if self.alarm_names:
            out["alarm_names"] = list(self.alarm_names)
        if self.min_severity:
            out["severity_threshold"] = self.min_severity.value
        if self.start_time:
            out["start_time"] = self.start_time
        if self.end_time:
            out["end_time"] = self.end_time
        return out


# Sort keys accepted by `GET /alarms`. Restricting to an explicit map means an unknown
# sort_by is a 422 rather than a silent fallback to insertion order.
SORT_KEYS: dict[str, Callable[[Alarm], Any]] = {
    "start_time": lambda a: a.start_time,
    "severity": lambda a: SEVERITY_ORDER[a.severity],
    "ack_delay_minutes": lambda a: (a.ack_delay_minutes is None, a.ack_delay_minutes or 0.0),
    "duration_minutes": lambda a: (a.duration_minutes is None, a.duration_minutes or 0.0),
    "asset_name": lambda a: a.asset_name,
    "alarm_name": lambda a: a.alarm_name,
}

# Grouping dimensions accepted by `/alarms/summary`, in the caller's chosen order.
GROUP_KEYS: dict[str, Callable[[Alarm], Any]] = {
    "asset_id": lambda a: a.asset_id,
    "asset_name": lambda a: a.asset_name,
    "site": lambda a: a.site.value,
    "unit": lambda a: a.unit,
    "severity": lambda a: a.severity.value,
    "alarm_type": lambda a: a.alarm_type.value,
    "alarm_name": lambda a: a.alarm_name,
    "status": lambda a: a.status.value,
    "day": lambda a: a.start_time.date().isoformat(),
}


class AlarmStore:
    """Indexed, read-only view of a generated world."""

    def __init__(self, world: World) -> None:
        self._world = world
        self._assets_by_id: dict[str, Asset] = {a.asset_id: a for a in world.assets}
        self._alarms_by_id: dict[str, Alarm] = {a.alarm_id: a for a in world.alarms}

        # Sorted by start_time (build_world guarantees this) so a time-range filter is a
        # pair of bisects instead of a full scan.
        self._by_time: list[Alarm] = list(world.alarms)
        self._start_times: list[datetime] = [a.start_time for a in self._by_time]

        self._by_asset: dict[str, list[Alarm]] = defaultdict(list)
        self._by_site: dict[Site, list[Alarm]] = defaultdict(list)
        self._by_unit: dict[str, list[Alarm]] = defaultdict(list)
        for alarm in self._by_time:
            self._by_asset[alarm.asset_id].append(alarm)
            self._by_site[alarm.site].append(alarm)
            self._by_unit[alarm.unit].append(alarm)

    # --- metadata ---------------------------------------------------------------------

    @property
    def world(self) -> World:
        return self._world

    @property
    def generated_at(self) -> datetime:
        return self._world.generated_at

    def stats(self) -> dict[str, Any]:
        return {
            "assets": len(self._assets_by_id),
            "alarms": len(self._alarms_by_id),
            "seed": self._world.seed,
            "days_of_history": self._world.days_of_history,
            "window_start": self._world.window_start,
            "generated_at": self._world.generated_at,
        }

    # --- assets -----------------------------------------------------------------------

    def get_asset(self, asset_id: str) -> Asset:
        try:
            return self._assets_by_id[asset_id]
        except KeyError:
            raise AssetNotFoundError(asset_id) from None

    def search_assets(
        self, query: str, limit: int = 10, unit: str | None = None, site: Site | None = None
    ) -> list[Asset]:
        """Rank assets by how well they match a free-text query.

        Ranking, not just filtering: the Postman chains read `results[0]`, `results[1]`
        and `results[2]` and expect the most relevant assets there. A search for
        "Boiler Feed Pump 101" has to put that exact pump first even though
        "Boiler Feed Pump 102" also matches most of the words.
        """
        needle = query.strip().lower()
        candidates: Iterable[Asset] = self._world.assets
        if unit:
            candidates = [a for a in candidates if a.unit == unit]
        if site:
            candidates = [a for a in candidates if a.site is site]
        if not needle:
            return sorted(candidates, key=lambda a: (-a.criticality, a.name))[:limit]

        terms = needle.split()
        scored: list[tuple[float, str, Asset]] = []
        for asset in candidates:
            haystack = f"{asset.name} {asset.tag} {asset.asset_id} {asset.asset_type}".lower()
            name = asset.name.lower()
            score = 0.0
            if name == needle:
                score += 100.0
            elif needle in name:
                # Prefer a prefix match, and shorter names, so "…101" beats "…101 Lube
                # Oil Pump" when the query is the full pump name.
                score += 60.0 if name.startswith(needle) else 40.0
                score += 10.0 * len(needle) / max(len(name), 1)
            elif needle in haystack:
                score += 25.0
            matched = sum(1 for t in terms if t in haystack)
            if matched == 0 and score == 0.0:
                continue
            score += 8.0 * matched
            score += asset.criticality * 0.5  # tie-break toward important equipment
            scored.append((score, asset.name, asset))

        scored.sort(key=lambda row: (-row[0], row[1]))
        return [asset for _, _, asset in scored[:limit]]

    def related_assets(self, asset_id: str) -> list[Asset]:
        asset = self.get_asset(asset_id)
        out = [self._assets_by_id[r] for r in asset.related_asset_ids if r in self._assets_by_id]
        if asset.parent_asset_id and asset.parent_asset_id in self._assets_by_id:
            out.append(self._assets_by_id[asset.parent_asset_id])
        return out

    # --- alarms -----------------------------------------------------------------------

    def get_alarm(self, alarm_id: str) -> Alarm:
        try:
            return self._alarms_by_id[alarm_id]
        except KeyError:
            raise AlarmNotFoundError(alarm_id) from None

    def query(self, flt: AlarmFilter) -> list[Alarm]:
        """Apply a filter, returning alarms in start_time order."""
        base = self._narrowest_index(flt)
        base = self._restrict_time(base, flt)
        return [a for a in base if self._matches(a, flt)]

    def _narrowest_index(self, flt: AlarmFilter) -> list[Alarm]:
        """Pick the most selective available index to avoid scanning everything."""
        if flt.asset_ids:
            # dict.fromkeys: asset_ids may legitimately contain duplicates (the Postman
            # correlation request sends the same id twice) and we must not double-count.
            out: list[Alarm] = []
            for asset_id in dict.fromkeys(flt.asset_ids):
                out.extend(self._by_asset.get(asset_id, ()))
            out.sort(key=lambda a: a.start_time)
            return out
        if flt.unit and flt.site:
            unit_rows = self._by_unit.get(flt.unit, [])
            return [a for a in unit_rows if a.site is flt.site]
        if flt.unit:
            return list(self._by_unit.get(flt.unit, ()))
        if flt.site:
            return list(self._by_site.get(flt.site, ()))
        return self._by_time

    def _restrict_time(self, rows: list[Alarm], flt: AlarmFilter) -> list[Alarm]:
        if flt.start_time is None and flt.end_time is None:
            return rows
        if rows is self._by_time:
            lo = bisect.bisect_left(self._start_times, flt.start_time) if flt.start_time else 0
            hi = bisect.bisect_right(self._start_times, flt.end_time) if flt.end_time else len(rows)
            return rows[lo:hi]
        return [
            a
            for a in rows
            if (flt.start_time is None or a.start_time >= flt.start_time)
            and (flt.end_time is None or a.start_time <= flt.end_time)
        ]

    def _matches(self, alarm: Alarm, flt: AlarmFilter) -> bool:
        # A flat ladder of guard clauses, one per filter field, so adding a filter means
        # adding a clause rather than extending a boolean expression. The severity threshold
        # comes last and returns its own comparison: by then it decides the answer.
        if flt.site and alarm.site is not flt.site:
            return False
        if flt.unit and alarm.unit != flt.unit:
            return False
        if flt.statuses and alarm.status not in flt.statuses:
            return False
        if flt.severities and alarm.severity not in flt.severities:
            return False
        if flt.alarm_types and alarm.alarm_type not in flt.alarm_types:
            return False
        if flt.alarm_names and alarm.alarm_name not in flt.alarm_names:
            return False
        if flt.min_severity:
            return SEVERITY_ORDER[alarm.severity] >= SEVERITY_ORDER[flt.min_severity]
        return True

    def paginate(
        self,
        rows: Sequence[Alarm],
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "start_time",
        sort_order: str = "desc",
    ) -> tuple[list[Alarm], dict[str, Any]]:
        """Sort then slice, returning the page plus a pagination envelope."""
        key = SORT_KEYS.get(sort_by)
        if key is None:
            raise ValueError(
                f"unsupported sort_by {sort_by!r}; expected one of {sorted(SORT_KEYS)}"
            )
        if sort_order not in ("asc", "desc"):
            raise ValueError(f"unsupported sort_order {sort_order!r}; expected 'asc' or 'desc'")
        # Secondary key on alarm_id makes the order total, so pagination can't drop or
        # repeat a row when the primary key ties.
        ordered = sorted(rows, key=lambda a: (key(a), a.alarm_id), reverse=sort_order == "desc")
        total = len(ordered)
        offset = (page - 1) * page_size
        window = ordered[offset : offset + page_size]
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return window, {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_next": offset + page_size < total,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }

    # --- KPIs -------------------------------------------------------------------------

    def compute_kpis(self, rows: Sequence[Alarm], names: Sequence[str]) -> dict[str, Any]:
        """Compute the requested KPIs over one set of alarms.

        Unknown names raise, rather than being silently dropped: a copilot that asks for a
        KPI it invented should get a clear error it can recover from, not a response that
        looks complete but is missing a field.
        """
        unknown = [n for n in names if n not in KPI_FUNCTIONS]
        if unknown:
            raise ValueError(
                f"unknown kpis {unknown}; expected a subset of {sorted(KPI_FUNCTIONS)}"
            )
        return {name: KPI_FUNCTIONS[name](rows) for name in names}

    def summarize(
        self,
        flt: AlarmFilter,
        group_by: Sequence[str] = (),
        kpis: Sequence[str] = ("alarm_count",),
    ) -> dict[str, Any]:
        """Filter, group by an ordered key list, and compute KPIs per group and overall."""
        unknown = [g for g in group_by if g not in GROUP_KEYS]
        if unknown:
            raise ValueError(
                f"unknown group_by {unknown}; expected a subset of {sorted(GROUP_KEYS)}"
            )
        rows = self.query(flt)

        groups: list[dict[str, Any]] = []
        if group_by:
            buckets: dict[tuple[Any, ...], list[Alarm]] = defaultdict(list)
            for alarm in rows:
                buckets[tuple(GROUP_KEYS[g](alarm) for g in group_by)].append(alarm)
            for composite, bucket in buckets.items():
                groups.append(
                    {
                        "key": dict(zip(group_by, composite, strict=True)),
                        "alarm_count": len(bucket),
                        "kpis": self.compute_kpis(bucket, kpis),
                    }
                )
            # Largest group first: that is the answer to "what is alarming most?".
            groups.sort(key=lambda g: (-g["alarm_count"], str(sorted(g["key"].items()))))

        return {
            "filters_applied": flt.describe(),
            "group_by": list(group_by),
            "total_alarms": len(rows),
            "overall": self.compute_kpis(rows, kpis),
            "groups": groups,
        }

    def recurring_alarms(
        self, flt: AlarmFilter, threshold: int = RECURRENCE_THRESHOLD
    ) -> list[dict[str, Any]]:
        """(asset, alarm_name) pairs that repeat at least `threshold` times.

        This is the primitive behind the acceptance scenario's "recurring high-severity
        alarms", so it also reports the **trend**: comparing the recent half of the window
        against the earlier half is what lets the copilot say "and it is getting worse"
        instead of only "it happened 23 times".
        """
        rows = self.query(flt)
        if not rows:
            return []
        window_start = flt.start_time or rows[0].start_time
        window_end = flt.end_time or rows[-1].start_time
        midpoint = window_start + (window_end - window_start) / 2

        buckets: dict[tuple[str, str], list[Alarm]] = defaultdict(list)
        for alarm in rows:
            buckets[(alarm.asset_id, alarm.alarm_name)].append(alarm)

        out: list[dict[str, Any]] = []
        for (asset_id, alarm_name), bucket in buckets.items():
            if len(bucket) < threshold:
                continue
            first_half = [a for a in bucket if a.start_time < midpoint]
            second_half = [a for a in bucket if a.start_time >= midpoint]
            acked = [a.ack_delay_minutes for a in bucket if a.ack_delay_minutes is not None]
            out.append(
                {
                    "asset_id": asset_id,
                    "asset_name": bucket[0].asset_name,
                    "alarm_name": alarm_name,
                    "alarm_type": bucket[0].alarm_type.value,
                    "occurrences": len(bucket),
                    "max_severity": max(
                        bucket, key=lambda a: SEVERITY_ORDER[a.severity]
                    ).severity.value,
                    "first_seen": min(a.start_time for a in bucket),
                    "last_seen": max(a.start_time for a in bucket),
                    "occurrences_first_half": len(first_half),
                    "occurrences_second_half": len(second_half),
                    "trend": _trend_label(len(first_half), len(second_half)),
                    "avg_ack_delay_minutes": round(sum(acked) / len(acked), 1) if acked else None,
                    "chattering_share": round(
                        sum(1 for a in bucket if a.chattering) / len(bucket), 3
                    ),
                }
            )
        out.sort(key=lambda r: (-int(r["occurrences"]), str(r["alarm_name"])))
        return out

    def stale_alarms(
        self, flt: AlarmFilter, now: datetime, older_than_minutes: int = STALE_AFTER_MINUTES
    ) -> list[Alarm]:
        """Active alarms never acknowledged within `older_than_minutes` of starting."""
        cutoff = now - timedelta(minutes=older_than_minutes)
        return [
            a
            for a in self.query(flt)
            if a.status is AlarmStatus.ACTIVE and a.ack_time is None and a.start_time <= cutoff
        ]


def _trend_label(first_half: int, second_half: int) -> str:
    """Coarse direction label. Coarse on purpose — the raw counts travel alongside it."""
    if first_half == 0 and second_half == 0:
        return "flat"
    if first_half == 0:
        return "increasing"
    ratio = second_half / first_half
    if ratio >= 1.3:
        return "increasing"
    if ratio <= 0.77:
        return "decreasing"
    return "flat"


# --- KPI registry ----------------------------------------------------------------------
# Each KPI is a pure function of an alarm list. Registry rather than a chain of ifs so
# `/analytics/kpi-definitions` can enumerate them and stay in sync with what actually runs.


def _alarm_count(rows: Sequence[Alarm]) -> int:
    return len(rows)


def _critical_count(rows: Sequence[Alarm]) -> int:
    return sum(1 for a in rows if a.severity is Severity.CRITICAL)


def _high_or_above_count(rows: Sequence[Alarm]) -> int:
    return sum(1 for a in rows if SEVERITY_ORDER[a.severity] >= SEVERITY_ORDER[Severity.HIGH])


def _avg_ack_delay(rows: Sequence[Alarm]) -> float | None:
    delays = [a.ack_delay_minutes for a in rows if a.ack_delay_minutes is not None]
    return round(sum(delays) / len(delays), 2) if delays else None


def _max_ack_delay(rows: Sequence[Alarm]) -> float | None:
    delays = [a.ack_delay_minutes for a in rows if a.ack_delay_minutes is not None]
    return round(max(delays), 2) if delays else None


def _avg_duration(rows: Sequence[Alarm]) -> float | None:
    durations = [a.duration_minutes for a in rows if a.duration_minutes is not None]
    return round(sum(durations) / len(durations), 2) if durations else None


def _recurring_rate(rows: Sequence[Alarm]) -> float:
    """Share of alarms belonging to an (asset, name) pair seen >= RECURRENCE_THRESHOLD times."""
    if not rows:
        return 0.0
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for alarm in rows:
        counts[(alarm.asset_id, alarm.alarm_name)] += 1
    recurring = sum(c for c in counts.values() if c >= RECURRENCE_THRESHOLD)
    return round(recurring / len(rows), 3)


def _unacknowledged_rate(rows: Sequence[Alarm]) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for a in rows if a.ack_time is None) / len(rows), 3)


def _suppression_candidate_rate(rows: Sequence[Alarm]) -> float:
    """Share that looks like noise: chattering, or self-clearing before an operator could act."""
    if not rows:
        return 0.0
    noisy = sum(
        1
        for a in rows
        if a.chattering
        or (
            a.duration_minutes is not None
            and a.duration_minutes <= NUISANCE_MAX_DURATION_MINUTES
            and a.ack_time is None
        )
    )
    return round(noisy / len(rows), 3)


def _operator_response_efficiency(rows: Sequence[Alarm]) -> float | None:
    """Share of alarms acknowledged within 10 minutes. Higher is better."""
    ackable = [a for a in rows if a.status is not AlarmStatus.SUPPRESSED]
    if not ackable:
        return None
    prompt = sum(
        1 for a in ackable if a.ack_delay_minutes is not None and a.ack_delay_minutes <= 10.0
    )
    return round(prompt / len(ackable), 3)


KPI_FUNCTIONS: dict[str, Callable[[Sequence[Alarm]], Any]] = {
    "alarm_count": _alarm_count,
    "critical_count": _critical_count,
    "high_or_above_count": _high_or_above_count,
    "avg_ack_delay": _avg_ack_delay,
    "max_ack_delay": _max_ack_delay,
    "avg_duration": _avg_duration,
    "recurring_rate": _recurring_rate,
    "unacknowledged_rate": _unacknowledged_rate,
    "suppression_candidate_rate": _suppression_candidate_rate,
    "operator_response_efficiency": _operator_response_efficiency,
}

KPI_DEFINITIONS: dict[str, dict[str, str]] = {
    "alarm_count": {
        "unit": "count",
        "description": "Number of alarms matching the filter.",
    },
    "critical_count": {
        "unit": "count",
        "description": "Alarms with severity 'critical'.",
    },
    "high_or_above_count": {
        "unit": "count",
        "description": "Alarms with severity 'high' or 'critical'.",
    },
    "avg_ack_delay": {
        "unit": "minutes",
        "description": (
            "Mean minutes from alarm start to acknowledgement, over acknowledged alarms "
            "only. Null when nothing in the set was acknowledged."
        ),
    },
    "max_ack_delay": {
        "unit": "minutes",
        "description": "Longest acknowledgement delay in the set.",
    },
    "avg_duration": {
        "unit": "minutes",
        "description": "Mean minutes from start to clear, over cleared alarms only.",
    },
    "recurring_rate": {
        "unit": "ratio",
        "description": (
            f"Share of alarms whose (asset, alarm_name) pair occurs at least "
            f"{RECURRENCE_THRESHOLD} times in the filtered window."
        ),
    },
    "unacknowledged_rate": {
        "unit": "ratio",
        "description": "Share of alarms that were never acknowledged.",
    },
    "suppression_candidate_rate": {
        "unit": "ratio",
        "description": (
            "Share of alarms flagged as chattering, or that self-cleared within "
            f"{NUISANCE_MAX_DURATION_MINUTES} minute(s) without acknowledgement. A "
            "rationalization signal, not an instruction to suppress."
        ),
    },
    "operator_response_efficiency": {
        "unit": "ratio",
        "description": (
            "Share of acknowledgeable alarms acknowledged within 10 minutes. Higher is better."
        ),
    },
}
