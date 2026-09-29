/**
 * The backend client. One module, so every fetch in the app has the same error behaviour.
 *
 * **Why `fetch` and not `EventSource` for the stream.** `EventSource` can only issue a GET,
 * and asking a question is a POST with a JSON body. Making `/chat` a GET with the question in
 * the query string to suit the browser API would put operator text in server access logs and
 * cap it at the URL length. So the SSE framing is parsed here instead: it is about twenty
 * lines, and it buys the thing `EventSource` cannot do — an abortable POST.
 *
 * The parser is deliberately strict about only one thing: it splits on a blank line, which is
 * what separates SSE events, and keeps the remainder buffered. A naive implementation that
 * assumes one chunk per event works locally and then drops events under load, which would look
 * like the orchestrator skipping steps.
 *
 * The blank line may be `\r\n\r\n`, `\n\n` or `\r\r` — the spec allows all three, and our server
 * (sse-starlette) emits the first. Splitting on `'\n\n'` alone finds no boundary at all in a
 * CRLF stream, which is a blank screen rather than a visible error; `tests/e2e` asserts against
 * the real framing for that reason.
 */

import type {
  Catalogue,
  ConversationTrace,
  CopilotAnswer,
  Health,
  TraceEvent,
} from './types'

const BASE = (import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8080').replace(/\/$/, '')

/** A failed request, carrying the status so callers can distinguish "not started" from "broke". */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, init)
  } catch {
    // A network-level failure, which in this app almost always means the backend is not
    // running. The message says so, because "Failed to fetch" sends people to the wrong place.
    throw new ApiError(
      `Could not reach the copilot backend at ${BASE}. Start it with \`make backend\`.`,
      null,
    )
  }
  if (!response.ok) {
    throw new ApiError(await describe(response), response.status)
  }
  return (await response.json()) as T
}

/** FastAPI puts a string in `detail` for our raised errors and a list for validation ones. */
async function describe(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') return body.detail
    if (body.detail) return JSON.stringify(body.detail)
  } catch {
    // fall through to the status line
  }
  return `${response.status} ${response.statusText}`
}

export function health(): Promise<Health> {
  return getJson<Health>('/health')
}

export function catalogue(): Promise<Catalogue> {
  return getJson<Catalogue>('/tools')
}

export function trace(conversationId: string): Promise<ConversationTrace> {
  return getJson<ConversationTrace>(`/trace/${encodeURIComponent(conversationId)}`)
}

export interface AskHandlers {
  onTrace: (event: TraceEvent) => void
  onAnswer: (answer: CopilotAnswer) => void
}

export interface AskOptions {
  /**
   * The conversation this question continues. Omitted for the first question of a thread; the
   * backend mints an id and returns it on the answer, and every later question in the thread
   * sends it back — which is what makes the exchange a conversation rather than a series of
   * unrelated questions. The server holds the history; the client only holds the id.
   */
  conversationId?: string | null
}

/**
 * Ask a question, streaming each traced step as the orchestrator records it.
 *
 * Resolves when the `answer` event has been delivered. Rejects with an `ApiError` if the
 * backend refuses the request or sends an `error` event — the stream's own error channel, which
 * is how a failed orchestration arrives once the response has already begun with a 200.
 */
export async function ask(
  question: string,
  handlers: AskHandlers,
  signal?: AbortSignal,
  options: AskOptions = {},
): Promise<void> {
  let response: Response
  try {
    response = await fetch(`${BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      // Omitted, not sent empty: "" is a valid string and would key a conversation whose id is
      // the empty string, shared by every first question the process ever serves.
      body: JSON.stringify(
        options.conversationId ? { question, conversation_id: options.conversationId } : { question },
      ),
      signal,
    })
  } catch (cause) {
    if (signal?.aborted) throw cause
    throw new ApiError(
      `Could not reach the copilot backend at ${BASE}. Start it with \`make backend\`.`,
      null,
    )
  }

  if (!response.ok) throw new ApiError(await describe(response), response.status)
  if (!response.body) throw new ApiError('the backend returned no stream', response.status)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let streamError: string | null = null

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // Events are separated by a blank line; anything after the last one is a partial event
      // and stays in the buffer until its remainder arrives. A boundary that straddles two
      // chunks simply is not matched yet, which is the behaviour we want.
      let boundary = FRAME_BOUNDARY.exec(buffer)
      while (boundary) {
        const frame = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        const parsed = parseFrame(frame)
        if (parsed) {
          if (parsed.event === 'trace') handlers.onTrace(parsed.data as TraceEvent)
          else if (parsed.event === 'answer') handlers.onAnswer(parsed.data as CopilotAnswer)
          else if (parsed.event === 'error') {
            streamError = String((parsed.data as { error?: unknown }).error ?? 'unknown failure')
          }
        }
        boundary = FRAME_BOUNDARY.exec(buffer)
      }
    }
  } finally {
    reader.releaseLock()
  }

  if (streamError) throw new ApiError(streamError, 502)
}

interface Frame {
  event: string
  data: unknown
}

/** The blank line between events, in any of the three spellings the SSE spec permits. */
const FRAME_BOUNDARY = /\r\n\r\n|\n\n|\r\r/

function parseFrame(frame: string): Frame | null {
  let event = 'message'
  const dataLines: string[] = []

  for (const line of frame.split(/\r\n|\n|\r/)) {
    if (line.startsWith(':')) continue // a comment, used as a keep-alive
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }
  if (dataLines.length === 0) return null

  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    // A frame we cannot parse is dropped rather than thrown: losing one trace row is a much
    // better outcome than abandoning an investigation that is otherwise succeeding.
    return null
  }
}
