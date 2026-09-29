"""The LLM seam: one interface, two implementations, no provider details above this line.

Nothing in `orchestration/` imports an HTTP client or names a vendor. That is the point — the
orchestrator, planner
and synthesis step are written against `LLMProvider`, so the deterministic `ScriptedProvider`
is not a mock of the real thing but the same interface with a different body. The end-to-end
test therefore exercises the production code path with no network and no token, which is the
only way the headline acceptance test can run in CI.

**Why the interface returns tool calls even when the provider has no tool protocol.** The whole
orchestration loop depends on tool selection, and not every model or gateway offers a native way
to ask for one. So `LLMResponse` always carries a `tool_calls` list and `supports_native_tools`
says where it came from: the provider's native tool protocol, or — when that is unavailable — a
JSON object the planner asked the model to emit and parsed out of the text. Callers above this
module cannot tell the difference, so the fallback is not a degraded second code path that only
gets exercised when something breaks. It is also what keeps the provider swappable in practice:
the seam survived replacing the LLM outright, and only this directory changed.

`latency_ms` and `usage` are returned rather than logged, because the trace panel is required
to show them and a provider that logged them would make that data unavailable to the caller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


class LLMError(RuntimeError):
    """The provider could not produce a response.

    One exception type for every provider failure — timeout, auth, rate limit, malformed
    response — because the orchestrator's handling is the same in all of them: the step is
    recorded as failed and the conversation cannot continue. The specifics belong in the
    message, and the message is shown to the operator, so it must never contain the token.
    """


class ToolCall(BaseModel):
    """A tool invocation the model asked for, after parsing but before validation."""

    model_config = ConfigDict(extra="forbid")

    #: Provider-assigned where there is one, synthesised otherwise. Needed to pair a result
    #: back to its request when several tools are called in one step.
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMMessage(BaseModel):
    """One turn. `tool` messages carry a tool result back to the model."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str
    #: Set on `tool` messages, matching the `ToolCall.call_id` being answered.
    tool_call_id: str | None = None


class LLMUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    model: str = ""
    latency_ms: float = 0.0
    usage: LLMUsage | None = None
    #: Why generation stopped, verbatim from the provider when it says. Worth surfacing: a
    #: response truncated at the output-token ceiling looks like a complete answer, and the
    #: only sign is this field.
    stop_reason: str | None = None


class ToolDefinition(BaseModel):
    """A tool as the *model* sees it — name, prose, and a JSON Schema for its arguments.

    Deliberately not the MCP `Tool` type. The registry serves both MCP-discovered tools and
    the local retrieval tool from one catalogue, so the planner must not be able to tell them
    apart; letting an MCP type reach this far would make that impossible.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class LLMProvider(ABC):
    """Generate a response from a message list, optionally with tools.

    Async because the orchestrator runs inside FastAPI: a synchronous HTTP call here would
    block the event loop for the whole generation, which is seconds, and the SSE stream that
    is supposed to be reporting progress would stall along with it.
    """

    #: Shown in the trace so a reader knows which provider produced an answer.
    name: str = "llm"

    #: When False the planner uses its JSON protocol instead of passing `tools`.
    supports_native_tools: bool = False

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        """Produce one response. Raises `LLMError` on any failure."""

    async def aclose(self) -> None:  # noqa: B027 - optional by design, see below
        """Release provider resources. Default is nothing to release.

        Deliberately concrete and empty rather than abstract: a provider with no resources —
        the scripted one, and any future in-process model — should not have to write a no-op
        to satisfy the interface, and the backend's shutdown path calls this unconditionally.
        """
