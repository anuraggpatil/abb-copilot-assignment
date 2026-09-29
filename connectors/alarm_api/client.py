"""Async HTTP client for the Alarm Management API.

Responsibilities, and nothing beyond them: authentication, trace-header propagation,
timeouts, retry of the failures that are worth retrying, mapping HTTP outcomes onto typed
exceptions, and validating responses into typed models.

It deliberately does **not** read the environment. A library that configures itself from
`os.environ` cannot be instantiated twice with different settings and forces every test to
mutate global state; the caller passes a base URL and a token. `mcp-servers/` and
`apps/backend/` own their own configuration and construct this client from it.

Retry policy: 5xx, 429 and transport failures are retried with exponential backoff and
jitter; 4xx are not, because a rejected credential or a malformed argument fails identically
on every attempt and retrying only delays the error the caller needs to see. Every endpoint
this connector exposes is a read — `POST /alarms/summary` is a query with a body — so retry
is safe without idempotency keys. That is a property of this API, not a general rule, and it
is why the retry set is defined here rather than left to the caller.

Observability: the caller may pass an `observer` callback, invoked once per HTTP attempt
with a `CallRecord`. The copilot's trace needs the status code and retry count of each
upstream call, and a callback keeps that requirement out of every method signature while
staying safe under concurrency — the MCP layer's observer writes to a contextvar scoped to
one tool call.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel, ValidationError
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from connectors.alarm_api.errors import (
    AlarmApiError,
    AlarmApiProtocolError,
    AlarmApiRateLimitError,
    AlarmApiUnavailableError,
    error_for_status,
)
from connectors.alarm_api.models import (
    Alarm,
    AlarmPage,
    AlarmSummary,
    AssetMetadata,
    AssetSearchResult,
    Health,
    KpiCatalogue,
    OperatorActions,
    RecurringAlarms,
)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 2

#: Exact spellings the API propagates and echoes. `trace_id` is snake_case and the other two
#: are `x-` prefixed; that asymmetry comes from the assignment's Postman collections, so it
#: is reproduced rather than tidied.
TRACE_ID_HEADER = "trace_id"
CLIENT_ID_HEADER = "x-client-id"
METADATA_TAG_HEADER = "x-metadata-tag"


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Identifiers that travel with a request so one investigation can be reconstructed."""

    trace_id: str | None = None
    client_id: str | None = None
    metadata_tag: str | None = None

    def headers(self) -> dict[str, str]:
        out = {}
        if self.trace_id:
            out[TRACE_ID_HEADER] = self.trace_id
        if self.client_id:
            out[CLIENT_ID_HEADER] = self.client_id
        if self.metadata_tag:
            out[METADATA_TAG_HEADER] = self.metadata_tag
        return out


@dataclass(frozen=True, slots=True)
class CallRecord:
    """What happened on one HTTP attempt. Never contains the token or a response body."""

    method: str
    path: str
    attempt: int
    status_code: int | None
    duration_ms: float
    error: str | None = None
    trace_id: str | None = None


Observer = Callable[[CallRecord], None]


def _retry_wait(state: RetryCallState) -> float:
    """Exponential backoff with jitter, but honour `Retry-After` when the API sends one.

    A server that has told us when to come back knows better than our backoff curve, and
    ignoring it is how a client turns a rate limit into a ban.
    """
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, AlarmApiRateLimitError) and exc.retry_after is not None:
        return min(exc.retry_after, 30.0)
    # 0.25, 0.5, 1.0, ... capped, plus jitter derived from the attempt so tests stay fast.
    base = min(0.25 * (2 ** (state.attempt_number - 1)), 4.0)
    return base


class AlarmApiClient:
    """Typed async client for the six endpoints the copilot needs, plus health and KPIs."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client_id: str | None = None,
        observer: Observer | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`max_retries` counts *additional* attempts after the first, so 0 means one try."""
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._max_retries = max(0, max_retries)
        self._client_id = client_id
        self._observer = observer
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            # The token is set per request rather than as a default header so that the
            # unauthenticated /health call cannot accidentally carry a credential.
            headers={"accept": "application/json"},
        )

    # --- lifecycle ----------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- endpoints ----------------------------------------------------------------------

    async def health(self) -> Health:
        """Liveness plus the dataset fingerprint. Sent without credentials, as the API allows."""
        payload = await self._request("GET", "/health", authenticated=False)
        return self._parse(Health, payload, "/health")

    async def search_assets(
        self,
        query: str,
        *,
        limit: int = 10,
        unit: str | None = None,
        site: str | None = None,
        trace: TraceContext | None = None,
    ) -> AssetSearchResult:
        params = self._params(query=query, limit=limit, unit=unit, site=site)
        payload = await self._request("GET", "/assets/search", params=params, trace=trace)
        return self._parse(AssetSearchResult, payload, "/assets/search")

    async def asset_metadata(
        self, asset_id: str, *, trace: TraceContext | None = None
    ) -> AssetMetadata:
        payload = await self._request("GET", f"/assets/{asset_id}/metadata", trace=trace)
        return self._parse(AssetMetadata, payload, "/assets/{asset_id}/metadata")

    async def get_alarms(
        self,
        *,
        asset_ids: Sequence[str] | None = None,
        unit: str | None = None,
        site: str | None = None,
        status: Sequence[str] | None = None,
        severity: Sequence[str] | None = None,
        alarm_type: Sequence[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "start_time",
        sort_order: str = "desc",
        trace: TraceContext | None = None,
    ) -> AlarmPage:
        params = self._params(
            asset_id=list(asset_ids) if asset_ids else None,
            unit=unit,
            site=site,
            status=list(status) if status else None,
            severity=list(severity) if severity else None,
            alarm_type=list(alarm_type) if alarm_type else None,
            start_time=start_time,
            end_time=end_time,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        payload = await self._request("GET", "/alarms", params=params, trace=trace)
        return self._parse(AlarmPage, payload, "/alarms")

    async def alarm_detail(self, alarm_id: str, *, trace: TraceContext | None = None) -> Alarm:
        payload = await self._request("GET", f"/alarms/{alarm_id}", trace=trace)
        if not isinstance(payload, dict) or "alarm" not in payload:
            raise AlarmApiProtocolError(
                "GET /alarms/{alarm_id} did not return an 'alarm' object.",
                detail=_shape_of(payload),
            )
        return self._parse(Alarm, payload["alarm"], "/alarms/{alarm_id}")

    async def summarize_alarms(
        self, body: dict[str, Any], *, trace: TraceContext | None = None
    ) -> AlarmSummary:
        payload = await self._request("POST", "/alarms/summary", json=body, trace=trace)
        return self._parse(AlarmSummary, payload, "/alarms/summary")

    async def recurring_alarms(
        self, body: dict[str, Any], *, trace: TraceContext | None = None
    ) -> RecurringAlarms:
        payload = await self._request("POST", "/alarms/recurring", json=body, trace=trace)
        return self._parse(RecurringAlarms, payload, "/alarms/recurring")

    async def operator_actions(
        self, body: dict[str, Any], *, trace: TraceContext | None = None
    ) -> OperatorActions:
        payload = await self._request(
            "POST", "/recommendations/operator-actions", json=body, trace=trace
        )
        return self._parse(OperatorActions, payload, "/recommendations/operator-actions")

    async def kpi_definitions(self, *, trace: TraceContext | None = None) -> KpiCatalogue:
        payload = await self._request("GET", "/analytics/kpi-definitions", trace=trace)
        return self._parse(KpiCatalogue, payload, "/analytics/kpi-definitions")

    # --- plumbing -----------------------------------------------------------------------

    @staticmethod
    def _params(**kwargs: Any) -> httpx.QueryParams:
        """Flatten query parameters, dropping `None` and expanding lists into repeats.

        `httpx` can do most of this, but sequence parameters need to repeat the key
        (`?severity=high&severity=critical`) and `None` must vanish rather than become the
        string "None" — which the API would then reject as an invalid enum value. Everything
        is stringified here so the result is a single, already-resolved type.
        """
        pairs: list[tuple[str, str]] = []
        for key, value in kwargs.items():
            if value is None:
                continue
            items = value if isinstance(value, list | tuple) else [value]
            for item in items:
                if item is None:
                    continue
                pairs.append((key, item.isoformat() if isinstance(item, datetime) else str(item)))
        # Passed as a tuple rather than the list: httpx accepts a list of tuples whose values
        # are a union type, and `list[tuple[str, str]]` is not assignable to that because
        # lists are invariant. Tuples are covariant, so this needs no cast.
        return httpx.QueryParams(tuple(pairs))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: httpx.QueryParams | None = None,
        json: dict[str, Any] | None = None,
        trace: TraceContext | None = None,
        authenticated: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        effective = trace or TraceContext(client_id=self._client_id)
        if effective.client_id is None and self._client_id:
            effective = TraceContext(
                trace_id=effective.trace_id,
                client_id=self._client_id,
                metadata_tag=effective.metadata_tag,
            )
        headers.update(effective.headers())

        # `reraise=True`: the caller gets the domain exception from the final attempt, not
        # tenacity's RetryError wrapper, which would hide the reason from the MCP mapping.
        retryer = AsyncRetrying(
            stop=stop_after_attempt(1 + self._max_retries),
            wait=_retry_wait,
            retry=retry_if_exception_type((AlarmApiUnavailableError, AlarmApiRateLimitError)),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                return await self._attempt(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                    attempt_number=attempt.retry_state.attempt_number,
                )
        raise AssertionError("unreachable: AsyncRetrying always yields or raises")

    async def _attempt(
        self,
        method: str,
        path: str,
        *,
        params: httpx.QueryParams | None,
        json: dict[str, Any] | None,
        headers: dict[str, str],
        attempt_number: int,
    ) -> Any:
        started = time.perf_counter()
        try:
            response = await self._http.request(
                method, path, params=params, json=json, headers=headers
            )
        except httpx.TimeoutException as exc:
            self._record(method, path, attempt_number, None, started, f"timeout: {exc!s}")
            raise AlarmApiUnavailableError(
                f"The Alarm API did not respond within the timeout for {method} {path}."
            ) from exc
        except httpx.HTTPError as exc:
            self._record(method, path, attempt_number, None, started, f"transport: {exc!s}")
            raise AlarmApiUnavailableError(
                f"Could not reach the Alarm API at {self._base_url}: {exc!s}"
            ) from exc

        trace_id = response.headers.get(TRACE_ID_HEADER)
        self._record(method, path, attempt_number, response.status_code, started, None, trace_id)

        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:
                raise AlarmApiProtocolError(
                    f"{method} {path} returned a success status with a non-JSON body.",
                    status_code=response.status_code,
                    trace_id=trace_id,
                ) from exc

        raise self._error_from(response, method, path, trace_id)

    def _error_from(
        self, response: httpx.Response, method: str, path: str, trace_id: str | None
    ) -> AlarmApiError:
        """Turn a failure response into the matching domain exception.

        The API's `message` is passed through verbatim: it names the offending parameter and
        lists the valid options, and that text is what lets a tool-calling model correct its
        own call. Rewriting it here would throw away the only actionable part.
        """
        error_code: str | None = None
        detail: Any = None
        message = f"{method} {path} failed with HTTP {response.status_code}."
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            error_code = body.get("error")
            detail = body.get("detail")
            if isinstance(body.get("message"), str):
                message = body["message"]
            trace_id = body.get("trace_id") or trace_id

        retry_after: float | None = None
        raw_retry_after = response.headers.get("retry-after")
        if raw_retry_after:
            try:
                retry_after = float(raw_retry_after)
            except ValueError:
                # A date-formatted Retry-After is legal but rare; falling back to the
                # backoff curve is better than failing to parse it.
                retry_after = None

        return error_for_status(
            response.status_code,
            message=message,
            error_code=error_code,
            trace_id=trace_id,
            detail=detail,
            retry_after=retry_after,
        )

    def _record(
        self,
        method: str,
        path: str,
        attempt: int,
        status_code: int | None,
        started: float,
        error: str | None,
        trace_id: str | None = None,
    ) -> None:
        if self._observer is None:
            return
        self._observer(
            CallRecord(
                method=method,
                path=path,
                attempt=attempt,
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error=error,
                trace_id=trace_id,
            )
        )

    @staticmethod
    def _parse[T: BaseModel](model: type[T], payload: Any, path: str) -> T:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            # A 200 that does not match the contract is not an empty result, and must not be
            # allowed to look like one downstream.
            raise AlarmApiProtocolError(
                f"{path} returned a body this connector cannot parse as {model.__name__}.",
                detail=exc.errors(include_url=False),
            ) from exc


def _shape_of(payload: Any) -> Any:
    """Keys only — enough to diagnose a contract break without logging plant data."""
    if isinstance(payload, dict):
        return sorted(payload)
    return type(payload).__name__
