"""Settings for the copilot backend.

Its own class reading the same `.env`, for the same reason as the other three: this process
is the only one that holds an LLM credential, and a settings object that cannot express one
is what keeps the simulator and the MCP server unable to leak it even by mistake.

Every field carries an explicit alias. That is not style — an un-aliased field named `path`
or `host` resolves from an environment variable that already exists on any shell, and the
bug it produces is invisible to an in-process test. It cost real time on the MCP server.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: `gemini` calls Google's Generative Language API. `scripted` returns canned responses and is
#: what the e2e test and CI use — see `apps/backend/llm/scripted.py`.
ProviderKind = Literal["gemini", "scripted"]


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- the LLM, behind the provider abstraction ---
    llm_provider: ProviderKind = Field(default="gemini", alias="LLM_PROVIDER")
    llm_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta", alias="LLM_BASE_URL"
    )
    llm_model: str = Field(default="gemini-flash-latest", alias="LLM_MODEL")
    llm_max_output_tokens: int = Field(default=2048, alias="LLM_MAX_OUTPUT_TOKENS")
    llm_timeout_seconds: float = Field(default=90.0, alias="LLM_TIMEOUT_SECONDS")
    #: Transient-failure retries per call. The free tier answers `503 high demand` under load
    #: often enough that one refusal must not fail an operator's question.
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")
    #: Whether to advertise tools natively rather than through the planner's JSON protocol.
    #: Gemini documents function calling, so this is on by default — unlike the gateway this
    #: replaced, whose support was never verified. `scripts/probe_llm.py` checks it for real.
    llm_native_tools: bool = Field(default=True, alias="LLM_NATIVE_TOOLS")
    #: The only secret this process holds. Never logged, never traced, never returned.
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    #: Output tokens the 2.5-series models may spend on hidden reasoning before writing. Unset
    #: leaves Gemini's default; `0` disables thinking, which is the fix when a reply comes back
    #: empty with `finishReason: MAX_TOKENS`.
    gemini_thinking_budget: int | None = Field(default=None, alias="GEMINI_THINKING_BUDGET")

    @field_validator("gemini_thinking_budget", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """An empty `GEMINI_THINKING_BUDGET=` in `.env` is "leave it to Gemini", not an error.

        A dotenv file has no way to spell "absent" other than an empty value, and `.env.example`
        and `docker-compose.yml` both ship this key blank because the sensible default is
        Gemini's own. Without this, copying the example verbatim fails every process at startup
        with an int-parsing error — which is the worst possible first experience of the repo.
        Only this field needs it: it is the one optional integer.
        """
        return None if isinstance(value, str) and not value.strip() else value

    # --- the MCP server it orchestrates ---
    #: The alarm API is reached *only* through this. The backend has no `ALARM_API_*` setting
    #: at all, which makes that rule structural rather than a matter of remembering it.
    mcp_server_url: str = Field(default="http://localhost:9100/mcp", alias="MCP_SERVER_URL")
    mcp_tool_timeout_seconds: float = Field(default=30.0, alias="MCP_TOOL_TIMEOUT_SECONDS")

    # --- orchestration ---
    #: Hard ceiling on planner iterations. A model that keeps calling tools without concluding
    #: is the failure mode that turns one question into an unbounded bill, so the loop is
    #: bounded by construction and the answer says when the ceiling was hit.
    max_steps: int = Field(default=8, alias="ORCHESTRATOR_MAX_STEPS")
    #: Conversations kept in the in-memory trace store. The GUI reads a trace right after the
    #: answer, so this only needs to cover a demo session; it is bounded because an
    #: ever-growing dict in a long-lived process is a leak, not a cache.
    trace_retention: int = Field(default=50, alias="TRACE_RETENTION")
    #: Earlier turns of a conversation put in front of the model on each new question. Every turn
    #: pays for this window in input tokens, and the referring expressions in this domain — "that
    #: procedure", "the other pump" — reach back one or two turns, not ten. Set to 1 to make each
    #: question effectively independent again.
    conversation_turns: int = Field(default=4, alias="CONVERSATION_TURNS")

    # --- serving ---
    host: str = Field(default="0.0.0.0", alias="BACKEND_HOST")
    port: int = Field(default=8080, alias="BACKEND_PORT")
    #: Comma-separated, and kept as a `str` on purpose: pydantic-settings parses a `list[str]`
    #: field as JSON, so `http://a,http://b` raises a validation error rather than splitting.
    #: See `cors_origins`.
    cors_origins_raw: str = Field(default="http://localhost:5173", alias="BACKEND_CORS_ORIGINS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins_raw.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> BackendSettings:
    return BackendSettings()
