"""The index: embedded Qdrant for dense search, BM25 for lexical, one class over both.

Qdrant runs in **embedded** mode — `QdrantClient(path=...)` writes to a local directory with
no server and no container. That is what lets `make ingest` work on a clone with nothing
installed but Python, and it is the reason this stack was chosen over a hosted vector DB.

The BM25 side is held in memory and rebuilt from the chunks stored in Qdrant when the index
is opened for reading. `rank_bm25` has no persistence format, and pickling it would tie the
on-disk index to a library version. Rebuilding costs milliseconds for 74 chunks and keeps
Qdrant as the single source of truth: one thing to delete, one thing that can be stale.

**Why both.** The two retrievers fail in opposite directions on this corpus, and each covers
the other. Dense search finds "what causes vibration on a feed pump" in a section that never
uses the word vibration. Lexical search is the only thing that reliably finds `OP-BFP-101`,
`4.5 mm/s`, or `§4.2` — identifiers embed to nearly the same vector as every other
identifier, and in an alarm investigation those exact tokens are most of the question.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from rag.models import Chunk, SectionRef

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from rag.retrieval.embedder import Embedder

log = logging.getLogger(__name__)


class IndexNotBuilt(Exception):
    """Reading an index that does not exist yet.

    A distinct type because the fix is a specific command, and the message says so: this is
    the error a reviewer will hit first if they run the backend before `make ingest`.
    """


def _tokens(text: str) -> list[str]:
    return "".join(c.lower() if c.isalnum() else " " for c in text).split()


class ChunkIndex:
    """Dense + lexical index over the chunk corpus."""

    def __init__(
        self,
        *,
        path: Path,
        collection: str,
        embedder: Embedder,
        client: QdrantClient | None = None,
    ) -> None:
        self.path = path
        self.collection = collection
        self.embedder = embedder
        # An injected client is how tests get `QdrantClient(":memory:")` without a temp
        # directory; production passes none and gets the on-disk one.
        self._client = client or QdrantClient(path=str(path))
        self._chunks: list[Chunk] = []
        self._bm25: Any | None = None

    # --- writing -----------------------------------------------------------------

    def build(self, chunks: Sequence[Chunk]) -> int:
        """Replace the collection's contents with `chunks`. Returns the count written.

        Deliberately a replace and not an upsert. An incremental update leaves chunks from
        sections that have since been deleted or renumbered, and a citation pointing at a
        section that no longer exists is worse than a slower ingest.
        """
        if not chunks:
            raise ValueError("refusing to build an empty index")

        # `recreate_collection` would say this in one call, but it is deprecated in
        # qdrant-client 1.19 and slated for removal.
        if self._client.collection_exists(self.collection):
            self._client.delete_collection(self.collection)
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.embedder.dimension, distance=Distance.COSINE),
        )

        vectors = self.embedder.embed_documents([chunk.embedding_text() for chunk in chunks])
        self._client.upsert(
            collection_name=self.collection,
            points=[
                # Qdrant ids must be int or UUID, so the position is the id and the
                # meaningful `chunk_id` lives in the payload.
                PointStruct(id=index, vector=vector, payload=chunk.model_dump())
                for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
            ],
        )

        self._chunks = list(chunks)
        self._bm25 = None
        log.info("indexed %d chunks into %r", len(chunks), self.collection)
        return len(chunks)

    # --- reading -----------------------------------------------------------------

    def load(self) -> int:
        """Read every chunk back out of Qdrant, so BM25 can be rebuilt over them."""
        if not self._client.collection_exists(self.collection):
            raise IndexNotBuilt(
                f"collection {self.collection!r} does not exist in {self.path} — run `make ingest`"
            )

        count = self._client.count(self.collection, exact=True).count
        if count == 0:
            raise IndexNotBuilt(f"collection {self.collection!r} is empty — run `make ingest`")

        records, _ = self._client.scroll(
            collection_name=self.collection, limit=count, with_payload=True, with_vectors=False
        )
        chunks = [Chunk.model_validate(record.payload) for record in records]
        # Scroll order is not document order; citations read better in document order and
        # the BM25 corpus index must match `self._chunks` positionally.
        self._chunks = sorted(chunks, key=lambda c: (c.doc_id, c.section.number_parts, c.ordinal))
        self._bm25 = None
        return len(self._chunks)

    @property
    def chunks(self) -> list[Chunk]:
        if not self._chunks:
            self.load()
        return self._chunks

    def dense_search(self, query: str, limit: int) -> list[tuple[Chunk, float]]:
        """Cosine nearest neighbours, highest first."""
        hits = self._client.query_points(
            collection_name=self.collection,
            query=self.embedder.embed_query(query),
            limit=limit,
            with_payload=True,
        ).points
        return [(Chunk.model_validate(hit.payload), float(hit.score)) for hit in hits]

    def lexical_search(self, query: str, limit: int) -> list[tuple[Chunk, float]]:
        """BM25 over the same chunks, highest first, zero-scoring hits dropped."""
        scores = self._bm25_index().get_scores(_tokens(query))
        ranked = sorted(zip(self.chunks, scores, strict=True), key=lambda pair: -pair[1])
        # A zero BM25 score means no query term occurs in the chunk. Keeping those would
        # hand RRF a rank for a chunk that matched nothing, which is a vote it has not
        # earned.
        return [(chunk, float(score)) for chunk, score in ranked[:limit] if score > 0]

    def _bm25_index(self) -> Any:
        if self._bm25 is None:
            from rank_bm25 import BM25Okapi

            # `lexical_text`, not `embedding_text`: it prepends the citation reference, so the
            # document id and section number are searchable even though neither appears in the
            # chunk's body. Without it a query for `OP-BFP-101` matches nothing.
            self._bm25 = BM25Okapi([_tokens(chunk.lexical_text()) for chunk in self.chunks])
        return self._bm25

    def resolve_reference(self, reference: str) -> list[Chunk]:
        """Find the chunks for a citation string like `OP-BFP-101 §4.2 Low suction …`.

        This is the seam between the MCP result and the RAG query: the alarm API's
        recommendations name procedure sections, and this turns a name into text.

        Exact section matches win. Failing that, the request is widened to the section's
        children — the API frequently cites a parent (`§5 Auxiliary systems`) whose own body
        is a sentence of preamble and whose substance is entirely in `§5.1`–`§5.3`. Returning
        nothing in that case would be technically correct and useless.
        """
        parsed = SectionRef.parse(reference)
        if parsed is None:
            return []

        exact = [
            chunk
            for chunk in self.chunks
            if chunk.doc_id == parsed.doc_id and chunk.section.number == parsed.number
        ]
        children = [chunk for chunk in self.chunks if parsed.covers(chunk.section)]
        # `covers` includes the section itself, so this is exact ∪ descendants, in order.
        return children or exact

    def close(self) -> None:
        """Release the embedded database's file lock and the embedding model.

        Embedded Qdrant holds an exclusive lock on its directory; a second client in the
        same process fails with a lock error. Tests that build and then reopen an index
        must call this in between.

        The embedder is closed too, in the same call, because the index is what owns it from
        every caller's point of view — `HybridRetriever.close`, `CopilotService.aclose` and the
        ingestion CLI all close an index and would otherwise each need to remember a second
        step. See `FastEmbedEmbedder.close` for what goes wrong when nobody does.
        """
        self._client.close()
        self.embedder.close()
