"""HTTP-level tests for the simulator.

Scope: the things only a real request can exercise — authentication, the trace-header
round trip, the wrapper keys the Postman collections pin, and the uniform error envelope
the connector and the MCP error mapping both parse. Query semantics and analytics are
covered at the store level, so they are not re-tested through the client.

The final group walks the actual chaining flow (resolve asset -> list alarms -> request
recommendations), because the copilot's whole value depends on the id from one response
being a valid input to the next.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FROZEN_NOW, TEST_TOKEN

PROTECTED_ENDPOINTS = [
    ("GET", "/assets/search?query=pump"),
    ("GET", "/assets/AST-PMP-0001/metadata"),
    ("GET", "/alarms"),
    ("POST", "/alarms/summary"),
    ("POST", "/alarms/recurring"),
    ("POST", "/recommendations/operator-actions"),
    ("GET", "/analytics/kpi-definitions"),
]


# --- health and auth --------------------------------------------------------------------


def test_health_needs_no_credentials(client: TestClient) -> None:
    """The collection calls /health with no Authorization header, and probes must not need one."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dataset"]["alarms"] > 0
    assert body["dataset"]["assets"] > 0


def test_health_reports_the_dataset_fingerprint(client: TestClient) -> None:
    """Seed and horizon in the body make "wrong data" diagnosable from one unauthenticated call."""
    dataset = client.get("/health").json()["dataset"]
    assert dataset["seed"] == 1729
    assert dataset["days_of_history"] == 400


@pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
def test_every_other_endpoint_requires_a_token(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, json={})
    assert response.status_code == 401, path


def test_missing_token_returns_the_standard_error_envelope(client: TestClient) -> None:
    response = client.get("/alarms")
    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "missing_credentials"
    assert "message" in body
    assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_rejected(client: TestClient) -> None:
    response = client.get("/alarms", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


@pytest.mark.parametrize(
    "header", ["token-without-scheme", "Basic dXNlcjpwYXNz", "Bearer", "Bearer "]
)
def test_malformed_authorization_headers_are_rejected(client: TestClient, header: str) -> None:
    response = client.get("/alarms", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json()["error"] in {"invalid_authorization_header", "invalid_token"}


def test_error_responses_never_echo_the_supplied_token(client: TestClient) -> None:
    """Reflecting a credential into a response body is how credentials reach logs."""
    secret = "super-secret-token-value"
    response = client.get("/alarms", headers={"Authorization": f"Bearer {secret}"})
    assert secret not in response.text


def test_a_valid_token_is_accepted(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.get("/alarms", headers=auth_headers).status_code == 200
    assert auth_headers["Authorization"] == f"Bearer {TEST_TOKEN}"


# --- trace propagation ------------------------------------------------------------------


def test_trace_headers_are_echoed_on_the_response(
    client: TestClient, trace_headers: dict[str, str]
) -> None:
    response = client.post("/alarms/summary", headers=trace_headers, json={})
    assert response.status_code == 200
    assert response.headers["trace_id"] == "trc-test-0001"
    assert response.headers["x-client-id"] == "alarm-copilot-tests"
    assert response.headers["x-metadata-tag"] == "acceptance"


def test_trace_identifiers_also_appear_in_the_body(
    client: TestClient, trace_headers: dict[str, str]
) -> None:
    """The copilot's trace panel joins on trace_id, including for replayed responses."""
    body = client.post("/alarms/summary", headers=trace_headers, json={}).json()
    assert body["trace"] == {
        "trace_id": "trc-test-0001",
        "client_id": "alarm-copilot-tests",
        "metadata_tag": "acceptance",
    }


def test_a_missing_trace_id_is_generated_and_flagged(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Generating one keeps requests traceable; the flag distinguishes it from a forwarded id."""
    response = client.post("/alarms/summary", headers=auth_headers, json={})
    assert response.headers["trace_id"].startswith("trc-")
    assert response.headers["x-trace-id-generated"] == "true"


def test_a_supplied_trace_id_is_not_flagged_as_generated(
    client: TestClient, trace_headers: dict[str, str]
) -> None:
    response = client.post("/alarms/summary", headers=trace_headers, json={})
    assert "x-trace-id-generated" not in response.headers


def test_every_response_carries_a_request_id(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    first = client.get("/alarms", headers=auth_headers).headers["x-request-id"]
    second = client.get("/alarms", headers=auth_headers).headers["x-request-id"]
    assert first != second, "request ids must identify a single call, not the conversation"


def test_error_responses_still_carry_the_trace_id(
    client: TestClient, trace_headers: dict[str, str]
) -> None:
    """A failed call is exactly when the trace id matters most."""
    response = client.get("/assets/NO-SUCH-ASSET/metadata", headers=trace_headers)
    assert response.status_code == 404
    assert response.json()["trace_id"] == "trc-test-0001"
    assert response.headers["trace_id"] == "trc-test-0001"


# --- pinned response shapes -------------------------------------------------------------


def test_asset_search_uses_the_results_wrapper(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Pinned by the collections: `results[0].asset_id`."""
    response = client.get(
        "/assets/search", params={"query": "Boiler Feed Pump 101"}, headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert "results" in body
    assert body["results"][0]["asset_id"]
    assert body["results"][0]["name"] == "Boiler Feed Pump 101"


def test_alarm_list_uses_the_data_wrapper(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Pinned by the collections: `data[0].alarm_id`."""
    response = client.get("/alarms", params={"page_size": 5}, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert "data" in body
    assert len(body["data"]) == 5
    assert body["data"][0]["alarm_id"]


def test_alarm_list_reports_pagination(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = client.get("/alarms", params={"page_size": 5}, headers=auth_headers).json()
    pagination = body["pagination"]
    assert pagination["page"] == 1
    assert pagination["page_size"] == 5
    assert pagination["total"] > 5
    assert pagination["has_next"] is True


def test_alarm_list_echoes_the_filters_it_applied(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """So a caller can tell a silently-ignored parameter from an honestly empty result."""
    body = client.get(
        "/alarms",
        params={"site": "EastRefinery", "status": "active"},
        headers=auth_headers,
    ).json()
    assert body["filters_applied"]["site"] == "EastRefinery"
    assert body["filters_applied"]["status"] == ["active"]


def test_asset_search_supports_a_unit_filter(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """CHAIN-08 sends `?query=motor&unit=Unit 5`."""
    body = client.get(
        "/assets/search", params={"query": "motor", "unit": "Unit 5"}, headers=auth_headers
    ).json()
    assert len(body["results"]) >= 3
    assert all(r["unit"] == "Unit 5" for r in body["results"])


def test_repeated_query_parameters_are_accepted(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = client.get(
        "/alarms",
        params=[("severity", "high"), ("severity", "critical")],
        headers=auth_headers,
    ).json()
    assert body["filters_applied"]["severity"] == ["high", "critical"]
    assert {row["severity"] for row in body["data"]} <= {"high", "critical"}


# --- error envelope ---------------------------------------------------------------------


def test_unknown_asset_returns_a_typed_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/assets/AST-NOPE-0000/metadata", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    # Flat envelope, not FastAPI's default nesting under "detail".
    assert body["error"] == "asset_not_found"
    assert "AST-NOPE-0000" in body["message"]


def test_unknown_alarm_returns_a_typed_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = client.get("/alarms/ALM-19700101-000001", headers=auth_headers).json()
    assert body["error"] == "alarm_not_found"


def test_an_invalid_kpi_is_rejected_with_the_valid_options(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """The message is what lets a tool-calling model fix its own call and retry."""
    response = client.post("/alarms/summary", headers=auth_headers, json={"kpis": ["invented_kpi"]})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid_parameter"
    assert "alarm_count" in body["message"]


def test_an_invalid_sort_field_is_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/alarms", params={"sort_by": "nope"}, headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_parameter"


def test_schema_violations_report_the_offending_field(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/recommendations/operator-actions",
        headers=auth_headers,
        json={"asset_id": "AST-PMP-0001", "lookback_days": 9999},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert any("lookback_days" in entry["loc"] for entry in body["detail"])


def test_recommendations_require_an_alarm_or_an_asset(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post("/recommendations/operator-actions", headers=auth_headers, json={})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_an_inverted_time_range_is_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/alarms/summary",
        headers=auth_headers,
        json={
            "time_range": {
                "start": FROZEN_NOW.isoformat(),
                "end": (FROZEN_NOW - timedelta(days=30)).isoformat(),
            }
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(("start_key", "end_key"), [("start_time", "end_time"), ("start", "end")])
def test_a_time_range_is_accepted_under_either_spelling(
    client: TestClient, auth_headers: dict[str, str], bfp101_id: str, start_key: str, end_key: str
) -> None:
    """`start_time`/`end_time` is what the collections send; `start`/`end` is what our MCP
    tools send. Rejecting the first returned a 422 on the assignment's own summary request —
    see `TimeRange` in `apps/alarm_api/schemas.py` and `test_postman_contract.py`."""
    response = client.post(
        "/alarms/summary",
        headers=auth_headers,
        json={
            "asset_ids": [bfp101_id],
            "time_range": {
                start_key: (FROZEN_NOW - timedelta(days=90)).isoformat(),
                end_key: FROZEN_NOW.isoformat(),
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["total_alarms"] > 0


def test_a_time_range_must_still_carry_both_bounds(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # The alias widens what is accepted; it must not make the window optional.
    response = client.post(
        "/alarms/summary",
        headers=auth_headers,
        json={"time_range": {"start_time": FROZEN_NOW.isoformat()}},
    )
    assert response.status_code == 422
    assert any("end" in str(entry["loc"]) for entry in response.json()["detail"])


# --- aggregation endpoints --------------------------------------------------------------


def test_summary_groups_by_the_requested_dimensions(
    client: TestClient, auth_headers: dict[str, str], bfp101_id: str
) -> None:
    response = client.post(
        "/alarms/summary",
        headers=auth_headers,
        json={
            "asset_ids": [bfp101_id],
            "time_range": {
                "start": (FROZEN_NOW - timedelta(days=90)).isoformat(),
                "end": FROZEN_NOW.isoformat(),
            },
            "group_by": ["alarm_name"],
            "kpis": ["alarm_count", "avg_ack_delay", "recurring_rate"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["group_by"] == ["alarm_name"]
    assert body["groups"]
    assert sum(g["alarm_count"] for g in body["groups"]) == body["total_alarms"]
    assert set(body["groups"][0]["kpis"]) == {"alarm_count", "avg_ack_delay", "recurring_rate"}


def test_recurring_endpoint_surfaces_the_planted_pattern(
    client: TestClient, auth_headers: dict[str, str], bfp101_id: str
) -> None:
    response = client.post(
        "/alarms/recurring",
        headers=auth_headers,
        json={
            "asset_ids": [bfp101_id],
            "time_range": {
                "start": (FROZEN_NOW - timedelta(days=90)).isoformat(),
                "end": FROZEN_NOW.isoformat(),
            },
            "severity_threshold": "high",
        },
    )
    assert response.status_code == 200
    groups = response.json()["groups"]
    vibration = next(g for g in groups if g["alarm_name"] == "Bearing Vibration High")
    assert vibration["occurrences"] >= 20
    assert vibration["trend"] == "increasing"


def test_every_documented_kpi_is_actually_accepted(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Guards against the KPI catalogue drifting from what the aggregation implements.

    `/analytics/kpi-definitions` is what the copilot reads to decide which KPIs to ask for.
    If it advertises one that `/alarms/summary` rejects, the copilot's call fails for a
    reason it cannot diagnose.
    """
    advertised = [
        k["name"]
        for k in client.get("/analytics/kpi-definitions", headers=auth_headers).json()["kpis"]
    ]
    assert advertised
    response = client.post("/alarms/summary", headers=auth_headers, json={"kpis": advertised})
    assert response.status_code == 200
    assert set(response.json()["overall"]) == set(advertised)


def test_every_documented_group_by_dimension_is_accepted(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dimensions = client.get("/analytics/kpi-definitions", headers=auth_headers).json()[
        "group_by_dimensions"
    ]
    for dimension in dimensions:
        response = client.post(
            "/alarms/summary",
            headers=auth_headers,
            json={"unit": "Unit 2", "group_by": [dimension]},
        )
        assert response.status_code == 200, dimension


def test_every_documented_sort_field_is_accepted(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    fields = client.get("/analytics/kpi-definitions", headers=auth_headers).json()["sort_fields"]
    for field in fields:
        response = client.get(
            "/alarms", params={"sort_by": field, "page_size": 1}, headers=auth_headers
        )
        assert response.status_code == 200, field


# --- the chaining flow ------------------------------------------------------------------


def test_asset_search_id_works_as_an_alarm_filter(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """`results[0].asset_id` -> `?asset_id=` must actually resolve.

    The collections chain exactly this way, and it is the first hop of the acceptance
    scenario. An id that does not round-trip breaks every downstream step.
    """
    asset_id = client.get(
        "/assets/search", params={"query": "Boiler Feed Pump 101"}, headers=auth_headers
    ).json()["results"][0]["asset_id"]

    body = client.get("/alarms", params={"asset_id": asset_id}, headers=auth_headers).json()
    assert body["data"]
    assert {row["asset_id"] for row in body["data"]} == {asset_id}


def test_alarm_id_from_the_list_works_for_recommendations(
    client: TestClient, trace_headers: dict[str, str]
) -> None:
    """`data[0].alarm_id` -> operator-actions, the collections' second chain hop."""
    alarm_id = client.get("/alarms", params={"page_size": 1}, headers=trace_headers).json()["data"][
        0
    ]["alarm_id"]

    response = client.post(
        "/recommendations/operator-actions",
        headers=trace_headers,
        json={"alarm_id": alarm_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["alarm_id"] == alarm_id
    assert body["recommendations"]
    assert body["trace"]["trace_id"] == "trc-test-0001"


def test_the_full_acceptance_chain_resolves_to_recommendations(
    client: TestClient, trace_headers: dict[str, str]
) -> None:
    """Resolve by name -> read metadata -> summarise 90 days -> get recommendations.

    The HTTP-level rehearsal of the mandatory scenario. It asserts the *joins* hold and
    that the API names a procedure document; retrieving that document and composing the
    final answer is the copilot's job, covered by the e2e test.
    """
    search = client.get(
        "/assets/search", params={"query": "Boiler Feed Pump 101"}, headers=trace_headers
    ).json()
    asset_id = search["results"][0]["asset_id"]

    metadata = client.get(f"/assets/{asset_id}/metadata", headers=trace_headers).json()
    assert metadata["asset"]["tag"] == "2-BFP-101"
    assert metadata["related_assets"], "the scenario's second contributing factor needs these"

    window = {
        "start": (FROZEN_NOW - timedelta(days=90)).isoformat(),
        "end": FROZEN_NOW.isoformat(),
    }
    summary = client.post(
        "/alarms/summary",
        headers=trace_headers,
        json={
            "asset_ids": [asset_id],
            "time_range": window,
            "severity": ["high", "critical"],
            "group_by": ["alarm_name"],
            "kpis": ["alarm_count", "avg_ack_delay"],
        },
    ).json()
    assert summary["total_alarms"] > 0

    recommendations = client.post(
        "/recommendations/operator-actions",
        headers=trace_headers,
        json={"asset_id": asset_id, "lookback_days": 90},
    ).json()
    assert recommendations["asset_name"] == "Boiler Feed Pump 101"
    assert recommendations["recommendations"]
    # The API points at a procedure; only RAG can say what it contains.
    assert recommendations["procedure_references"]


# --- operability ------------------------------------------------------------------------


def test_openapi_schema_is_generated(client: TestClient) -> None:
    """The MCP tool catalogue and the docs are both derived from this."""
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Alarm Management API (simulator)"
    assert "/assets/search" in schema["paths"]
    assert "/recommendations/operator-actions" in schema["paths"]
