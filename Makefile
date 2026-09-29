.DEFAULT_GOAL := help
SHELL := /bin/bash

# Everything runs through `uv run`, which resolves the project venv automatically.
UV := uv run

# Use the system certificate store. Required behind a TLS-inspecting corporate proxy, where
# uv's bundled roots reject the intercepted certificate; harmless otherwise. (Was
# UV_NATIVE_TLS, which uv 0.12 deprecates and warns about on every invocation.)
export UV_SYSTEM_CERTS := 1

# Overridable so a port already taken on the host does not require editing this file:
#   make mcp MCP_PORT=9200
# The MCP default is 9100, not the 9000 the assignment's own compose file uses: on a corporate
# macOS image 9000 is commonly held by a local proxy (verified on this machine — Microsoft
# OneDrive/Teams bind 127.0.0.1:9000), and the failure is an opaque "address already in use"
# followed by an MCP content-type error from the smoke script. Moving the default is cheaper
# than every reader hitting that once.
API_PORT ?= 8000
MCP_PORT ?= 9100
BACKEND_PORT ?= 8080

.PHONY: help install api mcp backend frontend dev ingest test test-unit test-integration \
        test-e2e test-frontend lint fmt typecheck check probe smoke mcp-smoke fetch-model docx clean

help: ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install Python deps (+ dev extras) and frontend deps
	uv sync --extra dev
	cd apps/frontend && npm install

api: ## Run the Alarm Management API simulator (API_PORT, default 8000)
	# --factory: the module exposes create_app(), not a module-level `app`. Building the app
	# generates the dataset, so a singleton would pay that cost on every import.
	$(UV) uvicorn apps.alarm_api.main:create_app --factory --reload --port $(API_PORT)

mcp: ## Run the MCP server standalone over streamable HTTP (MCP_PORT, default 9100)
	$(UV) python -m alarm_management --transport streamable-http --port $(MCP_PORT)

backend: ## Run the copilot backend (BACKEND_PORT, default 8080)
	$(UV) uvicorn apps.backend.api.main:app --reload --port $(BACKEND_PORT)

frontend: ## Run the Vite dev server (port 5173)
	cd apps/frontend && npm run dev

# The four targets above are for working on one service at a time — four windows, four logs. This
# is the one for bringing the system up: it checks the ports first and names whatever holds a busy
# one, starts all four, waits for health, tails every log, and stops the lot on Ctrl-C.
dev: ## Run all four services in one terminal (Ctrl-C stops everything)
	@API_PORT=$(API_PORT) MCP_PORT=$(MCP_PORT) BACKEND_PORT=$(BACKEND_PORT) ./scripts/dev.sh

fetch-model: ## Fetch embedding weights locally (only needed where HuggingFace is blocked)
	$(UV) python scripts/fetch_embedding_model.py

ingest: ## Build the RAG index from rag/documents into embedded Qdrant
	$(UV) python -m rag.ingestion.build_index

# Not `$(UV)`: python-docx is a documentation convenience, not a dependency of anything that
# builds, tests or runs, so it stays out of the lockfile every service resolves. Install it
# wherever your python3 is — `pip install python-docx`.
docx: ## Render docs/solution-overview.md as docs/Solution-Overview.docx
	python3 scripts/md_to_docx.py docs/solution-overview.md docs/Solution-Overview.docx

test: ## Run the full test suite with coverage
	$(UV) pytest --cov --cov-report=term-missing -m "not live"

test-unit: ## Unit tests only
	$(UV) pytest tests/unit -m "not live"

test-integration: ## Integration tests (simulator + MCP + connector)
	$(UV) pytest tests/integration -m "not live"

test-e2e: ## End-to-end acceptance scenario (deterministic, scripted LLM)
	$(UV) pytest tests/e2e -m "not live"

test-frontend: ## GUI component tests (vitest + jsdom)
	cd apps/frontend && npm test

lint: ## Lint with ruff
	$(UV) ruff check .

fmt: ## Auto-format and fix imports
	$(UV) ruff format .
	$(UV) ruff check --fix .

typecheck: ## Static type check with mypy
	# The whole tree, not a directory list: naming directories that a later phase has not
	# created yet makes the target fail for a reason unrelated to the code.
	$(UV) mypy .

check: lint typecheck test ## Lint + types + tests (what CI runs)

probe: ## Probe the Gemini API for native tool-calling support (needs GEMINI_API_KEY)
	$(UV) python scripts/probe_llm.py

mcp-smoke: ## Discover and chain the MCP tools over real HTTP (needs `make api` + `make mcp`)
	$(UV) python scripts/mcp_smoke.py

smoke: ## Run the acceptance scenario against the REAL LLM gateway
	$(UV) python scripts/live_smoke.py

clean: ## Remove generated indexes, caches and coverage output
	rm -rf .qdrant .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
