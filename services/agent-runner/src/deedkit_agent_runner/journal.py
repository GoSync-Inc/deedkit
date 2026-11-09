"""Per-task log files and the audit file.

A task log is JSON lines `{time, kind, text, raw}`, at most 5 MiB, then one marker line. Pages are at most 64 KiB,
addressed by an opaque cursor (the byte offset, base64). The audit holds hashes, never the texts.
"""

import base64
import hashlib
import json
import os
import threading

LOG_LIMIT = 5 * 1024 * 1024
PAGE_LIMIT = 64 * 1024
AUDIT_DAYS = 365
MARKER = "log truncated at 5 MiB"


def sha256(text):
    return hashlib.sha256((text or "").encode()).hexdigest()


def encode_cursor(offset):
    return base64.urlsafe_b64encode(f"o{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor):
    if not cursor:
        return 0
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        if not text.startswith("o"):
            raise ValueError
        value = int(text[1:])
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        return None


def private_append(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


class TaskLog:
    def __init__(self, path, clock):
        self.path, self.clock = path, clock
        self.size = path.stat().st_size if path.exists() else 0
        self.truncated = False

    def write(self, kind, text, raw):
        if self.truncated:
            return
        line = (json.dumps({"time": self.clock(), "kind": kind, "text": text, "raw": raw}, ensure_ascii=False)
                + "\n").encode()
        if self.size + len(line) > LOG_LIMIT:
            line = (json.dumps({"time": self.clock(), "kind": "result", "text": MARKER, "raw": ""}) + "\n").encode()
            self.truncated = True
        private_append(self.path, line)
        self.size += len(line)


def page(path, cursor, raw):
    """One log page: entries from `cursor`, at most PAGE_LIMIT bytes of source lines."""
    offset = decode_cursor(cursor)
    if offset is None:
        return None
    if not path.exists():
        return {"entries": [], "next_cursor": encode_cursor(offset), "end": True}
    with open(path, "rb") as handle:
        handle.seek(offset)
        chunk = handle.read(PAGE_LIMIT)
    entries, used = [], 0
    for line in chunk.splitlines(keepends=True):
        if not line.endswith(b"\n") or used + len(line) > PAGE_LIMIT:
            break
        used += len(line)
        value = json.loads(line)
        entries.append({"time": value["time"], "text": value["raw"]} if raw else
                       {"time": value["time"], "kind": value["kind"], "text": value["text"]})
    if not entries and chunk:
        # One line longer than a page: skip it rather than loop on the same cursor.
        used = chunk.find(b"\n") + 1 or len(chunk)
    end = offset + used >= path.stat().st_size
    return {"entries": entries, "next_cursor": encode_cursor(offset + used), "end": end}


class Audit:
    def __init__(self, path, clock):
        self.path, self.clock, self._lock = path, clock, threading.Lock()

    def record(self, task, state, key_id=None):
        entry = {"time": self.clock(), "key_id": key_id, "principal": task["principal"], "task_id": task["id"],
                 "request_id": task["request_id"], "engine": task["engine"],
                 "instruction_sha256": sha256(task.get("instruction")),
                 "owner_instruction_sha256": sha256(task.get("owner_instruction")),
                 "state": state, "exit_code": task.get("exit_code")}
        with self._lock:
            private_append(self.path, (json.dumps(entry, sort_keys=True) + "\n").encode())

    def prune(self):
        cutoff = self.clock() - AUDIT_DAYS * 86400
        with self._lock:
            if not self.path.exists():
                return 0
            lines = self.path.read_bytes().splitlines(keepends=True)
            keep = [line for line in lines if json.loads(line)["time"] >= cutoff]
            if len(keep) != len(lines):
                temporary = self.path.with_suffix(".tmp")
                temporary.unlink(missing_ok=True)
                private_append(temporary, b"".join(keep))
                os.replace(temporary, self.path)
            return len(lines) - len(keep)
