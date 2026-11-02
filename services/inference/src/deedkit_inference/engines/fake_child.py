"""The child process of the fake engine: reads the steering line of its input file and behaves."""

import json
import os
import subprocess
import sys
import time

from .fake import directive
from .ticker import tick


def main():
    path, kind, index, count = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    d = directive(path)
    pidfile = d.get("pidfile")
    if pidfile:
        grandchild = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(600)"])
        with open(pidfile, "w") as handle:
            handle.write(f"{os.getpid()} {grandchild.pid}")
    mode, only = d.get("mode", "ok"), d.get("phase")
    if only is not None and only != index:
        mode = "ok"
    if mode == "crash":
        os._exit(139)
    if mode == "exit":
        sys.exit(int(d.get("code", 3)))
    if mode == "hang":
        while True:
            time.sleep(1)
    if d.get("tick") and d.get("tick_phase", index) == index:
        tick(d["tick"], int(d.get("ticks", 100)))
    time.sleep(float(d.get("sleep", 0)))
    if mode == "garbage":
        sys.stdout.write("not json {")
        return
    if index < count - 1:
        sys.stdout.write("{}")
        return
    if kind == "audio":
        duration = float(d.get("duration", 3.0))
        text = "fake transcript"
        out = {"text": text, "language": "en", "duration": duration,
               "segments": [{"start": 0.0, "end": duration, "text": text}]}
    else:
        content = {"title": "fake title", "summary": "fake summary", "document_type": "unknown", "text": ""}
        out = {"content": json.dumps(content), "finish_reason": "stop",
               "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
