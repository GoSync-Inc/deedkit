"""`deedkit admin key list` and `key label` (ANYM-494) over synthetic stores and fake operator commands."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from scripts import deedkit_admin as admin

NOW = "2026-10-24T12:00:00+00:00"
LATER = "2027-04-01T00:00:00+00:00"
SOON = "2026-10-27T00:00:00+00:00"
PAST = "2026-10-01T00:00:00+00:00"
ABSENT = object()  # a field the synthetic recordings entry leaves out
CANARY = "canary-private-value"  # every recordings field that must never reach any output carries it
FAKE = r'''
import hashlib, json, sys
state = json.load(open(sys.argv[1]))
with open(sys.argv[1] + ".calls", "a") as log:
    log.write(json.dumps({"argv": sys.argv[2:], "env": dict(__import__("os").environ)}) + "\n")
service, command = sys.argv[2], sys.argv[3]
if command in state.get("fail", []):
    sys.exit(3)
if command in ("key-info", "agent-info"):
    rows = []
    for line in sys.stdin.read().splitlines():
        record = state[service]["by_digest"].get(hashlib.sha256(line.strip().encode()).hexdigest())
        rows.append(dict(record, found=True) if record else {"found": False})
    print(json.dumps({"keys" if service == "messaging" else "agents": rows}))
elif command == "list-keys":
    print(json.dumps(state["messaging"]["inventory"]))
elif command == "list-agents":
    print(json.dumps({"agents": state["calendar"]["inventory"]}))
else:
    sys.exit(2)
'''


def token(name):
    return f"synthetic-{name}-" + hashlib.sha256(name.encode()).hexdigest()[:40]


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)
    return path


class Stores:
    """A host layout like production's: policy, gateway key files, calls registry, archive files, admin labels."""

    def __init__(self, root):
        self.root, self.tokens = root, {}
        self.messaging, self.calendar, self.registry, self.principals = {}, {}, [], []
        self.recordings = []
        self.config = {"policy": str(root / "gateway-policy/policy.json"),
                       "calls_registry": str(root / "calls-agents.json"),
                       "labels": str(root / "admin/labels.json"),
                       "paths": {"/run/deedkit/keys": str(root / "gateway-keys"),
                                 "/run/deedkit/voice": str(root / "voice")}}

    def key(self, name):
        self.tokens[name] = token(name)
        private(self.root / "gateway-keys" / (name + ".key"), self.tokens[name] + "\n")
        return {"key_file": f"/run/deedkit/keys/{name}.key", "key_sha256": sha(self.tokens[name])}

    def messaging_key(self, name, key_id, principal, expires=LATER, revoked=False, bound=True):
        self.tokens[name] = token(name)
        self.messaging[key_id] = {"digest": sha(self.tokens[name]), "key_id": key_id, "principal": principal,
                                  "expires_at": expires, "revoked": revoked}
        return self.key(name) if bound else None

    def calendar_agent(self, name, expires=LATER, revoked=False, bound=True):
        self.tokens[name] = token(name)
        self.calendar[name] = {"digest": sha(self.tokens[name]), "principal_id": name, "expires_at": expires,
                               "revoked": revoked}
        return self.key(name) if bound else None

    def calls_agent(self, name, expires=LATER, enabled=True, archive=None):
        credential = self.key(name)
        entry = {"id": name, "key_sha256": credential["key_sha256"], "enabled": enabled, "expires_at": expires,
                 "scopes": ["calls:read"]}
        if archive:
            private(self.root / "voice" / (archive + ".token"), self.tokens[archive])
            private(self.root / "voice" / (archive + ".json"),
                    {"base_url": "http://messaging.invalid", "token_file": f"/run/deedkit/voice/{archive}.token"})
            entry["archive_config"] = f"/run/deedkit/voice/{archive}.json"
        self.registry.append(entry)
        return credential

    def recordings_agent(self, name, **fields):
        """A gateway recordings credential and its entry in the recordings private configuration."""
        credential = self.key(name)
        entry = {"id": name, "key_sha256": credential["key_sha256"], "expires_at": LATER,
                 "accounts": [CANARY + "-account"], "permissions": [CANARY + "-permission"], **fields}
        self.recordings.append({k: v for k, v in entry.items() if v is not ABSENT})
        self.config["recordings_config"] = str(self.recordings_path)
        return credential

    @property
    def recordings_path(self):
        return self.root / "recordings/config.json"

    def principal(self, name, credentials, expires=LATER, enabled=True, profile="full"):
        self.principals.append({"id": name, "key_sha256": sha("gateway-" + name), "profile": profile,
                                "enabled": enabled, "expires_at": expires, "grants": {},
                                "credentials": credentials})

    def write(self):
        private(Path(self.config["policy"]), {"version": 1, "revision": 3, "deployment": {"modules": [], "backends": {}},
                                              "profiles": {"full": {"tools": []}}, "principals": self.principals})
        private(Path(self.config["calls_registry"]), {"version": 1, "revision": 2, "principals": self.registry})
        if self.recordings:
            private(self.recordings_path, {"version": 1, "cursor_secret_file": CANARY + "-cursor",
                                           "accounts": [{"id": CANARY + "-account", "label": CANARY + "-label"}],
                                           "principals": self.recordings})
        public = lambda rows: [{k: v for k, v in row.items() if k != "digest"} for row in rows]  # noqa: E731
        return {"messaging": {"by_digest": {r["digest"]: public([r])[0] for r in self.messaging.values()},
                              "inventory": [dict(public([r])[0], id=r["key_id"]) for r in self.messaging.values()]},
                "calendar": {"by_digest": {r["digest"]: public([r])[0] for r in self.calendar.values()},
                             "inventory": public(self.calendar.values())}}

    def fake(self, broken=(), fail=()):
        """Real subprocess runner over a fake operator script: proves what reaches argv, stdin and environment.
        `broken` services exit 3 on every command, `fail` commands exit 3 alone."""
        config = self.root / "admin/config.json"
        if config.exists() and not broken and not fail:  # written once, so a read-only check sees no rewrite
            return config
        state = self.root / "operator-state.json"
        state.write_text(json.dumps(dict(self.write(), fail=list(fail))))
        script = self.root / "fake_operator.py"
        script.write_text(FAKE)
        for service in ("messaging", "calendar"):
            self.config[service] = [sys.executable, str(script), str(state), service]
            if service in broken:
                self.config[service] = [sys.executable, "-c", "import sys; sys.exit(3)"]
        private(self.root / "admin/config.json", self.config)
        return self.root / "admin/config.json"

    def snapshot(self):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(self.root.rglob("*"))
                if p.is_file() and not p.name.endswith(".calls")}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "datetime", Frozen)
    return Stores(tmp_path)


class Frozen(admin.datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return admin.datetime.fromisoformat(NOW)


def healthy(stores):
    stores.messaging_key("cc-messaging", "00000000-0000-4000-8000-000000000001", "claude-code")
    stores.messaging_key("cc-archive", "00000000-0000-4000-8000-000000000002", "claude-code-calls-archive",
                         bound=False)
    stores.principal("claude-code", {"messaging": stores.key("cc-messaging"),
                                     "calendar": stores.calendar_agent("cc-calendar"),
                                     "calls": stores.calls_agent("cc-calls", archive="cc-archive")})


def listing(stores, capsys, *arguments, broken=(), fail=()):
    code = admin.main(["--config", str(stores.fake(broken, fail)), "key", "list", "--json", *arguments])
    out = capsys.readouterr()
    return code, json.loads(out.out), out


# spec: ANYM-494
def test_one_row_per_gateway_principal_with_each_backend_credential_and_the_nested_archive(stores, capsys):
    healthy(stores)
    stores.principal("reader", {"messaging": stores.messaging_key("r-messaging", "00000000-0000-4000-8000-000000000003",
                                                                  "reader", expires=SOON)},
                     expires=SOON, profile="read")
    code, view, _ = listing(stores, capsys)
    assert code == 0 and [row["id"] for row in view["principals"]] == ["claude-code", "reader"]
    row = view["principals"][0]
    assert {k: row[k] for k in ("label", "profile", "enabled", "expires_at")} == {
        "label": None, "profile": "full", "enabled": True, "expires_at": LATER}
    credentials = {c["backend"]: c for c in row["credentials"]}
    assert credentials["messaging"] == {"backend": "messaging", "public_id": "00000000-0000-4000-8000-000000000001",
                                             "backend_principal": "claude-code", "expires_at": LATER, "status": "live"}
    assert (credentials["calendar"]["public_id"], credentials["calendar"]["status"]) == ("cc-calendar", "live")
    assert (credentials["calls"]["public_id"], credentials["calls"]["status"]) == ("cc-calls", "live")
    assert credentials["calls"]["archive"] == {
        "backend": "messaging", "public_id": "00000000-0000-4000-8000-000000000002",
        "backend_principal": "claude-code-calls-archive", "expires_at": LATER, "status": "live"}
    assert view["findings"] == []


# spec: ANYM-494
def test_valid_expiring_and_principal_filters_use_the_gateway_window(stores, capsys):
    stores.principal("live", {"calendar": stores.calendar_agent("live-cal")})
    stores.principal("soon", {"calendar": stores.calendar_agent("soon-cal")}, expires=SOON)
    stores.principal("backend-soon", {"calendar": stores.calendar_agent("bs-cal", expires=SOON)})
    stores.principal("gone", {"calendar": stores.calendar_agent("gone-cal", revoked=True)}, expires=PAST)
    stores.principal("off", {"calendar": stores.calendar_agent("off-cal", revoked=True)}, enabled=False)
    ids = lambda *a: [row["id"] for row in listing(stores, capsys, *a)[1]["principals"]]  # noqa: E731
    assert ids() == ids("--all") == ["live", "soon", "backend-soon", "gone", "off"]
    assert ids("--valid") == ["live", "soon", "backend-soon"]
    assert ids("--expiring") == ["soon", "backend-soon", "gone"]
    assert ids("--principal", "soon") == ["soon"]


# spec: ANYM-494
@pytest.mark.parametrize("case,expected", [
    ("missing", {"code": "credential_missing", "principal": "agent", "backend": "messaging", "public_id": None}),
    ("revoked", {"code": "credential_revoked", "principal": "agent", "backend": "calendar", "public_id": "agent-cal"}),
    ("expired", {"code": "credential_expired", "principal": "agent", "backend": "calls", "public_id": "agent-calls"}),
    ("unbound-messaging", {"code": "unbound_key", "backend": "messaging",
                           "public_id": "00000000-0000-4000-8000-0000000000aa", "status": "live",
                           "backend_principal": "forgotten"}),
    ("unbound-calendar", {"code": "unbound_key", "backend": "calendar", "public_id": "stray-cal", "status": "live",
                          "backend_principal": None}),
    ("unbound-calls", {"code": "unbound_key", "backend": "calls", "public_id": "stray-calls", "status": "live",
                       "backend_principal": None}),
    ("earlier", {"code": "backend_expires_first", "principal": "agent", "backend": "calendar",
                 "public_id": "agent-cal", "expires_at": SOON, "gateway_expires_at": LATER}),
    ("shared", {"code": "shared_credential", "backend": "calendar", "public_id": "agent-cal",
                "principals": ["agent", "twin"]}),
    ("shared-archive", {"code": "shared_credential", "backend": "messaging",
                        "public_id": "00000000-0000-4000-8000-0000000000bb", "principals": ["agent", "twin"]}),
])
def test_each_disagreement_between_stores_is_one_finding_and_strict_exits_1(stores, capsys, case, expected):
    credentials = {}
    if case == "missing":
        credentials["messaging"] = stores.key("never-issued")
    elif case in ("revoked", "earlier", "shared"):
        credentials["calendar"] = stores.calendar_agent("agent-cal", revoked=case == "revoked",
                                                        expires=SOON if case == "earlier" else LATER)
    elif case == "expired":
        credentials["calls"] = stores.calls_agent("agent-calls", expires=PAST)
    elif case == "unbound-messaging":  # bound to no gateway principal and no token file on the host
        stores.messaging_key("lost", "00000000-0000-4000-8000-0000000000aa", "forgotten", bound=False)
    elif case == "unbound-calendar":
        stores.calendar_agent("stray-cal", bound=False)
    elif case == "unbound-calls":
        stores.calls_agent("stray-calls")
    elif case == "shared-archive":
        stores.messaging_key("arch", "00000000-0000-4000-8000-0000000000bb", "archive", bound=False)
        credentials["calls"] = stores.calls_agent("agent-calls", archive="arch")
    stores.principal("agent", credentials)
    if case == "shared":
        stores.principal("twin", {"calendar": credentials["calendar"]})
    if case == "shared-archive":
        stores.principal("twin", {"messaging": stores.key("arch")})
    code, view, _ = listing(stores, capsys)
    assert view["findings"] == [expected]
    assert code == 0
    assert admin.main(["--config", str(stores.root / "admin/config.json"), "key", "list", "--strict"]) == 1


# spec: ANYM-494
def test_strict_exits_0_when_the_stores_agree_and_a_disabled_principal_is_no_finding(stores, capsys):
    healthy(stores)
    stores.principal("retired", {"calendar": stores.calendar_agent("old-cal")}, enabled=False)
    code, view, _ = listing(stores, capsys, "--strict")
    assert code == 0 and view["findings"] == []


# spec: ANYM-494
@pytest.mark.parametrize("enabled,expires", [(False, LATER), (True, PAST), (False, PAST)],
                         ids=["disabled", "expired", "disabled-expired"])
def test_a_disabled_or_expired_principal_still_reports_its_credential_findings(stores, capsys, enabled, expires):
    healthy(stores)
    stores.principal("retired", {"calendar": stores.calendar_agent("old-cal", revoked=True),
                                 "messaging": stores.key("never-issued")}, enabled=enabled, expires=expires)
    code, view, _ = listing(stores, capsys, "--strict")
    assert code == 1
    assert view["findings"] == [
        {"code": "credential_revoked", "principal": "retired", "backend": "calendar", "public_id": "old-cal"},
        {"code": "credential_missing", "principal": "retired", "backend": "messaging", "public_id": None}]


# spec: ANYM-494
@pytest.mark.parametrize("revoked,expires", [(True, LATER), (False, PAST)], ids=["revoked", "expired"])
def test_a_revoked_or_expired_unbound_key_is_still_a_finding_with_its_status(stores, capsys, revoked, expires):
    healthy(stores)
    stores.messaging_key("old", "00000000-0000-4000-8000-0000000000cc", "gone", revoked=revoked, expires=expires,
                         bound=False)
    stores.calendar_agent("old-cal", revoked=revoked, expires=expires, bound=False)
    stores.calls_agent("old-calls", enabled=not revoked, expires=expires)
    state = "revoked" if revoked else "expired"
    _, view, _ = listing(stores, capsys)
    assert view["findings"] == [
        {"code": "unbound_key", "backend": "calendar", "public_id": "old-cal", "status": state,
         "backend_principal": None},
        {"code": "unbound_key", "backend": "calls", "public_id": "old-calls", "status": state,
         "backend_principal": None},
        {"code": "unbound_key", "backend": "messaging", "public_id": "00000000-0000-4000-8000-0000000000cc",
         "status": state, "backend_principal": "gone"}]


# spec: ANYM-494
def test_principal_filter_keeps_the_unavailable_backend_it_uses_so_strict_fails(stores, capsys):
    healthy(stores)
    stores.principal("other", {"messaging": stores.messaging_key("o", "00000000-0000-4000-8000-0000000000dd", "o")})
    code, view, _ = listing(stores, capsys, "--principal", "claude-code", "--strict", broken=("calendar",))
    assert code == 1
    assert view["findings"] == [{"code": "backend_unavailable", "backend": "calendar",
                                 "detail": "calendar: agent-info exited 3"}]
    code, view, _ = listing(stores, capsys, "--principal", "other", "--strict", broken=("calendar",))
    assert code == 0 and view["findings"] == []


# spec: ANYM-494
def test_no_calls_registry_configured_is_a_finding_when_a_principal_holds_a_calls_key(stores, capsys):
    healthy(stores)
    stores.fake()
    config = json.loads((stores.root / "admin/config.json").read_text())
    del config["calls_registry"]
    private(stores.root / "admin/config.json", config)
    code, view, _ = listing(stores, capsys, "--principal", "claude-code", "--strict")
    calls = {c["backend"]: c for c in view["principals"][0]["credentials"]}["calls"]
    assert code == 1 and calls["status"] == "unknown" and calls["note"] == "calls_registry_not_configured"
    assert view["findings"] == [{"code": "backend_unavailable", "backend": "calls",
                                 "detail": "calls: no registry configured"}]


# spec: ANYM-772
@pytest.mark.parametrize("fields, expected", [
    ({}, ("agent-rec", LATER, "live")),
    ({"expires_at": PAST}, ("agent-rec", PAST, "expired")),
    ({"key_sha256": sha("another key")}, (None, None, "missing")),
    ({"expires_at": ABSENT}, ("agent-rec", None, "expired")),
    ({"expires_at": {"raw": CANARY}}, ("agent-rec", None, "expired")),
    ({"expires_at": 1893456000}, ("agent-rec", None, "expired")),
    ({"expires_at": CANARY}, ("agent-rec", None, "expired")),
    ({"expires_at": "2027-04-01T00:00:00Z"}, ("agent-rec", LATER, "live")),
    ({"expires_at": "2027-04-01T00:00:00." + "1" * 5000 + "+00:00"}, ("agent-rec", "2027-04-01T00:00:00.111111+00:00", "live")),
    ({"expires_at": "2027-04-01\x1b00:00:00+00:00"}, ("agent-rec", LATER, "live")),
    ({"expires_at": "2027-04-01\u00a000:00:00+00:00"}, ("agent-rec", LATER, "live")),
    ({"id": "a" * 65}, ("a" * 65, LATER, "live")),
    ({"id": "a" * 128}, ("a" * 128, LATER, "live")),
    ({"id": "a" * 129}, (None, LATER, "live")),
    ({"id": ""}, (None, LATER, "live")),
    ({"id": {"raw": CANARY}}, (None, LATER, "live")),
    ({"id": "bad\x1bid"}, ("bad\x1bid", LATER, "live")),
    ({"id": "\ud800"}, ("\ud800", LATER, "live")),
    ({"id": "a\u00a0b"}, ("a\u00a0b", LATER, "live")),
    ({"id": "агент"}, ("агент", LATER, "live")),
], ids=["live", "expired", "no-match", "missing-expiry", "object-expiry", "number-expiry", "text-expiry",
        "z-expiry", "oversized-expiry", "control-expiry", "nbsp-expiry", "id-65", "id-128", "id-129", "id-empty",
        "id-object", "id-control", "id-surrogate", "id-nbsp", "id-cyrillic"])
def test_a_recordings_credential_reports_only_validated_public_fields(stores, capsys, fields, expected):
    stores.principal("agent", {"recordings": stores.recordings_agent("agent-rec", **fields)})
    _, view, out = listing(stores, capsys)
    row = view["principals"][0]["credentials"][0]
    assert (row["public_id"], row["expires_at"], row["status"]) == expected
    assert "note" not in row and CANARY not in out.out + out.err
    assert all(part.isprintable() for part in out.out.split("\n"))


# spec: ANYM-772
def test_two_recordings_ids_that_print_alike_stay_two_credentials(stores, capsys):
    stores.principal("one", {"recordings": stores.recordings_agent("one-rec", id="a\u00a0b")})
    stores.principal("two", {"recordings": stores.recordings_agent("two-rec", id="a\\xa0b")})
    code, view, _ = listing(stores, capsys, "--strict")
    assert code == 0 and view["findings"] == []
    assert [row["public_id"] for row in recordings_rows(view)] == ["a\u00a0b", "a\\xa0b"]


# spec: ANYM-772
def test_a_recordings_id_with_a_newline_or_control_character_cannot_forge_a_text_line(stores, capsys):
    stores.principal("agent", {"recordings": stores.recordings_agent("agent-rec", id="x\nforged  live\x1b[2J",
                                                                     expires_at=PAST)})
    stores.fake()
    code = admin.main(["--config", str(stores.root / "admin/config.json"), "key", "list"])
    out = capsys.readouterr().out
    assert code == 0 and "\x1b" not in out and not any(row.startswith("forged") for row in out.split("\n"))
    assert "x\\nforged  live\\u001b[2J" in out
    assert out.count("x\\nforged") == 2  # the row and its credential_expired finding


# spec: ANYM-772
def test_a_recordings_key_ending_before_its_gateway_principal_is_backend_expires_first(stores, capsys):
    stores.principal("agent", {"recordings": stores.recordings_agent("agent-rec", expires_at=SOON)})
    code, view, _ = listing(stores, capsys, "--expiring", "--strict")
    assert code == 1 and [row["id"] for row in view["principals"]] == ["agent"]
    assert view["findings"] == [{"code": "backend_expires_first", "principal": "agent", "backend": "recordings",
                                 "public_id": "agent-rec", "expires_at": SOON, "gateway_expires_at": LATER}]


# spec: ANYM-772
@pytest.mark.parametrize("fields, finding", [
    ({"expires_at": PAST}, {"code": "credential_expired", "public_id": "agent-rec"}),
    ({"key_sha256": sha("another key")}, {"code": "credential_missing", "public_id": None}),
], ids=["expired", "missing"])
def test_an_expired_or_unmatched_recordings_credential_is_a_finding(stores, capsys, fields, finding):
    stores.principal("agent", {"recordings": stores.recordings_agent("agent-rec", **fields)})
    code, view, _ = listing(stores, capsys, "--strict")
    assert code == 1
    assert view["findings"] == [{"principal": "agent", "backend": "recordings", **finding}]


def recordings_rows(view):
    return [c for row in view["principals"] for c in row["credentials"] if c["backend"] == "recordings"]


def two_recordings_holders(stores):
    healthy(stores)
    stores.principal("agent", {"recordings": stores.recordings_agent("agent-rec")})
    stores.principal("second", {"recordings": stores.recordings_agent("second-rec")})
    stores.fake()


def assert_only_recordings_is_unavailable(stores, capsys, note):
    code, view, out = listing(stores, capsys, "--strict")
    assert code == 1
    assert [(r["status"], r["note"], r["public_id"], r["expires_at"]) for r in recordings_rows(view)] == [
        ("unknown", note, None, None)] * 2
    others = {c["backend"]: c["status"] for c in view["principals"][0]["credentials"]}
    assert others == {"messaging": "live", "calendar": "live", "calls": "live"}
    assert view["findings"] == [{"code": "backend_unavailable", "backend": "recordings", "note": note}]
    assert CANARY not in out.out + out.err and "Traceback" not in out.err


# spec: ANYM-772
@pytest.mark.parametrize("content, note", [
    (b"{not json " + CANARY.encode(), "recordings_config_invalid"),
    (b"\xff\xfe" + CANARY.encode(), "recordings_config_invalid"),
    (b"[" * 200000, "recordings_config_invalid"),
    (json.dumps([CANARY]).encode(), "recordings_config_invalid"),
    (b"{}", "recordings_config_invalid"),
    (b'{"principals": null}', "recordings_config_invalid"),
    (json.dumps({"principals": CANARY}).encode(), "recordings_config_invalid"),
    (json.dumps({"principals": {"a": CANARY}}).encode(), "recordings_config_invalid"),
    (json.dumps({"principals": [CANARY]}).encode(), "recordings_config_invalid"),
    (json.dumps({"principals": [{"id": CANARY}]}).encode(), "recordings_config_invalid"),
    (json.dumps({"principals": [{"id": "a", "key_sha256": [CANARY]}]}).encode(), "recordings_config_invalid"),
    (json.dumps({"principals": [{"id": "a", "key_sha256": CANARY}]}).encode(), "recordings_config_invalid"),
    (json.dumps({"principals": [{"id": CANARY, "key_sha256": "a" * 64}, {"id": "b", "key_sha256": "a" * 64}]}).encode(),
     "recordings_config_duplicate_key"),
], ids=["bad-json", "bad-utf8", "deep-nesting", "top-level-list", "principals-absent", "principals-null",
        "principals-text", "principals-object", "entry-text", "entry-no-hash", "entry-object-hash", "entry-bad-hash",
        "duplicate-hash"])
def test_a_malformed_recordings_configuration_is_one_fixed_note_and_the_rest_still_lists(stores, capsys, content, note):
    two_recordings_holders(stores)
    stores.recordings_path.write_bytes(content)
    assert_only_recordings_is_unavailable(stores, capsys, note)


# spec: ANYM-772
@pytest.mark.parametrize("case", ["missing", "mode-0644", "mode-0640", "symlink", "directory", "oversized"])
def test_a_missing_or_unsafe_recordings_file_is_unreadable_never_its_text(stores, capsys, case):
    two_recordings_holders(stores)
    path = stores.recordings_path
    if case == "missing":
        path.unlink()
    elif case.startswith("mode"):
        path.chmod(int(case[-4:], 8))
    elif case == "symlink":
        target = path.with_name("target.json")
        path.rename(target)
        path.symlink_to(target)
    elif case == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.write_text(" " * (admin.LIMIT + 1))
    assert_only_recordings_is_unavailable(stores, capsys, "recordings_config_unreadable")


# spec: ANYM-772
@pytest.mark.parametrize("value", ["relative/config.json", 0.5, ["/a"], "/bad\0path"],
                         ids=["relative", "number", "list", "nul"])
def test_a_recordings_config_value_that_is_no_absolute_path_is_unreadable(stores, capsys, value):
    two_recordings_holders(stores)
    config = json.loads((stores.root / "admin/config.json").read_text())
    config["recordings_config"] = value
    private(stores.root / "admin/config.json", config)
    assert_only_recordings_is_unavailable(stores, capsys, "recordings_config_unreadable")


# spec: ANYM-772
def test_no_recordings_config_is_unknown_and_a_finding_when_a_principal_holds_a_recordings_key(stores, capsys):
    two_recordings_holders(stores)
    config = json.loads((stores.root / "admin/config.json").read_text())
    del config["recordings_config"]
    private(stores.root / "admin/config.json", config)
    assert_only_recordings_is_unavailable(stores, capsys, "recordings_config_not_configured")


# spec: ANYM-772
def test_principal_filter_keeps_the_recordings_finding_only_for_a_holder(stores, capsys):
    two_recordings_holders(stores)
    stores.recordings_path.write_text("{}")
    expected = [{"code": "backend_unavailable", "backend": "recordings", "note": "recordings_config_invalid"}]
    code, view, _ = listing(stores, capsys, "--principal", "agent", "--strict")
    assert code == 1 and view["findings"] == expected and [r["id"] for r in view["principals"]] == ["agent"]
    code, view, _ = listing(stores, capsys, "--principal", "claude-code", "--strict")
    assert code == 0 and view["findings"] == []


# spec: ANYM-772
def test_a_broken_recordings_file_is_no_finding_without_a_holder(stores, capsys):
    healthy(stores)
    stores.fake()
    config = json.loads((stores.root / "admin/config.json").read_text())
    config["recordings_config"] = str(stores.recordings_path)
    private(stores.root / "admin/config.json", config)
    private(stores.recordings_path, "{}")
    code, view, _ = listing(stores, capsys, "--strict")
    assert code == 0 and view["findings"] == []


# spec: ANYM-772
@pytest.mark.parametrize("credential", [{"key_file": "/run/deedkit/keys/absent.key", "key_sha256": "a" * 64}, "pin", None],
                         ids=["unreadable-key-file", "pin-mismatch", "not-an-object"])
def test_a_broken_recordings_file_decides_a_row_whose_own_key_file_also_fails(stores, capsys, credential):
    if credential == "pin":
        credential = dict(stores.key("holder-rec"), key_sha256="a" * 64)
    stores.principal("holder", {"recordings": credential})
    stores.fake()
    config = json.loads((stores.root / "admin/config.json").read_text())
    config["recordings_config"] = str(stores.recordings_path)
    private(stores.root / "admin/config.json", config)
    private(stores.recordings_path, "{}")
    code, view, _ = listing(stores, capsys, "--strict")
    assert code == 1
    assert [(r["status"], r["note"]) for r in recordings_rows(view)] == [("unknown", "recordings_config_invalid")]
    assert view["findings"] == [{"code": "backend_unavailable", "backend": "recordings",
                                 "note": "recordings_config_invalid"}]


# spec: ANYM-772
@pytest.mark.parametrize("broken", [False, True], ids=["success", "failure"])
def test_no_recordings_hash_account_permission_or_raw_field_reaches_any_output(stores, capsys, caplog, broken):
    credential = stores.recordings_agent("agent-rec", expires_at={"raw": CANARY}, label=CANARY)
    stores.principal("agent", {"recordings": credential})
    stores.fake()
    if broken:
        stores.recordings_path.write_text(CANARY)
    before = stores.snapshot()
    configured = stores.root / "admin/config.json"
    assert admin.main(["--config", str(configured), "key", "list"]) == 0
    text = capsys.readouterr()
    assert admin.main(["--config", str(configured), "key", "list", "--json"]) == 0
    shown = capsys.readouterr()
    seen = text.out + text.err + shown.out + shown.err + caplog.text
    expected = "recordings_config_invalid" if broken else "agent-rec  expired"
    assert expected in text.out and stores.snapshot() == before
    row = json.loads(shown.out)["principals"][0]["credentials"][0]
    assert row == ({"backend": "recordings", "public_id": None, "expires_at": None, "status": "unknown",
                    "note": "recordings_config_invalid"} if broken else
                   {"backend": "recordings", "public_id": "agent-rec", "expires_at": None, "status": "expired"})
    for secret in (CANARY, credential["key_sha256"], stores.tokens["agent-rec"], "omi", "permissions", "accounts"):
        assert secret not in seen


JSON_VALUES = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=70),
                           lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=12), inner, max_size=3),
                           max_leaves=12)
ENTRIES = st.fixed_dictionaries({}, optional={
    "id": JSON_VALUES | st.text(max_size=140), "expires_at": JSON_VALUES | st.just(LATER),
    "key_sha256": JSON_VALUES | st.sampled_from(["a" * 64, "b" * 64])})
DOCUMENTS = JSON_VALUES | st.fixed_dictionaries({}, optional={"principals": JSON_VALUES | st.lists(ENTRIES | JSON_VALUES, max_size=4)})


# spec: ANYM-772
@settings(max_examples=200, deadline=None)
@given(DOCUMENTS)
def test_any_json_document_has_one_defined_outcome_and_only_validated_fields(document):
    now = admin.datetime.fromisoformat(NOW)
    with tempfile.TemporaryDirectory() as directory:
        path = private(Path(directory) / "config.json", document)
        found, note = admin.recordings_principals({"recordings_config": str(path)})
    assert note in (None, "recordings_config_invalid", "recordings_config_duplicate_key")
    assert (note is None) or found == {}
    for digest, entry in found.items():
        assert entry["key_sha256"] == digest
        row = admin.recordings_row(entry, now)
        assert set(row) == {"public_id", "expires_at", "status"}
        identity, moment = entry.get("id"), admin.instant(entry.get("expires_at"))
        assert (row["public_id"] is not None) == (isinstance(identity, str) and 1 <= len(identity) <= 128)
        assert row["public_id"] is None or row["public_id"] == identity
        printed = admin.safe_json(row)
        assert json.loads(printed) == row and all(part.isprintable() for part in printed.split("\n"))
        assert row["expires_at"] is None or row["expires_at"].isprintable()
        assert (admin.instant(row["expires_at"]) if row["expires_at"] else None) == moment  # the printed text round-trips
        assert row["status"] == ("live" if moment and moment > now else "expired")


# spec: ANYM-494
def test_a_key_file_that_does_not_match_the_policy_pin_is_a_content_free_finding_not_live(stores, capsys):
    healthy(stores)
    other = stores.calendar_agent("other-cal")  # a valid backend key, but not the one the policy pins
    stores.principal("agent", {"calendar": dict(stores.key("agent-cal"), key_file=other["key_file"])})
    stores.calendar_agent("agent-cal")
    _, view, out = listing(stores, capsys)
    row = next(r for r in view["principals"] if r["id"] == "agent")
    assert row["credentials"] == [{"backend": "calendar", "public_id": None, "expires_at": None,
                                   "status": "unknown", "note": "credential_pin_mismatch"}]
    assert {"code": "credential_pin_mismatch", "principal": "agent", "backend": "calendar"} in view["findings"]
    for value in stores.tokens.values():
        assert value not in out.out and sha(value) not in out.out


# spec: ANYM-494
@pytest.mark.parametrize("pin", ["é" * 64, "A" * 64, "0" * 63, None, 7], ids=["non-ascii", "upper", "short", "none",
                                                                          "number"])
def test_a_malformed_policy_pin_is_a_pin_mismatch_and_the_rest_still_lists(stores, capsys, pin):
    healthy(stores)
    stores.principal("agent", {"calendar": dict(stores.calendar_agent("agent-cal"), key_sha256=pin)})
    code, view, _ = listing(stores, capsys)
    assert code == 0 and [r["id"] for r in view["principals"]] == ["claude-code", "agent"]
    assert view["findings"] == [{"code": "credential_pin_mismatch", "principal": "agent", "backend": "calendar"},
                                {"code": "unbound_key", "backend": "calendar", "public_id": "agent-cal",
                                 "status": "live", "backend_principal": None}]


# spec: ANYM-494
def test_one_principal_holding_a_key_directly_and_as_its_calls_archive_is_not_shared(stores, capsys):
    stores.messaging_key("own", "00000000-0000-4000-8000-0000000000ee", "agent", bound=False)
    stores.principal("agent", {"messaging": stores.key("own"), "calls": stores.calls_agent("agent-calls", archive="own")})
    _, view, _ = listing(stores, capsys, "--strict")
    assert view["findings"] == []


# spec: ANYM-494
def test_a_failed_inventory_keeps_the_successful_lookups(stores, capsys):
    healthy(stores)
    _, view, _ = listing(stores, capsys, fail=("list-keys",))
    credentials = {c["backend"]: c for c in view["principals"][0]["credentials"]}
    assert credentials["messaging"]["status"] == "live" and credentials["calls"]["archive"]["status"] == "live"
    assert view["findings"] == [{"code": "backend_unavailable", "backend": "messaging",
                                 "detail": "messaging: list-keys exited 3"}]


# spec: ANYM-494
@pytest.mark.parametrize("interrupt", [False, True], ids=["timeout", "interrupt"])
def test_a_timed_out_or_interrupted_command_stops_only_its_own_container(stores, monkeypatch, interrupt):
    real, seen = admin.subprocess.run, []

    def run(argv, **options):
        seen.append(argv)
        if argv[0] == "docker":
            return admin.subprocess.CompletedProcess(argv, 0)
        if interrupt:
            raise KeyboardInterrupt
        return real(argv, **options)

    monkeypatch.setattr(admin, "TIMEOUT", 0.2)
    monkeypatch.setattr(admin.subprocess, "run", run)
    command = admin.runner({"calendar": [sys.executable, "-c", "import time; time.sleep(30)", "--name", "{name}"]})
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            command("calendar", ["list-agents"])
    else:
        with pytest.raises(admin.Unavailable, match="calendar: TimeoutExpired"):
            command("calendar", ["list-agents"])
    name = seen[0][-2]
    assert name.startswith("deedkit-admin-")
    assert seen[1] == ["docker", "stop", "--time", "5", name] and len(seen) == 2


# spec: ANYM-494
def test_a_private_file_that_is_oversized_or_changes_while_read_is_refused_never_truncated(tmp_path, monkeypatch):
    path = private(tmp_path / "key", "x" * 20)
    with pytest.raises(admin.AdminError, match="unsafe private file"):
        admin.read_private(path, 10)
    real, calls = admin.os.fstat, []

    def fstat(fd):
        info = real(fd)
        calls.append(fd)
        if len(calls) == 1:
            return info
        values = list(info)
        values[6] += 1  # st_size grew after the first look
        return admin.os.stat_result(values)

    monkeypatch.setattr(admin.os, "fstat", fstat)
    with pytest.raises(admin.AdminError, match="changed while read"):
        admin.read_private(path)


# spec: ANYM-494
def test_an_unavailable_backend_is_unknown_and_a_finding_never_a_crash(stores, capsys):
    healthy(stores)
    code, view, _ = listing(stores, capsys, broken=("calendar",))
    credentials = {c["backend"]: c for c in view["principals"][0]["credentials"]}
    assert credentials["calendar"]["status"] == "unknown" and credentials["messaging"]["status"] == "live"
    assert view["findings"] == [{"code": "backend_unavailable", "backend": "calendar",
                                 "detail": "calendar: agent-info exited 3"}]


# spec: ANYM-494
def test_list_is_read_only_and_no_token_or_digest_reaches_output_argv_or_environment(stores, capsys):
    healthy(stores)
    stores.fake()
    before = stores.snapshot()
    _, view, out = listing(stores, capsys)
    text = admin.main(["--config", str(stores.root / "admin/config.json"), "key", "list"])
    shown = capsys.readouterr()
    assert text == 0 and "claude-code" in shown.out
    assert stores.snapshot() == before
    calls = [json.loads(line) for line in (stores.root / "operator-state.json.calls").read_text().splitlines()]
    assert {tuple(c["argv"][1:]) for c in calls} == {("key-info", "--key-file", "/dev/stdin"), ("list-keys", "--all"),
                                                       ("agent-info", "--key-file", "/dev/stdin"), ("list-agents",)}
    seen = out.out + out.err + shown.out + shown.err + json.dumps(calls)
    for value in stores.tokens.values():
        for secret in (value, sha(value)):
            assert secret not in seen


# spec: ANYM-494
def test_label_lives_only_in_the_private_admin_file_and_shows_in_the_list(stores, capsys):
    healthy(stores)
    config = stores.fake()
    policy, registry = Path(stores.config["policy"]).read_bytes(), Path(stores.config["calls_registry"]).read_bytes()
    assert admin.main(["--config", str(config), "key", "label", "claude-code", "claude.ai connector"]) == 0
    capsys.readouterr()
    labels = Path(stores.config["labels"])
    assert labels.stat().st_mode & 0o777 == 0o600
    assert json.loads(labels.read_text()) == {"version": 1, "labels": {"claude-code": "claude.ai connector"}}
    _, view, _ = listing(stores, capsys)
    assert view["principals"][0]["label"] == "claude.ai connector"
    assert Path(stores.config["policy"]).read_bytes() == policy
    assert Path(stores.config["calls_registry"]).read_bytes() == registry
    for path in stores.root.rglob("*"):
        if path.is_file() and path != labels and not path.name.startswith("fake_operator"):
            assert "claude.ai connector" not in path.read_text(errors="replace")


# spec: ANYM-494
@pytest.mark.parametrize("principal,text", [("claude-code", ""), ("claude-code", "x" * 81),
                                            ("claude-code", "tab\there"), ("nobody", "a label")])
def test_label_refuses_bad_text_and_unknown_principals_and_writes_nothing(stores, capsys, principal, text):
    healthy(stores)
    config = stores.fake()
    assert admin.main(["--config", str(config), "key", "label", principal, text]) == 2
    assert not Path(stores.config["labels"]).exists()
    assert "deedkit admin:" in capsys.readouterr().err


# spec: ANYM-494
def test_the_deedkit_launcher_routes_admin_to_the_admin_cli(stores, capsys, monkeypatch):
    from scripts import deedkit

    healthy(stores)
    config = stores.fake()
    monkeypatch.syspath_prepend(str(Path(deedkit.__file__).parent))
    with pytest.raises(SystemExit) as done:
        deedkit.main(["admin", "--config", str(config), "key", "list", "--json"])
    assert done.value.code == 0
    assert [row["id"] for row in json.loads(capsys.readouterr().out)["principals"]] == ["claude-code"]



# spec: ANYM-520
def test_delivery_endpoints_list_shows_health_and_the_owners_key_label(tmp_path, capsys):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"version": 1, "labels": {"endpoint-agent": "Synthetic agent"}}))
    labels.chmod(0o600)
    config = tmp_path / "admin.json"
    config.write_text(json.dumps({"labels": str(labels)}))
    config.chmod(0o600)
    health = {"state": "failing", "consecutive_failures": 2, "last_success": None,
              "last_error": None, "next_step": "Fix the receiver"}
    def run(service, args, data=None):
        return {"items": [{"endpoint_id": "synthetic-1", "principal": "endpoint-agent", "key_id": "synthetic-key",
                           "delivery_health": health, "last_error": None},
                          {"endpoint_id": "synthetic-2", "principal": "unlabelled-agent", "key_id": "synthetic-key-2",
                           "delivery_health": {**health, "state": "ok", "consecutive_failures": 0}}],
                "endpoints_by_state": {"failing": 1, "ok": 1}}
    assert admin.main(["--config", str(config), "delivery-endpoints", "list"], run=run) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(item["principal"], item["key_label"]) for item in listed["items"]] == [
        ("endpoint-agent", "Synthetic agent"), ("unlabelled-agent", None)]
    assert listed["items"][0]["delivery_health"] == health and listed["endpoints_by_state"] == {"failing": 1, "ok": 1}


# spec: ANYM-519
def test_delivery_endpoints_list_disable_enable_run_the_messaging_operator_commands(tmp_path, capsys):
    config = tmp_path / "admin.json"
    config.write_text("{}")
    config.chmod(0o600)
    calls = []
    def run(service, args, data=None):
        calls.append((service, args))
        if args[0] == "delivery-endpoints-list":
            return {"items": [{"endpoint_id": "synthetic", "header_names": ["Authorization"]}]}
        return {"endpoint_id": args[-1], "state": "operator_disabled" if args[0].startswith("disable") else "enabled"}
    endpoint = "5f0c2a8e-2b8f-4c1d-9d36-0e6b1c1f9a01"
    base = ["--config", str(config), "delivery-endpoints"]
    assert admin.main(base + ["list", "--principal", "endpoint-agent"], run=run) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["header_names"] == ["Authorization"]
    assert admin.main(base + ["disable", endpoint], run=run) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "operator_disabled"
    assert admin.main(base + ["enable", endpoint.upper()], run=run) == 0
    assert calls == [("messaging", ["delivery-endpoints-list", "--principal", "endpoint-agent"]),
        ("messaging", ["disable-delivery-endpoint", "--id", endpoint]),
        ("messaging", ["enable-delivery-endpoint", "--id", endpoint])]
    # A malformed ID never reaches the operator command.
    assert admin.main(base + ["disable", "not-a-uuid"], run=run) == 2 and len(calls) == 3


# spec: ANYM-848
def test_wake_targets_list_disable_enable_run_the_messaging_operator_commands(tmp_path, capsys):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"version": 1, "labels": {"wake-agent": "Synthetic Codex"}}))
    labels.chmod(0o600)
    config = tmp_path / "admin.json"
    config.write_text(json.dumps({"labels": str(labels)}))
    config.chmod(0o600)
    calls = []
    def run(service, args, data=None):
        calls.append((service, args))
        if args[0] == "wake-targets-list":
            return {"items": [{"wake_target_id": "synthetic", "principal": "wake-agent", "state": "enabled",
                               "confirmed": True}], "targets_by_state": {"enabled": 1}, "unbound": 0}
        if args[-1] == "--all" or "--all" in args:
            return {"enabled": 2}
        return {"wake_target_id": args[-1], "state": "operator_disabled" if args[0].startswith("disable") else "enabled"}
    target = "6a1d3b9f-3c9a-4d2e-8e47-1f7c2d2a0b12"
    base = ["--config", str(config), "wake-targets"]
    assert admin.main(base + ["list", "--principal", "wake-agent"], run=run) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["items"][0]["key_label"] == "Synthetic Codex" and listed["targets_by_state"] == {"enabled": 1}
    assert admin.main(base + ["disable", target], run=run) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "operator_disabled"
    assert admin.main(base + ["enable", target.upper()], run=run) == 0
    capsys.readouterr()
    assert admin.main(base + ["enable", "--all", "--principal", "wake-agent"], run=run) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": 2}
    assert calls == [("messaging", ["wake-targets-list", "--principal", "wake-agent"]),
        ("messaging", ["disable-wake-target", "--id", target]),
        ("messaging", ["enable-wake-target", "--id", target]),
        ("messaging", ["enable-wake-target", "--all", "--principal", "wake-agent"])]
    # A malformed ID, a missing one and an ID with --all never reach the operator command.
    assert admin.main(base + ["disable", "not-a-uuid"], run=run) == 2
    assert admin.main(base + ["enable"], run=run) == 2
    assert admin.main(base + ["enable", target, "--all"], run=run) == 2 and len(calls) == 4


HOST = "a" * 32
CLAIM = {"account": "tg-main", "host_id": HOST, "boot_id": "boot-1", "lock_ref": "64:12", "holder_id": "b" * 32,
         "holder_kind": "collector", "generation": 3, "claimed_at": "2026-11-09T10:00:00+00:00",
         "seen_at": "2026-11-09T10:05:00+00:00", "released_at": None, "released_by": None, "restore_hold": False,
         "waiting_host_id": None, "waiting_since": None}


def claim_admin(tmp_path, listed, released=True):
    config = tmp_path / "admin.json"
    config.write_text("{}")
    config.chmod(0o600)
    calls = []
    def run(service, args, data=None):
        calls.append(args)
        if args[0] == "session-claim-list":
            return listed
        return {"account": args[2], "released": released}
    return ["--config", str(config), "session-claim"], run, calls


# spec: ANYM-914
def test_session_claim_list_is_read_only_and_release_compares_the_generation_and_holder_read(tmp_path, capsys):
    base, run, calls = claim_admin(tmp_path, {"items": [CLAIM], "restored_without_claim": []})
    assert admin.main(base + ["list"], run=run) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["generation"] == 3 and calls == [["session-claim-list"]]
    assert admin.main(base + ["release", "--account", "tg-main", "--other-host-down", HOST], run=run) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["released"] is True
    assert calls[-1] == ["session-claim-release", "--account", "tg-main", "--generation", "3", "--holder-id", "b" * 32,
                         "--host-id", HOST]
    assert "released account=tg-main" in captured.err and "boot-1" in captured.err


# spec: ANYM-914
def test_session_claim_release_with_a_stale_read_is_refused(tmp_path, capsys):
    base, run, calls = claim_admin(tmp_path, {"items": [CLAIM], "restored_without_claim": []}, released=False)
    assert admin.main(base + ["release", "--account", "tg-main", "--other-host-down", HOST], run=run) == 2
    assert "row changed since it was read" in capsys.readouterr().err and calls[-1][0] == "session-claim-release"


# spec: ANYM-914
def test_session_claim_release_clears_a_restore_hold_with_or_without_an_existing_row(tmp_path, capsys):
    held = dict(CLAIM, restore_hold=True, released_at="2026-11-09T09:00:00+00:00", released_by="holder")
    base, run, calls = claim_admin(tmp_path, {"items": [held], "restored_without_claim": []})
    assert admin.main(base + ["release", "--account", "tg-main", "--other-host-down", HOST], run=run) == 0
    assert calls[-1][:3] == ["session-claim-release", "--account", "tg-main"] and "--generation" in calls[-1]
    base, run, calls = claim_admin(tmp_path, {"items": [], "restored_without_claim": ["wa-main"]})
    assert admin.main(base + ["release", "--account", "wa-main", "--other-host-down", "wa-main"], run=run) == 0
    assert calls[-1] == ["session-claim-release", "--account", "wa-main"]
    capsys.readouterr()
    # Neither a row nor a restored account: nothing to release.
    assert admin.main(base + ["release", "--account", "other", "--other-host-down", "other"], run=run) == 2
    assert len(calls) == 3


# spec: ANYM-914
def test_session_claim_release_without_the_flag_refuses_on_a_non_tty_and_asks_on_a_tty(tmp_path, capsys, monkeypatch):
    base, run, calls = claim_admin(tmp_path, {"items": [CLAIM], "restored_without_claim": []})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert admin.main(base + ["release", "--account", "tg-main"], run=run) == 2
    assert "--other-host-down" in capsys.readouterr().err and calls == [["session-claim-list", "--account", "tg-main"]]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "wrong")
    assert admin.main(base + ["release", "--account", "tg-main"], run=run) == 2 and len(calls) == 2
    monkeypatch.setattr("builtins.input", lambda prompt="": HOST)
    assert admin.main(base + ["release", "--account", "tg-main"], run=run) == 0 and calls[-1][0] == "session-claim-release"


# spec: ANYM-914
def test_session_claim_release_with_a_mismatching_other_host_down_refuses_before_any_write(tmp_path, capsys):
    base, run, calls = claim_admin(tmp_path, {"items": [CLAIM], "restored_without_claim": []})
    assert admin.main(base + ["release", "--account", "tg-main", "--other-host-down", "c" * 32], run=run) == 2
    assert "does not match" in capsys.readouterr().err and [c[0] for c in calls] == ["session-claim-list"]
