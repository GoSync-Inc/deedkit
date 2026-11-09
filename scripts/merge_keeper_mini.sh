#!/bin/bash
# Merge keeper tick on the Mac mini (launchd, every 300 s; docs/operations/parallel-development.md, Merge keeper).
# Moves its own clone to origin/main, then runs scripts/merge_keeper.py from that commit with the host's gh login.
# The braces make bash read the whole file before the checkout can change it.
{
set -euo pipefail
clone=${MERGE_KEEPER_CLONE:-$HOME/.local/share/deedkit-merge-keeper/repo}
state=${MERGE_KEEPER_STATE:-$HOME/.local/state/deedkit-merge-keeper}
mkdir -p "$state"
# launchd starts no tick while one runs; each gh call has its own 60 s limit and git a stall limit.
git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 -C "$clone" fetch -q --no-tags origin main
git -C "$clone" checkout -q --detach FETCH_HEAD
cd "$clone"
python3 -I scripts/merge_keeper.py run --host mini \
  --heartbeat-file "$state/heartbeat.json" --heartbeat-variable
exit $?
}
