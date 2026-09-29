"""Translate connector exceptions into MCP tool errors.

The audience for these messages is a language model deciding what to do next, so each one
states the failure *and* the move that follows from it: re-resolve the id, fix the argument,
wait, or stop and tell the operator the data is unavailable. A bare "HTTP 404" leaves the
model to guess, and a guessing model retries the same call.

The other half of the job is deciding what *not* to say. `ToolError` text is returned to the
client verbatim, so it must carry no token, no internal traceback and no plant data beyond
what the caller already supplied.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from connectors.alarm_api import (
    AlarmApiAuthError,
    AlarmApiError,
    AlarmApiNotFoundError,
    AlarmApiProtocolError,
    AlarmApiRateLimitError,
    AlarmApiUnavailableError,
    AlarmApiValidationError,
)

#: Appended to messages the model is expected to act on itself.
_RESOLVE_HINT = "Call search_assets to resolve a name or tag to a valid asset_id."


def to_tool_error(exc: AlarmApiError, *, tool: str) -> ToolError:
    """Map a connector exception onto the tool error a model can act on."""
    trace = f" (trace_id={exc.trace_id})" if exc.trace_id else ""

    if isinstance(exc, AlarmApiNotFoundError):
        # The API's message already names the id it could not find.
        return ToolError(f"{exc.message} {_RESOLVE_HINT}{trace}")

    if isinstance(exc, AlarmApiValidationError):
        # Passed through verbatim: it names the offending field and lists the accepted
        # values, which is exactly the repair instruction. `detail` is added when present
        # because a field path turns "invalid request" into "fix lookback_days".
        fields = _invalid_fields(exc)
        where = f" Invalid field(s): {', '.join(fields)}." if fields else ""
        return ToolError(
            f"The alarm API rejected the arguments for {tool}: {exc.message}{where} "
            f"Correct them and call {tool} again.{trace}"
        )

    if isinstance(exc, AlarmApiAuthError):
        # A credential problem belongs to whoever deployed this server, not to the model.
        # Saying so stops it retrying, and the token itself is never in `exc.message`.
        return ToolError(
            "This MCP server is not authorised to read the alarm API, which is a server "
            "configuration problem rather than something the request can fix. Do not retry; "
            f"report that alarm data is unavailable.{trace}"
        )

    if isinstance(exc, AlarmApiRateLimitError):
        after = f" Retry after about {exc.retry_after:.0f}s." if exc.retry_after else ""
        return ToolError(f"The alarm API is rate limiting this server.{after}{trace}")

    if isinstance(exc, AlarmApiUnavailableError):
        # The important instruction is the last clause: an unreachable historian must not
        # become "there are no alarms".
        return ToolError(
            f"The alarm API could not be reached, so {tool} returned no data. This is an "
            "outage, not an empty result: tell the operator that live alarm data is "
            f"unavailable instead of answering without it.{trace}"
        )

    if isinstance(exc, AlarmApiProtocolError):
        return ToolError(
            f"The alarm API returned a response {tool} cannot interpret, which means the API "
            "contract has changed. Treat the alarm data as unavailable and report the "
            f"failure.{trace}"
        )

    return ToolError(f"The alarm API call behind {tool} failed: {exc.message}{trace}")


def _invalid_fields(exc: AlarmApiValidationError) -> list[str]:
    """Field paths out of the API's `detail` list, if it sent one.

    The simulator's 422 body carries pydantic's error list under `detail`; other 4xx carry
    no detail at all. Both are normal, so this returns an empty list rather than raising.
    """
    if not isinstance(exc.detail, list):
        return []
    fields: list[str] = []
    for entry in exc.detail:
        if not isinstance(entry, dict):
            continue
        loc = entry.get("loc")
        if isinstance(loc, list) and loc:
            # Drop the leading "body"/"query" segment: the model chose the field, not the
            # part of the HTTP request it ended up in.
            path = [str(part) for part in loc if str(part) not in {"body", "query", "path"}]
            if path:
                fields.append(".".join(path))
    return list(dict.fromkeys(fields))
