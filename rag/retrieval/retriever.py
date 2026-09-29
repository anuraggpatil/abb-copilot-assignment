"""Hybrid retrieval: dense + BM25, fused by reciprocal rank, turned into citations.

**Why reciprocal-rank fusion and not a weighted score sum.** Cosine similarity lands in
roughly 0.3–0.9 and BM25 is unbounded — it depends on corpus statistics and query length, so
the same query can score 4 on one corpus and 22 on another. Any `α·dense + β·lexical` needs
those onto a common scale, and every way of doing that (min-max over the candidate set,
z-scores) makes the weights depend on the result set being normalised. RRF throws the
magnitudes away and keeps only the ranks:

    score(chunk) = Σ over retrievers of 1 / (k + rank)

That has no weights to tune and is stable when one retriever returns nothing. It does have one
parameter, and `k` is not free: it sets how much rank within a list counts against appearing in
both lists, and the canonical 60 is wrong at this scale — see `RagSettings.rrf_k` for the
measurement and the derivation of 2.

The remaining cost is real
and must be stated, because it decides the shape of the rest of this module: **RRF cannot
support a quality threshold.** It never sees that the top cosine was 0.91 rather than 0.41 —
the top hit scores identically either way, so a threshold on the fused score cannot
distinguish a strong result from the best of a bad set. Ranking needs relative order;
abstention needs an absolute signal. They are different questions and they are answered from
different numbers here.

**The abstention decision is the point of this module, not a detail.** An alarm copilot that
answers confidently from a weak match is worse than one that says it has nothing: an operator
who is told "procedure §4.2 says restart is permitted" acts on it. So confidence is decided on
the **dense cosine** against `min_relevance` — measured, not assumed, and calibrated to the
embedding model in `RagSettings.min_relevance`. BM25 is deliberately not consulted: its scores
do not separate on-topic from off-topic text, so letting it participate would let stopword
matches rescue a decision that should fail. When nothing clears the threshold the result is
flagged, the candidates are still returned for display, and the synthesis step is required to
take the "insufficient documented evidence" path.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from rag.config import RagSettings, get_settings
from rag.models import Chunk, ChunkScores, Citation, RetrievalResult, RetrievedChunk
from rag.retrieval.embedder import build_embedder
from rag.retrieval.store import ChunkIndex

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger(__name__)

#: Longest span quoted into a citation. Long enough to carry a complete instruction, short
#: enough that the GUI shows a quote rather than the chunk, and short enough that a citation
#: cannot become a channel for dumping a whole document into the transcript.
MAX_QUOTE_CHARS = 320

#: Chunks admitted per pinned reference. A long section splits into several chunks, and
#: without a cap one cited section fills most of `top_k` with itself — `OP-BFP-101 §4.2` alone
#: is three chunks, which would leave two slots for every other piece of evidence. Two is
#: enough to carry a section's substance without crowding out corroboration from elsewhere.
MAX_CHUNKS_PER_REFERENCE = 2


class HybridRetriever:
    """Searches the index and returns citable, confidence-scored results."""

    def __init__(self, index: ChunkIndex, settings: RagSettings | None = None) -> None:
        self.index = index
        self.settings = settings or get_settings()

    @classmethod
    def open(cls, settings: RagSettings | None = None) -> HybridRetriever:
        """Open the on-disk index built by `make ingest`."""
        settings = settings or get_settings()
        index = ChunkIndex(
            path=settings.qdrant_path,
            collection=settings.collection,
            embedder=build_embedder(settings),
        )
        index.load()
        return cls(index, settings)

    def close(self) -> None:
        """Release the index's file lock.

        Not optional for an on-disk index: embedded Qdrant takes an exclusive lock, so a
        retriever left open blocks the next `make ingest`. Leaving it to garbage collection
        also prints an `ImportError` from Qdrant's `__del__` at interpreter shutdown, because
        by then the import system is gone.
        """
        self.index.close()

    def __enter__(self) -> HybridRetriever:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        doc_ids: Sequence[str] | None = None,
        doc_types: Sequence[str] | None = None,
        references: Sequence[str] | None = None,
    ) -> RetrievalResult:
        """Retrieve for `query`, optionally filtered, optionally seeded with references.

        `references` is the seam the combined workflow runs through: the MCP tools return
        recommendations that name procedure sections, and those named sections are pulled in
        directly rather than being hoped for from a similarity search. A reference that does
        not resolve is reported in `unresolved_references` rather than dropped, so the
        copilot can say the cited procedure is missing instead of quietly answering from
        whatever else it found.
        """
        top_k = top_k or self.settings.top_k
        limit = self.settings.candidates_per_retriever

        dense = self.index.dense_search(query, limit)
        lexical = self.index.lexical_search(query, limit)

        fused = self._fuse(dense, lexical)
        pinned, unresolved = self._pin_references(references or [], fused)

        allowed = self._filter(fused, doc_ids=doc_ids, doc_types=doc_types)
        # Pinned chunks bypass the score filter but not the document filter: they were asked
        # for by name, so their rank is irrelevant, but a caller scoping to one document
        # meant it.
        ordered = self._merge_pinned(allowed, pinned, doc_ids=doc_ids, doc_types=doc_types)
        selected = ordered[:top_k]

        top_score = max((item.scores.fused for item in selected), default=None)
        # The confidence decision reads the dense cosine of everything the *search* returned,
        # not of what was selected. A pinned reference carries a synthetic score of 1.0, and
        # letting that count as evidence would mean naming a section made the answer
        # confident regardless of whether anything actually matched the question.
        top_relevance = max((score for _, score in dense), default=None)
        low_confidence, note = self._assess(selected, top_relevance, unresolved)

        return RetrievalResult(
            query=query,
            chunks=selected,
            citations=[_citation(item, query) for item in selected],
            low_confidence=low_confidence,
            top_score=top_score,
            top_relevance=top_relevance,
            confidence_note=note,
            unresolved_references=unresolved,
            filters={
                "doc_ids": list(doc_ids or []),
                "doc_types": list(doc_types or []),
                "references": list(references or []),
                "min_relevance": self.settings.min_relevance,
            },
            candidates_considered=len({chunk.chunk_id for chunk, _ in [*dense, *lexical]}),
        )

    def fetch_references(self, references: Sequence[str]) -> RetrievalResult:
        """Resolve cited sections with no similarity search at all.

        Used when the question *is* "what does the procedure the API cited actually say".
        Ranking would only add noise: the sections were named, so they are returned in
        document order with a fused score of 1.0 and confidence is decided solely by whether
        they resolved.
        """
        resolved: list[RetrievedChunk] = []
        unresolved: list[str] = []
        seen: set[str] = set()

        for reference in references:
            chunks = self.index.resolve_reference(reference)
            if not chunks:
                unresolved.append(reference)
                continue
            for chunk in chunks:
                if chunk.chunk_id not in seen:
                    seen.add(chunk.chunk_id)
                    resolved.append(
                        RetrievedChunk(
                            chunk=chunk,
                            scores=ChunkScores(fused=1.0, synthetic=True),
                            pinned=True,
                        )
                    )

        query = "; ".join(references)
        note = (
            f"{len(unresolved)} cited reference(s) are not in the indexed corpus: "
            + ", ".join(unresolved)
            if unresolved
            else None
        )
        return RetrievalResult(
            query=query,
            chunks=resolved,
            citations=[_citation(item, query) for item in resolved],
            low_confidence=not resolved,
            # Both scores are 1.0 by fiat, not by measurement: these sections were named, not
            # matched. `top_relevance` is set so the field is not read as "similarity was
            # never computed" — but nothing here was ranked, and the trace says so via the
            # `references` filter.
            top_score=1.0 if resolved else None,
            top_relevance=1.0 if resolved else None,
            confidence_note=note,
            unresolved_references=unresolved,
            filters={"references": list(references)},
            candidates_considered=len(resolved),
        )

    # --- internals ---------------------------------------------------------------

    def _fuse(
        self,
        dense: list[tuple[Chunk, float]],
        lexical: list[tuple[Chunk, float]],
    ) -> list[RetrievedChunk]:
        """Combine two ranked lists by reciprocal rank, keeping both raw scores."""
        k = self.settings.rrf_k
        chunks: dict[str, Chunk] = {}
        parts: dict[str, dict[str, float | int]] = {}

        for label, results in (("dense", dense), ("lexical", lexical)):
            for rank, (chunk, score) in enumerate(results, start=1):
                chunks[chunk.chunk_id] = chunk
                entry = parts.setdefault(chunk.chunk_id, {})
                entry[label] = score
                entry[f"{label}_rank"] = rank

        fused: list[RetrievedChunk] = []
        for chunk_id, entry in parts.items():
            total = sum(
                1.0 / (k + int(entry[f"{label}_rank"]))
                for label in ("dense", "lexical")
                if f"{label}_rank" in entry
            )
            # Normalised by the best score a chunk could get — first in both lists — so the
            # threshold reads as a fraction of a perfect result rather than as an opaque
            # constant that changes meaning when `rrf_k` does.
            best_possible = 2.0 / (k + 1)
            fused.append(
                RetrievedChunk(
                    chunk=chunks[chunk_id],
                    scores=ChunkScores(
                        dense=_as_float(entry.get("dense")),
                        lexical=_as_float(entry.get("lexical")),
                        dense_rank=_as_int(entry.get("dense_rank")),
                        lexical_rank=_as_int(entry.get("lexical_rank")),
                        fused=total / best_possible,
                    ),
                )
            )

        fused.sort(key=lambda item: -item.scores.fused)
        return fused

    def _pin_references(
        self, references: Sequence[str], fused: list[RetrievedChunk]
    ) -> tuple[list[RetrievedChunk], list[str]]:
        """Resolve named sections, reusing their fused score where they also ranked."""
        scored = {item.chunk.chunk_id: item.scores for item in fused}
        pinned: list[RetrievedChunk] = []
        unresolved: list[str] = []
        seen: set[str] = set()

        for reference in references:
            chunks = self.index.resolve_reference(reference)
            if not chunks:
                unresolved.append(reference)
                continue
            # Prefer the chunks that search also ranked: within one cited section, the chunk
            # the question actually matched is a better representative than the section's
            # first paragraph.
            ordered = sorted(
                chunks, key=lambda c: -(scored[c.chunk_id].fused if c.chunk_id in scored else 0.0)
            )
            for chunk in ordered[:MAX_CHUNKS_PER_REFERENCE]:
                if chunk.chunk_id in seen:
                    continue
                seen.add(chunk.chunk_id)
                pinned.append(
                    RetrievedChunk(
                        chunk=chunk,
                        # A pinned chunk that search also found keeps its real scores, so the
                        # trace shows it was corroborated rather than merely asserted. One that
                        # search did not find gets 1.0 so it sorts first — it was asked for by
                        # name — but flagged synthetic, because that 1.0 is a sort key and not
                        # a measurement, and `_citation` must not publish it as one.
                        scores=scored.get(chunk.chunk_id, ChunkScores(fused=1.0, synthetic=True)),
                        pinned=True,
                    )
                )
        return pinned, unresolved

    def _filter(
        self,
        items: list[RetrievedChunk],
        *,
        doc_ids: Sequence[str] | None,
        doc_types: Sequence[str] | None,
    ) -> list[RetrievedChunk]:
        return [item for item in items if _passes(item.chunk, doc_ids, doc_types)]

    def _merge_pinned(
        self,
        ranked: list[RetrievedChunk],
        pinned: list[RetrievedChunk],
        *,
        doc_ids: Sequence[str] | None,
        doc_types: Sequence[str] | None,
    ) -> list[RetrievedChunk]:
        """Pinned chunks first, then ranked results, without duplicates."""
        out: list[RetrievedChunk] = []
        seen: set[str] = set()
        for item in [*pinned, *ranked]:
            if item.chunk.chunk_id in seen:
                continue
            if not _passes(item.chunk, doc_ids, doc_types):
                continue
            seen.add(item.chunk.chunk_id)
            out.append(item)
        return out

    def _assess(
        self,
        selected: list[RetrievedChunk],
        top_relevance: float | None,
        unresolved: Sequence[str],
    ) -> tuple[bool, str | None]:
        """Decide whether this result is strong enough to answer from.

        Keyed on the absolute dense similarity, never on the fused rank score — see
        `RagSettings.min_relevance` for why the fused score cannot serve here.

        The two findings — weak evidence, and a cited section that is missing — are reported
        together rather than as alternatives. They are independent, and an earlier version
        that returned on the first one lost the missing-citation warning in exactly the case
        where it matters most: thin evidence *and* the procedure the API named absent is the
        situation most likely to end in an answer improvised about the wrong document.
        """
        missing = (
            ", ".join(unresolved)
            + (" is" if len(unresolved) == 1 else " are")
            + " cited by the alarm API and not in the indexed corpus."
            if unresolved
            else None
        )

        if not selected:
            return True, _joined(
                "Nothing in the indexed procedures matched this question.", missing
            )

        if top_relevance is None or top_relevance < self.settings.min_relevance:
            shown = "0.00" if top_relevance is None else f"{top_relevance:.2f}"
            return True, _joined(
                f"Closest passage scored {shown} similarity, below the "
                f"{self.settings.min_relevance:.2f} threshold. The passages below are the "
                "nearest found, but the indexed procedures do not appear to cover this "
                "question — treat them as leads, not as documented guidance.",
                missing,
            )

        if missing:
            # Not low confidence: search found strong material. But the specific procedure
            # the API pointed at is missing, and the answer must say so rather than imply the
            # citation was honoured.
            return False, f"Retrieval succeeded, but {missing}"

        return False, None


def _joined(note: str, missing: str | None) -> str:
    return note if missing is None else f"{note} Separately, {missing}"


def _passes(chunk: Chunk, doc_ids: Sequence[str] | None, doc_types: Sequence[str] | None) -> bool:
    if doc_ids and chunk.doc_id not in doc_ids:
        return False
    return not (doc_types and chunk.doc_type not in doc_types)


def _citation(item: RetrievedChunk, query: str) -> Citation:
    chunk = item.chunk
    return Citation(
        reference=chunk.reference,
        doc_id=chunk.doc_id,
        doc_title=chunk.doc_title,
        revision=chunk.revision,
        section_number=chunk.section.number,
        section_title=chunk.section.title,
        quote=_quote(chunk.text, query),
        chunk_id=chunk.chunk_id,
        source_path=chunk.source_path,
        # A synthetic 1.0 is withheld rather than published. It is a sort key for a section
        # that was named, and showing it next to genuinely ranked citations scoring 0.21 would
        # present the one passage nothing matched as the best evidence in the answer.
        score=None if item.scores.synthetic else item.scores.fused,
        selected_by="reference" if item.pinned else "search",
    )


def _quote(text: str, query: str) -> str:
    """Pick the span of `text` most worth showing, verbatim.

    Sentence-level rather than a truncated prefix: a citation whose quote is the chunk's
    first 320 characters usually quotes the section's preamble instead of the instruction
    that made it match. Scoring by query-term overlap picks the sentence the reader wants,
    and neighbours are appended while the budget allows so the quote is not a fragment.
    """
    sentences = [
        part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-Z§\[*|`])", text) if part.strip()
    ]
    if not sentences:
        return text[:MAX_QUOTE_CHARS]

    terms = {token for token in re.findall(r"[a-z0-9]+", query.lower()) if len(token) > 2}
    scores = [
        (len(terms & set(re.findall(r"[a-z0-9]+", sentence.lower()))), -index, index)
        for index, sentence in enumerate(sentences)
    ]
    _, _, best = max(scores)

    quote = sentences[best]
    # Grow forward from the best sentence: the instruction's qualifications ("…unless the
    # standby pump is unavailable") follow it far more often than they precede it.
    for sentence in sentences[best + 1 :]:
        if len(quote) + 1 + len(sentence) > MAX_QUOTE_CHARS:
            break
        quote = f"{quote} {sentence}"

    return quote if len(quote) <= MAX_QUOTE_CHARS else quote[: MAX_QUOTE_CHARS - 1].rstrip() + "…"


def _as_float(value: float | int | None) -> float | None:
    return None if value is None else float(value)


def _as_int(value: float | int | None) -> int | None:
    return None if value is None else int(value)
