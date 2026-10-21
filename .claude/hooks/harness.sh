#!/bin/sh
# SessionStart: put the shared rulebook (harness process.md) into the session context.
# Source order: the merged harness (origin/main of ~/agent-harness; never a draft branch or the
# working tree), then this repository's pinned copy docs/harness/; otherwise a stop notice.
root=$(cd "$(dirname "$0")/../.." && pwd)
dir=${HARNESS_DIR:-$HOME/agent-harness}
text=$(git -C "$dir" show origin/main:process.md 2>/dev/null)
src="egerev/agent-harness origin/main $(git -C "$dir" rev-parse --short origin/main 2>/dev/null)"
if [ -z "$(printf '%s' "$text" | tr -d '[:space:]')" ]; then
  text=$(cat "$root/docs/harness/process.md" 2>/dev/null)
  src="pinned copy docs/harness, version $(cat "$root/docs/harness/VERSION" 2>/dev/null)"
fi
if [ -z "$(printf '%s' "$text" | tr -d '[:space:]')" ]; then
  echo "HARNESS UNAVAILABLE: the shared rulebook (owner-kept list, change paths, reviews) is not loaded."
  echo "Stop: report BLOCKED to the orchestrator. Do not merge, deploy or take any owner-kept action."
  exit 0
fi
echo "Harness process.md ($src). Other harness files: git -C ~/agent-harness show origin/main:<file>, or docs/harness/."
printf '%s\n' "$text"
