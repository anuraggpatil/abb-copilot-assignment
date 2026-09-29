"""Operator action recommendations — the simulator's advanced operation.

Thin by design: all the reasoning lives in `recommendations.py`, which is a pure function
of (store, now, subject) and therefore unit-testable without a client.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from apps.alarm_api import recommendations as engine
from apps.alarm_api.deps import NowDep, StoreDep, TraceDep
from apps.alarm_api.schemas import OperatorActionsRequest, OperatorActionsResponse
from apps.alarm_api.store import AlarmNotFoundError, AssetNotFoundError

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.post(
    "/operator-actions",
    response_model=OperatorActionsResponse,
    summary="Ranked operator actions for an alarm or an asset",
    responses={404: {"description": "Unknown alarm or asset id, or no alarms in the window"}},
)
def operator_actions(
    store: StoreDep, now: NowDep, trace: TraceDep, body: OperatorActionsRequest
) -> OperatorActionsResponse:
    alarm = None
    if body.alarm_id:
        try:
            alarm = store.get_alarm(body.alarm_id)
        except AlarmNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "alarm_not_found",
                    "message": f"No alarm with id {body.alarm_id!r}.",
                },
            ) from None

    try:
        result = engine.recommend(
            store,
            now,
            alarm=alarm,
            asset_id=body.asset_id,
            lookback_days=body.lookback_days,
            max_recommendations=body.max_recommendations,
        )
    except AssetNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "asset_not_found",
                "message": f"No asset with id {body.asset_id!r}.",
            },
        ) from None
    except LookupError as exc:
        # The asset exists but has no alarm history in the window. A 404 with a distinct
        # code, not an empty 200: "nothing to recommend" is a different answer from
        # "here are zero recommendations", and the copilot should say so.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_alarms_in_window", "message": str(exc)},
        ) from exc

    return OperatorActionsResponse(**result, trace=trace)
