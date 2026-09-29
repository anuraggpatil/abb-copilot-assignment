/**
 * The MCP execution trace — a required deliverable, and the part of this screen a reviewer will
 * spend the most time in.
 *
 * Rows arrive over SSE as the orchestrator records them, so the panel fills while the question is
 * still being answered. Collapsed, each row is one line: sequence, kind, name, duration, status.
 * Expanded, it shows what that kind of step actually did — for an MCP call, the raw request and
 * response plus every HTTP call the tool made underneath, with status codes and retry counts; for
 * a retrieval, the query and per-chunk scores.
 *
 * `detail` is rendered from untyped JSON on purpose. It is redacted server-side and its shape
 * varies by kind; narrowing it into an interface per kind would mean a frontend release every
 * time a step records one more number. The readers below are total — a missing key renders
 * nothing rather than throwing, because a trace panel that crashes takes the answer with it.
 */

import { useState } from 'react'

import type { TraceEvent, UpstreamCall } from '../types'

const KIND_LABEL: Record<string, string> = {
  tool_discovery: 'discover',
  plan: 'plan',
  mcp_tool_call: 'MCP',
  rag_retrieval: 'RAG',
  llm_call: 'llm',
  synthesis: 'synthesis',
  error: 'error',
}

interface Props {
  events: TraceEvent[]
  running: boolean
}

export default function TracePanel({ events, running }: Props) {
  const [open, setOpen] = useState<Set<string>>(new Set())

  function toggle(eventId: string) {
    setOpen((current) => {
      const next = new Set(current)
      if (next.has(eventId)) next.delete(eventId)
      else next.add(eventId)
      return next
    })
  }

  return (
    <section className="panel">
      <header className="panel__head">
        <h2>Execution trace</h2>
        <span className="panel__meta">
          {events.length} step{events.length === 1 ? '' : 's'}
          {running && ' · running'}
          {events.length > 0 && (
            <button
              className="button button--link"
              type="button"
              onClick={() =>
                setOpen((current) =>
                  current.size === events.length
                    ? new Set()
                    : new Set(events.map((event) => event.event_id)),
                )
              }
            >
              {open.size === events.length ? 'collapse all' : 'expand all'}
            </button>
          )}
        </span>
      </header>

      {events.length === 0 ? (
        <p className="empty">
          {running ? 'Waiting for the first step…' : 'Ask a question to see the trace.'}
        </p>
      ) : (
        <ol className="trace">
          {events.map((event) => (
            <li key={event.event_id} className={`trace__row trace__row--${event.status}`}>
              <button
                className="trace__summary"
                type="button"
                aria-expanded={open.has(event.event_id)}
                onClick={() => toggle(event.event_id)}
              >
                <span className="trace__seq">{event.seq}</span>
                <span className={`tag tag--${event.kind}`}>
                  {KIND_LABEL[event.kind] ?? event.kind}
                </span>
                <span className="trace__name">{event.name}</span>
                <span className="trace__text">{event.summary}</span>
                <span className="trace__ms">{Math.round(event.duration_ms)} ms</span>
                <span className={`trace__status trace__status--${event.status}`}>
                  {event.status}
                </span>
              </button>
              {open.has(event.event_id) && <Detail event={event} />}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function Detail({ event }: { event: TraceEvent }) {
  const detail = event.detail
  return (
    <div className="trace__detail">
      <div className="trace__ids">
        trace_id <code>{event.trace_id}</code> · request_id <code>{event.request_id}</code> ·{' '}
        {event.started_at}
      </div>

      {event.kind === 'mcp_tool_call' && <McpDetail detail={detail} />}
      {event.kind === 'rag_retrieval' && <RagDetail detail={detail} />}

      <details className="trace__raw">
        <summary>full detail (redacted server-side)</summary>
        <pre>{JSON.stringify(detail, null, 2)}</pre>
      </details>
    </div>
  )
}

function McpDetail({ detail }: { detail: Record<string, unknown> }) {
  const upstream = asUpstream(detail['upstream'])
  const retries = asNumber(detail['retry_count'])
  const echoed = asString(detail['trace_id_echoed'])

  return (
    <>
      <dl className="kv">
        <dt>server</dt>
        <dd>
          <code>{asString(detail['server']) ?? '—'}</code>
        </dd>
        <dt>tool</dt>
        <dd>
          <code>{asString(detail['tool']) ?? '—'}</code>
        </dd>
        <dt>retries</dt>
        <dd>{retries ?? 0}</dd>
        {/* The API echoes the id it was given. Seeing the same value here and in the API's own
            log is the proof that one operator question maps to one server-side request. */}
        <dt>trace_id echoed by the API</dt>
        <dd>{echoed ? <code>{echoed}</code> : <span className="muted">not echoed</span>}</dd>
      </dl>

      <h4>Upstream HTTP calls</h4>
      {upstream.length === 0 ? (
        <p className="empty">The tool reported no upstream calls.</p>
      ) : (
        <table className="upstream">
          <thead>
            <tr>
              <th>method</th>
              <th>path</th>
              <th>status</th>
              <th>attempts</th>
              <th>ms</th>
            </tr>
          </thead>
          <tbody>
            {upstream.map((call, index) => (
              <tr key={`${call.method}-${call.path}-${index}`}>
                <td>{call.method ?? '—'}</td>
                <td>
                  <code>{call.path ?? '—'}</code>
                </td>
                <td className={statusClass(call.status_code)}>{call.status_code ?? 'no response'}</td>
                <td>{call.attempts ?? 1}</td>
                <td>{call.duration_ms === undefined ? '—' : Math.round(call.duration_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Raw request</h4>
      <pre>{JSON.stringify(detail['raw_request'] ?? {}, null, 2)}</pre>
      <h4>Raw response</h4>
      <pre>{JSON.stringify(detail['raw_response'] ?? {}, null, 2)}</pre>
    </>
  )
}

function RagDetail({ detail }: { detail: Record<string, unknown> }) {
  const chunks = asRecords(detail['chunks'])
  const lowConfidence = detail['low_confidence'] === true

  return (
    <>
      <dl className="kv">
        <dt>query</dt>
        <dd>{asString(detail['query']) ?? '—'}</dd>
        <dt>filters</dt>
        <dd>
          <code>{JSON.stringify(detail['filters'] ?? {})}</code>
        </dd>
        <dt>candidates</dt>
        <dd>{asNumber(detail['candidates_considered']) ?? '—'}</dd>
        <dt>top score</dt>
        <dd>{formatScore(detail['top_score'])}</dd>
        <dt>confidence</dt>
        <dd>
          {lowConfidence ? (
            <span className="tag tag--error">low</span>
          ) : (
            <span className="tag">ok</span>
          )}
          {asString(detail['confidence_note']) && (
            <span className="muted"> {asString(detail['confidence_note'])}</span>
          )}
        </dd>
      </dl>

      <h4>Chunks considered</h4>
      {chunks.length === 0 ? (
        <p className="empty">Nothing was retrieved.</p>
      ) : (
        <table className="upstream">
          <thead>
            <tr>
              <th>reference</th>
              <th>dense</th>
              <th>lexical</th>
              <th>fused</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {chunks.map((chunk, index) => (
              <tr key={`${asString(chunk['chunk_id']) ?? index}`}>
                <td>
                  <code>{asString(chunk['reference']) ?? asString(chunk['chunk_id']) ?? '—'}</code>
                </td>
                <td>{formatScore(chunk['dense'])}</td>
                <td>{formatScore(chunk['lexical'])}</td>
                <td>{formatScore(chunk['fused'])}</td>
                <td>{chunk['pinned'] === true && <span className="tag">pinned</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

function statusClass(status: number | null | undefined): string {
  if (status === null || status === undefined) return 'bad'
  if (status >= 500) return 'bad'
  if (status >= 400) return 'warn'
  return 'good'
}

function formatScore(value: unknown): string {
  return typeof value === 'number' ? value.toFixed(3) : '—'
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

function asRecords(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is Record<string, unknown> => isRecord(item))
}

function asUpstream(value: unknown): UpstreamCall[] {
  return asRecords(value).map((call) => ({
    method: asString(call['method']) ?? undefined,
    path: asString(call['path']) ?? undefined,
    status_code: asNumber(call['status_code']),
    attempts: asNumber(call['attempts']) ?? undefined,
    duration_ms: asNumber(call['duration_ms']) ?? undefined,
  }))
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
