"""Retrieval quality with the real embedding model. Skipped when the weights are absent.

Everything else in `rag/tests` runs on the hash embedder, which is fast, deterministic and
offline — but it captures lexical overlap and nothing else, so it cannot support any claim
about *semantic* retrieval. Two claims need the real model and are made only here:

1. **Relevance.** A question phrased the way an operator would phrase it retrieves the section
   that answers it, even when they share few words. "Why does this pump keep vibrating" must
   find the cavitation mechanism, which never says "vibrating".
2. **Calibration.** `RAG_MIN_RELEVANCE = 0.60` is a number measured against
   `bge-small-en-v1.5` on this corpus. A threshold nothing verifies is a constant someone will
   eventually "clean up". These tests are what makes it a measurement: on-topic questions must
   clear it and off-topic ones must fall below it, with a margin on both sides.

Why skip rather than fail: the weights are 127 MB, are not committed, and `make fetch-model`
needs network. A developer running `pytest rag/tests` on a fresh clone gets 220 passing tests
and a clear skip reason, not a red suite for a missing download. The tradeoff is real — a skipped
test proves nothing — so the module reports the measured numbers on failure, and CI is where
this is meant to run with the model present.

Run with: `make fetch-model && uv run pytest rag/tests/test_retrieval_semantic.py -v`
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from rag.config import RagSettings
from rag.models import Chunk
from rag.retrieval.embedder import FastEmbedEmbedder
from rag.retrieval.retriever import HybridRetriever
from rag.retrieval.store import ChunkIndex

MODEL_PATH = Path(__file__).resolve().parents[2] / ".models" / "bge-small-en-v1.5"

pytestmark = pytest.mark.skipif(
    not (MODEL_PATH / "model_optimized.onnx").exists(),
    reason=f"embedding weights not found at {MODEL_PATH} — run `make fetch-model`",
)

#: The threshold under test. Kept as a literal rather than read from settings: these tests
#: exist to check that *this specific number* is the right one, and reading it from the config
#: they are validating would make them pass for any value it is changed to.
MIN_RELEVANCE = 0.60

#: Questions phrased the way an operator or engineer would ask them — not as keyword queries —
#: paired with the document that must be retrieved for the answer to be grounded.
ON_TOPIC = [
    pytest.param(
        "Why does Boiler Feed Pump 101 keep vibrating and what is causing it?",
        {"TS-BFP-VIB-CAV", "OP-BFP-101"},
        id="vibration-cause",
    ),
    pytest.param(
        "Suction pressure has fallen to 7 bar and the low-suction alarm is active — what do I do?",
        {"OP-BFP-101"},
        id="low-suction-response",
    ),
    pytest.param(
        # Phrased around "alarm setpoint" rather than the pump's behaviour, which legitimately
        # belongs to the maintenance manual's rationalization chapter rather than to the
        # operating procedure. Kept as a separate case because an earlier version of this test
        # expected OP-BFP-101 here and was simply wrong about what the question asks.
        "What should an operator do when an alarm setpoint keeps being exceeded?",
        {"OP-BFP-101", "MM-CP-MAINT"},
        id="setpoint-exceeded",
    ),
    pytest.param(
        "Is it safe to keep running the pump while it is cavitating?",
        {"TS-BFP-VIB-CAV", "OP-BFP-101", "SAF-PUMP-LOTO"},
        id="run-through-cavitation",
    ),
    pytest.param(
        "What has to happen before anyone opens up a pump for work?",
        {"SAF-PUMP-LOTO"},
        id="isolation-prerequisites",
    ),
    pytest.param(
        "How often should bearing oil be changed and when is an oil analysis needed?",
        {"MM-CP-MAINT"},
        id="lubrication-interval",
    ),
    pytest.param(
        "When is it legitimate to raise an alarm setpoint rather than fix the equipment?",
        {"MM-CP-MAINT"},
        id="rationalization",
    ),
    pytest.param(
        "The vibration reading is climbing week over week — at what point do we open the bearing?",
        {"MM-CP-MAINT", "OP-BFP-101", "TS-BFP-VIB-CAV"},
        id="vibration-trending",
    ),
    pytest.param(
        "How do I tell a real alarm from a faulty transmitter?",
        {"OP-BFP-101"},
        id="instrument-verification",
    ),
]

#: Questions this corpus genuinely cannot answer. These must abstain — an alarm copilot that
#: improvises from the nearest pump paragraph is the specific failure this threshold prevents.
#:
#: Deliberately *not* in this list: "what is the turbine blade inspection interval for a gas
#: turbine?". It scores 0.688, above any workable threshold, and it should — the maintenance
#: manual's §5 really is about inspection intervals. See
#: `test_a_topically_adjacent_question_is_not_caught_by_the_threshold`.
OFF_TOPIC = [
    pytest.param("What is the capital of France?", id="unrelated-fact"),
    pytest.param("How do I make sourdough bread rise faster?", id="unrelated-howto"),
    pytest.param("Write a Python function that sorts a list of dictionaries.", id="coding"),
    pytest.param("Who is on call for the electrical substation this weekend?", id="not-in-corpus"),
]


@pytest.fixture(scope="module")
def semantic_retriever(chunks: list[Chunk]) -> Iterator[HybridRetriever]:
    """The real corpus, the real model, an in-memory index.

    Module-scoped because loading the ONNX model and embedding 74 chunks is the expensive part
    and is identical for every test here.

    Closed at teardown, and that is load-bearing rather than tidiness: this is the only fixture
    in the suite holding an ONNX inference session, and leaving it for the garbage collector
    aborted the process at interpreter shutdown — *after* the run had reported success, so
    `pytest` printed "676 passed" and exited 134. See `FastEmbedEmbedder.close`.
    """
    settings = RagSettings(
        _env_file=None,
        RAG_EMBEDDER="fastembed",
        RAG_EMBED_MODEL_PATH=MODEL_PATH,
        RAG_COLLECTION="semantic_test",
        RAG_TOP_K=5,
        RAG_MIN_RELEVANCE=MIN_RELEVANCE,
    )
    index = ChunkIndex(
        path=Path(":memory:"),
        collection=settings.collection,
        embedder=FastEmbedEmbedder(settings.embed_model, model_path=MODEL_PATH),
        client=QdrantClient(":memory:"),
    )
    index.build(chunks)
    retriever = HybridRetriever(index, settings)
    yield retriever
    retriever.close()


class TestRelevance:
    @pytest.mark.parametrize(("question", "expected_docs"), ON_TOPIC)
    def test_the_answering_document_is_retrieved(
        self, semantic_retriever: HybridRetriever, question: str, expected_docs: set[str]
    ) -> None:
        result = semantic_retriever.search(question)

        found = {item.chunk.doc_id for item in result.chunks}
        assert found & expected_docs, (
            f"{question!r} retrieved {sorted(found)}, none of {sorted(expected_docs)}"
        )

    @pytest.mark.parametrize(("question", "expected_docs"), ON_TOPIC)
    def test_the_answering_document_ranks_first(
        self, semantic_retriever: HybridRetriever, question: str, expected_docs: set[str]
    ) -> None:
        result = semantic_retriever.search(question)

        # Stricter than retrieval: the synthesis prompt is length-bounded, so a section that
        # ranks fifth may not survive into the context the model actually reads.
        assert result.chunks[0].chunk.doc_id in expected_docs, (
            f"{question!r} put {result.chunks[0].chunk.reference} first"
        )

    def test_a_paraphrase_finds_a_section_it_shares_no_keywords_with(
        self, semantic_retriever: HybridRetriever
    ) -> None:
        # The case that justifies dense retrieval existing at all: no BM25 index can connect
        # this phrasing to a section about cavitation, because the words do not overlap.
        result = semantic_retriever.search(
            "the pump sounds like it is full of gravel and the flow is unsteady"
        )

        assert any(item.chunk.doc_id == "TS-BFP-VIB-CAV" for item in result.chunks)

    def test_the_acceptance_scenario_retrieves_its_procedure(
        self, semantic_retriever: HybridRetriever
    ) -> None:
        """The mandatory scenario's retrieval step, asserted directly.

        If this regresses, the headline demo still produces an answer — just one with no
        procedure behind it, which is the failure mode hardest to notice by eye.
        """
        result = semantic_retriever.search(
            "recurring high-severity vibration alarms on Boiler Feed Pump 101 over the last "
            "90 days — likely contributing factors and recommended actions"
        )

        assert result.low_confidence is False
        assert {item.chunk.doc_id for item in result.chunks} & {"OP-BFP-101", "TS-BFP-VIB-CAV"}


class TestCalibration:
    """What makes `min_relevance = 0.60` a measurement rather than a guess."""

    @pytest.mark.parametrize(("question", "expected_docs"), ON_TOPIC)
    def test_on_topic_questions_clear_the_threshold(
        self, semantic_retriever: HybridRetriever, question: str, expected_docs: set[str]
    ) -> None:
        result = semantic_retriever.search(question)

        assert result.top_relevance is not None
        assert result.top_relevance >= MIN_RELEVANCE, (
            f"{question!r} scored {result.top_relevance:.3f}, under the {MIN_RELEVANCE} "
            f"threshold — a real question would be refused"
        )
        assert result.low_confidence is False

    @pytest.mark.parametrize("question", OFF_TOPIC)
    def test_off_topic_questions_fall_below_the_threshold(
        self, semantic_retriever: HybridRetriever, question: str
    ) -> None:
        result = semantic_retriever.search(question)

        assert result.top_relevance is not None
        assert result.top_relevance < MIN_RELEVANCE, (
            f"{question!r} scored {result.top_relevance:.3f}, over the {MIN_RELEVANCE} "
            f"threshold — the copilot would answer it from pump procedures"
        )
        assert result.low_confidence is True
        assert result.confidence_note is not None

    def test_the_two_distributions_do_not_overlap(
        self, semantic_retriever: HybridRetriever
    ) -> None:
        """The property the threshold depends on, asserted as a property.

        The per-question tests above check each side against 0.60. This checks the thing that
        makes *any* threshold possible: that the worst on-topic score still beats the best
        off-topic one. If they ever cross, no value of `min_relevance` works and moving the
        number is the wrong fix — the failure message says so, with the numbers.
        """
        on = {
            param.values[0]: semantic_retriever.search(str(param.values[0])).top_relevance or 0.0
            for param in ON_TOPIC
        }
        off = {
            param.values[0]: semantic_retriever.search(str(param.values[0])).top_relevance or 0.0
            for param in OFF_TOPIC
        }

        worst_on = min(on.values())
        best_off = max(off.values())
        assert worst_on > best_off, (
            f"score distributions overlap: worst on-topic {worst_on:.3f} "
            f"({min(on, key=lambda q: on[q])!r}) <= best off-topic {best_off:.3f} "
            f"({max(off, key=lambda q: off[q])!r}). No threshold separates these; the corpus "
            f"or the model needs attention, not the threshold."
        )
        # And the chosen value must sit inside the gap with room on both sides, so ordinary
        # variation in phrasing does not flip a decision.
        assert best_off < MIN_RELEVANCE < worst_on

    def test_a_topically_adjacent_question_is_not_caught_by_the_threshold(
        self, semantic_retriever: HybridRetriever
    ) -> None:
        """A known limitation, asserted so it stays known.

        Cosine similarity measures topical similarity, not applicability. A gas-turbine
        inspection-interval question scores ~0.69 against the pump maintenance manual, above
        anything the on-topic distribution (0.663–0.820) would let us set the threshold to. The
        retrieved text genuinely is about inspection intervals; it is about the wrong machine,
        and that is not a distinction a single similarity number can carry.

        This is recorded as a characterization test rather than deleted or marked xfail,
        because the value of knowing it is that nobody later "fixes" it by raising
        `min_relevance` — which would start refusing the real questions in `ON_TOPIC` instead.
        What actually mitigates it is downstream and already built: every claim carries a
        citation the reader can check, and the documents declare their scope in `applies_to`.
        """
        result = semantic_retriever.search(
            "What is the turbine blade inspection interval for a gas turbine?"
        )

        assert result.top_relevance is not None
        assert result.top_relevance > MIN_RELEVANCE, (
            "the adjacent-domain question now abstains; if that is robust rather than "
            "accidental, this limitation can be retired"
        )
        # The mitigation that does work: whatever is returned is attributable, so a reader can
        # see it is about a pump and not a turbine.
        assert result.citations
        assert all(citation.quote and citation.reference for citation in result.citations)
        assert not any("turbine" in citation.doc_title.lower() for citation in result.citations), (
            "the corpus has no turbine document, so nothing here can be mistaken for one"
        )

    def test_bm25_is_correctly_excluded_from_the_confidence_decision(
        self, semantic_retriever: HybridRetriever
    ) -> None:
        """Why the threshold is on the cosine and not on anything BM25 contributes.

        BM25 scores an off-topic question comparably to an on-topic one — stopwords match
        plenty of chunks — so a confidence rule that consulted it could be rescued by noise.
        This asserts the weakness directly, so the reasoning is checked rather than just
        written down in a comment.
        """
        relevant = semantic_retriever.index.lexical_search(
            "What should an operator do when suction pressure drops?", limit=5
        )
        irrelevant = semantic_retriever.index.lexical_search(
            "What is the capital of France?", limit=5
        )

        assert relevant and irrelevant, "expected both queries to produce BM25 hits"
        # Not an assertion that BM25 is useless — it is the only thing that reliably finds
        # `OP-BFP-101` — only that it cannot carry an abstention decision.
        assert irrelevant[0][1] > relevant[0][1] * 0.5, (
            "BM25 separated on-topic from off-topic on this corpus; if that is now reliably "
            "true, the confidence rule could legitimately consider it"
        )


class TestQueryPrefix:
    """bge is asymmetric: queries take an instruction prefix, passages do not."""

    def test_the_query_and_document_paths_produce_different_vectors(self) -> None:
        embedder = FastEmbedEmbedder("BAAI/bge-small-en-v1.5", model_path=MODEL_PATH)
        text = "Switch to the standby lube oil filter above 1.0 bar differential."

        as_query = embedder.embed_query(text)
        as_document = embedder.embed_documents([text])[0]

        # If these are ever equal, the prefix has been dropped and every similarity score
        # silently degrades — with nothing failing, which is why this is asserted explicitly.
        assert as_query != as_document

    def test_the_dimension_matches_the_model(self) -> None:
        embedder = FastEmbedEmbedder("BAAI/bge-small-en-v1.5", model_path=MODEL_PATH)

        assert embedder.dimension == 384
        assert len(embedder.embed_query("test")) == 384
