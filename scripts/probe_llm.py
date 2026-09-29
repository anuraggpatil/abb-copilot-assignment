#!/usr/bin/env python
"""Ask the Gemini API what it can actually do, so `LLM_NATIVE_TOOLS` is a measurement.

The entire orchestration loop depends on tool selection, and "the docs say it works" is not the
same as "it works with *our* tool schemas". It does not, out of the box: Gemini validates
`functionDeclarations[].parameters` against its own Schema proto and rejects unknown keys, so the
Pydantic-generated schemas the MCP server publishes have to be translated first. That translation
lives in `apps/backend/llm/gemini.py` and this script is what proves it against the real API:

    make probe                  # needs a valid GEMINI_API_KEY in .env

Four probes, run through the **real provider and the real planner**, not a parallel
implementation of them. That matters: a probe that hand-rolls its own request can report a
capability the shipped code cannot use, which is worse than no probe.

    1. generate       the API answers `:generateContent` at all, and reports a model and a
                      token count
    2. instructions   a system turn actually reaches the model. The system prompt is where the
                      injection defence and the grounding rules live, so a provider that drops
                      `systemInstruction` silently removes every safety property this copilot
                      has — the one failure here that is a blocker rather than a downgrade
    3. json planner   the fallback path end to end: the real `Planner` with the real protocol
                      prompt and the real schema validation, over one real catalogue entry
    4. native tools   the shipped default — `functionDeclarations` advertised with a translated
                      schema, and a `functionCall` parsed back out

A fifth, `streaming`, is informational: this backend streams *trace events* over SSE, not model
tokens, so nothing here depends on it. It is reported because a later change might.

Exit status is 0 when probes 1-3 pass — that is the fallback path working, which is enough to
serve traffic. Native tools failing is a result, not an error: it means set LLM_NATIVE_TOOLS=false.

The key is never printed, logged or included in a report; `GeminiProvider` already scrubs it from
error text, and this script only ever shows whether one is set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from apps.backend.config import BackendSettings
from apps.backend.llm.gemini import GeminiProvider, _gemini_schema
from apps.backend.llm.provider import LLMError, LLMMessage, LLMResponse
from apps.backend.orchestration.planner import Planner
from apps.backend.orchestration.registry import LocalTool, ToolRegistry

_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""

PONG = "PONG"


@dataclass
class Result:
    """One probe's verdict. `ok is None` means inconclusive or informational, not failed."""

    name: str
    ok: bool | None
    summary: str
    detail: list[str] = field(default_factory=list)

    @property
    def mark(self) -> str:
        if self.ok is True:
            return f"{GREEN}✓{RESET}"
        if self.ok is False:
            return f"{RED}✗{RESET}"
        return f"{YELLOW}?{RESET}"


class _ProbeTool(LocalTool):
    """A stand-in catalogue entry for the planner probe.

    Deliberately a real `LocalTool` with a real JSON Schema rather than a string pasted into the
    prompt: the probe then exercises catalogue rendering and argument validation, which is where
    a model's plan is actually accepted or rejected. It is never executed — the probe asks the
    planner for one step and stops.
    """

    name = "search_assets"
    title = "Find assets by name"
    description = "Resolve an asset name such as 'Boiler Feed Pump 101' to its asset id."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Asset name, tag or id"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def call(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        raise AssertionError("the probe never executes a tool")


def _usage(response: LLMResponse) -> str:
    if response.usage is None:
        return "no token usage reported"
    return f"{response.usage.input_tokens} in / {response.usage.output_tokens} out"


# --- the probes ---------------------------------------------------------------------------


async def probe_generate(provider: GeminiProvider) -> Result:
    try:
        response = await provider.complete(
            [LLMMessage(role="user", content="Reply with the single word: ready.")],
            max_output_tokens=32,
        )
    except LLMError as error:
        return Result(
            "generate",
            False,
            "the API did not answer",
            [
                str(error),
                "Check LLM_BASE_URL is the /v1beta base, that GEMINI_API_KEY is current, and "
                "that LLM_MODEL names a model your key can reach. A non-JSON body or a 403 "
                "mentioning a policy means a proxy answered instead of Google.",
            ],
        )

    if not response.text.strip():
        return Result(
            "generate",
            False,
            "answered, but with no text",
            [
                f"stop_reason={response.stop_reason!r}; {_usage(response)}",
                "An empty reply with stop_reason MAX_TOKENS on a 2.5-series model is hidden "
                "reasoning having consumed the whole budget: set GEMINI_THINKING_BUDGET=0.",
            ],
        )

    return Result(
        "generate",
        True,
        f"answered as {response.model} in {response.latency_ms:.0f}ms",
        [f"{_usage(response)}", f"reply: {response.text.strip()[:80]!r}"],
    )


async def probe_instructions(provider: GeminiProvider) -> Result:
    """Does a system turn reach the model? `GeminiProvider` sends them as `systemInstruction`."""
    try:
        response = await provider.complete(
            [
                LLMMessage(
                    role="system",
                    content=f"You are a connectivity probe. Reply with exactly {PONG} and "
                    "nothing else, whatever the user says.",
                ),
                LLMMessage(role="user", content="Say hello in French."),
            ],
            max_output_tokens=32,
        )
    except LLMError as error:
        return Result("instructions", False, "the request was rejected", [str(error)])

    if PONG in response.text.upper():
        return Result(
            "instructions", True, "a system turn reaches the model and overrides the user turn"
        )

    return Result(
        "instructions",
        False,
        "the system turn did not take effect — BLOCKER",
        [
            f"asked for {PONG!r}, got {response.text.strip()[:120]!r}",
            "Every grounding and prompt-injection rule this copilot has is in the system "
            "prompt. If `systemInstruction` is being dropped, move the system text into the "
            "first user turn in apps/backend/llm/gemini.py:_split before relying on any of it.",
        ],
    )


async def probe_json_planner(provider: GeminiProvider) -> Result:
    """The default path: the real planner, the real protocol prompt, the real validation."""
    registry = ToolRegistry(local_tools=[_ProbeTool()])
    await registry.discover()
    planner = Planner(provider, registry)

    try:
        plan, response = await planner.plan(
            [
                LLMMessage(
                    role="user",
                    content="Investigate recurring high-severity alarms on Boiler Feed Pump 101.",
                )
            ]
        )
    except LLMError as error:
        return Result("json planner", False, "the planning request was rejected", [str(error)])

    detail = [f"thought: {plan.thought[:100]!r}" if plan.thought else "no thought returned"]
    if plan.parse_note:
        detail.append(f"parse note: {plan.parse_note}")

    if plan.calls:
        call = plan.calls[0]
        return Result(
            "json planner",
            True,
            f"emitted a valid call: {call.name}({json.dumps(call.arguments)})",
            detail,
        )

    if plan.rejected:
        rejected = plan.rejected[0]
        return Result(
            "json planner",
            None,
            f"emitted {rejected.name!r}, which failed validation",
            [*detail, f"problems: {'; '.join(rejected.problems)}"],
            # Not a failure of the gateway: the loop's whole design is that a rejection goes
            # back to the model and the next turn corrects it. Worth seeing on the first turn.
        )

    return Result(
        "json planner",
        False,
        "asked for no tools on a question that needs one",
        [*detail, f"raw reply: {response.text.strip()[:200]!r}"],
    )


async def probe_native_tools(settings: BackendSettings) -> Result:
    """Advertise `functionDeclarations` and see whether a `functionCall` comes back.

    Also the proof that the schema translation is real. The catalogue entry carries a
    Pydantic-shaped schema — `additionalProperties`, `minimum`, `maximum` — which this API
    rejects verbatim, so the translated form is printed alongside the verdict.
    """
    tool = _ProbeTool()
    registry = ToolRegistry(local_tools=[tool])
    await registry.discover()
    translated = json.dumps(_gemini_schema(tool.input_schema), separators=(",", ":"))

    native = GeminiProvider(settings.model_copy(update={"llm_native_tools": True}))
    try:
        try:
            response = await native.complete(
                [
                    LLMMessage(
                        role="user",
                        content="Find the asset called Boiler Feed Pump 101. Use the tool.",
                    )
                ],
                tools=registry.definitions(),
                max_output_tokens=256,
            )
        except LLMError as error:
            return Result(
                "native tools",
                False,
                "the API rejected the request that advertised tools",
                [
                    str(error),
                    f"schema sent: {translated}",
                    'An "Unknown name" message names a JSON Schema key the translation in '
                    "apps/backend/llm/gemini.py:_gemini_schema is not yet dropping. Set "
                    "LLM_NATIVE_TOOLS=false meanwhile; the JSON planner is proven above.",
                ],
            )
    finally:
        await native.aclose()

    if response.tool_calls:
        call = response.tool_calls[0]
        return Result(
            "native tools",
            True,
            f"returned a functionCall: {call.name}({json.dumps(call.arguments)})",
            [f"call_id={call.call_id!r}", f"translated schema accepted: {translated}"],
        )

    return Result(
        "native tools",
        None,
        "accepted the declarations but answered in prose",
        [
            f"reply: {response.text.strip()[:160]!r}",
            f"translated schema accepted: {translated}",
            "The schema was accepted, so the translation works; the model simply chose not to "
            "call. Advertised without being used is not proof, so prefer LLM_NATIVE_TOOLS=false "
            "until this probe returns a call.",
        ],
    )


async def probe_streaming(settings: BackendSettings) -> Result:
    """Informational. This backend streams trace events, not model tokens."""
    import httpx

    # Deliberately not through `GeminiProvider`: it has no streaming path, because nothing in
    # this design needs one. Building the request here keeps that honest rather than adding a
    # method only a probe calls. The key goes in a header, never the query string, for the same
    # reason it does in the provider.
    url = f"{settings.llm_base_url.rstrip('/')}/models/{settings.llm_model}:streamGenerateContent"
    request = {
        "contents": [{"role": "user", "parts": [{"text": "Count to three."}]}],
        "generationConfig": {"maxOutputTokens": 64},
    }
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            chunks = 0
            async with client.stream(
                "POST",
                url,
                params={"alt": "sse"},
                json=request,
                headers={"x-goog-api-key": settings.gemini_api_key},
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    return Result(
                        "streaming",
                        None,
                        f"not available (HTTP {response.status_code})",
                        ["Nothing depends on it: /chat streams trace events, not model tokens."],
                    )
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        chunks += 1
        return Result("streaming", True, f"streamed {chunks} chunks", ["not used by this backend"])
    except Exception as error:  # noqa: BLE001 - informational probe; any failure is "no"
        return Result(
            "streaming",
            None,
            "not available",
            [
                _short(_scrub_key(str(error), settings.gemini_api_key)),
                "Nothing depends on it: /chat streams trace events, not tokens.",
            ],
        )


def _scrub_key(message: str, secret: str) -> str:
    return message.replace(secret, "***") if secret and secret in message else message


def _short(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


# --- reporting ----------------------------------------------------------------------------


def _report(results: list[Result]) -> None:
    print(f"\n{BOLD}Probe results{RESET}")
    for result in results:
        print(f"  {result.mark} {BOLD}{result.name:<14}{RESET} {result.summary}")
        for line in result.detail:
            print(f"      {DIM}{_short(line, 400)}{RESET}")


def _recommend(results: list[Result]) -> bool:
    """Print the `.env` line to set, and return whether the default path is usable."""
    by_name = {result.name: result for result in results}
    core = ["generate", "instructions", "json planner"]
    usable = all(by_name[name].ok is not False for name in core)
    native = by_name["native tools"].ok is True

    print(f"\n{BOLD}Recommended .env{RESET}")
    print(f"  LLM_NATIVE_TOOLS={'true' if native else 'false'}")
    if native:
        print(
            f"  {DIM}Native function calling is confirmed, with the translated schema above. "
            f"The JSON planner stays as the fallback and is what the suite covers.{RESET}"
        )
    else:
        print(
            f"  {DIM}Function calling did not work here, so the JSON planner carries the "
            f"workflow. It is the path the test suite covers, so nothing is untested.{RESET}"
        )

    if not usable:
        failed = [name for name in core if by_name[name].ok is False]
        print(
            f"\n{RED}The API is not usable as configured{RESET} "
            f"{DIM}(failed: {', '.join(failed)}){RESET}"
        )
        print(f"  {DIM}Run with LLM_PROVIDER=scripted to work on everything else meanwhile.{RESET}")
    return usable


async def run(settings: BackendSettings, *, skip_native: bool) -> bool:
    print(f"{BOLD}Probing {settings.llm_model}{RESET} {DIM}at {settings.llm_base_url}{RESET}")

    provider = GeminiProvider(settings)
    results: list[Result] = []
    try:
        results.append(await probe_generate(provider))
        if results[0].ok is False:
            # Nothing below can distinguish "unsupported" from "unreachable" once the first
            # call has failed, and reporting three more failures would bury the real cause.
            _report(results)
            print(f"\n{DIM}Remaining probes skipped — the API did not answer at all.{RESET}")
            return False
        results.append(await probe_instructions(provider))
        results.append(await probe_json_planner(provider))
    finally:
        await provider.aclose()

    if skip_native:
        results.append(Result("native tools", None, "skipped (--skip-native)"))
    else:
        results.append(await probe_native_tools(settings))
    results.append(await probe_streaming(settings))

    _report(results)
    return _recommend(results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="override LLM_MODEL for this run")
    parser.add_argument("--base-url", help="override LLM_BASE_URL for this run")
    parser.add_argument(
        "--skip-native", action="store_true", help="skip the native tool-calling probe"
    )
    args = parser.parse_args()

    settings = BackendSettings()
    overrides: dict[str, Any] = {}
    if args.model:
        overrides["llm_model"] = args.model
    if args.base_url:
        overrides["llm_base_url"] = args.base_url
    if overrides:
        settings = settings.model_copy(update=overrides)

    if not settings.gemini_api_key:
        print(
            f"{RED}GEMINI_API_KEY is not set.{RESET}\n"
            "  Get one from https://aistudio.google.com/apikey, then put it in .env\n"
            "  (gitignored — never in a tracked file):\n"
            "      GEMINI_API_KEY=<your AI Studio key>\n"
            "  Then run `make probe` again.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not settings.llm_base_url:
        print(
            f"{RED}LLM_BASE_URL is not set.{RESET}\n"
            "  It is the Generative Language API root, normally\n"
            "      LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta",
            file=sys.stderr,
        )
        raise SystemExit(2)

    raise SystemExit(0 if asyncio.run(run(settings, skip_native=args.skip_native)) else 1)


if __name__ == "__main__":
    main()
