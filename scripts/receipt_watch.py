#!/usr/bin/env python3
"""Wait, read-only, until a merged main SHA is deployed and settled on production, or deploys are paused (ANYM-440).

    receipt_watch.py <sha> [--timeout 14400] [--interval 60] [--host deedkit-prod]

Each poll runs one constant `ssh` command that feeds REMOTE to python3 on the host: it opens receipts for reading, lists
directories and prints a projection (sha, status, reason, time, pause, unfinished attempts), never a receipt. The SHA
never reaches the remote command; ancestry is decided here with git. Exit 0 LIVE, 1 UNSETTLED or REFUSED,
2 TIMEOUT or UNREACHABLE, 64 usage. One stdout line.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

SHA = re.compile(r"[0-9a-f]{40}")
ROOT = Path(__file__).resolve().parents[1]
SSH_TIMEOUT = 60
GIT_TIMEOUT = 60
FETCH_TIMEOUT = 120
UNREACHABLE_AFTER = 5
TEXT_LIMIT = 200
SNAPSHOT_KEYS = {"plan_a", "http", "deploy", "pause", "unresolved", "served"}
# Runs on the host under python3 and only reads. Production.py's own rules name the unfinished attempts: a plan A
# attempt without a result, an `unhealthy` result without a rollback, a rollback attempt without its outcome
# (production.py `unfinished`, `unhealthy`) and an HTTP attempt that neither serves nor failed before admission.
REMOTE = r'''
import json
import os
ETC = %(etc)r
def text(path, limit=65536, whole=False):
    try:
        with open(path) as stream:
            raw = stream.read(limit + 1)
    except (OSError, ValueError):
        return None
    return None if whole and len(raw) > limit else raw[:limit]
def load(path):
    raw = text(path, whole=True)
    try:
        value = json.loads(raw) if raw is not None else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
def env(name, default):
    for line in (text(ETC + "/autodeploy.env") or "").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("\"'") or default
    return default
def pick(value):
    if value is None:
        return None
    return {key: value[key][:200] for key in ("sha", "status", "reason", "at") if isinstance(value.get(key), str)}
def state(config):
    value = load(config)
    directory = value.get("state_directory") if value else None
    return directory if isinstance(directory, str) and directory.startswith("/") else None
unreadable = []
def children(directory):
    if directory is None:
        return []
    try:
        names, found = os.listdir(directory), []
    except OSError:
        unreadable.append(os.path.basename(directory.rstrip("/")) + ":unreadable")
        return []
    for name in sorted(names):
        try:
            if os.stat(directory + "/" + name).st_mode & 0o170000 == 0o040000:
                found.append(name)
        except FileNotFoundError:
            pass
        except OSError:
            unreadable.append(name + ":unreadable")
    return found
def has(path):
    try:
        os.stat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        unreadable.append(path.rsplit("/", 2)[-2] + "/" + path.rsplit("/", 1)[-1] + ":unreadable")
        return False
def snapshot():
    plan = state(env("DEEDKIT_DEPLOY_CONFIG", ETC + "/production.json"))
    http = state(env("DEEDKIT_HTTP_CONFIG", ETC + "/http-production.json"))
    unresolved, served = [], []
    del unreadable[:]
    current = load(plan + "/current.json") if plan else None
    for name in children(plan):
        base = plan + "/" + name
        result = load(base + "/result.json")
        if has(base + "/result.json") and result is None:
            unresolved.append(name + ":result-unreadable")
        if has(base + "/rollback.json") and current and current.get("sha") == name and current.get("status") == "applied":
            unresolved.append(name + ":rolled-back-but-current-applied")
        if has(base + "/attempted.json") and (not has(base + "/result.json") or (
                (result or {}).get("status") == "unhealthy" and not has(base + "/rollback.json"))):
            unresolved.append(name + ":apply")
        if has(base + "/rollback-attempted.json") and not has(base + "/rollback.json"):
            unresolved.append(name + ":rollback")
    for name in children(http):
        path = http + "/" + name + "/http-attempted.json"
        status = (load(path) or {}).get("status")
        if has(path) and status not in ("serving", "startup-failed"):
            unresolved.append(name + ":http")
        if has(path) and status == "serving":
            served.append(name)
    pause = ETC + "/autodeploy.pause"
    raw = text(pause)
    reason = None
    if raw is not None:
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        reason = value.get("reason") if isinstance(value, dict) else raw
        reason = (reason[:200] or None) if isinstance(reason, str) else None
    return {"plan_a": pick(load(plan + "/current.json")) if plan else None,
            "http": pick(load(http + "/http-current.json")) if http else None,
            "deploy": pick(load(plan + "/deploy.json")) if plan else None,
            "pause": {"present": os.path.lexists(pause), "reason": reason},
            "unresolved": (unresolved + unreadable)[:10], "served": served}
first = snapshot()
print(json.dumps(first if first == snapshot() else {"unstable": True}))
'''


def remote_script(etc: str = "/etc/deedkit") -> str:
    return REMOTE % {"etc": etc}


def ssh_command(host: str) -> list[str]:
    """The script travels on stdin, so no quoting reaches the remote shell and the command never changes."""
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-"]


def valid_state(value: object) -> bool:
    return value is None or (isinstance(value, dict) and all(isinstance(item, str) for item in value.values()))


def valid(value: object) -> bool:
    if value == {"unstable": True}:
        return True
    if not isinstance(value, dict) or set(value) != SNAPSHOT_KEYS:
        return False
    pause, unresolved = value["pause"], value["unresolved"]
    if not isinstance(value["served"], list) or not all(isinstance(item, str) for item in value["served"]):
        return False
    return (all(valid_state(value[key]) for key in ("plan_a", "http", "deploy")) and isinstance(pause, dict)
            and set(pause) == {"present", "reason"} and isinstance(pause["present"], bool)
            and (pause["reason"] is None or isinstance(pause["reason"], str))
            and isinstance(unresolved, list) and all(isinstance(item, str) for item in unresolved))


def probe(host: str, timeout: float = SSH_TIMEOUT) -> dict:
    """The projection from the host; raises ValueError when ssh fails, times out or answers badly."""
    try:
        completed = subprocess.run(ssh_command(host), input=remote_script(), capture_output=True, text=True,
                                   timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("ssh could not run or timed out") from None
    if completed.returncode:
        raise ValueError(f"ssh exited {completed.returncode}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise ValueError("the host answered with malformed JSON") from None
    if not valid(value):
        raise ValueError("the host answered with an unexpected shape")
    return value


def shown(text: object, limit: int = TEXT_LIMIT) -> str:
    """Receipt text is data: ASCII only, control characters escaped, bounded."""
    text = text if isinstance(text, str) else str(text)
    text = text if text.isprintable() and text.isascii() else text.encode("unicode_escape").decode("ascii")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def short(state: dict | None) -> str:
    state = state or {}
    return f"{shown(state.get('sha', '-'))[:12]}/{shown(state.get('status', '-'), 30)}"


class Watch:
    def __init__(self, sha: str, root: Path, left):
        self.sha, self.root, self.left, self.fetched = sha, root, left, False

    def budget(self, limit: float) -> float | None:
        """The seconds a child process may take: the limit, cut to what the overall deadline leaves; None when none."""
        left = self.left()
        return min(limit, left) if left > 0 else None

    def ancestry(self, older: str, newer: str) -> bool | None:
        """True when OLDER is NEWER or its ancestor, False when not, None when unknown or out of time."""
        if older == newer:
            return True
        timeout = self.budget(GIT_TIMEOUT)
        if timeout is None:
            return None
        try:
            code = subprocess.run(["git", "-C", str(self.root), "merge-base", "--is-ancestor", older, newer],
                                  capture_output=True, timeout=timeout).returncode
        except (OSError, subprocess.SubprocessError):
            return None
        return {0: True, 1: False}.get(code)

    def related(self, older: str, newer: str) -> bool | None:
        """`ancestry`, with one fetch per run when a commit is unknown; the answer is read again after the fetch."""
        answer = self.ancestry(older, newer)
        if answer is None and not self.fetched:
            timeout = self.budget(FETCH_TIMEOUT)
            if timeout is None:
                return None
            self.fetched = True
            try:
                subprocess.run(["git", "-C", str(self.root), "fetch", "-q", "origin", "main"], capture_output=True,
                               timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                return None
            answer = self.ancestry(older, newer)
        return answer

    def reaches(self, state: dict | None) -> bool | None:
        """Whether this receipt's release contains the watched SHA."""
        other = (state or {}).get("sha")
        if not isinstance(other, str) or not SHA.fullmatch(other):
            return False
        return self.related(self.sha, other)

    def verdict(self, snapshot: dict) -> tuple[str, str] | None:
        """(LIVE | UNSETTLED | REFUSED, detail) once decided, else None. An unknown relation never decides."""
        plan, http, pause = snapshot["plan_a"] or {}, snapshot["http"] or {}, snapshot["pause"]
        unresolved, unknown = list(snapshot["unresolved"]), False
        # production.py rejects a `serving` HTTP attempt that http-current.json does not name or contain
        # (promote stopped before it published the receipt).
        for name in snapshot["served"]:
            if name == http.get("sha"):
                continue
            related = self.related(name, http["sha"]) if SHA.fullmatch(name) and SHA.fullmatch(http.get("sha", "")) \
                else False
            if related is False:
                unresolved.append(name + ":http-unpublished")
            unknown = unknown or related is None
        applied = plan.get("status") == "applied" and http.get("status") in ("serving", "serving-retirement-pending")
        both = applied and self.reaches(plan) is True and self.reaches(http) is True
        if both and unknown:
            return None
        if both and not pause["present"] and not unresolved:
            return "LIVE", f"plan-a={plan['sha'][:12]} http={http['sha'][:12]}"
        if both:
            why = f"deploys are paused: {shown(pause['reason'] or 'no reason')}" if pause["present"] else \
                "an attempt is unfinished: " + shown(", ".join(unresolved))
            return "UNSETTLED", f"the receipts record it applied, but {why}"
        if pause["present"]:
            deploy = snapshot["deploy"] or {}
            reason = pause["reason"] or (deploy.get("reason") if deploy.get("status") in ("halted", "rolled-back")
                                         else None)
            return "REFUSED", f"deploys are paused: {shown(reason or 'no reason')}"
        return None


def watch(sha: str, host: str, timeout: float, interval: float, out=print, root: Path = ROOT, probe=probe,
          clock=time.monotonic, sleep=time.sleep) -> int:
    deadline, failures, last = clock() + timeout, 0, "nothing read yet"
    machine = Watch(sha, root, lambda: deadline - clock())

    def end(status: str, detail: str, code: int) -> int:
        out(f"{status} {sha[:12]} {detail}")
        return code

    while True:
        left = deadline - clock()
        if left <= 0:
            return end("TIMEOUT", f"after {timeout:.0f}s, last: {last}", 2)
        try:
            snapshot = probe(host, min(SSH_TIMEOUT, left))
        except ValueError as error:
            failures += 1
            if deadline - clock() <= 0:
                return end("TIMEOUT", f"after {timeout:.0f}s, last: {shown(error)}", 2)
            if failures >= UNREACHABLE_AFTER:
                return end("UNREACHABLE", f"{shown(error)} ({failures} polls in a row)", 2)
        else:
            failures = 0
            if "unstable" in snapshot:
                last = "the receipts changed between two reads"
            else:
                answer = machine.verdict(snapshot)
                if answer and deadline - clock() <= 0:
                    return end("TIMEOUT", f"after {timeout:.0f}s, the verdict came too late: {answer[0]}", 2)
                if answer:
                    return end(answer[0], answer[1], 0 if answer[0] == "LIVE" else 1)
                last = (f"plan-a={short(snapshot['plan_a'])} http={short(snapshot['http'])} "
                        f"deploy={short(snapshot['deploy'])} unresolved={len(snapshot['unresolved'])}")
        left = deadline - clock()
        if left > 0:
            sleep(min(interval, left))


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("sha")
    parser.add_argument("--timeout", type=float, default=14400)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--host", default="deedkit-prod")
    args = parser.parse_args()
    if not SHA.fullmatch(args.sha) or not all(math.isfinite(v) and v > 0 for v in (args.timeout, args.interval)):
        print("usage: receipt_watch.py <40-hex main sha> [--timeout S] [--interval S] [--host H]", file=sys.stderr)
        return 64
    return watch(args.sha, args.host, args.timeout, args.interval)


if __name__ == "__main__":
    sys.exit(main())
