"""Tests for hybrid retrieval: fusion, filters, pinned references, citations, abstention.

The abstention tests here use a **stub embedder with dictated cosine values** rather than the
hash embedder. That is deliberate, and the reason is worth stating because it is the kind of
thing a green suite otherwise hides: the hash embedder's similarity distribution *overlaps*
between on-topic and off-topic queries on this corpus (relevant 0.275–0.447, irrelevant
0.070–0.316), so no threshold separates them and any abstention assertion built on it would
be asserting a coincidence. Dictating the score tests the *policy* exactly;
`test_retrieval_semantic.py` tests the *calibration* against the real model. Neither test
alone is sufficient.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from rag.config import RagSettings
from rag.models import Chunk
from rag.retrieval.embedder import HashEmbedder
from rag.retrieval.retriever import (
    MAX_CHUNKS_PER_REFERENCE,
    HybridRetriever,
    _quote,
)
from rag.retrieval.store import ChunkIndex


class DictatedEmbedder:
    """Returns a vector whose cosine against every stored chunk is a value we choose.

    Every chunk is embedded to the same unit vector, and a query is embedded to that vector
    scaled toward or away from it, so `dense_search` reports a similarity we control. This
    makes the abstention threshold testable without depending on what a model happens to
    think about sourdough.
    """

    dimension = 8

    def __init__(self, similarity: float = 0.9) -> None:
        self.similarity = similarity

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        # cos(θ) between (s, √(1−s²), 0…) and (1, 0, 0…) is exactly s.
        orthogonal = max(0.0, 1.0 - self.similarity**2) ** 0.5
        return [self.similarity, orthogonal, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def close(self) -> None:
        """Part of the `Embedder` protocol; this one holds nothing."""


def _settings(**overrides: object) -> RagSettings:
    base: dict[str, object] = {
        "_env_file": None,
        "RAG_EMBEDDER": "hash",
        "RAG_COLLECTION": "t",
        "RAG_TOP_K": 5,
        "RAG_MIN_RELEVANCE": 0.60,
    }
    return RagSettings(**{**base, **overrides})  # type: ignore[arg-type]


def _retriever(
    chunks: list[Chunk], *, embedder: object | None = None, **overrides: object
) -> HybridRetriever:
    settings = _settings(**overrides)
    index = ChunkIndex(
        path=Path(":memory:"),
        collection=settings.collection,
        embedder=embedder or HashEmbedder(),  # type: ignore[arg-type]
        client=QdrantClient(":memory:"),
    )
    index.build(chunks)
    return HybridRetriever(index, settings)


class TestFusion:
    def test_results_are_ordered_by_fused_score(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("pump vibration bearing")

        scores = [item.scores.fused for item in result.chunks]
        assert scores == sorted(scores, reverse=True)

    def test_both_retrievers_scores_are_retained_for_the_trace(self, chunks: list[Chunk]) -> None:
        # A single fused number cannot answer "why did this rank first", which is the entire
        # content of the trace panel's retrieval row.
        result = _retriever(chunks).search("low suction pressure cavitation")

        assert any(
            item.scores.dense is not None and item.scores.lexical is not None
            for item in result.chunks
        )
        for item in result.chunks:
            if item.scores.dense is not None:
                assert item.scores.dense_rank is not None
            if item.scores.lexical is not None:
                assert item.scores.lexical_rank is not None

    def test_a_chunk_found_by_both_retrievers_outranks_one_found_by_either(
        self, chunks: list[Chunk]
    ) -> None:
        result = _retriever(chunks).search("minimum flow protection recirculation valve")

        both = [
            item
            for item in result.chunks
            if item.scores.dense_rank is not None and item.scores.lexical_rank is not None
        ]
        one = [
            item
            for item in result.chunks
            if (item.scores.dense_rank is None) != (item.scores.lexical_rank is None)
        ]
        if both and one:
            # This is the whole point of fusing: agreement between two independent signals is
            # stronger evidence than a high score from one.
            assert min(i.scores.fused for i in both) > max(i.scores.fused for i in one)

    def test_the_fused_score_is_bounded_by_one(self, chunks: list[Chunk]) -> None:
        # Normalised by the best attainable RRF score, so the number is interpretable and a
        # threshold means something across queries.
        result = _retriever(chunks).search("pump")

        assert all(0.0 < item.scores.fused <= 1.0 for item in result.chunks)

    def test_top_k_is_respected(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks, RAG_TOP_K=3).search("pump vibration")

        assert len(result.chunks) == 3
        assert len(result.citations) == 3

    def test_candidates_considered_counts_the_union_of_both_retrievers(
        self, chunks: list[Chunk]
    ) -> None:
        result = _retriever(chunks, RAG_TOP_K=2).search("pump vibration bearing")

        # Reported so a reviewer can see retrieval looked wider than the five rows shown.
        assert result.candidates_considered > len(result.chunks)


class TestFilters:
    def test_a_document_filter_excludes_everything_else(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("pump maintenance", doc_ids=["SAF-PUMP-LOTO"])

        assert result.chunks
        assert {item.chunk.doc_id for item in result.chunks} == {"SAF-PUMP-LOTO"}

    def test_a_doc_type_filter_excludes_everything_else(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("pump", doc_types=["safety_procedure"])

        assert {item.chunk.doc_type for item in result.chunks} == {"safety_procedure"}

    def test_the_filters_are_reported_in_the_result(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("pump", doc_ids=["OP-BFP-101"])

        # The trace must show what retrieval was *allowed* to see, or a suspiciously narrow
        # answer is unexplainable.
        assert result.filters["doc_ids"] == ["OP-BFP-101"]
        assert result.filters["min_relevance"] == 0.60

    def test_an_impossible_filter_yields_nothing_and_abstains(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("pump", doc_ids=["DOES-NOT-EXIST"])

        assert result.chunks == []
        assert result.low_confidence is True
        assert result.confidence_note is not None


class TestPinnedReferences:
    """The MCP→RAG seam: sections named by a recommendation are fetched, not hoped for."""

    def test_a_named_reference_is_included_even_if_search_would_miss_it(
        self, chunks: list[Chunk]
    ) -> None:
        # The query is deliberately about something else: a cited section must arrive because
        # it was named, not because it happened to rank.
        result = _retriever(chunks).search(
            "bearing lubrication oil change interval",
            references=["SAF-PUMP-LOTO §3 Isolation sequence"],
        )

        pinned = [
            item
            for item in result.chunks
            if item.chunk.doc_id == "SAF-PUMP-LOTO"
            and (item.chunk.section.number == "3" or item.chunk.section.number.startswith("3."))
        ]
        assert pinned

    def test_a_named_reference_appears_first(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search(
            "pump maintenance", references=["OP-BFP-101 §4.3 Minimum flow protection"]
        )

        assert result.chunks[0].chunk.section.number == "4.3"

    def test_one_reference_cannot_crowd_out_all_other_evidence(self, chunks: list[Chunk]) -> None:
        # `OP-BFP-101 §4.2` is three chunks; uncapped it would fill three of five slots.
        result = _retriever(chunks).search(
            "what should the operator do about recurring vibration",
            references=["OP-BFP-101 §4.2 Low suction pressure response"],
        )

        pinned = [i for i in result.chunks if i.chunk.section.number == "4.2"]
        assert len(pinned) <= MAX_CHUNKS_PER_REFERENCE
        # The slots the cap freed must actually go to other evidence, or the cap achieved
        # nothing.
        assert len(result.chunks) - len(pinned) >= 3

    def test_an_unresolvable_reference_is_reported_not_swallowed(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search(
            "low suction pressure", references=["XX-MISSING §9 Nonexistent"]
        )

        # The copilot must be able to say the cited procedure is absent, instead of quietly
        # answering from whatever else turned up.
        assert result.unresolved_references == ["XX-MISSING §9 Nonexistent"]
        assert result.confidence_note is not None
        assert "XX-MISSING" in result.confidence_note

    def test_an_unresolvable_reference_does_not_by_itself_mean_low_confidence(
        self, chunks: list[Chunk]
    ) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(0.9)).search(
            "low suction pressure", references=["XX-MISSING §9 Nonexistent"]
        )

        # Search found strong material; the missing citation is a caveat on the answer, not a
        # reason to refuse to give one.
        assert result.low_confidence is False
        assert result.unresolved_references

    def test_a_pinned_chunk_that_search_also_found_keeps_its_measured_scores(
        self, chunks: list[Chunk]
    ) -> None:
        reference = "OP-BFP-101 §4.2 Low suction pressure response"
        result = _retriever(chunks).search("low suction pressure response", references=[reference])

        pinned = next(i for i in result.chunks if i.chunk.section.number == "4.2")
        # Corroborated rather than merely asserted — and the trace shows which.
        assert pinned.scores.dense is not None or pinned.scores.lexical is not None

    def test_a_named_section_search_did_not_find_publishes_no_score(
        self, chunks: list[Chunk]
    ) -> None:
        """The pinned 1.0 is a sort key and must never reach a citation as a measurement.

        It ranks the section first, which is correct — it was asked for by name. But rendered
        in the GUI's citation list beside genuinely ranked passages scoring 0.21, a 1.0 says
        "this is the strongest evidence in the answer" about the one passage nothing matched.
        """
        # One candidate per retriever, so the named section provably cannot be among them and
        # the synthetic path is the one under test. Without this the outcome depends on whether
        # the hash embedder happened to rank the cited section — it does, weakly, for some
        # queries, which would make the section corroborated and test nothing.
        result = _retriever(chunks, RAG_CANDIDATES=1).search(
            "bearing lubrication oil change interval",
            references=["SAF-PUMP-LOTO §3 Isolation sequence"],
        )

        pinned = [c for c in result.citations if c.doc_id == "SAF-PUMP-LOTO"]
        assert pinned, "the named section should have been pulled in"
        for citation in pinned:
            assert citation.selected_by == "reference"
            assert citation.score is None
        # And the measured ones are still reported, so the distinction is visible rather than
        # achieved by dropping scores everywhere.
        assert any(c.score is not None for c in result.citations)

    def test_a_named_section_search_also_found_publishes_its_real_score(
        self, chunks: list[Chunk]
    ) -> None:
        reference = "OP-BFP-101 §4.2 Low suction pressure response"
        result = _retriever(chunks).search("low suction pressure response", references=[reference])

        corroborated = next(c for c in result.citations if c.section_number == "4.2")
        assert corroborated.selected_by == "reference"
        assert corroborated.score is not None, (
            "a pinned section that search independently ranked has a measured score and "
            "withholding it would hide the corroboration"
        )

    def test_pinned_chunks_still_obey_the_document_filter(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search(
            "isolation",
            references=["SAF-PUMP-LOTO §3 Isolation sequence"],
            doc_ids=["OP-BFP-101"],
        )

        # Rank is irrelevant for a pinned chunk, but a caller scoping to one document meant it.
        assert all(item.chunk.doc_id == "OP-BFP-101" for item in result.chunks)


class TestFetchReferences:
    def test_named_sections_are_returned_without_ranking(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).fetch_references(["OP-BFP-101 §4.3 Minimum flow protection"])

        assert result.chunks
        assert all(item.scores.fused == 1.0 for item in result.chunks)
        assert result.low_confidence is False
        # Nothing here was ranked — the sections were named — so no citation may claim a score.
        assert all(c.score is None and c.selected_by == "reference" for c in result.citations)

    def test_a_parent_reference_returns_its_subsections(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).fetch_references(["MM-CP-MAINT §6 Alarm rationalization"])

        numbers = {item.chunk.section.number for item in result.chunks}
        assert numbers >= {"6.1", "6.2", "6.3", "6.4"}

    def test_resolving_nothing_is_low_confidence(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).fetch_references(["XX-MISSING §1 Nope"])

        assert result.chunks == []
        assert result.low_confidence is True
        assert result.unresolved_references == ["XX-MISSING §1 Nope"]

    def test_duplicates_across_references_are_not_repeated(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).fetch_references(
            ["OP-BFP-101 §4.2 Low suction pressure response", "OP-BFP-101 §4.2"]
        )

        ids = [item.chunk.chunk_id for item in result.chunks]
        assert len(ids) == len(set(ids))


class TestAbstention:
    """The policy, with the similarity dictated. See the module docstring for why."""

    def test_a_strong_match_answers(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(0.80)).search("anything")

        assert result.low_confidence is False
        assert result.confidence_note is None

    def test_a_weak_match_abstains(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(0.40)).search("anything")

        assert result.low_confidence is True
        assert result.confidence_note is not None

    def test_abstaining_still_returns_the_candidates_it_found(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(0.40)).search("anything")

        # Showing an operator what was found and that it was thin is more useful than an
        # empty response.
        assert result.chunks
        assert result.citations

    def test_the_note_names_the_score_and_the_threshold(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(0.40)).search("anything")

        assert result.confidence_note is not None
        assert "0.40" in result.confidence_note
        assert "0.60" in result.confidence_note
        # It must tell the reader how to treat what follows, not just that a number was low.
        assert "leads" in result.confidence_note

    # No case sits exactly on 0.60: the cosine is recomputed in float32 by Qdrant, so an
    # exact-boundary assertion would be testing rounding rather than the policy.
    @pytest.mark.parametrize(
        ("similarity", "abstains"),
        [(0.95, False), (0.70, False), (0.63, False), (0.57, True), (0.30, True)],
    )
    def test_the_threshold_boundary(
        self, chunks: list[Chunk], similarity: float, abstains: bool
    ) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(similarity)).search("anything")

        assert result.low_confidence is abstains

    def test_the_decision_uses_relevance_not_the_fused_rank_score(
        self, chunks: list[Chunk]
    ) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(0.40)).search("pump vibration")

        # The regression this guards. The fused score of a top hit is bounded below by 0.5
        # (found by one retriever at rank 1) and reaches 1.0 (found by both at rank 1),
        # *whatever the cosine was* — here it is 0.40, a match to nothing. So no fused-score
        # threshold can both admit real results and reject this one. An earlier version of
        # this retriever thresholded the fused score, and every off-topic question was
        # answered confidently.
        assert result.top_score is not None and result.top_score >= 0.5
        assert result.top_relevance is not None and result.top_relevance < 0.60
        assert result.low_confidence is True

    def test_weak_evidence_and_a_missing_citation_are_both_reported(
        self, chunks: list[Chunk]
    ) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(0.40)).search(
            "anything", references=["XX-MISSING §9 Nonexistent"]
        )

        # The two findings are independent, and this combination — thin evidence *and* the
        # cited procedure absent — is the one most likely to end in an answer improvised about
        # the wrong document. Reporting only the first would hide it.
        assert result.confidence_note is not None
        assert "0.60 threshold" in result.confidence_note
        assert "XX-MISSING" in result.confidence_note

    def test_a_pinned_reference_cannot_manufacture_confidence(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks, embedder=DictatedEmbedder(0.30)).search(
            "anything", references=["OP-BFP-101 §4.3 Minimum flow protection"]
        )

        # A pinned chunk carries a synthetic score of 1.0. If that counted as evidence, naming
        # a section would make any answer confident regardless of what matched the question.
        assert result.low_confidence is True


class TestCitations:
    def test_a_citation_is_produced_for_every_chunk(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("low suction pressure")

        assert len(result.citations) == len(result.chunks)

    def test_a_citation_carries_everything_needed_to_check_it(self, chunks: list[Chunk]) -> None:
        citation = _retriever(chunks).search("low suction pressure").citations[0]

        assert citation.doc_id
        assert citation.doc_title
        assert citation.revision
        assert citation.section_number
        assert citation.section_title
        assert citation.quote
        assert citation.chunk_id
        # Provenance: a reviewer must be able to open the file the quote came from.
        assert citation.source_path.startswith("rag/documents/")

    def test_the_reference_is_formatted_the_way_the_alarm_api_formats_its_own(
        self, chunks: list[Chunk]
    ) -> None:
        from rag.models import SectionRef

        for citation in _retriever(chunks).search("low suction pressure").citations:
            parsed = SectionRef.parse(citation.reference)
            assert parsed is not None
            assert parsed.doc_id == citation.doc_id
            assert parsed.number == citation.section_number

    def test_the_quote_is_verbatim_from_the_chunk(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("low suction pressure")

        for item, citation in zip(result.chunks, result.citations, strict=True):
            body = " ".join(item.chunk.text.split())
            quoted = " ".join(citation.quote.rstrip("…").split())
            # A citation the reader cannot check against the document is decoration.
            assert quoted in body, f"{citation.reference} quote is not verbatim"

    def test_quotes_are_bounded(self, chunks: list[Chunk]) -> None:
        from rag.retrieval.retriever import MAX_QUOTE_CHARS

        for citation in _retriever(chunks).search("pump").citations:
            assert len(citation.quote) <= MAX_QUOTE_CHARS


class TestQuoteSelection:
    def test_the_sentence_matching_the_query_is_chosen_over_the_preamble(self) -> None:
        text = (
            "This section describes general considerations for the auxiliary systems. "
            "Switch to the standby lube oil filter element above 1.0 bar differential. "
            "Record the change in the shift log."
        )
        quote = _quote(text, "lube oil filter differential pressure")

        # A truncated prefix would quote the useless first sentence.
        assert quote.startswith("Switch to the standby lube oil filter")

    def test_following_sentences_are_appended_while_the_budget_allows(self) -> None:
        text = "Alpha term here. Beta continues the thought. Gamma qualifies it."
        quote = _quote(text, "alpha")

        # Qualifications ("…unless the standby is unavailable") follow the instruction far
        # more often than they precede it.
        assert "Beta continues" in quote

    def test_an_empty_query_still_produces_a_quote(self) -> None:
        assert _quote("Some procedure text here.", "")

    def test_text_with_no_sentence_punctuation_is_handled(self) -> None:
        assert _quote("| Tag | Setpoint |\n| PT-101 | 8.5 bar |", "setpoint")


class TestResultShape:
    def test_the_query_is_echoed_back(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("low suction pressure")

        assert result.query == "low suction pressure"

    def test_both_score_kinds_are_reported(self, chunks: list[Chunk]) -> None:
        result = _retriever(chunks).search("low suction pressure")

        assert result.top_score is not None
        assert result.top_relevance is not None

    def test_no_secret_or_full_document_body_leaks_into_the_result(
        self, chunks: list[Chunk]
    ) -> None:
        result = _retriever(chunks).search("low suction pressure")
        serialised = result.model_dump_json()

        for forbidden in ("demo-token", "GEMINI_API_KEY", "x-goog-api-key", "AIza"):
            assert forbidden not in serialised
