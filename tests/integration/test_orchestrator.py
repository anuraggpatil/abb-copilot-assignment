"""The orchestration loop, driven through the real stack.

Real alarm API, real connector, real MCP server over a real MCP client, real retrieval index
over the real authored corpus. The model is scripted, which is the whole point: these tests
assert what *our* orchestration did — whether an id survived the hop from one tool to the next,
whether a failure came back as data instead of an abort, whether the trace recorded the step —
and not what a language model decided that afternoon.

The acceptance scenario itself — the question and the scripted investigation that answers it —
lives in `tests/scenario.py`, because the end-to-end test drives the same scenario through HTTP
and the two must not drift apart.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from apps.backend.api.service import MCP_UNREACHABLE, CopilotService
from apps.backend.config import BackendSettings
from apps.backend.llm.provider import LLMMessage, LLMResponse
from apps.backend.llm.scripted import (
    ScriptedProvider,
    answer_turn,
    finish_turn,
    plan_turn,
)
from apps.backend.orchestration.orchestrator import CopilotError
from apps.backend.tracing.trace import TraceEvent
from tests.scenario import QUESTION, acceptance_script, conversation_text


def _service(
    settings: BackendSettings,
    provider: ScriptedProvider,
    retriever: Any,
    session_factory: Any,
) -> CopilotService:
    return CopilotService(
        settings=settings,
        provider=provider,
        retriever=retriever,
        session_factory=session_factory,
    )


def _settings(base: BackendSettings, **overrides: Any) -> BackendSettings:
    return base.model_copy(update=overrides)


# --- the acceptance-shaped run ------------------------------------------------------------


@pytest.fixture
def acceptance_service(
    backend_settings: BackendSettings,
    procedure_retriever: Any,
    mcp_session_factory: Any,
) -> tuple[CopilotService, ScriptedProvider]:
    provider = ScriptedProvider(acceptance_script())
    settings = _settings(backend_settings, max_steps=6)
    return _service(settings, provider, procedure_retriever, mcp_session_factory), provider


class TestTheInvestigation:
    async def test_the_chain_runs_end_to_end_and_the_answer_is_attributed(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, provider = acceptance_service

        answer = await service.ask(QUESTION)

        # Every scripted turn was consumed: the loop did not stop early, and nothing was called
        # twice. The ids in the later calls were read out of earlier results.
        assert provider.turns_remaining == 0
        called = [(call.name, call.backend, call.ok) for call in answer.tool_calls]
        assert called == [
            ("search_assets", "mcp", True),
            ("get_recurring_alarms", "mcp", True),
            ("get_operator_recommendations", "mcp", True),
            ("search_procedures", "local", True),
        ]
        assert answer.steps_used == 4
        assert answer.steps_exhausted is False
        assert answer.caveats == []

    async def test_the_asset_id_reached_the_alarm_tools_from_the_search_result(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider], bfp101_id: str
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None
        recurring = next(
            event
            for event in trace.of_kind("mcp_tool_call")
            if event.name == "get_recurring_alarms"
        )

        # The id was never handed to the script. It travelled tool result → conversation →
        # next plan, which is the chaining the acceptance scenario requires.
        assert recurring.detail["arguments"]["asset_id"] == bfp101_id

    async def test_mcp_and_retrieval_both_served_the_same_workflow(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None

        # The assignment's hard requirement, and the red flag if violated: one workflow, both
        # backends. They come from one registry, so this cannot be satisfied by two demos.
        assert len(trace.of_kind("mcp_tool_call")) == 3
        assert len(trace.of_kind("rag_retrieval")) == 1

    async def test_the_answer_carries_checkable_citations(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)

        assert answer.citations
        cited = [citation for citation in answer.citations if citation.cited_in_answer]
        assert cited, "the answer cited sections that are not in the citation list"
        for citation in answer.citations:
            assert citation.quote
            assert citation.document
            assert citation.revision
        assert answer.invented_references == []

    async def test_a_section_the_recommendations_named_is_fetched_by_name(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)

        # `references` pass-through: the exact sections the API cited are retrieved rather than
        # approximated by similarity. A pinned section publishes no relevance score, because
        # nothing measured one.
        pinned = [c for c in answer.citations if c.selected_by == "reference"]
        assert pinned, "no cited section was fetched by name"
        assert all(citation.relevance is None for citation in pinned)

    async def test_the_kpi_board_reports_only_what_the_tools_returned(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)
        board = answer.kpis

        # The asset name and the recurring patterns came over MCP from the real simulator, so
        # this asserts the projection against genuine tool output rather than a fixture.
        assert board.asset_name
        assert board.patterns
        assert all(pattern.occurrences > 0 for pattern in board.patterns)
        assert all(
            pattern.trend in {"increasing", "flat", "decreasing"} for pattern in board.patterns
        )
        assert [pattern.occurrences for pattern in board.patterns] == sorted(
            (pattern.occurrences for pattern in board.patterns), reverse=True
        )
        assert {kpi.key for kpi in board.metrics} == {"recommended_actions", "immediate_actions"}

        # This chain never calls `get_alarm_summary`, so the volume and severity tiles are
        # absent — not zero. A board showing "0 critical" here would be a fabrication.
        assert "get_alarm_summary" not in board.sources
        assert board.total_alarms is None

    async def test_the_model_was_handed_one_catalogue_containing_both_backends(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, provider = acceptance_service

        await service.ask(QUESTION)
        system = "\n".join(
            message.content for message in provider.calls[0][0] if message.role == "system"
        )

        assert "### search_assets" in system
        assert "### get_recurring_alarms" in system
        assert "### search_procedures" in system


class TestTheTrace:
    async def test_every_step_is_recorded_in_order(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None

        assert [event.seq for event in trace.events] == list(range(1, len(trace.events) + 1))
        kinds = [event.kind for event in trace.events]
        assert kinds[0] == "tool_discovery"
        assert kinds[-1] == "synthesis"
        # The panel is meant to let a reader follow the investigation, so discovery comes before
        # the first plan and each tool call sits after the plan that asked for it.
        assert kinds.index("plan") < kinds.index("mcp_tool_call")
        assert kinds.index("rag_retrieval") > kinds.index("mcp_tool_call")

    async def test_an_mcp_row_carries_the_upstream_http_detail(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None
        row = trace.of_kind("mcp_tool_call")[0]

        assert row.detail["raw_request"] == {"query": "Boiler Feed Pump 101"}
        assert row.detail["raw_response"]
        # One row per upstream request the tool made — `search_assets` enriches its hits, so
        # there is more than one, and the panel shows each.
        assert row.detail["api_status_codes"] == [200] * len(row.detail["upstream"])
        assert len(row.detail["upstream"]) >= 1
        assert row.detail["retry_count"] == 0
        # The id that ties this row to a line in the alarm API's own log.
        assert row.detail["trace_id_echoed"] == answer.trace_id

    async def test_a_retrieval_row_carries_scores_and_no_document_text(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None
        row = trace.of_kind("rag_retrieval")[0]

        assert row.detail["chunks"]
        assert all("chunk_id" in chunk and "fused" in chunk for chunk in row.detail["chunks"])
        # The rule stated in docs/: a trace records ids and scores, never a document body. The
        # quotes travel with the citations instead, where they are bounded.
        rendered = json.dumps(row.detail)
        for citation in answer.citations:
            assert citation.quote[:60] not in rendered

    async def test_the_llm_rows_report_the_model_and_the_latency(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None

        calls = trace.of_kind("llm_call")
        assert [call.name for call in calls] == ["planner"] * 4 + ["synthesis"]
        assert all(call.detail["provider"] == "scripted" for call in calls)
        assert all(call.detail["native_tools"] is False for call in calls)

    async def test_the_answer_text_is_not_duplicated_into_the_trace(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None
        row = trace.of_kind("synthesis")[0]

        # It travels to the client as the answer. Duplicating it here doubles the payload for
        # nothing and puts the same text behind an endpoint with different rules.
        assert row.detail["answer_chars"] == len(answer.answer)
        assert answer.answer[:40] not in json.dumps(row.detail)

    async def test_events_stream_to_a_sink_in_the_order_they_are_stored(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service
        streamed: list[TraceEvent] = []

        async def sink(event: TraceEvent) -> None:
            streamed.append(event)

        answer = await service.ask(QUESTION, sink=sink)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None

        # The SSE endpoint is this sink. The order *is* the information the panel conveys, so it
        # has to match what a later `GET /trace` would return.
        assert [event.event_id for event in streamed] == [event.event_id for event in trace.events]

    async def test_a_conversation_id_supplied_by_the_caller_is_used(
        self, acceptance_service: tuple[CopilotService, ScriptedProvider]
    ) -> None:
        service, _ = acceptance_service

        answer = await service.ask(QUESTION, conversation_id="conv-supplied")

        # The GUI re-reads a trace after the stream closes, by this id.
        assert answer.conversation_id == "conv-supplied"
        assert service.trace("conv-supplied") is not None


class TestPartialFailure:
    async def test_a_failed_tool_is_reported_to_the_model_and_the_answer_continues(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        seen: dict[str, str] = {}

        def after_failure(messages: list[LLMMessage]) -> LLMResponse:
            seen["conversation"] = conversation_text(messages)
            return finish_turn()

        provider = ScriptedProvider(
            [
                # Neither alarm_id nor asset_id: the tool itself refuses, which is the realistic
                # shape of a model's mistake.
                plan_turn(("get_operator_recommendations", {}), thought="ask for actions"),
                after_failure,
                answer_turn("No recommendations were available, so no actions are proposed."),
            ]
        )
        service = _service(
            _settings(backend_settings, max_steps=4),
            provider,
            procedure_retriever,
            mcp_session_factory,
        )

        answer = await service.ask("What should the operator do?")

        # The failure is data. An orchestrator that raised here would produce nothing in exactly
        # the situation where an operator most needs whatever is known.
        assert [(call.name, call.ok, call.error_kind) for call in answer.tool_calls] == [
            ("get_operator_recommendations", False, "tool_error")
        ]
        assert "Supply either alarm_id or asset_id" in seen["conversation"]
        assert "Do not retry it with the same arguments" in seen["conversation"]
        assert any("1 tool call(s) failed" in caveat for caveat in answer.caveats)
        assert answer.answer

    async def test_a_tool_the_catalogue_does_not_have_is_refused_before_execution(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        seen: dict[str, str] = {}

        def after_rejection(messages: list[LLMMessage]) -> LLMResponse:
            seen["conversation"] = conversation_text(messages)
            return finish_turn()

        provider = ScriptedProvider(
            [
                plan_turn(("delete_alarm_history", {"asset_id": "AST-1"})),
                after_rejection,
                answer_turn("Nothing was changed."),
            ]
        )
        service = _service(
            _settings(backend_settings, max_steps=4),
            provider,
            procedure_retriever,
            mcp_session_factory,
        )

        answer = await service.ask("Delete the alarm history for the pump.")
        trace = service.store.get(answer.conversation_id)
        assert trace is not None

        # Nothing executed, and the model was told why — so a name that arrived from a retrieved
        # document or an API payload cannot become an action.
        assert answer.tool_calls == []
        assert "was NOT executed" in seen["conversation"]
        assert trace.of_kind("plan")[0].status == "partial"
        assert trace.of_kind("plan")[0].detail["rejected"][0]["name"] == "delete_alarm_history"

    async def test_the_step_ceiling_stops_the_loop_and_says_so(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        keep_going = plan_turn(
            ("search_procedures", {"query": "bearing lubrication interval"}),
            thought="one more look",
        )
        provider = ScriptedProvider([keep_going, keep_going, answer_turn("Partial findings.")])
        service = _service(
            _settings(backend_settings, max_steps=2),
            provider,
            procedure_retriever,
            mcp_session_factory,
        )

        answer = await service.ask("What are the bearing lubrication intervals?")

        # Bounded by construction. A model that keeps investigating rather than concluding is the
        # normal consequence of tools that return almost-but-not-quite enough, and unbounded it
        # turns one question into an open-ended bill against a metered gateway.
        assert answer.steps_used == 2
        assert answer.steps_exhausted is True
        assert any("step limit" in caveat for caveat in answer.caveats)
        assert len(answer.tool_calls) == 2

    async def test_an_unreachable_model_is_the_one_failure_that_has_no_answer(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        service = _service(
            backend_settings, ScriptedProvider([]), procedure_retriever, mcp_session_factory
        )

        with pytest.raises(CopilotError, match="the planner could not run"):
            await service.ask(QUESTION)

        trace = service.store.get(service.store.conversation_ids()[0])
        assert trace is not None
        # Recorded before it propagates, so the panel shows where it stopped rather than nothing.
        assert [event.name for event in trace.of_kind("error")] == ["planner"]

    async def test_a_model_that_fails_at_synthesis_is_reported_as_such(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        service = _service(
            backend_settings,
            ScriptedProvider([finish_turn()]),
            procedure_retriever,
            mcp_session_factory,
        )

        with pytest.raises(CopilotError, match="the answer could not be written"):
            await service.ask(QUESTION)

        trace = service.store.get(service.store.conversation_ids()[0])
        assert trace is not None
        assert [event.name for event in trace.of_kind("error")] == ["synthesis"]


class TestDegradedWithoutMcp:
    """The demo's second scenario: the MCP server is not running."""

    @pytest.fixture
    def broken_session_factory(self) -> Any:
        def factory() -> Any:
            raise ConnectionError("connection refused on port 9100")

        return factory

    async def test_the_question_is_still_answered_from_the_documents(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        broken_session_factory: Any,
    ) -> None:
        provider = ScriptedProvider(
            [
                plan_turn(
                    ("search_procedures", {"query": "bearing lubrication interval"}),
                    thought="the alarm tools are gone; use the documents",
                ),
                finish_turn(),
                answer_turn("Bearings are relubricated on schedule [MM-CP-MAINT §3]."),
            ]
        )
        service = _service(
            _settings(backend_settings, max_steps=4),
            provider,
            procedure_retriever,
            broken_session_factory,
        )

        answer = await service.ask("What are the bearing lubrication intervals?")

        # A designed path, not an exception someone meets for the first time on video.
        assert [call.backend for call in answer.tool_calls] == ["local"]
        assert answer.answer
        assert answer.citations

    async def test_the_loss_is_stated_in_the_trace_to_the_model_and_to_the_reader(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        broken_session_factory: Any,
    ) -> None:
        provider = ScriptedProvider([finish_turn(), answer_turn("Only documents were available.")])
        service = _service(
            _settings(backend_settings, max_steps=4),
            provider,
            procedure_retriever,
            broken_session_factory,
        )

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None
        system = "\n".join(
            message.content for message in provider.calls[0][0] if message.role == "system"
        )

        # All three, because any one alone leaves someone misled: the panel says a step was lost,
        # the model does not plan around tools that are absent, and the reader is told why the
        # answer is thinner than it should be.
        assert [event.name for event in trace.of_kind("error")] == ["degraded"]
        assert trace.of_kind("error")[0].status == "partial"
        assert "Operating in a degraded state" in system
        assert MCP_UNREACHABLE in system
        assert answer.caveats[0] == MCP_UNREACHABLE

    async def test_the_catalogue_shrinks_to_what_actually_works(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        broken_session_factory: Any,
    ) -> None:
        provider = ScriptedProvider([finish_turn(), answer_turn("ok")])
        service = _service(backend_settings, provider, procedure_retriever, broken_session_factory)

        answer = await service.ask(QUESTION)
        trace = service.store.get(answer.conversation_id)
        assert trace is not None
        discovery = trace.of_kind("tool_discovery")[0]

        # Advertising tools that cannot be reached would spend the step budget on calls that
        # were always going to fail.
        assert [tool["name"] for tool in discovery.detail["tools"]] == ["search_procedures"]
        assert await service.mcp_reachable() is False


class TestAConversationOfSeveralQuestions:
    """The follow-up path: what turn two knows about turn one, and what it must not.

    Driven through `CopilotService`, not the orchestrator, because the memory is deliberately
    held by the service — a new `Orchestrator` is built per request, so a test that asserted
    against one instance would pass while the GUI still saw a copilot with no memory.
    """

    @staticmethod
    def _two_turn_service(
        settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
        captured: list[list[LLMMessage]],
    ) -> tuple[CopilotService, ScriptedProvider]:
        def capture(response: LLMResponse) -> Any:
            def turn(messages: list[LLMMessage]) -> LLMResponse:
                captured.append(list(messages))
                return response

            return turn

        provider = ScriptedProvider(
            [
                capture(finish_turn()),
                capture(
                    answer_turn(
                        "Boiler Feed Pump 101 shows a recurring suction-pressure "
                        "pattern [OP-BFP-101 §4.2]."
                    )
                ),
                capture(finish_turn()),
                capture(answer_turn("Pump 102 shows nothing comparable.")),
            ]
        )
        return (
            _service(settings, provider, procedure_retriever, mcp_session_factory),
            provider,
        )

    async def test_the_second_question_is_planned_with_the_first_in_front_of_it(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        captured: list[list[LLMMessage]] = []
        service, _ = self._two_turn_service(
            backend_settings, procedure_retriever, mcp_session_factory, captured
        )

        first = await service.ask("What is happening on Boiler Feed Pump 101?")
        await service.ask("And what about pump 102?", conversation_id=first.conversation_id)

        second_plan = "\n".join(message.content for message in captured[2])
        assert "EARLIER TURNS IN THIS CONVERSATION" in second_plan
        assert "What is happening on Boiler Feed Pump 101?" in second_plan
        # Without this the pronoun in "and what about pump 102?" has no referent, which is the
        # difference between a chat and a form that forgets.
        assert "recurring suction-pressure pattern" in second_plan

    async def test_the_answer_to_the_second_question_is_written_with_it_too(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        captured: list[list[LLMMessage]] = []
        service, _ = self._two_turn_service(
            backend_settings, procedure_retriever, mcp_session_factory, captured
        )

        first = await service.ask("What is happening on Boiler Feed Pump 101?")
        second = await service.ask(
            "And what about pump 102?", conversation_id=first.conversation_id
        )

        synthesis = "\n".join(message.content for message in captured[3])
        assert "EARLIER TURNS IN THIS CONVERSATION" in synthesis
        # Reported to the client, so the GUI can say the turn was read in context.
        assert first.history_turns == 0
        assert second.history_turns == 1

    async def test_the_history_is_marked_as_context_rather_than_evidence(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        captured: list[list[LLMMessage]] = []
        service, _ = self._two_turn_service(
            backend_settings, procedure_retriever, mcp_session_factory, captured
        )

        first = await service.ask("What is happening on Boiler Feed Pump 101?")
        await service.ask("And what about pump 102?", conversation_id=first.conversation_id)

        second_plan = "\n".join(message.content for message in captured[2])
        # An earlier answer has no citation of its own. Allowed as a source, it would launder a
        # figure from turn one into an unattributed claim in turn two.
        assert "context, not evidence" in second_plan
        assert "call the tools again" in second_plan

    async def test_a_new_conversation_starts_from_nothing(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        captured: list[list[LLMMessage]] = []
        service, _ = self._two_turn_service(
            backend_settings, procedure_retriever, mcp_session_factory, captured
        )

        await service.ask("What is happening on Boiler Feed Pump 101?")
        second = await service.ask("And what about pump 102?")

        second_plan = "\n".join(message.content for message in captured[2])
        assert "EARLIER TURNS" not in second_plan
        assert second.history_turns == 0

    async def test_the_window_is_bounded_by_the_configured_number_of_turns(
        self,
        backend_settings: BackendSettings,
        procedure_retriever: Any,
        mcp_session_factory: Any,
    ) -> None:
        settings = _settings(backend_settings, conversation_turns=1)
        provider = ScriptedProvider(
            [
                finish_turn(),
                answer_turn("first"),
                finish_turn(),
                answer_turn("second"),
                finish_turn(),
                answer_turn("third"),
            ]
        )
        service = _service(settings, provider, procedure_retriever, mcp_session_factory)

        first = await service.ask("one")
        await service.ask("two", conversation_id=first.conversation_id)
        third = await service.ask("three", conversation_id=first.conversation_id)

        # Every turn pays for the window in input tokens, so it is a setting and the setting is
        # honoured: the oldest turn is gone rather than accumulating for the length of a shift.
        third_plan = "\n".join(message.content for message in provider.calls[4][0])
        assert "operator asked: two" in third_plan
        assert "operator asked: one" not in third_plan
        assert third.history_turns == 1
