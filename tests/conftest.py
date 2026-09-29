"""Shared fixtures.

Everything is built against one **frozen instant**. The seeder generates data relative to
`now`, so if the tests used the wall clock then "recurring over the last 90 days" would mean
a slightly different window on every run and assertions on occurrence counts would be
flaky. Freezing it makes the dataset a fixture in the proper sense: exactly reproducible.

Fixtures are session-scoped because the world is read-only once built. Rebuilding 400 days
of data per test would dominate the runtime for no isolation benefit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.alarm_api.config import AlarmApiSettings
from apps.alarm_api.main import create_app
from apps.alarm_api.seed import DEFAULT_SEED, build_world
from apps.alarm_api.store import AlarmStore

# Arbitrary but fixed. Mid-month, mid-day, UTC — so no month-boundary or DST edge case
# hides in a "last 90 days" calculation.
FROZEN_NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)

TEST_TOKEN = "test-token-value"


@pytest.fixture(scope="session")
def frozen_now() -> datetime:
    return FROZEN_NOW


@pytest.fixture(scope="session")
def world():  # noqa: ANN201 - the World dataclass, inferred
    return build_world(FROZEN_NOW, seed=DEFAULT_SEED, days_of_history=400)


@pytest.fixture(scope="session")
def store(world) -> AlarmStore:  # noqa: ANN001
    return AlarmStore(world)


@pytest.fixture(scope="session")
def settings() -> AlarmApiSettings:
    # Constructed explicitly rather than read from the environment: a developer's local
    # .env must not be able to change what the suite asserts.
    return AlarmApiSettings(
        ALARM_API_TOKEN=TEST_TOKEN,
        SEED_RANDOM_SEED=DEFAULT_SEED,
        SEED_DAYS_OF_HISTORY=400,
        LOG_LEVEL="WARNING",
    )


@pytest.fixture(scope="session")
def client(settings: AlarmApiSettings) -> Iterator[TestClient]:
    app = create_app(settings, now=FROZEN_NOW, freeze_now=True)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(scope="session")
def trace_headers() -> dict[str, str]:
    """Auth plus the three trace headers the Postman collections send."""
    return {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "trace_id": "trc-test-0001",
        "x-client-id": "alarm-copilot-tests",
        "x-metadata-tag": "acceptance",
    }


@pytest.fixture(scope="session")
def bfp101_id(store: AlarmStore) -> str:
    """Asset id of Boiler Feed Pump 101 — the acceptance scenario's subject."""
    from apps.alarm_api.seed import BFP101_NAME

    matches = [a for a in store.world.assets if a.name == BFP101_NAME]
    assert matches, f"{BFP101_NAME} must exist in the generated world"
    return matches[0].asset_id


# --- the copilot backend -----------------------------------------------------------------
#
# The backend's integration tests run the real orchestrator, the real registry, the real MCP
# server and a real retrieval index. Only two things are substituted, and each for a stated
# reason:
#
# * the **LLM**, by `ScriptedProvider` — so the headline acceptance test asserts what *our*
#   orchestration did rather than what a model felt like doing, and runs in CI with no token;
# * the **embedder**, by `HashEmbedder` — so no 50MB model download stands between a checkout
#   and a green suite. It ranks well enough to retrieve the right sections on this corpus; what
#   it cannot do is support a calibrated relevance threshold, which is why the abstention
#   behaviour is tested against the real model in `rag/tests/test_retrieval_semantic.py`.
#
# The socket is in-memory too: the MCP client talks to an `MCPServer` object, whose connector
# reaches the alarm API through `httpx.ASGITransport`. Three processes, one event loop, no ports.


@pytest.fixture(scope="session")
def procedure_index():  # noqa: ANN201 - ChunkIndex, inferred
    """An in-memory index over the real authored corpus, hash-embedded."""
    from pathlib import Path

    from qdrant_client import QdrantClient

    from rag.ingestion.chunker import chunk_corpus
    from rag.ingestion.loader import load_corpus
    from rag.retrieval.embedder import HashEmbedder
    from rag.retrieval.store import ChunkIndex

    repo_root = Path(__file__).resolve().parents[1]
    index = ChunkIndex(
        path=Path(":memory:"),
        collection="backend_test_procedures",
        embedder=HashEmbedder(),
        client=QdrantClient(":memory:"),
    )
    index.build(chunk_corpus(load_corpus(repo_root / "rag" / "documents", repo_root=repo_root)))
    return index


@pytest.fixture(scope="session")
def rag_settings():  # noqa: ANN201 - RagSettings, inferred
    from rag.config import RagSettings

    # `_env_file=None`: without it, whether these tests pass depends on a developer's local
    # `.env`, which is the kind of test that is green here and red in CI.
    # `RAG_MIN_RELEVANCE` is lower than production's 0.60 because the threshold is a property of
    # the *embedder*, not of the corpus: cosine similarities from the hash embedder sit in a
    # different range from bge-small's, so a number calibrated against one says nothing about the
    # other. Left at 0.60, every on-topic question here would be reported as low confidence — an
    # artefact of the fixture that would show up as a caveat on the acceptance answer and teach
    # the suite to expect hedging where there should be none. The abstention behaviour itself is
    # asserted against the real model in `rag/tests/test_retrieval_semantic.py`.
    return RagSettings(
        _env_file=None,
        RAG_EMBEDDER="hash",
        RAG_COLLECTION="backend_test_procedures",
        RAG_TOP_K=4,
        RAG_MIN_RELEVANCE=0.50,
    )


@pytest.fixture(scope="session")
def procedure_retriever(procedure_index, rag_settings):  # noqa: ANN001, ANN201
    from rag.retrieval.retriever import HybridRetriever

    return HybridRetriever(procedure_index, rag_settings)


@pytest.fixture
def backend_settings():  # noqa: ANN201 - BackendSettings, inferred
    from apps.backend.config import BackendSettings

    return BackendSettings(
        _env_file=None,
        LLM_PROVIDER="scripted",
        # Never dialled: the session factory in these tests hands back an in-memory client.
        # Present so `/health` has something to report.
        MCP_SERVER_URL="http://mcp.invalid/mcp",
        ORCHESTRATOR_MAX_STEPS=4,
        TRACE_RETENTION=10,
        LOG_LEVEL="WARNING",
    )


@pytest.fixture
def mcp_server_factory(client: TestClient):  # noqa: ANN201 - a callable, inferred
    """Builds the real MCP server against the suite's seeded, clock-frozen alarm API."""
    import httpx
    from alarm_management.config import McpSettings
    from alarm_management.context import record_upstream_call
    from alarm_management.server import build_server

    from connectors.alarm_api import AlarmApiClient

    def build(**overrides: Any):  # noqa: ANN202 - MCPServer, inferred
        fields: dict[str, Any] = {
            "ALARM_API_BASE_URL": "http://alarm-api.internal",
            "ALARM_API_TOKEN": TEST_TOKEN,
            "ALARM_API_MAX_RETRIES": 0,
            "MCP_CLIENT_ID": "alarm-copilot-tests",
        }
        mcp_settings = McpSettings(**{**fields, **overrides})
        transport = httpx.ASGITransport(app=client.app)
        return build_server(
            mcp_settings,
            client_factory=lambda s: AlarmApiClient(
                s.alarm_api_base_url,
                s.alarm_api_token,
                transport=transport,
                max_retries=s.alarm_api_max_retries,
                client_id=s.client_id,
                observer=record_upstream_call,
            ),
            # Frozen so `lookback_days=90` covers exactly the window the seeder planted.
            now=lambda: FROZEN_NOW,
        )

    return build


@pytest.fixture
def mcp_session_factory(mcp_server_factory):  # noqa: ANN001, ANN201
    """A `SessionFactory` yielding an in-memory MCP client, as the backend would open one.

    A factory rather than a session, matching production: the MCP client owns an anyio task
    group, so a session entered in one task and exited in another is refused — which is why the
    service opens one per request instead of pooling it, and why this fixture hands back the
    means to open one rather than an open one.
    """
    from mcp.client import Client

    server = mcp_server_factory()

    def factory():  # noqa: ANN202 - an async context manager, inferred
        return Client(server)

    return factory
