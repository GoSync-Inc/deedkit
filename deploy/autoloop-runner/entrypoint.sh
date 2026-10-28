#!/bin/sh
# The autoloop runner's entrypoint (ANYM-574): the isolation check, then a fresh clone, then the agent CLI.
# AUTOLOOP_MODE=check: the isolation check and one model call that proves the subscription login works here.
set -eu
python3 /opt/runner/selfcheck.py
if [ "${AUTOLOOP_MODE:-build}" = check ]; then
  exec claude -p 'Reply with the single word ok.' --model "$AUTOLOOP_MODEL" --tools '' --setting-sources '' \
    --strict-mcp-config --mcp-config '{"mcpServers":{}}' --no-session-persistence
fi
case "$AUTOLOOP_INTENT" in autoloop-build-*) ;; *) echo "runner: bad intent" >&2; exit 2 ;; esac
gh auth setup-git
git clone --quiet --branch main "https://github.com/$AUTOLOOP_REPO.git" "$HOME/deedkit"
cd "$HOME/deedkit"
git switch --quiet --create "$AUTOLOOP_INTENT"
: > /run/autoloop/outbox/requests.jsonl  # the launcher copies it out; a missing file reads as a failed copy
# Non-interactive: the agent ends after its last answer, so the launcher can apply the outbox.
# No session is saved: the transcript stays in the build terminal's scrollback only.
exec claude -p "$AUTOLOOP_PROMPT" --dangerously-skip-permissions --model "$AUTOLOOP_MODEL" --effort "$AUTOLOOP_EFFORT" \
  --no-session-persistence
