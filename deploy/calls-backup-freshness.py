#!/usr/bin/python3 -I
"""Is the calls state's protected backup recent? (ANYM-782)

Runs on the backup server. Standard library only and read-only on the calls verifier's directory: no lock, no docker, no
network. It reads ``last-success.json``, the proof of exactly that verification (``proof-<verification_id>.json``, never a
newer proof whose promotion failed) and ``last-failure.json``, and prints one JSON line. The age counts from the proof's
``captured_at``, so neither a rejected delivery nor a late retry of an old snapshot reads fresh. The runner's status file on
the production host is delivery state and is never read here (BACKUP.md, "Calls state: freshness").

Exit codes: 0 fresh, 4 stale (no success yet, or older than the limit), 5 unknown (no verifier directory, an unreadable
file, another profile's proof, a time that cannot be parsed or lies more than five minutes ahead), 2 a refused argument.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

FRESH, STALE, UNKNOWN = 0, 4, 5
DEFAULT_MAX_AGE_HOURS = 36
FUTURE_GRACE_SECONDS = 300
READ_LIMIT = 1 << 20


def hours(text):
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 168:
        raise argparse.ArgumentTypeError("the age limit is a whole number of hours from 1 to 168")
    return int(text)


def parse_time(value):
    if not isinstance(value, str) or not 10 < len(value) <= 64:
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def read_object(path):
    """The JSON object in `path`, None when the file is absent, or ValueError for anything unreadable."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(READ_LIMIT + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(type(exc).__name__) from None
    if len(raw) > READ_LIMIT:
        raise ValueError("file too large")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("not JSON") from None
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def captured_at(directory, success):
    """The capture time in the calls proof of the verification `success` names; ValueError when it is not that proof."""
    verification_id = success.get("verification_id")
    if success.get("status") != "verified" or not isinstance(verification_id, str) or not re.fullmatch("[a-f0-9]{32}", verification_id):
        raise ValueError("last-success has no verification_id")
    proof = read_object(Path(directory) / f"proof-{verification_id}.json")
    if proof is None:
        raise ValueError("the proof of the last success is missing")
    verification = proof.get("verification")
    if not isinstance(verification, dict) or verification.get("profile") != "calls" or verification.get("id") != verification_id:
        raise ValueError("not the calls proof of the last success")
    return verification.get("captured_at")


def evaluate(directory, limit, now) -> tuple[int, dict]:
    """(exit code, result dict). The limit applies to the snapshot age."""
    base = Path(directory)
    if not base.is_dir():
        return UNKNOWN, {"status": "unknown", "reason": "no verifier directory"}
    try:
        success, failure = read_object(base / "last-success.json"), read_object(base / "last-failure.json")
        value = captured_at(base, success) if success is not None else None
    except (ValueError, OSError) as exc:
        return UNKNOWN, {"status": "unknown", "reason": str(exc)[:60]}
    result = {"max_age_hours": limit}
    if failure is not None and isinstance(failure.get("error"), str):
        result["last_error"] = failure["error"][:64]
        result["last_error_at"] = failure.get("failed_at") if isinstance(failure.get("failed_at"), str) else None
    if success is None:
        return STALE, {**result, "status": "stale", "reason": "no verified success yet"}
    captured = parse_time(value)
    if captured is None:
        return UNKNOWN, {**result, "status": "unknown", "reason": "captured_at cannot be parsed"}
    seconds = (now - captured).total_seconds()
    if seconds < -FUTURE_GRACE_SECONDS:
        return UNKNOWN, {**result, "captured_at": value, "status": "unknown", "reason": "captured_at lies in the future"}
    result.update(captured_at=value, age_hours=round(max(seconds, 0) / 3600, 1))
    return (STALE, {**result, "status": "stale"}) if seconds > limit * 3600 else (FRESH, {**result, "status": "fresh"})


def run(argv, *, now=None, out=print):
    parser = argparse.ArgumentParser(description="Is the calls state's protected backup recent?")
    parser.add_argument("--verifier-directory", required=True, help="the calls verifier's directory")
    parser.add_argument("--max-age-hours", type=hours, default=DEFAULT_MAX_AGE_HOURS)
    args = parser.parse_args(argv)
    code, result = evaluate(args.verifier_directory, args.max_age_hours, now or datetime.now(timezone.utc))
    out(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
