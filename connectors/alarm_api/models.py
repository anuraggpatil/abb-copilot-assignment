"""Typed views of the Alarm Management API's wire format.

These are **re-declared**, not imported from `apps.alarm_api`. Sharing model classes with
the server would make the connector's tests pass by construction and would mean a change to
the server's internals silently redefines what its clients accept — the exact coupling a
connector layer exists to prevent. Against a real plant historian there would be no shared
types to import, and this code should not be shaped by the simulator happening to be local.

Two deliberate leniencies:

* `extra="ignore"` — a field added server-side must not break a running client. Ignoring
  unknown fields is what makes the API and the copilot independently deployable.
* Enums are typed as `str`. The connector's job is transport and error semantics, not
  policing vocabulary: a new severity level should reach the copilot as data, not as a
  validation failure in the middle of an investigation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WireModel(BaseModel):
    """Base for everything parsed off the wire."""

    model_config = ConfigDict(extra="ignore", frozen=True)


# --- health -----------------------------------------------------------------------------


class Health(WireModel):
    status: str
    service: str = ""
    version: str = ""
    dataset: dict[str, Any] = Field(default_factory=dict)


# --- assets -----------------------------------------------------------------------------


class AssetSummary(WireModel):
    asset_id: str
    name: str
    asset_type: str
    site: str
    unit: str
    tag: str
    criticality: int
    match_score: float = 0.0


class AssetSearchResult(WireModel):
    results: list[AssetSummary]
    query: str = ""
    total_matches: int = 0


class RelatedAsset(WireModel):
    asset_id: str
    name: str
    asset_type: str
    relationship: str


class Asset(WireModel):
    asset_id: str
    name: str
    asset_type: str
    site: str
    unit: str
    tag: str
    criticality: int
    manufacturer: str | None = None
    model: str | None = None
    commissioned_on: datetime | None = None
    maintenance_strategy: str | None = None
    parent_asset_id: str | None = None
    related_asset_ids: list[str] = Field(default_factory=list)


class AssetMetadata(WireModel):
    asset: Asset
    related_assets: list[RelatedAsset] = Field(default_factory=list)
    active_alarm_count: int = 0
    open_alarm_count: int = 0
    last_alarm_at: datetime | None = None


# --- alarms -----------------------------------------------------------------------------


class Alarm(WireModel):
    alarm_id: str
    alarm_name: str
    alarm_type: str
    asset_id: str
    asset_name: str
    site: str
    unit: str
    severity: str
    status: str
    start_time: datetime
    ack_time: datetime | None = None
    clear_time: datetime | None = None
    ack_delay_minutes: float | None = None
    duration_minutes: float | None = None
    value: float | None = None
    setpoint: float | None = None
    unit_of_measure: str | None = None
    priority_hint: int | None = None
    chattering: bool = False
    description: str | None = None


class Pagination(WireModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    sort_by: str = ""
    sort_order: str = ""


class AlarmPage(WireModel):
    data: list[Alarm]
    pagination: Pagination
    filters_applied: dict[str, Any] = Field(default_factory=dict)


class TraceEcho(WireModel):
    trace_id: str | None = None
    client_id: str | None = None
    metadata_tag: str | None = None


class SummaryGroup(WireModel):
    key: dict[str, Any]
    alarm_count: int
    kpis: dict[str, Any] = Field(default_factory=dict)


class AlarmSummary(WireModel):
    filters_applied: dict[str, Any] = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=list)
    total_alarms: int
    overall: dict[str, Any] = Field(default_factory=dict)
    groups: list[SummaryGroup] = Field(default_factory=list)
    trace: TraceEcho = Field(default_factory=TraceEcho)


class RecurringAlarmGroup(WireModel):
    asset_id: str
    asset_name: str
    alarm_name: str
    alarm_type: str
    occurrences: int
    max_severity: str
    first_seen: datetime
    last_seen: datetime
    occurrences_first_half: int = 0
    occurrences_second_half: int = 0
    trend: str = ""
    avg_ack_delay_minutes: float | None = None
    chattering_share: float = 0.0


class RecurringAlarms(WireModel):
    filters_applied: dict[str, Any] = Field(default_factory=dict)
    recurrence_threshold: int
    total_groups: int
    groups: list[RecurringAlarmGroup] = Field(default_factory=list)
    trace: TraceEcho = Field(default_factory=TraceEcho)


# --- recommendations --------------------------------------------------------------------


class Recommendation(WireModel):
    rank: int
    action: str
    rationale: str
    expected_outcome: str
    urgency: str
    procedure_reference: str
    requires_isolation: bool = False


class Escalation(WireModel):
    required: bool
    reason: str
    escalate_to: str | None = None
    asset_criticality: int | None = None


class OperatorActions(WireModel):
    alarm_id: str
    alarm_name: str
    asset_id: str
    asset_name: str
    site: str
    unit: str
    severity: str
    status: str
    context: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[Recommendation] = Field(default_factory=list)
    escalation: Escalation
    safety_notes: list[str] = Field(default_factory=list)
    # The document/section pointers the RAG step resolves. This field is the seam between
    # the alarm API and the procedure corpus.
    procedure_references: list[str] = Field(default_factory=list)
    generated_at: datetime | None = None
    disclaimer: str = ""
    trace: TraceEcho = Field(default_factory=TraceEcho)


# --- analytics --------------------------------------------------------------------------


class KpiDefinition(WireModel):
    name: str
    unit: str
    description: str


class KpiCatalogue(WireModel):
    kpis: list[KpiDefinition] = Field(default_factory=list)
    group_by_dimensions: list[str] = Field(default_factory=list)
    sort_fields: list[str] = Field(default_factory=list)
