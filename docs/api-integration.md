# Alarm Management API integration

How the source system is reached, what its contract is, and which parts of it this submission
implements. The tool-level view is in [`mcp-tool-catalog.md`](mcp-tool-catalog.md); this document
is about the HTTP layer underneath it.

---

## The two-hop path, and why

```
copilot backend ──MCP──▶ mcp-servers/alarm_management ──HTTP──▶ Alarm Management API
                                    │
                                    └── connectors/alarm_api  (httpx + tenacity)
```

The copilot never speaks HTTP to the alarm API. That is the assignment's requirement, and it is
enforced structurally rather than by convention: `apps/backend/config.py` has no `ALARM_API_*`
field at all, so the backend cannot express the alternative. The token, the base URL and the retry
policy all live one process away.

`connectors/alarm_api` is a package separate from the MCP server because retry and error semantics
are worth testing against `respx` without an MCP session in the way, and because a second MCP
server or a batch job would reuse it unchanged.

## The simulator, and pointing at something real

`apps/alarm_api` is a FastAPI simulator standing in for the real Alarm Management API, which is not
reachable from this machine. Everything above it is written against the documented contract — the
Postman collection shipped with the assignment — not against the simulator's internals, so
switching to a real deployment is:

```bash
ALARM_API_BASE_URL=https://alarm-api.internal.example.com
ALARM_API_TOKEN=<the real token>
```

Nothing else changes. `make mcp` then talks to the real API and the copilot is unaware.

Two properties of the simulator exist for the acceptance scenario rather than for realism:

- **The dataset is generated relative to `datetime.now(UTC)`**, from a fixed RNG seed. The
  collection's own examples use a static 2026-05-01 → 2026-07-01 window, but "the last 90 days" is
  evaluated against *now* — static fixtures return an empty window and silently turn the demo into
  a shrug while every test still passes.
- **Boiler Feed Pump 101 carries a planted recurring pattern**: `Suction Pressure Low` →
  `Vibration High` → `Bearing Temperature High` clusters, weighted high and critical, recurring
  across the window with rising frequency. It lines up with the authored corpus, so
  "compare the API's recommendations with the document guidance" has real substance.

## Authentication

| | |
|---|---|
| Scheme | `Authorization: Bearer <token>` |
| Configured by | `ALARM_API_TOKEN` (MCP server), `ALARM_API_TOKEN` (simulator, to validate against) |
| Local default | `demo-token` |
| Exempt | `GET /health` — no auth, per the collection |

The 401 body uses the same envelope as every other failure and **never echoes the supplied token**;
reflecting a credential into a response is how credentials reach logs. The token is held by the
MCP server's settings, cannot be overridden by any tool argument, and appears in no tool result,
trace event or log line — asserted end to end in `tests/e2e`.

A 401 or 403 is mapped to `AlarmApiAuthError`, which the MCP layer reports as a *server
configuration* problem with an explicit "do not retry". No retry by a model fixes a bad deployment
credential, and a model that keeps trying just burns the step budget.

## Correlation headers

Three headers, with exactly these spellings, are sent on every request and echoed by the API:

| Header | Value |
|---|---|
| `trace_id` | `mcp-<12 hex>`, or the client's own id when supplied through MCP request metadata |
| `x-client-id` | `MCP_CLIENT_ID`, default `alarm-copilot` |
| `x-metadata-tag` | The MCP tool name that made the call |

The collection requires propagation and echo on the four POST analytics endpoints; this
implementation sends them on **every** request, including the GETs, because a correlation scheme
with holes in it is not one you can follow through an incident. The echoed value comes back in each
tool result as `trace_id`, and the e2e test asserts `detail["trace_id_echoed"] == row["trace_id"]`
— one row in the GUI's trace panel, one line in the API's log.

## Endpoints

### Implemented

| Method | Path | Reached by |
|---|---|---|
| GET | `/health` | `make api` smoke, compose health check (no auth) |
| GET | `/assets/search` | `search_assets` |
| GET | `/assets/{asset_id}/metadata` | `search_assets` (enriches the best match) |
| GET | `/alarms` | `get_alarms` |
| GET | `/alarms/{alarm_id}` | `AlarmApiClient.alarm_detail` — no MCP tool |
| POST | `/alarms/summary` | `get_alarm_summary` |
| POST | `/alarms/recurring` | `get_recurring_alarms` |
| POST | `/recommendations/operator-actions` | `get_operator_recommendations` |
| GET | `/analytics/kpi-definitions` | `AlarmApiClient.kpi_definitions` — no MCP tool |

The last two are implemented and tested at the connector level but are not exposed as tools. That
is a judgement about the catalogue, not an oversight: `get_operator_recommendations` already
returns the alarm's full context, so a single-alarm lookup tool would mostly duplicate it, and a
tool listing KPI definitions gives a model one more thing to call on the way to an answer that does
not need it. A catalogue is a prompt — every entry in it costs attention on every planning step.
They are in the connector because the endpoints are in the contract and a client that covers the
contract is the reusable thing.

`POST /alarms/recurring` is **not in the collection** — it is an addition, because the acceptance
scenario's word *recurring* has no endpoint behind it otherwise, and computing recurrence by
pulling every alarm into the model's context and counting would be both slow and untraceable. It is
documented as ours rather than presented as part of the given contract.

### Not implemented

Seven of the collection's fifteen endpoints:

```
POST /alarms/trends                      POST /alarms/priority-score
POST /alarms/correlation                 POST /calculation-code/generate
POST /alarms/flood-analysis              POST /calculation-code/execute
POST /alarms/rationalization-candidates
```

This was a deliberate cut, taken when the time box got tight, on the guidelines' own statement that
a smaller fully-integrated solution beats a broad incomplete one. None of the seven is needed by
the acceptance scenario, and each would have added a simulator endpoint, a connector method, an MCP
tool, three layers of tests and a catalogue entry — spent on breadth rather than on the GUI, the
e2e test and these documents, which are graded. `/alarms/correlation` is the one whose absence
costs something real: contributing factors are currently reasoned from `get_recurring_alarms` plus
the troubleshooting guide rather than from a computed correlation. See
[`known-limitations.md`](known-limitations.md).

### Response shapes

Only four response shapes are pinned by the collection's own tests. Those are matched exactly:

| Endpoint | Pinned shape |
|---|---|
| `GET /assets/search` | `{"results": [{"asset_id": …}]}` |
| `GET /alarms` | `{"data": [{"alarm_id": …}]}` |
| `POST /alarms/flood-analysis` | `{"flood_windows": [{"start": …, "end": …}]}` (not implemented) |
| `POST /calculation-code/generate` | `{"calculation_id": …}` (not implemented) |

The rest were ours to design. They are documented in
[`mcp-tool-catalog.md`](mcp-tool-catalog.md) with real example responses, and pinned by
`tests/integration/test_postman_contract.py`, which replays the collection's requests and asserts
the four wrapper keys, the chaining data flow (`results[0].asset_id` → `/alarms` →
`data[0].alarm_id` → recommendations) and that the trace headers come back.

## Error envelope

Every failure has the same flat body, so the connector and the MCP error mapping have one shape to
parse:

```json
{
  "error": "asset_not_found",
  "message": "No asset with id AST-NOPE-0000.",
  "trace_id": "mcp-029c2cb0d26a"
}
```

422 adds a `detail` list of `{loc, msg, type}` from Pydantic, because field-level detail is what
lets a caller fix the call instead of guessing. FastAPI's defaults are deliberately overridden to
get here: its `HTTPException` body is whatever was passed as `detail`, and its 422 is a bare list.

Status → domain exception → what the model is told:

| Status | Exception | Retried? | Reported as |
|---|---|---|---|
| 404 | `AlarmApiNotFoundError` | no | the id does not exist; resolve it with `search_assets` |
| 400, 422 | `AlarmApiValidationError` | no | the API's message plus the offending field path |
| 401, 403 | `AlarmApiAuthError` | no | server configuration problem; do not retry |
| 429 | `AlarmApiRateLimitError` | yes | rate limited, with the retry-after when given |
| 500, 502, 503, 504 | `AlarmApiUnavailableError` | yes | an outage, **not** an empty result |
| timeout, connect error | `AlarmApiUnavailableError` | yes | same |
| unparseable body | `AlarmApiProtocolError` | no | the API contract has changed |

The distinction that matters most is the last column's "an outage, not an empty result". A model
told only that a call failed will happily answer that no high-severity alarms were found, which
during an actual outage is a confidently wrong answer to a safety question. The message says which
one it is.

## Timeouts, retries, pagination

| Setting | Default | Notes |
|---|---|---|
| `ALARM_API_TIMEOUT_SECONDS` | 10.0 | Total per-attempt httpx timeout: connect, read and write |
| `ALARM_API_MAX_RETRIES` | 3 | Attempts *after* the first |
| `MCP_MAX_ROWS` | 100 | Rows per tool response, under the API's own cap |

Retries use tenacity with `stop_after_attempt(1 + max_retries)` and exponential backoff —
`0.25 · 2^(n-1)`, capped at 4s — honouring `Retry-After` when the API sends one, itself capped at
30s so a hostile or mistaken header cannot park a request for an hour. Retryable statuses are
exactly `{429, 500, 502, 503, 504}`; 4xx is never retried, because a malformed argument or a
rejected credential fails identically every time.

Worst case for a slow API is therefore `(1 + 3) × 10s` before the tool reports failure, which is
inside `MCP_TOOL_TIMEOUT_SECONDS=30` for one attempt but not for four — a deliberate ordering, so a
persistent outage surfaces as an MCP timeout at the copilot rather than holding the SSE stream open
for forty seconds. Every endpoint behind these tools is a read, including the POSTs (queries with
bodies), so retrying needs no idempotency key.

Pagination is exposed on `get_alarms` only, as `total_matching` / `returned` / `page` / `has_more`.
The tool caps rows below the API's cap of 500 because these payloads are serialised into a context
window, and an unbounded page crowds out the question that was asked.

## Verifying the integration

```bash
make api                                        # simulator on :8000
curl -s localhost:8000/health | jq              # must succeed with NO auth header
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/assets/search?query=pump
                                                # 401 — the token is required
curl -s -H 'Authorization: Bearer demo-token' \
     'localhost:8000/assets/search?query=Boiler%20Feed%20Pump%20101' | jq '.results[0]'

make mcp                                        # MCP server on :9100/mcp
make mcp-smoke                                  # discover tools, chain three calls over the wire

uv run pytest tests/integration/test_postman_contract.py -q   # the collection, replayed
uv run pytest tests/unit/test_alarm_api_client.py -q          # retry, timeout, error mapping (respx)
```
