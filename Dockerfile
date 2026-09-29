# One Python image, three services.
#
# The simulator, the MCP server and the copilot backend share a dependency set and a source
# tree; what differs is the command. Three Dockerfiles would mean three near-identical
# dependency layers to keep in step, so compose overrides `command` instead.
#
# UNVERIFIED: `docker` is not installed on the machine this was built on, so this file has
# never been built. It is written against the documented behaviour of the base image and uv.
# See docs/known-limitations.md.

FROM python:3.13-slim AS base

# uv resolves and installs an order of magnitude faster than pip here, and the lockfile is
# already the source of truth for the local workflow — using pip would mean maintaining a
# second dependency path that could drift.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Install into the image's own interpreter rather than a venv: a container has no other
    # Python to protect, and a venv only adds a path to remember in every command.
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# --- dependency layer ---------------------------------------------------------------------
# Manifests only, so a source edit does not invalidate the dependency install. `--no-install-
# project` skips building this project itself, which needs the source that has not been copied
# yet.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

# --- source layer -------------------------------------------------------------------------
COPY apps ./apps
COPY connectors ./connectors
COPY mcp-servers ./mcp-servers
COPY rag ./rag
COPY scripts ./scripts
COPY README.md LICENSE ./

RUN uv sync --locked --no-dev

# `mcp-servers` is hyphenated per the assignment's mandated layout, so it is not importable;
# the package inside it is. Same reason pyproject sets pytest's pythonpath.
ENV PYTHONPATH=/app:/app/mcp-servers

# Non-root, and it must own /app/.qdrant: the index is written at runtime by the ingest step
# and read by the backend, both as this user.
RUN useradd --create-home --uid 10001 copilot \
    && mkdir -p /app/.qdrant /app/.models \
    && chown -R copilot:copilot /app
USER copilot

# Overridden per service in docker-compose.yml. The simulator is the default because it is the
# one service with no dependencies of its own.
EXPOSE 8000 8080 9100
CMD ["uvicorn", "apps.alarm_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
