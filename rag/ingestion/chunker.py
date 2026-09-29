"""Split documents into chunks that can be cited.

The design constraint here is unusual and it drives everything: **a chunk must belong to
exactly one numbered section**, because the alarm API cites sections and the answer has to
quote the section it cited. A chunk spanning the boundary between §4.2 and §4.3 cannot be
attributed to either, and a citation that names the wrong section is worse than no citation —
it sends someone to the wrong page of a procedure during an alarm.

So sections are the primary unit and the token budget is secondary. A long section is split
into several chunks that all carry the same `SectionRef`; a short section stays whole even
though it wastes budget. Sections are never merged, even when two adjacent ones would fit
together comfortably.

Two further choices worth stating:

**Headings are carried, not just stored.** Each chunk keeps the full heading trail from the
document title down to its own section, and `Chunk.embedding_text` prepends it. A chunk whose
text begins "Above 1.0 bar differential, switch to the standby element" is about lube oil
filters, but nothing in those words says so — without the trail it is unfindable by the
question it answers.

**Tables and numbered steps are kept intact.** The corpus's setpoints live in tables and its
procedures in numbered lists, and half a setpoint table is actively misleading: an operator
shown rows 1–3 of a table whose row 5 is the trip limit has been given the wrong answer, not
a partial one. When a table cannot fit the budget it gets its own oversized chunk rather than
being cut.
"""

from __future__ import annotations

import re

from rag.models import Chunk, SectionRef, SourceDocument

#: `## §4.2 Low suction pressure response` — the numbered headings that define sections.
_SECTION_HEADING = re.compile(
    r"^(?P<hashes>#{2,6})\s*§\s*(?P<number>\d[\d.]*)\s+(?P<title>.+?)\s*$"
)
#: Any other markdown heading, including the document's own `# Title`.
_ANY_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")

#: Roughly 4 characters per token for English prose. Deliberately an estimate: the true
#: count depends on the embedding model's tokenizer, and the budget is a soft target used to
#: decide where to split, not a hard limit that must be exact.
CHARS_PER_TOKEN = 4

DEFAULT_MAX_TOKENS = 380
DEFAULT_MIN_TOKENS = 40
#: Sentences of the previous chunk repeated at the start of the next, when a section splits.
#: Enough to keep a pronoun or a "this" attached to its subject across the boundary.
DEFAULT_OVERLAP_SENTENCES = 1


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


class _Section:
    """Accumulates the lines belonging to one numbered heading."""

    def __init__(self, ref: SectionRef, heading_path: list[str], level: int) -> None:
        self.ref = ref
        self.heading_path = heading_path
        self.level = level
        self.lines: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


def chunk_document(
    document: SourceDocument,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    overlap_sentences: int = DEFAULT_OVERLAP_SENTENCES,
) -> list[Chunk]:
    """Split one document into citable chunks, in document order."""
    sections = _split_into_sections(document)
    chunks: list[Chunk] = []

    for section in sections:
        body = section.text
        if not body:
            # A heading with no content of its own — a parent like `## §4 Abnormal condition
            # response` whose text is entirely in its subsections. Nothing to index.
            continue

        blocks = _split_to_budget(
            body, max_tokens=max_tokens, min_tokens=min_tokens, overlap_sentences=overlap_sentences
        )
        for ordinal, block in enumerate(blocks):
            chunks.append(
                Chunk(
                    chunk_id=f"{document.doc_id}#{section.ref.number}#{ordinal}",
                    doc_id=document.doc_id,
                    doc_title=document.title,
                    doc_type=document.doc_type,
                    revision=document.revision,
                    section=section.ref,
                    heading_path=list(section.heading_path),
                    text=block,
                    ordinal=ordinal,
                    token_estimate=estimate_tokens(block),
                    safety_critical=document.safety_critical,
                    source_path=document.path,
                )
            )
    return chunks


def chunk_corpus(documents: list[SourceDocument], **kwargs: int) -> list[Chunk]:
    return [chunk for document in documents for chunk in chunk_document(document, **kwargs)]


def _split_into_sections(document: SourceDocument) -> list[_Section]:
    """Walk the markdown, opening a new section at each numbered heading.

    Unnumbered headings below level 1 update the heading trail without starting a section,
    so their following text is attributed to whichever numbered section encloses it. Text
    before the first numbered heading is dropped: it is the title block, and it has no
    section to be cited as.

    The level-1 heading is left out of the trail entirely. It restates the frontmatter
    `title`, which `Chunk.doc_title` already carries and `Chunk.embedding_text` already
    prepends — including it here put the document title in twice, wasting the budget of
    every embedding on a repetition.
    """
    sections: list[_Section] = []
    current: _Section | None = None
    # Heading text by level, so the trail can be rebuilt at any depth.
    trail: dict[int, str] = {}

    for line in document.body.splitlines():
        numbered = _SECTION_HEADING.match(line)
        if numbered:
            level = len(numbered["hashes"])
            title = numbered["title"].strip()
            # Drop any deeper headings left over from the previous branch.
            trail = {lvl: text for lvl, text in trail.items() if lvl < level}
            trail[level] = f"§{numbered['number']} {title}"
            current = _Section(
                ref=SectionRef(doc_id=document.doc_id, number=numbered["number"], title=title),
                heading_path=[trail[lvl] for lvl in sorted(trail)],
                level=level,
            )
            sections.append(current)
            continue

        plain = _ANY_HEADING.match(line)
        if plain:
            level = len(plain["hashes"])
            trail = {lvl: text for lvl, text in trail.items() if lvl < level}
            if level > 1:
                trail[level] = plain["title"].strip()
            # An unnumbered heading does not open a citable section, but it does end the
            # previous one — its text belongs under it, not under the last numbered heading.
            if current is not None and level <= current.level:
                current = None
            continue

        if current is not None:
            current.lines.append(line)

    return sections


def _split_to_budget(
    body: str, *, max_tokens: int, min_tokens: int, overlap_sentences: int
) -> list[str]:
    """Split one section's text into blocks near the token budget.

    Splits at paragraph boundaries, then at sentence boundaries within an oversized
    paragraph. Atomic blocks — tables and numbered step lists — are never split.
    """
    if estimate_tokens(body) <= max_tokens:
        return [body]

    blocks: list[str] = []
    buffer: list[str] = []
    buffered_tokens = 0

    def flush() -> None:
        nonlocal buffer, buffered_tokens
        if buffer:
            blocks.append("\n\n".join(buffer).strip())
            buffer, buffered_tokens = [], 0

    for paragraph in _paragraphs(body):
        tokens = estimate_tokens(paragraph)

        if buffered_tokens and buffered_tokens + tokens > max_tokens:
            flush()

        if tokens > max_tokens and not _is_atomic(paragraph):
            # Too big even alone, and safe to cut: split on sentences.
            flush()
            blocks.extend(_split_sentences(paragraph, max_tokens=max_tokens))
            continue

        # Either it fits, or it is atomic and gets an oversized block of its own.
        buffer.append(paragraph)
        buffered_tokens += tokens
        if _is_atomic(paragraph) and buffered_tokens > max_tokens:
            flush()

    flush()

    merged = _merge_runts(blocks, min_tokens=min_tokens, max_tokens=max_tokens)
    return _add_overlap(merged, sentences=overlap_sentences) if overlap_sentences else merged


def _paragraphs(body: str) -> list[str]:
    """Blank-line-separated paragraphs, with tables and step lists held together.

    A markdown table is separated by single newlines, so paragraph splitting keeps it whole
    already; the case this handles is a table or list interrupted by a blank line, which
    would otherwise be torn apart.
    """
    raw = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()]

    merged: list[str] = []
    for block in raw:
        if merged and _continues_atomic(merged[-1], block):
            merged[-1] = f"{merged[-1]}\n{block}"
        else:
            merged.append(block)
    return merged


def _is_atomic(block: str) -> bool:
    """True for tables and numbered step lists, which lose their meaning when cut."""
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return False
    table_rows = sum(1 for line in lines if line.lstrip().startswith("|"))
    if table_rows >= 2:
        return True
    numbered = sum(1 for line in lines if re.match(r"^\s*\d+\.\s", line))
    return numbered >= 2


def _continues_atomic(previous: str, block: str) -> bool:
    """True when `block` is a continuation of an atomic structure in `previous`."""
    if not _is_atomic(previous):
        return False
    first = block.lstrip()
    return first.startswith("|") or bool(re.match(r"^\d+\.\s", first))


def _split_sentences(paragraph: str, *, max_tokens: int) -> list[str]:
    """Greedy sentence packing, used only for a paragraph that exceeds the budget alone."""
    sentences = _sentences(paragraph)
    blocks: list[str] = []
    buffer: list[str] = []
    tokens = 0

    for sentence in sentences:
        cost = estimate_tokens(sentence)
        if tokens and tokens + cost > max_tokens:
            blocks.append(" ".join(buffer).strip())
            buffer, tokens = [], 0
        buffer.append(sentence)
        tokens += cost

    if buffer:
        blocks.append(" ".join(buffer).strip())
    return blocks


def _sentences(text: str) -> list[str]:
    """Split on sentence-ending punctuation followed by whitespace and a capital.

    Requiring the capital keeps `4.5 mm/s` and `§3.2` from being read as sentence ends,
    which a bare `[.!?]\\s` split does constantly in this corpus.
    """
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z§\[*_`])", text.strip())
    return [part for part in parts if part.strip()]


def _merge_runts(blocks: list[str], *, min_tokens: int, max_tokens: int) -> list[str]:
    """Fold a too-small trailing block into its neighbour.

    Sentence packing tends to leave a final block of a few words. On its own it embeds to
    noise and can outrank a real chunk on a short query, so it is attached to the block it
    came from wherever that fits.
    """
    if len(blocks) < 2:
        return blocks

    out = list(blocks)
    index = len(out) - 1
    while index > 0:
        if estimate_tokens(out[index]) < min_tokens:
            combined = f"{out[index - 1]}\n\n{out[index]}"
            if estimate_tokens(combined) <= max_tokens * 1.5:
                out[index - 1] = combined
                out.pop(index)
        index -= 1
    return out


def _add_overlap(blocks: list[str], *, sentences: int) -> list[str]:
    """Repeat the tail of each block at the head of the next.

    Costs a little index size and buys continuity: a block starting "This is corrected by
    changing the alarm configuration" is meaningless without the sentence before it, and
    that sentence is in the previous block.
    """
    if len(blocks) < 2 or sentences <= 0:
        return blocks

    out = [blocks[0]]
    for previous, block in zip(blocks, blocks[1:], strict=False):
        tail = _sentences(previous)[-sentences:]
        # Never prepend a fragment of a table or step list: it reads as a duplicate row.
        if tail and not _is_atomic(previous) and not _is_atomic(block):
            out.append(f"{' '.join(tail)}\n\n{block}")
        else:
            out.append(block)
    return out
