"""The MCP server, driven by a real MCP client, over a real HTTP client, against the real API.

The only thing simulated is the socket: an in-memory MCP session talks to the server, and the
server's connector reaches the FastAPI app through `httpx.ASGITransport`. So a tool call here
exercises the whole path the copilot will use — argument validation, schema generation, trace
propagation, the connector's retry and error mapping, the API's own routing and its error
envelope — and the only thing a network would add is flakiness.

What is asserted falls into four groups, all of them things the copilot depends on and none
of them visible from a unit test of a tool function:

* the **catalogue** a planner reads, since a tool a model cannot understand is a tool it
  cannot use;
* **chaining**, because the assignment's workflow is a sequence and an id that does not
  survive one hop breaks it;
* **failure**, where the requirement is not "an error happened" but "the model was told
  something it can act on, and the token was not in it";
* **traceability**, since the execution trace the GUI renders is built from what these
  results carry.

Each test opens its own `Client(server)` rather than taking a session from a fixture. The
MCP session owns an anyio task group, and pytest-asyncio tears a fixture down in a different
task than it set it up in — which that task group correctly refuses. Keeping the `async with`
in the test body is the price of driving the real client instead of a stub.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from alarm_management.config import McpSettings
from alarm_management.context import TRACE_ID_META_KEY, record_upstream_call
from alarm_management.server import build_server
from alarm_management.tools.alarms import DEFAULT_KPIS
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, RequestParamsMeta, TextContent

from connectors.alarm_api import AlarmApiClient
from tests.conftest import FROZEN_NOW, TEST_TOKEN

BFP101 = "Boiler Feed Pump 101"
EXPECTED_TOOLS = {
    "search_assets",
    "get_alarms",
    "get_alarm_summary",
    "get_recurring_alarms",
    "get_operator_recommendations",
}


def _settings(**overrides: Any) -> McpSettings:
    values: dict[str, Any] = {
        "ALARM_API_BASE_URL": "http://alarm-api.internal",
        "ALARM_API_TOKEN": TEST_TOKEN,
        # Deliberate: every failure these tests provoke is permanent, so a retry budget
        # would only add latency.
        "ALARM_API_MAX_RETRIES": 0,
        "MCP_CLIENT_ID": "alarm-copilot-tests",
    }
    return McpSettings(**(values | overrides))


def _client_factory(
    transport: httpx.AsyncBaseTransport,
) -> Callable[[McpSettings], AlarmApiClient]:
    def build(settings: McpSettings) -> AlarmApiClient:
        return AlarmApiClient(
            settings.alarm_api_base_url,
            settings.alarm_api_token,
            transport=transport,
            max_retries=settings.alarm_api_max_retries,
            client_id=settings.client_id,
            observer=record_upstream_call,
        )

    return build


def _server(transport: httpx.AsyncBaseTransport, **overrides: Any) -> MCPServer:
    return build_server(
        _settings(**overrides),
        client_factory=_client_factory(transport),
        # Frozen so `lookback_days=90` covers exactly the window the seeder planted data in.
        now=lambda: FROZEN_NOW,
    )


@pytest.fixture
def mcp_server(client: TestClient) -> MCPServer:
    """A server wired to the suite's already-seeded, clock-frozen app."""
    return _server(httpx.ASGITransport(app=client.app))


def _meta(trace_id: str) -> RequestParamsMeta:
    """Build the `_meta` a copilot would send, carrying the conversation's trace id.

    `RequestParamsMeta` is a TypedDict that declares only `progress_token` and permits extra
    items — which is how the protocol allows this. The cast is for mypy's benefit, since it
    does not yet read that permission.
    """
    return cast("RequestParamsMeta", {TRACE_ID_META_KEY: trace_id})


def _text(result: CallToolResult) -> str:
    """The text block an LLM would actually read, as opposed to the structured content.

    Asserted on separately because the two are produced by different code paths, and a token
    leaking into one of them is not caught by inspecting the other.
    """
    block = result.content[0]
    assert isinstance(block, TextContent), f"expected a text block, got {type(block).__name__}"
    return block.text


def _error_text(result: Any) -> str:
    """The message a tool-calling model would actually receive."""
    assert result.is_error, "expected the tool call to fail"
    return "\n".join(block.text for block in result.content if block.type == "text")


async def _resolve_bfp101(mcp: Client) -> str:
    result = await mcp.call_tool("search_assets", {"query": BFP101})
    assert not result.is_error
    return str(result.structured_content["assets"][0]["asset_id"])


# --- the catalogue a planner reads -------------------------------------------------------


async def test_the_server_advertises_exactly_the_slice_tools(mcp_server: MCPServer) -> None:
    async with Client(mcp_server) as mcp:
        listed = await mcp.list_tools()
    assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS


async def test_every_tool_is_documented_well_enough_to_be_chosen(mcp_server: MCPServer) -> None:
    """The schema is the only documentation the planner sees; thin descriptions cost recall."""
    async with Client(mcp_server) as mcp:
        listed = await mcp.list_tools()

    for tool in listed.tools:
        assert tool.title, f"{tool.name} has no human-readable title"
        assert tool.description and len(tool.description) > 80, (
            f"{tool.name} needs a description that says when to use it, not just what it is"
        )
        assert tool.output_schema, f"{tool.name} returns an untyped result"
        # Every parameter documented: an undescribed field is one a model has to guess at.
        for name, schema in tool.input_schema["properties"].items():
            assert "description" in schema, f"{tool.name}.{name} has no description"


async def test_the_injected_context_is_not_exposed_as_a_tool_parameter(
    mcp_server: MCPServer,
) -> None:
    """`ctx` is server plumbing; a model offered it would try to fill it in."""
    async with Client(mcp_server) as mcp:
        listed = await mcp.list_tools()
    for tool in listed.tools:
        assert "ctx" not in tool.input_schema["properties"]


async def test_search_assets_requires_a_query_and_nothing_else(mcp_server: MCPServer) -> None:
    async with Client(mcp_server) as mcp:
        listed = await mcp.list_tools()
    tool = next(t for t in listed.tools if t.name == "search_assets")
    assert tool.input_schema["required"] == ["query"]


async def test_the_recommendations_tool_points_at_the_rag_step(mcp_server: MCPServer) -> None:
    """The combined workflow is only discoverable if the catalogue describes the handoff."""
    async with Client(mcp_server) as mcp:
        listed = await mcp.list_tools()
    tool = next(t for t in listed.tools if t.name == "get_operator_recommendations")
    assert tool.description is not None
    assert "search_procedures" in tool.description


async def test_the_server_instructions_state_the_chaining_order(mcp_server: MCPServer) -> None:
    """Instructions carry what holds across tools, which no single schema can say."""
    assert mcp_server.instructions is not None
    for tool in EXPECTED_TOOLS:
        assert tool in mcp_server.instructions


# --- tools return usable, typed results --------------------------------------------------


async def test_search_assets_resolves_a_name_and_includes_the_best_match_metadata(
    mcp_server: MCPServer,
) -> None:
    """One call, not two: resolving a name is never the actual goal."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("search_assets", {"query": BFP101})
    payload = result.structured_content

    assert not result.is_error
    assert payload["assets"][0]["name"] == BFP101
    assert payload["best_match_tag"] == "2-BFP-101"
    assert payload["best_match_criticality"] >= 4
    assert payload["best_match_related_assets"], "neighbours are where a shared cause shows up"
    # Two upstream calls, folded to one entry each.
    assert [call["path"] for call in payload["upstream"]] == [
        "/assets/search",
        f"/assets/{payload['assets'][0]['asset_id']}/metadata",
    ]


async def test_search_assets_reports_an_empty_result_rather_than_inventing_one(
    mcp_server: MCPServer,
) -> None:
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("search_assets", {"query": "zzzz-no-such-equipment"})
    payload = result.structured_content

    assert not result.is_error, "an empty search is an answer, not a failure"
    assert payload["assets"] == []
    assert payload["best_match_tag"] is None
    # The metadata call is skipped; only the search happened.
    assert len(payload["upstream"]) == 1


async def test_get_recurring_alarms_surfaces_the_planted_pattern(mcp_server: MCPServer) -> None:
    """The acceptance scenario's finding: recurring, high-severity, and getting worse."""
    async with Client(mcp_server) as mcp:
        asset_id = await _resolve_bfp101(mcp)
        result = await mcp.call_tool(
            "get_recurring_alarms",
            {"asset_id": asset_id, "min_severity": "high", "lookback_days": 90},
        )
    payload = result.structured_content

    assert not result.is_error
    assert payload["patterns"], "without a recurring pattern there is nothing to investigate"
    assert any(pattern["trend"] == "increasing" for pattern in payload["patterns"])
    top = payload["patterns"][0]
    assert top["occurrences"] >= payload["recurrence_threshold"]
    assert top["occurrences_first_half"] + top["occurrences_second_half"] == top["occurrences"]


async def test_get_alarms_returns_a_bounded_page_over_the_requested_window(
    mcp_server: MCPServer,
) -> None:
    async with Client(mcp_server) as mcp:
        asset_id = await _resolve_bfp101(mcp)
        result = await mcp.call_tool(
            "get_alarms",
            {
                "asset_id": asset_id,
                "severity": ["high", "critical"],
                "lookback_days": 90,
                "limit": 5,
            },
        )
    payload = result.structured_content

    assert not result.is_error
    assert len(payload["alarms"]) <= 5
    assert payload["returned"] == len(payload["alarms"])
    assert payload["total_matching"] >= payload["returned"]
    assert payload["window_end"].startswith("2026-09-29"), "the frozen clock must be honoured"
    assert payload["window_start"].startswith("2026-07-01")
    assert all(alarm["severity"] in {"high", "critical"} for alarm in payload["alarms"])


async def test_get_alarms_reports_when_more_pages_exist(mcp_server: MCPServer) -> None:
    """`has_more` is how a model knows its evidence is a sample rather than the whole set."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("get_alarms", {"lookback_days": 90, "limit": 1})
    assert result.structured_content["has_more"] is True


async def test_get_alarm_summary_groups_and_computes_the_requested_kpis(
    mcp_server: MCPServer,
) -> None:
    async with Client(mcp_server) as mcp:
        asset_id = await _resolve_bfp101(mcp)
        result = await mcp.call_tool(
            "get_alarm_summary",
            {
                "asset_id": asset_id,
                "lookback_days": 90,
                "group_by": ["alarm_name"],
                "kpis": ["alarm_count", "avg_ack_delay"],
            },
        )
    payload = result.structured_content

    assert not result.is_error
    assert payload["group_by"] == ["alarm_name"]
    assert sum(group["alarm_count"] for group in payload["groups"]) == payload["total_alarms"]
    assert set(payload["groups"][0]["kpis"]) == {"alarm_count", "avg_ack_delay"}


async def test_get_alarm_summary_without_kpis_computes_the_default_set(
    mcp_server: MCPServer,
) -> None:
    """Omitting `kpis` must not yield a bare count.

    The GUI's KPI header is assembled from whatever this returns, so a single-count default would
    make the board depend on the model's choice of arguments — the same question could produce one
    tile or eight. All ten KPIs are computed from one already-loaded set of rows upstream, so the
    breadth is free.
    """
    async with Client(mcp_server) as mcp:
        asset_id = await _resolve_bfp101(mcp)
        result = await mcp.call_tool(
            "get_alarm_summary", {"asset_id": asset_id, "lookback_days": 90}
        )
    payload = result.structured_content

    assert not result.is_error
    assert set(payload["overall"]) == set(DEFAULT_KPIS)
    assert payload["overall"]["alarm_count"] == payload["total_alarms"]


async def test_get_operator_recommendations_returns_ranked_actions_and_procedure_references(
    mcp_server: MCPServer,
) -> None:
    """The seam to RAG: these references are the retrieval query's raw material."""
    async with Client(mcp_server) as mcp:
        asset_id = await _resolve_bfp101(mcp)
        result = await mcp.call_tool(
            "get_operator_recommendations", {"asset_id": asset_id, "lookback_days": 90}
        )
    payload = result.structured_content

    assert not result.is_error
    assert payload["asset_name"] == BFP101
    assert [action["rank"] for action in payload["actions"]] == list(
        range(1, len(payload["actions"]) + 1)
    )
    assert payload["procedure_references"]
    assert all("§" in reference for reference in payload["procedure_references"])
    # Every cited section appears in the de-duplicated list the copilot will retrieve.
    for action in payload["actions"]:
        assert action["procedure_reference"] in payload["procedure_references"]
    assert payload["disclaimer"], "advisory scope must travel with the advice"


async def test_an_asset_scoped_recommendation_names_the_alarm_it_chose(
    mcp_server: MCPServer,
) -> None:
    """Asking about a pump must not hide which alarm the ranking was actually about."""
    async with Client(mcp_server) as mcp:
        asset_id = await _resolve_bfp101(mcp)
        result = await mcp.call_tool("get_operator_recommendations", {"asset_id": asset_id})
    payload = result.structured_content
    assert payload["alarm_id"]
    assert payload["alarm_name"]


# --- failures a model has to be able to act on -------------------------------------------


async def test_omitting_both_subjects_says_which_tool_to_call_for_each(
    mcp_server: MCPServer,
) -> None:
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("get_operator_recommendations", {})
    message = _error_text(result)
    assert "alarm_id" in message and "asset_id" in message
    assert "get_alarms" in message and "search_assets" in message


async def test_an_unknown_alarm_id_is_told_how_to_get_a_real_one(mcp_server: MCPServer) -> None:
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "get_operator_recommendations", {"alarm_id": "ALM-does-not-exist"}
        )
    message = _error_text(result)
    assert "search_assets" in message, "a 404 must come with the way to recover from it"


async def test_an_invalid_kpi_comes_back_with_the_list_of_valid_ones(
    mcp_server: MCPServer,
) -> None:
    """The API's message is the repair instruction; the mapping must not swallow it."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "get_alarm_summary", {"kpis": ["mean_time_to_coffee"], "lookback_days": 30}
        )
    message = _error_text(result)
    assert "alarm_count" in message
    assert "get_alarm_summary again" in message


async def test_out_of_range_arguments_are_rejected_before_any_http_call(
    mcp_server: MCPServer,
) -> None:
    """Schema bounds, not the upstream, are the first line of validation."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("get_alarms", {"lookback_days": 9999})
    assert "lookback_days" in _error_text(result)


async def test_an_inverted_window_is_explained_in_terms_of_the_tools_own_parameters(
    mcp_server: MCPServer,
) -> None:
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "get_alarms",
            {"start_time": "2026-09-01T00:00:00Z", "end_time": "2026-08-01T00:00:00Z"},
        )
    message = _error_text(result)
    assert "start_time" in message and "end_time" in message


async def test_an_unreachable_alarm_api_is_reported_as_an_outage_not_as_no_alarms() -> None:
    """The dangerous failure mode, and the one the degraded demo has to show.

    A model told only "0 results" will confidently tell an operator the pump is fine.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with Client(_server(httpx.MockTransport(refuse))) as mcp:
        message = _error_text(await mcp.call_tool("get_alarms", {"lookback_days": 7}))

    assert "unavailable" in message.lower()
    assert "not an empty result" in message


async def test_a_rejected_token_is_reported_as_a_configuration_problem(
    client: TestClient,
) -> None:
    """And without the token in it: this text is returned to the client and logged."""
    server = _server(httpx.ASGITransport(app=client.app), ALARM_API_TOKEN="wrong-token")
    async with Client(server) as mcp:
        message = _error_text(await mcp.call_tool("search_assets", {"query": BFP101}))

    assert "not authorised" in message
    assert "Do not retry" in message
    assert "wrong-token" not in message


async def test_no_successful_result_carries_the_api_token(mcp_server: MCPServer) -> None:
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("search_assets", {"query": BFP101})
    assert TEST_TOKEN not in _text(result)


# --- traceability -----------------------------------------------------------------------


async def test_a_caller_supplied_trace_id_reaches_the_result(mcp_server: MCPServer) -> None:
    """One id spans the GUI, this server and the alarm API's access log."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool(
            "search_assets", {"query": BFP101}, meta=_meta("trc-from-copilot")
        )
    assert result.structured_content["trace_id"] == "trc-from-copilot"


async def test_a_call_without_a_trace_id_still_gets_one(mcp_server: MCPServer) -> None:
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("search_assets", {"query": BFP101})
    trace_id = result.structured_content["trace_id"]
    assert trace_id and trace_id.startswith("mcp-")


async def test_results_report_the_upstream_status_and_attempt_count(
    mcp_server: MCPServer,
) -> None:
    """These fields are what the trace panel's per-call rows are built from."""
    async with Client(mcp_server) as mcp:
        result = await mcp.call_tool("get_alarms", {"lookback_days": 30, "limit": 5})
    calls = result.structured_content["upstream"]

    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/alarms"
    assert calls[0]["status_code"] == 200
    assert calls[0]["attempts"] == 1
    assert calls[0]["error"] is None
    assert calls[0]["duration_ms"] >= 0


async def test_concurrent_tool_calls_do_not_appear_in_each_others_traces(
    mcp_server: MCPServer,
) -> None:
    """The upstream sink is task-scoped; two investigations must not bleed together.

    `search_assets` makes two upstream calls and `get_alarms` makes one, so a shared sink
    would show up immediately as the wrong count.
    """
    async with Client(mcp_server) as mcp:
        search, alarms = await asyncio.gather(
            mcp.call_tool("search_assets", {"query": BFP101}),
            mcp.call_tool("get_alarms", {"lookback_days": 30, "limit": 1}),
        )
    assert len(search.structured_content["upstream"]) == 2
    assert len(alarms.structured_content["upstream"]) == 1


# --- the acceptance scenario, over MCP ---------------------------------------------------


async def test_the_investigation_chains_end_to_end_over_mcp(mcp_server: MCPServer) -> None:
    """Name → asset_id → recurring pattern → events → ranked actions → procedure references.

    Every argument after the first comes out of the previous tool's result, which is what
    makes this the workflow rather than five calls. It ends where RAG begins.
    """
    meta = _meta("trc-acceptance")
    async with Client(mcp_server) as mcp:
        search = await mcp.call_tool("search_assets", {"query": BFP101}, meta=meta)
        asset_id = search.structured_content["assets"][0]["asset_id"]

        recurring = await mcp.call_tool(
            "get_recurring_alarms",
            {"asset_id": asset_id, "min_severity": "high", "lookback_days": 90},
            meta=meta,
        )
        pattern = recurring.structured_content["patterns"][0]

        alarms = await mcp.call_tool(
            "get_alarms",
            {
                "asset_id": asset_id,
                "severity": ["high", "critical"],
                "lookback_days": 90,
                "limit": 10,
            },
            meta=meta,
        )
        names = {alarm["alarm_name"] for alarm in alarms.structured_content["alarms"]}
        assert pattern["alarm_name"] in names, "the pattern must be visible in the events"

        recommendations = await mcp.call_tool(
            "get_operator_recommendations",
            {"alarm_id": alarms.structured_content["alarms"][0]["alarm_id"], "lookback_days": 90},
            meta=meta,
        )

    payload = recommendations.structured_content
    assert payload["asset_id"] == asset_id
    assert payload["actions"]
    assert payload["procedure_references"], "RAG has nothing to retrieve without these"
    assert all(
        step.structured_content["trace_id"] == "trc-acceptance"
        for step in (search, recurring, alarms, recommendations)
    )
