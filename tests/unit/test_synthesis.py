"""The answer, and what is done to it before anyone is allowed to read it.

The failure mode this module exists to prevent is not a wrong sentence — it is a plausible
sentence with an authoritative-looking citation attached, because an operator who reads
"procedure §4.2 permits a restart" acts on it. So the heart of this file is the citation
enforcement: a section the answer cites that was never retrieved must be visibly neutralised and
reported, and a section that *was* retrieved must survive untouched. Both halves matter — an
over-eager check that stripped real citations would train a reader to ignore the marking.

The caveat tests assert something subtler: the caveats are written **here**, from the facts,
rather than asked of the model. "Admit it when your evidence is weak" is exactly the instruction
that is not followed in the case where it matters, so low confidence, unattributed answers and
failed tool calls are stated mechanically and cannot be talked out of.
"""

from __future__ import annotations

from typing import Any

from apps.backend.llm.provider import LLMMessage, LLMResponse
from apps.backend.llm.scripted import ScriptedProvider, answer_turn
from apps.backend.orchestration.registry import ErrorKind, ToolOutcome
from apps.backend.orchestration.synthesis import (
    SYNTHESIS_PROTOCOL,
    TRUST_NOTICE,
    synthesise,
)


def _passage(
    reference: str, quote: str = "Restore suction pressure before restart.", **kw: Any
) -> dict[str, Any]:
    return {
        "reference": reference,
        "document": kw.get("document", "BFP-101 Operating Procedure"),
        "revision": kw.get("revision", "3.1"),
        "section": kw.get("section", ""),
        "quote": quote,
        "relevance": kw.get("relevance", 0.72),
        "selected_by": kw.get("selected_by", "search"),
        "trusted": False,
    }


def _ok(name: str, result: dict[str, Any]) -> ToolOutcome:
    return ToolOutcome(call_id="c1", name=name, arguments={}, result=result, duration_ms=12.0)


def _failed(
    name: str, kind: ErrorKind = "transport", error: str = "connection refused"
) -> ToolOutcome:
    return ToolOutcome(
        call_id="c2",
        name=name,
        arguments={},
        ok=False,
        error=error,
        error_kind=kind,
        duration_ms=8.0,
    )


async def _synthesise(
    answer: str,
    *,
    passages: list[dict[str, Any]] | None = None,
    outcomes: list[ToolOutcome] | None = None,
    low_confidence: bool = False,
    confidence_notes: list[str] | None = None,
    unresolved_references: list[str] | None = None,
    steps_exhausted: bool = False,
) -> tuple[Any, ScriptedProvider]:
    provider = ScriptedProvider([answer_turn(answer)])
    result, _ = await synthesise(
        provider,
        question="Why does BFP-101 keep alarming?",
        outcomes=outcomes if outcomes is not None else [_ok("get_alarms", {"total": 14})],
        passages=passages if passages is not None else [_passage("OP-BFP-101 §4.2 Low suction")],
        low_confidence=low_confidence,
        confidence_notes=confidence_notes or [],
        unresolved_references=unresolved_references or [],
        steps_exhausted=steps_exhausted,
    )
    return result, provider


class TestCitationEnforcement:
    async def test_a_retrieved_citation_is_left_alone(self) -> None:
        result, _ = await _synthesise("Restore suction pressure first [OP-BFP-101 §4.2].")

        assert result.answer == "Restore suction pressure first [OP-BFP-101 §4.2]."
        assert result.cited_references == ["OP-BFP-101 §4.2"]
        assert result.invented_references == []

    async def test_a_section_that_was_never_retrieved_is_neutralised_in_the_text(self) -> None:
        result, _ = await _synthesise("A restart is permitted after 20 minutes [OP-BFP-101 §7.3].")

        # Rewriting a model's output is intrusive and is done anyway: left as-is, an invention
        # appears in the exact visual form the real citations take and the reader cannot tell.
        assert "[unverified: OP-BFP-101 §7.3]" in result.answer
        assert result.invented_references == ["OP-BFP-101 §7.3"]
        assert result.cited_references == []

    async def test_a_citation_from_another_document_entirely_is_an_invention(self) -> None:
        result, _ = await _synthesise("See [API-RP-686 §5.1] for alignment tolerances.")

        assert result.invented_references == ["API-RP-686 §5.1"]

    async def test_the_substitution_is_reported_so_it_is_auditable_not_hidden(self) -> None:
        result, _ = await _synthesise("Two inventions: [OP-BFP-101 §9.1] and [MM-PUMP §2.4].")

        # Silent correction would hide a model that fabricates from whoever has to decide
        # whether to trust this system.
        assert result.invented_references == ["OP-BFP-101 §9.1", "MM-PUMP §2.4"]

    async def test_citing_the_parent_of_a_retrieved_subsection_is_accepted(self) -> None:
        result, _ = await _synthesise(
            "Follow the low-suction response [OP-BFP-101 §4].",
            passages=[_passage("OP-BFP-101 §4.2 Low suction pressure response")],
        )

        # Checkable by a reader against text that was actually retrieved, which is the property
        # that matters. Only a reference with no retrieved relative is an invention.
        assert result.cited_references == ["OP-BFP-101 §4"]
        assert result.invented_references == []

    async def test_citing_a_subsection_of_a_retrieved_parent_is_accepted(self) -> None:
        result, _ = await _synthesise(
            "Specifically [OP-BFP-101 §4.2.1].",
            passages=[_passage("OP-BFP-101 §4 Abnormal conditions")],
        )

        assert result.invented_references == []

    async def test_a_sibling_section_is_not_accepted(self) -> None:
        result, _ = await _synthesise(
            "See [OP-BFP-101 §4.3].",
            passages=[_passage("OP-BFP-101 §4.2 Low suction pressure response")],
        )

        # §4.2 being retrieved says nothing about what §4.3 contains. Accepting siblings would
        # make the check cosmetic.
        assert result.invented_references == ["OP-BFP-101 §4.3"]

    async def test_a_repeated_citation_is_reported_once(self) -> None:
        result, _ = await _synthesise(
            "First [OP-BFP-101 §4.2], and again [OP-BFP-101 §4.2].",
        )

        assert result.cited_references == ["OP-BFP-101 §4.2"]

    async def test_an_answer_with_no_citation_markers_is_returned_unchanged(self) -> None:
        result, _ = await _synthesise("14 high-severity alarms occurred, rising over the window.")

        assert result.answer.startswith("14 high-severity alarms")


class TestCaveats:
    async def test_low_confidence_is_stated_by_us_with_the_retrievers_own_note(self) -> None:
        result, _ = await _synthesise(
            "The documents do not appear to cover this.",
            low_confidence=True,
            confidence_notes=["Top relevance 0.41, below the 0.60 threshold."],
        )

        # Added mechanically. Asking the model to admit weak evidence and trusting it to do so is
        # the step that fails when it matters.
        assert any("low confidence" in caveat for caveat in result.caveats)
        assert any("0.41" in caveat for caveat in result.caveats)

    async def test_an_invention_produces_a_caveat_naming_it(self) -> None:
        result, _ = await _synthesise("Restart after 20 minutes [OP-BFP-101 §7.3].")

        assert any(
            "OP-BFP-101 §7.3" in caveat and "not retrieved" in caveat for caveat in result.caveats
        )

    async def test_an_unattributed_answer_is_flagged_but_still_published(self) -> None:
        result, _ = await _synthesise("Restart the pump after checking the suction strainer.")

        # Not suppressed: it may be answering a question about alarm counts, which needs no
        # procedure. But the reader is told the recommendation is not attributed.
        assert result.answer.startswith("Restart the pump")
        assert any("does not cite any of the retrieved" in caveat for caveat in result.caveats)

    async def test_no_retrieval_at_all_says_nothing_is_backed_by_documentation(self) -> None:
        result, _ = await _synthesise("Here is what the alarms show.", passages=[])

        assert any("No procedure text was retrieved" in caveat for caveat in result.caveats)

    async def test_sections_the_api_cited_but_the_index_lacks_are_carried_through(self) -> None:
        result, _ = await _synthesise(
            "As documented [OP-BFP-101 §4.2].",
            unresolved_references=["OP-BFP-102 §3.1 Seal flush"],
        )

        # The alarm API pointed at it and it is not indexed. Silence here reads as "there is no
        # such procedure", which is a different and wrong claim.
        assert any("missing from the document index" in caveat for caveat in result.caveats)
        assert any("OP-BFP-102 §3.1" in caveat for caveat in result.caveats)

    async def test_failed_tool_calls_are_counted_and_named(self) -> None:
        result, _ = await _synthesise(
            "As documented [OP-BFP-101 §4.2].",
            outcomes=[_ok("get_alarms", {"total": 14}), _failed("get_recurring_alarms")],
        )

        assert any(
            "1 tool call(s) failed" in caveat and "get_recurring_alarms (transport)" in caveat
            for caveat in result.caveats
        )

    async def test_hitting_the_step_ceiling_is_disclosed(self) -> None:
        result, _ = await _synthesise("As documented [OP-BFP-101 §4.2].", steps_exhausted=True)

        assert any("step limit" in caveat for caveat in result.caveats)

    async def test_a_clean_answer_carries_no_caveats(self) -> None:
        result, _ = await _synthesise("As documented [OP-BFP-101 §4.2], restore suction pressure.")

        # The counterweight to the tests above: caveats must mean something, so an answer that
        # cited retrieved evidence from successful calls gets none.
        assert result.caveats == []


class TestEvidencePrompt:
    def _prompt(self, provider: ScriptedProvider) -> str:
        messages: list[LLMMessage] = provider.calls[0][0]
        return "\n".join(message.content for message in messages)

    async def test_passages_are_delimited_and_marked_untrusted(self) -> None:
        _, provider = await _synthesise("As documented [OP-BFP-101 §4.2].")
        prompt = self._prompt(provider)

        # The second injection layer, after the ingestion sanitiser — and the one that still
        # holds for a document added later without passing through it.
        assert TRUST_NOTICE in prompt
        assert 'trusted="false"' in prompt
        assert 'reference="OP-BFP-101 §4.2 Low suction"' in prompt
        assert 'revision="3.1"' in prompt

    async def test_a_pinned_passage_is_described_rather_than_given_a_score(self) -> None:
        _, provider = await _synthesise(
            "As documented [OP-BFP-101 §4.2].",
            passages=[
                _passage("OP-BFP-101 §4.2 Low suction", relevance=None, selected_by="reference")
            ],
        )

        # `relevance=None` means "fetched because something cited it, not because it ranked". A
        # placeholder number would tell the model its weakest-matched passage was its best.
        assert "not ranked (fetched because it was cited)" in self._prompt(provider)
        assert 'relevance="None"' not in self._prompt(provider)

    async def test_a_ranked_passage_carries_its_score(self) -> None:
        _, provider = await _synthesise("As documented [OP-BFP-101 §4.2].")

        assert 'relevance="0.72"' in self._prompt(provider)

    async def test_the_structure_and_citation_rules_are_stated(self) -> None:
        _, provider = await _synthesise("As documented [OP-BFP-101 §4.2].")
        prompt = self._prompt(provider)

        assert SYNTHESIS_PROTOCOL in prompt
        assert "Do not invent a section number" in prompt

    async def test_successful_tool_results_appear_as_data(self) -> None:
        _, provider = await _synthesise(
            "As documented [OP-BFP-101 §4.2].",
            outcomes=[_ok("get_recurring_alarms", {"total_patterns": 3})],
        )
        prompt = self._prompt(provider)

        assert "### get_recurring_alarms" in prompt
        assert '"total_patterns": 3' in prompt

    async def test_failures_are_shown_to_the_model_as_gaps_to_account_for(self) -> None:
        _, provider = await _synthesise(
            "As documented [OP-BFP-101 §4.2].",
            outcomes=[_failed("get_recurring_alarms")],
        )
        prompt = self._prompt(provider)

        # Hiding them would get an answer written as though the picture were complete.
        assert "Tool calls that failed" in prompt
        assert "connection refused" in prompt
        assert "Nothing — no tool call returned data." in prompt

    async def test_an_oversized_tool_result_is_truncated_not_dropped(self) -> None:
        _, provider = await _synthesise(
            "As documented [OP-BFP-101 §4.2].",
            outcomes=[
                _ok(
                    "get_alarms",
                    {"data": [{"id": f"ALM-{n}", "note": "x" * 200} for n in range(200)]},
                )
            ],
        )
        prompt = self._prompt(provider)

        # One unexpectedly large payload must not push the retrieved passages out of the context
        # the answer has to be grounded in.
        assert "(result truncated)" in prompt
        assert 'trusted="false"' in prompt

    async def test_low_confidence_is_also_stated_to_the_model(self) -> None:
        _, provider = await _synthesise(
            "The index does not cover this.",
            low_confidence=True,
            confidence_notes=["Top relevance 0.41."],
        )
        prompt = self._prompt(provider)

        # Belt and braces with the mechanical caveat: the caveat protects the reader, this asks
        # the answer itself not to present near-misses as guidance.
        assert "Retrieval confidence: LOW" in prompt
        assert "do not present the passages above as documented guidance" in prompt.lower()

    async def test_the_step_limit_is_disclosed_to_the_model(self) -> None:
        _, provider = await _synthesise("As documented [OP-BFP-101 §4.2].", steps_exhausted=True)

        assert "reached its step limit" in self._prompt(provider)

    async def test_no_tools_are_advertised_for_synthesis(self) -> None:
        _, provider = await _synthesise("As documented [OP-BFP-101 §4.2].")

        # The answer step must not be able to start another investigation: the prompt says "do
        # not call any more tools", and nothing is offered for it to call.
        assert provider.calls[0][1] == []


class TestResultMetadata:
    async def test_the_model_and_latency_come_back_for_the_trace(self) -> None:
        provider = ScriptedProvider(
            [LLMResponse(text="As documented [OP-BFP-101 §4.2].", model="m-1", latency_ms=123.0)]
        )

        result, _ = await synthesise(
            provider,
            question="q",
            outcomes=[],
            passages=[_passage("OP-BFP-101 §4.2 Low suction")],
            low_confidence=False,
            confidence_notes=[],
            unresolved_references=[],
        )

        assert (result.model, result.latency_ms) == ("m-1", 123.0)
