"""Alarm history: the events, the aggregate, and the patterns.

Three tools rather than one with a mode flag. They answer different questions and a planner
picks between them by name, so collapsing them would mean the choice moved from the tool
catalogue (where the schema documents it) into a parameter the model has to reason about.

* `get_alarms` — the individual events. Needed for evidence and for the `alarm_id` that
  `get_operator_recommendations` takes.
* `get_alarm_summary` — counts and KPIs, grouped. Needed because "over the last 90 days"
  routinely covers more alarms than fit in a context window.
* `get_recurring_alarms` — signatures that repeat, and whether they are getting worse. This
  is the one the acceptance scenario turns on: "recurring" and "increasing" are the finding,
  not a filter.

All three take a window as `lookback_days` with optional explicit endpoints, so the common
phrasing needs no date arithmetic from the model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from alarm_management.context import tool_session
from alarm_management.schemas import (
    AlarmListResult,
    AlarmRecord,
    AlarmSummaryResult,
    RecurringAlarmsResult,
    RecurringPattern,
    SummaryGroup,
)

SEVERITIES = "low | medium | high | critical"
STATUSES = "active | acknowledged | cleared | suppressed"

#: What `get_alarm_summary` computes when the caller names no KPIs.
#:
#: Not `["alarm_count"]`, which is what a minimal default would be. Two reasons. A model asking
#: "how bad is this" almost always wants severity and response quality alongside the count, and
#: leaving it to name them means a second round trip when it realises — the alarm API computes all
#: ten from one already-loaded set of rows, so the extra nine are free. And the GUI's KPI header is
#: assembled from whatever this returns, so a single-count default would make the board depend on
#: the model's choice of arguments: the same question could produce one tile or eight. A caller
#: that wants a narrower set still passes `kpis` explicitly.
#:
#: The two not included here — max_ack_delay and avg_duration — are available by name; they
#: answer a follow-up rather than the first question.
DEFAULT_KPIS: tuple[str, ...] = (
    "alarm_count",
    "critical_count",
    "high_or_above_count",
    "avg_ack_delay",
    "recurring_rate",
    "unacknowledged_rate",
    "suppression_candidate_rate",
    "operator_response_efficiency",
)

GET_ALARMS_DESCRIPTION = """\
List individual alarm events for an asset, unit or site over a time window, newest first.

Use this for the evidence behind a finding and to obtain an `alarm_id` for
get_operator_recommendations. For a question about volumes or trends over a long window,
prefer get_alarm_summary or get_recurring_alarms — a 90-day feed can run to thousands of
events and only one page is returned.
"""

SUMMARY_DESCRIPTION = """\
Aggregate alarm counts and KPIs over a window, optionally grouped by dimensions such as
asset, alarm name, severity or unit.

Use this to size a problem before reading events: it answers "how many, of what, where" in
one call. `group_by` accepts alarm_name, asset_id, asset_name, severity, status, alarm_type,
unit, site and day; `kpis` accepts alarm_count, avg_ack_delay, max_ack_delay, avg_duration,
critical_count, high_or_above_count, unacknowledged_rate, recurring_rate,
suppression_candidate_rate and operator_response_efficiency. Omitting `kpis` returns a default
set covering volume, severity, recurrence and operator response, which is what a first look at
an asset needs.
"""

RECURRING_DESCRIPTION = """\
Find alarm signatures that repeated on an asset over a window, with how often each occurred
and whether the rate is increasing, flat or decreasing.

This is the tool for "recurring alarms" questions. The `trend` and the first/second-half
occurrence split are the evidence that a problem is developing rather than steady, and
`chattering_share` distinguishes a genuine process fault from a badly tuned deadband.
"""


def register(server: MCPServer) -> None:
    @server.tool(name="get_alarms", title="List alarm events", description=GET_ALARMS_DESCRIPTION)
    async def get_alarms(
        ctx: Context,
        asset_id: Annotated[
            str | None,
            Field(description="Resolve names to ids with search_assets first"),
        ] = None,
        unit: Annotated[str | None, Field(description="e.g. 'Unit 2'")] = None,
        site: Annotated[str | None, Field(description="e.g. 'EastRefinery'")] = None,
        severity: Annotated[
            list[str] | None, Field(description=f"Keep only these severities: {SEVERITIES}")
        ] = None,
        status: Annotated[
            list[str] | None, Field(description=f"Keep only these statuses: {STATUSES}")
        ] = None,
        alarm_type: Annotated[
            list[str] | None,
            Field(description="Keep only these types: process | equipment | safety | system"),
        ] = None,
        lookback_days: Annotated[
            int, Field(ge=1, le=400, description="Window length ending now")
        ] = 90,
        start_time: Annotated[
            datetime | None, Field(description="Explicit window start; overrides lookback_days")
        ] = None,
        end_time: Annotated[datetime | None, Field(description="Explicit window end")] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Events per page")] = 25,
        page: Annotated[int, Field(ge=1, description="1-based; use when has_more is true")] = 1,
    ) -> AlarmListResult:
        async with tool_session(ctx, "get_alarms") as session:
            start, end = session.window(
                lookback_days=lookback_days, start_time=start_time, end_time=end_time
            )
            page_result = await session.client.get_alarms(
                asset_ids=[asset_id] if asset_id else None,
                unit=unit,
                site=site,
                severity=severity,
                status=status,
                alarm_type=alarm_type,
                start_time=start,
                end_time=end,
                page=page,
                page_size=session.rows(limit),
                trace=session.trace,
            )
            return AlarmListResult(
                total_matching=page_result.pagination.total,
                returned=len(page_result.data),
                page=page_result.pagination.page,
                has_more=page_result.pagination.has_next,
                window_start=start,
                window_end=end,
                filters_applied=page_result.filters_applied,
                alarms=[AlarmRecord.from_wire(alarm) for alarm in page_result.data],
                upstream=session.upstream(),
                trace_id=session.trace_id,
            )

    @server.tool(
        name="get_alarm_summary", title="Summarise alarms", description=SUMMARY_DESCRIPTION
    )
    async def get_alarm_summary(
        ctx: Context,
        asset_id: Annotated[str | None, Field(description="Restrict to one asset")] = None,
        unit: Annotated[str | None, Field(description="Restrict to one unit")] = None,
        site: Annotated[str | None, Field(description="Restrict to one site")] = None,
        severity: Annotated[
            list[str] | None, Field(description=f"Keep only these severities: {SEVERITIES}")
        ] = None,
        lookback_days: Annotated[
            int, Field(ge=1, le=400, description="Window length ending now")
        ] = 90,
        start_time: Annotated[datetime | None, Field(description="Overrides lookback_days")] = None,
        end_time: Annotated[datetime | None, Field(description="Explicit window end")] = None,
        group_by: Annotated[
            list[str] | None,
            Field(description="Dimensions to group by; omit for a single overall figure"),
        ] = None,
        kpis: Annotated[
            list[str] | None,
            Field(description=f"KPIs to compute; omit for the default set {DEFAULT_KPIS}"),
        ] = None,
    ) -> AlarmSummaryResult:
        async with tool_session(ctx, "get_alarm_summary") as session:
            start, end = session.window(
                lookback_days=lookback_days, start_time=start_time, end_time=end_time
            )
            body: dict[str, Any] = {
                "time_range": {"start": start.isoformat(), "end": end.isoformat()},
                "group_by": group_by or [],
                "kpis": kpis or list(DEFAULT_KPIS),
            }
            _narrow(body, asset_id=asset_id, unit=unit, site=site, severity=severity)

            summary = await session.client.summarize_alarms(body, trace=session.trace)
            return AlarmSummaryResult(
                total_alarms=summary.total_alarms,
                window_start=start,
                window_end=end,
                group_by=summary.group_by,
                overall=summary.overall,
                groups=[
                    SummaryGroup(key=group.key, alarm_count=group.alarm_count, kpis=group.kpis)
                    for group in summary.groups
                ],
                filters_applied=summary.filters_applied,
                upstream=session.upstream(),
                trace_id=session.trace_id,
            )

    @server.tool(
        name="get_recurring_alarms",
        title="Find recurring alarm patterns",
        description=RECURRING_DESCRIPTION,
    )
    async def get_recurring_alarms(
        ctx: Context,
        asset_id: Annotated[str | None, Field(description="Restrict to one asset")] = None,
        unit: Annotated[str | None, Field(description="Restrict to one unit")] = None,
        site: Annotated[str | None, Field(description="Restrict to one site")] = None,
        min_severity: Annotated[
            str | None,
            Field(description=f"Keep occurrences at this severity or above: {SEVERITIES}"),
        ] = None,
        lookback_days: Annotated[
            int, Field(ge=1, le=400, description="Window length ending now")
        ] = 90,
        start_time: Annotated[datetime | None, Field(description="Overrides lookback_days")] = None,
        end_time: Annotated[datetime | None, Field(description="Explicit window end")] = None,
        min_occurrences: Annotated[
            int, Field(ge=2, le=1000, description="How many repeats make a pattern")
        ] = 6,
        limit: Annotated[int, Field(ge=1, le=50, description="Patterns to return")] = 10,
    ) -> RecurringAlarmsResult:
        async with tool_session(ctx, "get_recurring_alarms") as session:
            start, end = session.window(
                lookback_days=lookback_days, start_time=start_time, end_time=end_time
            )
            body: dict[str, Any] = {
                "time_range": {"start": start.isoformat(), "end": end.isoformat()},
                "recurrence_threshold": min_occurrences,
                "limit": limit,
            }
            if asset_id:
                body["asset_ids"] = [asset_id]
            if unit:
                body["unit"] = unit
            if site:
                body["site"] = site
            if min_severity:
                body["severity_threshold"] = min_severity

            recurring = await session.client.recurring_alarms(body, trace=session.trace)
            return RecurringAlarmsResult(
                window_start=start,
                window_end=end,
                recurrence_threshold=recurring.recurrence_threshold,
                total_patterns=recurring.total_groups,
                patterns=[RecurringPattern.from_wire(group) for group in recurring.groups],
                filters_applied=recurring.filters_applied,
                upstream=session.upstream(),
                trace_id=session.trace_id,
            )


def _narrow(
    body: dict[str, Any],
    *,
    asset_id: str | None,
    unit: str | None,
    site: str | None,
    severity: list[str] | None,
) -> None:
    """Add only the scope filters that were actually supplied.

    Sending `null` would be rejected as an invalid enum value, and sending nothing at all is
    a legitimate request for a plant-wide figure — so absent and empty must stay distinct.
    """
    if asset_id:
        body["asset_ids"] = [asset_id]
    if unit:
        body["unit"] = unit
    if site:
        body["site"] = site
    if severity:
        body["severity"] = severity
