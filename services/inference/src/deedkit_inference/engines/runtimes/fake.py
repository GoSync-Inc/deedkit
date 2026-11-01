"""A runtime for CI: no library, no model. The uploaded file steers it like the fake engine's: `FAKE {json}` at the
start of the file sets `mode` (ok | crash | hang), `sleep`, `language`, `chars` and `segments` (a transcript of that
many segments of that many characters), `answer` (the chat model's raw text), `echo_env`, `pidfile`."""

import json
import os
import subprocess
import sys
import time


def directive(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return {}
    if not head.startswith(b"FAKE "):
        return {}
    try:
        value = json.loads(head[5:].split(b"\n", 1)[0])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def text_directive(path):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {}
    index = text.find("FAKE ")
    if index < 0:
        return {}
    try:
        value = json.loads(text[index + 5:].split("\n", 1)[0])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class Runtime:
    def __init__(self, paths, options):
        self.paths, self.options = paths, options
        self.cache = {"warm": True}  # an in-memory cache that unload must clear
        if options.get("load_sleep"):
            time.sleep(float(options["load_sleep"]))

    def run(self, job):
        if "max_tokens" in job:  # a chat: the steering line sits in the prompt text
            d = text_directive(os.path.join(job["directory"], "prompt.txt"))
        else:
            d = directive(job["files"][0])
        if d.get("pidfile"):
            pids = [os.getpid()]
            if d.get("grandchild"):  # a process of the worker's group that only a group kill reaches
                pids.append(subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(600)"]).pid)
            with open(d["pidfile"], "w") as handle:
                handle.write(" ".join(str(pid) for pid in pids))
        mode = d.get("mode", "ok")
        if mode == "crash":
            os._exit(139)
        if mode == "hang":
            while True:
                time.sleep(1)
        time.sleep(float(d.get("sleep", 0)))
        phase = job["phase"]
        self.cache["jobs"] = self.cache.get("jobs", 0) + 1
        if d.get("echo_env"):
            env = {k: os.environ.get(k, "") for k in d["echo_env"]}
            return {"text": json.dumps(env), "segments": [{"start": 0, "end": 1, "text": json.dumps(env)}],
                    "language": "en", "duration": 1.0}
        if self.options.get("mode") == "langid":
            return {"language": d.get("language", "en")}
        if "max_tokens" in job:
            answer = d.get("answer")
            if answer is None:
                answer = json.dumps({"title": f"fake {phase}", "summary": f"jobs {self.cache['jobs']}",
                                     "document_type": "note", "text": "visible"})
            return {"text": answer, "prompt_tokens": 5, "completion_tokens": 3, "finish_reason": "stop"}
        count, chars = int(d.get("segments", 1)), int(d.get("chars", 0))
        text = lambda i: ("x" * chars) if chars else f"{phase} transcript {i}"  # noqa: E731
        segments = [{"start": i * 2.0, "end": i * 2.0 + 1.5, "text": text(i)} for i in range(count)]
        return {"segments": segments, "language": d.get("language", job.get("language") or "en"),
                "duration": job.get("duration", count * 2.0)}

    def unload(self):
        self.cache.clear()
        marker = self.options.get("unload_marker")
        if marker:
            with open(marker, "w") as handle:
                handle.write(f"cleared:{len(self.cache)}")


def load(paths, options):
    return Runtime(paths, options)
