#!/usr/bin/python3 -I
"""Is the recordings archive backup recent, and is the archive still moving? (ANYM-728)

Standard library only and read-only on the recordings verifier's directory: no lock, no docker, no network. It reads
``last-success.json``, the proof of exactly that verification (``proof-<verification_id>.json``, never a newer proof
whose promotion failed) and ``last-failure.json``, and prints one JSON line: the last verified success, the snapshot
age (from the bundle manifest's capture time, authenticated by the decrypting verifier and carried in the proof), the
verification age, the last error code and time, the protected point's bytes and the archive identity (an opaque ID and
three counters; no title, no text). With ``--state`` it also remembers the archive's counters and says
``unchanged_for_7_days`` once they have not moved for that long: an archive that is healthy but no longer collecting.
The collector's own last success and error code are the recordings service's `POST /v1/archive_status`; this script
never calls it (BACKUP.md, "Recordings archive: freshness").

Exit codes: 0 fresh, 4 stale (no success yet, or older than the limit), 5 unknown (no verifier directory, an unreadable
file, a time that cannot be parsed or lies more than five minutes ahead), 2 a refused argument, 6 the ``--write`` or
``--state`` file could not be written.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile

FRESH, STALE, UNKNOWN, WRITE_FAILED = 0, 4, 5, 6
FUTURE_GRACE_SECONDS = 300
READ_LIMIT = 1 << 20
UNCHANGED_DAYS = 7
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ARCHIVE_KEYS = ("archive_id", "incarnation", "sequence", "records")


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


def proof_of(directory, verification_id):
    """(bytes, archive facts, capture time) of the proof of that verification; ValueError when it is not there."""
    if not isinstance(verification_id, str) or not re.fullmatch("[a-f0-9]{32}", verification_id):
        raise ValueError("last-success has no verification_id")
    proof = read_object(Path(directory) / f"proof-{verification_id}.json")
    if proof is None:
        raise ValueError("the proof of the last success is missing")
    backup, verification = proof.get("backup"), proof.get("verification")
    size = backup.get("bytes") if isinstance(backup, dict) else None
    archive = verification.get("archive") if isinstance(verification, dict) else None
    captured = verification.get("captured_at") if isinstance(verification, dict) else None
    if not isinstance(archive, dict) or set(archive) != set(ARCHIVE_KEYS):
        archive = None
    return (size if type(size) is int else None), archive, captured


def unchanged(state_path, archive, now):
    """(whole days the archive's counters have not moved, the state to write)."""
    if archive is None:
        return None, None
    before = read_object(state_path) if state_path else None
    since = parse_time(before.get("since")) if before else None
    if before and before.get("archive") == archive and since is not None and since <= now:
        return (now - since).days, before
    return 0, {"archive": archive, "since": now.strftime(TIME_FORMAT)}


def write_json(path, value, mode):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
            os.fchmod(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def evaluate(directory, limit, now) -> tuple[int, dict]:
    """(exit code, result dict). The limit applies to the snapshot age."""
    base = Path(directory)
    if not base.is_dir():
        return UNKNOWN, {"status": "unknown", "reason": "no verifier directory"}
    try:
        success, failure = read_object(base / "last-success.json"), read_object(base / "last-failure.json")
        proof = proof_of(base, success.get("verification_id")) if success is not None else None
    except (ValueError, OSError) as exc:
        return UNKNOWN, {"status": "unknown", "reason": str(exc)[:60]}
    result = {"max_age_hours": limit, "bytes": None, "archive": None}
    if failure is not None and isinstance(failure.get("error"), str):
        result["last_error"] = failure["error"][:64]
        result["last_error_at"] = failure.get("failed_at") if isinstance(failure.get("failed_at"), str) else None
    if success is None or proof is None:
        return STALE, {**result, "status": "stale", "reason": "no verified success yet"}
    result["bytes"], result["archive"] = proof[0], proof[1]
    verified, captured = parse_time(success.get("verified_at")), parse_time(proof[2])
    if verified is None or captured is None:
        return UNKNOWN, {**result, "status": "unknown", "reason": "verified_at or captured_at cannot be parsed"}
    seen = {"verified_at": success["verified_at"], "captured_at": proof[2]}
    snapshot_seconds = (now - captured).total_seconds()
    verified_seconds = (now - verified).total_seconds()
    if min(snapshot_seconds, verified_seconds) < -FUTURE_GRACE_SECONDS:
        return UNKNOWN, {**result, **seen, "status": "unknown", "reason": "a recorded time lies in the future"}
    result.update(seen, age_hours=round(max(snapshot_seconds, 0) / 3600, 1),
                  verified_age_hours=round(max(verified_seconds, 0) / 3600, 1))
    if snapshot_seconds > limit * 3600:
        return STALE, {**result, "status": "stale"}
    return FRESH, {**result, "status": "fresh"}


def run(argv, *, now=None, out=print):
    parser = argparse.ArgumentParser(description="Is the recordings archive backup verified recently?")
    parser.add_argument("--verifier-directory", required=True, help="the recordings verifier's directory")
    parser.add_argument("--max-age-hours", required=True, type=hours)
    parser.add_argument("--state", help="remembers the archive's counters for the unchanged hint (written)")
    parser.add_argument("--write", help="also write this result file")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    code, result = evaluate(args.verifier_directory, args.max_age_hours, now)
    failed = False
    if result.get("archive") is not None:
        try:
            days, state = unchanged(args.state, result["archive"], now)
        except ValueError:
            days, state = 0, {"archive": result["archive"], "since": now.strftime(TIME_FORMAT)}
        days = days or 0
        result["unchanged_days"] = days
        if days >= UNCHANGED_DAYS:
            result["hint"] = "unchanged_for_7_days"
        if args.state and state is not None:
            try:
                write_json(args.state, state, 0o600)
            except OSError as exc:
                print(f"state not written: {type(exc).__name__}", file=sys.stderr)
                failed = True
    out(json.dumps(result, sort_keys=True))
    if args.write:
        try:
            write_json(args.write, {"checked_at": now.strftime(TIME_FORMAT), "exit_code": code, **result}, 0o644)
        except OSError as exc:
            print(f"freshness result not written: {type(exc).__name__}", file=sys.stderr)
            failed = True
    return WRITE_FAILED if failed else code


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
