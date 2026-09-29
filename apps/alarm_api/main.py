"""FastAPI application for the Alarm Management API simulator.

An app *factory* rather than a module-level singleton: tests need several apps with
different seeds and a frozen `now`, and a singleton would force them to share one dataset
and mutate global state to get variety.

The dataset is generated once at startup. It is read-only afterwards, so there is no lock
and no cache invalidation to get wrong.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.alarm_api import __version__
from apps.alarm_api.auth import require_bearer_token
from apps.alarm_api.config import AlarmApiSettings, get_settings
from apps.alarm_api.middleware import TraceContextMiddleware
from apps.alarm_api.routers import alarms, analytics, assets, health, recommendations
from apps.alarm_api.seed import build_world
from apps.alarm_api.store import AlarmStore

DESCRIPTION = """
Simulated plant alarm management API. Stands in for a real alarm historian so the copilot
can be run and tested end to end with no external dependency.

`GET /health` is unauthenticated. Every other route requires `Authorization: Bearer <token>`.

The `trace_id`, `x-client-id` and `x-metadata-tag` request headers are propagated and echoed
on the response, and are also embedded in aggregation response bodies.
"""


def configure_logging(level: str = "INFO") -> None:
    """JSON-ish structured logging to stdout.

    Key-value rendering rather than a format string, because the access log is consumed by
    the copilot's trace view and by a human reading a terminal, and the former needs fields.
    """
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def create_app(
    settings: AlarmApiSettings | None = None,
    *,
    now: datetime | None = None,
    freeze_now: bool = False,
) -> FastAPI:
    """Build an app with its own generated dataset.

    `now` sets the instant the world is generated relative to. `freeze_now` additionally
    pins the value the *request handlers* use, so a test's "last 90 days" lines up exactly
    with the seeder's. Production leaves both unset: the world is built at startup and
    handlers read the wall clock.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    reference_now = now or datetime.now(UTC)

    app = FastAPI(
        title="Alarm Management API (simulator)",
        description=DESCRIPTION,
        version=__version__,
        # Grouped in the OpenAPI schema so the generated docs read as a product, not a
        # flat list. The MCP server's tool catalogue mirrors these groups.
        openapi_tags=[
            {"name": "health", "description": "Liveness and dataset fingerprint."},
            {"name": "assets", "description": "Asset resolution and metadata."},
            {"name": "alarms", "description": "Alarm feed and aggregations."},
            {"name": "recommendations", "description": "Ranked operator actions."},
            {"name": "analytics", "description": "Self-describing KPI metadata."},
        ],
    )
    app.state.settings = settings
    app.state.store = AlarmStore(
        build_world(reference_now, seed=settings.seed, days_of_history=settings.days_of_history)
    )
    if freeze_now:
        app.state.frozen_now = reference_now

    app.add_middleware(TraceContextMiddleware)

    # /health carries no dependency; everything else requires the bearer token. Declared
    # per-include so the exemption is visible here rather than inferred from a path list.
    app.include_router(health.router)
    protected = [Depends(require_bearer_token)]
    app.include_router(assets.router, dependencies=protected)
    app.include_router(alarms.router, dependencies=protected)
    app.include_router(recommendations.router, dependencies=protected)
    app.include_router(analytics.router, dependencies=protected)

    _install_error_handlers(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    """Give every failure the same body shape, carrying the trace id.

    FastAPI's default 422 body is a bare `detail` list, and its HTTPException body is
    whatever was passed as `detail`. The connector and the MCP error mapping both parse
    `{"error", "message"}`, so normalising here means neither needs a special case — and a
    tool-calling model gets a consistent, machine-readable failure it can retry against.
    """

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Routers raise HTTPException(detail={"error": ..., "message": ...}). FastAPI would
        # nest that under a "detail" key; unwrap it so the envelope is flat and identical
        # to every other error response. A plain-string detail still gets the same shape.
        body: dict[str, Any]
        if isinstance(exc.detail, dict):
            body = dict(exc.detail)
        else:
            body = {"error": f"http_{exc.status_code}", "message": str(exc.detail)}
        body.setdefault("trace_id", getattr(request.state, "trace_id", None))
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "The request did not match the expected schema.",
                # Pydantic's error list, made JSON-safe. Field-level detail is what lets a
                # caller fix the call instead of guessing.
                "detail": [
                    {"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ],
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the traceback, return a generic body. An internal error message can contain
        # data the caller should not see, so it stays server-side.
        structlog.get_logger("alarm_api.error").exception(
            "unhandled_exception",
            path=request.url.path,
            trace_id=getattr(request.state, "trace_id", None),
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "The API failed to handle the request.",
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )


def main() -> None:
    """`python -m apps.alarm_api` — run the simulator standalone.

    Passed as a factory (`uvicorn ... --factory`) rather than a module-level `app`
    singleton: constructing the app generates 400 days of data, and a module-level
    instance would pay that cost on *every* import, including in tests that only need the
    seeder.
    """
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "apps.alarm_api.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,  # structlog already owns stdout
    )


if __name__ == "__main__":
    main()
