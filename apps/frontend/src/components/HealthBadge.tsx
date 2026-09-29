/**
 * The status strip: which LLM is answering, how tools are selected, and whether MCP is up.
 *
 * `mcp_reachable` is the one that earns its place. The demo has to include the degraded case —
 * the MCP server stopped mid-session — and without this strip that looks like the copilot
 * becoming vague for no reason. With it, the reason is on screen before the answer is.
 */

import type { Health } from '../types'

interface Props {
  health: Health | null
  error: string | null
  degradations: string[]
}

export default function HealthBadge({ health, error, degradations }: Props) {
  if (error) {
    return (
      <div className="status status--down">
        <span className="dot dot--down" /> backend unreachable — {error}
      </div>
    )
  }
  if (!health) {
    return (
      <div className="status">
        <span className="dot" /> checking backend…
      </div>
    )
  }

  const degraded = !health.mcp_reachable
  return (
    <div className={degraded ? 'status status--degraded' : 'status status--up'}>
      <span className={degraded ? 'dot dot--degraded' : 'dot dot--up'} />
      <span>
        <strong>{health.provider}</strong>
        {' · '}
        {health.native_tools ? 'native tool calling' : 'JSON planner fallback'}
        {' · MCP '}
        {degraded ? 'unreachable' : 'reachable'}
        <span className="status__url"> ({health.mcp_server_url})</span>
      </span>
      {degradations.length > 0 && (
        <span className="status__degradations" title={degradations.join('\n')}>
          {degradations.length} degradation{degradations.length === 1 ? '' : 's'}
        </span>
      )}
    </div>
  )
}
