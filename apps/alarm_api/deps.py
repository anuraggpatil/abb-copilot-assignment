"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from apps.alarm_api.config import AlarmApiSettings, get_settings
from apps.alarm_api.schemas import TraceEcho
from apps.alarm_api.store import AlarmStore


def get_store(request: Request) -> AlarmStore:
    """The store built once at startup and held on `app.state`."""
    store: AlarmStore = request.app.state.store
    return store


def get_api_settings(request: Request) -> AlarmApiSettings:
    """The settings this app was *built* with — not the process-wide cached ones.

    `create_app` takes an explicit settings object, so a per-request dependency that called
    `get_settings()` instead would make that argument a half-truth: the app would serve the
    dataset it was given while authenticating against whatever the environment happened to
    hold. The fallback covers an app constructed without going through the factory.
    """
    settings: AlarmApiSettings | None = getattr(request.app.state, "settings", None)
    return settings if settings is not None else get_settings()


def get_trace(request: Request) -> TraceEcho:
    """Trace identifiers captured by `TraceContextMiddleware`, for embedding in bodies."""
    return TraceEcho(
        trace_id=getattr(request.state, "trace_id", None),
        client_id=getattr(request.state, "client_id", None),
        metadata_tag=getattr(request.state, "metadata_tag", None),
    )


def get_now(request: Request) -> datetime:
    """The reference instant for relative queries.

    Injectable rather than a bare `datetime.now()` call: tests pin it to the same instant
    the world was generated at, so "last 90 days" means the same thing to the assertions as
    it did to the seeder. `app.state.frozen_now` is set only by tests.
    """
    frozen: datetime | None = getattr(request.app.state, "frozen_now", None)
    return frozen if frozen is not None else datetime.now(UTC)


SettingsDep = Annotated[AlarmApiSettings, Depends(get_api_settings)]
StoreDep = Annotated[AlarmStore, Depends(get_store)]
TraceDep = Annotated[TraceEcho, Depends(get_trace)]
NowDep = Annotated[datetime, Depends(get_now)]
