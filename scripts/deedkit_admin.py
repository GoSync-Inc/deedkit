#!/usr/bin/env python3
"""DeedKit admin CLI (ANYM-491): every agent key across the gateway, messaging, calendar and calls stores.

`key list` joins the gateway policy, the calls registry and each backend's own operator command (ANYM-133:
messaging `list-keys --all` / `key-info`, calendar `list-agents` / `agent-info`) into one row per gateway
principal, and reports where the stores disagree. It is read-only: no lock, no write, safe during a call.
`key label` keeps a human label in a private admin file that no service reads, so the enforced policy schema
never changes (an older gateway image rejects unknown fields). Host only, standard library only.

Secrets: key files are read here and passed to the operator commands on stdin, never in argv, environment,
output or logs; the calls registry is matched by hashing in memory. Only public IDs are printed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
from uuid import uuid4

CONFIG = Path("/opt/deedkit/deploy/private/admin/config.json")
WINDOW = timedelta(days=7)  # the gateway's expiry warning window (ANYM-289, WARNING_WINDOW_DAYS)
TIMEOUT = 120  # seconds for one operator command
CLEANUP = 30  # seconds to stop this invocation's own container after a timeout or an interrupt
LIMIT = 1024 * 1024  # bytes of one private JSON or key file
BATCH = 100  # keys per lookup, the operator commands' own bound
LOOKUP = {"messaging": ["key-info", "--key-file", "/dev/stdin"], "calendar": ["agent-info", "--key-file", "/dev/stdin"]}
INVENTORY = {"messaging": ["list-keys", "--all"], "calendar": ["list-agents"]}


class AdminError(Exception):
    """A refusal or unreadable input; the message names a file or a service, never a secret."""


class Unavailable(Exception):
    pass


def read_private(path: str | Path, limit: int = LIMIT) -> bytes:
    """A regular, owner-only file that is not a symlink, read in full and unchanged while read (the gateway's
    own reader); an oversized or growing file is refused, never truncated."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_size > limit:
            raise AdminError(f"unsafe private file: {path}")
        value, after = os.read(fd, limit + 1), os.fstat(fd)
        if len(value) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AdminError(f"private file changed while read: {path}")
        return value
    finally:
        os.close(fd)


def load_json(path: str | Path, what: str) -> dict:
    try:
        value = json.loads(read_private(path))
    except (OSError, ValueError) as exc:
        raise AdminError(f"{what} unreadable: {path} ({type(exc).__name__})") from None
    if not isinstance(value, dict):
        raise AdminError(f"{what} is not a JSON object: {path}")
    return value


def instant(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def host_path(config: dict, path: str) -> str:
    """A container path (policy key_file, registry archive_config, archive token_file) on the host, by the
    longest configured mount prefix; a path under no prefix is taken as a host path."""
    pure, best = PurePosixPath(path), None
    for prefix in config.get("paths", {}):
        if pure == PurePosixPath(prefix) or PurePosixPath(prefix) in pure.parents:
            if best is None or len(prefix) > len(best):
                best = prefix
    if best is None:
        return path
    return str(Path(config["paths"][best]) / pure.relative_to(best))


def read_token(config: dict, path: str) -> str | None:
    """The token in a key file, or None when it is unreadable or not one whitespace-free line."""
    try:
        token = read_private(host_path(config, path), 1024).decode().strip()
    except (OSError, ValueError, AdminError):
        return None
    return token if token and not any(c.isspace() for c in token) else None


def runner(config: dict):
    """Run one operator command in its one-shot container; argv from the config, a secret only on stdin."""
    def run(service: str, args: list[str], data: bytes | None = None):
        prefix = config.get(service)
        if not isinstance(prefix, list) or not prefix or not all(isinstance(part, str) for part in prefix):
            raise Unavailable(f"{service}: no operator command configured")
        name = "deedkit-admin-" + uuid4().hex[:12]
        argv = [part.replace("{name}", name) for part in prefix] + args
        try:
            done = subprocess.run(argv, input=data if data is not None else b"", capture_output=True,
                                  timeout=TIMEOUT, check=False)
        except OSError as exc:
            raise Unavailable(f"{service}: {type(exc).__name__}") from None
        except BaseException as exc:  # timeout or interrupt: the killed client may leave its container running
            if any("{name}" in part for part in prefix):
                stop(name)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise Unavailable(f"{service}: TimeoutExpired") from None
            raise
        # stderr is dropped: a service's error text is not ours to echo.
        if done.returncode:
            raise Unavailable(f"{service}: {args[0]} exited {done.returncode}")
        try:
            return json.loads(done.stdout)
        except ValueError:
            raise Unavailable(f"{service}: {args[0]} printed no JSON") from None
    return run


def stop(name: str) -> None:
    """Stop only this invocation's uniquely named container, bounded; `run --rm` makes the daemon remove it."""
    try:
        subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True, timeout=CLEANUP, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def pinned(token: str, pin: object) -> bool:
    """The token matches the policy's key_sha256; a malformed pin (the gateway refuses it too) never matches."""
    if not isinstance(pin, str) or len(pin) != 64 or not all(c in "0123456789abcdef" for c in pin):
        return False
    return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), pin)


def status(record: dict | None, now: datetime) -> str:
    if record is None:
        return "missing"
    if record.get("revoked"):
        return "revoked"
    expiry = instant(record.get("expires_at"))
    return "expired" if expiry is None or expiry <= now else "live"


def lookup(run, service: str, tokens: list[str]) -> list[dict | None]:
    """Each token's public record from the service, in order; None where it is not found."""
    result: list[dict | None] = []
    for start in range(0, len(tokens), BATCH):
        chunk = tokens[start:start + BATCH]
        reply = run(service, LOOKUP[service], ("\n".join(chunk) + "\n").encode())
        rows = reply.get("keys" if service == "messaging" else "agents") if isinstance(reply, dict) else None
        if not isinstance(rows, list) or len(rows) != len(chunk) or not all(isinstance(r, dict) for r in rows):
            raise Unavailable(f"{service}: malformed lookup reply")
        result += [row if row.get("found") else None for row in rows]
    return result


def inventory(run, service: str) -> list[dict]:
    reply = run(service, INVENTORY[service])
    rows = reply.get("agents") if service == "calendar" and isinstance(reply, dict) else reply
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise Unavailable(f"{service}: malformed inventory reply")
    return rows


def public_id(service: str, record: dict) -> str | None:
    if service == "messaging":
        return record.get("key_id") or record.get("id")
    return record.get("principal_id") or record.get("id")


def collect(config: dict, run, now: datetime | None = None) -> dict:
    """The joined view: one row per gateway principal and the findings, public fields only."""
    now = now or datetime.now(timezone.utc)
    policy = load_json(config["policy"], "gateway policy")
    principals = policy.get("principals")
    if not isinstance(principals, list) or not all(isinstance(p, dict) and isinstance(p.get("id"), str)
                                                   for p in principals):
        raise AdminError(f"gateway policy has no principal list: {config['policy']}")
    registry = load_json(config["calls_registry"], "calls registry") if config.get("calls_registry") else {}
    calls = [entry for entry in registry.get("principals", []) if isinstance(entry, dict)]
    by_hash = {entry.get("key_sha256"): entry for entry in calls}
    labels = load_labels(config)
    pending: dict[str, list[tuple[dict, str]]] = {"messaging": [], "calendar": []}
    rows, unconfigured = [], False
    for principal in principals:
        credentials = []
        items = principal.get("credentials") if isinstance(principal.get("credentials"), dict) else {}
        for backend, credential in sorted(items.items()):
            entry = {"backend": backend, "public_id": None, "expires_at": None, "status": "missing"}
            token = read_token(config, credential.get("key_file", "")) if isinstance(credential, dict) else None
            if token is None:
                entry["note"] = "key_file_unreadable"
            elif not pinned(token, credential.get("key_sha256")):
                # The gateway refuses a key file that does not match its pin, so the binding does not work.
                entry.update(status="unknown", note="credential_pin_mismatch")
            elif backend in pending:
                pending[backend].append((entry, token))
            elif backend == "calls" and not config.get("calls_registry"):
                entry.update(status="unknown", note="calls_registry_not_configured")
                unconfigured = True
            elif backend == "calls":
                match = by_hash.get(hashlib.sha256(token.encode()).hexdigest())
                if match is not None:
                    entry.update(public_id=match.get("id"), expires_at=match.get("expires_at"),
                                 status=status({"revoked": not match.get("enabled"),
                                                "expires_at": match.get("expires_at")}, now))
                    if match.get("archive_config"):
                        entry["archive"] = archive(config, match["archive_config"], pending)
            else:
                entry.update(status="unknown", note="unsupported_backend")
            credentials.append(entry)
        rows.append({"id": principal["id"], "label": labels.get(principal["id"]),
                     "profile": principal.get("profile"), "enabled": principal.get("enabled") is True,
                     "expires_at": principal.get("expires_at"), "credentials": credentials})
    unavailable, records = [], {}
    if unconfigured:
        unavailable.append({"code": "backend_unavailable", "backend": "calls", "detail": "calls: no registry configured"})
    for service, items in pending.items():
        # The lookup and the inventory fail apart: a failed inventory keeps the rows' statuses and only loses
        # this backend's unbound_key findings.
        try:
            found = lookup(run, service, [token for _, token in items]) if items else []
        except Unavailable as exc:
            unavailable.append({"code": "backend_unavailable", "backend": service, "detail": str(exc)})
            for entry, _ in items:
                entry["status"] = "unknown"
            continue
        try:
            records[service] = inventory(run, service)
        except Unavailable as exc:
            unavailable.append({"code": "backend_unavailable", "backend": service, "detail": str(exc)})
        for (entry, _), record in zip(items, found, strict=True):
            entry["status"] = status(record, now)
            if record is not None:
                entry.update(public_id=public_id(service, record), backend_principal=record.get("principal"),
                             expires_at=record.get("expires_at"))
        items.clear()
    if config.get("calls_registry"):
        records["calls"] = [{"id": entry.get("id"), "expires_at": entry.get("expires_at"),
                             "revoked": not entry.get("enabled")} for entry in calls]
    return {"principals": rows, "findings": unavailable + findings(rows, records, now)}


def archive(config: dict, path: str, pending: dict) -> dict:
    """The calls entry's own messaging credential (archive_config -> token_file), looked up with the rest."""
    entry = {"backend": "messaging", "public_id": None, "expires_at": None, "status": "missing"}
    try:
        settings = json.loads(read_private(host_path(config, path), 65536))
        token = read_token(config, settings["token_file"])
    except (OSError, ValueError, KeyError, TypeError, AdminError):
        token = None
    if token is None:
        entry["note"] = "archive_unreadable"
    else:
        pending["messaging"].append((entry, token))
    return entry


def bound(row: dict):
    for credential in row["credentials"]:
        yield credential
        if "archive" in credential:
            yield credential["archive"]


def findings(rows: list[dict], records: dict, now: datetime) -> list[dict]:
    result, users = [], {}
    # Every principal counts, disabled or expired too: re-enabling or extending it would expose the finding.
    for row in rows:
        gateway = instant(row["expires_at"])
        for credential in bound(row):
            if credential["public_id"] is not None:
                users.setdefault((credential["backend"], credential["public_id"]), []).append(row["id"])
            if credential["status"] in ("missing", "revoked", "expired"):
                result.append({"code": "credential_" + credential["status"], "principal": row["id"],
                               "backend": credential["backend"], "public_id": credential["public_id"]})
            if credential.get("note") == "credential_pin_mismatch":
                result.append({"code": "credential_pin_mismatch", "principal": row["id"],
                               "backend": credential["backend"]})
            backend = instant(credential["expires_at"])
            if credential["status"] == "live" and backend is not None and gateway is not None and backend < gateway:
                result.append({"code": "backend_expires_first", "principal": row["id"],
                               "backend": credential["backend"], "public_id": credential["public_id"],
                               "expires_at": credential["expires_at"], "gateway_expires_at": row["expires_at"]})
    for (backend, key), names in sorted(users.items()):
        if len(set(names)) > 1:
            result.append({"code": "shared_credential", "backend": backend, "public_id": key,
                           "principals": sorted(set(names))})
    for backend, inventory_rows in sorted(records.items()):
        for record in inventory_rows:
            key = public_id(backend, record)
            if (backend, key) not in users:
                result.append({"code": "unbound_key", "backend": backend, "public_id": key,
                               "status": status(record, now),
                               "backend_principal": record.get("principal") or record.get("person_id")})
    return result


def select(view: dict, principal: str | None, scope: str, now: datetime) -> dict:
    """Rows by --principal and --valid/--expiring/--all. Findings: all without --principal; with it, those that
    name a shown row and every unavailable backend a shown row uses."""
    def expiries(row):
        return [instant(row["expires_at"])] + [instant(c["expires_at"]) for c in bound(row)]

    def keep(row):
        if principal is not None and row["id"] != principal:
            return False
        if scope == "valid":
            expiry = instant(row["expires_at"])
            return row["enabled"] and expiry is not None and expiry > now
        if scope == "expiring":  # in the window or already expired, as the gateway's own --check lists them
            gateway, *backends = expiries(row)
            return gateway is None or any(m is not None and m - now <= WINDOW for m in [gateway, *backends])
        return True

    rows = [row for row in view["principals"] if keep(row)]
    shown = {row["id"] for row in rows}
    used = {credential["backend"] for row in rows for credential in bound(row)}
    found = [item for item in view["findings"]
             if principal is None or item.get("principal") in shown or shown & set(item.get("principals", []))
             or (item["code"] == "backend_unavailable" and item["backend"] in used)]
    return {"principals": rows, "findings": found}


def load_labels(config: dict) -> dict:
    path = Path(config.get("labels", CONFIG.with_name("labels.json")))
    if not path.exists() and not path.is_symlink():
        return {}
    labels = load_json(path, "labels").get("labels", {})
    return {k: v for k, v in labels.items() if isinstance(k, str) and isinstance(v, str)}


def set_label(config: dict, principal: str, text: str) -> dict:
    if not 1 <= len(text) <= 80 or not text.isprintable():
        raise AdminError("a label is 1-80 printable characters")
    policy = load_json(config["policy"], "gateway policy")
    if principal not in {p.get("id") for p in policy.get("principals", []) if isinstance(p, dict)}:
        raise AdminError(f"no gateway principal {principal!r}")
    path = Path(config.get("labels", CONFIG.with_name("labels.json")))
    labels = load_labels(config)
    labels[principal] = text
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".labels-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as out:  # mkstemp creates it 0600
            json.dump({"version": 1, "labels": dict(sorted(labels.items()))}, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {"principal": principal, "label": text}


def render(view: dict) -> str:
    lines = []
    for row in view["principals"]:
        state = "enabled" if row["enabled"] else "disabled"
        label = json.dumps(row["label"], ensure_ascii=False) if row["label"] else "-"
        lines.append(f"{row['id']}  {label}  profile={row['profile']}  {state}  expires {row['expires_at']}")
        for credential in row["credentials"]:
            lines.append(line("  ", credential))
            if "archive" in credential:
                lines.append(line("    archive ", credential["archive"]))
    lines.append(f"{len(view['principals'])} principals, {len(view['findings'])} findings")
    for item in view["findings"]:
        rest = "  ".join(f"{k}={v}" for k, v in item.items() if k != "code")
        lines.append(f"  {item['code']}  {rest}")
    return "\n".join(lines)


def line(indent: str, credential: dict) -> str:
    note = f"  ({credential['note']})" if credential.get("note") else ""
    return (f"{indent}{credential['backend']:<9} {credential['public_id'] or '-'}  {credential['status']}"
            f"  expires {credential['expires_at'] or '-'}{note}")


def main(argv: list[str] | None = None, run=None) -> int:
    parser = argparse.ArgumentParser(prog="deedkit admin", description="DeedKit admin CLI (ANYM-491)")
    parser.add_argument("--config", type=Path, default=CONFIG, help=f"private admin configuration (default {CONFIG})")
    nouns = parser.add_subparsers(dest="noun", required=True)
    verbs = nouns.add_parser("key", help="agent keys").add_subparsers(dest="verb", required=True)
    listing = verbs.add_parser("list", help="every agent key, read-only")
    listing.add_argument("--principal")
    scope = listing.add_mutually_exclusive_group()
    scope.add_argument("--valid", action="store_const", const="valid", dest="scope")
    scope.add_argument("--expiring", action="store_const", const="expiring", dest="scope")
    scope.add_argument("--all", action="store_const", const="all", dest="scope")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--strict", action="store_true", help="exit 1 when there are findings")
    label = verbs.add_parser("label", help="set a principal's label in the private admin file")
    label.add_argument("principal")
    label.add_argument("text")
    args = parser.parse_args(argv)
    try:
        config = load_json(args.config, "admin configuration")
        if args.verb == "label":
            print(json.dumps(set_label(config, args.principal, args.text), ensure_ascii=False))
            return 0
        now = datetime.now(timezone.utc)
        view = select(collect(config, run or runner(config), now), args.principal, args.scope or "all", now)
    except (AdminError, KeyError) as exc:
        print(f"deedkit admin: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(view, ensure_ascii=False, indent=2) if args.json else render(view))
    return 1 if args.strict and view["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
