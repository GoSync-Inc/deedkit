"""A runtime for CI: no library, no model. The uploaded file steers it like the fake engine's: `FAKE {json}` at the
start of the file sets `mode` (ok | crash | hang), `sleep`, `language`, `chars` and `segments` (a transcript of that
many segments of that many characters), `answer` (the chat model's raw text), `echo_env`, `pidfile`. A document
(ANYM-850) carries the line after its magic bytes (`%PDF-1.7 FAKE {...}`): `refused`, `pages`, `markdown`,
`network_attempts`, `peak_rss_mb`, `raw` (printed as the whole result), `alloc_mb` (memory held while it sleeps)."""

import json
import os
import subprocess
import sys
import time

from deedkit_inference import media

from ..ticker import tick


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


def embedded_directive(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return {}
    index = head.find(b"FAKE ")
    if index < 0:
        return {}
    try:
        value = json.loads(head[index + 5:].split(b"\n", 1)[0])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def document(d, jobs):
    if d.get("refused"):
        return {"refused": d["refused"]}
    if "raw" in d:
        return d["raw"]
    if d.get("alloc_mb"):
        held = bytearray(int(d["alloc_mb"]) * 1024 * 1024)  # touched, so it is resident
        for i in range(0, len(held), 4096):
            held[i] = 1
        time.sleep(float(d.get("hold", 30)))
    pages = int(d.get("pages", 2))
    parts = [d.get("markdown", f"## Страница {n}\n\n| a | b |\n|---|---|\n| {n} | {jobs} |") for n in range(1, pages + 1)]
    markdown, spans, offset = "", [], 0
    for n, part in enumerate(parts, 1):
        if n > 1:
            markdown += "\n\n"
            offset += 2
        spans.append({"page": n, "start": offset, "end": offset + len(part)})
        markdown += part
        offset += len(part)
    return {"markdown": markdown, "truncated": False, "page_spans": spans,
            "coverage": {"pages": pages, "pages_with_text": pages, "tables": pages, "headings": pages},
            "engine": {"name": "docling", "version": "2.149.0", "models": {"layout": "a" * 40},
                       "network_attempts": int(d.get("network_attempts", 0)), "peak_rss_mb": int(d.get("peak_rss_mb", 120))}}


class Runtime:
    def __init__(self, paths, options):
        self.paths, self.options = paths, options
        self.cache = {"warm": True}  # an in-memory cache that unload must clear
        if options.get("load_sleep"):
            time.sleep(float(options["load_sleep"]))

    def run(self, job):
        if "max_tokens" in job:  # a chat: the steering line sits in the prompt text
            d = text_directive(os.path.join(job["directory"], "prompt.txt"))
        elif "max_pages" in job:  # a document: the steering line follows the magic bytes
            d = embedded_directive(job["file"])
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
        time.sleep(float(d.get("pre", 0)))  # work before the decode: a stop here lands before the marker exists
        if d.get("decode"):  # a decode helper behind the no-pause marker, as media.iter_chunks runs ffmpeg
            code, _ = media.run_tool([sys.executable, "-I", "-c", f"import time; time.sleep({float(d['decode']['seconds'])})"],
                                     float(d["decode"]["timeout"]), own_group=False,
                                     marker=os.path.join(os.getcwd(), media.MARKER_NAME))
            if code != 0:
                raise RuntimeError("decode_failed")
        if d.get("tick"):
            tick(d["tick"], int(d.get("ticks", 100)))
        time.sleep(float(d.get("sleep", 0)))
        phase = job["phase"]
        self.cache["jobs"] = self.cache.get("jobs", 0) + 1
        if "max_pages" in job:
            return document(d, self.cache["jobs"])
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
        if self.options.get("unload_sleep"):  # a slow unload: the parent must keep counting this process
            time.sleep(float(self.options["unload_sleep"]))
        self.cache.clear()
        marker = self.options.get("unload_marker")
        if marker:
            with open(marker, "w") as handle:
                handle.write(f"cleared:{len(self.cache)}")


def load(paths, options):
    return Runtime(paths, options)
