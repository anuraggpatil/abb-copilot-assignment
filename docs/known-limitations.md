# Known limitations

What this submission does not do, does not prove, or does with a caveat. Ordered by how much it
would matter to someone relying on the copilot, not by how easy it would be to fix.

---

## Retrieval quality

### Similarity cannot tell "related" from "applicable"

`RAG_MIN_RELEVANCE=0.60` separates on-topic questions (0.663–0.820 on this corpus) from off-topic
ones (0.406–0.535). It does not separate *adjacent* domains: "What is the turbine blade inspection
interval for a gas turbine?" scores **0.688** against the centrifugal-pump maintenance manual —
above anything the on-topic distribution allows the threshold to be raised to. The retrieved section
genuinely is about inspection intervals; it is about the wrong machine.

Raising the threshold is the wrong fix and would start refusing real questions. This is asserted as
a characterisation test — `test_a_topically_adjacent_question_is_not_caught_by_the_threshold` — so
that it cannot be "cleaned up" by moving the number. What mitigates it is downstream and already
built: every claim carries a checkable citation, and each document declares an `applies_to` scope
the reader can see. What would actually fix it is a scope check against the asset in the question,
comparing `applies_to` with the asset metadata `search_assets` already returns.

### No reranker

This is the **highest-value single addition** to the retrieval stack. Ranking is reciprocal-rank
fusion over dense and BM25 rank lists, which knows nothing about the query beyond rank agreement.
Two measured artefacts of that, both documented with numbers in
[`rag-design.md`](rag-design.md):

- a chunk that only BM25 ranked first enters the top-5 on rank agreement alone, despite a weak
  dense score;
- a chunk that dense ranked first is demoted by a poor lexical rank.

A cross-encoder over the fused top-20 — `bge-reranker-base` scoring `(query, chunk)` pairs directly
— would fix both, and would additionally give an absolute relevance signal good enough to replace
the cosine threshold in §10 of [`design-decisions.md`](design-decisions.md). It was cut for time and
because it adds a second model download to a machine where the first one was already difficult.

### Pinned references crowd out search at `RAG_TOP_K=5`

When `get_operator_recommendations` cites three sections and those are passed as `references`, three
of five slots are filled before ranking runs — so a search that would have surfaced a fourth,
genuinely relevant passage returns two results instead. The pinned sections are almost always the
right ones, which is why this is tolerable, but "pinned" and "ranked" are competing for one budget
when they should have separate ones. The fix is a separate cap per selection mode.

### The corpus is four documents

Four authored documents, 74 chunks. It is deliberately written to cover the acceptance scenario, so
retrieval on that scenario is a favourable test. Nothing here demonstrates behaviour at ten thousand
chunks: BM25 is an in-memory index rebuilt at startup, and `RAG_CANDIDATES=20` was tuned at this
scale (see the RRF `k` derivation — `k=2` is correct for depth 20 and would need raising with a
larger candidate pool).

## Index lifecycle

### Refresh is a full rebuild, and it is not atomic

`make ingest` deletes the Qdrant collection and recreates it. There is no content hashing, so an
unchanged document is re-embedded on every run, and there is no versioned collection alias, so
between the delete and the last upsert a concurrently running retriever sees a partial index. At 74
chunks the window is about a second and a rebuild is cheap, so neither costs anything here. Both
would matter at production scale, and both are standard: hash each chunk's `embedding_text()` to
skip unchanged work, and build into `alarm_procedures_v<n>` then move an alias.

### Embedded Qdrant holds an exclusive directory lock

One process at a time may open `.qdrant`. `make ingest` and a running backend cannot both hold it,
and the failure message is about a lock file rather than about what to do. Within the backend,
retrieval is serialised behind a lock and run in a worker thread. This is the correct tradeoff for a
local demo — no server to run — and is exactly what the `RAG_QDRANT_PATH` / client seam exists to
let you change.

## Environment and packaging

### Docker is written but unverified

`docker` is not installed on the build machine. The `Dockerfile`s and `docker-compose.yml` are
written against the assignment's own compose contract, with health checks and dependency ordering,
and they are **not known to build or run**. Treat them as a packaging proposal. Every service has a
verified local path — `make api`, `make mcp`, `make backend`, `make frontend`, or `make dev` for all
four at once — and that is what the README quick-start documents and what the demo video shows.

### The MCP port is 9100, not the 9000 the assignment's compose file names

On this macOS image Microsoft OneDrive/Teams holds `127.0.0.1:9000`, and the symptom is an opaque
"address already in use" followed by a content-type error from the smoke script rather than a
connection refusal. The default moved to **9100** in every place that names it — `.env.example`,
the `McpSettings` default, `MCP_SERVER_URL`, the `Makefile`, `docker-compose.yml` and `EXPOSE` — so
a reviewer does not have to hit that once to learn it. Two consequences worth knowing:

- It is a documented deviation from the assignment's `MCP_SERVER_URL=http://alarm-mcp:9000`. Inside
  the compose network 9000 would have been free; the container follows the host default anyway so
  the repo holds exactly one number.
- The coupling itself is still a rough edge. Moving the port again means moving three things
  together — `MCP_SERVER_PORT`, `MCP_SERVER_URL` and the `--port` flag (`make mcp MCP_PORT=…`) — or
  the server binds one port while the client dials another. Nothing detects that mismatch; it
  surfaces as a failed tool call.

### The embedding weights need a workaround behind TLS interception

`fastembed` downloads `bge-small-en-v1.5` from HuggingFace on first use. Behind a TLS-inspecting
proxy (Zscaler here) OpenSSL 3 rejects the intercepted certificate and every Python HTTPS request
fails, so that download cannot happen in-process. `make fetch-model` fetches the weights out of band
and `RAG_EMBED_MODEL_PATH` points the embedder at them. It works, but it is an extra manual step a
reviewer on a clean network does not need and will not discover from the error.

`RAG_EMBEDDER=hash` is the fallback: deterministic, offline, no weights — and **lexical overlap
only**, so it supports no claim about semantic retrieval. Its score distribution does not separate
on- from off-topic questions the way the real model's does, which is why the fixture suite in
`tests/conftest.py` runs at `RAG_MIN_RELEVANCE=0.50` rather than production's 0.60. A threshold that
has to change with the embedder is a sign the number belongs to the model, not to the corpus.

Consequence for a reviewer: `rag/tests/test_retrieval_semantic.py` — the module holding every
semantic-quality and calibration claim — **skips** without the weights. A skipped test proves
nothing. The module prints the measured numbers on failure and names `make fetch-model` in its skip
reason, but the honest statement is that the quality claims in `rag-design.md` are reproducible
rather than continuously verified.

### `httpx` and `httpx2` are both installed

`mcp` 2.x depends on `httpx2`; `respx` and FastAPI's `TestClient` target `httpx` 1.x. Both are in the
lockfile and the connector deliberately uses `httpx`. Nothing is broken and the boundary is clean —
the MCP client and the alarm connector are different packages — but two HTTP stacks in one
environment is a thing to know before debugging a connection.

### The ONNX shutdown abort

ONNX Runtime's inference session owns a native thread pool whose destructor, if it runs during
interpreter finalisation, can abort the process *after* everything has succeeded — the observed
symptom was `pytest` printing "676 passed" and then exiting 134. `FastEmbedEmbedder.close()` drops
the session while the interpreter is still healthy, and the tests that build an index call it in a
`finally`. Fixed, but it constrains how the embedder may be used: a code path that constructs one
and drops it on the floor can bring this back.

## Coverage of the source system

### Seven of fifteen endpoints are not implemented

```
POST /alarms/trends                      POST /alarms/priority-score
POST /alarms/correlation                 POST /calculation-code/generate
POST /alarms/flood-analysis              POST /calculation-code/execute
POST /alarms/rationalization-candidates
```

A deliberate cut when the time box tightened, on the guidelines' own statement that a smaller
fully-integrated solution beats a broad incomplete one. `/alarms/correlation` is the one whose
absence costs something real: contributing factors are currently reasoned from
`get_recurring_alarms` plus the troubleshooting guide rather than from a computed correlation, so
"likely contributing factors" in the acceptance scenario is an inference over a recurrence pattern,
not a statistical result. Details in [`api-integration.md`](api-integration.md).

`/alarms/recurring` is the reverse case — an endpoint **we added**, because the scenario's word
*recurring* has nothing behind it otherwise. It is documented as ours rather than presented as part
of the given contract.

### The source system is a simulator

`apps/alarm_api` stands in for the real Alarm Management API, which is not reachable from this
machine. Everything above it is written against the documented contract and pinned by
`tests/integration/test_postman_contract.py`, which replays the collection. Two properties exist for
the demo rather than for realism: the dataset is generated relative to `datetime.now(UTC)`, and
Boiler Feed Pump 101 carries a planted recurring pattern that the corpus explains. The copilot has
never been run against the real API, and `ALARM_API_BASE_URL` is the only thing that would change.

## Orchestration and operations

### The real model's tool selection is not covered by a test

`tests/e2e` runs every layer for real except the model, which is scripted — deliberately, so the
headline test is deterministic and runnable without a key. The consequence is direct: **nothing in
`make test` proves a real model picks the right tools.** That is what `make smoke`
(`scripts/live_smoke.py`, marked `live`) and the demo video are for, and both need a valid
`GEMINI_API_KEY`.

Related: `LLM_NATIVE_TOOLS` defaults to `true` because Gemini documents function calling, but the
JSON-planner path is the one the test suite exercises end to end. Both are implemented and unit
tested; `make probe` is what confirms which one this network and this key actually support.

### The live Gemini path could not be exercised from the machine this was built on

The honest version of the item above. The corporate network here blocks Google's Generative
Language API outright — Zscaler returns a block page ("Cloud App Category: AI & ML Applications",
policy `CD02`) on every request, 8 attempts out of 8, including plain `curl`. So while the request
shape, the schema translation, the retry behaviour and the reply parsing are all covered by 66 unit
tests against a mock transport, **no question has been answered by a real Gemini model from this
laptop.**

Three consequences worth stating plainly:

- `make probe` and `make smoke` will both fail here with a proxy error, not a Gemini error. The
  provider detects exactly this case and says so — a non-JSON body on an API endpoint means
  something between the process and Google answered — rather than failing with a `KeyError` three
  lines later.
- On an unfiltered network (home, or a personal hotspot) the same commands should work with a free
  AI Studio key and nothing else. The verified shape of a working call is in
  `scripts/probe_llm.py`; the one live exchange that *did* get through, before the policy was
  tightened, is what the schema translator was written against.
- `LLM_PROVIDER=scripted` keeps the whole app — GUI, MCP, RAG, trace — usable with no model at
  all. That is how the acceptance scenario is demonstrated when the network refuses.

### The free tier's limits are the copilot's limits

An AI Studio key is rate-limited per minute and per day, and answers `503 This model is currently
experiencing high demand` under load. `LLM_MAX_RETRIES=2` absorbs a single refusal per call; a
demo that asks eight questions in a minute may still see one fail, and the trace will say which
status it was. A paid key or a different provider is a configuration change, not a code change.

One gotcha worth knowing before it wastes an hour: the 2.5-series models spend output tokens on
hidden reasoning before writing anything, so an answer can come back **empty** with
`finishReason: MAX_TOKENS` even though the budget looked generous. Set `GEMINI_THINKING_BUDGET=0`.
The stop reason is surfaced in the trace panel precisely so this is diagnosable.

### A session per MCP request

The backend opens and closes an MCP session per request rather than holding one open, to avoid
owning an anyio task-group scope across the FastAPI lifespan. The cost is one connection setup per
question — tens of milliseconds against a multi-second investigation — and it would be the wrong
tradeoff against a remote MCP server over a slow link. Reasoning in §7 of
[`design-decisions.md`](design-decisions.md).

### The trace store is in memory

`TRACE_RETENTION=50` conversations, oldest evicted, cleared on restart. `GET /trace/{id}` is
unauthenticated, which is why redaction happens when an event is recorded rather than when it is
served. For anything beyond a local demo this needs a real store and an authorisation check.

### Conversation memory is in memory too, and deliberately shallow

`CONVERSATION_TURNS=4` turns per conversation, 50 conversations, oldest evicted, cleared on
restart — the same fragility as the trace store, and for the same reason.

Two of its limits are choices rather than omissions. What is remembered is the *delivered* turn
(question, answer, citations), not the planning transcript: tool payloads are large, and re-feeding
passages that were already citation-checked invites the model to quote memory instead of evidence.
And the history is injected as a system message labelled "context, not evidence", so a follow-up
re-runs its own tools rather than inheriting the earlier answer's findings. The cost is real work
repeated — asking about the same pump twice calls the same tools twice. The benefit is that no claim
can reach the operator sourced from a previous answer rather than from a document or an API.

Beyond four turns the oldest is dropped with no summarisation, so a long thread loses its opening.
There is no cross-process sharing either: two backend replicas behind a load balancer would each
hold half of a conversation.

### Single user, no rate limiting, no auth on the copilot

The backend has no authentication, no per-user quota and no limit on concurrent investigations. One
question can issue up to `ORCHESTRATOR_MAX_STEPS=8` planning rounds of tool calls against a metered
API. The loop is bounded, which prevents one runaway question; nothing bounds a hundred of them.

### The KPI board shows what the model asked for, and its colours are not a plant standard

Two separate gaps, both in the header above the answer.

The board is assembled from whatever tools the model chose to call. Broadening
`get_alarm_summary`'s default KPI set means *that* tool always returns eight figures, but nothing
makes the model call it: an investigation that goes straight to `get_recurring_alarms` produces a
board of patterns and no volume tiles. The alternative — running a summary call unconditionally on
every question — would put a fixed step back into a loop whose whole design is that the chain is
the model's, and would bill a tool call against questions that are purely about documentation. So
the board is honest about being partial rather than being made complete by force.

The tile colours come from thresholds in `kpis.py::_TONE` — 15 minutes of acknowledgement delay
reads amber, 60 reads red. They are a reading of what a control room cares about, not a figure from
an alarm-management standard such as EEMUA 191 or ISA-18.2, and a real deployment would take them
from the plant's own alarm philosophy. They are confined to presentation: no colour changes the
answer text, adds a caveat or reaches the model, and every tile prints its value and unit beside
the colour.

### Latency is not optimised

Tool calls within a planning step run sequentially, and retrieval is serialised behind a lock. A
full acceptance-scenario investigation takes several seconds, most of it in the model. Streaming the
trace is what makes that acceptable to watch rather than making it faster.

## Fixed, but worth knowing

### SSE framing

sse-starlette separates events with `\r\n\r\n`. A parser splitting on `'\n\n'` — the obvious
implementation, and the one written first — finds no event boundary at all: a blank screen, no
error, and every backend test still green. Both parsers now split on `/\r\n\r\n|\n\n|\r\r/` and
`/\r\n|\n|\r/`. Recorded here because it is the clearest example of why the e2e layer asserts
against the server's actual bytes rather than against a reconstruction of them.

### The secret scan reported "clean" without having scanned anything

`check_secrets.sh` finds candidates with `git grep`, which is the right choice — it reads *tracked*
content, so a local gitignored `.env` can never trip it. But `git grep` exits `1` for "no matches"
and `128` when git itself fails, and the original wrapped it in `if hits=$(… 2>/dev/null)`, which
treats both identically. Run anywhere without a work tree — an extracted archive, a checkout whose
`.git` was stripped — it scanned zero files, printed *"Secret scan clean"* and exited `0`.

Found by running the submission folder as a reviewer would, not by a test. A gate that fails open is
worse than no gate, because CI is green either way and nobody looks again. It now refuses with exit
`2` outside a work tree, and a `git grep` exit other than `0` or `1` fails the run naming the
pattern that went unchecked rather than passing quietly.

### The `doc_types` enum disagreed with the corpus

`search_procedures` offered `safety_instruction` while `SAF-pump-isolation-loto.md` declares
`safety_procedure`. Because filters apply after retrieval, filtering to the safety document returned
an empty result indistinguishable from "the corpus does not cover this" — and the value that would
have worked was rejected by schema validation. The enum is now derived from a `DOC_TYPES` constant
and `tests/unit/test_procedures.py` asserts it equals the set the corpus declares, in both
directions. Found while writing [`mcp-tool-catalog.md`](mcp-tool-catalog.md).
