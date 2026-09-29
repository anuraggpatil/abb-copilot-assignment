"""HTTP request and response contract for the simulator.

What lives here versus in `domain.py`: this module owns the **envelopes** — wrapper keys,
pagination, request bodies, validation rules — while the row payloads reuse the domain
`Alarm` and `Asset` models directly. Re-declaring 18 identical alarm fields would add a
mapping layer with nothing to map; the separation that actually earns its keep is the one
between *envelope* and *entity*, because the envelopes are what the Postman collections
pin and what the MCP tool schemas deliberately diverge from.

Four wrapper keys are fixed by the collections' test assertions and must not be renamed:
`results` on asset search, `data` on the alarm list, `flood_windows` on flood analysis,
and `calculation_id` on code generation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from apps.alarm_api.domain import (
    Alarm,
    AlarmStatus,
    AlarmType,
    Asset,
    AssetType,
    Severity,
    Site,
)

# --- shared -----------------------------------------------------------------------------


class TimeRange(BaseModel):
    """Inclusive window, accepted under either spelling.

    **`start_time`/`end_time` is what the Postman collections actually send** — every one of
    their `time_range` blocks uses it. This model originally declared `start`/`end` only, with a
    docstring asserting that was the published shape; it was not, and the consequence was a 422
    on the collection's own summary request. Found by `tests/integration/test_postman_contract.py`
    replaying the collection file rather than a transcription of it.

    `start`/`end` is kept as an accepted alias because the MCP server's tools send that spelling
    and because dropping it would be a breaking change for no gain. The Python attribute stays
    `start`/`end` so call sites read as `body.time_range.start`.
    """

    model_config = ConfigDict(populate_by_name=True)

    start: datetime = Field(validation_alias=AliasChoices("start_time", "start"))
    end: datetime = Field(validation_alias=AliasChoices("end_time", "end"))

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start > self.end:
            raise ValueError("time_range.start must be earlier than or equal to time_range.end")
        return self


class TraceEcho(BaseModel):
    """The trace identifiers echoed on every response that received them.

    Present on responses rather than only in headers because the copilot's trace panel
    correlates by body content when a response is replayed from a log.
    """

    trace_id: str | None = None
    client_id: str | None = None
    metadata_tag: str | None = None


class ErrorDetail(BaseModel):
    """Uniform error body. The connector maps this into typed domain exceptions."""

    error: str = Field(description="Stable machine-readable code, e.g. asset_not_found")
    message: str = Field(description="Human-readable explanation, safe to show a user")
    detail: Any | None = None
    trace_id: str | None = None


# --- health -----------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "alarm-management-api-simulator"
    version: str
    # Dataset fingerprint: makes it obvious from one unauthenticated call whether the
    # simulator holds the data a failing test expected.
    dataset: dict[str, Any]


# --- assets -----------------------------------------------------------------------------


class AssetSummary(BaseModel):
    """Light projection for search hits — a search may return many rows."""

    asset_id: str
    name: str
    asset_type: AssetType
    site: Site
    unit: str
    tag: str
    criticality: int
    match_score: float = Field(description="Relative relevance to the query; higher is better")

    @classmethod
    def from_asset(cls, asset: Asset, match_score: float) -> AssetSummary:
        return cls(
            asset_id=asset.asset_id,
            name=asset.name,
            asset_type=asset.asset_type,
            site=asset.site,
            unit=asset.unit,
            tag=asset.tag,
            criticality=asset.criticality,
            match_score=match_score,
        )


class AssetSearchResponse(BaseModel):
    # `results` is pinned by the Postman assertions.
    results: list[AssetSummary]
    query: str
    total_matches: int = Field(description="Matches before the limit was applied")


class RelatedAsset(BaseModel):
    asset_id: str
    name: str
    asset_type: AssetType
    relationship: str = Field(description="`parent` or `related`")


class AssetMetadataResponse(BaseModel):
    """Full asset record plus the live context the copilot needs for its next step."""

    asset: Asset
    related_assets: list[RelatedAsset]
    active_alarm_count: int
    open_alarm_count: int = Field(description="Active plus acknowledged but not cleared")
    last_alarm_at: datetime | None = None


# --- alarms -----------------------------------------------------------------------------


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    sort_by: str
    sort_order: str


class AlarmListResponse(BaseModel):
    # `data` is pinned by the Postman assertions.
    data: list[Alarm]
    pagination: Pagination
    filters_applied: dict[str, Any]


class AlarmDetailResponse(BaseModel):
    alarm: Alarm
    asset: Asset
    # Same alarm on the same asset, nearby in time: the cheapest signal that this is a
    # recurrence rather than a one-off.
    recent_occurrences: int
    similar_alarm_ids: list[str]


class SummaryRequest(BaseModel):
    """Body for `POST /alarms/summary`.

    Every scope field is optional, and `time_range` defaults to the full generated
    horizon. That is deliberate: the collections vary which scope they send, and rejecting
    an under-specified body with a 422 would fail contract conformance for no benefit. An
    unscoped request is a legitimate plant-wide question.
    """

    asset_ids: list[str] | None = None
    unit: str | None = None
    site: Site | None = None
    time_range: TimeRange | None = None
    severity: list[Severity] | None = None
    alarm_types: list[AlarmType] | None = None
    status: list[AlarmStatus] | None = None
    # Ordered: `["asset_id", "severity"]` and `["severity", "asset_id"]` produce the same
    # buckets but different key ordering, and callers rely on the order they asked for.
    group_by: list[str] = Field(default_factory=list)
    kpis: list[str] = Field(default_factory=lambda: ["alarm_count"])


class SummaryGroup(BaseModel):
    key: dict[str, Any]
    alarm_count: int
    kpis: dict[str, Any]


class SummaryResponse(BaseModel):
    filters_applied: dict[str, Any]
    group_by: list[str]
    total_alarms: int
    overall: dict[str, Any]
    groups: list[SummaryGroup]
    trace: TraceEcho = Field(default_factory=TraceEcho)


class RecurringAlarmsRequest(BaseModel):
    """Body for `POST /alarms/recurring`.

    Not in the Postman collections — added because "investigate *recurring* alarms" is the
    acceptance scenario's actual question, and making the copilot derive recurrence from a
    raw alarm list would put analytics in the prompt where it cannot be tested.
    """

    asset_ids: list[str] | None = None
    unit: str | None = None
    site: Site | None = None
    time_range: TimeRange | None = None
    severity_threshold: Severity | None = None
    alarm_types: list[AlarmType] | None = None
    recurrence_threshold: Annotated[int, Field(ge=2, le=1000)] = 6
    limit: Annotated[int, Field(ge=1, le=200)] = 20


class RecurringAlarmGroup(BaseModel):
    asset_id: str
    asset_name: str
    alarm_name: str
    alarm_type: str
    occurrences: int
    max_severity: str
    first_seen: datetime
    last_seen: datetime
    occurrences_first_half: int
    occurrences_second_half: int
    trend: str = Field(description="increasing | flat | decreasing, halves of the window compared")
    avg_ack_delay_minutes: float | None
    chattering_share: float


class RecurringAlarmsResponse(BaseModel):
    filters_applied: dict[str, Any]
    recurrence_threshold: int
    total_groups: int
    groups: list[RecurringAlarmGroup]
    trace: TraceEcho = Field(default_factory=TraceEcho)


# --- recommendations --------------------------------------------------------------------


class OperatorActionsRequest(BaseModel):
    """Body for `POST /recommendations/operator-actions`.

    Accepts either an `alarm_id` or an `asset_id`; with only an asset the engine selects
    that asset's most pressing open alarm. Supporting both is what lets the copilot ask for
    recommendations straight after asset resolution, without a mandatory alarm-list hop.
    """

    alarm_id: str | None = None
    asset_id: str | None = None
    lookback_days: Annotated[int, Field(ge=1, le=400)] = 90
    max_recommendations: Annotated[int, Field(ge=1, le=20)] = 6

    @model_validator(mode="after")
    def _needs_a_subject(self) -> Self:
        if not self.alarm_id and not self.asset_id:
            raise ValueError("one of alarm_id or asset_id is required")
        return self


class Recommendation(BaseModel):
    rank: int
    action: str
    rationale: str
    expected_outcome: str
    urgency: str = Field(description="immediate | this_shift | planned | monitor")
    procedure_reference: str = Field(
        description=(
            "Document and section that governs this action. A pointer, not evidence — the "
            "procedure text must be retrieved separately."
        )
    )
    requires_isolation: bool


class Escalation(BaseModel):
    required: bool
    reason: str
    escalate_to: str | None = None
    asset_criticality: int | None = None


class OperatorActionsResponse(BaseModel):
    alarm_id: str
    alarm_name: str
    asset_id: str
    asset_name: str
    site: str
    unit: str
    severity: str
    status: str
    context: dict[str, Any]
    recommendations: list[Recommendation]
    escalation: Escalation
    safety_notes: list[str]
    procedure_references: list[str]
    generated_at: datetime
    disclaimer: str
    trace: TraceEcho = Field(default_factory=TraceEcho)


# --- analytics --------------------------------------------------------------------------


class KpiDefinition(BaseModel):
    name: str
    unit: str
    description: str


class KpiDefinitionsResponse(BaseModel):
    kpis: list[KpiDefinition]
    group_by_dimensions: list[str]
    sort_fields: list[str]
