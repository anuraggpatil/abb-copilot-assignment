"""The connector against the real simulator, over ASGI, with no network.

`tests/unit/test_alarm_api_client.py` proves the connector *behaves* correctly — retries,
error classes, header spellings — against mocked responses. Mocks, though, only ever confirm
that the connector agrees with the author's beliefs about the wire format. These tests close
that loop: the same client code drives the actual FastAPI app, so a field the server renames
or a query parameter it spells differently fails here instead of in the demo.

`httpx.ASGITransport` means the requests are real httpx requests — params flattened,
headers set, statuses parsed — routed in-process. That keeps the connector's own plumbing
under test rather than bypassed, which a `TestClient` shortcut would not.

The acceptance chain at the bottom is the connector-level rehearsal of the assignment's
mandatory scenario, ending at the `procedure_references` the RAG step consumes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from connectors.alarm_api import (
    AlarmApiAuthError,
    AlarmApiClient,
    AlarmApiNotFoundError,
    AlarmApiValidationError,
    CallRecord,
    TraceContext,
)
from tests.conftest import FROZEN_NOW, TEST_TOKEN

BFP101 = "Boiler Feed Pump 101"

# ASGITransport never resolves this, but httpx requires an absolute base URL.
ASGI_BASE_URL = "http://alarm-api.internal"


@pytest.fixture
def records() -> list[CallRecord]:
    return []


@pytest.fixture
async def connector(client: TestClient, records: list[CallRecord]) -> AsyncIterator[AlarmApiClient]:
    """A connector wired to the session's already-seeded app.

    Reusing that app rather than building another keeps the 400-day dataset generation to
    once per suite, and means these tests see the same frozen `now` the rest of the
    integration suite asserts against.
    """
    async with AlarmApiClient(
        ASGI_BASE_URL,
        TEST_TOKEN,
        transport=httpx.ASGITransport(app=client.app),
        # No retries: every failure these tests provoke is a deliberate 4xx, and a retry
        # budget would only slow the suite down.
        max_retries=0,
        client_id="connector-contract-tests",
        observer=records.append,
    ) as connector:
        yield connector


@pytest.fixture
async def trace() -> TraceContext:
    return TraceContext(trace_id="trc-connector-0001", metadata_tag="contract")


# --- every endpoint parses what the server actually sends --------------------------------


async def test_health_parses_and_reports_the_dataset_fingerprint(
    connector: AlarmApiClient,
) -> None:
    health = await connector.health()
    assert health.status == "ok"
    assert health.dataset["seed"] == 1729


async def test_search_assets_parses(connector: AlarmApiClient, trace: TraceContext) -> None:
    result = await connector.search_assets(BFP101, trace=trace)
    assert result.results, "the acceptance scenario's asset must be findable"
    assert result.results[0].name == BFP101
    assert result.results[0].match_score > 0


async def test_asset_metadata_parses_including_the_nested_asset(
    connector: AlarmApiClient, bfp101_id: str
) -> None:
    metadata = await connector.asset_metadata(bfp101_id)
    assert metadata.asset.asset_id == bfp101_id
    assert metadata.asset.tag == "2-BFP-101"
    assert metadata.related_assets, "BFP-101 is seeded with neighbours to correlate against"


async def test_get_alarms_parses_with_the_filters_the_copilot_will_send(
    connector: AlarmApiClient, bfp101_id: str
) -> None:
    """Repeated keys, enum values and ISO datetimes, all accepted by the real routes.

    The unit tests prove the connector *sends* `?severity=high&severity=critical`; only the
    server can prove it *accepts* that spelling.
    """
    page = await connector.get_alarms(
        asset_ids=[bfp101_id],
        severity=["high", "critical"],
        start_time=FROZEN_NOW - timedelta(days=90),
        end_time=FROZEN_NOW,
        page_size=25,
    )
    assert page.data, "the planted BFP-101 pattern must fall inside the last 90 days"
    assert all(alarm.asset_id == bfp101_id for alarm in page.data)
    assert all(alarm.severity in {"high", "critical"} for alarm in page.data)
    assert page.pagination.page_size == 25
    assert page.filters_applied["asset_ids"] == [bfp101_id]


async def test_alarm_detail_parses(connector: AlarmApiClient, bfp101_id: str) -> None:
    page = await connector.get_alarms(asset_ids=[bfp101_id], page_size=1)
    alarm = await connector.alarm_detail(page.data[0].alarm_id)
    assert alarm.alarm_id == page.data[0].alarm_id


async def test_summarize_alarms_parses_groups_and_kpis(
    connector: AlarmApiClient, bfp101_id: str, trace: TraceContext
) -> None:
    summary = await connector.summarize_alarms(
        {
            "asset_ids": [bfp101_id],
            "time_range": {
                "start": (FROZEN_NOW - timedelta(days=90)).isoformat(),
                "end": FROZEN_NOW.isoformat(),
            },
            "group_by": ["alarm_name"],
            "kpis": ["alarm_count", "avg_ack_delay"],
        },
        trace=trace,
    )
    assert summary.total_alarms > 0
    assert summary.groups
    assert sum(group.alarm_count for group in summary.groups) == summary.total_alarms
    assert "alarm_count" in summary.groups[0].kpis


async def test_recurring_alarms_parses_and_surfaces_the_planted_pattern(
    connector: AlarmApiClient, bfp101_id: str
) -> None:
    recurring = await connector.recurring_alarms(
        {
            "asset_ids": [bfp101_id],
            "time_range": {
                "start": (FROZEN_NOW - timedelta(days=90)).isoformat(),
                "end": FROZEN_NOW.isoformat(),
            },
            "severity_threshold": "high",
        }
    )
    assert recurring.groups, "the whole scenario rests on a recurring pattern existing here"
    assert any(group.trend == "increasing" for group in recurring.groups)


async def test_operator_actions_parses_and_yields_procedure_references(
    connector: AlarmApiClient, bfp101_id: str
) -> None:
    """`procedure_references` is the handoff to RAG; a parse failure here breaks the workflow."""
    actions = await connector.operator_actions({"asset_id": bfp101_id, "lookback_days": 90})
    assert actions.asset_name == BFP101
    assert actions.recommendations
    assert actions.recommendations[0].rank == 1
    assert actions.procedure_references
    assert all("§" in reference for reference in actions.procedure_references)


async def test_kpi_definitions_parses(connector: AlarmApiClient) -> None:
    catalogue = await connector.kpi_definitions()
    assert {kpi.name for kpi in catalogue.kpis} >= {"alarm_count"}
    assert catalogue.group_by_dimensions
    assert catalogue.sort_fields


async def test_every_advertised_kpi_survives_the_connectors_models(
    connector: AlarmApiClient,
) -> None:
    """A self-describing catalogue the connector cannot actually parse is worse than none."""
    catalogue = await connector.kpi_definitions()
    summary = await connector.summarize_alarms(
        {"group_by": ["site"], "kpis": [kpi.name for kpi in catalogue.kpis]}
    )
    assert summary.groups
    assert set(summary.groups[0].kpis) == {kpi.name for kpi in catalogue.kpis}


# --- the error envelope, mapped from real responses --------------------------------------


async def test_a_wrong_token_raises_an_auth_error(client: TestClient) -> None:
    async with AlarmApiClient(
        ASGI_BASE_URL,
        "not-the-token",
        transport=httpx.ASGITransport(app=client.app),
        max_retries=0,
    ) as connector:
        with pytest.raises(AlarmApiAuthError) as exc:
            await connector.search_assets("pump")
    assert exc.value.status_code == 401
    assert exc.value.error_code == "invalid_token"


async def test_an_unknown_asset_id_raises_a_not_found_error(connector: AlarmApiClient) -> None:
    with pytest.raises(AlarmApiNotFoundError) as exc:
        await connector.asset_metadata("AST-DOES-NOT-EXIST")
    assert exc.value.error_code == "asset_not_found"


async def test_an_unknown_kpi_raises_a_validation_error_listing_the_valid_names(
    connector: AlarmApiClient,
) -> None:
    """This message is the repair instruction a tool-calling model acts on."""
    with pytest.raises(AlarmApiValidationError) as exc:
        await connector.summarize_alarms({"kpis": ["mean_time_to_coffee"]})
    assert exc.value.status_code == 422
    assert "alarm_count" in exc.value.message


async def test_a_schema_violation_raises_a_validation_error_naming_the_field(
    connector: AlarmApiClient, bfp101_id: str
) -> None:
    with pytest.raises(AlarmApiValidationError) as exc:
        await connector.operator_actions({"asset_id": bfp101_id, "lookback_days": 9999})
    assert exc.value.error_code == "validation_error"
    assert any("lookback_days" in entry["loc"] for entry in exc.value.detail)


# --- tracing round trip -----------------------------------------------------------------


async def test_the_trace_id_reaches_the_server_and_comes_back_in_the_body(
    connector: AlarmApiClient, bfp101_id: str, trace: TraceContext
) -> None:
    """One investigation, one id, visible on both sides — the point of propagating it."""
    summary = await connector.summarize_alarms({"asset_ids": [bfp101_id]}, trace=trace)
    assert summary.trace.trace_id == trace.trace_id
    assert summary.trace.metadata_tag == "contract"
    # Supplied by the constructor rather than the call, and still echoed.
    assert summary.trace.client_id == "connector-contract-tests"


async def test_the_observer_captures_the_servers_trace_id(
    connector: AlarmApiClient, records: list[CallRecord], trace: TraceContext
) -> None:
    await connector.search_assets(BFP101, trace=trace)
    assert [r.status_code for r in records] == [200]
    assert records[0].trace_id == trace.trace_id
    assert records[0].path == "/assets/search"


async def test_a_call_without_a_trace_id_still_succeeds(connector: AlarmApiClient) -> None:
    """The server generates one; the connector must not require the caller to supply it."""
    assert (await connector.search_assets(BFP101)).results


# --- the acceptance scenario, at the connector layer -------------------------------------


async def test_the_full_investigation_chain_runs_through_the_connector(
    connector: AlarmApiClient, records: list[CallRecord], trace: TraceContext
) -> None:
    """Name → asset → 90-day recurrence → recommendations → procedure references.

    Each step's input comes from the previous step's *parsed* output, which is what makes
    this a chaining test rather than five independent calls. It is the same sequence the MCP
    tools will expose, so if the models drift this fails before the copilot is involved.
    """
    search = await connector.search_assets(BFP101, trace=trace)
    asset_id = search.results[0].asset_id

    metadata = await connector.asset_metadata(asset_id, trace=trace)
    assert metadata.asset.criticality >= 4, "the escalation path depends on this being critical"

    window = {
        "start": (FROZEN_NOW - timedelta(days=90)).isoformat(),
        "end": FROZEN_NOW.isoformat(),
    }
    recurring = await connector.recurring_alarms(
        {"asset_ids": [asset_id], "time_range": window, "severity_threshold": "high"}, trace=trace
    )
    top_pattern = max(recurring.groups, key=lambda group: group.occurrences)

    alarms = await connector.get_alarms(
        asset_ids=[asset_id],
        severity=["high", "critical"],
        start_time=FROZEN_NOW - timedelta(days=90),
        end_time=FROZEN_NOW,
        trace=trace,
    )
    assert top_pattern.alarm_name in {alarm.alarm_name for alarm in alarms.data}

    actions = await connector.operator_actions(
        {"alarm_id": alarms.data[0].alarm_id, "lookback_days": 90}, trace=trace
    )
    assert actions.asset_id == asset_id
    assert actions.procedure_references, "RAG has nothing to retrieve without these"

    assert [record.status_code for record in records] == [200] * 5
    assert {record.trace_id for record in records} == {trace.trace_id}
