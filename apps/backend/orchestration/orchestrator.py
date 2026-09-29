"""The loop: plan, execute, feed back, repeat — bounded — then answer from what was gathered.

Four properties of this loop are load-bearing, and each one exists because of a way the
workflow fails without it.

**It is bounded by construction.** `max_steps` iterations, `MAX_CALLS_PER_STEP` calls in each.
A model that keeps investigating rather than concluding is not a hypothetical: it is the normal
consequence of an investigation whose tools keep returning almost-but-not-quite enough, and
unbounded it turns one question into an open-ended bill against a metered gateway. Hitting the
ceiling is not an error — the answer is written from what was gathered and says the limit was
reached.

**A failed tool is data, not an abort.** The upstream API can be down, an asset id can be
wrong, a window can be empty. Every outcome — including the failures — goes back into the
conversation, so the model can correct a bad argument, try another route, or conclude with a
partial picture. An orchestrator that raised on the first failure would produce nothing in
exactly the situations where an operator most needs whatever is known.

**Every step is traced as it happens.** Events go to the store and, when the caller supplies a
sink, out over SSE immediately. The GUI is required to show the MCP calls behind an answer, and
a trace assembled after the fact cannot show a question that is still being worked on.

**The chain is the model's, not ours.** Nothing here hardcodes "search assets, then recurring
alarms, then recommendations". The tool catalogue's descriptions and the planner's instructions
carry that guidance, and the model chains the ids itself — which is what makes the same
orchestrator answer a question the acceptance scenario did not anticipate.

**A conversation is more than one question.** Earlier turns of the same `conversation_id` are
recalled from `memory.py` and handed to both the planner and the synthesis step, so "and what
about pump 102?" is a question rather than a fragment. What is *not* carried over is the previous
turn's tool results: every turn investigates with fresh calls, because a figure reused from
memory would be a claim with no citation behind it. See that module for the trust boundary.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from apps.backend.config import BackendSettings
from apps.backend.llm.provider import LLMError, LLMMessage, LLMProvider, LLMResponse
from apps.backend.orchestration import kpis as kpi_board
from apps.backend.orchestration.kpis import InvestigationKpis
from apps.backend.orchestration.memory import (
    ConversationMemory,
    ConversationTurn,
    transcript,
)
from apps.backend.orchestration.planner import Plan, Planner
from apps.backend.orchestration.registry import ToolOutcome, ToolRegistry
from apps.backend.orchestration.synthesis import synthesise
from apps.backend.tracing.trace import TraceEvent, TraceRecorder, TraceStore

log = logging.getLogger(__name__)

Sink = Callable[[TraceEvent], Awaitable[None]]


class CopilotError(RuntimeError):
    """The question could not be answered at all — the model itself was unreachable.

    Distinct from a tool failure, which is handled inside the loop and reported in the answer.
    This is the only failure that leaves the caller with nothing.
    """


class ToolCallSummary(BaseModel):
    """One executed call, in the form the GUI's answer header shows."""

    model_config = ConfigDict(extra="forbid")

    name: str
    backend: str
    ok: bool
    duration_ms: float
    error_kind: str | None = None


class AnswerCitation(BaseModel):
    """A passage the answer could be checked against.

    `relevance` is `None` for a section that was fetched because something cited it rather than
    because it ranked — see `rag/retrieval/retriever.py`. The GUI must render that as "cited
    upstream", not as a score, and a number here would make that impossible.
    """

    model_config = ConfigDict(extra="forbid")

    reference: str
    document: str = ""
    revision: str = ""
    section: str = ""
    quote: str = ""
    relevance: float | None = None
    selected_by: str = "search"
    cited_in_answer: bool = False


class CopilotAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    request_id: str
    trace_id: str
    question: str
    answer: str
    citations: list[AnswerCitation] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    invented_references: list[str] = Field(default_factory=list)
    low_confidence: bool = False
    steps_used: int = 0
    steps_exhausted: bool = False
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    model: str = ""
    #: Earlier turns of this conversation that were in front of the model while it answered.
    #: Reported so the GUI can say the answer was read in context rather than leaving the
    #: operator to assume it — and so a test can assert the context was actually carried.
    history_turns: int = 0
    #: The numbers the tools returned, projected for the GUI's KPI header. Always present; its
    #: `metrics` and `patterns` are empty when no tool returned any, which is what the GUI must
    #: render as "no alarm figures" rather than as zeros. See `kpis.py`.
    kpis: InvestigationKpis = Field(default_factory=InvestigationKpis)


class Orchestrator:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: ToolRegistry,
        store: TraceStore,
        settings: BackendSettings,
        memory: ConversationMemory | None = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.store = store
        self.settings = settings
        # Optional, and private to this orchestrator when omitted: a single-question caller —
        # `/ask` from curl, most unit tests — gets the same behaviour as before without having to
        # know that conversations exist. The service supplies the shared, process-wide one.
        self.memory = memory or ConversationMemory(max_turns=settings.conversation_turns)
        self.planner = Planner(provider, registry)

    async def answer(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        sink: Sink | None = None,
        degradations: list[str] | None = None,
    ) -> CopilotAnswer:
        """Answer one question.

        `degradations` are conditions the caller already knows about — most often that the MCP
        server could not be reached, so the alarm tools are absent from the catalogue. They are
        handled in three places on purpose: recorded in the trace, stated to the model so it
        does not plan around tools that are not there, and carried into the answer's caveats so
        the reader is told why the answer is thinner than it should be. Any one of the three on
        its own leaves someone misled.
        """
        conversation_id = conversation_id or f"conv-{uuid.uuid4().hex[:12]}"
        recorder = TraceRecorder(self.store, conversation_id=conversation_id, sink=sink)
        degradations = list(degradations or [])
        history = self.memory.history(conversation_id)
        history_text = transcript(history)

        await self._record_discovery(recorder)
        for degradation in degradations:
            await recorder.record(
                "error", "degraded", status="partial", summary=degradation, detail={}
            )

        conversation: list[LLMMessage] = [LLMMessage(role="user", content=question)]
        # Ahead of the question, because it is what the question may be referring to; as a
        # system turn rather than replayed user/assistant turns, for the reasons in `memory.py`.
        if history_text:
            conversation.insert(0, LLMMessage(role="system", content=history_text))
        if degradations:
            conversation.insert(
                0,
                LLMMessage(
                    role="system",
                    content=(
                        "Operating in a degraded state. Do not plan around tools that are not in "
                        "your catalogue; answer from what is available and say what is missing.\n"
                        + "\n".join(f"- {note}" for note in degradations)
                    ),
                ),
            )
        outcomes: list[ToolOutcome] = []
        steps_used = 0
        steps_exhausted = True

        for _ in range(self.settings.max_steps):
            steps_used += 1
            plan = await self._plan(recorder, conversation, step=steps_used)

            # Only a plan with nothing at all to report ends the loop. A plan whose every call
            # was rejected still has something to say — the reasons — and stopping here would
            # discard the correction those reasons were written for, leaving the answer to be
            # composed from an investigation that never happened.
            if not plan.calls and not plan.rejected:
                steps_exhausted = False
                break

            # The model's own words go back as the assistant turn, so the next turn sees the
            # reasoning it committed to rather than a reconstruction of it.
            conversation.append(
                LLMMessage(role="assistant", content=plan.raw_text or plan.thought or "(no text)")
            )

            # Sequentially, not concurrently. The calls in one step are meant to be independent,
            # but the upstream is a single simulated API and a deterministic order is what makes
            # the trace panel readable and the e2e test reproducible.
            for call in plan.calls:
                outcome = await self.registry.call(
                    call.name,
                    call.arguments,
                    call_id=call.call_id,
                    trace_id=recorder.trace_id,
                )
                outcomes.append(outcome)
                await self._record_outcome(recorder, outcome)
                conversation.append(
                    LLMMessage(
                        role="tool", content=outcome.as_model_text(), tool_call_id=outcome.call_id
                    )
                )

            for rejected in plan.rejected:
                conversation.append(
                    LLMMessage(
                        role="tool",
                        content=rejected.as_model_text(),
                        tool_call_id=rejected.call_id,
                    )
                )

            if plan.done:
                steps_exhausted = False
                break

        answer = await self._synthesise(
            recorder,
            question=question,
            outcomes=outcomes,
            steps_used=steps_used,
            steps_exhausted=steps_exhausted,
            degradations=degradations,
            history=history,
            history_text=history_text,
        )
        # Remembered only once the answer exists. A turn that failed in synthesis was never shown
        # to anyone, and recalling it later would put words in the copilot's mouth that it never
        # said — the caveats and the neutralised citations included.
        self.memory.remember(
            conversation_id,
            ConversationTurn(
                question=question,
                answer=answer.answer,
                citations=[
                    citation.reference for citation in answer.citations if citation.cited_in_answer
                ],
            ),
        )
        return answer

    # --- steps ---------------------------------------------------------------------

    async def _record_discovery(self, recorder: TraceRecorder) -> None:
        timer = recorder.timer()
        tools = await self.registry.discover()
        await recorder.record(
            "tool_discovery",
            "list_tools",
            duration_ms=timer.elapsed_ms,
            started_at=timer.started_at,
            summary=f"{len(tools)} tools available",
            detail={
                "tools": [
                    {
                        "name": tool.name,
                        "backend": tool.backend,
                        "required": tool.input_schema.get("required", []),
                    }
                    for tool in tools
                ]
            },
        )

    async def _plan(
        self, recorder: TraceRecorder, conversation: list[LLMMessage], *, step: int
    ) -> Plan:
        timer = recorder.timer()
        try:
            plan, response = await self.planner.plan(conversation)
        except LLMError as error:
            await recorder.record(
                "error",
                "planner",
                status="error",
                duration_ms=timer.elapsed_ms,
                started_at=timer.started_at,
                summary=str(error),
            )
            raise CopilotError(f"the planner could not run: {error}") from error

        await self._record_llm(recorder, "planner", response, timer)
        await recorder.record(
            "plan",
            f"step {step}",
            duration_ms=timer.elapsed_ms,
            started_at=timer.started_at,
            status="partial" if plan.rejected else "ok",
            summary=plan.thought or ("finished" if plan.done else "no reasoning given"),
            detail={
                "step": step,
                "thought": plan.thought,
                "done": plan.done,
                "tool_calls": [
                    {"name": call.name, "arguments": call.arguments} for call in plan.calls
                ],
                "rejected": [
                    {
                        "name": rejected.name,
                        "arguments": rejected.arguments,
                        "problems": rejected.problems,
                    }
                    for rejected in plan.rejected
                ],
                "parse_note": plan.parse_note,
                "raw_reply": plan.raw_text,
            },
        )
        return plan

    async def _record_outcome(self, recorder: TraceRecorder, outcome: ToolOutcome) -> None:
        kind = "rag_retrieval" if outcome.backend == "local" else "mcp_tool_call"
        detail: dict[str, Any] = {"arguments": outcome.arguments, **outcome.trace}
        if not outcome.ok:
            detail["error"] = outcome.error
            detail["error_kind"] = outcome.error_kind
        await recorder.record(
            kind,  # type: ignore[arg-type]  # both literals are members of EventKind
            outcome.name,
            status="ok" if outcome.ok else "error",
            duration_ms=outcome.duration_ms,
            summary=_summarise(outcome),
            detail=detail,
        )

    async def _record_llm(
        self, recorder: TraceRecorder, name: str, response: LLMResponse, timer: Any
    ) -> None:
        await recorder.record(
            "llm_call",
            name,
            duration_ms=response.latency_ms or timer.elapsed_ms,
            started_at=timer.started_at,
            summary=f"{response.model or self.provider.name} in {response.latency_ms:.0f}ms",
            detail={
                "provider": self.provider.name,
                "model": response.model,
                "native_tools": self.provider.supports_native_tools,
                "latency_ms": response.latency_ms,
                "input_tokens": response.usage.input_tokens if response.usage else None,
                "output_tokens": response.usage.output_tokens if response.usage else None,
                "stop_reason": response.stop_reason,
            },
        )

    async def _synthesise(
        self,
        recorder: TraceRecorder,
        *,
        question: str,
        outcomes: list[ToolOutcome],
        steps_used: int,
        steps_exhausted: bool,
        degradations: list[str],
        history: list[ConversationTurn] | None = None,
        history_text: str = "",
    ) -> CopilotAnswer:
        retrievals = [
            outcome.result
            for outcome in outcomes
            if outcome.ok and outcome.backend == "local" and isinstance(outcome.result, dict)
        ]
        passages = _dedupe(
            passage
            for retrieval in retrievals
            for passage in retrieval.get("passages", [])
            if isinstance(passage, dict)
        )
        notes = [
            str(retrieval["confidence_note"])
            for retrieval in retrievals
            if retrieval.get("confidence_note")
        ]
        unresolved = _unique(
            str(reference)
            for retrieval in retrievals
            for reference in retrieval.get("unresolved_references", [])
        )
        # Low only when every retrieval was low. One confident retrieval means documented
        # evidence exists, even if a later broader query found nothing.
        low_confidence = bool(retrievals) and all(
            bool(retrieval.get("low_confidence")) for retrieval in retrievals
        )

        timer = recorder.timer()
        try:
            result, response = await synthesise(
                self.provider,
                question=question,
                outcomes=outcomes,
                passages=passages,
                low_confidence=low_confidence,
                confidence_notes=notes,
                unresolved_references=unresolved,
                steps_exhausted=steps_exhausted,
                history=history_text,
                max_output_tokens=self.settings.llm_max_output_tokens,
            )
        except LLMError as error:
            await recorder.record(
                "error",
                "synthesis",
                status="error",
                duration_ms=timer.elapsed_ms,
                started_at=timer.started_at,
                summary=str(error),
            )
            raise CopilotError(f"the answer could not be written: {error}") from error

        await self._record_llm(recorder, "synthesis", response, timer)
        await recorder.record(
            "synthesis",
            "answer",
            duration_ms=timer.elapsed_ms,
            started_at=timer.started_at,
            status="partial" if result.caveats else "ok",
            summary=f"{len(result.cited_references)} citations, {len(result.caveats)} caveats",
            detail={
                "cited_references": result.cited_references,
                "invented_references": result.invented_references,
                "caveats": result.caveats,
                "low_confidence": low_confidence,
                "history_turns": len(history or []),
                # The answer text itself is deliberately absent: it travels to the client as the
                # answer, and duplicating it into the trace would double the payload for nothing.
                "answer_chars": len(result.answer),
            },
        )

        cited = {reference for reference in result.cited_references}
        return CopilotAnswer(
            conversation_id=recorder.conversation_id,
            request_id=recorder.request_id,
            trace_id=recorder.trace_id,
            question=question,
            answer=result.answer,
            citations=[_citation_of(passage, cited) for passage in passages],
            # Degradations first: "the alarm API was unreachable" changes how every other
            # caveat should be read.
            caveats=[*degradations, *result.caveats],
            invented_references=result.invented_references,
            low_confidence=low_confidence,
            steps_used=steps_used,
            steps_exhausted=steps_exhausted,
            tool_calls=[
                ToolCallSummary(
                    name=outcome.name,
                    backend=outcome.backend,
                    ok=outcome.ok,
                    duration_ms=outcome.duration_ms,
                    error_kind=outcome.error_kind,
                )
                for outcome in outcomes
            ],
            model=result.model,
            history_turns=len(history or []),
            # Projected from the same outcomes the answer was written from, so the tiles and the
            # prose cannot disagree about a figure.
            kpis=kpi_board.extract(outcomes),
        )


def _citation_of(passage: dict[str, Any], cited: set[str]) -> AnswerCitation:
    reference = str(passage.get("reference", ""))
    relevance = passage.get("relevance")
    return AnswerCitation(
        reference=reference,
        document=str(passage.get("document", "")),
        revision=str(passage.get("revision", "")),
        section=str(passage.get("section", "")),
        quote=str(passage.get("quote", "")),
        relevance=float(relevance) if isinstance(relevance, (int, float)) else None,
        selected_by=str(passage.get("selected_by", "search")),
        # A reference may be cited as `OP-BFP-101 §4.2` while the passage's full reference
        # includes the section title, so the match is on the prefix the model writes.
        cited_in_answer=any(reference.startswith(marker) for marker in cited),
    )


def _summarise(outcome: ToolOutcome) -> str:
    if not outcome.ok:
        return f"{outcome.error_kind}: {outcome.error}"
    result = outcome.result or {}
    for key in ("total_patterns", "total_matching", "returned", "candidates_considered"):
        if key in result:
            return f"{key.replace('_', ' ')}: {result[key]}"
    if "passages" in result:
        flag = " (low confidence)" if result.get("low_confidence") else ""
        return f"{len(result['passages'])} passages{flag}"
    if "assets" in result:
        return f"{len(result['assets'])} assets"
    if "actions" in result:
        return f"{len(result['actions'])} recommended actions"
    return "ok"


def _dedupe(passages: Any) -> list[dict[str, Any]]:
    """One entry per section, first occurrence wins.

    Several retrieval calls in one investigation routinely return the same section — a broad
    question and then the reference the recommendation cited — and a duplicated passage both
    wastes prompt budget and makes the citation list look padded.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for passage in passages:
        key = str(passage.get("reference", "")) + "|" + str(passage.get("quote", ""))[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(passage)
    return out


def _unique(values: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
