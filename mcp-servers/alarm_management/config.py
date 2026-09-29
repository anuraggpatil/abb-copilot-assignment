"""Settings for the alarm-management MCP server.

Its own class, reading the same `.env` as everything else. The MCP server is a boundary
process: it needs the alarm API's address and token and nothing else, and giving it a
settings object that cannot express LLM credentials or RAG paths makes that boundary
mechanical rather than a matter of discipline — the same reasoning as
`apps/alarm_api/config.py`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Transport = Literal["stdio", "streamable-http"]


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- the upstream it fronts ---
    alarm_api_base_url: str = Field(default="http://localhost:8000", alias="ALARM_API_BASE_URL")
    alarm_api_token: str = Field(default="demo-token", alias="ALARM_API_TOKEN")
    alarm_api_timeout_seconds: float = Field(default=10.0, alias="ALARM_API_TIMEOUT_SECONDS")
    alarm_api_max_retries: int = Field(default=3, alias="ALARM_API_MAX_RETRIES")

    # --- how it is served ---
    transport: Transport = Field(default="streamable-http", alias="MCP_TRANSPORT")
    host: str = Field(default="0.0.0.0", alias="MCP_SERVER_HOST")
    # 9100 rather than the 9000 the assignment's compose file names: 9000 is commonly already
    # held on a corporate macOS image (see the Makefile), and a default that fails to bind on
    # a reviewer's machine is worse than one that differs from a sample file by 100.
    port: int = Field(default=9100, alias="MCP_SERVER_PORT")
    # `MCP_SERVER_URL` in `.env` is `http://localhost:9100/mcp`; the path is kept separate
    # here because the server binds a path while the client dials a full URL.
    #
    # The alias is not decoration. Without it pydantic-settings resolves this field from the
    # environment variable `PATH` — which always exists — and the server silently binds the
    # shell's executable search path as its endpoint. Every field below is aliased for the
    # same reason: an un-aliased name is one collision away from a bug no test will see.
    path: str = Field(default="/mcp", alias="MCP_SERVER_PATH")

    # Sent as `x-client-id` on every upstream call, so a request in the API's log can be
    # attributed to the MCP server rather than to an unidentified HTTP client.
    client_id: str = Field(default="alarm-copilot", alias="MCP_CLIENT_ID")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    #: Ceiling on rows any tool will return in one response. Lower than the API's own cap of
    #: 500 on purpose: these payloads are serialised into a model's context window, and an
    #: unbounded page is how a tool result crowds out the question it was meant to answer.
    max_rows: int = Field(default=100, alias="MCP_MAX_ROWS")


@lru_cache(maxsize=1)
def get_settings() -> McpSettings:
    return McpSettings()
