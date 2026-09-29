"""The one catalogue: discovery, the validation boundary, and how failures are classified.

Two things here are worth more than the rest.

*The merge.* A test that MCP tools and the local retrieval tool come back from one `discover()`
with one shape is the test that the assignment's "one workflow" requirement is structural. If
this ever splits into two lists, the planner gets two catalogues and the red flag the brief
names — MCP and RAG demonstrated separately — is back.

*The classification.* `registry.call` must never raise. Every way a tool can fail is asserted
to come back as a `ToolOutcome` the orchestrator can put in front of the model, because the
alternative is an investigation that dies on the first empty window.

The session here is a fake rather than a real MCP server: these are the registry's own rules,
and `tests/integration/` drives the same code against the real one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from apps.backend.orchestration.registry import LocalTool, ToolRegistry


def _tool(name: str, schema: dict[str, Any] | None = None, **kw: Any) -> SimpleNamespace:
    """An MCP `Tool` as the registry reads it — name, title, description, input_schema."""
    return SimpleNamespace(
        name=name,
        title=kw.get("title", ""),
        description=kw.get("description", f"{name} description"),
        input_schema=schema if schema is not None else {"type": "object"},
    )


def _result(
    *,
    structured: dict[str, Any] | None = None,
    text: str = "",
    is_error: bool = False,
) -> SimpleNamespace:
    """A `CallToolResult` as the registry reads it."""
    return SimpleNamespace(
        structured_content=structured,
        content=[SimpleNamespace(text=text)] if text else [],
        is_error=is_error,
    )


class FakeSession:
    """The two-method slice of the MCP client the registry declares it needs."""

    def __init__(self, tools: list[SimpleNamespace], results: dict[str, Any] | None = None) -> None:
        self._tools = tools
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []

    async def list_tools(self, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kw: Any) -> Any:
        self.calls.append((name, arguments or {}, kw.get("meta")))
        outcome = self._results.get(name, _result(structured={}))
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return await outcome()
        return outcome


class EchoTool(LocalTool):
    name = "search_procedures"
    title = "Search procedure documents"
    description = "Local retrieval, presented exactly like an MCP tool."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 3}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def call(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls.append(arguments)
        if self._error is not None:
            raise self._error
        return {"passages": [{"reference": "OP-BFP-101 §4.2"}]}, {"chunks": ["chk-1"]}


ALARM_TOOLS = [
    _tool(
        "search_assets",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "maximum": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    _tool("get_alarms"),
]


async def _discovered(session: FakeSession | None = None, **kw: Any) -> ToolRegistry:
    registry = ToolRegistry(session=session, **kw)
    await registry.discover()
    return registry


class TestDiscovery:
    async def test_mcp_tools_and_the_local_tool_form_one_catalogue(self) -> None:
        registry = await _discovered(FakeSession(ALARM_TOOLS), local_tools=[EchoTool()])

        names = {tool.name for tool in registry.tools}
        backends = {tool.name: tool.backend for tool in registry.tools}

        # The requirement made structural: the planner is handed one list, so there is no path
        # on which it can reach the alarm tools without also being able to reach the documents.
        assert names == {"search_assets", "get_alarms", "search_procedures"}
        assert backends == {
            "search_assets": "mcp",
            "get_alarms": "mcp",
            "search_procedures": "local",
        }

    async def test_the_local_tool_alone_is_a_valid_catalogue(self) -> None:
        # The degraded path: MCP unreachable, documents still answerable.
        registry = await _discovered(None, local_tools=[EchoTool()])

        assert [tool.name for tool in registry.tools] == ["search_procedures"]

    async def test_the_prompt_catalogue_carries_each_schema_verbatim(self) -> None:
        registry = await _discovered(FakeSession(ALARM_TOOLS), local_tools=[EchoTool()])

        text = registry.catalogue_text()

        # The planner is asked for arguments that will be validated against exactly this schema,
        # so a prose summary here guarantees rejections on fields it was never shown.
        assert "### search_assets" in text
        assert '"maximum":50' in text
        assert "### search_procedures" in text
        assert '"required":["query"]' in text

    async def test_discovery_is_re_read_rather_than_cached(self) -> None:
        session = FakeSession(ALARM_TOOLS)
        registry = await _discovered(session)

        session._tools = [_tool("get_alarms")]
        await registry.discover()

        # The MCP server is a separately deployed process. A catalogue frozen at first contact
        # would advertise tools a restarted server no longer has.
        assert [tool.name for tool in registry.tools] == ["get_alarms"]

    async def test_a_tool_advertising_a_broken_schema_is_still_callable(self) -> None:
        session = FakeSession([_tool("odd", {"type": "not-a-type"})])
        registry = await _discovered(session)

        # Unvalidatable, not unusable: the server rejects bad arguments itself, and refusing to
        # start over one malformed advertisement would take the whole catalogue down.
        assert registry.validate("odd", {"anything": 1}) == []
        outcome = await registry.call("odd", {}, call_id="c1", trace_id="trc-1")
        assert outcome.ok


class TestValidation:
    async def test_a_tool_that_does_not_exist_is_refused_with_the_alternatives(self) -> None:
        registry = await _discovered(FakeSession(ALARM_TOOLS))

        problems = registry.validate("drop_table", {})

        # The message goes back to the model verbatim, so it names what *is* available; the
        # allowlist is the discovered catalogue itself and so cannot drift from reality.
        assert len(problems) == 1
        assert "drop_table" in problems[0]
        assert "get_alarms" in problems[0] and "search_assets" in problems[0]

    async def test_a_schema_violation_names_the_offending_field(self) -> None:
        registry = await _discovered(FakeSession(ALARM_TOOLS))

        problems = registry.validate("search_assets", {"query": "pump", "limit": 5000})

        assert any("limit" in problem for problem in problems)

    async def test_an_invented_argument_is_refused(self) -> None:
        registry = await _discovered(FakeSession(ALARM_TOOLS))

        problems = registry.validate("search_assets", {"query": "pump", "exec": "rm -rf /"})

        # `additionalProperties: false` is the property that matters for injection: by this point
        # the conversation contains text retrieved from documents, and a parameter it named would
        # otherwise be forwarded to the MCP server.
        assert problems
        assert any("exec" in problem for problem in problems)

    async def test_invalid_arguments_never_reach_the_server(self) -> None:
        session = FakeSession(ALARM_TOOLS)
        registry = await _discovered(session)

        outcome = await registry.call(
            "search_assets", {"limit": 5000}, call_id="c1", trace_id="trc-1"
        )

        assert not outcome.ok
        assert outcome.error_kind == "validation"
        # The second enforcement point, after the planner's. One that a future caller could
        # bypass would not be a trust boundary.
        assert session.calls == []

    async def test_an_unknown_tool_is_an_outcome_rather_than_an_exception(self) -> None:
        registry = await _discovered(FakeSession(ALARM_TOOLS))

        outcome = await registry.call("delete_plant", {}, call_id="c1", trace_id="trc-1")

        assert not outcome.ok
        assert outcome.error_kind == "unknown_tool"
        assert "delete_plant" in (outcome.error or "")


class TestMcpExecution:
    async def test_a_successful_call_returns_the_structured_content(self) -> None:
        session = FakeSession(
            ALARM_TOOLS, {"get_alarms": _result(structured={"data": [{"alarm_id": "ALM-1"}]})}
        )
        registry = await _discovered(session)

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")

        assert outcome.ok
        assert outcome.result == {"data": [{"alarm_id": "ALM-1"}]}
        assert outcome.backend == "mcp"

    async def test_the_trace_id_is_propagated_to_the_server(self) -> None:
        session = FakeSession(ALARM_TOOLS)
        registry = await _discovered(session)

        await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-abc")

        # This is the id that makes a row in the trace panel findable in the alarm API's own log.
        # Without it the panel is a story with no way to check it.
        assert session.calls[0][2] == {"trace_id": "trc-abc"}

    async def test_upstream_http_detail_is_lifted_where_the_panel_can_show_it(self) -> None:
        session = FakeSession(
            ALARM_TOOLS,
            {
                "get_alarms": _result(
                    structured={
                        "data": [],
                        "trace_id": "trc-abc",
                        "upstream": [
                            {"status_code": 503, "attempts": 3},
                            {"status_code": 200, "attempts": 1},
                        ],
                    }
                )
            },
        )
        registry = await _discovered(session)

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-abc")

        # Status codes and retry counts are observability the brief names explicitly. Left buried
        # in the result blob, nobody looks at them.
        assert outcome.trace["api_status_codes"] == [503, 200]
        assert outcome.trace["retry_count"] == 2
        assert outcome.trace["trace_id_echoed"] == "trc-abc"
        assert outcome.trace["raw_request"] == {}
        assert outcome.trace["server"] == "alarm-management"

    async def test_a_tool_error_is_reported_with_the_servers_own_wording(self) -> None:
        session = FakeSession(
            ALARM_TOOLS,
            {"get_alarms": _result(text="asset AST-NOPE does not exist", is_error=True)},
        )
        registry = await _discovered(session)

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")

        assert not outcome.ok
        assert outcome.error_kind == "tool_error"
        # Passed through, not replaced: the MCP server's text names the constraint that was
        # violated, which is exactly what the model needs in order to correct the next call.
        assert outcome.error == "asset AST-NOPE does not exist"

    async def test_an_error_with_no_text_still_says_something(self) -> None:
        session = FakeSession(ALARM_TOOLS, {"get_alarms": _result(is_error=True)})
        registry = await _discovered(session)

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")

        assert not outcome.ok
        assert outcome.error == "the tool reported an error with no detail"

    async def test_a_reply_with_no_structured_content_is_a_failure_not_an_empty_result(
        self,
    ) -> None:
        session = FakeSession(ALARM_TOOLS, {"get_alarms": _result(text="here you go")})
        registry = await _discovered(session)

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")

        # Treating this as `{}` would hand the model an empty alarm list, and it would then
        # report "no alarms in the window" — a wrong finding, which is worse than a failed call.
        assert not outcome.ok
        assert outcome.error_kind == "tool_error"
        assert "no structured content" in (outcome.error or "")

    async def test_a_hanging_server_times_out_and_reports_the_limit(self) -> None:
        async def never() -> Any:
            await anyio.sleep(30)
            return _result(structured={})  # pragma: no cover - the timeout fires first

        session = FakeSession(ALARM_TOOLS, {"get_alarms": never})
        registry = await _discovered(session, tool_timeout_seconds=0.05)

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")

        assert not outcome.ok
        assert outcome.error_kind == "timeout"
        assert outcome.trace["timeout_seconds"] == 0.05

    async def test_a_transport_failure_says_the_data_is_unavailable(self) -> None:
        session = FakeSession(ALARM_TOOLS, {"get_alarms": ConnectionError("connection refused")})
        registry = await _discovered(session)

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")

        assert not outcome.ok
        assert outcome.error_kind == "transport"
        assert "connection refused" in (outcome.error or "")
        # The model is told the consequence, not just the exception, so it writes the answer
        # around the gap instead of asserting from the data it never received.
        assert "unavailable" in (outcome.error or "")

    @pytest.mark.parametrize(
        "failure",
        [
            ConnectionError("refused"),
            RuntimeError("session closed"),
            ValueError("garbage frame"),
        ],
    )
    async def test_no_failure_mode_escapes_as_an_exception(self, failure: Exception) -> None:
        session = FakeSession(ALARM_TOOLS, {"get_alarms": failure})
        registry = await _discovered(session)

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")

        assert not outcome.ok


class TestLocalExecution:
    async def test_the_local_tool_returns_a_model_result_and_a_separate_trace(self) -> None:
        tool = EchoTool()
        registry = await _discovered(FakeSession(ALARM_TOOLS), local_tools=[tool])

        outcome = await registry.call(
            "search_procedures", {"query": "bearing lubrication"}, call_id="c1", trace_id="trc-1"
        )

        assert outcome.ok
        assert outcome.backend == "local"
        assert outcome.result == {"passages": [{"reference": "OP-BFP-101 §4.2"}]}
        # Separate payloads, built from different fields rather than by filtering one — which is
        # what keeps document text out of the trace mechanically.
        assert outcome.trace == {"chunks": ["chk-1"]}

    async def test_a_crashing_local_tool_is_reported_like_any_other_failure(self) -> None:
        registry = await _discovered(
            FakeSession(ALARM_TOOLS), local_tools=[EchoTool(error=RuntimeError("index locked"))]
        )

        outcome = await registry.call(
            "search_procedures", {"query": "bearing"}, call_id="c1", trace_id="trc-1"
        )

        assert not outcome.ok
        assert outcome.error_kind == "tool_error"
        # The type is named: "RuntimeError: index locked" is debuggable from the trace panel,
        # "the tool failed" sends someone to the logs of three processes.
        assert outcome.error == "RuntimeError: index locked"


class TestOutcomeText:
    async def test_a_success_is_handed_to_the_model_as_json(self) -> None:
        registry = await _discovered(
            FakeSession(ALARM_TOOLS, {"get_alarms": _result(structured={"total": 14})})
        )

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")

        assert '"total": 14' in outcome.as_model_text()

    async def test_a_failure_is_reported_in_full_with_what_to_do_next(self) -> None:
        registry = await _discovered(
            FakeSession(ALARM_TOOLS, {"get_alarms": _result(text="window is empty", is_error=True)})
        )

        outcome = await registry.call("get_alarms", {}, call_id="c1", trace_id="trc-1")
        text = outcome.as_model_text()

        # Hiding the failure would get an answer written as though the call had succeeded; and
        # without the instruction, models retry the identical call until the step ceiling.
        assert "window is empty" in text
        assert "Do not retry it with the same arguments" in text
