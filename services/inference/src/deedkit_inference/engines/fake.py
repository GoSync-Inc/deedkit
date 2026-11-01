"""A test-only engine. The input file steers it: `FAKE {json}` at the start of the file sets `estimate`,
`sleep`, `mode` (ok | crash | hang | garbage | exit), `label`, `duration`, `megapixels`, `pidfile`.
Any other file content gets the default behaviour (answer at once)."""

import json
import sys
import threading

from . import MAX_AUDIO_SECONDS, MAX_PIXELS, Engine, Rejected

#: (event, label, phase index, slot) in the order the scheduler started and finished phases.
EVENTS = []
SPAWNED = []
SEEN_PATHS = []
LOCK = threading.Lock()


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


class FakeEngine(Engine):
    id = "fake"

    def precheck(self, path):
        d = directive(path)
        if self.kind == "audio" and d.get("duration", 0) > MAX_AUDIO_SECONDS:
            raise Rejected("audio_too_long")
        if self.kind == "chat" and d.get("megapixels", 0) > MAX_PIXELS / 1_000_000:
            raise Rejected("image_too_large")

    def estimate(self, path):
        d = directive(path)
        if "estimate" in d:
            return float(d["estimate"])
        if "duration" in d:
            return float(d["duration"]) * float(self.options.get("rtf", 0.1))
        return float(self.options.get("fixed_seconds", 1.0))

    def argv(self, ctx):
        label = directive(ctx.job.primary).get("label", "-")
        with LOCK:
            EVENTS.append(("start", label, ctx.index, ctx.slot))
            SPAWNED.append((label, ctx.index))
            SEEN_PATHS.append(ctx.job.primary)
        return [sys.executable, "-I", "-m", "deedkit_inference.engines.fake_child", str(ctx.job.primary),
                self.kind, str(ctx.index), str(ctx.count)]

    def parse(self, ctx, stdout):
        label = directive(ctx.job.primary).get("label", "-")
        with LOCK:
            EVENTS.append(("end", label, ctx.index, ctx.slot))
        return super().parse(ctx, stdout)

    def unload(self, phase_name):
        with LOCK:
            EVENTS.append(("unload", phase_name, -1, "-"))
