#!/usr/bin/env bash
# Refresh a drillable-context index from a PINNED origin/main source worktree.
#
# Fixes the two staleness modes:
#   - branch-drift  — index a DEDICATED detached worktree, never a dev checkout you develop in
#                     (a checkout silently tracks whatever branch it's on; the index goes stale,
#                      and nothing errors). See the README "Pin the source" note.
#   - reseed-lag    — this re-fetches + reseeds, so a just-merged fact becomes retrievable.
# Run it after a merge to the source repo, or on a schedule. Idempotent — safe to re-run.
#
# Configure via env (or edit the defaults):
#   SRC      the pinned detached worktree to index. Create it once:
#              git -C /path/to/repo worktree add --detach "$SRC" origin/main
#   CFG      the drillable-context config.json (its facts_dir/oracle_repo point at $SRC)
#   PLUGIN   this repo checkout (for src/seed.py); defaults to this script's repo
#   ENVFILE  optional: a file that exports OPENAI_API_KEY (for semantic embeddings)
#
# Usage:  SRC=~/sources/myproject CFG=~/configs/myproject.json bash examples/refresh.sh
set -euo pipefail

SRC="${SRC:?set SRC to the pinned source worktree, e.g. ~/sources/myproject}"
CFG="${CFG:?set CFG to the drillable-context config.json}"
PLUGIN="${PLUGIN:-$(cd "$(dirname "$0")/.." && pwd)}"
ENVFILE="${ENVFILE:-}"

git -C "$SRC" fetch origin main --quiet
git -C "$SRC" checkout --quiet --detach origin/main           # pin to the reviewed, merged record

if [ -n "$ENVFILE" ] && [ -f "$ENVFILE" ]; then               # e.g. OPENAI_API_KEY for embeddings
  set -a
  # shellcheck disable=SC1090
  . "$ENVFILE"
  set +a
fi

python3 "$PLUGIN/src/seed.py" --config "$CFG"
echo "index refreshed from $(git -C "$SRC" log -1 --oneline)"
