"""Gemini via Google's Generative Language API, over `httpx` and `:generateContent`.

Not the OpenAI SDK. Gemini's own REST surface is the one with documented function calling, so
this provider speaks it directly: `POST {base}/models/{model}:generateContent` with the key in
an `x-goog-api-key` **header**. Deliberately not the `?key=` query parameter Google's own
examples use — a URL travels into proxy logs, exception messages and `httpx` request reprs,
and this project asserts that the credential appears in none of them.

**Four accommodations the API forces, each verified against the live endpoint.**

1. *Tool schemas must be translated, not forwarded.* Gemini validates `parameters` against its
   own Schema proto and **rejects unknown keys outright** rather than ignoring them. The MCP
   server's schemas come from Pydantic, so they arrive carrying `title`, `default`, `minimum`,
   `additionalProperties` and `exclusiveMinimum`; a raw forward returns
   `400 Unknown name "additionalProperties" … Cannot find field`. `_gemini_schema` is that
   translation and it is the reason this provider has a non-trivial test file.

2. *`anyOf: [{…}, {"type": "null"}]` has to collapse.* Pydantic spells every optional argument
   that way and Gemini's type enum has no `NULL` member. The null branch becomes
   `nullable: true` on the surviving branch, which is the same statement in Gemini's vocabulary.

3. *Roles are `user` and `model`.* There is no `assistant`, and system turns go in a separate
   top-level `systemInstruction` rather than in `contents`.

4. *Consecutive same-role turns are merged.* Two tool results in a row would otherwise send two
   adjacent `user` turns, which this API is documented to expect to alternate. Merging into one
   turn with several parts keeps that true without the orchestrator having to know.

Tool results come back as labelled text rather than as `functionResponse` parts, for the same
reason the trust boundary exists at all: everything inside `[tool result]` is data, and the
label is what stops a retrieved document from being read as an instruction. Pairing by
`functionResponse.name` would also silently mis-pair when one step calls the same tool twice.

`429` and `503` are retried — the free tier returns `503 This model is currently experiencing
high demand` under load, and a single transient refusal should not fail an operator's question.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

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

log = logging.getLogger(__name__)

#: Keys Gemini's Schema proto understands. Everything else is dropped rather than passed
#: through, because this API rejects an unknown key instead of ignoring it.
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "items",
        "properties",
        "required",
        "anyOf",
        "minItems",
        "maxItems",
    }
)

#: `format` is only legal for a handful of values. Pydantic emits `uri`, `email` and friends,
#: and an unrecognised one is a 400 rather than a warning.
_SCHEMA_FORMATS = frozenset({"date-time", "enum", "float", "double", "int32", "int64"})

#: Retried once each with a short backoff. 429 is the free tier's rate limit; 503 is its
#: capacity refusal, which is common enough to have been hit while writing this module.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

#: How deep schema translation will walk. A self-referential Pydantic model (`Node` with a
#: `child: Node`) is a legitimate schema and an infinite tree once `$ref`s are inlined, so the
#: recursion needs a floor. Deeper than any tool in this catalogue.
_MAX_SCHEMA_DEPTH = 8


class GeminiProvider(LLMProvider):
    """Calls the Generative Language API. Holds the only credential in this process."""

    name = "gemini"

    def __init__(self, settings: BackendSettings, *, client: httpx.AsyncClient | None = None):
        if not settings.gemini_api_key and client is None:
            raise LLMError(
                "GEMINI_API_KEY is not set. Put it in .env (never in a tracked file), or "
                "run with LLM_PROVIDER=scripted to use the deterministic provider."
            )
        if not settings.llm_base_url:
            raise LLMError(
                "LLM_BASE_URL is not set; it must be the Generative Language API base, "
                "e.g. https://generativelanguage.googleapis.com/v1beta"
            )

        self.settings = settings
        self.supports_native_tools = settings.llm_native_tools
        self._secret = settings.gemini_api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            # The key rides in a header on every request, never in the URL or the query string.
            headers={"x-goog-api-key": settings.gemini_api_key},
        )

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        instruction, contents = _split(messages)
        generation: dict[str, Any] = {
            "maxOutputTokens": max_output_tokens or self.settings.llm_max_output_tokens
        }
        # 2.5-series Flash spends output tokens on hidden reasoning before it writes anything,
        # so a budget that looks generous can return `MAX_TOKENS` with an empty answer. Setting
        # it to 0 is how that is turned off; left unset by default because it is one more field
        # for a proxy to reject, and `_text_of` reports the empty-answer case explicitly.
        if self.settings.gemini_thinking_budget is not None:
            generation["thinkingConfig"] = {"thinkingBudget": self.settings.gemini_thinking_budget}

        request: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if instruction:
            request["systemInstruction"] = {"parts": [{"text": instruction}]}
        if tools and self.supports_native_tools:
            request["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": _gemini_schema(tool.input_schema),
                        }
                        for tool in tools
                    ]
                }
            ]

        model = self.settings.llm_model
        started = time.perf_counter()
        payload = await self._post(f"/models/{model}:generateContent", request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        candidate = _candidate_of(payload, model)
        parts = candidate.get("content", {}).get("parts") or []
        return LLMResponse(
            text=_text_of(parts),
            tool_calls=_tool_calls_of(parts),
            model=str(payload.get("modelVersion") or model),
            latency_ms=elapsed_ms,
            usage=_usage_of(payload),
            stop_reason=candidate.get("finishReason"),
        )

    async def _post(self, path: str, request: dict[str, Any]) -> dict[str, Any]:
        """One call, with a bounded retry on the statuses that mean "try again"."""
        attempts = 1 + max(0, self.settings.llm_max_retries)
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post(path, json=request)
            except httpx.HTTPError as error:
                # Includes the corporate-proxy case: a TLS interception or a policy block
                # arrives here, and its message must not be allowed to quote the key.
                raise LLMError(
                    f"{self.settings.llm_model} was unreachable: "
                    f"{_scrub(f'{type(error).__name__}: {error}', self._secret)}"
                ) from None

            if response.status_code in _RETRY_STATUS and attempt < attempts:
                delay = _retry_after(response) or 2.0 * attempt
                log.warning(
                    "gemini returned %s; retrying in %.1fs (attempt %d of %d)",
                    response.status_code,
                    delay,
                    attempt,
                    attempts,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                raise LLMError(
                    f"{self.settings.llm_model} did not respond: {response.status_code} "
                    f"{_scrub(_error_of(response), self._secret)}"
                )

            try:
                body = response.json()
            except ValueError:
                # A proxy that answers 200 with an HTML block page lands here. Saying so is
                # more useful than a KeyError three lines later.
                raise LLMError(
                    f"{self.settings.llm_model} returned a non-JSON body "
                    f"({response.headers.get('content-type', 'unknown content-type')}); "
                    "something between this process and Google answered instead of the API."
                ) from None

            return body if isinstance(body, dict) else {}

        raise LLMError(f"{self.settings.llm_model} did not respond after {attempts} attempts")

    async def aclose(self) -> None:
        # Only if this provider opened it. A client passed in by a test is the test's to close.
        if self._owns_client:
            await self._client.aclose()


def _split(messages: list[LLMMessage]) -> tuple[str, list[dict[str, Any]]]:
    """Separate system turns from the conversation, and merge adjacent same-role turns."""
    instructions: list[str] = []
    contents: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            instructions.append(message.content)
            continue

        if message.role == "assistant":
            role, text = "model", message.content
        elif message.role == "tool":
            # Labelled so the model can tell a tool result from something a human typed. The
            # label is the trust boundary: everything inside it is data, never instruction.
            role, text = "user", f"[tool result]\n{message.content}"
        else:
            role, text = "user", message.content

        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": role, "parts": [{"text": text}]})

    return "\n\n".join(instructions), contents


def _gemini_schema(
    schema: Any, defs: dict[str, Any] | None = None, depth: int = 0
) -> dict[str, Any]:
    """Translate a JSON Schema into the subset Gemini's Schema proto accepts.

    Recursive, and lossy on purpose: a constraint Gemini cannot express (`minimum`, say) is
    dropped here rather than sent, because the alternative is a 400 that fails the whole
    question. Nothing is lost in practice — every argument is validated against the *original*
    schema in the planner before the tool is called, so the model being told about a bound is a
    hint, and the enforcement is somewhere this cannot weaken.

    `defs` is threaded down the recursion rather than read from each node: Pydantic puts `$defs`
    at the top level of the schema and the `$ref`s that use it inside `properties`, so a nested
    reference has no way to find its own definition otherwise. `depth` is counted here rather
    than in `_resolve` for the same reason — inlining is what makes the tree unbounded, and the
    cycle it creates runs through this function, not through a chain of refs.
    """
    if not isinstance(schema, dict) or depth > _MAX_SCHEMA_DEPTH:
        # An untyped schema, which Gemini reads as "any value". The argument is still validated
        # against the original schema in the planner, so nothing is weakened by stopping here.
        return {}

    defs = {**(defs or {}), **(schema.get("$defs") or {})}
    schema = _resolve(schema, defs)
    out: dict[str, Any] = {}

    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "format":
            if isinstance(value, str) and value in _SCHEMA_FORMATS:
                out[key] = value
        elif key == "properties" and isinstance(value, dict):
            out[key] = {
                name: _gemini_schema(child, defs, depth + 1) for name, child in value.items()
            }
        elif key == "items":
            out[key] = _gemini_schema(value, defs, depth + 1)
        elif key == "anyOf" and isinstance(value, list):
            out.update(_collapse_any_of(value, defs, depth))
        else:
            out[key] = value

    # An object with no properties is rejected as an incomplete Schema, and a no-argument tool
    # is a real case (`list_procedures`). `{"type": "object"}` alone is accepted.
    if out.get("type") == "object" and not out.get("properties"):
        out.pop("properties", None)
        out.pop("required", None)

    return out


def _collapse_any_of(branches: list[Any], defs: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """`[X, {"type": "null"}]` → `X` plus `nullable: true`. Gemini has no NULL type.

    This is how Pydantic spells every optional argument, so without it nearly every tool in the
    catalogue is unusable. A genuine multi-branch union (two non-null types) is kept as `anyOf`,
    which Gemini does support.
    """
    concrete = [
        branch for branch in branches if isinstance(branch, dict) and branch.get("type") != "null"
    ]
    nullable = len(concrete) < len(branches)

    if not concrete:
        # `anyOf: [{"type": "null"}]` — nothing to say. An untyped schema is accepted as "any".
        return {"nullable": True} if nullable else {}
    if len(concrete) == 1:
        collapsed = _gemini_schema(concrete[0], defs, depth + 1)
        if nullable:
            collapsed["nullable"] = True
        return collapsed

    out: dict[str, Any] = {
        "anyOf": [_gemini_schema(branch, defs, depth + 1) for branch in concrete]
    }
    if nullable:
        out["nullable"] = True
    return out


def _resolve(schema: dict[str, Any], defs: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Inline a local `$ref`. Gemini has no `$ref`, and Pydantic emits them for nested models.

    Depth-limited for a `$ref` that points at another `$ref`; the limit on the tree as a whole
    is `_gemini_schema`'s, which is where a cycle actually closes.
    """
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return schema
    if depth >= _MAX_SCHEMA_DEPTH:
        return {}

    target = defs.get(ref.removeprefix("#/$defs/"))
    if not isinstance(target, dict):
        return {}
    # The sibling keys of a `$ref` (a `description`, usually) win over the target's.
    siblings = {key: value for key, value in schema.items() if key != "$ref"}
    return {**_resolve(target, defs, depth + 1), **siblings}


def _candidate_of(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """The first candidate, or a stated reason there is none."""
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]

    # No candidate at all means the prompt itself was refused. An empty answer would be
    # indistinguishable from a model that had nothing to say, and an operator needs to know
    # the difference — one is retryable by rewording, the other is not.
    feedback = payload.get("promptFeedback") or {}
    reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
    if reason:
        raise LLMError(f"{model} returned no answer: the prompt was blocked ({reason})")
    raise LLMError(f"{model} returned no answer and gave no reason")


def _text_of(parts: list[Any]) -> str:
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]
    ]
    return "\n".join(texts)


def _tool_calls_of(parts: list[Any]) -> list[ToolCall]:
    """Parse `functionCall` parts. Absent in JSON-planner mode, which is fine."""
    calls: list[ToolCall] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        call = part.get("functionCall")
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = call.get("args")
        # Gemini issues no call id, so one is synthesised. Results are paired back to requests
        # by it, and an empty one collides the moment a step asks for two tools.
        calls.append(
            ToolCall(
                call_id=f"call_{index}",
                name=name,
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls


def _usage_of(payload: dict[str, Any]) -> LLMUsage | None:
    usage = payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("promptTokenCount")
    output = usage.get("candidatesTokenCount")
    return LLMUsage(
        input_tokens=prompt if isinstance(prompt, int) else None,
        output_tokens=output if isinstance(output, int) else None,
    )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        return min(float(raw), 30.0) if raw else None
    except (TypeError, ValueError):
        return None


def _error_of(response: httpx.Response) -> str:
    """The API's own message when it sent one, the status text otherwise."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or response.reason_phrase
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return response.reason_phrase


def _scrub(message: str, secret: str) -> str:
    """Remove the key from anything that might be shown or logged.

    `httpx` does not normally echo headers in an exception, but "does not normally" is not a
    property to build a security guarantee on, and this text reaches an operator's screen.
    """
    if secret and secret in message:
        return message.replace(secret, "***")
    return message
