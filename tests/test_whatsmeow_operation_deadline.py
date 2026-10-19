"""The native group-operation deadline stays inside the command worker's timeout."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# spec: ANYM-431
def test_native_group_deadline_below_worker_timeout():
    go = (ROOT/'whatsapp/whatsmeow/group_iq.go').read_text()
    deadline = re.search(r'^var groupOperationDeadline = (\d+) \* time\.Second$', go, re.M)
    worker = (ROOT/'services/messaging/messaging_service/commands.py').read_text()
    timeout = re.search(r"timeout=\d+ if job\['operation'\]=='send_file' else (\d+)\n", worker)
    assert deadline and timeout
    # Room for the last-moment preflight and IPC before the worker cancels the call.
    assert int(deadline.group(1)) + 10 <= int(timeout.group(1))
