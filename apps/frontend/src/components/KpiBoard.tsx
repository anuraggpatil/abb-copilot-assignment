/**
 * The KPI header: the figures an operator reads before the prose.
 *
 * Everything rendered here was computed by the alarm API and extracted server-side in
 * `apps/backend/orchestration/kpis.py`. This component formats and nothing else — no arithmetic,
 * no derived rates, no defaults. That is deliberate: a number the browser worked out could
 * disagree with the same number in the answer beside it, and the operator has no way to tell
 * which one to act on.
 *
 * It renders nothing at all when no tool returned figures. An empty board is the honest output
 * for a documentation-only question or a degraded run where MCP was unreachable; a board of
 * zeroes would read as "checked, all clear".
 */

import type { InvestigationKpis, Kpi, PatternKpi } from '../types'

interface Props {
  kpis: InvestigationKpis
}

export default function KpiBoard({ kpis }: Props) {
  const tiles = withTotal(kpis)
  if (tiles.length === 0 && kpis.patterns.length === 0) return null

  return (
    <section className="panel kpis">
      <header className="panel__head">
        <h2>Alarm KPIs</h2>
        <span className="panel__meta">
          {kpis.asset_name && <span className="kpis__asset">{kpis.asset_name}</span>}
          {kpis.window_days !== null && <span>last {kpis.window_days} days</span>}
          {windowLabel(kpis) && <span className="muted">{windowLabel(kpis)}</span>}
        </span>
      </header>

      {tiles.length > 0 && (
        <div className="kpis__grid">
          {tiles.map((kpi) => (
            <Tile key={kpi.key} kpi={kpi} />
          ))}
        </div>
      )}

      {kpis.patterns.length > 0 && <Patterns patterns={kpis.patterns} />}

      {kpis.sources.length > 0 && (
        // Named so a reviewer can point at a tile and then at the trace row that produced it.
        <p className="kpis__sources">
          from{' '}
          {kpis.sources.map((source, index) => (
            <span key={source}>
              {index > 0 && ', '}
              <code>{source}</code>
            </span>
          ))}
        </p>
      )}
    </section>
  )
}

/**
 * The tiles, with the alarm total promoted to one when no KPI already reports it.
 *
 * `get_alarms` returns `total_matching` but computes no KPIs, so an investigation that read
 * events without summarising them would otherwise carry a count the GUI never showed. It is
 * labelled differently from the summary's `alarm_count` on purpose: this one is the total matching
 * *that call's* filters, not the whole window, and presenting the two under one label would
 * invite a comparison that is not valid.
 */
function withTotal(kpis: InvestigationKpis): Kpi[] {
  if (kpis.total_alarms === null || kpis.metrics.some((kpi) => kpi.key === 'alarm_count')) {
    return kpis.metrics
  }
  return [
    {
      key: 'total_alarms',
      label: 'Alarms matched',
      value: kpis.total_alarms,
      unit: 'count',
      tone: 'neutral',
      hint: 'Alarms matching the filters of the call that returned them.',
    },
    ...kpis.metrics,
  ]
}

function Tile({ kpi }: { kpi: Kpi }) {
  return (
    <div className={`kpi kpi--${kpi.tone}`} title={kpi.hint || undefined}>
      <span className="kpi__label">{kpi.label}</span>
      <span className="kpi__value">
        {formatValue(kpi)}
        {suffix(kpi.unit) && <span className="kpi__unit">{suffix(kpi.unit)}</span>}
      </span>
    </div>
  )
}

function Patterns({ patterns }: { patterns: PatternKpi[] }) {
  return (
    <>
      <h3>Recurring patterns</h3>
      <div className="kpis__tablewrap">
        <table className="kpis__table">
          <thead>
            <tr>
              <th>Alarm</th>
              <th>Occurrences</th>
              <th>Trend</th>
              <th>First half → second</th>
              <th>Max severity</th>
              <th>Chattering</th>
            </tr>
          </thead>
          <tbody>
            {patterns.map((pattern) => (
              <tr key={`${pattern.alarm_name}-${pattern.asset_name}`}>
                <td>{pattern.alarm_name}</td>
                <td className="kpis__num">{pattern.occurrences}</td>
                <td>
                  <span className={`trend trend--${pattern.trend || 'unknown'}`}>
                    {/* Arrow and word together: the arrow alone is unreadable on a projector and
                        invisible to a screen reader. */}
                    {ARROW[pattern.trend] ?? ''} {pattern.trend || '—'}
                  </span>
                </td>
                <td className="kpis__num">{halves(pattern)}</td>
                <td>
                  <span className={`sev sev--${pattern.max_severity || 'unknown'}`}>
                    {pattern.max_severity || '—'}
                  </span>
                </td>
                <td className="kpis__num">
                  {pattern.chattering_share === null
                    ? '—'
                    : `${percent(pattern.chattering_share)}%`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

const ARROW: Record<string, string> = {
  increasing: '▲',
  flat: '▬',
  decreasing: '▼',
}

function halves(pattern: PatternKpi): string {
  if (pattern.occurrences_first_half === null || pattern.occurrences_second_half === null) return '—'
  return `${pattern.occurrences_first_half} → ${pattern.occurrences_second_half}`
}

function formatValue(kpi: Kpi): string {
  if (kpi.unit === 'ratio') return percent(kpi.value)
  if (kpi.unit === 'minutes') return kpi.value >= 10 ? Math.round(kpi.value).toString() : kpi.value.toFixed(1)
  // A count is an integer upstream; `toLocaleString` only adds the thousands separator that
  // makes 1204 legible at a glance.
  return Number.isInteger(kpi.value) ? kpi.value.toLocaleString() : kpi.value.toFixed(2)
}

function suffix(unit: string): string {
  if (unit === 'ratio') return '%'
  if (unit === 'minutes') return 'min'
  return ''
}

function percent(ratio: number): string {
  const shown = ratio * 100
  // Below 10% a whole number loses the difference between 1% and 9%-of-a-small-set, and above it
  // the decimal is false precision on a 90-day count.
  return shown < 10 && shown > 0 ? shown.toFixed(1) : Math.round(shown).toString()
}

function windowLabel(kpis: InvestigationKpis): string {
  if (!kpis.window_start || !kpis.window_end) return ''
  const start = new Date(kpis.window_start)
  const end = new Date(kpis.window_end)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return ''
  return `${day(start)} – ${day(end)}`
}

function day(value: Date): string {
  return value.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}
