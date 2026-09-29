"""Liveness endpoint. Deliberately unauthenticated — see `auth.py`."""

from __future__ import annotations

from fastapi import APIRouter

from apps.alarm_api import __version__
from apps.alarm_api.deps import StoreDep
from apps.alarm_api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness and dataset fingerprint")
def health(store: StoreDep) -> HealthResponse:
    return HealthResponse(version=__version__, dataset=store.stats())
