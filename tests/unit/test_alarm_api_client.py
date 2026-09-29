"""Tests for the Alarm Management API connector.

Everything here is mocked at the transport with `respx`, because what is under test is the
connector's *protocol* behaviour — which statuses retry, which exception a 404 becomes, that
the token never leaks — and those are precisely the cases a real server makes hard to
produce on demand. A 503 followed by a 200 is one line here and a fixture nightmare
otherwise.

The connector is also exercised against the real simulator in
`tests/integration/test_connector_contract.py`; that is where "the two agree about the wire
format" is asserted. Neither test alone is sufficient: mocks confirm behaviour the server
cannot easily produce, and the real server confirms the mocks describe reality.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from connectors.alarm_api.client import (
    CLIENT_ID_HEADER,
    METADATA_TAG_HEADER,
    TRACE_ID_HEADER,
    AlarmApiClient,
    CallRecord,
    Observer,
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

BASE_URL = "http://alarm-api.test"
TOKEN = "connector-test-token"

ALARM_ROW = {
    "alarm_id": "ALM-20260901-000001",
    "alarm_name": "Bearing Vibration High",
    "alarm_type": "process",
    "asset_id": "AST-PMP-0001",
    "asset_name": "Boiler Feed Pump 101",
    "site": "EastRefinery",
    "unit": "Unit 2",
    "severity": "high",
    "status": "active",
    "start_time": "2026-09-01T10:00:00Z",
}

ALARM_PAGE = {
    "data": [ALARM_ROW],
    "pagination": {
        "page": 1,
        "page_size": 50,
        "total": 1,
        "total_pages": 1,
        "has_next": False,
        "sort_by": "start_time",
        "sort_order": "desc",
    },
    "filters_applied": {"unit": "Unit 2"},
}


def _client(
    *,
    max_retries: int = 2,
    client_id: str | None = None,
    observer: Observer | None = None,
) -> AlarmApiClient:
    return AlarmApiClient(
        BASE_URL,
        TOKEN,
        max_retries=max_retries,
        client_id=client_id,
        observer=observer,
    )


# --- requests the connector sends -------------------------------------------------------


@respx.mock
async def test_bearer_token_is_attached() -> None:
    route = respx.get(f"{BASE_URL}/alarms").mock(return_value=httpx.Response(200, json=ALARM_PAGE))
    async with _client() as client:
        await client.get_alarms(unit="Unit 2")
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
async def test_health_is_called_without_credentials() -> None:
    """A liveness probe must not carry a token; the API does not ask for one."""
    route = respx.get(f"{BASE_URL}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "dataset": {}})
    )
    async with _client() as client:
        assert (await client.health()).status == "ok"
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_trace_headers_are_propagated_with_their_exact_spellings() -> None:
    """`trace_id` is snake_case while the other two are `x-` prefixed. Not a typo."""
    route = respx.post(f"{BASE_URL}/alarms/summary").mock(
        return_value=httpx.Response(200, json={"total_alarms": 0})
    )
    async with _client() as client:
        await client.summarize_alarms(
            {},
            trace=TraceContext(trace_id="trc-1", client_id="copilot", metadata_tag="investigation"),
        )
    headers = route.calls.last.request.headers
    assert headers[TRACE_ID_HEADER] == "trc-1"
    assert headers[CLIENT_ID_HEADER] == "copilot"
    assert headers[METADATA_TAG_HEADER] == "investigation"


@respx.mock
async def test_constructor_client_id_is_used_when_the_call_omits_one() -> None:
    route = respx.get(f"{BASE_URL}/alarms").mock(return_value=httpx.Response(200, json=ALARM_PAGE))
    async with _client(client_id="alarm-copilot") as client:
        await client.get_alarms(trace=TraceContext(trace_id="trc-9"))
    headers = route.calls.last.request.headers
    assert headers[CLIENT_ID_HEADER] == "alarm-copilot"
    assert headers[TRACE_ID_HEADER] == "trc-9"


@respx.mock
async def test_list_parameters_repeat_the_key() -> None:
    """`?severity=high&severity=critical`, not `severity=['high', 'critical']`."""
    route = respx.get(f"{BASE_URL}/alarms").mock(return_value=httpx.Response(200, json=ALARM_PAGE))
    async with _client() as client:
        await client.get_alarms(severity=["high", "critical"], asset_ids=["A1", "A2"])
    query = route.calls.last.request.url.params
    assert query.get_list("severity") == ["high", "critical"]
    assert query.get_list("asset_id") == ["A1", "A2"]


@respx.mock
async def test_none_parameters_are_dropped_not_stringified() -> None:
    """A literal "None" in the query string would come back as a 422 invalid enum value."""
    route = respx.get(f"{BASE_URL}/alarms").mock(return_value=httpx.Response(200, json=ALARM_PAGE))
    async with _client() as client:
        await client.get_alarms(unit=None, site=None)
    query = str(route.calls.last.request.url.query)
    assert "None" not in query
    assert "unit" not in query


@respx.mock
async def test_datetimes_are_sent_as_iso8601() -> None:
    route = respx.get(f"{BASE_URL}/alarms").mock(return_value=httpx.Response(200, json=ALARM_PAGE))
    async with _client() as client:
        await client.get_alarms(start_time=datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
    assert route.calls.last.request.url.params["start_time"] == "2026-07-01T12:00:00+00:00"


# --- error mapping ----------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AlarmApiAuthError),
        (403, AlarmApiAuthError),
        (404, AlarmApiNotFoundError),
        (400, AlarmApiValidationError),
        (422, AlarmApiValidationError),
        (429, AlarmApiRateLimitError),
        (500, AlarmApiUnavailableError),
        (503, AlarmApiUnavailableError),
    ],
)
async def test_statuses_map_to_actionable_exception_types(
    status: int, expected: type[Exception]
) -> None:
    respx.get(f"{BASE_URL}/assets/AST-1/metadata").mock(
        return_value=httpx.Response(status, json={"error": "some_code", "message": "nope"})
    )
    async with _client(max_retries=0) as client:
        with pytest.raises(expected):
            await client.asset_metadata("AST-1")


@respx.mock
async def test_the_apis_own_message_survives_intact() -> None:
    """It names the valid options, which is what lets a model fix its own call and retry."""
    respx.post(f"{BASE_URL}/alarms/summary").mock(
        return_value=httpx.Response(
            422,
            json={
                "error": "invalid_parameter",
                "message": "unknown kpis: made_up. valid: alarm_count, avg_ack_delay",
                "trace_id": "trc-err",
            },
        )
    )
    async with _client(max_retries=0) as client:
        with pytest.raises(AlarmApiValidationError) as exc:
            await client.summarize_alarms({"kpis": ["made_up"]})
    assert "valid: alarm_count" in exc.value.message
    assert exc.value.error_code == "invalid_parameter"
    assert exc.value.trace_id == "trc-err"
    assert exc.value.status_code == 422


@respx.mock
async def test_a_failure_with_no_json_body_still_maps_cleanly() -> None:
    """A proxy returning an HTML 502 must not become an unhandled parse error."""
    respx.get(f"{BASE_URL}/alarms").mock(
        return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
    )
    async with _client(max_retries=0) as client:
        with pytest.raises(AlarmApiUnavailableError) as exc:
            await client.get_alarms()
    assert exc.value.status_code == 502


@respx.mock
async def test_the_token_never_appears_in_an_exception() -> None:
    """Exception text reaches logs and MCP tool errors; a credential must not travel with it."""
    respx.get(f"{BASE_URL}/alarms").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token", "message": "no"})
    )
    async with _client(max_retries=0) as client:
        with pytest.raises(AlarmApiError) as exc:
            await client.get_alarms()
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(exc.value)


@respx.mock
async def test_connection_failure_becomes_unavailable_not_a_raw_httpx_error() -> None:
    """The MCP layer maps domain exceptions; a leaked httpx type would fall through it."""
    respx.get(f"{BASE_URL}/alarms").mock(side_effect=httpx.ConnectError("refused"))
    async with _client(max_retries=0) as client:
        with pytest.raises(AlarmApiUnavailableError) as exc:
            await client.get_alarms()
    assert BASE_URL in str(exc.value)


@respx.mock
async def test_timeout_becomes_unavailable() -> None:
    respx.get(f"{BASE_URL}/alarms").mock(side_effect=httpx.ReadTimeout("too slow"))
    async with _client(max_retries=0) as client:
        with pytest.raises(AlarmApiUnavailableError) as exc:
            await client.get_alarms()
    assert "timeout" in str(exc.value).lower()


# --- retry policy -----------------------------------------------------------------------


@respx.mock
async def test_a_transient_503_is_retried_and_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/alarms").mock(
        side_effect=[
            httpx.Response(503, json={"error": "unavailable", "message": "restarting"}),
            httpx.Response(200, json=ALARM_PAGE),
        ]
    )
    async with _client(max_retries=2) as client:
        page = await client.get_alarms()
    assert page.pagination.total == 1
    assert route.call_count == 2


@respx.mock
async def test_a_connection_error_is_retried() -> None:
    route = respx.get(f"{BASE_URL}/alarms").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=ALARM_PAGE)]
    )
    async with _client(max_retries=2) as client:
        await client.get_alarms()
    assert route.call_count == 2


@respx.mock
async def test_retries_are_bounded_and_then_the_error_surfaces() -> None:
    """Surfacing the failure beats retrying forever: the copilot must be able to say so."""
    route = respx.get(f"{BASE_URL}/alarms").mock(
        return_value=httpx.Response(503, json={"error": "unavailable", "message": "down"})
    )
    async with _client(max_retries=2) as client:
        with pytest.raises(AlarmApiUnavailableError):
            await client.get_alarms()
    assert route.call_count == 3  # the first attempt plus two retries


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_not_retried(status: int) -> None:
    """A rejected credential or a bad argument fails identically every time.

    Retrying only delays the message the caller needs, and repeating a rejected token is how
    a service gets itself locked out of an upstream.
    """
    route = respx.get(f"{BASE_URL}/alarms").mock(
        return_value=httpx.Response(status, json={"error": "e", "message": "m"})
    )
    async with _client(max_retries=3) as client:
        with pytest.raises(AlarmApiError):
            await client.get_alarms()
    assert route.call_count == 1


@respx.mock
async def test_rate_limit_retry_honours_retry_after() -> None:
    route = respx.get(f"{BASE_URL}/alarms").mock(
        side_effect=[
            httpx.Response(
                429, headers={"retry-after": "0"}, json={"error": "rate_limited", "message": "slow"}
            ),
            httpx.Response(200, json=ALARM_PAGE),
        ]
    )
    async with _client(max_retries=1) as client:
        await client.get_alarms()
    assert route.call_count == 2


@respx.mock
async def test_max_retries_zero_means_a_single_attempt() -> None:
    route = respx.get(f"{BASE_URL}/alarms").mock(
        return_value=httpx.Response(503, json={"error": "e", "message": "m"})
    )
    async with _client(max_retries=0) as client:
        with pytest.raises(AlarmApiUnavailableError):
            await client.get_alarms()
    assert route.call_count == 1


# --- response validation ----------------------------------------------------------------


@respx.mock
async def test_unknown_response_fields_are_ignored() -> None:
    """The API must be able to add a field without breaking every deployed client."""
    page = {**ALARM_PAGE, "brand_new_field": 42}
    page["data"] = [{**ALARM_ROW, "another_new_field": "x"}]
    respx.get(f"{BASE_URL}/alarms").mock(return_value=httpx.Response(200, json=page))
    async with _client() as client:
        result = await client.get_alarms()
    assert result.data[0].alarm_id == ALARM_ROW["alarm_id"]


@respx.mock
async def test_a_200_with_the_wrong_shape_is_a_protocol_error_not_an_empty_result() -> None:
    """The dangerous failure: "no alarms" when the truth is "the contract changed"."""
    respx.get(f"{BASE_URL}/alarms").mock(
        return_value=httpx.Response(200, json={"rows": [], "meta": {}})
    )
    async with _client() as client:
        with pytest.raises(AlarmApiProtocolError) as exc:
            await client.get_alarms()
    assert "AlarmPage" in exc.value.message


@respx.mock
async def test_a_success_status_with_a_non_json_body_is_a_protocol_error() -> None:
    respx.get(f"{BASE_URL}/health").mock(return_value=httpx.Response(200, text="OK"))
    async with _client() as client:
        with pytest.raises(AlarmApiProtocolError):
            await client.health()


@respx.mock
async def test_alarm_detail_unwraps_the_alarm_object() -> None:
    respx.get(f"{BASE_URL}/alarms/ALM-1").mock(
        return_value=httpx.Response(
            200, json={"alarm": ALARM_ROW, "asset": {}, "recent_occurrences": 3}
        )
    )
    async with _client() as client:
        alarm = await client.alarm_detail("ALM-1")
    assert alarm.alarm_name == "Bearing Vibration High"


@respx.mock
async def test_alarm_detail_without_an_alarm_key_is_a_protocol_error() -> None:
    respx.get(f"{BASE_URL}/alarms/ALM-1").mock(return_value=httpx.Response(200, json={"row": {}}))
    async with _client() as client:
        with pytest.raises(AlarmApiProtocolError):
            await client.alarm_detail("ALM-1")


# --- observability ----------------------------------------------------------------------


@respx.mock
async def test_the_observer_sees_one_record_per_attempt() -> None:
    """The copilot's trace shows retry counts, so each attempt has to be reported."""
    respx.get(f"{BASE_URL}/alarms").mock(
        side_effect=[
            httpx.Response(503, json={"error": "e", "message": "m"}),
            httpx.Response(200, json=ALARM_PAGE),
        ]
    )
    seen: list[CallRecord] = []
    async with _client(max_retries=2, observer=seen.append) as client:
        await client.get_alarms()

    assert [r.attempt for r in seen] == [1, 2]
    assert [r.status_code for r in seen] == [503, 200]
    assert all(r.method == "GET" and r.path == "/alarms" for r in seen)
    assert all(r.duration_ms >= 0 for r in seen)


@respx.mock
async def test_the_observer_sees_transport_failures_too() -> None:
    respx.get(f"{BASE_URL}/alarms").mock(side_effect=httpx.ConnectError("refused"))
    seen: list[CallRecord] = []
    async with _client(max_retries=0, observer=seen.append) as client:
        with pytest.raises(AlarmApiUnavailableError):
            await client.get_alarms()
    assert len(seen) == 1
    assert seen[0].status_code is None
    assert seen[0].error is not None and "transport" in seen[0].error


@respx.mock
async def test_observer_records_carry_no_credential_and_no_body() -> None:
    """Records go into the trace the GUI renders, which is not a place for either."""
    respx.get(f"{BASE_URL}/alarms").mock(return_value=httpx.Response(200, json=ALARM_PAGE))
    seen: list[CallRecord] = []
    async with _client(observer=seen.append) as client:
        await client.get_alarms()
    rendered = repr(seen[0])
    assert TOKEN not in rendered
    assert "Bearing Vibration High" not in rendered


@respx.mock
async def test_the_returned_trace_id_is_recorded() -> None:
    respx.get(f"{BASE_URL}/alarms").mock(
        return_value=httpx.Response(200, json=ALARM_PAGE, headers={TRACE_ID_HEADER: "trc-echo"})
    )
    seen: list[CallRecord] = []
    async with _client(observer=seen.append) as client:
        await client.get_alarms(trace=TraceContext(trace_id="trc-echo"))
    assert seen[0].trace_id == "trc-echo"
