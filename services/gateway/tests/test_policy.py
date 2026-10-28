import hashlib
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway import policy as policy_module
from agent_gateway.app import create_app
from agent_gateway.policy import Policy, Denied


def config():
    return {
        "version": 1,
        "revision": 1,
        "deployment": {
            "modules": ["calendar"],
            "backends": {"calendar": {"url": "http://calendar-api:13232"}},
        },
        "profiles": {
            "reader": {
                "tools": ["platform_capabilities", "calendar_calendars", "calendar_slots"],
                "actions": {},
            }
        },
        "principals": [
            {
                "id": "agent-a",
                "key_sha256": hashlib.sha256(b"gateway-a").hexdigest(),
                "profile": "reader",
                "enabled": True,
                "expires_at": "2099-01-01T00:00:00Z",
                "grants": {"calendar": ["agenda:read", "availability:read"]},
                "credentials": {
                    "calendar": {
                        "key_file": "/private/calendar-a.key",
                        "key_sha256": hashlib.sha256(b"backend-calendar-a").hexdigest(),
                    }
                },
                "actions": {},
            }
        ],
    }


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(config()))
    path.chmod(0o600)
    return Policy(path)


def test_authentication_and_exact_tools(policy):
    snap, agent = policy.authenticate("gateway-a")
    assert agent["id"] == "agent-a"
    assert policy.visible(snap, agent) == ["platform_capabilities", "calendar_calendars", "calendar_slots"]
    for key in ("", "gateway-b", "Bearer gateway-a"):
        with pytest.raises(Denied):
            policy.authenticate(key)


def test_revocation_applies_without_reconnect(policy):
    policy.authenticate("gateway-a")
    value = config()
    value["revision"] = 2
    value["principals"][0]["enabled"] = False
    policy.path.write_text(json.dumps(value))
    with pytest.raises(Denied):
        policy.authenticate("gateway-a")


def test_partial_or_reused_revision_fails_closed(policy):
    policy.authenticate("gateway-a")
    value = config()
    value["profiles"]["reader"]["tools"] = ["platform_capabilities"]
    policy.path.write_text(json.dumps(value))
    with pytest.raises(Denied):
        policy.authenticate("gateway-a")
    policy.path.write_text('{"revision":2')
    with pytest.raises(Denied):
        policy.authenticate("gateway-a")


def test_module_scope_and_exposure_all_required(policy):
    value = config()
    value["revision"] = 2
    value["principals"][0]["grants"]["calendar"] = ["agenda:read"]
    policy.path.write_text(json.dumps(value))
    snap, agent = policy.authenticate("gateway-a")
    assert "calendar_slots" not in policy.visible(snap, agent)
    value["revision"] = 3
    value["deployment"]["modules"] = []
    policy.path.write_text(json.dumps(value))
    snap, agent = policy.authenticate("gateway-a")
    assert policy.visible(snap, agent) == ["platform_capabilities"]


@pytest.mark.parametrize("name", ["*", "calendar_*", "execute", "unknown"])
def test_unknown_and_wildcard_exposure_rejected(policy, name):
    value = config()
    value["profiles"]["reader"]["tools"] = [name]
    policy.path.write_text(json.dumps(value))
    with pytest.raises(Denied):
        policy.authenticate("gateway-a")


def test_public_policy_and_symlink_rejected(policy, tmp_path):
    policy.path.chmod(0o644)
    with pytest.raises(Denied):
        policy.authenticate("gateway-a")
    policy.path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(policy.path)
    with pytest.raises(Denied):
        Policy(link).authenticate("gateway-a")


# spec: ANYM-394
def test_public_or_symlinked_policy_reports_policy_unavailable_503(policy, tmp_path):
    def backend(request):
        raise AssertionError("a refused policy never reaches a backend")

    app = create_app(policy.path, "http://testserver", transports={"calendar": httpx.MockTransport(backend)})
    headers = {"Authorization": "Bearer gateway-a"}
    with TestClient(app) as client:
        assert client.post("/v1/platform_capabilities", json={}, headers=headers).status_code == 200
        policy.path.chmod(0o644)
        for path in ("/v1/platform_capabilities", "/mcp"):
            response = client.post(path, json={}, headers=headers)
            assert (response.status_code, response.json()) == (503, {"error": "policy_unavailable"}), path
        target = tmp_path / "target.json"
        target.write_text(policy.path.read_text())
        target.chmod(0o600)
        policy.path.unlink()
        policy.path.symlink_to(target)
        response = client.post("/v1/platform_capabilities", json={}, headers=headers)
        assert (response.status_code, response.json()) == (503, {"error": "policy_unavailable"})


# spec: ANYM-394
def test_policy_larger_than_one_mib_is_refused(policy):
    raw = json.dumps(config())
    policy.path.write_text(raw + " " * (1024 * 1024 - len(raw)))
    assert Policy(policy.path).authenticate("gateway-a")[1]["id"] == "agent-a"
    policy.path.write_text(raw + " " * (1024 * 1024 + 1 - len(raw)))
    with pytest.raises(Denied) as refused:
        Policy(policy.path).authenticate("gateway-a")
    assert (refused.value.code, refused.value.status) == ("policy_unavailable", 503)


# spec: ANYM-394
@pytest.mark.parametrize("field", [None, "st_size", "st_mtime_ns", "st_ctime_ns"])
def test_policy_changed_while_read_is_refused(policy, monkeypatch, field):
    calls = []

    class Changing:
        def __getattr__(self, name):
            return getattr(os, name)

        def fstat(self, fd):
            value = os.fstat(fd)
            calls.append(fd)
            if len(calls) == 1 or field is None:
                return value
            # The second fstat, after the read, sees the file changed in one field.
            fields = {name: getattr(value, name) for name in ("st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}
            fields[field] += 1
            return SimpleNamespace(**fields)

    monkeypatch.setattr(policy_module, "os", Changing())
    if field is None:
        assert policy.authenticate("gateway-a")[1]["id"] == "agent-a"
    else:
        with pytest.raises(Denied) as refused:
            policy.authenticate("gateway-a")
        assert (refused.value.code, refused.value.status) == ("policy_unavailable", 503)
    assert len(calls) == 2
