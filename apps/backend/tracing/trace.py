"""The execution trace: what the copilot did, in order, with enough detail to audit it.

The assignment requires the GUI to show the MCP calls behind an answer, and a trace that only
said "called get_alarms — ok" would not let anyone check whether the answer was earned. So each
event carries the raw arguments, the raw result, the upstream HTTP status codes and retry
counts the MCP server reported, and the retrieval scores. That is the whole content of the
trace panel.

**Two rules constrain what may be written here, and both are enforced in code rather than by
convention, because a trace is exactly where a secret would end up by accident.**

1. *No secrets.* `redact` walks every payload and replaces the value of any key whose name
   looks like a credential. The backend holds one token and the MCP server holds another; a
   tool result or an error string that quoted either would publish it to an unauthenticated
   `GET /trace/{id}` and to the browser.

2. *No complete documents.* Retrieval events record chunk ids, references and scores — never
   chunk text. The quotes the answer cites are bounded and travel with the citations; a trace
   that carried full bodies would turn the retrieval layer's access controls into a formality
   and would make the trace unreadable besides. Strings are truncated at `MAX_FIELD_CHARS` as
   a backstop, so a payload nobody anticipated cannot become a document dump.

The store is in-memory and bounded. Persistence would be the right call for a real deployment
and is deliberately out of scope here: it is one interface away, and an unbounded dict in a
long-lived process is a leak, not a feature.
"""

from __future__ import annotations

import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Steps worth showing separately in the panel. `plan` is the model choosing; `mcp_tool_call`
#: and `rag_retrieval` are the two backends; `llm_call` covers synthesis.
EventKind = Literal[
    "plan",
    "tool_discovery",
    "mcp_tool_call",
    "rag_retrieval",
    "llm_call",
    "synthesis",
    "error",
]
EventStatus = Literal["ok", "error", "partial"]

#: Keys whose values never appear in a trace, matched case-insensitively anywhere in the name.
#: Broad on purpose: the cost of redacting one harmless field is nothing, and the cost of
#: missing one is a published credential.
SECRET_KEY = re.compile(
    r"token|secret|password|passwd|api[_-]?key|authorization|credential|bearer|cookie",
    re.IGNORECASE,
)

#: Backstop cap on any single string written into a trace. Above the largest legitimate field
#: (a bounded citation quote is 320 characters) and far below a document.
MAX_FIELD_CHARS = 2000

#: Backstop cap on list length, for the same reason.
MAX_LIST_ITEMS = 50

REDACTED = "***redacted***"


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    conversation_id: str
    #: One request — one question asked. A conversation accumulates several.
    request_id: str
    #: Propagated to the MCP server and on to the alarm API, which echoes it back. This is the
    #: id that lets one line in the API's log be tied to one row in this panel.
    trace_id: str
    seq: int
    kind: EventKind
    name: str
    started_at: datetime
    duration_ms: float = 0.0
    status: EventStatus = "ok"
    #: One line for the collapsed row. Written by the caller because only the caller knows
    #: what mattered about the step.
    summary: str = ""
    #: Everything else, redacted. Shape depends on `kind`; see the module docstring.
    detail: dict[str, Any] = Field(default_factory=dict)


class ConversationTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    events: list[TraceEvent] = Field(default_factory=list)

    def of_kind(self, kind: EventKind) -> list[TraceEvent]:
        return [event for event in self.events if event.kind == kind]


class TraceStore:
    """Bounded, in-memory, insertion-ordered. The oldest conversation is evicted first."""

    def __init__(self, max_conversations: int = 50) -> None:
        self._max = max(1, max_conversations)
        self._conversations: OrderedDict[str, list[TraceEvent]] = OrderedDict()

    def append(self, event: TraceEvent) -> None:
        events = self._conversations.setdefault(event.conversation_id, [])
        events.append(event)
        self._conversations.move_to_end(event.conversation_id)
        while len(self._conversations) > self._max:
            self._conversations.popitem(last=False)

    def get(self, conversation_id: str) -> ConversationTrace | None:
        events = self._conversations.get(conversation_id)
        if events is None:
            return None
        return ConversationTrace(conversation_id=conversation_id, events=list(events))

    def conversation_ids(self) -> list[str]:
        return list(self._conversations)


class TraceRecorder:
    """Records events for one request: assigns sequence numbers and fans them out.

    The `sink` is how the SSE endpoint streams a step the moment it happens instead of after
    the answer is finished. It is optional so the orchestrator is testable without one, and
    awaited rather than fired-and-forgotten so ordering is preserved — an out-of-order trace
    panel is worse than a slow one.
    """

    def __init__(
        self,
        store: TraceStore,
        *,
        conversation_id: str,
        request_id: str | None = None,
        trace_id: str | None = None,
        sink: Callable[[TraceEvent], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.conversation_id = conversation_id
        self.request_id = request_id or f"req-{uuid.uuid4().hex[:12]}"
        # The alarm API's own trace-header convention: a readable prefix, not a bare UUID, so
        # a request can be recognised in a log by eye.
        self.trace_id = trace_id or f"trc-{uuid.uuid4().hex[:12]}"
        self._sink = sink
        self._seq = 0

    async def record(
        self,
        kind: EventKind,
        name: str,
        *,
        status: EventStatus = "ok",
        summary: str = "",
        detail: dict[str, Any] | None = None,
        duration_ms: float = 0.0,
        started_at: datetime | None = None,
    ) -> TraceEvent:
        self._seq += 1
        event = TraceEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            conversation_id=self.conversation_id,
            request_id=self.request_id,
            trace_id=self.trace_id,
            seq=self._seq,
            kind=kind,
            name=name,
            started_at=started_at or datetime.now(UTC),
            duration_ms=duration_ms,
            status=status,
            summary=summary,
            detail=redact(detail or {}),
        )
        self.store.append(event)
        if self._sink is not None:
            await self._sink(event)
        return event

    def timer(self) -> Timer:
        return Timer()


class Timer:
    """Elapsed milliseconds, on a monotonic clock.

    `perf_counter` rather than wall clock: durations are shown in the panel, and a clock
    adjustment mid-call must not be able to produce a negative one.
    """

    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self._start = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Strip credentials and cap sizes, recursively.

    Applied to every payload on the way into a trace rather than at the boundary it is read
    from, so a new event kind is covered without anyone remembering to opt in.
    """
    if _depth > 8:
        # Depth limit rather than cycle detection: these payloads come from JSON, so they are
        # trees, and a limit is the cheaper guard against a pathological one.
        return "<truncated: nesting too deep>"

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            out[name] = REDACTED if SECRET_KEY.search(name) else redact(item, _depth=_depth + 1)
        return out

    if isinstance(value, (list, tuple)):
        items = [redact(item, _depth=_depth + 1) for item in list(value)[:MAX_LIST_ITEMS]]
        dropped = len(value) - len(items)
        if dropped > 0:
            items.append(f"<{dropped} more items omitted>")
        return items

    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"… <{len(value) - MAX_FIELD_CHARS} chars omitted>"

    return value
