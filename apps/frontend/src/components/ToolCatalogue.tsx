/**
 * The catalogue the planner is handed, exactly as `GET /tools` returns it.
 *
 * It is on screen because the `backend` column is the architecture in one word per row: the alarm
 * tools say `mcp` — they are discovered from the running MCP server, never called directly — and
 * `search_procedures` says `local`. One catalogue, two backends, one planning loop. When MCP is
 * down, the `mcp` rows disappear and a degradation note explains why; that is the degraded demo.
 */

import { useState } from 'react'

import type { ToolDescriptor } from '../types'

interface Props {
  tools: ToolDescriptor[]
  degradations: string[]
  error: string | null
}

export default function ToolCatalogue({ tools, degradations, error }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null)

  return (
    <section className="panel">
      <header className="panel__head">
        <h2>Tool catalogue</h2>
        <span className="panel__meta">{tools.length} available</span>
      </header>

      {error && <div className="notice notice--warn">Could not load the catalogue: {error}</div>}

      {degradations.map((note) => (
        <div key={note} className="notice notice--warn">
          {note}
        </div>
      ))}

      {tools.length === 0 && !error ? (
        <p className="empty">No tools were discovered.</p>
      ) : (
        <ul className="tools">
          {tools.map((tool) => (
            <li key={tool.name}>
              <button
                className="tools__row"
                type="button"
                aria-expanded={expanded === tool.name}
                onClick={() => setExpanded((current) => (current === tool.name ? null : tool.name))}
              >
                <code>{tool.name}</code>
                <span className={`tag tag--${tool.backend}`}>{tool.backend}</span>
                <span className="tools__title">{tool.title}</span>
              </button>
              {expanded === tool.name && (
                <div className="tools__detail">
                  <p>{tool.description}</p>
                  <pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
