# MCP tool catalogue

The `alarm-management` MCP server (`mcp-servers/alarm_management/`) is the **only** path from the
copilot to the Alarm Management API. It exposes five tools. Everything below is generated from the
running server and a real investigation against the seeded simulator — the example responses are
verbatim output, trimmed where a list repeats itself, not illustrations written by hand.

Start the server on its own:

```bash
make mcp                                    # streamable HTTP on :9100/mcp
make mcp MCP_PORT=9200                      # if 9100 is taken too
uv run python -m alarm_management --transport stdio    # for MCP Inspector / Claude Desktop
make mcp-smoke                              # discover tools and run the chain over the network
```

---

## Conventions that apply to every tool

**Typed contracts, both directions.** Tool arguments are declared as annotated Python parameters,
so the JSON Schema a client discovers is generated from the signature and cannot drift from it.
Results are Pydantic models (`mcp-servers/alarm_management/schemas.py`), so a response that does
not match its declared shape raises before it reaches the client rather than after.

**Authentication.** The server authenticates to the alarm API with a bearer token from
`ALARM_API_TOKEN`, held in the server's own settings and sent on every upstream request. Clients
of the MCP server do not present a credential and cannot influence which one is used — the copilot
never sees it, no tool argument can override it, and it appears in no response, log line or trace.
A 401/403 from the API is reported as a *server configuration* problem with an explicit
instruction not to retry, because no retry by a model can fix a bad deployment credential.

**Correlation metadata.** Every tool call generates a `trace_id` (`mcp-<12 hex>`, or the client's
own when one is supplied through request metadata) and sends it upstream as `trace_id`, alongside
`x-client-id: ${MCP_CLIENT_ID}` and `x-metadata-tag: <tool name>`. The API echoes `trace_id` back;
the tool returns it in the `trace_id` field, so one row in the GUI's trace panel ties to one line
in the API's log.

**Upstream visibility.** Every result carries `upstream`: one entry per HTTP attempt, with
`method`, `path`, `status_code`, `attempts`, `duration_ms` and `error`. This is what the GUI's
trace panel renders, and it is why retry counts and status codes are observable without reading
server logs.

**Timeouts.** `ALARM_API_TIMEOUT_SECONDS` (default 10s) is a total per-attempt httpx timeout —
connect, read and write. A timeout is raised as `AlarmApiUnavailableError`, which is retryable, so
a slow API costs at most `(1 + ALARM_API_MAX_RETRIES) × timeout` before the tool reports failure.
There is no separate MCP-level timeout: the client's own request timeout applies, and stacking a
second deadline inside would only produce two different error messages for one condition.

**Retries.** `ALARM_API_MAX_RETRIES` (default 3) attempts *after* the first, for 429 and 5xx and
transport failures only, with exponential backoff (0.25s, 0.5s, 1s, capped at 4s) that honours
`Retry-After` when the API sends one. 4xx is never retried: a malformed argument or a rejected
credential fails identically every time. Every endpoint behind these tools is a read — including
the POSTs, which are queries with bodies — so retrying needs no idempotency key.

**Error behaviour.** Connector exceptions are mapped to MCP `ToolError` by
`mcp-servers/alarm_management/mapping.py`. The message is written for the model that has to decide
what to do next, and carries no token, traceback or plant data beyond what the caller supplied:

| Upstream | Exception | What the tool error says |
|---|---|---|
| 404 | `AlarmApiNotFoundError` | the id does not exist; call `search_assets` to resolve it |
| 400, 422 | `AlarmApiValidationError` | the API's message plus the offending field path; fix and call again |
| 401, 403 | `AlarmApiAuthError` | server configuration problem; **do not retry**; report data unavailable |
| 429 | `AlarmApiRateLimitError` | rate limited, with the retry-after when given |
| 5xx, timeout, connection failure | `AlarmApiUnavailableError` | an outage, **not** an empty result — say alarm data is unavailable rather than answering without it |
| unparseable body | `AlarmApiProtocolError` | the API contract has changed; treat alarm data as unavailable |

Invalid *arguments* never reach the connector: the MCP SDK validates them against the tool's
schema first, so a wrong type or an out-of-range integer is rejected with a schema error before
any HTTP request is made.

**Pagination.** `get_alarms` is the only paged tool. It returns `total_matching`, `returned`,
`page` and `has_more`; the model advances by passing `page`. Rows per response are capped by
`MCP_MAX_ROWS` (default 100) below the API's own cap of 500, because these payloads are
serialised into a context window and an unbounded page crowds out the question.

**Windows.** Tools that read history accept `lookback_days` (default 90) or an explicit
`start_time`/`end_time` pair, which overrides it. The window is resolved on the server against
its own clock and returned as `window_start`/`window_end`, so an answer states the window it
actually read rather than the one that was asked for.

---

## `search_assets`

**Purpose.** Resolve a plant asset from a name, tag or free text into the `asset_id` every other
tool is keyed on. The first call of any investigation, and the reason the copilot never has to
guess an id.

**Underlying operation.** `GET /assets/search`, then `GET /assets/{asset_id}/metadata` for the
best match — one tool call, two upstream requests, so the model gets criticality and related
assets without a second round trip.

**Input schema**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | — | Asset name, plant tag or free text, e.g. `Boiler Feed Pump 101` |
| `limit` | integer 1–25 | no | 5 | Maximum matches |
| `unit` | string \| null | no | null | Restrict to one process unit |
| `site` | string \| null | no | null | Restrict to one site |

**Output schema.** `query`, `total_matches`, `assets[]` (`asset_id`, `name`, `asset_type`, `tag`,
`site`, `unit`, `criticality`, `match_score`), plus the best match's enrichment:
`best_match_tag`, `best_match_criticality`, `best_match_related_assets[]`,
`best_match_active_alarms`; and the common `trace_id`, `upstream[]`.

**Example invocation**

```json
{ "name": "search_assets", "arguments": { "query": "Boiler Feed Pump 101" } }
```

**Example response** (trimmed to two of six matches)

```json
{
  "upstream": [
    {"method": "GET", "path": "/assets/search", "status_code": 200, "attempts": 1, "duration_ms": 4.48, "error": null},
    {"method": "GET", "path": "/assets/AST-PMP-0001/metadata", "status_code": 200, "attempts": 1, "duration_ms": 1.4, "error": null}
  ],
  "trace_id": "mcp-029c2cb0d26a",
  "query": "Boiler Feed Pump 101",
  "total_matches": 6,
  "assets": [
    {"asset_id": "AST-PMP-0001", "name": "Boiler Feed Pump 101", "asset_type": "centrifugal_pump",
     "tag": "2-BFP-101", "site": "EastRefinery", "unit": "Unit 2", "criticality": 5, "match_score": 1.0},
    {"asset_id": "AST-PMP-0003", "name": "BFP-101 Lube Oil Pump", "asset_type": "centrifugal_pump",
     "tag": "2-BFP-101-LOP", "site": "EastRefinery", "unit": "Unit 2", "criticality": 3, "match_score": 0.9}
  ],
  "best_match_tag": "2-BFP-101",
  "best_match_criticality": 5,
  "best_match_related_assets": [
    {"asset_id": "AST-PMP-0002", "name": "Boiler Feed Pump 102", "relationship": "related"},
    {"asset_id": "AST-MTR-0001", "name": "Motor M-201", "relationship": "related"}
  ],
  "best_match_active_alarms": 6
}
```

---

## `get_alarms`

**Purpose.** List individual alarm events for an asset, unit or site over a window, newest first.
The evidence layer: individual events with values, setpoints and acknowledgement delays.

**Underlying operation.** `GET /alarms` with query filters and paging.

**Input schema**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `asset_id` | string \| null | no | null | Resolve names with `search_assets` first |
| `unit`, `site` | string \| null | no | null | Alternative scopes |
| `severity` | string[] \| null | no | null | `low` \| `medium` \| `high` \| `critical` |
| `status` | string[] \| null | no | null | `active` \| `acknowledged` \| `cleared` \| `suppressed` |
| `alarm_type` | string[] \| null | no | null | `process` \| `equipment` \| `safety` \| `system` |
| `lookback_days` | integer 1–400 | no | 90 | Window ending now |
| `start_time`, `end_time` | datetime \| null | no | null | Explicit window; overrides `lookback_days` |
| `limit` | integer 1–100 | no | 25 | Events per page |
| `page` | integer ≥1 | no | 1 | Use when `has_more` is true |

**Output schema.** `total_matching`, `returned`, `page`, `has_more`, `window_start`, `window_end`,
`filters_applied`, `alarms[]` (`alarm_id`, `alarm_name`, `alarm_type`, `severity`, `status`,
`start_time`, `asset_id`, `asset_name`, `unit`, `ack_delay_minutes`, `duration_minutes`, `value`,
`setpoint`, `unit_of_measure`, `chattering`), `trace_id`, `upstream[]`.

**Example invocation**

```json
{ "name": "get_alarms",
  "arguments": { "asset_id": "AST-PMP-0001", "severity": ["high", "critical"], "lookback_days": 90, "limit": 2 } }
```

**Example response** (trimmed to one of two events)

```json
{
  "upstream": [{"method": "GET", "path": "/alarms", "status_code": 200, "attempts": 1, "duration_ms": 6.56, "error": null}],
  "trace_id": "mcp-cb63951bc3a5",
  "total_matching": 47,
  "returned": 2,
  "page": 1,
  "has_more": true,
  "window_start": "2026-07-01T12:00:00Z",
  "window_end": "2026-09-29T12:00:00Z",
  "filters_applied": {"asset_ids": ["AST-PMP-0001"], "severity": ["high", "critical"],
                      "start_time": "2026-07-01T12:00:00Z", "end_time": "2026-09-29T12:00:00Z"},
  "alarms": [
    {"alarm_id": "ALM-20260927-002367", "alarm_name": "Bearing Vibration High", "alarm_type": "process",
     "severity": "high", "status": "active", "start_time": "2026-09-27T10:22:32.309865Z",
     "asset_id": "AST-PMP-0001", "asset_name": "Boiler Feed Pump 101", "unit": "Unit 2",
     "ack_delay_minutes": null, "duration_minutes": null, "value": 6.75, "setpoint": 4.5,
     "unit_of_measure": "mm/s", "chattering": false}
  ]
}
```

---

## `get_alarm_summary`

**Purpose.** Aggregate counts and KPIs over a window, optionally grouped — how bad, where, and in
what proportion, without reading every event into the model's context.

**Underlying operation.** `POST /alarms/summary` (a query with a body). Sends and echoes all three
trace headers.

**Input schema**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `asset_id`, `unit`, `site` | string \| null | no | null | Scope |
| `severity` | string[] \| null | no | null | Filter before aggregating |
| `lookback_days` | integer 1–400 | no | 90 | Window ending now |
| `start_time`, `end_time` | datetime \| null | no | null | Explicit window |
| `group_by` | string[] \| null | no | null | e.g. `["severity"]`, `["asset_id"]`; omit for one overall figure |
| `kpis` | string[] \| null | no | null | Omit for the default set below; ten are available |

**The default KPI set.** Omitting `kpis` computes `alarm_count`, `critical_count`,
`high_or_above_count`, `avg_ack_delay`, `recurring_rate`, `unacknowledged_rate`,
`suppression_candidate_rate` and `operator_response_efficiency` — volume, severity, recurrence and
operator response. `max_ack_delay` and `avg_duration` are available by name. The breadth is
deliberate ([design decision 18](design-decisions.md)): the API computes all ten from one
already-loaded set of rows, and the GUI's KPI header is built from whatever comes back, so a
one-count default would leave the dashboard at the mercy of the model's arguments.

**Output schema.** `total_alarms`, `window_start`, `window_end`, `group_by`, `overall`, `groups[]`
(`key`, `alarm_count`, `kpis`), `filters_applied`, `trace_id`, `upstream[]`.

**Example invocation**

```json
{ "name": "get_alarm_summary",
  "arguments": { "asset_id": "AST-PMP-0001", "lookback_days": 90, "group_by": ["severity"],
                 "kpis": ["alarm_count"] } }
```

**Example response.** Narrowed to one KPI so the shape stays readable; the default set returns the
same structure with eight entries in `overall` and in each group's `kpis`.

```json
{
  "upstream": [{"method": "POST", "path": "/alarms/summary", "status_code": 200, "attempts": 1, "duration_ms": 1.86, "error": null}],
  "trace_id": "mcp-bbfbe38fb43c",
  "total_alarms": 96,
  "window_start": "2026-07-01T12:00:00Z",
  "window_end": "2026-09-29T12:00:00Z",
  "group_by": ["severity"],
  "overall": {"alarm_count": 96},
  "groups": [
    {"key": {"severity": "medium"}, "alarm_count": 49, "kpis": {"alarm_count": 49}},
    {"key": {"severity": "high"}, "alarm_count": 43, "kpis": {"alarm_count": 43}},
    {"key": {"severity": "critical"}, "alarm_count": 4, "kpis": {"alarm_count": 4}}
  ],
  "filters_applied": {"asset_ids": ["AST-PMP-0001"], "start_time": "2026-07-01T12:00:00Z", "end_time": "2026-09-29T12:00:00Z"}
}
```

---

## `get_recurring_alarms`

**Purpose.** Find alarm signatures that repeated over a window, with how often each occurred and
which direction it is heading. This is the tool the acceptance scenario's word *recurring* maps
onto, and `trend` is what turns a count into a finding.

**Underlying operation.** `POST /alarms/recurring`, with all three trace headers.

**Input schema**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `asset_id`, `unit`, `site` | string \| null | no | null | Scope |
| `min_severity` | string \| null | no | null | Keep occurrences at this severity or above |
| `lookback_days` | integer 1–400 | no | 90 | Window ending now |
| `start_time`, `end_time` | datetime \| null | no | null | Explicit window |
| `min_occurrences` | integer 2–1000 | no | 6 | How many repeats make a pattern |
| `limit` | integer 1–50 | no | 10 | Patterns to return |

**Output schema.** `window_start`, `window_end`, `recurrence_threshold`, `total_patterns`,
`patterns[]` (`alarm_name`, `alarm_type`, `asset_id`, `asset_name`, `occurrences`, `max_severity`,
`first_seen`, `last_seen`, `trend`, `occurrences_first_half`, `occurrences_second_half`,
`avg_ack_delay_minutes`, `chattering_share`), `filters_applied`, `trace_id`, `upstream[]`.

`chattering_share` is deliberately part of the contract: a pattern that is mostly chattering
points at a badly configured deadband rather than a process problem, and an answer that cannot
tell those apart gives the wrong advice confidently.

**Example invocation**

```json
{ "name": "get_recurring_alarms",
  "arguments": { "asset_id": "AST-PMP-0001", "min_severity": "high", "lookback_days": 90 } }
```

**Example response** (trimmed to one of two patterns)

```json
{
  "upstream": [{"method": "POST", "path": "/alarms/recurring", "status_code": 200, "attempts": 1, "duration_ms": 1.84, "error": null}],
  "trace_id": "mcp-c881605c8bd6",
  "window_start": "2026-07-01T12:00:00Z",
  "window_end": "2026-09-29T12:00:00Z",
  "recurrence_threshold": 6,
  "total_patterns": 2,
  "patterns": [
    {"alarm_name": "Bearing Vibration High", "alarm_type": "process", "asset_id": "AST-PMP-0001",
     "asset_name": "Boiler Feed Pump 101", "occurrences": 23, "max_severity": "high",
     "first_seen": "2026-07-06T06:11:39.095365Z", "last_seen": "2026-09-27T10:22:32.309865Z",
     "trend": "increasing", "occurrences_first_half": 10, "occurrences_second_half": 13,
     "avg_ack_delay_minutes": 12.4, "chattering_share": 0.0}
  ],
  "filters_applied": {"asset_ids": ["AST-PMP-0001"], "severity_threshold": "high",
                      "start_time": "2026-07-01T12:00:00Z", "end_time": "2026-09-29T12:00:00Z"}
}
```

---

## `get_operator_recommendations`

**Purpose.** Ranked, actionable recommendations for an alarm, derived from its history on the
asset. This is the advanced operation the assignment asks for, and the bridge between the two
halves of the workflow: `procedure_references` names the document sections the advice comes from,
and those exact references are passed to `search_procedures`, which fetches them by name.

**Underlying operation.** `POST /recommendations/operator-actions`, with all three trace headers.
Given `asset_id` rather than `alarm_id`, the API selects the asset's top open alarm.

**Input schema**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `alarm_id` | string \| null | no | null | A specific alarm, from `get_alarms` |
| `asset_id` | string \| null | no | null | An asset, from `search_assets`; its top open alarm is used |
| `lookback_days` | integer 1–400 | no | 90 | History the ranking is derived from |
| `max_recommendations` | integer 1–20 | no | 6 | How many ranked actions |

**Output schema.** `alarm_id`, `alarm_name`, `asset_id`, `asset_name`, `severity`, `status`,
`unit`, `context` (occurrence counts, ack delays, `likely_precursor`, `related_asset_ids`),
`actions[]` (`rank`, `action`, `rationale`, `expected_outcome`, `urgency`,
`procedure_reference`, `requires_isolation`), `escalation`, `safety_notes[]`,
`procedure_references[]`, `disclaimer`, `trace_id`, `upstream[]`.

**Example invocation**

```json
{ "name": "get_operator_recommendations", "arguments": { "asset_id": "AST-PMP-0001" } }
```

**Example response** (trimmed to one of three actions)

```json
{
  "upstream": [{"method": "POST", "path": "/recommendations/operator-actions", "status_code": 200, "attempts": 1, "duration_ms": 3.3, "error": null}],
  "trace_id": "mcp-94b5c7ee906e",
  "alarm_id": "ALM-20260923-002430",
  "alarm_name": "Motor Overload Trip",
  "asset_id": "AST-PMP-0001",
  "asset_name": "Boiler Feed Pump 101",
  "severity": "critical",
  "status": "active",
  "unit": "Unit 2",
  "context": {"lookback_days": 90, "occurrences_in_window": 4, "occurrences_last_14_days": 1,
              "total_alarms_on_asset": 96, "avg_ack_delay_minutes": 18.2,
              "highest_severity_in_window": "critical", "open_alarms_on_asset": 3,
              "likely_precursor": null, "related_asset_ids": ["AST-PMP-0002", "AST-MTR-0001"]},
  "actions": [
    {"rank": 1, "action": "Do not attempt a restart until the driven-end cause is identified",
     "rationale": "Repeated restarts against a mechanical or hydraulic overload escalate motor and coupling damage.",
     "expected_outcome": "Prevented compounding damage", "urgency": "immediate",
     "procedure_reference": "SAF-PUMP-LOTO §2 Before any intervention", "requires_isolation": true}
  ],
  "escalation": {"required": true, "reason": "Critical-severity safety alarm.",
                 "escalate_to": "Rotating Equipment Engineer", "asset_criticality": 5},
  "safety_notes": ["One or more recommended actions require the asset to be isolated and locked out first. Follow SAF-PUMP-LOTO before any hands-on work."],
  "procedure_references": ["MM-CP-MAINT §7 Failure reporting", "SAF-PUMP-LOTO §2 Before any intervention"]
}
```

---

## `search_procedures` — a local tool in the same catalogue

Not an MCP tool, and documented here because the planner cannot tell the difference: it is
discovered in the same catalogue, called through the same registry, validated the same way, and
traced the same way, with `backend: "local"` instead of `"mcp"`. That is what puts MCP and RAG in
one workflow rather than two demos. Why it is local rather than a second MCP server is argued in
[`design-decisions.md`](design-decisions.md); the retrieval design is in
[`rag-design.md`](rag-design.md).

**Purpose.** Search the indexed procedure corpus and return the passages that answer a question,
each with the citation needed to attribute it.

**Input schema**

| Field | Type | Required | Notes |
|---|---|---|---|
| `query` | string (min 3) | yes | Phrased as the question to answer — the index is semantic as well as lexical |
| `references` | string[] (≤10) | no | Sections to fetch **by name**, e.g. `OP-BFP-101 §4.2 …`; pass through whatever the alarm tools cited |
| `doc_ids` | string[] (≤10) | no | Restrict to these documents |
| `doc_types` | string[] (≤4) | no | `operating_procedure` \| `troubleshooting_guide` \| `maintenance_manual` \| `safety_procedure` |
| `top_k` | integer 1–8 | no | Passages to return |

`additionalProperties: false` — an argument the schema does not name is rejected rather than
ignored, so a hallucinated parameter is a visible error instead of a silent no-op.

**Output schema (model-facing).** `passages[]` with `reference`, `document`, `revision`,
`section`, `quote`, `relevance`, `selected_by`, `trusted: false`; plus `low_confidence`,
`confidence_note` and `unresolved_references`.

Two fields carry rules rather than data. `relevance` is `null` for a passage fetched by name —
nothing measured a score, and reporting one would be an invention. `trusted: false` marks the
passage as reference data: instructions found inside a document are to be reported, never obeyed.

**Trace-facing output** is separate and carries no document text at all: `query`, `filters`,
`candidates_considered`, `top_score`, `top_relevance`, `low_confidence`, `confidence_note`,
`unresolved_references`, and per chunk the `chunk_id`, `reference` and dense/lexical/fused scores.
The two payloads are built from different fields, which is how "a trace never carries a complete
document" is made mechanical rather than a matter of care.

**Error behaviour.** A retrieval that finds nothing is a result, not an error:
`low_confidence: true` with the nearest matches still listed, and the synthesis prompt then takes
the "insufficient documented evidence" path. The tool raises only when the index is missing or
unreadable, which the backend reports as a degradation on `/health` and `/tools`.
