"""The alarm-management MCP server.

A factory, not a module-level singleton, for the same reason the simulator is one: tests
need a server wired to an in-process alarm API and a frozen clock, and a singleton would
force them to mutate global state to get it.

The connector lives in the server's **lifespan**, so one HTTP connection pool is shared by
every tool call and is closed when the server stops. `client_factory` exists so a test can
hand in a connector pointed at the FastAPI app over `httpx.ASGITransport`: the tools, the
error mapping and the trace propagation are then all exercised for real, with no socket.

The `instructions` string is part of the contract too. It is sent to the client at
initialisation and is the only place to say things that hold across tools — chain from
names to ids, and never treat an outage as an absence of alarms.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer

from alarm_management.config import McpSettings, get_settings
from alarm_management.context import ServerContext, record_upstream_call
from alarm_management.tools import alarms, assets, recommendations
from connectors.alarm_api import AlarmApiClient

SERVER_NAME = "alarm-management"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
Tools for investigating plant alarms against a plant alarm management system.

How to use them together:
1. search_assets — turn the equipment the operator named into an asset_id.
2. get_recurring_alarms or get_alarm_summary — establish what has been happening and
   whether it is getting worse. Prefer these over get_alarms for windows longer than a
   few days.
3. get_alarms — pull the individual events you intend to cite, and an alarm_id.
4. get_operator_recommendations — ranked actions, each citing a procedure section.

Ground every recommendation in the procedure text those references point to; do not
paraphrase a procedure from memory.

These tools only read. If one reports that the alarm system is unavailable, say so — an
outage is not the same as an asset having no alarms, and the difference matters to whoever
is standing in front of the pump.
"""


def build_server(
    settings: McpSettings | None = None,
    *,
    client_factory: Callable[[McpSettings], AlarmApiClient] | None = None,
    now: Callable[[], datetime] | None = None,
) -> MCPServer:
    """Assemble the server with its tools and its connector lifespan."""
    resolved_settings = settings or get_settings()
    build_client = client_factory or _default_client

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[ServerContext]:
        client = build_client(resolved_settings)
        try:
            yield ServerContext(
                client=client,
                settings=resolved_settings,
                now=now or (lambda: datetime.now(UTC)),
            )
        finally:
            # Owned here because it was created here: whoever opens the pool closes it.
            await client.aclose()

    server = MCPServer(
        name=SERVER_NAME,
        title="Alarm Management",
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        log_level="WARNING",
    )

    assets.register(server)
    alarms.register(server)
    recommendations.register(server)
    return server


def _default_client(settings: McpSettings) -> AlarmApiClient:
    return AlarmApiClient(
        settings.alarm_api_base_url,
        settings.alarm_api_token,
        timeout_seconds=settings.alarm_api_timeout_seconds,
        max_retries=settings.alarm_api_max_retries,
        client_id=settings.client_id,
        # The observer files each attempt against whichever tool call is in scope; see
        # `context.collecting_upstream_calls`.
        observer=record_upstream_call,
    )
