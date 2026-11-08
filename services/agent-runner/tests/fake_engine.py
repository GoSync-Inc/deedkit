"""A fake engine: the process a FakeDocker container runs. FAKE_MODE picks the behaviour; nothing contacts a vendor."""

import os
import pathlib
import subprocess
import sys
import time

mode = os.environ["FAKE_MODE"]
streams = pathlib.Path(os.environ["FAKE_STREAMS"])
workspace = pathlib.Path(os.environ["FAKE_WORKSPACE"])
out = pathlib.Path(os.environ["FAKE_OUT"])
prompt = sys.stdin.read()
with open(os.environ["FAKE_RUNS"], "a") as runs:
    runs.write(f"{out.name.removeprefix('agent-out-')}\n")
(out / "prompt.txt").write_text(prompt)


def emit(line):
    sys.stdout.write(line.rstrip("\n") + "\n")
    sys.stdout.flush()


if mode == "claude_ok":
    for line in (streams / "claude.jsonl").read_text().splitlines():
        emit(line)
elif mode == "codex_ok":
    for line in (streams / "codex.jsonl").read_text().splitlines():
        emit(line)
    (out / "result.txt").write_text("Fixed the typo.\nRESULT: partial — one file left for a person to check")
elif mode == "login":
    emit('{"type":"result","subtype":"success","is_error":true,"result":"Invalid API key · Please run /login"}')
    sys.exit(1)
elif mode == "fail":
    emit('{"type":"result","subtype":"error_during_execution","is_error":true,"result":"tool crashed"}')
    sys.exit(3)
elif mode == "sleep":
    emit('{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}')
    time.sleep(600)
elif mode == "detached":
    # A child that outlives the engine unless the container's whole process tree is killed (the real
    # setsid case runs in tests/test_docker_live.py).
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=False)
    (out / "child.pid").write_text(str(child.pid))
    emit('{"type":"assistant","message":{"content":[{"type":"text","text":"forked"}]}}')
    time.sleep(600)
elif mode == "write_secret":
    (workspace / "task-secret.txt").write_text("synthetic secret of one task")
    pathlib.Path(os.environ["FAKE_CODEX_HOME"], "history.jsonl").write_text("synthetic history")
    (out / "result.txt").write_text("wrote\nRESULT: done — wrote")
    emit('{"type":"item.completed","item":{"type":"agent_message","text":"wrote"}}')
elif mode == "list_workspace":
    seen = sorted(p.name for p in workspace.iterdir()) + sorted(
        p.name for p in pathlib.Path(os.environ["FAKE_CODEX_HOME"]).iterdir())
    (out / "result.txt").write_text("seen: " + " ".join(seen))
elif mode == "flood":
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"' + "x" * 1000 + '"}]}}'
    for _ in range(7000):
        emit(line)
else:
    sys.exit(f"unknown mode {mode}")
