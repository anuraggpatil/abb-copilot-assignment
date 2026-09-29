"""Tests for the heading-aware chunker.

The properties asserted here are the ones citations depend on. If a chunk can span two
sections, or a table can be cut in half, then a citation can point at text the section does
not contain — and in this application that means sending somebody to the wrong page of a
procedure during an alarm.
"""

from __future__ import annotations

import pytest

from rag.ingestion.chunker import (
    _is_atomic,
    _sentences,
    chunk_document,
    estimate_tokens,
)
from rag.models import Chunk, SourceDocument


def _document(body: str, **overrides: object) -> SourceDocument:
    defaults: dict[str, object] = {
        "doc_id": "TEST-1",
        "title": "Test Procedure",
        "doc_type": "operating_procedure",
        "revision": "1",
        "effective_date": "2026-01-01",
        "owner": "Testing",
        "path": "rag/tests/inline.md",
        "body": body,
    }
    return SourceDocument.model_validate({**defaults, **overrides})


class TestSectionBoundaries:
    """A chunk belongs to exactly one section. Everything else is negotiable; this is not."""

    def test_each_chunk_carries_the_section_it_came_from(self) -> None:
        document = _document(
            "# Test Procedure\n\n"
            "## §1 First\n\nAlpha text about the first thing.\n\n"
            "## §2 Second\n\nBeta text about the second thing.\n"
        )
        chunks = chunk_document(document)

        by_section = {chunk.section.number: chunk for chunk in chunks}
        assert set(by_section) == {"1", "2"}
        assert "Alpha" in by_section["1"].text
        assert "Beta" not in by_section["1"].text
        assert "Beta" in by_section["2"].text

    def test_no_chunk_spans_two_sections_even_when_both_would_fit(self) -> None:
        # Two tiny adjacent sections. A size-driven chunker would merge them to fill its
        # budget; merging them would make the combined chunk uncitable.
        document = _document("# Test Procedure\n\n## §1 Tiny\n\nOne.\n\n## §2 Also tiny\n\nTwo.\n")
        chunks = chunk_document(document, max_tokens=500)

        assert len(chunks) == 2
        assert [chunk.section.number for chunk in chunks] == ["1", "2"]

    def test_a_parent_section_with_no_body_produces_no_chunk(self) -> None:
        # `## §4 Abnormal conditions` followed immediately by `### §4.1` has no text of its
        # own. Indexing an empty chunk for it would put a heading with no content into the
        # results.
        document = _document(
            "# Test Procedure\n\n## §4 Parent\n\n### §4.1 Child\n\nThe actual content.\n"
        )
        chunks = chunk_document(document)

        assert [chunk.section.number for chunk in chunks] == ["4.1"]

    def test_text_before_the_first_section_is_dropped(self) -> None:
        # Front matter prose has no section number, so no citation could name it.
        document = _document(
            "# Test Procedure\n\nThis preamble belongs to no section.\n\n"
            "## §1 Real\n\nCitable content.\n"
        )
        chunks = chunk_document(document)

        assert len(chunks) == 1
        assert "preamble" not in chunks[0].text

    def test_deeper_numbering_does_not_leak_into_a_later_sibling(self) -> None:
        document = _document(
            "# Test Procedure\n\n"
            "## §1 One\n\nText one.\n\n"
            "### §1.1 One-one\n\nText one-one.\n\n"
            "## §2 Two\n\nText two.\n"
        )
        chunks = chunk_document(document)

        trail = {chunk.section.number: chunk.heading_path for chunk in chunks}
        assert trail["1.1"] == ["§1 One", "§1.1 One-one"]
        # §2 must not inherit §1.1 from the previous branch.
        assert trail["2"] == ["§2 Two"]


class TestHeadingPath:
    def test_the_trail_excludes_the_document_title(self) -> None:
        # `doc_title` already carries it and `embedding_text` already prepends it. Including
        # it here put the title into every embedding twice.
        document = _document("# Test Procedure\n\n## §1 First\n\nContent.\n")
        chunk = chunk_document(document)[0]

        assert chunk.heading_path == ["§1 First"]
        assert chunk.embedding_text().count("Test Procedure") == 1

    def test_embedding_text_prepends_the_trail_so_a_bare_chunk_is_findable(self) -> None:
        # "Switch to the standby element" says nothing about lube oil filters on its own.
        document = _document(
            "# Pump Manual\n\n## §5 Auxiliary systems\n\n"
            "### §5.1 Lube oil filters\n\nSwitch to the standby element.\n",
            title="Pump Manual",
        )
        chunk = chunk_document(document)[0]
        embedded = chunk.embedding_text()

        assert "Pump Manual" in embedded
        assert "§5 Auxiliary systems" in embedded
        assert "§5.1 Lube oil filters" in embedded
        assert embedded.endswith("Switch to the standby element.")

    def test_an_unnumbered_subheading_joins_the_trail_without_starting_a_section(self) -> None:
        document = _document("# Test Procedure\n\n## §1 First\n\nIntro.\n\n### Notes\n\nA note.\n")
        chunks = chunk_document(document)

        # "Notes" is not citable, so it opens no section of its own.
        assert all(chunk.section.number == "1" for chunk in chunks)


class TestAtomicBlocks:
    def test_a_table_is_never_split(self) -> None:
        rows = "\n".join(f"| Tag-{i:03d} | {i * 1.5:.1f} bar | Alarm | Trip |" for i in range(60))
        document = _document(
            f"# Test Procedure\n\n## §1 Setpoints\n\n"
            f"| Tag | Setpoint | Action | Trip |\n|---|---|---|---|\n{rows}\n"
        )
        chunks = chunk_document(document, max_tokens=100)

        holding = [chunk for chunk in chunks if "Tag-000" in chunk.text]
        assert len(holding) == 1, "the table was split across chunks"
        # Half a setpoint table is worse than none: row 59 might be the trip limit.
        assert "Tag-059" in holding[0].text
        assert holding[0].token_estimate > 100, "oversized on purpose, not accidentally"

    def test_a_numbered_step_list_is_never_split(self) -> None:
        steps = "\n".join(
            f"{i}. Perform startup action number {i} carefully." for i in range(1, 40)
        )
        document = _document(f"# Test Procedure\n\n## §2 Startup\n\n{steps}\n")
        chunks = chunk_document(document, max_tokens=80)

        holding = [chunk for chunk in chunks if "action number 1 " in chunk.text]
        assert len(holding) == 1
        assert "action number 39" in holding[0].text

    def test_a_table_broken_by_a_blank_line_is_rejoined(self) -> None:
        body = (
            "# Test Procedure\n\n## §1 Setpoints\n\n"
            "| Tag | Value |\n|---|---|\n| A | 1 |\n\n| B | 2 |\n| C | 3 |\n"
        )
        chunks = chunk_document(_document(body), max_tokens=500)

        assert len(chunks) == 1
        assert all(tag in chunks[0].text for tag in ("| A |", "| B |", "| C |"))

    @pytest.mark.parametrize(
        ("block", "atomic"),
        [
            ("| a | b |\n| c | d |", True),
            ("1. First step\n2. Second step", True),
            ("Just a paragraph of prose.", False),
            ("| only one row |", False),
            ("1. A lone numbered item", False),
            ("", False),
        ],
    )
    def test_atomic_detection(self, block: str, atomic: bool) -> None:
        assert _is_atomic(block) is atomic


class TestBudgetSplitting:
    def test_a_long_section_splits_but_keeps_one_section_ref(self) -> None:
        paragraphs = "\n\n".join(
            f"Paragraph {i} describes pump behaviour in detail. " * 8 for i in range(12)
        )
        document = _document(f"# Test Procedure\n\n## §3 Long\n\n{paragraphs}\n")
        chunks = chunk_document(document, max_tokens=200)

        assert len(chunks) > 1
        assert {chunk.section.number for chunk in chunks} == {"3"}
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    def test_prose_chunks_respect_the_budget(self) -> None:
        paragraphs = "\n\n".join(
            f"Paragraph {i} about pump operation and its consequences. " * 6 for i in range(15)
        )
        chunks = chunk_document(
            _document(f"# Test Procedure\n\n## §3 Long\n\n{paragraphs}\n"),
            max_tokens=200,
            overlap_sentences=0,
        )

        # No table or step list here, so nothing has a licence to exceed the budget. A little
        # slack for the estimator being an estimator.
        assert max(chunk.token_estimate for chunk in chunks) <= 240

    def test_overlap_carries_the_previous_sentence_forward(self) -> None:
        body = " ".join(f"Sentence number {i} explains a distinct fact." for i in range(1, 60))
        chunks = chunk_document(
            _document(f"# Test Procedure\n\n## §1 Long\n\n{body}\n"),
            max_tokens=120,
            overlap_sentences=1,
        )

        assert len(chunks) > 1
        # Each chunk after the first must begin with the last sentence of the one before.
        for previous, current in zip(chunks, chunks[1:], strict=False):
            tail = _sentences(previous.text)[-1]
            assert current.text.startswith(tail), f"{current.text[:60]!r} lost its overlap"

    def test_overlap_can_be_switched_off(self) -> None:
        body = " ".join(f"Sentence number {i} explains a distinct fact." for i in range(1, 60))
        chunks = chunk_document(
            _document(f"# Test Procedure\n\n## §1 Long\n\n{body}\n"),
            max_tokens=120,
            overlap_sentences=0,
        )
        for previous, current in zip(chunks, chunks[1:], strict=False):
            assert not current.text.startswith(_sentences(previous.text)[-1])


class TestSentenceSplitting:
    @pytest.mark.parametrize(
        ("text", "count"),
        [
            ("First sentence. Second sentence.", 2),
            # The corpus is full of decimals and section marks; a bare `[.!?]\s` split
            # shatters on both, which is why the pattern requires a capital or a §.
            ("Vibration alerts at 4.5 mm/s and trips at 7.1 mm/s.", 1),
            ("Apply §3.2 before restarting. Then log it.", 2),
            ("See §4.2 for the response.", 1),
        ],
    )
    def test_sentence_boundaries(self, text: str, count: int) -> None:
        assert len(_sentences(text)) == count


class TestIdentity:
    def test_chunk_ids_are_stable_across_runs(self) -> None:
        document = _document("# Test Procedure\n\n## §1 First\n\nAlpha.\n\n## §2 Second\n\nBeta.\n")
        first = [chunk.chunk_id for chunk in chunk_document(document)]
        second = [chunk.chunk_id for chunk in chunk_document(document)]

        assert first == second
        # Derived from doc_id + section + ordinal, so re-ingesting an unchanged document
        # produces the same ids and stored citations keep resolving.
        assert first == ["TEST-1#1#0", "TEST-1#2#0"]

    def test_chunk_ids_are_unique_across_the_real_corpus(self, chunks: list[Chunk]) -> None:
        ids = [chunk.chunk_id for chunk in chunks]
        assert len(ids) == len(set(ids))

    def test_safety_critical_flag_propagates_to_every_chunk(self) -> None:
        document = _document(
            "# Test Procedure\n\n## §1 Isolation\n\nLock out the pump.\n", safety_critical=True
        )
        assert all(chunk.safety_critical for chunk in chunk_document(document))


class TestRealCorpus:
    def test_every_chunk_has_a_resolvable_reference(self, chunks: list[Chunk]) -> None:
        from rag.models import SectionRef

        for chunk in chunks:
            parsed = SectionRef.parse(chunk.reference)
            assert parsed is not None, f"{chunk.reference!r} does not parse"
            assert parsed.doc_id == chunk.doc_id
            assert parsed.number == chunk.section.number

    def test_no_chunk_is_empty_or_whitespace(self, chunks: list[Chunk]) -> None:
        assert all(chunk.text.strip() for chunk in chunks)

    def test_oversized_chunks_are_only_ever_atomic_blocks(self, chunks: list[Chunk]) -> None:
        # A prose chunk over budget would mean the splitter failed. A table or step list over
        # budget is the documented, deliberate behaviour.
        for chunk in chunks:
            if chunk.token_estimate > 380:
                assert any(_is_atomic(block) for block in chunk.text.split("\n\n")), (
                    f"{chunk.reference} is oversized but holds no atomic block"
                )

    def test_estimate_tokens_never_returns_zero(self) -> None:
        # A zero would make a chunk free, and the packer would admit it forever.
        assert estimate_tokens("") == 1
        assert estimate_tokens("a") == 1
