"""Tests for corpus loading.

The bias in every assertion here is that a corpus problem must be *loud*. A document that
silently fails to load is a procedure that silently cannot be cited, and the copilot would
then answer a safety question from an incomplete corpus with no indication anything is
missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.ingestion.loader import DocumentLoadError, load_corpus, load_document
from rag.models import SourceDocument

VALID = """---
doc_id: OP-TEST-1
title: Test Operating Procedure
doc_type: operating_procedure
revision: 3
effective_date: 2026-02-01
owner: Operations
applies_to:
  - Test Pump 1
safety_critical: true
custom_key: kept verbatim
---

# Test Operating Procedure

## §1 Scope

This applies to Test Pump 1.
"""


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


class TestFrontmatter:
    def test_all_declared_fields_are_read(self, tmp_path: Path) -> None:
        document = load_document(_write(tmp_path, "doc.md", VALID))

        assert document.doc_id == "OP-TEST-1"
        assert document.title == "Test Operating Procedure"
        assert document.doc_type == "operating_procedure"
        assert document.applies_to == ["Test Pump 1"]
        assert document.safety_critical is True

    def test_numeric_and_date_values_become_strings(self, tmp_path: Path) -> None:
        # YAML parses `revision: 3` as an int and `2026-02-01` as a `datetime.date`. Both are
        # identifiers here, not quantities, and a citation renders them as text.
        document = load_document(_write(tmp_path, "doc.md", VALID))

        assert document.revision == "3"
        assert document.effective_date == "2026-02-01"

    def test_unrecognised_keys_are_kept_in_metadata_not_dropped(self, tmp_path: Path) -> None:
        document = load_document(_write(tmp_path, "doc.md", VALID))

        assert document.metadata == {"custom_key": "kept verbatim"}

    def test_the_body_excludes_the_frontmatter(self, tmp_path: Path) -> None:
        document = load_document(_write(tmp_path, "doc.md", VALID))

        assert "doc_id" not in document.body
        assert document.body.startswith("# Test Operating Procedure")

    @pytest.mark.parametrize(
        "missing", ["doc_id", "title", "doc_type", "revision", "effective_date", "owner"]
    )
    def test_a_missing_required_key_is_fatal_and_named(self, tmp_path: Path, missing: str) -> None:
        content = "\n".join(
            line for line in VALID.splitlines() if not line.startswith(f"{missing}:")
        )
        path = _write(tmp_path, "doc.md", content)

        with pytest.raises(DocumentLoadError, match=missing):
            load_document(path)

    def test_no_frontmatter_block_is_fatal(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "doc.md", "# Just markdown\n\n## §1 Scope\n\nText.\n")

        with pytest.raises(DocumentLoadError, match="no YAML frontmatter"):
            load_document(path)

    def test_an_unclosed_frontmatter_block_is_fatal(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "doc.md", "---\ndoc_id: X\n\n# Body with no closing fence\n")

        with pytest.raises(DocumentLoadError, match="not closed"):
            load_document(path)

    def test_invalid_yaml_is_fatal(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "doc.md", "---\ndoc_id: [unclosed\n---\n\n# Body\n")

        with pytest.raises(DocumentLoadError, match="not valid YAML"):
            load_document(path)

    def test_scalar_frontmatter_is_fatal(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "doc.md", "---\njust a string\n---\n\n# Body\n")

        with pytest.raises(DocumentLoadError, match="must be a mapping"):
            load_document(path)


class TestProvenance:
    def test_an_absolute_path_inside_the_repo_is_recorded_relative(self, tmp_path: Path) -> None:
        nested = tmp_path / "rag" / "documents"
        nested.mkdir(parents=True)
        path = _write(nested, "doc.md", VALID)

        document = load_document(path, repo_root=tmp_path)

        assert document.path == "rag/documents/doc.md"

    def test_a_path_outside_the_root_degrades_instead_of_raising(self, tmp_path: Path) -> None:
        # `RAG_DOCUMENTS_DIR` is relative by default and a fixture may live in a temp dir.
        # Provenance is for a human reading a citation; failing the ingest over it is wrong.
        path = _write(tmp_path, "doc.md", VALID)

        document = load_document(path, repo_root=Path("/nonexistent/root"))

        assert document.path == str(path)

    def test_no_root_keeps_the_path_as_given(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "doc.md", VALID)

        assert load_document(path).path == str(path)


class TestCorpus:
    def test_documents_load_in_sorted_order(self, tmp_path: Path) -> None:
        for name, doc_id in (("c.md", "C"), ("a.md", "A"), ("b.md", "B")):
            _write(tmp_path, name, VALID.replace("OP-TEST-1", doc_id))

        # Deterministic order means deterministic chunk ordinals and a reviewable diff of the
        # ingest report between runs.
        assert [d.doc_id for d in load_corpus(tmp_path)] == ["A", "B", "C"]

    def test_an_empty_directory_is_fatal(self, tmp_path: Path) -> None:
        # An empty index answers every question with "no documented evidence", which looks
        # exactly like a working low-confidence path rather than the misconfiguration it is.
        with pytest.raises(DocumentLoadError, match="nothing to index"):
            load_corpus(tmp_path)

    def test_a_missing_directory_is_fatal(self, tmp_path: Path) -> None:
        with pytest.raises(DocumentLoadError, match="not a directory"):
            load_corpus(tmp_path / "nope")

    def test_duplicate_doc_ids_are_fatal_and_both_files_named(self, tmp_path: Path) -> None:
        _write(tmp_path, "first.md", VALID)
        _write(tmp_path, "second.md", VALID)

        with pytest.raises(DocumentLoadError, match="duplicate doc_id") as caught:
            load_corpus(tmp_path)

        # A reference resolving to two documents is unfixable without knowing which files
        # collided, so the message must name them.
        assert "first.md" in str(caught.value)
        assert "second.md" in str(caught.value)

    def test_non_markdown_files_are_ignored(self, tmp_path: Path) -> None:
        _write(tmp_path, "doc.md", VALID)
        _write(tmp_path, "notes.txt", "not a document")
        _write(tmp_path, "data.json", "{}")

        assert len(load_corpus(tmp_path)) == 1

    def test_the_markdown_extension_variant_is_accepted(self, tmp_path: Path) -> None:
        _write(tmp_path, "doc.markdown", VALID)

        assert len(load_corpus(tmp_path)) == 1


class TestRealCorpus:
    def test_the_authored_corpus_loads(self, documents: list[SourceDocument]) -> None:
        assert len(documents) == 4
        assert {d.doc_id for d in documents} == {
            "OP-BFP-101",
            "TS-BFP-VIB-CAV",
            "MM-CP-MAINT",
            "SAF-PUMP-LOTO",
        }

    def test_the_loto_document_is_marked_safety_critical(
        self, documents: list[SourceDocument]
    ) -> None:
        by_id = {d.doc_id: d for d in documents}

        assert by_id["SAF-PUMP-LOTO"].safety_critical is True
        # The flag is meant to distinguish, so it must not be set on everything.
        assert by_id["MM-CP-MAINT"].safety_critical is False

    def test_every_document_declares_what_it_applies_to(
        self, documents: list[SourceDocument]
    ) -> None:
        for document in documents:
            assert document.applies_to, f"{document.doc_id} declares no applies_to"

    def test_paths_are_repo_relative_so_citations_are_portable(
        self, documents: list[SourceDocument]
    ) -> None:
        for document in documents:
            assert document.path.startswith("rag/documents/")
            assert not Path(document.path).is_absolute()
