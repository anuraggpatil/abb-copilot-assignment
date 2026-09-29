"""Shared fixtures for the RAG tests.

Everything here is built on the **hash embedder** and an in-memory Qdrant, so the whole
module runs with no network, no model download and no files left behind. The tests that need
the real model are separated into `test_retrieval_semantic.py` and skip when it is absent —
see that module for why the split is deliberate rather than convenient.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag.config import RagSettings
from rag.ingestion.chunker import chunk_corpus, chunk_document
from rag.ingestion.loader import load_corpus, load_document
from rag.models import Chunk, SourceDocument
from rag.retrieval.embedder import HashEmbedder
from rag.retrieval.retriever import HybridRetriever
from rag.retrieval.store import ChunkIndex

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS_DIR = REPO_ROOT / "rag" / "documents"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
POISONED = FIXTURES_DIR / "POISONED-pump-procedure.md"


@pytest.fixture(scope="session")
def documents() -> list[SourceDocument]:
    """The real authored corpus. Session-scoped: reading it is pure and not cheap."""
    return load_corpus(DOCUMENTS_DIR, repo_root=REPO_ROOT)


@pytest.fixture(scope="session")
def chunks(documents: list[SourceDocument]) -> list[Chunk]:
    return chunk_corpus(documents)


@pytest.fixture
def poisoned_document() -> SourceDocument:
    """The hostile fixture, loaded through the real loader so it is really sanitised."""
    return load_document(POISONED, repo_root=REPO_ROOT)


@pytest.fixture
def poisoned_chunks(poisoned_document: SourceDocument) -> list[Chunk]:
    return chunk_document(poisoned_document)


@pytest.fixture
def settings() -> RagSettings:
    """Settings that never touch the developer's `.env` or download a model.

    `_env_file=None` matters: without it, whether these tests pass depends on what is in a
    local `.env`, which is the kind of test that passes on one machine and fails in CI.
    """
    return RagSettings(
        _env_file=None,
        RAG_EMBEDDER="hash",
        RAG_COLLECTION="test_procedures",
        RAG_TOP_K=5,
        RAG_MIN_RELEVANCE=0.60,
    )


@pytest.fixture
def index(chunks: list[Chunk], settings: RagSettings) -> ChunkIndex:
    """An in-memory index over the real corpus."""
    from qdrant_client import QdrantClient

    built = ChunkIndex(
        path=Path(":memory:"),
        collection=settings.collection,
        embedder=HashEmbedder(),
        client=QdrantClient(":memory:"),
    )
    built.build(chunks)
    return built


@pytest.fixture
def retriever(index: ChunkIndex, settings: RagSettings) -> HybridRetriever:
    return HybridRetriever(index, settings)
