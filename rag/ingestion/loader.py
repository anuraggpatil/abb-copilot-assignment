"""Read the authored corpus off disk into `SourceDocument`s.

Frontmatter is required rather than inferred. The alternative — deriving a document id from
the filename and a title from the first heading — works until a file is renamed, at which
point every stored chunk id changes and the index silently no longer matches the references
the alarm API emits. Declaring the id in the file makes that impossible.

Sanitising happens here, before the text reaches the chunker, so that no component
downstream of this module ever handles unsanitised document text. That is easier to verify
than sanitising at several call sites.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from rag.ingestion.sanitizer import SanitizeResult, sanitize
from rag.models import SourceDocument

log = logging.getLogger(__name__)

#: Frontmatter keys promoted to typed fields; everything else is kept in `metadata`.
_REQUIRED = ("doc_id", "title", "doc_type", "revision", "effective_date", "owner")
_KNOWN = (*_REQUIRED, "applies_to", "safety_critical")

SUPPORTED_SUFFIXES = (".md", ".markdown")


class DocumentLoadError(Exception):
    """A corpus file could not be read as a document.

    Fatal by design: a document that fails to load is a document whose procedure cannot be
    cited, and the copilot would then answer a safety question from an incomplete corpus
    without any indication that something is missing.
    """


def load_document(path: Path, *, repo_root: Path | None = None) -> SourceDocument:
    """Parse one markdown file with YAML frontmatter, sanitising its body."""
    raw = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw, path)

    missing = [key for key in _REQUIRED if key not in frontmatter]
    if missing:
        raise DocumentLoadError(f"{path}: frontmatter is missing {', '.join(missing)}")

    # The body is sanitised; the frontmatter is not, because it is not free text and is
    # never placed in a prompt. Injecting via a `title:` would require the attacker to
    # already be able to write to the corpus directory.
    result = sanitize(body, origin=path.name)
    if result.findings:
        log.warning("sanitizer modified %s: %d finding(s)", path.name, len(result.findings))

    return _build(frontmatter, body=result.text, path=_provenance(path, repo_root), result=result)


def _provenance(path: Path, repo_root: Path | None) -> str:
    """A repo-relative path for the citation, whatever form the caller passed in.

    `Path.relative_to` raises when the path is already relative or lies outside the root —
    both of which happen in practice, since `RAG_DOCUMENTS_DIR` is a relative path by default
    and a test fixture may live in a temp directory. Provenance is for a human reading a
    citation, so degrading to the path as given is right; failing the whole ingest is not.
    """
    if repo_root is None:
        return str(path)
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path)


def _build(
    frontmatter: dict[str, Any], *, body: str, path: str, result: SanitizeResult
) -> SourceDocument:
    return SourceDocument(
        doc_id=str(frontmatter["doc_id"]),
        title=str(frontmatter["title"]),
        doc_type=str(frontmatter["doc_type"]),
        # Stringified because YAML parses `revision: 7` as an int and `2026-03-01` as a
        # date. Both are identifiers here, not quantities.
        revision=str(frontmatter["revision"]),
        effective_date=str(frontmatter["effective_date"]),
        owner=str(frontmatter["owner"]),
        path=path,
        body=body,
        applies_to=[str(item) for item in frontmatter.get("applies_to", []) or []],
        safety_critical=bool(frontmatter.get("safety_critical", False)),
        metadata={key: value for key, value in frontmatter.items() if key not in _KNOWN},
        sanitizer_findings=result.findings,
    )


def load_corpus(directory: Path, *, repo_root: Path | None = None) -> list[SourceDocument]:
    """Load every markdown file in `directory`, sorted by path for deterministic output.

    Raises when the directory is empty: an empty corpus produces an index that answers
    every question with "no documented evidence", which looks like a working
    low-confidence path rather than like the misconfiguration it is.
    """
    if not directory.is_dir():
        raise DocumentLoadError(f"{directory} is not a directory")

    paths = sorted(p for p in directory.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)
    if not paths:
        raise DocumentLoadError(
            f"no {'/'.join(SUPPORTED_SUFFIXES)} files in {directory} — nothing to index"
        )

    documents = [load_document(path, repo_root=repo_root) for path in paths]

    seen: dict[str, str] = {}
    for document in documents:
        if document.doc_id in seen:
            raise DocumentLoadError(
                f"duplicate doc_id {document.doc_id!r} in {document.path} and "
                f"{seen[document.doc_id]} — references would resolve ambiguously"
            )
        seen[document.doc_id] = document.path
    return documents


def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    """Separate a leading `---` fenced YAML block from the markdown body."""
    if not raw.lstrip().startswith("---"):
        raise DocumentLoadError(f"{path}: no YAML frontmatter block")

    stripped = raw.lstrip()
    end = stripped.find("\n---", 3)
    if end == -1:
        raise DocumentLoadError(f"{path}: frontmatter block is not closed with ---")

    header, body = stripped[3:end], stripped[end + 4 :]
    try:
        parsed = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise DocumentLoadError(f"{path}: frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise DocumentLoadError(
            f"{path}: frontmatter must be a mapping, got {type(parsed).__name__}"
        )
    return parsed, body.lstrip("\n")
