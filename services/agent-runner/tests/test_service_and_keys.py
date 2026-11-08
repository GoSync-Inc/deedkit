import json
import os
import time
import uuid

import pytest
from starlette.testclient import TestClient

from deedkit_agent_runner import config as cfg
from deedkit_agent_runner.app import PeerGuard, create_app

from support import PEER, auth, document, fields, sha, write_config, write_private


def body(**overrides):
    value = fields(**overrides)
    value.pop("continue_task_id")
    return value


# spec: ANYM-819
def test_a_forged_principal_field_is_refused_and_nothing_is_admitted(client, runner):
    response = client.post("/v1/tasks", json={**body(), "principal": "orchestrator-b"}, headers=auth("a_run"))
    assert response.status_code == 422 and response.json()["error"] == "invalid_request"
    assert "principal" in response.json()["fields"]
    assert runner.store.rows("queued", "starting", "running", "succeeded") == []


# spec: ANYM-819
def test_a_read_key_cannot_start_or_stop(client, runner):
    response = client.post("/v1/tasks", json=body(), headers=auth("a_read"))
    assert response.status_code == 403 and response.json()["error"] == "forbidden"
    created = client.post("/v1/tasks", json=body(), headers=auth("a_run")).json()
    assert client.post(f"/v1/tasks/{created['id']}/stop", headers=auth("a_read")).status_code == 403
    assert runner.store.task(created["id"])["state"] == "queued"
    # The same principal's read key sees the task.
    assert client.get(f"/v1/tasks/{created['id']}", headers=auth("a_read")).status_code == 200


# spec: ANYM-819
def test_principal_b_cannot_see_or_stop_principal_a_task(client, runner):
    created = client.post("/v1/tasks", json=body(), headers=auth("a_run")).json()
    for path in (f"/v1/tasks/{created['id']}", f"/v1/tasks/{created['id']}/log",
                 f"/v1/tasks?request_id={created['request_id']}"):
        response = client.get(path, headers=auth("b_run"))
        assert response.status_code == 404, path
    assert client.post(f"/v1/tasks/{created['id']}/stop", headers=auth("b_run")).status_code == 404
    assert runner.store.task(created["id"])["state"] == "queued"
    assert client.get(f"/v1/tasks?request_id={created['request_id']}", headers=auth("a_run")).json()["id"] == \
        created["id"]


# spec: ANYM-819
@pytest.mark.parametrize("header", [{"authorization": "Bearer key-revoked-0001"},
                                    {"authorization": "Bearer key-never-issued"}, {}])
def test_revoked_or_unknown_keys_get_401_and_nothing_changes(client, runner, header):
    for method, path in (("post", "/v1/tasks"), ("get", "/v1/capabilities"), ("get", f"/v1/tasks/{uuid.uuid4()}")):
        response = getattr(client, method)(path, json=body(), headers=header) if method == "post" else \
            getattr(client, method)(path, headers=header)
        assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
    assert runner.store.rows("queued") == []


# spec: ANYM-819
def test_removing_a_key_from_the_private_file_revokes_it_without_a_restart(client, runner, tmp_path):
    assert client.get("/v1/capabilities", headers=auth("b_run")).status_code == 200
    data = document(tmp_path)
    data["keys"] = [k for k in data["keys"] if k["key_id"] != "b-run"]
    time.sleep(0.01)
    write_private(runner.config_path, data)
    assert client.get("/v1/capabilities", headers=auth("b_run")).status_code == 401
    assert client.get("/v1/capabilities", headers=auth("a_run")).status_code == 200
    # A broken file authorizes nobody until it is valid again.
    write_private(runner.config_path, "{not json")
    assert client.get("/v1/capabilities", headers=auth("a_run")).status_code == 401


# spec: ANYM-819
def test_a_revocation_made_while_the_service_starts_holds_at_the_first_request(tmp_path):
    path, config = write_config(tmp_path)  # serve loaded the config here and is running its startup checks
    data = document(tmp_path)
    data["keys"] = [k for k in data["keys"] if k["key_id"] != "b-run"]
    write_private(path, data)
    keys = cfg.Keys(path)  # built after the change
    assert keys.get(sha("key-b-run-0001")) is None
    assert keys.get(sha("key-a-run-0001")).principal == "orchestrator-a"
    assert any(k.key_id == "b-run" for k in config.keys)  # the earlier load still had it


# spec: ANYM-819
def test_only_production_peer_is_answered_on_the_tailnet_listener(runner):
    app = PeerGuard(create_app(runner, cfg.Keys(runner.config_path)), [PEER])
    for peer in ("100.100.1.3", "127.0.0.1", "192.168.1.10"):
        with TestClient(app, client=(peer, 50000)) as other:
            response = other.get("/v1/capabilities", headers=auth("a_run"))
            assert response.status_code == 403 and response.json() == {"error": "peer_refused"}, peer
    loopback = PeerGuard(create_app(runner, cfg.Keys(runner.config_path)), ["127.0.0.1", "::1"])
    with TestClient(loopback, client=("127.0.0.1", 50000)) as local:
        assert local.get("/v1/capabilities", headers=auth("a_read")).status_code == 200
    with TestClient(loopback, client=(PEER, 50000)) as remote:
        assert remote.get("/v1/capabilities", headers=auth("a_read")).status_code == 403


# spec: ANYM-819
@pytest.mark.parametrize("listen, code", [
    ({"tailnet_host": "0.0.0.0"}, "bind_must_be_the_tailnet_address"),
    ({"tailnet_host": "192.168.1.5"}, "bind_must_be_the_tailnet_address"),
    ({"port": 443}, "bind_port_must_be_8448"),
    ({"loopback_host": "0.0.0.0"}, "cli_bind_must_be_loopback"),
    ({"loopback_host": "100.101.102.103"}, "cli_bind_must_be_loopback"),
])
def test_any_other_bind_refuses_to_start(tmp_path, listen, code):
    data = document(tmp_path)
    data["listen"].update(listen)
    path = write_private(tmp_path / "bad.json", data)
    with pytest.raises(cfg.ConfigError) as refused:
        cfg.load(path)
    assert code in str(refused.value)


# spec: ANYM-819
def test_the_config_file_is_read_with_the_private_file_rules(tmp_path):
    path, _ = write_config(tmp_path)
    os.chmod(path, 0o640)
    with pytest.raises(cfg.ConfigError, match="private_file_refused"):
        cfg.load(path)
    os.chmod(path, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(cfg.ConfigError):
        cfg.load(link)
    data = document(tmp_path)
    data["production_peer"] = "203.0.113.9"
    with pytest.raises(cfg.ConfigError, match="production_peer_must_be_a_tailnet_address"):
        cfg.load(write_private(tmp_path / "peer.json", data))
    data = document(tmp_path)
    data["keys"][1]["operations"] = ["run"]
    with pytest.raises(cfg.ConfigError, match="operations_must_be_read_or_read_run"):
        cfg.load(write_private(tmp_path / "ops.json", data))
    data = document(tmp_path)
    data["keys"].append({**data["keys"][0], "key_id": "dup"})
    with pytest.raises(cfg.ConfigError, match="duplicate_key"):
        cfg.load(write_private(tmp_path / "dup.json", data))


# spec: ANYM-819
def test_the_example_config_validates(tmp_path):
    example = json.loads((cfg.Path(__file__).resolve().parents[3] / "deploy" / "agent-runner.example.json").read_text())
    path = write_private(tmp_path / "example.json", example)
    loaded = cfg.load(path)
    assert loaded.listen.port == 8448 and loaded.daily_starts == 10
    assert all(k.sha256 != sha("") for k in loaded.keys)
