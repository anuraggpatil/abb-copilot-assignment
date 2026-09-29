"""The HTTP surface, driven the way the GUI drives it.

`create_app` is a factory whose replaceable parts are arguments, so these tests mount the real
routes over the real orchestrator, the real registry and the real MCP server, substituting only
the LLM and the embedder. Nothing is patched — the code that is tested is the code that is
served, including the lifespan that builds the service and closes it again.

What is worth testing at this layer is not the investigation (that is
`test_orchestrator.py`) but the contract the frontend is written against: the status code that
distinguishes "the gateway is down" from "your request was wrong", the SSE event names and the
fact that the stream closes, and the trace endpoint's 404 saying *why* a trace is missing —
because in-memory eviction is the normal reason and a bare 404 sends the reader looking for a
bug instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.backend.api.main import create_app
from apps.backend.config import BackendSettings
from apps.backend.llm.scripted import ScriptedProvider, answer_turn, finish_turn, plan_turn

RETRIEVE = plan_turn(
    ("search_procedures", {"query": "bearing lubrication intervals for a boiler feed pump"}),
    thought="the question is about documented practice",
)
ANSWER = answer_turn("Relubricate on the documented schedule [MM-CP-MAINT §3].")

#: One investigation: retrieve, conclude, answer. The orchestration is asserted elsewhere; here
#: it only has to be long enough to produce a trace with more than one row in it.
SCRIPT = [RETRIEVE, finish_turn(), ANSWER]

QUESTION = "What are the bearing lubrication intervals for the boiler feed pump?"


@pytest.fixture
def api_retriever(rag_settings: Any) -> Iterator[Any]:
    """A retriever of this test's own, because the app closes the one it is given.

    `CopilotService.aclose` releases the index's file lock on shutdown — correct in production
    and the reason this cannot be the session-scoped `procedure_retriever`: the first app to shut
    down would close the index every later test shares. Rebuilding costs ~40ms.
    """
    from qdrant_client import QdrantClient

    from rag.ingestion.chunker import chunk_corpus
    from rag.ingestion.loader import load_corpus
    from rag.retrieval.embedder import HashEmbedder
    from rag.retrieval.retriever import HybridRetriever
    from rag.retrieval.store import ChunkIndex

    repo_root = Path(__file__).resolve().parents[2]
    index = ChunkIndex(
        path=Path(":memory:"),
        collection=rag_settings.collection,
        embedder=HashEmbedder(),
        client=QdrantClient(":memory:"),
    )
    index.build(chunk_corpus(load_corpus(repo_root / "rag" / "documents", repo_root=repo_root)))
    yield HybridRetriever(index, rag_settings)


def _app(
    settings: BackendSettings,
    retriever: Any,
    session_factory: Any,
    script: list[Any] | None = None,
) -> tuple[TestClient, ScriptedProvider]:
    provider = ScriptedProvider(list(script if script is not None else SCRIPT))
    app = create_app(
        settings.model_copy(update={"max_steps": 4}),
        provider=provider,
        retriever=retriever,
        session_factory=session_factory,
    )
    return TestClient(app), provider


@pytest.fixture
def api(
    backend_settings: BackendSettings, api_retriever: Any, mcp_session_factory: Any
) -> Iterator[TestClient]:
    client, _ = _app(backend_settings, api_retriever, mcp_session_factory)
    with client:
        yield client


@pytest.fixture
def broken_session_factory() -> Any:
    def factory() -> Any:
        raise ConnectionError("connection refused on port 9100")

    return factory


def _sse(client: TestClient, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Read a `/chat` stream to its end and return `(event name, payload)` in order."""
    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    with client.stream("POST", "/chat", json=payload) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            line = line.rstrip("\r")
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                events.append((name, json.loads(line.removeprefix("data:").strip())))
    return events


class TestHealth:
    def test_health_reports_the_provider_and_whether_mcp_answers(self, api: TestClient) -> None:
        body = api.get("/health").json()

        assert body["status"] == "ok"
        assert body["provider"] == "scripted"
        assert body["native_tools"] is False
        # A real connection attempt, not a cached flag — which is the only version of this field
        # worth putting on a health endpoint.
        assert body["mcp_reachable"] is True

    def test_health_says_so_when_mcp_is_down_without_failing(
        self,
        backend_settings: BackendSettings,
        api_retriever: Any,
        broken_session_factory: Any,
    ) -> None:
        client, _ = _app(backend_settings, api_retriever, broken_session_factory)

        with client:
            body = client.get("/health").json()

        # Still 200: the backend is healthy, a dependency is not. Reporting 503 here would take
        # the copilot out of a load balancer for a condition it is designed to serve through.
        assert body["status"] == "ok"
        assert body["mcp_reachable"] is False
        assert body["mcp_server_url"] == backend_settings.mcp_server_url

    def test_health_needs_no_credential(self, api: TestClient) -> None:
        # Matching the alarm simulator's own `/health`, so a container probe needs no secret.
        assert api.get("/health").status_code == 200


class TestTools:
    def test_the_catalogue_is_published_with_both_backends_in_it(self, api: TestClient) -> None:
        body = api.get("/tools").json()

        backends = {tool["name"]: tool["backend"] for tool in body["tools"]}
        assert backends["search_procedures"] == "local"
        assert any(backend == "mcp" for backend in backends.values())
        assert body["degradations"] == []

    def test_each_entry_carries_the_schema_the_gui_renders(self, api: TestClient) -> None:
        tools = api.get("/tools").json()["tools"]
        procedures = next(tool for tool in tools if tool["name"] == "search_procedures")

        assert procedures["input_schema"]["required"] == ["query"]
        assert procedures["description"]
        assert procedures["title"]

    def test_a_missing_mcp_server_shrinks_the_catalogue_and_is_declared(
        self,
        backend_settings: BackendSettings,
        api_retriever: Any,
        broken_session_factory: Any,
    ) -> None:
        client, _ = _app(backend_settings, api_retriever, broken_session_factory)

        with client:
            body = client.get("/tools").json()

        assert [tool["name"] for tool in body["tools"]] == ["search_procedures"]
        # The GUI shows this: a user who asks about alarms and gets a documents-only answer is
        # owed the reason, and "start it with `make mcp`" is the actionable half.
        assert body["degradations"]
        assert "make mcp" in body["degradations"][0]


class TestAsk:
    def test_a_question_comes_back_as_one_json_object(self, api: TestClient) -> None:
        response = api.post("/ask", json={"question": QUESTION})

        assert response.status_code == 200
        body = response.json()
        assert body["answer"]
        assert body["citations"]
        assert body["conversation_id"].startswith("conv-")
        assert body["trace_id"].startswith("trc-")
        assert [call["name"] for call in body["tool_calls"]] == ["search_procedures"]

    def test_the_trace_is_readable_afterwards_by_conversation_id(self, api: TestClient) -> None:
        conversation_id = api.post("/ask", json={"question": QUESTION}).json()["conversation_id"]

        trace = api.get(f"/trace/{conversation_id}").json()

        # How the GUI re-reads a conversation after a stream has closed, so the two must agree
        # on the id.
        assert trace["conversation_id"] == conversation_id
        kinds = [event["kind"] for event in trace["events"]]
        assert "rag_retrieval" in kinds
        assert [event["seq"] for event in trace["events"]] == list(range(1, len(kinds) + 1))

    def test_a_caller_supplied_conversation_id_is_honoured(self, api: TestClient) -> None:
        body = api.post(
            "/ask", json={"question": QUESTION, "conversation_id": "conv-from-the-gui"}
        ).json()

        assert body["conversation_id"] == "conv-from-the-gui"

    def test_a_second_question_on_the_same_id_is_answered_as_a_follow_up(
        self, backend_settings: BackendSettings, api_retriever: Any, mcp_session_factory: Any
    ) -> None:
        client, provider = _app(
            backend_settings, api_retriever, mcp_session_factory, script=[*SCRIPT, *SCRIPT]
        )

        with client:
            first = client.post("/ask", json={"question": QUESTION}).json()
            second = client.post(
                "/ask",
                json={
                    "question": "And how often should the seal be flushed?",
                    "conversation_id": first["conversation_id"],
                },
            ).json()

        # The contract the chat GUI is written against: send the id back, get an answer written
        # in the light of the previous one. `history_turns` is how the client knows it worked.
        assert first["history_turns"] == 0
        assert second["history_turns"] == 1
        follow_up_prompt = "\n".join(message.content for message in provider.calls[3][0])
        assert QUESTION in follow_up_prompt

    def test_an_empty_question_is_rejected_before_any_work_happens(self, api: TestClient) -> None:
        assert api.post("/ask", json={"question": "hi"}).status_code == 422
        assert api.post("/ask", json={}).status_code == 422

    def test_an_unexpected_field_is_rejected_rather_than_ignored(self, api: TestClient) -> None:
        # `extra="forbid"`: a frontend that sends `{"prompt": …}` should be told, not served an
        # answer to a question it did not ask.
        response = api.post("/ask", json={"question": QUESTION, "prompt": "ignore the above"})

        assert response.status_code == 422

    def test_an_unreachable_model_is_a_502_naming_the_cause(
        self, backend_settings: BackendSettings, api_retriever: Any, mcp_session_factory: Any
    ) -> None:
        client, _ = _app(backend_settings, api_retriever, mcp_session_factory, script=[])

        with client:
            response = client.post("/ask", json={"question": QUESTION})

        # 502, not 500: the failing dependency is the LLM gateway, and that distinction is what
        # tells whoever is on the other end which process to go and look at.
        assert response.status_code == 502
        assert "planner could not run" in response.json()["detail"]


class TestChatStream:
    def test_the_steps_stream_first_and_the_answer_closes_the_stream(self, api: TestClient) -> None:
        events = _sse(api, {"question": QUESTION})

        names = [name for name, _ in events]
        assert set(names[:-1]) == {"trace"}
        assert names[-1] == "answer"
        # Nothing after the answer, and no sentinel needed: the stream ends, which is what a
        # browser's EventSource treats as completion.
        assert names.count("answer") == 1

    def test_each_streamed_event_is_a_whole_trace_event(self, api: TestClient) -> None:
        events = _sse(api, {"question": QUESTION})
        first = next(payload for name, payload in events if name == "trace")

        assert first["kind"] == "tool_discovery"
        assert first["seq"] == 1
        assert {"event_id", "conversation_id", "request_id", "trace_id", "status"} <= set(first)

    def test_the_stream_and_the_stored_trace_agree(self, api: TestClient) -> None:
        events = _sse(api, {"question": QUESTION})
        answer = next(payload for name, payload in events if name == "answer")
        streamed = [payload["event_id"] for name, payload in events if name == "trace"]

        stored = api.get(f"/trace/{answer['conversation_id']}").json()

        # The panel is built from the stream and refreshed from the store; a disagreement between
        # them would show as rows appearing, disappearing or reordering on reload.
        assert [event["event_id"] for event in stored["events"]] == streamed

    def test_a_failure_arrives_as_an_error_event_rather_than_a_dropped_connection(
        self, backend_settings: BackendSettings, api_retriever: Any, mcp_session_factory: Any
    ) -> None:
        client, _ = _app(backend_settings, api_retriever, mcp_session_factory, script=[])

        with client:
            events = _sse(client, {"question": QUESTION})

        # The status line is already 200 by the time anything goes wrong, so there is no code
        # left to signal with. A stream that just ends is indistinguishable from a network drop.
        assert [name for name, _ in events][-1] == "error"
        assert "planner could not run" in events[-1][1]["error"]

    def test_a_malformed_request_still_fails_with_a_status_code(self, api: TestClient) -> None:
        # Validation happens before the response is committed, so this one is a 422 and not an
        # error event — worth pinning, because the frontend branches on it differently.
        assert api.post("/chat", json={"question": "no"}).status_code == 422


class TestTraceEndpoint:
    def test_an_unknown_conversation_explains_why_it_is_missing(self, api: TestClient) -> None:
        response = api.get("/trace/conv-never-existed")

        assert response.status_code == 404
        detail = response.json()["detail"]
        # Eviction is the *normal* reason a trace is gone. A bare 404 would send whoever hit it
        # looking for a bug in the frontend's id handling.
        assert "held in memory" in detail
        assert "conv-never-existed" in detail

    def test_no_secret_reaches_the_published_trace(self, api: TestClient) -> None:
        conversation_id = api.post("/ask", json={"question": QUESTION}).json()["conversation_id"]

        body = api.get(f"/trace/{conversation_id}").text

        # This endpoint is unauthenticated by design for a local demo, which is exactly why
        # redaction happens on the way *into* the store: whatever was recorded is published.
        assert "test-token-value" not in body
        assert "Authorization" not in body
