"""Staged uploads mounted read-only into collectors are data, not configuration, for the kept-service digest (ANYM-895).

The messaging API writes the staged-upload root (`uploads.py`: `PROBE_FILES/.uploads`) while collectors mount it
read-only to send staged files. Every other read-only mounted directory, file, env file and secret stays an input
hashed by content, and prepare/apply here are the real ones over the synthetic host of test_production.
"""
import json
from pathlib import Path
import re
import shutil

import pytest

from scripts import production as p
from test_production import RELEASES, KeepHost, keep_outcomes, no_apply_settle, no_host_state  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
UPLOADS = "/data/files/.uploads"
WITH_UPLOADS = ("telegram", "whatsapp1")


def staged_host(tmp_path, monkeypatch):
    """KeepHost whose collectors read a secret and a read-only configuration directory, and two of them the
    staged-upload root read-only, as compose.collectors.yaml and compose.whatsapp.yaml mount it."""
    host = KeepHost(tmp_path, monkeypatch)
    paths = {name: tmp_path / "host" / name for name in ("uploads", "configuration", "older", "secret", "conf")}
    for name in ("uploads", "configuration", "older"):
        paths[name].mkdir(parents=True)
    (paths["configuration"] / "policy.json").write_text("{}\n")
    (paths["older"] / "note.txt").write_text("synthetic\n")
    (paths["uploads"] / "first").mkdir()
    (paths["uploads"] / "first" / "part").write_bytes(b"synthetic upload bytes")
    for name in ("secret", "conf"):
        paths[name].write_text("synthetic\n")
    for service in ("telegram", "whatsapp1", "whatsapp2"):
        volumes = [{"type": "bind", "source": str(paths["configuration"]), "target": "/run/deedkit/configuration",
                    "read_only": True},
                   {"type": "bind", "source": str(paths["older"]), "target": "/data/files/.uploads-older", "read_only": True},
                   {"type": "bind", "source": str(paths["conf"]), "target": "/run/deedkit/synthetic.conf", "read_only": True}]
        if service in WITH_UPLOADS:
            volumes.append({"type": "bind", "source": str(paths["uploads"]), "target": UPLOADS, "read_only": True})
        host.units[service].update({"volumes": volumes, "secrets": [{"source": "token"}]})
    host.write_compose = lambda: host.original.write_text(json.dumps(
        {"name": "synthetic", "services": {**host.units, "db": {"image": "postgres:pinned"}},
         "secrets": {"token": {"file": str(paths["secret"])}}}))
    host.write_compose()
    return host, paths


def churn(uploads):
    """An upload created, one completed and one removed, as uploads.py stages them."""
    (uploads / "second").mkdir(mode=0o700)
    (uploads / "second" / "part").write_bytes(b"x" * 4096)
    (uploads / "second" / "part").rename(uploads / "second" / "complete")
    shutil.rmtree(uploads / "first")


# spec: ANYM-895
def test_uploads_staged_while_a_release_is_prepared_neither_refuse_apply_nor_recreate_the_collectors(tmp_path, monkeypatch):
    host, paths = staged_host(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    churn(paths["uploads"])  # between two releases: still the same inputs
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    (paths["uploads"] / "third").mkdir(mode=0o700)  # after prepare, before apply's check
    (paths["uploads"] / "third" / "part").write_bytes(b"y")
    before, host.order = host.ids(), []
    result = p.apply(host.settings, RELEASES[1])
    assert host.stopped() == ["gateway"]
    assert keep_outcomes(result) == {"gateway": "recreated", "telegram": "kept", "whatsapp1": "kept", "whatsapp2": "kept"}
    assert {s: host.ids()[s] for s in ("telegram", "whatsapp1", "whatsapp2")} == {
        s: before[s] for s in ("telegram", "whatsapp1", "whatsapp2")}


# spec: ANYM-895
@pytest.mark.parametrize("change", ["configuration directory", "configuration mode", "similar read-only directory",
                                    "mounted file", "secret file", "uploads replaced by a file",
                                    "uploads missing", "uploads unreadable", "uploads mount source"])
def test_configuration_secret_and_upload_mount_changes_after_prepare_still_refuse_apply(tmp_path, monkeypatch, change):
    host, paths = staged_host(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    message = "inputs_changed_since_prepare"
    if change == "configuration directory":
        (paths["configuration"] / "policy.json").write_text('{"changed": true}\n')
    elif change == "configuration mode":
        (paths["configuration"] / "policy.json").chmod(0o600)
    elif change == "similar read-only directory":  # only the staged-upload root itself is data
        (paths["older"] / "note.txt").write_text("synthetic2\n")
    elif change == "mounted file":
        paths["conf"].write_text("synthetic2\n")
    elif change == "secret file":
        paths["secret"].write_text("synthetic2\n")
    elif change == "uploads replaced by a file":
        shutil.rmtree(paths["uploads"])
        paths["uploads"].write_text("not a directory\n")
    elif change == "uploads missing":
        shutil.rmtree(paths["uploads"])
    elif change == "uploads unreadable":
        if p.os.geteuid() == 0:
            pytest.skip("root ignores the permission bits that make the upload root unreadable")
        paths["uploads"].parent.chmod(0o000)
    else:  # the bind now names another host directory with the same content: the rendered service changed
        moved = paths["uploads"].with_name("uploads-moved")
        shutil.copytree(paths["uploads"], moved)
        for service in WITH_UPLOADS:
            next(v for v in host.units[service]["volumes"] if v["target"] == UPLOADS)["source"] = str(moved)
        host.write_compose()
        message = "inputs_changed_since_prepare|configuration changed since preparation"
    host.order = []
    try:
        with pytest.raises(p.Refused, match=message):
            p.apply(host.settings, RELEASES[1])
    finally:
        paths["uploads"].parent.chmod(0o755)
    assert host.order == [] and not (host.state / RELEASES[1] / "attempted.json").exists()


# spec: ANYM-895
def test_the_rule_change_recreates_only_the_upload_mounting_collectors_once_and_refuses_a_receipt_prepared_before_it(
        tmp_path, monkeypatch):
    host, paths = staged_host(tmp_path, monkeypatch)
    previous = staticmethod(lambda volume: bool(volume.get("read_only")))  # the rule before: every read-only directory
    with monkeypatch.context() as old:
        old.setattr(p, "mounted_configuration", previous)
        host.release(RELEASES[0])
        host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
        host.main = RELEASES[1]
        p.prepare(host.settings, RELEASES[1])
    # A receipt prepared under the old rule records content digests: the new tool refuses it before any stop.
    host.order = []
    with pytest.raises(p.Refused, match="inputs_changed_since_prepare"):
        p.apply(host.settings, RELEASES[1])
    assert host.order == []
    # Prepared again under the new rule: the INPUT_VERSION is unchanged, so only the services whose digest the rule
    # changed (the staged-upload mounts) are recreated, never kept on a stale label, and every other service stays.
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 3\n")
    host.main = RELEASES[2]
    p.prepare(host.settings, RELEASES[2])
    result = p.apply(host.settings, RELEASES[2])
    assert keep_outcomes(result) == {"gateway": "recreated", "telegram": "recreated", "whatsapp1": "recreated",
                                     "whatsapp2": "kept"}
    assert {s: result["services"]["synthetic"][s]["recreated"] for s in WITH_UPLOADS} == dict.fromkeys(WITH_UPLOADS, ["mounts"])
    assert p.INPUT_VERSION == "i1:"


# spec: ANYM-895
def test_the_apply_input_check_ignores_upload_churn_and_still_refuses_a_changed_env_file(tmp_path):
    # The synthetic KeepHost Compose does not resolve env files, so the env part goes through check_inputs directly.
    uploads, env = tmp_path / "uploads", tmp_path / "telegram.env"
    uploads.mkdir()
    (uploads / "first").mkdir()
    (uploads / "first" / "part").write_bytes(b"synthetic upload bytes")
    env.write_text("SYNTHETIC=1\n")
    document = {"services": {"telegram": {"env_file": [str(env)], "volumes": [
        {"type": "bind", "source": str(uploads), "target": UPLOADS, "read_only": True}]}}}
    project, item = {"name": "synthetic"}, {"inputs": {"telegram": p.service_inputs(document, "telegram", {})}}
    assert item["inputs"]["telegram"]["digest"] is not None
    churn(uploads)
    p.check_inputs(project, item, document)
    env.write_text("SYNTHETIC=2\n")
    with pytest.raises(p.Refused, match="inputs_changed_since_prepare"):
        p.check_inputs(project, item, document)


# spec: ANYM-895
def test_the_staged_upload_target_is_the_one_every_compose_file_and_the_store_declare():
    assert p.STAGED_UPLOADS == UPLOADS
    targets, files = set(), set()
    for compose in sorted((ROOT / "deploy").glob("compose*.yaml")):
        text = compose.read_text()
        for target, mode in re.findall(r"- file_uploads:(\S+?)(:ro)?\s*$", text, re.M):
            targets.add(target)
        files |= set(re.findall(r"PROBE_FILES: (\S+)", text))
    assert targets == {UPLOADS} and files == {str(Path(UPLOADS).parent)}
    assert "root=store.files/'.uploads'" in (ROOT / "services/messaging/messaging_service/uploads.py").read_text()
