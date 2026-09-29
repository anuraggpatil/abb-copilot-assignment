"""Asset resolution endpoints.

`/assets/search` is the entry point for the whole workflow: the copilot receives a
human-written asset name ("Boiler Feed Pump 101") and has to turn it into an `asset_id`
before it can do anything else. Ranking quality therefore matters more here than anywhere
else in the simulator — see `AlarmStore.search_assets`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from apps.alarm_api.deps import StoreDep
from apps.alarm_api.domain import AlarmStatus, Site
from apps.alarm_api.schemas import (
    AssetMetadataResponse,
    AssetSearchResponse,
    AssetSummary,
    RelatedAsset,
)
from apps.alarm_api.store import AlarmFilter, AssetNotFoundError

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/search", response_model=AssetSearchResponse, summary="Find assets by name or tag")
def search_assets(
    store: StoreDep,
    query: Annotated[str, Query(description="Free-text asset name, tag or id")] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    unit: Annotated[str | None, Query(description="Restrict to one unit, e.g. 'Unit 5'")] = None,
    site: Annotated[Site | None, Query(description="Restrict to one site")] = None,
) -> AssetSearchResponse:
    # Ask for one more than the limit so `total_matches` can distinguish "exactly this
    # many" from "more were truncated" without scoring the corpus twice.
    matches = store.search_assets(query, limit=limit + 1, unit=unit, site=site)
    truncated = matches[:limit]
    # Descending synthetic score: the store already returned best-first, and callers use
    # the value for display and tie-breaking, not as a calibrated probability.
    results = [
        AssetSummary.from_asset(asset, match_score=round(1.0 - i * 0.05, 3))
        for i, asset in enumerate(truncated)
    ]
    return AssetSearchResponse(results=results, query=query, total_matches=len(matches))


@router.get(
    "/{asset_id}/metadata",
    response_model=AssetMetadataResponse,
    summary="Full asset record, relationships and live alarm counts",
    responses={404: {"description": "Unknown asset id"}},
)
def asset_metadata(store: StoreDep, asset_id: str) -> AssetMetadataResponse:
    try:
        asset = store.get_asset(asset_id)
    except AssetNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "asset_not_found",
                "message": f"No asset with id {asset_id!r}.",
            },
        ) from None

    related = [
        RelatedAsset(
            asset_id=r.asset_id,
            name=r.name,
            asset_type=r.asset_type,
            relationship="parent" if r.asset_id == asset.parent_asset_id else "related",
        )
        for r in store.related_assets(asset_id)
    ]
    alarms = store.query(AlarmFilter(asset_ids=[asset_id]))
    active = sum(1 for a in alarms if a.status is AlarmStatus.ACTIVE)
    open_count = sum(
        1 for a in alarms if a.status in (AlarmStatus.ACTIVE, AlarmStatus.ACKNOWLEDGED)
    )
    return AssetMetadataResponse(
        asset=asset,
        related_assets=related,
        active_alarm_count=active,
        open_alarm_count=open_count,
        # query() returns start_time order, so the last row is the most recent alarm.
        last_alarm_at=alarms[-1].start_time if alarms else None,
    )
