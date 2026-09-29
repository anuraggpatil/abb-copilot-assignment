"""A provider that returns what it was told to, in order.

This is what makes the acceptance scenario an automated test rather than a demo. The e2e run
uses the real simulator, the real MCP server and the real RAG index; only the model is
scripted, so the assertion is about *our* orchestration — did the planner's call chain
resolve the asset, find the recurring pattern, retrieve the cited procedure, and did the
answer come out attributed — and not about what a language model felt like doing that
afternoon.

It is not a mock. It implements `LLMProvider` exactly, so the code under test is the
production path; nothing is patched and no call is intercepted.

Two design choices worth stating:

* **Running out of script raises.** A provider that quietly returned an empty response would
  turn "the orchestrator asked for one more step than expected" into a passing test with a
  vacuous answer. The error names the turn and the last question, which is what you need.
* **Turns may be callables.** Fixed responses are enough for a happy path, but asserting that
  the planner *reacted* to a tool failure needs a turn that can look at the messages it was
  given. A callable turn receives the conversation and returns the response.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from apps.backend.llm.provider import (
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    ToolCall,
    ToolDefinition,
)

#: A scripted turn: either a canned response or a function of the conversation so far.
type Turn = LLMResponse | Callable[[list[LLMMessage]], LLMResponse]


class ScriptedProvider(LLMProvider):
    name = "scripted"

    def __init__(self, script: Sequence[Turn], *, supports_native_tools: bool = False) -> None:
        self._script = list(script)
        self.supports_native_tools = supports_native_tools
        #: Every call made, in order, so a test can assert on what the orchestrator sent —
        #: which tools it advertised, whether it passed the retrieved passages, whether the
        #: system prompt carried the trust boundary.
        self.calls: list[tuple[list[LLMMessage], list[ToolDefinition]]] = []

    @property
    def turns_used(self) -> int:
        return len(self.calls)

    @property
    def turns_remaining(self) -> int:
        return len(self._script) - len(self.calls)

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        index = len(self.calls)
        self.calls.append((list(messages), list(tools or [])))

        if index >= len(self._script):
            last = next(
                (m.content for m in reversed(messages) if m.role == "user"), "<no user message>"
            )
            raise LLMError(
                f"scripted provider exhausted: turn {index + 1} requested but the script has "
                f"{len(self._script)}. Last prompt began: {last[:120]!r}"
            )

        turn = self._script[index]
        return turn(messages) if callable(turn) else turn


def plan_turn(*calls: tuple[str, dict[str, object]], thought: str = "") -> LLMResponse:
    """A turn that asks for tool calls using the planner's JSON protocol.

    Written as the JSON the planner parses rather than as `tool_calls`, deliberately: the
    fallback path is the one that runs by default (`LLM_NATIVE_TOOLS=false`), so the tests
    should exercise its parser, not bypass it.
    """
    payload = {
        "thought": thought,
        "done": False,
        "tool_calls": [{"name": name, "arguments": arguments} for name, arguments in calls],
    }
    return LLMResponse(text=json.dumps(payload), model="scripted")


def native_plan_turn(*calls: tuple[str, dict[str, object]]) -> LLMResponse:
    """The same plan expressed as native tool calls, for the native-tools path."""
    return LLMResponse(
        tool_calls=[
            ToolCall(call_id=f"call_{index}", name=name, arguments=dict(arguments))
            for index, (name, arguments) in enumerate(calls)
        ],
        model="scripted",
    )


def finish_turn(thought: str = "enough evidence gathered") -> LLMResponse:
    """A turn that declares the investigation complete and asks for no further tools."""
    return LLMResponse(
        text=json.dumps({"thought": thought, "done": True, "tool_calls": []}), model="scripted"
    )


def answer_turn(text: str) -> LLMResponse:
    """A synthesis turn: prose, no protocol."""
    return LLMResponse(text=text, model="scripted")
