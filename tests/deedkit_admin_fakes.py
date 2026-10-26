"""A synthetic host for the `deedkit admin` write tests (ANYM-493, ANYM-496).

`Operator` plays the messaging and calendar operator commands over a JSON state file: in process, or as this
script under the real runner, so child argv and environment can be read back. `Docker` plays `docker ps`,
`inspect` and the one-shot check containers; its images validate a candidate with the gateway's own policy
loader (the strict "old" image) or the calls registry's schema. `Site` lays out production-like private files and
the installed host tool's locks and state directories under a temporary directory. Every secret is synthetic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services/gateway/src"))
from agent_gateway.policy import CATALOG, Policy  # noqa: E402

LATER = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
REGISTRY_FIELDS = {"id", "key_sha256", "enabled", "expires_at", "scopes", "channels", "destinations", "archive_config"}


class Refusal(Exception):
    pass


LEGACY = ("cursor-write", "download", "read", "review-write", "send")  # messaging policy.LEGACY_PERMISSIONS


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def options(args: list[str]) -> dict:
    found, key = {}, None
    for part in args[1:]:
        if part.startswith("--"):
            key = part[2:]
            found.setdefault(key, [])
        else:
            found[key].append(part)
    return found


def expiry(state: dict, service: str, days: int) -> str:
    hours = state["offset_hours"].get(service, 0)
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=-hours)).replace(microsecond=0).isoformat()


def handle(state: dict, service: str, args: list[str], stdin: str):
    """One operator command over the state: the reply, or Refusal for a nonzero exit."""
    command, o = args[0], options(args)
    if command in state["missing"].get(service, []) or command in state["fail"].get(service, []):
        raise Refusal(command)
    answer = {"check": state["schema"].get(service, "ok")}
    if service == "messaging":
        principals, keys = state["messaging"]["principals"], state["messaging"]["keys"]

        def public(key_id, **extra):
            key = keys[key_id]
            return {"key_id": key_id, "principal": key["principal"], "expires_at": key["expires_at"],
                    "revoked": key["revoked"], **extra}
        if command == "key-info":
            if "check" in o:
                return answer
            if "operation-id" in o:
                rows = [dict(public(k), found=True) for k, v in keys.items() if v["operation_id"] == o["operation-id"][0]]
                return {"keys": rows or [{"found": False}]}
            by_digest = {v["digest"]: k for k, v in keys.items()}
            return {"keys": [dict(public(by_digest[sha(line.strip())]), found=True) if sha(line.strip()) in by_digest
                             else {"found": False} for line in stdin.splitlines()]}
        if command == "principal-info":
            if "check" in o:
                return answer
            name = o["principal"][0]
            if name not in principals:
                return {"found": False, "principal": name}
            return {"found": True, "principal": name, **principals[name],
                    "keys": [{"key_id": k, "permissions": v["permissions"], "expires_at": v["expires_at"],
                              "revoked": v["revoked"],
                              "effective_permissions": sorted(set(principals[name]["permissions"])
                                                              & set(LEGACY if v["permissions"] is None
                                                                    else v["permissions"]))}
                             for k, v in keys.items() if v["principal"] == name]}
        if command == "issue-key":
            name, operation, token = o["principal"][0], o["operation-id"][0], stdin.strip()
            done = next((k for k, v in keys.items() if v["operation_id"] == operation), None)
            if done is not None:
                if keys[done]["principal"] != name or keys[done]["digest"] != sha(token):
                    raise Refusal("operation_conflict")
                return answer if "check" in o else public(done, operation_id=operation, replayed=True)
            if name in principals:
                # The real store rewrites an existing principal's scope unless --new-principal refuses it.
                if "check" not in o and "new-principal" not in o:
                    state["violations"].append(f"issue-key on the existing principal {name}")
                raise Refusal("principal_exists")
            if "key-permissions-of" in o and "key-permissions" in o:
                raise Refusal("key_permissions_conflict")
            if "key-permissions-of" in o and o["key-permissions-of"][0] not in keys:
                raise Refusal("unknown_key")
            if "check" in o:
                return answer
            principals[name] = {"enabled": True, "accounts": o["accounts"],
                                "chat_ids": None if "all-chats" in o else o["chats"], "permissions": o["permissions"]}
            key_id = f"key-{len(keys) + 1}"
            # The stored ceiling of --key-permissions-of as is (a NULL one too), or the explicit list.
            ceiling = keys[o["key-permissions-of"][0]]["permissions"] if "key-permissions-of" in o \
                else o["key-permissions"]
            keys[key_id] = {"digest": sha(token), "principal": name, "permissions": ceiling,
                            "expires_at": expiry(state, service, int(o["days"][0])), "revoked": False,
                            "operation_id": operation}
            state["issued"].append(operation)
            return public(key_id, operation_id=operation, replayed=False)
        if command == "revoke-key":
            if "check" in o:
                return answer
            keys[o["key-id"][0]]["revoked"] = True
            return {"revoked": 1}
    if service == "calendar":
        agents = state["calendar"]["agents"]

        def row(name, **extra):
            agent = agents[name]
            return {"principal_id": name, "expires_at": agent["expires_at"], "revoked": agent["revoked"], **extra}
        if command == "agent-info":
            if "check" in o:
                return answer
            if "operation-id" in o:
                rows = [dict(row(k), found=True) for k, v in agents.items() if v["operation_id"] == o["operation-id"][0]]
                return {"agents": rows or [{"found": False}]}
            by_digest = {v["digest"]: k for k, v in agents.items()}
            return {"agents": [dict(row(by_digest[sha(line.strip())]), found=True) if sha(line.strip()) in by_digest
                               else {"found": False} for line in stdin.splitlines()]}
        if command == "agent-scope":
            if "check" in o:
                return answer
            name = o["id"][0]
            if name not in agents:
                return {"found": False, "principal_id": name}
            agent = agents[name]
            return {"found": True, **row(name), "group_id": agent["group_id"], "person_id": agent["person_id"],
                    "scopes": agent["scopes"], "calendars": agent["calendars"]}
        if command == "agent-key":
            name, operation, token = o["id"][0], o["operation-id"][0], stdin.strip()
            done = next((k for k, v in agents.items() if v["operation_id"] == operation), None)
            if done is not None:
                if done != name or agents[done]["digest"] != sha(token):
                    raise Refusal("operation_conflict")
                return answer if "check" in o else row(done, operation_id=operation, replayed=True)
            if name in agents:
                if "check" not in o:
                    state["violations"].append(f"agent-key on the existing agent {name}")
                raise Refusal("principal_exists")
            if "check" in o:
                return answer
            agents[name] = {"group_id": o["group"][0], "person_id": o["member"][0], "scopes": o["scopes"][0].split(","),
                            "calendars": [{"ref": ref, "visibility": o["visibility"][0]} for ref in o["calendar"]],
                            "digest": sha(token), "expires_at": expiry(state, service, int(o["days"][0])),
                            "revoked": False, "operation_id": operation}
            state["issued"].append(operation)
            return row(name, operation_id=operation, replayed=False)
        if command == "revoke-agent":
            if "check" in o:
                return answer
            agents[o["id"][0]]["revoked"] = True
            return {"revoked": True}
    raise Refusal(f"unknown command {command}")


class Operator:
    """The operator commands over `path`, called in process as the admin CLI's `run`."""

    def __init__(self, path: Path):
        self.path = path

    @property
    def state(self) -> dict:
        return json.loads(self.path.read_text())

    def change(self, edit) -> None:
        state = self.state
        edit(state)
        self.path.write_text(json.dumps(state))

    def __call__(self, service: str, args: list[str], data: bytes | None = None):
        from scripts import deedkit_admin as admin
        state = self.state
        state["log"].append({"service": service, "argv": list(args)})
        try:
            return handle(state, service, args, (data or b"").decode())
        except Refusal:
            raise admin.Unavailable(f"{service}: {args[0]} exited 1") from None
        finally:
            self.path.write_text(json.dumps(state))


def gateway_rule(strict: bool):
    """A gateway image's `deedkit-gateway --check`: the real policy loader (strict, like an older image that has no
    newer field) or any well-formed policy (a newer image)."""
    def check(path: Path) -> bool:
        if strict:
            try:
                Policy(path).load()
            except Exception:  # noqa: BLE001  the loader's own refusal
                return False
            return True
        return isinstance(json.loads(path.read_text()).get("principals"), list)
    return check


def registry_rule(path: Path) -> bool:
    """The calls image's `agent_api.py --check-registry`: the registry schema (services/calls/agent_api.py:262-276)."""
    doc = json.loads(path.read_text())
    rows = doc.get("principals")
    return (doc.get("version") == 1 and isinstance(doc.get("revision"), int) and isinstance(rows, list)
            and all(set(row) <= REGISTRY_FIELDS for row in rows)
            and len({row["id"] for row in rows}) == len({row["key_sha256"] for row in rows}) == len(rows))


class Docker:
    """`docker ps -q`, `inspect` and `run --rm` of a check container; any other verb fails and is recorded."""

    def __init__(self, containers: list[dict], images: dict):
        self.containers, self.images, self.calls = containers, images, []

    def __call__(self, args: list[str], name: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        out, code = b"", 0
        if args[0] == "ps":
            out = "\n".join(c["Id"] for c in self.containers).encode()
        elif args[0] == "inspect":
            out = json.dumps([c for c in self.containers if c["Id"] in args[1:]]).encode()
        elif args[0] == "run":
            mount = dict(part.split("=", 1) for part in args[args.index("--mount") + 1].split(",") if "=" in part)
            at = args.index("--entrypoint")
            image, argv = args[at + 2], [args[at + 1], *args[at + 3:]]
            path = next(part for part in argv if part.startswith(mount["target"] + "/"))
            host = Path(mount["source"]) / PurePosixPath(path).relative_to(mount["target"])
            verdict = self.images[image](host)  # "silent": an older image that exits 0 having checked nothing
            code = 0 if verdict else 1
            out = b"" if verdict == "silent" else b'{"valid": true}\n' if code == 0 else b'{"valid": false}\n'
        else:
            code = 1
        return subprocess.CompletedProcess(args, code, out, b"")


def private(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2))
    path.chmod(0o600)
    return path


def container(name: str, image: str, *mounts: tuple[Path, str]) -> dict:
    return {"Id": sha(name), "Name": "/" + name, "Image": image, "Config": {"User": ""},
            "Mounts": [{"Type": "bind", "Source": str(source), "Destination": target} for source, target in mounts]}


class Site:
    """Production's layout in miniature: the gateway policy directory and its key directory (gateway mounts), the
    voice secrets directory (calls registry, archive files), the admin token directory and journal, and the host
    tool's plan A, HTTP and global lock directories. The source principal `src` has all three backends and a calls
    archive; `other` is a bystander with its own keys."""

    def __init__(self, root: Path, monkeypatch, admin, s, policy_directory="gateway", keys_directory="keys"):
        self.root, self.admin, self.s, self.tool = root, admin, s, s.p
        self.gw, self.keys, self.voice = root / policy_directory, root / keys_directory, root / "voice"
        self.tokens = {}
        self.config = {"policy": str(self.gw / "policy.json"), "calls_registry": str(self.voice / "agents.json"),
                       "labels": str(root / "admin/labels.json"), "journal": str(root / "ops"),
                       "tokens": str(root / "admin-tokens"), "calls_lock": str(root / "locks/calls-registry.lock"),
                       "paths": {"/run/deedkit/keys": str(self.keys), "/run/secrets": str(self.voice)},
                       "messaging": ["operator"], "calendar": ["operator"]}
        state = {"messaging": {"principals": {}, "keys": {}}, "calendar": {"agents": {}}, "log": [], "issued": [],
                 "violations": [], "missing": {}, "fail": {}, "schema": {}, "offset_hours": {}}
        self.principals, self.registry = [], []
        for name in ("src", "other"):
            credentials = {"messaging": self.messaging(state, name, ["read", "send", "chat-manage"]),
                           "calendar": self.calendar(state, name), "calls": self.key(f"{name}-calls")}
            self.principals.append({"id": name, "key_sha256": self.secret(f"{name}-gateway"), "profile": "full",
                                    "enabled": True, "expires_at": LATER,
                                    "grants": {"messaging": ["read", "send", "download", "upload", "chat-manage",
                                                             "contacts-manage"],
                                               "calendar": ["agenda:read", "event:read", "plan:create"],
                                               "calls": ["calls:read", "calls:start"]},
                                    "actions": {"messenger_operations": ["archive_chat", "add_contact"]},
                                    "credentials": credentials})
            archive = f"{name}-calls-archive"
            self.messaging(state, archive, ["read"], file=False)
            private(self.voice / f"{name}-archive.token", self.tokens[archive] + "\n")
            private(self.voice / f"{name}-archive.json", {"base_url": "http://messaging.invalid",
                                                          "token_file": f"/run/secrets/{name}-archive.token"})
            self.registry.append({"id": name, "key_sha256": credentials["calls"]["key_sha256"], "enabled": True,
                                  "expires_at": LATER, "scopes": ["calls:read", "calls:start"], "channels": ["phone"],
                                  "archive_config": f"/run/secrets/{name}-archive.json"})
        self.write_policy()
        private(Path(self.config["calls_registry"]), {"version": 1, "revision": 5, "principals": self.registry})
        self.operator = Operator(private(root / "operator.json", state))
        self.gateway = container("deedkit-gateway-1", "gateway:new", (self.gw, "/etc/deedkit-gateway"),
                                 (self.keys, "/run/deedkit/keys"))
        self.voice_container = container("personal-os-voice-voice-1", "voice:1", (self.voice, "/run/secrets"))
        self.docker = Docker([self.gateway, self.voice_container, container("deedkit-api-1", "api:1")],
                             {"gateway:new": gateway_rule(strict=False), "gateway:old": gateway_rule(strict=True),
                              "voice:1": registry_rule})
        self.host_tool(monkeypatch)
        private(root / "admin/config.json", self.config)

    def secret(self, name: str) -> str:
        self.tokens[name] = f"synthetic-{name}-" + sha(name)[:40]
        return sha(self.tokens[name])

    def key(self, name: str) -> dict:
        digest = self.secret(name)
        private(self.keys / f"{name}.key", self.tokens[name] + "\n")
        return {"key_file": f"/run/deedkit/keys/{name}.key", "key_sha256": digest}

    def messaging(self, state: dict, name: str, key_permissions: list[str], file: bool = True) -> dict | None:
        credential = self.key(f"{name}-messaging") if file else None
        if not file:
            self.secret(name)
        token = self.tokens[f"{name}-messaging" if file else name]
        state["messaging"]["principals"][name] = {"enabled": True, "accounts": ["telegram:100", "whatsapp:200"],
                                                  "chat_ids": None, "permissions": ["read", "send", "download", "chat-manage"]}
        state["messaging"]["keys"][f"key-{name}"] = {"digest": sha(token), "principal": name,
                                                     "permissions": key_permissions, "expires_at": LATER,
                                                     "revoked": False, "operation_id": None}
        return credential

    def calendar(self, state: dict, name: str) -> dict:
        credential = self.key(f"{name}-calendar")
        state["calendar"]["agents"][name] = {"group_id": "family", "person_id": "owner",
                                             "scopes": ["agenda:read", "event:read"],
                                             "calendars": [{"ref": "cal-a", "visibility": "details"},
                                                           {"ref": "cal-b", "visibility": "details"}],
                                             "digest": credential["key_sha256"], "expires_at": LATER,
                                             "revoked": False, "operation_id": None}
        return credential

    def write_policy(self) -> None:
        private(Path(self.config["policy"]), {
            "version": 1, "revision": 7,
            "deployment": {"modules": ["messaging", "calendar", "calls"],
                           "backends": {m: {"url": f"http://{m}.invalid"} for m in ("messaging", "calendar", "calls")}},
            "profiles": {"full": {"tools": sorted(CATALOG),
                                  "actions": {"messenger_operations": ["archive_chat", "add_contact"]}}}, "principals": self.principals})

    def host_tool(self, monkeypatch) -> None:
        """The installed tool's locks and checks over this site's directories; no call is active."""
        state, http = self.root / "state/a", self.root / "state/http"
        for directory in (state, http, self.root / "state/global"):
            directory.mkdir(parents=True, mode=0o700)
        monkeypatch.setattr(self.s, "GLOBAL_LOCK", self.root / "state/global")
        monkeypatch.setattr(self.tool, "PAUSE", self.root / "autodeploy.pause")
        monkeypatch.setattr(self.tool, "main_history", lambda state: None)
        self.calls = []
        monkeypatch.setattr(self.tool, "active_calls", lambda settings: self.calls)
        self.voice_calls = {}  # container ID: the active calls deploy's per-container probe finds there
        monkeypatch.setattr(self.tool, "calls_in", lambda container, until: list(self.voice_calls.get(container, [])))
        monkeypatch.setattr(self.admin, "LOCK_WAIT", 0.6)
        settings = {"state_directory": str(state),
                    "projects": [{"name": "deedkit", "services": {"gateway": "gateway", "api": "runtime"}}]}
        self.http = {"state_directory": str(http), "http_release": {"gateway_public_url": "https://deedkit.test/deedkit"}}
        self.host = self.admin.Host(self.tool, self.s, settings, self.http)
        sha_ = "a" * 40
        (state / "current.json").write_text(json.dumps({"sha": sha_}))
        (state / sha_).mkdir()
        (state / sha_ / "deedkit.before.json").write_text(json.dumps({"services": {"gateway": {"image": "gateway:old"}}}))

    def main(self, *argv: str, run=None) -> int:
        return self.admin.main(["--config", str(self.root / "admin/config.json"), *argv], run=run or self.operator,
                               docker=self.docker, host=self.host)

    def snapshot(self) -> dict:
        """Every store's bytes: the private files and the operator state without its call log."""
        files = {str(p): p.read_bytes() for p in sorted(self.root.rglob("*"))
                 if p.is_file() and p.name not in ("operator.json", "deploy.lock", "calls-registry.lock")}
        state = self.operator.state
        files["operator"] = json.dumps({k: state[k] for k in ("messaging", "calendar")}, sort_keys=True).encode()
        return files

    def policy(self) -> dict:
        return json.loads(Path(self.config["policy"]).read_text())

    def registry_doc(self) -> dict:
        return json.loads(Path(self.config["calls_registry"]).read_text())

    def journal(self, operation: str) -> dict:
        return json.loads((self.root / "ops" / f"{operation}.json").read_text())


def script() -> None:
    """`python deedkit_admin_fakes.py STATE SERVICE COMMAND ...` for the real runner: logs argv and environment."""
    path, service, args = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
    with open(str(path) + ".calls", "a") as log:
        log.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\n")
    state = json.loads(path.read_text())
    state["log"].append({"service": service, "argv": args})
    try:
        print(json.dumps(handle(state, service, args, sys.stdin.read())))
    except Refusal:
        sys.exit(1)
    finally:
        path.write_text(json.dumps(state))


if __name__ == "__main__":
    script()
