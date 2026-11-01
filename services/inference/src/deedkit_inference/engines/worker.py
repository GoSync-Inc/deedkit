"""The warm worker child: `python -I -m deedkit_inference.engines.worker <runtime module> <spec json>`.

It loads one model from the verified local paths in the spec, prints `{"ready": true}`, then answers each
`{"op": "job"}` line on stdin with one JSON line (`{"ok": true, "result": ...}` or `{"ok": false, "error": code}`)
and leaves on `{"op": "unload"}` or when stdin closes, after the runtime cleared its caches. stdout belongs to this
protocol alone: whatever a library prints goes to stderr, which the service discards. Only the code of a failure
goes back, never a message that could carry file content."""

import os
import sys

from .offline import OFFLINE

# The offline and telemetry switches are applied before the first engine import (tests/test_engine_hygiene.py reads
# this order). The service also spawns this process with the same values in its environment.
os.environ.update(OFFLINE)

import importlib  # noqa: E402
import json  # noqa: E402

PREFIX = "deedkit_inference.engines.runtimes."


def main(argv):
    module_name, spec = argv[1], json.loads(argv[2])
    if not module_name.startswith(PREFIX):
        return 2
    channel = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)

    def send(value):
        channel.write(json.dumps(value, ensure_ascii=False) + "\n")
        channel.flush()

    runtime = importlib.import_module(module_name).load(spec["paths"], spec["options"])
    try:
        send({"ready": True})
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                break
            if not isinstance(message, dict) or message.get("op") != "job":
                break
            try:
                send({"ok": True, "result": runtime.run(message["job"])})
            except Exception:  # noqa: BLE001 - the parent kills a worker after any failed job
                send({"ok": False, "error": "runtime_failed"})
    finally:
        runtime.unload()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
