/**
 * The whole screen: the conversation on the left, the trace of the selected turn on the right,
 * status across the top.
 *
 * State is a handful of `useState` hooks and an `AbortController` — no store, no router, no query
 * library. There is one page, and the interesting complexity of this app is in the backend's
 * orchestration, not in the client's state management. A reviewer should be able to read this file
 * top to bottom and know what the GUI can do.
 *
 * **It is a conversation, and the server is what remembers it.** The client holds the turns so it
 * can draw them, and one `conversation_id`: minted by the backend on the first answer and sent
 * with every question after it. The history that the model is shown never travels through the
 * browser — which keeps one copy of it, server-side, and means a reload cannot silently change
 * what the copilot "remembers". `conversationRef` mirrors the id because a question can be sent
 * from the same tick an answer arrives in, and a stale closure there would quietly start a second
 * conversation.
 *
 * **The layout is deliberate.** The trace sits *beside* the thread rather than behind a tab,
 * because the assignment asks for the MCP execution trace to be visible, and a tab nobody clicks
 * is not visible. Each turn has its own trace; the newest is selected as it runs, and any earlier
 * turn can be selected back into the panel.
 *
 * **The screen is one viewport and never grows.** The shell is a `100dvh` grid — brand bar, then
 * the two columns — and everything that can outgrow its share scrolls inside itself: the thread,
 * the trace column, each JSON block. So the health badge and the composer are always where they
 * were, which is what a control-room screen has to do and what a page scroll takes away. The CSS
 * carries the rules; there are no heights in this file.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import ChatThread from './components/ChatThread'
import type { ChatTurn } from './components/ChatThread'
import Composer from './components/Composer'
import HealthBadge from './components/HealthBadge'
import RunMetrics from './components/RunMetrics'
import ToolCatalogue from './components/ToolCatalogue'
import TracePanel from './components/TracePanel'
import { ApiError, ask, catalogue, health } from './api'
import type { Catalogue, Health, TraceEvent } from './types'

export default function App() {
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [conversationId, setConversationId] = useState<string | null>(null)

  const [backend, setBackend] = useState<Health | null>(null)
  const [backendError, setBackendError] = useState<string | null>(null)
  const [tools, setTools] = useState<Catalogue>({ tools: [], degradations: [] })
  const [toolsError, setToolsError] = useState<string | null>(null)

  const abort = useRef<AbortController | null>(null)
  const conversationRef = useRef<string | null>(null)

  const refreshStatus = useCallback(async () => {
    try {
      setBackend(await health())
      setBackendError(null)
    } catch (error) {
      setBackend(null)
      setBackendError(message(error))
    }
    try {
      setTools(await catalogue())
      setToolsError(null)
    } catch (error) {
      setTools({ tools: [], degradations: [] })
      setToolsError(message(error))
    }
  }, [])

  // On load, and again after every question: MCP reachability is checked live on each `/health`,
  // so re-reading it after a run is how the badge notices that the server went away mid-demo.
  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  useEffect(() => () => abort.current?.abort(), [])

  const update = useCallback((id: string, change: (turn: ChatTurn) => ChatTurn) => {
    setTurns((current) => current.map((turn) => (turn.id === id ? change(turn) : turn)))
  }, [])

  async function onAsk(question: string) {
    abort.current?.abort()
    const controller = new AbortController()
    abort.current = controller

    const id = `turn-${turns.length + 1}-${Date.now()}`
    setTurns((current) => [
      ...current,
      { id, question, status: 'running', events: [], answer: null, failure: null },
    ])
    setSelectedId(id)

    try {
      await ask(
        question,
        {
          // Appended, never replaced: the stream is the only place these arrive in order, and
          // re-sorting them client-side would hide an ordering bug rather than show it.
          onTrace: (event: TraceEvent) =>
            update(id, (turn) => ({ ...turn, events: [...turn.events, event] })),
          onAnswer: (answer) => {
            conversationRef.current = answer.conversation_id
            setConversationId(answer.conversation_id)
            update(id, (turn) => ({ ...turn, answer }))
          },
        },
        controller.signal,
        { conversationId: conversationRef.current },
      )
      update(id, (turn) => ({ ...turn, status: 'done' }))
    } catch (error) {
      if (controller.signal.aborted) {
        update(id, (turn) => ({ ...turn, status: 'cancelled' }))
      } else {
        update(id, (turn) => ({ ...turn, status: 'error', failure: message(error) }))
      }
    } finally {
      if (abort.current === controller) abort.current = null
      void refreshStatus()
    }
  }

  function onReset() {
    abort.current?.abort()
    setTurns([])
    setSelectedId(null)
    setConversationId(null)
    // The server's copy of the history is keyed by the old id and simply stops being asked for;
    // it ages out of a bounded store. Nothing is deleted from here, because a client being able
    // to reach into server-side state by id is a hole, not a feature.
    conversationRef.current = null
  }

  const running = turns.some((turn) => turn.status === 'running')
  const selected = turns.find((turn) => turn.id === selectedId) ?? turns.at(-1) ?? null

  return (
    <div className="app">
      <header className="app__head">
        <div>
          <h1>Alarm Investigation &amp; Procedure Guidance Copilot</h1>
          <p className="app__sub">
            Alarm data is reached only through the MCP server; procedures come from the document
            index. Every step is traced.
          </p>
        </div>
        <HealthBadge health={backend} error={backendError} degradations={tools.degradations} />
      </header>

      <div className="app__columns">
        <div className="app__column chat">
          <div className="chat__head">
            <h2>Conversation</h2>
            <span className="panel__meta">
              {conversationId ? (
                <>
                  <code>{conversationId}</code> · {turns.length} turn
                  {turns.length === 1 ? '' : 's'}
                </>
              ) : (
                'not started'
              )}
            </span>
          </div>

          <ChatThread
            turns={turns}
            selectedId={selected?.id ?? null}
            onSelect={setSelectedId}
            onAsk={onAsk}
          />

          <div className="chat__composer">
            <Composer
              busy={running}
              inConversation={turns.length > 0}
              onAsk={onAsk}
              onCancel={() => abort.current?.abort()}
              onReset={onReset}
            />
          </div>
        </div>

        <div className="app__column app__column--side">
          <RunMetrics answer={selected?.answer ?? null} events={selected?.events ?? []} />
          <TracePanel
            events={selected?.events ?? []}
            running={selected?.status === 'running'}
          />
          <ToolCatalogue tools={tools.tools} degradations={tools.degradations} error={toolsError} />
        </div>
      </div>
    </div>
  )
}

function message(error: unknown): string {
  if (error instanceof ApiError)
    return error.status ? `${error.message} (${error.status})` : error.message
  if (error instanceof Error) return error.message
  return String(error)
}
