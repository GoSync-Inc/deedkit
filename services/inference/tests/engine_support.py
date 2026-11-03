"""Test support for the real engines (ANYM-683) running on the fake runtime: fake ffprobe/ffmpeg, a synthetic model
store with matching pins, and the alias patches that turn the shipped table into one a test can run."""

import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from deedkit_inference.aliases import shipped_aliases

RUNTIME = "deedkit_inference.engines.runtimes.fake"
PROBE_SCRIPT = f"""#!{sys.executable}
import json, sys
head = open(sys.argv[-1], "rb").read(4096)
d = json.loads(head[5:].split(b"\\n", 1)[0]) if head.startswith(b"FAKE ") else {{}}
if d.get("unreadable"):
    sys.exit(1)
print(d.get("duration", 3.0))
"""


#: A valid 1x1 PNG (the contract fixtures' image): the vision engine reads the header before any model.
TINY_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def vis(**directive):
    """A chat body for vision-default: a real image, the steering line in the prompt text."""
    from support import chat_body

    return chat_body(image=TINY_PNG, text="FAKE " + json.dumps(directive))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_tools(tmp):
    """A fake ffprobe (prints the `duration` of the steering line) and a fake ffmpeg (never used with the fake runtime)."""
    probe, mpeg = Path(tmp) / "ffprobe", Path(tmp) / "ffmpeg"
    probe.write_text(PROBE_SCRIPT)
    mpeg.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    for tool in (probe, mpeg):
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return str(mpeg), str(probe)


def phase_files(alias, phase):
    return {"config.json": b"{}", "weights.safetensors": f"synthetic weights of {alias}/{phase}".encode()}


def synthetic_store(tmp, extra=None):
    """(models.json path, model dir): every shipped phase with tiny files whose pins match. `extra` maps
    `alias/phase/artifact/file` to replacement bytes that are pinned (so a bad file passes the hash check)."""
    extra = extra or {}
    store, phases = Path(tmp) / "models", {}
    for alias, spec in shipped_aliases().items():
        for phase in spec["phases"]:
            artifacts = ["model", "vad"] if phase["name"] == "gigaam" else ["model"]
            entry = {}
            for name in artifacts:
                files = {}
                for file_name, data in phase_files(alias, phase["name"]).items():
                    data = extra.get(f"{alias}/{phase['name']}/{name}/{file_name}", data)
                    directory = store / alias / phase["name"] / name
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / file_name).write_bytes(data)
                    files[file_name] = {"size": len(data), "sha256": sha256(data)}
                entry[name] = {"repo": "synthetic/model", "revision": "a" * 40, "files": files}
            phases[f"{alias}/{phase['name']}"] = entry
    models = Path(tmp) / "models.json"
    models.write_text(json.dumps({"version": 1, "packages": {}, "phases": phases}))
    return models, store


def engines(env, *, timeout=20, extra_options=None, store_extra=None, **over):
    """Keyword arguments for `env.running(...)`: the shipped aliases on the fake runtime, a synthetic store."""
    ffmpeg, ffprobe = write_tools(env.tmp)
    models, store = synthetic_store(env.tmp, store_extra)
    aliases = {}
    for name, spec in shipped_aliases().items():
        options = {"runtime": RUNTIME, "ffmpeg": ffmpeg, "ffprobe": ffprobe, **(extra_options or {})}
        aliases[name] = {**spec, "engine_options": options, "timeout_seconds": timeout}
    return {"aliases": aliases, "models_file": str(models), "model_dir": str(store), **over}


def flip_in_place(path):
    """Change one byte without changing size or mtime."""
    before = os.stat(path)
    data = bytearray(Path(path).read_bytes())
    data[0] ^= 0xFF
    Path(path).write_bytes(bytes(data))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
