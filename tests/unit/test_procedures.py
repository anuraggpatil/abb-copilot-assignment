"""The local retrieval tool's contract with the corpus it filters.

One thing is asserted here that no other test could catch, because it is agreement between two
files rather than behaviour of either: the `doc_types` enum the planner is offered and the
`doc_type` values the documents actually declare.

It was wrong. The schema offered `safety_instruction`; `SAF-pump-isolation-loto.md` declares
`safety_procedure`. Nothing failed — the filter is applied after retrieval, so asking for the
safety document returned an empty result that looked exactly like "the corpus does not cover
this", and the one value that *would* have worked was rejected by schema validation as not in
the enum. A copilot cannot be told the difference. Hence this test.
"""

from __future__ import annotations

from pathlib import Path

from apps.backend.orchestration.procedures import DOC_TYPES, MAX_TOP_K, ProcedureSearchTool
from rag.ingestion.loader import load_corpus

REPO_ROOT = Path(__file__).resolve().parents[2]


def _schema() -> dict:
    return ProcedureSearchTool.input_schema


class TestTheDocTypeFilterMatchesTheCorpus:
    def test_every_document_declares_a_type_the_filter_offers(self) -> None:
        documents = load_corpus(REPO_ROOT / "rag" / "documents", repo_root=REPO_ROOT)

        declared = {document.doc_type for document in documents}

        # Not a subset check in one direction only: a type in the corpus that the enum omits
        # is a document the planner cannot filter to, and a type in the enum that no document
        # carries is a filter that silently matches nothing.
        assert declared == set(DOC_TYPES)

    def test_the_offered_enum_is_the_constant_and_not_a_second_copy(self) -> None:
        doc_types = _schema()["properties"]["doc_types"]

        assert set(doc_types["items"]["enum"]) == set(DOC_TYPES)
        assert doc_types["maxItems"] == len(DOC_TYPES)


class TestTheSchemaTheModelIsShown:
    def test_a_parameter_the_tool_does_not_have_is_rejected_rather_than_ignored(self) -> None:
        # `additionalProperties: false` is what turns a hallucinated argument into a visible
        # error instead of a call that quietly does something else.
        assert _schema()["additionalProperties"] is False

    def test_the_query_is_required_and_bounded_and_top_k_is_capped(self) -> None:
        schema = _schema()

        assert schema["required"] == ["query"]
        assert schema["properties"]["query"]["minLength"] == 3
        assert schema["properties"]["top_k"]["maximum"] == MAX_TOP_K
