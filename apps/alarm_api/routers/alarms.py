"""Alarm listing and aggregation endpoints.

`GET /alarms` is the raw feed; `POST /alarms/summary` and `POST /alarms/recurring` are the
aggregations. The split matters for the copilot: answering "recurring high-severity alarms
over 90 days" by paging the raw feed would mean hundreds of rows in the model's context and
arithmetic done in a prompt. The aggregation endpoints keep the counting in tested Python
and hand the model a short, already-meaningful result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from apps.alarm_api.deps import StoreDep, TraceDep
from apps.alarm_api.domain import AlarmStatus, AlarmType, Severity, Site
from apps.alarm_api.schemas import (
    AlarmDetailResponse,
    AlarmListResponse,
    Pagination,
    RecurringAlarmsRequest,
    RecurringAlarmsResponse,
    SummaryRequest,
    SummaryResponse,
)
from apps.alarm_api.store import AlarmFilter, AlarmNotFoundError

router = APIRouter(prefix="/alarms", tags=["alarms"])


def _unprocessable(exc: ValueError) -> HTTPException:
    """Map a store-level rejection to a 422 the caller can act on.

    The store's messages name the valid options, and that text is passed through
    unchanged: when an LLM picks a KPI that does not exist, the error it reads back is what
    lets it retry correctly instead of giving up.
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"error": "invalid_parameter", "message": str(exc)},
    )


@router.get("", response_model=AlarmListResponse, summary="List alarms with filters and paging")
def list_alarms(
    store: StoreDep,
    asset_id: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    unit: str | None = None,
    site: Site | None = None,
    alarm_status: Annotated[
        list[AlarmStatus] | None,
        # The collections send `?status=active`; `status` shadows the imported module name
        # inside this function, so the parameter is aliased rather than renamed in the API.
        Query(alias="status", description="Repeatable"),
    ] = None,
    severity: Annotated[list[Severity] | None, Query(description="Repeatable")] = None,
    alarm_type: Annotated[list[AlarmType] | None, Query(description="Repeatable")] = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
    sort_by: str = "start_time",
    sort_order: str = "desc",
) -> AlarmListResponse:
    flt = AlarmFilter(
        asset_ids=asset_id,
        unit=unit,
        site=site,
        statuses=alarm_status,
        severities=severity,
        alarm_types=alarm_type,
        start_time=start_time,
        end_time=end_time,
    )
    rows = store.query(flt)
    try:
        window, pagination = store.paginate(
            rows, page=page, page_size=page_size, sort_by=sort_by, sort_order=sort_order
        )
    except ValueError as exc:
        raise _unprocessable(exc) from exc
    return AlarmListResponse(
        data=window,
        pagination=Pagination(**pagination),
        filters_applied=flt.describe(),
    )


@router.post("/summary", response_model=SummaryResponse, summary="Grouped KPI summary")
def alarm_summary(store: StoreDep, trace: TraceDep, body: SummaryRequest) -> SummaryResponse:
    flt = AlarmFilter(
        asset_ids=body.asset_ids,
        unit=body.unit,
        site=body.site,
        statuses=body.status,
        severities=body.severity,
        alarm_types=body.alarm_types,
        start_time=body.time_range.start if body.time_range else None,
        end_time=body.time_range.end if body.time_range else None,
    )
    try:
        result = store.summarize(flt, group_by=body.group_by, kpis=body.kpis)
    except ValueError as exc:
        raise _unprocessable(exc) from exc
    return SummaryResponse(**result, trace=trace)


@router.post(
    "/recurring",
    response_model=RecurringAlarmsResponse,
    summary="Alarm patterns that repeat, with a direction of travel",
)
def recurring_alarms(
    store: StoreDep, trace: TraceDep, body: RecurringAlarmsRequest
) -> RecurringAlarmsResponse:
    flt = AlarmFilter(
        asset_ids=body.asset_ids,
        unit=body.unit,
        site=body.site,
        alarm_types=body.alarm_types,
        min_severity=body.severity_threshold,
        start_time=body.time_range.start if body.time_range else None,
        end_time=body.time_range.end if body.time_range else None,
    )
    groups = store.recurring_alarms(flt, threshold=body.recurrence_threshold)
    return RecurringAlarmsResponse(
        filters_applied=flt.describe(),
        recurrence_threshold=body.recurrence_threshold,
        total_groups=len(groups),
        groups=groups[: body.limit],
        trace=trace,
    )


@router.get(
    "/{alarm_id}",
    response_model=AlarmDetailResponse,
    summary="One alarm, with its asset and recurrence context",
    responses={404: {"description": "Unknown alarm id"}},
)
def alarm_detail(store: StoreDep, alarm_id: str) -> AlarmDetailResponse:
    try:
        alarm = store.get_alarm(alarm_id)
    except AlarmNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "alarm_not_found", "message": f"No alarm with id {alarm_id!r}."},
        ) from None

    same = store.query(AlarmFilter(asset_ids=[alarm.asset_id], alarm_names=[alarm.alarm_name]))
    return AlarmDetailResponse(
        alarm=alarm,
        asset=store.get_asset(alarm.asset_id),
        recent_occurrences=len(same),
        # Nearest occurrences by time, excluding this one. Capped: the point is to show
        # this alarm is part of a pattern, not to return the whole pattern.
        similar_alarm_ids=[
            a.alarm_id
            for a in sorted(
                (a for a in same if a.alarm_id != alarm.alarm_id),
                key=lambda a: abs((a.start_time - alarm.start_time).total_seconds()),
            )[:10]
        ],
    )
