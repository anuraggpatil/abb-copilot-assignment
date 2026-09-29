"""Tool result schemas.

These are the MCP server's public contract. Two things make them different from the
connector's wire models, which they are built from:

* **They are written for a reader with a context window.** Every field costs tokens in the
  model's prompt and every field is also something the model may hallucinate a use for, so
  the alarm API's fuller records are narrowed to what an investigation actually needs.
  `Field(description=...)` is not decoration here: the descriptions become the tool's
  JSON Schema, which is the only documentation the planner ever sees.
* **They are stable.** The connector absorbs upstream drift; this layer is what the copilot
  and its tests are written against.

Every result carries `upstream`, a compact record of the HTTP calls the tool made. It is
diagnostics rather than plant data, and it is in the result body on purpose: the copilot's
trace view has to show status codes and retry counts, and the backend reaches this server
over MCP, so anything it cannot see in a response it cannot display.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from connectors.alarm_api import CallRecord
from connectors.alarm_api.models import (
    Alarm,
)
from connectors.alarm_api.models import (
    AssetSummary as WireAssetSummary,
)
from connectors.alarm_api.models import (
    Recommendation as WireRecommendation,
)
from connectors.alarm_api.models import (
    RecurringAlarmGroup as WireRecurringGroup,
)


class ToolModel(BaseModel):
    """Strict by default: an unexpected field in a tool *result* is a bug in this server."""

    model_config = ConfigDict(extra="forbid")


class UpstreamCall(ToolModel):
    """One logical HTTP call to the alarm API, with its retries folded in."""

    method: str
    path: str
    status_code: int | None = Field(default=None, description="Absent if the call never landed")
    attempts: int = Field(description="1 when the first attempt succeeded; higher means retries")
    duration_ms: float
    error: str | None = None


class ToolResult(ToolModel):
    """Base for every tool result."""

    upstream: list[UpstreamCall] = Field(
        default_factory=list,
        description=(
            "Diagnostics for the copilot's execution trace: the alarm API calls this tool "
            "made. Not part of the answer — do not quote it to the operator."
        ),
    )
    trace_id: str | None = Field(
        default=None, description="Correlates this tool call with the alarm API's own logs"
    )


def upstream_from(records: list[CallRecord]) -> list[UpstreamCall]:
    """Fold per-attempt records into one entry per logical call.

    The connector reports every attempt; a trace wants "one call, three attempts, ended 200"
    rather than three rows that a reader has to re-assemble.
    """
    calls: list[UpstreamCall] = []
    for record in records:
        if calls and calls[-1].method == record.method and calls[-1].path == record.path:
            previous = calls[-1]
            calls[-1] = UpstreamCall(
                method=previous.method,
                path=previous.path,
                status_code=record.status_code,
                attempts=previous.attempts + 1,
                duration_ms=round(previous.duration_ms + record.duration_ms, 2),
                error=record.error,
            )
            continue
        calls.append(
            UpstreamCall(
                method=record.method,
                path=record.path,
                status_code=record.status_code,
                attempts=1,
                duration_ms=record.duration_ms,
                error=record.error,
            )
        )
    return calls


# --- search_assets ----------------------------------------------------------------------


class AssetMatch(ToolModel):
    asset_id: str = Field(description="Pass this to the other alarm tools")
    name: str
    asset_type: str
    tag: str = Field(description="Plant tag, e.g. 2-BFP-101")
    site: str
    unit: str
    criticality: int = Field(description="1 (low) to 5 (safety-critical); drives escalation")
    match_score: float = Field(description="Relevance to the query; higher is better")

    @classmethod
    def from_wire(cls, asset: WireAssetSummary) -> AssetMatch:
        return cls(
            asset_id=asset.asset_id,
            name=asset.name,
            asset_type=asset.asset_type,
            tag=asset.tag,
            site=asset.site,
            unit=asset.unit,
            criticality=asset.criticality,
            match_score=round(asset.match_score, 3),
        )


class RelatedAsset(ToolModel):
    asset_id: str
    name: str
    relationship: str = Field(description="How it is connected, e.g. parent or related")


class AssetSearchResult(ToolResult):
    """Assets matching a name, tag or free-text description."""

    query: str
    total_matches: int = Field(description="Matches before `limit` was applied")
    assets: list[AssetMatch]
    # Metadata for the single best match, so resolving a name does not cost two round trips
    # on the overwhelmingly common "one asset, then look at it" path.
    best_match_tag: str | None = None
    best_match_criticality: int | None = None
    best_match_related_assets: list[RelatedAsset] = Field(
        default_factory=list,
        description="Upstream/downstream equipment worth checking for a shared cause",
    )
    best_match_active_alarms: int | None = None


# --- get_alarms -------------------------------------------------------------------------


class AlarmRecord(ToolModel):
    alarm_id: str = Field(description="Pass this to get_operator_recommendations")
    alarm_name: str
    alarm_type: str
    severity: str
    status: str
    start_time: datetime
    asset_id: str
    asset_name: str
    unit: str
    ack_delay_minutes: float | None = Field(
        default=None, description="Minutes from raise to acknowledgement; null if never acked"
    )
    duration_minutes: float | None = None
    value: float | None = Field(default=None, description="Measured value when the alarm raised")
    setpoint: float | None = None
    unit_of_measure: str | None = None
    chattering: bool = Field(
        default=False, description="Raised and cleared repeatedly in a short window"
    )

    @classmethod
    def from_wire(cls, alarm: Alarm) -> AlarmRecord:
        return cls(
            alarm_id=alarm.alarm_id,
            alarm_name=alarm.alarm_name,
            alarm_type=alarm.alarm_type,
            severity=alarm.severity,
            status=alarm.status,
            start_time=alarm.start_time,
            asset_id=alarm.asset_id,
            asset_name=alarm.asset_name,
            unit=alarm.unit,
            ack_delay_minutes=alarm.ack_delay_minutes,
            duration_minutes=alarm.duration_minutes,
            value=alarm.value,
            setpoint=alarm.setpoint,
            unit_of_measure=alarm.unit_of_measure,
            chattering=alarm.chattering,
        )


class AlarmListResult(ToolResult):
    """A page of individual alarm events."""

    total_matching: int = Field(description="Total alarms matching the filters, not just this page")
    returned: int
    page: int
    has_more: bool = Field(description="If true, request the next page for the rest")
    window_start: datetime | None = None
    window_end: datetime | None = None
    filters_applied: dict[str, Any] = Field(
        description="What the API actually filtered on — check this before concluding anything"
    )
    alarms: list[AlarmRecord]


# --- get_alarm_summary ------------------------------------------------------------------


class SummaryGroup(ToolModel):
    key: dict[str, Any] = Field(
        description="The grouping dimension values, e.g. {'unit': 'Unit 2'}"
    )
    alarm_count: int
    kpis: dict[str, Any]


class AlarmSummaryResult(ToolResult):
    """Aggregated alarm counts and KPIs over a window, grouped as requested."""

    total_alarms: int
    window_start: datetime
    window_end: datetime
    group_by: list[str]
    overall: dict[str, Any] = Field(description="The same KPIs computed across every group")
    groups: list[SummaryGroup]
    filters_applied: dict[str, Any]


# --- get_recurring_alarms ---------------------------------------------------------------


class RecurringPattern(ToolModel):
    alarm_name: str
    alarm_type: str
    asset_id: str
    asset_name: str
    occurrences: int
    max_severity: str
    first_seen: datetime
    last_seen: datetime
    trend: str = Field(description="increasing | flat | decreasing, comparing halves of the window")
    occurrences_first_half: int
    occurrences_second_half: int
    avg_ack_delay_minutes: float | None = None
    chattering_share: float = Field(
        description="Fraction of occurrences that were chattering; high values suggest a "
        "badly configured deadband rather than a real process problem"
    )

    @classmethod
    def from_wire(cls, group: WireRecurringGroup) -> RecurringPattern:
        return cls(
            alarm_name=group.alarm_name,
            alarm_type=group.alarm_type,
            asset_id=group.asset_id,
            asset_name=group.asset_name,
            occurrences=group.occurrences,
            max_severity=group.max_severity,
            first_seen=group.first_seen,
            last_seen=group.last_seen,
            trend=group.trend,
            occurrences_first_half=group.occurrences_first_half,
            occurrences_second_half=group.occurrences_second_half,
            avg_ack_delay_minutes=group.avg_ack_delay_minutes,
            chattering_share=round(group.chattering_share, 3),
        )


class RecurringAlarmsResult(ToolResult):
    """Alarm signatures that repeated over the window, with their direction of travel."""

    window_start: datetime
    window_end: datetime
    recurrence_threshold: int = Field(description="Minimum occurrences for a pattern to be listed")
    total_patterns: int
    patterns: list[RecurringPattern]
    filters_applied: dict[str, Any]


# --- get_operator_recommendations -------------------------------------------------------


class OperatorAction(ToolModel):
    rank: int = Field(description="1 is the first thing to do")
    action: str
    rationale: str = Field(description="Why this follows from the alarm history")
    expected_outcome: str
    urgency: str = Field(description="immediate | this_shift | planned | monitor")
    procedure_reference: str = Field(
        description=(
            "Document and section this action comes from, e.g. 'OP-BFP-101 §4.2 Low suction "
            "pressure response'. Use search_procedures to retrieve the text before quoting it."
        )
    )
    requires_isolation: bool = Field(
        description="If true, the plant must be isolated and locked out first"
    )

    @classmethod
    def from_wire(cls, rec: WireRecommendation) -> OperatorAction:
        return cls(
            rank=rec.rank,
            action=rec.action,
            rationale=rec.rationale,
            expected_outcome=rec.expected_outcome,
            urgency=rec.urgency,
            procedure_reference=rec.procedure_reference,
            requires_isolation=rec.requires_isolation,
        )


class Escalation(ToolModel):
    required: bool
    reason: str
    escalate_to: str | None = None
    asset_criticality: int | None = None


class OperatorRecommendationsResult(ToolResult):
    """Ranked actions for an alarm, from the alarm system's own rule engine."""

    alarm_id: str
    alarm_name: str
    asset_id: str
    asset_name: str
    severity: str
    status: str
    unit: str
    context: dict[str, Any] = Field(
        description="The alarm history the ranking was derived from (recurrence, neighbours)"
    )
    actions: list[OperatorAction]
    escalation: Escalation
    safety_notes: list[str]
    procedure_references: list[str] = Field(
        description=(
            "Every document section cited above, de-duplicated. Feed these to "
            "search_procedures to ground the recommendations in the written procedure."
        )
    )
    disclaimer: str = Field(
        description="Advisory scope of this output; surface it alongside the actions"
    )
