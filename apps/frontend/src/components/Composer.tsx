/**
 * The message box at the foot of the chat: type, send, stop, or start a new conversation.
 *
 * It clears itself on send, which is the one behavioural difference from the form it replaces and
 * the reason this is a chat: the previous question is in the thread above, so leaving it in the
 * box would only invite it to be sent twice.
 *
 * The suggestion chips appear only *inside* a conversation, and they are follow-ups: questions that
 * only mean something in context ("and what about pump 102?"). That is both the fastest way to show
 * the copilot carries context and the fastest way to catch it if it stops. Openers are not here —
 * the opening screen offers those as example questions, and having both was the same three questions
 * twice on one screen.
 */

import { useEffect, useRef, useState } from 'react'
import type { SyntheticEvent } from 'react'

const FOLLOW_UPS: ReadonlyArray<{ label: string; question: string }> = [
  { label: 'The other pump', question: 'And what about Boiler Feed Pump 102 over the same window?' },
  { label: 'Isolation', question: 'Which procedure covers isolating it before that work, and what does it require?' },
  { label: 'First action', question: 'Of those actions, which one should the operator do first, and why?' },
]

interface Props {
  busy: boolean
  /** True once this thread has at least one turn: switches the chips to follow-ups. */
  inConversation: boolean
  onAsk: (question: string) => void
  onCancel: () => void
  onReset: () => void
}

export default function Composer({ busy, inConversation, onAsk, onCancel, onReset }: Props) {
  // Empty, always. This box used to open pre-filled with the 180-character acceptance scenario,
  // which saved the demo some typing and cost every other visitor a wall of text to read and delete
  // before they could ask their own question. The opening screen offers that question as a one-click
  // example, so nothing is lost by leaving the box to the operator.
  const [question, setQuestion] = useState('')
  const input = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    if (!busy) input.current?.focus()
  }, [busy, inConversation])

  function submit(event: SyntheticEvent) {
    event.preventDefault()
    const trimmed = question.trim()
    if (trimmed.length < 3 || busy) return
    onAsk(trimmed)
    setQuestion('')
  }

  return (
    <form className="composer" onSubmit={submit}>
      {inConversation && (
        <div className="composer__chips">
          <span className="composer__chiplabel">Follow-ups</span>
          {FOLLOW_UPS.map((suggestion) => (
            <button
              key={suggestion.label}
              className="button button--chip"
              type="button"
              disabled={busy}
              title={suggestion.question}
              onClick={() => {
                setQuestion(suggestion.question)
                input.current?.focus()
              }}
            >
              {suggestion.label}
            </button>
          ))}
          <button className="button button--link composer__new" type="button" onClick={onReset}>
            New conversation
          </button>
        </div>
      )}

      <div className="composer__row">
        <label className="visually-hidden" htmlFor="question">
          Message the copilot
        </label>
        <textarea
          id="question"
          ref={input}
          className="composer__input"
          rows={3}
          value={question}
          disabled={busy}
          placeholder={
            inConversation
              ? 'Ask a follow-up — "and what about pump 102?" resolves against the turns above'
              : 'e.g. Investigate recurring high-severity alarms for Boiler Feed Pump 101…'
          }
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            // Enter sends, Shift+Enter makes a newline. The questions are long enough that the
            // box has to keep its newlines.
            if (event.key === 'Enter' && !event.shiftKey) submit(event)
          }}
        />
        <div className="composer__actions">
          {/* Icon-only, so the glyph is hidden from assistive tech and the name comes from
              `aria-label` — which is also what the tests select it by, so the button cannot be
              relabelled without them noticing. */}
          <button
            className="button button--primary button--send"
            type="submit"
            disabled={busy}
            aria-label={busy ? 'Investigating…' : 'Send'}
            title={busy ? 'Investigating…' : 'Send (Enter)'}
          >
            <span aria-hidden="true">{busy ? '⋯' : '↑'}</span>
          </button>
          {busy && (
            <button className="button" type="button" onClick={onCancel}>
              Stop
            </button>
          )}
        </div>
      </div>
      {/* Keystrokes only. That a follow-up can refer back to the thread is something the placeholder
          says at the moment it becomes true, and is not worth a standing paragraph. */}
      <p className="composer__hint">Enter sends · Shift+Enter for a new line</p>
    </form>
  )
}
