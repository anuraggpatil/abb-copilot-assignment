"""Tests for the index: persistence, reference resolution, and both search paths.

Runs on the hash embedder and an in-memory Qdrant, so it exercises the plumbing without a
model download. Nothing here asserts *semantic* ranking quality — the hash embedder cannot
support that claim, and `test_retrieval_semantic.py` is where it is made.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from rag.models import Chunk
from rag.retrieval.embedder import HashEmbedder
from rag.retrieval.store import ChunkIndex, IndexNotBuilt


def _index(chunks: list[Chunk] | None = None, collection: str = "t") -> ChunkIndex:
    index = ChunkIndex(
        path=Path(":memory:"),
        collection=collection,
        embedder=HashEmbedder(),
        client=QdrantClient(":memory:"),
    )
    if chunks:
        index.build(chunks)
    return index


class TestBuild:
    def test_building_stores_every_chunk(self, chunks: list[Chunk]) -> None:
        index = _index(chunks)

        assert index.build(chunks) == len(chunks)
        assert len(index.chunks) == len(chunks)

    def test_an_empty_build_is_refused(self) -> None:
        # An empty index answers everything with "no documented evidence", which is
        # indistinguishable from a working abstention path.
        with pytest.raises(ValueError, match="empty index"):
            _index().build([])

    def test_rebuilding_replaces_rather_than_accumulates(self, chunks: list[Chunk]) -> None:
        index = _index(chunks)
        subset = chunks[:5]

        index.build(subset)

        # An upsert would leave chunks for sections that have since been renumbered or
        # deleted, and a citation to a section that no longer exists is worse than a slow
        # ingest.
        assert len(index.chunks) == 5

    def test_a_round_trip_preserves_every_field(self, chunks: list[Chunk]) -> None:
        index = _index(chunks)
        index._chunks = []  # force a read back out of Qdrant

        loaded = {chunk.chunk_id: chunk for chunk in index.chunks}
        original = chunks[10]
        restored = loaded[original.chunk_id]

        assert restored == original


class TestLoad:
    def test_loading_a_missing_collection_names_the_remedy(self) -> None:
        index = _index(collection="never_built")

        with pytest.raises(IndexNotBuilt, match="make ingest"):
            index.load()

    def test_chunks_come_back_in_document_order(self, chunks: list[Chunk]) -> None:
        index = _index(chunks)
        index._chunks = []

        loaded = index.chunks
        keys = [(c.doc_id, c.section.number_parts, c.ordinal) for c in loaded]

        # Scroll order is arbitrary. Citations read better in document order, and the BM25
        # corpus index must line up with this list positionally.
        assert keys == sorted(keys)

    def test_number_parts_sorts_4_10_after_4_2(self) -> None:
        from rag.models import SectionRef

        refs = [
            SectionRef(doc_id="D", number="4.10", title="Ten"),
            SectionRef(doc_id="D", number="4.2", title="Two"),
        ]
        assert [r.number for r in sorted(refs, key=lambda r: r.number_parts)] == ["4.2", "4.10"]


class TestSearch:
    def test_dense_search_returns_at_most_the_limit(self, chunks: list[Chunk]) -> None:
        results = _index(chunks).dense_search("pump vibration", limit=3)

        assert len(results) <= 3

    def test_dense_search_is_ordered_by_descending_score(self, chunks: list[Chunk]) -> None:
        results = _index(chunks).dense_search("pump vibration bearing", limit=10)

        assert [score for _, score in results] == sorted(
            (score for _, score in results), reverse=True
        )

    def test_lexical_search_finds_an_exact_identifier(self, chunks: list[Chunk]) -> None:
        # The case dense search is worst at: identifiers embed to nearly the same vector as
        # each other, and in an alarm investigation these tokens are most of the question.
        results = _index(chunks).lexical_search("OP-BFP-101", limit=5)

        assert results
        assert any(chunk.doc_id == "OP-BFP-101" for chunk, _ in results)

    def test_lexical_search_drops_zero_scoring_chunks(self, chunks: list[Chunk]) -> None:
        # A zero BM25 score means no query term occurs in the chunk. Keeping it would hand
        # fusion a rank for something that matched nothing.
        results = _index(chunks).lexical_search("zzzznonexistenttoken", limit=20)

        assert all(score > 0 for _, score in results)

    def test_lexical_search_covers_the_heading_trail(self, chunks: list[Chunk]) -> None:
        # `§4.2` appears in the heading, not in the body text.
        results = _index(chunks).lexical_search("minimum flow protection", limit=5)

        assert any(
            "4.3" in chunk.section.number or "flow" in chunk.text.lower() for chunk, _ in results
        )


class TestReferenceResolution:
    """The seam between an MCP result and the RAG query."""

    def test_an_exact_section_reference_resolves(self, chunks: list[Chunk]) -> None:
        found = _index(chunks).resolve_reference("OP-BFP-101 §4.2 Low suction pressure response")

        assert found
        assert all(c.doc_id == "OP-BFP-101" and c.section.number == "4.2" for c in found)

    def test_a_reference_without_a_title_still_resolves(self, chunks: list[Chunk]) -> None:
        # A recommendation may cite a bare section number.
        found = _index(chunks).resolve_reference("OP-BFP-101 §4.2")

        assert found
        assert all(c.section.number == "4.2" for c in found)

    def test_a_parent_reference_widens_to_its_subsections(self, chunks: list[Chunk]) -> None:
        # `§5 Auxiliary systems` has a sentence of preamble; its substance is in §5.1–§5.3.
        # Returning nothing would be technically correct and useless.
        found = _index(chunks).resolve_reference("OP-BFP-101 §5 Auxiliary systems")

        numbers = {c.section.number for c in found}
        assert numbers >= {"5.1", "5.2", "5.3"}

    def test_a_malformed_reference_resolves_to_nothing_without_raising(
        self, chunks: list[Chunk]
    ) -> None:
        # References arrive from the API and from a language model; a malformed one is
        # routine, not exceptional.
        index = _index(chunks)

        assert index.resolve_reference("not a reference at all") == []
        assert index.resolve_reference("") == []

    def test_an_unknown_document_resolves_to_nothing(self, chunks: list[Chunk]) -> None:
        assert _index(chunks).resolve_reference("XX-NOPE §1 Missing") == []

    def test_a_wrong_section_in_a_known_document_resolves_to_nothing(
        self, chunks: list[Chunk]
    ) -> None:
        assert _index(chunks).resolve_reference("OP-BFP-101 §99 Invented") == []

    @pytest.mark.parametrize(
        "reference",
        [
            "MM-CP-MAINT §3 Bearing lubrication",
            "MM-CP-MAINT §4 Strainers and seals",
            "MM-CP-MAINT §5 Condition-based inspection intervals",
            "MM-CP-MAINT §6 Alarm rationalization",
            "MM-CP-MAINT §7 Failure reporting",
            "OP-BFP-101 §3 Normal and emergency shutdown",
            "OP-BFP-101 §4.2 Low suction pressure response",
            "OP-BFP-101 §4.3 Minimum flow protection",
            "OP-BFP-101 §5 Auxiliary systems",
            "OP-BFP-101 §6 Instrumentation checks",
            "SAF-PUMP-LOTO §2 Before any intervention",
            "SAF-PUMP-LOTO §3 Isolation sequence",
            "TS-BFP-VIB-CAV §2 Symptom-to-cause matrix",
            "TS-BFP-VIB-CAV §3 Cavitation mechanism",
        ],
    )
    def test_every_reference_the_alarm_api_emits_resolves(
        self, chunks: list[Chunk], reference: str
    ) -> None:
        """The hard contract between the simulator and the corpus.

        These strings are the ones `apps/alarm_api/recommendations.py` puts in its
        `procedure_reference` fields. If one stops resolving, the acceptance scenario silently
        degrades to an answer with no procedure behind it — so this is parametrized per
        reference, to name which one broke.
        """
        assert _index(chunks).resolve_reference(reference), f"{reference} no longer resolves"


class TestClosing:
    """Closing is a correctness requirement here, not housekeeping.

    Both resources the index owns misbehave when they are left to the garbage collector: the
    embedded Qdrant directory keeps its exclusive lock, so the next `make ingest` blocks; and
    an ONNX inference session destroyed during interpreter finalisation aborts the process with
    `recursive_mutex lock failed` *after* the work has succeeded. That one cost a green test run
    exiting 134, which is why the embedder is released from here rather than by whoever
    remembers.
    """

    def test_closing_the_index_closes_the_embedder(self, chunks: list[Chunk]) -> None:
        class RecordingEmbedder(HashEmbedder):
            closed = False

            def close(self) -> None:
                self.closed = True

        embedder = RecordingEmbedder()
        index = ChunkIndex(
            path=Path(":memory:"),
            collection="closing",
            embedder=embedder,
            client=QdrantClient(":memory:"),
        )
        index.build(chunks)

        index.close()

        assert embedder.closed, (
            "ChunkIndex.close must release the embedder: callers close an index, and nothing "
            "else in the codebase knows that a model is hiding behind it"
        )

    def test_a_closed_embedder_reloads_rather_than_breaking(self) -> None:
        # `close()` is not a one-way door: loading is lazy, so a reused embedder works again.
        # Asserted on the hash embedder because it needs no weights; the lazy-load path itself
        # is `FastEmbedEmbedder._load`.
        embedder = HashEmbedder()
        before = embedder.embed_query("low suction pressure")

        embedder.close()

        assert embedder.embed_query("low suction pressure") == before
