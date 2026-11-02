#!/usr/bin/python3 -I
"""Is the newest family-calendar backup snapshot recent enough? (ANYM-694)

Standard library only and read-only: no lock, no docker, no network. It reads ``last_snapshot_at`` from the calendar
backup's ``status.json`` and prints one JSON line. Exit codes: 0 fresh, 4 stale (older than the limit, or the file has
no ``last_snapshot_at``), 5 unknown (no file, unreadable, not a JSON object, a time that cannot be parsed or more than
five minutes in the future), 2 a refused argument, 6 the ``--write`` file could not be written. The age counts from the
snapshot, never from a delivery retry. See BACKUP.md, "Daily run and freshness".
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

FRESH, STALE, UNKNOWN = 0, 4, 5
WRITE_FAILED = 6
FUTURE_GRACE_SECONDS = 300  # a clock step just after a capture is not an alarm
READ_LIMIT = 1 << 20
REMEDY = ("calendar backup is not fresh: the catch-up runs at 19:15 UTC when the newest snapshot is over 22 h old; "
          "BACKUP.md, Daily run and freshness")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def hours(text):
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 168:
        raise argparse.ArgumentTypeError("the age limit is a whole number of hours from 1 to 168")
    return int(text)


def parse_time(value):
    """A timezone-aware ISO 8601 time, or None."""
    if not isinstance(value, str) or not 10 < len(value) <= 64:
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def read_status(path):
    """The status file as a dict, or the reason it cannot be read."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(READ_LIMIT + 1)
    except OSError as exc:
        return None, type(exc).__name__
    if len(raw) > READ_LIMIT:
        return None, "file too large"
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "not JSON"
    return (data, None) if isinstance(data, dict) else (None, "not a JSON object")


def evaluate(path, limit, now):
    """(exit code, status word, last_snapshot_at, age in hours or None, reason or None)."""
    data, reason = read_status(path)
    if data is None:
        return UNKNOWN, "unknown", None, None, reason
    value = data.get("last_snapshot_at")
    if value is None:
        return STALE, "stale", None, None, "no last_snapshot_at"
    moment = parse_time(value)
    if moment is None:
        return UNKNOWN, "unknown", None, None, "last_snapshot_at cannot be parsed"
    seconds = (now - moment).total_seconds()
    if seconds < 0:
        if -seconds > FUTURE_GRACE_SECONDS:
            return UNKNOWN, "unknown", value, None, "last_snapshot_at is in the future"
        seconds = 0.0
    age = round(seconds / 3600, 1)
    if seconds > limit * 3600:
        return STALE, "stale", value, age, None
    return FRESH, "fresh", value, age, None


def write_result(path, result):
    """Replace the result file atomically, mode 0644 so another user's `production.py status` can read it."""
    path = Path(path)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
            os.fchmod(handle.fileno(), 0o644)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def run(argv, *, now=None, out=print):
    parser = argparse.ArgumentParser(description="Is the newest calendar backup snapshot recent enough?")
    parser.add_argument("--status", required=True, help="the calendar backup's status.json")
    parser.add_argument("--max-age-hours", required=True, type=hours)
    parser.add_argument("--write", help="also write this result file (checked_at, status, exit_code, ...)")
    parser.add_argument("--remedy", action="store_true", help="name the remedy on stderr when not fresh")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    code, word, last, age, reason = evaluate(args.status, args.max_age_hours, now)
    line = {"status": word, "age_hours": age, "last_snapshot_at": last, "max_age_hours": args.max_age_hours}
    if reason:
        line["reason"] = reason
    out(json.dumps(line, sort_keys=True))
    if code != FRESH and args.remedy:
        print(REMEDY, file=sys.stderr)
    if args.write:
        result = {"checked_at": now.strftime(TIME_FORMAT), "last_snapshot_at": last,
                  "max_age_hours": args.max_age_hours, "status": word, "exit_code": code, "age_hours": age}
        try:
            write_result(args.write, result)
        except OSError as exc:
            print(f"freshness result not written: {type(exc).__name__}", file=sys.stderr)
            return WRITE_FAILED
    return code


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
