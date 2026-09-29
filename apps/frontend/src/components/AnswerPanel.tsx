/**
 * The answer, and everything that qualifies it.
 *
 * The answer text is rendered as markdown *elements* — never `dangerouslySetInnerHTML`, which
 * appears nowhere in this codebase. It is model output partly derived from retrieved documents,
 * which is the exact path a prompt-injection payload travels; the backend defends against the model
 * *obeying* an injected instruction, and the browser must not let one *execute*. See
 * `AnswerMarkdown` for how that line is held while still formatting the headings, bullets and
 * tables the synthesis prompt asks for — including why images and links are stripped.
 *
 * The qualifiers are not footnotes. `caveats`, `low_confidence`, `invented_references` and
 * `steps_exhausted` are the difference between a copilot an operator can trust and one that
 * sounds equally confident when it has nothing. They are rendered above the answer, not below it.
 */

import type { CopilotAnswer } from '../types'
import AnswerMarkdown from './AnswerMarkdown'
import CitationList from './CitationList'

interface Props {
  answer: CopilotAnswer
}

export default function AnswerPanel({ answer }: Props) {
  return (
    <section className="panel">
      <header className="panel__head">
        <h2>Answer</h2>
        <span className="panel__meta">
          {answer.model} · {answer.steps_used} step{answer.steps_used === 1 ? '' : 's'}
        </span>
      </header>

      {answer.low_confidence && (
        <div className="notice notice--warn">
          Low confidence: no retrieved document cleared the relevance threshold, so this answer is
          not backed by documented evidence.
        </div>
      )}

      {answer.steps_exhausted && (
        <div className="notice notice--warn">
          The step budget was exhausted before the investigation finished — the answer is based on
          what had been gathered by then.
        </div>
      )}

      {answer.invented_references.length > 0 && (
        <div className="notice notice--warn">
          Unsupported references were removed from the answer text:{' '}
          {answer.invented_references.map((reference) => (
            <code key={reference}>{reference}</code>
          ))}
        </div>
      )}

      {answer.caveats.length > 0 && (
        <ul className="notice notice--info">
          {answer.caveats.map((caveat) => (
            <li key={caveat}>{caveat}</li>
          ))}
        </ul>
      )}

      <AnswerMarkdown>{answer.answer}</AnswerMarkdown>

      <h3>Evidence</h3>
      <CitationList citations={answer.citations} />

      <h3>Tools used</h3>
      {answer.tool_calls.length === 0 ? (
        <p className="empty">No tools were called.</p>
      ) : (
        <ul className="toolcalls">
          {answer.tool_calls.map((call, index) => (
            <li key={`${call.name}-${index}`} className={call.ok ? '' : 'toolcalls--failed'}>
              <code>{call.name}</code>
              <span className={`tag tag--${call.backend}`}>{call.backend}</span>
              <span className="toolcalls__ms">{Math.round(call.duration_ms)} ms</span>
              {!call.ok && <span className="tag tag--error">{call.error_kind ?? 'failed'}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
