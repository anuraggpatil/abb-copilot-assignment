"""Trace-context propagation.

The assignment's Postman collections send three headers — spelled exactly `trace_id`,
`x-client-id` and `x-metadata-tag` — on the summary, correlation, operator-actions and
calculation-execute requests, and the simulator is expected to propagate them.

Two behaviours, both needed:

* **Read or generate.** A missing `trace_id` is generated rather than left empty, so every
  request is traceable end to end even when a caller forgets. The response reports which
  happened, because "the copilot forwarded its trace id" and "the API invented one" are
  different facts when you are debugging a broken chain.
* **Echo on the response**, and stash on `request.state` so route handlers can also embed
  the values in the body. The copilot's trace panel joins on `trace_id`, so it has to
  survive the round trip.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

TRACE_ID_HEADER = "trace_id"
CLIENT_ID_HEADER = "x-client-id"
METADATA_TAG_HEADER = "x-metadata-tag"
# Distinct from trace_id: one trace spans a whole copilot conversation step, one request id
# identifies this single HTTP call within it.
REQUEST_ID_HEADER = "x-request-id"
TRACE_GENERATED_HEADER = "x-trace-id-generated"

_log = structlog.get_logger("alarm_api.request")


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Populate `request.state.trace` and echo the trace headers on the response."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(TRACE_ID_HEADER)
        trace_id = incoming or f"trc-{uuid.uuid4().hex[:16]}"
        request_id = request.headers.get(REQUEST_ID_HEADER) or f"req-{uuid.uuid4().hex[:12]}"
        client_id = request.headers.get(CLIENT_ID_HEADER)
        metadata_tag = request.headers.get(METADATA_TAG_HEADER)

        request.state.trace_id = trace_id
        request.state.request_id = request_id
        request.state.client_id = client_id
        request.state.metadata_tag = metadata_tag

        response = await call_next(request)

        response.headers[TRACE_ID_HEADER] = trace_id
        response.headers[REQUEST_ID_HEADER] = request_id
        if client_id:
            response.headers[CLIENT_ID_HEADER] = client_id
        if metadata_tag:
            response.headers[METADATA_TAG_HEADER] = metadata_tag
        if incoming is None:
            response.headers[TRACE_GENERATED_HEADER] = "true"

        # Path and query only. Never the Authorization header, and never the request body —
        # the point of a structured access log is to be safe to ship somewhere.
        _log.info(
            "request",
            method=request.method,
            path=request.url.path,
            query=str(request.url.query) or None,
            status=response.status_code,
            trace_id=trace_id,
            request_id=request_id,
            client_id=client_id,
        )
        return response
