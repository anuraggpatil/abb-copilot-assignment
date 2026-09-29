"""The types that cross between ingestion, retrieval and the copilot.

Kept in one module because they are a contract, not an implementation detail: the chunker
writes `Chunk`, the index stores its payload, the retriever reconstructs it, and the
synthesis step turns it into a `Citation` the GUI renders. A field added here without
re-indexing is a field the retriever will not find, so the coupling is real and worth
putting in one place where it can be seen.

Two shapes matter more than the rest:

`Chunk.section` carries the document id and section number separately as well as the
formatted `reference`. The alarm API emits references as opaque strings like
`OP-BFP-101 §4.2 Low suction pressure response`; being able to take that apart and match on
`(doc_id, number)` is what lets a recommendation's reference be resolved to the text of the
procedure rather than merely searched for.

`RetrievedChunk.scores` keeps the dense and lexical scores separately alongside the fused
one. A single number cannot answer "why did this rank first", and that question is the whole
content of the trace panel's retrieval row.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

#: `OP-BFP-101 §4.2 Low suction pressure response` -> ("OP-BFP-101", "4.2", "Low suction …")
#: The title is optional because a recommendation may cite a bare section, and a reference
#: that omits it must still resolve.
REFERENCE_PATTERN = re.compile(
    r"^\s*(?P<doc_id>[A-Z][A-Z0-9-]*)\s*§\s*(?P<number>\d[\d.]*)\s*(?P<title>.*?)\s*$"
)


class RagModel(BaseModel):
    """Forbid extras, because every one of these is serialised into an index payload.

    A typo in a keyword argument would otherwise be stored silently and then be missing
    when something reads the field it was meant to set.
    """

    model_config = ConfigDict(extra="forbid")


class SectionRef(RagModel):
    """Where a chunk came from, in the vocabulary the alarm API uses to cite it."""

    doc_id: str = Field(description="e.g. OP-BFP-101")
    number: str = Field(description="Section number as written, e.g. '4.2'")
    title: str = Field(description="Section heading text without the number")

    @property
    def reference(self) -> str:
        """The citation string, formatted the way the alarm API formats its references."""
        return f"{self.doc_id} §{self.number} {self.title}".rstrip()

    @property
    def number_parts(self) -> tuple[int, ...]:
        """For sorting `4.10` after `4.2` rather than before it."""
        return tuple(int(part) for part in self.number.split(".") if part.isdigit())

    def covers(self, other: SectionRef) -> bool:
        """True when `other` is this section or one nested inside it.

        `§4` covers `§4.2`. Needed because a recommendation may cite the parent section
        while the answer lives in a subsection, and vice versa.
        """
        if self.doc_id != other.doc_id:
            return False
        return other.number == self.number or other.number.startswith(f"{self.number}.")

    @classmethod
    def parse(cls, reference: str) -> Self | None:
        """Take apart a reference string, returning None when it is not one.

        Returns None rather than raising: references arrive from the alarm API and, in the
        planner's case, from a language model. A malformed one is a routine event to be
        handled, not an exceptional one.
        """
        match = REFERENCE_PATTERN.match(reference)
        if match is None:
            return None
        return cls(
            doc_id=match["doc_id"],
            number=match["number"],
            title=match["title"],
        )


class SourceDocument(RagModel):
    """One file from `rag/documents/`, after frontmatter parsing and sanitising."""

    doc_id: str
    title: str
    doc_type: str = Field(description="operating_procedure | troubleshooting_guide | …")
    revision: str
    effective_date: str
    owner: str
    path: str = Field(description="Repo-relative source path, for provenance in citations")
    body: str = Field(description="Markdown with the frontmatter removed")
    applies_to: list[str] = Field(default_factory=list)
    safety_critical: bool = False
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Any other frontmatter keys, kept verbatim"
    )
    #: Set when the sanitizer removed or neutralised something. Carried rather than
    #: discarded so `make ingest` can report it and a test can assert on it.
    sanitizer_findings: list[str] = Field(default_factory=list)


class Chunk(RagModel):
    """An indexed unit of text, with enough context to be cited without re-reading the file."""

    chunk_id: str = Field(description="Stable across re-ingestion: doc_id + section + ordinal")
    doc_id: str
    doc_title: str
    doc_type: str
    revision: str
    section: SectionRef
    #: Headings from the document root down to this chunk's section. Prepended to the text
    #: at embedding time so a chunk about "step 4" is not stranded without its subject.
    heading_path: list[str] = Field(default_factory=list)
    text: str
    ordinal: int = Field(description="Position within its section, for multi-chunk sections")
    token_estimate: int
    safety_critical: bool = False
    source_path: str = ""

    @property
    def reference(self) -> str:
        return self.section.reference

    def embedding_text(self) -> str:
        """What actually gets embedded: the heading trail, then the body.

        Without the trail, a chunk reading "Above 1.0 bar differential, switch to the
        standby element" embeds as generic maintenance text and will not be found by a
        question about lube oil filters — the words that make it specific are all in the
        headings above it.
        """
        trail = " > ".join([self.doc_title, *self.heading_path])
        return f"{trail}\n\n{self.text}"

    def lexical_text(self) -> str:
        """What gets indexed for BM25: the citation reference, then the embedding text.

        Separate from `embedding_text` because the two retrievers need different things from
        the same chunk. `embedding_text` omits the document id deliberately — `OP-BFP-101`
        carries no meaning a sentence embedder can use, and it would occupy budget in every
        vector. But the id is exactly what a *lexical* query needs: the alarm API cites
        `OP-BFP-101 §4.2`, an operator types `OP-BFP-101`, and with only the document
        *title* indexed, BM25 could not match either.
        """
        return f"{self.reference}\n{self.embedding_text()}"


class ChunkScores(RagModel):
    """Why a chunk ranked where it did."""

    dense: float | None = Field(
        default=None, description="Cosine similarity, when it was a dense hit"
    )
    lexical: float | None = Field(default=None, description="BM25 score, when it was a lexical hit")
    dense_rank: int | None = None
    lexical_rank: int | None = None
    fused: float = Field(description="Reciprocal-rank-fusion score; the ranking key")
    #: True when `fused` was *assigned* rather than measured — a section pulled in because a
    #: caller named it, which neither retriever independently returned. The flag is necessary
    #: because the assigned value is 1.0: without it, a section nothing matched is
    #: indistinguishable from the strongest hit in the set, and reads as the best evidence
    #: available wherever the number is rendered.
    synthetic: bool = False


class RetrievedChunk(RagModel):
    chunk: Chunk
    scores: ChunkScores
    #: Admitted because a caller asked for this section by name rather than because it ranked.
    #: Kept separate from `scores.synthetic`: a pinned section that search *also* found is
    #: pinned with real scores, and "named and corroborated" versus "named only" is exactly
    #: the distinction a reader needs in order to weigh it.
    pinned: bool = False

    @property
    def reference(self) -> str:
        return self.chunk.reference


class Citation(RagModel):
    """What the GUI renders and the answer must be traceable to.

    `quote` is a span taken verbatim from the chunk, not a summary. A citation the reader
    cannot check against the document is decoration.

    `score` is deliberately optional. A section that was cited by the alarm API and pulled in
    by name has no measured score, and putting a placeholder there — 1.0, which is what the
    internal ranking uses — would render in the GUI as the most relevant passage found. `None`
    plus `selected_by` says what actually happened: this text is here because something named
    it, and no similarity search corroborated it.
    """

    reference: str = Field(description="e.g. OP-BFP-101 §4.2 Low suction pressure response")
    doc_id: str
    doc_title: str
    revision: str
    section_number: str
    section_title: str
    quote: str
    chunk_id: str
    source_path: str
    score: float | None = Field(
        default=None,
        description="Fused retrieval score; None when the section was pinned, not ranked",
    )
    selected_by: Literal["search", "reference"] = Field(
        default="search",
        description="How this passage entered the result: ranked by search, or named by a caller",
    )


class RetrievalResult(RagModel):
    """The whole outcome of one retrieval, including the case where it found nothing good.

    `low_confidence` exists so the caller cannot accidentally treat a weak result as a
    strong one: the chunks are still returned, because showing an operator what was found
    and that it was thin is more useful than an empty response, but the flag is what the
    synthesis step checks before it is allowed to answer from them.
    """

    query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    low_confidence: bool = False
    #: Best fused (reciprocal-rank) score — the ranking key. Comparable *within* one result
    #: set and meaningless across them, since it depends only on rank.
    top_score: float | None = None
    #: Best dense cosine similarity. This is the absolute measure, and it — not `top_score` —
    #: is what `low_confidence` is decided on. Kept in the result so the trace panel can show
    #: the number the abstention decision was actually made from.
    top_relevance: float | None = None
    #: Human-readable note on why confidence is low, shown in the GUI's warning.
    confidence_note: str | None = None
    #: References that were asked for by name and could not be found, so the copilot can say
    #: so rather than silently answering about something else.
    unresolved_references: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    candidates_considered: int = 0
