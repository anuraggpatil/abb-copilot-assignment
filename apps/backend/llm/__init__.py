"""LLM providers behind one interface. `build_provider` is the only place the choice is made."""

from __future__ import annotations

from apps.backend.config import BackendSettings
from apps.backend.llm.provider import (
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ToolCall,
    ToolDefinition,
)

__all__ = [
    "LLMError",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMUsage",
    "ToolCall",
    "ToolDefinition",
    "build_provider",
]


def build_provider(settings: BackendSettings) -> LLMProvider:
    """Construct the configured provider.

    `scripted` with no script is a deliberate error rather than a silent no-op: a backend
    started with `LLM_PROVIDER=scripted` would otherwise come up healthy and then fail on the
    first question, which is the least useful moment to find out. Tests construct
    `ScriptedProvider` directly with the script they mean.
    """
    if settings.llm_provider == "scripted":
        raise ValueError(
            "LLM_PROVIDER=scripted has no script to run. It exists for tests, which build "
            "ScriptedProvider directly; set LLM_PROVIDER=gemini to serve requests."
        )

    from apps.backend.llm.gemini import GeminiProvider

    return GeminiProvider(settings)
