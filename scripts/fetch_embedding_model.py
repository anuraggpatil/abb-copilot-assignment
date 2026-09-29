#!/usr/bin/env python3
"""Fetch the embedding model when HuggingFace is unreachable.

On an unrestricted network none of this is needed: `make ingest` lets fastembed pull
`BAAI/bge-small-en-v1.5` from HuggingFace on first use. This script exists for the case this
project was actually developed on, which is worth stating precisely because it is common in
the kind of plant network this copilot would really run in:

  * The machine sits behind a **Zscaler TLS intercept**. Every certificate is reissued by
    `Zscaler Root CA`, whose `X509v3 Basic Constraints: CA:TRUE` is *not marked critical*.
    OpenSSL 3 rejects that as an invalid CA, so **every** Python HTTPS request fails with
    `CERTIFICATE_VERIFY_FAILED`, no matter what is in `certifi` — adding the Zscaler root to
    the bundle does not help, because the root itself is what OpenSSL objects to.
  * `huggingface.co` model downloads are additionally blocked with a 403.

Two things this script deliberately does **not** do:

  * It does not disable certificate verification. `verify=False` would "fix" the symptom by
    removing the only protection against the intercept being someone other than the employer,
    and a repo that ships that teaches the wrong lesson.
  * It does not vendor the model into git. 127 MB of ONNX weights in a repository somebody
    has to clone is its own problem.

Instead it downloads through `curl`, which uses the platform TLS stack (lenient about the
non-critical constraint, still verifying the chain), from the Qdrant GCS mirror, which is not
blocked. The downloaded archive is then repaired — see `_repair_tokenizer_config` — and
verified by actually embedding a sentence.

Usage:
    python scripts/fetch_embedding_model.py            # fetch, repair, verify
    python scripts/fetch_embedding_model.py --verify   # check an existing copy only

Then set, in `.env`:
    RAG_EMBED_MODEL_PATH=.models/bge-small-en-v1.5
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The Qdrant-hosted mirror of the same ONNX export fastembed would otherwise take from
#: HuggingFace. Reachable where `huggingface.co` is not.
MODEL_URL = "https://storage.googleapis.com/qdrant-fastembed/fast-bge-small-en-v1.5.tar.gz"
#: The directory inside the archive.
ARCHIVE_ROOT = "fast-bge-small-en-v1.5"
DEFAULT_DEST = REPO_ROOT / ".models" / "bge-small-en-v1.5"

#: What fastembed needs to load the model. Checked explicitly so a truncated download is
#: reported as a missing file rather than as an onnxruntime crash.
REQUIRED_FILES = (
    "model_optimized.onnx",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
)

PROBE_SENTENCE = "Low suction pressure causes cavitation in a centrifugal feed pump."


class FetchFailed(Exception):
    """A step did not do what it was supposed to."""


def _run_curl(url: str, destination: Path) -> None:
    """Download with curl, which succeeds through the TLS intercept where Python cannot."""
    if shutil.which("curl") is None:
        raise FetchFailed(
            "curl is not on PATH. Download the model manually and unpack it so that "
            f"{DEFAULT_DEST}/model_optimized.onnx exists:\n  {url}"
        )

    print(f"downloading {url}")
    print("  (via curl: the platform TLS stack, so this works behind the TLS intercept)")
    result = subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--show-error",
            "--max-time",
            "600",
            "--retry",
            "2",
            "--progress-bar",
            "--output",
            str(destination),
            url,
        ],
        check=False,
    )
    if result.returncode != 0:
        raise FetchFailed(
            f"curl exited {result.returncode}. If this network blocks the mirror too, "
            "fetch the archive on another machine and unpack it to "
            f"{DEFAULT_DEST}, or run the RAG layer with RAG_EMBEDDER=hash."
        )


def _extract(archive: Path, staging: Path) -> Path:
    print(f"extracting {archive.name}")
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getnames():
            # The archive is from a third party. An absolute or `..` member would write
            # outside the staging directory, and this script is run from a repo checkout.
            if member.startswith("/") or ".." in Path(member).parts:
                raise FetchFailed(f"refusing to extract unsafe archive member {member!r}")
        # `filter="data"` is the 3.12+ default-to-be and rejects device files, symlinks
        # pointing outside the tree, and setuid bits.
        tar.extractall(staging, filter="data")

    extracted = staging / ARCHIVE_ROOT
    if not extracted.is_dir():
        raise FetchFailed(f"archive did not contain a {ARCHIVE_ROOT}/ directory")
    return extracted


def _repair_tokenizer_config(model_dir: Path) -> None:
    """Give `tokenizer_config.json` a real `model_max_length`.

    This archive was published in 2023 with `model_max_length` set to HuggingFace's
    "unspecified" sentinel — the integer 1e30 — which fastembed 0.8 rejects outright with
    "Could not determine the maximum context length". The true bound is not a guess: it is
    `max_position_embeddings` in the model's own `config.json`, which is 512 for
    bge-small-en-v1.5. Reading it from there rather than hardcoding 512 means this stays
    correct if the mirror ever serves a different export.
    """
    config = json.loads((model_dir / "config.json").read_text())
    max_context = config.get("max_position_embeddings")
    if not isinstance(max_context, int) or max_context <= 0:
        raise FetchFailed(
            f"config.json has no usable max_position_embeddings (got {max_context!r})"
        )

    path = model_dir / "tokenizer_config.json"
    tokenizer_config = json.loads(path.read_text())
    current = tokenizer_config.get("model_max_length")

    # Anything at or above this is the sentinel, not a real context length.
    if isinstance(current, int) and 0 < current <= max_context:
        print(f"  tokenizer_config.json already declares model_max_length={current}")
        return

    tokenizer_config["model_max_length"] = max_context
    path.write_text(json.dumps(tokenizer_config, indent=2) + "\n")
    print(f"  set model_max_length={max_context} (was {current!r}, HuggingFace's unset sentinel)")


def _check_files(model_dir: Path) -> None:
    missing = [name for name in REQUIRED_FILES if not (model_dir / name).is_file()]
    if missing:
        raise FetchFailed(f"{model_dir} is missing {', '.join(missing)}")


def verify(model_dir: Path) -> None:
    """Load the model and embed one sentence. Nothing else proves it actually works."""
    _check_files(model_dir)
    print(f"verifying {model_dir}")

    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise FetchFailed("fastembed is not installed — run `make install`") from exc

    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", specific_model_path=str(model_dir))
    vectors = list(model.embed([PROBE_SENTENCE]))
    if len(vectors) != 1 or len(vectors[0]) != 384:
        raise FetchFailed(
            f"expected one 384-dimension vector, got {len(vectors)} of "
            f"{len(vectors[0]) if vectors else 0}"
        )
    print(f"  ok — embedded a sentence to {len(vectors[0])} dimensions")


def fetch(destination: Path, *, url: str = MODEL_URL) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bge-fetch-") as tmp:
        staging = Path(tmp)
        archive = staging / "model.tar.gz"
        _run_curl(url, archive)
        extracted = _extract(archive, staging)
        _repair_tokenizer_config(extracted)
        _check_files(extracted)

        # Moved into place only once it is complete and repaired, so an interrupted run
        # never leaves a half-model that looks present to `--verify`.
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(extracted), str(destination))

    print(f"installed to {destination}")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="Where to install.")
    parser.add_argument("--url", default=MODEL_URL, help="Override the mirror URL.")
    parser.add_argument(
        "--verify", action="store_true", help="Only verify an existing copy; do not download."
    )
    args = parser.parse_args(argv)

    try:
        if args.verify:
            if not args.dest.is_dir():
                raise FetchFailed(f"{args.dest} does not exist — run without --verify first")
            verify(args.dest)
        else:
            if args.dest.is_dir():
                print(f"{args.dest} already exists; verifying instead of re-downloading")
                verify(args.dest)
            else:
                verify(fetch(args.dest, url=args.url))
    except FetchFailed as exc:
        print(f"\nfailed: {exc}", file=sys.stderr)
        return 1

    relative = (
        args.dest.relative_to(REPO_ROOT) if args.dest.is_relative_to(REPO_ROOT) else args.dest
    )
    print(f"\nNow set this in .env:\n  RAG_EMBED_MODEL_PATH={relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
