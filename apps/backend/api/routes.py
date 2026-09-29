"""The copilot's HTTP surface: ask a question, watch it work, read back what it did.

`POST /chat` streams. The trace panel is a requirement, and a trace that arrives with the answer
is a log; one that arrives as the work happens is the thing the assignment asks for — an operator
watching `search_assets → get_recurring_alarms → search_procedures` appear in sequence can see
the investigation being done, and a 20-second wait stops looking like a hang.

**What is streamed and what is not.** Trace events stream as they are recorded; the answer
arrives as one final event. Token-by-token streaming of the answer is deliberately not attempted:
the answer is post-processed before it may be shown — fabricated citations are neutralised against
the evidence actually retrieved, which cannot be done on a half-written sentence. Streaming the
text would mean either publishing an unchecked citation and correcting it afterwards, or holding
every token back until the end and pretending otherwise.

`POST /ask` is the same orchestration without the stream, for curl, for the e2e test, and for
anything that wants one JSON object. It exists because a test that has to parse SSE to assert on
an answer is testing the transport.

`GET /trace/{conversation_id}` is a read of the in-memory store, which is bounded and not
authenticated. That is deliberate for a local demo and is exactly why `TraceEvent.detail` is
redacted on the way in rather than on the way out: this endpoint publishes whatever was recorded,
so the recording is what has to be safe.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from apps.backend.api.service import CopilotService
from apps.backend.orchestration.orchestrator import CopilotAnswer, CopilotError
from apps.backend.tracing.trace import ConversationTrace, TraceEvent

log = logging.getLogger(__name__)

router = APIRouter()

#: Trace events buffered between the orchestrator and the stream. Generous, because the
#: orchestrator must never be slowed by a client that reads slowly, and small enough that a
#: client which has stopped reading entirely cannot grow it without bound.
EVENT_BUFFER = 256


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=2000)
    #: Supply to continue an existing trace; omit for a new conversation. The trace store keys
    #: on this, so the GUI can re-read a conversation after the stream has closed.
    conversation_id: str | None = Field(default=None, max_length=64)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    provider: str
    native_tools: bool
    mcp_reachable: bool
    mcp_server_url: str


def get_service(request: Request) -> CopilotService:
    service = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - only reachable if the lifespan did not run
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="copilot is not initialised"
        )
    return service


ServiceDep = Annotated[CopilotService, Depends(get_service)]


@router.get("/health", response_model=HealthResponse)
async def health(service: ServiceDep) -> HealthResponse:
    """Liveness plus the one dependency worth reporting: whether MCP answers right now.

    No auth, like the alarm simulator's own `/health`, so a container probe does not need a
    credential. It reports reachability and nothing about configuration.
    """
    reachable = await service.mcp_reachable()
    return HealthResponse(
        status="ok",
        provider=service.provider.name,
        native_tools=service.provider.supports_native_tools,
        mcp_reachable=reachable,
        mcp_server_url=service.settings.mcp_server_url,
    )


@router.get("/tools")
async def tools(service: ServiceDep) -> dict[str, Any]:
    """The catalogue the planner is handed, MCP and local together, as the GUI shows it."""
    catalogue, degradations = await service.catalogue()
    return {"tools": catalogue, "degradations": degradations}


@router.post("/ask", response_model=CopilotAnswer)
async def ask(payload: ChatRequest, service: ServiceDep) -> CopilotAnswer:
    try:
        return await service.ask(payload.question, conversation_id=payload.conversation_id)
    except CopilotError as error:
        # 502, not 500: the failure is an upstream the backend depends on — the LLM gateway —
        # and the distinction is what tells an operator which process to go and look at.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.post("/chat")
async def chat(payload: ChatRequest, service: ServiceDep) -> EventSourceResponse:
    """Run the investigation, streaming each traced step, then the answer.

    Event names are `trace`, `answer` and `error`; the stream closes after `answer` or `error`,
    so a client knows it is finished without a sentinel.
    """
    send, receive = anyio.create_memory_object_stream[tuple[str, str]](EVENT_BUFFER)

    async def sink(event: TraceEvent) -> None:
        await send.send(("trace", event.model_dump_json()))

    async def run() -> None:
        try:
            answer = await service.ask(
                payload.question, conversation_id=payload.conversation_id, sink=sink
            )
            await send.send(("answer", answer.model_dump_json()))
        except CopilotError as error:
            await send.send(("error", _error_json(str(error))))
        except Exception as error:  # noqa: BLE001 - the stream must report, never hang
            log.exception("unhandled failure answering a question")
            await send.send(("error", _error_json(f"{type(error).__name__}: {error}")))
        finally:
            await send.aclose()

    async def events() -> Any:
        # `create_task` rather than a task group inside the generator: the generator is closed
        # by the SSE response, potentially from a different task than the one that started it,
        # and a task group's cancel scope cannot cross that boundary. The `finally` is what
        # guarantees the orchestration is cancelled when the client disconnects rather than
        # running on to completion for a browser tab that has closed.
        worker = asyncio.create_task(run())
        try:
            async with receive:
                async for name, data in receive:
                    yield {"event": name, "data": data}
        finally:
            if not worker.done():
                worker.cancel()

    return EventSourceResponse(events())


@router.get("/trace/{conversation_id}", response_model=ConversationTrace)
async def trace(conversation_id: str, service: ServiceDep) -> ConversationTrace:
    found = service.trace(conversation_id)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no trace for conversation {conversation_id!r}. Traces are held in memory and "
                "the oldest are evicted; a restart clears them."
            ),
        )
    return found


def _error_json(message: str) -> str:
    # Hand-built rather than a model: this path runs when something has already gone wrong, and
    # it must not be able to fail in serialisation.
    return ConversationErrorEvent(error=message).model_dump_json()


class ConversationErrorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
