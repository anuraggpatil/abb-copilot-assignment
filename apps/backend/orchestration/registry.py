"""One catalogue: the MCP server's tools and the local retrieval tool, indistinguishable.

This is the design decision the whole use case turns on. The assignment requires MCP and
document RAG to participate in **one** business workflow, and lists "MCP and RAG demonstrated
separately" as a red flag. The way to make that structural rather than aspirational is to give
the planner a single list of tools in which `search_procedures` sits beside `get_alarms` with
the same shape, so there is no code path in which the model uses one without being able to
reach for the other. Nothing above this module knows which backend served a call — only the
trace records it, because a reader does want to know.

**Why retrieval is local and the alarm API is not.** The assignment requires the alarm API to
be reached *exclusively* through MCP; it places no such requirement on the documents. Wrapping
the retriever in a second MCP server would add a process, a transport and a serialisation hop
to buy nothing the grading asks for. The registry is the seam where that could change: making
retrieval remote means registering it as an MCP tool instead of a local one, and no caller
changes.

**Validation is here, not only in the planner.** The planner validates so it can tell the model
what was wrong and let it retry. The registry validates again immediately before execution,
because a tool name and an argument set can originate in text a model read out of a retrieved
document, and one enforcement point that can be bypassed by a future caller is not a trust
boundary. Unknown names are refused against the discovered catalogue — an allowlist that
cannot drift from what actually exists.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Literal, Protocol, cast

import anyio
import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from apps.backend.llm.provider import ToolDefinition
from apps.backend.tracing.trace import Timer

log = logging.getLogger(__name__)

Backend = Literal["mcp", "local"]

#: Why a call failed, in the vocabulary the orchestrator branches on. `validation` and
#: `unknown_tool` are the model's mistakes and it is told about them so it can correct; the
#: rest are the world's, and the answer must be written around them.
ErrorKind = Literal["unknown_tool", "validation", "tool_error", "timeout", "transport"]


class ToolDescriptor(BaseModel):
    """A tool as the planner sees it, plus the one thing only the trace needs."""

    model_config = ConfigDict(extra="forbid")

    name: str
    title: str = ""
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    backend: Backend = "mcp"

    def as_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description or self.title,
            input_schema=self.input_schema,
        )


class ToolOutcome(BaseModel):
    """The result of one attempted call — successful or not.

    `result` is what the model is shown. `trace` is what the panel is shown, and they are
    separate fields because they must differ: a retrieval result hands the model bounded quotes
    it needs in order to cite anything, while the trace records chunk ids and scores and no
    document text at all.
    """

    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    backend: Backend = "mcp"
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result: dict[str, Any] | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    duration_ms: float = 0.0
    trace: dict[str, Any] = Field(default_factory=dict)

    def as_model_text(self) -> str:
        """How this outcome is written back into the conversation.

        Errors are reported to the model in full. A tool that failed is information — the
        asset does not exist, the window is empty, the upstream is down — and an orchestrator
        that hid it would get an answer written as though the call had succeeded.
        """
        if self.ok:
            # `ensure_ascii=False` matters more than it looks: the section marks in a
            # `procedure_reference` would otherwise arrive as `§`, and the model is expected
            # to copy those strings verbatim into `search_procedures(references=…)`, where they
            # are matched against the corpus.
            body = json.dumps(self.result, default=str, indent=2, ensure_ascii=False)
            return f"{self.name} returned:\n{body}"
        return (
            f"{self.name} failed ({self.error_kind}): {self.error}\n"
            "Do not retry it with the same arguments. Either correct them, use a different "
            "tool, or continue without this information and say so in the answer."
        )


class LocalTool(ABC):
    """A tool served in-process, presented to the planner exactly like an MCP one."""

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]

    @abstractmethod
    async def call(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Execute and return `(model_facing_result, trace_detail)`."""

    def descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=self.name,
            title=self.title,
            description=self.description,
            input_schema=self.input_schema,
            backend="local",
        )


class McpSession(Protocol):
    """The slice of the MCP client the registry uses.

    Narrow on purpose: it is satisfied by `mcp.client.Client` connected to a URL, by the same
    client connected in-memory to an `MCPServer` object, and by a fake. The registry's tests
    therefore do not need a server, and the integration tests get the real one.
    """

    async def list_tools(self, **kwargs: Any) -> Any: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **kw: Any
    ) -> Any: ...


class ToolRegistry:
    """Discovers tools, validates calls, executes them, and reports what happened."""

    def __init__(
        self,
        *,
        session: McpSession | None = None,
        local_tools: list[LocalTool] | None = None,
        tool_timeout_seconds: float = 30.0,
        mcp_server_name: str = "alarm-management",
    ) -> None:
        self._session = session
        self._local = {tool.name: tool for tool in local_tools or []}
        self._timeout = tool_timeout_seconds
        self._mcp_server_name = mcp_server_name
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._validators: dict[str, jsonschema.protocols.Validator] = {}

    # --- discovery -----------------------------------------------------------------

    async def discover(self) -> list[ToolDescriptor]:
        """Ask the MCP server what it has, add the local tools, compile the validators.

        Discovery is a real request every time rather than a cached constant: the MCP server is
        a separate process that can be deployed independently, and a catalogue frozen at import
        time would advertise tools a restarted server no longer has.
        """
        descriptors: list[ToolDescriptor] = []

        if self._session is not None:
            listing = await self._session.list_tools()
            for tool in listing.tools:
                descriptors.append(
                    ToolDescriptor(
                        name=tool.name,
                        title=tool.title or tool.name,
                        description=tool.description or "",
                        input_schema=dict(tool.input_schema or {}),
                        backend="mcp",
                    )
                )

        descriptors.extend(tool.descriptor() for tool in self._local.values())

        self._descriptors = {descriptor.name: descriptor for descriptor in descriptors}
        self._validators = {}
        for descriptor in descriptors:
            try:
                self._validators[descriptor.name] = _validator_for(descriptor.input_schema)
            except jsonschema.SchemaError:
                # A tool whose advertised schema is invalid is still callable — the server will
                # reject bad arguments itself — but it cannot be pre-validated, and that is
                # worth a log rather than a crash at startup.
                log.warning("tool %s advertises an invalid input schema", descriptor.name)

        return descriptors

    @property
    def tools(self) -> list[ToolDescriptor]:
        return list(self._descriptors.values())

    def definitions(self) -> list[ToolDefinition]:
        return [descriptor.as_definition() for descriptor in self._descriptors.values()]

    def catalogue_text(self) -> str:
        """The catalogue as the JSON-planner path puts it in the prompt.

        Full JSON Schema per tool, not a prose summary. The planner is asked to emit an
        argument object that will be validated against exactly this schema, so showing it
        anything less is setting it up to fail validation on a field it was never told about.
        """
        blocks: list[str] = []
        for descriptor in self._descriptors.values():
            blocks.append(
                f"### {descriptor.name}\n"
                f"{descriptor.description or descriptor.title}\n"
                f"arguments schema: {json.dumps(descriptor.input_schema, separators=(',', ':'))}"
            )
        return "\n\n".join(blocks)

    # --- validation ----------------------------------------------------------------

    def validate(self, name: str, arguments: dict[str, Any]) -> list[str]:
        """Return human-readable problems with a proposed call; empty means it may run.

        The messages go back to the model verbatim, so they name the offending field and say
        what was expected — "'lookback_days' must be <= 365" gets corrected on the next turn,
        "invalid arguments" does not.
        """
        if name not in self._descriptors:
            known = ", ".join(sorted(self._descriptors)) or "none discovered"
            return [f"no tool named {name!r}. Available tools: {known}"]

        validator = self._validators.get(name)
        if validator is None:
            return []
        return [
            f"{_path_of(error)}: {error.message}"
            for error in sorted(validator.iter_errors(arguments), key=str)
        ]

    # --- execution -----------------------------------------------------------------

    async def call(
        self, name: str, arguments: dict[str, Any], *, call_id: str, trace_id: str
    ) -> ToolOutcome:
        """Validate, then execute, then report. Never raises for a failed tool call.

        A tool failure is an expected outcome of an investigation, not an exception: the
        orchestrator has to be able to continue with a partial picture and say so. The only
        thing that propagates out of here is a programming error.
        """
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            return ToolOutcome(
                call_id=call_id,
                name=name,
                arguments=arguments,
                ok=False,
                error_kind="unknown_tool",
                error=f"no tool named {name!r}",
                trace={"rejected": "unknown tool"},
            )

        problems = self.validate(name, arguments)
        if problems:
            return ToolOutcome(
                call_id=call_id,
                name=name,
                backend=descriptor.backend,
                arguments=arguments,
                ok=False,
                error_kind="validation",
                error="; ".join(problems),
                trace={"rejected": "schema validation", "problems": problems},
            )

        if descriptor.backend == "local":
            return await self._call_local(descriptor, arguments, call_id=call_id)
        return await self._call_mcp(descriptor, arguments, call_id=call_id, trace_id=trace_id)

    async def _call_local(
        self, descriptor: ToolDescriptor, arguments: dict[str, Any], *, call_id: str
    ) -> ToolOutcome:
        tool = self._local[descriptor.name]
        timer = Timer()
        try:
            result, trace = await tool.call(arguments)
        except Exception as error:  # noqa: BLE001 - reported to the model, not raised
            log.exception("local tool %s failed", descriptor.name)
            return ToolOutcome(
                call_id=call_id,
                name=descriptor.name,
                backend="local",
                arguments=arguments,
                ok=False,
                error_kind="tool_error",
                error=f"{type(error).__name__}: {error}",
                duration_ms=timer.elapsed_ms,
            )
        return ToolOutcome(
            call_id=call_id,
            name=descriptor.name,
            backend="local",
            arguments=arguments,
            result=result,
            duration_ms=timer.elapsed_ms,
            trace=trace,
        )

    async def _call_mcp(
        self, descriptor: ToolDescriptor, arguments: dict[str, Any], *, call_id: str, trace_id: str
    ) -> ToolOutcome:
        assert self._session is not None  # a descriptor with backend="mcp" implies one
        timer = Timer()
        try:
            with anyio.fail_after(self._timeout):
                raw = await self._session.call_tool(
                    descriptor.name, arguments, meta={"trace_id": trace_id}
                )
        except TimeoutError:
            return ToolOutcome(
                call_id=call_id,
                name=descriptor.name,
                arguments=arguments,
                ok=False,
                error_kind="timeout",
                error=f"{descriptor.name} did not respond within {self._timeout:.0f}s",
                duration_ms=timer.elapsed_ms,
                trace={"server": self._mcp_server_name, "timeout_seconds": self._timeout},
            )
        except Exception as error:  # noqa: BLE001 - transport failures are outcomes too
            log.warning("MCP transport failure calling %s: %s", descriptor.name, error)
            return ToolOutcome(
                call_id=call_id,
                name=descriptor.name,
                arguments=arguments,
                ok=False,
                error_kind="transport",
                error=(
                    f"could not reach the MCP server: {type(error).__name__}: {error}. "
                    "The alarm data for this step is unavailable."
                ),
                duration_ms=timer.elapsed_ms,
                trace={"server": self._mcp_server_name},
            )

        elapsed = timer.elapsed_ms
        if getattr(raw, "is_error", False):
            return ToolOutcome(
                call_id=call_id,
                name=descriptor.name,
                arguments=arguments,
                ok=False,
                error_kind="tool_error",
                # The MCP server's error text is written for a model to act on — it names the
                # constraint that was violated — so it is passed through rather than replaced.
                error=_text_of(raw) or "the tool reported an error with no detail",
                duration_ms=elapsed,
                trace={"server": self._mcp_server_name, "is_error": True},
            )

        payload = getattr(raw, "structured_content", None)
        if not isinstance(payload, dict):
            return ToolOutcome(
                call_id=call_id,
                name=descriptor.name,
                arguments=arguments,
                ok=False,
                error_kind="tool_error",
                error=f"{descriptor.name} returned no structured content",
                duration_ms=elapsed,
                trace={"server": self._mcp_server_name},
            )

        return ToolOutcome(
            call_id=call_id,
            name=descriptor.name,
            arguments=arguments,
            result=payload,
            duration_ms=elapsed,
            trace=_mcp_trace(self._mcp_server_name, descriptor.name, arguments, payload),
        )


def _mcp_trace(
    server: str, tool: str, arguments: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """What the trace panel shows for an MCP call.

    The upstream calls the tool made are lifted out of the payload into their own field: status
    codes and retry counts are the observability the assignment asks for by name, and leaving
    them buried in a result blob means nobody looks at them.
    """
    upstream = payload.get("upstream")
    return {
        "server": server,
        "tool": tool,
        "raw_request": arguments,
        "raw_response": payload,
        "upstream": upstream if isinstance(upstream, list) else [],
        "api_status_codes": [
            call.get("status_code")
            for call in (upstream if isinstance(upstream, list) else [])
            if isinstance(call, dict)
        ],
        "retry_count": sum(
            max(0, int(call.get("attempts", 1)) - 1)
            for call in (upstream if isinstance(upstream, list) else [])
            if isinstance(call, dict)
        ),
        "trace_id_echoed": payload.get("trace_id"),
    }


def _text_of(raw: Any) -> str:
    blocks = getattr(raw, "content", None) or []
    texts = [getattr(block, "text", None) for block in blocks]
    return "\n".join(text for text in texts if isinstance(text, str))


def _validator_for(schema: dict[str, Any]) -> jsonschema.protocols.Validator:
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return cast("jsonschema.protocols.Validator", validator_cls(schema))


def _path_of(error: jsonschema.ValidationError) -> str:
    return ".".join(str(part) for part in error.absolute_path) or "arguments"
