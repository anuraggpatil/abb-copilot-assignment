"""The copilot backend's ASGI app.

A factory, and everything replaceable is one of its arguments: `create_app(settings, provider=…,
retriever=…, session_factory=…)` is how the integration tests drive the real routes, the real
orchestrator and the real registry against a scripted LLM and an in-memory MCP session. Nothing
is patched, so there is no gap between the code that is tested and the code that is served.

The expensive resources — the LLM client and the retrieval index, which takes an exclusive file
lock — are built in the **lifespan**, not in the factory. That is what makes the module-level
`app` below harmless: importing this module constructs routes and nothing else, so a test
collection run does not open an index it will never query, and a missing token fails at startup
with a message rather than during someone else's import.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.backend.api.routes import router
from apps.backend.api.service import CopilotService
from apps.backend.config import BackendSettings, get_settings
from apps.backend.llm import build_provider
from apps.backend.llm.provider import LLMProvider
from apps.backend.tracing.trace import TraceStore
from rag.config import RagSettings
from rag.retrieval.retriever import HybridRetriever

log = logging.getLogger(__name__)

DESCRIPTION = """\
Alarm investigation copilot. Orchestrates the Alarm Management API exclusively through the
alarm-management MCP server, combined with retrieval over the plant's procedure documents, and
returns an attributed answer plus the execution trace behind it."""


def create_app(
    settings: BackendSettings | None = None,
    *,
    provider: LLMProvider | None = None,
    retriever: HybridRetriever | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    rag_settings: RagSettings | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Built here rather than at module scope so a missing token or an unbuilt index fails at
        # startup with a message, instead of at import time inside whatever imported this.
        service = CopilotService(
            settings=settings,
            provider=provider or build_provider(settings),
            retriever=retriever or HybridRetriever.open(rag_settings),
            session_factory=session_factory or _default_session_factory(settings),
            store=TraceStore(settings.trace_retention),
        )
        app.state.service = service
        try:
            yield
        finally:
            await service.aclose()

    app = FastAPI(
        title="Alarm Investigation Copilot",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


def _default_session_factory(
    settings: BackendSettings,
) -> Callable[[], AbstractAsyncContextManager[Any]]:
    """Dial the MCP server over Streamable HTTP, one session per request.

    Imported lazily: `mcp` pulls in its own HTTP stack, and a test that supplies its own session
    factory should not pay for it.
    """

    def factory() -> AbstractAsyncContextManager[Any]:
        from mcp.client import Client

        return Client(settings.mcp_server_url)

    return factory


#: For `uvicorn apps.backend.api.main:app` (`make backend`). Safe at import because the
#: resources live in the lifespan — see the module docstring.
app = create_app()
