"""Dense embeddings, behind a two-method protocol.

The real implementation is `BAAI/bge-small-en-v1.5` at 384 dimensions via fastembed, chosen
over a larger model because the corpus is four documents: recall is not the bottleneck, and
a 33M-parameter model keeps `make ingest` to a few seconds on a laptop.

fastembed rather than sentence-transformers for one specific reason: sentence-transformers
pulls in torch — roughly 2.5 GB of wheels — to run inference on a model this small.
fastembed runs the same weights through ONNX Runtime in about 80 MB. For a repo somebody
else has to clone and install before they can review it, that matters more than any
flexibility torch would buy.

**Why there are two implementations.** `HashEmbedder` is a deterministic offline stand-in
that CI and most tests use. It downloads nothing and needs no network, which means the test
suite is runnable on a fresh clone behind a firewall. Be clear about what it does and does
not prove: it is a bag-of-words hashing projection, so it captures lexical overlap and
nothing else — it cannot tell that "cavitation" relates to "low suction pressure". Tests
that assert *semantic* retrieval quality are marked to require the real model
(`rag/tests/test_retrieval_semantic.py`); everything else — plumbing, fusion, citation
construction, the low-confidence path — is model-independent and runs on the hash embedder.

Pretending otherwise would be the real failure mode here: a green suite that only ever
exercised a stand-in, asserting a quality property the stand-in cannot have.
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from rag.config import RagSettings, get_settings

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

log = logging.getLogger(__name__)


class EmbeddingModelUnavailable(Exception):
    """The embedding model could not be loaded, with the remedy in the message.

    Its own type because the caller — `make ingest`, or the backend on startup — should print
    this and stop rather than show a TLS traceback from six frames inside onnxruntime.
    """


DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

#: bge models were trained with an asymmetric objective: queries carry this instruction and
#: passages do not. Embedding passages *with* it silently degrades every result, so the two
#: directions are separate methods rather than one method with a flag somebody can forget.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@runtime_checkable
class Embedder(Protocol):
    """Two directions, because bge treats them differently, plus one release."""

    #: Vector width, needed to create the Qdrant collection before anything is embedded.
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def close(self) -> None:
        """Release whatever native resources the model holds.

        Part of the protocol rather than of the one implementation that needs it, because the
        caller holds an `Embedder` and should not have to ask which kind it got. `HashEmbedder`
        implements it as a no-op; `FastEmbedEmbedder.close` explains why it is not optional.
        """
        ...


class FastEmbedEmbedder:
    """ONNX-backed bge-small. Loads its weights on first use, not at import."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        dimension: int = EMBEDDING_DIM,
        model_path: Path | None = None,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        #: When set, weights are read from here instead of downloaded. Populated by
        #: `scripts/fetch_embedding_model.py` on networks where HuggingFace is blocked.
        self.model_path = model_path
        self._model: object | None = None

    def _load(self) -> object:
        if self._model is None:
            # Imported here so that importing this module — which the chunker tests do
            # transitively — does not pull in fastembed's runtime or touch the model cache.
            from fastembed import TextEmbedding

            if self.model_path is not None:
                if not self.model_path.is_dir():
                    raise EmbeddingModelUnavailable(
                        f"RAG_EMBED_MODEL_PATH points at {self.model_path}, which does not "
                        "exist. Run `make fetch-model`, or set RAG_EMBEDDER=hash to run "
                        "offline without semantic search."
                    )
                log.info("loading embedding model %s from %s", self.model_name, self.model_path)
                self._model = TextEmbedding(
                    model_name=self.model_name, specific_model_path=str(self.model_path)
                )
            else:
                log.info("loading embedding model %s", self.model_name)
                try:
                    self._model = TextEmbedding(model_name=self.model_name)
                except Exception as exc:
                    # The download is the single most likely thing to fail on a first run,
                    # and its native error is a TLS or 403 traceback that says nothing about
                    # what to do. Both remedies are in the message.
                    raise EmbeddingModelUnavailable(
                        f"could not load {self.model_name}: {type(exc).__name__}: {exc}\n"
                        "If this network blocks HuggingFace or intercepts TLS, run "
                        "`make fetch-model` and set RAG_EMBED_MODEL_PATH. To run without "
                        "semantic search entirely, set RAG_EMBEDDER=hash."
                    ) from exc
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._run(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._run([QUERY_PREFIX + text])[0]

    def close(self) -> None:
        """Drop the ONNX session while the interpreter is still healthy.

        Not housekeeping — it is a correctness fix. ONNX Runtime's inference session owns a
        native thread pool whose destructor runs when the last Python reference goes. If that
        only happens during interpreter finalisation, the C++ static it locks may already be
        gone, and the process aborts:

            libc++abi: terminating due to uncaught exception of type std::__1::system_error:
            recursive_mutex lock failed: Invalid argument

        …*after* everything has succeeded. The observed symptom was `pytest` printing "676
        passed" and then exiting 134, which fails CI on a green suite and is an unpleasant thing
        to debug because the abort names nothing that appears in this repository. Releasing the
        model here, rather than leaving it to garbage collection, is what makes shutdown
        deterministic. Loading is lazy, so a closed embedder that is used again simply reloads.
        """
        self._model = None

    def _run(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.embed(list(texts))  # type: ignore[attr-defined]
        # fastembed yields numpy arrays; converting to plain floats here keeps numpy out of
        # every downstream type signature and out of the Qdrant payloads.
        return [[float(value) for value in vector] for vector in vectors]


class HashEmbedder:
    """Deterministic hashing projection. Lexical overlap only — see the module docstring.

    Each token is hashed to a dimension and accumulated with a sublinear term weight, then
    the vector is L2-normalised so cosine similarity behaves. Two texts sharing vocabulary
    score high; two texts meaning the same thing in different words score zero.
    """

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        self.dimension = dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        # No prefix: it is a bag of words, and the instruction would be nine tokens of noise
        # present in every query and no document.
        return self._vector(text)

    def close(self) -> None:
        """Nothing to release — pure Python and no state."""

    def _vector(self, text: str) -> list[float]:
        counts: dict[int, float] = {}
        for token in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self.dimension
            counts[index] = counts.get(index, 0.0) + 1.0

        vector = [0.0] * self.dimension
        for index, count in counts.items():
            # Sublinear, like BM25's term saturation: the tenth "pump" says less than the
            # first, and without this a long document is dominated by its commonest word.
            vector[index] = 1.0 + math.log(count)

        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


def _tokens(text: str) -> list[str]:
    return [token for token in "".join(c.lower() if c.isalnum() else " " for c in text).split()]


def build_embedder(settings: RagSettings | None = None) -> Embedder:
    """Construct whichever embedder the configuration asks for."""
    settings = settings or get_settings()
    if settings.embedder == "hash":
        log.info("using the offline hash embedder — lexical overlap only, no semantics")
        return HashEmbedder()
    return FastEmbedEmbedder(model_name=settings.embed_model, model_path=settings.embed_model_path)
