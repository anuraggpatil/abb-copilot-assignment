"""Domain exceptions for the Alarm Management API connector.

One exception per *thing the caller can do about it*, not one per HTTP status. The MCP
layer turns these into tool errors and a model reads the message, so the distinction that
matters is "fix your arguments" (`AlarmApiValidationError`) versus "that does not exist"
(`AlarmApiNotFoundError`) versus "try again later" (`AlarmApiUnavailableError`). Statuses
that call for the same response share a class.

Nothing here imports from `apps.alarm_api`. The connector talks to the API over HTTP like
any other client would, and sharing types with the server it calls would hide exactly the
contract drift the connector exists to absorb.
"""

from __future__ import annotations

from typing import Any


class AlarmApiError(Exception):
    """Base class: anything that went wrong reaching or using the Alarm API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        trace_id: str | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        # The API's own machine-readable code (`asset_not_found`, `invalid_parameter`, …).
        self.error_code = error_code
        self.trace_id = trace_id
        self.detail = detail

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.error_code:
            parts.append(self.error_code)
        if self.trace_id:
            parts.append(f"trace_id={self.trace_id}")
        return " | ".join(parts)


class AlarmApiAuthError(AlarmApiError):
    """401/403 — the token is missing, malformed or not accepted.

    Deliberately not retried: repeating a rejected credential cannot start working, and
    retrying an auth failure is how a service locks itself out of an upstream.
    """


class AlarmApiNotFoundError(AlarmApiError):
    """404 — the asset or alarm id does not exist.

    Actionable: the caller should re-resolve the id rather than retry the same request.
    """


class AlarmApiValidationError(AlarmApiError):
    """400/422 — the request did not match the API's schema or allowed values.

    The API's message lists the valid options, so it is passed through verbatim: that
    message is what lets a tool-calling model correct its own arguments and retry.
    """


class AlarmApiRateLimitError(AlarmApiError):
    """429 — too many requests. Retryable, honouring `Retry-After` when present."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class AlarmApiUnavailableError(AlarmApiError):
    """5xx, a connection failure, or a timeout — the API could not answer.

    Retryable. Distinct from the 4xx classes because the caller's request was fine; it is
    the upstream that is unwell, and the copilot should say so rather than claim the plant
    has no alarms.
    """


class AlarmApiProtocolError(AlarmApiError):
    """The API answered, but not with something this connector can parse.

    A 200 whose body does not match the expected schema is a contract break, and surfacing
    it as its own class stops it being mistaken for "no results".
    """


#: Statuses worth another attempt. 429 and 5xx are transient by definition; 4xx (other
#: than 429) will fail identically on every retry, so retrying them only adds latency.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def error_for_status(
    status_code: int,
    *,
    message: str,
    error_code: str | None = None,
    trace_id: str | None = None,
    detail: Any = None,
    retry_after: float | None = None,
) -> AlarmApiError:
    """Map an HTTP status onto the exception class that describes what to do about it."""
    kwargs: dict[str, Any] = {
        "status_code": status_code,
        "error_code": error_code,
        "trace_id": trace_id,
        "detail": detail,
    }
    if status_code in (401, 403):
        return AlarmApiAuthError(message, **kwargs)
    if status_code == 404:
        return AlarmApiNotFoundError(message, **kwargs)
    if status_code == 429:
        return AlarmApiRateLimitError(message, retry_after=retry_after, **kwargs)
    if status_code in (400, 422):
        return AlarmApiValidationError(message, **kwargs)
    if status_code >= 500:
        return AlarmApiUnavailableError(message, **kwargs)
    # Any other 4xx: a client-side problem we have no more specific advice for.
    return AlarmApiError(message, **kwargs)
