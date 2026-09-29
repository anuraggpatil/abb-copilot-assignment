/**
 * A one-line strip of run metrics above the trace: how much work this answer took.
 *
 * These are counts over the trace the browser already holds, not measurements of their own —
 * every figure here is derivable by eye from the rows underneath, which is exactly why it is
 * safe to compute client-side. The alarm *figures* are not: those come from `KpiBoard`, whose
 * numbers the answer's prose is also written from.
 *
 * `tool time` is the sum of the traced step durations, not wall clock. They differ — the model's
 * thinking between steps is not in it — so it is labelled for what it is rather than presented
 * as "time to answer".
 */

import type { CopilotAnswer, TraceEvent } from '../types'

interface Props {
  answer: CopilotAnswer | null
  events: TraceEvent[]
}

export default function RunMetrics({ answer, events }: Props) {
  if (events.length === 0 && !answer) return null

  const mcpCalls = events.filter((event) => event.kind === 'mcp_tool_call').length
  const retrievals = events.filter((event) => event.kind === 'rag_retrieval').length
  const failed = events.filter((event) => event.status === 'error').length
  const toolMs = events.reduce((total, event) => total + event.duration_ms, 0)
  const cited = answer?.citations.filter((citation) => citation.cited_in_answer).length ?? 0

  return (
    <div className="runmetrics">
      <Stat label="steps" value={answer ? `${answer.steps_used}` : `${events.length}`} />
      <Stat label="MCP calls" value={`${mcpCalls}`} />
      <Stat label="retrievals" value={`${retrievals}`} />
      {answer && <Stat label="citations" value={`${cited}/${answer.citations.length}`} />}
      <Stat label="tool time" value={`${(toolMs / 1000).toFixed(1)} s`} />
      {failed > 0 && <Stat label="failed" value={`${failed}`} tone="bad" />}
      {answer?.model && <Stat label="model" value={answer.model} />}
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'bad' }) {
  return (
    <span className={tone ? `runmetrics__stat runmetrics__stat--${tone}` : 'runmetrics__stat'}>
      <span className="runmetrics__label">{label}</span>
      <span className="runmetrics__value">{value}</span>
    </span>
  )
}
