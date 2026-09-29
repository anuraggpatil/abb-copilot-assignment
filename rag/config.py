"""Settings for ingestion and retrieval.

Same pattern as the other two settings classes: its own class, the same `.env`, and an
explicit alias on **every** field. The alias is not style — an un-aliased field named `path`
resolves from the shell's `PATH`, which is a bug that cost real time on the MCP server and
which no in-process test can see.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: `fastembed` is the real model. `hash` is a deterministic offline stand-in: no weights,
#: no download, no network — see `rag/retrieval/embedder.py` for what it does and does not
#: prove.
EmbedderKind = Literal["fastembed", "hash"]


class RagSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    documents_dir: Path = Field(default=Path("rag/documents"), alias="RAG_DOCUMENTS_DIR")
    #: Embedded Qdrant writes to a local directory — no server, no container. This is the
    #: whole reason the RAG layer runs on a laptop with nothing installed but Python.
    qdrant_path: Path = Field(default=Path(".qdrant"), alias="RAG_QDRANT_PATH")
    collection: str = Field(default="alarm_procedures", alias="RAG_COLLECTION")

    embedder: EmbedderKind = Field(default="fastembed", alias="RAG_EMBEDDER")
    embed_model: str = Field(default="BAAI/bge-small-en-v1.5", alias="RAG_EMBED_MODEL")
    #: Set to load the model from a local directory instead of downloading it. Needed on any
    #: network where HuggingFace is unreachable — see `scripts/fetch_embedding_model.py`,
    #: which populates it. Empty means "download on first use", the normal path.
    embed_model_path: Path | None = Field(default=None, alias="RAG_EMBED_MODEL_PATH")

    chunk_max_tokens: int = Field(default=380, alias="RAG_CHUNK_SIZE")
    chunk_min_tokens: int = Field(default=40, alias="RAG_CHUNK_MIN_TOKENS")
    chunk_overlap_sentences: int = Field(default=1, alias="RAG_CHUNK_OVERLAP_SENTENCES")

    top_k: int = Field(default=5, alias="RAG_TOP_K")
    #: Candidates pulled from each retriever before fusion. Wider than `top_k` on purpose:
    #: reciprocal-rank fusion can only promote a chunk that at least one retriever returned.
    candidates_per_retriever: int = Field(default=20, alias="RAG_CANDIDATES")
    #: Floor on the best **dense cosine similarity**, below which retrieval reports low
    #: confidence and the copilot takes the "insufficient documented evidence" path.
    #:
    #: Deliberately not a threshold on the fused RRF score, which cannot work: RRF encodes
    #: only rank, so whatever ranks first scores the same whether its cosine was 0.85 or
    #: 0.41, and a threshold on it can never abstain. BM25 is not consulted either, because it
    #: does not separate the two — "what is the capital of France?" outscores "cavitation" on
    #: BM25, since the stopwords match plenty of chunks.
    #:
    #: Measured on this corpus with bge-small-en-v1.5 (the numbers are asserted in
    #: `rag/tests/test_retrieval_semantic.py`, so this stays a measurement rather than
    #: becoming folklore): eight operator-phrased questions span 0.663–0.820, four
    #: out-of-domain ones span 0.406–0.535. 0.60 is the midpoint of that gap, which puts the
    #: same ~0.06 of margin on each side rather than crowding one.
    #:
    #: The margin is genuinely narrow and the limit is worth naming: cosine measures topical
    #: similarity, not applicability. A question about *turbine* inspection intervals scores
    #: 0.688 against the pump maintenance manual — above any workable threshold — because the
    #: text really is about inspection intervals. No value of this setting fixes that; it is
    #: handled downstream by citations the reader can check and the `applies_to` metadata.
    #:
    #: The number is calibrated to this model. `RAG_EMBEDDER=hash` has a different and
    #: overlapping distribution, and no threshold separates it — see the embedder module.
    min_relevance: float = Field(default=0.60, alias="RAG_MIN_RELEVANCE")
    #: The `k` in RRF's `1/(k + rank)`, which sets how much *rank within a list* counts
    #: against *appearing in both lists*.
    #:
    #: **Not 60.** That value comes from the paper that introduced RRF, where it was tuned on
    #: TREC runs 1000 candidates deep; there `1/(k+rank)` spans 1/61…1/1060, a 17× range, so
    #: rank discriminates strongly. Transplanted onto a list 20 deep it spans 1/61…1/80 — a
    #: 1.3× range — which flattens rank almost completely and degenerates RRF into counting
    #: how many retrievers returned the chunk. That was measurably wrong here: BM25's top 20
    #: is 27% of a 74-chunk corpus, so its membership is close to noise, yet it was worth up
    #: to a doubling. The chunk with the *highest* cosine for "is it safe to keep running the
    #: pump while it is cavitating" ranked last, behind a lexical match about strainers.
    #:
    #: 2 restores the original's character at this depth (1/3…1/22, a 7.3× range) and is the
    #: canonical 60 scaled by the same ratio to candidate depth. Top-1 accuracy on the
    #: question set went from 6/8 to 7/8, and it is why fused scores now spread across
    #: 0.34–0.88 instead of clustering near 0.9. Raise it if `RAG_CANDIDATES` grows a lot.
    rrf_k: int = Field(default=2, alias="RAG_RRF_K")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> RagSettings:
    return RagSettings()
