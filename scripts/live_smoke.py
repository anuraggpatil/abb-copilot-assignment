#!/usr/bin/env python
"""Run the acceptance scenario against the real model, the real MCP server and the real index.

`tests/e2e` runs the same scenario with a scripted provider, and that is the right thing to run
in CI: it is deterministic and needs no credential, so it asserts what *our* orchestration did.
What it cannot tell you is whether a language model, given this catalogue and these
instructions, actually resolves the asset before asking for its alarms, passes the procedure
references it was handed into retrieval, and cites what it was shown. That is what this script
is for, and it is the reason the two exist separately rather than one being a weaker version of
the other.

    make api        # terminal 1 — the alarm API
    make mcp        # terminal 2 — the MCP server
    make ingest     # once, to build the index
    make smoke      # needs a valid GEMINI_API_KEY in .env

It drives `CopilotService` — the same object the HTTP layer holds — rather than `POST /chat`,
for two reasons: the backend does not have to be running (one less process to explain in a demo)
and the trace sink is available directly, so what prints here is exactly what the SSE stream
would carry, event for event.

Exit status is 0 only when the scenario met its requirements: the alarm data came through MCP,
procedure text came through retrieval, both in one conversation, and the answer carried a
citation. Anything else a real model does — hedging, a fabricated section number, hitting the
step ceiling — is reported as an observation rather than silently passed, because those are the
things worth seeing before recording a demo.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from apps.backend.api.main import _default_session_factory
from apps.backend.api.service import CopilotService
from apps.backend.config import BackendSettings
from apps.backend.llm import build_provider
from apps.backend.orchestration.orchestrator import CopilotAnswer, CopilotError
from apps.backend.tracing.trace import TraceEvent
from rag.config import RagSettings
from rag.retrieval.retriever import HybridRetriever

QUESTION = (
    "Investigate recurring high-severity alarms for Boiler Feed Pump 101 over the last 90 days. "
    "Identify likely contributing factors, retrieve the relevant operating procedure, and give "
    "recommended actions with source evidence."
)

_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""

KIND_LABEL = {
    "tool_discovery": "discover",
    "plan": "plan",
    "mcp_tool_call": "MCP",
    "rag_retrieval": "RAG",
    "llm_call": "llm",
    "synthesis": "synthesis",
    "error": "error",
}


def _log(message: str = "") -> None:
    print(message, file=sys.stderr)


class SmokeFailure(Exception):
    """A requirement was not met. An `Exception`, not `SystemExit`, so that an
    `ExceptionGroup` raised out of the MCP client's task group still catches it —
    see the same note in `scripts/mcp_smoke.py`."""


# --- live trace ---------------------------------------------------------------------------


async def _print_event(event: TraceEvent) -> None:
    """The sink. Prints each step as it happens, which is what `POST /chat` streams."""
    colour = {"ok": GREEN, "partial": YELLOW, "error": RED}.get(event.status, "")
    label = KIND_LABEL.get(event.kind, event.kind)
    _log(
        f"  {colour}{event.seq:>2}{RESET} {DIM}{label:<9}{RESET} {BOLD}{event.name}{RESET} "
        f"{DIM}{event.duration_ms:.0f}ms{RESET}"
    )
    if event.summary:
        _log(f"       {DIM}{event.summary}{RESET}")


def _extra(event: TraceEvent) -> str:
    """The per-kind columns the GUI's collapsed row shows. Keys come from the recorders:
    `upstream`/`retry_count` from `registry._trace`, `chunks`/`top_score` from
    `procedures._trace_payload`. Read defensively so a new kind prints blank, not raises."""
    detail = event.detail
    parts: list[str] = []
    upstream = detail.get("upstream")
    if isinstance(upstream, list) and upstream:
        codes = ",".join(str(call.get("status_code")) for call in upstream)
        parts.append(f"api={codes}")
    if detail.get("retry_count"):
        parts.append(f"retries={detail['retry_count']}")
    chunks = detail.get("chunks")
    if isinstance(chunks, list):
        parts.append(f"chunks={len(chunks)}")
    if isinstance(detail.get("top_score"), int | float):
        parts.append(f"top={detail['top_score']:.3f}")
    if detail.get("error_kind"):
        parts.append(f"{detail['error_kind']}")
    return " ".join(parts)


def _trace_table(events: list[TraceEvent]) -> None:
    _log(f"\n{BOLD}Trace{RESET} {DIM}(what the GUI panel shows){RESET}")
    for event in events:
        _log(
            f"  {event.seq:>2}  {KIND_LABEL.get(event.kind, event.kind):<9} "
            f"{event.name:<30} {event.status:<7} {event.duration_ms:>7.0f}ms  "
            f"{DIM}{_extra(event)}{RESET}"
        )


# --- the report --------------------------------------------------------------------------


def _print_answer(answer: CopilotAnswer) -> None:
    _log(f"\n{BOLD}Answer{RESET} {DIM}({answer.model}){RESET}")
    for line in answer.answer.splitlines():
        _log(f"  {line}")

    _log(f"\n{BOLD}Citations{RESET}")
    if not answer.citations:
        _log(f"  {RED}none{RESET}")
    for citation in answer.citations:
        mark = f"{GREEN}cited{RESET}" if citation.cited_in_answer else f"{DIM}offered{RESET}"
        # `None` means the section was fetched because a recommendation named it, not because it
        # ranked — printing 0.000 there would read as "irrelevant", which is the opposite.
        score = (
            "cited upstream"
            if citation.relevance is None
            else f"relevance {citation.relevance:.3f}"
        )
        _log(f"  [{mark}] {citation.reference} {DIM}— {citation.document} rev {citation.revision},")
        _log(f"           {score}, selected by {citation.selected_by}{RESET}")

    _log(f"\n{BOLD}Tool calls{RESET}")
    for call in answer.tool_calls:
        mark = f"{GREEN}ok{RESET}" if call.ok else f"{RED}{call.error_kind}{RESET}"
        _log(f"  [{mark}] {call.name} {DIM}via {call.backend}, {call.duration_ms:.0f}ms{RESET}")

    if answer.caveats:
        _log(f"\n{BOLD}Caveats{RESET} {DIM}(written from the facts, not asked of the model){RESET}")
        for caveat in answer.caveats:
            _log(f"  {YELLOW}!{RESET} {caveat}")


def _check(answer: CopilotAnswer, events: list[TraceEvent], *, degraded: bool) -> None:
    """Fail the run if the scenario's own requirements were not met.

    `degraded` drops the two MCP requirements, because the caller asked for the
    documents-only path with `--allow-degraded` and it is the *absence* of alarm data that is
    under test there. Everything else still has to hold: a degraded answer that loses its
    citations is a different bug, and worth catching on the same run.
    """
    mcp_calls = [event for event in events if event.kind == "mcp_tool_call"]
    retrievals = [event for event in events if event.kind == "rag_retrieval"]
    problems: list[str] = []

    if not degraded and not mcp_calls:
        problems.append(
            "no MCP tool call — the alarm API must be reached exclusively through the MCP server, "
            "so an answer without one is not the required workflow"
        )
    if not degraded and not any(
        call.name == "search_assets" and call.ok for call in answer.tool_calls
    ):
        problems.append("the asset was never resolved by name through search_assets")
    if degraded and not answer.caveats:
        problems.append(
            "the MCP server was unreachable and the answer said nothing about it — the reader "
            "has to be told the alarm data is missing, not just given a thinner answer"
        )
    if not retrievals:
        problems.append("no retrieval — the answer was not grounded in the procedure documents")
    if not answer.citations:
        problems.append("no citations were offered to the answer")
    if not any(citation.cited_in_answer for citation in answer.citations):
        problems.append("the answer cited none of the retrieved passages")
    if not answer.answer.strip():
        problems.append("the answer was empty")

    # Reported, not fatal: these are things a real model does that the deterministic test
    # cannot surface, and seeing them is the point of running this.
    if answer.invented_references:
        _log(
            f"\n{YELLOW}Observation{RESET} the model cited "
            f"{', '.join(answer.invented_references)}, which was not retrieved. The citation "
            f"check neutralised it in the text — working as designed, and worth knowing."
        )
    if answer.steps_exhausted:
        _log(
            f"\n{YELLOW}Observation{RESET} the step ceiling "
            f"({answer.steps_used} steps) was reached before the model said it was done. "
            f"Raise ORCHESTRATOR_MAX_STEPS if the investigation was genuinely incomplete."
        )
    if answer.low_confidence:
        _log(
            f"\n{YELLOW}Observation{RESET} retrieval reported low confidence. Check the index "
            f"was built with the same embedder the backend is configured for."
        )

    if problems:
        raise SmokeFailure(
            "the scenario did not meet its requirements:\n    - " + "\n    - ".join(problems)
        )


# --- wiring ------------------------------------------------------------------------------


def _open_retriever(rag_settings: RagSettings) -> HybridRetriever:
    try:
        return HybridRetriever.open(rag_settings)
    except Exception as error:  # noqa: BLE001 - one message is more use here than a traceback
        raise SmokeFailure(
            f"could not open the index at {rag_settings.qdrant_path}: {error}\n"
            "    Build it with `make ingest`. If it exists, something else holds the lock — "
            "embedded Qdrant is exclusive, so stop `make backend` first."
        ) from None


async def smoke(
    settings: BackendSettings,
    rag_settings: RagSettings,
    *,
    question: str,
    allow_degraded: bool,
    as_json: bool,
) -> None:
    retriever = _open_retriever(rag_settings)
    # `_default_session_factory` rather than dialling the URL here: the point of this script is
    # that the connection it makes is the one the served backend makes, including whatever the
    # factory grows later. Reaching past the underscore is the lesser evil against duplicating it.
    service = CopilotService(
        settings=settings,
        provider=build_provider(settings),
        retriever=retriever,
        session_factory=_default_session_factory(settings),
    )

    try:
        _log(f"{BOLD}Checking the MCP server{RESET} {DIM}{settings.mcp_server_url}{RESET}")
        reachable = await service.mcp_reachable()
        if not reachable and not allow_degraded:
            raise SmokeFailure(
                f"the MCP server at {settings.mcp_server_url} did not answer.\n"
                "    Start it with `make mcp` (and the API it fronts with `make api`).\n"
                "    If something else already holds that port, run `make mcp MCP_PORT=<free "
                "port>` and set MCP_SERVER_URL to match.\n"
                "    To smoke the documents-only degraded path on purpose, pass --allow-degraded."
            )
        _log(
            f"{GREEN}✓{RESET} reachable"
            if reachable
            else f"{YELLOW}!{RESET} unreachable — running the degraded, documents-only path"
        )

        tools, degradations = await service.catalogue()
        _log(f"\n{BOLD}Catalogue{RESET} {DIM}one list, both backends{RESET}")
        for tool in tools:
            _log(f"  {tool['name']:<32} {DIM}{tool['backend']}{RESET}")
        for degradation in degradations:
            _log(f"  {YELLOW}!{RESET} {degradation}")

        _log(f"\n{BOLD}Question{RESET}")
        _log(f"  {question}")
        _log(f"\n{BOLD}Steps{RESET} {DIM}(the SSE stream, as it happens){RESET}")

        try:
            answer = await service.ask(question, sink=_print_event)
        except CopilotError as error:
            # Two failures dominate real runs of this script, and neither says what to do:
            # a rejected key (Google answers 400 API_KEY_INVALID, not 401) and the free tier
            # refusing service under load.
            text = str(error)
            if "API key" in text or "API_KEY_INVALID" in text or "403" in text:
                hint = (
                    "\n    Google rejected the key. Create a new one at "
                    "https://aistudio.google.com/apikey, put it in .env, then confirm it with "
                    "`make probe` before rerunning."
                )
            elif "429" in text or "503" in text:
                hint = (
                    "\n    The free tier is rate-limited or busy. LLM_MAX_RETRIES already "
                    "retries; wait a minute and rerun."
                )
            else:
                hint = ""
            raise SmokeFailure(
                f"the question could not be answered at all: {error}{hint}"
            ) from None

        trace = service.trace(answer.conversation_id)
        events = trace.events if trace else []

        _print_answer(answer)
        _trace_table(events)
        _check(answer, events, degraded=not reachable)

        _log(
            f"\n{GREEN}✓ the scenario met its requirements{RESET} "
            f"{DIM}({answer.steps_used} steps, conversation={answer.conversation_id}, "
            f"trace_id={answer.trace_id}){RESET}"
        )
        if as_json:
            print(answer.model_dump_json(indent=2))
    finally:
        await service.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=QUESTION, help="override the acceptance question")
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="proceed with documents only when the MCP server is unreachable",
    )
    parser.add_argument("--json", action="store_true", help="dump the answer object to stdout")
    args = parser.parse_args()

    settings = BackendSettings()
    if settings.llm_provider == "scripted":
        _log(
            f"{RED}LLM_PROVIDER=scripted{RESET} — this script exists to exercise the real "
            f"model.\n  Set LLM_PROVIDER=gemini in .env, or run `make test-e2e` for the "
            f"deterministic version of this scenario."
        )
        raise SystemExit(2)
    if not settings.gemini_api_key:
        _log(
            f"{RED}GEMINI_API_KEY is not set.{RESET}\n"
            "  Get one from https://aistudio.google.com/apikey and put it in .env (gitignored — "
            "never in a tracked file), then run `make probe` to confirm the API answers before "
            "running this."
        )
        raise SystemExit(2)

    try:
        asyncio.run(
            smoke(
                settings,
                RagSettings(),
                question=args.question,
                allow_degraded=args.allow_degraded,
                as_json=args.json,
            )
        )
    except* Exception as failures:
        for failure in _leaves(failures):
            label = "" if isinstance(failure, SmokeFailure) else f"{type(failure).__name__}: "
            _log(f"\n{RED}✗ {label}{failure}{RESET}")
        raise SystemExit(1) from None


def _leaves(error: BaseException) -> list[BaseException]:
    """Flatten nested ExceptionGroups to the real exceptions. The MCP client's task group
    wraps anything that propagates through it, sometimes twice."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for nested in error.exceptions for leaf in _leaves(nested)]
    return [error]


if __name__ == "__main__":
    main()
