"""Fixtures for the end-to-end run.

The app is the real one: `create_app` with the real routes, the real orchestrator, the real
registry, the real MCP server and a real index over the real authored corpus. Two things are
substituted and each for a reason stated in `tests/conftest.py` — the LLM (so the scenario is
deterministic and needs no token) and the embedder (so a checkout is green without a 50MB
download). Nothing is monkeypatched, so the code under test is the code that is served.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.backend.api.main import create_app
from apps.backend.config import BackendSettings
from apps.backend.llm.scripted import ScriptedProvider
from tests.scenario import acceptance_script

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def e2e_retriever(rag_settings: Any) -> Iterator[Any]:
    """An index of this run's own, because the app closes the one it is handed.

    `CopilotService.aclose` releases the embedded database's file lock on shutdown — correct in
    production, and the reason this cannot be the session-scoped `procedure_retriever` that other
    tests share. Rebuilding costs about 40ms.
    """
    from qdrant_client import QdrantClient

    from rag.ingestion.chunker import chunk_corpus
    from rag.ingestion.loader import load_corpus
    from rag.retrieval.embedder import HashEmbedder
    from rag.retrieval.retriever import HybridRetriever
    from rag.retrieval.store import ChunkIndex

    index = ChunkIndex(
        path=Path(":memory:"),
        collection=rag_settings.collection,
        embedder=HashEmbedder(),
        client=QdrantClient(":memory:"),
    )
    index.build(chunk_corpus(load_corpus(REPO_ROOT / "rag" / "documents", repo_root=REPO_ROOT)))
    yield HybridRetriever(index, rag_settings)


@pytest.fixture
def copilot(
    backend_settings: BackendSettings, e2e_retriever: Any, mcp_session_factory: Any
) -> Iterator[TestClient]:
    """The copilot, served, with MCP up."""
    app = create_app(
        backend_settings.model_copy(update={"max_steps": 6}),
        provider=ScriptedProvider(acceptance_script()),
        retriever=e2e_retriever,
        session_factory=mcp_session_factory,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def copilot_without_mcp(
    backend_settings: BackendSettings, e2e_retriever: Any
) -> Iterator[TestClient]:
    """The same app with the MCP server unreachable — the demo's degraded scenario.

    A `ConnectionError` from the factory is what a stopped server actually produces: the backend
    opens a session per request, so the failure arrives when the session is opened rather than at
    startup. The script is the answer-only tail of the acceptance script, because with no alarm
    tools in the catalogue there is nothing for the earlier turns to call.
    """

    def broken_factory() -> Any:
        raise ConnectionError("connection refused on port 9100")

    from apps.backend.llm.scripted import answer_turn, finish_turn, plan_turn

    script = [
        plan_turn(
            ("search_procedures", {"query": "boiler feed pump low suction pressure response"}),
            thought="the alarm tools are unavailable; use what documentation there is",
        ),
        finish_turn(),
        answer_turn(
            "Alarm history is unavailable, so this is documentation only: respond to low suction "
            "pressure as the procedure directs [OP-BFP-101 §4.2]."
        ),
    ]
    app = create_app(
        backend_settings.model_copy(update={"max_steps": 4}),
        provider=ScriptedProvider(script),
        retriever=e2e_retriever,
        session_factory=broken_factory,
    )
    with TestClient(app) as client:
        yield client
