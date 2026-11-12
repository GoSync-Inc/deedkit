#!/usr/bin/env bash
# bb script automation `deedkit-feedback-dispatch-run` (every 10 minutes): one dispatcher tick (ANYM-991).
# Silent when nothing is due, and a no-op until `dispatch` is true in ~/.config/deedkit/feedback-dispatch.json. Exit 75
# means another tick holds the lock, which is not a failure.
set -u
python3 "$HOME/.local/lib/deedkit-feedback-bridge/feedback_dispatch.py" run
status=$?
if [ "$status" -eq 75 ]; then exit 0; fi
exit "$status"
