#!/usr/bin/env python
"""Prove the MCP server works as a standalone service, over the network.

The test suite connects to an `MCPServer` object in-process. That is the right default — it
is fast and deterministic — but it also bypasses the transport, the CLI, the settings
loading and the process boundary. Everything this script exercises is therefore untested by
`pytest`, and all of it is what breaks first when the server is deployed as its own
container.

So this speaks real Streamable HTTP to a separately-started process:

    make api      # terminal 1 — the alarm API the server reads from
    make mcp      # terminal 2 — the MCP server itself
    make mcp-smoke

It discovers the tool catalogue the way a copilot would, then runs the full investigation
chain, passing each result into the next call's arguments. A `--json` flag dumps the last
result for piping into `jq`.

Exit code is 0 only if every step succeeded, so this is usable as a container healthcheck
or a CI gate against a deployed instance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, cast

from mcp.client import Client
from mcp.types import CallToolResult, RequestParamsMeta, TextContent

DEFAULT_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:9100/mcp")
TRACE_ID = "trc-mcp-smoke"

# ANSI, but only when stdout is a terminal — piping into jq or a log file should not get
# escape codes embedded in it.
_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""


def _log(message: str) -> None:
    print(message, file=sys.stderr)


class SmokeFailure(Exception):
    """A step did not do what it was supposed to.

    Deliberately an `Exception` and not `SystemExit`: this is raised inside the client's anyio
    task group, which wraps whatever escapes it in an `ExceptionGroup`. `SystemExit` derives
    from `BaseException`, so a group containing one is not caught by `except* Exception` — the
    script would print a bare traceback and exit 0, reporting success for a failed run.
    """


def _structured(result: CallToolResult, *, step: str) -> dict[str, Any]:
    """Unwrap a tool result, failing loudly with the model-facing text if it errored."""
    if result.is_error:
        block = result.content[0] if result.content else None
        detail = block.text if isinstance(block, TextContent) else "no detail returned"
        raise SmokeFailure(f"{step} was refused\n  {detail}")
    if result.structured_content is None:
        raise SmokeFailure(f"{step} returned no structured content")
    return result.structured_content


def _report(payload: dict[str, Any]) -> None:
    """Print the upstream calls a tool made, which is what the GUI's trace panel shows."""
    for call in payload.get("upstream", []):
        status = call["status_code"] or "—"
        attempts = f" ×{call['attempts']}" if call["attempts"] > 1 else ""
        _log(
            f"    {DIM}{call['method']} {call['path']} → {status}{attempts} "
            f"in {call['duration_ms']:.0f}ms{RESET}"
        )


async def smoke(url: str, *, asset: str, as_json: bool) -> None:
    meta = cast("RequestParamsMeta", {"trace_id": TRACE_ID})

    _log(f"{BOLD}Connecting to {url}{RESET}")
    async with Client(url) as mcp:
        # --- discovery: exactly what a planner is handed ---------------------------------
        catalogue = await mcp.list_tools()
        _log(f"{GREEN}✓{RESET} discovered {len(catalogue.tools)} tools")
        for tool in catalogue.tools:
            required = tool.input_schema.get("required", [])
            _log(f"    {BOLD}{tool.name}{RESET} — {tool.title}")
            _log(f"      {DIM}required: {', '.join(required) or 'nothing'}{RESET}")

        # --- the chain: each call's arguments come from the last call's result -----------
        _log(f"\n{BOLD}Resolving {asset!r}{RESET}")
        search = _structured(
            await mcp.call_tool("search_assets", {"query": asset}, meta=meta),
            step="search_assets",
        )
        _report(search)
        if not search["assets"]:
            raise SmokeFailure(f"no asset matched {asset!r} — is the API seeded?")
        asset_id = search["assets"][0]["asset_id"]
        _log(
            f"{GREEN}✓{RESET} {search['best_match_tag']} → {asset_id} "
            f"(criticality {search['best_match_criticality']}, "
            f"{search['best_match_active_alarms']} active alarms)"
        )

        _log(f"\n{BOLD}Looking for recurring high-severity patterns{RESET}")
        recurring = _structured(
            await mcp.call_tool(
                "get_recurring_alarms",
                {"asset_id": asset_id, "min_severity": "high", "lookback_days": 90},
                meta=meta,
            ),
            step="get_recurring_alarms",
        )
        _report(recurring)
        _log(f"{GREEN}✓{RESET} {recurring['total_patterns']} patterns")
        for pattern in recurring["patterns"][:3]:
            _log(
                f"    {pattern['alarm_name']}: {pattern['occurrences']}× "
                f"({pattern['occurrences_first_half']}→{pattern['occurrences_second_half']}, "
                f"{BOLD}{pattern['trend']}{RESET}, "
                f"{pattern['chattering_share']:.0%} chattering)"
            )

        _log(f"\n{BOLD}Fetching the underlying events{RESET}")
        alarms = _structured(
            await mcp.call_tool(
                "get_alarms",
                {
                    "asset_id": asset_id,
                    "severity": ["high", "critical"],
                    "lookback_days": 90,
                    "limit": 5,
                },
                meta=meta,
            ),
            step="get_alarms",
        )
        _report(alarms)
        if not alarms["alarms"]:
            raise SmokeFailure("no high-severity alarms in the window")
        _log(
            f"{GREEN}✓{RESET} {alarms['returned']} of {alarms['total_matching']} events "
            f"({alarms['window_start'][:10]} → {alarms['window_end'][:10]})"
        )
        alarm_id = alarms["alarms"][0]["alarm_id"]

        _log(f"\n{BOLD}Requesting ranked operator actions{RESET}")
        actions = _structured(
            await mcp.call_tool(
                "get_operator_recommendations",
                {"alarm_id": alarm_id, "lookback_days": 90},
                meta=meta,
            ),
            step="get_operator_recommendations",
        )
        _report(actions)
        _log(f"{GREEN}✓{RESET} {len(actions['actions'])} actions for {actions['alarm_name']}")
        for action in actions["actions"][:3]:
            _log(f"    {action['rank']}. {action['action']}")
            _log(f"       {DIM}↳ {action['procedure_reference']}{RESET}")
        if actions["escalation"]["required"]:
            _log(f"    {BOLD}escalate to {actions['escalation']['escalate_to']}{RESET}")

        # --- the seam RAG picks up ------------------------------------------------------
        references = actions["procedure_references"]
        _log(f"\n{GREEN}✓{RESET} {BOLD}{len(references)} procedure references for RAG{RESET}")
        for reference in references:
            _log(f"    {reference}")

        if actions["trace_id"] != TRACE_ID:
            raise SmokeFailure(
                f"the supplied trace id did not survive the round trip: sent {TRACE_ID!r}, "
                f"got {actions['trace_id']!r}"
            )
        _log(f"\n{GREEN}✓ all steps passed{RESET} {DIM}(trace_id={TRACE_ID}){RESET}")

    if as_json:
        print(json.dumps(actions, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"default: {DEFAULT_URL}")
    parser.add_argument("--asset", default="Boiler Feed Pump 101", help="asset to investigate")
    parser.add_argument(
        "--json", action="store_true", help="dump the recommendations result to stdout"
    )
    args = parser.parse_args()

    try:
        asyncio.run(smoke(args.url, asset=args.asset, as_json=args.json))
    except* Exception as failures:
        # A failure inside the session surfaces as an ExceptionGroup out of the client's task
        # group, and the groups nest — so flatten to the leaves. Printing only the top level
        # reports "unhandled errors in a TaskGroup" and hides the one thing worth knowing.
        leaves = _leaves(failures)
        for failure in leaves:
            label = "" if isinstance(failure, SmokeFailure) else f"{type(failure).__name__}: "
            _log(f"{RED}✗ {label}{failure}{RESET}")
        if not any(isinstance(failure, SmokeFailure) for failure in leaves):
            # Only suggest this when nothing reached the server at all. A tool that answered
            # with an error proves the server is up, and saying otherwise sends whoever is
            # debugging to the wrong process — the alarm API is the likelier suspect.
            _log(f"\n{DIM}Could not reach {args.url} — start it with: make mcp{RESET}")
        raise SystemExit(1) from None


def _leaves(error: BaseException) -> list[BaseException]:
    """Flatten arbitrarily nested ExceptionGroups down to the real exceptions."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for nested in error.exceptions for leaf in _leaves(nested)]
    return [error]


if __name__ == "__main__":
    main()
