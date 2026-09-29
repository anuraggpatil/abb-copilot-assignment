/**
 * The backend's payloads, as the GUI consumes them.
 *
 * Hand-written rather than generated from the OpenAPI schema. A generator would be the right
 * call on a larger surface; here it is four endpoints, and a hand-written file lets each field
 * carry the note that explains how to *render* it — `relevance: null` means "cited upstream,
 * not ranked", which no generator would tell you and which is the difference between an honest
 * citation panel and one that reports 0.000 relevance for the most important section.
 *
 * Source of truth: `apps/backend/orchestration/orchestrator.py` (CopilotAnswer) and
 * `apps/backend/tracing/trace.py` (TraceEvent). Extra fields are tolerated — the models are
 * `extra="forbid"` on the way in, so anything new here arrives additively.
 */

export type EventKind =
  | 'plan'
  | 'tool_discovery'
  | 'mcp_tool_call'
  | 'rag_retrieval'
  | 'llm_call'
  | 'synthesis'
  | 'error'

export type EventStatus = 'ok' | 'error' | 'partial'

export interface TraceEvent {
  event_id: string
  conversation_id: string
  request_id: string
  /** Propagated to the MCP server and on to the alarm API, which echoes it back. */
  trace_id: string
  seq: number
  kind: EventKind
  name: string
  started_at: string
  duration_ms: number
  status: EventStatus
  summary: string
  /**
   * Shape depends on `kind`. For `mcp_tool_call` it holds `raw_request`, `raw_response` and
   * `upstream` (one entry per HTTP call the tool made, with `status_code` and `attempts`);
   * for `rag_retrieval`, `chunks` with per-chunk scores. Already redacted server-side, so it
   * is safe to display verbatim — that is the design, not an assumption made here.
   */
  detail: Record<string, unknown>
}

export interface UpstreamCall {
  method?: string
  path?: string
  status_code?: number | null
  attempts?: number
  duration_ms?: number
}

export interface Citation {
  reference: string
  document: string
  revision: string
  section: string
  quote: string
  /** `null` means the section was fetched because a recommendation named it, not because it
   *  ranked. Render that as "cited upstream" — never as a score. */
  relevance: number | null
  selected_by: string
  cited_in_answer: boolean
}

export interface ToolCallSummary {
  name: string
  backend: string
  ok: boolean
  duration_ms: number
  error_kind: string | null
}

/** One KPI tile. `value` is raw; the unit decides how it is formatted and whether it is a
 *  percentage. `tone` is colour only — it is decided server-side in `kpis.py` so the thresholds
 *  live in one place, and it never travels alone: the tile always prints the number too. */
export interface Kpi {
  key: string
  label: string
  value: number
  unit: string
  tone: 'neutral' | 'good' | 'warn' | 'bad'
  hint: string
}

export interface PatternKpi {
  alarm_name: string
  asset_name: string
  occurrences: number
  max_severity: string
  trend: string
  occurrences_first_half: number | null
  occurrences_second_half: number | null
  chattering_share: number | null
}

/** Numbers lifted from the tool results, never computed in the browser. An absent KPI means no
 *  tool returned it — which is why every field here is optional-by-emptiness rather than zero.
 *  Rendering a zero an operator reads as "checked and clear" is the failure mode this shape
 *  exists to prevent. */
export interface InvestigationKpis {
  asset_name: string
  asset_id: string
  window_start: string | null
  window_end: string | null
  window_days: number | null
  total_alarms: number | null
  metrics: Kpi[]
  patterns: PatternKpi[]
  /** Tool names the figures came from, so a tile can be tied back to a trace row. */
  sources: string[]
}

export interface CopilotAnswer {
  conversation_id: string
  request_id: string
  trace_id: string
  question: string
  answer: string
  citations: Citation[]
  caveats: string[]
  /** References the model cited that were never retrieved. Already neutralised in the answer
   *  text; surfaced here because hiding it would make the guard invisible. */
  invented_references: string[]
  low_confidence: boolean
  steps_used: number
  steps_exhausted: boolean
  tool_calls: ToolCallSummary[]
  model: string
  /** Earlier turns of this conversation the copilot had in front of it. `0` on a first question;
   *  rendered as "answered in context of N earlier turns" so an operator can tell the difference
   *  between a follow-up that was understood and one that was answered cold. */
  history_turns?: number
  /** Optional in the type, always sent by the backend: a trace replayed from an older build of
   *  the store has no `kpis` key, and the board must not be what crashes the answer. */
  kpis?: InvestigationKpis
}

export interface ConversationTrace {
  conversation_id: string
  events: TraceEvent[]
}

export interface Health {
  status: string
  provider: string
  native_tools: boolean
  mcp_reachable: boolean
  mcp_server_url: string
}

export interface ToolDescriptor {
  name: string
  title: string
  description: string
  /** `mcp` or `local`. The whole point of showing it: one catalogue, two backends. */
  backend: string
  input_schema: Record<string, unknown>
}

export interface Catalogue {
  tools: ToolDescriptor[]
  degradations: string[]
}
