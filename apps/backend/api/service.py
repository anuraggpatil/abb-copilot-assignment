"""What the API layer holds onto, and the one thing it deliberately does not.

Long-lived, built once at startup: the LLM provider, the retrieval index (embedded Qdrant takes
an exclusive file lock, so one instance per process is not a choice), the trace store, and the
conversation memory that makes a second question a follow-up rather than a fresh start.

**The MCP session is opened per request, not held open.** That is the interesting decision here
and it goes against the instinct to pool a connection, so the reasoning matters:

* *Correctness.* The MCP client owns an anyio task group. A session entered in the ASGI lifespan
  task and used from a request task — or worse, reconnected from one — puts a cancel scope in a
  task that did not create it, which anyio correctly refuses. The same constraint is why the MCP
  integration tests open their own client inside each test body rather than taking one from a
  fixture.
* *Operability.* The MCP server is a separate service that can be restarted, redeployed or not
  started yet. A per-request connection means the backend survives all three, and starting the
  three processes in any order works. A pooled session would come up broken and stay broken.
* *Cost.* A local handshake plus `list_tools` is a few milliseconds against an LLM call measured
  in seconds. There is nothing to optimise here yet, and if there ever is, `session_factory` is
  the single place it changes.

When the MCP server cannot be reached the request is still served, from the document tools
alone, with the loss stated in the trace, to the model, and in the answer's caveats. That is the
degraded scenario the demo has to show, so it is a designed path rather than an exception
someone will meet for the first time on video.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    suppress,
)
from typing import Any

from apps.backend.config import BackendSettings
from apps.backend.llm.provider import LLMProvider
from apps.backend.orchestration.memory import ConversationMemory
from apps.backend.orchestration.orchestrator import CopilotAnswer, Orchestrator, Sink
from apps.backend.orchestration.procedures import ProcedureSearchTool
from apps.backend.orchestration.registry import ToolRegistry
from apps.backend.tracing.trace import ConversationTrace, TraceStore
from rag.retrieval.retriever import HybridRetriever

log = logging.getLogger(__name__)

#: Opens an MCP session. Injected so tests can hand back an in-memory client talking to an
#: `MCPServer` object, and production can dial the URL, with no branch in the service.
SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]

MCP_UNREACHABLE = (
    "The MCP server could not be reached, so no alarm, asset or recommendation data was "
    "available for this answer — only the procedure documents. Start it with `make mcp` "
    "(and the API it fronts with `make api`)."
)


def _single_error(error: BaseException) -> BaseException:
    """The one exception inside a chain of single-member groups, or the group itself."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


class CopilotService:
    """Owns the process-wide resources and runs one question at a time through them."""

    def __init__(
        self,
        *,
        settings: BackendSettings,
        provider: LLMProvider,
        retriever: HybridRetriever,
        session_factory: SessionFactory,
        store: TraceStore | None = None,
        memory: ConversationMemory | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.retriever = retriever
        self.store = store or TraceStore(settings.trace_retention)
        # Process-wide, like the trace store, and for the same reason: a new `Orchestrator` is
        # built per request (the MCP session cannot outlive one), so anything a conversation is
        # supposed to remember between questions has to be held out here.
        self.memory = memory or ConversationMemory(
            max_turns=settings.conversation_turns,
            max_conversations=settings.trace_retention,
        )
        self._session_factory = session_factory
        self._procedures = ProcedureSearchTool(retriever)

    async def ask(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        sink: Sink | None = None,
    ) -> CopilotAnswer:
        async with self._registry() as (registry, degradations):
            orchestrator = Orchestrator(
                provider=self.provider,
                registry=registry,
                store=self.store,
                settings=self.settings,
                memory=self.memory,
            )
            return await orchestrator.answer(
                question,
                conversation_id=conversation_id,
                sink=sink,
                degradations=degradations,
            )

    async def catalogue(self) -> tuple[list[dict[str, Any]], list[str]]:
        """The tool catalogue as the planner would see it, for `GET /tools` and the GUI."""
        async with self._registry() as (registry, degradations):
            tools = await registry.discover()
            return [
                {
                    "name": tool.name,
                    "title": tool.title,
                    "description": tool.description,
                    "backend": tool.backend,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ], degradations

    async def mcp_reachable(self) -> bool:
        """Used by `/health`. Deliberately a real connection attempt, not a cached flag."""
        try:
            async with self._session_factory() as session:
                await session.list_tools()
        except Exception:  # noqa: BLE001 - any failure means "not reachable"
            return False
        return True

    def trace(self, conversation_id: str) -> ConversationTrace | None:
        return self.store.get(conversation_id)

    async def aclose(self) -> None:
        await self.provider.aclose()
        # Releases embedded Qdrant's exclusive lock. Without it the next `make ingest` blocks
        # on a process that has already stopped serving.
        with suppress(Exception):
            self.retriever.close()

    @asynccontextmanager
    async def _registry(self) -> AsyncIterator[tuple[ToolRegistry, list[str]]]:
        """A registry with the MCP tools if the server answers, local tools only if it does not.

        The connection is entered through an `AsyncExitStack` rather than a plain `async with`
        so that "could not connect" and "the request failed while connected" are caught in
        different places. Wrapping the body in the same `try` would conflate them, and every
        orchestration bug would then be reported to the user as an unreachable MCP server —
        sending whoever debugs it to the wrong process, which this project has already lost time
        to once over a port conflict.
        """
        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(self._session_factory())
        except Exception as error:  # noqa: BLE001 - any connect failure degrades, none fails
            await stack.aclose()
            log.warning("MCP server unreachable (%s); serving from documents only", error)
            yield ToolRegistry(local_tools=[self._procedures]), [MCP_UNREACHABLE]
            return

        try:
            async with stack:
                yield (
                    ToolRegistry(
                        session=session,
                        local_tools=[self._procedures],
                        tool_timeout_seconds=self.settings.mcp_tool_timeout_seconds,
                    ),
                    [],
                )
        except BaseExceptionGroup as group:
            # The MCP client closes over an anyio task group, which re-raises anything that
            # propagated through it wrapped — often twice — in an `ExceptionGroup`. Left alone,
            # a `CopilotError` leaving `ask()` would no longer be caught by `except CopilotError`
            # in the route, so "the model was unreachable" would be reported as an unhandled 500
            # instead of the 502 it is. Unwrapped only while a group holds exactly one error;
            # anything genuinely concurrent stays a group.
            single = _single_error(group)
            if single is group:
                raise
            raise single from None
