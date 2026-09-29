"""Per-call plumbing shared by every tool.

Three things every tool needs and none of them should re-implement:

1. **The connector**, built once in the server's lifespan and reached through the request's
   lifespan context, so one HTTP connection pool serves the whole process.
2. **The trace id**, lifted out of the MCP request's `_meta` so a question asked in the GUI
   and the alarm API's own access log can be joined afterwards.
3. **Uniform failure behaviour** — connector exceptions become `ToolError`s with the same
   wording rules everywhere, rather than each tool inventing its own.

`tool_session` bundles all three into one `async with`, which is also what keeps the tool
bodies readable: they contain the call and the mapping to the result, and nothing else.

The upstream-call collection uses a `ContextVar` rather than per-call client state because
one connector instance serves concurrent tool calls. A contextvar set inside the session is
visible to the observer callback on that task and invisible to every other, so two
simultaneous investigations cannot appear in each other's traces.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from alarm_management.config import McpSettings
from alarm_management.mapping import to_tool_error
from alarm_management.schemas import UpstreamCall, upstream_from
from connectors.alarm_api import AlarmApiClient, AlarmApiError, CallRecord, TraceContext

log = structlog.get_logger("alarm_mcp")

#: Key the copilot sets in the MCP request's `_meta`. Not an `io.modelcontextprotocol/`
#: name because it is ours; the SDK passes unknown `_meta` keys through untouched.
TRACE_ID_META_KEY = "trace_id"

_call_sink: ContextVar[list[CallRecord] | None] = ContextVar("alarm_api_calls", default=None)


def record_upstream_call(record: CallRecord) -> None:
    """Connector observer: file an attempt against the tool call in scope, if any."""
    sink = _call_sink.get()
    if sink is not None:
        sink.append(record)


@contextmanager
def collecting_upstream_calls() -> Iterator[list[CallRecord]]:
    bucket: list[CallRecord] = []
    token = _call_sink.set(bucket)
    try:
        yield bucket
    finally:
        _call_sink.reset(token)


@dataclass(frozen=True, slots=True)
class ServerContext:
    """What the lifespan owns and every tool borrows."""

    client: AlarmApiClient
    settings: McpSettings
    #: Injected rather than read from the clock inside tools, so a test can freeze "now" and
    #: have `lookback_days` line up exactly with the seeded dataset.
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(slots=True)
class ToolSession:
    """One tool invocation's view of the world."""

    client: AlarmApiClient
    settings: McpSettings
    trace: TraceContext
    now: datetime
    records: list[CallRecord] = field(default_factory=list)

    @property
    def trace_id(self) -> str | None:
        return self.trace.trace_id

    def upstream(self) -> list[UpstreamCall]:
        return upstream_from(self.records)

    def window(
        self,
        *,
        lookback_days: int,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> tuple[datetime, datetime]:
        """Resolve the time window a tool was asked about.

        `lookback_days` is the ergonomic form — "the last 90 days" is how the question is
        actually phrased, and it spares a model from doing date arithmetic it is bad at.
        Explicit endpoints override it, either of them alone, so "since 1 July" needs no
        matching end date.
        """
        end = end_time or self.now
        start = start_time or end - timedelta(days=lookback_days)
        if start >= end:
            raise ToolError(
                f"The requested window starts at or after it ends ({start.isoformat()} to "
                f"{end.isoformat()}). Supply start_time before end_time, or pass only "
                "lookback_days."
            )
        return start, end

    def rows(self, requested: int) -> int:
        """Clamp a row count to the server's ceiling, silently.

        Raising instead would turn a model asking for too much into a failed investigation;
        returning fewer rows than asked is both recoverable and visible, since every result
        reports `total_matching` alongside what it returned.
        """
        return max(1, min(requested, self.settings.max_rows))


def server_context(ctx: Context) -> ServerContext:
    resolved = ctx.request_context.lifespan_context
    if not isinstance(resolved, ServerContext):  # pragma: no cover - defensive
        raise RuntimeError("The MCP lifespan did not install a ServerContext.")
    return resolved


def trace_id_from(ctx: Context) -> str:
    """The caller's trace id, or a fresh one so the call is still traceable.

    `request_context.meta` is a plain dict in this SDK version, carrying the protocol's own
    `io.modelcontextprotocol/*` keys alongside whatever the client added.
    """
    meta = ctx.request_context.meta
    if isinstance(meta, dict):
        supplied = meta.get(TRACE_ID_META_KEY)
        if isinstance(supplied, str) and supplied.strip():
            return supplied
    return f"mcp-{uuid.uuid4().hex[:12]}"


@asynccontextmanager
async def tool_session(ctx: Context, tool: str) -> AsyncIterator[ToolSession]:
    """Open a session for one tool call, mapping connector failures on the way out."""
    resolved = server_context(ctx)
    session = ToolSession(
        client=resolved.client,
        settings=resolved.settings,
        trace=TraceContext(
            trace_id=trace_id_from(ctx),
            client_id=resolved.settings.client_id,
            # The API echoes this, so its access log says which MCP tool caused the call.
            metadata_tag=tool,
        ),
        now=resolved.now(),
    )
    started = time.perf_counter()
    with collecting_upstream_calls() as records:
        session.records = records
        try:
            yield session
        except AlarmApiError as exc:
            log.warning(
                "tool_failed",
                tool=tool,
                trace_id=session.trace_id,
                # The class name, not the message: the message can quote the API's text and
                # this line goes to stdout.
                reason=type(exc).__name__,
                status_code=exc.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise to_tool_error(exc, tool=tool) from exc
        log.info(
            "tool_ok",
            tool=tool,
            trace_id=session.trace_id,
            api_calls=len(records),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
