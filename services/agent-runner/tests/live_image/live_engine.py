"""The fake engine inside the live test image: answers `--help` and `status` like the pinned engines and runs the
mode the prompt names (`MODE=<name>`). It never contacts anyone; the test containers have no network."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

engine, args = sys.argv[1], sys.argv[2:]
HELP = {"claude": "-p, --print\n--output-format\n--verbose\n--permission-mode\n--no-session-persistence\n",
        "codex": "--json\n--ephemeral\n-s, --sandbox\n-C, --cd\n-o, --output-last-message\n"}


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def finish(text):
    if engine == "codex":
        with open("/out/result.txt", "w") as handle:
            handle.write(text)
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    else:
        emit({"type": "result", "subtype": "success", "is_error": False, "result": text})


def attempt(action):
    try:
        action()
        return "ok"
    except OSError as error:
        return __import__("errno").errorcode.get(error.errno, str(error.errno))


if "--help" in args:
    print(HELP[engine])
    sys.exit(0)
if args[-1:] == ["status"]:
    print("Logged in (synthetic)")
    sys.exit(0)

signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
prompt = sys.stdin.read()
match = re.search(r"MODE=(\w+)", prompt)
mode = match.group(1) if match else "ok"

if mode == "ok":
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
    finish("done\nRESULT: done — synthetic")
elif mode == "probe":
    status = dict(line.split(":\t", 1) for line in open("/proc/self/status").read().splitlines() if ":\t" in line)
    report = {
        "uid": os.getuid(), "cap_eff": status["CapEff"].strip(), "no_new_privs": status["NoNewPrivs"].strip(),
        "root_write": attempt(lambda: open("/probe", "w").close()),
        "workspace_write": attempt(lambda: open("/workspace/probe", "w").close()),
        "docker_socket": os.path.exists("/var/run/docker.sock"),
        "env": sorted(os.environ), "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
        "mounts": sorted(line.split()[1] for line in open("/proc/self/mounts")),
        "prompt_has_preamble": "agent-runner preamble v1" in prompt,
    }
    finish(json.dumps(report))
elif mode == "detached":
    child = subprocess.Popen(["sleep", "600"], start_new_session=True)  # setsid: outside the engine's group
    with open("/out/child.pid", "w") as handle:
        handle.write(str(child.pid))
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "forked"}]}})
    time.sleep(600)
elif mode == "memory":
    hold = []
    while True:
        hold.append(b"x" * (64 * 1024 * 1024))
elif mode == "disk":
    chunk = b"x" * (1024 * 1024)

    def fill(path):
        with open(path, "wb") as handle:
            while True:
                handle.write(chunk)
                handle.flush()

    finish(json.dumps({"workspace": attempt(lambda: fill("/workspace/fill")), "tmp": attempt(lambda: fill("/tmp/fill")),
                       "root": attempt(lambda: fill("/fill"))}))
elif mode == "pids":
    started = 0
    try:
        for _ in range(700):
            threading.Thread(target=time.sleep, args=(30,), daemon=True).start()
            started += 1
    except RuntimeError:
        pass
    finish(json.dumps({"threads": started}))
else:
    sys.exit(f"unknown mode {mode}")
