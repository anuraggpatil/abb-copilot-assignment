# RAG design

Every number in this document was measured on the committed corpus with
`BAAI/bge-small-en-v1.5`, not estimated. The commands that produce them are at the bottom.

---

## 1. Source document types

Four authored documents in `rag/documents/`, one per `doc_type` the retrieval filter accepts:

| File | `doc_id` | `doc_type` | Rev | Role in the scenario |
|---|---|---|---|---|
| `OP-BFP-101-operating-procedure.md` | `OP-BFP-101` | `operating_procedure` | 7 | **The procedure the acceptance scenario must retrieve.** Startup, controlled shutdown, low-suction-pressure response, high-vibration response, lube oil system |
| `TS-BFP-vibration-cavitation.md` | `TS-BFP-VIB-CAV` | `troubleshooting_guide` | 4 | Symptom → cause: low suction pressure → cavitation → vibration → bearing wear. Supplies the *contributing factors* half of the answer |
| `MM-centrifugal-pump-maintenance.md` | `MM-CP-MAINT` | `maintenance_manual` | 11 | Bearing lubrication intervals, oil analysis, alignment tolerances, failure reporting |
| `SAF-pump-isolation-loto.md` | `SAF-PUMP-LOTO` | `safety_procedure` | 9 | Lockout/tagout, and "do not restart a tripped pump" — the section the API's recommendations cite |

The corpus is **synthetic and written for this assignment**, per §8 of the guidelines: no real
plant documentation is committed. It is written deliberately to explain the *planted* alarm
pattern on Boiler Feed Pump 101, so the step the workflow calls "compare API recommendations with
document guidance" has real substance rather than two unrelated texts placed side by side.

74 chunks across the four documents.

## 2. Ingestion flow

```
make ingest      →  python -m rag.ingestion.build_index
```

```
rag/documents/*.md
   │
   ├─ loader.py      read file, split YAML frontmatter, validate required keys
   │                 └─ sanitizer.py  strip model-directed instructions, record findings
   ├─ chunker.py     section-aligned chunks with heading trails
   ├─ embedder.py    bge-small-en-v1.5 → 384-d vectors
   └─ store.py       delete + recreate the Qdrant collection, upsert all points
```

It prints a report rather than running silently, because the two things most likely to be wrong
about an index are invisible otherwise: which documents went in, and what the sanitiser took out
of them. A sanitiser finding on the *authored* corpus means a false positive is redacting real
procedure text, and that has to be seen. Exit code is 1 on any failure, so a broken ingest fails a
build rather than leaving a stale index in place.

A document that fails to load is fatal by design. A corpus silently missing one procedure answers
safety questions from an incomplete index with no indication that anything is missing.

## 3. Text extraction

Markdown with YAML frontmatter, read as UTF-8 — no PDF or Office extraction layer, and that is a
deliberate scope choice rather than an omission. Extraction from PDF is where most of the failure
modes of a real ingestion pipeline live (columns interleaved, tables flattened, headers repeated
into the body), and none of those failures would have been *visible* in this assignment while all
of them would have consumed the budget. Markdown makes the section structure explicit, which is
what the rest of the design depends on. A real deployment would add an extraction stage in front
of `loader.py`; nothing downstream would change, because everything downstream consumes
`SourceDocument`.

Frontmatter is **required, not inferred**. Deriving `doc_id` from the filename works until a file
is renamed, at which point every stored `chunk_id` changes and the index silently no longer matches
the references the alarm API emits. Declaring the id in the file makes that impossible.

## 4. Chunking strategy

**One chunk belongs to exactly one numbered section.** That constraint drives everything else,
and it comes from the citation requirement: the alarm API cites sections (`OP-BFP-101 §4.2`), the
answer must quote the section it cited, and a chunk spanning §4.2 into §4.3 can be attributed to
neither. A citation naming the wrong section is worse than no citation — it sends somebody to the
wrong page of a procedure during an alarm.

So sections are the primary unit and the token budget is secondary:

- Headings matching `## §N Title` open a section; a long section splits into several chunks that
  all carry the same `SectionRef`; a short section stays whole even though it wastes budget.
- **Sections are never merged**, even when two adjacent ones would fit comfortably together.
- `RAG_CHUNK_SIZE=380` tokens target, `RAG_CHUNK_MIN_TOKENS=40`, `RAG_CHUNK_OVERLAP_SENTENCES=1`.
  The token count is estimated at 4 characters per token — deliberately an estimate, because the
  budget decides *where* to split and is not a hard limit that must be exact.
- **Tables and numbered steps are kept intact.** Half a setpoint table is actively misleading: an
  operator shown rows 1–3 of a table whose row 5 is the trip limit has been given a wrong answer,
  not a partial one. A table that cannot fit the budget gets its own oversized chunk.
- **The heading trail is carried and embedded.** `Chunk.embedding_text()` prepends
  `doc_title > … > section`. A chunk beginning "Above 1.0 bar differential, switch to the standby
  element" is about lube oil filters, but nothing in those words says so; without the trail it is
  unfindable by the question it answers.

## 5. Chunk metadata

Stored as the Qdrant point payload, so retrieval reconstructs a citable chunk without re-reading
the file (`rag/models.py::Chunk`):

| Field | Purpose |
|---|---|
| `chunk_id` | `MM-CP-MAINT#7.1#0` — doc, section, ordinal. Stable across re-ingestion |
| `doc_id`, `doc_title`, `doc_type`, `revision` | Provenance shown in every citation |
| `section` | `SectionRef(doc_id, number, title)` — kept *split*, not just formatted, so an API reference can be matched on `(doc_id, number)` rather than by string search |
| `heading_path` | Root-to-section trail, embedded with the text |
| `text`, `ordinal`, `token_estimate` | The content and its position within a multi-chunk section |
| `safety_critical` | From frontmatter; a safety document's passages are marked as such |
| `source_path` | Repo-relative path, so a citation can be opened |

Document-level frontmatter also carries `effective_date`, `owner`, `applies_to` and any other
keys verbatim in `metadata`. `applies_to` matters beyond bookkeeping — see the applicability
limitation in §13.

`SectionRef.covers()` treats `§4` as covering `§4.2`, because a recommendation may cite the parent
section while the answer lives in a subsection, or the reverse.

## 6. Embedding model

`BAAI/bge-small-en-v1.5` via fastembed, 384 dimensions, ONNX on CPU — no GPU, no API call, no
per-query cost. Chosen because it is small enough to run on a laptop inside a test suite, strong
enough on short technical prose, and Apache-2.0.

Two things are configurable for reasons that turned out to matter:

- `RAG_EMBED_MODEL_PATH` loads the weights from a local directory instead of downloading them.
  Needed wherever HuggingFace is unreachable — this project was developed behind a Zscaler TLS
  intercept that OpenSSL 3 rejects. `make fetch-model` populates it without disabling certificate
  verification; see `scripts/fetch_embedding_model.py`.
- `RAG_EMBEDDER=hash` is a deterministic offline stand-in with no weights and no download, so a
  fresh checkout's tests are green without a 50 MB fetch. It is **not** equivalent: its score
  distribution overlaps between on- and off-topic text, so no relevance threshold separates them.
  Tests that assert on *scores* use the real model and are the ones that keep §7's threshold
  honest.

## 7. Vector database / index

**Embedded Qdrant** — `qdrant-client` in path mode, writing to `.qdrant/`. No server, no
container, no Docker requirement for the RAG layer to work. Cosine distance over 384-d vectors.

BM25 (`rank-bm25`) is built in the same process over the same chunk list, so the two retrievers
never disagree about what is in the corpus.

`build()` **replaces** the collection rather than upserting into it. An incremental update leaves
behind chunks from sections that have since been deleted or renumbered, and a citation pointing at
a section that no longer exists is worse than a slower ingest.

Embedded Qdrant holds an **exclusive lock** on its directory, so a process that does not close the
index leaves the next one failing in a way that looks like corruption. `index.close()` is in a
`finally`, and the copilot service closes what it is handed on shutdown.

## 8. Hybrid search

Both retrievers run on every query and return `RAG_CANDIDATES=20` candidates each — wider than
`top_k` on purpose, because reciprocal-rank fusion can only promote a chunk at least one retriever
returned.

They are indexed on deliberately different text. `embedding_text()` omits the document id:
`OP-BFP-101` carries no meaning a sentence embedder can use and would occupy budget in all 74
vectors. `lexical_text()` puts the full reference first, because the id is exactly what a lexical
query needs — the alarm API cites `OP-BFP-101 §4.2` and an operator types `OP-BFP-101`, and with
only the document *title* indexed BM25 could match neither.

## 9. Ranking and fusion

```
score(chunk) = Σ over retrievers of 1 / (k + rank)
```

**Why RRF and not a weighted sum.** Cosine similarity lands in roughly 0.3–0.9; BM25 is unbounded
and depends on corpus statistics and query length, so the same query scores 4 on one corpus and 22
on another. Any `α·dense + β·lexical` first needs both onto a common scale, and every way of doing
that (min-max over the candidate set, z-scores) makes the weights depend on the result set being
normalised. RRF discards magnitudes and keeps ranks: no weights to tune, and stable when one
retriever returns nothing.

**`RAG_RRF_K=2`, not the canonical 60.** 60 comes from the paper that introduced RRF, tuned on
TREC runs 1000 candidates deep, where `1/(k+rank)` spans 1/61…1/1060 — a 17× range, so rank
discriminates strongly. Transplanted onto a list 20 deep it spans 1/61…1/80, a 1.3× range, which
flattens rank almost completely and degenerates RRF into counting how many retrievers returned the
chunk. That was measurably wrong here: BM25's top 20 is 27% of a 74-chunk corpus, so its membership
is close to noise, yet it was worth up to a doubling — the chunk with the *highest* cosine for "is
it safe to keep running the pump while it is cavitating" ranked last, behind a lexical match about
strainers. At `k=2` the range is 1/3…1/22 (7.3×), the canonical value scaled by the same ratio to
candidate depth. Top-1 accuracy on the eight-question set went from 6/8 to 7/8, and fused scores
now spread across 0.34–0.88 instead of clustering near 0.9.

**Reference pinning.** `search(..., references=[...])` pulls named sections in directly instead of
hoping search lands on them. This is the seam the combined workflow runs through: the alarm API's
`get_operator_recommendations` returns `procedure_references`, and those exact strings are passed
here. A pinned chunk gets `pinned=True` and, if neither retriever independently returned it,
`scores.synthetic=True` with a fused score of 1.0 — flagged, because an *assigned* 1.0 would
otherwise be indistinguishable from the strongest real hit in the set. At most two chunks are
admitted per reference, so one long cited section cannot fill `top_k` with itself.

Nothing is re-ranked by a cross-encoder. That is a considered omission: a reranker is the single
highest-value addition to this design, and it is in [`known-limitations.md`](known-limitations.md)
with the reason it was not attempted inside the time box.

## 10. Retrieval filters

| Filter | Effect |
|---|---|
| `doc_ids` (≤10) | Restrict to specific documents |
| `doc_types` (≤4) | `operating_procedure` \| `troubleshooting_guide` \| `maintenance_manual` \| `safety_procedure` — the values the corpus declares, asserted against the frontmatter in `tests/unit/test_procedures.py` |
| `references` (≤10) | Pin named sections (above) |
| `top_k` (1–8) | Passages returned; default `RAG_TOP_K=5` |

Filters are applied **after** retrieval, over the fused candidate list, and the applied filters are
echoed into the trace so the panel shows what the search was actually allowed to see.

## 11. Citation construction

One `Citation` per returned chunk (`rag/models.py::Citation`):

```json
{
  "reference": "OP-BFP-101 §4.4 High vibration response",
  "doc_id": "OP-BFP-101",
  "doc_title": "Boiler Feed Pump 101 — Operating Procedure",
  "revision": "7",
  "section_number": "4.4",
  "section_title": "High vibration response",
  "quote": "Check lube oil pressure, temperature and filter differential (§5.1). Loss of oil film\n   produces rising vibration and rising bearing temperature together, and it is\n   independently correctable.\n4. Check bearing metal temperatures.",
  "chunk_id": "OP-BFP-101#4.4#0",
  "source_path": "rag/documents/OP-BFP-101-operating-procedure.md",
  "score": 0.7142857142857143,
  "selected_by": "search"
}
```

- `reference` is formatted **exactly** the way the alarm API formats its references, which is what
  lets an API recommendation and a retrieved passage be recognised as the same section.
- `quote` is a verbatim span of at most 320 characters, selected around the query terms. Verbatim
  because a citation the reader cannot check against the document is decoration; capped because a
  citation must not become a channel for dumping a whole document into the transcript.
- `revision` is carried so an answer cites *revision 7* of a procedure, not "the procedure".
- `score` is `null` for a pinned section and `selected_by` is `"reference"`. Putting the internal
  1.0 there would render in the GUI as the most relevant passage found; `null` plus `selected_by`
  says what actually happened — something named this text, and no similarity search corroborated
  it. The GUI renders that as "cited upstream".

The copilot then adds one more check the retrieval layer cannot: after synthesis, every `[DOC §N]`
marker in the answer is resolved against the citations actually returned, and anything that
resolves to nothing is recorded in `invented_references` and neutralised before the answer is
shown.

## 12. Low-confidence handling

Ranking and abstention are different questions answered from different numbers.

**RRF cannot support a quality threshold.** It never sees whether the top cosine was 0.91 or 0.41 —
whatever ranks first scores identically either way. So confidence is decided on the **best dense
cosine** against `RAG_MIN_RELEVANCE=0.60`. BM25 is deliberately not consulted: its scores do not
separate on-topic from off-topic text ("what is the capital of France?" outscores "cavitation" on
BM25, because the stopwords match plenty of chunks), so letting it participate would let a stopword
match rescue a decision that should fail.

**The threshold is measured, not chosen.** On this corpus with bge-small-en-v1.5, eight
operator-phrased questions span 0.663–0.820 and four out-of-domain ones span 0.406–0.535. 0.60 is
the midpoint of that gap — the same ~0.06 of margin on each side rather than crowding one. The
numbers are asserted in `rag/tests/test_retrieval_semantic.py`, so this stays a measurement rather
than becoming folklore.

When nothing clears it, the candidates are **still returned** with a note, because showing an
operator what was found and that it was thin beats an empty response:

```
query: "what is the capital of France"
low_confidence: true   top_relevance: 0.447
note: "Closest passage scored 0.45 similarity, below the 0.60 threshold. The passages below
       are the nearest found, but the indexed procedures do not appear to cover this
       question — treat them as leads, not as documented guidance."
```

The synthesis prompt is then required to take the "insufficient documented evidence" path, and the
GUI shows the notice above the answer. A reference that was asked for by name and cannot be found
is reported in `unresolved_references` rather than dropped, so the copilot can say the cited
procedure is missing instead of quietly answering from something else.

## 13. Prompt-injection protection

Two layers, because neither alone is sufficient.

**Layer 1 — ingestion (`rag/ingestion/sanitizer.py`).** Patterns that only ever appear in an
injection attempt are removed before anything is indexed, and each removal leaves a visible
marker (`[redacted: instruction-like content removed at ingestion]`) and a recorded finding in the
ingest report. This is the stronger control: text that was never stored cannot be retrieved.

It is deliberately blunt — it matches imperative phrasings aimed at a model, not meaning, so it
cannot be complete and a novel phrasing will pass. What it must *not* do is mangle legitimate
procedure text, which is full of imperatives ("do not restart", "ignore the reading if it
disagrees"). So every pattern requires a model-directed subject: `your instructions`,
`system prompt`, `you are an`. `SAF-PUMP-LOTO §2.1` says "do not restart" and a test asserts it
survives verbatim.

**Layer 2 — prompt assembly (`apps/backend/orchestration/`).** What survives is wrapped in a
delimited `<document id=…>` block declared *untrusted reference data*, and the system prompt
forbids following instructions found inside one. The model-facing passage carries an explicit
`"trusted": false`. Instructions found in a document are to be *reported*, never obeyed.

**Layer 3 — execution.** Every tool call is checked against the catalogue by name and validated
against its schema before it runs, so retrieved text cannot invent a tool or a parameter even if a
model repeated one.

A deliberately poisoned fixture in `rag/tests/fixtures/` backs tests that the instruction is
stripped at ingestion and ignored end to end.

## 14. Index refresh

The index is a build artefact: `.qdrant/` is gitignored and `make ingest` recreates it from
`rag/documents/` in about a second for this corpus. Refresh is therefore a full rebuild, for the
reason in §7 — a partial refresh can leave a citation pointing at a section that no longer exists.

- After editing or adding a document: `make ingest`.
- After changing `RAG_CHUNK_*`, the embedder, or `RAG_EMBED_MODEL`: `make ingest` is **required**.
  Vectors from a different model are not comparable, and a stale collection built at a different
  chunk size will retrieve text that no longer matches its stored metadata.
- Tests never use the on-disk index; each builds its own in memory, which is why the suite is not
  affected by whatever state `.qdrant/` is in.

What a production deployment would need and this does not have: content hashing per document so an
unchanged corpus skips re-embedding, and a versioned collection alias so a rebuild is atomic
instead of leaving a window where the collection is missing. Both are in
[`known-limitations.md`](known-limitations.md).

---

## Example retrieved chunks

Real output, produced by the commands below. `dense` is cosine similarity with its rank, `lexical`
is BM25 with its rank, `fused` is the RRF score that decided the order.

**Query:** *"what should an operator do when boiler feed pump suction pressure falls below the
alarm setpoint"* — 28 candidates, `top_relevance` 0.776, `low_confidence` false

| Reference | dense | lexical | fused |
|---|---|---|---|
| `OP-BFP-101 §4.2 Low suction pressure response` | 0.776 (r1) | 12.61 (r2) | 0.8750 |
| `OP-BFP-101 §3.2 Controlled shutdown on an abnormal condition` | 0.763 (r5) | 12.37 (r3) | 0.5143 |
| `MM-CP-MAINT §6.2 Rationalization candidates` | — | 14.41 (r1) | 0.5000 |
| `TS-BFP-VIB-CAV §3.1 Why low suction pressure produces vibration` | 0.758 (r6) | 12.04 (r4) | 0.4375 |
| `TS-BFP-VIB-CAV §1 Purpose and how to use this guide` | 0.767 (r3) | 8.69 (r17) | 0.3789 |

The section the scenario needs ranks first, found independently by both retrievers. Row 3 shows
RRF's honest cost: a chunk no dense search returned still reaches the list on a BM25 rank-1 alone.
Row 5 shows the reverse — a strong dense hit demoted by a weak lexical rank. Both are why
reranking is the top item in `known-limitations.md`.

**Query:** the acceptance scenario's, with `references` from the API's recommendation —
`["SAF-PUMP-LOTO §2 Before any intervention", "MM-CP-MAINT §7 Failure reporting"]`

| Reference | pinned | dense | lexical | fused | synthetic |
|---|---|---|---|---|---|
| `SAF-PUMP-LOTO §2 Before any intervention` | yes | — | — | 1.0000 | yes |
| `SAF-PUMP-LOTO §2.1 Do not restart a tripped pump` | yes | — | — | 1.0000 | yes |
| `MM-CP-MAINT §7.1 What to report` | yes | — | — | 1.0000 | yes |
| `MM-CP-MAINT §7.2 Contents` | yes | — | — | 1.0000 | yes |
| `OP-BFP-101 §4.4 High vibration response` | no | 0.838 (r1) | 14.01 (r5) | 0.7143 | no |

Two things to read off this. `§2` was requested and `§2.1` came with it — `covers()` expanding a
parent reference into its subsections, which is how "do not restart a tripped pump" reaches the
answer when the API cited only the parent. And with two references pinned, four of five slots are
pinned: at `RAG_TOP_K=5` a heavily-cited recommendation crowds out search, which is noted as a
limitation rather than defended.

## Reproducing these numbers

```bash
make fetch-model      # only where HuggingFace is blocked
make ingest           # build .qdrant from rag/documents
uv run pytest rag/tests -m "not live"                  # 6 test modules, hash embedder
uv run pytest rag/tests/test_retrieval_semantic.py     # the real model; asserts the distributions above
```
