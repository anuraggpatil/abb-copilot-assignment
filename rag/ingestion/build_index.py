"""`make ingest` — load, sanitise, chunk and index the corpus.

Run as `python -m rag.ingestion.build_index`. Prints a report rather than staying silent,
because the two things most likely to be wrong about an index are invisible otherwise: which
documents went in, and what the sanitizer took out of them. A sanitizer finding on the
authored corpus is a false positive redacting real procedure text, and it needs to be seen.

Exit code is 1 on any failure, so `make ingest` fails a build rather than leaving a stale or
partial index in place.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rag.config import RagSettings, get_settings
from rag.ingestion.chunker import chunk_corpus
from rag.ingestion.loader import DocumentLoadError, load_corpus
from rag.models import Chunk, SourceDocument
from rag.retrieval.embedder import build_embedder
from rag.retrieval.store import ChunkIndex

log = logging.getLogger("rag.ingest")


def build(settings: RagSettings, *, repo_root: Path | None = None) -> tuple[int, int]:
    """Do the whole ingest. Returns (documents, chunks)."""
    root = repo_root or Path.cwd()
    documents = load_corpus(settings.documents_dir, repo_root=root)
    chunks = chunk_corpus(
        documents,
        max_tokens=settings.chunk_max_tokens,
        min_tokens=settings.chunk_min_tokens,
        overlap_sentences=settings.chunk_overlap_sentences,
    )
    if not chunks:
        raise DocumentLoadError(
            f"{len(documents)} document(s) produced no chunks — are the section headings "
            "formatted as `## §N Title`?"
        )

    index = ChunkIndex(
        path=settings.qdrant_path,
        collection=settings.collection,
        embedder=build_embedder(settings),
    )
    try:
        index.build(chunks)
    finally:
        # Embedded Qdrant holds an exclusive lock on its directory. Not releasing it here
        # means the next process to open the index fails, which looks like a corrupt index.
        index.close()

    _report(documents, chunks, settings)
    return len(documents), len(chunks)


def _report(documents: list[SourceDocument], chunks: list[Chunk], settings: RagSettings) -> None:
    print(f"\nIndexed {len(chunks)} chunks from {len(documents)} documents")
    print(f"  collection : {settings.collection} at {settings.qdrant_path}")
    print(f"  embedder   : {settings.embedder} ({settings.embed_model})")
    print()

    for document in documents:
        owned = [chunk for chunk in chunks if chunk.doc_id == document.doc_id]
        sections = len({chunk.section.number for chunk in owned})
        flag = "  [safety-critical]" if document.safety_critical else ""
        print(
            f"  {document.doc_id:<16} rev {document.revision:<4} "
            f"{len(owned):>3} chunks  {sections:>2} sections{flag}"
        )

    findings = [(doc.doc_id, f) for doc in documents for f in doc.sanitizer_findings]
    if findings:
        # Loud, because on the authored corpus this means the sanitizer removed legitimate
        # procedure text — a redacted safety instruction is a wrong answer waiting to happen.
        print(f"\n  ⚠  sanitizer modified {len(findings)} span(s):")
        for doc_id, finding in findings:
            print(f"       {doc_id}: {finding}")
    else:
        print("\n  sanitizer: no instruction-like content found")

    oversized = [chunk for chunk in chunks if chunk.token_estimate > settings.chunk_max_tokens]
    if oversized:
        # Expected, not a warning: tables and numbered step lists are never split, so a few
        # chunks exceed the budget by design. Named so the count is not a surprise.
        print(
            f"\n  {len(oversized)} chunk(s) over the {settings.chunk_max_tokens}-token budget "
            "(atomic tables / step lists, kept whole deliberately):"
        )
        for chunk in oversized:
            print(f"       {chunk.reference}  ~{chunk.token_estimate} tokens")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--documents",
        type=Path,
        help="Override RAG_DOCUMENTS_DIR, e.g. to index a test fixture directory.",
    )
    parser.add_argument("--collection", help="Override RAG_COLLECTION.")
    parser.add_argument(
        "--embedder",
        choices=("fastembed", "hash"),
        help="Override RAG_EMBEDDER. `hash` needs no model download.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    overrides = {
        key: value
        for key, value in (
            ("documents_dir", args.documents),
            ("collection", args.collection),
            ("embedder", args.embedder),
        )
        if value is not None
    }
    if overrides:
        settings = settings.model_copy(update=overrides)

    logging.basicConfig(
        level=settings.log_level.upper(), format="%(levelname)-8s %(name)s  %(message)s"
    )

    try:
        build(settings)
    except DocumentLoadError as exc:
        print(f"\ningest failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the CLI boundary; a traceback helps nobody here
        print(f"\ningest failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
