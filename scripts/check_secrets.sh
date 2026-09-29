#!/usr/bin/env bash
# Fail if anything that looks like a live credential is tracked by git.
# Scans tracked files only, so a local .env (gitignored) never trips it.
set -uo pipefail

cd "$(dirname "$0")/.."

# This scans *git-tracked* content, so it is only meaningful inside a work tree. Run from an
# extracted archive it would otherwise find nothing, report "clean" and exit 0 — a security gate
# that silently scans zero files is worse than no gate, because CI goes green either way.
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "check_secrets.sh: not inside a git work tree, so there is no tracked content to scan." >&2
  echo "Run it from a clone. Refusing to report 'clean' without having looked." >&2
  exit 2
fi

# Google AI Studio keys — both the AIza… and the newer AQ.… forms, this project's own LLM
# credential — then Databricks PAT, OpenAI key, AWS key id, JWT bearer, private key header.
# The Databricks pattern stays although that provider was replaced: the history still has to be
# clean of one, and an extra pattern costs nothing.
PATTERNS=(
  'AIza[0-9A-Za-z_-]{35}'
  'AQ\.[A-Za-z0-9_-]{30,}'
  'dapi[0-9a-f]{32}'
  'sk-[A-Za-z0-9]{20,}'
  'AKIA[0-9A-Z]{16}'
  'Bearer[[:space:]]+ey[A-Za-z0-9_-]{10,}'
  'BEGIN[[:space:]]+(RSA[[:space:]]+)?PRIVATE[[:space:]]+KEY'
)

status=0
for pattern in "${PATTERNS[@]}"; do
  # Tracked content only, so a local gitignored .env never trips it. Exclude this script so its
  # own patterns don't match themselves.
  hits=$(git grep -n -I -E "$pattern" -- ':!scripts/check_secrets.sh')
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "Possible secret matching /${pattern}/:"
    echo "$hits"
    status=1
  elif [ "$rc" -ne 1 ]; then
    # 1 is "no matches", which is the outcome we want. Anything else means git itself failed and
    # this pattern was never actually checked — that must not read as a pass.
    echo "check_secrets.sh: git grep failed with exit ${rc} on /${pattern}/ — pattern NOT checked." >&2
    status=1
  fi
done

if [ -n "$(git ls-files .env)" ]; then
  echo ".env is tracked by git — it must stay untracked and gitignored."
  status=1
fi

if [ "$status" -eq 0 ]; then
  echo "Secret scan clean: no credential patterns in tracked files."
fi
exit "$status"
