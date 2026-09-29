/**
 * The evidence panel: every document section the copilot was given, and whether it used it.
 *
 * Two rendering rules carry most of the honesty of this screen:
 *
 * 1. `relevance: null` is printed as "cited upstream", not as a number. Those sections were
 *    fetched because an API recommendation named them, so they never went through the retriever
 *    and have no score. Rendering a missing score as 0.000 would put the most authoritative
 *    evidence at the bottom of the list looking irrelevant.
 * 2. Sections that were retrieved but *not* cited are still shown, greyed. Hiding them would
 *    turn the low-confidence case into a blank panel, when what an operator needs to know is
 *    "these four documents were searched and none of them covers your question".
 */

import type { Citation } from '../types'

interface Props {
  citations: Citation[]
}

export default function CitationList({ citations }: Props) {
  if (citations.length === 0) {
    return <p className="empty">No document evidence was retrieved for this question.</p>
  }

  // Cited first, then by score. Stable within each group, so the retriever's own order shows.
  const ordered = [...citations].sort((a, b) => {
    if (a.cited_in_answer !== b.cited_in_answer) return a.cited_in_answer ? -1 : 1
    return (b.relevance ?? Number.POSITIVE_INFINITY) - (a.relevance ?? Number.POSITIVE_INFINITY)
  })

  return (
    <ul className="citations">
      {ordered.map((citation) => (
        <li
          key={citation.reference}
          className={citation.cited_in_answer ? 'citation' : 'citation citation--unused'}
        >
          <div className="citation__head">
            <code className="citation__ref">{citation.reference}</code>
            <span className="citation__score">
              {citation.relevance === null ? 'cited upstream' : citation.relevance.toFixed(3)}
            </span>
            {!citation.cited_in_answer && <span className="tag">retrieved, not cited</span>}
          </div>
          <div className="citation__where">
            {citation.document} · rev {citation.revision} · {citation.section}
          </div>
          <blockquote className="citation__quote">{citation.quote}</blockquote>
          <div className="citation__how">selected by {citation.selected_by}</div>
        </li>
      ))}
    </ul>
  )
}
