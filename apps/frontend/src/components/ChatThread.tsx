/**
 * The conversation: every turn asked and answered, oldest at the top.
 *
 * A turn is not a chat bubble. The question is one — but the reply is a KPI board, a caveated
 * answer with citations, a tool list and a trace, and shrinking that into a speech balloon would
 * hide the evidence the assignment asks to be shown. So each turn renders as the question, then
 * the full answer surface beneath it, and the thread is the sequence of those.
 *
 * **Why the trace is selected rather than inlined.** Each turn has its own trace, and the panel
 * for it lives in the right-hand column where a long one can scroll independently. Inlining
 * fifteen expandable steps per turn would push the next question off the screen. The footer of
 * each turn says how many steps it took and selects that turn's trace into the panel; the newest
 * turn is selected automatically, so watching a fresh investigation needs no clicks.
 *
 * Auto-scroll follows the newest turn only while the reader is already at the bottom. Yanking the
 * view back down while someone is reading an earlier answer is the behaviour that makes streaming
 * chat UIs unusable, and a trace event arrives every few hundred milliseconds.
 */

import { useEffect, useRef } from 'react'

import type { CopilotAnswer, TraceEvent } from '../types'
import AnswerPanel from './AnswerPanel'
import KpiBoard from './KpiBoard'

export interface ChatTurn {
  id: string
  question: string
  status: 'running' | 'done' | 'error' | 'cancelled'
  events: TraceEvent[]
  answer: CopilotAnswer | null
  failure: string | null
}

/* The opening screen, phrased as questions an operator would actually ask rather than as a tour of
 * the architecture. The first is the assignment's acceptance scenario in plain words; the second
 * needs only the document index; the third is answerable by nothing here, and the honest "no
 * documented evidence" reply to it is worth seeing. Clicking one sends it — the fastest way from
 * "what can I ask?" to an answer. */
const EXAMPLES: readonly string[] = [
  'Why does Boiler Feed Pump 101 keep raising high-severity alarms, and what should we do about it?',
  'What does the procedure say about low suction pressure on a boiler feed pump?',
  'Why did the cooling tower fan on Rig 7 trip last night?',
]

interface Props {
  turns: ChatTurn[]
  selectedId: string | null
  onSelect: (id: string) => void
  /** Sends an example question from the opening screen. Same path as the composer's chips. */
  onAsk: (question: string) => void
}

export default function ChatThread({ turns, selectedId, onSelect, onAsk }: Props) {
  const scroller = useRef<HTMLDivElement | null>(null)
  const pinned = useRef(true)

  // The last thing that changed, cheaply: a new turn, or a new event inside the running one.
  const tail = turns.at(-1)
  const progress = `${turns.length}:${tail?.events.length ?? 0}:${tail?.status ?? ''}`

  useEffect(() => {
    const element = scroller.current
    if (element && pinned.current) element.scrollTop = element.scrollHeight
  }, [progress])

  function onScroll() {
    const element = scroller.current
    if (!element) return
    // 48px of slack: an exact comparison unpins the view on a one-pixel rounding difference,
    // which reads as auto-scroll randomly not working.
    pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48
  }

  if (turns.length === 0) {
    return (
      <div className="chat__scroll chat__scroll--empty">
        <div className="chat__hello">
          <h2>What would you like to look into?</h2>
          <p>Ask about an asset&rsquo;s alarms, or about what a procedure says:</p>
          <ul className="chat__examples">
            {EXAMPLES.map((example) => (
              <li key={example}>
                <button className="chat__example" type="button" onClick={() => onAsk(example)}>
                  {example}
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    )
  }

  return (
    <div className="chat__scroll" ref={scroller} onScroll={onScroll}>
      <ol className="chat__turns">
        {turns.map((turn, index) => (
          <li className="chat__turn" key={turn.id}>
            <div className="chat__ask">
              <span className="chat__who">
                You · turn {index + 1}
              </span>
              <p className="chat__question">{turn.question}</p>
            </div>

            {turn.status === 'running' && (
              <div className="notice notice--info chat__working">
                <span className="chat__spinner" aria-hidden="true" />
                Investigating — {turn.events.length === 0
                  ? 'discovering the tool catalogue'
                  : `${turn.events.length} step${turn.events.length === 1 ? '' : 's'} so far: ${
                      turn.events.at(-1)?.name ?? ''
                    }`}
              </div>
            )}

            {turn.status === 'cancelled' && !turn.answer && (
              <div className="notice">Stopped before an answer was written.</div>
            )}

            {turn.status === 'error' && turn.failure && (
              <div className="notice notice--error">
                <strong>The investigation failed.</strong> {turn.failure}
              </div>
            )}

            {turn.answer?.kpis && <KpiBoard kpis={turn.answer.kpis} />}
            {turn.answer && <AnswerPanel answer={turn.answer} />}

            <footer className="chat__foot">
              <button
                className={
                  selectedId === turn.id
                    ? 'button button--chip button--chip-on'
                    : 'button button--chip'
                }
                type="button"
                onClick={() => onSelect(turn.id)}
              >
                {selectedId === turn.id ? 'Trace shown →' : 'Show trace'}
              </button>
              <span className="chat__footmeta">
                {turn.events.length} traced step{turn.events.length === 1 ? '' : 's'}
              </span>
              {/* Absent on a first turn, and absent if the backend ever stops sending it — which
                  is the point: silence here means the turn was answered cold. */}
              {!!turn.answer?.history_turns && (
                <span className="chat__context" title="Earlier turns of this conversation were in front of the model, as context — not as evidence. Every figure was re-established by calling the tools again.">
                  in context of {turn.answer.history_turns} earlier turn
                  {turn.answer.history_turns === 1 ? '' : 's'}
                </span>
              )}
            </footer>
          </li>
        ))}
      </ol>
    </div>
  )
}
