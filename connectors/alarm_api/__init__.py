"""Reusable typed client for the Alarm Management API.

The copilot never imports this package directly — it reaches the alarm API only through the
MCP server, which is the assignment's hard constraint. The connector exists so that the MCP
tools contain tool logic rather than HTTP plumbing, and so the API integration (auth, retry,
timeouts, error semantics) can be tested without an MCP session in the way.
"""

from connectors.alarm_api.client import (
    AlarmApiClient,
    CallRecord,
    TraceContext,
)
from connectors.alarm_api.errors import (
    AlarmApiAuthError,
    AlarmApiError,
    AlarmApiNotFoundError,
    AlarmApiProtocolError,
    AlarmApiRateLimitError,
    AlarmApiUnavailableError,
    AlarmApiValidationError,
)

__all__ = [
    "AlarmApiAuthError",
    "AlarmApiClient",
    "AlarmApiError",
    "AlarmApiNotFoundError",
    "AlarmApiProtocolError",
    "AlarmApiRateLimitError",
    "AlarmApiUnavailableError",
    "AlarmApiValidationError",
    "CallRecord",
    "TraceContext",
]
