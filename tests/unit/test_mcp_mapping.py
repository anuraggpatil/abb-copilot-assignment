"""Unit tests for the MCP layer's two pure functions.

Both are small and both are load-bearing, which is exactly the shape that benefits from
tests at this level: `to_tool_error` decides what a language model is told when the alarm
system refuses, and `upstream_from` decides what the operator sees in the trace panel.

`tests/integration/test_mcp_server.py` checks that these messages survive the round trip
through a real MCP session. Here the concern is the wording itself — every branch, including
the ones a live API is awkward to push into, like a 502 with no JSON body.
"""

from __future__ import annotations

import pytest
from alarm_management.mapping import to_tool_error
from alarm_management.schemas import upstream_from

from connectors.alarm_api import (
    AlarmApiAuthError,
    AlarmApiError,
    AlarmApiNotFoundError,
    AlarmApiProtocolError,
    AlarmApiRateLimitError,
    AlarmApiUnavailableError,
    AlarmApiValidationError,
    CallRecord,
)


def _record(path: str, *, status: int | None = 200, error: str | None = None) -> CallRecord:
    return CallRecord(
        method="GET",
        path=path,
        attempt=1,
        status_code=status,
        duration_ms=10.0,
        error=error,
        trace_id="trc-1",
    )


# --- to_tool_error ----------------------------------------------------------------------


def test_a_missing_asset_is_told_how_to_find_a_real_one() -> None:
    """A 404 without a recovery route is a dead end the model will just retry."""
    error = to_tool_error(
        AlarmApiNotFoundError("No asset with id 'AST-9'.", status_code=404), tool="get_alarms"
    )
    assert "No asset with id 'AST-9'." in str(error)
    assert "search_assets" in str(error)


def test_a_validation_failure_passes_the_apis_message_through_verbatim() -> None:
    """That message lists the accepted values, which is the whole repair instruction."""
    error = to_tool_error(
        AlarmApiValidationError(
            "unknown kpis ['x']; expected a subset of ['alarm_count', 'avg_ack_delay']",
            status_code=422,
        ),
        tool="get_alarm_summary",
    )
    message = str(error)
    assert "expected a subset of ['alarm_count', 'avg_ack_delay']" in message
    assert "call get_alarm_summary again" in message


def test_a_validation_failure_names_the_offending_field_when_the_api_said_which() -> None:
    error = to_tool_error(
        AlarmApiValidationError(
            "The request did not match the expected schema.",
            status_code=422,
            detail=[
                {"loc": ["body", "lookback_days"], "msg": "too large", "type": "less_than_equal"}
            ],
        ),
        tool="get_operator_recommendations",
    )
    assert "Invalid field(s): lookback_days." in str(error)


def test_repeated_and_nested_field_paths_are_reported_once_each() -> None:
    error = to_tool_error(
        AlarmApiValidationError(
            "nope",
            status_code=422,
            detail=[
                {"loc": ["body", "time_range", "start"]},
                {"loc": ["body", "time_range", "start"]},
                {"loc": ["query", "page"]},
                # Neither a dict nor a usable loc: both must be skipped, not crash.
                "unexpected",
                {"loc": []},
            ],
        ),
        tool="get_alarms",
    )
    assert "Invalid field(s): time_range.start, page." in str(error)


def test_a_validation_failure_without_detail_omits_the_field_clause() -> None:
    """Most 4xx carry no detail; an empty "Invalid field(s):" would be noise."""
    error = to_tool_error(
        AlarmApiValidationError("bad request", status_code=400), tool="get_alarms"
    )
    assert "Invalid field(s)" not in str(error)


def test_an_auth_failure_tells_the_model_to_stop_rather_than_retry() -> None:
    """A credential is not something the request can fix, and retrying it risks a lockout."""
    error = to_tool_error(
        AlarmApiAuthError("Invalid or unknown bearer token.", status_code=401),
        tool="search_assets",
    )
    message = str(error)
    assert "Do not retry" in message
    assert "configuration problem" in message
    # The upstream's own wording is dropped here: it describes a credential.
    assert "bearer token" not in message


def test_a_rate_limit_reports_how_long_to_wait_when_the_api_said() -> None:
    error = to_tool_error(
        AlarmApiRateLimitError("slow down", status_code=429, retry_after=12.0),
        tool="get_alarms",
    )
    assert "Retry after about 12s" in str(error)


def test_a_rate_limit_without_a_retry_after_still_reads_sensibly() -> None:
    error = to_tool_error(AlarmApiRateLimitError("slow down", status_code=429), tool="get_alarms")
    assert "rate limiting" in str(error)
    assert "Retry after" not in str(error)


def test_an_outage_is_stated_as_an_outage_and_not_as_an_absence_of_alarms() -> None:
    """The single most important message in this module.

    "0 alarms" and "we could not ask" lead to opposite advice, and only one of them is safe
    to give someone standing in front of a running pump.
    """
    error = to_tool_error(
        AlarmApiUnavailableError("connection refused", status_code=None), tool="get_alarms"
    )
    message = str(error)
    assert "not an empty result" in message
    assert "unavailable" in message


def test_a_contract_break_is_distinguished_from_an_outage() -> None:
    error = to_tool_error(AlarmApiProtocolError("cannot parse AlarmPage"), tool="get_alarms")
    assert "contract has changed" in str(error)


def test_an_unclassified_failure_still_produces_a_usable_message() -> None:
    """A 418 has no bespoke advice, but it must not surface as a bare class name."""
    error = to_tool_error(AlarmApiError("teapot", status_code=418), tool="get_alarms")
    assert "get_alarms" in str(error)
    assert "teapot" in str(error)


@pytest.mark.parametrize(
    "exc",
    [
        AlarmApiNotFoundError("gone", status_code=404, trace_id="trc-9"),
        AlarmApiValidationError("bad", status_code=422, trace_id="trc-9"),
        AlarmApiAuthError("nope", status_code=401, trace_id="trc-9"),
        AlarmApiRateLimitError("slow", status_code=429, trace_id="trc-9"),
        AlarmApiUnavailableError("down", status_code=503, trace_id="trc-9"),
        AlarmApiProtocolError("weird", trace_id="trc-9"),
        AlarmApiError("other", status_code=418, trace_id="trc-9"),
    ],
)
def test_every_mapped_error_carries_the_trace_id(exc: AlarmApiError) -> None:
    """Without it the failure in the GUI cannot be joined to the API's own log line."""
    assert "trace_id=trc-9" in str(to_tool_error(exc, tool="get_alarms"))


# --- upstream_from ----------------------------------------------------------------------


def test_retries_of_one_call_are_folded_into_a_single_entry() -> None:
    """A trace should read "one call, three attempts", not present three rows to reassemble."""
    calls = upstream_from(
        [
            _record("/alarms", status=503),
            _record("/alarms", status=503),
            _record("/alarms", status=200),
        ]
    )
    assert len(calls) == 1
    assert calls[0].attempts == 3
    assert calls[0].status_code == 200, "the outcome is the last attempt's"
    assert calls[0].duration_ms == 30.0, "elapsed time is the sum of the attempts"
    assert calls[0].error is None


def test_distinct_calls_stay_distinct() -> None:
    calls = upstream_from([_record("/assets/search"), _record("/assets/AST-1/metadata")])
    assert [call.path for call in calls] == ["/assets/search", "/assets/AST-1/metadata"]
    assert all(call.attempts == 1 for call in calls)


def test_the_same_path_called_twice_is_two_calls_not_two_attempts() -> None:
    """Only *consecutive* records are retries; pagination revisits the same path on purpose."""
    calls = upstream_from([_record("/alarms"), _record("/assets/search"), _record("/alarms")])
    assert [call.path for call in calls] == ["/alarms", "/assets/search", "/alarms"]
    assert all(call.attempts == 1 for call in calls)


def test_a_call_that_never_landed_keeps_its_error_and_has_no_status() -> None:
    calls = upstream_from([_record("/alarms", status=None, error="transport: refused")])
    assert calls[0].status_code is None
    assert calls[0].error == "transport: refused"


def test_no_calls_means_no_entries() -> None:
    assert upstream_from([]) == []
