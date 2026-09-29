/**
 * The conversation, driven the way an operator drives it.
 *
 * Only `./api` is mocked. Everything else is the real component tree, so these tests fail if the
 * thread stops rendering a turn, if the composer stops clearing, or — the one that matters — if
 * the `conversation_id` stops being sent back with the second question, which is the whole
 * difference between a chat and a form that forgets. That last one cannot be caught by a backend
 * test: the server will happily start a new conversation for every question if the client never
 * tells it otherwise.
 */

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import * as api from './api'
import type { AskHandlers, AskOptions } from './api'
import type { CopilotAnswer, TraceEvent } from './types'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    health: vi.fn(),
    catalogue: vi.fn(),
    trace: vi.fn(),
    ask: vi.fn(),
  }
})

function answerOf(overrides: Partial<CopilotAnswer> = {}): CopilotAnswer {
  return {
    conversation_id: 'conv-abc123',
    request_id: 'req-1',
    trace_id: 'trc-1',
    question: 'q',
    answer: 'The pump is cavitating [OP-BFP-101 §4.2].',
    citations: [],
    caveats: [],
    invented_references: [],
    low_confidence: false,
    steps_used: 2,
    steps_exhausted: false,
    tool_calls: [{ name: 'search_assets', backend: 'mcp', ok: true, duration_ms: 12, error_kind: null }],
    model: 'system.ai.claude-sonnet-5',
    history_turns: 0,
    ...overrides,
  }
}

function eventOf(seq: number, name: string): TraceEvent {
  return {
    event_id: `evt-${seq}`,
    conversation_id: 'conv-abc123',
    request_id: 'req-1',
    trace_id: 'trc-1',
    seq,
    kind: 'mcp_tool_call',
    name,
    started_at: '2026-09-29T10:00:00Z',
    duration_ms: 12,
    status: 'ok',
    summary: 'returned 1 row',
    detail: {},
  }
}

/** Records the options each `ask` was called with, and replies with the given answers in order. */
function scriptAsk(answers: CopilotAnswer[]): AskOptions[] {
  const seen: AskOptions[] = []
  let turn = 0
  vi.mocked(api.ask).mockImplementation(
    async (_question: string, handlers: AskHandlers, _signal, options: AskOptions = {}) => {
      seen.push(options)
      const answer = answers[Math.min(turn, answers.length - 1)]
      turn += 1
      handlers.onTrace(eventOf(1, 'search_assets'))
      if (answer) handlers.onAnswer(answer)
    },
  )
  return seen
}

beforeEach(() => {
  vi.mocked(api.health).mockResolvedValue({
    status: 'ok',
    provider: 'gemini',
    native_tools: false,
    mcp_reachable: true,
    mcp_server_url: 'http://localhost:9100/mcp',
  })
  vi.mocked(api.catalogue).mockResolvedValue({ tools: [], degradations: [] })
})

async function send(user: ReturnType<typeof userEvent.setup>, text: string) {
  const box = screen.getByLabelText('Message the copilot')
  await user.clear(box)
  await user.type(box, text)
  await user.click(screen.getByRole('button', { name: 'Send' }))
}

describe('the conversation', () => {
  it('opens with example questions and an empty box', async () => {
    render(<App />)

    expect(await screen.findByText(/What would you like to look into/)).toBeInTheDocument()
    expect(screen.getByText(/not started/)).toBeInTheDocument()
    // No pre-filled wall of text: the examples are one click, the box is the operator's.
    expect(screen.getByLabelText('Message the copilot')).toHaveValue('')
  })

  // The opening screen's whole job is to get a first question asked, so clicking an example has to
  // ask it — not fill the box and wait for a second click.
  it('asks an example question when it is clicked', async () => {
    const seen = scriptAsk([answerOf()])
    const user = userEvent.setup()
    render(<App />)

    await user.click(
      await screen.findByRole('button', { name: /Why does Boiler Feed Pump 101 keep raising/ }),
    )

    expect(await screen.findByText(/The pump is cavitating/)).toBeInTheDocument()
    expect(seen).toEqual([{ conversationId: null }])
  })

  it('renders the question and the answer as a turn', async () => {
    scriptAsk([answerOf()])
    const user = userEvent.setup()
    render(<App />)

    await send(user, 'What is happening on Boiler Feed Pump 101?')

    expect(await screen.findByText('What is happening on Boiler Feed Pump 101?')).toBeInTheDocument()
    expect(await screen.findByText(/The pump is cavitating/)).toBeInTheDocument()
    expect(screen.getByText('You · turn 1')).toBeInTheDocument()
    // The id the server minted is shown, because it is what a follow-up is keyed on.
    expect(screen.getByText('conv-abc123')).toBeInTheDocument()
  })

  it('sends the conversation id back with the second question', async () => {
    const seen = scriptAsk([answerOf(), answerOf({ history_turns: 1, answer: 'Pump 102 is quiet.' })])
    const user = userEvent.setup()
    render(<App />)

    await send(user, 'What is happening on Boiler Feed Pump 101?')
    await screen.findByText(/The pump is cavitating/)
    await send(user, 'And what about the other pump?')
    await screen.findByText(/Pump 102 is quiet/)

    // The first question opens a conversation; every later one continues it. This is the contract
    // the backend's conversation memory is keyed on.
    expect(seen).toEqual([{ conversationId: null }, { conversationId: 'conv-abc123' }])
  })

  it('says when a turn was answered with earlier turns in context', async () => {
    scriptAsk([answerOf(), answerOf({ history_turns: 1, answer: 'Pump 102 is quiet.' })])
    const user = userEvent.setup()
    render(<App />)

    await send(user, 'first')
    await screen.findByText(/The pump is cavitating/)
    await send(user, 'second')

    expect(await screen.findByText('in context of 1 earlier turn')).toBeInTheDocument()
  })

  it('keeps both turns in the thread, oldest first', async () => {
    scriptAsk([answerOf(), answerOf({ history_turns: 1, answer: 'Pump 102 is quiet.' })])
    const user = userEvent.setup()
    render(<App />)

    await send(user, 'first question')
    await screen.findByText(/The pump is cavitating/)
    await send(user, 'second question')
    await screen.findByText(/Pump 102 is quiet/)

    const turns = screen.getAllByRole('listitem').filter((node) => node.className === 'chat__turn')
    expect(turns).toHaveLength(2)
    expect(turns[0]).toHaveTextContent('first question')
    expect(turns[1]).toHaveTextContent('second question')
  })

  it('clears the box and offers follow-ups once a conversation exists', async () => {
    scriptAsk([answerOf()])
    const user = userEvent.setup()
    render(<App />)

    await send(user, 'What is happening on Boiler Feed Pump 101?')

    await waitFor(() => expect(screen.getByLabelText('Message the copilot')).toHaveValue(''))
    expect(screen.getByText('Follow-ups')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'The other pump' })).toBeInTheDocument()
  })

  it('starts a new conversation without the old id', async () => {
    const seen = scriptAsk([answerOf(), answerOf()])
    const user = userEvent.setup()
    render(<App />)

    await send(user, 'first')
    await screen.findByText(/The pump is cavitating/)
    await user.click(screen.getByRole('button', { name: 'New conversation' }))

    expect(await screen.findByText(/What would you like to look into/)).toBeInTheDocument()
    await send(user, 'unrelated question')
    await waitFor(() => expect(seen).toHaveLength(2))
    // Not the old id: the previous thread's history must not leak into a fresh one.
    expect(seen[1]).toEqual({ conversationId: null })
  })

  it('shows the failure on the turn it belongs to and keeps the earlier ones', async () => {
    const user = userEvent.setup()
    vi.mocked(api.ask)
      .mockImplementationOnce(async (_q, handlers) => {
        handlers.onAnswer(answerOf())
      })
      .mockImplementationOnce(async () => {
        throw new api.ApiError('the planner could not run', 502)
      })
    render(<App />)

    await send(user, 'first')
    await screen.findByText(/The pump is cavitating/)
    await send(user, 'second')

    expect(await screen.findByText(/the planner could not run \(502\)/)).toBeInTheDocument()
    // The first answer is still there — a failed follow-up must not take the thread with it.
    expect(screen.getByText(/The pump is cavitating/)).toBeInTheDocument()
  })
})

describe('the trace beside the conversation', () => {
  it('follows the newest turn and can be pointed back at an earlier one', async () => {
    scriptAsk([answerOf(), answerOf({ history_turns: 1, answer: 'Pump 102 is quiet.' })])
    const user = userEvent.setup()
    render(<App />)

    await send(user, 'first')
    await screen.findByText(/The pump is cavitating/)
    await send(user, 'second')
    await screen.findByText(/Pump 102 is quiet/)

    // The newest turn is selected without a click, which is what makes a running investigation
    // watchable; the earlier one is one click away.
    const turns = screen.getAllByRole('listitem').filter((node) => node.className === 'chat__turn')
    expect(within(turns[1] as HTMLElement).getByRole('button', { name: 'Trace shown →' })).toBeInTheDocument()

    await user.click(within(turns[0] as HTMLElement).getByRole('button', { name: 'Show trace' }))

    expect(
      within(turns[0] as HTMLElement).getByRole('button', { name: 'Trace shown →' }),
    ).toBeInTheDocument()
  })
})
