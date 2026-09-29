"""The mandatory acceptance scenario, end to end over HTTP.

> Investigate recurring high-severity alarms for Boiler Feed Pump 101 over the last 90 days,
> identify likely contributing factors, retrieve the relevant operating procedure, and provide
> recommended actions with source evidence.

This is the one test that has to pass. It runs the whole system in one process — alarm API,
connector, MCP server over a real MCP client, hybrid retrieval over the authored corpus,
orchestrator, and the HTTP surface — and drives it the way the browser does: `POST /chat`, read
the SSE stream, then `GET /trace/{conversation_id}`.

**Why it duplicates some of `tests/integration/test_orchestrator.py` on purpose.** Those tests
call `CopilotService` directly and assert the orchestration. This one asserts the *delivery*: that
the events reach a client with the names and fields the GUI is written against, that the stream
terminates, that the trace survives the request, and that the answer a browser receives carries
checkable evidence. A bug in the SSE layer — a renamed event, a field that serialises as null —
would leave the orchestration tests green and the product broken.

The model is scripted (see `tests/scenario.py`), so this runs in CI with no token and no network,
and its failures mean something changed *here* rather than in a model's mood. The real gateway is
exercised separately by `scripts/live_smoke.py`, which runs the same question for the demo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.backend.api.service import MCP_UNREACHABLE
from tests.conftest import TEST_TOKEN
from tests.scenario import QUESTION

#: The blank line between SSE events, in any of the three spellings the spec permits. Kept in
#: step with `FRAME_BOUNDARY` in `apps/frontend/src/api.ts`, which is the parser this stands in for.
FRAME_BOUNDARY = re.compile(r"\r\n\r\n|\n\n|\r\r")

#: Fields the GUI reads off an answer (`apps/frontend/src/types.ts`). Asserted as a set, so
#: removing one here is a deliberate act rather than something a reviewer has to notice.
ANSWER_FIELDS = {
    "conversation_id",
    "request_id",
    "trace_id",
    "question",
    "answer",
    "citations",
    "caveats",
    "invented_references",
    "low_confidence",
    "steps_used",
    "steps_exhausted",
    "tool_calls",
    "model",
}

TRACE_EVENT_FIELDS = {
    "event_id",
    "conversation_id",
    "request_id",
    "trace_id",
    "seq",
    "kind",
    "name",
    "started_at",
    "duration_ms",
    "status",
    "summary",
    "detail",
}


@dataclass
class Streamed:
    """One `/chat` response, parsed the way the browser parses it."""

    raw: str
    names: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    answer: dict[str, Any] | None = None
    error: str | None = None


def ask(client: TestClient, question: str = QUESTION) -> Streamed:
    """POST the question and read the stream to its end.

    Framed by hand, on the blank line between events, rather than with `iter_lines()` — the same
    rule `apps/frontend/src/api.ts` applies, including the fact that our server separates lines
    with CRLF. Reading it any other way here would leave the browser's parser untested against
    the server's actual framing, which is how the GUI ends up blank on a stream the tests call
    healthy.
    """
    raw = ""
    with client.stream("POST", "/chat", json={"question": question}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for chunk in response.iter_text():
            raw += chunk

    streamed = Streamed(raw=raw)
    for frame in FRAME_BOUNDARY.split(raw):
        name = ""
        data: list[str] = []
        for line in frame.splitlines():
            if line.startswith(":"):
                continue  # a keep-alive comment
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data.append(line.removeprefix("data:").strip())
        if not data:
            continue
        payload = json.loads("\n".join(data))
        streamed.names.append(name)
        if name == "trace":
            streamed.trace.append(payload)
        elif name == "answer":
            streamed.answer = payload
        elif name == "error":
            streamed.error = str(payload.get("error"))
    return streamed


@pytest.fixture
def run(copilot: TestClient) -> Streamed:
    """The acceptance scenario, run once per test that needs it."""
    return ask(copilot)


def rows(streamed: Streamed, kind: str) -> list[dict[str, Any]]:
    return [event for event in streamed.trace if event["kind"] == kind]


class TestTheRequiredWorkflow:
    """One test per step the assignment lists, in the order it lists them."""

    def test_the_asset_name_is_resolved_to_an_id_through_an_mcp_tool(
        self, run: Streamed, bfp101_id: str
    ) -> None:
        search = rows(run, "mcp_tool_call")[0]

        assert search["name"] == "search_assets"
        assert search["status"] == "ok"
        # Resolution happened over MCP, not by the copilot guessing an id or reaching for the
        # alarm API directly. The id below is what every later call is keyed on.
        assert bfp101_id in json.dumps(search["detail"]["raw_response"])

    def test_several_alarm_operations_are_chained_through_mcp_on_that_id(
        self, run: Streamed, bfp101_id: str
    ) -> None:
        calls = rows(run, "mcp_tool_call")

        assert [call["name"] for call in calls] == [
            "search_assets",
            "get_recurring_alarms",
            "get_operator_recommendations",
        ]
        # The id travelled result → conversation → next plan. It was never handed to the script,
        # which is the only way to prove the chaining rather than assume it.
        for call in calls[1:]:
            assert call["detail"]["raw_request"]["asset_id"] == bfp101_id

    def test_the_alarm_pattern_the_scenario_describes_is_actually_found(
        self, run: Streamed
    ) -> None:
        recurring = next(row for row in rows(run, "mcp_tool_call") if "recurring" in row["name"])
        payload = recurring["detail"]["raw_response"]

        # The seeder plants a recurring high-severity cluster on this pump inside the 90-day
        # window. If this is empty the scenario still "works" and says nothing — the failure this
        # guards against is a date-window bug that silently turns the demo into a shrug.
        assert payload["patterns"], "no recurring pattern was found in the last 90 days"
        assert recurring["detail"]["raw_request"]["min_severity"] == "high"
        assert all(
            pattern["max_severity"] in {"high", "critical"} for pattern in payload["patterns"]
        )
        assert any(
            pattern["occurrences"] >= payload["recurrence_threshold"]
            for pattern in payload["patterns"]
        )
        window = datetime.fromisoformat(payload["window_end"]) - datetime.fromisoformat(
            payload["window_start"]
        )
        assert window.days == 90

    def test_the_procedure_is_retrieved_from_the_document_index(self, run: Streamed) -> None:
        retrievals = rows(run, "rag_retrieval")

        assert len(retrievals) == 1
        detail = retrievals[0]["detail"]
        assert detail["chunks"], "retrieval returned no candidates"
        # The operating procedure for this pump is the document the scenario asks for by name.
        assert any("OP-BFP-101" in str(chunk["reference"]) for chunk in detail["chunks"])

    def test_mcp_and_retrieval_served_one_workflow_rather_than_two_demos(
        self, run: Streamed
    ) -> None:
        # The assignment's hard requirement, and a listed red flag if violated. Both backends
        # appear in one conversation because they come from one tool catalogue.
        assert rows(run, "mcp_tool_call")
        assert rows(run, "rag_retrieval")
        conversation_ids = {event["conversation_id"] for event in run.trace}
        assert len(conversation_ids) == 1

        assert run.answer is not None
        backends = {call["backend"] for call in run.answer["tool_calls"]}
        assert backends == {"mcp", "local"}

    def test_the_answer_recommends_actions_and_attributes_them(self, run: Streamed) -> None:
        assert run.answer is not None
        answer = run.answer

        assert answer["answer"].strip()
        cited = [citation for citation in answer["citations"] if citation["cited_in_answer"]]
        assert cited, "the answer cited nothing that was retrieved"

        # A marker is the `DOC §N` prefix of a citation's full `DOC §N Section title` reference.
        # Every marker resolves to a real passage of a real document at a known revision, and
        # every citation flagged as cited is one a marker points at — which together are what
        # make the recommendations checkable by an operator rather than merely plausible.
        markers = set(re.findall(r"\[([^\]]+)\]", answer["answer"]))
        assert markers, "the answer text carries no citation markers"
        references = [citation["reference"] for citation in answer["citations"]]
        for marker in markers:
            assert any(reference.startswith(marker) for reference in references)
        for citation in cited:
            assert any(citation["reference"].startswith(marker) for marker in markers)
            assert citation["quote"]
            assert citation["document"]
            assert citation["revision"]
        assert answer["invented_references"] == []
        assert answer["low_confidence"] is False
        assert answer["steps_exhausted"] is False
        assert answer["caveats"] == []

    def test_a_section_the_api_recommendation_named_is_fetched_by_name(self, run: Streamed) -> None:
        assert run.answer is not None

        # The bridge between the two halves of the workflow: the operator-actions response cites
        # procedure sections, and those exact sections are retrieved rather than approximated.
        # A pinned section carries no relevance, because nothing measured one.
        pinned = [c for c in run.answer["citations"] if c["selected_by"] == "reference"]
        assert pinned, "no section was fetched by the reference the API gave"
        assert all(citation["relevance"] is None for citation in pinned)


class TestTheStreamTheGuiConsumes:
    def test_trace_events_arrive_before_the_answer_and_the_stream_then_ends(
        self, run: Streamed
    ) -> None:
        assert run.names[-1] == "answer"
        assert run.names.count("answer") == 1
        assert set(run.names[:-1]) == {"trace"}
        # Ordered, gap-free sequence numbers: the panel renders in arrival order and must not
        # have to sort or detect a hole.
        assert [event["seq"] for event in run.trace] == list(range(1, len(run.trace) + 1))

    def test_every_field_the_gui_reads_is_present(self, run: Streamed) -> None:
        assert run.answer is not None

        assert set(run.answer) >= ANSWER_FIELDS
        for event in run.trace:
            assert set(event) >= TRACE_EVENT_FIELDS
        for citation in run.answer["citations"]:
            assert {"reference", "document", "revision", "section", "quote", "selected_by"} <= set(
                citation
            )
            assert isinstance(citation["cited_in_answer"], bool)
            assert citation["relevance"] is None or isinstance(citation["relevance"], float)
        for call in run.answer["tool_calls"]:
            assert {"name", "backend", "ok", "duration_ms", "error_kind"} <= set(call)

    def test_an_mcp_row_shows_the_upstream_http_calls_the_tool_made(self, run: Streamed) -> None:
        row = rows(run, "mcp_tool_call")[0]
        detail = row["detail"]

        # What the expandable trace panel renders: the raw request and response, plus one line
        # per HTTP call underneath with its status code and attempt count.
        assert detail["raw_request"] == {"query": "Boiler Feed Pump 101"}
        assert detail["raw_response"]
        assert detail["upstream"]
        assert detail["api_status_codes"] == [200] * len(detail["upstream"])
        assert detail["retry_count"] == 0
        # The id propagated to the API and echoed back, so one panel row ties to one API log line.
        assert detail["trace_id_echoed"] == row["trace_id"]

    def test_the_trace_can_be_read_back_after_the_stream_closes(
        self, copilot: TestClient, run: Streamed
    ) -> None:
        assert run.answer is not None
        conversation_id = run.answer["conversation_id"]

        replay = copilot.get(f"/trace/{conversation_id}").json()

        assert replay["conversation_id"] == conversation_id
        # Identical, not merely similar: the stream is a live view of the stored trace, so a
        # reader who reloads the page sees exactly what they were watching.
        assert [event["event_id"] for event in replay["events"]] == [
            event["event_id"] for event in run.trace
        ]

    def test_the_catalogue_the_gui_shows_names_both_backends(self, copilot: TestClient) -> None:
        body = copilot.get("/tools").json()

        assert body["degradations"] == []
        backends = {tool["backend"] for tool in body["tools"]}
        assert backends == {"mcp", "local"}
        assert {"search_assets", "search_procedures"} <= {t["name"] for t in body["tools"]}

    def test_health_reports_the_provider_and_that_mcp_answers(self, copilot: TestClient) -> None:
        body = copilot.get("/health").json()

        assert body["status"] == "ok"
        assert body["provider"] == "scripted"
        assert body["mcp_reachable"] is True


class TestNothingSensitiveIsPublished:
    def test_the_api_token_appears_nowhere_in_the_stream_or_the_trace(
        self, copilot: TestClient, run: Streamed
    ) -> None:
        assert run.answer is not None
        replay = copilot.get(f"/trace/{run.answer['conversation_id']}").text

        # The MCP server authenticates to the alarm API with a bearer token, and the trace shows
        # raw requests and responses. `redact()` is what keeps those two facts compatible; this
        # is the end-to-end proof that it held for a real investigation.
        assert TEST_TOKEN not in run.raw
        assert TEST_TOKEN not in replay
        assert "Authorization" not in run.raw

    def test_no_whole_document_is_published_through_the_trace(self, run: Streamed) -> None:
        retrieval = rows(run, "rag_retrieval")[0]

        # Ids and scores only. Quotes reach the client through citations, which are bounded;
        # a trace that carried document bodies would be a way around that bound.
        for chunk in retrieval["detail"]["chunks"]:
            assert set(chunk) == {
                "chunk_id",
                "reference",
                "dense",
                "lexical",
                "fused",
                "synthetic_score",
                "pinned",
            }


class TestTheDegradedScenario:
    """The demo's second half: the MCP server is down mid-session."""

    def test_the_copilot_still_answers_and_says_what_was_missing(
        self, copilot_without_mcp: TestClient
    ) -> None:
        streamed = ask(copilot_without_mcp)

        assert streamed.error is None, "a stopped MCP server must not fail the request"
        assert streamed.answer is not None
        assert MCP_UNREACHABLE in streamed.answer["caveats"]
        # Answered from documentation alone, and honest about it, rather than inventing alarm
        # history — the behaviour the whole guard exists for.
        assert {call["backend"] for call in streamed.answer["tool_calls"]} == {"local"}
        assert streamed.answer["citations"]

    def test_the_degradation_is_visible_to_the_gui_before_a_question_is_asked(
        self, copilot_without_mcp: TestClient
    ) -> None:
        health = copilot_without_mcp.get("/health").json()
        catalogue = copilot_without_mcp.get("/tools").json()

        assert health["status"] == "ok"  # the copilot is up; its dependency is not
        assert health["mcp_reachable"] is False
        assert catalogue["degradations"] == [MCP_UNREACHABLE]
        # No alarm tool is offered, so the planner cannot choose one and then fail.
        assert {tool["backend"] for tool in catalogue["tools"]} == {"local"}
