#!/usr/bin/env bash
# bb script automation `deedkit-feedback-bridge-run` (every 10 minutes): the fallback for the watch loop (ANYM-990).
# One forward + reverse cycle under the bridge's lock. Silent when nothing changed (bb records a skipped run); exit 75
# means the watch loop holds the lock, which is not a failure.
set -u
python3 "$HOME/.local/lib/deedkit-feedback-bridge/feedback_bridge.py" run
status=$?
if [ "$status" -eq 75 ]; then exit 0; fi
exit "$status"
